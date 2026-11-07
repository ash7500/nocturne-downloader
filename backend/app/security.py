"""Access-key authentication and CORS configuration (shared by every download API)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)


def access_key() -> str:
    return os.environ.get("ACCESS_KEY", "").strip()


def require_key(x_key: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency. Fails closed: with no ACCESS_KEY configured nothing is served."""
    key = access_key()
    if not key:
        raise HTTPException(status_code=503, detail="Service is not configured: set the ACCESS_KEY environment variable.")
    if not x_key or not hmac.compare_digest(x_key.encode(), key.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing access key (X-Key header).")


def cover_token(job_id: str) -> str:
    """Per-job token for the cover URL. Nocturne fetches meta.cover without any headers,
    so the cover link carries this HMAC instead of the real key (valid for that job only)."""
    return hmac.new(access_key().encode(), f"cover:{job_id}".encode(), hashlib.sha256).hexdigest()[:32]


def valid_cover_token(job_id: str, token: Optional[str]) -> bool:
    return bool(access_key() and token and hmac.compare_digest(token, cover_token(job_id)))


def allowed_origins() -> list[str]:
    """Explicit origins only. A wildcard is rejected on purpose so the service is never open to every site."""
    out = []
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(","):
        o = o.strip().rstrip("/")
        if not o:
            continue
        if o == "*":
            logger.warning("ALLOWED_ORIGINS='*' is ignored: list explicit origins instead.")
            continue
        out.append(o)
    return out
