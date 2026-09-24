import asyncio
import html as htmlmod
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import quote

import httpx
import yt_dlp
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from imageio_ffmpeg import get_ffmpeg_exe
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

FRONTEND_ORIGINS = {o for o in [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    os.environ.get("FRONTEND_ORIGIN", ""),
] if o}

YT_FORMAT_SELECTOR = (
    "bestvideo[height<=1080][ext=mp4][protocol=https][vcodec^=avc]"
    "+bestaudio[ext=m4a][protocol=https]/"
    "bestvideo[height<=1080][ext=mp4][protocol=https]+bestaudio[ext=m4a][protocol=https]/"
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "best[height<=1080]/best"
)

app = FastAPI(title="Personal Media Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(FRONTEND_ORIGINS),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExtractRequest(BaseModel):
    url: str


ALLOWED_NET_HEADERS = {
    "User-Agent",
    "Accept",
    "Accept-Language",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Dest",
    "Sec-Fetch-Site",
    "Referer",
    "Origin",
}


class MediaItem(BaseModel):
    url: str
    type: str  # "video" | "image"
    filename: str
    thumbnail: str | None = None
    audio_url: str | None = None
    request_headers: dict[str, str] | None = None


class ExtractResponse(BaseModel):
    type: str  # "video" | "image" | "carousel"
    title: str
    items: list[MediaItem]


class StashRequest(BaseModel):
    url: str
    filename: str = "media"
    audio_url: str | None = None
    request_headers: dict[str, str] | None = None


STASH_LOCK = threading.Lock()
STASH: dict[str, tuple[float, dict]] = {}
STASH_TTL = 30 * 60


def _stash_lookup(token: str) -> dict | None:
    now = time.monotonic()
    with STASH_LOCK:
        stale = [k for k, (created, _) in STASH.items() if now - created > STASH_TTL]
        for k in stale:
            STASH.pop(k, None)
        entry = STASH.get(token)
        if not entry:
            return None
        if now - entry[0] > STASH_TTL:
            STASH.pop(token, None)
            return None
        return entry[1]


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "media")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .") or "media"


