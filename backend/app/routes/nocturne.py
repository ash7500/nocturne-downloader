"""Nocturne adapter: the /api/jobs contract consumed by the Nocturne PWA (Download service).

Contract, as read from Nocturne's index.html (see docs/API.md):
  POST   /api/jobs            {url}            -> 2xx {id}
  GET    /api/jobs/{id}                        -> {status, meta?, error?}   status: resolving|downloading|converting|ready|error
  GET    /api/jobs/{id}/file  (Range)          -> audio/mp4, 200/206 with Content-Length
  DELETE /api/jobs/{id}                        -> any 2xx
  GET    /api/jobs/{id}/cover?t=<token>        -> image (this is what meta.cover points to)
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from app.security import cover_token, require_key, valid_cover_token, access_key
from app.services.audio_jobs import audio_jobs

router = APIRouter(prefix="/api", dependencies=[Depends(require_key)], tags=["Nocturne"])
cover_router = APIRouter(prefix="/api", tags=["Nocturne"])  # authenticated by header OR per-job token


class NewJob(BaseModel):
    url: str


def _public_base(request: Request) -> str:
    return (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or str(request.base_url)).rstrip("/")


@router.post("/jobs", status_code=202)
async def create_job(body: NewJob, request: Request):
    try:
        job = audio_jobs.create(body.url.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return {"id": job.id}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, request: Request):
    job = audio_jobs.get(job_id)
    if job is None:
        # Nocturne reads the JSON body even on errors, so include status/error and not just "detail".
        return JSONResponse(status_code=404, content={"status": "error", "error": "This download expired. Start it again."})
    meta = dict(job.meta) if job.meta else None
    if meta and job.cover:
        meta["cover"] = f"{_public_base(request)}/api/jobs/{job.id}/cover?t={cover_token(job.id)}"
    return {"status": job.status, "meta": meta, "error": job.error, "progress": round(job.progress, 1)}


@router.get("/jobs/{job_id}/file")
async def job_file(job_id: str):
    job = audio_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "ready" or not job.file or not job.file.exists():
        raise HTTPException(status_code=409, detail=f"Job is not ready (status: {job.status})")
    # Starlette's FileResponse answers Range requests (206 + Content-Range), which Nocturne uses to resume.
    return FileResponse(job.file, media_type="audio/mp4")


@router.delete("/jobs/{job_id}", status_code=204, response_class=Response)
async def delete_job(job_id: str):
    audio_jobs.delete(job_id)  # idempotent: the cleanup task may already have removed it
    return Response(status_code=204)


@cover_router.get("/jobs/{job_id}/cover")
async def job_cover(job_id: str, request: Request, t: Optional[str] = None):
    if not valid_cover_token(job_id, t):
        require_key(request.headers.get("x-key"))
    job = audio_jobs.get(job_id)
    if job is None or not job.cover or not job.cover.exists():
        raise HTTPException(status_code=404, detail="No cover")
    return FileResponse(job.cover, media_type=job.cover_type)
