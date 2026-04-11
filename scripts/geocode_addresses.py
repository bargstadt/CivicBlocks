"""
geocode_addresses.py — Populate voters.geom from address fields.

Primary:  Census Geocoder batch API (free, no key, up to 1000 addresses/request)
Fallback: Nominatim (free, no key, 1 req/sec rate limit)

Only processes rows where geom IS NULL by default — safe to re-run after failures.

Usage:
    python scripts/geocode_addresses.py
    python scripts/geocode_addresses.py --batch-size 500
    python scripts/geocode_addresses.py --all        # re-geocode everything, not just NULLs
    python scripts/geocode_addresses.py --limit 1000 # process at most N rows (for testing)

Pipeline position:
    ingest_voter_file.py  →  [this script]  →  spatial_join_districts.py  →  build_address_embeddings.py

Security notes:
    - voter_id values are never written to logs
    - addresses are sent to Census Geocoder and Nominatim (both US government / OSM)
    - no PII beyond address is transmitted
"""

import argparse
import io
import logging
import os
import sys
import time
from typing import Iterator

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "CivicBlocks/0.1 (civic feedback platform; contact via github.com/civicblocks)"

# Census batch API hard limit is 10,000; staying at 1,000 keeps response times predictable
CENSUS_MAX_BATCH = 1000

# ---------------------------------------------------------------------------
# Census Geocoder (batch)
# ---------------------------------------------------------------------------

