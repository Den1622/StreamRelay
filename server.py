"""
Twitch Stream Relay — Backend

Architecture:
  1. Streamlink extracts HLS playlist URLs from Twitch (token negotiation)
  2. Server proxies ONLY the m3u8 playlists (a few KB of text every 2-4s)
  3. Segment URLs inside playlists are rewritten to absolute Twitch CDN URLs
  4. Browser fetches .ts/.m4s video segments DIRECTLY from Twitch CDN

Result: server bandwidth ≈ 0, all video data uses client bandwidth.
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import httpx
import streamlink

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Twitch Stream Relay")

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Streamlink session (reuse across requests) ──

_session = None

def get_session() -> streamlink.Streamlink:
    global _session
    if _session is None:
        _session = streamlink.Streamlink()
        # Optional: Twitch Turbo OAuth for ad-free
        # _session.set_plugin_option("twitch", "api-header", [
        #     "Authorization=OAuth YOUR_TOKEN_HERE"
        # ])
    return _session


# ── Shared HTTP client ──

_http: httpx.AsyncClient | None = None

async def get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    return _http


# ── API: resolve stream → return proxy-wrapped playlist URL ──

@app.get("/api/stream")
async def get_stream(channel: str = Query(..., min_length=1, max_length=50)):
    """
    Resolve a Twitch channel to playable HLS URLs.
    Returns playlist URLs wrapped through /api/proxy so the browser never
    needs to hit Twitch's playlist endpoints directly (CORS-safe).
    Video segment URLs inside the playlists point to Twitch CDN directly
    so all actual video bandwidth is on the client.
    """
    channel = channel.strip().lower()
    if not re.match(r"^[a-z0-9_]{1,50}$", channel):
        raise HTTPException(400, "Invalid channel name")

    url = f"https://twitch.tv/{channel}"

    try:
        streams = await asyncio.to_thread(_resolve_streams, url)
    except streamlink.exceptions.NoPluginError:
        raise HTTPException(400, "No plugin found for this URL")
    except streamlink.exceptions.PluginError as e:
        raise HTTPException(502, f"Streamlink plugin error: {e}")
    except Exception as e:
        logger.exception("Unexpected error resolving streams")
        raise HTTPException(500, str(e))

    if not streams:
        raise HTTPException(404, f"No streams found for channel '{channel}' — is it live?")

    # Wrap every m3u8 URL through our proxy endpoint.
    # The proxy rewrites sub-playlist URLs to also go through the proxy,
    # but leaves segment URLs (.ts, .m4s) as absolute Twitch CDN URLs.
    result = {}
    for name, stream_obj in streams.items():
        raw_url = stream_obj.url if hasattr(stream_obj, "url") else str(stream_obj)
        result[name] = f"/api/proxy?url={quote(raw_url, safe='')}"

    return JSONResponse({"channel": channel, "streams": result})


def _resolve_streams(url: str) -> dict:
    session = get_session()
    return session.streams(url)


# ── Playlist proxy ──
#
# This is the key piece. It proxies m3u8 text (tiny) and rewrites URLs:
#   - Sub-playlists (.m3u8) → wrapped through /api/proxy  (server, ~KB)
#   - Segments (.ts, .m4s, etc.) → absolute Twitch CDN URL (client, ~MB)
#
# The server touches only playlist manifests. All video data goes
# browser → Twitch CDN with zero server involvement.

SEGMENT_EXTENSIONS = ('.ts', '.m4s', '.mp4', '.aac', '.fmp4', '.cmfv', '.cmfa')

@app.get("/api/proxy")
async def proxy_playlist(url: str = Query(...)):
    """
    Proxy an m3u8 playlist and rewrite internal URLs.
    Playlists are re-wrapped through this proxy.
    Segment URLs become absolute so the browser fetches them directly.
    """
    parsed = urlparse(url)

    # Safety: only proxy known Twitch CDN / HLS domains
    allowed_hosts = (
        '.ttvnw.net',       # Twitch video CDN
        '.twitch.tv',
        '.jtvnw.net',
        '.cloudfront.net',  # Twitch sometimes uses CF
        '.akamaized.net',
    )
    if not any(parsed.hostname and parsed.hostname.endswith(h) for h in allowed_hosts):
        raise HTTPException(403, f"Domain not allowed: {parsed.hostname}")

    http = await get_http()
    resp = await http.get(url)

    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "Upstream fetch failed")

    content_type = resp.headers.get("content-type", "")

    # If this is an m3u8 playlist, rewrite URLs inside it
    if "mpegurl" in content_type or url.endswith(".m3u8") or "m3u8" in url:
        body = resp.text
        base_url = url.rsplit("/", 1)[0] + "/"  # directory of this playlist
        rewritten = _rewrite_playlist(body, base_url)

        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store",
            },
        )

    # Non-playlist (shouldn't happen, but pass through)
    return Response(
        content=resp.content,
        media_type=content_type or "application/octet-stream",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _rewrite_playlist(body: str, base_url: str) -> str:
    """
    Rewrite URLs in an HLS playlist:
      - Lines ending in .m3u8 → wrap through /api/proxy (stay on server)
      - Lines ending in segment extensions → make absolute (go to client)
      - Everything else (tags, comments) → pass through unchanged
    """
    lines = body.splitlines()
    out = []

    for line in lines:
        stripped = line.strip()

        # Skip empty lines and HLS tags
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue

        # Make URL absolute first
        if not stripped.startswith("http"):
            absolute = urljoin(base_url, stripped)
        else:
            absolute = stripped

        # Sub-playlist → proxy it
        if absolute.endswith(".m3u8") or "m3u8" in absolute.split("?")[0]:
            out.append(f"/api/proxy?url={quote(absolute, safe='')}")
        # Segment → leave as absolute CDN URL (browser fetches directly!)
        elif any(absolute.split("?")[0].endswith(ext) for ext in SEGMENT_EXTENSIONS):
            out.append(absolute)
        else:
            # Unknown — assume segment (safe default, goes to client)
            out.append(absolute)

    return "\n".join(out)


# ── Serve frontend ──

STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/health")
async def health():
    return {"status": "ok"}