def _media_kind(info: dict) -> str:
    vcodec = info.get("vcodec")
    if vcodec and vcodec != "none":
        return "video"
    ext = (info.get("ext") or "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif", "avif"):
        return "image"
    url = info.get("url") or ""
    if re.search(r"\.(mp4|mov|webm|avi|m3u8|mkv)(\?|$)", url):
        return "video"
    if re.search(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", url):
        return "image"
    return "image"


def _item_from_entry(entry: dict, base_title: str, suffix: str) -> MediaItem:
    requested = entry.get("requested_formats") or []
    url = entry.get("url") or (requested[0].get("url") if requested else None)
    if not url:
        raise HTTPException(status_code=400, detail="Could not find a media URL in that post.")
    kind = "video" if requested else _media_kind(entry)
    ext = (entry.get("ext") or "").lower()
    if ext == "jpeg":
        ext = "jpg"
    if not ext:
        m = re.search(r"\.(mp4|mov|webm|avi|mkv|jpe?g|png|webp|gif|avif)(\?|$)", url)
        ext = m.group(1) if m else ("jpg" if kind == "image" else "mp4")
    headers = None
    for source in (requested[0] if requested else entry, requested[1] if len(requested) > 1 else None):
        if not source:
            continue
        fmt_headers = source.get("http_headers") or {}
        picked = {k: v for k, v in fmt_headers.items() if k in ALLOWED_NET_HEADERS and isinstance(v, str)}
        if picked:
            headers = picked
            break
    return MediaItem(
        url=url,
        type=kind,
        filename=f"{_safe_filename(base_title)}_{suffix}.{ext}",
        thumbnail=entry.get("thumbnail"),
        audio_url=requested[1].get("url") if len(requested) >= 2 and requested[1].get("url") else None,
        request_headers=headers,
    )


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return (m.group(1) if m else "").lower()


def _want_youtube(url: str) -> bool:
    host = _host_of(url)
    return "youtu" in host


def _is_instagram(url: str) -> bool:
    return "instagram.com" in _host_of(url)


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in _host_of(url)


def _try_tiktok_photo(url: str) -> ExtractResponse | None:
    m = re.search(r"/(?:@[^/]+|t)/photo/(\d+)", url)
    if not m:
        return None
    video_id = m.group(1)
    user_match = re.search(r"@([^/]+)", url)
    user = user_match.group(1) if user_match else "_"
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    cookie = _load_cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    try:
        html = ""
        for _attempt in range(3):
            resp = curl_requests.get(
                f"https://www.tiktok.com/embed/v2/{video_id}",
                headers=headers,
                impersonate="chrome",
                timeout=25,
            )
            resp.raise_for_status()
            html = resp.text
            if "photomode-image" in html:
                break
    except Exception:
        return None
    photo_urls = [
        htmlmod.unescape(u)
        for u in re.findall(r'<img[^>]+src="([^"]+)"', html)
        if "photomode-image" in u
    ]
    photo_urls = list(dict.fromkeys(photo_urls))
    if not photo_urls:
        return None
    title = _try_tiktok_caption(user, video_id) or "tiktok-photo"
    base = _safe_filename(title)
    items = [
        MediaItem(
            url=image_url,
            type="image",
            filename=f"{base}_{i + 1:02d}.jpg",
            thumbnail=photo_urls[0],
        )
        for i, image_url in enumerate(photo_urls)
    ]
    return ExtractResponse(
        type="carousel" if len(items) > 1 else "image",
        title=title,
        items=items,
    )


def _try_tiktok_caption(user: str, video_id: str) -> str | None:
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    cookie = _load_cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    try:
        resp = curl_requests.get(
            f"https://www.tiktok.com/@{user}/video/{video_id}",
            headers=headers,
            impersonate="chrome",
            timeout=25,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return None
    data_match = re.search(
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not data_match:
        return None
    try:
        scope = json.loads(data_match.group(1)).get("__DEFAULT_SCOPE__") or {}
    except Exception:
        return None
    desc = scope.get("webapp.video-detail", {})
    desc = desc.get("itemInfo", {})
    desc = desc.get("itemStruct", {})
    desc = desc.get("desc")
    return str(desc).strip() or None


def _is_twitter(url: str) -> bool:
    host = _host_of(url)
    return "twitter.com" in host or "x.com" in host


_OG_PROPS = [
    r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
]


def _fetch_og(url: str, ua: str) -> ExtractResponse | None:
    try:
        with httpx.Client(follow_redirects=True, timeout=20.0, headers={"User-Agent": ua}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError:
        return None
    for pattern in _OG_PROPS:
        m = re.search(pattern, html, re.IGNORECASE)
        if not m:
            continue
        catch_url = m.group(1).strip().replace("&amp;", "&")
        if not catch_url.startswith(("http://", "https://")):
            continue
        if "og:video" in pattern:
            return ExtractResponse(
                type="video",
                title=_safe_filename("media"),
                items=[MediaItem(url=catch_url, type="video", filename=_safe_filename("media") + ".mp4")],
            )
        return ExtractResponse(
            type="image",
            title=_safe_filename("media"),
            items=[MediaItem(url=catch_url.split("?")[0], type="image", filename=_safe_filename("media") + ".jpg")],
        )
    return None


def _try_instagram_photo(url: str) -> ExtractResponse | None:
    og = _fetch_og(url, MOBILE_UA)
    if og and og.type == "image":
        short = re.search(r"/(?:p|reel|tv)/[^/?#]*", url)
        resolved = og.items[0].url
        if short:
            short_url = f"https://www.instagram.com/p/{short.group(0).rsplit('/', 1)[-1]}/media/?size=l"
            try:
                with httpx.Client(follow_redirects=True, timeout=20.0, headers={"User-Agent": BROWSER_UA}) as client:
                    direct = client.get(short_url)
                    if direct.status_code == 200:
                        resolved = str(direct.url)
            except httpx.HTTPError:
                pass
        return ExtractResponse(
            type="image",
            title=_safe_filename("Instagram image"),
            items=[MediaItem(url=resolved, type="image", thumbnail=resolved, filename=_safe_filename("instagram_image") + ".jpg")],
        )
    return None


def _twitter_media_url(url: str) -> str:
    url = url.strip()
    if "pbs.twimg.com" in url:
        if "?" in url:
            if "format=" not in url and "name=" not in url:
                url += "&format=jpg&name=large"
        else:
            url += "?format=jpg&name=large"
    return url


def _twitter_avatar_url(url: str) -> bool:
    return "/profile_images/" in url or "/profile_banners/" in url


def _try_twitter_og(url: str) -> ExtractResponse | None:
    og = _fetch_og(url, BROWSER_UA)
    if og:
        usable = [it for it in og.items if it.type == "video" or not _twitter_avatar_url(it.url)]
        if usable:
            brand = "twitter-media"
            return ExtractResponse(
                type=og.type,
                title=_safe_filename(brand),
                items=[
                    MediaItem(url=_twitter_media_url(it.url), type=it.type, thumbnail=_twitter_media_url(it.url) if it.type == "image" else None, filename=f"{_safe_filename(brand)}.{'mp4' if it.type == 'video' else 'jpg'}") for it in usable
                ],
            )
    return None


def _extract_media(url: str) -> ExtractResponse:
    if _is_tiktok(url) and "/photo/" in url:
        photo = _try_tiktok_photo(url)
        if photo:
            return photo

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best",
    }
    if _want_youtube(url):
        opts["format"] = YT_FORMAT_SELECTOR
    cookies_file = _cookie_file()
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file
    cookies_browser = os.environ.get("COOKIES_BROWSER", "").strip().lower()
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if _is_instagram(url):
            photo = _try_instagram_photo(url)
            if photo:
                return photo
            raise HTTPException(
                status_code=400,
                detail=(
                    "Instagram did not expose that media to the downloader. Single photos should "
                    "work; reels and multi-image posts often need cookies. "
                    "Set COOKIES_FILE or COOKIES_BROWSER on the backend (see README). "
                    "Original error: " + str(exc)
                ),
            )
        if _is_twitter(url):
            og = _try_twitter_og(url)
            if og:
                return og
            raise HTTPException(
                status_code=400,
                detail=(
                    "X is hiding this tweet from logged-out visitors. Log in to x.com in your "
                    "browser, open this tweet, export your cookies (Get cookies.txt LOCALLY) "
                    "and replace backend/cookies.txt, then restart the backend and retry."
                ),
            )
        if _is_tiktok(url):
            if "/photo/" in url:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "TikTok did not serve its photo gallery this time (they challenge "
                        "automated requests). Wait a few seconds and try again."
                    ),
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    "TikTok blocked the request (they challenge servers without a logged-in session). "
                    "Make sure backend/cookies.txt contains a fresh logged-in TikTok session, "
                    "then redeploy. Original error: " + str(exc)
                ),
            )
        raise HTTPException(status_code=400, detail=f"Could not resolve that link: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unexpected error resolving link: {exc}")

    if not info:
        raise HTTPException(status_code=400, detail="Could not find any media at that link.")

    title = info.get("title") or info.get("id") or "media"
    fallback_thumbnail = info.get("thumbnail")

    entries = info.get("entries")
    if entries:
        items: list[MediaItem] = []
        for i, entry in enumerate(entries, start=1):
            if not entry:
                continue
            sub_entries = entry.get("entries")
            if sub_entries:
                for j, sub in enumerate(sub_entries, start=1):
                    if sub:
                        items.append(_item_from_entry(sub, title, f"{i:02d}_{j:02d}"))
            else:
                items.append(_item_from_entry(entry, title, f"{i:02d}"))
        if not items:
            raise HTTPException(status_code=400, detail="Could not find any media in that carousel.")
        for it in items:
            if not it.thumbnail:
                it.thumbnail = fallback_thumbnail
        return ExtractResponse(type="carousel", title=_safe_filename(title), items=items)

    item = _item_from_entry(info, title, "01")
    if not item.thumbnail:
        item.thumbnail = fallback_thumbnail
    kind = "video" if info.get("requested_formats") else _media_kind(info)
    return ExtractResponse(
        type=kind,
        title=_safe_filename(title),
        items=[item],
    )


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/extract", response_model=ExtractResponse)
def extract(payload: ExtractRequest):
    url = (payload.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) URL.")
    try:
        return _extract_media(url)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unexpected error: {exc}")


