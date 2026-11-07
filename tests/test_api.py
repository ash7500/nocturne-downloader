"""Tests for the Video Downloader API.

These tests use a fully in-process TestClient (no real downloads) and mock
yt-dlp so the test suite runs without network access or ffmpeg.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Make backend importable when running pytest from the repo root
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Point downloads to a temp dir so tests don't pollute the filesystem
_TMP_DOWNLOADS = tempfile.mkdtemp(prefix="test_downloads_")
os.environ["DOWNLOADS_DIR"] = _TMP_DOWNLOADS

from app.main import app  # noqa: E402 – must come after env var is set
from app.services.job_manager import job_manager  # noqa: E402
from app.models.schemas import JobStatus  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
# All download routes now require the access key (set in conftest.py)
client.headers.update({"X-Key": os.environ["ACCESS_KEY"]})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_file(job_id: str, filename: str = "video.mp4", size: int = 1024) -> Path:
    """Create a fake downloaded file inside the downloads temp directory."""
    job_dir = Path(_TMP_DOWNLOADS) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    f = job_dir / filename
    f.write_bytes(b"0" * size)
    return f


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "yt_dlp_version" in data


# ---------------------------------------------------------------------------
# Download request validation
# ---------------------------------------------------------------------------

class TestDownloadRequestValidation:
    def test_missing_url_returns_422(self) -> None:
        response = client.post("/jobs", json={})
        assert response.status_code == 422

    def test_invalid_url_scheme_returns_422(self) -> None:
        response = client.post("/jobs", json={"url": "ftp://example.com/video"})
        assert response.status_code == 422

    def test_invalid_quality_returns_422(self) -> None:
        response = client.post(
            "/jobs", json={"url": "https://youtube.com/watch?v=abc", "quality": "4k"}
        )
        assert response.status_code == 422

    def test_valid_request_creates_job(self) -> None:
        """A valid POST /jobs should return 202 with a job_id."""
        with patch(
            "app.services.job_manager.download_video",
            new_callable=AsyncMock,
        ) as mock_dl:
            mock_dl.return_value = {
                "filename": "video.mp4",
                "file_path": "/tmp/fake/video.mp4",
                "file_size": 1024,
                "platform": "youtube",
            }
            response = client.post(
                "/jobs",
                json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            )

        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert data["status"] in (JobStatus.PENDING, JobStatus.DOWNLOADING, JobStatus.COMPLETED)
        assert data["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# Job status / list
# ---------------------------------------------------------------------------

class TestJobManagement:
    def test_get_nonexistent_job_returns_404(self) -> None:
        response = client.get(f"/jobs/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_list_jobs_returns_list(self) -> None:
        response = client.get("/jobs")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_delete_nonexistent_job_returns_404(self) -> None:
        response = client.delete(f"/jobs/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_delete_existing_job(self) -> None:
        """Create a job manually, then delete it."""
        job = job_manager.create_job(
            url="https://www.youtube.com/watch?v=test",
            quality="best",
            audio_only=False,
            cookies=None,
        )
        response = client.delete(f"/jobs/{job.job_id}")
        assert response.status_code == 204

        # Should be gone now
        response2 = client.get(f"/jobs/{job.job_id}")
        assert response2.status_code == 404

    def test_download_file_for_pending_job_returns_409(self) -> None:
        job = job_manager.create_job(
            url="https://www.youtube.com/watch?v=test2",
            quality="best",
            audio_only=False,
            cookies=None,
        )
        response = client.get(f"/jobs/{job.job_id}/file")
        assert response.status_code == 409

    def test_download_file_for_completed_job(self) -> None:
        """Simulate a completed job and retrieve the file."""
        job = job_manager.create_job(
            url="https://www.youtube.com/watch?v=test3",
            quality="best",
            audio_only=False,
            cookies=None,
        )
        fake_file = _make_fake_file(job.job_id)
        job.status = JobStatus.COMPLETED
        job.filename = fake_file.name
        job.file_path = str(fake_file)
        job.file_size = fake_file.stat().st_size

        response = client.get(f"/jobs/{job.job_id}/file")
        assert response.status_code == 200
        assert response.content == b"0" * 1024


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

class TestPlatformDetection:
    def test_youtube_detected(self) -> None:
        from app.utils.platform import detect_platform

        assert detect_platform("https://www.youtube.com/watch?v=abc") == "youtube"
        assert detect_platform("https://youtu.be/abc") == "youtube"

    def test_instagram_detected(self) -> None:
        from app.utils.platform import detect_platform

        assert detect_platform("https://www.instagram.com/reel/abc/") == "instagram"

    def test_tiktok_detected(self) -> None:
        from app.utils.platform import detect_platform

        assert detect_platform("https://www.tiktok.com/@user/video/123") == "tiktok"
        assert detect_platform("https://vm.tiktok.com/abc") == "tiktok"

    def test_twitter_detected(self) -> None:
        from app.utils.platform import detect_platform

        assert detect_platform("https://twitter.com/user/status/123") == "twitter"
        assert detect_platform("https://x.com/user/status/123") == "twitter"

    def test_unknown_platform(self) -> None:
        from app.utils.platform import detect_platform

        assert detect_platform("https://example.com/video") is None


# ---------------------------------------------------------------------------
# User-agent rotation
# ---------------------------------------------------------------------------

class TestUserAgentRotation:
    def test_returns_string(self) -> None:
        from app.utils.user_agents import get_random_user_agent

        ua = get_random_user_agent()
        assert isinstance(ua, str)
        assert len(ua) > 20

    def test_rotation_is_random(self) -> None:
        from app.utils.user_agents import get_random_user_agent

        agents = {get_random_user_agent() for _ in range(50)}
        # We have ≥8 desktop UAs; over 50 calls we expect variety
        assert len(agents) > 1


# ---------------------------------------------------------------------------
# Quality map format strings
# ---------------------------------------------------------------------------

class TestQualityMap:
    """Verify that the _QUALITY_MAP format strings prefer iOS-compatible
    H.264 video and that the downloader opts include the FFmpegVideoConvertor
    postprocessor so the final file is always playable in the iOS Photos app."""

    def _get_quality_map(self):
        from app.services.downloader import _QUALITY_MAP
        return _QUALITY_MAP

    def test_all_expected_qualities_present(self) -> None:
        qmap = self._get_quality_map()
        for key in ("best", "1080p", "720p", "480p", "360p", "worst", "audio"):
            assert key in qmap, f"Missing quality key: {key}"
            assert isinstance(qmap[key], str) and qmap[key], (
                f"Quality '{key}' has an empty or non-string format value"
            )

    def test_primary_video_selectors_prefer_h264(self) -> None:
        """Primary (first-preference) bestvideo selectors must request the
        H.264 (vcodec^=avc) codec so the downloaded file is natively
        compatible with the iOS Photos app."""
        qmap = self._get_quality_map()
        for quality, fmt in qmap.items():
            if quality in ("audio", "worst"):
                continue  # no H.264 requirement for audio-only or worst
            primary = fmt.split("/")[0]
            if "bestvideo" in primary:
                assert "vcodec^=avc" in primary, (
                    f"Quality '{quality}' primary segment '{primary}' "
                    "does not prefer H.264 (vcodec^=avc) for iOS compatibility"
                )

    def test_format_strings_use_video_audio_merge(self) -> None:
        """Primary (first-preference) segment for high-quality tiers must
        combine separate video+audio streams so ffmpeg can merge them."""
        for quality in ("best", "1080p", "720p", "480p", "360p"):
            fmt = self._get_quality_map()[quality]
            primary = fmt.split("/")[0]
            assert "+" in primary, (
                f"Quality '{quality}' primary segment '{primary}' does not "
                "merge video+audio streams"
            )

    def test_ydl_opts_include_ffmpeg_video_convertor(self) -> None:
        """_build_ydl_opts must include the FFmpegVideoConvertor postprocessor
        for non-audio downloads so the server always produces an iOS-compatible
        H.264 MP4 file."""
        from app.services.downloader import _build_ydl_opts

        opts = _build_ydl_opts(
            job_id="test-ios-pp",
            quality="best",
            audio_only=False,
            platform=None,
            cookies_content=None,
            progress_hook=lambda d: None,
        )
        postprocessors = opts.get("postprocessors", [])
        pp_keys = [pp.get("key") for pp in postprocessors]
        assert "FFmpegVideoConvertor" in pp_keys, (
            "FFmpegVideoConvertor postprocessor is missing from ydl opts; "
            "downloads may not be iOS Photos-compatible"
        )
        convertor = next(pp for pp in postprocessors if pp.get("key") == "FFmpegVideoConvertor")
        assert convertor.get("preferedformat") == "mp4", (
            "FFmpegVideoConvertor must target 'mp4' format for iOS compatibility"
        )
