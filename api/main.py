"""
main.py — CivicBlocks FastAPI application.

Run locally:
    uvicorn api.main:app --reload

Production (Railway):
    uvicorn api.main:app --host 0.0.0.0 --port $PORT
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.deps import init_db_pool, init_voyage_client
from api.routers import voters, verification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up CivicBlocks API...")
    init_db_pool()
    init_voyage_client()
    log.info("Ready.")
    yield
    log.info("Shutting down.")


app = FastAPI(
    title="CivicBlocks API",
    description="Verified Iowa voter civic feedback platform",
    version="0.1.0",
    lifespan=lifespan,
    # Disable docs in production via env var if desired
)

# ---------------------------------------------------------------------------
# CORS
# Tighten allowed_origins before public launch.
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # TODO: restrict to your frontend domain before launch
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(voters.router)
app.include_router(verification.router)


# ---------------------------------------------------------------------------
# Health check — unauthenticated, used by Railway for health monitoring
# ---------------------------------------------------------------------------
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
