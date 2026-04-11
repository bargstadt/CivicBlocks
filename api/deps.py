"""
deps.py — FastAPI dependencies: database connections, auth, Voyage AI client.
"""

import os
from typing import Generator

import jwt
import psycopg2
import psycopg2.pool
import voyageai
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ---------------------------------------------------------------------------
# Database connection pool
# Initialised once at app startup via init_db_pool() called from main.py.
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def init_db_pool() -> None:
    global _pool
    db_url = os.environ["DATABASE_URL"]
    _pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=10, dsn=db_url)


def get_db() -> Generator:
    """Yield a psycopg2 connection from the pool; return it on exit."""
    if _pool is None:
        raise RuntimeError("Database pool not initialised")
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# Supabase JWT authentication
# Validates the Bearer token issued by Supabase Auth and extracts user_id.
# ---------------------------------------------------------------------------

_bearer = HTTPBearer()


def get_current_user_id(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    Validate a Supabase-issued JWT and return the user_id (sub claim).
    Raises 401 if the token is missing, invalid, or expired.
    """
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET")
    if not jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_JWT_SECRET not configured",
        )

    try:
        payload = jwt.decode(
            creds.credentials,
            jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing sub claim",
        )
    return user_id


# ---------------------------------------------------------------------------
# Voyage AI client (optional — Stage 2 matching disabled if key absent)
# ---------------------------------------------------------------------------

_voyage_client: voyageai.Client | None = None


def init_voyage_client() -> None:
    global _voyage_client
    api_key = os.environ.get("VOYAGE_API_KEY")
    if api_key:
        _voyage_client = voyageai.Client(api_key=api_key)


def get_voyage_client() -> voyageai.Client | None:
    return _voyage_client


# ---------------------------------------------------------------------------
# Match token signing secret
# ---------------------------------------------------------------------------

def get_match_secret() -> str:
    secret = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_SERVICE_ROLE_KEY not configured",
        )
    return secret
