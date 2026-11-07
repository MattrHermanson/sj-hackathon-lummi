import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import AliasChoices, BaseModel, Field, field_validator

from sj_hackathon_lummi.aisstream import create_ingestor

app = FastAPI()

STATIC_DIR = Path(__file__).parent / "static"

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/lummi-db"
INGEST_API_KEY = "1234567890"


@app.get("/api/health")
def health():
    return {"status": "ok"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    pool.open()
    app.state.pool = pool

    ingestor = create_ingestor(pool)
    app.state.ais_ingestor = ingestor
    if ingestor:
        await ingestor.start()

    yield

    if ingestor:
        await (
            ingestor.stop()
        )  # flushes buffered rows; must happen before the pool closes
    pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/api/ais/status")
def ais_status(request: Request):
    ingestor = request.app.state.ais_ingestor
    return ingestor.status() if ingestor else {"enabled": False}


NEARBY_SQL = """
SELECT
    id, mmsi, base_datetime, lat, lon, sog, cog, heading,
    vessel_name, call_sign, vessel_type, status,
    ST_Distance(
        geom::geography,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
    ) / 1000.0 AS distance_km
FROM ais_positions
WHERE
    geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)
    AND ST_DWithin(
        geom::geography,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
        %(radius_m)s
    )
    AND (%(start)s::timestamp IS NULL OR base_datetime >= %(start)s::timestamp)
    AND (%(end)s::timestamp   IS NULL OR base_datetime <= %(end)s::timestamp)
ORDER BY distance_km
LIMIT %(limit)s
"""


@app.get("/api/vessels/nearby")
def vessels_nearby(
    request: Request,
    lat: Annotated[float, Query(ge=-90, le=90, description="Center latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Center longitude")],
    radius_km: Annotated[float, Query(gt=0, le=500, description="Search radius in km")],
    start: Annotated[
        datetime | None, Query(description="Earliest time (ISO 8601)")
    ] = None,
    end: Annotated[datetime | None, Query(description="Latest time (ISO 8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
):
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start must be before end")

    deg_lat = radius_km / 111.0
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    deg_lon = radius_km / (111.0 * cos_lat)

    params = {
        "lat": lat,
        "lon": lon,
        "radius_m": radius_km * 1000.0,
        "limit": limit,
        "min_lat": lat - deg_lat,
        "max_lat": lat + deg_lat,
        "min_lon": lon - deg_lon,
        "max_lon": lon + deg_lon,
        "start": start,
        "end": end,
    }

    with request.app.state.pool.connection() as conn:
        rows = conn.execute(NEARBY_SQL, params).fetchall()

    return {
        "center": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "start": start,
        "end": end,
        "count": len(rows),
        "results": rows,
    }


PATH_SQL = """
SELECT mmsi, vessel_name, base_datetime, lat, lon, sog
FROM ais_positions
WHERE mmsi = ANY(%(mmsis)s)
  AND (%(start)s::timestamp IS NULL OR base_datetime >= %(start)s::timestamp)
  AND (%(end)s::timestamp   IS NULL OR base_datetime <= %(end)s::timestamp)
ORDER BY mmsi, base_datetime
LIMIT %(limit)s
"""


@app.get("/api/vessels/path")
def vessel_paths(
    request: Request,
    mmsi: Annotated[
        list[str],
        Query(min_length=1, max_length=200, description="Repeat for each vessel"),
    ],
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500000)] = 100000,
):
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start must be before end")

    params = {"mmsis": mmsi, "start": start, "end": end, "limit": limit}

    with request.app.state.pool.connection() as conn:
        rows = conn.execute(PATH_SQL, params).fetchall()

    # Rows arrive sorted by (mmsi, time), so group them in one pass
    paths: dict[str, dict] = {}
    for r in rows:
        p = paths.setdefault(
            r["mmsi"],
            {"mmsi": r["mmsi"], "vessel_name": r["vessel_name"], "points": []},
        )
        if p["vessel_name"] is None and r["vessel_name"]:
            p["vessel_name"] = r["vessel_name"]
        # Compact array form keeps the payload small: [lat, lon, time, sog]
        p["points"].append([r["lat"], r["lon"], r["base_datetime"], r["sog"]])

    return {
        "count": len(paths),
        "truncated": len(rows) >= limit,
        "paths": list(paths.values()),
    }


BOUNDARIES_SQL = """
SELECT json_build_object(
    'type', 'FeatureCollection',
    'features', COALESCE(json_agg(json_build_object(
        'type', 'Feature',
        'properties', json_build_object('name', name, 'description', description),
        'geometry', ST_AsGeoJSON(geom, 6)::json
    )), '[]'::json)
) AS fc
FROM boundaries
"""


@app.get("/api/boundaries")
def boundaries(request: Request):
    with request.app.state.pool.connection() as conn:
        return conn.execute(BOUNDARIES_SQL).fetchone()["fc"]


def require_api_key(x_api_key: Annotated[str | None, Header()] = None):
    # If INGEST_API_KEY isn't set, the endpoint is open (fine locally, not in production)
    if INGEST_API_KEY and not secrets.compare_digest(x_api_key or "", INGEST_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class AISPosition(BaseModel):
    # Each field accepts either the DB column name or the NOAA CSV header,
    # so you can post rows straight from AIS-data.csv.
    mmsi: str | None = Field(None, validation_alias=AliasChoices("mmsi", "MMSI"))
    base_datetime: datetime = Field(
        validation_alias=AliasChoices("base_datetime", "BaseDateTime")
    )
    lat: float = Field(ge=-90, le=90, validation_alias=AliasChoices("lat", "LAT"))
    lon: float = Field(ge=-180, le=180, validation_alias=AliasChoices("lon", "LON"))
    sog: float | None = Field(None, validation_alias=AliasChoices("sog", "SOG"))
    cog: float | None = Field(None, validation_alias=AliasChoices("cog", "COG"))
    heading: float | None = Field(
        None, validation_alias=AliasChoices("heading", "Heading")
    )
    vessel_name: str | None = Field(
        None, validation_alias=AliasChoices("vessel_name", "VesselName")
    )
    imo: str | None = Field(None, validation_alias=AliasChoices("imo", "IMO"))
    call_sign: str | None = Field(
        None, validation_alias=AliasChoices("call_sign", "CallSign")
    )
    vessel_type: int | None = Field(
        None, validation_alias=AliasChoices("vessel_type", "VesselType")
    )
    status: int | None = Field(None, validation_alias=AliasChoices("status", "Status"))
    length: float | None = Field(
        None, validation_alias=AliasChoices("length", "Length")
    )
    width: float | None = Field(None, validation_alias=AliasChoices("width", "Width"))
    draft: float | None = Field(None, validation_alias=AliasChoices("draft", "Draft"))
    cargo: int | None = Field(None, validation_alias=AliasChoices("cargo", "Cargo"))
    transceiver_class: str | None = Field(
        None, validation_alias=AliasChoices("transceiver_class", "TransceiverClass")
    )

    # JSON often sends MMSI/IMO as numbers, but the columns are TEXT
    @field_validator("mmsi", "imo", "call_sign", mode="before")
    @classmethod
    def to_text(cls, v):
        if v is None or v == "":
            return None
        return str(v).strip()

    # base_datetime is TIMESTAMP without time zone, so store naive UTC.
    # Otherwise Postgres would shift aware values using the session time zone.
    @field_validator("base_datetime")
    @classmethod
    def to_naive_utc(cls, v: datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


INSERT_SQL = """
INSERT INTO ais_positions (
    mmsi, base_datetime, lat, lon, sog, cog, heading, vessel_name, imo,
    call_sign, vessel_type, status, length, width, draft, cargo, transceiver_class
) VALUES (
    %(mmsi)s, %(base_datetime)s, %(lat)s, %(lon)s, %(sog)s, %(cog)s, %(heading)s,
    %(vessel_name)s, %(imo)s, %(call_sign)s, %(vessel_type)s, %(status)s,
    %(length)s, %(width)s, %(draft)s, %(cargo)s, %(transceiver_class)s
)
"""


@app.post(
    "/api/positions",
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
def insert_positions(
    request: Request,
    positions: Annotated[list[AISPosition], Body(min_length=1, max_length=5000)],
):
    rows = [p.model_dump() for p in positions]

    # Leaving the connection block commits; any error rolls back the whole batch
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, rows)

    return {"inserted": len(rows)}


@app.get("/api/vessels/live")
async def live_vessels(
    request: Request,
    max_age_minutes: Annotated[int, Query(ge=1, le=60)] = 15,
):
    ingestor = request.app.state.ais_ingestor
    if ingestor is None:
        raise HTTPException(503, "Live AIS ingestion is not running")

    vessels = ingestor.live_vessels(max_age_minutes * 60)
    return {
        "count": len(vessels),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "vessels": vessels,
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
