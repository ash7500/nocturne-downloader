"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.routes import download, health, nocturne
from app.security import access_key, allowed_origins, require_key
from app.services.audio_jobs import audio_jobs

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure downloads directory exists
# ---------------------------------------------------------------------------
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/app/downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Rate limiter (slowapi / limits)
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

# ---------------------------------------------------------------------------
# Lifespan: purge leftovers at startup, then expire old Nocturne jobs/files every minute
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    if len(access_key()) < 16:
        logger.warning("ACCESS_KEY is missing or shorter than 16 characters; the API refuses requests until it is set.")
    audio_jobs.purge_all()

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                audio_jobs.purge()
            except Exception:  # never let cleanup stop the service
                logger.exception("purge failed")

    task = asyncio.create_task(sweeper())
    try:
        yield
    finally:
        task.cancel()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    lifespan=lifespan,
    title="Video Downloader API",
    description=(
        "Self-hosted backend for downloading videos from YouTube, Instagram, TikTok, "
        "and 1000+ other sites via yt-dlp. Designed for use with an iOS Shortcut."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS: explicit origins only (ALLOWED_ORIGINS, comma separated). Default = no cross-origin access.
# Credentials are not used (auth is the X-Key header), so allow_credentials stays off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["X-Key", "Content-Type", "Range"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
    max_age=600,
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(health.router)
# Every download route (legacy /jobs and the Nocturne /api/jobs) requires the access key.
app.include_router(download.router, dependencies=[Depends(require_key)])
app.include_router(nocturne.router)
app.include_router(nocturne.cover_router)


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
