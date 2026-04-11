"""
ingest_voter_file.py — Load the Iowa voter file CSV into the voters table.

The Iowa voter file is a READ-ONLY government dataset. This script only
inserts/updates records; it never deletes or modifies source data.
The raw CSV is archived (renamed) after a successful ingest run.

Usage:
    python scripts/ingest_voter_file.py
    python scripts/ingest_voter_file.py --file ./data/iowa_voter_file.csv
    python scripts/ingest_voter_file.py --synthetic --rows 10000
    python scripts/ingest_voter_file.py --counties "POLK,LINN,SCOTT"

After ingest, run in order:
    geocode_addresses.py         → populates voters.geom
    spatial_join_districts.py    → populates voters.block_group_id
    build_address_embeddings.py  → populates voters.address_embedding

Security notes:
    - voter_id values are never written to logs
    - party and vote_history are loaded as-is from the government file
    - geom, block_group_id, address_embedding are left NULL here
"""

import argparse
import hashlib
import logging
import os
import random
import shutil
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Iowa SOS voter file column mapping
#
# The Iowa Secretary of State voter registration file uses these column names.
# If the real file differs, update the values here — do not change the keys,
# which are the internal names used throughout this script.
#
# Columns intentionally excluded:
#   - MIDDLE_NAME, SUFFIX   (not in schema — not needed for matching)
#   - PHONE                 (not collected — minimal footprint principle)
#   - PRECINCT              (district assignment handled by spatial join)
#   - MAILING_* fields      (residence address is authoritative for postcards)
# ---------------------------------------------------------------------------
COLUMN_MAP = {
    "voter_id":    "VOTERID",
    "last_name":   "LAST_NAME",
    "first_name":  "FIRST_NAME",
    "address":     "RESADDRESS",   # full street address string
    "city":        "RESCITY",
    "zip":         "RESZIP",
    "party":       "PARTY",
}

# Election history columns follow the pattern: 2-digit year + election type
# e.g. "11GEN", "12PRI", "14GEN", "16GEN", "18PRI", "20GEN", "22GEN", "24GEN"
# Any column matching this pattern is treated as a vote history indicator.
# A non-empty value means the voter participated in that election.
ELECTION_COL_PREFIXES = (
    "00", "02", "04", "06", "08",
    "10", "11", "12", "13", "14", "15", "16", "17", "18", "19",
    "20", "21", "22", "23", "24",
)

BATCH_SIZE = 500  # rows per INSERT batch

# ---------------------------------------------------------------------------
# Synthetic data — Iowa-flavored fake voter records for local development
# ---------------------------------------------------------------------------

IOWA_CITIES = [
    ("Des Moines",   "50301"), ("Cedar Rapids", "52401"), ("Davenport",   "52801"),
    ("Sioux City",   "51101"), ("Iowa City",    "52240"), ("Waterloo",    "50701"),
    ("Ames",         "50010"), ("West Des Moines", "50265"), ("Ankeny",   "50021"),
    ("Council Bluffs", "51501"),
]
PARTIES = ["REP", "DEM", "NP", "LIB", "GRN"]
PARTY_WEIGHTS = [0.35, 0.33, 0.25, 0.04, 0.03]
STREET_TYPES = ["St", "Ave", "Blvd", "Dr", "Rd", "Ln", "Ct", "Way"]
ELECTION_CODES = ["16GEN", "18PRI", "18GEN", "20PRI", "20GEN", "22PRI", "22GEN", "24PRI", "24GEN"]

FIRST_NAMES = [
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael",
    "Linda", "William", "Barbara", "David", "Elizabeth", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Anderson", "Taylor", "Thomas", "Hernandez", "Moore",
    "Martin", "Jackson", "Thompson", "White", "Lopez", "Lee",
]


def _synthetic_voter_id() -> str:
    return "SYN" + "".join(random.choices(string.digits, k=8))


