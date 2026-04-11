"""
build_address_embeddings.py — Generate pgvector address embeddings for voter
matching using Voyage AI's voyage-large-2 model (1536 dimensions).

Embeddings are generated from a normalized string:
    "{first_name} {last_name} {address} {city} {zip}"
with standard Iowa address abbreviation expansion applied first.

Only processes rows where address_embedding IS NULL — safe to re-run.
Only processes voters that have already been geocoded (geom IS NOT NULL),
since ungeocoded addresses are lower confidence for matching anyway.

Usage:
    python scripts/build_address_embeddings.py
    python scripts/build_address_embeddings.py --batch-size 64
    python scripts/build_address_embeddings.py --all       # re-embed everything
    python scripts/build_address_embeddings.py --limit 500 # for testing

Pipeline position:
    ingest_voter_file.py  →  geocode_addresses.py  →  spatial_join_districts.py  →  [this script]

Voyage AI notes:
    - Model: voyage-large-2 (1536 dimensions — matches schema VECTOR(1536))
    - input_type="document" for stored embeddings
    - input_type="query" must be used at match time (in match_voter.py)
    - Default rate limit: 300 RPM, 1M tokens/min
    - Max 128 texts or ~120k tokens per request
    - Short addresses (~15 tokens each) → batch size of 128 is safe
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterator

import psycopg2
import psycopg2.extras
import voyageai
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.match_voter import normalize_address  # noqa: E402

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

VOYAGE_MODEL = "voyage-large-2"
VOYAGE_MAX_BATCH = 128   # Voyage AI hard limit per request
RETRY_WAIT_S = 60        # seconds to wait on rate limit error before retrying
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def fetch_rows(conn, limit: int | None, reprocess_all: bool) -> list[dict]:
    """
    Fetch voters that need embeddings.
    Server-side cursor avoids loading millions of rows into memory.
    Only voters with geom (geocoded) are included — ungeocoded addresses
    are lower confidence and can be embedded in a follow-up run if needed.
    """
    conditions = [] if reprocess_all else ["address_embedding IS NULL"]
    conditions.append("geom IS NOT NULL")
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    limit_clause = f"LIMIT {limit}" if limit else ""

    sql = f"""
        SELECT voter_id, first_name, last_name, address, city, zip
        FROM voters
        {where}
        {limit_clause}
    """

    rows = []
    with conn.cursor(name="embed_cursor", cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.itersize = 5000
        cur.execute(sql)
        for row in cur:
            rows.append(dict(row))
    return rows


def update_embeddings_batch(conn, updates: list[tuple]) -> None:
    """
    Bulk update voters.address_embedding.
    updates: list of (embedding_list, voter_id) tuples
    voter_id never appears in logs.
    """
    sql = "UPDATE voters SET address_embedding = %s::vector WHERE voter_id = %s"
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            sql,
            [(str(emb), vid) for emb, vid in updates],
            page_size=500,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Voyage AI embedding with retry
# ---------------------------------------------------------------------------

def embed_batch(client: voyageai.Client, texts: list[str]) -> list[list[float]] | None:
    """
    Embed a batch of texts. Returns list of embeddings or None on unrecoverable error.
    Retries on rate limit errors with exponential backoff.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = client.embed(texts, model=VOYAGE_MODEL, input_type="document")
            return result.embeddings
        except voyageai.error.RateLimitError:
            if attempt < MAX_RETRIES:
                wait = RETRY_WAIT_S * attempt
                log.warning("Rate limit hit — waiting %ds before retry %d/%d", wait, attempt, MAX_RETRIES)
                time.sleep(wait)
            else:
                log.error("Rate limit exceeded after %d retries — halting", MAX_RETRIES)
                return None
        except voyageai.error.VoyageError as e:
            log.error("Voyage API error: %s", e)
            return None

    return None


# ---------------------------------------------------------------------------
# Chunking utility
# ---------------------------------------------------------------------------

def chunked(lst: list, size: int) -> Iterator[list]:
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def build_embeddings(conn, rows: list[dict], batch_size: int, client: voyageai.Client) -> None:
    total = len(rows)
    embedded = 0
    failed_batches = 0
    total_batches = (total + batch_size - 1) // batch_size

    log.info("Generating embeddings for %d voters in %d batches (model: %s)", total, total_batches, VOYAGE_MODEL)

    for batch_num, chunk in enumerate(chunked(rows, batch_size), start=1):
        texts = [
            normalize_address(
                row["first_name"], row["last_name"],
                row["address"], row["city"], row["zip"],
            )
            for row in chunk
        ]

        embeddings = embed_batch(client, texts)
        if embeddings is None:
            log.error("Batch %d/%d failed — stopping. Re-run to resume from this point.", batch_num, total_batches)
            failed_batches += 1
            break

        updates = [(emb, row["voter_id"]) for emb, row in zip(embeddings, chunk)]
        update_embeddings_batch(conn, updates)
        embedded += len(chunk)

        log.info("Batch %d/%d — %d / %d embeddings stored", batch_num, total_batches, embedded, total)

    if failed_batches == 0:
        log.info("Embedding complete: %d rows updated", embedded)
    else:
        log.warning(
            "Embedding incomplete: %d rows updated before failure. "
            "Re-run to continue — already-embedded rows will be skipped.",
            embedded,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Voyage AI address embeddings for voter matching"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=128,
        help=f"Texts per Voyage AI request (max {VOYAGE_MAX_BATCH}, default: 128)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-embed all geocoded voters, not just those with NULL address_embedding",
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

    if args.batch_size > VOYAGE_MAX_BATCH:
        log.error("--batch-size cannot exceed %d (Voyage AI limit)", VOYAGE_MAX_BATCH)
        sys.exit(1)

    voyage_api_key = os.getenv("VOYAGE_API_KEY")
    if not voyage_api_key:
        log.error("VOYAGE_API_KEY not set in .env")
        sys.exit(1)

    db_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not db_url:
        log.error("No database URL found. Set DATABASE_URL or SUPABASE_DB_URL in .env")
        sys.exit(1)

    client = voyageai.Client(api_key=voyage_api_key)

    try:
        conn = psycopg2.connect(db_url)
    except psycopg2.OperationalError as e:
        log.error("Database connection failed: %s", e)
        sys.exit(1)

    try:
        rows = fetch_rows(conn, limit=args.limit, reprocess_all=args.all)
        if not rows:
            log.info("No rows to embed — all geocoded voters already have address_embedding set.")
            return
        log.info("Fetched %d rows to embed", len(rows))
        build_embeddings(conn, rows, batch_size=args.batch_size, client=client)
    finally:
        conn.close()

    log.info("Done. Phase 1 data pipeline complete.")
    log.info("Next: python scripts/spatial_join_districts.py verified, then move to Phase 2 (auth + voter matching).")


if __name__ == "__main__":
    main()
