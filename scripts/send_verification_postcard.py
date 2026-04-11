"""
send_verification_postcard.py — Send a USPS verification postcard via Lob.com.

The postcard is sent to the address on file in the Iowa voter file — never to
a user-supplied address. This is the physical proof-of-residency step that
links a CivicBlocks account to a real registered voter.

Flow:
    1. Look up the user's linked voter record for their mailing address
    2. Generate a cryptographically random 6-digit code (lib/crypto.py)
    3. Hash the code and store in users.postcard_code_hash (plaintext discarded)
    4. Update users.verification_status = 'postcard_sent', users.postcard_sent_at = now()
    5. Send the postcard via Lob.com API
    6. Code expires 30 days after postcard_sent_at (enforced at verification time)

Resend rules:
    - Will not send to already-verified users (verification_status = 'verified')
    - Will not resend if a postcard was sent within the last RESEND_COOLDOWN_DAYS days
    - Will resend if postcard_sent_at is older than RESEND_COOLDOWN_DAYS (resend flow)

Usage:
    python scripts/send_verification_postcard.py --user-id <uuid>
    python scripts/send_verification_postcard.py --user-id <uuid> --force  # bypass cooldown

Security notes:
    - The postcard address is read from voters table, never from user input
    - The plaintext code is generated, used once to render the postcard, then discarded
    - Only the SHA-256 hash is persisted (users.postcard_code_hash)
    - voter_id and user_id are never logged
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.crypto import generate_postcard_code, hash_postcard_code

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

RESEND_COOLDOWN_DAYS = 7    # minimum days between postcard sends
CODE_EXPIRY_DAYS     = 30   # codes expire this many days after postcard_sent_at

# ---------------------------------------------------------------------------
# Postcard HTML content
# Front: verification code prominently displayed
# Lob renders this HTML to a 4x6" postcard
# ---------------------------------------------------------------------------

POSTCARD_FRONT_HTML = """
<html>
<head>
<style>
  body {{
    font-family: Georgia, serif;
    background: #f8f8f0;
    margin: 0;
    padding: 40px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    box-sizing: border-box;
  }}
  .logo {{
    font-size: 22px;
    font-weight: bold;
    color: #2c5f2e;
    letter-spacing: 2px;
    margin-bottom: 12px;
  }}
  .tagline {{
    font-size: 11px;
    color: #666;
    margin-bottom: 30px;
    text-align: center;
  }}
  .code-label {{
    font-size: 12px;
    color: #444;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
  }}
  .code {{
    font-size: 42px;
    font-weight: bold;
    letter-spacing: 10px;
    color: #1a1a1a;
    background: #fff;
    border: 2px solid #2c5f2e;
    padding: 12px 24px;
    border-radius: 4px;
    margin-bottom: 20px;
  }}
  .instructions {{
    font-size: 11px;
    color: #555;
    text-align: center;
    max-width: 300px;
    line-height: 1.6;
  }}
  .expiry {{
    font-size: 10px;
    color: #999;
    margin-top: 12px;
  }}
</style>
</head>
<body>
  <div class="logo">CIVICBLOCKS</div>
  <div class="tagline">Your verified voice in Iowa civic feedback</div>
  <div class="code-label">Your verification code</div>
  <div class="code">{code}</div>
  <div class="instructions">
    Enter this code at civicblocks.io/verify to activate your account
    and start submitting feedback to your elected representatives.
  </div>
  <div class="expiry">Code expires {expiry_date}</div>
</body>
</html>
"""

POSTCARD_BACK_HTML = """
<html>
<head>
<style>
  body {{
    font-family: Georgia, serif;
    padding: 20px 20px 20px 160px;
    font-size: 10px;
    color: #333;
    line-height: 1.5;
  }}
  .intro {{
    margin-bottom: 12px;
    font-size: 11px;
  }}
  .privacy {{
    color: #666;
    font-size: 9px;
    margin-top: 10px;
  }}
