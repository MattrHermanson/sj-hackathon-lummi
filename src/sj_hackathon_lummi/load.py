import csv
import sys
from pathlib import Path

import geopandas as gpd
import psycopg

DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/lummi-db"

CREATE_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS ais_positions (
    id                  BIGSERIAL PRIMARY KEY,
    mmsi                TEXT,
    base_datetime       TIMESTAMP NOT NULL,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    sog                 REAL,
    cog                 REAL,
    heading             REAL,
    vessel_name         TEXT,
    imo                 TEXT,
    call_sign           TEXT,
    vessel_type         SMALLINT,
    status              SMALLINT,
    length              REAL,
    width               REAL,
    draft               REAL,
    cargo               SMALLINT,
    transceiver_class   TEXT,

    geom GEOMETRY(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED
);

CREATE INDEX IF NOT EXISTS ais_positions_callsign_time_idx
    ON ais_positions (call_sign, base_datetime);

CREATE INDEX IF NOT EXISTS ais_positions_geom_idx
    ON ais_positions USING GIST (geom);

CREATE INDEX IF NOT EXISTS ais_positions_mmsi_time_idx
    ON ais_positions (mmsi, base_datetime);
"""

INSERT_SQL = """
INSERT INTO ais_positions (
    mmsi, base_datetime, lat, lon, sog, cog, heading,
    vessel_name, imo, call_sign, vessel_type, status,
    length, width, draft, cargo, transceiver_class
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def parse_float(val):
    return float(val) if val is not None and str(val).strip() != "" else None


def parse_int(val):
    return int(val) if val is not None and str(val).strip() != "" else None


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yield (
                row.get("MMSI"),
                row.get("BaseDateTime"),
                float(row["LAT"]),
                float(row["LON"]),
                parse_float(row.get("SOG")),
                parse_float(row.get("COG")),
                parse_float(row.get("Heading")),
                row.get("VesselName"),
                row.get("IMO"),
                row.get("CallSign"),
                parse_int(row.get("VesselType")),
                parse_int(row.get("Status")),
                parse_float(row.get("Length")),
                parse_float(row.get("Width")),
                parse_float(row.get("Draft")),
                parse_int(row.get("Cargo")),
                row.get("TransceiverClass"),
            )


def read_gpkg_rows(path):
    gdf = gpd.read_file(path)
    for _, row in gdf.iterrows():
        geom = row.geometry

        # Extract lon/lat safely depending on geometry type
        if geom is not None:
            if geom.geom_type == "Point":
                lon, lat = geom.x, geom.y
            elif geom.geom_type in ["LineString", "MultiLineString"]:
                # Use the starting coordinate of the line/track
                first_coords = (
                    geom.coords[0]
                    if geom.geom_type == "LineString"
                    else geom.geoms[0].coords[0]
                )
                lon, lat = first_coords[0], first_coords[1]
            else:
                # Fallback to centroid for polygons or other shapes
                centroid = geom.centroid
                lon, lat = centroid.x, centroid.y
        else:
            lon = parse_float(row.get("LON"))
            lat = parse_float(row.get("LAT"))

        yield (
            str(row.get("MMSI")) if row.get("MMSI") is not None else None,
            str(row.get("BaseDateTime"))
            if row.get("BaseDateTime") is not None
            else None,
            float(lat) if lat is not None else 0.0,
            float(lon) if lon is not None else 0.0,
            parse_float(row.get("SOG")),
            parse_float(row.get("COG")),
            parse_float(row.get("Heading")),
            row.get("VesselName"),
            str(row.get("IMO")) if row.get("IMO") is not None else None,
            str(row.get("CallSign")) if row.get("CallSign") is not None else None,
            parse_int(row.get("VesselType")),
            parse_int(row.get("Status")),
            parse_float(row.get("Length")),
            parse_float(row.get("Width")),
            parse_float(row.get("Draft")),
            parse_int(row.get("Cargo")),
            str(row.get("TransceiverClass"))
            if row.get("TransceiverClass") is not None
            else None,
        )


def read_rows(path):
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        yield from read_csv_rows(path)
    elif ext == ".gpkg":
        yield from read_gpkg_rows(path)
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: uv run main.py <file.csv|file.gpkg>")

    rows = list(read_rows(sys.argv[1]))

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.executemany(INSERT_SQL, rows)
        conn.commit()

    print(f"Inserted {len(rows)} rows.")


if __name__ == "__main__":
    main()
