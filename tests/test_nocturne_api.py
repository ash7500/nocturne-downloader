"""Tests for the Nocturne adapter (/api/jobs). The e2e test runs the real yt-dlp + FFmpeg pipeline
against a tiny local HTTP server (no internet needed)."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from app.main import app  # noqa: E402
from app.services.audio_jobs import AudioJob, audio_jobs  # noqa: E402

KEY = os.environ["ACCESS_KEY"]
H = {"X-Key": KEY}
ORIGIN = "https://nocturne.example.app"
c = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def tone_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "libmp3lame", str(d / "tone.mp3")], check=True)
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/tone.mp3"
    srv.shutdown()


def wait(job_id, want="ready", timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/jobs/{job_id}", headers=H).json()
        if s["status"] in (want, "error"):
            return s
        time.sleep(0.3)
    raise AssertionError("timeout")


# ---------------------------------------------------------------- auth
class TestAuth:
    @pytest.mark.parametrize("method,path", [("post", "/api/jobs"), ("get", "/api/jobs/x"), ("get", "/api/jobs/x/file"), ("delete", "/api/jobs/x"),
                                              ("post", "/jobs"), ("get", "/jobs"), ("get", "/jobs/x/file"), ("delete", "/jobs/x")])
    def test_missing_key_rejected(self, method, path):
        r = getattr(c, method)(path, **({"json": {"url": "https://a.b/c"}} if method == "post" else {}))
        assert r.status_code == 401

    def test_wrong_key_rejected(self):
        assert c.post("/api/jobs", json={"url": "https://a.b/c"}, headers={"X-Key": "nope"}).status_code == 401

    def test_unconfigured_service_fails_closed(self, monkeypatch):
        monkeypatch.delenv("ACCESS_KEY")
        assert c.post("/api/jobs", json={"url": "https://a.b/c"}, headers=H).status_code == 503

    def test_health_stays_public(self):
        assert c.get("/health").status_code == 200


# ---------------------------------------------------------------- CORS
class TestCors:
    def test_preflight_allowed_origin(self):
        r = c.options("/api/jobs", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-key"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ORIGIN
        assert "x-key" in r.headers["access-control-allow-headers"].lower()

    def test_preflight_range_header_for_resume(self):
        r = c.options("/api/jobs/x/file", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "range,x-key"})
        assert r.status_code == 200 and "range" in r.headers["access-control-allow-headers"].lower()

    def test_other_origin_gets_no_cors_headers(self):
        r = c.options("/api/jobs", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in r.headers

    def test_wildcard_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_ORIGINS", "*, https://ok.example/")
        from app.security import allowed_origins
        assert allowed_origins() == ["https://ok.example"]

    def test_content_length_exposed(self):
        r = c.get("/api/jobs/none", headers={**H, "Origin": ORIGIN})
        assert "content-length" in r.headers["access-control-expose-headers"].lower()


# ---------------------------------------------------------------- validation / errors
class TestValidation:
    def test_bad_scheme(self):
        assert c.post("/api/jobs", json={"url": "ftp://x/y"}, headers=H).status_code == 400

    def test_missing_url(self):
        assert c.post("/api/jobs", json={}, headers=H).status_code == 422

    def test_private_addresses_blocked(self, monkeypatch):
        monkeypatch.setenv("ALLOW_PRIVATE_URLS", "0")
        for u in ("http://127.0.0.1:8000/x", "http://localhost/x", "http://169.254.169.254/latest", "http://10.0.0.5/a"):
            assert c.post("/api/jobs", json={"url": u}, headers=H).status_code == 400, u

    def test_domain_allowlist(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_DOMAINS", "archive.org")
        assert c.post("/api/jobs", json={"url": "https://example.com/a.mp3"}, headers=H).status_code == 400

    def test_unknown_job_body_matches_what_nocturne_reads(self):
        r = c.get("/api/jobs/missing", headers=H)
        assert r.status_code == 404 and r.json()["status"] == "error" and r.json()["error"]

    def test_delete_is_idempotent(self):
        assert c.delete("/api/jobs/missing", headers=H).status_code == 204

    def test_file_before_ready_is_409(self):
        j = AudioJob("https://x/y"); j.dir.mkdir(parents=True); audio_jobs.jobs[j.id] = j
        assert c.get(f"/api/jobs/{j.id}/file", headers=H).status_code == 409
        audio_jobs.delete(j.id)


# ---------------------------------------------------------------- full cycle
class TestFullCycle:
    def test_create_poll_download_resume_delete(self, tone_server):
        r = c.post("/api/jobs", json={"url": tone_server}, headers=H)
        assert r.status_code == 202
        job_id = r.json()["id"]
        assert set(r.json()) == {"id"}

        s = wait(job_id)
        assert s["status"] == "ready", s
        # exactly the fields Nocturne reads: status, meta{title,artist,album,year,duration,cover}, error
        assert {"title", "artist", "album", "year", "duration", "cover"} <= set(s["meta"])
        assert isinstance(s["meta"]["title"], str) and s["meta"]["title"]
        assert isinstance(s["meta"]["duration"], (int, float))

        full = c.get(f"/api/jobs/{job_id}/file", headers=H)
        assert full.status_code == 200 and full.headers["content-type"] == "audio/mp4"
        assert int(full.headers["content-length"]) == len(full.content) > 1000
        assert full.headers.get("accept-ranges") == "bytes"

        # resume exactly the way Nocturne does it: Range: bytes=<got>-
        got = 500
        part = c.get(f"/api/jobs/{job_id}/file", headers={**H, "Range": f"bytes={got}-"})
        assert part.status_code == 206 and part.content == full.content[got:]
        assert part.headers["content-range"] == f"bytes {got}-{len(full.content) - 1}/{len(full.content)}"

        # the file really is AAC in an MP4/M4A container (iPhone-compatible), not just a renamed file
        f = Path(os.environ["DOWNLOADS_DIR"]) / "probe.m4a"; f.write_bytes(full.content)
        p = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(f)]))
        assert [s_["codec_name"] for s_ in p["streams"]] == ["aac"] and "mp4" in p["format"]["format_name"]
        assert abs(float(p["format"]["duration"]) - 3) < 0.5

        job_dir = Path(os.environ["DOWNLOADS_DIR"]) / "nocturne" / job_id
        assert job_dir.exists()
        assert c.delete(f"/api/jobs/{job_id}", headers=H).status_code == 204
        assert not job_dir.exists()
        assert c.get(f"/api/jobs/{job_id}", headers=H).status_code == 404
        assert c.get(f"/api/jobs/{job_id}/file", headers=H).status_code == 404

    def test_failed_source_reports_error_status(self, tone_server):
        job_id = c.post("/api/jobs", json={"url": tone_server.replace("tone.mp3", "missing.mp3")}, headers=H).json()["id"]
        s = wait(job_id)
        assert s["status"] == "error" and s["error"]
        c.delete(f"/api/jobs/{job_id}", headers=H)


# ---------------------------------------------------------------- cover link
class TestCover:
    def _job_with_cover(self):
        j = AudioJob("https://x/y"); j.dir.mkdir(parents=True); (j.dir / "cover.img").write_bytes(b"\xff\xd8\xffJPEG")
        j.set(status="ready", meta={"title": "t", "artist": "a", "album": "", "year": "", "duration": 1, "cover": None}, cover=j.dir / "cover.img")
        audio_jobs.jobs[j.id] = j
        return j

    def test_status_exposes_cover_url_without_leaking_key(self):
        j = self._job_with_cover()
        url = c.get(f"/api/jobs/{j.id}", headers=H).json()["meta"]["cover"]
        assert f"/api/jobs/{j.id}/cover?t=" in url and KEY not in url
        # Nocturne fetches the cover with NO headers: the token alone must work
        r = c.get(url.replace("http://testserver", ""))
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        audio_jobs.delete(j.id)

    def test_cover_needs_token_or_key(self):
        j = self._job_with_cover()
        assert c.get(f"/api/jobs/{j.id}/cover").status_code == 401
        assert c.get(f"/api/jobs/{j.id}/cover?t=bad").status_code == 401
        assert c.get(f"/api/jobs/{j.id}/cover", headers=H).status_code == 200
        other = self._job_with_cover()
        tok = c.get(f"/api/jobs/{other.id}", headers=H).json()["meta"]["cover"].split("t=")[1]
        assert c.get(f"/api/jobs/{j.id}/cover?t={tok}").status_code == 401  # token is per job
        audio_jobs.delete(j.id); audio_jobs.delete(other.id)


# ---------------------------------------------------------------- cleanup
class TestCleanup:
    def test_expired_jobs_and_orphans_are_purged(self, monkeypatch):
        monkeypatch.setenv("JOB_TTL_SECONDS", "10")
        old, fresh = AudioJob("https://x/old"), AudioJob("https://x/new")
        for j in (old, fresh):
            j.dir.mkdir(parents=True); (j.dir / "audio.m4a").write_bytes(b"x"); j.set(status="ready"); audio_jobs.jobs[j.id] = j
        old.updated -= 60
        orphan = Path(os.environ["DOWNLOADS_DIR"]) / "nocturne" / "orphan"; orphan.mkdir(); os.utime(orphan, (time.time() - 600,) * 2)
        audio_jobs.purge()
        assert old.id not in audio_jobs.jobs and not old.dir.exists()
        assert fresh.id in audio_jobs.jobs and fresh.dir.exists()
        assert not orphan.exists()
        audio_jobs.delete(fresh.id)

    def test_stuck_job_is_cancelled_and_removed(self, monkeypatch):
        monkeypatch.setenv("JOB_MAX_RUNTIME_SECONDS", "10")
        j = AudioJob("https://x/stuck"); j.dir.mkdir(parents=True); j.created -= 60; audio_jobs.jobs[j.id] = j
        audio_jobs.purge()
        assert j.cancelled and j.id not in audio_jobs.jobs and not j.dir.exists()

    def test_delete_while_running_cancels(self):
        j = AudioJob("https://x/run"); j.dir.mkdir(parents=True); audio_jobs.jobs[j.id] = j
        assert c.delete(f"/api/jobs/{j.id}", headers=H).status_code == 204
        assert j.cancelled and not j.dir.exists()
