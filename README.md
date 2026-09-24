# ALL-MEDIA-SAVER-BACKEND

Streaming FastAPI + yt-dlp backend for the family media saver (TikTok, Instagram,
YouTube, X). Resolves direct media URLs (`/extract`) and streams them to the
browser (`/download`) without storing anything on disk. `/stash` keeps download
payloads in memory so URLs stay short. YouTube video+audio are merged in memory
with a bundled ffmpeg (`imageio-ffmpeg`).

> This repo contains `cookies.txt` (your logged-in browser sessions for TikTok
> and X, required for reliable extraction). Keep this repository **private**.

## Run locally

```
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Health check: `GET /` -> `{"status":"ok"}`.

## Env

- `FRONTEND_ORIGIN` = frontend origin (for CORS).
- `COOKIES_FILE` = path to the cookies file (default `cookies.txt`).
- `COOKIES_BROWSER` = optional browser cookie source.