# Nocturne adapter contract

Derived from the code in Nocturne v2 `index.html` (function `acquire`). Authentication header: `X-Key` (the only custom header Nocturne sends).

| Nocturne does | Service answers |
|---|---|
| `POST {base}/api/jobs`, JSON `{url}`, checks only `response.ok`, reads `id` | `202 {"id"}` |
| polls `GET {base}/api/jobs/{id}` every 800 ms, reads `status`, `meta`, `error` (ignores the HTTP status) | `{status, meta, error, progress}`; unknown id: `404 {"status":"error","error":"..."}` so the error shows instead of an endless spinner |
| stops on `status === "error"` (shows `error`) or `"ready"`; any other value just keeps waiting | `resolving`, `downloading`, `converting`, `ready`, `error` |
| uses `meta.title/artist/album/year/duration/cover` | same keys; `year` is a 4-char string, `duration` seconds, `cover` URL or `null` |
| `GET {base}/api/jobs/{id}/file`, on resume adds `Range: bytes=N-`, reads `content-length` and `content-type`, accepts 200 or 206 | `audio/mp4`, `Accept-Ranges: bytes`, 206 with `Content-Range` |
| `DELETE {base}/api/jobs/{id}` (result ignored) | `204`, also when the job is already gone |
| `fetch(meta.cover)` **with no headers** | `meta.cover` = `{service}/api/jobs/{id}/cover?t=<HMAC token>`; the token is valid for that job only and is not the access key |

Known mismatches in Nocturne (not changed, since Nocturne was not to be modified):
- If the first cover request fails, Nocturne retries it once after sending `DELETE`, so that retry gets a 404 and the song is saved without a cover.
- Polling never gives up on its own: if the service stops answering with JSON, the sheet stays on "Looking up the song..." until Cancel is tapped.

---

# Video Downloader API — Technical Documentation