def _content_disposition(filename: str) -> str:
    disp_name = _safe_filename(filename)
    ascii_name = "".join(ch if ord(ch) < 128 else "_" for ch in disp_name).strip(" .") or "media"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(disp_name)}"
    )


async def _stream_merged(video_url: str, audio_url: str):
    ffmpeg = get_ffmpeg_exe()
    if not ffmpeg:
        raise HTTPException(status_code=502, detail="ffmpeg is not available on this server.")

    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-user_agent", BROWSER_UA,
        "-referer", "https://www.youtube.com/",
        "-i", video_url,
        "-i", audio_url,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c", "copy",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def drain_stderr():
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
        except Exception:
            pass

    stderr_task = asyncio.create_task(drain_stderr())

    try:
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        return_code = await proc.wait()
        if return_code != 0:
            raise HTTPException(status_code=502, detail="Media merge failed.")
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        stderr_task.cancel()


def _cookie_file() -> str:
    path = os.environ.get("COOKIES_FILE", "cookies.txt").strip()
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def _load_cookie_header() -> str | None:
    path = _cookie_file()
    if not path or not os.path.exists(path):
        return None
    pairs = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split("\t")
                if len(parts) >= 7 and not line.startswith("#"):
                    pairs.append(f"{parts[5]}={parts[6]}")
    except OSError:
        return None
    return "; ".join(pairs) if pairs else None