def _census_batch(rows: list[tuple]) -> dict[str, tuple[float, float]]:
    """
    Submit a batch of addresses to the Census Geocoder.

    rows: list of (internal_id, street, city, zip) tuples
          internal_id is a row index — NOT voter_id — to avoid transmitting PII

    Returns: dict mapping internal_id → (lon, lat) for matched rows only
    """
    csv_lines = [f"{rid},{street},{city},IA,{zip_code}" for rid, street, city, zip_code in rows]
    csv_content = "\n".join(csv_lines)

    try:
        response = requests.post(
            CENSUS_GEOCODER_URL,
            data={"benchmark": "Public_AR_Current"},
            files={"addressFile": ("addresses.csv", io.StringIO(csv_content), "text/csv")},
            timeout=120,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        log.warning("Census Geocoder request failed: %s", e)
        return {}

    results = {}
    for line in response.text.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 6:
            continue
        rid = parts[0].strip()
        match_status = parts[2].strip()  # "Match" or "No_Match" or "Tie"
        coordinates = parts[5].strip().strip('"')  # "lon,lat"
        if match_status == "Match" and coordinates:
            try:
                lon_str, lat_str = coordinates.split(",")
                results[rid] = (float(lon_str), float(lat_str))
            except ValueError:
                continue

    return results


# ---------------------------------------------------------------------------
# Nominatim (single address fallback)
# ---------------------------------------------------------------------------

def _nominatim_single(street: str, city: str, zip_code: str) -> tuple[float, float] | None:
    """
    Geocode one address via Nominatim. Returns (lon, lat) or None.
    Caller is responsible for rate-limiting (1 req/sec).
    """
    params = {
        "street": street,
        "city": city,
        "state": "Iowa",
        "postalcode": zip_code,
        "country": "US",
        "format": "json",
        "limit": 1,
    }
    headers = {"User-Agent": NOMINATIM_USER_AGENT}

    try:
        response = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data:
            return float(data[0]["lon"]), float(data[0]["lat"])
    except (requests.RequestException, KeyError, ValueError, IndexError) as e:
        log.debug("Nominatim failed for '%s, %s %s': %s", street, city, zip_code, e)

    return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def fetch_ungeocoded(conn, limit: int | None, reprocess_all: bool) -> list[dict]:
    """
    Return rows from voters that need geocoding.
    Uses server-side cursor to avoid loading 2M+ rows into memory at once.
    """
    where = "" if reprocess_all else "WHERE geom IS NULL"
    limit_clause = f"LIMIT {limit}" if limit else ""
    sql = f"SELECT voter_id, address, city, zip FROM voters {where} {limit_clause}"

    rows = []
    with conn.cursor(name="geocode_cursor", cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.itersize = 2000
        cur.execute(sql)
        for row in cur:
            rows.append(dict(row))

    return rows


def update_geom_batch(conn, updates: list[tuple]) -> None:
    """
    Bulk update voters.geom.
    updates: list of (lon, lat, voter_id) tuples
    voter_id never appears in logs.
    """
    sql = """
        UPDATE voters
        SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)
        WHERE voter_id = %s
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, updates, page_size=500)
    conn.commit()


# ---------------------------------------------------------------------------
# Chunking utility
# ---------------------------------------------------------------------------

def chunked(lst: list, size: int) -> Iterator[list]:
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ---------------------------------------------------------------------------
# Main geocoding loop
# ---------------------------------------------------------------------------

def geocode_all(conn, rows: list[dict], batch_size: int, delay_ms: int) -> None:
    total = len(rows)
    geocoded = 0
    failed = 0
    delay_s = delay_ms / 1000

    log.info("Starting geocoding: %d addresses, batch size %d", total, batch_size)

    for chunk_index, chunk in enumerate(chunked(rows, batch_size)):
        # Build Census batch input — use chunk-local index as ID, not voter_id
        census_input = [
            (str(i), row["address"], row["city"], row["zip"])
            for i, row in enumerate(chunk)
        ]

        log.info(
            "Batch %d/%d — submitting %d addresses to Census Geocoder",
            chunk_index + 1,
            (total + batch_size - 1) // batch_size,
            len(chunk),
        )

        census_results = _census_batch(census_input)

        updates = []
        nominatim_queue = []

        for i, row in enumerate(chunk):
            local_id = str(i)
            if local_id in census_results:
                lon, lat = census_results[local_id]
                updates.append((lon, lat, row["voter_id"]))
            else:
                nominatim_queue.append(row)

        log.info(
            "  Census matched: %d / %d  |  Nominatim fallback: %d",
            len(updates),
            len(chunk),
            len(nominatim_queue),
        )

        # Nominatim fallback — 1 req/sec required
        for row in nominatim_queue:
            result = _nominatim_single(row["address"], row["city"], row["zip"])
            time.sleep(1.0)  # Nominatim hard rate limit
            if result:
                lon, lat = result
                updates.append((lon, lat, row["voter_id"]))
            else:
                failed += 1
                log.debug("No geocode result for address in %s %s", row["city"], row["zip"])

        if updates:
            update_geom_batch(conn, updates)
            geocoded += len(updates)

        log.info(
            "Progress: %d / %d geocoded, %d failed so far",
            geocoded,
            total,
            failed,
        )

        # Polite delay between Census batches
        if delay_s > 0:
            time.sleep(delay_s)

    log.info(
        "Geocoding complete: %d geocoded, %d could not be matched (geom remains NULL)",
        geocoded,
        failed,
    )
    if failed > 0:
        log.info(
            "Tip: re-run with --all to retry failed rows after fixing address data, "
            "or accept that some rural addresses may not geocode reliably."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Geocode voter addresses into PostGIS points")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("GEOCODING_BATCH_SIZE", 500)),
        help="Addresses per Census Geocoder batch (max 1000, default: $GEOCODING_BATCH_SIZE or 500)",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=int(os.getenv("GEOCODING_DELAY_MS", 200)),
        help="Milliseconds to wait between Census batches (default: $GEOCODING_DELAY_MS or 200)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-geocode all rows, not just those with NULL geom",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N rows (useful for testing)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.batch_size > CENSUS_MAX_BATCH:
        log.error("--batch-size cannot exceed %d (Census Geocoder hard limit)", CENSUS_MAX_BATCH)
        sys.exit(1)

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
        rows = fetch_ungeocoded(conn, limit=args.limit, reprocess_all=args.all)
        if not rows:
            log.info("No rows to geocode — all voters already have geom set.")
            return

        log.info("Fetched %d rows to geocode", len(rows))
        geocode_all(conn, rows, batch_size=args.batch_size, delay_ms=args.delay_ms)
    finally:
        conn.close()

    log.info("Done. Next step: python scripts/spatial_join_districts.py")


if __name__ == "__main__":
    main()