</style>
</head>
<body>
  <div class="intro">
    <strong>CivicBlocks</strong> is a free, open source platform for verified Iowa
    voters to share feedback with their elected representatives.
    Every verified voter gets equal weight — no algorithms, no ads.
  </div>
  <div class="privacy">
    This postcard was sent because someone initiated account registration at civicblocks.io
    using the name and address associated with this Iowa voter registration record.
    If you did not request this, you can safely discard this postcard.
    Your voter registration is unaffected. Questions: hello@civicblocks.io
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def fetch_user_and_address(conn, user_id: str) -> dict | None:
    """
    Fetch the user's current status and their voter file mailing address.
    Returns None if the user has no linked voter record.
    voter_id is not included in the returned dict — it stays in the DB.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT
                u.verification_status,
                u.postcard_sent_at,
                v.first_name,
                v.last_name,
                v.address,
                v.city,
                v.zip
            FROM users u
            JOIN voters v ON v.voter_id = u.voter_id
            WHERE u.user_id = %s
              AND u.voter_id IS NOT NULL
            """,
            (user_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def store_code_hash(conn, user_id: str, code_hash: str) -> None:
    """
    Persist the code hash and update verification status.
    The plaintext code is never passed to this function.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET postcard_code_hash   = %s,
                postcard_sent_at     = now(),
                verification_status  = 'postcard_sent'
            WHERE user_id = %s
            """,
            (code_hash, user_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Lob API
# ---------------------------------------------------------------------------

def _send_via_lob(
    api_key: str,
    from_address_id: str,
    first_name: str,
    last_name: str,
    address: str,
    city: str,
    zip_code: str,
    plaintext_code: str,
    expiry_date: str,
) -> str:
    """
    Submit the postcard to Lob via their REST API. Returns the Lob postcard ID.
    plaintext_code is used only to render the HTML — it must be
    discarded by the caller immediately after this function returns.
    Authenticates with HTTP Basic Auth (api_key as username, empty password).
    """
    front_html = POSTCARD_FRONT_HTML.format(
        code=plaintext_code,
        expiry_date=expiry_date,
    )

    payload = {
        "to[name]":             f"{first_name} {last_name}",
        "to[address_line1]":    address,
        "to[address_city]":     city,
        "to[address_state]":    "IA",
        "to[address_zip]":      zip_code,
        "to[address_country]":  "US",
        "from":                 from_address_id,
        "front":                front_html,
        "back":                 POSTCARD_BACK_HTML,
        "size":                 "4x6",
    }

    response = requests.post(
        "https://api.lob.com/v1/postcards",
        data=payload,
        auth=(api_key, ""),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["id"]


# ---------------------------------------------------------------------------
# Core send logic
# ---------------------------------------------------------------------------

def send_for_user(conn, user_id: str, force: bool = False) -> bool:
    """
    Send a verification postcard to the voter file address for this user.

    Args:
        conn:    psycopg2 connection
        user_id: UUID of the user requesting verification
        force:   bypass the resend cooldown window (use with caution)

    Returns:
        True if the postcard was sent, False if skipped (with reason logged).

    This function is designed to be called by the API layer when a user
    initiates or re-requests verification. It can also be called from the CLI.
    """
    api_key = os.getenv("LOB_API_KEY")
    from_address_id = os.getenv("LOB_FROM_ADDRESS_ID")

    if not api_key or not from_address_id:
        log.error("LOB_API_KEY and LOB_FROM_ADDRESS_ID must be set in .env")
        return False

    record = fetch_user_and_address(conn, user_id)
    if record is None:
        log.warning("User has no linked voter record — cannot send postcard")
        return False

    # Guard: already verified
    if record["verification_status"] == "verified":
        log.info("User is already verified — postcard not sent")
        return False

    # Guard: resend cooldown
    if not force and record["postcard_sent_at"] is not None:
        sent_at = record["postcard_sent_at"]
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - sent_at
        if age < timedelta(days=RESEND_COOLDOWN_DAYS):
            days_remaining = RESEND_COOLDOWN_DAYS - age.days
            log.info(
                "Postcard sent recently — resend available in %d day(s). Use --force to override.",
                days_remaining,
            )
            return False

    # Generate code — plaintext lives only in this local scope
    plaintext_code = generate_postcard_code()
    code_hash = hash_postcard_code(plaintext_code)

    expiry_date = (
        datetime.now(timezone.utc) + timedelta(days=CODE_EXPIRY_DAYS)
    ).strftime("%B %-d, %Y")

    # Persist hash before calling Lob — if Lob fails, the hash is stored and
    # the user can retry; if we did it after and the DB write failed, we'd have
    # sent a code with no way to verify it.
    store_code_hash(conn, user_id, code_hash)

    try:
        lob_id = _send_via_lob(
            api_key=api_key,
            from_address_id=from_address_id,
            first_name=record["first_name"],
            last_name=record["last_name"],
            address=record["address"],
            city=record["city"],
            zip_code=record["zip"],
            plaintext_code=plaintext_code,
            expiry_date=expiry_date,
        )
    except Exception as e:
        log.error("Lob API call failed: %s", e)
        log.warning(
            "Code hash was stored before the Lob call. "
            "User can retry — a new code will be generated on next attempt."
        )
        return False
    finally:
        # Explicitly clear the plaintext code from local scope
        del plaintext_code

    log.info("Postcard submitted to Lob (id: %s)", lob_id)
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a USPS verification postcard via Lob")
    parser.add_argument(
        "--user-id",
        required=True,
        help="UUID of the user to send the postcard to",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"Bypass the {RESEND_COOLDOWN_DAYS}-day resend cooldown",
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
        sent = send_for_user(conn, user_id=args.user_id, force=args.force)
    finally:
        conn.close()

    sys.exit(0 if sent else 1)


if __name__ == "__main__":
    main()