def _upstream_headers(url: str) -> dict:
    host = _host_of(url)
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if host in ("pbs.twimg.com", "video.twimg.com", "abs.twimg.com"):
        headers["Referer"] = "https://x.com/"
        cookie = _load_cookie_header()
        if cookie:
            headers["Cookie"] = cookie
    elif "tiktok" in host:
        headers["Referer"] = "https://www.tiktok.com/"
        cookie = _load_cookie_header()
        if cookie:
            headers["Cookie"] = cookie
    elif "instagram" in host or "fbcdn" in host:
        headers["Referer"] = "https://www.instagram.com/"
    elif "googlevideo" in host:
        headers["Referer"] = "https://www.youtube.com/"
    elif "fna.fbcdn" in host:
        headers["Referer"] = "https://www.instagram.com/"
    return headers


@app.post("/stash")
def stash(payload: StashRequest):
    _stash_lookup("")
    token = secrets.token_urlsafe(16)
    with STASH_LOCK:
        STASH[token] = (time.monotonic(), payload.model_dump())
    return {"token": token}


@app.get("/download")
async def download(
    url: str | None = Query(None),
    filename: str = Query("media"),
    audio_url: str | None = Query(None),
    request_headers: str | None = Query(None),
    token: str | None = Query(None),
):
    if token:
        entry = _stash_lookup(token)
        if not entry:
            raise HTTPException(status_code=404, detail="This download link has expired.")
        url = entry.get("url") or url
        filename = entry.get("filename") or filename
        audio_url = entry.get("audio_url") or audio_url
        request_headers = json.dumps(entry["request_headers"]) if entry.get("request_headers") else None
    if not url:
        raise HTTPException(status_code=400, detail="Invalid media URL.")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid media URL.")
    if audio_url and not audio_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid audio URL.")

    content_disposition = _content_disposition(filename)

    if audio_url:
        return StreamingResponse(
            _stream_merged(url, audio_url),
            media_type="video/mp4",
            headers={"Content-Disposition": content_disposition, "Cache-Control": "no-store"},
        )

    headers = _upstream_headers(url)
    if request_headers:
        try:
            extra = json.loads(request_headers)
            picked = {k: v for k, v in extra.items() if k in ALLOWED_NET_HEADERS and isinstance(v, str)}
            headers.update(picked)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid request headers.")

    try:
        client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)
        request = client.build_request("GET", url, headers=headers)
        upstream = await client.send(request, stream=True)
        upstream.raise_for_status()
    except httpx.HTTPStatusError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="The media server rejected the request.")
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Could not reach the media server.")

    response_headers = {
        "Content-Disposition": content_disposition,
        "Cache-Control": "no-store",
    }
    upstream_len = upstream.headers.get("content-length")
    if upstream_len:
        response_headers["Content-Length"] = upstream_len

    media_type = upstream.headers.get("content-type", "application/octet-stream")

    async def iter_chunks():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(iter_chunks(), media_type=media_type, headers=response_headers)