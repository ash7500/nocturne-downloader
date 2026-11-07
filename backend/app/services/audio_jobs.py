"""Audio-only job manager used by the Nocturne adapter (/api/jobs).

Differences from the legacy job manager on purpose: no cookies, no user-agent rotation,
no player-client tricks. It only runs stock yt-dlp + FFmpeg and keeps files briefly.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadCancelled

logger = logging.getLogger(__name__)
TERMINAL = {"ready", "error"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def root() -> Path:
    p = Path(os.environ.get("DOWNLOADS_DIR", "/app/downloads")) / "nocturne"
    p.mkdir(parents=True, exist_ok=True)
    return p


def check_url(url: str) -> None:
    """Raise ValueError for links the service refuses (bad scheme, private network, not allow-listed)."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host:
        raise ValueError("Enter a full link starting with https://")
    allow = [d.strip().lower() for d in os.environ.get("ALLOWED_DOMAINS", "").split(",") if d.strip()]
    if allow and not any(host == d or host.endswith("." + d) for d in allow):
        raise ValueError("This service only accepts links from: " + ", ".join(allow))
    if os.environ.get("ALLOW_PRIVATE_URLS") == "1":  # local development / tests only
        return
    try:
        addrs = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        raise ValueError("Could not resolve that address.")
    if not addrs or any(not a.is_global for a in addrs):
        raise ValueError("Links to private or local addresses are not allowed.")


class AudioJob:
    def __init__(self, url: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.status = "resolving"  # resolving -> downloading -> converting -> ready | error
        self.meta: Optional[dict] = None
        self.error: Optional[str] = None
        self.file: Optional[Path] = None
        self.cover: Optional[Path] = None
        self.cover_type = "image/jpeg"
        self.cancelled = False
        self.progress = 0.0
        self.created = self.updated = time.time()
        self.dir = root() / self.id

    def set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.updated = time.time()


class AudioJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, AudioJob] = {}
        self.lock = threading.Lock()
        self.pool: Optional[ThreadPoolExecutor] = None

    # ---- public API ----------------------------------------------------
    def active(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status not in TERMINAL)

    def create(self, url: str) -> AudioJob:
        check_url(url)
        with self.lock:
            if self.active() >= _int("MAX_ACTIVE_JOBS", 4):
                raise RuntimeError("The service is busy. Try again in a minute.")
            job = AudioJob(url)
            job.dir.mkdir(parents=True, exist_ok=True)
            self.jobs[job.id] = job
        if self.pool is None:
            self.pool = ThreadPoolExecutor(max_workers=_int("MAX_CONCURRENT_JOBS", 2))
        self.pool.submit(self._work, job)
        return job

    def get(self, job_id: str) -> Optional[AudioJob]:
        return self.jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        job = self.jobs.pop(job_id, None)
        if job is None:
            return False
        job.cancelled = True
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    def purge(self) -> int:
        """Remove expired jobs/files and orphan directories. Called periodically and at startup."""
        ttl, runtime, now, n = _int("JOB_TTL_SECONDS", 900), _int("JOB_MAX_RUNTIME_SECONDS", 1800), time.time(), 0
        for j in list(self.jobs.values()):
            age = now - (j.updated if j.status in TERMINAL else j.created)
            if age > (ttl if j.status in TERMINAL else runtime):
                self.delete(j.id); n += 1
        for d in root().iterdir():
            if d.is_dir() and d.name not in self.jobs and now - d.stat().st_mtime > 60:
                shutil.rmtree(d, ignore_errors=True); n += 1
        return n

    def purge_all(self) -> None:
        for d in root().iterdir():
            shutil.rmtree(d, ignore_errors=True) if d.is_dir() else None

    # ---- worker --------------------------------------------------------
    def _work(self, job: AudioJob) -> None:
        def on_progress(d: dict) -> None:
            if job.cancelled:
                raise DownloadCancelled()
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if d.get("status") == "downloading" and tot:
                job.progress = min(d.get("downloaded_bytes", 0) / tot * 100, 99.0)

        def on_pp(d: dict) -> None:
            if d.get("status") == "started" and job.status == "downloading":
                job.set(status="converting")

        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True, "ignoreconfig": True, "cachedir": False,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(job.dir / "audio.%(ext)s"),
            "max_filesize": _int("MAX_FILESIZE_MB", 100) * 1_000_000,
            "socket_timeout": 30, "retries": 3,
            "progress_hooks": [on_progress], "postprocessor_hooks": [on_pp],
            # AAC in an .m4a container: plays natively on iPhone (Safari / Media Session / lock screen).
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(job.url, download=False)
                if not info:
                    raise ValueError("No media found at this link.")
                if info.get("_type") == "playlist":
                    raise ValueError("Playlists are not supported. Paste a link to a single track.")
                max_dur = _int("MAX_DURATION_SECONDS", 1800)
                if max_dur and (info.get("duration") or 0) > max_dur:
                    raise ValueError(f"This is longer than {max_dur // 60} minutes, which is the limit here.")
                job.set(meta=self._meta(info), status="downloading")
                self._fetch_cover(job, info.get("thumbnail"))
                y.process_ie_result(info, download=True)
            f = job.dir / "audio.m4a"
            if job.cancelled:
                return
            if not f.exists():
                raise RuntimeError("Conversion finished but no audio file was produced.")
            job.set(file=f, progress=100.0, status="ready")
        except DownloadCancelled:
            logger.info("job %s cancelled", job.id)
        except Exception as exc:  # reported to the client as status=error
            job.set(status="error", error=re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "")[:200])
            logger.warning("job %s failed: %s", job.id, job.error)
        finally:
            if job.cancelled:
                shutil.rmtree(job.dir, ignore_errors=True)

    @staticmethod
    def _meta(info: dict) -> dict:
        artist = (info.get("artist") or (info.get("artists") or [None])[0]
                  or re.sub(r" - Topic$", "", info.get("channel") or info.get("uploader") or ""))
        date = str(info.get("release_date") or info.get("upload_date") or "")
        return {
            "title": info.get("track") or info.get("title") or "Untitled",
            "artist": (artist or "").split(",")[0].strip() or "Unknown artist",
            "album": info.get("album") or "",
            "year": date[:4],
            "duration": info.get("duration") or 0,
            "cover": None,  # filled in by the route once a cover file exists (URL depends on the request)
        }

    @staticmethod
    def _fetch_cover(job: AudioJob, url: Optional[str]) -> None:
        """Best effort. The service fetches the thumbnail itself so the browser never needs CORS on a third-party host."""
        if not url or not url.startswith("https://"):
            return
        try:
            check_url(url)
            with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as r:
                ctype = r.headers.get("Content-Type", "")
                data = r.read(5_000_001)
            if ctype.startswith("image/") and 0 < len(data) <= 5_000_000:
                (job.dir / "cover.img").write_bytes(data)
                job.cover, job.cover_type = job.dir / "cover.img", ctype
        except Exception as exc:
            logger.info("cover fetch skipped for %s: %s", job.id, exc)


audio_jobs = AudioJobs()
