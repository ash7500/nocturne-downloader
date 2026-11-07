# Downloader

Audio download service for the **Nocturne** PWA (plus the original video-download API for iOS Shortcuts).

## Nocturne adapter (`/api/jobs`)

Stateless audio-only service. Nocturne asks for a link, previews the metadata, pulls the finished file to the phone
(with Range resume) and then deletes the job. Nothing here is a library: files are removed on `DELETE` and by an
automatic cleanup (default 15 minutes).

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/jobs` | body `{"url": "..."}` -> `202 {"id": "..."}` |
| `GET` | `/api/jobs/{id}` | `{status, meta, error, progress}`; status is `resolving`, `downloading`, `converting`, `ready` or `error` |
| `GET` | `/api/jobs/{id}/file` | `audio/mp4` (AAC in .m4a, plays on iPhone). Supports `Range` |
| `DELETE` | `/api/jobs/{id}` | `204`, deletes files immediately. Safe to call twice |
| `GET` | `/api/jobs/{id}/cover?t=...` | the link inside `meta.cover` (see API.md) |

Every route except `/health` and the cover link needs the header `X-Key: <ACCESS_KEY>`. If `ACCESS_KEY` is not set the service answers 503 to everything.
The service uses stock yt-dlp and FFmpeg only: no cookies, no user-agent rotation, no tricks to get around a site's protections.
If a site refuses the request, the job ends with `status: "error"` and a message. Download only what you are allowed to keep.

## Deploy on Render (free)

1. Push this repo to your GitHub account.
2. Render Dashboard > **New > Blueprint** > select the repo (it reads `render.yaml`). Or create a **Web Service** by hand:
   Runtime **Docker**, Dockerfile path `./backend/Dockerfile`, Docker context `./backend`, Health check path `/health`.
3. Environment variables:

   | Variable | Value |
   |---|---|
   | `ACCESS_KEY` | Required. Blueprint generates one; or run `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
   | `ALLOWED_ORIGINS` | Required for Nocturne. Only the origin of your PWA, e.g. `https://yourname.github.io` (no path, no trailing slash). `*` is ignored. |
   | `PUBLIC_URL` | Optional. Render provides `RENDER_EXTERNAL_URL`, which is used automatically. |
   | `PORT` | Do not set. Render provides it and the container listens on it. |
   | `JOB_TTL_SECONDS`, `MAX_DURATION_SECONDS`, `MAX_FILESIZE_MB`, `MAX_CONCURRENT_JOBS`, `MAX_ACTIVE_JOBS`, `ALLOWED_DOMAINS` | Optional limits, see `.env.example`. |

4. After deploy, open `https://<your-service>.onrender.com/health`. It should answer `{"status":"ok",...}`.
5. In Nocturne: **Settings > Download service**. Address = the `onrender.com` URL, access key = your `ACCESS_KEY`.

Things to know about the free tier (check Render's current pricing page, they change): the service sleeps when idle, so the first request after a pause is slow;
the disk is temporary (fine here, nothing is kept); memory and CPU are small, so keep `MAX_CONCURRENT_JOBS` low.
Sites such as YouTube often block requests from datacenter addresses, so downloads from them may fail on any hosting provider with an error message.
Direct links to audio files and sites that allow downloads work much better.

## Run locally

```bash
cd backend && pip install -r requirements.txt   # needs ffmpeg
ACCESS_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") ALLOWED_ORIGINS=http://localhost:8080 \
  DOWNLOADS_DIR=/tmp/downloads uvicorn app.main:app --port 8000
```

## Tests

```bash
python -m pytest tests/ -v    # needs ffmpeg and ffprobe; no internet required
```

## Breaking changes from the previous version

- The legacy routes (`/jobs`, ...) now also require `X-Key`. Update iOS Shortcuts to send the header.
- `ALLOWED_ORIGINS` now defaults to empty (no browser origin allowed) and `*` is ignored. Credentials are no longer allowed in CORS.
- The Dockerfile now listens on `$PORT` and installs `curl` (the compose health check needed it).

---

# Original documentation (legacy video API)

Self-hosted video downloader backend — designed to work with an iOS Shortcut.

Downloads videos from YouTube, Instagram, TikTok, and 1000+ other sites via **yt-dlp**.

## Quick Start

```bash
# Requires Docker & Docker Compose
docker compose up -d

# Server is available at http://localhost:8000
# API docs:  http://localhost:8000/docs
```

## Features

- **Wide platform support** — YouTube, Instagram, TikTok, Twitter/X, Reddit, Facebook, Vimeo, Twitch, and 1000+ more sites via yt-dlp
- **Quality selection** — best, 1080p, 720p, 480p, 360p, worst, or audio-only
- **Async job queue** — submit a URL and poll for progress; no blocking
- **Anti-detection** — user-agent rotation, per-request jitter, platform-specific sleep intervals
- **Cookie support** — pass base64-encoded Netscape cookies for private/age-gated content
- **REST API** — JSON in/out; compatible with iOS Shortcuts, curl, and any HTTP client
- **Rate limiting** — 60 requests/minute per IP (server-side, via slowapi)

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/jobs` | Submit a URL for download |
| `GET` | `/jobs` | List all jobs |
| `GET` | `/jobs/{id}` | Get job status / progress |
| `GET` | `/jobs/{id}/file` | Download the finished file |
| `DELETE` | `/jobs/{id}` | Delete a job and its file |

## iOS Shortcut

See [`docs/API.md`](docs/API.md) for the complete technical documentation including a step-by-step iOS Shortcut integration guide.

## Development

```bash
cd backend
pip install -r requirements.txt
# ffmpeg must be installed: brew install ffmpeg  /  apt install ffmpeg
DOWNLOADS_DIR=/tmp/downloads uvicorn app.main:app --reload
```

## Tests

```bash
python -m pytest tests/ -v
```

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Listening port |
| `DOWNLOADS_DIR` | `/app/downloads` | Download storage path |
| `ALLOWED_ORIGINS` | empty | CORS origins (explicit list, no `*`) |
| `ACCESS_KEY` | none | Required. Sent as `X-Key` |
