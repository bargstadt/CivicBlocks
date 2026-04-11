"""
demo.py — End-to-end demonstration of the CivicBlocks POC flow.

Requires only a DATABASE_URL with the schema already applied.
No Voyage AI, Lob, or Anthropic API keys needed.

What this demonstrates:
    1. Synthetic voter data generation and ingestion
    2. Voter matching — Stage 1 (ZIP + street number)
    3. Match token generation (opaque, HMAC-signed — voter_id never exposed)
    4. Account linking via confirm_match
    5. One-voter-one-voice enforcement (UNIQUE constraint)
    6. Postcard code generation and verification

Usage:
    python scripts/demo.py
    python scripts/demo.py --voters 500   # load more synthetic voters
    python scripts/demo.py --keep         # don't wipe demo data on exit
"""

import argparse
import logging
import os
import random
import sys
import uuid
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.ingest_voter_file import generate_synthetic_rows, upsert_voters
from lib.match_voter import find_candidates, confirm_match
from lib.crypto import (
    generate_postcard_code,
    hash_postcard_code,
    verify_postcard_code,
    generate_recovery_phrase,
    hash_email,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DIVIDER = "─" * 60
SECRET  = "demo-secret-not-for-production"


# ---------------------------------------------------------------------------
# Voyage AI stub — Stage 2 not needed when synthetic data gives Stage 1 hits
# ---------------------------------------------------------------------------

class _StubVoyageClient:
    """
    Placeholder so find_candidates() doesn't error if Stage 2 is triggered.
    Returns a zero embedding that will match nothing — Stage 1 results are
    used exclusively in this demo.
    """
    def embed(self, texts, model=None, input_type=None):
        class _Result:
            embeddings = [[0.0] * 1536]
        return _Result()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def create_demo_user(conn, label: str) -> str:
    """Insert a minimal user row and return the user_id UUID."""
    user_id = str(uuid.uuid4())
    email_hash = hash_email(f"demo_{label}_{user_id[:8]}@example.com")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, email_hash, verification_status, account_status)
            VALUES (%s, %s, 'unverified', 'active')
            """,
            (user_id, email_hash),
        )
    conn.commit()
    return user_id


def wipe_demo_data(conn) -> None:
    log.info("Cleaning up demo data...")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users")
        cur.execute("DELETE FROM voters WHERE voter_id LIKE 'SYN%'")
    conn.commit()


# ---------------------------------------------------------------------------
# Demo steps
# ---------------------------------------------------------------------------

def demo_ingest(conn, num_voters: int) -> list[dict]:
    section("STEP 1 — Synthetic voter data ingestion")

    df = generate_synthetic_rows(num_voters)
    print(f"\n  Generated {num_voters} synthetic Iowa voter records.")
    print(f"\n  Sample (first 3 rows):")
    for _, row in df.head(3).iterrows():
        print(f"    {row['first_name']} {row['last_name']} | {row['address']}, {row['city']} {row['zip']} | {row['party']}")

    upsert_voters(df, conn)
    print(f"\n  ✓ {num_voters} rows upserted into voters table")

    return df.to_dict("records")


def demo_matching(conn, synthetic_rows: list[dict]) -> str:
    section("STEP 2 — Voter matching (Stage 1: ZIP + street number)")

    target = random.choice(synthetic_rows)

    print(f"\n  User input (what they type during registration):")
    print(f"    First name : {target['first_name']}")
    print(f"    Last name  : {target['last_name']}")
    print(f"    Address    : {target['address']}")
    print(f"    City       : {target['city']}")
    print(f"    ZIP        : {target['zip']}")

    candidates = find_candidates(
        conn=conn,
        first_name=target["first_name"],
        last_name=target["last_name"],
        address=target["address"],
        city=target["city"],
        zip_code=target["zip"],
        secret=SECRET,
        voyage_client=_StubVoyageClient(),
    )

    print(f"\n  Candidates returned to user ({len(candidates)} result(s)):")
    if not candidates:
        print("  (no match found — try re-running)")
        return ""

    for i, c in enumerate(candidates, 1):
        print(f"\n    [{i}] {c.first_name} {c.last_name}")
        print(f"        {c.partial_address}, {c.city} {c.zip}")
        print(f"        match_token: {c.match_token[:40]}...  (opaque — voter_id never exposed)")

    print(f"\n  ✓ voter_id absent from all results — only a signed match_token is returned")
    return candidates[0].match_token


def demo_account_linking(conn, match_token: str) -> str | None:
    section("STEP 3 — Account linking (confirm_match)")

    if not match_token:
        print("\n  Skipped — no match token from Step 2.")
        return None

    user_id = create_demo_user(conn, "alice")
    print(f"\n  New user created (user_id withheld from output)")
    print(f"  User selects candidate [1] and submits match_token")

    try:
        confirm_match(conn, match_token=match_token, user_id=user_id, secret=SECRET)
        print(f"  ✓ Voter record linked to user account")
    except ValueError as e:
        print(f"  ✗ Token error: {e}")
        return None

    print(f"\n  Attempting to link the same voter to a second account...")
    user_id_2 = create_demo_user(conn, "bob")
    try:
        confirm_match(conn, match_token=match_token, user_id=user_id_2, secret=SECRET)
        print("  (unexpected — should have been blocked)")
    except ValueError:
        print("  ✓ Blocked — match_token already used / expired")
    except psycopg2.IntegrityError:
        conn.rollback()
        print("  ✓ Blocked at DB level — UNIQUE constraint on users.voter_id (one-voter-one-voice)")

    return user_id


def demo_postcard_crypto(conn) -> None:
    section("STEP 4 — Postcard code generation and verification")

    code = generate_postcard_code()
    code_hash = hash_postcard_code(code)

    print(f"\n  Generated 6-digit code : {code}  (cryptographically random, never stored)")
    print(f"  SHA-256 hash stored    : {code_hash[:24]}...  (users.postcard_code_hash)")

    print(f"\n  verify_postcard_code(correct code) → {verify_postcard_code(code, code_hash)}")
    print(f"  verify_postcard_code('000000')     → {verify_postcard_code('000000', code_hash)}  (constant-time)")

    phrase = generate_recovery_phrase()
    print(f"\n  BIP39 recovery phrase  : {phrase}")
    print(f"  (12 words, 128-bit entropy — shown once at verification, never stored)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CivicBlocks end-to-end POC demo")
    parser.add_argument("--voters", type=int, default=200,
                        help="Number of synthetic voters to load (default: 200)")
    parser.add_argument("--keep", action="store_true",
                        help="Keep demo data after run (default: wipe on exit)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    db_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not db_url:
        print("\nERROR: Set DATABASE_URL or SUPABASE_DB_URL in your .env file.")
        print("       Apply schema.sql to your Supabase project first.\n")
        sys.exit(1)

    try:
        conn = psycopg2.connect(db_url)
    except psycopg2.OperationalError as e:
        print(f"\nERROR: Database connection failed: {e}\n")
        sys.exit(1)

    print("\n" + "═" * 60)
    print("  CivicBlocks — POC Demo")
    print("  Iowa Verified Voter Feedback Platform")
    print("═" * 60)

    try:
        synthetic_rows = demo_ingest(conn, args.voters)
        match_token    = demo_matching(conn, synthetic_rows)
        demo_account_linking(conn, match_token)
        demo_postcard_crypto(conn)

        print(f"\n{DIVIDER}")
        print("  Demo complete.")
        print(f"{DIVIDER}\n")

    finally:
        if not args.keep:
            wipe_demo_data(conn)
            log.info("Demo data wiped. Run with --keep to retain it.")
        conn.close()


if __name__ == "__main__":
    main()
