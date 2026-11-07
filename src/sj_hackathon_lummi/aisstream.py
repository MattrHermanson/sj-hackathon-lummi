"""Background ingestor for AISstream.io.

Opens one WebSocket to AISstream for the lifetime of the FastAPI app, converts
each message to a row for the `ais_positions` table, and inserts rows in
batches. Reconnects automatically with exponential backoff.

Configuration (environment variables):
    AISSTREAM_API_KEY        Required. Free key from https://aisstream.io
    AIS_BOUNDING_BOXES       Optional JSON, [[[lat, lon], [lat, lon]], ...]
                             (each box is two opposite corners).
                             Default covers Bellingham Bay / Cherry Point.
    AIS_MIN_INTERVAL_SECONDS Optional. Keep at most one position per vessel per
                             this many seconds. Default 0 (keep everything).
    AIS_STREAM_ENABLED       Optional. Set to "false" to disable (default true).

Install:  uv add websockets
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import websockets

log = logging.getLogger("ais_stream")

STREAM_URL = "wss://stream.aisstream.io/v0/stream"

# [[lat, lon], [lat, lon]] corner pairs. Covers Bellingham Bay, the Lummi
# reservation and Cherry Point. Override with AIS_BOUNDING_BOXES.
DEFAULT_BOXES = [[[48.60, -122.95], [48.95, -122.40]]]

MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport", "ShipStaticData"]

BATCH_SIZE = 200
FLUSH_SECONDS = 5.0
QUEUE_MAX = 10_000
STATIC_CACHE_MAX = 50_000
LIVE_RETENTION_SECONDS = 3600  # forget vessels not heard from for an hour

INSERT_SQL = """
INSERT INTO ais_positions (
    mmsi, base_datetime, lat, lon, sog, cog, heading, vessel_name, imo,
    call_sign, vessel_type, status, length, width, draft, cargo, transceiver_class
) VALUES (
    %(mmsi)s, %(base_datetime)s, %(lat)s, %(lon)s, %(sog)s, %(cog)s, %(heading)s,
    %(vessel_name)s, %(imo)s, %(call_sign)s, %(vessel_type)s, %(status)s,
    %(length)s, %(width)s, %(draft)s, %(cargo)s, %(transceiver_class)s
)
ON CONFLICT DO NOTHING
"""


# ---------------------------------------------------------------------------
# Message parsing (pure functions, no I/O)
# ---------------------------------------------------------------------------
def clean_text(value) -> str | None:
    """AIS text fields are padded with spaces or '@' characters."""
    if value is None:
        return None
    text = str(value).strip().strip("@").strip()
    return text or None


def parse_time_utc(value) -> datetime:
    """Parse '2022-12-29 18:22:32.318353 +0000 UTC' to a naive UTC datetime.

    The table column is TIMESTAMP (no time zone), so naive UTC is what we store.
    Falls back to the current time if the field is missing or malformed.
    """
    try:
        date_part, time_part = str(value).split()[:2]
        if "." in time_part:  # the source can send nanoseconds; keep microseconds
            whole, frac = time_part.split(".")
            time_part = f"{whole}.{frac[:6]}"
        return datetime.fromisoformat(f"{date_part} {time_part}")
    except Exception:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_message(msg: dict, static: dict[str, dict]) -> dict | None:
    """Convert one AISstream message to an ais_positions row.

    ShipStaticData messages update the `static` cache and return None. Position
    messages return a row, enriched with whatever static data we have for the
    vessel. Anything unusable returns None.
    """
    mtype = msg.get("MessageType")
    meta = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get(mtype) or {}

    mmsi_raw = body.get("UserID") or meta.get("MMSI")
    if not mmsi_raw:
        return None
    mmsi = str(mmsi_raw)

    if mtype == "ShipStaticData":
        dim = body.get("Dimension") or {}
        a, b, c, d = (dim.get(k) or 0 for k in "ABCD")
        if len(static) >= STATIC_CACHE_MAX:
            static.clear()
        static[mmsi] = {
            "vessel_name": clean_text(body.get("Name")),
            "imo": str(body["ImoNumber"]) if body.get("ImoNumber") else None,
            "call_sign": clean_text(body.get("CallSign")),
            "vessel_type": body.get("Type") or None,
            "length": float(a + b) or None,
            "width": float(c + d) or None,
            "draft": body.get("MaximumStaticDraught") or None,
        }
        return None

    if mtype not in ("PositionReport", "StandardClassBPositionReport"):
        return None

    lat = body.get("Latitude", meta.get("latitude"))
    lon = body.get("Longitude", meta.get("longitude"))
    if lat is None or lon is None:
        return None
    # 91 / 181 are the AIS "not available" values
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    sog = body.get("Sog")
    cog = body.get("Cog")
    heading = body.get("TrueHeading")

    info = static.get(mmsi, {})
    return {
        "mmsi": mmsi,
        "base_datetime": parse_time_utc(meta.get("time_utc")),
        "lat": lat,
        "lon": lon,
        # AIS sentinel values for "not available" become NULL
        "sog": None if sog is None or sog >= 102.3 else sog,
        "cog": None if cog is None or cog >= 360 else cog,
        "heading": None if heading is None or heading >= 511 else heading,
        "vessel_name": clean_text(meta.get("ShipName")) or info.get("vessel_name"),
        "imo": info.get("imo"),
        "call_sign": info.get("call_sign"),
        "vessel_type": info.get("vessel_type"),
        "status": body.get("NavigationalStatus") if mtype == "PositionReport" else None,
        "length": info.get("length"),
        "width": info.get("width"),
        "draft": info.get("draft"),
        "cargo": None,
        "transceiver_class": "A" if mtype == "PositionReport" else "B",
    }


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------
_TICK = object()  # wakes the writer so it can flush on a timer


class AISStreamIngestor:
    def __init__(self, pool, api_key: str, boxes: list, min_interval: float = 0.0):
        self.pool = pool
        self.api_key = api_key
        self.boxes = boxes
        self.min_interval = min_interval

        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.static: dict[str, dict] = {}
        self._last_kept: dict[str, float] = {}
        # Most recent position per vessel, for the live map: mmsi -> (unix time, row)
        self.latest: dict[str, tuple[float, dict]] = {}
        self._last_flush = time.monotonic()
        self._reader: asyncio.Task | None = None
        self._writer: asyncio.Task | None = None

        self.connected = False
        self.received = 0
        self.queued = 0
        self.inserted = 0
        self.dropped = 0
        self.last_message_at: str | None = None

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        self._writer = asyncio.create_task(self._write_loop(), name="ais-writer")
        self._reader = asyncio.create_task(self._read_loop(), name="ais-reader")
        log.info("AISstream ingestor started (boxes=%s)", self.boxes)

    async def stop(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        # Tell the writer to flush what is left and exit
        await self.queue.put(None)
        if self._writer:
            try:
                await asyncio.wait_for(self._writer, timeout=10)
            except asyncio.TimeoutError:
                log.warning("AIS writer did not finish flushing in time")
        self.connected = False
        log.info("AISstream ingestor stopped")

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "received": self.received,
            "queued": self.queued,
            "inserted": self.inserted,
            "dropped": self.dropped,
            "last_message_at": self.last_message_at,
            "queue_size": self.queue.qsize(),
            "live_vessels": len(self.latest),
        }

    # -- live view ------------------------------------------------------------
    def live_vessels(self, max_age_seconds: float = 900) -> list[dict]:
        """Latest known position of every vessel heard from recently."""
        now = time.time()
        vessels = []
        for received_at, row in list(self.latest.values()):
            age = now - received_at
            if age > max_age_seconds:
                continue
            vessels.append(
                {
                    "mmsi": row["mmsi"],
                    "vessel_name": row["vessel_name"],
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "sog": row["sog"],
                    "cog": row["cog"],
                    "heading": row["heading"],
                    "vessel_type": row["vessel_type"],
                    "status": row["status"],
                    "length": row["length"],
                    "width": row["width"],
                    "base_datetime": row["base_datetime"].isoformat() + "Z",
                    "age_seconds": round(age),
                }
            )
        return vessels

    def _prune_latest(self) -> None:
        cutoff = time.time() - LIVE_RETENTION_SECONDS
        stale = [m for m, (t, _) in self.latest.items() if t < cutoff]
        for m in stale:
            del self.latest[m]

    # -- reading from the websocket -----------------------------------------
    async def _read_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(STREAM_URL, ping_interval=20) as ws:
                    # The subscription must be sent within 3 seconds of connecting
                    await ws.send(
                        json.dumps(
                            {
                                "APIKey": self.api_key,
                                "BoundingBoxes": self.boxes,
                                "FilterMessageTypes": MESSAGE_TYPES,
                            }
                        )
                    )
                    async for raw in ws:
                        if not self.connected:
                            log.info("AISstream connected and receiving data")
                        self.connected = True
                        backoff = 1.0
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "AISstream connection problem: %s (retry in %.0fs)", exc, backoff
                )

            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _handle(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return

        # Errors arrive as a JSON object, e.g. {"error": "Api Key Is Not Valid"}
        if "error" in msg:
            raise RuntimeError(f"AISstream error: {msg['error']}")

        self.received += 1
        row = parse_message(msg, self.static)
        if row is None:
            return

        # Update the live view before throttling, so it always shows the newest position
        self.latest[row["mmsi"]] = (time.time(), row)

        if self.min_interval > 0:
            now = time.monotonic()
            last = self._last_kept.get(row["mmsi"])
            if last is not None and now - last < self.min_interval:
                return
            self._last_kept[row["mmsi"]] = now
            if len(self._last_kept) > STATIC_CACHE_MAX:
                self._last_kept.clear()

        try:
            self.queue.put_nowait(row)
            self.queued += 1
            self.last_message_at = datetime.now(timezone.utc).isoformat()
        except asyncio.QueueFull:
            self.dropped += 1  # the database can't keep up

    # -- writing to the database --------------------------------------------
    async def _write_loop(self) -> None:
        batch: list[dict] = []
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=FLUSH_SECONDS)
            except asyncio.TimeoutError:
                item = _TICK

            if item is None:  # shutdown
                await self._flush(batch)
                return
            if item is not _TICK:
                batch.append(item)

            due = time.monotonic() - self._last_flush >= FLUSH_SECONDS
            if batch and (len(batch) >= BATCH_SIZE or due):
                await self._flush(batch)
                batch = []

    async def _flush(self, batch: list[dict]) -> None:
        self._last_flush = time.monotonic()
        self._prune_latest()
        if not batch:
            return
        try:
            # psycopg's pool is blocking, so keep it off the event loop
            await asyncio.to_thread(self._insert, batch)
            self.inserted += len(batch)
        except Exception:
            self.dropped += len(batch)
            log.exception("Failed to insert %d AIS rows", len(batch))

    def _insert(self, batch: list[dict]) -> None:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(INSERT_SQL, batch)


# ---------------------------------------------------------------------------
# Factory used by the FastAPI lifespan
# ---------------------------------------------------------------------------
def create_ingestor(pool) -> AISStreamIngestor | None:
    """Build an ingestor from environment variables, or None if disabled."""
    if os.environ.get("AIS_STREAM_ENABLED", "true").lower() in ("0", "false", "no"):
        log.info("AISstream ingestion disabled by AIS_STREAM_ENABLED")
        return None

    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        log.warning("AISSTREAM_API_KEY is not set; live AIS ingestion is off")
        return None

    boxes = DEFAULT_BOXES
    if os.environ.get("AIS_BOUNDING_BOXES"):
        boxes = json.loads(os.environ["AIS_BOUNDING_BOXES"])

    min_interval = float(os.environ.get("AIS_MIN_INTERVAL_SECONDS", "0"))
    return AISStreamIngestor(pool, api_key, boxes, min_interval)
