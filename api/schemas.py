"""
schemas.py — Pydantic request and response models for the CivicBlocks API.

voter_id is intentionally absent from all response models.
"""

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Voter matching
# ---------------------------------------------------------------------------

class MatchVoterRequest(BaseModel):
    first_name: str = Field(..., min_length=1)
    last_name:  str = Field(..., min_length=1)
    address:    str = Field(..., min_length=1)
    city:       str = Field(..., min_length=1)
    zip_code:   str = Field(..., min_length=5, max_length=10)


class CandidateOut(BaseModel):
    first_name:      str
    last_name:       str
    partial_address: str
    city:            str
    zip:             str
    match_token:     str   # opaque HMAC-signed token — voter_id never exposed


class MatchVoterResponse(BaseModel):
    candidates: list[CandidateOut]


class ConfirmMatchRequest(BaseModel):
    match_token: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Postcard verification
# ---------------------------------------------------------------------------

class SendPostcardResponse(BaseModel):
    sent:    bool
    message: str


class VerifyCodeRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class VerifyCodeResponse(BaseModel):
    verified: bool
