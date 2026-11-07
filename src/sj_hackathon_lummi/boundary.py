"""Load polygon boundaries from a KML file into PostGIS.

Usage:
    uv run python scripts/load_boundary.py provided-resources/Lummi_Reservation_Boundary.kml
    uv run python scripts/load_boundary.py boundary.kml --name "Lummi Reservation"
    uv run python scripts/load_boundary.py boundary.kml --geojson-out static/boundary.geojson

The database connection comes from DATABASE_URL (same variable the API uses).
Re-running the script updates existing rows (matched by name) instead of
creating duplicates.

Only uses the standard library for KML parsing, plus psycopg for the database.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/lummi-db"

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS boundaries (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    source_file TEXT,
    geom        GEOMETRY(MultiPolygon, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS boundaries_geom_idx
    ON boundaries USING GIST (geom);
"""

UPSERT = """
INSERT INTO boundaries (name, description, source_file, geom)
VALUES (
    %(name)s,
    %(description)s,
    %(source_file)s,
    -- MakeValid repairs self-intersections; CollectionExtract(..., 3) keeps
    -- only the polygon parts in case repair produced mixed geometry types.
    ST_Multi(
        ST_CollectionExtract(
            ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%(geojson)s), 4326)),
            3
        )
    )
)
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    source_file = EXCLUDED.source_file,
    geom        = EXCLUDED.geom
"""


# ---------------------------------------------------------------------------
# KML parsing
# ---------------------------------------------------------------------------
def parse_ring(coord_text: str) -> list[list[float]]:
    """KML coordinates are 'lon,lat[,alt]' tuples separated by whitespace."""
    ring = []
    for token in coord_text.split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        ring.append([float(parts[0]), float(parts[1])])  # drop altitude

    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(ring[0])  # GeoJSON/PostGIS rings must be closed
    return ring


def parse_polygon(poly: ET.Element) -> list[list[list[float]]] | None:
    """Returns GeoJSON polygon coordinates: [outer_ring, *inner_rings]."""
    outer = poly.find("{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
    if outer is None or not outer.text:
        return None

    rings = [parse_ring(outer.text)]
    for inner in poly.findall("{*}innerBoundaryIs/{*}LinearRing/{*}coordinates"):
        if inner.text:
            rings.append(parse_ring(inner.text))

    return [r for r in rings if len(r) >= 4] or None


def polygons_in(element: ET.Element) -> list[list[list[list[float]]]]:
    """All polygons under an element, including those inside MultiGeometry."""
    polys = []
    for poly in element.findall(".//{*}Polygon"):
        coords = parse_polygon(poly)
        if coords:
            polys.append(coords)
    return polys


def text_of(element: ET.Element, tag: str) -> str | None:
    child = element.find(f"{{*}}{tag}")
    if child is not None and child.text and child.text.strip():
        return child.text.strip()
    return None


def read_kml(path: Path, fallback_name: str) -> list[dict]:
    """Returns one feature dict per placemark that contains polygons."""
    root = ET.parse(path).getroot()
    features = []

    for i, placemark in enumerate(root.findall(".//{*}Placemark"), start=1):
        polys = polygons_in(placemark)
        if not polys:
            continue  # points, lines, etc. are ignored
        name = text_of(placemark, "name") or (
            fallback_name if i == 1 else f"{fallback_name} {i}"
        )
        features.append(
            {
                "name": name,
                "description": text_of(placemark, "description"),
                "geometry": {"type": "MultiPolygon", "coordinates": polys},
            }
        )

    # Some files put Polygon elements outside any Placemark
    if not features:
        polys = polygons_in(root)
        if polys:
            features.append(
                {
                    "name": fallback_name,
                    "description": None,
                    "geometry": {"type": "MultiPolygon", "coordinates": polys},
                }
            )

    return features


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_geojson(features: list[dict], out_path: Path) -> None:
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": f["name"], "description": f["description"]},
                "geometry": f["geometry"],
            }
            for f in features
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(collection))
    print(f"Wrote GeoJSON to {out_path}")


def load_into_db(features: list[dict], dsn: str, source_file: str) -> None:
    import psycopg  # imported here so --skip-db works without it

    with psycopg.connect(dsn) as conn:
        conn.execute(DDL)
        for f in features:
            conn.execute(
                UPSERT,
                {
                    "name": f["name"],
                    "description": f["description"],
                    "source_file": source_file,
                    "geojson": json.dumps(f["geometry"]),
                },
            )
            print(f"Upserted boundary: {f['name']}")
        # leaving the `with` block commits the transaction


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("kml", type=Path, help="Path to the KML file")
    parser.add_argument(
        "--name",
        help="Name for the boundary (default: the Placemark name, else the file name)",
    )
    parser.add_argument(
        "--geojson-out",
        type=Path,
        help="Also write a GeoJSON file (handy for static hosting or debugging)",
    )
    parser.add_argument(
        "--skip-db", action="store_true", help="Only convert; don't touch the database"
    )
    args = parser.parse_args()

    if not args.kml.exists():
        print(f"File not found: {args.kml}", file=sys.stderr)
        return 1

    features = read_kml(args.kml, fallback_name=args.name or args.kml.stem)
    if not features:
        print("No polygons found in the KML file.", file=sys.stderr)
        return 1

    # --name overrides the Placemark name when there is exactly one feature
    if args.name and len(features) == 1:
        features[0]["name"] = args.name

    n_polys = sum(len(f["geometry"]["coordinates"]) for f in features)
    print(f"Found {len(features)} placemark(s) with {n_polys} polygon(s)")

    if args.geojson_out:
        write_geojson(features, args.geojson_out)

    if not args.skip_db:
        dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
        load_into_db(features, dsn, source_file=args.kml.name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
