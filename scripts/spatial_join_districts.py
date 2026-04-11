"""
spatial_join_districts.py — Load Census TIGER/Line shapefiles and assign
voters to block groups via PostGIS spatial join.

Does three things in order:
  1. Downloads Iowa TIGER/Line shapefiles if not already present
  2. Loads block_groups and districts_geo tables (skips if already populated)
  3. Runs a PostGIS spatial join to populate voters.block_group_id

The spatial join runs entirely in the database using PostGIS — much faster
than doing it in Python at 2.4M rows.

Usage:
    python scripts/spatial_join_districts.py
    python scripts/spatial_join_districts.py --data-dir ./data/tiger
    python scripts/spatial_join_districts.py --skip-download  # use existing shapefiles
    python scripts/spatial_join_districts.py --reload-districts  # re-load even if table is populated

Pipeline position:
    ingest_voter_file.py  →  geocode_addresses.py  →  [this script]  →  build_address_embeddings.py

Notes on TIGER/Line URLs:
    The Census Bureau publishes TIGER/Line files at predictable URLs. The year
    in the URL (2023 below) may need updating for future runs. Verify at:
    https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html
"""

import argparse
import io
import logging
import os
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from shapely.geometry import mapping

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

IOWA_FIPS = "19"
TIGER_YEAR = "2023"

# ---------------------------------------------------------------------------
# TIGER/Line download URLs — verify year at census.gov if these return 404
# ---------------------------------------------------------------------------
TIGER_FILES = {
    "block_groups": {
        "url": f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/BG/tl_{TIGER_YEAR}_{IOWA_FIPS}_bg.zip",
        "filename": f"tl_{TIGER_YEAR}_{IOWA_FIPS}_bg.zip",
        "description": "Iowa Census block groups",
    },
    "state_senate": {
        "url": f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/SLDU/tl_{TIGER_YEAR}_{IOWA_FIPS}_sldu.zip",
        "filename": f"tl_{TIGER_YEAR}_{IOWA_FIPS}_sldu.zip",
        "description": "Iowa State Senate districts",
    },
    "state_house": {
        "url": f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/SLDL/tl_{TIGER_YEAR}_{IOWA_FIPS}_sldl.zip",
        "filename": f"tl_{TIGER_YEAR}_{IOWA_FIPS}_sldl.zip",
        "description": "Iowa State House districts",
    },
    "us_congress": {
        "url": f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/CD/tl_{TIGER_YEAR}_us_cd118.zip",
        "filename": f"tl_{TIGER_YEAR}_us_cd118.zip",
        "description": "US Congressional districts (national file — filtered to Iowa)",
    },
}

# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path) -> None:
    log.info("Downloading: %s", url)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                downloaded += len(chunk)
        log.info("  Saved %d MB → %s", downloaded // 1_000_000, dest)


def ensure_shapefiles(data_dir: Path, skip_download: bool) -> dict[str, Path]:
    """
    Ensure all required shapefiles exist in data_dir.
    Returns dict mapping key → path to extracted shapefile directory.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    shapefile_dirs = {}

    for key, meta in TIGER_FILES.items():
        zip_path = data_dir / meta["filename"]
        extract_dir = data_dir / zip_path.stem

        if not extract_dir.exists():
            if skip_download and not zip_path.exists():
                log.error(
                    "--skip-download set but %s not found. "
                    "Download it manually from: %s",
                    zip_path,
                    meta["url"],
                )
                sys.exit(1)
            if not zip_path.exists():
                download_file(meta["url"], zip_path)
            log.info("Extracting %s → %s", zip_path.name, extract_dir)
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(extract_dir)
        else:
            log.info("Already extracted: %s", extract_dir)

        shapefile_dirs[key] = extract_dir

    return shapefile_dirs


# ---------------------------------------------------------------------------
# Load block_groups table
# ---------------------------------------------------------------------------

def load_block_groups(conn, shapefile_dir: Path, reload: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM block_groups")
        count = cur.fetchone()[0]

    if count > 0 and not reload:
        log.info("block_groups already has %d rows — skipping load (use --reload-districts to force)", count)
        return

    shp_files = list(shapefile_dir.glob("*.shp"))
    if not shp_files:
        log.error("No .shp file found in %s", shapefile_dir)
        sys.exit(1)

    log.info("Loading block groups from %s", shp_files[0])
    gdf = gpd.read_file(shp_files[0])
    gdf = gdf.to_crs(epsg=4326)

    log.info("Inserting %d block groups...", len(gdf))
    sql = """
        INSERT INTO block_groups (block_group_id, geoid, geom, state_fips, county_fips, tract, block_group)
        VALUES %s
        ON CONFLICT (block_group_id) DO UPDATE SET
            geoid       = EXCLUDED.geoid,
            geom        = EXCLUDED.geom,
            state_fips  = EXCLUDED.state_fips,
            county_fips = EXCLUDED.county_fips,
            tract       = EXCLUDED.tract,
            block_group = EXCLUDED.block_group
    """

    rows = []
    for _, row in gdf.iterrows():
        geoid = str(row.get("GEOID", "")).strip()
        state_fips = str(row.get("STATEFP", "")).strip()
        county_fips = str(row.get("COUNTYFP", "")).strip()
        tract = str(row.get("TRACTCE", "")).strip()
        bg = str(row.get("BLKGRPCE", "")).strip()
        geom_wkt = row.geometry.wkt if row.geometry else None
        rows.append((geoid, geoid, geom_wkt, state_fips, county_fips, tract, bg))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            sql,
            rows,
            template="(%s, %s, ST_GeomFromText(%s, 4326), %s, %s, %s, %s)",
            page_size=500,
        )
    conn.commit()
    log.info("block_groups loaded: %d rows", len(rows))


# ---------------------------------------------------------------------------
# Load districts_geo table
# ---------------------------------------------------------------------------

DISTRICT_TYPE_MAP = {
    "state_senate": "IA Senate",
    "state_house":  "IA House",
    "us_congress":  "US House",
}


def _district_id(district_type: str, name_or_num: str) -> str:
    prefix = district_type.upper().replace(" ", "_")
    return f"{prefix}_{name_or_num.strip().zfill(3)}"


def load_districts(conn, shapefile_dirs: dict[str, Path], reload: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM districts_geo")
        count = cur.fetchone()[0]

    if count > 0 and not reload:
        log.info("districts_geo already has %d rows — skipping load (use --reload-districts to force)", count)
        return

    sql = """
        INSERT INTO districts_geo (district_id, district_type, district_name, geom)
        VALUES %s
        ON CONFLICT (district_id) DO UPDATE SET
            district_type = EXCLUDED.district_type,
            district_name = EXCLUDED.district_name,
            geom          = EXCLUDED.geom
    """

    for key, district_type_label in DISTRICT_TYPE_MAP.items():
        shapefile_dir = shapefile_dirs[key]
        shp_files = list(shapefile_dir.glob("*.shp"))
        if not shp_files:
            log.error("No .shp file found in %s", shapefile_dir)
            sys.exit(1)

        log.info("Loading %s from %s", district_type_label, shp_files[0])
        gdf = gpd.read_file(shp_files[0])
        gdf = gdf.to_crs(epsg=4326)

        # Congressional file is national — filter to Iowa (STATEFP = '19')
        if key == "us_congress":
            statefp_col = next((c for c in gdf.columns if c.upper() == "STATEFP"), None)
            if statefp_col:
                gdf = gdf[gdf[statefp_col] == IOWA_FIPS]
                log.info("  Filtered to Iowa: %d congressional districts", len(gdf))

        # Detect district number column (SLDUST, SLDLST, CD118FP, etc.)
        num_col = next(
            (c for c in gdf.columns if c.upper() in ("SLDUST", "SLDLST", "CD118FP", "DISTRICT")),
            None,
        )
        if not num_col:
            log.error(
                "Could not find district number column in %s. Columns: %s",
                shp_files[0],
                list(gdf.columns),
            )
            sys.exit(1)

        rows = []
        for _, row in gdf.iterrows():
            num = str(row[num_col]).strip()
            district_id = _district_id(district_type_label, num)
            district_name = f"{district_type_label} District {num.lstrip('0') or '0'}"
            geom_wkt = row.geometry.wkt if row.geometry else None
            rows.append((district_id, district_type_label, district_name, geom_wkt))

        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                sql,
                rows,
                template="(%s, %s, %s, ST_GeomFromText(%s, 4326))",
                page_size=200,
            )
        conn.commit()
        log.info("  Inserted %d %s districts", len(rows), district_type_label)


# ---------------------------------------------------------------------------
# PostGIS spatial join — voters → block_groups
# ---------------------------------------------------------------------------

def spatial_join_block_groups(conn) -> None:
    """
    Assign voters.block_group_id using a PostGIS ST_Within join.
    Only processes voters with geom IS NOT NULL AND block_group_id IS NULL.
    Runs entirely in the database — no Python row iteration needed.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM voters WHERE geom IS NOT NULL AND block_group_id IS NULL")
        pending = cur.fetchone()[0]

    if pending == 0:
        log.info("All geocoded voters already have block_group_id assigned.")
        return

    log.info("Assigning block_group_id for %d voters via PostGIS spatial join...", pending)

    sql = """
        UPDATE voters v
        SET block_group_id = bg.block_group_id
        FROM block_groups bg
        WHERE v.geom IS NOT NULL
          AND v.block_group_id IS NULL
          AND ST_Within(v.geom, bg.geom)
    """

    with conn.cursor() as cur:
        cur.execute(sql)
        updated = cur.rowcount
    conn.commit()

    unassigned = pending - updated
    log.info("block_group_id assigned: %d rows updated", updated)
    if unassigned > 0:
        log.warning(
            "%d voters have geom but no matching block group — "
            "likely on a district boundary or outside Iowa. Geom is retained; "
            "block_group_id will remain NULL.",
            unassigned,
        )


def log_summary(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM block_groups")
        bg_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM districts_geo")
        dist_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM voters WHERE block_group_id IS NOT NULL")
        assigned = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM voters WHERE geom IS NOT NULL AND block_group_id IS NULL")
        unassigned = cur.fetchone()[0]

    log.info("--- Summary ---")
    log.info("  block_groups rows:    %d", bg_count)
    log.info("  districts_geo rows:   %d", dist_count)
    log.info("  voters with block_group_id: %d", assigned)
    log.info("  voters with geom but no block_group_id: %d", unassigned)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load TIGER/Line shapefiles and assign voters to block groups"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./data/tiger"),
        help="Directory to store downloaded TIGER/Line files (default: ./data/tiger)",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading — use shapefiles already present in --data-dir",
    )
    parser.add_argument(
        "--reload-districts",
        action="store_true",
        help="Re-load block_groups and districts_geo even if tables are already populated",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    db_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not db_url:
        log.error("No database URL found. Set DATABASE_URL or SUPABASE_DB_URL in .env")
        sys.exit(1)

    try:
        conn = psycopg2.connect(db_url)
    except psycopg2.OperationalError as e:
        log.error("Database connection failed: %s", e)
        sys.exit(1)

    try:
        shapefile_dirs = ensure_shapefiles(args.data_dir, skip_download=args.skip_download)
        load_block_groups(conn, shapefile_dirs["block_groups"], reload=args.reload_districts)
        load_districts(conn, shapefile_dirs, reload=args.reload_districts)
        spatial_join_block_groups(conn)
        log_summary(conn)
    finally:
        conn.close()

    log.info("Done. Next step: python scripts/build_address_embeddings.py")


if __name__ == "__main__":
    main()
