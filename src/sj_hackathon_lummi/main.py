import math
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

app = FastAPI()

STATIC_DIR = Path(__file__).parent / "static"

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/lummi-db"


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
    yield
    pool.close()


app = FastAPI(lifespan=lifespan)

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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