> **Audience**: This document is intended to be fed into an AI agent to generate an iOS Shortcut that integrates with the self-hosted Video Downloader backend.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Running the Server](#3-running-the-server)
4. [API Reference](#4-api-reference)
   - [GET /health](#get-health)
   - [POST /jobs](#post-jobs)
   - [GET /jobs](#get-jobs)
   - [GET /jobs/{job_id}](#get-jobsjob_id)
   - [GET /jobs/{job_id}/file](#get-jobsjob_idfile)
   - [DELETE /jobs/{job_id}](#delete-jobsjob_id)
5. [Data Models](#5-data-models)
6. [iOS Shortcut Integration Guide](#6-ios-shortcut-integration-guide)
   - [Step-by-Step Shortcut Flow](#step-by-step-shortcut-flow)
   - [Polling Strategy](#polling-strategy)
   - [Saving to Photos vs Files](#saving-to-photos-vs-files)
   - [Audio-Only Downloads](#audio-only-downloads)
   - [Handling Errors](#handling-errors)
7. [Supported Platforms](#7-supported-platforms)
8. [Anti-Detection & Rate Limiting](#8-anti-detection--rate-limiting)
9. [Environment Variables & Configuration](#9-environment-variables--configuration)
10. [Self-Hosting Guide](#10-self-hosting-guide)
11. [Security Considerations](#11-security-considerations)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Overview

This backend provides a REST API to download videos (and audio) from YouTube, Instagram, TikTok, and 1000+ other sites. It is powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) and built with Python + FastAPI.

**Key characteristics:**

| Property | Value |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI |
| Downloader engine | yt-dlp |
| Transport | HTTP/1.1 and HTTP/2 |
| Auth | None (self-hosted, trust-based) |
| Rate limiting | 60 requests/minute per IP (server-side) |
| Output formats | MP4 (video), M4A (audio) |

---

## 2. Architecture

```
iOS Shortcut
    │
    │  1. POST /jobs  (submit URL)
    ▼
┌─────────────────────────────────────┐
│          FastAPI Application         │
│                                     │
│  Routes ──► JobManager ──► yt-dlp  │
│                 │                   │
│                 └──► /app/downloads │
└─────────────────────────────────────┘
    │
    │  2. GET /jobs/{id}  (poll status)
    │  3. GET /jobs/{id}/file  (fetch binary)
    ▼
iOS Shortcut saves file → Photos / Files app
```

**Download lifecycle:**

```
PENDING  →  DOWNLOADING  →  COMPLETED
                         →  FAILED
```

---

## 3. Running the Server

### Docker Compose (recommended)

```bash
# Clone the repo
git clone https://github.com/taygoober/Downloader.git
cd Downloader

# (Optional) copy and edit environment file
cp .env.example .env

# Start
docker compose up -d

# Server is now available at http://<your-server-ip>:8000
```

### Manual / Development

```bash
cd backend
pip install -r requirements.txt
# ffmpeg must also be installed: brew install ffmpeg  /  apt install ffmpeg
DOWNLOADS_DIR=/tmp/downloads uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Interactive API Docs

Once running, open in a browser:

- **Swagger UI**: `http://<server>:8000/docs`
- **ReDoc**: `http://<server>:8000/redoc`

---

## 4. API Reference

> **Base URL**: `http://<YOUR_SERVER_IP>:8000`
>
> All request/response bodies are **JSON** (`Content-Type: application/json`).
> The file download endpoint (`/jobs/{id}/file`) returns a binary stream.

---

### GET /health

Check if the server is alive.

**Response 200**

```json
{
  "status": "ok",
  "version": "1.0.0",
  "yt_dlp_version": "2026.03.17"
}
```

---

### POST /jobs

Submit a URL for download. The server creates a job and starts downloading asynchronously.

**Request body**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `url` | string | ✅ | — | Full URL to the video/audio. Must start with `http://` or `https://`. |
| `quality` | string | ❌ | `"best"` | `"best"`, `"1080p"`, `"720p"`, `"480p"`, `"360p"`, `"worst"`, `"audio"` |
| `audio_only` | boolean | ❌ | `false` | When `true`, downloads audio only as M4A; overrides `quality`. |
| `cookies` | string | ❌ | `null` | Base64-encoded Netscape cookie file content. Used for age-gated or private content. |

**Example request**

```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "quality": "720p"
}
```

**Response 202 (Accepted)**

Returns a [JobResponse](#jobresponse) object:

```json
{
  "job_id": "3f2a1b4c-...",
  "status": "pending",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "quality": "720p",
  "audio_only": false,
  "progress": 0.0,
  "filename": null,
  "file_size": null,
  "error": null,
  "download_url": null,
  "platform": null
}
```

**Response 422** — Validation error (bad URL, unsupported quality, etc.)

---

### GET /jobs

List all jobs (across all clients since server start).

**Response 200** — Array of [JobResponse](#jobresponse) objects.

```json
[
  { "job_id": "...", "status": "completed", ... },
  { "job_id": "...", "status": "downloading", "progress": 45.2, ... }
]
```

---

### GET /jobs/{job_id}

Get the current status of a single job. **Poll this endpoint** to track download progress.

**Path parameters**

| Parameter | Type | Description |
|---|---|---|
| `job_id` | string (UUID) | The job identifier returned by `POST /jobs`. |

**Response 200** — [JobResponse](#jobresponse)

```json
{
  "job_id": "3f2a1b4c-...",
  "status": "completed",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "quality": "720p",
  "audio_only": false,
  "progress": 100.0,
  "filename": "Rick Astley - Never Gonna Give You Up (Official Music Video).mp4",
  "file_size": 52428800,
  "error": null,
  "download_url": "http://192.168.1.100:8000/jobs/3f2a1b4c-.../file",
  "platform": "youtube",
}
```

**Response 404** — Job not found.

**Progress values:**

| `progress` | Meaning |
|---|---|
| `0.0` | Not started / pending |
| `1 – 99` | Downloading (percentage of total bytes) |
| `100.0` | Fully downloaded and ready |

---

### GET /jobs/{job_id}/file

Download the finished file. Only available when `status == "completed"`.

**Response 200** — Binary file stream

| Header | Value |
|---|---|
| `Content-Type` | `video/mp4` or `audio/mp4` |
| `Content-Disposition` | `attachment; filename="<original filename>"` |

**Response 404** — Job not found or file missing from disk.

**Response 409** — Job is not yet completed (`status != "completed"`).

> **iOS Note**: Use a "Get Contents of URL" action with the `download_url` from the job status, then save the result with "Save File" or "Save to Photo Album".

---

### DELETE /jobs/{job_id}

Delete a job and its downloaded file from the server. Call this after the iOS Shortcut has saved the file locally to free up server disk space.

**Response 204** — Deleted successfully (no body).

**Response 404** — Job not found.

---

## 5. Data Models

### DownloadRequest

```typescript
interface DownloadRequest {
  url: string;              // Required. https:// URL
  quality?: string;         // "best"|"1080p"|"720p"|"480p"|"360p"|"worst"|"audio"
  audio_only?: boolean;     // default false
  cookies?: string;         // base64-encoded Netscape cookie file
}
```

### JobResponse

```typescript
interface JobResponse {
  job_id: string;           // UUID
  status: "pending" | "downloading" | "completed" | "failed";
  url: string;
  quality: string;
  audio_only: boolean;
  progress: number;         // 0.0 – 100.0
  filename: string | null;  // Original filename (e.g. "video.mp4")
  file_size: number | null; // Bytes
  error: string | null;     // Error message if status == "failed"
  download_url: string | null; // Full URL to fetch the file (set when completed)
  platform: string | null;  // "youtube", "instagram", "tiktok", etc.
}
```

### HealthResponse

```typescript
interface HealthResponse {
  status: string;       // "ok"
  version: string;      // API version
  yt_dlp_version: string;
}
```

---

## 6. iOS Shortcut Integration Guide

### Prerequisites

- Server is running and reachable from your iPhone (same Wi-Fi, Tailscale, or public IP).
- Note your server's base URL, e.g., `http://192.168.1.100:8000`.

---

### Step-by-Step Shortcut Flow

Below is the complete logic your iOS Shortcut should implement:

#### Step 1 — Receive the share-sheet URL

```
Action: "Receive [URLs] from Share Sheet"
Variable: sharedURL
```

#### Step 2 — Ask for quality (optional)

```
Action: "Choose from Menu"
Prompt: "Select quality"
Options: Best, 1080p, 720p, 480p, Audio only
Variable: selectedQuality
```

Map menu choice to API value:
- "Best" → `"best"`
- "1080p" → `"1080p"`
- "720p" → `"720p"`
- "480p" → `"480p"`
- "Audio only" → `"audio"` with `audio_only: true`

#### Step 3 — Submit the download job

```
Action: "Get Contents of URL"
  URL: http://<SERVER>/jobs
  Method: POST
  Headers:
    Content-Type: application/json
  Request Body (JSON):
    {
      "url": <sharedURL>,
      "quality": <selectedQuality>,
      "audio_only": <true if audio selected>
    }
Variable: jobResponse
```

Extract `job_id` from `jobResponse`:
```
Action: "Get Value for Key" → key: "job_id"
Variable: jobID
```

#### Step 4 — Show progress notification (optional but recommended)

```
Action: "Show Notification"
  Title: "Download Started"
  Body: "Your video is being downloaded..."
```

#### Step 5 — Poll for completion

Use a **Repeat** loop (up to 60 iterations with a 5-second wait between each):

```
Action: "Repeat"
  Count: 60
  
  Inside loop:
  
  Action: "Wait"
    Duration: 5 seconds
  
  Action: "Get Contents of URL"
    URL: http://<SERVER>/jobs/<jobID>
    Method: GET
  Variable: statusResponse
  
  Action: "Get Value for Key" → key: "status"
  Variable: jobStatus
  
  Action: "If"
    Condition: jobStatus == "completed"
    → Exit Repeat
  
  Action: "If"
    Condition: jobStatus == "failed"
    → Get Value for Key: "error" → Variable: errorMsg
    → Show Alert: "Download failed: <errorMsg>"
    → Stop Shortcut
```

#### Step 6 — Fetch the file

```
Action: "Get Value for Key" → key: "download_url"
Variable: downloadURL

Action: "Get Contents of URL"
  URL: <downloadURL>
  Method: GET
Variable: fileData
```

#### Step 7 — Save the file

**For videos:**
```
Action: "Save to Photo Album"
  Input: <fileData>
```

**For audio / mixed:**
```
Action: "Save File"
  Path: /On My iPhone/Downloads/<filename>
  Input: <fileData>
```

Get the filename:
```
Action: "Get Value for Key" → key: "filename"
Variable: fileName
```

#### Step 8 — Clean up server

```
Action: "Get Contents of URL"
  URL: http://<SERVER>/jobs/<jobID>
  Method: DELETE
```

#### Step 9 — Notify success

```
Action: "Show Notification"
  Title: "Download Complete"
  Body: "Saved to your device!"
```

---

### Polling Strategy

| Poll interval | Max polls | Max wait |
|---|---|---|
| 5 seconds | 60 | ~5 minutes |

For very long videos (>30 min), increase the poll count to 120 (10 minutes).

---

### Saving to Photos vs Files

| Content type | Recommended save action |
|---|---|
| Video (MP4) | "Save to Photo Album" → works with iOS Photos |
| Audio (M4A) | "Save File" → save to Files app |
| Reels/Shorts | "Save to Photo Album" |

---

### Audio-Only Downloads

Send `"audio_only": true` in the request body. The server will extract audio as M4A using FFmpeg. Use `"Save File"` on the iOS side to save to the Files app, then open it in Music or Files.

---

### Handling Errors

| HTTP Status | Meaning | Shortcut action |
|---|---|---|
| `202` | Job created successfully | Extract `job_id` and start polling |
| `404` | Job not found | Show error alert |
| `409` | File not ready | Keep polling |
| `422` | Bad request (invalid URL/quality) | Show validation error |
| `429` | Rate limited (>60 req/min) | Wait 60s and retry |
| `500` | Server error | Show generic error |

**Shortcut error snippet:**

```
Action: "If"
  Condition: <HTTP Status Code> is not 202
  → Show Alert: "Error submitting URL (HTTP <status>)"
  → Stop Shortcut
```

---

## 7. Supported Platforms

The following platforms are explicitly tested and have platform-specific optimizations:

| Platform | URL examples | Notes |
|---|---|---|
| **YouTube** | `youtube.com/watch?v=...`, `youtu.be/...` | Supports playlists (first video only by default); age-gated with cookies |
| **Instagram** | `instagram.com/reel/...`, `instagram.com/p/...` | May require cookies for private accounts |
| **TikTok** | `tiktok.com/@user/video/...`, `vm.tiktok.com/...` | Watermark removal not guaranteed |
| **Twitter / X** | `twitter.com/...`, `x.com/...` | Videos in tweets |
| **Reddit** | `reddit.com/r/.../comments/...` | Hosted videos and v.redd.it links |
| **Facebook** | `facebook.com/...`, `fb.watch/...` | Public videos only without cookies |
| **Vimeo** | `vimeo.com/...` | Private videos need cookies |
| **Twitch** | `twitch.tv/videos/...`, clips | VODs and clips |
| **Dailymotion** | `dailymotion.com/video/...` | |
| **Pinterest** | `pinterest.com/pin/...` | Video pins |
| **Bilibili** | `bilibili.com/video/...` | Chinese platform |

**And 1000+ more** — yt-dlp supports any site that is listed at https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md

---

## 8. Anti-Detection & Rate Limiting

The server implements several measures to avoid being blocked or rate limited by target sites:

### User Agent Rotation

Every request to a target site uses a randomly selected user agent string drawn from a pool of 11 realistic browser UAs spanning Chrome, Firefox, Safari, Edge on desktop and mobile platforms.

### Request Jitter

A random delay of 0.5–2 seconds is added before each download begins to break the bot-like instant-request pattern.

### Platform-Specific Sleep Intervals

| Platform | Min sleep | Max sleep |
|---|---|---|
| YouTube | 2s | 5s |
| Instagram | 3s | 8s |
| TikTok | 2s | 6s |
| Others | yt-dlp default | |

### Retry Logic

| Setting | Value |
|---|---|
| HTTP retries | 5 |
| Fragment retries | 10 |
| File access retries | 5 |
| Extractor retries | 3 |
| Socket timeout | 30s |

### Cookie Support

For sites requiring authentication (private Instagram posts, age-gated YouTube videos), export your browser cookies in Netscape format, base64-encode them, and pass the result in the `cookies` field of the request.

**Example (macOS/Linux):**

```bash
# Export cookies with browser extension like "Get cookies.txt LOCALLY"
# Then encode:
base64 -i cookies.txt | tr -d '\n'
```

Pass the resulting string as `cookies` in the POST body.

---

## 9. Environment Variables & Configuration

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Port the server listens on |
| `DOWNLOADS_DIR` | `/app/downloads` | Absolute path for downloaded files |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins (e.g. `https://myserver.com`) |

---

## 10. Self-Hosting Guide

### Option A: Docker Compose (recommended)

```bash
git clone https://github.com/taygoober/Downloader.git
cd Downloader
cp .env.example .env
# Edit .env if needed
docker compose up -d
```

Check health:
```bash
curl http://localhost:8000/health
```

### Option B: Reverse Proxy with HTTPS (production)

Use Nginx or Caddy as a reverse proxy in front of the Docker container to add HTTPS/TLS. This is required if you want to access the server from outside your LAN.

**Caddy example:**

```caddyfile
downloader.yourdomain.com {
  reverse_proxy localhost:8000
}
```

**Nginx example:**

```nginx
server {
    listen 443 ssl;
    server_name downloader.yourdomain.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Large body size for cookie uploads
        client_max_body_size 10M;
    }
}
```

### Option C: Local Network (Tailscale / VPN)

The simplest approach for personal use:
1. Install [Tailscale](https://tailscale.com) on the server and your iPhone.
2. Use the Tailscale IP as your server address in the iOS Shortcut.
3. No port forwarding or TLS required.

### Persistent Downloads

The `downloads` Docker volume persists downloaded files across container restarts. To mount a specific host path:

```yaml
# docker-compose.yml
volumes:
  - /mnt/data/downloads:/app/downloads
```

---

## 11. Security Considerations

| Risk | Mitigation |
|---|---|
| Open to internet | Use a reverse proxy with authentication (HTTP Basic Auth or API key header in Nginx) |
| Disk space exhaustion | Monitor disk usage; call `DELETE /jobs/{id}` from the Shortcut after saving |
| Cookie leakage | Cookies are only written to a temp file during download and deleted immediately after |
| Rate abuse | Server-side 60 req/min limit per IP |
| CORS | Set `ALLOWED_ORIGINS` to your specific client origin in production |

---

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `status: "failed"`, error mentions "Unable to extract" | Site changed its format | Update yt-dlp: `docker compose pull && docker compose up -d` |
| Job stuck at `downloading` for many minutes | Network issue or very large file | Check server logs: `docker compose logs -f` |
| `409 Conflict` on file download | Job not yet complete | Keep polling until `status == "completed"` |
| `422 Unprocessable Entity` | Invalid URL or quality value | Check that URL starts with `https://` and quality is one of the allowed values |
| `429 Too Many Requests` | Hit 60 req/min limit | Wait 60 seconds; the Shortcut should not poll more often than every 5 seconds |
| Instagram "login required" | Private content | Export and pass cookies |
| YouTube "Sign in to confirm your age" | Age-restricted video | Export and pass YouTube cookies |

---

*Last updated: April 2026*
