"""
routers/voters.py — Voter matching and account linking endpoints.

POST /voters/match   — find up to 3 voter file candidates for a name + address
POST /voters/confirm — link the selected candidate to the user's account
"""

import logging

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, status

from api.deps import get_current_user_id, get_db, get_match_secret, get_voyage_client
from api.schemas import (
    CandidateOut,
    ConfirmMatchRequest,
    MatchVoterRequest,
    MatchVoterResponse,
)
from lib.match_voter import find_candidates, confirm_match, VoterCandidate

log = logging.getLogger(__name__)
router = APIRouter(prefix="/voters", tags=["voters"])


class _NoEmbeddingClient:
    """Fallback when VOYAGE_API_KEY is not set — Stage 2 returns nothing."""
    def embed(self, texts, model=None, input_type=None):
        class _R:
            embeddings = [[0.0] * 1536]
        return _R()


@router.post("/match", response_model=MatchVoterResponse)
def match_voter(
    body:        MatchVoterRequest,
    user_id:     str = Depends(get_current_user_id),
    conn         = Depends(get_db),
    voyage       = Depends(get_voyage_client),
    secret:  str = Depends(get_match_secret),
):
    """
    Search the voter file for records matching the supplied name and address.

    Returns up to 3 candidates. voter_id is never included in the response —
    only an opaque match_token the client passes back to /voters/confirm.

    The user must manually select a candidate. Never auto-confirm.
    """
    voyage_client = voyage or _NoEmbeddingClient()

    candidates: list[VoterCandidate] = find_candidates(
        conn=conn,
        first_name=body.first_name,
        last_name=body.last_name,
        address=body.address,
        city=body.city,
        zip_code=body.zip_code,
        secret=secret,
        voyage_client=voyage_client,
    )

    return MatchVoterResponse(
        candidates=[
            CandidateOut(
                first_name=c.first_name,
                last_name=c.last_name,
                partial_address=c.partial_address,
                city=c.city,
                zip=c.zip,
                match_token=c.match_token,
            )
            for c in candidates
        ]
    )


@router.post("/confirm", status_code=status.HTTP_204_NO_CONTENT)
def confirm_voter(
    body:        ConfirmMatchRequest,
    user_id:     str = Depends(get_current_user_id),
    conn         = Depends(get_db),
    secret:  str = Depends(get_match_secret),
):
    """
    Link the selected voter record to the authenticated user's account.

    Validates the match_token, extracts the voter_id server-side, and writes
    the link. The UNIQUE constraint on users.voter_id enforces one-voter-one-voice
    at the database level — returns 409 if the voter is already claimed.

    On success: triggers postcard send automatically.
    """
    try:
        confirm_match(conn=conn, match_token=body.match_token, user_id=user_id, secret=secret)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This voter record is already linked to an account",
        )
