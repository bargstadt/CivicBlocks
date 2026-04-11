"""
routers/verification.py — Postcard sending and code verification endpoints.

POST /verification/send   — send (or resend) a USPS verification postcard
POST /verification/verify — verify the 6-digit code from the postcard
"""

import logging
from datetime import datetime, timedelta, timezone

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, status

from api.deps import get_current_user_id, get_db
from api.schemas import SendPostcardResponse, VerifyCodeRequest, VerifyCodeResponse
from lib.crypto import verify_postcard_code
from scripts.send_verification_postcard import send_for_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/verification", tags=["verification"])

CODE_EXPIRY_DAYS = 30


@router.post("/send", response_model=SendPostcardResponse)
def send_postcard(
    user_id: str = Depends(get_current_user_id),
    conn         = Depends(get_db),
):
    """
    Send a USPS verification postcard to the address on file in the voter record.

    Will not send if:
      - The user has no linked voter record (must call /voters/confirm first)
      - The user is already verified
      - A postcard was sent within the last 7 days (resend cooldown)

    Returns 409 if the user is not yet linked to a voter record.
    """
    # Check user has a linked voter record before attempting send
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT voter_id, verification_status FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if row["voter_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No voter record linked. Complete voter matching first.",
        )

    if row["verification_status"] == "verified":
        return SendPostcardResponse(sent=False, message="Account is already verified")

    sent = send_for_user(conn=conn, user_id=user_id)

    if sent:
        return SendPostcardResponse(sent=True, message="Postcard submitted via Lob")
    else:
        return SendPostcardResponse(
            sent=False,
            message="Postcard not sent — already sent recently or configuration error. Check logs.",
        )


@router.post("/verify", response_model=VerifyCodeResponse)
def verify_code(
    body:    VerifyCodeRequest,
    user_id: str = Depends(get_current_user_id),
    conn         = Depends(get_db),
):
    """
    Verify the 6-digit code from the postcard.

    On success: sets verification_status = 'verified', verified_at = now(),
    and clears postcard_code_hash (code cannot be reused).

    Returns 400 if the code is wrong or expired.
    Returns 409 if the account is already verified.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT verification_status, postcard_code_hash, postcard_sent_at
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if row["verification_status"] == "verified":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account is already verified",
        )

    if not row["postcard_code_hash"] or not row["postcard_sent_at"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No postcard has been sent. Request a postcard first.",
        )

    # Check expiry
    sent_at = row["postcard_sent_at"]
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > sent_at + timedelta(days=CODE_EXPIRY_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification code has expired. Request a new postcard.",
        )

    if not verify_postcard_code(body.code, row["postcard_code_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect verification code",
        )

    # Mark verified and clear the code hash so it cannot be reused
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET verification_status = 'verified',
                verified_at          = now(),
                postcard_code_hash   = NULL
            WHERE user_id = %s
            """,
            (user_id,),
        )
    conn.commit()

    return VerifyCodeResponse(verified=True)