def generate_synthetic_rows(n: int) -> pd.DataFrame:
    log.info("Generating %d synthetic voter rows...", n)
    rows = []
    for _ in range(n):
        city, zip_code = random.choice(IOWA_CITIES)
        house = random.randint(100, 9999)
        street = random.choice(LAST_NAMES) + " " + random.choice(STREET_TYPES)
        history = random.sample(ELECTION_CODES, k=random.randint(0, len(ELECTION_CODES)))
        rows.append({
            "voter_id":    _synthetic_voter_id(),
            "last_name":   random.choice(LAST_NAMES),
            "first_name":  random.choice(FIRST_NAMES),
            "address":     f"{house} {street}",
            "city":        city,
            "zip":         zip_code,
            "party":       random.choices(PARTIES, weights=PARTY_WEIGHTS)[0],
            "vote_history": history,  # already a list — skip election col parsing
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Iowa voter file parsing
# ---------------------------------------------------------------------------

def _detect_election_columns(columns: list[str]) -> list[str]:
    """Return column names that look like Iowa election history codes."""
    return [
        c for c in columns
        if len(c) >= 4 and c[:2] in ELECTION_COL_PREFIXES and c[2:].isalpha()
    ]


def _build_vote_history(row: pd.Series, election_cols: list[str]) -> list[str]:
    """Return list of election codes where the voter has a non-empty value."""
    return [col for col in election_cols if pd.notna(row[col]) and str(row[col]).strip() != ""]


def load_voter_file(path: Path, counties_filter: list[str] | None) -> pd.DataFrame:
    log.info("Reading voter file: %s", path)
    df = pd.read_csv(path, dtype=str, low_memory=False)
    log.info("Raw rows: %d, columns: %d", len(df), len(df.columns))

    # Optional county filter
    if counties_filter:
        county_col = next(
            (c for c in df.columns if c.upper() in ("COUNTY", "COUNTY_NAME", "COUNTYNAME")),
            None,
        )
        if county_col:
            upper_filter = [c.upper() for c in counties_filter]
            df = df[df[county_col].str.upper().isin(upper_filter)]
            log.info("After county filter (%s): %d rows", ", ".join(counties_filter), len(df))
        else:
            log.warning("--counties specified but no COUNTY column found; loading all rows")

    # Validate required columns exist
    missing = [v for v in COLUMN_MAP.values() if v not in df.columns]
    if missing:
        log.error(
            "Required columns missing from voter file: %s\n"
            "Update COLUMN_MAP in this script to match the actual file headers.\n"
            "File headers: %s",
            missing,
            list(df.columns),
        )
        sys.exit(1)

    # Rename to internal names
    df = df.rename(columns={v: k for k, v in COLUMN_MAP.items()})

    # Build vote_history list from election columns
    election_cols = _detect_election_columns(list(df.columns))
    log.info("Detected %d election history columns", len(election_cols))
    df["vote_history"] = df.apply(_build_vote_history, axis=1, election_cols=election_cols)

    # Keep only schema columns
    df = df[["voter_id", "last_name", "first_name", "address", "city", "zip", "party", "vote_history"]]

    # Strip whitespace, uppercase zip
    for col in ["voter_id", "last_name", "first_name", "address", "city", "zip", "party"]:
        df[col] = df[col].str.strip()
    df["zip"] = df["zip"].str[:5]  # some files include ZIP+4

    # Drop rows missing any required field
    required = ["voter_id", "last_name", "first_name", "address", "city", "zip"]
    before = len(df)
    df = df.dropna(subset=required)
    df = df[df[required].apply(lambda col: col.str.strip() != "").all(axis=1)]
    dropped = before - len(df)
    if dropped:
        log.warning("Dropped %d rows with missing required fields", dropped)

    return df


# ---------------------------------------------------------------------------
# Database upsert
# ---------------------------------------------------------------------------

def upsert_voters(df: pd.DataFrame, conn) -> None:
    """
    Bulk upsert voters into the database.
    ON CONFLICT (voter_id) DO UPDATE — re-running is safe and idempotent.
    geom, block_group_id, address_embedding left NULL — populated by later scripts.
    voter_id values are never written to logs.
    """
    sql = """
        INSERT INTO voters (voter_id, last_name, first_name, address, city, zip, party, vote_history)
        VALUES %s
        ON CONFLICT (voter_id) DO UPDATE SET
            last_name    = EXCLUDED.last_name,
            first_name   = EXCLUDED.first_name,
            address      = EXCLUDED.address,
            city         = EXCLUDED.city,
            zip          = EXCLUDED.zip,
            party        = EXCLUDED.party,
            vote_history = EXCLUDED.vote_history
    """

    total = len(df)
    inserted = 0

    with conn.cursor() as cur:
        for start in range(0, total, BATCH_SIZE):
            batch = df.iloc[start : start + BATCH_SIZE]
            values = [
                (
                    row["voter_id"],
                    row["last_name"],
                    row["first_name"],
                    row["address"],
                    row["city"],
                    row["zip"],
                    row["party"] if pd.notna(row["party"]) else None,
                    row["vote_history"],  # psycopg2 serialises list → jsonb
                )
                for _, row in batch.iterrows()
            ]
            execute_values(cur, sql, values)
            conn.commit()
            inserted += len(batch)
            log.info("  %d / %d rows upserted", inserted, total)

    log.info("Upsert complete: %d rows processed", total)


def archive_voter_file(path: Path) -> None:
    """Rename the raw CSV to mark it as archived. Never delete — always keep a copy."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = path.with_name(f"{path.stem}_archived_{ts}{path.suffix}")
    shutil.move(str(path), str(archive_path))
    log.info("Raw voter file archived to: %s", archive_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest Iowa voter file into CivicBlocks database")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(os.getenv("VOTER_FILE_PATH", "./data/iowa_voter_file.csv")),
        help="Path to Iowa voter file CSV (default: $VOTER_FILE_PATH or ./data/iowa_voter_file.csv)",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Generate synthetic voter data instead of reading a real file",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=50000,
        help="Number of synthetic rows to generate (default: 50000, only used with --synthetic)",
    )
    parser.add_argument(
        "--counties",
        type=str,
        default=None,
        help="Comma-separated list of county names to load (default: all). E.g. 'POLK,LINN,SCOTT'",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip archiving the raw CSV after ingest (not recommended for real data)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    db_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not db_url:
        log.error(
            "No database URL found. Set DATABASE_URL or SUPABASE_DB_URL in your .env file."
        )
        sys.exit(1)

    # Load or generate data
    if args.synthetic:
        log.info("--synthetic flag set: generating fake Iowa voter data")
        df = generate_synthetic_rows(args.rows)
    else:
        if not args.file.exists():
            log.error("Voter file not found: %s", args.file)
            sys.exit(1)
        counties = [c.strip().upper() for c in args.counties.split(",")] if args.counties else None
        df = load_voter_file(args.file, counties)

    log.info("Rows ready for upsert: %d", len(df))

    # Connect and upsert
    try:
        conn = psycopg2.connect(db_url)
    except psycopg2.OperationalError as e:
        log.error("Database connection failed: %s", e)
        sys.exit(1)

    try:
        upsert_voters(df, conn)
    finally:
        conn.close()

    # Archive the real file (never archive synthetic data)
    if not args.synthetic and not args.no_archive:
        archive_voter_file(args.file)

    log.info("Done.")


if __name__ == "__main__":
    main()
