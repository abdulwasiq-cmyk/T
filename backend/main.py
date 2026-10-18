import asyncio
from html import unescape
import base64
import contextvars
import secrets
from datetime import date, datetime, timedelta, timezone
from collections import deque
import gzip
import json
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

import httpx
import libtorrent as lt
from cryptography.fernet import Fernet, InvalidToken
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel


from cache_diagnostics import (
    _CACHE_DIAGNOSTICS,
    _CACHE_DIAGNOSTICS_STARTED_AT,
    _UPSTREAM_FEATURE,
    _InstrumentedAsyncClient,
    _record_cache_event,
    _record_upstream_request,
    _upstream_provider_name,
    _upstream_endpoint_group,
)
from nexstream import nexstream_api_key_configured, resolve_nexstream_movie

_TRUTHY = {"1", "true", "yes", "on"}
# Cache-only deployments (e.g. Render free without provider keys) must never
# call TMDB/Watchmode/OMDb/poster providers; prefilled sections read the bundled cache.
_PROVIDER_KEY_ENV_NAMES = (
    "TMDB_API_KEY", "TMDB_API_KEYS", "TMDB_READ_ACCESS_TOKEN", "TMDB_READ_ACCESS_TOKENS",
    "WATCHMODE_API_KEY", "WATCHMODE_API_KEYS", "OMDB_API_KEY", "OMDB_API_KEYS",
)
_CACHE_ONLY_ENV = os.getenv("CACHE_ONLY_MODE", "").strip().casefold()
if _CACHE_ONLY_ENV in _TRUTHY or os.getenv("OTT_CACHE_ONLY", "false").strip().casefold() in _TRUTHY:
    CACHE_ONLY_MODE = True
elif _CACHE_ONLY_ENV in {"0", "false", "no", "off"}:
    CACHE_ONLY_MODE = False
else:
    # No explicit setting: a deployment without any provider key can only serve
    # from the bundled cache, so behave as cache-only instead of failing live calls.
    CACHE_ONLY_MODE = not any(os.getenv(name, "").strip() for name in _PROVIDER_KEY_ENV_NAMES)



APP_NAME = "Torrent Studio API"
logger = logging.getLogger("torrent-studio")

# Live torrent-provider searches are allowed only inside the explicit manual-search route.
_MANUAL_TORRENT_SEARCH = contextvars.ContextVar("manual_torrent_search", default=False)
_REGIONAL_MOVIE_CATALOGUE_FORCE_REFRESH = contextvars.ContextVar(
    "regional_movie_catalogue_force_refresh",
    default=False,
)
_REGIONAL_MOVIE_SOURCE_FORCE_REFRESH = contextvars.ContextVar(
    "regional_movie_source_force_refresh",
    default=False,
)
_REGIONAL_MOVIE_SOURCE_REFRESHED_KEYS = contextvars.ContextVar(
    "regional_movie_source_refreshed_keys",
    default=None,
)

# TMDB provides dynamic, paginated movie discovery. Credentials stay server-side;
# use a free developer key for non-commercial use or the API Read Access Token.
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
def _split_env_keys(*values: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for value in values
        for item in str(value or "").replace("\\n", "\n").replace(",", "\n").splitlines()
        if item.strip()))

TMDB_API_KEYS = _split_env_keys(os.getenv("TMDB_API_KEYS", ""), os.getenv("TMDB_API_KEY", ""))
TMDB_READ_ACCESS_TOKENS = _split_env_keys(os.getenv("TMDB_READ_ACCESS_TOKENS", ""),
    os.getenv("TMDB_READ_ACCESS_TOKEN", ""), os.getenv("TMDB_BEARER_TOKEN", ""))
TMDB_READ_ACCESS_TOKEN = TMDB_READ_ACCESS_TOKENS[0] if TMDB_READ_ACCESS_TOKENS else ""
TMDB_API_BASE = "https://api.themoviedb.org/3"
WATCHMODE_API_KEYS = _split_env_keys(os.getenv("WATCHMODE_API_KEYS", ""), os.getenv("WATCHMODE_API_KEY", ""))
WATCHMODE_API_KEY = WATCHMODE_API_KEYS[0] if WATCHMODE_API_KEYS else ""
WATCHMODE_API_BASE = "https://api.watchmode.com/v1"
STREAMING_AVAILABILITY_API_KEY = os.getenv("STREAMING_AVAILABILITY_API_KEY", "").strip()
STREAMING_AVAILABILITY_API_BASE = "https://api.movieofthenight.com/v4"
_PROVIDER_KEY_COOLDOWNS: dict[str, float] = {}
_PROVIDER_KEY_FAILURES: dict[str, int] = {}
_PROVIDER_KEY_CURSORS: dict[str, int] = {}
# Reuse metadata-provider connections and bound concurrent catalogue backfills.
_TMDB_HTTP_CLIENT: httpx.AsyncClient | None = None
_TMDB_REQUEST_SEMAPHORE = asyncio.Semaphore(4)
_WATCHMODE_HTTP_CLIENT: httpx.AsyncClient | None = None
_WATCHMODE_REQUEST_SEMAPHORE = asyncio.Semaphore(3)
_WATCHMODE_RATE_LIMITED_UNTIL = 0.0
_WATCHMODE_RESOLVED_TITLE_IDS: dict[int, int | None] = {}
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


def _provider_key_id(provider: str, key: str) -> str:
    return provider + ":" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _provider_key_available(provider: str, key: str) -> bool:
    return _PROVIDER_KEY_COOLDOWNS.get(_provider_key_id(provider, key), 0.0) <= time.time()


def _provider_key_failure(provider: str, key: str, seconds: float, reason: str) -> None:
    identity = _provider_key_id(provider, key)
    _PROVIDER_KEY_FAILURES[identity] = _PROVIDER_KEY_FAILURES.get(identity, 0) + 1
    _PROVIDER_KEY_COOLDOWNS[identity] = max(_PROVIDER_KEY_COOLDOWNS.get(identity, 0.0), time.time() + max(1.0, seconds))
    logger.warning("%s credential %s cooling down for %.0fs (%s; failures=%s)",
        provider, identity.rsplit(":", 1)[-1], max(1.0, seconds), reason, _PROVIDER_KEY_FAILURES[identity])


def _provider_key_success(provider: str, key: str) -> None:
    identity = _provider_key_id(provider, key)
    _PROVIDER_KEY_FAILURES.pop(identity, None)
    _PROVIDER_KEY_COOLDOWNS.pop(identity, None)


def _retry_after_seconds(response: httpx.Response, default: float = 60.0) -> float:
    try:
        return max(1.0, min(21600.0, float(str(response.headers.get("Retry-After") or "").strip())))
    except (TypeError, ValueError):
        return default


async def _watchmode_get(path: str, *, params: dict[str, Any] | None = None,
    timeout: httpx.Timeout | float = 10.0, auth_header: bool = True) -> httpx.Response | None:
    """Rotate keys when available, but honor Watchmode's shared rate-limit cooldown."""
    global _WATCHMODE_RATE_LIMITED_UNTIL
    if CACHE_ONLY_MODE or not WATCHMODE_API_KEYS:
        return None
    # Watchmode rate limits and monthly credits are plan/account governed. Once any
    # credential receives 429, pause all credentials in this process instead of
    # immediately retrying the same account through another key.
    if _WATCHMODE_RATE_LIMITED_UNTIL > time.time():
        logger.debug("Watchmode request skipped during shared rate-limit cooldown (%s)", path)
        return None
    provider = "watchmode"
    request_params = dict(params or {})
    start = _PROVIDER_KEY_CURSORS.get(provider, 0) % len(WATCHMODE_API_KEYS)
    ordered = WATCHMODE_API_KEYS[start:] + WATCHMODE_API_KEYS[:start]
    _PROVIDER_KEY_CURSORS[provider] = (start + 1) % len(WATCHMODE_API_KEYS)
    last_response = None
    transient_failed: list[str] = []
    eligible = 0

    async def attempt(key: str) -> tuple[httpx.Response | None, bool]:
        nonlocal last_response, eligible
        global _WATCHMODE_RATE_LIMITED_UNTIL
        if not _provider_key_available(provider, key):
            return None, False
        eligible += 1
        headers = {"Accept": "application/json"}
        query = dict(request_params)
        if auth_header:
            headers["X-API-Key"] = key
            query.pop("apiKey", None)
        else:
            query["apiKey"] = key
        try:
            global _WATCHMODE_HTTP_CLIENT
            if _WATCHMODE_HTTP_CLIENT is None or _WATCHMODE_HTTP_CLIENT.is_closed:
                _WATCHMODE_HTTP_CLIENT = _InstrumentedAsyncClient(
                    timeout=httpx.Timeout(10.0, connect=4.0),
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                    limits=httpx.Limits(
                        max_connections=6,
                        max_keepalive_connections=3,
                        keepalive_expiry=30.0,
                    ),
                )
            async with _WATCHMODE_REQUEST_SEMAPHORE:
                response = await _WATCHMODE_HTTP_CLIENT.get(
                    f"{WATCHMODE_API_BASE}{path}",
                    params=query,
                    headers=headers,
                    timeout=timeout,
                )
            last_response = response
            if response.status_code < 300:
                _provider_key_success(provider, key)
                return response, True
            if response.status_code == 429:
                # Watchmode documents plan-level request limits and account-level
                # quota. Treat 429 as a provider-wide cooldown, not a per-key issue.
                retry_after = _retry_after_seconds(response)
                _WATCHMODE_RATE_LIMITED_UNTIL = max(
                    _WATCHMODE_RATE_LIMITED_UNTIL, time.time() + retry_after
                )
                _provider_key_failure(provider, key, retry_after, "HTTP 429 (shared cooldown)")
                logger.warning(
                    "Watchmode shared cooldown activated for %.0fs on %s (limit=%s remaining=%s quota_used=%s)",
                    retry_after, path,
                    response.headers.get("X-RateLimit-Limit", "unknown"),
                    response.headers.get("X-RateLimit-Remaining", "unknown"),
                    response.headers.get("X-Account-Quota-Used", "unknown"),
                )
                return response, True
            if response.status_code == 401:
                _provider_key_failure(provider, key, 3600, "HTTP 401")
                return None, False
            if response.status_code == 403:
                # A plan/access restriction is not a transient credential failure.
                _provider_key_failure(provider, key, 3600, "HTTP 403")
                return response, True
            if response.status_code >= 500:
                transient_failed.append(key)
                return None, False
            return response, True
        except httpx.HTTPError as exc:
            logger.info("Watchmode transport failure for %s credential %s: %s",
                path, _provider_key_id(provider, key).rsplit(":", 1)[-1], type(exc).__name__)
            transient_failed.append(key)
            return None, False

    for key in ordered:
        response, terminal = await attempt(key)
        if response is not None and terminal:
            return response
    if transient_failed:
        await asyncio.sleep(0.75)
        retry_keys = list(dict.fromkeys(transient_failed))
        transient_failed.clear()
        for key in retry_keys:
            response, terminal = await attempt(key)
            if response is not None and terminal:
                return response
        # Transport failures and HTTP 5xx responses do not prove the key is invalid.
        # Keep the key eligible; explicit 401/403/429 responses have their own policy.
    if last_response is not None:
        return last_response
    if not eligible:
        logger.info("All Watchmode keys are cooling down for %s", path)
    return None


# If TMDB API connections are reset before TLS completes# If TMDB API connections are reset before TLS completes, pause new API attempts
# briefly so catalogue requests fail fast and can fall back to persistent caches.
TMDB_API_CONNECT_FAILURE_COOLDOWN_SECONDS = max(15, int(os.getenv("TMDB_API_CONNECT_FAILURE_COOLDOWN_SECONDS", "60")))
_tmdb_api_connect_failure_until = 0.0
TMDB_CATALOGUE_CACHE_SECONDS = max(300, int(os.getenv("TMDB_CATALOGUE_CACHE_SECONDS", str(6 * 60 * 60))))
REGIONAL_MOVIE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
REGIONAL_MOVIE_CACHE_PAGE_SIZE = 18
REGIONAL_MOVIE_PREWARM_PAGES = 3
REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE = Path(os.getenv(
    "REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE",
    "/app/data/regional_movie_trending_archive.json",
))
REGIONAL_MOVIE_TRENDING_ARCHIVE_WEEKS = 52
REGIONAL_MOVIE_CATALOGUE_KEYS = (
    "popular-hollywood",
    "trending-hollywood",
    "latest-hollywood",
    "popular-bollywood",
    "trending-bollywood",
    "latest-bollywood",
)
TMDB_COMPANY_CACHE_SECONDS = max(3600, int(os.getenv("TMDB_COMPANY_CACHE_SECONDS", str(24 * 60 * 60))))
TMDB_CATALOGUE_CACHE_FILE = Path(os.getenv("TMDB_CATALOGUE_CACHE_FILE", "/app/data/tmdb_movie_catalogue_cache.json"))
TMDB_SEARCH_METADATA_CACHE_FILE = Path(os.getenv("TMDB_SEARCH_METADATA_CACHE_FILE", "/app/data/tmdb_search_metadata_cache.json"))
TMDB_SEARCH_METADATA_QUERY_TTL_SECONDS = max(3600, int(os.getenv("TMDB_SEARCH_METADATA_QUERY_TTL_SECONDS", str(30 * 24 * 60 * 60))))
IMDB_RATING_CACHE_FILE = Path(os.getenv("IMDB_RATING_CACHE_FILE", "/app/data/imdb_rating_cache.json"))
IMDB_RATING_CACHE_TTL_SECONDS = max(86400, int(os.getenv("IMDB_RATING_CACHE_TTL_SECONDS", str(30 * 24 * 60 * 60))))
IMDB_RATING_LOOKUP_VERSION = 5
# Misses are re-checked sooner than hits: OMDb data and title matching improve over time.
IMDB_RATING_NOT_FOUND_TTL_SECONDS = max(3600, int(os.getenv("IMDB_RATING_NOT_FOUND_TTL_SECONDS", str(24 * 60 * 60))))
_OMDB_HTTP_CLIENT: httpx.AsyncClient | None = None
_OMDB_REQUEST_SEMAPHORE = asyncio.Semaphore(4)
_OMDB_RATE_LIMITED_UNTIL = 0.0
_IMDB_RATING_CACHE: dict[str, dict[str, Any]] | None = None
_IMDB_RATING_CACHE_LOCK = asyncio.Lock()
_IMDB_RATING_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
TMDB_SYNOPSIS_LOOKUP_VERSION = 5
SYNOPSIS_CACHE_RETRY_INTERVAL_SECONDS = max(300, int(os.getenv("SYNOPSIS_CACHE_RETRY_INTERVAL_SECONDS", "900")))
TMDB_OTT_CACHE_SECONDS = max(3600, int(os.getenv("TMDB_OTT_CACHE_SECONDS", str(24 * 60 * 60))))
# Results with no provider names (not_confirmed, release dates only) may just mean the
# upstream lookup missed or failed, so they are re-checked much sooner than hits.
TMDB_OTT_INCOMPLETE_CACHE_SECONDS = max(300, int(os.getenv("TMDB_OTT_INCOMPLETE_CACHE_SECONDS", "1800")))
TMDB_OTT_MISSING_CACHE_SECONDS = max(900, int(os.getenv("TMDB_OTT_MISSING_CACHE_SECONDS", str(6 * 60 * 60))))
TMDB_OTT_CACHE_FILE = Path(os.getenv("TMDB_OTT_CACHE_FILE", "/app/data/tmdb_ott_availability_cache.json"))
MARVEL_TV_CATALOGUE_CACHE_FILE = Path(os.getenv("MARVEL_TV_CATALOGUE_CACHE_FILE", "/app/data/marvel_tv_catalogue_cache.json"))
DC_TV_CATALOGUE_CACHE_FILE = Path(os.getenv("DC_TV_CATALOGUE_CACHE_FILE", "/app/data/dc_tv_catalogue_cache.json"))
_tmdb_ott_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_tmdb_ott_inflight: dict[tuple[int, str], asyncio.Task[dict[str, Any]]] = {}
_tmdb_movie_catalogue_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_tmdb_movie_catalogue_inflight: dict[tuple[str, int], asyncio.Task[dict[str, Any]]] = {}
_tmdb_company_id_cache: dict[str, tuple[float, list[str]]] = {}
_tmdb_company_inflight: dict[str, asyncio.Task[list[str]]] = {}
_tmdb_cache_clear_last_at = 0.0

GITHUB_FEEDBACK_TOKEN = os.getenv("GITHUB_FEEDBACK_TOKEN", "").strip()
GITHUB_FEEDBACK_REPO = os.getenv("GITHUB_FEEDBACK_REPO", "wroxtaaar/new-test").strip()
SEEDR_BASE = "https://www.seedr.cc/api/v0.1/p"
SEEDR_MEDIA_BASE = "https://www.seedr.cc/api"
SEEDR_V2_BASE = "https://v2.seedr.cc/api/v0.1/p"
SEEDR_PAT_BASE = "https://www.seedr.cc/api/v0.1/p"
# Legacy developer-level Seedr credentials remain supported only when explicitly enabled.
# Normal requests use a per-browser Seedr connection established through device auth.
SEEDR_TOKEN = os.getenv("SEEDR_API_TOKEN", "").strip()
ALLOW_LEGACY_SEEDR_TOKEN = os.getenv("ALLOW_LEGACY_SEEDR_TOKEN", "false").strip().lower() in {"1", "true", "yes", "on"}
# A shared folder ID cannot be used safely across different users. Personal
# connections always start at each Seedr account's own root folder.
SEEDR_LIBRARY_FOLDER_ID = "0"
SEEDR_DEVICE_CLIENT_ID = os.getenv("SEEDR_DEVICE_CLIENT_ID", "seedr_xbmc").strip() or "seedr_xbmc"
SEEDR_DEVICE_CODE_URL = "https://www.seedr.cc/api/device/code"
SEEDR_DEVICE_AUTHORIZE_URL = "https://www.seedr.cc/api/device/authorize"
SEEDR_SESSION_COOKIE = os.getenv("SEEDR_SESSION_COOKIE", "torrent_studio_seedr_session").strip() or "torrent_studio_seedr_session"
SEEDR_SESSION_TTL_SECONDS = int(float(os.getenv("SEEDR_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60))))
SEEDR_SESSION_SECRET = os.getenv("SEEDR_SESSION_SECRET", "").strip()
if SEEDR_SESSION_SECRET:
    _seedr_fernet_key = base64.urlsafe_b64encode(hashlib.sha256(SEEDR_SESSION_SECRET.encode("utf-8")).digest())
else:
    _seedr_fernet_key = Fernet.generate_key()
    logger.warning("SEEDR_SESSION_SECRET is not configured; personal Seedr connections will reset on backend restart.")
_seedr_fernet = Fernet(_seedr_fernet_key)

_seedr_sessions: dict[str, dict[str, Any]] = {}
_seedr_request_token: contextvars.ContextVar[str] = contextvars.ContextVar("seedr_request_token", default="")
_seedr_request_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("seedr_request_session_id", default="")
SEARCH_STOPWORDS = {"the", "a", "an", "movie", "film", "series", "season", "episode", "web", "show", "tv"}
TORRENT_SEARCH_API_URL = os.getenv("TORRENT_SEARCH_API_URL", "https://torrent-search-api-ujfa.onrender.com").rstrip("/")
KNABEN_API_URL = os.getenv("KNABEN_API_URL", "https://api.knaben.org/v2/search").rstrip("/")
TORRENT_METADATA_API_URL = os.getenv("TORRENT_METADATA_API_URL", "https://torrentmeta.fly.dev").rstrip("/")
FAST_SEARCH_TEST_URL = os.getenv("FAST_SEARCH_TEST_URL", "https://torrent-search-test.onrender.com").rstrip("/")
SEARCH_SOURCE_TIMEOUT_SECONDS = float(os.getenv("SEARCH_SOURCE_TIMEOUT_SECONDS", "2.25"))
SEARCH_TOTAL_TIMEOUT_SECONDS = float(os.getenv("SEARCH_TOTAL_TIMEOUT_SECONDS", "3.5"))
SEARCH_GRACE_SECONDS = float(os.getenv("SEARCH_GRACE_SECONDS", "0.2"))
SEARCH_CACHE_SECONDS = float(os.getenv("SEARCH_CACHE_SECONDS", "60"))
SEARCH_CACHE_STALE_SECONDS = float(os.getenv("SEARCH_CACHE_STALE_SECONDS", "600"))
SEARCH_CACHE_MAX_ENTRIES = int(os.getenv("SEARCH_CACHE_MAX_ENTRIES", "75"))
SEARCH_CACHE_MIN_RESULTS = int(os.getenv("SEARCH_CACHE_MIN_RESULTS", "8"))
# Catalogue warm-ups query many titles at once. Keep outbound provider requests bounded.
_X1337_REQUEST_SEMAPHORE = asyncio.Semaphore(2)
_LIMETORRENTS_REQUEST_SEMAPHORE = asyncio.Semaphore(2)
_KNABEN_REQUEST_SEMAPHORE = asyncio.Semaphore(1)
_YTS_REQUEST_SEMAPHORE = asyncio.Semaphore(2)
# Shared root config is used by both the backend and frontend.
_SEARCH_LIMITS_CANDIDATES = (
    Path(__file__).resolve().parent / "search_limits.json",
    Path(__file__).resolve().parent.parent / "search_limits.json",
)
_SEARCH_LIMITS = {}
for _limits_path in _SEARCH_LIMITS_CANDIDATES:
    try:
        with _limits_path.open("r", encoding="utf-8") as _limits_file:
            _SEARCH_LIMITS = json.load(_limits_file)
        break
    except (OSError, json.JSONDecodeError):
        continue
MIN_SEARCH_RESULT_SIZE_BYTES = max(0, int(_SEARCH_LIMITS.get("minSizeMb", 100))) * 1024 * 1024
MAX_SEARCH_RESULT_SIZE_BYTES = max(MIN_SEARCH_RESULT_SIZE_BYTES, int(_SEARCH_LIMITS.get("maxSizeGb", 5))) * 1024 * 1024 * 1024
SEARCH_COMPOUND_ALIASES = {
    "antman": "ant man",
    "spiderman": "spider man",
    "ironman": "iron man",
    "blackpanther": "black panther",
    "doctorstrange": "doctor strange",
    "captainamerica": "captain america",
    "guardiansofthegalaxy": "guardians of the galaxy",
}
# Match the movie app's mirror strategy. YTS availability varies by ISP/domain,
# so keep multiple API mirrors and allow an environment override for ordering.
# YTS domains are inconsistent across networks. The accelerator API is a
# keyless YTS-compatible mirror, followed by the public YTS domains.
YTS_API_HOSTS = tuple(
    host.strip()
    for host in os.getenv(
        "YTS_API_HOSTS",
        "movies-api.accel.li,yts.bz,yts.mx,yts.am,yts.lt,yts.rs",
    ).split(",")
    if host.strip()
)
FAST_SEARCH_TRACKERS = (
    "http://tracker.dler.org:6969/announce",
    "http://tracker2.dler.org:80/announce",
    "http://1337.abcvg.info:80/announce",
)
TORRENT_METADATA_CACHE_FILE = Path(os.getenv("TORRENT_METADATA_CACHE_FILE", "/app/.torrent_metadata_cache.json"))
TORRENT_METADATA_JOB_TIMEOUT_SECONDS = float(os.getenv("TORRENT_METADATA_JOB_TIMEOUT_SECONDS", "60"))
TORRENT_METADATA_ITORRENTS_BASE_URL = os.getenv("TORRENT_METADATA_ITORRENTS_BASE_URL", "https://itorrents.net/torrent").rstrip("/")
TORRENT_METADATA_ITORRENTS_TIMEOUT_SECONDS = float(os.getenv("TORRENT_METADATA_ITORRENTS_TIMEOUT_SECONDS", "5"))
TORRENT_METADATA_BACKGROUND_TTL_SECONDS = float(os.getenv("TORRENT_METADATA_BACKGROUND_TTL_SECONDS", str(6 * 60 * 60)))
TORRENT_METADATA_BACKGROUND_RETRY_SECONDS = float(os.getenv("TORRENT_METADATA_BACKGROUND_RETRY_SECONDS", "30"))
TORRENT_METADATA_JOB_RETENTION_SECONDS = float(os.getenv("TORRENT_METADATA_JOB_RETENTION_SECONDS", str(60 * 60)))
_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_search_inflight: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}

# Rolling server-side byte counters for browser media streams. This is more
# reliable than Resource Timing for long-lived media responses, which browsers
# may report with transferSize=0 until the response completes.
_media_download_stats: dict[str, deque[tuple[float, int]]] = {}
_media_stats_lock = asyncio.Lock()
_MEDIA_STATS_WINDOW_SECONDS = 5.0

def _record_media_bytes(file_id: str, amount: int) -> None:
    if amount <= 0:
        return
    now = time.monotonic()
    bucket = _media_download_stats.setdefault(file_id, deque())
    bucket.append((now, amount))
    cutoff = now - _MEDIA_STATS_WINDOW_SECONDS
    while bucket and bucket[0][0] < cutoff:
        bucket.popleft()

def _media_download_speed(file_id: str) -> float:
    now = time.monotonic()
    bucket = _media_download_stats.get(file_id)
    if not bucket:
        return 0.0
    cutoff = now - _MEDIA_STATS_WINDOW_SECONDS
    while bucket and bucket[0][0] < cutoff:
        bucket.popleft()
    if not bucket:
        return 0.0
    total = sum(amount for _, amount in bucket)
    span = max(0.5, now - bucket[0][0])
    return total / span


# Metadata is deliberately independent of Seedr. The cache survives requests
# within a Render instance, while the global libtorrent session keeps DHT state
# warm between magnets until the container is restarted.
_metadata_cache: dict[str, dict[str, Any]] = {}
_metadata_jobs: dict[str, dict[str, Any]] = {}
_libtorrent_session: Any | None = None
_libtorrent_session_lock: asyncio.Lock | None = None

class SeedrError(Exception):
    def __init__(self, code: str, status_code: int, detail: str):
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


app = FastAPI(title=APP_NAME)


def _diagnostic_feature_for_path(path: str) -> str:
    cleaned = re.sub(r"/movie-\d+(?=/|$)", "/movie-{id}", path or "/")
    cleaned = re.sub(r"(?<=/)\d+(?=/|$)", "{id}", cleaned)
    return "request:" + cleaned[:120]


@app.middleware("http")
async def _set_upstream_diagnostic_feature(request: Request, call_next):
    token = _UPSTREAM_FEATURE.set(_diagnostic_feature_for_path(request.url.path))
    try:
        return await call_next(request)
    finally:
        _UPSTREAM_FEATURE.reset(token)


@app.exception_handler(SeedrError)
async def handle_seedr_error(request: Request, exc: SeedrError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "detail": exc.detail,
            "code": exc.code,
            "provider": "seedr",
        },
    )

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
# Credentials are allowed below, so the default must NOT trust shared hosting
# domains such as *.vercel.app or *.onrender.com: anyone can deploy a page there
# and make credentialed requests to this API. Deployed frontends must be listed
# explicitly in CORS_ORIGINS (or matched by a deliberate CORS_ORIGIN_REGEX).
CORS_ORIGIN_REGEX = os.getenv(
    "CORS_ORIGIN_REGEX",
    r"http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Fast catalogue serving -------------------------------------------------
# Catalogue snapshots are several MB. Parse/normalise once per file version and
# keep the serialized (and gzipped) response so each page view is a memory copy.
_FILE_MEMO: dict[str, tuple[tuple[int, int], Any]] = {}
_RESPONSE_MEMO: dict[str, tuple[Any, bytes, bytes]] = {}


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _memoized_file_read(memo_key: str, path: Path, reader):
    sig = _file_signature(path)
    hit = _FILE_MEMO.get(memo_key)
    if hit and hit[0] == sig:
        return hit[1]
    value = reader()
    # The reader may rewrite the file after normalising; re-stat so we cache the final version.
    _FILE_MEMO[memo_key] = (_file_signature(path), value)
    return value


def _fast_json_response(request: Request, memo_key: str, signature: Any, build) -> Response:
    entry = _RESPONSE_MEMO.get(memo_key)
    if entry is None or entry[0] != signature:
        raw = json.dumps(build(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        entry = (signature, raw, gzip.compress(raw, compresslevel=6))
        _RESPONSE_MEMO[memo_key] = entry
    headers = {"Cache-Control": "public, max-age=300", "Vary": "Accept-Encoding"}
    if "gzip" in (request.headers.get("accept-encoding") or "").casefold():
        headers["Content-Encoding"] = "gzip"
        return Response(entry[2], media_type="application/json", headers=headers)
    return Response(entry[1], media_type="application/json", headers=headers)


def _catalogue_group_key(item: dict[str, Any], index: int = 0) -> str:
    title = re.sub(r"[^a-z0-9]+", "", str(
        item.get("catalogueTitle") or item.get("marvelCatalogueTitle")
        or item.get("mediaTitle") or item.get("title") or ""
    ).casefold())
    if title:
        return title
    # Preserve grouping for rows that lack titles but carry a stable media/release ID.
    for field in ("tmdbId", "tmdb_id", "movieId", "movie_id", "imdbId", "imdb_id",
                  "infoHash", "info_hash", "magnet", "id"):
        value = str(item.get(field) or "").strip().casefold()
        if value:
            return field + ":" + value
    # Avoid treating all malformed/untitled rows as the same movie.
    return "__untitled_row_" + str(index)


def _page_catalogue_payload(
    payload: dict[str, Any],
    page: int,
    page_size: int,
    sort_by_release_date: bool = False,
) -> dict[str, Any]:
    """Slice a catalogue by movie (all torrent rows of a title stay together)."""
    if page <= 0 or page_size <= 0:
        return payload
    groups: dict[str, list[dict[str, Any]]] = {}
    for index, row in enumerate(payload["results"]):
        groups.setdefault(_catalogue_group_key(row, index), []).append(row)
    keys = list(groups)
    if sort_by_release_date:
        def sort_key(key: str) -> tuple[int, str]:
            rows = groups[key]
            movie = next((row for row in rows if row.get("releaseDate") or row.get("release_date")), rows[0])
            raw_date = str(movie.get("releaseDate") or movie.get("release_date") or "").strip()
            try:
                release_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                year_match = re.search(r"\b((?:19|20)\d{2})\b", raw_date)
                try:
                    year = int(movie.get("year") or (year_match.group(1) if year_match else 0))
                except (TypeError, ValueError):
                    year = 0
                release_date = date(year, 1, 1) if 1 <= year <= 9999 else date.min
            title = str(
                movie.get("catalogueTitle") or movie.get("marvelCatalogueTitle")
                or movie.get("mediaTitle") or movie.get("title") or ""
            )
            normalized_title = re.sub(r"[^a-z0-9]+", "", title.casefold())
            return -release_date.toordinal(), normalized_title

        keys.sort(key=sort_key)
    total_pages = max(1, -(-len(keys) // page_size))
    start = (page - 1) * page_size
    rows = [row for key in keys[start:start + page_size] for row in groups[key]]
    return {**payload, "results": rows, "page": page, "pageSize": page_size,
            "totalPages": total_pages, "totalMovies": len(keys)}


_PREBUILT_PAGE_SIZE = 18
_PREBUILT_PAGE_MEMO: dict[str, bytes] = {}
_DATE_SORTED_EXTRA_CATALOGUES = {
    "dc-live-action",
    "dc-live-action-hindi",
    "dc-animated",
    "classics",
}


def _prebuilt_catalogue_page(request: Request, key: str, page: int, page_size: int) -> Response | None:
    """Serve a build-time pre-split catalogue page without parsing the big file."""
    if page <= 0 or page_size != _PREBUILT_PAGE_SIZE:
        return None
    memo_key = f"{key}:{page}"
    blob = _PREBUILT_PAGE_MEMO.get(memo_key)
    if blob is None:
        try:
            blob = (_CACHE_SEED_DIR / "pages" / key / f"{page}.json.gz").read_bytes()
        except OSError:
            return None
        _PREBUILT_PAGE_MEMO[memo_key] = blob
    headers = {"Cache-Control": "public, max-age=300", "Vary": "Accept-Encoding"}
    if "gzip" in (request.headers.get("accept-encoding") or "").casefold():
        headers["Content-Encoding"] = "gzip"
        return Response(blob, media_type="application/json", headers=headers)
    return Response(gzip.decompress(blob), media_type="application/json", headers=headers)


def _seedr_empty_session() -> dict[str, Any]:
    now = time.time()
    return {
        "csrf_token": secrets.token_urlsafe(32),
        "access_token": "",
        "refresh_token": "",
        "token_type": "Bearer",
        "auth_mode": "legacy",
        "device": None,
        "created_at": now,
        "last_seen": now,
    }

def _seedr_new_session() -> tuple[str, dict[str, Any]]:
    session_id = secrets.token_urlsafe(32)
    session = _seedr_empty_session()
    _seedr_sessions[session_id] = session
    return session_id, session

def _seedr_get_session(session_id: str) -> dict[str, Any]:
    return _seedr_sessions.setdefault(session_id, _seedr_empty_session())

def _seedr_session_cookie_value(session_id: str, session: dict[str, Any]) -> str:
    payload = {
        "v": 1,
        "sid": session_id,
        "csrf": str(session.get("csrf_token") or ""),
        "access": str(session.get("access_token") or ""),
        "refresh": str(session.get("refresh_token") or ""),
        "type": str(session.get("token_type") or "Bearer"),
        "mode": str(session.get("auth_mode") or "legacy"),
        "device": session.get("device") if isinstance(session.get("device"), dict) else None,
        "created": float(session.get("created_at") or time.time()),
        "last": time.time(),
    }
    encrypted = _seedr_fernet.encrypt(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return "v1." + encrypted.decode("ascii")

def _seedr_restore_session(raw_cookie: str) -> tuple[str, dict[str, Any]] | None:
    if not raw_cookie.startswith("v1."):
        return None
    try:
        payload = json.loads(_seedr_fernet.decrypt(raw_cookie[3:].encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        session_id = str(payload.get("sid") or "").strip()
        csrf_token = str(payload.get("csrf") or "").strip()
        if not session_id or not csrf_token:
            return None
        now = time.time()
        last_seen = float(payload.get("last") or 0)
        if not last_seen or now - last_seen > SEEDR_SESSION_TTL_SECONDS:
            return None
        return session_id, {
            "csrf_token": csrf_token,
            "access_token": normalize_seedr_token(str(payload.get("access") or "")),
            "refresh_token": str(payload.get("refresh") or "").strip(),
            "token_type": str(payload.get("type") or "Bearer").strip() or "Bearer",
            "auth_mode": str(payload.get("mode") or "legacy").strip().lower() or "legacy",
            "device": payload.get("device") if isinstance(payload.get("device"), dict) else None,
            "created_at": float(payload.get("created") or now),
            "last_seen": now,
        }
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return None

def current_seedr_token() -> str:
    """Return the Seedr token for the current request's connected account."""
    token = _seedr_request_token.get().strip()
    if token:
        return normalize_seedr_token(token)
    if ALLOW_LEGACY_SEEDR_TOKEN and SEEDR_TOKEN:
        return normalize_seedr_token(SEEDR_TOKEN)
    return ""

def _seedr_session_token(session_id: str) -> str:
    session = _seedr_sessions.get(session_id)
    return normalize_seedr_token(str(session.get("access_token") or "")) if session else ""


def _seedr_session_auth_mode(session_id: str) -> str:
    session = _seedr_sessions.get(session_id)
    mode = str(session.get("auth_mode") or "legacy").strip().lower() if session else "legacy"
    return mode if mode in {"legacy", "pat"} else "legacy"


def _seedr_session_fingerprint(session_id: str) -> str:
    """Short non-secret identifier for correlating session logs."""
    if not session_id:
        return "none"
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]

def _clear_seedr_session_token(session_id: str | None = None) -> None:
    sid = session_id or _seedr_request_session_id.get().strip()
    if not sid:
        return
    session = _seedr_sessions.get(sid)
    if not session:
        return
    session["access_token"] = ""
    session["refresh_token"] = ""
    session["token_type"] = "Bearer"
    session["auth_mode"] = "legacy"
    session["device"] = None
    session["last_seen"] = time.time()

@app.middleware("http")
async def attach_seedr_session(request: Request, call_next):
    raw_cookie = str(request.cookies.get(SEEDR_SESSION_COOKIE) or "").strip()
    restored = _seedr_restore_session(raw_cookie)
    is_new = False

    if restored:
        session_id, restored_session = restored
        _seedr_sessions[session_id] = restored_session
    elif raw_cookie in _seedr_sessions:
        session_id = raw_cookie
    else:
        session_id, _ = _seedr_new_session()
        is_new = True

    session = _seedr_get_session(session_id)
    session["last_seen"] = time.time()

    token_ctx = _seedr_request_token.set(_seedr_session_token(session_id))
    session_ctx = _seedr_request_session_id.set(session_id)
    try:
        path = request.url.path or ""
        method = request.method.upper()
        # Every state-changing API call needs the session CSRF token, not only
        # /api/seedr/*: compatibility routes such as /api/files/delete and
        # /api/v2/torrents/* also act on the connected Seedr account.
        if path.startswith("/api/") and method not in {"GET", "HEAD", "OPTIONS"}:
            if not _seedr_session_csrf_ok(request):
                response = JSONResponse(
                    status_code=403,
                    content={"error": "Seedr session security check failed.", "code": "SEEDR_CSRF_INVALID"},
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
    finally:
        _seedr_request_token.reset(token_ctx)
        _seedr_request_session_id.reset(session_ctx)

    is_https = (
        request.url.scheme.lower() == "https"
        or str(request.headers.get("x-forwarded-proto") or "").lower() == "https"
    )
    response.set_cookie(
        SEEDR_SESSION_COOKIE,
        _seedr_session_cookie_value(session_id, session),
        max_age=SEEDR_SESSION_TTL_SECONDS,
        httponly=True,
        secure=is_https,
        samesite="none" if is_https else "lax",
        path="/",
    )

    now = time.time()
    if len(_seedr_sessions) > 2000:
        stale = [
            sid for sid, item in _seedr_sessions.items()
            if now - float(item.get("last_seen") or now) > SEEDR_SESSION_TTL_SECONDS
        ]
        for sid in stale:
            _seedr_sessions.pop(sid, None)

    return response

def _seedr_session_csrf_ok(request: Request) -> bool:
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_sessions.get(session_id)
    if not session:
        return False
    expected = str(session.get("csrf_token") or "")
    supplied = str(request.headers.get("X-Torrent-Studio-CSRF") or "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


@app.middleware("http")
async def add_timing_allow_origin(request: Request, call_next):
    # Allows the frontend to read Resource Timing transfer sizes for the
    # cross-origin Render media stream when the frontend is hosted on Vercel.
    response = await call_next(request)
    response.headers["Timing-Allow-Origin"] = "*"
    return response

class FeedbackRequest(BaseModel):
    type: str
    rating: int | None = None
    message: str
    name: str | None = None


class MagnetRequest(BaseModel):
    magnet: str
    folder_id: str | int | None = None
    torrent_name: str | None = None
    size: int | float | None = None
    # Browser clients may serialize indexes as strings; normalize them in the endpoint.
    selected_indexes: list[int | str] | None = None
    manifest: list[dict[str, Any]] | None = None



def decode_torrent_metadata(raw: bytes) -> dict[bytes, Any]:
    """Decode the small bencoded .torrent metadata file produced by aria2."""
    position = 0

    def parse() -> Any:
        nonlocal position
        if position >= len(raw):
            raise ValueError("unexpected end of bencode")

        marker = raw[position:position + 1]
        if marker == b"i":
            position += 1
            end = raw.find(b"e", position)
            if end < 0:
                raise ValueError("unterminated integer")
            value = int(raw[position:end])
            position = end + 1
            return value

        if marker == b"l":
            position += 1
            value = []
            while position < len(raw) and raw[position:position + 1] != b"e":
                value.append(parse())
            if position >= len(raw):
                raise ValueError("unterminated list")
            position += 1
            return value

        if marker == b"d":
            position += 1
            value: dict[bytes, Any] = {}
            while position < len(raw) and raw[position:position + 1] != b"e":
                key = parse()
                if not isinstance(key, bytes):
                    raise ValueError("dictionary key is not bytes")
                value[key] = parse()
            if position >= len(raw):
                raise ValueError("unterminated dictionary")
            position += 1
            return value

        if marker.isdigit():
            colon = raw.find(b":", position)
            if colon < 0:
                raise ValueError("invalid byte string")
            size = int(raw[position:colon])
            position = colon + 1
            end = position + size
            if end > len(raw):
                raise ValueError("byte string exceeds metadata")
            value = raw[position:end]
            position = end
            return value

        raise ValueError("invalid bencode token")

    value = parse()
    if position != len(raw):
        raise ValueError("trailing bencode data")
    if not isinstance(value, dict):
        raise ValueError("torrent metadata root is not a dictionary")
    return value

def _load_metadata_cache() -> None:
    global _metadata_cache
    try:
        if TORRENT_METADATA_CACHE_FILE.exists():
            data = json.loads(TORRENT_METADATA_CACHE_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                _metadata_cache = {
                    str(key).lower(): value
                    for key, value in data.items()
                    if isinstance(value, dict)
                }
    except Exception as exc:
        logger.info("Metadata cache load skipped: %s", exc)


def _save_metadata_cache() -> None:
    try:
        TORRENT_METADATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TORRENT_METADATA_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_metadata_cache, ensure_ascii=False), "utf-8")
        tmp.replace(TORRENT_METADATA_CACHE_FILE)
    except Exception as exc:
        logger.info("Metadata cache save skipped: %s", exc)


async def _fetch_itorrents_metadata(info_hash_value: str) -> dict[str, Any] | None:
    """Try the public iTorrents descriptor cache before network metadata discovery."""
    target = info_hash_value.strip().lower()
    if not TORRENT_METADATA_ITORRENTS_BASE_URL or not re.fullmatch(r"[0-9a-f]{40}", target):
        return None

    url = f"{TORRENT_METADATA_ITORRENTS_BASE_URL}/{target}.torrent"
    try:
        async with _InstrumentedAsyncClient(
            timeout=TORRENT_METADATA_ITORRENTS_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Torrent-Studio/1.0"},
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            body = response.content
    except httpx.HTTPError as exc:
        logger.info("iTorrents cache lookup failed for %s: %s", target, exc)
        return None

    if len(body) > 8 * 1024 * 1024 or not body.startswith(b"d") or b"4:info" not in body:
        return None

    try:
        result = _metadata_from_torrent_bytes(body, target, "itorrents_cache")
    except (ValueError, TypeError) as exc:
        logger.info("iTorrents returned invalid metadata for %s: %s", target, exc)
        return None
    logger.info("Metadata cache hit for %s via iTorrents", target)
    return result


async def _fetch_source_torrent_descriptor(source_url: str, info_hash_value: str) -> dict[str, Any] | None:
    """Try to obtain a real .torrent descriptor from a trusted search detail URL."""
    if not source_url:
        return None

    try:
        parsed = urlsplit(source_url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return None

    # Search results currently expose Knaben detail URLs. Keep this narrowly
    # scoped rather than turning the endpoint into an arbitrary URL fetcher.
    allowed_hosts = {
        "knaben.org", "www.knaben.org", "api.knaben.org",
        "knaben.eu", "www.knaben.eu",
        "knaben.xyz", "www.knaben.xyz",
    }
    if host not in allowed_hosts:
        return None

    candidates = [source_url]
    try:
        async with _InstrumentedAsyncClient(
            timeout=8.0,
            follow_redirects=True,
            headers={"User-Agent": "Torrent-Studio/1.0"},
        ) as client:
            response = await client.get(source_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()

            if "bittorrent" in content_type or source_url.lower().split("?", 1)[0].endswith(".torrent"):
                candidates = [str(response.url)]
                raw = response.content
            else:
                html = response.text
                soup = BeautifulSoup(html, "html.parser")
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "").strip()
                    absolute = urljoin(str(response.url), href)
                    if absolute.lower().split("?", 1)[0].endswith(".torrent"):
                        candidates.append(absolute)

                # Some indexers expose a download attribute without a .torrent
                # suffix in the visible URL.
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "").strip()
                    absolute = urljoin(str(response.url), href)
                    label = " ".join(anchor.stripped_strings).lower()
                    if "torrent" in label and absolute not in candidates:
                        candidates.append(absolute)

                raw = None

            for candidate in candidates[:8]:
                try:
                    if raw is None or candidate != str(response.url):
                        descriptor = await client.get(candidate)
                        descriptor.raise_for_status()
                        candidate_type = descriptor.headers.get("content-type", "").lower()
                        body = descriptor.content
                    else:
                        candidate_type = content_type
                        body = raw

                    if len(body) > 8 * 1024 * 1024:
                        continue
                    if body.startswith(b"d") and b"4:info" in body:
                        return _metadata_from_torrent_bytes(
                            body,
                            info_hash_value,
                            "search_torrent_descriptor",
                        )
                except Exception:
                    continue
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Direct torrent descriptor lookup failed: %s", exc)

    return None


def _metadata_magnet_with_trackers(magnet: str) -> str:
    """Add the known-good HTTP trackers used by the fast WebTorrent test."""
    try:
        parsed = urlsplit(magnet)
        params = parse_qs(parsed.query, keep_blank_values=True)
        rebuilt: list[tuple[str, str]] = []
        for key in ("xt", "dn"):
            for value in params.get(key, []):
                if value:
                    rebuilt.append((key, str(value)))
        existing = {str(value).strip() for value in params.get("tr", []) if value}
        for tracker in FAST_SEARCH_TRACKERS:
            if tracker not in existing:
                rebuilt.append(("tr", tracker))
        return "magnet:?" + urlencode(rebuilt, doseq=True)
    except Exception:
        return magnet


async def _fetch_fast_test_metadata(magnet: str, info_hash_value: str) -> dict[str, Any] | None:
    """Use the proven WebTorrent resolver service as a metadata-only race participant."""
    if not FAST_SEARCH_TEST_URL:
        return None

    try:
        async with _InstrumentedAsyncClient(
            timeout=4.0,
            follow_redirects=True,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as client:
            response = await client.post(
                FAST_SEARCH_TEST_URL + "/api/metadata",
                json={"magnet": magnet},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Fast WebTorrent metadata service failed for %s: %s", info_hash_value, exc)
        return None

    if not isinstance(payload, dict):
        return None

    if str(payload.get("status") or "").lower() == "completed" and isinstance(payload.get("files"), list):
        metadata = payload
    else:
        job_id = str(payload.get("jobId") or "").strip()
        if not job_id:
            return None

        deadline = time.monotonic() + min(
            12.0,
            max(0.0, TORRENT_METADATA_JOB_TIMEOUT_SECONDS),
        )
        metadata = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.75)
            remaining = max(0.5, deadline - time.monotonic())
            try:
                async with _InstrumentedAsyncClient(
                    timeout=min(3.0, remaining),
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                ) as poll_client:
                    poll_response = await poll_client.get(
                        FAST_SEARCH_TEST_URL + "/api/metadata-jobs/" + quote(job_id, safe=""),
                    )
                    if poll_response.status_code == 404:
                        return None
                    poll_response.raise_for_status()
                    poll = poll_response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                continue

            job = poll.get("job") if isinstance(poll, dict) else None
            if not isinstance(job, dict):
                continue
            if str(job.get("status") or "").lower() == "completed":
                candidate = job.get("metadata")
                if isinstance(candidate, dict) and isinstance(candidate.get("files"), list):
                    metadata = candidate
                    break
            if str(job.get("status") or "").lower() == "failed":
                return None

        if metadata is None:
            return None

    returned_hash = str(metadata.get("infoHash") or metadata.get("hash") or "").strip().lower()
    if returned_hash and returned_hash != info_hash_value.lower():
        logger.info(
            "Fast WebTorrent metadata hash mismatch: wanted %s, got %s",
            info_hash_value,
            returned_hash,
        )
        return None

    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return None

    files: list[dict[str, Any]] = []
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        if not path:
            continue
        files.append({
            "index": int(item.get("index") if item.get("index") is not None else index),
            "name": str(item.get("name") or path),
            "size": int(float(item.get("size") or 0)),
            "path": path,
            "type": "file",
            "priority": 1,
        })
    if not files:
        return None

    result = {
        "name": str(metadata.get("name") or files[0]["name"]),
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item["size"]) for item in files),
        "source": "webtorrent_fast_test",
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    return result


def _metadata_from_torrent_bytes(raw: bytes, info_hash_value: str, source: str) -> dict[str, Any]:
    meta = decode_torrent_metadata(raw)
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent metadata has no info dictionary")

    def btext(value: Any) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")

    name = btext(info.get(b"name")) or f"Torrent {info_hash_value[:8]}"
    files: list[dict[str, Any]] = []
    multi = info.get(b"files")
    if isinstance(multi, list):
        for index, item in enumerate(multi):
            if not isinstance(item, dict):
                continue
            parts = item.get(b"path") or []
            relative = "/".join(btext(part) for part in parts) if isinstance(parts, list) else btext(parts)
            files.append({
                "index": index,
                "name": relative or name,
                "size": int(item.get(b"length") or 0),
                "path": relative or name,
                "type": "file",
                "priority": 1,
            })
    else:
        files.append({
            "index": 0,
            "name": name,
            "size": int(info.get(b"length") or 0),
            "path": name,
            "type": "file",
            "priority": 1,
        })

    result = {
        "name": name,
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item.get("size") or 0) for item in files),
        "source": source,
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    _metadata_cache[info_hash_value.lower()] = result
    _save_metadata_cache()
    return result


async def _get_libtorrent_session() -> Any:
    global _libtorrent_session, _libtorrent_session_lock
    if _libtorrent_session is not None:
        return _libtorrent_session
    if _libtorrent_session_lock is None:
        _libtorrent_session_lock = asyncio.Lock()
    async with _libtorrent_session_lock:
        if _libtorrent_session is not None:
            return _libtorrent_session
        session = lt.session()
        session.apply_settings({
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_tcp": True,
            "enable_outgoing_utp": True,
            "enable_incoming_tcp": True,
            "enable_incoming_utp": True,
            "listen_interfaces": "0.0.0.0:0",
            "dht_bootstrap_nodes": "",
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connection_speed": 50,
            "handshake_timeout": 10,
        })
        _libtorrent_session = session
        logger.info("Started persistent libtorrent metadata session")
        return session


def _metadata_from_libtorrent_sync(magnet: str, info_hash_value: str) -> dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="torrent-metadata-lt-")
    session = _libtorrent_session
    if session is None:
        raise RuntimeError("libtorrent session is not initialized")

    handle = None
    try:
        atp = lt.parse_magnet_uri(_metadata_magnet_with_trackers(magnet))
        atp.save_path = tmp
        atp.flags = atp.flags | lt.torrent_flags.upload_mode
        atp.flags = atp.flags & ~lt.torrent_flags.auto_managed
        handle = session.add_torrent(atp)

        deadline = time.monotonic() + TORRENT_METADATA_JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if handle.has_metadata():
                ti = handle.torrent_file()
                if ti is None:
                    raise RuntimeError("libtorrent returned metadata without torrent info")
                fs = ti.layout()
                files = []
                for index in range(fs.num_files()):
                    path = str(fs.file_path(index))
                    files.append({
                        "index": index,
                        "name": path,
                        "size": int(fs.file_size(index)),
                        "path": path,
                        "type": "file",
                        "priority": 1,
                    })
                return {
                    "name": str(ti.name() or f"Torrent {info_hash_value[:8]}"),
                    "hash": info_hash_value.lower(),
                    "files": files,
                    "totalSize": sum(int(item["size"]) for item in files),
                    "source": "libtorrent_metadata",
                    "pending": False,
                    "createdPreview": False,
                    "message": "Torrent metadata loaded without starting Seedr.",
                }

            for alert in session.pop_alerts():
                message = str(alert)
                if "error" in message.lower() or "tracker" in message.lower():
                    logger.info("libtorrent metadata alert: %s", message[:500])
            time.sleep(0.2)

        status = handle.status()
        raise TimeoutError(
            f"Metadata is still resolving (state={status.state}, "
            f"peers={status.num_peers}, seeds={status.num_seeds})"
        )
    finally:
        try:
            if handle is not None and handle.is_valid():
                session.remove_torrent(handle)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


async def _schedule_metadata_job_expiry(job_id: str) -> None:
    try:
        await asyncio.sleep(TORRENT_METADATA_JOB_RETENTION_SECONDS)
    except asyncio.CancelledError:
        return
    job = _metadata_jobs.get(job_id)
    if job and job.get("status") in {"ready", "error"}:
        _metadata_jobs.pop(job_id, None)


async def _run_metadata_job(job_id: str, magnet: str, info_hash_value: str) -> None:
    """Keep retrying independent metadata sources for up to the configured background TTL."""
    job = _metadata_jobs[job_id]
    started_at = float(job.get("startedAt") or time.time())
    deadline_at = float(job.get("deadlineAt") or (started_at + TORRENT_METADATA_BACKGROUND_TTL_SECONDS))
    job.update({
        "status": "resolving",
        "startedAt": started_at,
        "updatedAt": time.time(),
        "deadlineAt": deadline_at,
        "rounds": int(job.get("rounds") or 0),
    })

    async def resolve_libtorrent() -> dict[str, Any] | None:
        session = await _get_libtorrent_session()
        return await asyncio.to_thread(
            _metadata_from_libtorrent_sync,
            magnet,
            info_hash_value,
        )

    while time.time() < deadline_at:
        job["rounds"] = int(job.get("rounds") or 0) + 1
        job["updatedAt"] = time.time()

        # Shared public cache is the fastest path and costs no torrent peer work.
        cached_result = await _fetch_itorrents_metadata(info_hash_value)
        if cached_result:
            job.update({
                "status": "ready",
                "result": cached_result,
                "error": None,
                "updatedAt": time.time(),
            })
            logger.info("Metadata job %s resolved via iTorrents cache on round %s", job_id, job["rounds"])
            asyncio.create_task(_schedule_metadata_job_expiry(job_id))
            return

        remote_task = asyncio.create_task(
            _fetch_remote_torrent_metadata(magnet, info_hash_value)
        )
        knaben_task = asyncio.create_task(_lookup_knaben_by_hash(info_hash_value))
        webtorrent_task = asyncio.create_task(
            _fetch_fast_test_metadata(
                _metadata_magnet_with_trackers(magnet),
                info_hash_value,
            )
        )
        libtorrent_task = asyncio.create_task(resolve_libtorrent())
        tasks = (remote_task, knaben_task, webtorrent_task, libtorrent_task)
        errors: list[str] = []
        winner: dict[str, Any] | None = None

        try:
            # Race the independent remote/indexer/libtorrent paths. The first
            # usable file list wins; the libtorrent worker finishes before a
            # retry round begins so the same torrent is never added twice.
            for task in asyncio.as_completed(tasks):
                try:
                    result = await task
                except TimeoutError as exc:
                    errors.append(str(exc))
                    continue
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                if result and isinstance(result, dict) and result.get("files"):
                    winner = result
                    break

            if winner:
                _metadata_cache[info_hash_value.lower()] = winner
                _save_metadata_cache()
                job.update({
                    "status": "ready",
                    "result": winner,
                    "error": None,
                    "updatedAt": time.time(),
                })
                logger.info(
                    "Metadata job %s resolved via %s on round %s",
                    job_id,
                    winner.get("source", "unknown"),
                    job["rounds"],
                )
                return
        finally:
            for task in (remote_task, knaben_task, webtorrent_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                remote_task,
                knaben_task,
                webtorrent_task,
                return_exceptions=True,
            )

            # asyncio.to_thread cannot force-stop the libtorrent worker.
            # When another resolver already won, let that worker finish in the
            # shared session without delaying the successful response. When the
            # round failed, wait for it before starting the next retry round so
            # the same torrent is never added twice concurrently.
            if winner is None:
                try:
                    await libtorrent_task
                except Exception as exc:
                    errors.append(str(exc))

        if time.time() >= deadline_at:
            break
        delay = min(
            max(0, TORRENT_METADATA_BACKGROUND_RETRY_SECONDS),
            max(0, deadline_at - time.time()),
        )
        job["updatedAt"] = time.time()
        if delay > 0:
            await asyncio.sleep(delay)

    job.update({
        "status": "error",
        "error": "Torrent metadata could not be resolved within 6 hours.",
        "updatedAt": time.time(),
    })
    logger.warning("Metadata job %s exhausted its background deadline", job_id)
    asyncio.create_task(_schedule_metadata_job_expiry(job_id))


_load_metadata_cache()


def seedr_data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value

def normalize_seedr_token(value: str) -> str:
    """Normalize common Seedr API Console token copy formats without logging it."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    for _ in range(2):
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
            continue

        # A copied JSON response may be either an object or a JSON string.
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                raw = parsed.strip()
                continue
            if isinstance(parsed, dict):
                candidate = (
                    parsed.get("access_token")
                    or parsed.get("token")
                    or parsed.get("personal_access_token")
                )
                if candidate:
                    raw = str(candidate).strip()
                    continue
        except Exception:
            pass
        break

    # Some older Torrent Studio deployments stored the OAuth token as a
    # base64-encoded JSON object.
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        payload = json.loads(decoded)
        if isinstance(payload, dict) and payload.get("access_token"):
            raw = str(payload["access_token"]).strip()
    except Exception:
        pass

    return raw.strip().strip('"').strip("'").strip()


def seedr_access_token() -> str:
    """Access token used only by Seedr's legacy resource.php API."""
    return normalize_seedr_token(current_seedr_token())


async def legacy_seedr_request(
    func: str,
    method: str = "POST",
    body: dict[str, Any] | None = None,
) -> Any:
    """Call Seedr's documented legacy resource API using form data."""
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    access_token = seedr_access_token()
    if not access_token:
        raise HTTPException(503, "Seedr access token is empty")

    url = "https://www.seedr.cc/oauth_test/resource.php"
    params = {
        "access_token": access_token,
        "func": str(func),
    }
    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.request(
            method,
            url,
            params=params,
            data=body or {},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        raise SeedrError(
            *seedr_problem(response.status_code, data, raw)
        )

    if isinstance(data, dict):
        raw_error = str(data.get("error") or "").strip().lower()
        error_detail = str(
            data.get("error_description") or data.get("message") or data.get("error") or ""
        ).strip()
        normalized_error = error_detail.lower().replace("_", " ")
        if raw_error not in ("", "0"):
            if (
                "not enough space" in normalized_error
                or "insufficient space" in normalized_error
                or "not enough storage" in normalized_error
                or "storage full" in normalized_error
                or "not_enough_space" in raw_error
                or "insufficient_space" in raw_error
                or "not_enough_space" in normalized_error
                or normalized_error.startswith("not enough space")
                or normalized_error.startswith("insufficient space")
            ):
                raise SeedrError(
                    "SEEDR_INSUFFICIENT_SPACE",
                    413,
                    "Seedr does not have enough free space for this torrent.",
                )
            if "access_denied" in raw_error or "access denied" in normalized_error:
                raise SeedrError(
                    "SEEDR_LIBRARY_ACCESS_DENIED",
                    403,
                    "Seedr denied this account operation.",
                )
            if "unauthor" in raw_error or "invalid token" in normalized_error:
                raise SeedrError(
                    "SEEDR_TOKEN_REJECTED",
                    401,
                    "Seedr rejected the API token.",
                )
            # Seedr's legacy API often reports operation failures inside a 200
            # JSON response. Preserve the provider's error text instead of
            # treating that as a successful task creation.
            raise SeedrError(
                "SEEDR_API_ERROR",
                502,
                error_detail or "Seedr rejected the torrent.",
            )

    return data


async def legacy_seedr_list_contents(folder_id: str = "0") -> Any:
    """List a Seedr folder using the legacy resource endpoint.
    
    The free account/token used by Torrent Studio can deny the modern root
    filesystem endpoint while still permitting list_contents through Seedr's
    legacy resource API.
    """
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    access_token = seedr_access_token()
    if not access_token:
        raise HTTPException(503, "Seedr access token is empty")

    url = "https://www.seedr.cc/oauth_test/resource.php"
    params = {
        "access_token": access_token,
        "func": "list_contents",
    }
    form = {
        "content_type": "folder",
        "content_id": str(folder_id),
    }
    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.post(
            url,
            params=params,
            data=form,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        raise HTTPException(
            response.status_code,
            seedr_error_message(response.status_code, data, raw),
        )

    if isinstance(data, dict) and str(data.get("error") or "").strip() not in ("", "0"):
        raw_error = str(data.get("error") or "").strip().lower()
        if "access_denied" in raw_error or "access denied" in raw_error:
            raise SeedrError(
                "SEEDR_LIBRARY_ACCESS_DENIED",
                403,
                "Seedr denied access to the account library. The token is recognized, but this library operation is not permitted.",
            )
        if "unauthor" in raw_error or "invalid token" in raw_error:
            raise SeedrError(
                "SEEDR_TOKEN_REJECTED",
                401,
                "Seedr rejected the API token. Generate a fresh Seedr API token and update SEEDR_API_TOKEN in Render.",
            )
        raise SeedrError(
            "SEEDR_API_ERROR",
            502,
            f"Seedr legacy library request failed: {data.get('error')}",
        )

    return data


async def seedr_root_request() -> Any:
    """Fetch the Seedr account root using Seedr's dedicated root endpoint."""
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    url = "https://www.seedr.cc/api/folder"
    headers = {"Accept": "application/json"}
    params = {"access_token": seedr_access_token()}

    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        detail = raw
        if isinstance(data, dict):
            detail = (
                data.get("error_description")
                or data.get("reason_phrase")
                or data.get("message")
                or data.get("error")
                or raw
            )
        raise HTTPException(response.status_code, str(detail or "Seedr root request failed"))

    return data


def seedr_problem(status_code: int, data: Any, raw: str) -> tuple[str, int, str]:
    reason = ""
    if isinstance(data, dict):
        reason = str(
            data.get("reason_phrase")
            or data.get("error_description")
            or data.get("message")
            or data.get("error")
            or data.get("detail")
            or ""
        ).strip()

    normalized = reason.lower().replace("_", " ").strip()

    if status_code == 401 or "unauthorized" in normalized or normalized in {"invalid token", "token expired", "token invalid"}:
        return (
            "SEEDR_TOKEN_REJECTED",
            401,
            "Seedr rejected the API token. Generate a fresh Seedr API token and update SEEDR_API_TOKEN in Render.",
        )

    if status_code == 403 or normalized in {"access denied", "forbidden", "access_denied"}:
        return (
            "SEEDR_LIBRARY_ACCESS_DENIED",
            403,
            "Seedr denied access to this library operation. The API token is valid, but this operation/folder is not permitted.",
        )

    if status_code == 413 or normalized == "not enough space" or "not enough space" in normalized:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            413,
            "Seedr reports insufficient storage space for this operation.",
        )

    if status_code == 429:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            429,
            "Seedr rate limit reached. Please wait a moment and try again.",
        )

    if status_code >= 500:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            status_code,
            "Seedr is temporarily unavailable. Please try again in a moment.",
        )

    return (
        "SEEDR_API_ERROR",
        status_code,
        reason or raw[:500] or f"Seedr API request failed (HTTP {status_code})",
    )


def seedr_error_message(status_code: int, data: Any, raw: str) -> str:
    return seedr_problem(status_code, data, raw)[2]

async def seedr_request(path: str, method: str = "GET", body: Any = None, form: bool = False, base_url: str = SEEDR_BASE) -> Any:
    if not current_seedr_token():
        raise SeedrError(
            "SEEDR_TOKEN_MISSING",
            503,
            "No Seedr account is connected to this browser session.",
        )

    request_path = str(path).lstrip("/")
    selected_base_url = base_url
    if base_url == SEEDR_BASE and _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat":
        selected_base_url = SEEDR_PAT_BASE
    url = f"{str(selected_base_url).rstrip("/")}/{request_path}"
    token = normalize_seedr_token(current_seedr_token())

    kwargs: dict[str, Any] = {}
    if body is not None:
        if form:
            headers_base = {"Content-Type": "application/x-www-form-urlencoded"}
            kwargs["data"] = body
        else:
            headers_base = {"Content-Type": "application/json"}
            kwargs["json"] = body
    else:
        headers_base = {}

    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        tried_tokens: list[str] = []
        # Never fall back from one browser session to another Seedr credential.
        candidates = [token]

        last_status = 0
        last_data: Any = None
        last_raw = ""

        for candidate in candidates:
            if not candidate or candidate in tried_tokens:
                continue
            tried_tokens.append(candidate)

            headers = {
                "Authorization": f"Bearer {candidate}",
                "Accept": "application/json",
                **headers_base,
            }
            request_started = time.monotonic()
            logger.info(
                "Seedr API request start: session=%s mode=%s method=%s url=%s "
                "token_present=%s token_length=%s token_fingerprint=%s body_keys=%s",
                _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                method,
                url,
                bool(token),
                len(token),
                hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "none",
                sorted(body.keys()) if isinstance(body, dict) else [],
            )
            try:
                response = await client.request(method, url, headers=headers, **kwargs)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - request_started) * 1000)
                logger.exception(
                    "Seedr API transport failure: session=%s mode=%s method=%s url=%s "
                    "elapsed_ms=%s exception_type=%s exception=%s",
                    _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                    _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                    method,
                    url,
                    elapsed_ms,
                    type(exc).__name__,
                    str(exc)[:1000],
                )
                raise SeedrError(
                    "SEEDR_NETWORK_ERROR",
                    502,
                    f"Seedr request could not be completed: {type(exc).__name__}: {str(exc)[:500]}",
                ) from exc

            elapsed_ms = int((time.monotonic() - request_started) * 1000)
            raw = response.text
            try:
                data = response.json() if raw else None
            except Exception:
                data = raw

            response_request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("x-correlation-id")
                or response.headers.get("cf-ray")
                or ""
            )
            logger.info(
                "Seedr API response: session=%s mode=%s method=%s path=%s status=%s "
                "elapsed_ms=%s response_bytes=%s content_type=%s provider_request_id=%s",
                _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                method,
                request_path,
                response.status_code,
                elapsed_ms,
                len(raw.encode("utf-8", errors="ignore")),
                response.headers.get("content-type", ""),
                response_request_id[:200],
            )

            if response.status_code < 400:
                if isinstance(data, dict):
                    soft = str(data.get("reason_phrase") or "").strip().lower()
                    if soft == "not_enough_space":
                        raise SeedrError(
                            "SEEDR_QUOTA_UNAVAILABLE",
                            413,
                            "Seedr reports insufficient storage space for this operation.",
                        )
                return data

            last_status, last_data, last_raw = response.status_code, data, raw
            logger.warning(
                "Seedr API request failed: session=%s mode=%s method=%s path=%s url=%s "
                "status=%s elapsed_ms=%s token_length=%s token_fingerprint=%s "
                "provider_request_id=%s response_json=%s response_body=%s",
                _seedr_session_fingerprint(_seedr_request_session_id.get().strip()),
                _seedr_session_auth_mode(_seedr_request_session_id.get().strip()),
                method,
                request_path,
                url,
                response.status_code,
                elapsed_ms,
                len(token),
                hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "none",
                response_request_id[:200],
                json.dumps(data, ensure_ascii=False, default=str)[:4000] if isinstance(data, (dict, list)) else "<not-json>",
                raw[:4000] if raw else "<empty>",
            )

            break

    code, status_code, detail = seedr_problem(last_status, last_data, last_raw)
    raise SeedrError(code, status_code, detail)


def arr(value: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return []

def unwrap_seedr_task(value: Any) -> dict[str, Any]:
    """Normalize task responses that may be wrapped in data/task objects."""
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return {}
        nested = current.get("task")
        if isinstance(nested, dict):
            current = nested
            continue
        return current
    return current if isinstance(current, dict) else {}


def seedr_task_folder_id(task: dict[str, Any]) -> str:
    nested_torrent = task.get("torrent") if isinstance(task.get("torrent"), dict) else {}
    payload = task.get("torrent_payload") if isinstance(task.get("torrent_payload"), dict) else {}

    return str(
        task.get("folder_created_id")
        or task.get("folder_created")
        or task.get("folder_id")
        or task.get("folderId")
        or nested_torrent.get("folder_created_id")
        or nested_torrent.get("folder_id")
        or nested_torrent.get("folderId")
        or payload.get("folder_created_id")
        or payload.get("folder_id")
        or payload.get("folderId")
        or ""
    ).strip()


def magnet_display_name(value: Any) -> str:
    """Extract the torrent display name (dn) from a magnet-like value."""
    raw = str(value or "").strip()
    if not raw.lower().startswith("magnet:?"):
        return ""
    try:
        params = parse_qs(urlsplit(raw).query, keep_blank_values=True)
        return str((params.get("dn") or [""])[0]).strip()
    except Exception:
        return ""


def task_complete(task: dict[str, Any]) -> bool:
    """Return True only when Seedr reports a task as finished."""
    if not isinstance(task, dict):
        return False

    # Explicit completion flags are authoritative.
    for key in ("completed", "complete", "finished", "done", "is_completed", "isComplete"):
        value = task.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, (int, float)) and value == 1:
            return True

    state = str(
        task.get("state")
        or task.get("status")
        or task.get("phase")
        or ""
    ).strip().lower().replace("-", "_")

    try:
        progress = float(task.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0.0

    # A new Prepare must be able to replace stuck, queued, paused, or
    # metadata/seed-discovery tasks, even if the provider calls them "seeding".
    # Keep these as unfinished unless Seedr's explicit completion flags above
    # confirm completion. This avoids treating "Collecting Seeds" as completed.
    active_states = {
        "paused", "paused_dl", "pauseddl", "paused_download",
        "queued", "queued_dl", "queueddl", "queued_download",
        "downloading", "download", "collecting_seeds", "collecting seeds",
        "waiting", "metadata", "fetching_metadata", "checking",
    }
    if state in active_states:
        return False

    completed_states = {"complete", "completed", "finished", "done", "success"}
    if state in completed_states:
        return True

    # "seeding" normally means the payload is complete, but some Seedr task
    # variants use it for a stalled seed-discovery state without progress.
    if state == "seeding":
        return progress >= 100

    # A numeric 100% is authoritative even when the state field is absent or
    # uses a provider-specific value.
    return progress >= 100

def seedr_task_name(task: dict[str, Any]) -> str:
    nested_torrent = task.get("torrent") if isinstance(task.get("torrent"), dict) else {}
    payload = task.get("torrent_payload") if isinstance(task.get("torrent_payload"), dict) else {}
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    nested_link = (
        task.get("torrent_magnet")
        or task.get("magnet")
        or task.get("magnet_url")
        or nested_torrent.get("torrent_magnet")
        or nested_torrent.get("magnet")
        or payload.get("torrent_magnet")
        or payload.get("magnet")
        or ""
    )
    magnet_name = magnet_display_name(nested_link)

    # A magnet's dn is the closest match to the title the user selected from
    # search. Prefer it before Seedr's own generated/provider title.
    return str(
        task.get("torrent_name")
        or magnet_name
        or nested_torrent.get("torrent_name")
        or payload.get("torrent_name")
        or task.get("torrent_title")
        or task.get("torrentTitle")
        or nested_torrent.get("title")
        or nested_torrent.get("name")
        or payload.get("title")
        or payload.get("name")
        or task.get("title")
        or task.get("name")
        or meta.get("torrent_name")
        or meta.get("title")
        or ""
    ).strip()


def normalize_magnet(magnet: str) -> str:
    """Normalize the BTIH while preserving the magnet's tracker and name parameters."""
    value = re.sub(r"[\r\n\t]+", "", str(magnet or "").strip())
    decoded = unquote(value)
    if decoded.lower().startswith("magnet:?"):
        value = decoded
    if not value.lower().startswith("magnet:?"):
        return value

    try:
        parsed = urlsplit(value)
        params = parse_qs(parsed.query, keep_blank_values=True)
        valid_hash = ""
        for raw in params.get("xt", []):
            raw = unquote(raw)
            match = re.fullmatch(r"urn:btih:([A-Za-z0-9]{32,40})", raw, re.I)
            if not match:
                continue
            candidate = match.group(1)
            if len(candidate) == 32:
                candidate = base64.b32decode(
                    candidate.upper() + "=" * ((8 - len(candidate) % 8) % 8)
                ).hex()
            if len(candidate) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
                valid_hash = candidate.lower()
                break

        if not valid_hash:
            return value

        # Keep the human-readable name and all supplied trackers. Some
        # metadata-only peers are reachable only through the trackers present
        # in the original magnet.
        rebuilt: list[tuple[str, str]] = [("xt", "urn:btih:" + valid_hash)]
        for key in ("dn", "tr"):
            for item in params.get(key, []):
                text_value = str(item or "").strip()
                if text_value:
                    rebuilt.append((key, text_value))

        return "magnet:?" + urlencode(rebuilt, doseq=True)
    except Exception:
        return value

def info_hash(magnet: str) -> str:
    for _ in range(3):
        m = re.search(r"(?:urn:btih:|btih:)([A-Za-z0-9]{32,40})", magnet, re.I)
        if m:
            h = m.group(1)
            if len(h) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", h):
                return h.lower()
            if len(h) == 32:
                try:
                    return base64.b32decode(h.upper() + "=" * ((8-len(h)%8)%8)).hex()
                except Exception:
                    pass
        magnet = unquote(magnet)
    return ""

def task_id(task: dict[str, Any]) -> str:
    return str(task.get("user_torrent_id") or task.get("id") or task.get("task_id") or "").strip()

async def rename_seedr_folder(folder_id: str, name: str) -> bool:
    # Folder rename is deliberately not part of the free-account download path.
    # Seedr can expose the generated folder name asynchronously.
    return False


async def add_task(magnet: str, folder_id: int = 0) -> dict[str, Any]:
    # Forward the exact magnet URI supplied by the caller.
    # Do not parse, rebuild, decode, lowercase, or add/remove query parameters.
    logger.info(
        "Seedr direct add: POST /tasks folder_id=%s magnet_length=%s",
        int(folder_id),
        len(magnet),
    )
    result = await seedr_request(
        "/tasks",
        method="POST",
        body={
            "torrent_magnet": magnet,
            "folder_id": int(folder_id),
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(502, "Seedr did not return a valid task response")
    return result


def normalize_file(item: Any, folder_id: str = "") -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"id": "", "name": "Unnamed file", "size": 0, "folderId": folder_id}
    return {
        "id": str(item.get("id") or item.get("file_id") or ""),
        # Seedr V2 presentation endpoints use the canonical file id.
        # Keep legacy folder_file_id only as a fallback for older API shapes.
        "streamId": str(
            item.get("id")
            or item.get("file_id")
            or item.get("folder_file_id")
            or item.get("folderFileId")
            or ""
        ),
        "name": str(item.get("name") or item.get("title") or "Unnamed file"),
        "size": int(float(item.get("size") or 0)),
        "folderId": str(item.get("folder_id") or item.get("folderId") or folder_id),
    }

async def task_contents(tid: str) -> list[dict[str, Any]]:
    payload = seedr_data(await seedr_request(f"/tasks/{quote(tid)}/contents"))
    if not isinstance(payload, dict):
        return []
    files = [normalize_file(x, str(payload.get("folder_created_id") or "")) for x in arr(payload, ("files", "items"))]
    folder = str(payload.get("folder_created_id") or "").strip()
    if folder and (not files or any(not x["id"] for x in files)):
        try:
            folder_payload = seedr_data(await seedr_request(f"/fs/folder/{quote(folder)}/contents"))
            folder_files = arr(folder_payload, ("files", "items"))
            if folder_files:
                files = [normalize_file(x, folder) for x in folder_files]
        except HTTPException:
            pass
    return files

async def folder_name(folder_id: str) -> str:
    for endpoint in (f"/fs/folder/{quote(folder_id)}", f"/fs/folder/{quote(folder_id)}/contents"):
        try:
            payload = seedr_data(await seedr_request(endpoint))
            if isinstance(payload, dict):
                for key in ("name", "title", "folder_name", "folderName", "path"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        return Path(value.rstrip("/")).name
                for key in ("folder", "directory"):
                    child = payload.get(key)
                    if isinstance(child, dict):
                        for name_key in ("name", "title", "folder_name", "folderName", "path"):
                            value = str(child.get(name_key) or "").strip()
                            if value:
                                return Path(value.rstrip("/")).name
        except HTTPException:
            continue
    return ""

SEEDR_FOLDER_CONCURRENCY = 8
_seedr_folder_semaphore = asyncio.Semaphore(SEEDR_FOLDER_CONCURRENCY)

# Library metadata is shared between the Files explorer and the Seedr Library
# so opening the Files tab does not trigger the same Seedr tree walk twice.
SEEDR_METADATA_CACHE_SECONDS = 5
SEEDR_FOLDER_CACHE_SECONDS = 15
_seedr_metadata_cache: tuple[float, dict[str, Any]] | None = None
_seedr_metadata_task: asyncio.Task | None = None
_seedr_folder_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_seedr_torrent_names: dict[str, str] = {}
_seedr_torrent_names_by_task: dict[str, str] = {}

SEEDR_AUTO_DELETE_SECONDS = 2 * 60 * 60
SEEDR_CLEANUP_FILE = Path("/app/.seedr_cleanup.json")
_seedr_cleanup_jobs: dict[str, dict[str, Any]] = {}
_seedr_cleanup_worker_task: asyncio.Task | None = None


def _load_seedr_cleanup_jobs() -> None:
    global _seedr_cleanup_jobs
    try:
        raw = SEEDR_CLEANUP_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            _seedr_cleanup_jobs = {
                str(k): v for k, v in data.items()
                if isinstance(v, dict) and float(v.get("deleteAt") or 0) > 0
            }
    except Exception:
        _seedr_cleanup_jobs = {}


def _save_seedr_cleanup_jobs() -> None:
    try:
        SEEDR_CLEANUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEDR_CLEANUP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_seedr_cleanup_jobs, separators=(",", ":")), encoding="utf-8")
        tmp.replace(SEEDR_CLEANUP_FILE)
    except Exception as exc:
        logger.info("Could not persist Seedr cleanup schedule: %s", exc)


def schedule_seedr_cleanup(
    task_id_value: str,
    torrent_name: str,
    folder_id: str = "",
    added_at: float | None = None,
) -> None:
    tid = str(task_id_value or "").strip()
    if not tid:
        return

    now = time.time()
    started = float(added_at or now)
    _seedr_cleanup_jobs[tid] = {
        "taskId": tid,
        "torrentName": str(torrent_name or "").strip() or f"Torrent {tid}",
        "folderId": str(folder_id or "").strip(),
        "addedAt": started,
        "deleteAt": started + SEEDR_AUTO_DELETE_SECONDS,
    }
    _save_seedr_cleanup_jobs()


async def _cleanup_seedr_job(tid: str, job: dict[str, Any]) -> bool:
    folder_id = str(job.get("folderId") or "").strip()
    torrent_name = str(job.get("torrentName") or f"Torrent {tid}").strip()

    # Seedr may expose the folder only after the task starts.
    if not folder_id:
        try:
            raw = seedr_data(await seedr_request(f"/tasks/{quote(tid)}"))
            task = unwrap_seedr_task(raw)
            folder_id = seedr_task_folder_id(task)
            if folder_id:
                job["folderId"] = folder_id
                _save_seedr_cleanup_jobs()
        except HTTPException as exc:
            if exc.status_code == 404:
                _seedr_cleanup_jobs.pop(tid, None)
                _save_seedr_cleanup_jobs()
                return True
            return False
        except Exception:
            return False

    if not folder_id or not folder_id.isdigit() or folder_id == "0":
        return False

    # Stop the Seedr task first so an active transfer cannot keep rebuilding
    # files while the folder is being removed.
    try:
        await seedr_request(f"/tasks/{quote(tid)}", "DELETE")
    except HTTPException as exc:
        if exc.status_code != 404:
            logger.info("Seedr task cleanup failed for %s: HTTP %s", tid, exc.status_code)
            return False
    except Exception as exc:
        logger.info("Seedr task cleanup failed for %s: %s", tid, exc)
        return False

    try:
        await seedr_folder_delete(folder_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            logger.info("Seedr folder cleanup failed for %s (%s): HTTP %s", torrent_name, folder_id, exc.status_code)
            return False
    except Exception as exc:
        logger.info("Seedr folder cleanup failed for %s (%s): %s", torrent_name, folder_id, exc)
        return False

    _seedr_cleanup_jobs.pop(tid, None)
    _save_seedr_cleanup_jobs()
    logger.info("Auto-deleted Seedr torrent after 2 hours: %s (task=%s folder=%s)", torrent_name, tid, folder_id)
    return True


async def _seedr_cleanup_worker() -> None:
    while True:
        try:
            now = time.time()
            due = [
                (tid, job)
                for tid, job in list(_seedr_cleanup_jobs.items())
                if float(job.get("deleteAt") or 0) <= now
            ]
            for tid, job in due:
                await _cleanup_seedr_job(tid, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Seedr cleanup worker error: %s", exc)
        await asyncio.sleep(30)



async def collect_folder(folder_id: str, path: str = "/", depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []

    async with _seedr_folder_semaphore:
        try:
            payload = seedr_data(await seedr_request(f"/fs/folder/{quote(folder_id)}/contents"))
        except HTTPException as exc:
            if exc.status_code == 404:
                return []
            raise

    if not isinstance(payload, dict):
        return []

    files: list[dict[str, Any]] = []
    for raw in arr(payload, ("files", "items")):
        item = normalize_file(raw, folder_id)
        item["folderPath"] = path
        item["url"] = None
        files.append(item)

    child_jobs: list[asyncio.Future] = []
    for raw in arr(payload, ("folders", "directories")):
        child_id = str(raw.get("id") or raw.get("folder_id") or "") if isinstance(raw, dict) else ""
        if not child_id:
            continue

        child_name = (
            str(raw.get("name") or raw.get("title") or child_id)
            if isinstance(raw, dict)
            else child_id
        )
        child_path = path.rstrip("/") + "/" + child_name
        child_jobs.append(collect_folder(child_id, child_path, depth + 1))

    if child_jobs:
        children = await asyncio.gather(*child_jobs, return_exceptions=True)
        for child in children:
            if isinstance(child, list):
                files.extend(child)

    return files

def _safe_download_filename(filename: str, fallback: str) -> str:
    value = str(filename or "").strip().replace("\r", "").replace("\n", "")
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:240] or fallback


async def _stream_seedr_download(url: str):
    client = _InstrumentedAsyncClient(timeout=None, follow_redirects=True)
    request = client.build_request("GET", url, headers={"Accept": "*/*"})
    response = await client.send(request, stream=True)

    if response.status_code >= 400:
        try:
            detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
        finally:
            await response.aclose()
            await client.aclose()
        raise HTTPException(response.status_code, detail or "Seedr download request failed")

    content_type = response.headers.get("content-type") or "application/octet-stream"
    content_length = response.headers.get("content-length")
    content_range = response.headers.get("content-range")

    async def body():
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": "",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "no-store",
    }
    if content_length:
        headers["Content-Length"] = content_length
    if content_range:
        headers["Content-Range"] = content_range

    return body, content_type, headers


async def download_url(file_id: str) -> dict[str, str]:
    payload = seedr_data(await seedr_request(f"/download/file/{quote(file_id)}/url"))
    if isinstance(payload, dict):
        url = str(payload.get("url") or payload.get("download_url") or payload.get("downloadUrl") or payload.get("direct_url") or "")
        name = str(payload.get("name") or payload.get("filename") or "")
    else:
        url, name = str(payload or ""), ""
    if not url:
        raise HTTPException(502, "Seedr did not return a download URL")
    return {"url": url, "name": name}

def _srt_to_webvtt(text: str) -> str:
    """Convert a UTF-8/legacy SRT subtitle into browser-compatible WebVTT."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = normalized.split("\n")
    output = ["WEBVTT", ""]
    for line in lines:
        if "-->" in line:
            line = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", line)
        output.append(line)
    result = "\n".join(output)
    return result if result.endswith("\n") else result + "\n"


async def _seedr_subtitle_text(file_id: str, filename: str) -> tuple[str, str]:
    result = await download_url(file_id)
    url = result["url"]
    lower = (filename or result.get("name") or "").lower()
    async with _InstrumentedAsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        raw = response.content

    if lower.endswith(".vtt"):
        text = raw.decode("utf-8-sig", errors="replace")
        if not text.lstrip().startswith("WEBVTT"):
            text = _srt_to_webvtt(text)
    elif lower.endswith(".srt"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        text = _srt_to_webvtt(text)
    else:
        raise HTTPException(415, "Only SRT and WebVTT subtitle files are supported")
    return text, "text/vtt; charset=utf-8"


@app.get("/api/seedr/files/{file_id}/subtitle")
async def seedr_file_subtitle(
    file_id: str,
    filename: str = Query(""),
):
    """Proxy a Seedr SRT/VTT file as WebVTT for the browser <track> element."""
    text, content_type = await _seedr_subtitle_text(file_id, filename)
    return Response(
        content=text,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


def _search_tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token]


def _media_search_parts(value: str) -> tuple[str, int | None, int | None]:
    """Split a media query into title text plus optional season/episode constraints."""
    q = re.sub(r"\s+", " ", value.strip())
    season_match = re.search(r"\b(?:season|series)\s*(\d{1,2})\b", q, re.I)
    episode_match = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", q, re.I)
    season = int(season_match.group(1)) if season_match else (
        int(episode_match.group(1)) if episode_match else None
    )
    episode = (
        int(season_match.group(1)) if False else None
    )
    if episode_match and episode_match.group(2):
        episode = int(episode_match.group(2))

    title = q
    title = re.sub(r"\b(?:season|series)\s*\d{1,2}\b", " ", title, flags=re.I)
    title = re.sub(r"\bS\d{1,2}(?:E\d{1,3})?\b", " ", title, flags=re.I)
    title = re.sub(
        r"\b(?:19|20)\d{2}\b|"
        r"\b(?:2160p|1440p|1080p|720p|480p|4k|8k)\b|"
        r"\b(?:webrip|web-dl|bluray|brrip|x264|x265|h264|h265|hevc|hdr)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip()
    return title, season, episode


def _season_episode_match(title: str, season: int | None, episode: int | None) -> bool:
    if season is None:
        return True

    upper = title.upper()
    match = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", upper)
    if match:
        if int(match.group(1)) != season:
            return False
        if episode is not None and (not match.group(2) or int(match.group(2)) != episode):
            return False
        return True

    season_word = re.search(r"\bSEASON[\s._-]*(\d{1,2})\b", upper)
    if season_word:
        if int(season_word.group(1)) != season:
            return False
        return episode is None

    return False


def _tvmaze_query(value: str) -> str:
    value = re.sub(r"\bS\d{1,2}(?:E\d{1,3})?.*$", "", value, flags=re.I)
    value = re.sub(r"\b(?:season|series)\s*\d+\b", "", value, flags=re.I)
    value = re.sub(r"\b(?:19|20)\d{2}\b", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_title(value: str) -> str:
    return " ".join(_search_tokens(value))


X1337_HOSTS = [
    "1337x.to",
    "1337x.st",
    "x1337x.ws",
    "x1337x.eu",
    "x1337x.cc",
]

# 1337x subcategory IDs represented by the icon at the left of each result.
# The icon describes the release type; it is not a quality filter.
X1337_MOVIE_SUBCATEGORIES = {"1","2","3","4","42","54","55","66","70","73","76"}
X1337_TV_SUBCATEGORIES = {"5","6","7","9","41","71","74","75"}
X1337_MEDIA_SUBCATEGORIES = X1337_MOVIE_SUBCATEGORIES | X1337_TV_SUBCATEGORIES


def _x1337_rows(html_text: str) -> list[dict[str, str]]:
    """Parse 1337x search rows across mirror HTML variations."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    rows: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for link in soup.find_all("a", href=re.compile(r"^/torrent/")):
        href = str(link.get("href") or "").strip()
        if not href or href in seen_paths:
            continue
        row = link.find_parent("tr")
        if row is None:
            continue
        title = link.get_text(" ", strip=True)
        if not title:
            continue
        cells = row.find_all("td")
        cell_text = [cell.get_text(" ", strip=True) for cell in cells]
        row_text = " ".join(cell_text) or row.get_text(" ", strip=True)
        size = ""
        seeders = "0"
        leechers = "0"
        for cell in cells:
            classes = " ".join(cell.get("class") or []).lower()
            text = cell.get_text(" ", strip=True)
            size_match = re.search(r"([\d.]+\s*[KMGT]i?B)", text, re.I)
            if not size and size_match:
                size = size_match.group(1)
            number = re.search(r"\d[\d,]*", text)
            if number and "seed" in classes:
                seeders = number.group(0).replace(",", "")
            elif number and ("leech" in classes or "leeches" in classes):
                leechers = number.group(0).replace(",", "")
        if not size:
            size_match = re.search(r"([\d.]+\s*[KMGT]i?B)", row_text, re.I)
            if size_match:
                size = size_match.group(1)
        numeric_cells = [re.sub(r"[^\d,]", "", text) for text in cell_text if re.fullmatch(r"\s*[\d,]+\s*", text or "")]
        if seeders == "0" and len(numeric_cells) >= 2:
            seeders = numeric_cells[-2] or "0"
        if leechers == "0" and len(numeric_cells) >= 1:
            leechers = numeric_cells[-1] or "0"
        seen_paths.add(href)
        sub_href = ""
        sub_id = ""
        if cells:
            sub_link = cells[0].find("a", href=re.compile(r"^/sub/"))
            if sub_link is not None:
                sub_href = str(sub_link.get("href") or "").strip()
                sub_match = re.search(r"^/sub/(\d+)/", sub_href)
                if sub_match:
                    sub_id = sub_match.group(1)

        rows.append({
            "title": title,
            "path": href,
            "size": size or "0 B",
            "seeders": seeders,
            "leechers": leechers,
            "subcategory_path": sub_href,
            "subcategory_id": sub_id,
        })
    return rows

async def search_1337x_direct(
    query: str,
    limit: int = 50,
    pages: int = 2,
    category: str | None = None,
    provider_query: str | None = None,
) -> list[dict[str, Any]]:
    if not _MANUAL_TORRENT_SEARCH.get():
        _record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Primary 1337x search using media categories and cheap listing-page scraping.

    The unsorted category route is the reliable base path. The server-side
    seeders-sorted route is fetched as an optional second listing because 1337x
    integrations document that sorted keyword searches can fail under load.
    """
    q, season, episode = _media_search_parts(query)
    if not q:
        return []
    q = re.sub(r"\s+", " ", provider_query or _media_provider_query(query)).strip()
    if not q:
        return []

    encoded = quote(q, safe="").replace("%20", "+")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # Apply the shared configured search-size window.
    minimum_size, maximum_size = MIN_SEARCH_RESULT_SIZE_BYTES, MAX_SEARCH_RESULT_SIZE_BYTES
    categories = (
        [category]
        if category in {"Movies", "TV"}
        else (["TV"] if season is not None or episode is not None else ["Movies", "TV"])
    )

    async with _InstrumentedAsyncClient(
        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:

        async def find_category_page(category_name: str) -> tuple[str, str]:
            # Probe only the normal category-search route. One cheap route is
            # enough to identify a usable mirror; do not serially probe both
            # normal and sorted routes across every mirror.
            path = f"/category-search/{encoded}/{category_name}/1/"
            for host in X1337_HOSTS:
                try:
                    async with _X1337_REQUEST_SEMAPHORE:
                        response = await client.get(f"https://{host}{path}")
                    if response.status_code < 400 and _x1337_rows(response.text):
                        return f"https://{host}", response.text
                except httpx.HTTPError:
                    continue
            return "", ""

        category_pages = await asyncio.gather(
            *(find_category_page(name) for name in categories),
            return_exceptions=False,
        )

        # Generic search is also queried because language searches can surface
        # Hindi/dual-audio releases there that are not near the top of a
        # category search. Every generic row is classified through /sub/<id>/
        # so non-media categories cannot leak into the app.
        generic_base = next((base for base, _ in category_pages if base), "")
        generic_pages: list[str] = []
        if generic_base:
            generic_urls = [f"{generic_base}/search/{encoded}/1/"]
            if pages > 1:
                generic_urls.append(f"{generic_base}/search/{encoded}/2/")

            async def fetch_generic(url: str) -> str:
                try:
                    async with _X1337_REQUEST_SEMAPHORE:
                        response = await client.get(url)
                    if response.status_code < 400 and _x1337_rows(response.text):
                        return response.text
                except httpx.HTTPError:
                    pass
                return ""

            generic_pages = [
                value for value in await asyncio.gather(
                    *(fetch_generic(url) for url in generic_urls),
                    return_exceptions=False,
                )
                if value
            ]

        async def fetch_category(
            category_name: str,
            base: str,
            first_page: str,
        ) -> list[dict[str, str]]:
            if not base or not first_page:
                return []

            # Normal page 1 + sorted page 1 are fetched together. Sorted is
            # treated as enrichment, never as a requirement.
            paths = [
                ("normal", first_page),
                (
                    "sorted",
                    f"{base}/sort-category-search/{encoded}/{category_name}/seeders/desc/1/",
                ),
            ]
            if pages > 1:
                paths.append((
                    "normal2",
                    f"{base}/category-search/{encoded}/{category_name}/2/",
                ))

            async def fetch_one(kind: str, value: str):
                if kind == "normal":
                    return value
                try:
                    async with _X1337_REQUEST_SEMAPHORE:
                        response = await client.get(value)
                    if response.status_code < 400 and _x1337_rows(response.text):
                        return response.text
                except httpx.HTTPError:
                    pass
                return ""

            fetched = await asyncio.gather(
                *(fetch_one(kind, value) for kind, value in paths),
                return_exceptions=False,
            )
            pages_html = [text for text in fetched if isinstance(text, str) and text]
            return pages_html

        fetched_by_category = await asyncio.gather(
            *(
                fetch_category(name, base, first_page)
                for name, (base, first_page) in zip(categories, category_pages)
            ),
            return_exceptions=False,
        )

        kind_hint = "tv" if (season is not None or episode is not None) else "both"

        async def parse_candidates(
            category_name: str,
            html_pages: list[str],
            generic_search: bool = False,
        ) -> list[dict[str, str]]:
            target = _media_provider_query(query)
            tokens = _search_tokens(target)
            compact_query = "".join(tokens)
            candidates, seen_paths = [], set()

            for html_text in html_pages:
                for row in _x1337_rows(html_text):
                    path = row.get("path") or ""
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)

                    normalized = _normalize_title(row["title"])
                    compact_normalized = normalized.replace(" ", "")
                    if tokens and not (
                        all(token in normalized for token in tokens)
                        or (compact_query and compact_query in compact_normalized)
                    ):
                        continue
                    if not _season_episode_match(row["title"], season, episode):
                        continue

                    if generic_search:
                        sub_id = str(row.get("subcategory_id") or "")
                        if sub_id not in X1337_MEDIA_SUBCATEGORIES:
                            continue
                        if kind_hint == "tv" and sub_id not in X1337_TV_SUBCATEGORIES:
                            continue

                    size_match = re.match(
                        r"([\d.]+)\s*([KMGT]i?B)",
                        row.get("size", ""),
                        re.IGNORECASE,
                    )
                    if not size_match:
                        continue
                    units = {
                        "KB": 1024, "KIB": 1024,
                        "MB": 1024**2, "MIB": 1024**2,
                        "GB": 1024**3, "GIB": 1024**3,
                        "TB": 1024**4, "TIB": 1024**4,
                    }
                    size = int(
                        float(size_match.group(1))
                        * units[size_match.group(2).upper()]
                    )
                    if not minimum_size <= size <= maximum_size:
                        continue

                    row["size_bytes"] = str(size)
                    row["media_category"] = category_name
                    candidates.append(row)

            candidates.sort(
                key=lambda row: (
                    _title_relevance(row["title"], query)[0],
                    int(row.get("seeders") or 0),
                    int(row.get("leechers") or 0),
                ),
                reverse=True,
            )
            return candidates

        candidate_lists = await asyncio.gather(
            *(
                parse_candidates(name, html_pages)
                for name, html_pages in zip(categories, fetched_by_category)
            ),
            return_exceptions=False,
        )

        candidates = [
            row
            for values in candidate_lists
            for row in values
        ]

        if generic_pages:
            candidates.extend(
                await parse_candidates(
                    "TV" if kind_hint == "tv" else "Movies",
                    generic_pages,
                    generic_search=True,
                )
            )

        unique: dict[str, dict[str, str]] = {}
        for row in candidates:
            key = row.get("path") or _normalize_title(row.get("title", ""))
            unique.setdefault(key, row)

        candidates = list(unique.values())
        candidates.sort(
            key=lambda row: (
                _title_relevance(row["title"], query)[0],
                int(row.get("seeders") or 0),
                int(row.get("leechers") or 0),
            ),
            reverse=True,
        )
        # Detail pages are the expensive part. Keep lookup fan-out bounded
        # on Render Free while letting DVD/HD/HEVC/dual-audio/h.264 and other
        # valid 1337x release types compete.
        candidates = candidates[:min(max(limit, 1), 20)]

        async def fetch_detail(row):
            # Keep useful listing results even when the provider's detail page
            # is slow or blocks server-side requests. Magnets are resolved on
            # demand by /api/search/resolve-magnet when a user clicks Prepare.
            detail_url = base_for_row(row) + row["path"]
            return {
                "guid": "1337x-" + hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:20],
                "title": row["title"],
                "size": int(row["size_bytes"]),
                "seeders": int(row.get("seeders") or 0),
                "leechers": int(row.get("leechers") or 0),
                "indexer": "1337x",
                "protocol": "torrent",
                "publishDate": "",
                "magnetUrl": None,
                "infoHash": "",
                "downloadUrl": None,
                "infoUrl": detail_url,
                "sourceUrl": detail_url,
                "descriptorUrl": "",
                "category": row.get("media_category") or "Video",
            }

        # Preserve the base URL selected for each category without doing a
        # second mirror probe before opening detail pages.
        row_bases: dict[str, str] = {}
        for category_name, (base, _) in zip(categories, category_pages):
            if base:
                for row in candidate_lists[categories.index(category_name)]:
                    row_bases[row["path"]] = base

        def base_for_row(row: dict[str, str]) -> str:
            return row_bases.get(row["path"], category_pages[0][0] if category_pages else "")

        fetched = await asyncio.gather(
            *(fetch_detail(row) for row in candidates),
            return_exceptions=True,
        )
    return [item for item in fetched if isinstance(item, dict)]

async def search_yts_movies(query: str, limit: int = 50) -> list[dict[str, Any]]:
    if not _MANUAL_TORRENT_SEARCH.get():
        _record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Search YTS directly so movie searches are not lost in aggregate ranking."""
    movie_query = _media_provider_query(query)
    if not movie_query:
        return []

    payload = None
    async with _InstrumentedAsyncClient(timeout=12, follow_redirects=True) as client:
        for host in YTS_API_HOSTS:
            try:
                async with _YTS_REQUEST_SEMAPHORE:
                    response = await client.get(
                        f"https://{host}/api/v2/list_movies.json",
                        params={"query_term": movie_query, "limit": "50"},
                        headers={"Accept": "application/json"},
                    )
                response.raise_for_status()
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
                    break
            except (httpx.HTTPError, ValueError):
                continue

    if not isinstance(payload, dict):
        return []

    target_tokens = _search_tokens(movie_query)
    results: list[dict[str, Any]] = []
    for movie in (payload.get("data") or {}).get("movies") or []:
        if not isinstance(movie, dict):
            continue
        title = str(movie.get("title_long") or movie.get("title") or "").strip()
        if not title:
            continue
        title_tokens = _normalize_title(title)
        if target_tokens and not all(token in title_tokens for token in target_tokens):
            continue

        released = movie.get("date_uploaded_unix")
        try:
            from datetime import datetime, timezone
            published = datetime.fromtimestamp(
                int(released), tz=timezone.utc
            ).isoformat() if released else ""
        except Exception:
            published = ""

        for torrent in movie.get("torrents") or []:
            if not isinstance(torrent, dict):
                continue
            h = str(torrent.get("hash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            quality = str(torrent.get("quality") or "").strip()
            kind = str(torrent.get("type") or "").strip()
            suffix = " ".join(x for x in (quality, kind) if x)
            display_title = f"{title} [{suffix}]" if suffix else title
            magnet = (
                f"magnet:?xt=urn:btih:{h}&dn={quote(display_title, safe='')}"
            )
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet += "&tr=" + quote(tracker, safe="")

            results.append({
                "guid": f"yts-{h}",
                "title": display_title,
                "mediaTitle": title,
                "year": int(movie.get("year") or 0) or None,
                "rating": float(movie.get("rating") or 0) or None,
                "genres": [str(g).strip() for g in (movie.get("genres") or []) if str(g).strip()],
                "posterUrl": f"/api/poster?title={quote(title, safe='')}&year={quote(str(movie.get('year') or ''), safe='')}",
                "quality": suffix,
                "size": int(float(torrent.get("size_bytes") or 0)),
                "seeders": int(torrent.get("seeds") or 0),
                "leechers": int(torrent.get("peers") or 0),
                "indexer": "yts.mx",
                "protocol": "torrent",
                "publishDate": published,
                "magnetUrl": magnet,
                "infoHash": h,
                "downloadUrl": magnet,
                "infoUrl": "",
                "sourceUrl": "",
            })

    results.sort(
        key=lambda row: (row["seeders"] + row["leechers"], row["publishDate"]),
        reverse=True,
    )
    return results[:limit]


async def search_tv_eztv(query: str, limit: int = 30) -> list[dict[str, Any]]:
    tv_query, _season, _episode = _media_search_parts(query)
    tv_query = _tvmaze_query(tv_query)
    if not tv_query:
        return []

    try:
        async with _InstrumentedAsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                "https://api.tvmaze.com/search/shows",
                params={"q": tv_query},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            matches = response.json()
    except (httpx.HTTPError, ValueError):
        return []

    if not isinstance(matches, list):
        return []

    target = _normalize_title(tv_query)
    imdb_id = ""
    for match in matches[:10]:
        show = match.get("show") if isinstance(match, dict) else None
        if not isinstance(show, dict):
            continue
        name = str(show.get("name") or "").strip()
        external = show.get("externals")
        candidate = str(external.get("imdb") or "").strip() if isinstance(external, dict) else ""
        if candidate and _normalize_title(name) == target:
            imdb_id = candidate
            break

    if not imdb_id:
        return []

    payload: dict[str, Any] | None = None
    async with _InstrumentedAsyncClient(timeout=15, follow_redirects=True) as client:
        for host in ("eztv.yt", "eztvx.to"):
            try:
                response = await client.get(
                    f"https://{host}/api/get-torrents",
                    params={"imdb_id": imdb_id, "limit": "100", "page": "1"},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
                    break
            except (httpx.HTTPError, ValueError):
                continue

    if not payload:
        return []

    results: list[dict[str, Any]] = []
    for item in payload.get("torrents") or []:
        if not isinstance(item, dict):
            continue
        h = str(item.get("hash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", h):
            continue
        title = str(item.get("filename") or item.get("title") or "").strip()
        if not title:
            continue
        magnet = str(item.get("magnet_url") or "").strip()
        if not magnet:
            magnet = f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}"
        try:
            from datetime import datetime, timezone
            published = datetime.fromtimestamp(
                int(item.get("date_released_unix") or 0), tz=timezone.utc
            ).isoformat() if item.get("date_released_unix") else ""
        except Exception:
            published = ""
        results.append({
            "guid": f"eztv-{h}",
            "title": title,
            "size": int(float(item.get("size_bytes") or 0)),
            "seeders": int(item.get("seeds") or 0),
            "leechers": int(item.get("peers") or 0),
            "indexer": "eztv.yt",
            "protocol": "torrent",
            "publishDate": published,
            "magnetUrl": magnet,
            "infoHash": h,
            "downloadUrl": magnet,
            "infoUrl": "",
            "sourceUrl": "",
        })
    _, season, episode = _media_search_parts(query)
    if season is not None:
        results = [
            row for row in results
            if _season_episode_match(row["title"], season, episode)
        ]

    results.sort(
        key=lambda row: (row["seeders"] + row["leechers"], row["publishDate"]),
        reverse=True,
    )
    return results[:limit]



LIMETORRENTS_HOSTS = tuple(
    host.strip()
    for host in os.getenv(
        "LIMETORRENTS_HOSTS",
        "www.limetorrents.fun,www.limetorrents.lol,limetorrents.fun,limetorrents.lol",
    ).split(",")
    if host.strip()
)


def _limetorrents_rows(html_text: str, base_url: str) -> list[dict[str, str]]:
    """Parse LimeTorrents rows using the current table structure."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    # LimeTorrents result rows may be direct children of table.table2
    # rather than tbody children, and may not carry a bgcolor attribute.
    # Avoid relying on either HTML detail.
    for row in soup.select("table.table2 tr"):
        # Prefer the visible title link over the empty download-icon link.
        title_anchor = row.select_one(".tt-name a[href*='-torrent-']")
        if title_anchor is None:
            title_anchor = next(
                (
                    anchor
                    for anchor in row.select(".tt-name a[href]")
                    if anchor.get_text(" ", strip=True)
                    and str(anchor.get("href") or "").lower().endswith(".html")
                ),
                None,
            )
        if title_anchor is None:
            continue

        title = title_anchor.get_text(" ", strip=True)
        detail_href = str(title_anchor.get("href") or "").strip()
        if not title or not detail_href:
            continue

        # LimeTorrents exposes the BTIH hash on the small download-icon
        # link in the listing row. Prefer this hash so Prepare can submit a
        # real magnet to Seedr without relying only on a detail-page scrape.
        download_anchor = (
            row.select_one(".tt-name a.csprite_dl14[href]")
            or row.select_one(".tt-name a[href*='/download/']")
        )
        download_href = str(download_anchor.get("href") or "").strip() if download_anchor else ""
        hash_match = re.search(
            r"(?i)(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
            download_href,
        )
        torrent_hash = hash_match.group(1).lower() if hash_match else ""
        magnet_url = ""
        if torrent_hash:
            magnet_url = f"magnet:?xt=urn:btih:{torrent_hash}&dn={quote(title, safe='')}"
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet_url += "&tr=" + quote(tracker, safe="")

        seed_cell = row.select_one(".tdseed")
        leech_cell = row.select_one(".tdleech")
        seeders = "0"
        leechers = "0"
        if seed_cell is not None:
            match = re.search(r"\d[\d,]*", seed_cell.get_text(" ", strip=True))
            if match:
                seeders = match.group(0).replace(",", "")
        if leech_cell is not None:
            match = re.search(r"\d[\d,]*", leech_cell.get_text(" ", strip=True))
            if match:
                leechers = match.group(0).replace(",", "")

        cells = row.find_all("td")
        size = ""
        if len(cells) >= 3:
            match = re.search(r"([\d.]+\s*[KMGT]i?B)", cells[2].get_text(" ", strip=True), re.I)
            if match:
                size = match.group(1)
        if not size:
            match = re.search(r"([\d.]+\s*[KMGT]i?B)", row.get_text(" ", strip=True), re.I)
            if match:
                size = match.group(1)

        source_url = urljoin(base_url.rstrip("/") + "/", detail_href.lstrip("/"))
        key = source_url.lower()
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "title": title,
            "detail_url": source_url,
            "size": size,
            "seeders": seeders,
            "leechers": leechers,
            "info_hash": torrent_hash,
            "magnet_url": magnet_url,
        })

    return rows


async def search_limetorrents(
    query: str,
    limit: int = 50,
    pages: int = 2,
) -> list[dict[str, Any]]:
    if not _MANUAL_TORRENT_SEARCH.get():
        _record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Search LimeTorrents concurrently across mirrors; resolve magnets on demand."""
    _title_query, season, episode = _media_search_parts(query)
    provider_query = _media_provider_query(query)

    # Keep language terms in the provider query so "thor hindi" searches the
    # Hindi result pool directly instead of searching only for "thor".
    _, languages = _search_query_constraints(query)
    language_terms = [
        language
        for language in languages
        if language in {
            "hindi", "tamil", "telugu", "malayalam", "kannada",
            "bengali", "marathi", "punjabi", "gujarati", "urdu",
        }
    ]
    provider_query = re.sub(
        r"\s+",
        " ",
        f"{provider_query} {' '.join(language_terms)}".strip(),
    )
    if not provider_query:
        return []

    category = "tv" if season is not None or episode is not None else "movies"
    encoded = quote(provider_query, safe="")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with _InstrumentedAsyncClient(
        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:
        async def fetch_host(host: str) -> list[tuple[str, str]]:
            base = f"https://{host}"
            urls = [f"{base}/search/all/{encoded}/seeds/1/"]
            if pages > 1:
                urls.append(f"{base}/search/all/{encoded}/seeds/2/")

            async def fetch_page(url: str) -> str:
                try:
                    async with _LIMETORRENTS_REQUEST_SEMAPHORE:
                        response = await client.get(url)
                    if response.status_code < 400 and response.text:
                        return response.text
                except httpx.HTTPError:
                    pass
                return ""

            bodies = await asyncio.gather(
                *(fetch_page(url) for url in urls),
                return_exceptions=False,
            )
            return [(base, body) for body in bodies if body]

        # Probe all mirrors together. Previously a dead first mirror could
        # consume the timeout sequentially before a healthy mirror was tried.
        mirror_pages = await asyncio.gather(
            *(fetch_host(host) for host in LIMETORRENTS_HOSTS),
            return_exceptions=True,
        )
        pages_html: list[tuple[str, str]] = []
        for value in mirror_pages:
            if isinstance(value, list):
                pages_html.extend(value)

        raw_rows: list[dict[str, str]] = []
        for base, html_text in pages_html:
            raw_rows.extend(_limetorrents_rows(html_text, base))

        if not raw_rows:
            logger.warning(
                "LimeTorrents unavailable for '%s' (hosts=%s)",
                query,
                ",".join(LIMETORRENTS_HOSTS),
            )
            return []

        units = {
            "KB": 1024, "KIB": 1024,
            "MB": 1024**2, "MIB": 1024**2,
            "GB": 1024**3, "GIB": 1024**3,
            "TB": 1024**4, "TIB": 1024**4,
        }

        # Merge repeated rows from mirror domains, retaining the highest
        # reported seed/peer counts for each title and size.
        unique_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in raw_rows:
            size_match = re.match(r"([\d.]+)\s*([KMGT]i?B)", row.get("size", ""), re.I)
            if not size_match:
                continue
            size = int(float(size_match.group(1)) * units[size_match.group(2).upper()])
            key = (_normalize_title(str(row.get("title") or "")), str(size))
            candidate = {**row, "size_bytes": size}
            previous = unique_rows.get(key)
            if previous is None or (
                int(candidate.get("seeders") or 0), int(candidate.get("leechers") or 0)
            ) > (
                int(previous.get("seeders") or 0), int(previous.get("leechers") or 0)
            ):
                unique_rows[key] = candidate

        parsed_rows = list(unique_rows.values())
        parsed_rows.sort(
            key=lambda row: (
                int(row.get("seeders") or 0),
                int(row.get("leechers") or 0),
            ),
            reverse=True,
        )

        results: list[dict[str, Any]] = []
        for row in parsed_rows[:min(max(limit, 1), 30)]:
            detail_url = str(row.get("detail_url") or "").strip()
            if not detail_url:
                continue
            results.append({
                "guid": "limetorrents-" + hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:20],
                "title": str(row.get("title") or ""),
                "size": int(row.get("size_bytes") or 0),
                "seeders": int(row.get("seeders") or 0),
                "leechers": int(row.get("leechers") or 0),
                "indexer": "LimeTorrents",
                "protocol": "torrent",
                "publishDate": "",
                "magnetUrl": str(row.get("magnet_url") or "").strip() or None,
                "infoHash": str(row.get("info_hash") or "").strip(),
                "downloadUrl": str(row.get("magnet_url") or "").strip() or None,
                "infoUrl": detail_url,
                "sourceUrl": detail_url,
                "descriptorUrl": "",
                "category": "TV" if category == "tv" else "Movies",
            })

    logger.warning(
        "LimeTorrents listing '%s': %d results; top seeders=%s",
        query,
        len(results),
        ",".join(str(item.get("seeders") or 0) for item in results[:5]),
    )
    return results[:limit]


async def search_knaben(
    query: str,
    limit: int = 100,
    provider_query: str | None = None,
) -> list[dict[str, Any]]:
    if not _MANUAL_TORRENT_SEARCH.get():
        _record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Search Knaben API v2 using its edge-cached GET interface."""
    title_query, season, episode = _media_search_parts(query)
    title_query = re.sub(r"\s+", " ", provider_query or _media_provider_query(query)).strip()
    if not title_query:
        return []

    target_tokens = _search_tokens(title_query)
    # Knaben API v2 moved search to GET /v2/search and renamed the parameters.
    # Accept an older /v1 environment override but transparently upgrade it.
    endpoint = KNABEN_API_URL.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3] + "/v2/search"
    elif endpoint.endswith("/v2"):
        endpoint += "/search"
    elif endpoint in {"https://api.knaben.org", "http://api.knaben.org"}:
        endpoint += "/v2/search"

    params = {
        "q": title_query,
        "sf": "title",
        "o": "seeders",
        "d": "desc",
        "s": min(max(int(limit or 100), 1), 150),
        "f": 0,
        "seen": 604800,
    }

    try:
        # v2 is edge-cached, but give it a little more than the old 2.25s v1
        # budget: the latter was consistently timing out in Home's live logs.
        timeout = httpx.Timeout(max(7.0, SEARCH_SOURCE_TIMEOUT_SECONDS), connect=6.0)
        async with _InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with _KNABEN_REQUEST_SEMAPHORE:
                response = await client.get(
                    endpoint,
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "HomeMovieSearch/1.0"},
                )
            response.raise_for_status()
            payload = response.json()
            raw_hits = payload if isinstance(payload, list) else (
                payload.get("hits", []) if isinstance(payload, dict) else []
            )
            logger.info(
                "Knaben v2 HTTP %s for '%s': %d hits",
                response.status_code,
                query,
                len(raw_hits) if isinstance(raw_hits, list) else 0,
            )
    except (httpx.HTTPError, ValueError) as exc:
        # Include the exception type because timeout exceptions often have an
        # empty string, which made the previous production logs unhelpful.
        logger.warning(
            "Knaben v2 search failed for '%s': %s: %r",
            query,
            type(exc).__name__,
            str(exc),
        )
        return []

    if not isinstance(raw_hits, list):
        return []

    results: list[dict[str, Any]] = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue

        title = str(hit.get("title") or "").strip()
        category = str(hit.get("category") or "").strip()
        if not title:
            continue

        normalized_title = _normalize_title(title)
        if target_tokens and not all(token in normalized_title for token in target_tokens):
            continue
        if category and any(
            blocked in category.lower()
            for blocked in ("anime", "games", "music", "software", "books", "porn", "xxx", "adult")
        ):
            continue
        if category and not any(
            allowed in category.lower()
            for allowed in ("video", "movie", "tv", "television", "series")
        ):
            continue
        if season is not None and not _season_episode_match(title, season, episode):
            continue

        magnet = str(hit.get("magnetUrl") or hit.get("magnet_url") or "").strip()
        h = str(hit.get("hash") or hit.get("infoHash") or hit.get("info_hash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", h):
            h = info_hash(magnet)
        if not magnet and re.fullmatch(r"[0-9a-f]{40}", h):
            magnet = f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}"

        try:
            size = int(hit.get("bytes") or hit.get("size_bytes") or hit.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            seeders = int(hit.get("seeders") or hit.get("seeds") or 0)
        except (TypeError, ValueError):
            seeders = 0
        try:
            leechers = int(hit.get("peers") or hit.get("leechers") or 0)
        except (TypeError, ValueError):
            leechers = 0

        detail_url = str(hit.get("details") or hit.get("infoUrl") or "").strip()
        descriptor_url = str(hit.get("link") or hit.get("descriptorUrl") or "").strip()
        tracker = str(hit.get("cachedOrigin") or hit.get("tracker") or hit.get("trackerId") or "Knaben").strip()
        published = str(hit.get("date") or hit.get("publishDate") or "").strip()

        results.append({
            "guid": f"knaben-{h or hit.get('id') or title}",
            "title": title,
            "size": size,
            "seeders": seeders,
            "leechers": leechers,
            "indexer": tracker,
            "protocol": "torrent",
            "publishDate": published,
            "magnetUrl": magnet or None,
            "infoHash": h if re.fullmatch(r"[0-9a-f]{40}", h, re.I) else "",
            "downloadUrl": magnet or None,
            "infoUrl": detail_url,
            "sourceUrl": detail_url or descriptor_url,
            "descriptorUrl": descriptor_url,
            "category": category,
        })

    results.sort(
        key=lambda row: (
            1 if _normalize_title(str(row["title"])).startswith(_normalize_title(title_query)) else 0,
            int(row.get("seeders") or 0),
        ),
        reverse=True,
    )
    logger.info("Knaben v2 search '%s': %d relevant results after title/category filtering", query, len(results))
    return results[:limit]



async def search_torrents_csv(query: str, limit: int) -> dict[str, Any]:
    if not _MANUAL_TORRENT_SEARCH.get():
        _record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return {"source": "torrents-csv", "elapsedMs": 0, "results": []}
    started = time.monotonic()
    try:
        async with _InstrumentedAsyncClient(timeout=SEARCH_SOURCE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(
                "https://torrents-csv.com/service/search",
                params={
                    "q": _media_provider_query(query),
                    "size": min(limit, 50),
                    "type": "torrent",
                },
                headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
            )
            response.raise_for_status()
            payload = response.json()

        rows = payload if isinstance(payload, list) else (
            payload.get("torrents", []) if isinstance(payload, dict) else []
        )
        results: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            h = str(item.get("infohash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            title = str(item.get("name") or "Untitled").strip()
            if not title:
                continue
            results.append({
                "guid": f"torrents-csv-{h}",
                "title": title,
                "size": int(float(item.get("size_bytes") or 0)),
                "seeders": int(item.get("seeders") or 0),
                "leechers": int(item.get("leechers") or 0),
                "indexer": "torrents-csv",
                "protocol": "torrent",
                "publishDate": (
                    datetime.fromtimestamp(
                        int(item.get("created_unix") or 0), tz=timezone.utc
                    ).isoformat()
                    if item.get("created_unix") else ""
                ),
                "infoHash": h,
                "magnetUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "downloadUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "infoUrl": "",
                "sourceUrl": "",
                "descriptorUrl": "",
            })
        return {"source": "torrents-csv", "elapsedMs": round((time.monotonic() - started) * 1000), "results": results}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Torrents-CSV search failed for '%s': %s", query, exc)
        return {"source": "torrents-csv", "elapsedMs": round((time.monotonic() - started) * 1000), "results": [], "error": str(exc)}


async def search_apibay(query: str, limit: int) -> dict[str, Any]:
    if not _MANUAL_TORRENT_SEARCH.get():
        _record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return {"source": "apibay", "elapsedMs": 0, "results": []}
    started = time.monotonic()
    try:
        async with _InstrumentedAsyncClient(timeout=SEARCH_SOURCE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(
                "https://apibay.org/q.php",
                params={"q": _media_provider_query(query), "cat": "0"},
                headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
            )
            response.raise_for_status()
            payload = response.json()

        rows = payload if isinstance(payload, list) else []
        results: list[dict[str, Any]] = []
        for item in rows[:limit]:
            if not isinstance(item, dict):
                continue
            h = str(item.get("info_hash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            title = str(item.get("name") or "Untitled").strip()
            if not title:
                continue
            results.append({
                "guid": f"apibay-{h}",
                "title": title,
                "size": int(float(item.get("size") or 0)),
                "seeders": int(item.get("seeders") or 0),
                "leechers": int(item.get("leechers") or 0),
                "indexer": "apibay",
                "protocol": "torrent",
                "publishDate": "",
                "infoHash": h,
                "magnetUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "downloadUrl": f"magnet:?xt=urn:btih:{h}&dn={quote(title, safe='')}",
                "infoUrl": "",
                "sourceUrl": "",
                "descriptorUrl": "",
            })
        return {"source": "apibay", "elapsedMs": round((time.monotonic() - started) * 1000), "results": results}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("APIBay search failed for '%s': %s", query, exc)
        return {"source": "apibay", "elapsedMs": round((time.monotonic() - started) * 1000), "results": [], "error": str(exc)}


def _search_media_kind(query: str) -> str:
    """Classify only enough to select strict media sources; never opens generic categories."""
    q = query.strip()
    if re.search(r"\b(?:S\d{1,2}(?:E\d{1,3})?|season\s*\d+|episode\s*\d+)\b", q, re.I):
        return "tv"
    return "both"


def _expand_compound_search_title(value: str) -> str:
    title = str(value or "")
    for compact, canonical in SEARCH_COMPOUND_ALIASES.items():
        title = re.sub(
            rf"(?<![a-z]){re.escape(compact)}(?![a-z])",
            canonical,
            title,
            flags=re.I,
        )
    return title


def _media_provider_query(value: str) -> str:
    """Return the title portion used for provider searches, excluding qualifiers."""
    title, _season, _episode = _media_search_parts(value)
    _year, languages = _search_query_constraints(value)
    for language in sorted(languages, key=len, reverse=True):
        title = re.sub(
            rf"(?<![a-z]){re.escape(language)}(?![a-z])",
            " ",
            title,
            flags=re.I,
        )
    title = _expand_compound_search_title(title)
    title = re.sub(r"\bspider[- ]?man\b", "spider man", title, flags=re.I)
    title = re.sub(r"\bant[- ]?man\b", "ant man", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip()


def _search_query_constraints(value: str) -> tuple[int | None, list[str]]:
    """Extract constraints that should affect matching but not title search."""
    years = re.findall(r"\b((?:19|20)\d{2})\b", value)
    year = int(years[-1]) if years else None

    language_aliases = (
        "hindi", "tamil", "telugu", "malayalam", "kannada",
        "bengali", "marathi", "punjabi", "gujarati", "urdu",
        "dual audio", "multi audio", "dubbed", "dub",
    )
    lower = value.lower()
    languages = [term for term in language_aliases if re.search(
        rf"(?<![a-z]){re.escape(term)}(?![a-z])", lower
    )]
    return year, languages


def _constraint_matches_title(title: str, query: str) -> bool:
    """Match year/language qualifiers without making normal title search brittle."""
    year, languages = _search_query_constraints(query)
    normalized = _normalize_title(title)
    lower = title.lower()

    if year is not None:
        title_years = {int(value) for value in re.findall(r"\b((?:19|20)\d{2})\b", title)}
        if year not in title_years:
            return False

    language_aliases = {
        "hindi": (r"(?<![a-z])hindi(?![a-z])", r"(?<![a-z])hin(?![a-z])"),
        "tamil": (r"(?<![a-z])tamil(?![a-z])", r"(?<![a-z])tam(?![a-z])"),
        "telugu": (r"(?<![a-z])telugu(?![a-z])", r"(?<![a-z])tel(?![a-z])"),
        "malayalam": (r"(?<![a-z])malayalam(?![a-z])", r"(?<![a-z])mal(?![a-z])"),
        "kannada": (r"(?<![a-z])kannada(?![a-z])", r"(?<![a-z])kan(?![a-z])"),
        "bengali": (r"(?<![a-z])bengali(?![a-z])", r"(?<![a-z])ben(?![a-z])"),
        "marathi": (r"(?<![a-z])marathi(?![a-z])", r"(?<![a-z])mar(?![a-z])"),
        "punjabi": (r"(?<![a-z])punjabi(?![a-z])", r"(?<![a-z])pun(?![a-z])"),
        "gujarati": (r"(?<![a-z])gujarati(?![a-z])", r"(?<![a-z])guj(?![a-z])"),
        "urdu": (r"(?<![a-z])urdu(?![a-z])", r"(?<![a-z])urd(?![a-z])"),
    }

    for language in languages:
        patterns = language_aliases.get(language)
        if patterns:
            if not any(re.search(pattern, lower, re.I) for pattern in patterns):
                return False
        elif language not in lower and language.replace(" ", "") not in normalized.replace(" ", ""):
            return False

    return True


def _search_quality_filter(item: dict[str, Any], query: str) -> bool:
    """Hard media/size gate plus title and qualifier relevance."""
    title = str(item.get("title") or "").strip()
    if not title:
        return False

    normalized = _normalize_title(title)
    normalized_tokens = set(normalized.split())
    _query_title, season, episode = _media_search_parts(query)
    # Language/codec/year qualifiers are validated separately below. The title
    # matcher should use only the actual media title, otherwise a release using
    # "Hin"/"Dub" instead of the exact query word "Hindi" gets discarded even
    # though the provider found it correctly.
    provider_title = _media_provider_query(query)
    target_tokens = _search_tokens(provider_title)

    # Match whole normalized tokens, not arbitrary substrings. This keeps
    # "thor" from matching "Thoroughbreds" while allowing punctuation variants
    # such as "Spider-Man" / "Spider Man" after title normalization.
    if target_tokens and not all(token in normalized_tokens for token in target_tokens):
        return False

    if not _constraint_matches_title(title, query):
        return False

    if season is not None and not _season_episode_match(title, season, episode):
        return False

    category = str(item.get("category") or "").lower()
    blocked = (
        "game", "software", "application", "music", "audio",
        "book", "ebook", "porn", "xxx", "adult", "anime",
    )
    if any(word in category for word in blocked):
        return False

    size = int(item.get("size") or 0)
    return MIN_SEARCH_RESULT_SIZE_BYTES <= size <= MAX_SEARCH_RESULT_SIZE_BYTES


def _title_relevance(title: str, query: str) -> tuple[int, int]:
    """Prefer titles that begin with the searched title, ignoring leading articles."""
    normalized = _normalize_title(title)
    target = _normalize_title(_media_search_parts(query)[0])
    if not target:
        return (0, 0)

    # Indexers often add "The" before a movie title. Do not demote an otherwise
    # direct match just because the article is present.
    candidate = re.sub(r"^(?:the|a|an)\s+", "", normalized)
    if candidate == target:
        return (4, len(target))
    if candidate.startswith(target + " "):
        return (3, len(target))

    candidate_tokens = candidate.split()
    target_tokens = target.split()
    if not target_tokens:
        return (0, 0)

    # Exact consecutive title tokens anywhere in the name are better than
    # tokens that only happen to appear in a longer related title (e.g. LEGO).
    for index in range(max(0, len(candidate_tokens) - len(target_tokens) + 1)):
        if candidate_tokens[index:index + len(target_tokens)] == target_tokens:
            return (2, len(target))
    if all(token in candidate_tokens for token in target_tokens):
        return (1, len(target))
    return (0, 0)


def _release_quality_score(title: str) -> int:
    """Estimate release quality from common filename tags; not a safety/trust score."""
    lower = unescape(str(title or "")).lower()
    score = 0

    # Resolution is a strong, broadly available signal.
    if re.search(r"\b(?:2160p|4k|uhd)\b", lower):
        score += 8
    elif re.search(r"\b1080p\b", lower):
        score += 6
    elif re.search(r"\b720p\b", lower):
        score += 4
    elif re.search(r"\b480p\b", lower):
        score += 2

    # Prefer modern digital/Blu-ray sources over older disc rips.
    if re.search(r"\b(?:blu[ .-]?ray|bdrip|brrip|remux)\b", lower):
        score += 4
    elif re.search(r"\b(?:web[ .-]?dl|webdl|webrip)\b", lower):
        score += 3
    elif re.search(r"\bhdtv\b", lower):
        score += 2
    elif re.search(r"\b(?:dvdrip|dvd)\b", lower):
        score += 1

    if re.search(r"\b(?:x265|h265|hevc|av1)\b", lower):
        score += 1
    if re.search(r"\b(?:x264|h264)\b", lower):
        score += 1
    if re.search(r"\b(?:hdr10?|dolby[ .]?vision|10bit)\b", lower):
        score += 1

    # Strongly demote low-quality theatrical captures and screeners without
    # removing them, so users can still find smaller/older releases if needed.
    if re.search(r"\b(?:hdcam|camrip|cam|telesync|telecine|ts|dvdscr|screener)\b", lower):
        score -= 8
    elif re.search(r"\br5\b", lower):
        score -= 2

    return score


def _media_provider_queries(value: str) -> list[str]:
    """Build a tiny title-variant set without exploding provider traffic.

    The first query is canonicalized (e.g. Spider-Man -> Spider Man). A second
    spelling is used only when the fast path returns too few usable results.
    For long titles, a third short anchor can recover indexers that struggle
    with punctuation while local filtering still requires the full user query.
    """
    title, _season, _episode = _media_search_parts(value)
    _year, languages = _search_query_constraints(value)

    raw_title = title
    for language in sorted(languages, key=len, reverse=True):
        raw_title = re.sub(
            rf"(?<![a-z]){re.escape(language)}(?![a-z])",
            " ",
            raw_title,
            flags=re.I,
        )
    raw_title = re.sub(r"\s+", " ", raw_title).strip()

    canonical = _media_provider_query(value)
    language_terms = [
        language for language in languages
        if language in {
            "hindi", "tamil", "telugu", "malayalam", "kannada",
            "bengali", "marathi", "punjabi", "gujarati", "urdu",
        }
    ]
    language_suffix = " ".join(language_terms)
    canonical_with_language = re.sub(
        r"\s+",
        " ",
        f"{canonical} {language_suffix}".strip(),
    )

    variants: list[str] = []
    seen: set[str] = set()
    for candidate in (
        canonical_with_language,
        canonical,
        raw_title,
        f"{raw_title} {language_suffix}".strip(),
    ):
        candidate = re.sub(r"\s+", " ", candidate or "").strip()
        lowered = candidate.lower()
        if candidate and lowered not in seen:
            seen.add(lowered)
            variants.append(candidate)

    tokens = _search_tokens(raw_title)
    if len(tokens) >= 4:
        anchor = " ".join(tokens[:3])
        if anchor and anchor.lower() not in {x.lower() for x in variants}:
            variants.append(anchor)

    return variants[:3]


def _search_cache_key(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip()).lower()


def _trim_search_cache() -> None:
    while len(_search_cache) > max(10, SEARCH_CACHE_MAX_ENTRIES):
        oldest_key = min(
            _search_cache.items(),
            key=lambda pair: pair[1][0],
        )[0]
        _search_cache.pop(oldest_key, None)


async def _search_1337x_uncached(
    query: str,
    allow_series_fallback: bool = True,
) -> list[dict[str, Any]]:
    if not _MANUAL_TORRENT_SEARCH.get():
        _record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Perform one bounded live media search.

    This is intentionally conservative for Render Free: only the primary
    1337x/Knaben pair runs on the first pass. More expensive alternate queries
    and specialist providers are activated only when fewer than 8 usable
    results survive the hard media and configured size gates.
    """
    limit = 50
    kind = _search_media_kind(query)
    variants = _media_provider_queries(query)
    primary_provider_query = variants[0] if variants else _media_provider_query(query)

    async def run_1337x(provider_query: str):
        try:
            return await asyncio.wait_for(
                search_1337x_direct(
                    query,
                    limit=20,
                    pages=2,
                    category="TV" if kind == "tv" else None,
                    provider_query=provider_query,
                ),
                timeout=max(8.0, SEARCH_TOTAL_TIMEOUT_SECONDS + 4.0),
            )
        except Exception as exc:
            logger.warning("1337x search failed for '%s' using '%s': %s: %r", query, provider_query, type(exc).__name__, str(exc))
            return []

    async def run_lime(provider_query: str):
        try:
            lime_results = await asyncio.wait_for(
                search_limetorrents(
                    query,
                    limit=20,
                    pages=1,
                ),
                timeout=10.0,
            )
            logger.warning(
                "LimeTorrents primary source '%s': %d results",
                query,
                len(lime_results),
            )
            return lime_results
        except Exception as exc:
            logger.warning(
                "LimeTorrents search failed for '%s' using '%s': %s: %s",
                query,
                provider_query,
                type(exc).__name__,
                str(exc) or "<no message>",
            )
            return []

    async def run_knaben(provider_query: str):
        try:
            return await asyncio.wait_for(
                search_knaben(query, 50, provider_query=provider_query),
                timeout=8.0,
            )
        except Exception as exc:
            logger.info("Knaben search failed for '%s' using '%s': %s", query, provider_query, exc)
            return []

    yts_task = (
        asyncio.create_task(
            asyncio.wait_for(
                search_yts_movies(query, min(limit, 20)),
                timeout=SEARCH_SOURCE_TIMEOUT_SECONDS + 0.75,
            )
        )
        if kind == "both"
        else None
    )

    primary_1337x, primary_lime, primary_knaben = await asyncio.gather(
        run_1337x(primary_provider_query),
        run_lime(primary_provider_query),
        run_knaben(primary_provider_query),
        return_exceptions=False,
    )

    yts_primary: list[dict[str, Any]] = []
    if yts_task is not None:
        try:
            yts_primary = await yts_task
        except Exception as exc:
            logger.warning("YTS primary search failed for '%s': %s: %r", query, type(exc).__name__, str(exc))

    logger.warning(
        "Raw search source counts for '%s': 1337x=%d, LimeTorrents=%d, Knaben=%d, YTS=%d",
        query,
        len(primary_1337x),
        len(primary_lime),
        len(primary_knaben),
        len(yts_primary),
    )

    results: list[dict[str, Any]] = []

    def add_filtered(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if _search_quality_filter(item, query):
                results.append(item)

    add_filtered(primary_1337x)
    add_filtered(primary_lime)
    add_filtered(primary_knaben)
    add_filtered(yts_primary)

    yts_metadata: dict[str, dict[str, Any]] = {}
    for item in yts_primary:
        media_title = _normalize_title(str(item.get("mediaTitle") or ""))
        if media_title:
            yts_metadata.setdefault(media_title, item)

    def enrich_movie_metadata(items: list[dict[str, Any]]) -> None:
        for item in items:
            provider_title = str(item.get("mediaTitle") or item.get("title") or "")
            media_title = _normalize_title(_media_provider_query(provider_title))
            if not media_title:
                continue
            metadata = yts_metadata.get(media_title)
            if not metadata:
                compact = media_title.replace(" ", "")
                metadata = next(
                    (
                        candidate for key, candidate in yts_metadata.items()
                        if compact and key.replace(" ", "") == compact
                    ),
                    None,
                )
            if metadata:
                for field in ("mediaTitle", "year", "rating", "genres", "posterUrl"):
                    value = metadata.get(field)
                    if value not in (None, "", []):
                        item[field] = value

    enrich_movie_metadata(results)

    # When the primary title spelling cannot get enough usable hits, retry
    # with the alternate spelling and a short anchor in parallel. This is the
    # cheap way to handle Spider-Man/Spiderman-style provider differences
    # without always doubling Render traffic.
    if allow_series_fallback and len(results) < min(8, limit):
        secondary_queries = variants[1:3]

        variant_tasks = [
            asyncio.create_task(run_1337x(provider_query))
            for provider_query in secondary_queries
        ]
        variant_tasks.extend(
            asyncio.create_task(run_knaben(provider_query))
            for provider_query in secondary_queries
        )

        fallback_tasks: list[asyncio.Task] = [
            asyncio.create_task(
                search_torrents_csv(
                    query,
                    min(limit, 50),
                )
            ),
            asyncio.create_task(
                search_apibay(
                    query,
                    min(limit, 50),
                )
            ),
        ]

        if kind in {"tv", "both"}:
            fallback_tasks.append(
                asyncio.create_task(
                    asyncio.wait_for(
                        search_tv_eztv(query, min(limit, 20)),
                        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS + 0.5,
                    )
                )
            )
        if kind == "both":
            fallback_tasks.append(
                asyncio.create_task(
                    asyncio.wait_for(
                        search_yts_movies(query, min(limit, 20)),
                        timeout=SEARCH_SOURCE_TIMEOUT_SECONDS + 0.5,
                    )
                )
            )

        values = await asyncio.gather(
            *(variant_tasks + fallback_tasks),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, list):
                add_filtered(value)
            elif isinstance(value, dict):
                add_filtered(value.get("results", []))

        enrich_movie_metadata(results)

    merged: dict[str, dict[str, Any]] = {}
    for item in results:
        digest = str(item.get("infoHash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", digest):
            digest = info_hash(str(item.get("magnetUrl") or ""))

        if re.fullmatch(r"[0-9a-f]{40}", digest, re.I):
            key = "hash:" + digest.lower()
        else:
            # Listing-page results now resolve magnets lazily, so use a
            # normalized title + size to collapse the same release returned
            # by more than one provider.
            normalized_title = _normalize_title(str(item.get("title") or ""))
            if not normalized_title:
                continue
            size = int(item.get("size") or 0)
            key = f"title-size:{normalized_title}|{size}"

        previous = merged.get(key)
        if previous is None or (
            int(item.get("seeders") or 0),
            int(item.get("leechers") or 0),
            1 if item.get("magnetUrl") else 0,
        ) > (
            int(previous.get("seeders") or 0),
            int(previous.get("leechers") or 0),
            1 if previous.get("magnetUrl") else 0,
        ):
            merged[key] = item

    results = list(merged.values())
    enrich_movie_metadata(results)

    source_counts: dict[str, int] = {}
    for item in results:
        source_name = str(item.get("indexer") or "unknown").strip() or "unknown"
        source_counts[source_name] = source_counts.get(source_name, 0) + 1
    logger.warning(
        "Search result sources for '%s': %s",
        query,
        ", ".join(f"{name}={count}" for name, count in sorted(source_counts.items())),
    )

    # Poster artwork is resolved lazily by /api/poster so search latency is
    # unaffected. Every normal movie result gets a cleaned poster URL,
    # regardless of whether it came from YTS, Knaben, 1337x, or another source.
    if kind == "both":
        for item in results:
            if not str(item.get("posterUrl") or "").strip():
                poster_url = _poster_url_for_release(str(item.get("title") or ""))
                if poster_url:
                    item["posterUrl"] = poster_url

    # Do not let questionable seed counts from one indexer dominate the list.
    # Rank direct title matches first, then recognizable release quality, and
    # use swarm size/date as secondary signals within comparable results.
    results.sort(
        key=lambda item: (
            _title_relevance(str(item.get("title") or ""), query)[0],
            _release_quality_score(str(item.get("title") or "")),
            min(int(item.get("seeders") or 0), 5000),
            int(item.get("leechers") or 0),
            str(item.get("publishDate") or ""),
        ),
        reverse=True,
    )

    return results[:limit]


async def search_1337x(
    query: str,
    limit: int = 50,
    allow_series_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Fast media-only search with stale cache and in-flight request sharing."""
    query = re.sub(r"\s+", " ", query.strip())
    if not query:
        return []

    limit = min(max(int(limit or 50), 1), 50)
    cache_key = _search_cache_key(query)
    now = time.monotonic()

    cached = _search_cache.get(cache_key)
    if cached and len(cached[1]) < SEARCH_CACHE_MIN_RESULTS:
        _search_cache.pop(cache_key, None)
        cached = None

    if cached:
        cached_at, cached_results = cached
        age = max(0.0, now - cached_at)
        ttl = SEARCH_CACHE_SECONDS

        if age < ttl:
            _record_cache_event("torrent_search", "hit")
            return cached_results[:limit]

        if age < SEARCH_CACHE_STALE_SECONDS:
            _record_cache_event("torrent_search", "stale_hit")
            # Serve stale results immediately and refresh only once per query.
            existing = _search_inflight.get(cache_key)
            if existing is None or existing.done():
                refresh = asyncio.create_task(
                    _search_1337x_uncached(
                        query,
                        allow_series_fallback=allow_series_fallback,
                    )
                )
                _search_inflight[cache_key] = refresh

                def _finish_refresh(task: asyncio.Task, key: str = cache_key):
                    _search_inflight.pop(key, None)
                    if task.cancelled():
                        return
                    try:
                        refreshed = task.result()
                    except Exception as exc:
                        logger.info("Background search refresh failed for '%s': %s", query, exc)
                        return
                    if len(refreshed) >= SEARCH_CACHE_MIN_RESULTS:
                        _search_cache[key] = (time.monotonic(), refreshed)
                        _trim_search_cache()

                refresh.add_done_callback(_finish_refresh)
            return cached_results[:limit]

    # Share the same live request among concurrent users. This is particularly
    # useful on Render Free where multiple browser tabs/devices can otherwise
    # fan out identical provider requests.
    _record_cache_event("torrent_search", "miss")
    task = _search_inflight.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(
            _search_1337x_uncached(
                query,
                allow_series_fallback=allow_series_fallback,
            )
        )
        _search_inflight[cache_key] = task

    try:
        results = await task
    except asyncio.CancelledError:
        # The provider task may still be useful to other callers; do not leave
        # a dead task in the inflight map.
        if _search_inflight.get(cache_key) is task:
            _search_inflight.pop(cache_key, None)
        raise
    except Exception as exc:
        if _search_inflight.get(cache_key) is task:
            _search_inflight.pop(cache_key, None)
        logger.warning("Media search failed for '%s': %s", query, exc)
        return cached[1][:limit] if cached else []

    if _search_inflight.get(cache_key) is task:
        _search_inflight.pop(cache_key, None)

    if len(results) >= SEARCH_CACHE_MIN_RESULTS:
        _search_cache[cache_key] = (time.monotonic(), results)
        _trim_search_cache()
    return results[:limit]

def parse_size(value: str) -> int:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)", value or "", re.I)
    if not m:
        return 0
    n = float(m.group(1))
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(n * units[m.group(2).upper()])

_POSTER_CACHE_SECONDS = 6 * 60 * 60
_POSTER_NEGATIVE_CACHE_SECONDS = 2 * 60
_CINEMETA_BASE_URL = "https://v3-cinemeta.strem.io"
_poster_cache: dict[str, tuple[float, str | None]] = {}
_poster_inflight: dict[str, asyncio.Task[str | None]] = {}


def _poster_normalize_title(value: str) -> str:
    value = str(value or "").lower().replace("’", "'")
    # Torrent sources frequently vary only in punctuation/spelling. Treat
    # possessives as the same words and normalize common title variants.
    value = value.replace("&", " and ")
    value = value.replace("'", "")
    tokens = re.findall(r"[a-z0-9]+", value)
    token_aliases = {
        "v": "vs",
        "vs": "vs",
        "carribean": "caribbean",
    }
    tokens = [token_aliases.get(token, token) for token in tokens]
    normalized = " ".join(tokens).strip()
    return re.sub(r"^(?:the|a|an)\s+", "", normalized)




def _poster_title_matches(wanted: str, candidate: str) -> bool:
    """Accept only exact normalized titles to avoid unrelated poster artwork."""
    return bool(wanted and candidate and wanted == candidate)

def _poster_prepare_lookup_title(title: str, year: str = "") -> str:
    """Strip common release artifacts before querying poster providers."""
    value = str(title or "").strip()
    clean_year = str(year or "").strip()

    # The frontend can send both "(YEAR)" inside title and year=YEAR.
    if clean_year:
        value = re.sub(
            rf"\s*[\(\[\-:]?\s*{re.escape(clean_year)}\s*[\)\]]?\s*$",
            "",
            value,
            flags=re.I,
        )

    # Frontend poster candidates can end with a lone opening delimiter after
    # removing release information (e.g. "Batman Returns (").
    value = value.rstrip(" ([{").strip(" -._")

    # Strip season/episode information and everything after it. This lets a
    # series release such as "Batman The Animated Series S03E05 Time Out of Joint"
    # resolve to the series poster instead of treating the episode title as a movie.
    value = re.sub(
        r"\s+S\d{1,2}(?:E\d{1,3})?\b.*$",
        "",
        value,
        flags=re.I,
    ).strip(" -._([")

    return value.strip()

def _poster_title_aliases(title: str, year: str = "") -> list[str]:
    """Return provider-friendly title aliases for common torrent shorthand."""
    clean_title = str(title or "").strip()
    clean_year = str(year or "").strip()
    normalized = _poster_normalize_title(clean_title)
    aliases = [clean_title] if clean_title else []

    # Torrent sources often shorten sequel titles to a franchise number.
    # Keep this deliberately narrow so numeric titles do not create broad false matches.
    pirates_sequels = {
        "1": "Pirates of the Caribbean: The Curse of the Black Pearl",
        "2": "Pirates of the Caribbean: Dead Man's Chest",
        "3": "Pirates of the Caribbean: At World's End",
        "4": "Pirates of the Caribbean: On Stranger Tides",
        "5": "Pirates of the Caribbean: Dead Men Tell No Tales",
    }
    match = re.fullmatch(r"pirates of the caribbean ([1-5])", normalized)
    if match:
        alias = pirates_sequels[match.group(1)]
        if alias not in aliases:
            aliases.append(alias)

    # Common LimeTorrents naming variants differ from catalog titles. These
    # explicit aliases preserve exact matching and do not reopen broad substring
    # matches that previously returned unrelated artwork.
    known_aliases = {
        "avengers 2": ("Avengers: Age of Ultron", "2015"),
        "marvels the avengers": ("The Avengers", "2012"),
        "marvel s the avengers": ("The Avengers", "2012"),
        "superman the movie": ("Superman", "1978"),
        "superman movie": ("Superman", "1978"),
    }
    known = known_aliases.get(normalized)
    if known:
        alias, expected_year = known
        if not clean_year or clean_year == expected_year:
            if alias not in aliases:
                aliases.append(alias)

    # Cinemeta/IMDb listings may use the film's full subtitle instead of its
    # marketed short title. These remain exact normalized-title matches.
    if normalized == "birds of prey" and (not clean_year or clean_year == "2020"):
        for alias in (
            "Birds of Prey: And the Fantabulous Emancipation of One Harley Quinn",
            "Birds of Prey (and the Fantabulous Emancipation of One Harley Quinn)",
        ):
            if alias not in aliases:
                aliases.append(alias)

    return aliases

def _poster_title_parts(raw_title: str) -> tuple[str, str]:
    """Extract the movie title/year while removing common release-name debris."""
    value = str(raw_title or "").replace(".", " ").replace("_", " ")
    # Release helpers often append the real release year after the title
    # (e.g. "Wonder Woman 1984 2020"). Prefer the last year token so a year
    # that is part of the title is preserved.
    year_matches = list(re.finditer(r"\b((?:19|20)\d{2})\b", value))
    year_match = year_matches[-1] if year_matches else None
    year = year_match.group(1) if year_match else ""

    if year_match:
        value = value[:year_match.start()]

    # Clean bracketed tags before splitting on codec/quality markers. Doing
    # this in the opposite order leaves a dangling "[" in titles such as
    # "The Avengers 2 [1080p]", which then prevents an exact poster match.
    value = re.sub(r"\[[^\]]*(?:\]|$)", " ", value)
    value = re.sub(r"\([^)]*(?:\)|$)", " ", value)

    if not year_match:
        value = re.split(
            r"\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|"
            r"web[- ]?dl|web[- ]?rip|webrip|bluray|brrip|hdrip|"
            r"dvdrip|dvd|cam|hdcam|telesync|telecine|scr|r5|r6|"
            r"x264|x265|h264|h265|hevc|xvid|divx|dts|aac|ac3|ddp|"
            r"eng|english|nlsub|proper|repack|remux|hdr10|hdr)\b",
            value,
            maxsplit=1,
            flags=re.I,
        )[0]

    # A few common LimeTorrents uploader tags don't have a preceding codec
    # marker, so remove them if they remain as the final token/group.
    value = re.sub(
        r"\s+(?:BlueLady(?:RG)?|Jaybob|HAGGiS|Voltage|DTRG|DOCUMENT|ViSiON|"
        r"SONiDO|SAiMORNY|LTT)$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip(" -._[](){}")
    return value, year


def _poster_url_for_release(raw_title: str) -> str:
    title, year = _poster_title_parts(raw_title)
    if not title:
        return ""
    return f"/api/poster?{urlencode({'title': title, 'year': year})}"


def _canonical_catalogue_poster_url(item: dict[str, Any]) -> str:
    """Build a poster URL from canonical catalogue metadata, not a torrent filename."""
    # Keep a known-good TMDB CDN URL when the catalogue already has one. The old
    # rewrite replaced these working image URLs with a title-search endpoint that
    # can return 404, leaving the whole poster area blank.
    existing = str(item.get("posterUrl") or item.get("poster_url") or "").strip()
    if existing:
        try:
            parts = urlsplit(existing)
            if (
                parts.scheme in {"http", "https"}
                and (parts.hostname or "").casefold() == "image.tmdb.org"
                and "/t/p/" in parts.path
            ):
                return existing
        except Exception:
            pass
    poster_path = str(item.get("poster_path") or item.get("posterPath") or "").strip()
    if poster_path.startswith("/"):
        return "https://image.tmdb.org/t/p/w500" + poster_path

    title = str(
        item.get("marvelCatalogueTitle")
        or item.get("catalogueTitle")
        or item.get("mediaTitle")
        or item.get("title")
        or ""
    ).strip()
    if not title:
        return ""
    year = str(item.get("year") or "").strip()

    # These cached release names previously resolved to unrelated poster art.
    # Pin them to canonical TMDB poster files for the exact title/year.
    poster_key_title = re.sub(r"\[[^\]]*(?:\]|$)", " ", title)
    poster_key_title = re.sub(r"\([^)]*(?:\)|$)", " ", poster_key_title)
    if year:
        poster_key_title = re.sub(rf"\s*{re.escape(year)}\s*$", "", poster_key_title)
    poster_key_title = re.split(
        r"\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|3d|uhd|hdrip|webrip|web[- ]?dl|bluray|blu[- ]?ray|brrip|bdrip|hdtv|x264|x265|h264|h265|hevc)\b",
        poster_key_title,
        maxsplit=1,
        flags=re.I,
    )[0]
    poster_key = re.sub(r"[^a-z0-9]+", "", poster_key_title.casefold())
    if year == "2014" and poster_key in {"theamazingspiderman2", "amazingspiderman2"}:
        return "https://image.tmdb.org/t/p/w500/bU7nTmvmy0h3VUP01v1T2imgH6N.jpg"
    if year == "2020" and poster_key == "wonderwoman1984":
        return "https://image.tmdb.org/t/p/w500/8UlWHLMpgZm9bx6QYh0NFoq67TZ.jpg"

    params = {"title": title}
    if year:
        params["year"] = year
    return "/api/poster?" + urlencode(params)


_EXCLUDED_FRANCHISE_TITLE_KEYS = {
    "avengersconfidentialblackwidowpunisher",
    "ironmanrisetechnovore",
    "nextavengersheroesoftomorrow",
}


def _catalogue_row_has_3d_quality(item: dict[str, Any]) -> bool:
    marker = re.compile(r"(?:^|[^a-z0-9])3[ ._-]*d(?:$|[^a-z0-9])", re.I)
    for field in ("quality", "title", "releaseName", "qualityLabel", "resolution"):
        if marker.search(str(item.get(field) or "")):
            return True
    return False


def _catalogue_row_is_excluded_franchise_title(item: dict[str, Any]) -> bool:
    for field in ("catalogueTitle", "marvelCatalogueTitle", "mediaTitle", "title"):
        raw = str(item.get(field) or "").strip()
        if not raw:
            continue
        normalized = re.sub(r"[^a-z0-9]+", "", raw.casefold())
        if any(normalized.startswith(excluded) for excluded in _EXCLUDED_FRANCHISE_TITLE_KEYS):
            return True
    return False


def _cached_catalogue_year(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        year = int(item.get("year") or 0)
        if 1880 <= year <= 2100:
            return year
    except (TypeError, ValueError):
        pass
    for field in ("catalogueTitle", "marvelCatalogueTitle", "mediaTitle", "title"):
        matches = re.findall(r"\b((?:19|20)\d{2})\b", str(item.get(field) or ""))
        if matches:
            try:
                return int(matches[-1])
            except (TypeError, ValueError):
                continue
    return 0


def _sort_cached_catalogue_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[int, str, int]:
        title = str(item.get("catalogueTitle") or item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or "")
        normalized = re.sub(r"[^a-z0-9]+", "", title.casefold())
        try:
            seeders = int(float(item.get("seeders") or 0))
        except (TypeError, ValueError):
            seeders = 0
        return (-_cached_catalogue_year(item), normalized, -seeders)
    return sorted(results, key=key)


async def _cinemeta_poster_lookup(title: str, year: str = "") -> str | None:
    """Resolve title/year through Cinemeta's catalog, then use its stable ID metadata."""
    clean_title = str(title or "").strip()
    clean_year = str(year or "").strip()
    if not clean_title:
        return None

    title_aliases = _poster_title_aliases(clean_title, clean_year)
    wanted_titles = [_poster_normalize_title(value) for value in title_aliases]
    queries = []
    for alias in title_aliases:
        for query_value in (
            f"{alias} {clean_year}".strip(),
            alias,
        ):
            if query_value and query_value not in queries:
                queries.append(query_value)

    try:
        async with _InstrumentedAsyncClient(timeout=4, follow_redirects=True) as client:
            for media_type in ("movie", "series"):
                for query_value in queries:
                    encoded = quote(query_value, safe="")
                    response = await client.get(
                        f"{_CINEMETA_BASE_URL}/catalog/{media_type}/top/search={encoded}.json",
                        headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
                    )
                    if response.status_code != 200:
                        continue

                    data = response.json()
                    metas = data.get("metas") if isinstance(data, dict) else None
                    if not isinstance(metas, list):
                        continue

                    candidates: list[tuple[int, str, str]] = []
                    for row in metas:
                        if not isinstance(row, dict):
                            continue
                        name = str(row.get("name") or "").strip()
                        candidate = _poster_normalize_title(name)
                        poster = str(row.get("poster") or "").strip()
                        imdb_id = str(row.get("id") or "").strip()
                        if not name or not any(_poster_title_matches(wanted, candidate) for wanted in wanted_titles):
                            continue

                        candidate_year = str(
                            row.get("releaseInfo")
                            or row.get("year")
                            or ""
                        ).strip()

                        # Once the torrent/search result gives us a year, do not
                        # allow a different-year title just because its name is a
                        # fuzzy/contained match (e.g. "Justice League" 2017 must
                        # never satisfy "Justice League: War" 2014).
                        if clean_year:
                            candidate_year_value = candidate_year[:4]
                            if not candidate_year_value or candidate_year_value != clean_year:
                                continue

                        score = 100
                        if clean_year and candidate_year:
                            if candidate_year == clean_year or candidate_year.startswith(clean_year):
                                score += 120
                            else:
                                score -= 80
                        if poster:
                            score += 30
                        if imdb_id.startswith("tt"):
                            score += 10
                        candidates.append((score, poster, imdb_id))

                    if not candidates:
                        continue

                    candidates.sort(key=lambda item: item[0], reverse=True)
                    _score, poster, imdb_id = candidates[0]
                    if poster:
                        return poster

                    if imdb_id.startswith("tt"):
                        meta_response = await client.get(
                            f"{_CINEMETA_BASE_URL}/meta/{media_type}/{quote(imdb_id, safe='')}.json",
                            headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
                        )
                        if meta_response.status_code == 200:
                            meta_data = meta_response.json()
                            meta = meta_data.get("meta") if isinstance(meta_data, dict) else None
                            poster = str(meta.get("poster") or "").strip() if isinstance(meta, dict) else ""
                            if poster:
                                return poster
    except Exception:
        pass

    return None


async def _tmdb_poster_lookup(title: str, year: str = "") -> str | None:
    """Resolve an exact-title poster from TMDB movies or TV series.

    Torrent releases often include episode numbers, while the poster belongs to
    the parent series. Search both media types and only accept normalized exact
    title matches; this avoids returning artwork for a loosely related title.
    """
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        return None

    clean_title = str(title or "").strip()
    clean_year = str(year or "").strip()
    if not clean_title:
        return None

    # Strip common season/episode suffixes so "Lanterns S01E08" resolves to
    # the series "Lanterns", not a movie with a similar name.
    search_title = re.sub(
        r"\s+(?:s\d{1,2}e\d{1,3}|season\s+\d{1,2}(?:\s+episode\s+\d{1,3})?|\d{1,2}x\d{1,3})\b.*$",
        "",
        clean_title,
        flags=re.IGNORECASE,
    ).strip() or clean_title
    aliases = _poster_title_aliases(search_title, clean_year)
    wanted_titles = {_poster_normalize_title(alias) for alias in aliases if alias}
    params_base: dict[str, Any] = {"include_adult": "false", "language": "en-US", "page": 1}

    async def search_media(media_type: str, alias: str) -> list[tuple[float, str]]:
        params = dict(params_base)
        params["query"] = alias
        if clean_year.isdigit() and len(clean_year) == 4:
            params["year" if media_type == "movie" else "first_air_date_year"] = clean_year
        try:
            data = await _tmdb_get_json(f"search/{media_type}", params)
        except Exception as exc:
            logger.debug("TMDB %s poster search failed for '%s': %s", media_type, alias, type(exc).__name__)
            return []

        candidates: list[tuple[float, str]] = []
        title_keys = ("title", "original_title") if media_type == "movie" else ("name", "original_name")
        date_key = "release_date" if media_type == "movie" else "first_air_date"
        for row in data.get("results") or []:
            if not isinstance(row, dict):
                continue
            name = next((str(row.get(key) or "").strip() for key in title_keys if row.get(key)), "")
            normalized = _poster_normalize_title(name)
            date_value = str(row.get(date_key) or "").strip()
            candidate_year = date_value[:4] if re.match(r"^\d{4}-\d{2}-\d{2}$", date_value) else ""
            poster_path = str(row.get("poster_path") or "").strip()
            if not name or not poster_path or normalized not in wanted_titles:
                continue
            if clean_year and candidate_year and candidate_year != clean_year:
                continue
            score = 100.0
            if normalized == _poster_normalize_title(search_title):
                score += 100
            if clean_year and candidate_year == clean_year:
                score += 100
            score += float(row.get("vote_average") or 0)
            score += min(float(row.get("vote_count") or 0), 10000) / 1000
            candidates.append((score, poster_path))
        return candidates

    try:
        for alias in aliases[:4]:
            # Both endpoints are independent and fast to query concurrently.
            movie_candidates, tv_candidates = await asyncio.gather(
                search_media("movie", alias),
                search_media("tv", alias),
            )
            candidates = movie_candidates + tv_candidates
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                return TMDB_IMAGE_BASE + candidates[0][1]
    except Exception as exc:
        logger.debug("TMDB poster lookup failed for '%s (%s)': %s", clean_title, clean_year, type(exc).__name__)
    return None

_BUNDLED_POSTERS: dict[str, str] | None = None


def _bundled_poster(clean_title: str, clean_year: str) -> str | None:
    """Poster URLs harvested from the bundled caches (no network)."""
    global _BUNDLED_POSTERS
    if _BUNDLED_POSTERS is None:
        loaded: dict[str, str] = {}
        try:
            for title, year, url in json.loads((_CACHE_SEED_DIR / "poster_map.json").read_text(encoding="utf-8")):
                loaded.setdefault(f"{_poster_normalize_title(title)}|{year}", url)
        except (OSError, ValueError, TypeError):
            pass
        _BUNDLED_POSTERS = loaded
    return _BUNDLED_POSTERS.get(f"{_poster_normalize_title(clean_title)}|{clean_year}")


async def _watchmode_poster_lookup(title: str, year: str = "") -> str | None:
    """Use Watchmode only when configured; accept only exact title/year matches."""
    if CACHE_ONLY_MODE or not WATCHMODE_API_KEYS:
        return None
    clean_title = str(title or "").strip()
    clean_year = str(year or "").strip()
    if not clean_title:
        return None

    for alias in _poster_title_aliases(clean_title, clean_year)[:3]:
        try:
            response = await _watchmode_get(
                "/search/",
                params={"search_field": "name", "search_value": alias, "types": "movie,tv"},
                timeout=5.0,
            )
            if response is None or response.status_code != 200:
                continue
            data = response.json()
            rows = data.get("title_results") if isinstance(data, dict) else []
            matches = []
            wanted = _poster_normalize_title(alias)
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or row.get("title") or "").strip()
                candidate = _poster_normalize_title(name)
                candidate_year = str(row.get("year") or "").strip()[:4]
                if not name or candidate != wanted:
                    continue
                if clean_year and candidate_year != clean_year:
                    continue
                if not row.get("id"):
                    continue
                matches.append(row)
            if not matches:
                continue
            details = await asyncio.gather(*[
                _watchmode_get(f"/title/{int(row['id'])}/details/",
                    timeout=5.0)
                for row in matches[:2]
            ])
            for detail_response in details:
                if detail_response is None or detail_response.status_code != 200:
                    continue
                detail = detail_response.json()
                if not isinstance(detail, dict):
                    continue
                poster = str(detail.get("posterLarge") or detail.get("posterMedium") or detail.get("poster") or "").strip()
                if poster.startswith("https://") or poster.startswith("http://"):
                    return poster
        except Exception as exc:
            logger.debug("Watchmode poster lookup failed for '%s (%s)': %s", clean_title, clean_year, type(exc).__name__)
    return None


async def _omdb_poster_lookup(title: str, year: str = "") -> str | None:
    """Use OMDb as a low-priority fallback with strict title/year validation."""
    if CACHE_ONLY_MODE:
        return None
    keys = _split_env_keys(os.getenv("OMDB_API_KEYS", ""), os.getenv("OMDB_API_KEY", ""))
    if not keys:
        return None
    clean_title = str(title or "").strip()
    clean_year = str(year or "").strip()
    if not clean_title:
        return None

    aliases = _poster_title_aliases(clean_title, clean_year)[:3]
    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=True) as client:
        for alias in aliases:
            for key in keys:
                try:
                    params = {"apikey": key, "t": alias, "type": "movie"}
                    if clean_year:
                        params["y"] = clean_year
                    response = await client.get("https://www.omdbapi.com/", params=params)
                    if response.status_code != 200:
                        continue
                    data = response.json()
                    if not isinstance(data, dict) or str(data.get("Response") or "").lower() != "true":
                        continue
                    candidate = _poster_normalize_title(str(data.get("Title") or ""))
                    candidate_year = str(data.get("Year") or "").strip()[:4]
                    if candidate != _poster_normalize_title(alias):
                        continue
                    if clean_year and candidate_year != clean_year:
                        continue
                    poster = str(data.get("Poster") or "").strip()
                    if poster.startswith(("https://", "http://")) and poster.casefold() != "n/a":
                        return poster
                except Exception as exc:
                    logger.debug("OMDb poster lookup failed for '%s (%s)': %s", clean_title, clean_year, type(exc).__name__)
    return None


async def _poster_lookup_uncached(clean_title: str, clean_year: str, cache_key: str) -> str | None:
    # Check the bundled poster index before any network lookup, even when live
    # metadata is enabled. It contains verified TMDB poster URLs and avoids
    # repeated title-search requests (or blank posters after upstream timeouts).
    bundled = _bundled_poster(clean_title, clean_year)
    if bundled:
        _poster_cache[cache_key] = (time.time(), bundled)
        return bundled
    now = time.time()
    # Prefer TMDB's year-filtered movie results to prevent a similarly named
    # title from supplying the wrong artwork. Other sources remain fallbacks.
    poster = None if CACHE_ONLY_MODE else await _tmdb_poster_lookup(clean_title, clean_year)
    if not poster and not CACHE_ONLY_MODE:
        poster = await _watchmode_poster_lookup(clean_title, clean_year)
    if not poster and not CACHE_ONLY_MODE:
        poster = await _omdb_poster_lookup(clean_title, clean_year)

    try:
        title_aliases = _poster_title_aliases(clean_title, clean_year)
        wanted_titles = [_poster_normalize_title(value) for value in title_aliases]
        wanted_year = clean_year
        candidates: list[tuple[int, str]] = []
        async with _InstrumentedAsyncClient(timeout=4, follow_redirects=True) as client:
            for alias in title_aliases:
                query = quote((alias + " " + clean_year).strip(), safe="")
                response = await client.get(
                    f"https://v3.sg.media-imdb.com/suggestion/titles/x/{query}.json?includeVideos=0",
                    headers={"Accept": "application/json"},
                )
                if response.status_code != 200:
                    continue
                data = response.json()
                for row in data.get("d") or []:
                    if not isinstance(row, dict):
                        continue
                    image = str((row.get("i") or {}).get("imageUrl") or "").strip()
                    candidate = _poster_normalize_title(str(row.get("l") or ""))
                    candidate_year = str(row.get("y") or "")
                    # Ignore episodes, shorts, games, and other unrelated title types.
                    # Search suggestions can contain similarly named fan clips with
                    # artwork that is not the poster for the requested movie/series.
                    media_kind = str(row.get("qid") or "").lower()
                    if media_kind not in {"movie", "tvseries", "tvminiseries", "tvmovie"}:
                        continue
                    if not image or not any(_poster_title_matches(wanted, candidate) for wanted in wanted_titles):
                        continue
                    if wanted_year:
                        if not candidate_year or candidate_year != wanted_year:
                            continue
                    score = 100
                    if wanted_year and candidate_year == wanted_year:
                        score += 100
                    if str(row.get("qid") or "") == "movie":
                        score += 10
                    score += max(0, 20 - int(row.get("rank") or 20))
                    candidates.append((score, image))
                if candidates and not poster:
                    candidates.sort(key=lambda pair: pair[0], reverse=True)
                    poster = candidates[0][1]
    except Exception:
        pass

    if not poster:
        try:
            title_aliases = _poster_title_aliases(clean_title, clean_year)
            wanted_titles = [_poster_normalize_title(value) for value in title_aliases]
            wanted_year = clean_year
            candidates: list[tuple[int, str]] = []
            async with _InstrumentedAsyncClient(timeout=4, follow_redirects=True) as client:
                for alias in title_aliases:
                    response = await client.get(
                        "https://itunes.apple.com/search",
                        params={"term": (alias + " " + clean_year).strip(), "limit": "25"},
                        headers={"Accept": "application/json"},
                    )
                    if response.status_code != 200:
                        continue
                    data = response.json()
                    for row in data.get("results") or []:
                        if str(row.get("kind") or "") != "feature-movie" or not row.get("artworkUrl100"):
                            continue
                        candidate = _poster_normalize_title(str(row.get("trackName") or row.get("collectionName") or ""))
                        candidate_year = str(row.get("releaseDate") or "")[:4]
                        if not any(_poster_title_matches(wanted, candidate) for wanted in wanted_titles):
                            continue
                        if wanted_year:
                            if not candidate_year or candidate_year != wanted_year:
                                continue
                        score = 100
                        if wanted_year and candidate_year == wanted_year:
                            score += 100
                        candidates.append((score, str(row["artworkUrl100"]).replace("100x100bb", "600x600bb")))
                if candidates:
                    candidates.sort(key=lambda pair: pair[0], reverse=True)
                    poster = candidates[0][1]
        except Exception:
            pass

    # Cinemeta is intentionally last: it is a metadata/identity fallback and
    # never sits on the torrent-search critical path.
    if not poster:
        poster = await _cinemeta_poster_lookup(clean_title, clean_year)

    _poster_cache[cache_key] = (now, poster)
    return poster


async def _poster_lookup(title: str, year: str = "") -> str | None:
    clean_year = str(year or "").strip()
    clean_title = _poster_prepare_lookup_title(title, clean_year)
    if not clean_title:
        return None

    cache_key = f"{_poster_normalize_title(clean_title)}|{clean_year}"
    now = time.time()
    cached = _poster_cache.get(cache_key)
    cache_ttl = _POSTER_CACHE_SECONDS if cached and cached[1] else _POSTER_NEGATIVE_CACHE_SECONDS
    if cached and now - cached[0] < cache_ttl:
        _record_cache_event("poster", "hit")
        return cached[1]

    _record_cache_event("poster", "miss")
    existing = _poster_inflight.get(cache_key)
    if existing is not None and not existing.done():
        _record_cache_event("poster", "inflight_join")
        try:
            return await asyncio.shield(existing)
        except Exception:
            return None

    task = asyncio.create_task(_poster_lookup_uncached(clean_title, clean_year, cache_key))
    _poster_inflight[cache_key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _poster_inflight.get(cache_key) is task:
            _poster_inflight.pop(cache_key, None)


# Return a lightweight redirect to the image CDN instead of proxying image bytes
# through the Render instance. The latter can time out on CDN connections and caused
# every poster to remain blank. Keep size selection here so w185 can load first.
def _poster_url_for_size(poster_url: str, size: str) -> str:
    url = str(poster_url or "").strip()
    if not url:
        return ""
    width = 185 if size == "w185" else 780
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        path = parts.path
        if host == "image.tmdb.org":
            # Replace the TMDB size segment without depending on a fragile regex.
            marker = "/t/p/"
            marker_index = path.find(marker)
            if marker_index >= 0:
                size_start = marker_index + len(marker)
                slash_index = path.find("/", size_start)
                if slash_index >= 0:
                    path = path[:size_start] + size + path[slash_index:]
        elif host.endswith("media-amazon.com") or host == "images-na.ssl-images-amazon.com":
            marker = "_V1_"
            marker_index = path.find(marker)
            if marker_index >= 0:
                path = path[:marker_index] + f"_V1_UX{width}_.jpg"
        elif "mzstatic.com" in host:
            path = re.sub(r"[0-9]+x[0-9]+bb", f"{width}x{width}bb", path)
        return parts._replace(path=path).geturl()
    except Exception:
        return url


@app.get("/api/poster")
async def api_poster(
    title: str = Query(..., min_length=1),
    year: str = Query(""),
    size: str = Query("w185"),
):
    poster = await _poster_lookup(title, year)
    if not poster:
        raise HTTPException(404, "Poster not found")
    size = size if size in {"w185", "w780"} else "w185"
    return RedirectResponse(
        _poster_url_for_size(poster, size),
        status_code=302,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/poster/resolve")
async def api_poster_resolve(title: str = Query(..., min_length=1), year: str = Query("")):
    poster = await _poster_lookup(title, year)
    if not poster:
        raise HTTPException(404, "Poster not found")
    return {"url": poster}

@app.get("/")
async def root():
    return {"name": APP_NAME, "status": "ok"}

@app.on_event("startup")
async def start_seedr_cleanup_worker():
    global _seedr_cleanup_worker_task
    _load_seedr_cleanup_jobs()
    if _seedr_cleanup_worker_task is None or _seedr_cleanup_worker_task.done():
        _seedr_cleanup_worker_task = asyncio.create_task(_seedr_cleanup_worker())


@app.get("/health")
async def health():
    return {"status": "ok", "seedrConfigured": bool(current_seedr_token()), "torrentSearchApi": TORRENT_SEARCH_API_URL}

@app.post("/api/search/resolve-magnet")
async def resolve_search_magnet(body: dict[str, Any]):
    """Resolve a provider detail page to a magnet only when the user prepares it."""
    info_url = str(body.get("infoUrl") or body.get("info_url") or "").strip()
    if not info_url:
        raise HTTPException(400, "A provider detail URL is required.")

    parsed = urlsplit(info_url)
    allowed_hosts = {
        str(host).lower().removeprefix("www.")
        for host in (*X1337_HOSTS, *LIMETORRENTS_HOSTS)
    }
    hostname = str(parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https" or hostname not in allowed_hosts:
        raise HTTPException(400, "The provider URL is not allowed.")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        # Follow redirects manually so every hop is checked against the
        # allowlist BEFORE it is requested (httpx would otherwise fetch an
        # unapproved redirect target and we would only notice afterwards).
        async with _InstrumentedAsyncClient(timeout=8.0, follow_redirects=False, headers=headers) as client:
            current_url = info_url
            response = None
            for _ in range(6):
                response = await client.get(current_url)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    break
                next_url = urljoin(current_url, location)
                next_parts = urlsplit(next_url)
                next_host = str(next_parts.hostname or "").lower().removeprefix("www.")
                if next_parts.scheme != "https" or next_host not in allowed_hosts:
                    raise HTTPException(400, "The provider redirected to an unapproved domain.")
                current_url = next_url
            else:
                raise HTTPException(502, "The torrent provider redirected too many times.")
            response.raise_for_status()
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.info("Lazy magnet resolution failed for provider URL %s: %s", info_url, exc)
        raise HTTPException(502, "Could not reach the torrent provider. Please try another result.") from exc

    # Prefer a real magnet anchor when the detail page includes one.
    # Parsing hrefs handles HTML-escaped ampersands better than scanning the
    # entire page for arbitrary strings.
    page = BeautifulSoup(response.text or "", "html.parser")
    magnet = ""
    for anchor in page.select('a[href^="magnet:"]'):
        candidate = unescape(str(anchor.get("href") or "")).strip()
        if info_hash(candidate):
            magnet = candidate
            break

    if not magnet:
        match = re.search(
            r"magnet:\?xt=urn:btih:[^\"<\s]+",
            response.text,
            re.IGNORECASE,
        )
        if match:
            candidate = unescape(match.group(0))
            if info_hash(candidate):
                magnet = candidate

    if not magnet:
        # LimeTorrents also exposes a hash on its download-icon/.torrent URL.
        # Recover a standard magnet when the detail page omits a magnet anchor.
        download_anchor = (
            page.select_one("a.csprite_dl14[href]")
            or page.select_one("a[href*='/download/']")
        )
        download_href = str(download_anchor.get("href") or "").strip() if download_anchor else ""
        hash_match = re.search(
            r"(?i)(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
            download_href,
        )
        if hash_match:
            digest = hash_match.group(1).lower()
            title_node = page.select_one("#content h1, h1, .tt-name")
            display_name = (
                title_node.get_text(" ", strip=True)
                if title_node is not None
                else Path(parsed.path).stem.replace("-", " ")
            )
            magnet = f"magnet:?xt=urn:btih:{digest}&dn={quote(display_name, safe='')}"
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet += "&tr=" + quote(tracker, safe="")

    digest = info_hash(magnet)
    if not digest:
        raise HTTPException(404, "The provider did not expose a usable magnet/hash for this result.")
    return {"magnet": magnet, "infoHash": digest}



# One-time Marvel catalogue. Results are shared by every visitor to this Home
# instance and never expire; the deployment mounts /app/data on persistent VPS
# storage so a container replacement does not force another provider crawl.
MARVEL_CATALOGUE_VERSION = 11
MARVEL_CATALOGUE_PATH = Path(os.getenv("MARVEL_CATALOGUE_PATH", "/app/data/marvel_catalogue.json"))
# Curated live-action theatrical Marvel films across studios (Marvel Studios, Fox,
# Sony, Universal, etc.). Excludes TV films, specials, documentaries, and animated
# feature films so the persistent torrent catalogue stays focused on live-action movies.
MARVEL_MOVIE_SEARCHES: tuple[tuple[str, int], ...] = (
    # MCU theatrical films, newest first.
    ("Spider-Man: Brand New Day", 2026),
    # Marvel Television special presentation starring Jon Bernthal as the Punisher.
    ("The Punisher: One Last Kill", 2026),
    ("The Fantastic Four: First Steps", 2025),
    ("Thunderbolts", 2025),
    ("Captain America: Brave New World", 2025),
    ("Deadpool & Wolverine", 2024),
    ("The Marvels", 2023),
    ("Guardians of the Galaxy Vol. 3", 2023),
    ("Ant-Man and the Wasp: Quantumania", 2023),
    ("Black Panther: Wakanda Forever", 2022),
    ("Thor: Love and Thunder", 2022),
    ("Doctor Strange in the Multiverse of Madness", 2022),
    ("Spider-Man: No Way Home", 2021),
    ("Eternals", 2021),
    ("Shang-Chi and the Legend of the Ten Rings", 2021),
    ("Black Widow", 2021),
    ("Spider-Man: Far From Home", 2019),
    ("Avengers: Endgame", 2019),
    ("Captain Marvel", 2019),
    ("Ant-Man and the Wasp", 2018),
    ("Avengers: Infinity War", 2018),
    ("Black Panther", 2018),
    ("Thor: Ragnarok", 2017),
    ("Spider-Man: Homecoming", 2017),
    ("Guardians of the Galaxy Vol. 2", 2017),
    ("Doctor Strange", 2016),
    ("Captain America: Civil War", 2016),
    ("Ant-Man", 2015),
    ("Avengers: Age of Ultron", 2015),
    ("Guardians of the Galaxy", 2014),
    ("Captain America: The Winter Soldier", 2014),
    ("Thor: The Dark World", 2013),
    ("Iron Man 3", 2013),
    ("The Avengers", 2012),
    ("Captain America: The First Avenger", 2011),
    ("Thor", 2011),
    ("Iron Man 2", 2010),
    ("The Incredible Hulk", 2008),
    ("Iron Man", 2008),

    # Non-MCU live-action theatrical films from Sony, Fox, and other studios.
    ("Kraven the Hunter", 2024),
    ("Madame Web", 2024),
    ("Venom: The Last Dance", 2024),
    ("Morbius", 2022),
    ("The New Mutants", 2020),
    ("Deadpool 2", 2018),
    ("Venom", 2018),
    ("Logan", 2017),
    ("Dark Phoenix", 2019),
    ("X-Men: Apocalypse", 2016),
    ("Deadpool", 2016),
    ("The Amazing Spider-Man 2", 2014),
    ("X-Men: Days of Future Past", 2014),
    ("The Wolverine", 2013),
    ("The Amazing Spider-Man", 2012),
    ("Ghost Rider: Spirit of Vengeance", 2011),
    ("X-Men: First Class", 2011),
    ("X-Men Origins: Wolverine", 2009),
    ("Punisher: War Zone", 2008),
    ("Venom: Let There Be Carnage", 2021),
    ("Fantastic Four: Rise of the Silver Surfer", 2007),
    ("Spider-Man 3", 2007),
    ("Ghost Rider", 2007),
    ("X-Men: The Last Stand", 2006),
    ("Elektra", 2005),
    ("Fantastic Four", 2005),
    ("Blade: Trinity", 2004),
    ("The Punisher", 2004),
    ("Spider-Man 2", 2004),
    ("X2: X-Men United", 2003),
    ("Daredevil", 2003),
    ("Hulk", 2003),
    ("Blade II", 2002),
    ("Spider-Man", 2002),
    ("X-Men", 2000),
    ("Blade", 1998),
    ("Howard the Duck", 1986),

    # Animated Spider-Verse films and other Marvel animated features.
    ("Spider-Man: Across the Spider-Verse", 2023),
    ("Spider-Man: Into the Spider-Verse", 2018),
    ("Spider-Man: The Venom Saga", 2005),
    ("Ultimate Avengers: The Movie", 2006),
    ("Ultimate Avengers 2", 2006),
    ("The Invincible Iron Man", 2007),
    ("Doctor Strange: The Sorcerer Supreme", 2007),
    ("Hulk Vs.", 2009),
    ("Planet Hulk", 2010),
    ("Thor: Tales of Asgard", 2011),
    ("Iron Man: Rise of Technovore", 2013),
    ("Avengers Confidential: Black Widow & Punisher", 2014),
    ("Marvel Super Hero Adventures: Frost Fight!", 2015),
    ("Captain America", 1944),
    ("The Amazing Spider-Man", 1977),
    ("Spider-Man Strikes Back", 1978),
    ("Spider-Man: The Dragon's Challenge", 1981),
    ("Dr. Strange", 1978),
    ("Captain America", 1990),
    ("Captain America II: Death Too Soon", 1979),
    ("The Incredible Hulk Returns", 1988),
    ("The Trial of the Incredible Hulk", 1989),
    ("The Death of the Incredible Hulk", 1990),
    ("Man-Thing", 2005),
    ("Generation X", 1996),
    ("Nick Fury: Agent of S.H.I.E.L.D.", 1998),
    ("Blade", 1998),
    ("Howard the Duck", 1986),
    ("The Punisher", 1989),
    ("The Punisher", 2004),
    ("Dr. Strange", 1978),
    ("The Fantastic Four", 1994),
)
_MARVEL_CATALOGUE_CONCURRENCY = 2
_marvel_catalogue_task: asyncio.Task | None = None
_marvel_catalogue_retry_after = 0.0
_marvel_catalogue_state: dict[str, Any] = {
    "status": "idle",
    "completed": 0,
    "total": len(MARVEL_MOVIE_SEARCHES),
    "resultCount": 0,
    "error": "",
}


def _read_marvel_catalogue() -> dict[str, Any] | None:
    return _memoized_file_read("marvel", MARVEL_CATALOGUE_PATH, _read_marvel_catalogue_uncached)


def _read_marvel_catalogue_uncached() -> dict[str, Any] | None:
    try:
        payload = json.loads(MARVEL_CATALOGUE_PATH.read_text(encoding="utf-8"))
        if (
            isinstance(payload, dict)
            # Reuse the previous curated snapshot across catalogue-version
            # changes. A version bump must not discard thousands of usable
            # cached torrent rows and trigger a full provider crawl.
            and isinstance(payload.get("version"), int)
            and payload.get("version") <= MARVEL_CATALOGUE_VERSION
            and isinstance(payload.get("results"), list)
            and payload["results"]
        ):
            changed = False
            retained_results = [
                item for item in payload["results"]
                if isinstance(item, dict)
                and not _catalogue_row_has_3d_quality(item)
                and not _catalogue_row_is_excluded_franchise_title(item)
            ]
            if len(retained_results) != len(payload["results"]):
                changed = True
            sorted_results = _sort_cached_catalogue_results(retained_results)
            if sorted_results != payload["results"]:
                changed = True
            payload["results"] = sorted_results
            payload["resultCount"] = len(sorted_results)
            payload["movieCount"] = len({
                re.sub(r"[^a-z0-9]+", "", str(item.get("catalogueTitle") or item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or "").casefold())
                for item in sorted_results
            })
            for item in payload["results"]:
                canonical_poster = _canonical_catalogue_poster_url(item)
                if canonical_poster and item.get("posterUrl") != canonical_poster:
                    item["posterUrl"] = canonical_poster
                    changed = True
            if changed:
                _write_marvel_catalogue(payload)
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_marvel_catalogue(payload: dict[str, Any]) -> None:
    MARVEL_CATALOGUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="marvel-catalogue-",
            suffix=".tmp",
            dir=str(MARVEL_CATALOGUE_PATH.parent),
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, MARVEL_CATALOGUE_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _merge_bundled_marvel_catalogue_cache(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Restore curated, OTT-enriched torrent rows from the bundled snapshot.

    The VPS's persistent cache can be older and narrower than the bundled
    snapshot. Merge only titles in the current curated list, while leaving the
    cache version old so the normal background refresh still runs.
    """
    if payload and (
        int(payload.get("version") or 0) >= MARVEL_CATALOGUE_VERSION
        or int(payload.get("seedMigrationVersion") or 0) >= MARVEL_CATALOGUE_VERSION
    ):
        return payload
    seed_path = _CACHE_SEED_DIR / "marvel_catalogue.json"
    try:
        seed_payload = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return payload

    if not isinstance(seed_payload, dict) or not isinstance(seed_payload.get("results"), list):
        return payload
    if payload and (
        int(payload.get("version") or 0) >= MARVEL_CATALOGUE_VERSION
        or int(payload.get("seedMigrationVersion") or 0) >= MARVEL_CATALOGUE_VERSION
    ):
        return payload

    # With no usable live file, start from the seed's metadata but rebuild its
    # result list from current allowed titles only (no legacy TV films/specials).
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        migrated = dict(payload)
        rows = [dict(item) for item in payload["results"] if isinstance(item, dict)]
    else:
        migrated = dict(seed_payload)
        migrated["results"] = []
        rows = []

    current_titles = {
        (re.sub(r"[^a-z0-9]+", "", title.casefold()), year): title
        for title, year in MARVEL_MOVIE_SEARCHES
    }

    def row_key(item: dict[str, Any]) -> str:
        digest = str(item.get("infoHash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", digest):
            digest = info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
        if re.fullmatch(r"[0-9a-f]{40}", digest, re.I):
            return "hash:" + digest.lower()
        normalized = _normalize_title(str(item.get("title") or ""))
        if not normalized:
            return ""
        try:
            size = int(float(item.get("size") or 0))
        except (TypeError, ValueError, OverflowError):
            size = 0
        return f"title-size:{normalized}|{size}"

    def score(item: dict[str, Any]) -> tuple[int, int]:
        try:
            seeders = int(float(item.get("seeders") or 0))
        except (TypeError, ValueError, OverflowError):
            seeders = 0
        try:
            leechers = int(float(item.get("leechers") or 0))
        except (TypeError, ValueError, OverflowError):
            leechers = 0
        return seeders, leechers

    def missing(value: Any) -> bool:
        return value is None or value == "" or value == [] or value == {}

    def ott_score(value: Any) -> int:
        if not isinstance(value, dict):
            return 0
        streaming = value.get("streamingProviders") or []
        rent = value.get("rentProviders") or []
        buy = value.get("buyProviders") or []
        status = str(value.get("status") or "").strip().lower()
        if streaming:
            return 4
        if rent or buy:
            return 3
        if status in {"unavailable", "not_available", "not available"}:
            return 2
        if status and status not in {"unknown", "error", "pending"}:
            return 1
        return 0

    metadata_fields = (
        "tmdbId", "tmdbUrl", "releaseDate", "rating", "posterUrl",
        "backdropUrl", "overview", "genres", "synopsisCheckedAt",
        "synopsisLookupVersion", "synopsisSource", "ottAvailability",
    )
    positions: dict[str, int] = {}
    for index, item in enumerate(rows):
        key = row_key(item)
        if key:
            positions[key] = index

    changed = False
    restored_rows = 0
    restored_titles: set[tuple[str, int]] = set()
    for seed_item in seed_payload["results"]:
        if (
            not isinstance(seed_item, dict)
            or _catalogue_row_has_3d_quality(seed_item)
            or _catalogue_row_is_excluded_franchise_title(seed_item)
        ):
            continue
        old_title = str(
            seed_item.get("marvelCatalogueTitle")
            or seed_item.get("catalogueTitle")
            or seed_item.get("mediaTitle")
            or ""
        ).strip()
        try:
            old_year = int(seed_item.get("year") or 0)
        except (TypeError, ValueError, OverflowError):
            old_year = 0
        canonical = current_titles.get((
            re.sub(r"[^a-z0-9]+", "", old_title.casefold()),
            old_year,
        ))
        if not canonical:
            continue

        candidate = dict(seed_item)
        candidate["mediaTitle"] = canonical
        candidate["marvelCatalogueTitle"] = canonical
        candidate["year"] = old_year
        key = row_key(candidate)
        if not key:
            continue
        restored_titles.add((
            re.sub(r"[^a-z0-9]+", "", canonical.casefold()),
            old_year,
        ))

        position = positions.get(key)
        if position is None:
            positions[key] = len(rows)
            rows.append(candidate)
            restored_rows += 1
            changed = True
            continue

        existing = rows[position]
        merged = dict(existing)
        if score(candidate) > score(existing):
            merged = dict(candidate)
            for field in metadata_fields:
                if missing(merged.get(field)) and not missing(existing.get(field)):
                    merged[field] = existing[field]

        # Preserve the better existing metadata, but restore useful India OTT
        # results when the persistent row has no provider data or only an error.
        for field in metadata_fields:
            value = candidate.get(field)
            if field == "ottAvailability":
                if ott_score(value) > ott_score(merged.get(field)):
                    merged[field] = value
            elif missing(merged.get(field)) and not missing(value):
                merged[field] = value

        if merged != existing:
            rows[position] = merged
            changed = True

    if not changed and int(migrated.get("seedMigrationVersion") or 0) >= MARVEL_CATALOGUE_VERSION:
        return migrated

    migrated["results"] = _sort_cached_catalogue_results(rows)
    migrated["resultCount"] = len(migrated["results"])
    migrated["movieCount"] = len({
        (
            re.sub(
                r"[^a-z0-9]+", "",
                str(item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or "").casefold(),
            ),
            int(item.get("year") or 0),
        )
        for item in migrated["results"]
        if isinstance(item, dict)
    })
    migrated["titlesQueried"] = len(MARVEL_MOVIE_SEARCHES)
    migrated["seedMigrationVersion"] = MARVEL_CATALOGUE_VERSION
    _write_marvel_catalogue(migrated)
    logger.info(
        "Restored %d torrent variants across %d curated Marvel titles from bundled snapshot %s",
        restored_rows,
        len(restored_titles),
        seed_path.name,
    )
    return migrated


def _marvel_result_matches_title(item: dict[str, Any], title: str, year: int) -> bool:
    """Prevent wrong-title and 3D releases from entering shared franchise caches."""
    raw_title = str(item.get("title") or "").strip()
    if not raw_title or _catalogue_row_has_3d_quality(item) or _catalogue_row_is_excluded_franchise_title(item):
        return False

    actual_tokens = set(_search_tokens(raw_title))
    expected_tokens = _search_tokens(title)
    if expected_tokens and expected_tokens[0] in {"the", "a", "an"}:
        expected_tokens = expected_tokens[1:]
    if not expected_tokens or not all(token in actual_tokens for token in expected_tokens):
        return False

    # If a release explicitly carries a year, don't cross-wire remakes/sequels.
    title_years = {
        int(value) for value in re.findall(r"\b((?:19|20)\d{2})\b", raw_title)
    }
    if title_years and year not in title_years:
        return False

    metadata_year = item.get("year")
    if metadata_year not in (None, "") and not title_years:
        try:
            if int(metadata_year) != year:
                return False
        except (TypeError, ValueError):
            pass

    return True


def _extra_result_matches_language(item: dict[str, Any], language: str | None) -> bool:
    """Separate English-original and Hindi/dubbed torrent releases for shared caches."""
    normalized_language = str(language or "").strip().lower()
    if normalized_language not in {"english", "hindi"}:
        return True

    release_text = " ".join(
        str(item.get(field) or "")
        for field in ("title", "quality", "audio", "language", "languages", "audioLanguage", "releaseName")
    )
    hindi_marker = re.compile(
        r"\b(?:hindi|hin|dual[\s._-]*audio|multi[\s._-]*(?:audio|language)|hindi[\s._-]*(?:dubbed|audio))\b",
        re.I,
    )
    other_language_marker = re.compile(
        r"\b(?:hindi|hin|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|"
        r"dual[\s._-]*audio|multi[\s._-]*audio|multi[\s._-]*language|dubbed)\b",
        re.I,
    )
    if normalized_language == "hindi":
        return bool(hindi_marker.search(release_text))
    return not bool(other_language_marker.search(release_text))


async def _build_marvel_catalogue() -> None:
    if CACHE_ONLY_MODE:
        logger.info("CACHE_ONLY_MODE: skipped Marvel catalogue build; using bundled snapshot.")
        return
    global _marvel_catalogue_retry_after
    semaphore = asyncio.Semaphore(_MARVEL_CATALOGUE_CONCURRENCY)
    poster_semaphore = asyncio.Semaphore(2)
    rows_by_hash: dict[str, dict[str, Any]] = {}
    found_movies: set[str] = set()

    async def resolve_movie_poster(title: str, year: int) -> str:
        async with poster_semaphore:
            try:
                poster = await asyncio.wait_for(
                    _poster_lookup(title, str(year)),
                    timeout=14.0,
                )
                if poster:
                    return poster
            except Exception as exc:
                logger.debug("Marvel poster lookup failed for '%s (%s)': %s", title, year, exc)
        return _poster_url_for_release(f"{title} {year}")

    async def search_movie(title: str, year: int) -> list[dict[str, Any]]:
        async with semaphore:
            try:
                query = f"{title} {year}"
                # Use Home's full provider fan-out first. Some providers return
                # zero rows for older/animated Marvel titles, so fall back to
                # the dedicated YTS catalogue search for those titles.
                try:
                    primary = await asyncio.wait_for(
                        search_1337x(query, limit=50, allow_series_fallback=False),
                        timeout=16.0,
                    )
                except Exception as exc:
                    logger.info("Marvel provider search timed out for '%s': %s", query, exc)
                    primary = []

                def validate_items(items: Any) -> list[dict[str, Any]]:
                    accepted: list[dict[str, Any]] = []
                    if not isinstance(items, list):
                        return accepted
                    for original in items:
                        if (
                            not isinstance(original, dict)
                            or not _marvel_result_matches_title(original, title, year)
                            or not _extra_result_matches_language(original, "english")
                        ):
                            continue
                        item = dict(original)
                        try:
                            size = int(float(item.get("size") or 0))
                            seeders = int(float(item.get("seeders") or 0))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if size < MIN_SEARCH_RESULT_SIZE_BYTES or size > MAX_SEARCH_RESULT_SIZE_BYTES or seeders <= 0:
                            continue
                        if not any(str(item.get(key) or "").strip() for key in (
                            "magnetUrl", "downloadUrl", "sourceUrl", "infoUrl", "infoHash"
                        )):
                            continue

                        # This field is the stable catalogue identity, not the
                        # raw torrent release name. Quality/magnet stay per variant.
                        item["mediaTitle"] = title
                        item["year"] = year
                        if not item.get("posterUrl"):
                            item["posterUrl"] = _poster_url_for_release(f"{title} {year}")
                        accepted.append(item)
                    return accepted

                accepted = validate_items(primary)
                if len(accepted) < 3:
                    try:
                        yts_items = await asyncio.wait_for(
                            search_yts_movies(query, limit=20),
                            timeout=8.0,
                        )
                        accepted.extend(validate_items(yts_items))
                    except Exception as exc:
                        logger.info("Marvel YTS fallback failed for '%s': %s", query, exc)

                # Deduplicate alternatives per movie before enriching shared poster
                # metadata. Keep the strongest copy of the same info hash.
                per_movie: dict[str, dict[str, Any]] = {}
                for item in accepted:
                    digest = str(item.get("infoHash") or "").strip().lower()
                    if not re.fullmatch(r"[0-9a-f]{40}", digest):
                        digest = info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
                    key = "hash:" + digest if re.fullmatch(r"[0-9a-f]{40}", digest, re.I) else (
                        f"title-size:{_normalize_title(str(item.get('title') or ''))}|{int(item.get('size') or 0)}"
                    )
                    previous = per_movie.get(key)
                    if previous is None or (
                        int(item.get("seeders") or 0),
                        int(item.get("leechers") or 0),
                        1 if str(item.get("magnetUrl") or "").startswith("magnet:") else 0,
                    ) > (
                        int(previous.get("seeders") or 0),
                        int(previous.get("leechers") or 0),
                        1 if str(previous.get("magnetUrl") or "").startswith("magnet:") else 0,
                    ):
                        per_movie[key] = item

                accepted = list(per_movie.values())
                if accepted:
                    found_movies.add(f"{_normalize_title(title)}|{year}")
                    poster_url = await resolve_movie_poster(title, year)
                    for item in accepted:
                        item["mediaTitle"] = title
                        item["year"] = year
                        item["posterUrl"] = poster_url
                        item["marvelCatalogueTitle"] = title

                return accepted
            except Exception as exc:
                logger.info("Marvel catalogue search failed for '%s (%s)': %s", title, year, exc)
                return []
            finally:
                _marvel_catalogue_state["completed"] = int(
                    _marvel_catalogue_state.get("completed") or 0
                ) + 1

    try:
        _marvel_catalogue_state.update({
            "status": "building",
            "completed": 0,
            "total": len(MARVEL_MOVIE_SEARCHES),
            "resultCount": 0,
            "movieCount": 0,
            "error": "",
        })
        tasks = [
            asyncio.create_task(search_movie(title, year))
            for title, year in MARVEL_MOVIE_SEARCHES
        ]
        for task in asyncio.as_completed(tasks):
            try:
                items = await task
            except Exception as exc:
                logger.info("Marvel catalogue worker failed: %s", exc)
                items = []

            for item in items:
                digest = str(item.get("infoHash") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", digest):
                    source = str(item.get("magnetUrl") or item.get("downloadUrl") or "")
                    digest = info_hash(source)
                if re.fullmatch(r"[0-9a-f]{40}", digest, re.I):
                    key = "hash:" + digest.lower()
                else:
                    normalized = _normalize_title(str(item.get("title") or ""))
                    if not normalized:
                        continue
                    key = f"title-size:{normalized}|{int(item.get('size') or 0)}"

                previous = rows_by_hash.get(key)
                candidate_score = (
                    1 if str(item.get("magnetUrl") or "").startswith("magnet:") else 0,
                    int(item.get("seeders") or 0),
                    int(item.get("leechers") or 0),
                )
                previous_score = (
                    1 if previous and str(previous.get("magnetUrl") or "").startswith("magnet:") else 0,
                    int(previous.get("seeders") or 0) if previous else 0,
                    int(previous.get("leechers") or 0) if previous else 0,
                )
                if previous is None or candidate_score > previous_score:
                    rows_by_hash[key] = item
            _marvel_catalogue_state["resultCount"] = len(rows_by_hash)
            _marvel_catalogue_state["movieCount"] = len(found_movies)

        # Preserve valid variants for current curated Marvel titles if a provider misses them
        # during this rebuild; new search hits are merged into the existing cache.
        previous_payload = _read_marvel_catalogue()
        current_titles = {
            (re.sub(r"[^a-z0-9]+", "", title.casefold()), year): title
            for title, year in MARVEL_MOVIE_SEARCHES
        }
        if previous_payload:
            for old_item in previous_payload.get("results") or []:
                if not isinstance(old_item, dict):
                    continue
                old_title = str(old_item.get("marvelCatalogueTitle") or old_item.get("mediaTitle") or "").strip()
                try:
                    old_year = int(old_item.get("year") or 0)
                except (TypeError, ValueError):
                    old_year = 0
                # Preserve every previously cached row during additive catalogue
                # updates. Curated titles get canonical naming; legacy entries that
                # are no longer in the current list remain intact rather than vanishing.
                canonical = current_titles.get(
                    (re.sub(r"[^a-z0-9]+", "", old_title.casefold()), old_year),
                    old_title,
                )
                if not canonical:
                    continue
                preserved = dict(old_item)
                preserved["mediaTitle"] = canonical
                preserved["marvelCatalogueTitle"] = canonical
                if old_year:
                    preserved["year"] = old_year
                digest = str(preserved.get("infoHash") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", digest):
                    digest = info_hash(str(preserved.get("magnetUrl") or preserved.get("downloadUrl") or ""))
                dedupe_key = "hash:" + digest if re.fullmatch(r"[0-9a-f]{40}", digest, re.I) else (
                    f"title-size:{_normalize_title(str(preserved.get('title') or ''))}|{int(preserved.get('size') or 0)}"
                )
                existing = rows_by_hash.get(dedupe_key)
                score = (int(preserved.get("seeders") or 0), int(preserved.get("leechers") or 0))
                existing_score = (int(existing.get("seeders") or 0), int(existing.get("leechers") or 0)) if existing else (-1, -1)
                if existing is None or score > existing_score:
                    rows_by_hash[dedupe_key] = preserved

        results = list(rows_by_hash.values())
        movie_count = len({
            f"{_normalize_title(str(item.get('mediaTitle') or item.get('title') or ''))}|{item.get('year') or ''}"
            for item in results
        })
        # Don't permanently cache a partial crawl like a list of just a handful
        # of recent releases. A shared catalogue must contain a useful spread.
        if movie_count < 12:
            raise RuntimeError(
                f"Only {movie_count} Marvel movie titles produced usable results; refusing to cache an incomplete catalogue."
            )

        def catalogue_sort_key(item: dict[str, Any]) -> tuple[int, float, int, str]:
            source = str(item.get("mediaTitle") or item.get("title") or "")
            embedded_year = re.search(r"\b((?:19|20)\d{2})\b", source)
            try:
                year = int(item.get("year") or (embedded_year.group(1) if embedded_year else 0))
            except (TypeError, ValueError):
                year = int(embedded_year.group(1)) if embedded_year else 0
            try:
                rating = float(item.get("rating") or 0)
            except (TypeError, ValueError):
                rating = 0.0
            return (year, rating, int(item.get("seeders") or 0), source.lower())

        results.sort(key=catalogue_sort_key, reverse=True)
        payload = {
            "version": MARVEL_CATALOGUE_VERSION,
            "builtAt": datetime.now(timezone.utc).isoformat(),
            "titlesQueried": len(MARVEL_MOVIE_SEARCHES),
            "movieCount": movie_count,
            "resultCount": len(results),
            "results": results,
        }
        _merge_saved_catalogue_metadata(payload, MARVEL_CATALOGUE_PATH)
        _write_marvel_catalogue(payload)
        _marvel_catalogue_state.update({
            "status": "ready",
            "completed": len(MARVEL_MOVIE_SEARCHES),
            "total": len(MARVEL_MOVIE_SEARCHES),
            "resultCount": len(results),
            "movieCount": movie_count,
            "error": "",
            "builtAt": payload["builtAt"],
        })
        logger.info(
            "Built persistent Marvel catalogue: %d movie titles, %d torrent options from %d title searches",
            movie_count,
            len(results),
            len(MARVEL_MOVIE_SEARCHES),
        )
    except Exception as exc:
        _marvel_catalogue_retry_after = time.time() + 60
        _marvel_catalogue_state.update({
            "status": "failed",
            "error": "The first Marvel catalogue build failed. Please try again in a minute.",
        })
        logger.warning("Marvel catalogue build failed: %s", exc)




# Additional shared catalogues. Curated DC lists are long-lived; latest-release
# catalogues are refreshed monthly and keep serving the previous good cache during refresh.
_EXTRA_CATALOGUES: dict[str, dict[str, Any]] = {
    "dc-live-action": {
        "file": "dc_live_action_catalogue.json", "version": 6, "refresh_days": 36500,
        "minimum_movies": 5, "mode": "fixed", "language": "english",
        "exclude_titles": [
            "The Old Guard", "The Old Guard 2", "RED 2", "The Kitchen", "RED",
            "The Losers", "Stardust", "A History of Violence", "The League of Extraordinary Gentlemen",
            "Road to Perdition", "Steel", "The Spirit", "Batman: Mask of the Phantasm",
            "Superman II: The Richard Donner Cut", "Watchmen: Chapter I", "Watchmen: Chapter II",
        ],
        "titles": [
            ("Superman", 2025), ("Supergirl", 2026), ("The Batman Part II", 2027),
            ("Joker: Folie à Deux", 2024), ("The Batman", 2022), ("The Suicide Squad", 2021),
            ("Batman: The Brave and the Bold", 2028), ("Clayface", 2026),
            ("Superman: Legacy", 2025), ("The Batman - Part II", 2027),
            ("Batman Forever", 1995), ("Batman & Robin", 1997), ("Batman: Mask of the Phantasm", 1993),
            ("Superman III", 1983), ("Superman IV: The Quest for Peace", 1987),
            ("Superman: The Movie", 1978),
            ("The Crow", 1994),
            ("The Crow", 2024), ("Spawn", 1997), ("Jonah Hex", 2010),
            ("Superman II: The Richard Donner Cut", 2006), ("Watchmen: Chapter I", 2024),
            ("Watchmen: Chapter II", 2024), ("Swamp Thing", 1982),
            ("Zack Snyder's Justice League", 2021), ("Wonder Woman 1984", 2020),
            ("Birds of Prey", 2020), ("Joker", 2019), ("Shazam!", 2019),
            ("Aquaman", 2018), ("Aquaman and the Lost Kingdom", 2023),
            ("Wonder Woman", 2017), ("Justice League", 2017), ("Man of Steel", 2013),
            ("Batman v Superman: Dawn of Justice", 2016), ("Suicide Squad", 2016),
            ("Green Lantern", 2011), ("The Dark Knight Rises", 2012),
            ("The Dark Knight", 2008), ("Batman Begins", 2005), ("Batman Returns", 1992),
            ("Batman", 1989), ("Superman Returns", 2006), ("Superman II", 1980),
            ("Superman", 1978), ("Constantine", 2005), ("Watchmen", 2009),
            ("V for Vendetta", 2005),
            ("Catwoman", 2004), ("The Flash", 2023), ("Black Adam", 2022),
            ("Blue Beetle", 2023), ("Shazam! Fury of the Gods", 2023),
        ],
    },
    "dc-animated": {
        "file": "dc_animated_catalogue.json", "version": 3, "refresh_days": 36500,
        "minimum_movies": 5, "mode": "fixed",
        "titles": [
            ("Batman: Knightfall - Part 1: Knightfall", 2026),
            ("Aztec Batman: Clash of Empires", 2025),
            ("Watchmen Chapter II", 2024),
            ("Watchmen Chapter I", 2024),
            ("Justice League: Crisis on Infinite Earths - Part Three", 2024),
            ("Justice League: Crisis on Infinite Earths - Part Two", 2024),
            ("Justice League: Crisis on Infinite Earths - Part One", 2024),
            ("Justice League: Warworld", 2023),
            ("Batman: The Doom That Came to Gotham", 2023),
            ("Legion of Super-Heroes", 2023),
            ("Batman and Superman: Battle of the Super Sons", 2022),
            ("Green Lantern: Beware My Power", 2022),
            ("Catwoman: Hunted", 2022),
            ("Injustice", 2021),
            ("Batman: The Long Halloween, Part Two", 2021),
            ("Batman: The Long Halloween, Part One", 2021),
            ("Justice Society: World War II", 2021),
            ("Batman: Soul of the Dragon", 2021),
            ("Superman: Man of Tomorrow", 2020),
            ("Deathstroke: Knights & Dragons", 2020),
            ("Justice League Dark: Apokolips War", 2020),
            ("Superman: Red Son", 2020),
            ("Wonder Woman: Bloodlines", 2019),
            ("Batman: Hush", 2019),
            ("Justice League vs. the Fatal Five", 2019),
            ("Reign of the Supermen", 2019),
            ("Constantine: City of Demons", 2019),
            ("The Death of Superman", 2018),
            ("Suicide Squad: Hell to Pay", 2018),
            ("Batman: Gotham by Gaslight", 2018),
            ("Batman and Harley Quinn", 2017),
            ("Teen Titans: The Judas Contract", 2017),
            ("Justice League Dark", 2017),
            ("Batman: The Killing Joke", 2016),
            ("Justice League vs. Teen Titans", 2016),
            ("Batman: Bad Blood", 2016),
            ("Justice League: Gods and Monsters", 2015),
            ("Batman vs. Robin", 2015),
            ("Justice League: Throne of Atlantis", 2015),
            ("Justice League: War", 2014),
            ("Batman: Assault on Arkham", 2014),
            ("Son of Batman", 2014),
            ("Justice League: The Flashpoint Paradox", 2013),
            ("Superman: Unbound", 2013),
            ("Batman: The Dark Knight Returns - Part 2", 2013),
            ("Batman: The Dark Knight Returns - Part 1", 2012),
            ("Superman vs. The Elite", 2012),
            ("Justice League: Doom", 2012),
            ("Batman: Year One", 2011),
            ("All-Star Superman", 2011),
            ("Green Lantern: Emerald Knights", 2011),
            ("Superman/Batman: Apocalypse", 2010),
            ("Batman: Under the Red Hood", 2010),
            ("Justice League: Crisis on Two Earths", 2010),
            ("Superman/Batman: Public Enemies", 2009),
            ("Green Lantern: First Flight", 2009),
            ("Wonder Woman", 2009),
            ("Justice League: The New Frontier", 2008),
            ("Batman: Gotham Knight", 2008),
            ("Superman: Doomsday", 2007),
        ],
    },
}
# Separate persistent caches keep English-original and Hindi/dubbed options
# independent so neither language can pollute the other catalogue.
_EXTRA_CATALOGUES["dc-live-action-hindi"] = {
    **_EXTRA_CATALOGUES["dc-live-action"],
    "file": "dc_live_action_hindi_catalogue.json",
    "version": 6,
    "language": "hindi",
    "titles": list(_EXTRA_CATALOGUES["dc-live-action"]["titles"]),
}
_EXTRA_CATALOGUES["classics"] = {
    "file": "classics_catalogue.json",
    "version": 5,
    "refresh_days": 36500,
    "minimum_movies": 5,
    "mode": "fixed",
    "language": "english",
    "titles": [
        ("The Shawshank Redemption", 1994),
        ("Memento", 2000),
        ("Bruce Almighty", 2003),
        ("Good Will Hunting", 1997),
        ("The Truman Show", 1998),
        ("The Sixth Sense", 1999),
        ("The Departed", 2006),
        ("Liar Liar", 1997),
        ("Source Code", 2011),
        ("In Time", 2011),
        ("Bird Box", 2018),
        ("The Interview", 2014),
        ("Focus", 2015),
        ("Nobody", 2021),
        ("Kick-Ass", 2010),
        ("The Raid: Redemption", 2011),
        ("The Raid 2", 2014),
        ("Fight Club", 1999),
        ("Mad Max: Fury Road", 2015),
        ("The Equalizer", 2014),
        ("World War Z", 2013),
        ("Hellboy", 2004),
        ("Hellboy II: The Golden Army", 2008),
        ("Predestination", 2014),
        ("Prometheus", 2012),
        ("National Treasure", 2004),
        ("National Treasure: Book of Secrets", 2007),
        ("Jumanji", 1995),
        ("Jumanji: Welcome to the Jungle", 2017),
        ("Ender's Game", 2013),
        ("Battleship", 2012),
        ("A Quiet Place", 2018),
        ("A Quiet Place Part II", 2020),
        ("John Wick", 2014),
        ("John Wick: Chapter 2", 2017),
        ("John Wick: Chapter 3 - Parabellum", 2019),
        ("The Matrix", 1999),
        ("The Matrix Reloaded", 2003),
        ("The Matrix Revolutions", 2003),
        ("Men in Black", 1997),
        ("Men in Black II", 2002),
        ("Men in Black 3", 2012),
        ("The Mummy", 1999),
        ("The Mummy Returns", 2001),
        ("The Mummy: Tomb of the Dragon Emperor", 2008),
        ("Taken", 2008),
        ("Taken 2", 2012),
        ("Taken 3", 2014),
        ("Van Helsing", 2004),
        ("G.I. Joe: The Rise of Cobra", 2009),
        ("Hancock", 2008),
        ("Wanted", 2008),
        ("Prince of Persia: The Sands of Time", 2010),
        ("AVP: Alien vs. Predator", 2004),
        ("The Rundown", 2003),
        ("Ghost Rider", 2007),
        ("Now You See Me", 2013),
        ("Edge of Tomorrow", 2014),
        ("John Carter", 2012),
        ("Inception", 2010),
        ("Kingsman: The Secret Service", 2014),
        ("Kingsman: The Golden Circle", 2017),
        ("Constantine", 2005),
        ("The Hitman's Bodyguard", 2017),
        ("The Hitman's Wife's Bodyguard", 2021),
        ("Cast Away", 2000),
        ("Dracula Untold", 2014),
        ("Abraham Lincoln: Vampire Hunter", 2012),
        ("300", 2006),
        ("Escape Plan", 2013),
        ("The Maze Runner", 2014),
        ("The A-Team", 2010),
        ("Skyscraper", 2018),
        ("Sherlock Holmes", 2009),
        ("Sherlock Holmes: A Game of Shadows", 2011),
    ],
}
_EXTRA_CATALOGUES["marvel-hindi"] = {
    "file": "marvel_hindi_catalogue.json",
    "version": 4,
    "refresh_days": 36500,
    "minimum_movies": 5,
    "mode": "fixed",
    "language": "hindi",
    "titles": list(MARVEL_MOVIE_SEARCHES),
}

_EXTRA_CATALOGUE_PATHS = {
    key: Path(os.getenv("CATALOGUE_" + key.upper().replace("-", "_") + "_PATH", "/app/data/" + config["file"]))
    for key, config in _EXTRA_CATALOGUES.items()
}

# Bundled snapshots let ephemeral hosts (such as Render) start with the same
# catalogues as the VPS. Seed only missing/empty data files; never replace a
# populated persistent cache with an older image snapshot.
_CACHE_SEED_DIR = Path(os.getenv("CACHE_SEED_DIR", "/app/cache-seed"))
_CACHE_SEED_FILES = (
    "dc_animated_catalogue.json",
    "dc_live_action_catalogue.json",
    "dc_live_action_hindi_catalogue.json",
    "classics_catalogue.json",
    "regional_tv_prebuilt_cache.json",
    "latest_bollywood_catalogue.json",
    "latest_hollywood_catalogue.json",
    "marvel_catalogue.json",
    "marvel_web_series_catalogue.json",
    "marvel_hindi_catalogue.json",
    "bollywood_torrent_matched_catalogue.json",
)


# Runtime metadata caches are snapshotted to GitHub alongside the curated catalogues.
# This gives fresh/ephemeral deployments a warm starting point without replacing
# any cache data already populated by that deployment.
_RUNTIME_CACHE_SEED_FILES = {
    "tmdb_movie_catalogue_cache.json": TMDB_CATALOGUE_CACHE_FILE,
    "tmdb_search_metadata_cache.json": TMDB_SEARCH_METADATA_CACHE_FILE,
    "imdb_rating_cache.json": IMDB_RATING_CACHE_FILE,
    "tmdb_ott_availability_cache.json": TMDB_OTT_CACHE_FILE,
    "marvel_tv_catalogue_cache.json": MARVEL_TV_CATALOGUE_CACHE_FILE,
    "dc_tv_catalogue_cache.json": DC_TV_CATALOGUE_CACHE_FILE,
    "regional_movie_prebuilt_cache.json": Path(os.getenv(
        "REGIONAL_MOVIE_PREBUILT_CACHE_PATH",
        str(Path(os.getenv("APP_DATA_DIR", "/app/data")) / "regional_movie_prebuilt_cache.json"),
    )),
    "regional_movie_trending_archive.json": REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE,
    "nexstream_cache.json": Path(os.getenv(
        "NEXSTREAM_CACHE_PATH", "/app/data/nexstream_cache.json",
    )),
    "nexstream_episode_metadata_cache.json": Path(os.getenv(
        "NEXSTREAM_EPISODE_CACHE_PATH", "/app/data/nexstream_episode_metadata_cache.json",
    )),
}


def _seed_bundled_runtime_caches() -> None:
    seed_dir = _CACHE_SEED_DIR / "runtime"
    for filename, destination in _RUNTIME_CACHE_SEED_FILES.items():
        source = seed_dir / filename
        if not source.is_file() or source.stat().st_size == 0:
            logger.info("No bundled runtime snapshot for %s at %s", filename, source)
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)

            # OTT is a shared, append-only lookup cache. Merge the bundled VPS
            # snapshot into the runtime file even when the latter already exists;
            # otherwise an old/non-empty Render file can silently hide thousands
            # of useful entries shipped with the deployment.
            if filename == "tmdb_ott_availability_cache.json":
                bundled = json.loads(source.read_text(encoding="utf-8"))
                bundled_items = bundled.get("items") if isinstance(bundled, dict) else None
                if not isinstance(bundled_items, dict):
                    logger.warning("Ignoring invalid bundled OTT cache snapshot: %s", source)
                    continue

                try:
                    runtime = json.loads(destination.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    runtime = {}
                if not isinstance(runtime, dict):
                    runtime = {}
                runtime_items = runtime.get("items")
                if not isinstance(runtime_items, dict):
                    runtime_items = {}

                added = 0
                replaced = 0
                for key, bundled_row in bundled_items.items():
                    if not isinstance(bundled_row, dict) or not isinstance(bundled_row.get("payload"), dict):
                        continue
                    current_row = runtime_items.get(key)
                    try:
                        bundled_saved_at = float(bundled_row.get("savedAt") or 0)
                        current_saved_at = float(current_row.get("savedAt") or 0) if isinstance(current_row, dict) else 0
                    except (TypeError, ValueError):
                        bundled_saved_at = 0
                        current_saved_at = 0
                    if not isinstance(current_row, dict):
                        runtime_items[key] = bundled_row
                        added += 1
                    elif bundled_saved_at > current_saved_at:
                        runtime_items[key] = bundled_row
                        replaced += 1

                runtime["items"] = runtime_items
                if added or replaced or not destination.is_file() or destination.stat().st_size == 0:
                    temp_path = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
                    temp_path.write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
                    os.replace(temp_path, destination)
                    logger.info(
                        "Merged bundled OTT cache snapshot: %s added, %s newer entries applied, %s total entries",
                        added, replaced, len(runtime_items),
                    )
                else:
                    logger.info("OTT cache snapshot already represented in runtime cache (%s entries)", len(runtime_items))
                continue

            if destination.is_file() and destination.stat().st_size > 0:
                continue
            shutil.copy2(source, destination)
            logger.info("Seeded runtime cache %s from GitHub snapshot", filename)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Could not seed runtime cache %s: %s", filename, exc)


def _seed_bundled_catalogue_caches() -> None:
    destinations = {
        "marvel_catalogue.json": MARVEL_CATALOGUE_PATH,
        "marvel_web_series_catalogue.json": Path(os.getenv("MARVEL_WEB_SERIES_CATALOGUE_PATH", "/app/data/marvel_web_series_catalogue.json")),
        **{config["file"]: _EXTRA_CATALOGUE_PATHS[key] for key, config in _EXTRA_CATALOGUES.items()},
        "bollywood_torrent_matched_catalogue.json": Path(os.getenv(
            "BOLLYWOOD_TORRENT_CATALOGUE_PATH",
            str(Path(os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_torrent_matched_catalogue.json"),
        )),
        "regional_tv_prebuilt_cache.json": REGIONAL_TV_PREBUILT_CACHE_FILE,
    }
    for filename in _CACHE_SEED_FILES:
        source = _CACHE_SEED_DIR / filename
        destination = destinations.get(filename)
        if destination is None or not source.is_file() or source.stat().st_size == 0:
            continue
        try:
            if destination.is_file() and destination.stat().st_size > 0:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            logger.info("Seeded catalogue cache %s from bundled snapshot", filename)
        except OSError as exc:
            logger.warning("Could not seed catalogue cache %s: %s", filename, exc)


_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE: dict[str, Any] = {
    "status": "idle", "filesCompleted": 0, "filesTotal": 0,
    "titlesCompleted": 0, "titlesTotal": 0, "updatedRows": 0, "error": "",
}
_catalogue_synopsis_enrichment_task: asyncio.Task | None = None
_CATALOGUE_SYNOPSIS_RECHECK_SECONDS = 30 * 24 * 60 * 60


def _synopsis_cache_paths() -> list[tuple[str, Path]]:
    paths = [("marvel", MARVEL_CATALOGUE_PATH)]
    paths.extend((key, path) for key, path in _EXTRA_CATALOGUE_PATHS.items())
    # De-duplicate aliases that may intentionally point to the same configured path.
    seen: set[str] = set()
    unique: list[tuple[str, Path]] = []
    for name, path in paths:
        identity = str(path.resolve())
        if identity not in seen:
            seen.add(identity)
            unique.append((name, path))
    return unique


def _synopsis_row_identity(item: dict[str, Any]) -> tuple[str, int]:
    title = str(
        item.get("catalogueTitle") or item.get("marvelCatalogueTitle")
        or item.get("mediaTitle") or item.get("title") or ""
    ).strip()
    year_value = item.get("year")
    try:
        year = int(year_value) if year_value else 0
    except (TypeError, ValueError):
        year = 0
    if not year:
        match = re.search(r"\b(19\d{2}|20\d{2})\b", title)
        if match:
            year = int(match.group(1))
    return _normalize_title(title), year


_CATALOGUE_PERSISTENT_METADATA_FIELDS = (
    "overview",
    "tmdbId",
    "tmdbUrl",
    "releaseDate",
    "rating",
    "posterUrl",
    "backdropUrl",
    "genres",
    "synopsisCheckedAt",
    "synopsisLookupVersion",
    "synopsisSource",
)


def _catalogue_metadata_value_present(field: str, value: Any) -> bool:
    if value in (None, "", []):
        return False
    # Zero-value metadata should not prevent a useful saved value being reused.
    if field in {"tmdbId", "rating", "synopsisCheckedAt", "synopsisLookupVersion"}:
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False
    return True


def _catalogue_metadata_tmdb_id(item: dict[str, Any]) -> int:
    try:
        return int(item.get("tmdbId") or item.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _merge_catalogue_metadata_profile(
    target: dict[str, Any],
    source: dict[str, Any],
) -> None:
    for field in _CATALOGUE_PERSISTENT_METADATA_FIELDS:
        value = source.get(field)
        if (
            not _catalogue_metadata_value_present(field, target.get(field))
            and _catalogue_metadata_value_present(field, value)
        ):
            target[field] = value


def _merge_saved_catalogue_metadata(
    payload: dict[str, Any],
    destination_path: Path,
) -> int:
    """Carry saved TMDB metadata forward when rebuilding a torrent catalogue.

    Cache rows are matched by TMDB ID first, then by normalized canonical title
    and release year. Only absent fields are copied, so freshly fetched data wins
    and an empty synopsis from a torrent provider can never erase a saved one.
    Metadata is shared across the English/Hindi Marvel and DC cache variants.
    """
    fresh_rows = payload.get("results")
    if not isinstance(fresh_rows, list):
        return 0

    metadata_by_tmdb_id: dict[int, dict[str, Any]] = {}
    metadata_by_identity: dict[tuple[str, int], dict[str, Any]] = {}

    def collect_profile(
        index: dict[Any, dict[str, Any]],
        key: Any,
        row: dict[str, Any],
    ) -> None:
        if not key:
            return
        profile = index.setdefault(key, {})
        _merge_catalogue_metadata_profile(profile, row)

    seen_paths: set[str] = set()
    for _name, cache_path in _synopsis_cache_paths():
        try:
            canonical_path = str(cache_path.resolve())
        except OSError:
            canonical_path = str(cache_path)
        if canonical_path in seen_paths:
            continue
        seen_paths.add(canonical_path)

        try:
            saved_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        saved_rows = saved_payload.get("results", []) if isinstance(saved_payload, dict) else []
        if not isinstance(saved_rows, list):
            continue

        for saved_row in saved_rows:
            if not isinstance(saved_row, dict):
                continue
            profile = {
                field: saved_row[field]
                for field in _CATALOGUE_PERSISTENT_METADATA_FIELDS
                if _catalogue_metadata_value_present(field, saved_row.get(field))
            }
            if not profile:
                continue

            tmdb_id = _catalogue_metadata_tmdb_id(saved_row)
            if tmdb_id:
                collect_profile(metadata_by_tmdb_id, tmdb_id, profile)

            identity = _synopsis_row_identity(saved_row)
            if identity[0]:
                collect_profile(metadata_by_identity, identity, profile)

    updated_rows = 0
    for row in fresh_rows:
        if not isinstance(row, dict):
            continue

        row_tmdb_id = _catalogue_metadata_tmdb_id(row)
        identity = _synopsis_row_identity(row)
        profile: dict[str, Any] = {}

        if row_tmdb_id and row_tmdb_id in metadata_by_tmdb_id:
            _merge_catalogue_metadata_profile(profile, metadata_by_tmdb_id[row_tmdb_id])

        identity_profile = metadata_by_identity.get(identity) if identity[0] else None
        # If both records already have authoritative but conflicting TMDB IDs,
        # don't transfer the synopsis based on title similarity alone.
        identity_tmdb_id = _catalogue_metadata_tmdb_id(identity_profile or {})
        conflicting_ids = bool(row_tmdb_id and identity_tmdb_id and row_tmdb_id != identity_tmdb_id)
        if identity_profile and not conflicting_ids:
            _merge_catalogue_metadata_profile(profile, identity_profile)

        before = {
            field: row.get(field)
            for field in _CATALOGUE_PERSISTENT_METADATA_FIELDS
        }
        _merge_catalogue_metadata_profile(row, profile)
        if any(row.get(field) != before[field] for field in _CATALOGUE_PERSISTENT_METADATA_FIELDS):
            updated_rows += 1

    if updated_rows:
        logger.info(
            "Preserved previously saved TMDB metadata for %d rows before rebuilding %s",
            updated_rows,
            destination_path.name,
        )
    return updated_rows


def _write_synopsis_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=path.stem + "-synopsis-",
            suffix=".tmp", dir=str(path.parent), delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _normalize_synopsis_match_title(value: Any) -> str:
    """Normalize harmless catalogue/provider title variants before matching metadata."""
    normalized = _normalize_title(str(value or ""))
    # Some sources omit a leading article and spell numerals as words; normalize
    # these common variants without allowing broad fuzzy matches to unrelated films.
    normalized = re.sub(r"^(?:the|a|an)\s+", "", normalized)
    normalized = re.sub(r"\bfour\b", "4", normalized)
    return normalized


async def _lookup_watchmode_catalogue_synopsis(
    title: str,
    year: int,
    tmdb_id: int = 0,
) -> tuple[dict[str, Any] | None, bool]:
    """Fetch Watchmode plot_overview as a fallback; return whether lookup completed."""
    if not WATCHMODE_API_KEYS:
        return None, False

    query = re.sub(r"\s+", " ", title or "").strip()
    completed = False
    timeout = httpx.Timeout(8.0, connect=3.0)

    def make_metadata(details: dict[str, Any], fallback_tmdb_id: int = 0) -> dict[str, Any] | None:
        overview = re.sub(
            r"\s+",
            " ",
            str(details.get("plot_overview") or details.get("overview") or ""),
        ).strip()
        if not overview:
            return None

        try:
            resolved_tmdb_id = int(details.get("tmdb_id") or fallback_tmdb_id or tmdb_id or 0)
        except (TypeError, ValueError):
            resolved_tmdb_id = fallback_tmdb_id or tmdb_id or 0

        release_date = str(details.get("release_date") or "").strip()
        resolved_year = details.get("year") or year or None
        if release_date:
            year_match = re.match(r"^(\d{4})", release_date)
            if year_match:
                resolved_year = int(year_match.group(1))

        metadata: dict[str, Any] = {
            "overview": overview,
            "synopsisSource": "Watchmode",
            "synopsisCheckedAt": int(time.time()),
            "synopsisLookupVersion": TMDB_SYNOPSIS_LOOKUP_VERSION,
        }
        if resolved_tmdb_id > 0:
            metadata["tmdbId"] = resolved_tmdb_id
            metadata["tmdbUrl"] = f"https://www.themoviedb.org/movie/{resolved_tmdb_id}"
        if release_date:
            metadata["releaseDate"] = release_date
        if resolved_year:
            metadata["year"] = resolved_year
        return metadata

    async def get_details(identifier: str, fallback_tmdb_id: int = 0) -> tuple[dict[str, Any] | None, bool]:
        try:
            response = await _watchmode_get(
                f"/title/{quote(identifier, safe='')}/details/",
                params={"language": "en"},
                timeout=timeout,
            )
        except Exception as exc:
            logger.info("Watchmode synopsis lookup failed for %s: %s", identifier, type(exc).__name__)
            return None, False

        if response is None or response.status_code >= 400:
            return None, False
        try:
            details = response.json()
        except (ValueError, TypeError):
            return None, False
        if not isinstance(details, dict):
            return None, False

        metadata = make_metadata(details, fallback_tmdb_id)
        return metadata, True

    # Watchmode supports TMDB-format IDs (movie-123); this avoids a title search
    # for the large group of franchise rows that already have a reliable TMDB ID.
    if tmdb_id > 0:
        metadata, succeeded = await get_details(f"movie-{tmdb_id}", tmdb_id)
        if metadata:
            return metadata, True
        completed = succeeded

    if not query:
        return None, completed

    try:
        response = await _watchmode_get(
            "/search/",
            params={
                "search_field": "name",
                "search_value": query,
                "types": "movie,tv_movie,tv_miniseries,tv_special,short_film",
            },
            timeout=timeout,
        )
    except Exception as exc:
        logger.info("Watchmode synopsis title search failed for %r: %s", query, type(exc).__name__)
        return None, False

    if response is None or response.status_code >= 400:
        return None, False

    try:
        data = response.json()
    except (ValueError, TypeError):
        return None, False

    title_results = data.get("title_results", []) if isinstance(data, dict) else []
    target = _normalize_synopsis_match_title(query)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for candidate in title_results if isinstance(title_results, list) else []:
        if not isinstance(candidate, dict) or not candidate.get("id"):
            continue
        candidate_title = str(candidate.get("name") or candidate.get("title") or "").strip()
        if not candidate_title or _normalize_synopsis_match_title(candidate_title) != target:
            continue
        try:
            candidate_year = int(candidate.get("year") or 0)
        except (TypeError, ValueError):
            candidate_year = 0
        if year and candidate_year and candidate_year != year:
            continue
        year_rank = 0 if year and candidate_year == year else 1
        candidates.append((year_rank, candidate))

    candidates.sort(key=lambda pair: pair[0])
    for _, candidate in candidates[:3]:
        watchmode_id = str(candidate.get("id") or "").strip()
        if not watchmode_id:
            continue
        fallback_id = 0
        try:
            fallback_id = int(candidate.get("tmdb_id") or tmdb_id or 0)
        except (TypeError, ValueError):
            fallback_id = tmdb_id or 0
        metadata, succeeded = await get_details(watchmode_id, fallback_id)
        completed = completed or succeeded
        if metadata:
            return metadata, True

    # A successful search with no exact title/year match is a completed negative
    # result. Failed detail requests stay eligible for retry in the next worker run.
    return None, True if not candidates else completed


async def _lookup_catalogue_synopsis(
    title: str,
    year: int,
    tmdb_id: int = 0,
) -> dict[str, Any] | None:
    """Resolve a synopsis from TMDB first, then use Watchmode's plot overview."""
    query = re.sub(r"\s+", " ", title).strip()
    if not query and not tmdb_id:
        return None

    now = int(time.time())

    def details_to_metadata(
        details: dict[str, Any],
        resolved_id: int,
        fallback_title: str,
        fallback_year: int,
        fallback_row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = fallback_row or {}
        release_date = str(details.get("release_date") or fallback.get("release_date") or "")
        year_match = re.match(r"^(\d{4})", release_date)
        resolved_year = int(year_match.group(1)) if year_match else (fallback_year or None)
        poster_path = str(details.get("poster_path") or fallback.get("poster_path") or "").strip()
        backdrop_path = str(details.get("backdrop_path") or fallback.get("backdrop_path") or "").strip()
        overview = re.sub(r"\s+", " ", str(details.get("overview") or fallback.get("overview") or "")).strip()
        genres = details.get("genres") or fallback.get("genres") or []
        if genres and isinstance(genres[0], dict):
            genres = [str(genre.get("name") or "").strip() for genre in genres if isinstance(genre, dict)]
            genres = [genre for genre in genres if genre]
        return {
            "id": int(details.get("id") or resolved_id),
            "tmdbId": int(details.get("id") or resolved_id),
            "title": str(details.get("title") or fallback.get("title") or fallback_title).strip(),
            "year": resolved_year,
            "mediaType": "movie",
            "overview": overview,
            "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "",
            "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
            "tmdbUrl": f"https://www.themoviedb.org/movie/{int(details.get('id') or resolved_id)}",
            "releaseDate": release_date,
            "rating": float(details.get("vote_average") or fallback.get("vote_average") or 0),
            "genres": genres if isinstance(genres, list) else [],
            "synopsisSource": "TMDB" if overview else "",
            "synopsisCheckedAt": now,
            "synopsisLookupVersion": TMDB_SYNOPSIS_LOOKUP_VERSION,
        }

    async def watchmode_fallback() -> tuple[dict[str, Any] | None, bool]:
        return await _lookup_watchmode_catalogue_synopsis(query, year, tmdb_id)

    if tmdb_id > 0:
        successful_details_response = False
        details: dict[str, Any] = {}
        for language in ("en-US", "en"):
            try:
                response = await _tmdb_get_json(
                    f"movie/{tmdb_id}", {"language": language}, request_timeout=6.0
                )
            except Exception as exc:
                logger.info(
                    "Synopsis details lookup failed for %s (%s): %s",
                    tmdb_id, language, type(exc).__name__,
                )
                continue
            successful_details_response = True
            details = response
            if re.sub(r"\s+", " ", str(details.get("overview") or "")).strip():
                break

        if successful_details_response:
            metadata = details_to_metadata(details, tmdb_id, query or str(details.get("title") or ""), year)
            if metadata.get("overview"):
                return metadata
            watchmode_metadata, watchmode_completed = await watchmode_fallback()
            if watchmode_metadata:
                return watchmode_metadata
            if WATCHMODE_API_KEYS and not watchmode_completed:
                # Keep an unresolved record retryable if Watchmode failed transiently.
                metadata["synopsisLookupVersion"] = 0
            return metadata

        watchmode_metadata, _ = await watchmode_fallback()
        return watchmode_metadata

    if not query:
        return None

    params: dict[str, Any] = {
        "query": query, "include_adult": "false", "language": "en-US", "page": 1,
    }
    if 1870 <= year <= 2100:
        params["year"] = year
    try:
        result = await _tmdb_get_json("search/movie", params, request_timeout=6.0)
    except Exception as exc:
        logger.info("Synopsis cache lookup failed for %r (%s): %s", query, year, type(exc).__name__)
        watchmode_metadata, _ = await watchmode_fallback()
        return watchmode_metadata

    rows = [row for row in (result.get("results") or []) if isinstance(row, dict) and row.get("id")]
    target = _normalize_synopsis_match_title(query)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        candidate_title = str(row.get("title") or row.get("original_title") or "").strip()
        if _normalize_synopsis_match_title(candidate_title) != target:
            continue
        release_date = str(row.get("release_date") or "")
        try:
            candidate_year = int(release_date[:4])
        except (TypeError, ValueError):
            candidate_year = 0
        if year and candidate_year and candidate_year != year:
            continue
        candidates.append((0 if year and candidate_year == year else 1, -int(row.get("vote_count") or 0), row))

    if not candidates:
        watchmode_metadata, watchmode_completed = await watchmode_fallback()
        if watchmode_metadata:
            return watchmode_metadata
        if WATCHMODE_API_KEYS and not watchmode_completed:
            return None
        return {"synopsisCheckedAt": now, "synopsisLookupVersion": TMDB_SYNOPSIS_LOOKUP_VERSION}

    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    row = candidates[0][2]
    resolved_id = int(row["id"])
    details: dict[str, Any] = row
    overview = re.sub(r"\s+", " ", str(row.get("overview") or "")).strip()

    if not overview:
        successful_details_response = False
        for language in ("en-US", "en"):
            try:
                response = await _tmdb_get_json(
                    f"movie/{resolved_id}", {"language": language}, request_timeout=6.0
                )
            except Exception as exc:
                logger.info(
                    "Synopsis details lookup failed for %s (%s): %s",
                    resolved_id, language, type(exc).__name__,
                )
                continue
            details = response
            successful_details_response = True
            overview = re.sub(r"\s+", " ", str(details.get("overview") or "")).strip()
            if overview:
                break
        if not successful_details_response:
            watchmode_metadata, _ = await watchmode_fallback()
            return watchmode_metadata

    metadata = details_to_metadata(
        details,
        resolved_id,
        str(row.get("title") or row.get("original_title") or query).strip(),
        year,
        fallback_row=row,
    )
    if metadata.get("overview"):
        return metadata

    watchmode_metadata, watchmode_completed = await watchmode_fallback()
    if watchmode_metadata:
        return watchmode_metadata
    if WATCHMODE_API_KEYS and not watchmode_completed:
        metadata["synopsisLookupVersion"] = 0
    return metadata


async def _enrich_catalogue_synopses() -> None:
    """Fill synopsis metadata into every persistent movie-catalogue JSON without rebuilding torrent rows."""
    state = _CATALOGUE_SYNOPSIS_ENRICHMENT_STATE
    paths = _synopsis_cache_paths()
    state.update({
        "status": "running", "filesCompleted": 0, "filesTotal": len(paths),
        "titlesCompleted": 0, "titlesTotal": 0, "updatedRows": 0, "error": "",
    })
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        state.update({"status": "waiting_for_credentials", "error": "TMDB credentials are not configured"})
        return

    # First collect unique missing-synopsis titles across every cache. Repeated torrent
    # releases for the same film share one TMDB request and one metadata result.
    jobs: dict[tuple[str, int], tuple[str, int, int]] = {}
    file_rows: list[tuple[str, Path, dict[str, Any]]] = []
    now = int(time.time())
    for name, path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            state["filesCompleted"] += 1
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            state["filesCompleted"] += 1
            continue
        file_rows.append((name, path, payload))
        for item in payload["results"]:
            if not isinstance(item, dict) or str(item.get("overview") or "").strip():
                continue
            checked_at = item.get("synopsisCheckedAt")
            try:
                checked_at = int(checked_at or 0)
            except (TypeError, ValueError):
                checked_at = 0
            try:
                lookup_version = int(item.get("synopsisLookupVersion") or 0)
            except (TypeError, ValueError):
                lookup_version = 0
            if (
                lookup_version >= TMDB_SYNOPSIS_LOOKUP_VERSION
                and checked_at
                and now - checked_at < _CATALOGUE_SYNOPSIS_RECHECK_SECONDS
            ):
                continue
            identity = _synopsis_row_identity(item)
            normalized_title = identity[0]
            if not normalized_title:
                continue
            title = str(
                item.get("catalogueTitle") or item.get("marvelCatalogueTitle")
                or item.get("mediaTitle") or item.get("title") or ""
            ).strip()
            tmdb_id = _catalogue_metadata_tmdb_id(item)
            existing_job = jobs.get(identity)
            if existing_job is None or (not existing_job[2] and tmdb_id):
                jobs[identity] = (title, identity[1], tmdb_id)

    state["titlesTotal"] = len(jobs)
    metadata_by_identity: dict[tuple[str, int], dict[str, Any]] = {}
    # Conservative pacing avoids a burst against TMDB. Rate-limit responses are
    # respected by _tmdb_get_json and are not bypassed by trying other accounts.
    for identity, (title, year, tmdb_id) in jobs.items():
        metadata = await _lookup_catalogue_synopsis(title, year, tmdb_id)
        if metadata:
            metadata_by_identity[identity] = metadata
        state["titlesCompleted"] += 1
        if state["titlesCompleted"] % 25 == 0:
            logger.info("Catalogue synopsis enrichment: %d/%d titles processed",
                state["titlesCompleted"], state["titlesTotal"])
        await asyncio.sleep(0.25)

    # Re-read each file before writing, so a catalogue rebuilt during enrichment
    # keeps its fresh torrent rows. Only synopsis fields are merged into matching rows.
    for name, path, _initial_payload in file_rows:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            state["filesCompleted"] += 1
            continue
        if not isinstance(current, dict) or not isinstance(current.get("results"), list):
            state["filesCompleted"] += 1
            continue
        changed = False
        for item in current["results"]:
            if not isinstance(item, dict):
                continue
            identity = _synopsis_row_identity(item)
            metadata = metadata_by_identity.get(identity)
            if not metadata:
                continue
            # Never overwrite a synopsis that another process has already supplied.
            if not str(item.get("overview") or "").strip() and metadata.get("overview"):
                item["overview"] = metadata["overview"]
                changed = True
                state["updatedRows"] += 1
            for field in ("tmdbId", "tmdbUrl", "releaseDate", "rating", "synopsisSource"):
                if field in metadata and metadata[field] not in (None, "", 0) and not item.get(field):
                    item[field] = metadata[field]
                    changed = True
            try:
                old_checked_at = int(item.get("synopsisCheckedAt") or 0)
                new_checked_at = int(metadata.get("synopsisCheckedAt") or 0)
                old_lookup_version = int(item.get("synopsisLookupVersion") or 0)
                new_lookup_version = int(metadata.get("synopsisLookupVersion") or 0)
            except (TypeError, ValueError):
                old_checked_at = new_checked_at = old_lookup_version = new_lookup_version = 0
            if new_checked_at > old_checked_at:
                item["synopsisCheckedAt"] = new_checked_at
                changed = True
            if new_lookup_version > old_lookup_version:
                item["synopsisLookupVersion"] = new_lookup_version
                changed = True
        if changed:
            try:
                _write_synopsis_cache(path, current)
                logger.info("Saved synopsis enrichment for catalogue cache %s", path.name)
            except OSError as exc:
                logger.warning("Could not save synopsis-enriched cache %s: %s", path.name, exc)
        state["filesCompleted"] += 1

    state["status"] = "completed"
    logger.info(
        "Catalogue synopsis enrichment completed: %d unique titles checked, %d rows enriched across %d caches",
        state["titlesCompleted"], state["updatedRows"], state["filesCompleted"],
    )


_synopsis_enrichment_retry_task: asyncio.Task | None = None


async def _start_catalogue_synopsis_enrichment() -> None:
    global _catalogue_synopsis_enrichment_task, _synopsis_enrichment_retry_task
    # Let cache initialization finish first. Enrichment must never delay the API.
    await asyncio.sleep(10)
    if _catalogue_synopsis_enrichment_task is None or _catalogue_synopsis_enrichment_task.done():
        _catalogue_synopsis_enrichment_task = asyncio.create_task(_enrich_catalogue_synopses())
    if _synopsis_enrichment_retry_task is None or _synopsis_enrichment_retry_task.done():
        # Failed transient lookups remain unversioned and will be retried periodically.
        _synopsis_enrichment_retry_task = asyncio.create_task(_synopsis_enrichment_retry_loop())


@app.get("/api/catalogue/synopsis-enrichment")
async def api_catalogue_synopsis_enrichment_status():
    """Expose progress for the background synopsis fill across persistent catalogue JSON files."""
    return dict(_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE)


def _warm_tmdb_ott_memory_cache() -> None:
    """Load persisted OTT entries once so each cache miss does not reparse the entire JSON file."""
    try:
        disk = json.loads(TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        logger.warning("Could not warm in-memory OTT cache from %s", TMDB_OTT_CACHE_FILE, exc_info=True)
        return

    items = disk.get("items") if isinstance(disk, dict) else None
    if not isinstance(items, dict) or not items:
        logger.warning("Persistent OTT cache has no readable items at %s", TMDB_OTT_CACHE_FILE)
        return

    now_wall = time.time()
    now_monotonic = time.monotonic()
    warmed = 0
    invalid = 0
    for disk_key, row in items.items():
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            invalid += 1
            continue
        parts = str(disk_key).split(":", 1)
        if len(parts) != 2:
            invalid += 1
            continue
        try:
            movie_id = int(parts[0])
            saved_at = float(row.get("savedAt") or 0)
        except (TypeError, ValueError):
            invalid += 1
            continue
        region = parts[1].strip().upper()
        if movie_id < 1 or not re.fullmatch(r"[A-Z]{2}", region) or saved_at <= 0:
            invalid += 1
            continue

        cache_key = (movie_id, region)
        if cache_key in _tmdb_ott_cache:
            continue
        # Translate the saved wall-clock age to monotonic time. Expired entries
        # remain available as stale fallbacks instead of being marked fresh again.
        age_seconds = max(0.0, now_wall - saved_at)
        _tmdb_ott_cache[cache_key] = (now_monotonic - age_seconds, row["payload"])
        warmed += 1

    logger.info(
        "Warm-loaded persistent OTT cache into memory: %s entries warmed, %s valid disk entries, %s invalid rows",
        warmed, len(items) - invalid, invalid,
    )


def _initialize_catalogue_caches() -> None:
    # Seed bundled snapshots first; warm OTT entries into memory before serving requests,
    # then normalize the curated catalogue files on disk.
    _seed_bundled_runtime_caches()
    _warm_tmdb_ott_memory_cache()
    _seed_bundled_catalogue_caches()
    _read_marvel_catalogue()
    for key in ("marvel-hindi", "dc-live-action", "dc-live-action-hindi", "dc-animated"):
        _read_extra_catalogue(key)


app.router.add_event_handler("startup", _initialize_catalogue_caches)
app.router.add_event_handler("startup", _start_catalogue_synopsis_enrichment)
_extra_catalogue_tasks: dict[str, asyncio.Task | None] = {key: None for key in _EXTRA_CATALOGUES}
_extra_catalogue_retry_after: dict[str, float] = {}
_extra_catalogue_states: dict[str, dict[str, Any]] = {
    key: {"status": "idle", "completed": 0, "total": len(config.get("titles", config.get("queries", []))),
          "resultCount": 0, "movieCount": 0, "error": ""}
    for key, config in _EXTRA_CATALOGUES.items()
}


def _read_extra_catalogue(key: str) -> dict[str, Any] | None:
    return _memoized_file_read("extra:" + key, _EXTRA_CATALOGUE_PATHS[key], lambda: _read_extra_catalogue_uncached(key))


def _read_extra_catalogue_uncached(key: str) -> dict[str, Any] | None:
    path = _EXTRA_CATALOGUE_PATHS[key]
    config = _EXTRA_CATALOGUES[key]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload_version = payload.get("version")
        current_version = int(config["version"])
        # Keep a prior compatible JSON snapshot visible while a behaviour/version
        # refresh runs, so a temporary TMDB outage never blanks a working catalogue.
        is_current_version = payload_version == current_version
        is_older_compatible_version = (
            isinstance(payload_version, int) and payload_version < current_version
        )
        if not ((is_current_version or is_older_compatible_version) and isinstance(payload.get("results"), list) and payload["results"]):
            return None

        changed = False
        excluded_titles = {
            _normalize_title(value)
            for value in (config.get("exclude_titles") or [])
            if str(value or "").strip()
        }
        retained_results = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                changed = True
                continue
            canonical_title = str(
                item.get("catalogueTitle")
                or item.get("marvelCatalogueTitle")
                or item.get("mediaTitle")
                or item.get("title")
                or ""
            ).strip()
            if (
                _normalize_title(canonical_title) in excluded_titles
                or _catalogue_row_has_3d_quality(item)
                or _catalogue_row_is_excluded_franchise_title(item)
            ):
                changed = True
                continue
            retained_results.append(item)

        if not retained_results:
            return None
        if len(retained_results) != len(payload["results"]):
            payload = {
                **payload,
                "results": retained_results,
                "resultCount": len(retained_results),
                "movieCount": len({
                    _normalize_title(str(
                        item.get("catalogueTitle") or item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or ""
                    ))
                    for item in retained_results
                }),
            }
        sorted_results = _sort_cached_catalogue_results(payload["results"])
        if sorted_results != payload["results"]:
            changed = True
        payload["results"] = sorted_results
        payload["resultCount"] = len(sorted_results)
        payload["movieCount"] = len({
            _normalize_title(str(item.get("catalogueTitle") or item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or ""))
            for item in sorted_results
        })
        for item in payload["results"]:
            canonical_poster = _canonical_catalogue_poster_url(item)
            if canonical_poster and item.get("posterUrl") != canonical_poster:
                item["posterUrl"] = canonical_poster
                changed = True
        if changed:
            _write_extra_catalogue(key, payload)
        return payload
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _write_extra_catalogue(key: str, payload: dict[str, Any]) -> None:
    path = _EXTRA_CATALOGUE_PATHS[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=key + "-", suffix=".tmp",
                                         dir=str(path.parent), delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _extra_catalogue_is_stale(key: str, payload: dict[str, Any] | None) -> bool:
    if not payload:
        return True
    config = _EXTRA_CATALOGUES[key]
    try:
        if int(payload.get("version") or 0) != int(config["version"]):
            return True
    except (TypeError, ValueError):
        return True
    days = int(config["refresh_days"])
    if days >= 36500:
        return False
    try:
        built = datetime.fromisoformat(str(payload.get("builtAt") or "").replace("Z", "+00:00"))
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - built).total_seconds() >= days * 86400
    except (ValueError, TypeError):
        return True


def _extra_item_year(item: dict[str, Any], fallback: int = 0) -> int:
    try:
        if item.get("year"):
            return int(item["year"])
    except (TypeError, ValueError):
        pass
    match = re.search(r"\b((?:19|20)\d{2})\b", str(item.get("title") or ""))
    return int(match.group(1)) if match else fallback


def _extra_item_release_date(item: dict[str, Any]) -> str:
    """Use full release dates where known; fall back to January 1 of the year."""
    for field in ("releaseDate", "release_date", "releaseDateISO", "firstAirDate"):
        value = str(item.get(field) or "").strip()
        if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", value):
            try:
                datetime.strptime(value, "%Y-%m-%d")
                return value
            except ValueError:
                pass
    year = _extra_item_year(item)
    return f"{year:04d}-01-01" if year else ""


async def _build_extra_catalogue(key: str) -> None:
    if CACHE_ONLY_MODE:
        logger.info("CACHE_ONLY_MODE: skipped build for extra catalogue %s", key)
        return
    config = _EXTRA_CATALOGUES[key]
    state = _extra_catalogue_states[key]
    fixed_mode = config["mode"] == "fixed"
    jobs = list(config.get("titles", [])) if fixed_mode else []
    semaphore = asyncio.Semaphore(2)
    poster_semaphore = asyncio.Semaphore(2)
    state.update({"status": "building", "completed": 0, "total": len(jobs), "resultCount": 0, "error": ""})
    rows: dict[str, dict[str, Any]] = {}
    found_movies: set[str] = set()

    async def search_job(title: str, year: int) -> list[dict[str, Any]]:
        async with semaphore:
            query = f"{title} {year}".strip() if year else title
            query_suffix = str(config.get("query_suffix") or "").strip()
            if query_suffix:
                query = query + " " + query_suffix
            try:
                if fixed_mode:
                    if str(config.get("language") or "").strip().lower() == "hindi":
                        # Search the movie title without appending "Hindi dubbed".
                        # Exact-word constraints previously hid releases labelled only
                        # "Hin", "Dual Audio" or "Multi Audio".
                        try:
                            items = await asyncio.wait_for(
                                search_1337x(query, limit=50, allow_series_fallback=False),
                                timeout=18,
                            )
                        except Exception as exc:
                            logger.info("Catalogue %s primary Hindi search failed for %s: %s", key, query, exc)
                            items = []

                        matched_hindi = [
                            item for item in items
                            if isinstance(item, dict)
                            and _marvel_result_matches_title(item, title, year)
                            and _extra_result_matches_language(item, "hindi")
                        ]
                        if len(matched_hindi) < 3:
                            targeted_queries = ("Hindi", "dual audio", "multi audio")
                            targeted_calls = [
                                asyncio.wait_for(
                                    search_1337x_direct(
                                        f"{title} {language_term}",
                                        limit=50,
                                        pages=2,
                                        category="Movies",
                                        provider_query=f"{title} {language_term}",
                                    ),
                                    timeout=18,
                                )
                                for language_term in targeted_queries
                            ]
                            targeted_results = await asyncio.gather(*targeted_calls, return_exceptions=True)
                            for result in targeted_results:
                                if isinstance(result, list):
                                    items.extend(result)
                                elif isinstance(result, Exception):
                                    logger.info(
                                        "Catalogue %s targeted Hindi search failed for %s: %s",
                                        key, title, str(result) or type(result).__name__,
                                    )
                    else:
                        try:
                            items = await asyncio.wait_for(
                                search_1337x(query, limit=50, allow_series_fallback=False),
                                timeout=18,
                            )
                        except Exception as exc:
                            logger.info("Catalogue %s primary search failed for %s: %s", key, query, exc)
                            items = []
                        if len(items) < 2:
                            try:
                                fallback = await asyncio.wait_for(search_yts_movies(query, limit=20), timeout=9)
                                items = list(items or []) + list(fallback or [])
                            except Exception as exc:
                                logger.debug("Catalogue %s YTS fallback failed for %s: %s", key, query, exc)
                else:
                    # Search each known title directly in movie listings instead of
                    # passing it through the broad result filter (which requires year
                    # and all title tokens to occur literally in provider filenames).
                    year_match = re.search(r"\b((?:19|20)\d{2})\b", query)
                    expected_year = int(year_match.group(1)) if year_match else datetime.now(timezone.utc).year
                    provider_query = re.sub(r"\b((?:19|20)\d{2})\b", "", query).strip(" .-_")
                    calls = [
                        asyncio.wait_for(
                            search_1337x_direct(
                                query,
                                limit=50,
                                pages=2,
                                category="Movies",
                                provider_query=provider_query,
                            ),
                            timeout=20,
                        ),
                        asyncio.wait_for(
                            search_limetorrents(query, limit=30, pages=1),
                            timeout=14,
                        ),
                    ]
                    searched = await asyncio.gather(*calls, return_exceptions=True)
                    items = []
                    for value in searched:
                        if isinstance(value, list):
                            items.extend(value)
                        elif isinstance(value, Exception):
                            logger.info(
                                "Catalogue %s provider search failed for %s: %s: %s",
                                key, query, type(value).__name__, str(value) or "<no message>",
                            )

                accepted: list[dict[str, Any]] = []
                for original in items or []:
                    if not isinstance(original, dict):
                        continue
                    item = dict(original)
                    if _catalogue_row_has_3d_quality(item) or _catalogue_row_is_excluded_franchise_title(item):
                        continue
                    raw_title = str(item.get("title") or "").strip()
                    if not raw_title:
                        continue
                    if fixed_mode:
                        if (
                            not _marvel_result_matches_title(item, title, year)
                            or not _extra_result_matches_language(item, config.get("language"))
                        ):
                            continue
                        media_title, media_year = title, year
                    else:
                        normalized = _normalize_title(unescape(raw_title))
                        query_year_match = re.search(r"\b((?:19|20)\d{2})\b", query)
                        expected_year = int(query_year_match.group(1)) if query_year_match else datetime.now(timezone.utc).year
                        query_title = re.sub(r"\b((?:19|20)\d{2})\b", "", query).strip(" .-_")
                        query_tokens = [
                            token for token in _normalize_title(query_title).split()
                            if token not in {"the", "a", "an", "of", "and", "for", "to", "movie", "film"}
                        ]
                        result_tokens = set(normalized.split())
                        # Roman-numbered sequels are frequently indexed with Arabic numerals.
                        matched_tokens = sum(
                            1 if token in result_tokens or (
                                token in {"ii", "iii", "iv"} and
                                ({"ii": "2", "iii": "3", "iv": "4"}[token] in result_tokens)
                            ) else 0
                            for token in query_tokens
                        )
                        minimum_matches = max(1, len(query_tokens) - 1) if len(query_tokens) > 2 else len(query_tokens)
                        if query_tokens and matched_tokens < minimum_matches:
                            continue

                        category = str(item.get("category") or "").strip().lower()
                        if (
                            category in {"tv", "television", "series", "tv series", "television series"}
                            or "television series" in category
                            or re.search(r"\b(?:S\d{1,2}E\d{1,2}|season\s+\d+|complete\s+series|episode\s+\d+|web[\s.-]?series)\b", raw_title, re.I)
                        ):
                            continue

                        current_year = datetime.now(timezone.utc).year
                        title_years = [int(value) for value in re.findall(r"\b((?:19|20)\d{2})\b", raw_title)]
                        if title_years:
                            media_year = title_years[-1]
                        else:
                            media_year = _extra_item_year(item, fallback=expected_year)
                        if media_year < current_year - 1 or media_year > current_year:
                            continue

                        # The query title is the canonical display/group title for all of its torrent variants.
                        media_title = unescape(query_title)
                    try:
                        raw_size = item.get("size_bytes") or item.get("sizeBytes") or item.get("size") or 0
                        if isinstance(raw_size, (int, float)):
                            size = int(raw_size)
                        else:
                            size = parse_size(str(raw_size))
                        seeders = int(float(str(item.get("seeders") or 0).replace(",", "")))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if size < MIN_SEARCH_RESULT_SIZE_BYTES or size > MAX_SEARCH_RESULT_SIZE_BYTES or seeders <= 0:
                        continue
                    if not any(str(item.get(field) or "").strip() for field in ("magnetUrl", "downloadUrl", "sourceUrl", "infoUrl", "infoHash")):
                        continue
                    item["mediaTitle"] = media_title
                    item["year"] = media_year or None
                    if not item.get("posterUrl"):
                        item["posterUrl"] = _poster_url_for_release(f"{media_title} {media_year or ''}")
                    accepted.append(item)

                # Resolve one poster per movie group and reuse it across quality variants.
                grouped: dict[str, list[dict[str, Any]]] = {}
                for item in accepted:
                    group_key = _normalize_title(str(item.get("mediaTitle") or item.get("title") or ""))
                    grouped.setdefault(group_key, []).append(item)
                for group_key, variants in grouped.items():
                    if not variants:
                        continue
                    poster = str(variants[0].get("posterUrl") or "")
                    if not poster:
                        async with poster_semaphore:
                            try:
                                poster = await asyncio.wait_for(_poster_lookup(str(variants[0]["mediaTitle"]), str(variants[0].get("year") or "")), timeout=10)
                            except Exception:
                                poster = ""
                    for item in variants:
                        item["posterUrl"] = poster or _poster_url_for_release(str(item.get("mediaTitle") or item.get("title") or ""))
                        item["catalogueKey"] = key
                        item["catalogueTitle"] = str(item.get("mediaTitle") or item.get("title") or "")
                    found_movies.update(group_key + "|" + str(v.get("year") or "") for v in variants for group_key in [_normalize_title(str(v.get("mediaTitle") or v.get("title") or ""))])
                return accepted
            finally:
                state["completed"] = int(state.get("completed") or 0) + 1

    try:
        if not fixed_mode:
            jobs = await _discover_latest_catalogue_jobs(key)
            state.update({"status": "building", "completed": 0, "total": len(jobs), "resultCount": 0, "error": ""})
        tasks = [asyncio.create_task(search_job(title, year)) for title, year in jobs]
        for task in asyncio.as_completed(tasks):
            try:
                items = await task
            except Exception as exc:
                logger.info("Catalogue %s search task failed: %s", key, exc)
                items = []
            for item in items:
                digest = str(item.get("infoHash") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", digest):
                    digest = info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
                dedupe = "hash:" + digest if re.fullmatch(r"[0-9a-f]{40}", digest, re.I) else (
                    "title-size:" + _normalize_title(str(item.get("title") or "")) + "|" + str(item.get("size") or 0)
                )
                previous = rows.get(dedupe)
                if previous is None or (int(item.get("seeders") or 0), int(item.get("leechers") or 0)) > (int(previous.get("seeders") or 0), int(previous.get("leechers") or 0)):
                    rows[dedupe] = item
            state["resultCount"] = len(rows)
            state["movieCount"] = len(found_movies)

        # Keep cached torrent variants for titles still in the refreshed catalogue
        # when providers return a temporary miss; normalize older title aliases.
        previous_payload = _read_extra_catalogue(key)
        canonical_titles = {
            (re.sub(r"[^a-z0-9]+", "", title.casefold()), year): title
            for title, year in jobs
        }
        title_aliases = {
            ("battleofthesupersons", 2022): "Batman and Superman: Battle of the Super Sons",
        }
        if previous_payload:
            for old_item in previous_payload.get("results") or []:
                if not isinstance(old_item, dict):
                    continue
                old_title = str(old_item.get("catalogueTitle") or old_item.get("mediaTitle") or old_item.get("title") or "").strip()
                try:
                    old_year = int(old_item.get("year") or 0)
                except (TypeError, ValueError):
                    old_year = 0
                old_key = (re.sub(r"[^a-z0-9]+", "", old_title.casefold()), old_year)
                canonical = canonical_titles.get(old_key) or title_aliases.get(old_key)
                if not canonical or _catalogue_row_has_3d_quality(old_item) or _catalogue_row_is_excluded_franchise_title(old_item):
                    continue
                preserved = dict(old_item)
                preserved["mediaTitle"] = canonical
                preserved["catalogueTitle"] = canonical
                preserved["catalogueKey"] = key
                preserved["year"] = old_year
                digest = str(preserved.get("infoHash") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", digest):
                    digest = info_hash(str(preserved.get("magnetUrl") or preserved.get("downloadUrl") or ""))
                dedupe_key = "hash:" + digest if re.fullmatch(r"[0-9a-f]{40}", digest, re.I) else (
                    "title-size:" + _normalize_title(str(preserved.get("title") or "")) + "|" + str(preserved.get("size") or 0)
                )
                existing = rows.get(dedupe_key)
                score = (int(preserved.get("seeders") or 0), int(preserved.get("leechers") or 0))
                existing_score = (int(existing.get("seeders") or 0), int(existing.get("leechers") or 0)) if existing else (-1, -1)
                if existing is None or score > existing_score:
                    rows[dedupe_key] = preserved

        results = list(rows.values())
        movie_count = len({ _normalize_title(str(item.get("mediaTitle") or item.get("title") or "")) + "|" + str(item.get("year") or "") for item in results })
        if movie_count < int(config["minimum_movies"]):
            raise RuntimeError(f"Only {movie_count} titles found; keeping the previous catalogue.")
        payload = {"version": config["version"], "builtAt": datetime.now(timezone.utc).isoformat(),
                   "titlesQueried": len(jobs), "movieCount": movie_count, "resultCount": len(results), "results": results}
        # Merge saved metadata before sorting so full release dates are available
        # across Classics, Marvel, DC live-action, DC animated, and language variants.
        _merge_saved_catalogue_metadata(payload, _EXTRA_CATALOGUE_PATHS[key])
        results.sort(
            key=lambda item: (
                _extra_item_release_date(item),
                float(item.get("rating") or 0),
                int(item.get("seeders") or 0),
            ),
            reverse=True,
        )
        _write_extra_catalogue(key, payload)
        _extra_catalogue_retry_after.pop(key, None)
        state.update({"status": "ready", "completed": len(jobs), "total": len(jobs), "movieCount": movie_count,
                      "resultCount": len(results), "builtAt": payload["builtAt"], "error": ""})
        logger.info("Built %s catalogue: %d titles / %d torrent options", key, movie_count, len(results))
    except Exception as exc:
        _extra_catalogue_retry_after[key] = time.monotonic() + 300
        state.update({
            "status": "failed",
            "error": "Catalogue refresh failed; previous cached results were retained.",
            "debugError": f"{type(exc).__name__}: {str(exc)[:240]}",
        })
        logger.exception("Catalogue %s build failed: %s", key, exc)


async def _ensure_extra_catalogue_refresh(key: str) -> None:
    cached = _read_extra_catalogue(key)
    if _extra_catalogue_is_stale(key, cached):
        if time.monotonic() < _extra_catalogue_retry_after.get(key, 0.0):
            return
        task = _extra_catalogue_tasks.get(key)
        if task is None or task.done():
            _extra_catalogue_tasks[key] = asyncio.create_task(_build_extra_catalogue(key))


async def _extra_catalogue_monthly_scheduler() -> None:
    # Do not launch bulk provider traffic during startup. Catalogue endpoints
    # refresh the requested cache on demand; this is only the daily backstop.
    while True:
        await asyncio.sleep(24 * 60 * 60)
        for key, config in _EXTRA_CATALOGUES.items():
            if int(config["refresh_days"]) < 36500:
                await _ensure_extra_catalogue_refresh(key)



def _tmdb_cache_disk_key(cache_key: tuple[str, int]) -> str:
    return cache_key[0] + ":" + str(cache_key[1])


def _read_tmdb_persistent_cache(cache_key: tuple[str, int]) -> tuple[float, dict[str, Any]] | None:
    try:
        payload = json.loads(TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
        row = (payload.get("catalogues") or {}).get(_tmdb_cache_disk_key(cache_key))
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return None
        saved_at = float(row.get("savedAt") or 0)
        if saved_at <= 0:
            return None
        return saved_at, row["payload"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_tmdb_persistent_cache(cache_key: tuple[str, int], payload: dict[str, Any]) -> None:
    try:
        TMDB_CATALOGUE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            disk = json.loads(TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(disk, dict):
                disk = {}
        except (OSError, ValueError, TypeError):
            disk = {}
        catalogues = disk.get("catalogues")
        if not isinstance(catalogues, dict):
            catalogues = {}
        catalogues[_tmdb_cache_disk_key(cache_key)] = {
            "savedAt": time.time(),
            "payload": payload,
        }
        # Keep the file bounded if an unusual client requests many pages.
        if len(catalogues) > 300:
            ordered = sorted(
                catalogues.items(),
                key=lambda pair: float((pair[1] or {}).get("savedAt") or 0),
                reverse=True,
            )
            catalogues = dict(ordered[:300])
        disk["catalogues"] = catalogues
        temp_path = TMDB_CATALOGUE_CACHE_FILE.with_name(
            TMDB_CATALOGUE_CACHE_FILE.name + "." + uuid.uuid4().hex + ".tmp"
        )
        temp_path.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, TMDB_CATALOGUE_CACHE_FILE)
    except (OSError, ValueError, TypeError) as exc:
        # Caching is best-effort; it must never turn a successful API response into a failure.
        logger.info("Could not write TMDB persistent cache: %s", exc)


# The seven franchise snapshots above are separate from the dynamic Popular/Trending/
# Latest catalogues. Those catalogues are persisted in TMDB_CATALOGUE_CACHE_FILE, so
# backfill their missing overviews there too; otherwise the movie cards never see them.
_TMDB_CATALOGUE_SYNOPSIS_RECHECK_SECONDS = 30 * 24 * 60 * 60
_TMDB_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE: dict[str, Any] = {
    "status": "idle", "entriesCompleted": 0, "entriesTotal": 0,
    "titlesCompleted": 0, "titlesTotal": 0, "updatedRows": 0, "error": "",
}
_tmdb_catalogue_synopsis_enrichment_task: asyncio.Task | None = None
_TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED = False


def _tmdb_payload_needs_synopsis(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return False
    now = int(time.time())
    for item in payload["results"]:
        if not isinstance(item, dict) or str(item.get("overview") or "").strip():
            continue
        try:
            checked_at = int(item.get("synopsisCheckedAt") or 0)
            lookup_version = int(item.get("synopsisLookupVersion") or 0)
        except (TypeError, ValueError):
            checked_at = 0
            lookup_version = 0
        if (
            lookup_version < TMDB_SYNOPSIS_LOOKUP_VERSION
            or not checked_at
            or now - checked_at >= _TMDB_CATALOGUE_SYNOPSIS_RECHECK_SECONDS
        ):
            return True
    return False


async def _lookup_tmdb_catalogue_synopsis_by_id(item: dict[str, Any]) -> dict[str, Any] | None:
    """Use TMDB movie details by ID; list/discover results may omit an overview."""
    try:
        tmdb_id = int(item.get("tmdbId") or item.get("id") or 0)
    except (TypeError, ValueError):
        tmdb_id = 0
    if tmdb_id < 1:
        return None

    successful_response = False
    for language in ("en-US", "en"):
        try:
            details = await _tmdb_get_json(
                f"movie/{tmdb_id}", {"language": language}, request_timeout=6.0
            )
            successful_response = True
        except Exception as exc:
            logger.info("TMDB details synopsis lookup failed for ID %s (%s): %s",
                tmdb_id, language, type(exc).__name__)
            continue

        overview = re.sub(r"\s+", " ", str(details.get("overview") or "")).strip()
        if overview:
            return {
                "overview": overview,
                "tmdbId": tmdb_id,
                "tmdbUrl": f"https://www.themoviedb.org/movie/{tmdb_id}",
                "synopsisCheckedAt": int(time.time()),
                "synopsisLookupVersion": TMDB_SYNOPSIS_LOOKUP_VERSION,
            }

    # Mark only a successfully checked empty response. Transport failures stay
    # unmarked so the scheduled worker retries them.
    if successful_response:
        return {
            "tmdbId": tmdb_id,
            "tmdbUrl": f"https://www.themoviedb.org/movie/{tmdb_id}",
            "synopsisCheckedAt": int(time.time()),
            "synopsisLookupVersion": TMDB_SYNOPSIS_LOOKUP_VERSION,
        }
    return None


async def _enrich_tmdb_persistent_catalogue_synopses() -> None:
    global _tmdb_catalogue_synopsis_enrichment_task, _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED
    state = _TMDB_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        state.update({"status": "waiting_for_credentials",
            "error": "TMDB credentials are not configured"})
        return

    state.update({
        "status": "running", "entriesCompleted": 0, "entriesTotal": 0,
        "titlesCompleted": 0, "titlesTotal": 0, "updatedRows": 0, "error": "",
    })
    try:
        try:
            disk = json.loads(TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            disk = {}
        catalogues = disk.get("catalogues", {}) if isinstance(disk, dict) else {}
        if not isinstance(catalogues, dict):
            catalogues = {}

        cache_entries: list[tuple[tuple[str, int], dict[str, Any]]] = []
        jobs: dict[int, dict[str, Any]] = {}
        now = int(time.time())
        for disk_key, envelope in catalogues.items():
            if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
                continue
            try:
                variant_and_page, page_text = str(disk_key).rsplit(":", 1)
                page = int(page_text)
            except (ValueError, TypeError):
                continue
            cache_key = (variant_and_page, page)
            payload = envelope["payload"]
            if not isinstance(payload.get("results"), list):
                continue
            cache_entries.append((cache_key, payload))
            for item in payload["results"]:
                if not isinstance(item, dict) or str(item.get("overview") or "").strip():
                    continue
                try:
                    checked_at = int(item.get("synopsisCheckedAt") or 0)
                    tmdb_id = int(item.get("tmdbId") or item.get("id") or 0)
                except (TypeError, ValueError):
                    checked_at, tmdb_id = 0, 0
                try:
                    lookup_version = int(item.get("synopsisLookupVersion") or 0)
                except (TypeError, ValueError):
                    lookup_version = 0
                if (
                    lookup_version >= TMDB_SYNOPSIS_LOOKUP_VERSION
                    and checked_at
                    and now - checked_at < _TMDB_CATALOGUE_SYNOPSIS_RECHECK_SECONDS
                ):
                    continue
                if tmdb_id > 0:
                    jobs.setdefault(tmdb_id, item)

        state["entriesTotal"] = len(cache_entries)
        state["titlesTotal"] = len(jobs)
        metadata_by_id: dict[int, dict[str, Any]] = {}
        for tmdb_id, item in jobs.items():
            metadata = await _lookup_tmdb_catalogue_synopsis_by_id(item)
            if metadata:
                metadata_by_id[tmdb_id] = metadata
            state["titlesCompleted"] += 1
            if state["titlesCompleted"] % 25 == 0:
                logger.info("TMDB persistent catalogue synopsis enrichment: %d/%d titles",
                    state["titlesCompleted"], state["titlesTotal"])
            await asyncio.sleep(0.2)

        # Re-read every entry before merging, preserving any newer catalogue rows
        # written while the metadata lookups were running.
        for cache_key, initial_payload in cache_entries:
            current_entry = _read_tmdb_persistent_cache(cache_key)
            payload = current_entry[1] if current_entry else initial_payload
            changed = False
            rows = payload.get("results", []) if isinstance(payload, dict) else []
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict) or str(item.get("overview") or "").strip():
                    continue
                try:
                    tmdb_id = int(item.get("tmdbId") or item.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                metadata = metadata_by_id.get(tmdb_id)
                if not metadata:
                    continue
                if metadata.get("overview") and not str(item.get("overview") or "").strip():
                    item["overview"] = metadata["overview"]
                    state["updatedRows"] += 1
                    changed = True
                for field in ("tmdbId", "tmdbUrl"):
                    if metadata.get(field) and not item.get(field):
                        item[field] = metadata[field]
                        changed = True
                try:
                    old_lookup_version = int(item.get("synopsisLookupVersion") or 0)
                    new_lookup_version = int(metadata.get("synopsisLookupVersion") or 0)
                except (TypeError, ValueError):
                    old_lookup_version = new_lookup_version = 0
                if new_lookup_version > old_lookup_version:
                    item["synopsisLookupVersion"] = new_lookup_version
                    changed = True
                try:
                    old_checked_at = int(item.get("synopsisCheckedAt") or 0)
                    new_checked_at = int(metadata.get("synopsisCheckedAt") or 0)
                except (TypeError, ValueError):
                    old_checked_at, new_checked_at = 0, 0
                if new_checked_at > old_checked_at:
                    item["synopsisCheckedAt"] = new_checked_at
                    changed = True
            if changed:
                _write_tmdb_persistent_cache(cache_key, payload)
                _tmdb_movie_catalogue_cache[cache_key] = (time.monotonic(), payload)
            state["entriesCompleted"] += 1

        state.update({"status": "completed", "error": ""})
        logger.info("TMDB persistent catalogue synopsis enrichment completed: %d titles, %d rows updated",
            state["titlesCompleted"], state["updatedRows"])
    except Exception as exc:
        logger.exception("TMDB persistent catalogue synopsis enrichment failed")
        state.update({"status": "failed", "error": type(exc).__name__ + ": " + str(exc)[:300]})
    finally:
        # If a new cache page arrived while the startup sweep was running, do one
        # more pass after this task releases its slot so that page is not missed.
        if _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED:
            _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED = False
            _tmdb_catalogue_synopsis_enrichment_task = asyncio.create_task(
                _enrich_tmdb_persistent_catalogue_synopses()
            )


def _schedule_tmdb_catalogue_synopsis_enrichment(payload: dict[str, Any] | None = None) -> None:
    global _tmdb_catalogue_synopsis_enrichment_task, _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED
    if payload is not None and not _tmdb_payload_needs_synopsis(payload):
        return
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        _TMDB_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE.update({
            "status": "waiting_for_credentials", "error": "TMDB credentials are not configured"})
        return
    if (_tmdb_catalogue_synopsis_enrichment_task is not None
            and not _tmdb_catalogue_synopsis_enrichment_task.done()):
        _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED = True
        return
    _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED = False
    _tmdb_catalogue_synopsis_enrichment_task = asyncio.create_task(
        _enrich_tmdb_persistent_catalogue_synopses()
    )


async def _synopsis_enrichment_retry_loop() -> None:
    """Retry only unresolved synopsis rows on a schedule; completed cache hits stay on disk."""
    global _catalogue_synopsis_enrichment_task
    while True:
        await asyncio.sleep(SYNOPSIS_CACHE_RETRY_INTERVAL_SECONDS)
        if _catalogue_synopsis_enrichment_task is None or _catalogue_synopsis_enrichment_task.done():
            _catalogue_synopsis_enrichment_task = asyncio.create_task(_enrich_catalogue_synopses())
        _schedule_tmdb_catalogue_synopsis_enrichment()
        tasks = [
            task for task in (
                _catalogue_synopsis_enrichment_task,
                _tmdb_catalogue_synopsis_enrichment_task,
            )
            if task is not None and not task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _start_tmdb_catalogue_synopsis_enrichment() -> None:
    # Registered after cache initialization; launch the backfill without holding
    # application startup open while hundreds of TMDB details are fetched.
    _schedule_tmdb_catalogue_synopsis_enrichment()

# Register only after the callback above exists; earlier startup hooks are defined
# further up the module and must not reference this function prematurely.
app.router.add_event_handler("startup", _start_tmdb_catalogue_synopsis_enrichment)


@app.get("/api/movies/catalogue-cache/synopsis-enrichment")
async def api_tmdb_catalogue_synopsis_enrichment_status():
    """Return progress for TMDB synopsis enrichment in the persistent catalogue JSON."""
    return dict(_TMDB_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE)


def _tmdb_ott_disk_key(cache_key: tuple[int, str]) -> str:
    # Version the persistent key so deployments can deliberately refresh stale
    # provider listings without deleting the historical cache file by hand.
    return "v2:" + str(cache_key[0]) + ":" + cache_key[1]


def _tmdb_ott_ttl(payload: Any) -> float:
    """Providers found -> long cache; nothing found -> short cache so it self-heals."""
    if isinstance(payload, dict) and payload.get("incomplete"):
        # A provider (e.g. Streaming Availability) errored or hit its quota: retry soon.
        return TMDB_OTT_INCOMPLETE_CACHE_SECONDS
    if isinstance(payload, dict) and any(
        payload.get(key) for key in ("streamingProviders", "rentProviders", "buyProviders")
    ):
        return TMDB_OTT_CACHE_SECONDS
    return TMDB_OTT_MISSING_CACHE_SECONDS


def _read_tmdb_ott_persistent_cache(cache_key: tuple[int, str]) -> tuple[float, dict[str, Any]] | None:
    try:
        payload = json.loads(TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
        row = (payload.get("items") or {}).get(_tmdb_ott_disk_key(cache_key))
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return None
        saved_at = float(row.get("savedAt") or 0)
        if saved_at <= 0:
            return None
        return saved_at, row["payload"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_tmdb_ott_persistent_cache(cache_key: tuple[int, str], payload: dict[str, Any]) -> None:
    try:
        TMDB_OTT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            disk = json.loads(TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(disk, dict):
                disk = {}
        except (OSError, ValueError, TypeError):
            disk = {}
        items = disk.get("items")
        if not isinstance(items, dict):
            items = {}
        items[_tmdb_ott_disk_key(cache_key)] = {
            "savedAt": time.time(),
            "payload": payload,
        }
        # Keep every saved OTT lookup permanently. Evicting old entries would
        # cause repeat provider lookups when a previously viewed film is opened.
        disk["items"] = items
        temp_path = TMDB_OTT_CACHE_FILE.with_name(
            TMDB_OTT_CACHE_FILE.name + "." + uuid.uuid4().hex + ".tmp"
        )
        temp_path.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, TMDB_OTT_CACHE_FILE)
    except (OSError, ValueError, TypeError) as exc:
        # A cache write failure must not hide otherwise usable availability data.
        logger.info("Could not write TMDB OTT cache: %s", exc)


def _tmdb_watch_provider_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("provider_name") or "").strip()
        provider_id = item.get("provider_id")
        if not name:
            continue
        identity = str(provider_id if provider_id is not None else name.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        logo_path = str(item.get("logo_path") or "").strip()
        rows.append({
            "providerId": provider_id,
            "name": name,
            "logoUrl": "https://image.tmdb.org/t/p/w92" + logo_path if logo_path else "",
        })
    return rows


async def _resolve_watchmode_title_id(movie_id: int) -> int | None:
    """Resolve and memoize the native Watchmode title ID for a TMDB movie ID."""
    if movie_id in _WATCHMODE_RESOLVED_TITLE_IDS:
        return _WATCHMODE_RESOLVED_TITLE_IDS[movie_id]
    if not WATCHMODE_API_KEYS:
        return None
    try:
        response = await _watchmode_get(
            "/search/",
            params={"search_field": "tmdb_movie_id", "search_value": str(movie_id), "types": "movie"},
            timeout=httpx.Timeout(10.0, connect=4.0),
        )
        if response is None or response.status_code >= 400:
            status = response.status_code if response is not None else "no response"
            logger.info("Watchmode ID resolution failed for TMDB movie %s (%s)", movie_id, status)
            return None
        payload = response.json()
        results = payload.get("title_results") if isinstance(payload, dict) else None
        matches: set[int] = set()
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                try:
                    result_tmdb_id = int(item.get("tmdb_id") or 0)
                    watchmode_id = int(item.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if (
                    result_tmdb_id == movie_id
                    and str(item.get("type") or "").casefold() == "movie"
                    and watchmode_id > 0
                ):
                    matches.add(watchmode_id)
        resolved = next(iter(matches)) if len(matches) == 1 else None
        _WATCHMODE_RESOLVED_TITLE_IDS[movie_id] = resolved
        if resolved is None:
            logger.info("Watchmode has no unambiguous native title ID for TMDB movie %s", movie_id)
        return resolved
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Watchmode ID resolution failed for TMDB movie %s: %s", movie_id, type(exc).__name__)
        return None


async def _watchmode_movie_sources(movie_id: int, region: str) -> list[dict[str, Any]]:
    """Look up sources by TMDB ID, falling back to Watchmode's native title ID."""
    if not WATCHMODE_API_KEYS:
        return []
    try:
        response = await _watchmode_get(
            f"/title/movie-{movie_id}/sources/",
            params={"regions": region},
            timeout=httpx.Timeout(10.0, connect=4.0),
        )
        if response is None:
            return []
        if response.status_code < 400:
            payload = response.json()
            if not isinstance(payload, list):
                logger.info("Watchmode returned an unexpected source payload for TMDB movie %s", movie_id)
                return []
            rows = [item for item in payload if isinstance(item, dict)]
            if rows:
                return rows
            # A valid empty response can mean the TMDB-formatted ID did not map
            # to a Watchmode record. Resolve the native ID before concluding empty.
        elif response.status_code != 404:
            logger.info("Watchmode source lookup failed for TMDB movie %s (%s)", movie_id, response.status_code)
            return []

        watchmode_id = await _resolve_watchmode_title_id(movie_id)
        if watchmode_id is None:
            return []
        fallback = await _watchmode_get(
            f"/title/{watchmode_id}/sources/",
            params={"regions": region},
            timeout=httpx.Timeout(10.0, connect=4.0),
        )
        if fallback is None or fallback.status_code >= 400:
            status = fallback.status_code if fallback is not None else "no response"
            logger.info("Watchmode native-ID source lookup failed for TMDB movie %s (%s)", movie_id, status)
            return []
        payload = fallback.json()
        if not isinstance(payload, list):
            logger.info("Watchmode returned an unexpected native-ID source payload for TMDB movie %s", movie_id)
            return []
        return [item for item in payload if isinstance(item, dict)]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Watchmode source lookup failed for movie %s in %s: %s", movie_id, region, type(exc).__name__)
        return []


def _watchmode_provider_rows(rows: list[dict[str, Any]], accepted_types: set[str]) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        name = str(item.get("name") or item.get("source_name") or "").strip()
        kind = str(item.get("type") or item.get("source_type") or "").strip().lower()
        if not name or kind not in accepted_types:
            continue
        provider_id = item.get("source_id", item.get("id"))
        identity = str(provider_id if provider_id is not None else name.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        logo = str(item.get("logo_url") or item.get("logo") or "").strip()
        providers.append({
            "providerId": provider_id,
            "name": name,
            "logoUrl": logo,
            "webUrl": str(item.get("web_url") or item.get("url") or "").strip(),
        })
    return providers


async def _streaming_availability_movie_sources(movie_id: int, region: str) -> tuple[list[dict[str, Any]], bool]:
    """Return normalized provider rows and whether the lookup failed."""
    if not STREAMING_AVAILABILITY_API_KEY:
        return [], False
    try:
        async with _InstrumentedAsyncClient(
            timeout=httpx.Timeout(10.0, connect=4.0),
            follow_redirects=True,
            headers={"Accept": "application/json", "X-API-Key": STREAMING_AVAILABILITY_API_KEY},
        ) as client:
            response = await client.get(
                f"{STREAMING_AVAILABILITY_API_BASE}/shows/movie/{movie_id}",
                params={"country": region.lower(), "series_granularity": "show"},
            )
        if response.status_code >= 300:
            logger.info("Streaming Availability API returned HTTP %s for movie %s", response.status_code, movie_id)
            return [], True
        payload = response.json()
        if not isinstance(payload, dict):
            return [], True
        options_by_country = payload.get("streamingOptions")
        if not isinstance(options_by_country, dict):
            return [], True
        options = options_by_country.get(region.lower()) or []
        if not isinstance(options, list):
            return [], True
        normalized: list[dict[str, Any]] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            service = option.get("service") if isinstance(option.get("service"), dict) else {}
            name = str(service.get("name") or "").strip()
            if not name:
                continue
            kind = str(option.get("type") or "").lower()
            if kind not in {"subscription", "free", "addon", "rent", "buy"}:
                continue
            images = service.get("imageSet") if isinstance(service.get("imageSet"), dict) else {}
            normalized.append({
                "providerId": service.get("id") or name.casefold(),
                "name": name,
                "logoUrl": str(images.get("darkThemeImage") or images.get("lightThemeImage") or ""),
                "webUrl": str(option.get("link") or service.get("homePage") or "").strip(),
                "type": kind,
            })
        return normalized, False
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Streaming Availability API lookup failed for movie %s: %s", movie_id, type(exc).__name__)
        return [], True


def _streaming_availability_provider_rows(rows: list[dict[str, Any]], accepted_types: set[str]) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if str(item.get("type") or "").lower() not in accepted_types:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        identity = str(item.get("providerId") or name.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        providers.append({
            "providerId": item.get("providerId"),
            "name": name,
            "logoUrl": str(item.get("logoUrl") or ""),
            "webUrl": str(item.get("webUrl") or ""),
        })
    return providers


async def _tmdb_fetch_movie_ott_availability(movie_id: int, region: str) -> dict[str, Any]:
    # Render serves OTT availability from the bundled/persistent cache only.
    # Never spend provider API quota refreshing streaming information at runtime.
    if CACHE_ONLY_MODE:
        cached = _tmdb_ott_cache.get((movie_id, region.upper()))
        if cached and isinstance(cached[1], dict):
            _record_cache_event("ott", "cache_only_memory_hit")
            return cached[1]
        persistent = _read_tmdb_ott_persistent_cache((movie_id, region.upper()))
        if persistent and isinstance(persistent[1], dict):
            _record_cache_event("ott", "cache_only_persistent_hit")
            return persistent[1]
        _record_cache_event("ott", "cache_only_miss_blocked")
        raise HTTPException(
            status_code=503,
            detail="OTT availability is not present in the local cache; live provider lookups are disabled.",
        )

    _record_cache_event("ott", "live_provider_lookup")
    # Prefer Watchmode for legal platform availability when configured. Keep TMDB
    # as a fallback and as the source of regional release-date metadata.
    watchmode_rows, release_result = await asyncio.gather(
        _watchmode_movie_sources(movie_id, region),
        _tmdb_get_json(f"movie/{movie_id}/release_dates"),
        return_exceptions=True,
    )
    if isinstance(watchmode_rows, Exception):
        watchmode_rows = []
    if isinstance(release_result, Exception):
        logger.info("Could not load digital release dates for TMDB movie %s: %s", movie_id, release_result)
        release_payload: dict[str, Any] = {}
    else:
        release_payload = release_result if isinstance(release_result, dict) else {}

    watchmode_streaming = _watchmode_provider_rows(watchmode_rows, {"sub", "free", "ads", "tve", "subscription"})
    watchmode_rent = _watchmode_provider_rows(watchmode_rows, {"rent", "rental"})
    watchmode_buy = _watchmode_provider_rows(watchmode_rows, {"buy", "purchase"})

    availability_rows: list[dict[str, Any]] = []
    availability_failed = False
    if not (watchmode_streaming or watchmode_rent or watchmode_buy):
        availability_rows, availability_failed = await _streaming_availability_movie_sources(movie_id, region)
    availability_streaming = _streaming_availability_provider_rows(availability_rows, {"subscription", "free", "addon"})
    availability_rent = _streaming_availability_provider_rows(availability_rows, {"rent"})
    availability_buy = _streaming_availability_provider_rows(availability_rows, {"buy"})

    # TMDB/JustWatch is the final fallback when neither Watchmode nor Streaming Availability has providers.
    tmdb_provider_result: dict[str, Any] = {}
    tmdb_provider_lookup_failed = False
    if not (watchmode_streaming or watchmode_rent or watchmode_buy or availability_streaming or availability_rent or availability_buy):
        try:
            result = await _tmdb_get_json(f"movie/{movie_id}/watch/providers")
            if isinstance(result, dict):
                tmdb_provider_result = result
            else:
                tmdb_provider_lookup_failed = True
        except Exception as exc:
            tmdb_provider_lookup_failed = True
            logger.info("TMDB provider fallback failed for movie %s: %s", movie_id, exc)

    # Don't cache provider outages as an empty availability result.
    if tmdb_provider_lookup_failed and availability_failed and not (watchmode_streaming or watchmode_rent or watchmode_buy):
        raise HTTPException(status_code=503, detail="Streaming availability could not be verified because the availability providers are temporarily unavailable.")
    if tmdb_provider_lookup_failed and not (watchmode_streaming or watchmode_rent or watchmode_buy or availability_streaming or availability_rent or availability_buy):
        raise HTTPException(status_code=503, detail="Streaming availability could not be verified because Watchmode and TMDB fallback are temporarily unavailable.")

    all_regions = (tmdb_provider_result.get("results") or {}) if isinstance(tmdb_provider_result, dict) else {}
    region_payload = all_regions.get(region) or {}
    streaming = watchmode_streaming or availability_streaming or (
        _tmdb_watch_provider_rows(region_payload.get("flatrate"))
        + _tmdb_watch_provider_rows(region_payload.get("free"))
        + _tmdb_watch_provider_rows(region_payload.get("ads"))
    )
    rent = watchmode_rent or availability_rent or _tmdb_watch_provider_rows(region_payload.get("rent"))
    buy = watchmode_buy or availability_buy or _tmdb_watch_provider_rows(region_payload.get("buy"))
    availability_source = (
        "Watchmode" if watchmode_streaming or watchmode_rent or watchmode_buy
        else "Streaming Availability API" if availability_streaming or availability_rent or availability_buy
        else "TMDB"
    )

    def merge_providers(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            for provider in group:
                identity = str(provider.get("providerId") or provider.get("name", "").casefold())
                if identity not in seen:
                    seen.add(identity)
                    merged.append(provider)
        return merged

    streaming = merge_providers([streaming])
    rent = merge_providers([rent])
    buy = merge_providers([buy])

    digital_dates: list[str] = []
    theatrical_dates: list[str] = []
    for region_release in release_payload.get("results") or []:
        if not isinstance(region_release, dict) or str(region_release.get("iso_3166_1") or "").upper() != region:
            continue
        for release in region_release.get("release_dates") or []:
            if not isinstance(release, dict):
                continue
            release_type = str(release.get("type") or "")
            if release_type not in {"2", "3", "4"}:
                continue
            day = str(release.get("release_date") or "").strip()[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                try:
                    datetime.strptime(day, "%Y-%m-%d")
                    if release_type == "4":
                        digital_dates.append(day)
                    else:
                        # Types 2 and 3 are limited and wide theatrical releases.
                        theatrical_dates.append(day)
                except ValueError:
                    continue

    digital_release_date = min(digital_dates) if digital_dates else None
    theatrical_release_date = min(theatrical_dates) if theatrical_dates else None
    today = datetime.now(timezone.utc).date().isoformat()
    if streaming:
        status = "streaming"
    elif rent or buy:
        status = "rent_buy"
    elif digital_release_date and digital_release_date > today:
        status = "digital_scheduled"
    elif digital_release_date:
        # A listed digital release date does not guarantee subscription streaming.
        status = "digital_release_known"
    else:
        status = "not_confirmed"

    provider_link = str(region_payload.get("link") or "").strip()
    if not provider_link:
        provider_link = f"https://www.themoviedb.org/movie/{movie_id}/watch?locale={region}"

    return {
        "movieId": movie_id,
        "region": region,
        "status": status,
        "streamingProviders": streaming,
        "rentProviders": rent,
        "buyProviders": buy,
        "theatricalReleaseDate": theatrical_release_date,
        "digitalReleaseDate": digital_release_date,
        "providerLink": provider_link,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        # True when no providers were found AND an upstream lookup failed, so "nothing
        # found" may just be a provider error/quota; it is re-checked within minutes.
        "incomplete": bool(
            not (streaming or rent or buy) and (availability_failed or tmdb_provider_lookup_failed)
        ),
        "source": availability_source,
        "providerAttribution": ("Streaming availability data powered by Watchmode." if availability_source == "Watchmode" else "Streaming availability data powered by Streaming Availability API." if availability_source == "Streaming Availability API" else "Streaming availability data powered by JustWatch via TMDB."),
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }

async def _tmdb_get_json(path: str, params: dict[str, Any] | None = None, *, request_timeout: float = 12.0) -> dict[str, Any]:
    if CACHE_ONLY_MODE:
        raise HTTPException(status_code=503, detail="Cache-only mode: live TMDB lookups are disabled.")
    credentials = [("bearer", token) for token in TMDB_READ_ACCESS_TOKENS] + [("api_key", key) for key in TMDB_API_KEYS]
    if not credentials:
        raise HTTPException(status_code=503, detail="TMDB credentials are not configured. Set TMDB_API_KEYS and/or TMDB_READ_ACCESS_TOKENS.")
    provider = "tmdb"
    start = _PROVIDER_KEY_CURSORS.get(provider, 0) % len(credentials)
    ordered = credentials[start:] + credentials[:start]
    _PROVIDER_KEY_CURSORS[provider] = (start + 1) % len(credentials)
    query = dict(params or {})
    last_response = None
    transient_failed: list[tuple[str, str]] = []
    last_error = None

    async def attempt(kind: str, credential: str) -> dict[str, Any] | None:
        nonlocal last_response, last_error
        fingerprint = kind + ":" + credential
        identity = _provider_key_id(provider, fingerprint)
        if _PROVIDER_KEY_COOLDOWNS.get(identity, 0.0) > time.time():
            return None
        headers = {"Accept": "application/json"}
        request_params = dict(query)
        if kind == "bearer":
            headers["Authorization"] = "Bearer " + credential
        else:
            request_params["api_key"] = credential
        try:
            global _TMDB_HTTP_CLIENT
            if _TMDB_HTTP_CLIENT is None or _TMDB_HTTP_CLIENT.is_closed:
                _TMDB_HTTP_CLIENT = _InstrumentedAsyncClient(
                    timeout=httpx.Timeout(12.0, connect=4.0),
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                    limits=httpx.Limits(
                        max_connections=8,
                        max_keepalive_connections=4,
                        keepalive_expiry=30.0,
                    ),
                )
            async with _TMDB_REQUEST_SEMAPHORE:
                response = await _TMDB_HTTP_CLIENT.get(
                    f"{TMDB_API_BASE}/{path.lstrip('/')}",
                    params=request_params,
                    headers=headers,
                    timeout=httpx.Timeout(
                        request_timeout,
                        connect=min(4.0, request_timeout),
                    ),
                )
            last_response = response
            if 200 <= response.status_code < 300:
                _provider_key_success(provider, fingerprint)
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                last_error = ValueError("TMDB returned non-object JSON")
                transient_failed.append((kind, credential))
                return None
            if response.status_code == 429:
                # Respect provider quotas instead of switching accounts to bypass them.
                _provider_key_failure(provider, fingerprint, _retry_after_seconds(response), "HTTP 429")
                raise HTTPException(status_code=503, detail="TMDB rate limit reached; retry after the provider cooldown.")
            if response.status_code == 401:
                _provider_key_failure(provider, fingerprint, 3600, "HTTP 401")
                return None
            if response.status_code == 403:
                # A credential may lack permission for this endpoint; cool it down
                # and try the remaining configured credentials before failing.
                _provider_key_failure(provider, fingerprint, 3600, "HTTP 403")
                return None
            if response.status_code >= 500:
                transient_failed.append((kind, credential))
                return None
            response.raise_for_status()
            return None
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"TMDB returned HTTP {exc.response.status_code}.") from exc
        except (httpx.RequestError, ValueError) as exc:
            last_error = exc
            logger.info("TMDB request failed for %s credential %s: %s", path,
                _provider_key_id(provider, fingerprint).rsplit(":", 1)[-1], type(exc).__name__)
            transient_failed.append((kind, credential))
            return None

    for kind, credential in ordered:
        payload = await attempt(kind, credential)
        if payload is not None:
            return payload
    if transient_failed:
        await asyncio.sleep(0.75)
        retry_credentials = list(dict.fromkeys(transient_failed))
        transient_failed.clear()
        for kind, credential in retry_credentials:
            payload = await attempt(kind, credential)
            if payload is not None:
                return payload
        # Transport failures and upstream 5xx responses do not mean a credential
        # is invalid. Keep those credentials eligible; only explicit HTTP 401/403/429
        # responses should trigger credential-specific cooldowns.
    if last_response is not None and last_response.status_code == 429:
        raise HTTPException(status_code=503, detail="All eligible TMDB credentials are rate-limited or cooling down; retry shortly.")
    if last_response is not None and last_response.status_code in {401, 403}:
        raise HTTPException(status_code=503, detail="TMDB credentials were rejected or are cooling down. Check the configured keys.")
    logger.warning("TMDB request exhausted available credentials for %s (%s)", path, type(last_error).__name__ if last_error else "no eligible credential")
    raise HTTPException(status_code=503, detail="TMDB is temporarily unavailable or all credentials are cooling down. Cached catalogues will be used when available.")


@app.on_event("shutdown")
async def _close_tmdb_http_client() -> None:
    """Close pooled metadata clients cleanly when the application shuts down."""
    global _TMDB_HTTP_CLIENT, _WATCHMODE_HTTP_CLIENT, _OMDB_HTTP_CLIENT
    if _TMDB_HTTP_CLIENT is not None and not _TMDB_HTTP_CLIENT.is_closed:
        await _TMDB_HTTP_CLIENT.aclose()
    _TMDB_HTTP_CLIENT = None
    if _WATCHMODE_HTTP_CLIENT is not None and not _WATCHMODE_HTTP_CLIENT.is_closed:
        await _WATCHMODE_HTTP_CLIENT.aclose()
    _WATCHMODE_HTTP_CLIENT = None
    if _OMDB_HTTP_CLIENT is not None and not _OMDB_HTTP_CLIENT.is_closed:
        await _OMDB_HTTP_CLIENT.aclose()
    _OMDB_HTTP_CLIENT = None


async def _tmdb_discovered_company_ids(franchise: str) -> list[str]:
    normalized = franchise.strip().lower()
    now = time.monotonic()
    cached = _tmdb_company_id_cache.get(normalized)
    if cached and now - cached[0] < TMDB_COMPANY_CACHE_SECONDS:
        _record_cache_event("tmdb_company_ids", "memory_hit")
        return cached[1]

    _record_cache_event("tmdb_company_ids", "miss")
    task = _tmdb_company_inflight.get(normalized)
    if task is None or task.done():
        task = asyncio.create_task(_tmdb_load_company_ids(normalized))
        _tmdb_company_inflight[normalized] = task
    try:
        return await task
    finally:
        if task.done() and _tmdb_company_inflight.get(normalized) is task:
            _tmdb_company_inflight.pop(normalized, None)


async def _tmdb_load_company_ids(franchise: str) -> list[str]:
    if franchise == "marvel":
        ids = {"420", "19551", "38679", "2301", "13252"}
        queries = ("Marvel Studios", "Marvel Entertainment", "Marvel Animation", "Marvel Productions")
    else:
        ids = {"9993"}
        queries = ("DC Entertainment", "DC Films", "DC Comics")

    async def search_company(query: str) -> list[str]:
        try:
            payload = await _tmdb_get_json("search/company", {"query": query, "page": 1})
        except HTTPException as exc:
            logger.info("TMDB company lookup failed for %s: %s", query, exc.detail)
            return []
        found: list[str] = []
        for row in payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            company_id = row.get("id")
            if not name or not isinstance(company_id, (int, str)):
                continue
            lower_name = name.casefold()
            if franchise == "marvel" and "marvel" not in lower_name:
                continue
            if franchise == "dc" and not (
                "dc entertainment" in lower_name
                or "dc films" in lower_name
                or "dc comics" in lower_name
                or lower_name.startswith("dc ")
            ):
                continue
            found.append(str(company_id))
        return found

    discovered = await asyncio.gather(*(search_company(query) for query in queries))
    for values in discovered:
        ids.update(values)
    result = sorted(ids, key=lambda value: (value not in {"420", "9993"}, int(value)))[:20]
    _tmdb_company_id_cache[franchise] = (time.monotonic(), result)
    return result


def _tmdb_release_date_is_released(value: Any, today: str) -> bool:
    """Return True only when a real ISO release date is on or before today's UTC date."""
    release_text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_text):
        return False
    try:
        release_date = datetime.strptime(release_text, "%Y-%m-%d").date()
        cutoff_date = datetime.strptime(today, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return release_date <= cutoff_date


_BOLLYWOOD_COMEDY_SPECIAL_TITLES = {
    "madhurvirlicasualtea",
    "maheepsinghfir",
    "abijitgangulybaby",
}
_BOLLYWOOD_COMEDY_SPECIAL_DESCRIPTION = re.compile(
    r"\bstand[\s-]?up(?:\s+comedy)?\s+(?:special|show|performance)\b"
    r"|\b(?:\d+(?:st|nd|rd|th)?\s+)?(?:comedy|comic)\s+special\b"
    r"|\blive\s+(?:comedy|stand[\s-]?up)\s+(?:special|show|performance)\b",
    re.IGNORECASE,
)


def _is_bollywood_comedy_special(row: dict[str, Any]) -> bool:
    title = str(row.get("title") or row.get("name") or row.get("original_title") or "").strip()
    title_key = re.sub(r"[^a-z0-9]+", "", title.casefold())
    if title_key in _BOLLYWOOD_COMEDY_SPECIAL_TITLES:
        return True
    description = " ".join(
        str(row.get(field) or "")
        for field in ("overview", "tagline", "description")
    )
    return bool(_BOLLYWOOD_COMEDY_SPECIAL_DESCRIPTION.search(description))


async def _tmdb_fetch_movie_catalogue(catalogue_key: str, page: int, released_only: bool = True) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    params: dict[str, Any] = {
        "include_adult": "false",
        "include_video": "false",
        "language": "en-US",
        "page": page,
        "sort_by": "primary_release_date.desc",
    }
    if released_only:
        params["release_date.lte"] = today

    regional_modes = {
        "popular-bollywood": ("hi", "popular"),
        "trending-bollywood": ("hi", "trending"),
        "latest-bollywood": ("hi", "latest"),
        "popular-hollywood": ("en", "popular"),
        "trending-hollywood": ("en", "trending"),
        "latest-hollywood": ("en", "latest"),
    }
    if catalogue_key in {"marvel", "dc-live-action", "dc-animated"}:
        franchise = "marvel" if catalogue_key == "marvel" else "dc"
        company_ids = await _tmdb_discovered_company_ids(franchise)
        params["with_companies"] = "|".join(company_ids)
        if catalogue_key == "dc-animated":
            params["with_genres"] = "16"
        elif catalogue_key == "dc-live-action":
            params["without_genres"] = "16"
        payload = await _tmdb_get_json("discover/movie", params)
    elif catalogue_key in regional_modes:
        language_code, mode = regional_modes[catalogue_key]
        if mode == "trending":
            # TMDB's trending feed uses a different ranking signal from Popular.
            # Filter the feed by original language so the two menus stay distinct.
            payload = await _tmdb_get_json("trending/movie/week", {
                "language": "en-US",
                "page": page,
                "include_adult": "false",
            })
            # Keep Trending faithful to TMDB's weekly trending ranking. The
            # endpoint is global, so a language filter can produce short pages;
            # do not pad them with popularity-ranked Discover results. The
            # regional page builder scans subsequent trending pages as needed.
            payload = {
                **payload,
                "results": [
                    row for row in (payload.get("results") or [])
                    if isinstance(row, dict)
                    and str(row.get("original_language") or "").lower() == language_code
                ],
            }
        else:
            params["with_original_language"] = language_code
            params["sort_by"] = (
                "popularity.desc" if mode == "popular"
                else "release_date.desc" if mode == "latest"
                else "primary_release_date.desc"
            )
            if mode == "latest":
                # Use release_date rather than primary_release_date: many English-
                # language TMDB records have no primary_release_date, so that filter
                # can return future/undated records which the released-only guard
                # then rejects, leaving the entire Latest Hollywood page empty.
                recent_cutoff = datetime.fromtimestamp(time.time() - 5 * 365 * 86400, timezone.utc).date()
                params["release_date.gte"] = recent_cutoff.isoformat()
            payload = await _tmdb_get_json("discover/movie", params)
            if mode == "latest" and not (payload.get("results") or []):
                # TMDB's primary_release_date filter can return an empty page
                # for English-language films with regional-only release dates.
                # Retry using the broader release_date fields while retaining
                # the language and five-year window.
                fallback_params = dict(params)
                fallback_params.pop("primary_release_date.gte", None)
                fallback_params["release_date.gte"] = recent_cutoff.isoformat()
                fallback_params["sort_by"] = "release_date.desc"
                fallback = await _tmdb_get_json("discover/movie", fallback_params)
                if fallback.get("results"):
                    payload = fallback
                else:
                    # Last fallback: popularity ranking inside the same date
                    # window. This avoids a permanently empty catalogue when
                    # the release-date sort/filter combination is sparse.
                    fallback_params["sort_by"] = "popularity.desc"
                    fallback = await _tmdb_get_json("discover/movie", fallback_params)
                    if fallback.get("results"):
                        payload = fallback
    else:
        raise HTTPException(status_code=404, detail="Unknown movie catalogue.")

    raw_results = payload.get("results") or []

    # TMDB's company-based Marvel Discover feed can omit this special even
    # though it is an officially released Marvel title. Resolve it by exact
    # title/year and merge it into page 1 so it is visible in the main catalogue.
    if catalogue_key == "marvel" and page == 1:
        def punisher_special_key(value: Any) -> str:
            key = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
            presentation_prefix = "a marvel television special presentation "
            if key.startswith(presentation_prefix):
                key = key[len(presentation_prefix):]
            return key

        expected_punisher_key = "the punisher one last kill"
        already_in_page = any(
            isinstance(item, dict)
            and punisher_special_key(item.get("title") or item.get("original_title")) == expected_punisher_key
            for item in raw_results
        )
        if not already_in_page:
            try:
                special_search = await _tmdb_get_json("search/movie", {
                    "query": "The Punisher: One Last Kill",
                    "year": 2026,
                    "include_adult": "false",
                    "language": "en-US",
                    "page": 1,
                })
            except HTTPException as exc:
                logger.info("Could not resolve The Punisher: One Last Kill for Marvel catalogue: %s", exc.detail)
            else:
                special_item = next((
                    item for item in (special_search.get("results") or [])
                    if isinstance(item, dict)
                    and punisher_special_key(item.get("title") or item.get("original_title")) == expected_punisher_key
                    and re.match(r"^2026", str(item.get("release_date") or ""))
                    and (not released_only or _tmdb_release_date_is_released(item.get("release_date"), today))
                ), None)
                if special_item:
                    raw_results = list(raw_results)
                    raw_results.append(special_item)
                    payload = dict(payload)
                    payload["results"] = raw_results
                    payload["total_results"] = int(payload.get("total_results") or (len(raw_results) - 1)) + 1
                    payload["total_pages"] = max(
                        int(payload.get("total_pages") or 1),
                        (int(payload["total_results"]) + 19) // 20,
                    )
                    # Keep the injected title in the same newest-first order as
                    # the Discover response, including the fast first-paint path.
                    raw_results.sort(key=lambda item: (
                        str(item.get("release_date") or ""),
                        float(item.get("popularity") or 0),
                    ), reverse=True)

    results: list[dict[str, Any]] = []
    regional = regional_modes.get(catalogue_key)
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        if regional and str(item.get("original_language") or "").lower() != regional[0]:
            continue
        title = str(item.get("title") or item.get("original_title") or "").strip()
        if not title:
            continue
        if catalogue_key == "latest-bollywood" and _is_bollywood_comedy_special(item):
            continue
        release_date = str(item.get("release_date") or "").strip()
        if released_only and not _tmdb_release_date_is_released(release_date, today):
            continue
        year_match = re.match(r"^(\d{4})", release_date)
        poster_path = str(item.get("poster_path") or "").strip()
        backdrop_path = str(item.get("backdrop_path") or "").strip()
        results.append({
            "id": item.get("id"),
            "title": title,
            "year": int(year_match.group(1)) if year_match else None,
            "releaseDate": release_date,
            "overview": str(item.get("overview") or "").strip(),
            "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "",
            "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
            "rating": float(item.get("vote_average") or 0),
            "genres": [int(genre) for genre in (item.get("genre_ids") or []) if str(genre).isdigit()],
            "source": "TMDB",
        })

    total_pages = max(1, min(500, int(payload.get("total_pages") or 1)))
    current_page = max(1, min(500, int(payload.get("page") or page)))
    return {
        "catalogue": catalogue_key,
        "releasedOnly": released_only,
        "provider": "TMDB",
        "page": current_page,
        "totalPages": total_pages,
        "totalResults": int(payload.get("total_results") or len(results)),
        "results": results,
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }

MARVEL_WEB_SERIES: tuple[tuple[str, int], ...] = (
    ("WandaVision", 2021), ("The Falcon and the Winter Soldier", 2021), ("Loki", 2021),
    ("What If...?", 2021), ("Hawkeye", 2021), ("Moon Knight", 2022), ("Ms. Marvel", 2022),
    ("She-Hulk: Attorney at Law", 2022), ("Secret Invasion", 2023), ("Echo", 2024),
    ("Agatha All Along", 2024), ("Daredevil: Born Again", 2025), ("Ironheart", 2025),
    ("Eyes of Wakanda", 2025), ("Wonder Man", 2026), ("VisionQuest", 2026),
    ("Marvel Zombies", 2025), ("Your Friendly Neighborhood Spider-Man", 2025), ("X-Men '97", 2024),
    ("I Am Groot", 2022), ("Marvel Studios: Legends", 2021),
    ("Marvel's Avengers", 2018), ("Marvel Rising", 2018),
    ("Legion", 2017), ("The Gifted", 2017), ("Agents of S.H.I.E.L.D.", 2013),
    ("Agent Carter", 2015), ("Inhumans", 2017), ("Runaways", 2017), ("Cloak & Dagger", 2018),
    ("Daredevil", 2015), ("Jessica Jones", 2015), ("Luke Cage", 2016), ("Iron Fist", 2017),
    ("The Defenders", 2017), ("The Punisher", 2017), ("Helstrom", 2020), ("Blade: The Series", 2006),
    ("Mutant X", 2001), ("Avengers Assemble", 2013), ("Guardians of the Galaxy", 2015),
    ("Ultimate Spider-Man", 2012), ("Hulk and the Agents of S.M.A.S.H.", 2013),
    ("The Spectacular Spider-Man", 2008), ("Wolverine and the X-Men", 2009),
    ("X-Men: The Animated Series", 1992), ("Spider-Man: The Animated Series", 1994),
    ("Fantastic Four", 1994), ("Iron Man", 1994), ("The Incredible Hulk", 1996),
    ("Silver Surfer", 1998), ("Spider-Man Unlimited", 1999),
    ("The Avengers: Earth's Mightiest Heroes", 2010), ("Wolverine", 2011),
)
DC_WEB_SERIES: tuple[tuple[str, int], ...] = (
    ("Adventures of Superman", 1952), ("Batman", 1966), ("Shazam!", 1974), ("Wonder Woman", 1975),
    ("Superboy", 1988), ("Swamp Thing", 1990), ("The Flash", 1990), ("Lois & Clark: The New Adventures of Superman", 1993),
    ("Smallville", 2001), ("Birds of Prey", 2002), ("Human Target", 2010), ("Arrow", 2012),
    ("The Flash", 2014), ("Constantine", 2014), ("Gotham", 2014), ("Supergirl", 2015), ("iZombie", 2015),
    ("Legends of Tomorrow", 2016), ("Lucifer", 2016), ("Preacher", 2016), ("Powerless", 2017),
    ("Krypton", 2018), ("Titans", 2018), ("Black Lightning", 2018), ("Doom Patrol", 2019), ("Pennyworth", 2019),
    ("Swamp Thing", 2019), ("Watchmen", 2019), ("Batwoman", 2019), ("Stargirl", 2020),
    ("Superman & Lois", 2021), ("Sweet Tooth", 2021), ("Y: The Last Man", 2021), ("Naomi", 2022),
    ("The Sandman", 2022), ("Peacemaker", 2022), ("Gotham Knights", 2023), ("Dead Boy Detectives", 2024), ("The Penguin", 2024), ("Lanterns", 2026),
)

_MARVEL_TV_CATALOGUE_CACHE: dict[str, Any] = {"expiresAt": 0.0, "payload": None}
_DC_TV_CATALOGUE_CACHE: dict[str, Any] = {"expiresAt": 0.0, "payload": None}
def _load_persistent_tv_catalogue(cache: dict[str, Any], path: Path) -> dict[str, Any] | None:
    """Load a completed TV catalogue from persistent storage; it has no time-based expiry."""
    store = "tv_catalogue:" + path.stem
    if cache.get("payload"):
        _record_cache_event(store, "memory_hit")
        return cache["payload"]
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("results"), list)
            and payload["results"]
            # Provider enrichment was added after the original permanent cache.
            # Rebuild older cache files once so cards gain streaming-service data.
            and all(isinstance(item, dict) and isinstance(item.get("streamingProviders"), list) for item in payload["results"])
        ):
            cache.update({"expiresAt": float("inf"), "payload": payload})
            _record_cache_event(store, "persistent_hit")
            _record_cache_event(store, "persistent_rows_served", len(payload["results"]))
            logger.info("Loaded persistent TV catalogue cache %s (%d titles)", path.name, len(payload["results"]))
            return payload
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Persistent TV catalogue cache unavailable at %s: %s", path, exc)
    _record_cache_event(store, "miss")
    return None


def _save_persistent_tv_catalogue(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist TV metadata so container restarts never trigger a full TMDB scan."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        logger.info("Saved persistent TV catalogue cache %s (%d titles)", path.name, len(payload.get("results") or []))
    except OSError as exc:
        logger.warning("Could not persist TV catalogue cache %s: %s", path, exc)


async def _tv_streaming_providers(tmdb_id: int) -> list[dict[str, Any]]:
    """Resolve India streaming/rent/buy providers for one TMDB TV series."""
    if CACHE_ONLY_MODE:
        _record_cache_event("tv_streaming_availability", "cache_only_miss_blocked")
        return []
    _record_cache_event("tv_streaming_availability", "live_provider_lookup")
    try:
        payload = await _tmdb_get_json(f"tv/{tmdb_id}/watch/providers", {})
    except Exception as exc:
        logger.info("TMDB TV provider lookup failed for %s: %s", tmdb_id, exc)
        return []
    region = (payload.get("results") or {}).get("IN") or {}
    providers: list[dict[str, Any]] = []
    seen: set[tuple[int | None, str]] = set()
    for category, kind in (
        ("flatrate", "subscription"),
        ("free", "free"),
        ("ads", "ads"),
        ("rent", "rent"),
        ("buy", "buy"),
    ):
        for item in region.get(category) or []:
            name = str(item.get("provider_name") or "").strip()
            if not name:
                continue
            try:
                provider_id = int(item.get("provider_id")) if item.get("provider_id") is not None else None
            except (TypeError, ValueError):
                provider_id = None
            identity = (provider_id, name.casefold())
            if identity in seen:
                continue
            seen.add(identity)
            logo_path = str(item.get("logo_path") or "").strip()
            providers.append({
                "providerId": provider_id,
                "name": name,
                "type": kind,
                "region": "IN",
                "logoUrl": "https://image.tmdb.org/t/p/w92" + logo_path if logo_path else "",
                "webUrl": str(region.get("link") or ""),
            })
    return providers


@app.get("/api/tv/catalogue/marvel")
async def api_tmdb_marvel_tv_catalogue(page: int = Query(1, ge=1, le=500)):
    """Return cards for the curated Marvel TV/web-series list, enriched from TMDB."""
    cached_payload = _load_persistent_tv_catalogue(_MARVEL_TV_CATALOGUE_CACHE, MARVEL_TV_CATALOGUE_CACHE_FILE)
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS) and not cached_payload:
        raise HTTPException(status_code=503, detail="TMDB credentials are not configured.")

    semaphore = asyncio.Semaphore(3)

    async def resolve_series(title: str, year: int) -> dict[str, Any]:
        fallback = {
            "id": f"marvel-tv:{re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')}:{year}",
            "title": title,
            "year": year,
            "releaseDate": str(year),
            "overview": "",
            "posterUrl": "",
            "backdropUrl": "",
            "rating": 0.0,
            "tmdbUrl": "",
            "streamingProviders": [],
            "providerLink": "",
            "source": "Curated Marvel series list",
        }
        async with semaphore:
            try:
                payload = await _tmdb_get_json("search/tv", {
                    "query": title,
                    "first_air_date_year": year,
                    "include_adult": "false",
                    "language": "en-US",
                    "page": 1,
                })
            except HTTPException as exc:
                logger.info("TMDB Marvel TV lookup failed for %s (%s): %s", title, year, exc.detail)
                return fallback
            target = _normalize_title(title)
            candidates = [row for row in (payload.get("results") or []) if isinstance(row, dict) and row.get("id")]
            candidates.sort(key=lambda row: (
                _normalize_title(str(row.get("title") if is_anime_film else row.get("name") or "")) == target,
                str(row.get("release_date" if is_anime_film else "first_air_date") or "").startswith(str(year)),
                float(row.get("popularity") or 0),
            ), reverse=True)
            if not candidates:
                return fallback
            item = candidates[0]
            air_date = str(item.get("release_date" if is_anime_film else "first_air_date") or "").strip()
            year_match = re.match(r"^(\d{4})", air_date)
            poster_path = str(item.get("poster_path") or "").strip()
            backdrop_path = str(item.get("backdrop_path") or "").strip()
            tmdb_id = int(item["id"])
            return {
                "id": tmdb_id,
                "title": title,
                "year": int(year_match.group(1)) if year_match else year,
                "releaseDate": air_date,
                "overview": str(item.get("overview") or "").strip(),
                "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "",
                "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
                "rating": float(item.get("vote_average") or 0),
                "tmdbUrl": f"https://www.themoviedb.org/tv/{tmdb_id}",
                "streamingProviders": await _tv_streaming_providers(tmdb_id),
                "providerLink": "",
                "source": "TMDB",
            }

    if cached_payload:
        # Extend existing persistent catalogues additively. Never discard existing
        # entries: official Marvel titles missing from an older cache are appended.
        results = list(cached_payload.get("results") or [])
        known_titles = {_normalize_title(str(row.get("title") or "")) for row in results if isinstance(row, dict)}
        missing = [
            (title, year) for title, year in MARVEL_WEB_SERIES
            if _normalize_title(title) not in known_titles
        ]
        if missing and (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
            additions = await asyncio.gather(*(resolve_series(title, year) for title, year in missing))
            results.extend(additions)
            cached_payload = {**cached_payload, "results": results, "totalResults": len(results),
                              "provider": "TMDB + curated Marvel series list"}
            _save_persistent_tv_catalogue(MARVEL_TV_CATALOGUE_CACHE_FILE, cached_payload)
        _MARVEL_TV_CATALOGUE_CACHE.update({"expiresAt": float("inf"), "payload": cached_payload})
        return cached_payload

    results = await asyncio.gather(*(resolve_series(title, year) for title, year in MARVEL_WEB_SERIES))
    result = {
        "catalogue": "marvel-web-series",
        "page": 1,
        "totalPages": 1,
        "totalResults": len(results),
        "provider": "TMDB + curated Marvel series list",
        "results": results,
    }
    _MARVEL_TV_CATALOGUE_CACHE.update({"expiresAt": float("inf"), "payload": result})
    _save_persistent_tv_catalogue(MARVEL_TV_CATALOGUE_CACHE_FILE, result)
    return result


@app.get("/api/tv/catalogue/dc-live-action")
async def api_tmdb_dc_live_action_tv_catalogue(page: int = Query(1, ge=1, le=500)):
    """Return curated live-action DC TV series cards enriched from TMDB."""
    cached_payload = _load_persistent_tv_catalogue(_DC_TV_CATALOGUE_CACHE, DC_TV_CATALOGUE_CACHE_FILE)
    if cached_payload:
        return cached_payload
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        raise HTTPException(status_code=503, detail="TMDB credentials are not configured.")
    semaphore = asyncio.Semaphore(3)
    async def resolve_series(title: str, year: int) -> dict[str, Any]:
        fallback = {"id": f"dc-tv:{re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')}:{year}", "title": title, "year": year, "releaseDate": str(year), "overview": "", "posterUrl": "", "backdropUrl": "", "rating": 0.0, "tmdbUrl": "", "streamingProviders": [], "providerLink": "", "source": "Curated DC live-action series list"}
        async with semaphore:
            try:
                payload = await _tmdb_get_json("search/tv", {"query": title, "first_air_date_year": year, "include_adult": "false", "language": "en-US", "page": 1})
            except HTTPException as exc:
                logger.info("TMDB DC TV lookup failed for %s (%s): %s", title, year, exc.detail)
                return fallback
            target = _normalize_title(title)
            candidates = [row for row in (payload.get("results") or []) if isinstance(row, dict) and row.get("id")]
            candidates.sort(key=lambda row: (_normalize_title(str(row.get("name") or "")) == target, str(row.get("first_air_date") or "").startswith(str(year)), float(row.get("popularity") or 0)), reverse=True)
            if not candidates: return fallback
            item = candidates[0]
            air_date = str(item.get("first_air_date") or "").strip()
            year_match = re.match(r"^(\d{4})", air_date)
            poster_path = str(item.get("poster_path") or "").strip()
            backdrop_path = str(item.get("backdrop_path") or "").strip()
            tmdb_id = int(item["id"])
            return {"id": tmdb_id, "title": title, "year": int(year_match.group(1)) if year_match else year, "releaseDate": air_date, "overview": str(item.get("overview") or "").strip(), "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "", "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "", "rating": float(item.get("vote_average") or 0), "tmdbUrl": f"https://www.themoviedb.org/tv/{tmdb_id}", "streamingProviders": await _tv_streaming_providers(tmdb_id), "providerLink": "", "source": "TMDB"}
    results = await asyncio.gather(*(resolve_series(title, year) for title, year in DC_WEB_SERIES))
    result = {"catalogue": "dc-live-action-web-series", "page": 1, "totalPages": 1, "totalResults": len(results), "provider": "TMDB + curated DC live-action series list", "results": results}
    _DC_TV_CATALOGUE_CACHE.update({"expiresAt": float("inf"), "payload": result})
    _save_persistent_tv_catalogue(DC_TV_CATALOGUE_CACHE_FILE, result)
    return result




# Curated Classics TV and anime catalogues. These use NexStream/TMDB cards,
# not torrent searches.
_CLASSICS_TV_CATALOGUES: dict[str, list[tuple[str, int]]] = {
    "classics-web-series": [
        ("Breaking Bad", 2008), ("Better Call Saul", 2015), ("Arrow", 2012),
        ("The Flash", 2014), ("Marvel's Daredevil", 2015),
        ("The Queen's Gambit", 2020), ("Sherlock", 2010),
        ("How I Met Your Mother", 2005), ("The Last of Us", 2023),
        ("Peaky Blinders", 2013), ("The Big Bang Theory", 2007),
        ("Chernobyl", 2019), ("Dark", 2017), ("Succession", 2018),
        ("The Wire", 2002), ("The Office", 2005), ("Wednesday", 2022),
        ("Squid Game", 2021), ("Stranger Things", 2016),
        ("Game of Thrones", 2011), ("Severance", 2022),
        ("True Detective", 2014), ("Billions", 2016),
        ("The Studio", 2025), ("Secret Level", 2024),
        ("Love, Death & Robots", 2019), ("Two and a Half Men", 2003),
        ("Person of Interest", 2011),
    ],
    "classics-anime": [
        ("Death Note", 2006), ("Attack on Titan", 2013), ("Code Geass", 2006),
        ("Monster", 2004), ("Dragon Ball", 1986), ("Dragon Ball Z", 1989),
        ("Dragon Ball GT", 1996), ("Dragon Ball Z Kai", 2009), ("Super Dragon Ball Heroes", 2018), ("Dragon Ball Super", 2015),
        ("Dragon Ball Daima", 2024), ("Naruto", 2002),
        ("Naruto: Shippuden", 2007), ("Bleach", 2004), ("One Piece", 1999),
        ("One-Punch Man", 2015), ("Terror in Resonance", 2014),
        ("My Hero Academia", 2016), ("Fullmetal Alchemist: Brotherhood", 2009),
        ("Demon Slayer: Kimetsu no Yaiba", 2019), ("Vinland Saga", 2019),
        ("Cowboy Bebop", 1998), ("Jujutsu Kaisen", 2020), ("Hunter x Hunter", 2011),
        ("Hajime no Ippo", 2000), ("Chainsaw Man", 2022), ("Berserk", 1997),
        ("Parasyte: The Maxim", 2014), ("Avatar: The Last Airbender", 2005),
    ],
}
_CLASSICS_TV_CATALOGUE_CACHE: dict[str, dict[str, Any]] = {}
_CLASSICS_TV_CACHE_FILES = {
    "classics-web-series": "classics_web_series_tv_catalogue.json",
    "classics-anime": "classics_anime_tv_catalogue.json",
}


def _load_bundled_classics_tv_cache(catalogue_key: str) -> dict[str, Any] | None:
    filename = _CLASSICS_TV_CACHE_FILES.get(catalogue_key)
    if not filename:
        return None
    candidates = (
        _CACHE_SEED_DIR / filename,
        Path(os.getenv("APP_DATA_DIR", "/app/data")) / filename,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("catalogue") == catalogue_key and isinstance(payload.get("results"), list):
                return payload
        except (OSError, ValueError, AttributeError):
            continue
    return None


async def _get_classics_tv_catalogue(catalogue_key: str) -> dict[str, Any]:
    titles = _CLASSICS_TV_CATALOGUES.get(catalogue_key)
    if titles is None:
        raise HTTPException(status_code=404, detail="Unknown Classics TV catalogue.")
    cached = _CLASSICS_TV_CATALOGUE_CACHE.get(catalogue_key)
    if cached is not None:
        _record_cache_event("classics_tv:" + catalogue_key, "memory_hit")
        return cached
    bundled = _load_bundled_classics_tv_cache(catalogue_key)
    if bundled is not None:
        _record_cache_event("classics_tv:" + catalogue_key, "bundled_hit")
        _CLASSICS_TV_CATALOGUE_CACHE[catalogue_key] = bundled
        return bundled
    _record_cache_event("classics_tv:" + catalogue_key, "miss")
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        raise HTTPException(status_code=503, detail="TMDB credentials are not configured.")
    semaphore = asyncio.Semaphore(3)

    async def resolve_series(title: str, year: int) -> dict[str, Any]:
        fallback = {
            "id": f"classics-tv:{re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')}:{year}",
            "title": title, "year": year, "releaseDate": str(year), "overview": "",
            "posterUrl": "", "backdropUrl": "", "rating": 0.0, "tmdbUrl": "",
            "streamingProviders": [], "providerLink": "", "source": "Curated Classics list",
        }
        async with semaphore:
            try:
                is_anime_film = title == "Akira"
                payload = await _tmdb_get_json("search/movie" if is_anime_film else "search/tv", {
                    "query": title,
                    ("year" if is_anime_film else "first_air_date_year"): year,
                    "include_adult": "false", "language": "en-US", "page": 1,
                })
            except HTTPException as exc:
                logger.info("TMDB Classics TV lookup failed for %s (%s): %s", title, year, exc.detail)
                return fallback
            target = _normalize_title(title)
            candidates = [row for row in (payload.get("results") or []) if isinstance(row, dict) and row.get("id")]
            candidates.sort(key=lambda row: (
                _normalize_title(str(row.get("name") or "")) == target,
                str(row.get("first_air_date") or "").startswith(str(year)),
                float(row.get("popularity") or 0),
            ), reverse=True)
            if not candidates:
                return fallback
            item = candidates[0]
            air_date = str(item.get("first_air_date") or "").strip()
            year_match = re.match(r"^(\d{4})", air_date)
            poster_path = str(item.get("poster_path") or "").strip()
            backdrop_path = str(item.get("backdrop_path") or "").strip()
            tmdb_id = int(item["id"])
            return {
                "id": tmdb_id, "title": title,
                "year": int(year_match.group(1)) if year_match else year,
                "releaseDate": air_date, "overview": str(item.get("overview") or "").strip(),
                "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "",
                "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
                "rating": float(item.get("vote_average") or 0),
                "tmdbUrl": f"https://www.themoviedb.org/{'movie' if is_anime_film else 'tv'}/{tmdb_id}",
                "streamingProviders": [] if is_anime_film else await _tv_streaming_providers(tmdb_id),
                "providerLink": "", "source": "TMDB",
            }

    results = await asyncio.gather(*(resolve_series(title, year) for title, year in titles))
    results.sort(key=lambda row: (str(row.get("releaseDate") or row.get("year") or ""), str(row.get("title") or "")), reverse=True)
    result = {
        "catalogue": catalogue_key, "page": 1, "totalPages": 1,
        "totalResults": len(results), "provider": "TMDB + curated Classics list", "results": results,
    }
    _CLASSICS_TV_CATALOGUE_CACHE[catalogue_key] = result

    # Persist the resolved catalogue so the manual GitHub snapshot workflow can
    # copy it from APP_DATA_DIR and seed ephemeral deployments with a warm cache.
    filename = _CLASSICS_TV_CACHE_FILES[catalogue_key]
    cache_path = Path(os.getenv("APP_DATA_DIR", "/app/data")) / filename
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary_path.replace(cache_path)
        logger.info("Persisted Classics TV catalogue cache %s (%s titles)", filename, len(results))
    except OSError as exc:
        logger.warning("Could not persist Classics TV catalogue cache %s: %s", filename, exc)

    return result


@app.get("/api/tv/catalogue/classics-web-series")
async def api_tmdb_classics_web_series_catalogue():
    return await _get_classics_tv_catalogue("classics-web-series")


@app.get("/api/tv/catalogue/classics-anime")
async def api_tmdb_classics_anime_catalogue():
    return await _get_classics_tv_catalogue("classics-anime")


def _read_regional_tv_prebuilt_page(catalogue_key: str, page: int) -> dict[str, Any] | None:
    """Read one page from the persistent TV catalogue built by scripts/build_regional_movie_cache.py."""
    try:
        if not REGIONAL_TV_PREBUILT_CACHE_FILE.exists():
            return None
        payload = json.loads(REGIONAL_TV_PREBUILT_CACHE_FILE.read_text(encoding="utf-8"))
        # The cache builder writes "version", not "schemaVersion". Accept both
        # formats so valid snapshots created by the VPS builder are actually used.
        cache_version = int(payload.get("schemaVersion") or payload.get("version") or 0)
        if cache_version < 3:
            return None
        catalogue = (payload.get("catalogues") or {}).get(catalogue_key) or {}
        # The builder marks snapshots with status/seriesCount; the API's own
        # writer uses verifiedWebSeries. Accept either valid persisted format.
        pages = catalogue.get("pages") or []
        is_verified = bool(catalogue.get("verifiedWebSeries")) or (
            catalogue.get("status") in {"ready", "partial"} and bool(pages)
        )
        if not is_verified:
            return None
        for entry in pages:
            if int(entry.get("page") or 0) == page:
                rows = [row for row in (entry.get("results") or []) if isinstance(row, dict)]
                if catalogue_key.startswith("indian-"):
                    excluded_titles = {"gangaa", "dearishq", "mastram"}
                    rows = [
                        row for row in rows
                        if re.sub(r"[^a-z0-9]+", "", str(row.get("title") or row.get("name") or "").casefold()) not in excluded_titles
                    ]
                return {
                    "catalogue": catalogue_key,
                    "page": page,
                    "totalPages": max(1, len(pages)),
                    "totalResults": int(catalogue.get("totalResults") or catalogue.get("seriesCount") or 0),
                    "provider": "Persistent TMDB + verified India OTT availability",
                    "results": rows,
                    "prebuilt": True,
                }
    except Exception as exc:
        logger.warning("Could not read regional TV prebuilt cache for %s page %s: %s", catalogue_key, page, exc)
    return None


def _save_regional_tv_prebuilt_pages(catalogue_key: str, rows: list[dict[str, Any]]) -> None:
    """Store three 18-item pages in the persistent VPS TV catalogue."""
    try:
        REGIONAL_TV_PREBUILT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schemaVersion": 3, "catalogues": {}}
        if REGIONAL_TV_PREBUILT_CACHE_FILE.exists():
            try:
                existing = json.loads(REGIONAL_TV_PREBUILT_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload = existing
            except Exception:
                logger.warning("Regional TV cache unreadable; rebuilding its document")
        payload["schemaVersion"] = 3
        catalogues = payload.setdefault("catalogues", {})
        if not isinstance(catalogues, dict):
            catalogues = {}
            payload["catalogues"] = catalogues
        # Persist every discovered page, not only the first three. Page 4+
        # requires more discovery/enrichment on its first request; saving all
        # verified rows prevents repeating that expensive work on every visit.
        page_count = max(3, (len(rows) + 17) // 18)
        pages = [{"page": n + 1, "results": rows[n * 18:(n + 1) * 18]} for n in range(page_count)]
        catalogues[catalogue_key] = {
            "verifiedWebSeries": True,
            "totalResults": len(rows),
            "seriesCount": len(rows),
            "pages": pages,
        }
        temporary = REGIONAL_TV_PREBUILT_CACHE_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(REGIONAL_TV_PREBUILT_CACHE_FILE)
    except Exception as exc:
        logger.warning("Could not save regional TV catalogue %s: %s", catalogue_key, exc)


_REGIONAL_TV_PAGE_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_REGIONAL_TV_PAGE_CACHE_TTL = 6 * 60 * 60
_REGIONAL_TV_DISCOVERY_CACHE = _REGIONAL_TV_PAGE_CACHE
REGIONAL_TV_PREBUILT_CACHE_FILE = Path(os.getenv("REGIONAL_TV_PREBUILT_CACHE_FILE", "/app/data/regional_tv_prebuilt_cache.json"))
_INDIAN_LINEAR_TV_NETWORKS = {
    "starplus", "colors", "zee tv", "zeetv", "sony entertainment television",
    "sony sab", "sab tv", "dd national", "andtv", "&tv", "star bharat",
    "sony pal", "dangal tv", "sun tv", "star utsav",
}


@app.get("/api/tv/catalogue/{catalogue_key}")
async def api_tmdb_regional_tv_catalogue(catalogue_key: str, page: int = Query(1, ge=1, le=500)):
    """Return Hindi Indian OTT web-series, excluding non-Hindi and non-scripted TV formats."""
    configs = {
        "indian-popular": ("IN", "hi", "popularity.desc"),
        "indian-trending": ("IN", "hi", "popularity.desc"),
        "indian-latest": ("IN", "hi", "first_air_date.desc"),
        "hollywood-popular": ("US", "en", "popularity.desc"),
        "hollywood-trending": ("US", "en", "popularity.desc"),
        "hollywood-latest": ("US", "en", "first_air_date.desc"),
    }
    if catalogue_key not in configs:
        raise HTTPException(status_code=404, detail="Unknown TV catalogue.")
    # Indian Popular and Trending exclude scripted series whose TMDB network
    # metadata identifies Zee TV, Star Plus, Colors, or Sony TV as a broadcaster.
    exclude_indian_broadcast_networks = catalogue_key in {"indian-popular", "indian-trending"}
    excluded_network_names = {
        "zee tv", "zeetv", "star plus", "starplus", "colors", "colorstv",
        "sony tv", "sony entertainment television", "sony entertainment tv",
        "set india", "sony sab",
    }
    # TMDB's genre 10766 is Soap. TMDB genre tagging is not complete for every
    # Indian serial, so retain a small, exact-title safety list for known soaps.
    # Keep names normalized and exact-match only to avoid excluding unrelated titles.
    known_indian_soap_titles = {
        "agniphera", "bhabiji ghar par hain", "devon ke dev mahadev",
        "jiji maa", "radha krishn", "sirf tum", "pushpa impossible",
        "yeh rishta kya kehlata hai", "kundali bhagya", "kumkum bhagya",
        "anupamaa", "ghum hai kisikey pyaar mein", "imli", "udne ki aasha",
        "taarak mehta ka ooltah chashmah", "sasural simar ka", "saath nibhaana saathiya",
        "diya aur baati hum", "yeh hai mohabbatein", "kasautii zindagii kay",
        "naagin", "ishqbaaaz", "shakti astitva ke ehsaas ki", "choti sarrdaarni",
        "molkki", "pinjara khubsurti ka", "mere angne mein", "swaragini",
        "balika vadhu", "uttaran", "pavitra rishta", "kumkum",
    }

    def _normalized_tv_title(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    def has_soap_genre(genres: Any) -> bool:
        return isinstance(genres, list) and any(
            isinstance(genre, dict) and str(genre.get("id") or "").strip() == "10766"
            for genre in genres
        )

    def is_known_indian_soap(*titles: Any) -> bool:
        return any(_normalized_tv_title(title) in known_indian_soap_titles for title in titles if title)

    def has_excluded_broadcast_network(networks: Any) -> bool:
        if not isinstance(networks, list):
            return False
        for network in networks:
            if not isinstance(network, dict):
                continue
            name = re.sub(r"[^a-z0-9]+", " ", str(network.get("name") or "").casefold()).strip()
            compact = name.replace(" ", "")
            if name in excluded_network_names or compact in {n.replace(" ", "") for n in excluded_network_names}:
                return True
        return False

    async def filter_cached_networks(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return result
        if not exclude_indian_broadcast_networks:
            rows = result.get("results")
            if isinstance(rows, list):
                filtered_by_year = []
                for row in rows:
                    try:
                        air_date = str(row.get("releaseDate") or row.get("first_air_date") or "").strip()
                        if air_date and datetime.strptime(air_date[:10], "%Y-%m-%d").date() >= datetime(2015, 1, 1).date():
                            filtered_by_year.append(row)
                    except (TypeError, ValueError):
                        continue
                updated = dict(result)
                updated["results"] = filtered_by_year[:18]
                updated["totalResults"] = len(filtered_by_year)
                updated["pageSize"] = 18
                return updated
            return result
        rows = result.get("results")
        if not isinstance(rows, list):
            return result
        filtered = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                tmdb_id = int(row.get("id") or 0)
                if not tmdb_id:
                    continue
                # Always resolve canonical metadata for cached entries. Old cache
                # rows predate the soap filter and cannot be trusted on their own.
                details = await _tmdb_get_json(f"tv/{tmdb_id}", {})
                networks = details.get("networks") if isinstance(details.get("networks"), list) else []
                genres = details.get("genres") if isinstance(details.get("genres"), list) else []
                titles = (details.get("name"), details.get("original_name"), row.get("title"), row.get("name"))
                series_type = str(details.get("type") or row.get("seriesType") or "").casefold()
                if series_type not in {"scripted", "miniseries"}:
                    continue
                air_date = str(details.get("first_air_date") or row.get("releaseDate") or row.get("first_air_date") or "").strip()
                try:
                    if not air_date or datetime.strptime(air_date[:10], "%Y-%m-%d").date() < datetime(2015, 1, 1).date():
                        continue
                except ValueError:
                    continue
                if has_soap_genre(genres) or is_known_indian_soap(*titles):
                    continue
                # Fail closed when network metadata is missing: this catalogue
                # must not leak linear-TV entries merely because metadata is absent.
                if not networks or has_excluded_broadcast_network(networks):
                    continue
                row = dict(row)
                row["networks"] = networks
                row["genres"] = genres
                row["seriesType"] = series_type
            except Exception as exc:
                logger.info("Skipping cached TV item %s because its metadata could not be verified: %s", row.get("id"), type(exc).__name__)
                continue
            filtered.append(row)
        result = dict(result)
        result["results"] = filtered
        result["totalResults"] = len(filtered)
        return result

    # Use only pages explicitly marked as verified by this endpoint.
    prebuilt = _read_regional_tv_prebuilt_page(catalogue_key, page)
    if prebuilt is not None:
        _record_cache_event("regional_tv_catalogue", "prebuilt_hit")
        return prebuilt
    _record_cache_event("regional_tv_catalogue", "prebuilt_miss")

    country, language, sort_by = configs[catalogue_key]
    cache_key = (catalogue_key, page)
    cached = _REGIONAL_TV_DISCOVERY_CACHE.get(cache_key)
    cache_ttl = 900 if catalogue_key.endswith("-trending") else 21600
    if cached and time.monotonic() - cached[0] < cache_ttl and not exclude_indian_broadcast_networks:
        _record_cache_event("regional_tv_catalogue", "memory_hit")
        return await filter_cached_networks(cached[1])
    _record_cache_event("regional_tv_catalogue", "discovery_miss")
    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        raise HTTPException(status_code=503, detail="TMDB credentials are not configured.")

    today = datetime.now(timezone.utc).date().isoformat()
    try:
        # Build UI pages from the filtered result set, not raw TMDB pages.
        # Continue fetching source pages until this page has 18 eligible series
        # (or TMDB runs out), so strict Indian web-series filters don't leave gaps.
        source_page = 1
        source_total_pages = 1
        eligible_rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        # Build enough verified rows to fill the requested page, then persist
        # every verified row so later visits to deeper pages can use disk cache.
        required_count = max(54, page * 18)

        while source_page <= source_total_pages and len(eligible_rows) < required_count:
            payload = await _tmdb_get_json("discover/tv", {
                "include_adult": "false",
                "language": "en-US",
                "sort_by": sort_by,
                "with_origin_country": country,
                "with_original_language": language,
                "with_type": "2|4",
                "watch_region": "IN",
                "with_watch_monetization_types": "flatrate|free|ads",
                "first_air_date.gte": "2015-01-01",
                "first_air_date.lte": today,
                "page": source_page,
            })
            source_total_pages = max(1, min(int(payload.get("total_pages") or 1), 500))

            # Resolve independent series concurrently. The old sequential loop
            # made page 4+ wait for two remote requests per candidate in series.
            tv_lookup_semaphore = asyncio.Semaphore(8)

            async def verify_tv_item(item: Any) -> dict[str, Any] | None:
                if not isinstance(item, dict) or not item.get("id"):
                    return None
                try:
                    tmdb_id = int(item["id"])
                except (TypeError, ValueError):
                    return None
                origins = item.get("origin_country") or []
                original_language = str(item.get("original_language") or "").casefold()
                if country not in origins or original_language != language:
                    return None
                air_date = str(item.get("first_air_date") or "").strip()
                try:
                    parsed_air_date = datetime.strptime(air_date[:10], "%Y-%m-%d").date()
                    if parsed_air_date < datetime(2015, 1, 1).date() or parsed_air_date > datetime.now(timezone.utc).date():
                        return None
                except ValueError:
                    return None

                async with tv_lookup_semaphore:
                    try:
                        details = await _tmdb_get_json(f"tv/{tmdb_id}", {})
                    except Exception as exc:
                        logger.info("Skipping TV item %s because series type could not be verified: %s", tmdb_id, type(exc).__name__)
                        return None
                    series_type = str(details.get("type") or item.get("type") or "").casefold()
                    if series_type not in {"scripted", "miniseries"}:
                        return None

                    networks = details.get("networks") if isinstance(details.get("networks"), list) else []
                    genres = details.get("genres") if isinstance(details.get("genres"), list) else []
                    title_candidates = (details.get("name"), details.get("original_name"), item.get("name"), item.get("original_name"))
                    title_keys = {
                        re.sub(r"[^a-z0-9]+", "", str(title or "").casefold())
                        for title in title_candidates
                    }
                    if catalogue_key.startswith("indian-") and title_keys.intersection({"gangaa", "dearishq", "mastram"}):
                        return None
                    if exclude_indian_broadcast_networks:
                        if has_soap_genre(genres) or is_known_indian_soap(*title_candidates):
                            return None
                        if not networks or has_excluded_broadcast_network(networks):
                            return None

                    try:
                        providers = await _tv_streaming_providers(tmdb_id)
                    except Exception as exc:
                        logger.info("Skipping TV item %s because OTT availability could not be verified: %s", tmdb_id, type(exc).__name__)
                        return None
                    providers = [
                        provider for provider in providers
                        if str(provider.get("type") or "").casefold() in {"subscription", "free", "ads"}
                    ]
                    if not providers:
                        return None

                poster = str(item.get("poster_path") or details.get("poster_path") or "").strip()
                backdrop = str(item.get("backdrop_path") or details.get("backdrop_path") or "").strip()
                year_match = re.match(r"^(\d{4})", air_date)
                return {
                    "id": tmdb_id,
                    "title": str(item.get("name") or details.get("name") or "").strip(),
                    "year": int(year_match.group(1)) if year_match else None,
                    "releaseDate": air_date,
                    "overview": str(item.get("overview") or details.get("overview") or "").strip(),
                    "posterUrl": TMDB_IMAGE_BASE + poster if poster else "",
                    "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop if backdrop else "",
                    "rating": float(item.get("vote_average") or details.get("vote_average") or 0),
                    "tmdbUrl": f"https://www.themoviedb.org/tv/{tmdb_id}",
                    "streamingProviders": providers,
                    "providerLink": "",
                    "source": "TMDB",
                    "originalLanguage": original_language,
                    "originCountry": origins,
                    "seriesType": series_type,
                    "networks": networks,
                    "genres": genres,
                }

            verified_rows = await asyncio.gather(
                *(verify_tv_item(item) for item in payload.get("results", [])),
                return_exceptions=True,
            )
            for row in verified_rows:
                if isinstance(row, dict) and row.get("id") not in seen_ids:
                    eligible_rows.append(row)
                    seen_ids.add(int(row["id"]))
            source_page += 1
    except HTTPException:
        raise

    _save_regional_tv_prebuilt_pages(catalogue_key, eligible_rows)
    start_index = (page - 1) * 18
    results = eligible_rows[start_index:start_index + 18]
    # UI pages are 18 filtered titles each. Estimate the number of pages from
    # TMDB's candidate count; the last page may be shorter after verification.
    total_eligible_pages = max(1, (int(payload.get("total_results") or len(eligible_rows)) + 17) // 18)
    result = {
        "catalogue": catalogue_key, "page": page,
        "totalPages": total_eligible_pages,
        "totalResults": len(eligible_rows),
        "pageSize": 18,
        "provider": "TMDB + verified India OTT availability",
        "results": results,
    }
    _REGIONAL_TV_DISCOVERY_CACHE[cache_key] = (time.monotonic(), result)
    return result


async def _apply_movie_release_eligibility(payload: dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    """Show films streaming on Indian OTT, or otherwise only after one month."""
    if not enabled or not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return payload
    today = datetime.now(timezone.utc).date()
    rows = [row for row in payload.get("results", []) if isinstance(row, dict)]
    semaphore = asyncio.Semaphore(4)

    async def eligible(row: dict[str, Any]) -> bool:
        # Older releases qualify without an OTT lookup. This avoids spending
        # provider quota on films that already pass the 30-day rule.
        raw_date = str(row.get("releaseDate") or row.get("release_date") or "").strip()
        try:
            release_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            release_date = None
        if release_date is not None and release_date <= today and (today - release_date).days >= 30:
            return True

        # Recent releases qualify only with verified subscription/free/ad-supported
        # streaming in India; rent/buy listings do not satisfy this rule.
        try:
            movie_id = int(row.get("tmdbId") or row.get("id") or 0)
        except (TypeError, ValueError):
            movie_id = 0
        if movie_id > 0:
            saved = row.get("ottAvailability")
            if isinstance(saved, dict):
                checked_at = str(saved.get("checkedAt") or "").strip()
                try:
                    checked_dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                    if checked_dt.tzinfo is None:
                        checked_dt = checked_dt.replace(tzinfo=timezone.utc)
                    saved_is_fresh = 0 <= (datetime.now(timezone.utc) - checked_dt).total_seconds() < TMDB_OTT_CACHE_SECONDS
                except (TypeError, ValueError):
                    saved_is_fresh = False
                status = str(saved.get("status") or "").casefold()
                providers = saved.get("streamingProviders") or []
                if saved_is_fresh and status == "streaming" and providers:
                    return True
            async with semaphore:
                try:
                    ott_key = (movie_id, "IN")
                    now_monotonic = time.monotonic()
                    cached_ott = _tmdb_ott_cache.get(ott_key)
                    availability = None
                    stale_availability = cached_ott[1] if cached_ott else None
                    if cached_ott and now_monotonic - cached_ott[0] < _tmdb_ott_ttl(cached_ott[1]):
                        availability = cached_ott[1]
                    if availability is None:
                        persistent_ott = _read_tmdb_ott_persistent_cache(ott_key)
                        if persistent_ott:
                            saved_at, persistent_payload = persistent_ott
                            if 0 <= time.time() - saved_at < _tmdb_ott_ttl(persistent_payload):
                                availability = persistent_payload
                                _tmdb_ott_cache[ott_key] = (time.monotonic(), availability)
                            elif stale_availability is None:
                                stale_availability = persistent_payload
                    if availability is None:
                        try:
                            availability = await asyncio.wait_for(
                                _tmdb_fetch_movie_ott_availability(movie_id, "IN"), timeout=12
                            )
                        except Exception:
                            if stale_availability is None:
                                raise
                            availability = stale_availability
                        else:
                            _tmdb_ott_cache[ott_key] = (time.monotonic(), availability)
                            _write_tmdb_ott_persistent_cache(ott_key, availability)
                    status = str(availability.get("status") or "").casefold() if isinstance(availability, dict) else ""
                    providers = availability.get("streamingProviders") or [] if isinstance(availability, dict) else []
                    if status == "streaming" and providers:
                        row["ottAvailability"] = availability
                        return True
                except Exception as exc:
                    logger.info("OTT eligibility lookup failed for movie %s: %s", movie_id, type(exc).__name__)

        # Missing, future, or too-recent release dates do not qualify unless
        # verified Indian OTT streaming was confirmed above.
        return False

    flags = await asyncio.gather(*(eligible(row) for row in rows))
    filtered = [row for row, keep in zip(rows, flags) if keep]
    result = dict(payload)
    result["results"] = filtered
    result["totalResults"] = len(filtered)
    result["releaseEligibility"] = "streaming-in-india-or-30-days"
    return result


def _regional_movie_week_start(value: date | None = None) -> str:
    current = value or datetime.now(timezone.utc).date()
    return (current - timedelta(days=current.weekday())).isoformat()


def _read_regional_movie_trending_archive() -> dict[str, Any]:
    try:
        payload = json.loads(REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "weeks": []}
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Could not read weekly regional trending archive: %s", exc)
        return {"version": 1, "weeks": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("weeks"), list):
        logger.warning("Ignoring invalid weekly regional trending archive")
        return {"version": 1, "weeks": []}
    return payload


def _write_regional_movie_trending_archive(payload: dict[str, Any]) -> bool:
    try:
        REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE.with_name(
            REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE.name + "." + uuid.uuid4().hex + ".tmp"
        )
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, REGIONAL_MOVIE_TRENDING_ARCHIVE_FILE)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.exception("Could not write weekly regional trending archive: %s", exc)
        return False


def _regional_movie_trending_archive_page(catalogue_key: str, page: int) -> dict[str, Any] | None:
    cache_key = (catalogue_key + "|weekly-archive-v1", page)
    cached = _tmdb_movie_catalogue_cache.get(cache_key)
    if cached:
        return cached[1]

    archive = _read_regional_movie_trending_archive()
    snapshots = [
        week for week in archive["weeks"]
        if isinstance(week, dict)
        and isinstance(week.get("catalogues"), dict)
        and isinstance(week["catalogues"].get(catalogue_key), list)
    ]
    snapshots.sort(key=lambda week: str(week.get("weekOf") or ""), reverse=True)
    if not snapshots:
        return None

    unique_rows: dict[str, dict[str, Any]] = {}
    for week in snapshots:
        week_of = str(week.get("weekOf") or "")
        rows = week["catalogues"][catalogue_key]
        for rank, source_row in enumerate(rows, 1):
            if not isinstance(source_row, dict):
                continue
            row = dict(source_row)
            title = str(row.get("title") or row.get("name") or "").strip()
            if not title:
                continue
            identity = str(row.get("tmdbId") or row.get("id") or "").strip()
            if not identity:
                identity = re.sub(r"[^a-z0-9]+", "", title.casefold()) + "|" + str(row.get("year") or "")
            existing = unique_rows.get(identity)
            if existing:
                history = list(existing.get("trendingWeeks") or [])
                if week_of and week_of not in history:
                    history.append(week_of)
                    existing["trendingWeeks"] = history
                continue
            row["trendingWeek"] = week_of
            row["trendingRank"] = int(row.get("trendingRank") or rank)
            row["trendingWeeks"] = [week_of] if week_of else []
            unique_rows[identity] = row

    rows = list(unique_rows.values())
    page_size = REGIONAL_MOVIE_CACHE_PAGE_SIZE
    total_results = len(rows)
    total_pages = max(1, (total_results + page_size - 1) // page_size)
    start = (page - 1) * page_size
    response = {
        "catalogue": catalogue_key,
        "releasedOnly": True,
        "provider": "TMDB weekly trending archive",
        "page": page,
        "totalPages": total_pages,
        "totalResults": total_results,
        "pageSize": page_size,
        "results": rows[start:start + page_size],
        "releaseEligibility": "streaming-in-india-or-30-days",
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }
    _tmdb_movie_catalogue_cache[cache_key] = (time.monotonic(), response)
    return response


def _save_regional_movie_trending_week(
    week_of: str,
    catalogue_key: str,
    rows: list[dict[str, Any]],
) -> bool:
    archive = _read_regional_movie_trending_archive()
    weeks = [
        week for week in archive["weeks"]
        if isinstance(week, dict) and str(week.get("weekOf") or "") != week_of
    ]
    current_week = next(
        (
            week for week in archive["weeks"]
            if isinstance(week, dict) and str(week.get("weekOf") or "") == week_of
        ),
        None,
    )
    if current_week is None:
        current_week = {"weekOf": week_of, "catalogues": {}}
    catalogues = current_week.get("catalogues")
    if not isinstance(catalogues, dict):
        catalogues = {}
    catalogues[catalogue_key] = [
        {**row, "trendingWeek": week_of, "trendingRank": rank}
        for rank, row in enumerate(rows, 1)
    ]
    current_week["catalogues"] = catalogues
    weeks.append(current_week)
    weeks.sort(key=lambda week: str(week.get("weekOf") or ""), reverse=True)
    archive.update({
        "version": 1,
        "builtAt": datetime.now(timezone.utc).isoformat(),
        "weeks": weeks[:REGIONAL_MOVIE_TRENDING_ARCHIVE_WEEKS],
    })
    if not _write_regional_movie_trending_archive(archive):
        return False
    for key in ("trending-hollywood", "trending-bollywood"):
        for cache_key in [item for item in _tmdb_movie_catalogue_cache if item[0] == key + "|weekly-archive-v1"]:
            _tmdb_movie_catalogue_cache.pop(cache_key, None)
    return True


def _cache_only_regional_movie_catalogue(catalogue_key: str, page: int) -> dict[str, Any] | None:
    """Build a regional movie page from shipped snapshots when live TMDB is disabled."""
    if catalogue_key.startswith("trending-"):
        archive_page = _regional_movie_trending_archive_page(catalogue_key, page)
        if archive_page is not None:
            return archive_page

    page_size = REGIONAL_MOVIE_CACHE_PAGE_SIZE
    today = datetime.now(timezone.utc).date().isoformat()
    rows_by_identity: dict[str, dict[str, Any]] = {}
    prebuilt_paths = (
        REGIONAL_MOVIE_PREBUILT_CACHE_PATH,
        _CACHE_SEED_DIR / "runtime" / "regional_movie_prebuilt_cache.json",
    )
    seen_paths: set[str] = set()
    for path in prebuilt_paths:
        identity = str(path)
        if identity in seen_paths:
            continue
        seen_paths.add(identity)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not read regional movie snapshot %s: %s", path, exc)
            continue
        movies = payload.get("movies") if isinstance(payload, dict) else None
        if not isinstance(movies, dict):
            continue
        for entry in movies.values():
            if not isinstance(entry, dict) or catalogue_key not in (entry.get("catalogues") or []):
                continue
            movie = entry.get("movie")
            if not isinstance(movie, dict):
                continue
            row = dict(movie)
            if catalogue_key == "latest-bollywood" and row.get("nexStreamPlayable") is not True:
                continue
            if catalogue_key == "latest-bollywood" and _is_bollywood_comedy_special(row):
                continue
            release_date = str(row.get("releaseDate") or row.get("release_date") or "").strip()
            if release_date and not _tmdb_release_date_is_released(release_date[:10], today):
                continue
            row.setdefault("tmdbId", row.get("id"))
            row.setdefault("source", "Bundled TMDB cache")
            movie_id = str(row.get("tmdbId") or row.get("id") or "").strip()
            title_key = re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").casefold())
            key = movie_id or (title_key + "|" + str(row.get("year") or ""))
            if key.strip("|"):
                rows_by_identity.setdefault(key, row)

    if not rows_by_identity and catalogue_key == "latest-hollywood":
        seed_path = _CACHE_SEED_DIR / "latest_hollywood_catalogue.json"
        try:
            payload = json.loads(seed_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {}
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not read Hollywood latest snapshot %s: %s", seed_path, exc)
            payload = {}
        for source_row in payload.get("results", []) if isinstance(payload, dict) else []:
            if not isinstance(source_row, dict):
                continue
            row = dict(source_row)
            row["title"] = str(
                row.get("catalogueTitle") or row.get("mediaTitle") or row.get("title") or ""
            ).strip()
            if not row["title"]:
                continue
            release_date = str(row.get("releaseDate") or row.get("release_date") or "").strip()
            if release_date and not _tmdb_release_date_is_released(release_date[:10], today):
                continue
            key = str(row.get("tmdbId") or "").strip() or (
                re.sub(r"[^a-z0-9]+", "", row["title"].casefold())
                + "|" + str(row.get("year") or "")
            )
            if not row.get("id"):
                row["id"] = row.get("tmdbId") or key
            row.setdefault("source", "Bundled Hollywood latest cache")
            rows_by_identity.setdefault(key, row)

    if not rows_by_identity:
        return None

    rows = sorted(
        rows_by_identity.values(),
        key=lambda row: (
            str(row.get("releaseDate") or row.get("release_date") or ""),
            re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").casefold()),
        ),
        reverse=True,
    )
    total_results = len(rows)
    total_pages = max(1, (total_results + page_size - 1) // page_size)
    start = (page - 1) * page_size
    return {
        "catalogue": catalogue_key,
        "releasedOnly": True,
        "provider": "Bundled TMDB cache",
        "page": page,
        "totalPages": total_pages,
        "totalResults": total_results,
        "pageSize": page_size,
        "results": rows[start:start + page_size],
        "releaseEligibility": "streaming-in-india-or-30-days",
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }


def _regional_movie_cache_page_is_usable(payload: dict[str, Any], page: int) -> bool:
    results = payload.get("results")
    if not isinstance(results, list):
        return False
    try:
        total_results = int(payload.get("totalResults") or 0)
        total_pages = int(payload.get("totalPages") or 0)
    except (TypeError, ValueError, OverflowError):
        return bool(results)
    return bool(results) or total_results == 0 or page > total_pages


async def _fetch_paginated_regional_movie_catalogue(catalogue_key: str, page: int) -> dict[str, Any]:
    """Build stable 18-item display pages from eligible regional movie candidates."""
    page_size = REGIONAL_MOVIE_CACHE_PAGE_SIZE
    wanted = page * page_size + 1
    is_trending = catalogue_key.startswith("trending-")
    is_bollywood_latest = catalogue_key == "latest-bollywood"
    force_refresh = _REGIONAL_MOVIE_SOURCE_FORCE_REFRESH.get()
    refreshed_source_keys = _REGIONAL_MOVIE_SOURCE_REFRESHED_KEYS.get()
    # Trending is global; language + India release/OTT eligibility can discard
    # most candidates. Scan up to 30 weekly-trending pages to fill each
    # 18-card regional page without borrowing popularity-ranked Discover rows.
    max_source_pages = min(30, max(3, page)) if is_bollywood_latest else 30
    source_cache_variant = (
        "|trending-v4" if is_trending
        else "|released-v7" if catalogue_key == "latest-bollywood"
        else "|released-v6"
    )
    source_cache_prefix = catalogue_key + source_cache_variant
    seen_ids: set[str] = set()
    eligible_rows: list[dict[str, Any]] = []
    source_total_pages = max_source_pages
    exhausted = True

    for source_page in range(1, max_source_pages + 1):
        cache_key = (source_cache_prefix, source_page)
        refresh_source_page = force_refresh and (
            refreshed_source_keys is None or cache_key not in refreshed_source_keys
        )
        cached = _tmdb_movie_catalogue_cache.get(cache_key)
        source_payload = cached[1] if (
            not refresh_source_page and cached
            and time.monotonic() - cached[0] < REGIONAL_MOVIE_CACHE_TTL_SECONDS
        ) else None
        if source_payload is not None:
            _record_cache_event("regional_movie_source_page", "memory_hit")
        if cached and source_payload is None:
            _tmdb_movie_catalogue_cache.pop(cache_key, None)
        if source_payload is None and not refresh_source_page:
            persistent = _read_tmdb_persistent_cache(cache_key)
            if persistent and 0 <= time.time() - persistent[0] < REGIONAL_MOVIE_CACHE_TTL_SECONDS:
                _record_cache_event("regional_movie_source_page", "persistent_hit")
                source_payload = persistent[1]
                _tmdb_movie_catalogue_cache[cache_key] = (time.monotonic(), source_payload)
        if source_payload is None:
            _record_cache_event("regional_movie_source_page", "miss")
            source_payload = await _tmdb_fetch_movie_catalogue(catalogue_key, source_page, True)
            _tmdb_movie_catalogue_cache[cache_key] = (time.monotonic(), source_payload)
            _write_tmdb_persistent_cache(cache_key, source_payload)
            if refreshed_source_keys is not None:
                refreshed_source_keys.add(cache_key)
        source_total_pages = min(
            max_source_pages,
            500,
            max(1, int(source_payload.get("totalPages") or source_page)),
        )
        filtered_payload = await _apply_movie_release_eligibility(source_payload, enabled=True)
        source_candidates: list[dict[str, Any]] = []
        for row in filtered_payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            if is_bollywood_latest and _is_bollywood_comedy_special(row):
                continue
            title_key = re.sub(r"[^a-z0-9]+", "", str(row.get("title") or row.get("name") or "").casefold())
            # Filter known catalogue contamination before pagination, allowing
            # later source candidates to backfill the page to 18 cards.
            excluded_regional_titles = {"tantesiska", "kunwaridulhan", "mastram"}
            if catalogue_key == "popular-bollywood" and title_key in excluded_regional_titles:
                continue
            identity = str(row.get("id") or row.get("tmdbId") or row.get("title") or "").strip()
            if not identity or identity in seen_ids:
                continue
            seen_ids.add(identity)
            source_candidates.append(row)
        if is_bollywood_latest and source_candidates:
            playable_rows = await _filter_nexstream_playable_movies(source_candidates)
            eligible_rows.extend(playable_rows)
        else:
            eligible_rows.extend(source_candidates)
        if len(eligible_rows) >= wanted:
            exhausted = source_page >= source_total_pages
            break
        if source_page >= source_total_pages:
            exhausted = True
            break
        exhausted = False

    start = (page - 1) * page_size
    page_rows = eligible_rows[start:start + page_size]
    if exhausted:
        total_pages = max(1, (len(eligible_rows) + page_size - 1) // page_size)
    else:
        # Progressive pagination: the next display page is available, and its
        # request will scan further source pages if the user continues.
        total_pages = max(page + 1, (len(eligible_rows) + page_size - 1) // page_size)
    return {
        "catalogue": catalogue_key,
        "releasedOnly": True,
        "provider": "TMDB + verified India OTT availability",
        "page": page,
        "totalPages": total_pages,
        "totalResults": len(eligible_rows) if exhausted else max(len(eligible_rows), (page + 1) * page_size),
        "pageSize": page_size,
        "results": page_rows,
        "releaseEligibility": "streaming-in-india-or-30-days",
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }


async def _enrich_regional_movie_catalogue_metadata(
    payload: dict[str, Any],
) -> dict[str, Any]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        return payload

    semaphore = asyncio.Semaphore(4)

    async def enrich(row: Any) -> None:
        if not isinstance(row, dict):
            return
        try:
            movie_id = int(row.get("tmdbId") or row.get("id") or 0)
        except (TypeError, ValueError):
            movie_id = 0
        title = str(row.get("title") or "").strip()
        try:
            year = int(row.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if movie_id <= 0 or not title:
            return

        async with semaphore:
            try:
                rating = await _get_imdb_rating_record(
                    title, year, "movie", movie_id,
                )
                row["imdbRatingStatus"] = str(rating.get("status") or "unavailable")
                for source_field, target_field in (
                    ("imdbRating", "imdbRating"),
                    ("imdbVotes", "imdbVotes"),
                    ("imdbId", "imdbId"),
                    ("imdbUrl", "imdbUrl"),
                    ("checkedAt", "imdbCheckedAt"),
                ):
                    value = rating.get(source_field)
                    if value is not None:
                        row[target_field] = value
            except Exception:
                logger.warning(
                    "IMDb enrichment failed for regional movie %s (%s)",
                    title, movie_id, exc_info=True,
                )
                row["imdbRatingStatus"] = "unavailable"

            try:
                row["ottAvailability"] = await api_tmdb_movie_ott_availability(
                    movie_id, region="IN",
                )
            except Exception as exc:
                logger.info(
                    "OTT enrichment unavailable for regional movie %s (%s): %s",
                    title, movie_id, type(exc).__name__,
                )
                row["ottAvailability"] = {
                    "movieId": movie_id,
                    "region": "IN",
                    "status": "unavailable",
                    "streamingProviders": [],
                    "rentProviders": [],
                    "buyProviders": [],
                    "checkedAt": datetime.now(timezone.utc).isoformat(),
                }

    await asyncio.gather(*(enrich(row) for row in rows))
    return payload


async def _filter_nexstream_playable_movies(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not nexstream_api_key_configured():
        raise HTTPException(
            status_code=503,
            detail="NEXSTREAM_API_KEY is required to build the playable Bollywood Latest catalogue.",
        )
    if not rows:
        return []

    semaphore = asyncio.Semaphore(4)

    async def check(row: dict[str, Any]) -> dict[str, Any]:
        try:
            movie_id = int(row.get("tmdbId") or row.get("id") or 0)
        except (TypeError, ValueError):
            movie_id = 0
        if movie_id <= 0:
            return {"success": False, "sources": []}
        async with semaphore:
            return await resolve_nexstream_movie(movie_id)

    checks = await asyncio.gather(
        *(check(row) for row in rows),
        return_exceptions=True,
    )
    playable_rows = []
    transient_failures = 0
    for row, result in zip(rows, checks):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            transient_failures += 1
            logger.info(
                "NexStream playback check failed for %s (%s): %s",
                row.get("title"), row.get("id"), type(result).__name__,
            )
            continue
        sources = result.get("sources")
        if result.get("success") and isinstance(sources, list) and sources:
            row["nexStreamPlayable"] = True
            row["nexStreamCheckedAt"] = datetime.now(timezone.utc).isoformat()
            playable_rows.append(row)

    if not playable_rows and transient_failures:
        raise HTTPException(
            status_code=503,
            detail="NexStream checks failed before any playable Bollywood Latest title could be verified.",
        )
    if transient_failures:
        logger.warning(
            "Publishing %s verified NexStream titles; %s movie checks failed.",
            len(playable_rows), transient_failures,
        )
    return playable_rows


@app.get("/api/movies/catalogue/{catalogue_key}")
async def api_tmdb_movie_catalogue(
    catalogue_key: str,
    page: int = Query(1, ge=1, le=500),
    released_only: bool = Query(True),
    max_size_gb: int = Query(5, ge=1, le=5),
    basic: bool = Query(False),
):
    allowed = {
        "marvel", "dc-live-action", "dc-animated",
        "popular-bollywood", "trending-bollywood", "latest-bollywood",
        "popular-hollywood", "trending-hollywood", "latest-hollywood",
    }
    if catalogue_key not in allowed:
        raise HTTPException(status_code=404, detail="Unknown movie catalogue.")

    if basic:
        # Fast first paint for deep pages: return basic source metadata immediately;
        # the frontend replaces this with the fully eligible/enriched page.
        fast_variant = (
            "|fast-basic-v4" if catalogue_key == "latest-bollywood"
            else "|fast-basic-v3" if catalogue_key == "marvel"
            else "|fast-basic-v2"
        )
        fast_key = (catalogue_key + fast_variant, page)
        fast_cached = _tmdb_movie_catalogue_cache.get(fast_key)
        if fast_cached and time.monotonic() - fast_cached[0] < 900:
            _record_cache_event("movie_catalogue", "fast_memory_hit")
            return fast_cached[1]
        _record_cache_event("movie_catalogue", "fast_miss")
        raw = await _tmdb_fetch_movie_catalogue(catalogue_key, page, True)
        rows = [row for row in (raw.get("results") or []) if isinstance(row, dict)]
        normalized_titles = {
            re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").casefold())
            for row in rows
        }
        # Keep the same permanent exclusions in the fast first-paint response.
        excluded_bollywood = {"tantesiska", "kunwaridulhan", "mastram"}
        if catalogue_key == "popular-bollywood":
            rows = [row for row in rows if re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").casefold()) not in excluded_bollywood]
        if catalogue_key in {"popular-bollywood", "trending-bollywood", "latest-bollywood"}:
            rows = [row for row in rows if re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").casefold()) not in {"gangaa", "dearishq", "mastram"}]
        if catalogue_key == "latest-bollywood":
            rows = await _filter_nexstream_playable_movies(rows)
        fast_payload = dict(raw)
        fast_payload.update({"catalogue": catalogue_key, "page": page, "results": rows[:18], "pageSize": 18, "basic": True})
        _tmdb_movie_catalogue_cache[fast_key] = (time.monotonic(), fast_payload)
        return fast_payload

    if released_only and catalogue_key in {
        "popular-bollywood", "trending-bollywood", "latest-bollywood",
        "popular-hollywood", "trending-hollywood", "latest-hollywood",
    }:
        force_refresh = _REGIONAL_MOVIE_CATALOGUE_FORCE_REFRESH.get()
        if catalogue_key.startswith("trending-") and not force_refresh:
            archived_page = _regional_movie_trending_archive_page(catalogue_key, page)
            if archived_page is not None:
                _record_cache_event("movie_catalogue", "weekly_trending_archive_hit")
                return archived_page

        eligible_cache_key = (catalogue_key + "|eligible-v6", page)
        eligible_cached = _tmdb_movie_catalogue_cache.get(eligible_cache_key)
        stale_payload = None
        if eligible_cached:
            cached_payload = eligible_cached[1]
            if _regional_movie_cache_page_is_usable(cached_payload, page):
                stale_payload = cached_payload
                if (
                    not force_refresh
                    and time.monotonic() - eligible_cached[0] < REGIONAL_MOVIE_CACHE_TTL_SECONDS
                ):
                    _record_cache_event("movie_catalogue", "eligible_memory_hit")
                    return cached_payload
            else:
                logger.warning(
                    "Ignoring empty regional movie cache page %s for %s with a positive total",
                    page, catalogue_key,
                )
                _tmdb_movie_catalogue_cache.pop(eligible_cache_key, None)
        eligible_persistent = _read_tmdb_persistent_cache(eligible_cache_key)
        if eligible_persistent:
            payload = eligible_persistent[1]
            if _regional_movie_cache_page_is_usable(payload, page):
                if stale_payload is None:
                    stale_payload = payload
                if (
                    not force_refresh
                    and 0 <= time.time() - eligible_persistent[0] < REGIONAL_MOVIE_CACHE_TTL_SECONDS
                ):
                    _record_cache_event("movie_catalogue", "eligible_persistent_hit")
                    age_seconds = max(0.0, time.time() - eligible_persistent[0])
                    _tmdb_movie_catalogue_cache[eligible_cache_key] = (
                        time.monotonic() - age_seconds,
                        payload,
                    )
                    return payload
            else:
                logger.warning(
                    "Ignoring empty regional movie cache page %s for %s with a positive total",
                    page, catalogue_key,
                )
        if CACHE_ONLY_MODE:
            bundled_payload = _cache_only_regional_movie_catalogue(catalogue_key, page)
            if bundled_payload is not None:
                bundled_payload = await _enrich_regional_movie_catalogue_metadata(
                    bundled_payload,
                )
                _record_cache_event("movie_catalogue", "bundled_cache_hit")
                _tmdb_movie_catalogue_cache[eligible_cache_key] = (time.monotonic(), bundled_payload)
                logger.info(
                    "Serving bundled regional movie catalogue %s page %s (%s titles)",
                    catalogue_key, page, bundled_payload["totalResults"],
                )
                return bundled_payload
            if stale_payload is not None:
                stale_response = dict(stale_payload)
                stale_response["stale"] = True
                stale_response["cacheWarning"] = "Live TMDB refresh is disabled; showing the last saved catalogue."
                return stale_response
            raise HTTPException(
                status_code=503,
                detail=f"No saved movie catalogue is available for {catalogue_key}; configure TMDB credentials to load it.",
            )
        _record_cache_event("movie_catalogue", "eligible_miss")
        try:
            payload = await _fetch_paginated_regional_movie_catalogue(catalogue_key, page)
            payload = await _enrich_regional_movie_catalogue_metadata(payload)
        except Exception as exc:
            if stale_payload is None:
                raise
            logger.exception(
                "Regional movie catalogue refresh failed for %s page %s; serving stale cached data",
                catalogue_key, page,
            )
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = "TMDB is temporarily unavailable; showing the last saved catalogue."
            return stale_response
        _tmdb_movie_catalogue_cache[eligible_cache_key] = (time.monotonic(), payload)
        _write_tmdb_persistent_cache(eligible_cache_key, payload)
        return payload

    # Keep released-only and include-upcoming responses in separate cache slots.
    is_trending_catalogue = catalogue_key.startswith("trending-")
    cache_variant = (
        "|trending-v4" if is_trending_catalogue
        else ("|released-v7" if catalogue_key == "marvel" else "|released-v6") if released_only
        else ("|upcoming-v4" if catalogue_key == "marvel" else "|upcoming-v3")
    )
    cache_key = (catalogue_key + cache_variant, page)
    now = time.monotonic()
    is_regional_movie_catalogue = catalogue_key in {
        "popular-bollywood", "trending-bollywood", "latest-bollywood",
        "popular-hollywood", "trending-hollywood", "latest-hollywood",
    }
    # Regional movie pages 1-3 are durable VPS catalogue snapshots. Keep using
    # them across restarts/TTL expiry; refresh only when a page has never been saved.
    durable_regional_page = is_regional_movie_catalogue and not is_trending_catalogue and page <= 3
    cache_ttl = min(TMDB_CATALOGUE_CACHE_SECONDS, 900) if catalogue_key.startswith("trending-") else TMDB_CATALOGUE_CACHE_SECONDS
    cached = _tmdb_movie_catalogue_cache.get(cache_key)
    if cached and (durable_regional_page or now - cached[0] < cache_ttl):
        _record_cache_event("movie_catalogue", "memory_hit")
        _schedule_tmdb_catalogue_synopsis_enrichment(cached[1])
        return await _apply_movie_release_eligibility(cached[1], enabled=released_only)

    # A disk cache survives container restarts, unlike the in-memory TTL cache.
    persistent = None if is_trending_catalogue else _read_tmdb_persistent_cache(cache_key)
    if persistent:
        saved_at, persistent_payload = persistent
        age_seconds = max(0.0, time.time() - saved_at)
        if durable_regional_page or age_seconds < cache_ttl:
            _record_cache_event("movie_catalogue", "persistent_hit")
            _tmdb_movie_catalogue_cache[cache_key] = (
                time.monotonic() - age_seconds,
                persistent_payload,
            )
            _schedule_tmdb_catalogue_synopsis_enrichment(persistent_payload)
            return await _apply_movie_release_eligibility(persistent_payload, enabled=released_only)

    _record_cache_event("movie_catalogue", "miss")
    task = _tmdb_movie_catalogue_inflight.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(_tmdb_fetch_movie_catalogue(catalogue_key, page, released_only))
        _tmdb_movie_catalogue_inflight[cache_key] = task
    try:
        payload = await task
        _tmdb_movie_catalogue_cache[cache_key] = (time.monotonic(), payload)
        if not is_trending_catalogue:
            _write_tmdb_persistent_cache(cache_key, payload)
        _schedule_tmdb_catalogue_synopsis_enrichment(payload)
        return await _apply_movie_release_eligibility(payload, enabled=released_only)
    except HTTPException as exc:
        # Prefer a stale response over an empty screen when TMDB has an outage.
        stale_payload = cached[1] if cached else (persistent[1] if persistent else None)
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = exc.detail
            logger.warning("Serving stale TMDB catalogue %s page %s after upstream failure", catalogue_key, page)
            _schedule_tmdb_catalogue_synopsis_enrichment(stale_response)
            return await _apply_movie_release_eligibility(stale_response, enabled=released_only)
        raise
    except Exception as exc:
        logger.exception("TMDB movie catalogue failed for %s page %s", catalogue_key, page)
        stale_payload = cached[1] if cached else (persistent[1] if persistent else None)
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = "TMDB is temporarily unavailable; showing the last cached catalogue."
            _schedule_tmdb_catalogue_synopsis_enrichment(stale_response)
            return await _apply_movie_release_eligibility(stale_response, enabled=released_only)
        raise HTTPException(status_code=502, detail="Could not load the movie catalogue from TMDB.") from exc
    finally:
        if task.done() and _tmdb_movie_catalogue_inflight.get(cache_key) is task:
            _tmdb_movie_catalogue_inflight.pop(cache_key, None)


@app.get("/api/movies/ott/{movie_id}")
async def api_tmdb_movie_ott_availability(
    movie_id: int,
    region: str = Query("IN", min_length=2, max_length=2),
    force: bool = Query(False),
):
    """Return cached streaming, rental, purchase and digital-release data for one movie."""
    if movie_id < 1:
        raise HTTPException(status_code=404, detail="Unknown movie.")

    region = region.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", region):
        raise HTTPException(status_code=422, detail="Region must be a two-letter country code.")

    cache_key = (movie_id, region)
    cached = _tmdb_ott_cache.get(cache_key)
    stale_payload = cached[1] if cached else None
    if cached and not force and time.monotonic() - cached[0] < _tmdb_ott_ttl(cached[1]):
        _record_cache_event("ott", "memory_hit")
        return cached[1]

    persistent = None if force else _read_tmdb_ott_persistent_cache(cache_key)
    if persistent:
        saved_at, persistent_payload = persistent
        if 0 <= time.time() - saved_at < _tmdb_ott_ttl(persistent_payload):
            _record_cache_event("ott", "persistent_hit")
            _tmdb_ott_cache[cache_key] = (time.monotonic(), persistent_payload)
            return persistent_payload
        if stale_payload is None:
            stale_payload = persistent_payload

    # Serve expired-but-usable data immediately. Refresh in the background so
    # slow or rate-limited providers cannot block catalogue cards.
    if stale_payload is not None and not force:
        _record_cache_event("ott", "stale_hit")
        task = _tmdb_ott_inflight.get(cache_key)
        if not CACHE_ONLY_MODE and (task is None or task.done()):
            async def refresh_stale_ott_cache():
                try:
                    payload = await _tmdb_fetch_movie_ott_availability(movie_id, region)
                    _tmdb_ott_cache[cache_key] = (time.monotonic(), payload)
                    _write_tmdb_ott_persistent_cache(cache_key, payload)
                except Exception:
                    logger.warning(
                        "Background OTT refresh failed for movie %s in %s; keeping stale cache",
                        movie_id, region, exc_info=True,
                    )
                finally:
                    current = asyncio.current_task()
                    if _tmdb_ott_inflight.get(cache_key) is current:
                        _tmdb_ott_inflight.pop(cache_key, None)

            task = asyncio.create_task(refresh_stale_ott_cache())
            _tmdb_ott_inflight[cache_key] = task
        stale_response = dict(stale_payload)
        stale_response["stale"] = True
        stale_response["cacheWarning"] = "Showing cached OTT availability while it refreshes."
        return stale_response

    # A forced request intentionally bypasses stored entries; otherwise this is
    # a true cache miss. The helper blocks provider lookup when OTT_CACHE_ONLY=true.
    _record_cache_event("ott", "force_bypass" if force else "miss")
    task = _tmdb_ott_inflight.get(cache_key)
    owns_inflight_task = False
    if force:
        task = asyncio.create_task(_tmdb_fetch_movie_ott_availability(movie_id, region))
    elif task is None or task.done():
        task = asyncio.create_task(_tmdb_fetch_movie_ott_availability(movie_id, region))
        _tmdb_ott_inflight[cache_key] = task
        owns_inflight_task = True
    try:
        payload = await task
        _tmdb_ott_cache[cache_key] = (time.monotonic(), payload)
        _write_tmdb_ott_persistent_cache(cache_key, payload)
        return payload
    except HTTPException as exc:
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = exc.detail
            return stale_response
        raise
    except Exception as exc:
        logger.exception("TMDB OTT availability failed for movie %s in %s", movie_id, region)
        if stale_payload:
            stale_response = dict(stale_payload)
            stale_response["stale"] = True
            stale_response["cacheWarning"] = "OTT availability is temporarily unavailable; showing the last cached result."
            return stale_response
        raise HTTPException(status_code=502, detail="Could not load OTT availability from TMDB.") from exc
    finally:
        if owns_inflight_task and task.done() and _tmdb_ott_inflight.get(cache_key) is task:
            _tmdb_ott_inflight.pop(cache_key, None)


def _marvel_ott_normalize_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


async def _resolve_marvel_tmdb_movie_id(title: str, year: int) -> tuple[int | None, str]:
    """Resolve a curated Marvel title from the saved review manifest, then TMDB search."""
    expected = _marvel_ott_normalize_title(title)

    # Reuse the user's manually reviewed manifest when it is mounted in /app/data.
    # This avoids wasting TMDB search calls for IDs that have already been resolved.
    manifest_path = Path(os.getenv(
        "MARVEL_TMDB_ID_REVIEW_PATH",
        "/app/data/marvel_tmdb_id_review.json",
    ))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = manifest.get("movies", []) if isinstance(manifest, dict) else []
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_title = str(row.get("title") or row.get("movieTitle") or row.get("searchTitle") or "")
            row_year = row.get("year")
            row_id = row.get("tmdbId", row.get("movieId", row.get("id")))
            if _marvel_ott_normalize_title(row_title) == expected and (not row_year or int(row_year) == year):
                if isinstance(row_id, int) and row_id > 0:
                    matches.append(row_id)
        if len(set(matches)) == 1:
            return matches[0], "saved_manifest"
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    # Render's OTT cache-only mode must not resolve unknown movie IDs over TMDB.
    # The reviewed manifest above remains the allowed offline ID source.
    if CACHE_ONLY_MODE:
        _record_cache_event("marvel_ott_backfill", "cache_only_id_miss_blocked")
        return None, "cache_only_no_saved_id"

    errors = []
    for params in (
        {"query": title, "year": year, "include_adult": "false", "language": "en-US", "page": 1},
        {"query": title, "include_adult": "false", "language": "en-US", "page": 1},
    ):
        try:
            payload = await _tmdb_get_json("search/movie", params)
        except Exception as exc:
            errors.append(str(getattr(exc, "detail", "") or type(exc).__name__))
            await asyncio.sleep(0.5)
            continue

        candidates = []
        for row in payload.get("results") or []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), int):
                continue
            names = {
                _marvel_ott_normalize_title(row.get("title")),
                _marvel_ott_normalize_title(row.get("original_title")),
            }
            release_year = str(row.get("release_date") or "")[:4]
            if expected in names and (not release_year or release_year == str(year) or "year" not in params):
                candidates.append(row)
        ids = {int(row["id"]) for row in candidates}
        if len(ids) == 1:
            return next(iter(ids)), "exact_match" if "year" in params else "exact_match_year_fallback"
        if len(ids) > 1:
            return None, "ambiguous_exact_match"

    if errors:
        logger.warning("Marvel OTT ID lookup failed for %s (%s): %s", title, year, "; ".join(errors))
        return None, "lookup_failed: " + errors[-1][:180]
    return None, "no_exact_tmdb_match"


@app.get("/api/movies/ott/backfill/marvel")
async def api_backfill_marvel_ott(
    offset: int = Query(0, ge=0),
    limit: int = Query(5, ge=1, le=10),
    region: str = Query("IN", min_length=2, max_length=2),
    force: bool = Query(False),
):
    """One-time, resumable Marvel OTT backfill. Results are saved to the permanent disk cache."""
    region = region.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", region):
        raise HTTPException(status_code=422, detail="Region must be a two-letter country code.")
    total = len(MARVEL_MOVIE_SEARCHES)
    if offset >= total:
        return {"status": "complete", "offset": offset, "limit": limit, "total": total, "processed": 0, "results": []}

    batch = MARVEL_MOVIE_SEARCHES[offset:offset + limit]
    results: list[dict[str, Any]] = []
    for title, year in batch:
        movie_id, resolution = await _resolve_marvel_tmdb_movie_id(title, year)
        if movie_id is None:
            results.append({
                "title": title, "year": year, "status": "unresolved_tmdb_id",
                "resolution": resolution, "movieId": None,
            })
            continue

        cache_key = (movie_id, region)
        saved = _read_tmdb_ott_persistent_cache(cache_key)
        memory = _tmdb_ott_cache.get(cache_key)
        payload = None
        if not force and saved:
            payload = saved[1]
        elif not force and memory:
            payload = memory[1]
        if payload is None:
            try:
                payload = await _tmdb_fetch_movie_ott_availability(movie_id, region)
            except Exception as exc:
                results.append({
                    "title": title, "year": year, "status": "lookup_failed",
                    "resolution": resolution, "movieId": movie_id,
                    "error": str(getattr(exc, "detail", "") or type(exc).__name__),
                })
                continue
            _write_tmdb_ott_persistent_cache(cache_key, payload)
            _tmdb_ott_cache[cache_key] = (time.monotonic(), payload)

        providers = payload.get("streamingProviders") or []
        rent = payload.get("rentProviders") or []
        buy = payload.get("buyProviders") or []
        results.append({
            "title": title, "year": year, "movieId": movie_id,
            "resolution": resolution,
            "status": "streaming_info_found" if providers else "no_streaming_provider_reported",
            "streamingProviders": [p.get("name") for p in providers if isinstance(p, dict)],
            "rentProviders": [p.get("name") for p in rent if isinstance(p, dict)],
            "buyProviders": [p.get("name") for p in buy if isinstance(p, dict)],
            "providerLink": payload.get("providerLink"),
            "cacheSaved": bool(saved) or payload is not None,
        })

    counts: dict[str, int] = {}
    for row in results:
        key = str(row.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "status": "batch_processed",
        "region": region,
        "offset": offset,
        "limit": limit,
        "total": total,
        "processed": len(results),
        "nextOffset": offset + len(batch),
        "counts": counts,
        "results": results,
    }


@app.get("/api/catalogue/extra/{catalogue_key}")
async def api_extra_catalogue(catalogue_key: str, request: Request, page: int = Query(0, ge=0), page_size: int = Query(0, ge=0, le=200)):
    if catalogue_key not in _EXTRA_CATALOGUES:
        raise HTTPException(404, "Unknown catalogue")
    if CACHE_ONLY_MODE:
        prebuilt = _prebuilt_catalogue_page(request, catalogue_key, page, page_size)
        if prebuilt is not None:
            return prebuilt
    cached = _read_extra_catalogue(catalogue_key)
    _record_cache_event("extra_catalogue", "hit" if cached else "miss")
    if cached and CACHE_ONLY_MODE:
        return _fast_json_response(request, "extra:%s:%d:%d" % (catalogue_key, page, page_size), _file_signature(_EXTRA_CATALOGUE_PATHS[catalogue_key]), lambda: _page_catalogue_payload({
            "status": "ready", "builtAt": cached.get("builtAt"),
            "completed": int(cached.get("titlesQueried") or 0), "total": int(cached.get("titlesQueried") or 0),
            "resultCount": int(cached.get("resultCount") or len(cached["results"])),
            "movieCount": int(cached.get("movieCount") or 0), "refreshing": False,
            "results": cached["results"]}, page, page_size,
            sort_by_release_date=catalogue_key in _DATE_SORTED_EXTRA_CATALOGUES))
    if catalogue_key == "classics" and cached:
        _schedule_classics_movie_ott_enrichment()
    await _ensure_extra_catalogue_refresh(catalogue_key)
    state = _extra_catalogue_states[catalogue_key]
    # For monthly catalogues, stale-but-useful data is returned immediately while
    # the refresh runs; first-ever builds report progress for the UI to poll.
    if cached:
        response_payload = {
            "status": "ready",
            "builtAt": cached.get("builtAt"),
            "completed": int(cached.get("titlesQueried") or 0),
            "total": int(cached.get("titlesQueried") or 0),
            "resultCount": int(cached.get("resultCount") or len(cached["results"])),
            "movieCount": int(cached.get("movieCount") or 0),
            "refreshing": bool(_extra_catalogue_tasks.get(catalogue_key) and not _extra_catalogue_tasks[catalogue_key].done()),
            "results": cached["results"],
        }
        if page > 0 and page_size > 0:
            return _fast_json_response(
                request,
                "extra:%s:%d:%d" % (catalogue_key, page, page_size),
                _file_signature(_EXTRA_CATALOGUE_PATHS[catalogue_key]),
                lambda: _page_catalogue_payload(
                    response_payload,
                    page,
                    page_size,
                    sort_by_release_date=catalogue_key in _DATE_SORTED_EXTRA_CATALOGUES,
                ),
            )
        return response_payload
    return {**state, "results": []}


_classics_ott_enrichment_task: asyncio.Task | None = None

async def _enrich_classics_movie_ott_cache() -> None:
    """Backfill India OTT metadata for existing Favourites movie cache rows."""
    key = "classics"
    path = _EXTRA_CATALOGUE_PATHS[key]
    try:
        payload = _read_extra_catalogue(key)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return
        rows = payload["results"]
        semaphore = asyncio.Semaphore(3)
        changed = False

        async def enrich(row: dict[str, Any]) -> None:
            nonlocal changed
            existing = row.get("ottAvailability")
            if isinstance(existing, dict):
                checked_at = str(existing.get("checkedAt") or "").strip()
                status = str(existing.get("status") or "").casefold()
                try:
                    checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                    if checked.tzinfo is None:
                        checked = checked.replace(tzinfo=timezone.utc)
                    fresh = 0 <= (datetime.now(timezone.utc) - checked).total_seconds() < _tmdb_ott_ttl(existing)
                except (TypeError, ValueError):
                    fresh = False
                if fresh and status not in {"unknown", "error", "pending"}:
                    return

            title = str(row.get("catalogueTitle") or row.get("mediaTitle") or row.get("title") or "").strip()
            if not title:
                return
            try:
                year = int(row.get("year") or _extra_item_year(row))
            except (TypeError, ValueError):
                year = 0
            tmdb_id = int(row.get("tmdbId") or 0) if str(row.get("tmdbId") or "").isdigit() else 0
            async with semaphore:
                try:
                    # If the catalogue row already has a TMDB ID, consult the shared
                    # cache before resolving metadata. The previous order performed a
                    # network metadata lookup for every Favourites row on every cold
                    # start, even when OTT availability was already cached.
                    availability = None
                    resolved_id = tmdb_id
                    if resolved_id > 0:
                        cache_key = (resolved_id, "IN")
                        memory = _tmdb_ott_cache.get(cache_key)
                        if memory and isinstance(memory[1], dict):
                            age = max(0.0, time.monotonic() - memory[0])
                            if age < _tmdb_ott_ttl(memory[1]):
                                availability = memory[1]
                        if availability is None:
                            persistent = _read_tmdb_ott_persistent_cache(cache_key)
                            if persistent and isinstance(persistent[1], dict):
                                age = max(0.0, time.time() - persistent[0])
                                if age < _tmdb_ott_ttl(persistent[1]):
                                    availability = persistent[1]
                                    _tmdb_ott_cache[cache_key] = (time.monotonic() - age, availability)
                        if isinstance(availability, dict) and str(availability.get("status") or "").casefold() not in {"unknown", "error", "pending"}:
                            row["ottAvailability"] = availability
                            row["ottEnrichedAt"] = datetime.now(timezone.utc).isoformat()
                            changed = True
                            return

                    # In cache-only mode, a missing OTT entry must not fan out into
                    # Watchmode/TMDB title-search calls just to obtain an ID for a backfill.
                    if CACHE_ONLY_MODE:
                        _record_cache_event("classics_ott_enrichment", "cache_only_miss_skipped")
                        return

                    # Only resolve metadata when there is no usable cached OTT entry.
                    metadata = await asyncio.wait_for(_lookup_catalogue_synopsis(title, year, tmdb_id), timeout=15)
                    resolved_id = int((metadata or {}).get("tmdbId") or (metadata or {}).get("id") or tmdb_id)
                    if resolved_id <= 0:
                        return
                    cache_key = (resolved_id, "IN")
                    if availability is None:
                        memory = _tmdb_ott_cache.get(cache_key)
                        if memory and isinstance(memory[1], dict):
                            age = max(0.0, time.monotonic() - memory[0])
                            if age < _tmdb_ott_ttl(memory[1]):
                                availability = memory[1]
                    if availability is None:
                        persistent = _read_tmdb_ott_persistent_cache(cache_key)
                        if persistent and isinstance(persistent[1], dict):
                            age = max(0.0, time.time() - persistent[0])
                            if age < _tmdb_ott_ttl(persistent[1]):
                                availability = persistent[1]
                                _tmdb_ott_cache[cache_key] = (time.monotonic() - age, availability)
                    if availability is None:
                        availability = await asyncio.wait_for(_tmdb_fetch_movie_ott_availability(resolved_id, "IN"), timeout=18)
                        if isinstance(availability, dict):
                            _write_tmdb_ott_persistent_cache(cache_key, availability)
                            _tmdb_ott_cache[cache_key] = (time.monotonic(), availability)
                    if not isinstance(availability, dict) or str(availability.get("status") or "").casefold() in {"unknown", "error", "pending"}:
                        return
                    row["tmdbId"] = resolved_id
                    row["ottAvailability"] = availability
                    _tmdb_ott_cache[(resolved_id, "IN")] = (time.monotonic(), availability)
                    _write_tmdb_ott_persistent_cache((resolved_id, "IN"), availability)
                    row["ottEnrichedAt"] = datetime.now(timezone.utc).isoformat()
                    if metadata:
                        for field in ("tmdbUrl", "releaseDate", "rating", "posterUrl", "backdropUrl", "overview", "genres"):
                            if not row.get(field) and metadata.get(field):
                                row[field] = metadata[field]
                    changed = True
                except Exception as exc:
                    logger.info("Favourites OTT enrichment failed for %s: %s", title, type(exc).__name__)

        await asyncio.gather(*(enrich(row) for row in rows if isinstance(row, dict)))
        if changed:
            payload["results"] = rows
            payload["ottEnrichedAt"] = datetime.now(timezone.utc).isoformat()
            _write_extra_catalogue(key, payload)
            logger.info("Enriched Favourites movie cache with India OTT data")
    except Exception:
        logger.exception("Favourites movie OTT cache enrichment failed")


def _schedule_classics_movie_ott_enrichment() -> None:
    global _classics_ott_enrichment_task
    if CACHE_ONLY_MODE:
        return
    if _classics_ott_enrichment_task is None or _classics_ott_enrichment_task.done():
        _classics_ott_enrichment_task = asyncio.create_task(_enrich_classics_movie_ott_cache())


async def _warm_extra_catalogues_on_startup() -> None:
    # Catalogue builds are deliberately lazy. The old startup warm-up launched
    # all stale/missing catalogues together and flooded every upstream provider.
    asyncio.create_task(_extra_catalogue_monthly_scheduler())


app.router.add_event_handler("startup", _warm_extra_catalogues_on_startup)


def _regional_movie_cache_refresh_delay() -> float:
    oldest_saved_at = time.time()
    for catalogue_key in REGIONAL_MOVIE_CATALOGUE_KEYS:
        for page in range(1, REGIONAL_MOVIE_PREWARM_PAGES + 1):
            cached = _read_tmdb_persistent_cache((catalogue_key + "|eligible-v6", page))
            if not cached or not _regional_movie_cache_page_is_usable(cached[1], page):
                return 0
            oldest_saved_at = min(oldest_saved_at, cached[0])

    archive = _read_regional_movie_trending_archive()
    if not archive.get("builtAt"):
        return 0
    try:
        archive_built_at = datetime.fromisoformat(
            str(archive["builtAt"]).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0
    oldest_saved_at = min(oldest_saved_at, archive_built_at)
    return max(0.0, REGIONAL_MOVIE_CACHE_TTL_SECONDS - (time.time() - oldest_saved_at))


async def _warm_regional_movie_catalogue_pages() -> bool:
    if CACHE_ONLY_MODE:
        logger.info("Regional movie catalogue warm-up skipped: cache-only mode is enabled.")
        return True

    refresh_succeeded = True
    refreshed_source_keys: set[tuple[str, int]] = set()
    for catalogue_key in REGIONAL_MOVIE_CATALOGUE_KEYS:
        is_trending = catalogue_key.startswith("trending-")
        catalogue_refresh_succeeded = True
        trending_rows: list[dict[str, Any]] = []
        for page in range(1, REGIONAL_MOVIE_PREWARM_PAGES + 1):
            refresh_token = _REGIONAL_MOVIE_CATALOGUE_FORCE_REFRESH.set(True)
            source_token = _REGIONAL_MOVIE_SOURCE_FORCE_REFRESH.set(True)
            refreshed_token = _REGIONAL_MOVIE_SOURCE_REFRESHED_KEYS.set(refreshed_source_keys)
            try:
                payload = await api_tmdb_movie_catalogue(
                    catalogue_key,
                    page=page,
                    released_only=True,
                    max_size_gb=5,
                    basic=False,
                )
            except Exception:
                refresh_succeeded = False
                catalogue_refresh_succeeded = False
                logger.exception(
                    "Weekly regional movie cache refresh failed for %s page %s",
                    catalogue_key, page,
                )
                continue
            finally:
                _REGIONAL_MOVIE_SOURCE_REFRESHED_KEYS.reset(refreshed_token)
                _REGIONAL_MOVIE_SOURCE_FORCE_REFRESH.reset(source_token)
                _REGIONAL_MOVIE_CATALOGUE_FORCE_REFRESH.reset(refresh_token)

            result_count = len(payload.get("results") or [])
            if payload.get("cacheWarning"):
                refresh_succeeded = False
                catalogue_refresh_succeeded = False
                logger.warning(
                    "Regional movie cache kept stale data for %s page %s: %s",
                    catalogue_key, page, payload["cacheWarning"],
                )
            elif result_count != REGIONAL_MOVIE_CACHE_PAGE_SIZE:
                logger.warning(
                    "Regional movie cache %s page %s has %s titles; TMDB may have fewer eligible titles.",
                    catalogue_key, page, result_count, REGIONAL_MOVIE_CACHE_PAGE_SIZE,
                )
            else:
                logger.info(
                    "Regional movie cache ready: %s page %s (%s titles).",
                    catalogue_key, page, result_count,
                )
            if is_trending:
                trending_rows.extend(
                    row for row in (payload.get("results") or []) if isinstance(row, dict)
                )

        if is_trending:
            week_of = _regional_movie_week_start()
            if trending_rows and catalogue_refresh_succeeded:
                if _save_regional_movie_trending_week(week_of, catalogue_key, trending_rows):
                    logger.info(
                        "Saved %s unique weekly trend candidates for %s (%s).",
                        len(trending_rows), catalogue_key, week_of,
                    )
                else:
                    refresh_succeeded = False
            elif not trending_rows:
                refresh_succeeded = False
                logger.warning(
                    "No weekly trending candidates were returned for %s; preserving earlier snapshots.",
                    catalogue_key,
                )
    return refresh_succeeded


async def _regional_movie_catalogue_weekly_scheduler() -> None:
    while True:
        delay = _regional_movie_cache_refresh_delay()
        if delay > 0:
            logger.info(
                "Regional movie caches are current; next weekly refresh in %s seconds.",
                int(delay),
            )
            await asyncio.sleep(delay)

        refresh_succeeded = await _warm_regional_movie_catalogue_pages()
        delay = REGIONAL_MOVIE_CACHE_TTL_SECONDS if refresh_succeeded else 6 * 60 * 60
        logger.info(
            "Next regional movie catalogue refresh in %s seconds.",
            int(delay),
        )
        await asyncio.sleep(delay)


async def _start_regional_movie_catalogue_scheduler() -> None:
    asyncio.create_task(_regional_movie_catalogue_weekly_scheduler())


app.router.add_event_handler("startup", _start_regional_movie_catalogue_scheduler)


@app.get("/api/catalogue/ott/search")
async def api_search_saved_ott(titles: list[str] = Query(..., min_length=1, max_length=80)):
    """Look up saved OTT enrichment by title from persistent franchise catalogue caches only."""
    _record_cache_event("saved_ott_title_search", "lookup")
    # Read the persistent OTT JSON once for the entire title search. The cache
    # file may contain over a thousand rows; parsing the complete file once per
    # catalogue row turns a cache-only lookup into unnecessary repeated I/O.
    try:
        ott_disk = json.loads(TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
        ott_items = ott_disk.get("items", {}) if isinstance(ott_disk, dict) else {}
        if not isinstance(ott_items, dict):
            ott_items = {}
    except (OSError, ValueError, TypeError):
        ott_items = {}

    source_paths = [MARVEL_CATALOGUE_PATH, *_EXTRA_CATALOGUE_PATHS.values()]
    saved_rows: list[dict[str, Any]] = []
    seen_rows: set[tuple[str, str]] = set()
    for source_path in source_paths:
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("catalogueTitle") or row.get("marvelCatalogueTitle") or row.get("mediaTitle") or row.get("title") or "").strip()
            if not title:
                continue
            year = str(row.get("year") or "")
            identity = (_normalize_title(re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", title)), year)
            if identity in seen_rows:
                continue
            availability = row.get("ottAvailability")
            tmdb_id = row.get("tmdbId")
            try:
                tmdb_id = int(tmdb_id or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            # Catalogue rows are not the only source of saved OTT data. The shared
            # runtime cache is bundled into the image and may already contain a
            # result for this TMDB ID even when the catalogue row predates enrichment.
            if not isinstance(availability, dict) and tmdb_id > 0:
                cached = _tmdb_ott_cache.get((tmdb_id, "IN"))
                if cached and isinstance(cached[1], dict):
                    availability = cached[1]
                if not isinstance(availability, dict):
                    envelope = ott_items.get(_tmdb_ott_disk_key((tmdb_id, "IN")))
                    persistent_payload = envelope.get("payload") if isinstance(envelope, dict) else None
                    if isinstance(persistent_payload, dict):
                        availability = persistent_payload
                        _tmdb_ott_cache[(tmdb_id, "IN")] = (time.monotonic(), availability)
            if not isinstance(availability, dict):
                continue
            seen_rows.add(identity)
            saved_rows.append({
                "title": title,
                "year": year or None,
                "tmdbId": tmdb_id or row.get("tmdbId"),
                "ottAvailability": availability,
            })

    # Also use title-to-TMDB-ID mappings in the general saved metadata/catalogue
    # cache. This makes OTT entries reusable even when their title is not in a
    # Marvel/DC/Classics catalogue snapshot.
    for row in _read_search_metadata_catalogue_rows():
        if not isinstance(row, dict):
            continue
        raw_title = str(
            row.get("catalogueTitle") or row.get("marvelCatalogueTitle")
            or row.get("mediaTitle") or row.get("title") or row.get("name") or ""
        ).strip()
        if not raw_title:
            continue
        try:
            tmdb_id = int(row.get("tmdbId") or row.get("id") or 0)
        except (TypeError, ValueError):
            tmdb_id = 0
        if tmdb_id <= 0:
            continue
        availability = row.get("ottAvailability")
        if not isinstance(availability, dict):
            envelope = ott_items.get(_tmdb_ott_disk_key((tmdb_id, "IN")))
            availability = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(availability, dict):
            continue
        year_value = row.get("year")
        title_identity = (
            _normalize_title(re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", raw_title)),
            str(year_value or ""),
        )
        if title_identity in seen_rows:
            continue
        seen_rows.add(title_identity)
        saved_rows.append({
            "title": raw_title,
            "year": str(year_value or "") or None,
            "tmdbId": tmdb_id,
            "ottAvailability": availability,
        })

    requested = []
    for raw in titles:
        title = re.sub(r"\s+", " ", str(raw or "")).strip()
        if title:
            requested.append({"title": title, "normalized": _normalize_title(re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", title))})

    matches = []
    for request in requested:
        candidates = [row for row in saved_rows
            if _normalize_title(re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", row["title"])) == request["normalized"]]
        if candidates:
            matches.append({"query": request["title"], "found": True, "entries": candidates[:3]})
        else:
            matches.append({"query": request["title"], "found": False, "entries": []})
    matched_count = sum(1 for item in matches if item.get("found"))
    _record_cache_event("saved_ott_title_search", "title_hit" if matched_count else "title_miss")
    _record_cache_event("saved_ott_title_search", "matched_rows", sum(len(item.get("entries") or []) for item in matches))
    return {"status": "ready", "results": matches}


@app.get("/api/catalogue/marvel")
async def api_marvel_catalogue(request: Request, page: int = Query(0, ge=0), page_size: int = Query(0, ge=0, le=200)):
    global _marvel_catalogue_task
    if CACHE_ONLY_MODE:
        prebuilt = _prebuilt_catalogue_page(request, "marvel", page, page_size)
        if prebuilt is not None:
            return prebuilt
    cached = _merge_bundled_marvel_catalogue_cache(_read_marvel_catalogue())
    _record_cache_event("marvel_catalogue", "hit" if cached else "miss")
    if cached and CACHE_ONLY_MODE:
        return _fast_json_response(request, "marvel-page:%d:%d" % (page, page_size), _file_signature(MARVEL_CATALOGUE_PATH), lambda: _page_catalogue_payload({
            "status": "ready",
            "builtAt": cached.get("builtAt"),
            "completed": int(cached.get("titlesQueried") or len(MARVEL_MOVIE_SEARCHES)),
            "total": int(cached.get("titlesQueried") or len(MARVEL_MOVIE_SEARCHES)),
            "resultCount": int(cached.get("resultCount") or len(cached["results"])),
            "movieCount": int(cached.get("movieCount") or 0),
            "results": cached["results"],
            "refreshing": False,
        }, page, page_size, sort_by_release_date=True))
    if cached:
        # Keep the current cache available immediately while rebuilding it once
        # when its version predates the updated MCU film list.
        if int(cached.get("version") or 0) < MARVEL_CATALOGUE_VERSION:
            if _marvel_catalogue_task is None or _marvel_catalogue_task.done():
                if not (_marvel_catalogue_state.get("status") == "failed" and time.time() < _marvel_catalogue_retry_after):
                    _marvel_catalogue_state.update({
                        "status": "building",
                        "completed": 0,
                        "total": len(MARVEL_MOVIE_SEARCHES),
                        "resultCount": int(cached.get("resultCount") or len(cached["results"])),
                        "movieCount": int(cached.get("movieCount") or 0),
                        "error": "",
                    })
                    _marvel_catalogue_task = asyncio.create_task(_build_marvel_catalogue())
        response_payload = {
            "status": "ready",
            "builtAt": cached.get("builtAt"),
            "completed": int(cached.get("titlesQueried") or len(MARVEL_MOVIE_SEARCHES)),
            "total": int(cached.get("titlesQueried") or len(MARVEL_MOVIE_SEARCHES)),
            "resultCount": int(cached.get("resultCount") or len(cached["results"])),
            "movieCount": int(cached.get("movieCount") or 0),
            "results": cached["results"],
            "refreshing": int(cached.get("version") or 0) < MARVEL_CATALOGUE_VERSION,
        }
        if page > 0 and page_size > 0:
            return _fast_json_response(
                request,
                "marvel-page:%d:%d" % (page, page_size),
                _file_signature(MARVEL_CATALOGUE_PATH),
                lambda: _page_catalogue_payload(response_payload, page, page_size, sort_by_release_date=True),
            )
        return response_payload

    if _marvel_catalogue_task is None or _marvel_catalogue_task.done():
        if _marvel_catalogue_state.get("status") == "failed" and time.time() < _marvel_catalogue_retry_after:
            return {**_marvel_catalogue_state, "results": []}
        _marvel_catalogue_state.update({
            "status": "building",
            "completed": 0,
            "total": len(MARVEL_MOVIE_SEARCHES),
            "resultCount": 0,
            "error": "",
        })
        _marvel_catalogue_task = asyncio.create_task(_build_marvel_catalogue())

    return {
        **_marvel_catalogue_state,
        "total": len(MARVEL_MOVIE_SEARCHES),
        "results": [],
    }


async def _warm_marvel_catalogue_on_startup() -> None:
    """Keep startup quiet; the Marvel catalogue builds when first requested."""
    if _read_marvel_catalogue():
        logger.info("Persistent Marvel catalogue is available; startup warm-up skipped.")
    else:
        logger.info("Marvel catalogue build deferred until the catalogue is requested.")


app.router.add_event_handler("startup", _warm_marvel_catalogue_on_startup)


_watchmode_search_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_WATCHMODE_SEARCH_CACHE_TTL = 6 * 60 * 60
_TVMAZE_SEARCH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _availability_normalize_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _availability_title_is_relevant(query: str, title: Any) -> bool:
    """Reject distant partial-title matches while allowing modest subtitle/region suffixes."""
    target = _availability_normalize_title(query)
    candidate = _availability_normalize_title(title)
    if not target or not candidate:
        return False
    if target == candidate:
        return True
    # Containment is useful for regional editions and subtitles, but not arbitrary
    # longer titles that happen to begin with the requested words.
    max_extra = max(4, int(len(target) * 0.5))
    if target in candidate:
        return len(candidate) - len(target) <= max_extra
    if candidate in target:
        return len(target) - len(candidate) <= max_extra
    return False


def _availability_canonical_type(value: Any) -> str:
    kind = str(value or "").casefold().replace("-", "_").replace(" ", "_")
    if any(token in kind for token in ("tv", "series", "show", "episode")):
        return "tv"
    if any(token in kind for token in ("movie", "film", "feature")):
        return "movie"
    return kind or "unknown"


async def _fallback_tvmaze_search(query: str, timeout: httpx.Timeout) -> list[dict[str, Any]]:
    """Metadata-only fallback used only when Watchmode and TMDB find no title."""
    cache_key = query.casefold()
    cached = _TVMAZE_SEARCH_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _WATCHMODE_SEARCH_CACHE_TTL:
        return cached[1]
    try:
        async with _InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get("https://api.tvmaze.com/search/shows", params={"q": query})
        if response.status_code != 200:
            return []
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return []
    target = _availability_normalize_title(query)
    matches = []
    for row in payload if isinstance(payload, list) else []:
        show = row.get("show") if isinstance(row, dict) else None
        if not isinstance(show, dict) or not show.get("id"):
            continue
        title = str(show.get("name") or "").strip()
        if not title:
            continue
        normalized = _availability_normalize_title(title)
        if not _availability_title_is_relevant(query, title):
            continue
        premiered = str(show.get("premiered") or "")
        network = show.get("network") or show.get("webChannel") or {}
        matches.append({
            "id": str(show["id"]), "title": title, "year": premiered[:4] or None,
            "type": "tv", "providers": [], "status": "not_confirmed", "source": "TVmaze",
            "tvmazeId": show["id"], "imdbId": (show.get("externals") or {}).get("imdb"),
            "overview": re.sub(r"<[^>]+>", " ", str(show.get("summary") or "")).strip(),
            "network": network.get("name") if isinstance(network, dict) else None,
            "url": show.get("url"),
        })
    matches.sort(key=lambda item: (
        _availability_normalize_title(item.get("title")) == target,
        bool(item.get("year")),
    ), reverse=True)
    matches = matches[:5]
    _TVMAZE_SEARCH_CACHE[cache_key] = (time.time(), matches)
    return matches


async def _fallback_omdb_search(query: str, timeout: httpx.Timeout) -> list[dict[str, Any]]:
    """OMDb metadata fallback; OMDb does not provide streaming availability."""
    keys = _split_env_keys(os.getenv("OMDB_API_KEYS", ""), os.getenv("OMDB_API_KEY", ""))
    if CACHE_ONLY_MODE or not keys:
        return []
    target = _availability_normalize_title(query)
    for key in keys:
        try:
            async with _InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get("https://www.omdbapi.com/", params={"apikey": key, "s": query})
            if response.status_code != 200:
                continue
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            continue
        matches = []
        for row in payload.get("Search") or []:
            title = str(row.get("Title") or "").strip()
            imdb_id = str(row.get("imdbID") or "").strip()
            if not title or not imdb_id:
                continue
            normalized = _availability_normalize_title(title)
            if not _availability_title_is_relevant(query, title):
                continue
            kind = "tv" if str(row.get("Type") or "").casefold() == "series" else "movie"
            matches.append({
                "id": imdb_id, "title": title, "year": row.get("Year"), "type": kind,
                "providers": [], "status": "not_confirmed", "source": "OMDb",
                "imdbId": imdb_id, "poster": row.get("Poster"),
            })
        if matches:
            matches.sort(key=lambda item: _availability_normalize_title(item["title"]) == target, reverse=True)
            return matches[:5]
        # Search returned no titles; try the next configured key only on auth errors
        # is intentionally avoided here so rate limits cannot be bypassed.
        if payload.get("Response") == "False":
            return []
    return []


@app.get("/api/search/availability")
async def api_search_availability(q: str = Query(..., min_length=2, max_length=160)):
    """Bound streaming lookups so slow provider APIs cannot leave the UI scanning forever."""
    query = re.sub(r"\s+", " ", q).strip()
    try:
        # Upstream providers have their own timeouts and retries; apply one total
        # budget to the whole lookup so nested retries cannot accumulate indefinitely.
        return await asyncio.wait_for(_api_search_availability_impl(query), timeout=12.0)
    except asyncio.TimeoutError:
        logger.info("Streaming availability lookup timed out for query %r", query)
        return {
            "status": "unavailable",
            "query": query,
            "title": query,
            "year": None,
            "matches": [],
            "providers": [],
            "message": "Streaming availability timed out. Torrent results are still available.",
        }


async def _api_search_availability_impl(q: str) -> dict[str, Any]:
    """Search Watchmode and TMDB first; use TVmaze/OMDb for metadata only if neither finds a title."""
    query = re.sub(r"\s+", " ", q).strip()
    cache_key = query.casefold()
    now = time.time()
    cached = _watchmode_search_cache.get(cache_key)
    if cached and now - cached[0] < _WATCHMODE_SEARCH_CACHE_TTL:
        _record_cache_event("availability_search", "hit")
        return cached[1]

    _record_cache_event("availability_search", "miss")
    if CACHE_ONLY_MODE:
        # This endpoint previously searched Watchmode/TMDB directly, bypassing the
        # movie-ID OTT cache-only guard. Reuse saved, title-matched OTT enrichment.
        try:
            saved_payload = await api_search_saved_ott(titles=[query])
        except Exception:
            saved_payload = {"results": []}
        saved_results = saved_payload.get("results") if isinstance(saved_payload, dict) else []
        saved_match = next((
            item for item in saved_results
            if isinstance(item, dict) and _availability_normalize_title(item.get("query")) == _availability_normalize_title(query)
        ), {})
        saved_entries = saved_match.get("entries") if isinstance(saved_match, dict) else []
        matches: list[dict[str, Any]] = []
        merged_providers: list[dict[str, Any]] = []
        seen_providers: set[tuple[str, str]] = set()
        for entry in saved_entries if isinstance(saved_entries, list) else []:
            if not isinstance(entry, dict):
                continue
            availability = entry.get("ottAvailability")
            if not isinstance(availability, dict):
                continue
            providers: list[dict[str, Any]] = []
            for source_field, kind in (
                ("streamingProviders", "subscription"),
                ("rentProviders", "rent"),
                ("buyProviders", "buy"),
            ):
                for provider in availability.get(source_field) or []:
                    if not isinstance(provider, dict):
                        continue
                    name = str(provider.get("name") or "").strip()
                    if not name:
                        continue
                    normalized_provider = {
                        "providerId": provider.get("providerId"),
                        "name": name,
                        "region": str(availability.get("region") or "IN").upper(),
                        "type": kind,
                        "webUrl": str(provider.get("webUrl") or availability.get("providerLink") or ""),
                        "logoUrl": str(provider.get("logoUrl") or ""),
                    }
                    providers.append(normalized_provider)
                    identity = (name.casefold(), kind)
                    if identity not in seen_providers:
                        seen_providers.add(identity)
                        merged_providers.append(normalized_provider)
            matches.append({
                "id": str(entry.get("tmdbId") or ""),
                "tmdbId": entry.get("tmdbId"),
                "title": str(entry.get("title") or query),
                "year": entry.get("year"),
                "type": "movie",
                "providers": providers[:24],
                "status": "ready" if providers else "not_confirmed",
                "source": str(availability.get("source") or "Saved OTT cache"),
            })
        result = {
            "status": "ready" if merged_providers else ("not_confirmed" if matches else "not_cached"),
            "query": query,
            "title": matches[0].get("title", query) if matches else query,
            "year": matches[0].get("year") if matches else None,
            "matches": matches[:10],
            "providers": merged_providers[:24],
            "source": "Saved OTT cache",
            "cacheOnly": True,
            "message": "" if matches else "No saved OTT entry matched this title; live provider lookups are disabled on this service.",
        }
        _watchmode_search_cache[cache_key] = (time.time(), result)
        _record_cache_event("availability_search", "cache_only_saved_hit" if matches else "cache_only_miss_blocked")
        return result

    timeout = httpx.Timeout(8.0, connect=3.0)
    target = _availability_normalize_title(query)
    primary_matches: list[dict[str, Any]] = []
    primary_errors = []

    # Primary source 1: Watchmode. Search both films and TV series automatically.
    if WATCHMODE_API_KEYS:
        try:
            response = await _watchmode_get("/search/", params={
                "search_field": "name", "search_value": query, "types": "movie,tv_series,tv_miniseries,tv_movie"
            }, timeout=timeout)
            if response is not None and response.status_code < 400:
                data = response.json()
                titles = data.get("title_results", []) if isinstance(data, dict) else []
                titles = [item for item in titles if isinstance(item, dict) and item.get("id")
                    and _availability_title_is_relevant(query, item.get("name"))]
                titles.sort(key=lambda item: (
                    _availability_normalize_title(item.get("name")) == target,
                    bool(item.get("year")),
                ), reverse=True)
                selected, seen_ids = [], set()
                for item in titles:
                    wid = str(item.get("id") or "")
                    if not wid or wid in seen_ids:
                        continue
                    kind = str(item.get("type") or "").casefold()
                    if kind and not any(token in kind for token in ("movie", "film", "feature", "tv", "show", "series")):
                        continue
                    seen_ids.add(wid)
                    selected.append(item)
                    if len(selected) >= 6:
                        break

                async def resolve_watchmode(item: dict[str, Any]) -> dict[str, Any]:
                    wid = str(item.get("id") or "")
                    kind = str(item.get("type") or "").lower()
                    try:
                        resp = await _watchmode_get(
                            f"/title/{quote(wid, safe='')}/sources/",
                            params={"regions": "IN"}, timeout=timeout)
                        raw = resp.json() if resp is not None and resp.status_code < 400 else []
                    except (httpx.HTTPError, ValueError, TypeError):
                        raw = []
                    providers, seen_sources = [], set()
                    for source in raw if isinstance(raw, list) else []:
                        if not isinstance(source, dict):
                            continue
                        name = str(source.get("name") or source.get("source_name") or "").strip()
                        if not name:
                            continue
                        region = str(source.get("region") or "IN").upper()
                        provider_type = str(source.get("type") or "").lower()
                        identity = (name.casefold(), region, provider_type)
                        if identity in seen_sources:
                            continue
                        seen_sources.add(identity)
                        logo_url = str(
                            source.get("logo_url") or source.get("logoUrl") or source.get("logo")
                            or source.get("image_url") or source.get("image") or ""
                        ).strip()
                        logo_path = str(source.get("logo_path") or "").strip()
                        if not logo_url and logo_path:
                            logo_url = (
                                "https://image.tmdb.org/t/p/w92" + logo_path
                                if logo_path.startswith("/") else logo_path
                            )
                        providers.append({
                            "providerId": source.get("source_id", source.get("id")),
                            "name": name,
                            "region": region,
                            "type": provider_type,
                            "webUrl": source.get("web_url") or source.get("ios_url") or source.get("android_url") or "",
                            "logoUrl": logo_url,
                        })
                    return {"id": wid, "title": str(item.get("name") or query), "year": item.get("year"),
                        "type": kind or "unknown", "tmdbId": item.get("tmdb_id"), "providers": providers[:24],
                        "status": "ready" if providers else "not_confirmed", "source": "Watchmode"}

                primary_matches.extend(await asyncio.gather(*(resolve_watchmode(item) for item in selected)))
            else:
                primary_errors.append("Watchmode unavailable")
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            primary_errors.append("Watchmode " + type(exc).__name__)

    # Primary source 2: search movies and TV concurrently. A movie miss or a
    # movie with no providers must not delay or suppress the TV-series lookup.
    if TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS:
        media_specs = (
            ("movie", "search/movie", "title", "release_date"),
            ("tv", "search/tv", "name", "first_air_date"),
        )

        async def search_tmdb_media(spec: tuple[str, str, str, str]) -> tuple[str, str, str, list[dict[str, Any]]]:
            media_type, search_path, title_key, date_key = spec
            try:
                payload = await _tmdb_get_json(search_path, {
                    "query": query, "include_adult": "false", "language": "en-US", "page": 1
                })
            except Exception as exc:
                primary_errors.append("TMDB " + type(exc).__name__)
                return media_type, title_key, date_key, []
            rows = []
            for row in payload.get("results") or []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                title = str(row.get(title_key) or "").strip()
                if not title or not _availability_title_is_relevant(query, title):
                    continue
                rows.append(row)
            rows.sort(key=lambda row: (
                _availability_normalize_title(row.get(title_key)) == target,
                bool(row.get(date_key)),
                float(row.get("popularity") or 0),
            ), reverse=True)
            return media_type, title_key, date_key, rows[:3]

        searched_media = await asyncio.gather(*(search_tmdb_media(spec) for spec in media_specs))

        async def resolve_tmdb_match(
            media_type: str, title_key: str, date_key: str, row: dict[str, Any]
        ) -> dict[str, Any]:
            tmdb_id = int(row["id"])
            try:
                provider_data = await _tmdb_get_json(f"{media_type}/{tmdb_id}/watch/providers", {})
            except Exception as exc:
                primary_errors.append("TMDB providers " + type(exc).__name__)
                provider_data = {}
            region_data = (provider_data.get("results") or {}).get("IN") or {}
            providers = []
            for category, kind in (("flatrate", "subscription"), ("free", "free"),
                                   ("ads", "ads"), ("rent", "rent"), ("buy", "buy")):
                for provider in region_data.get(category) or []:
                    name = str(provider.get("provider_name") or "").strip()
                    if name:
                        logo_path = str(provider.get("logo_path") or "").strip()
                        providers.append({
                            "providerId": provider.get("provider_id"),
                            "name": name,
                            "region": "IN",
                            "type": kind,
                            "webUrl": region_data.get("link") or "",
                            "logoUrl": "https://image.tmdb.org/t/p/w92" + logo_path if logo_path else "",
                        })
            return {
                "id": str(tmdb_id), "title": str(row.get(title_key) or query),
                "year": str(row.get(date_key) or "")[:4] or None,
                "type": media_type, "tmdbId": tmdb_id, "providers": providers[:24],
                "status": "ready" if providers else "not_confirmed", "source": "TMDB",
            }

        # Resolve providers concurrently too. This makes the fallback quick when
        # the exact movie has no listing: the TV-series provider result can win
        # without waiting for every unrelated movie candidate.
        tmdb_candidates = [
            (media_type, title_key, date_key, row)
            for media_type, title_key, date_key, rows in searched_media
            for row in rows
        ]
        if tmdb_candidates:
            primary_matches.extend(await asyncio.gather(*(
                resolve_tmdb_match(media_type, title_key, date_key, row)
                for media_type, title_key, date_key, row in tmdb_candidates
            )))

    # De-duplicate same title/media/ID across sources; merge their provider entries.
    deduped: list[dict[str, Any]] = []
    index_by_identity: dict[tuple[str, str], int] = {}
    for match in primary_matches:
        identity = (_availability_canonical_type(match.get("type")),
                    _availability_normalize_title(match.get("title")))
        if identity in index_by_identity:
            existing = deduped[index_by_identity[identity]]
            existing_providers = existing.get("providers") or []
            provider_indexes = {
                (str(provider.get("name") or "").casefold(), str(provider.get("type") or "").casefold()): provider
                for provider in existing_providers
            }
            for provider in match.get("providers") or []:
                key = (str(provider.get("name") or "").casefold(), str(provider.get("type") or "").casefold())
                previous_provider = provider_indexes.get(key)
                if previous_provider is None:
                    existing_providers.append(provider)
                    provider_indexes[key] = provider
                    continue
                for field in ("webUrl", "providerId"):
                    if not previous_provider.get(field) and provider.get(field):
                        previous_provider[field] = provider[field]
            existing["providers"] = existing_providers
            if match.get("source") not in str(existing.get("source") or ""):
                existing["source"] = str(existing.get("source") or "") + " + " + str(match.get("source") or "")
            existing["status"] = "ready" if existing.get("providers") else "not_confirmed"
        else:
            index_by_identity[identity] = len(deduped)
            deduped.append(match)

    # Only use TVmaze/OMDb when both primary catalogues returned no title match.
    if not deduped:
        fallback_matches = await _fallback_tvmaze_search(query, timeout)
        if not fallback_matches:
            fallback_matches = await _fallback_omdb_search(query, timeout)
        if fallback_matches:
            result = {"status": "not_confirmed", "query": query, "title": fallback_matches[0]["title"],
                "year": fallback_matches[0].get("year"), "matches": fallback_matches, "providers": [],
                "source": fallback_matches[0].get("source"),
                "message": "Metadata match found, but no streaming provider could be confirmed by Watchmode or TMDB."}
        else:
            result = {"status": "not_found" if not primary_errors else "unavailable", "query": query,
                "title": query, "year": None, "matches": [], "providers": [],
                "message": "No title match found in Watchmode, TMDB, TVmaze, or OMDb."}
        _watchmode_search_cache[cache_key] = (time.time(), result)
        return result

    deduped.sort(key=lambda item: (
        _availability_normalize_title(item.get("title")) == target,
        bool(item.get("providers")),
        bool(item.get("year")),
    ), reverse=True)

    merged_providers, merged_provider_index = [], {}
    for match in deduped:
        for provider in match.get("providers") or []:
            identity = (str(provider.get("name") or "").casefold(),
                        str(provider.get("region") or "IN").upper(),
                        str(provider.get("type") or "").casefold())
            existing_provider = merged_provider_index.get(identity)
            if existing_provider is None:
                merged_provider_index[identity] = provider
                merged_providers.append(provider)
                continue
            for field in ("webUrl", "providerId"):
                if not existing_provider.get(field) and provider.get(field):
                    existing_provider[field] = provider[field]
    result = {
        "status": "ready" if merged_providers else "not_confirmed",
        "query": query, "title": deduped[0].get("title") or query, "year": deduped[0].get("year"),
        "matches": deduped[:10], "providers": merged_providers[:24],
        "source": "Watchmode + TMDB",
    }
    _watchmode_search_cache[cache_key] = (time.time(), result)
    return result



# Small, independent TMDB lookup for the quick search cards. This intentionally
# does not wait for torrent metadata or Watchmode source resolution.
_TMDB_SEARCH_METADATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TMDB_SEARCH_METADATA_CACHE_TTL = 1800.0


@app.get("/api/search/metadata")
async def api_search_metadata(q: str = Query(..., min_length=2, max_length=160)):
    """Return poster, synopsis, release year and TMDB link for fast search cards."""
    query = re.sub(r"\s+", " ", q).strip()
    cache_key = query.casefold()
    now = time.time()
    cached = _TMDB_SEARCH_METADATA_CACHE.get(cache_key)
    if cached and now - cached[0] < _TMDB_SEARCH_METADATA_CACHE_TTL:
        _record_cache_event("search_metadata", "memory_hit")
        return cached[1]
    _record_cache_event("search_metadata", "miss")

    # Disk-backed query envelopes survive process/container restarts. The per-title
    # cache below remains durable indefinitely and is refreshed whenever a new
    # successful lookup supplies better data.
    persistent_cache = _read_persistent_search_metadata_cache()
    query_entry = (persistent_cache.get("queries") or {}).get(cache_key)
    try:
        query_saved_at = int((query_entry or {}).get("savedAt") or 0)
    except (TypeError, ValueError):
        query_saved_at = 0
    if (
        isinstance(query_entry, dict)
        and now - query_saved_at < TMDB_SEARCH_METADATA_QUERY_TTL_SECONDS
    ):
        # Read newer reference-only envelopes; continue to support any legacy
        # envelope from a previous deployment.
        cached_response = query_entry.get("response")
        if isinstance(cached_response, dict) and cached_response.get("status") == "ready":
            _record_cache_event("search_metadata", "persistent_query_hit")
            _TMDB_SEARCH_METADATA_CACHE[cache_key] = (now, cached_response)
            return cached_response
        key_rows = query_entry.get("keys", [])
        requested_keys = {
            (
                str(key.get("title") or ""),
                int(key.get("year") or 0),
                str(key.get("mediaType") or "movie"),
                str(key.get("tmdbId") or ""),
            )
            for key in key_rows if isinstance(key, dict)
        } if isinstance(key_rows, list) else set()
        cached_results: list[dict[str, Any]] = []
        for raw in persistent_cache.get("results", []):
            if not isinstance(raw, dict):
                continue
            candidate = _search_metadata_cache_candidate(raw)
            if not candidate:
                continue
            row_key = (
                _normalize_title(candidate["title"]),
                int(candidate.get("year") or 0),
                str(candidate.get("mediaType") or "movie"),
                str(candidate.get("tmdbId") or ""),
            )
            if row_key in requested_keys:
                cached_results.append(candidate)
        if cached_results:
            _record_cache_event("search_metadata", "persistent_title_hit")
            _record_cache_event("search_metadata", "persistent_rows_served", len(cached_results))
            cached_response = {
                "status": "ready",
                "query": query,
                "source": "Persistent synopsis cache",
                "results": cached_results[:30],
            }
            _TMDB_SEARCH_METADATA_CACHE[cache_key] = (now, cached_response)
            return cached_response

    if not (TMDB_READ_ACCESS_TOKENS or TMDB_API_KEYS):
        return {"status": "unavailable", "query": query, "source": "TMDB", "results": []}

    try:
        # Give the helper time to try each configured token on key-specific
        # failures. Use a shorter per-token timeout for this non-blocking UI lookup.
        payload = await asyncio.wait_for(
            _tmdb_get_json("search/multi", {
                "query": query,
                "include_adult": "false",
                "language": "en-US",
                "page": 1,
            }, request_timeout=3.5),
            timeout=14.0,
        )
    except Exception as exc:
        logger.info("TMDB quick metadata search failed for %r: %s", query, type(exc).__name__)
        return {"status": "unavailable", "query": query, "source": "TMDB", "results": []}

    results: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        media_type = str(item.get("media_type") or "").lower()
        if media_type not in {"movie", "tv"} or not item.get("id"):
            continue
        title = str((item.get("title") if media_type == "movie" else item.get("name")) or "").strip()
        # TMDB has already ranked these results for the submitted query. Do not
        # apply the stricter Watchmode availability matcher here: franchise titles
        # such as "The Amazing Spider-Man 2" are valid hits for "Spider-Man".
        if not title:
            continue
        release_date = str(
            (item.get("release_date") if media_type == "movie" else item.get("first_air_date")) or ""
        ).strip()
        year_match = re.match(r"^(\d{4})", release_date)
        poster_path = str(item.get("poster_path") or "").strip()
        backdrop_path = str(item.get("backdrop_path") or "").strip()
        tmdb_id = int(item["id"])
        results.append({
            "id": tmdb_id,
            "tmdbId": tmdb_id,
            "title": title,
            "year": int(year_match.group(1)) if year_match else None,
            "mediaType": media_type,
            "overview": str(item.get("overview") or "").strip(),
            "posterUrl": TMDB_IMAGE_BASE + poster_path if poster_path else "",
            "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
            "rating": float(item.get("vote_average") or 0),
            "tmdbUrl": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
            "source": "TMDB",
        })

    target = _availability_normalize_title(query)
    results.sort(key=lambda item: (
        _availability_normalize_title(item["title"]) == target,
        bool(item.get("year")),
        float(item.get("rating") or 0),
    ), reverse=True)
    response = {
        "status": "ready" if results else "not_found",
        "query": query,
        "source": "TMDB",
        "results": results[:30],
    }
    if response["status"] == "ready":
        _persist_search_metadata_cache_rows(results[:30], query=query, query_response=response)
        if len(_TMDB_SEARCH_METADATA_CACHE) > 300:
            expired_keys = [
                key for key, value in _TMDB_SEARCH_METADATA_CACHE.items()
                if now - value[0] >= _TMDB_SEARCH_METADATA_CACHE_TTL
            ]
            for key in expired_keys:
                _TMDB_SEARCH_METADATA_CACHE.pop(key, None)
            if len(_TMDB_SEARCH_METADATA_CACHE) > 300:
                _TMDB_SEARCH_METADATA_CACHE.pop(next(iter(_TMDB_SEARCH_METADATA_CACHE)), None)
        _TMDB_SEARCH_METADATA_CACHE[cache_key] = (now, response)
    return response


def _search_metadata_cache_candidate(raw: dict[str, Any]) -> dict[str, Any] | None:
    title = str(
        raw.get("catalogueTitle") or raw.get("marvelCatalogueTitle")
        or raw.get("mediaTitle") or raw.get("title") or raw.get("name") or ""
    ).strip()
    if not title:
        return None

    year_value = raw.get("year")
    release_date = str(raw.get("releaseDate") or raw.get("release_date") or raw.get("first_air_date") or "")
    try:
        year = int(year_value) if year_value else 0
    except (TypeError, ValueError):
        year = 0
    if not year and re.match(r"^\d{4}", release_date):
        year = int(release_date[:4])
    if not year:
        year_match = re.search(r"\b((?:19|20)\d{2})\b\s*\)?\s*$", title)
        if year_match:
            year = int(year_match.group(1))

    canonical_title = re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", title).strip()
    normalized = _normalize_title(canonical_title)
    if not normalized:
        return None

    media_type = str(raw.get("mediaType") or raw.get("media_type") or "movie").lower()
    if media_type not in {"movie", "tv"}:
        media_type = "movie"
    tmdb_id = raw.get("tmdbId") or raw.get("id")
    try:
        numeric_id = int(tmdb_id) if tmdb_id else 0
    except (TypeError, ValueError):
        numeric_id = 0
    overview = re.sub(r"\s+", " ", str(raw.get("overview") or "")).strip()
    # Keep rows with a known TMDB ID even when overview is empty. The batch
    # endpoint can hydrate those rows from TMDB's movie-details endpoint.

    def _number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    poster_url = str(raw.get("posterUrl") or raw.get("poster_url") or "")
    if not poster_url and str(raw.get("poster_path") or "").strip():
        poster_url = TMDB_IMAGE_BASE + str(raw["poster_path"]).strip()
    backdrop_url = str(raw.get("backdropUrl") or raw.get("backdrop_url") or "")
    if not backdrop_url and str(raw.get("backdrop_path") or "").strip():
        backdrop_url = "https://image.tmdb.org/t/p/w780" + str(raw["backdrop_path"]).strip()
    tmdb_url = str(raw.get("tmdbUrl") or raw.get("tmdb_url") or "")
    if not tmdb_url and numeric_id:
        tmdb_url = f"https://www.themoviedb.org/{media_type}/{numeric_id}"

    return {
        "id": numeric_id or ("cached:" + normalized + (":" + str(year) if year else "")),
        "tmdbId": numeric_id or None,
        "title": canonical_title,
        "year": year or None,
        "mediaType": media_type,
        "overview": overview,
        "posterUrl": poster_url,
        "backdropUrl": backdrop_url,
        "rating": _number(raw.get("rating") or raw.get("vote_average")),
        "tmdbUrl": tmdb_url,
        "source": str(raw.get("source") or "VPS catalogue cache"),
        "synopsisCheckedAt": raw.get("synopsisCheckedAt"),
        "synopsisLookupVersion": raw.get("synopsisLookupVersion"),
    }


def _read_persistent_search_metadata_cache() -> dict[str, Any]:
    try:
        payload = json.loads(TMDB_SEARCH_METADATA_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("results", [])
            payload.setdefault("queries", {})
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "results": [], "queries": {}}


def _write_persistent_search_metadata_cache(payload: dict[str, Any]) -> None:
    # The same atomic/fsync writer used by the catalogue snapshots keeps this
    # cache safe across container restarts and interrupted writes.
    _write_synopsis_cache(TMDB_SEARCH_METADATA_CACHE_FILE, payload)


def _persist_search_metadata_cache_rows(
    raw_rows: list[dict[str, Any]],
    *,
    query: str | None = None,
    query_response: dict[str, Any] | None = None,
) -> int:
    """Persist per-title metadata permanently without duplicating synopsis text in query envelopes."""
    disk = _read_persistent_search_metadata_cache()
    rows = disk.get("results", [])
    if not isinstance(rows, list):
        rows = []
    index: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        candidate = _search_metadata_cache_candidate(raw)
        if not candidate:
            continue
        key = (
            _normalize_title(candidate["title"]),
            int(candidate.get("year") or 0),
            str(candidate.get("mediaType") or "movie"),
            str(candidate.get("tmdbId") or ""),
        )
        index[key] = {**raw, **candidate}

    now = int(time.time())
    added = 0
    query_keys: list[dict[str, Any]] = []
    seen_query_keys: set[tuple[str, int, str, str]] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        candidate = _search_metadata_cache_candidate(raw)
        if not candidate:
            continue
        key = (
            _normalize_title(candidate["title"]),
            int(candidate.get("year") or 0),
            str(candidate.get("mediaType") or "movie"),
            str(candidate.get("tmdbId") or ""),
        )
        previous = index.get(key, {})
        # Do not replace a previously stored synopsis or useful artwork with blanks.
        merged = {**previous, **candidate}
        for field in ("overview", "posterUrl", "backdropUrl", "tmdbUrl", "tmdbId", "year", "rating"):
            if candidate.get(field) in (None, "", 0):
                if previous.get(field) not in (None, "", 0):
                    merged[field] = previous[field]
        merged["cacheUpdatedAt"] = now
        if not previous or merged != previous:
            added += 1
        index[key] = merged
        if query and query_response and query_response.get("status") == "ready" and key not in seen_query_keys:
            seen_query_keys.add(key)
            query_keys.append({
                "title": key[0],
                "year": key[1],
                "mediaType": key[2],
                "tmdbId": key[3],
            })

    if query and query_response and query_response.get("status") == "ready":
        queries = disk.get("queries", {})
        if not isinstance(queries, dict):
            queries = {}
        queries[re.sub(r"\s+", " ", query).strip().casefold()] = {
            "savedAt": now,
            "keys": query_keys,
        }
        # Query envelopes only store references; actual synopsis text exists once
        # per title in results and is retained permanently.
        if len(queries) > 1000:
            queries = dict(sorted(
                queries.items(),
                key=lambda pair: int((pair[1] or {}).get("savedAt") or 0),
                reverse=True,
            )[:1000])
        disk["queries"] = queries

    max_rows = 8000
    merged_rows = list(index.values())
    merged_rows.sort(key=lambda item: int(item.get("cacheUpdatedAt") or 0), reverse=True)
    disk["version"] = 1
    disk["updatedAt"] = now
    disk["results"] = merged_rows[:max_rows]
    try:
        _write_persistent_search_metadata_cache(disk)
    except OSError as exc:
        logger.warning("Could not persist search-card synopsis cache: %s", exc)
    return added



def _persist_search_metadata_overview(tmdb_id: int, overview: str) -> int:
    """Persist a newly fetched overview into any matching runtime catalogue row."""
    overview = re.sub(r"\s+", " ", str(overview or "")).strip()
    if tmdb_id < 1 or not overview:
        return 0

    updated_rows = 0
    now = int(time.time())
    for _name, path in _synopsis_cache_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            continue
        changed = False
        for row in rows:
            if not isinstance(row, dict) or str(row.get("overview") or "").strip():
                continue
            try:
                row_id = int(row.get("tmdbId") or row.get("id") or 0)
            except (TypeError, ValueError):
                row_id = 0
            if row_id != tmdb_id:
                continue
            row["overview"] = overview
            row["synopsisCheckedAt"] = now
            row["synopsisLookupVersion"] = TMDB_SYNOPSIS_LOOKUP_VERSION
            changed = True
            updated_rows += 1
        if changed:
            try:
                _write_synopsis_cache(path, payload)
                logger.info("Saved fetched TMDB synopsis for ID %s into %s", tmdb_id, path.name)
            except OSError as exc:
                logger.warning("Could not persist synopsis for TMDB ID %s in %s: %s", tmdb_id, path.name, exc)

    # Popular/Trending/Latest responses are stored in a separate persistent file.
    try:
        disk = json.loads(TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(disk, dict):
            disk = {}
    except (OSError, ValueError, TypeError):
        disk = {}
    catalogues = disk.get("catalogues", {})
    disk_changed = False
    if isinstance(catalogues, dict):
        for envelope in catalogues.values():
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            rows = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or str(row.get("overview") or "").strip():
                    continue
                try:
                    row_id = int(row.get("tmdbId") or row.get("id") or 0)
                except (TypeError, ValueError):
                    row_id = 0
                if row_id == tmdb_id:
                    row["overview"] = overview
                    row["synopsisCheckedAt"] = now
                    row["synopsisLookupVersion"] = TMDB_SYNOPSIS_LOOKUP_VERSION
                    updated_rows += 1
                    disk_changed = True

    if disk_changed:
        temporary: Path | None = None
        try:
            TMDB_CATALOGUE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                prefix=TMDB_CATALOGUE_CACHE_FILE.stem + "-synopsis-",
                suffix=".tmp", dir=str(TMDB_CATALOGUE_CACHE_FILE.parent), delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(disk, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, TMDB_CATALOGUE_CACHE_FILE)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not persist dynamic catalogue synopsis for TMDB ID %s: %s", tmdb_id, exc)
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    # Keep in-memory catalogue responses consistent with the disk cache.
    for cache_key, entry in list(_tmdb_movie_catalogue_cache.items()):
        payload = entry[1]
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or str(row.get("overview") or "").strip():
                continue
            try:
                row_id = int(row.get("tmdbId") or row.get("id") or 0)
            except (TypeError, ValueError):
                row_id = 0
            if row_id == tmdb_id:
                row["overview"] = overview
                row["synopsisCheckedAt"] = now
                row["synopsisLookupVersion"] = TMDB_SYNOPSIS_LOOKUP_VERSION
    return updated_rows


async def _lookup_search_metadata_by_tmdb_id(item: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch a missing synopsis using the cached row's authoritative TMDB ID."""
    try:
        tmdb_id = int(item.get("tmdbId") or item.get("id") or 0)
    except (TypeError, ValueError):
        tmdb_id = 0
    if tmdb_id < 1:
        return None

    details: dict[str, Any] | None = None
    for language in ("en-US", "en"):
        try:
            details = await asyncio.wait_for(
                _tmdb_get_json(
                    f"movie/{tmdb_id}",
                    {"language": language},
                    request_timeout=3.0,
                ),
                timeout=4.5,
            )
        except Exception as exc:
            logger.info("On-demand synopsis lookup failed for TMDB ID %s (%s): %s",
                tmdb_id, language, type(exc).__name__)
            continue
        overview = re.sub(r"\s+", " ", str(details.get("overview") or "")).strip()
        if overview:
            return {
                "overview": overview,
                "tmdbId": tmdb_id,
                "tmdbUrl": f"https://www.themoviedb.org/movie/{tmdb_id}",
                "rating": float(details.get("vote_average") or item.get("rating") or 0),
                "posterUrl": (
                    TMDB_IMAGE_BASE + str(details.get("poster_path")).strip()
                    if str(details.get("poster_path") or "").strip()
                    else str(item.get("posterUrl") or "")
                ),
                "backdropUrl": (
                    "https://image.tmdb.org/t/p/w780" + str(details.get("backdrop_path")).strip()
                    if str(details.get("backdrop_path") or "").strip()
                    else str(item.get("backdropUrl") or "")
                ),
                "synopsisCheckedAt": int(time.time()),
                "synopsisLookupVersion": TMDB_SYNOPSIS_LOOKUP_VERSION,
            }
        # Only issue the alternate-language request if TMDB answered but had no overview.
    return None


def _read_search_metadata_catalogue_rows() -> list[dict[str, Any]]:
    """Read metadata from the persistent VPS JSON snapshots used by the catalogues."""
    candidates: list[dict[str, Any]] = []
    for _name, path in _synopsis_cache_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if isinstance(rows, list):
            candidates.extend(row for row in rows if isinstance(row, dict))

    # Query search results and successful synopses are kept separately so arbitrary
    # searches survive restarts even when they are not in a prebuilt catalogue.
    persistent_metadata = _read_persistent_search_metadata_cache()
    persistent_rows = persistent_metadata.get("results", [])
    if isinstance(persistent_rows, list):
        candidates.extend(row for row in persistent_rows if isinstance(row, dict))

    # Popular, Trending and Latest are stored separately from franchise snapshots.
    try:
        disk = json.loads(TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
        catalogues = disk.get("catalogues", {}) if isinstance(disk, dict) else {}
        for envelope in catalogues.values() if isinstance(catalogues, dict) else []:
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            rows = payload.get("results", []) if isinstance(payload, dict) else []
            if isinstance(rows, list):
                candidates.extend(row for row in rows if isinstance(row, dict))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return candidates


def _imdb_rating_cache_key(title: str, year: int = 0, media_type: str = "movie") -> str:
    resolved_type = "tv" if str(media_type or "movie").casefold() in {"tv", "series"} else "movie"
    return f"{resolved_type}|{_normalize_synopsis_match_title(title)}|{int(year or 0)}"


def _parse_imdb_rating_title(value: str) -> tuple[str, int]:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    match = re.search(r"\s*\(?((?:18|19|20|21)\d{2})\)?\s*$", raw)
    year = int(match.group(1)) if match else 0
    title = raw[:match.start()].strip() if match else raw
    if not title or not 1870 <= year <= 2100:
        return raw, 0
    return title, year


def _load_imdb_rating_cache() -> dict[str, dict[str, Any]]:
    global _IMDB_RATING_CACHE
    if _IMDB_RATING_CACHE is not None:
        return _IMDB_RATING_CACHE
    cache: dict[str, dict[str, Any]] = {}
    try:
        payload = json.loads(IMDB_RATING_CACHE_FILE.read_text(encoding="utf-8"))
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or "").strip()
                try:
                    year = int(row.get("year") or 0)
                except (TypeError, ValueError):
                    year = 0
                key = str(row.get("cacheKey") or (_imdb_rating_cache_key(title, year) if title else ""))
                if key:
                    cache[key] = row
    except (OSError, ValueError, TypeError):
        pass
    _IMDB_RATING_CACHE = cache
    return cache


async def _save_imdb_rating_cache_record(record: dict[str, Any]) -> None:
    key = str(record.get("cacheKey") or "")
    if not key:
        return
    async with _IMDB_RATING_CACHE_LOCK:
        cache = _load_imdb_rating_cache()
        cache[key] = record
        payload = {
            "version": 1,
            "updatedAt": int(time.time()),
            "results": list(cache.values())[-10000:],
        }
        try:
            _write_synopsis_cache(IMDB_RATING_CACHE_FILE, payload)
        except OSError as exc:
            logger.warning("Could not persist IMDb rating cache: %s", exc)


async def _fetch_imdb_rating(
    title: str,
    year: int,
    cache_key: str,
    media_type: str = "movie",
    tmdb_id: int = 0,
) -> dict[str, Any]:
    global _OMDB_HTTP_CLIENT, _OMDB_RATE_LIMITED_UNTIL
    keys = _split_env_keys(os.getenv("OMDB_API_KEYS", ""), os.getenv("OMDB_API_KEY", ""))
    now = int(time.time())
    normalized_type = "tv" if str(media_type or "movie").casefold() in {"tv", "series"} else "movie"
    omdb_type = "series" if normalized_type == "tv" else "movie"
    base = {
        "cacheKey": cache_key,
        "title": title,
        "year": year or None,
        "mediaType": normalized_type,
        "checkedAt": now,
        "lookupVersion": IMDB_RATING_LOOKUP_VERSION,
        "tmdbIdTried": tmdb_id or None,
    }
    if not keys:
        return {**base, "status": "unavailable", "reason": "OMDb API key is not configured"}
    if time.time() < _OMDB_RATE_LIMITED_UNTIL:
        return {**base, "status": "rate_limited", "cacheable": False}

    provider = "omdb"
    start_index = _PROVIDER_KEY_CURSORS.get(provider, 0) % len(keys)
    ordered = keys[start_index:] + keys[:start_index]
    _PROVIDER_KEY_CURSORS[provider] = (start_index + 1) % len(keys)
    timeout = httpx.Timeout(5.0, connect=2.5)
    target = _normalize_synopsis_match_title(title)

    async def call_omdb(key: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        global _OMDB_HTTP_CLIENT, _OMDB_RATE_LIMITED_UNTIL
        try:
            if _OMDB_HTTP_CLIENT is None or _OMDB_HTTP_CLIENT.is_closed:
                _OMDB_HTTP_CLIENT = _InstrumentedAsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=8,
                        max_keepalive_connections=4,
                        keepalive_expiry=30.0,
                    ),
                )
            async with _OMDB_REQUEST_SEMAPHORE:
                response = await _OMDB_HTTP_CLIENT.get(
                    "https://www.omdbapi.com/",
                    params={"apikey": key, **params, "r": "json"},
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            logger.info("OMDb rating lookup failed for %r (%s): %s", title, type(exc).__name__)
            return None, "transient"

        if response.status_code == 429:
            _provider_key_failure(provider, key, _retry_after_seconds(response), "HTTP 429")
            _OMDB_RATE_LIMITED_UNTIL = time.time() + 1800
            return None, "rate_limited"
        if response.status_code in {401, 403}:
            _provider_key_failure(provider, key, 3600, f"HTTP {response.status_code}")
            return None, "invalid_key"
        if response.status_code >= 500:
            return None, "transient"
        if response.status_code >= 400:
            return None, "unavailable"
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return None, "unavailable"

        if str(payload.get("Response") or "").casefold() == "false":
            error = str(payload.get("Error") or "")
            lowered_error = error.casefold()
            if "request limit reached" in lowered_error:
                # Do not try another key to bypass the account's daily quota.
                _OMDB_RATE_LIMITED_UNTIL = time.time() + 3600
                return None, "rate_limited"
            if "invalid api key" in lowered_error:
                _provider_key_failure(provider, key, 3600, "invalid API key")
                return None, "invalid_key"
        return payload, "ok"

    async def lookup_by_tmdb_imdb_id(key: str) -> dict[str, Any] | None:
        """Use TMDB's IMDb ID as an authoritative fallback when title search misses."""
        if tmdb_id < 1:
            return None
        try:
            if normalized_type == "tv":
                tmdb_details = await _tmdb_get_json(
                    f"tv/{tmdb_id}/external_ids", {}, request_timeout=3.0,
                )
            else:
                tmdb_details = await _tmdb_get_json(
                    f"movie/{tmdb_id}",
                    {"language": "en-US"},
                    request_timeout=3.0,
                )
        except Exception as exc:
            logger.info("TMDB IMDb-ID fallback failed for %s (%s): %s",
                tmdb_id, type(exc).__name__, title)
            return None

        imdb_id = str(tmdb_details.get("imdb_id") or "").strip()
        if not re.fullmatch(r"tt\d+", imdb_id):
            return None
        details, state = await call_omdb(key, {"i": imdb_id})
        if state == "rate_limited":
            return {**base, "status": "rate_limited", "reason": "OMDb rate limit reached", "cacheable": False}
        if state != "ok" or not details or str(details.get("Response") or "").casefold() == "false":
            return None

        returned_id = str(details.get("imdbID") or "").strip()
        returned_year_match = re.search(r"\b((?:18|19|20|21)\d{2})\b", str(details.get("Year") or ""))
        returned_year = int(returned_year_match.group(1)) if returned_year_match else 0
        # The TMDB-to-IMDb ID relationship is authoritative. IMDb/OMDb may
        # classify a special differently or use a different premiere year.
        if returned_id != imdb_id:
            return None

        raw_rating = str(details.get("imdbRating") or "").strip()
        try:
            imdb_rating = float(raw_rating) if raw_rating.casefold() != "n/a" else None
        except (TypeError, ValueError):
            imdb_rating = None
        return {
            **base,
            "title": title,
            "year": year or returned_year or None,
            "status": "found",
            "imdbRating": imdb_rating,
            "imdbVotes": str(details.get("imdbVotes") or "").strip() or None,
            "imdbId": imdb_id,
            "imdbUrl": f"https://www.imdb.com/title/{imdb_id}/",
            "ratingLookupSource": "tmdb_imdb_id",
        }

    for key in ordered:
        if not _provider_key_available(provider, key):
            continue

        params: dict[str, Any] = {"t": title, "type": omdb_type}
        if 1870 <= year <= 2100:
            params["y"] = year
        payload, state = await call_omdb(key, params)
        if state == "rate_limited":
            return {**base, "status": "rate_limited", "reason": "OMDb rate limit reached", "cacheable": False}
        if state in {"transient", "invalid_key"}:
            continue
        if state != "ok" or payload is None:
            return {**base, "status": "unavailable", "reason": "OMDb request failed", "cacheable": False}

        if str(payload.get("Response") or "").casefold() == "false":
            error = str(payload.get("Error") or "")
            lowered_error = error.casefold()
            if "movie not found" in lowered_error or "series not found" in lowered_error or "not found" in lowered_error:
                # Title lookups can miss a valid entry; use OMDb search and then
                # fetch details by IMDb ID, but only accept an exact title/year.
                search_params: dict[str, Any] = {"s": title, "type": omdb_type}
                if 1870 <= year <= 2100:
                    search_params["y"] = year
                search_payload, search_state = await call_omdb(key, search_params)
                if search_state == "rate_limited":
                    return {**base, "status": "rate_limited", "reason": "OMDb rate limit reached", "cacheable": False}
                if search_state in {"transient", "invalid_key"}:
                    continue
                if search_state != "ok" or not search_payload:
                    return {**base, "status": "unavailable", "reason": "OMDb search failed", "cacheable": False}
                candidates: list[tuple[int, dict[str, Any]]] = []
                for row in search_payload.get("Search") or []:
                    if not isinstance(row, dict):
                        continue
                    candidate_title = str(row.get("Title") or "").strip()
                    candidate_id = str(row.get("imdbID") or "").strip()
                    if not candidate_title or not re.fullmatch(r"tt\d+", candidate_id):
                        continue
                    if _normalize_synopsis_match_title(candidate_title) != target:
                        continue
                    year_match = re.search(r"\b((?:18|19|20|21)\d{2})\b", str(row.get("Year") or ""))
                    candidate_year = int(year_match.group(1)) if year_match else 0
                    if year and candidate_year and candidate_year != year:
                        continue
                    if year and not candidate_year:
                        continue
                    candidates.append((0 if year and candidate_year == year else 1, row))
                if not candidates:
                    direct_match = await lookup_by_tmdb_imdb_id(key)
                    if direct_match:
                        return direct_match
                    return {**base, "status": "not_found", "reason": "OMDb returned no exact title/year match"}
                candidates.sort(key=lambda item: item[0])
                selected_id = str(candidates[0][1].get("imdbID") or "").strip()
                payload, details_state = await call_omdb(key, {"i": selected_id, "type": omdb_type})
                if details_state == "rate_limited":
                    return {**base, "status": "rate_limited", "reason": "OMDb rate limit reached", "cacheable": False}
                if details_state in {"transient", "invalid_key"}:
                    continue
                if details_state != "ok" or payload is None or str(payload.get("Response") or "").casefold() == "false":
                    return {**base, "status": "unavailable", "reason": "OMDb title details lookup failed", "cacheable": False}
            else:
                return {**base, "status": "not_found", "reason": error or "OMDb returned no match"}

        returned_title = str(payload.get("Title") or "").strip()
        returned_year_match = re.search(r"\b((?:18|19|20|21)\d{2})\b", str(payload.get("Year") or ""))
        returned_year = int(returned_year_match.group(1)) if returned_year_match else 0
        candidate = _normalize_synopsis_match_title(returned_title)
        returned_type = str(payload.get("Type") or "").casefold()
        wanted_type = "series" if normalized_type == "tv" else "movie"
        if (
            not returned_title
            or candidate != target
            or (year and returned_year != year)
            or (returned_type and returned_type != wanted_type)
        ):
            # Never display another title's score just because OMDb returned a nearby match.
            # TMDB's IMDb ID is safer than accepting a fuzzy title result.
            direct_match = await lookup_by_tmdb_imdb_id(key)
            if direct_match:
                return direct_match
            return {
                **base,
                "status": "not_found",
                "reason": "OMDb title/year/type did not exactly match the requested entry",
            }

        imdb_id = str(payload.get("imdbID") or "").strip()
        raw_rating = str(payload.get("imdbRating") or "").strip()
        try:
            imdb_rating = float(raw_rating) if raw_rating.casefold() != "n/a" else None
        except (TypeError, ValueError):
            imdb_rating = None
        record = {
            **base,
            "title": title,
            "year": year or returned_year or None,
            "status": "found",
            "imdbRating": imdb_rating,
            "imdbVotes": str(payload.get("imdbVotes") or "").strip() or None,
            "imdbId": imdb_id if re.fullmatch(r"tt\d+", imdb_id) else None,
            "imdbUrl": f"https://www.imdb.com/title/{imdb_id}/" if re.fullmatch(r"tt\d+", imdb_id) else None,
        }
        _provider_key_success(provider, key)
        return record

    return {**base, "status": "unavailable", "reason": "No usable OMDb key or transient upstream error", "cacheable": False}

async def _get_imdb_rating_record(
    title: str, year: int, media_type: str = "movie", tmdb_id: int = 0
) -> dict[str, Any]:
    normalized_type = "tv" if str(media_type or "movie").casefold() in {"tv", "series"} else "movie"
    cache_key = _imdb_rating_cache_key(title, year, normalized_type)
    cached = _load_imdb_rating_cache().get(cache_key)
    now = int(time.time())
    if CACHE_ONLY_MODE:
        # Never call OMDb/TMDB; serve whatever is stored, however old.
        _record_cache_event("imdb_rating", "hit" if cached else "miss")
        return cached or {
            "status": "unavailable", "title": title, "year": year or None,
            "mediaType": normalized_type, "cacheKey": cache_key,
            "reason": "Not in bundled cache",
        }
    # A miss recorded before a TMDB id was available deserves one retry with the id.
    if (
        cached
        and tmdb_id > 0
        and not cached.get("tmdbIdTried")
        and (cached.get("status") != "found" or cached.get("imdbRating") is None)
    ):
        cached = None
    if cached:
        try:
            checked_at = int(cached.get("checkedAt") or 0)
            lookup_version = int(cached.get("lookupVersion") or 0)
        except (TypeError, ValueError):
            checked_at = lookup_version = 0
        if (
            lookup_version >= (
                IMDB_RATING_LOOKUP_VERSION
                if cached.get("status") != "found" or cached.get("imdbRating") is None
                else 4
            )
            and checked_at
            and now - checked_at < (
                IMDB_RATING_NOT_FOUND_TTL_SECONDS
                if cached.get("status") != "found" or cached.get("imdbRating") is None
                else IMDB_RATING_CACHE_TTL_SECONDS
            )
        ):
            _record_cache_event("imdb_rating", "hit")
            return cached

    _record_cache_event("imdb_rating", "miss")
    task = _IMDB_RATING_INFLIGHT.get(cache_key)
    if task is None or task.done():
        async def fetch_and_save() -> dict[str, Any]:
            result = await _fetch_imdb_rating(title, year, cache_key, normalized_type, tmdb_id)
            if result.get("status") in {"found", "not_found"} and result.get("cacheable", True):
                result.pop("cacheable", None)
                await _save_imdb_rating_cache_record(result)
            return result

        task = asyncio.create_task(fetch_and_save())
        _IMDB_RATING_INFLIGHT[cache_key] = task
    try:
        return await task
    finally:
        if task.done() and _IMDB_RATING_INFLIGHT.get(cache_key) is task:
            _IMDB_RATING_INFLIGHT.pop(cache_key, None)

@app.get("/api/catalogue/imdb-ratings")
async def api_catalogue_imdb_ratings(
    titles: list[str] = Query(..., min_length=1, max_length=80),
    media_types: list[str] = Query(default=[]),
    tmdb_ids: list[int] = Query(default=[]),
):
    """Return cached IMDb ratings, using TMDB's IMDb ID to resolve ambiguous titles."""
    requested: dict[str, tuple[str, int, str, str, int]] = {}
    for index, raw in enumerate(titles[:80]):
        title, year = _parse_imdb_rating_title(raw)
        if not title:
            continue
        supplied_type = media_types[index] if index < len(media_types) else "movie"
        media_type = "tv" if str(supplied_type or "movie").casefold() in {"tv", "series"} else "movie"
        try:
            tmdb_id = max(0, int(tmdb_ids[index])) if index < len(tmdb_ids) else 0
        except (TypeError, ValueError):
            tmdb_id = 0
        key = _imdb_rating_cache_key(title, year, media_type)
        requested.setdefault(key, (title, year, media_type, raw, tmdb_id))
    if not requested:
        return {"status": "ready", "results": []}

    results = await asyncio.gather(
        *(
            _get_imdb_rating_record(title, year, media_type, tmdb_id)
            for title, year, media_type, _raw, tmdb_id in requested.values()
        ),
        return_exceptions=True,
    )
    output = []
    for (title, year, media_type, raw, _tmdb_id), result in zip(requested.values(), results):
        if isinstance(result, Exception):
            logger.info("IMDb rating lookup failed for %r (%s)", title, type(result).__name__)
            output.append({
                "query": raw, "title": title, "year": year or None,
                "mediaType": media_type, "status": "unavailable",
            })
            continue
        output.append({
            **result,
            "query": raw,
            # Preserve the requested title/year for reliable client-side card matching.
            "title": title,
            "year": year or result.get("year"),
            "mediaType": media_type,
        })
    statuses = {str(item.get("status") or "") for item in output}
    return {
        "status": "ready" if statuses <= {"found", "not_found"} else "partial",
        "requested": len(requested),
        "matched": sum(1 for item in output if item.get("status") == "found" and item.get("imdbRating") is not None),
        "results": output,
    }

@app.get("/api/search/metadata/batch")
async def api_search_metadata_batch(
    titles: list[str] = Query(..., min_length=2, max_length=180),
):
    """Resolve search-card metadata from persistent VPS catalogue JSON by exact title/year."""
    requested = list(dict.fromkeys(
        re.sub(r"\s+", " ", str(value or "")).strip()
        for value in titles[:80] if str(value or "").strip()
    ))
    if not requested:
        return {"status": "ready", "source": "VPS catalogue cache", "results": []}

    candidates = []
    seen_rows: set[tuple[str, int, str, str]] = set()
    for raw in _read_search_metadata_catalogue_rows():
        item = _search_metadata_cache_candidate(raw)
        if not item:
            continue
        identity = (
            _normalize_title(item["title"]),
            int(item.get("year") or 0),
            str(item.get("mediaType") or "movie"),
            str(item.get("tmdbId") or ""),
        )
        if identity not in seen_rows:
            seen_rows.add(identity)
            candidates.append(item)

    matched: list[dict[str, Any]] = []
    missing_synopsis_candidates: dict[int, dict[str, Any]] = {}
    seen_matches: set[tuple[str, int, str]] = set()
    for requested_title in requested:
        year_match = re.search(r"\s*\(?((?:19|20)\d{2})\)?\s*$", requested_title)
        requested_year = int(year_match.group(1)) if year_match else 0
        clean_title = re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", requested_title).strip()
        target = _normalize_title(clean_title)
        if not target:
            continue
        options = [item for item in candidates if _normalize_title(item["title"]) == target]
        same_year = [item for item in options if int(item.get("year") or 0) == requested_year] if requested_year else []
        unknown_year = [item for item in options if not int(item.get("year") or 0)]
        if same_year:
            selected = same_year
        elif unknown_year:
            selected = unknown_year if len(unknown_year) == 1 else []
        elif not requested_year and options:
            selected = options
        elif len(options) == 1:
            selected = options
        else:
            selected = []
        if not selected:
            continue
        selected.sort(key=lambda item: (
            bool(item.get("overview")),
            bool(item.get("tmdbId")),
            float(item.get("rating") or 0),
        ), reverse=True)
        best = selected[0]
        match_key = (
            _normalize_title(best["title"]),
            int(best.get("year") or 0),
            str(best.get("mediaType") or "movie"),
        )
        if match_key not in seen_matches:
            seen_matches.add(match_key)
            matched.append(best)
        if not str(best.get("overview") or "").strip():
            try:
                missing_id = int(best.get("tmdbId") or best.get("id") or 0)
            except (TypeError, ValueError):
                missing_id = 0
            if missing_id > 0:
                missing_synopsis_candidates.setdefault(missing_id, best)

    # The saved catalogues can know a movie's TMDB ID even when the discover/search
    # list originally supplied no overview. Hydrate a small batch of those misses by
    # ID, then write the successful synopsis back into the persistent JSON caches.
    # Bound concurrency/work so a large catalogue search does not turn into a slow scan.
    semaphore = asyncio.Semaphore(4)
    async def hydrate_missing(item_id: int, item: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        async with semaphore:
            metadata = await _lookup_search_metadata_by_tmdb_id(item)
            return item_id, metadata

    selected_missing = list(missing_synopsis_candidates.items())[:12]
    if selected_missing:
        hydrated = await asyncio.gather(
            *(hydrate_missing(item_id, item) for item_id, item in selected_missing),
            return_exceptions=True,
        )
        for value in hydrated:
            if isinstance(value, Exception) or not isinstance(value, tuple):
                continue
            item_id, metadata = value
            if not metadata or not str(metadata.get("overview") or "").strip():
                continue
            for item in matched:
                try:
                    existing_id = int(item.get("tmdbId") or item.get("id") or 0)
                except (TypeError, ValueError):
                    existing_id = 0
                if existing_id != item_id:
                    continue
                item.update(metadata)
                item["source"] = "TMDB movie details"
            persisted_item = {**item, **metadata}
            _persist_search_metadata_cache_rows([persisted_item])
            saved_rows = _persist_search_metadata_overview(item_id, metadata["overview"])
            logger.info("Hydrated missing search-card synopsis for TMDB ID %s; persisted rows=%s",
                item_id, saved_rows)

    # Permanently save successful overview hits from any of the catalogue sources.
    matched = [item for item in matched if str(item.get("overview") or "").strip()]
    if matched:
        _persist_search_metadata_cache_rows(matched)

    # Never send blank synopsis entries if no details lookup could recover them;
    # this keeps the response list focused on usable saved metadata.
    return {
        "status": "ready",
        "source": "VPS catalogue cache",
        "requested": len(requested),
        "matched": len(matched),
        "results": matched,
    }


@app.get("/api/search")
async def api_search(request: Request, q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=50)):
    prepare_intent = request.headers.get("x-prepare-intent", "missing")
    prepare_trace = request.headers.get("x-prepare-trace", "missing")
    if prepare_intent != "missing" or prepare_trace != "missing":
        logger.info(
            "PREPARE_TRACE event=live-search-request intent=%s trace=%s query=%r",
            prepare_intent, prepare_trace, q,
        )
    # Live torrent-provider access is explicitly scoped to this user-triggered endpoint.
    token = _MANUAL_TORRENT_SEARCH.set(True)
    try:
        return await search_1337x(q, limit)
    finally:
        _MANUAL_TORRENT_SEARCH.reset(token)

REGIONAL_MOVIE_PREBUILT_CACHE_PATH = Path(os.getenv("REGIONAL_MOVIE_PREBUILT_CACHE_PATH", "/app/data/regional_movie_prebuilt_cache.json"))


@app.get("/api/regional-movies/prebuilt")
async def api_regional_movie_prebuilt(
    request: Request,
    title: str = Query(..., min_length=1, max_length=200),
    year: str = Query("", max_length=4),
):
    """Serve precomputed regional catalogue and torrent details from persistent VPS JSON."""
    prepare_intent = request.headers.get("x-prepare-intent", "missing")
    prepare_trace = request.headers.get("x-prepare-trace", "missing")
    logger.info(
        "PREPARE_TRACE event=prebuilt-request intent=%s trace=%s title=%r year=%s referer=%r",
        prepare_intent, prepare_trace, title, year, request.headers.get("referer", ""),
    )

    def cache_title_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    try:
        payload = json.loads(REGIONAL_MOVIE_PREBUILT_CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "cache_missing", "movie": None, "torrentDetails": None}
    except Exception as exc:
        logger.warning("Could not read regional movie cache: %s", exc)
        return {"status": "cache_unavailable", "movie": None, "torrentDetails": None}
    movies = payload.get("movies") if isinstance(payload, dict) else {}
    key = cache_title_key(title) + "|" + str(year or "")
    entry = (movies or {}).get(key)
    if not entry and not year:
        matches = [value for item_key, value in (movies or {}).items()
                   if item_key.startswith(cache_title_key(title) + "|")]
        entry = matches[0] if len(matches) == 1 else None
    if not isinstance(entry, dict):
        _record_cache_event("regional_movie_prebuilt", "miss")
        logger.info(
            "PREPARE_TRACE event=prebuilt-result intent=%s trace=%s cache=miss title=%r year=%s",
            prepare_intent, prepare_trace, title, year,
        )
        return {"status": "not_cached", "movie": None, "torrentDetails": None,
                "builtAt": payload.get("builtAt")}
    _record_cache_event("regional_movie_prebuilt", "hit")
    cached_details = entry.get("torrentDetails")
    cached_rows = cached_details.get("results") if isinstance(cached_details, dict) else cached_details
    logger.info(
        "PREPARE_TRACE event=prebuilt-result intent=%s trace=%s cache=hit cached_variants=%s title=%r year=%s",
        prepare_intent, prepare_trace, len(cached_rows) if isinstance(cached_rows, list) else 0, title, year,
    )

    # Apply the current size window to saved torrent results at read time too.
    # This filters old snapshots without mutating the persistent cache file.
    torrent_details = entry.get("torrentDetails")
    if isinstance(torrent_details, dict):
        filtered_details = dict(torrent_details)
        cached_results = torrent_details.get("results")
        if isinstance(cached_results, list):
            kept_results = []
            for result in cached_results:
                if not isinstance(result, dict):
                    continue
                try:
                    size_bytes = int(result.get("size") or 0)
                except (TypeError, ValueError, OverflowError):
                    size_bytes = 0
                if MIN_SEARCH_RESULT_SIZE_BYTES <= size_bytes <= MAX_SEARCH_RESULT_SIZE_BYTES:
                    kept_results.append(result)
            filtered_details["results"] = kept_results
            filtered_details["filteredOutBySize"] = len(cached_results) - len(kept_results)
        torrent_details = filtered_details
    elif isinstance(torrent_details, list):
        kept_results = []
        for result in torrent_details:
            if not isinstance(result, dict):
                continue
            try:
                size_bytes = int(result.get("size") or 0)
            except (TypeError, ValueError, OverflowError):
                size_bytes = 0
            if MIN_SEARCH_RESULT_SIZE_BYTES <= size_bytes <= MAX_SEARCH_RESULT_SIZE_BYTES:
                kept_results.append(result)
        torrent_details = kept_results

    return {
        "status": "ready",
        "builtAt": payload.get("builtAt"),
        "movie": entry.get("movie"),
        "catalogues": entry.get("catalogues", []),
        "torrentDetails": torrent_details,
    }



def _cache_file_snapshot(name: str, path: Path, *, bundled: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "file": path.name,
        "exists": False,
        "bytes": 0,
        "modifiedAt": None,
        "entryCount": None,
        "entryGroups": {},
        "bundledSnapshot": bundled,
    }
    try:
        if not path.is_file():
            return row
        stat = path.stat()
        row["exists"] = True
        row["bytes"] = stat.st_size
        row["modifiedAt"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            groups: dict[str, int] = {}
            for key in ("items", "catalogues", "queries", "movies", "results", "ratings", "entries", "records", "pages", "shows"):
                value = payload.get(key)
                if isinstance(value, (dict, list)):
                    groups[key] = len(value)
            row["entryGroups"] = groups
            for key in ("items", "movies", "results", "entries", "records", "ratings", "catalogues", "pages", "shows"):
                if key in groups:
                    row["entryCount"] = groups[key]
                    break
            if row["entryCount"] is None:
                row["entryCount"] = len(payload)
        elif isinstance(payload, list):
            row["entryCount"] = len(payload)
        else:
            row["entryCount"] = 0
    except (OSError, ValueError, TypeError):
        row["parseError"] = True
    return row


@app.get("/api/cache/report")
async def api_cache_report():
    """Inventory saved cache data and report process-local cache/use counters."""
    runtime_paths: dict[str, Path] = {
        name: path for name, path in _RUNTIME_CACHE_SEED_FILES.items()
    }
    runtime_paths["marvel_catalogue.json"] = MARVEL_CATALOGUE_PATH
    runtime_paths["marvel_web_series_catalogue.json"] = Path(
        os.getenv("MARVEL_WEB_SERIES_CATALOGUE_PATH", "/app/data/marvel_web_series_catalogue.json")
    )
    runtime_paths["bollywood_torrent_matched_catalogue.json"] = Path(
        os.getenv("BOLLYWOOD_TORRENT_CATALOGUE_PATH", str(Path(os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_torrent_matched_catalogue.json"))
    )
    runtime_paths["torrent_metadata_cache"] = TORRENT_METADATA_CACHE_FILE
    runtime_paths["regional_tv_prebuilt_cache.json"] = REGIONAL_TV_PREBUILT_CACHE_FILE
    runtime_paths["regional_movie_prebuilt_cache.json"] = REGIONAL_MOVIE_PREBUILT_CACHE_PATH
    runtime_paths["classics_web_series_tv_catalogue.json"] = Path(os.getenv("APP_DATA_DIR", "/app/data")) / "classics_web_series_tv_catalogue.json"
    runtime_paths["classics_anime_tv_catalogue.json"] = Path(os.getenv("APP_DATA_DIR", "/app/data")) / "classics_anime_tv_catalogue.json"
    runtime_paths["nexstream_cache.json"] = Path(os.getenv("NEXSTREAM_CACHE_PATH", "/app/data/nexstream_cache.json"))
    runtime_paths["nexstream_episode_metadata_cache.json"] = Path(os.getenv("NEXSTREAM_EPISODE_CACHE_PATH", "/app/data/nexstream_episode_metadata_cache.json"))
    for key, path in _EXTRA_CATALOGUE_PATHS.items():
        runtime_paths["catalogue:" + key] = path
    runtime_files = [
        _cache_file_snapshot(name, path)
        for name, path in sorted(runtime_paths.items())
    ]

    bundled_paths: dict[str, Path] = {
        name: _CACHE_SEED_DIR / name for name in _CACHE_SEED_FILES
    }
    for name in (
        "tmdb_movie_catalogue_cache.json",
        "tmdb_search_metadata_cache.json",
        "imdb_rating_cache.json",
        "tmdb_ott_availability_cache.json",
        "marvel_tv_catalogue_cache.json",
        "dc_tv_catalogue_cache.json",
        "regional_movie_prebuilt_cache.json",
        "nexstream_cache.json",
        "nexstream_episode_metadata_cache.json",
    ):
        bundled_paths["runtime/" + name] = _CACHE_SEED_DIR / "runtime" / name
    bundled_files = [
        _cache_file_snapshot(name, path, bundled=True)
        for name, path in sorted(bundled_paths.items())
    ]

    memory_store_names = (
        "_tmdb_ott_cache", "_tmdb_movie_catalogue_cache", "_tmdb_company_id_cache",
        "_TMDB_SEARCH_METADATA_CACHE", "_IMDB_RATING_CACHE", "_search_cache",
        "_poster_cache", "_watchmode_search_cache", "_TVMAZE_SEARCH_CACHE",
        "_metadata_cache", "_seedr_folder_cache", "_REGIONAL_TV_PAGE_CACHE",
        "_REGIONAL_TV_DISCOVERY_CACHE", "_REGIONAL_TV_PREBUILT_CACHE",
        "_MARVEL_TV_CATALOGUE_CACHE", "_DC_TV_CATALOGUE_CACHE",
        "_CLASSICS_TV_CATALOGUE_CACHE", "_seedr_ffprobe_cache",
        "_seedr_media_info_cache", "_seedr_embedded_subtitle_cache",
    )
    memory_stores: dict[str, Any] = {}
    for name in memory_store_names:
        value = globals().get(name)
        if isinstance(value, (dict, list, tuple, set)):
            memory_stores[name.lstrip("_")] = len(value)
        elif value is None:
            memory_stores[name.lstrip("_")] = None

    cache_events = {
        store: dict(events)
        for store, events in sorted(_CACHE_DIAGNOSTICS["cache_events"].items())
    }
    explicit_cache_hits = sum(
        int(count)
        for events in cache_events.values()
        for event, count in events.items()
        if "hit" in event
    )
    explicit_cache_misses = sum(
        int(count)
        for events in cache_events.values()
        for event, count in events.items()
        if "miss" in event or "bypass" in event
    )
    blocked_live_lookups = sum(
        int(count)
        for events in cache_events.values()
        for event, count in events.items()
        if "blocked" in event
    )
    availability_live_lookup_events = sum(
        int(count)
        for events in cache_events.values()
        for event, count in events.items()
        if "live_provider_lookup" in event
    )
    cache_use_summary = {
        "explicitCacheHitEvents": explicit_cache_hits,
        "explicitCacheMissOrBypassEvents": explicit_cache_misses,
        "blockedLiveLookupEvents": blocked_live_lookups,
        "availabilityLiveLookupEvents": availability_live_lookup_events,
        "hitRatePercentAmongExplicitHitMissEvents": round(
            100 * explicit_cache_hits / (explicit_cache_hits + explicit_cache_misses), 2
        ) if explicit_cache_hits + explicit_cache_misses else None,
        "scope": "Only explicitly instrumented cache-decision points. Not a whole-app hit rate or catalogue-row percentage.",
    }
    upstream = {
        "requests": int(_CACHE_DIAGNOSTICS["upstream_requests"]),
        "failures": int(_CACHE_DIAGNOSTICS["upstream_failures"]),
        "byProvider": dict(sorted(_CACHE_DIAGNOSTICS["upstream_by_provider"].items())),
        "byEndpoint": dict(sorted(
            _CACHE_DIAGNOSTICS["upstream_by_endpoint"].items(),
            key=lambda pair: pair[1],
            reverse=True,
        )[:100]),
        "byFeature": dict(sorted(
            _CACHE_DIAGNOSTICS["upstream_by_feature"].items(),
            key=lambda pair: pair[1],
            reverse=True,
        )[:150]),
        "statusCodes": dict(sorted(_CACHE_DIAGNOSTICS["upstream_status_codes"].items())),
    }
    now = time.time()
    return {
        "status": "ok",
        "measurementStartedAt": datetime.fromtimestamp(_CACHE_DIAGNOSTICS_STARTED_AT, timezone.utc).isoformat(),
        "measurementScope": "This worker process only; counters reset on worker restart and are not aggregated across Render instances.",
        "ottCacheOnly": CACHE_ONLY_MODE,
        "uptimeSeconds": max(0, int(now - _CACHE_DIAGNOSTICS_STARTED_AT)),
        "cacheEvents": cache_events,
        "cacheUseSummary": cache_use_summary,
        "outboundHttp": upstream,
        "memoryCacheSizes": memory_stores,
        "runtimeCacheFiles": runtime_files,
        "bundledCacheSnapshots": bundled_files,
        "notes": [
            "File entry counts describe saved inventory; they are not cache-hit counts.",
            "Cache event counters measure only the explicitly instrumented cache decision points.",
            "Outbound HTTP counts cover backend requests issued through instrumented httpx clients, grouped by provider and endpoint without query strings or credentials.",
            "NexStream search and episode caches are measured by the shared diagnostics module; frontend localStorage hit counts and Vercel serverless cache hits are outside this backend report.",
            "A cache hit means that path returned or reused an entry; outbound request totals show actual httpx requests attempted by this worker.",
        ],
    }


@app.get("/api/health")
async def api_health():
    return {"name": APP_NAME, "status": "ok"}

@app.get("/api/seedr/session")
async def seedr_session(request: Request):
    """Initialize the browser session quickly; provider health checks happen on demand."""
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)
    # Session bootstrap must not wait on Seedr. The CSRF token is needed before
    # the browser can submit a PAT, and a slow provider must not cause POST 403s.
    # Actual token/API failures are reported by the operation the user requests.
    return {
        "connected": bool(current_seedr_token()),
        "csrfToken": str(session.get("csrf_token") or ""),
    }


async def _validate_seedr_pat(token: str) -> None:
    """Validate a Seedr PAT sequentially; avoid bursts of concurrent auth requests."""
    token = normalize_seedr_token(token)
    if not token:
        raise SeedrError("SEEDR_PAT_MISSING", 400, "A Seedr Personal Access Token is required.")

    endpoints = (
        f"{SEEDR_PAT_BASE}/user",
        f"{SEEDR_PAT_BASE}/tasks",
        f"{SEEDR_PAT_BASE}/fs/root/contents",
        f"{SEEDR_V2_BASE}/tasks",
        f"{SEEDR_V2_BASE}/fs/root/contents",
    )
    outcomes: list[str] = []
    saw_unauthorized = False
    timeout = httpx.Timeout(8.0, connect=3.0)

    # The endpoints have been verified to accept the current PAT when called
    # individually. Do not fan out concurrently: concurrent probes previously
    # all failed at the transport layer despite these same endpoints succeeding
    # when tested one at a time. Stop on the first 2xx response.
    async with _InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in endpoints:
            try:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                logger.info(
                    "Seedr PAT validation transport error: endpoint=%s error=%s",
                    url,
                    type(exc).__name__,
                )
                outcomes.append(f"{url}=transport:{type(exc).__name__}")
                continue

            if 200 <= response.status_code < 300:
                logger.info(
                    "Seedr PAT validation succeeded: endpoint=%s status=%s",
                    url,
                    response.status_code,
                )
                return

            if response.status_code == 401:
                saw_unauthorized = True
                outcomes.append(f"{url}=401")
                continue

            if response.status_code in {403, 404, 405}:
                outcomes.append(f"{url}={response.status_code}")
                continue

            try:
                payload = response.json() if response.content else {}
                reason = str(
                    payload.get("error_description")
                    or payload.get("reason_phrase")
                    or payload.get("message")
                    or payload.get("error")
                    or ""
                ).strip() if isinstance(payload, dict) else ""
            except (ValueError, TypeError):
                reason = ""

            # Retry the next known API variant for transient/provider-specific
            # errors rather than concluding the token is invalid prematurely.
            outcomes.append(f"{url}=HTTP{response.status_code}")
            logger.info(
                "Seedr PAT validation endpoint returned HTTP %s: endpoint=%s reason=%s",
                response.status_code,
                url,
                reason[:160],
            )

    logger.warning(
        "Seedr PAT validation found no successful endpoint: %s",
        ", ".join(outcomes),
    )
    if saw_unauthorized and not any("transport:" in row for row in outcomes):
        raise SeedrError(
            "SEEDR_PAT_REJECTED",
            401,
            "Seedr rejected the Personal Access Token. Copy a fresh PAT from Seedr and try again.",
        )

    raise SeedrError(
        "SEEDR_PAT_VALIDATION_FAILED",
        502,
        "Could not validate the Seedr token using the account API. Please retry shortly.",
    )


@app.post("/api/seedr/connect/pat")
async def seedr_connect_pat(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)

    try:
        payload = await request.json()
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be JSON.")

    raw_pat = payload.get("pat") or payload.get("token")
    if not isinstance(raw_pat, str):
        raise HTTPException(400, "A Seedr Personal Access Token is required.")

    pat = normalize_seedr_token(raw_pat)
    if not pat:
        raise HTTPException(400, "A Seedr Personal Access Token is required.")

    await _validate_seedr_pat(pat)

    session["access_token"] = pat
    session["refresh_token"] = ""
    session["token_type"] = "Bearer"
    session["auth_mode"] = "pat"
    session["device"] = None
    session["last_seen"] = time.time()

    logger.info(
        "Seedr PAT connected: session=%s token_length=%s",
        _seedr_session_fingerprint(session_id),
        len(pat),
    )
    return {"status": "connected", "connected": True}


@app.post("/api/seedr/connect/start")
async def seedr_connect_start(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)

    logger.info(
        "Seedr connect start: session=%s cookie_present=%s has_device=%s has_token=%s",
        _seedr_session_fingerprint(session_id),
        bool(request.cookies.get(SEEDR_SESSION_COOKIE)),
        bool(isinstance(session.get("device"), dict)),
        bool(_seedr_session_token(session_id)),
    )

    token = current_seedr_token()
    if token:
        return {"status": "connected", "connected": True}

    try:
        async with _InstrumentedAsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(
                SEEDR_DEVICE_CODE_URL,
                params={"client_id": SEEDR_DEVICE_CLIENT_ID},
                headers={"Accept": "application/json"},
            )
        raw = response.text
        try:
            data = response.json() if raw else {}
        except Exception:
            data = {}

        if response.status_code >= 400 or not isinstance(data, dict):
            detail = seedr_error_message(response.status_code, data, raw)
            raise SeedrError("SEEDR_DEVICE_CODE_FAILED", 502, detail)

        device_code = str(data.get("device_code") or data.get("deviceCode") or "").strip()
        user_code = str(data.get("user_code") or data.get("userCode") or "").strip()
        verification_url = str(
            data.get("verification_url")
            or data.get("verificationUrl")
            or "https://www.seedr.cc/devices"
        ).strip()
        expires_in = int(float(data.get("expires_in") or data.get("expiresIn") or 900))

        if not device_code or not user_code:
            raise SeedrError(
                "SEEDR_DEVICE_CODE_INVALID",
                502,
                "Seedr returned an incomplete device authorization response.",
            )

        session["device"] = {
            "device_code": device_code,
            "user_code": user_code,
            "verification_url": verification_url,
            "expires_at": time.time() + max(60, expires_in),
            "created_at": time.time(),
        }
        session["last_seen"] = time.time()

        poll_interval = int(float(data.get("interval") or 5))
        poll_interval = max(2, min(30, poll_interval))
        session["device"]["interval"] = poll_interval
        logger.info(
            "Seedr connect start stored device: session=%s expires_in=%s interval=%s",
            _seedr_session_fingerprint(session_id),
            expires_in,
            poll_interval,
        )
        return {
            "status": "pending",
            "connected": False,
            "userCode": user_code,
            "verificationUrl": verification_url,
            "expiresIn": expires_in,
            "interval": poll_interval,
        }
    except SeedrError:
        raise
    except Exception as exc:
        logger.warning("Seedr device-code request failed: %s", exc)
        raise SeedrError(
            "SEEDR_DEVICE_CODE_FAILED",
            502,
            "Could not start Seedr account authorization right now.",
        ) from exc


def _extract_seedr_access_token(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("access_token", "accessToken", "token"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                normalized = normalize_seedr_token(candidate)
                if normalized:
                    return normalized

        for key in ("data", "result", "response"):
            nested = payload.get(key)
            token = _extract_seedr_access_token(nested)
            if token:
                return token
    return ""


@app.get("/api/seedr/connect/status")
async def seedr_connect_status(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    session = _seedr_get_session(session_id)
    token = _seedr_session_token(session_id)
    device = session.get("device")

    logger.info(
        "Seedr connect status: session=%s cookie_present=%s has_device=%s has_token=%s",
        _seedr_session_fingerprint(session_id),
        bool(request.cookies.get(SEEDR_SESSION_COOKIE)),
        bool(isinstance(device, dict)),
        bool(token),
    )

    if token:
        try:
            token_ctx = _seedr_request_token.set(token)
            try:
                await seedr_request("/user")
            finally:
                _seedr_request_token.reset(token_ctx)
            session["device"] = None
            return {"status": "connected", "connected": True}
        except SeedrError as exc:
            if exc.status_code == 401:
                _clear_seedr_session_token(session_id)
            else:
                return {"status": "pending", "connected": False}

    device = session.get("device")
    if not isinstance(device, dict):
        return {
            "status": "error",
            "connected": False,
            "code": "SEEDR_SESSION_LOST",
            "message": "The Seedr connection session was lost. Start the connection again.",
        }

    expires_at = float(device.get("expires_at") or 0)
    if expires_at and time.time() >= expires_at:
        session["device"] = None
        return {
            "status": "expired",
            "connected": False,
            "message": "The Seedr authorization code expired. Start a new connection.",
        }

    device_code = str(device.get("device_code") or "").strip()
    if not device_code:
        session["device"] = None
        return {
            "status": "error",
            "connected": False,
            "message": "Seedr did not provide a usable device code.",
        }

    try:
        async with _InstrumentedAsyncClient(timeout=10, follow_redirects=True) as client:
            # Seedr's device authorization endpoint expects the original
            # device_code. The current Seedr client implementations pass only
            # device_code here; do not add client_id to this request.
            response = await client.get(
                SEEDR_DEVICE_AUTHORIZE_URL,
                params={"device_code": device_code},
                headers={"Accept": "application/json"},
            )

        raw = response.text
        try:
            data = response.json() if raw else {}
        except Exception:
            data = {}

        access_token = _extract_seedr_access_token(data)
        logger.info(
            "Seedr device authorization poll: http=%s token_present=%s error=%s message=%s",
            response.status_code,
            bool(access_token),
            str(data.get("error") or data.get("code") or "")[:120] if isinstance(data, dict) else "",
            str(
                (data.get("error_description") or data.get("message") or data.get("error") or "")
                if isinstance(data, dict) else ""
            )[:200],
        )
        if access_token:
            refresh_token = ""
            token_type = "Bearer"
            if isinstance(data, dict):
                refresh_token = str(data.get("refresh_token") or data.get("refreshToken") or "").strip()
                token_type = str(data.get("token_type") or "Bearer").strip() or "Bearer"
                for key in ("data", "result", "response"):
                    nested = data.get(key)
                    if isinstance(nested, dict):
                        refresh_token = refresh_token or str(
                            nested.get("refresh_token") or nested.get("refreshToken") or ""
                        ).strip()
                        token_type = str(nested.get("token_type") or token_type).strip() or token_type

            session["access_token"] = access_token
            session["refresh_token"] = refresh_token
            session["token_type"] = token_type
            session["device"] = None
            session["last_seen"] = time.time()

            token_ctx = _seedr_request_token.set(access_token)
            try:
                await seedr_request("/user")
            except SeedrError:
                _clear_seedr_session_token(session_id)
                raise
            finally:
                _seedr_request_token.reset(token_ctx)

            return {"status": "connected", "connected": True}

        error_code = ""
        error_message = ""
        if isinstance(data, dict):
            error_code = str(data.get("error") or data.get("code") or "").strip().lower()
            error_message = str(
                data.get("error_description")
                or data.get("message")
                or data.get("error")
                or ""
            ).strip()

        pending = (
            response.status_code in {400, 409, 428}
            and (
                not error_code
                or "pending" in error_code
                or "authorize" in error_code
                or "not_authorized" in error_code
                or "not approved" in error_message.lower()
            )
        )
        if pending:
            return {
                "status": "pending",
                "connected": False,
                "expiresIn": max(0, int(expires_at - time.time())),
                "interval": max(2, min(30, int(float(device.get("interval") or 5)))),
            }

        if response.status_code >= 400:
            if response.status_code == 401:
                session["device"] = None
                return {
                    "status": "error",
                    "connected": False,
                    "message": "Seedr rejected this authorization request.",
                }
            return {
                "status": "error",
                "connected": False,
                "message": error_message or seedr_error_message(response.status_code, data, raw),
            }

        return {
            "status": "pending",
            "connected": False,
            "expiresIn": max(0, int(expires_at - time.time())),
        }
    except SeedrError:
        raise
    except Exception as exc:
        logger.info("Seedr device authorization poll failed: %s", exc)
        return {
            "status": "pending",
            "connected": False,
            "expiresIn": max(0, int(expires_at - time.time())),
        }


@app.post("/api/seedr/connect/disconnect")
async def seedr_connect_disconnect(request: Request):
    session_id = _seedr_request_session_id.get().strip()
    _clear_seedr_session_token(session_id)
    return {"status": "disconnected", "connected": False}


@app.get("/api/seedr/token-diagnostic")
async def seedr_token_diagnostic():
    if not current_seedr_token():
        return {
            "configured": False,
            "code": "SEEDR_TOKEN_MISSING",
            "tokenLength": 0,
            "tokenFingerprint": None,
            "checks": {},
        }

    token = normalize_seedr_token(current_seedr_token())
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else None

    async def check(path: str) -> dict[str, Any]:
        try:
            await seedr_request(path)
            return {"ok": True, "status": 200}
        except SeedrError as exc:
            return {"ok": False, "status": exc.status_code, "code": exc.code, "message": exc.detail}
        except Exception as exc:
            return {"ok": False, "status": 0, "code": "LOCAL_ERROR", "message": str(exc)[:200]}

    return {
        "configured": True,
        "code": "SEEDR_TOKEN_PRESENT",
        "tokenLength": len(token),
        "tokenFingerprint": fingerprint,
        "checks": {
            "user": await check("/tasks" if _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat" else "/user"),
        },
    }


@app.get("/api/seedr/auth-status")
async def seedr_auth_status():
    if not current_seedr_token():
        return {
            "configured": False,
            "authenticated": False,
            "code": "SEEDR_TOKEN_MISSING",
            "message": "Seedr API token is not configured in Render.",
        }

    try:
        # The old working integration treats a successful /user call as the
        # authentication test. Do the same here; /user is explicitly documented
        # by Seedr for Bearer-authenticated requests.
        await seedr_request(
            "/tasks" if _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat" else "/user"
        )
        return {
            "configured": True,
            "authenticated": True,
            "code": "SEEDR_AUTH_OK",
        }
    except SeedrError as exc:
        return {
            "configured": True,
            "authenticated": False,
            "code": exc.code,
            "message": exc.detail,
        }



@app.get("/api/seedr/quota")
async def seedr_quota():
    if not current_seedr_token():
        return {"configured": False, "maxSpace": 0, "usedSpace": 0, "remainingSpace": 0}

    try:
        is_pat = _seedr_session_auth_mode(_seedr_request_session_id.get().strip()) == "pat"
        result = seedr_data(await seedr_request("/fs/root/contents" if is_pat else "/user"))
        storage = result.get("account", {}).get("storage", {}) if isinstance(result, dict) else {}
        if not isinstance(storage, dict):
            storage = result.get("storage", {}) if isinstance(result, dict) else {}
        result_dict = result if isinstance(result, dict) else {}
        if is_pat:
            max_space = int(float(
                result_dict.get("space_max", 0)
                or result_dict.get("max_space", 0)
                or result_dict.get("maxSpace", 0)
                or storage.get("limit", 0)
            ))
            used = int(float(
                result_dict.get("space_used", 0)
                or result_dict.get("used_space", 0)
                or result_dict.get("usedSpace", 0)
                or storage.get("used", 0)
            ))
        else:
            max_space = int(float(
                storage.get("limit")
                or storage.get("max_space")
                or storage.get("maxSpace")
                or result_dict.get("max_space", 0)
                or result_dict.get("space_max", 0)
                or 0
            ))
            used = int(float(
                storage.get("used")
                or storage.get("used_space")
                or storage.get("usedSpace")
                or result_dict.get("used_space", 0)
                or result_dict.get("space_used", 0)
                or 0
            ))
    except SeedrError:
        raise
    except Exception as exc:
        logger.warning("Seedr quota parsing failed: %s", exc)
        raise SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            503,
            "Seedr account storage information is temporarily unavailable.",
        ) from exc

    if not (max_space > 0) or used < 0 or used > max_space:
        raise SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            503,
            "Seedr account storage information is temporarily unavailable.",
        )

    return {
        "configured": True,
        "maxSpace": max_space,
        "usedSpace": used,
        "remainingSpace": max(0, max_space - used),
    }

async def _prepare_seedr_space(
    required_bytes: int,
    *,
    force_one: bool = False,
) -> list[dict[str, Any]]:
    """Free old completed Seedr folders; optionally force at least one deletion after a quota rejection."""
    required = max(0, int(required_bytes or 0))
    if required <= 0:
        return []

    quota = await seedr_quota()
    remaining = int(quota.get("remainingSpace") or 0)
    if remaining >= required and not force_one:
        return []

    library = await get_seedr_metadata_tree(force_refresh=True)
    folders = [
        folder for folder in (library.get("folders") or [])
        if isinstance(folder, dict)
        and str(folder.get("folderId") or folder.get("id") or "").strip()
        and str(folder.get("folderId") or folder.get("id") or "").strip() != "0"
    ]

    # Prefer actual Seedr task timestamps so the oldest completed torrent is
    # removed first. Fall back to the library order when a provider response
    # does not expose a timestamp.
    task_meta: dict[str, dict[str, Any]] = {}
    active_folder_ids: set[str] = set()
    try:
        tasks_payload = seedr_data(await seedr_request("/tasks"))
        for raw_task in arr(tasks_payload, ("tasks", "torrents", "items")):
            task = unwrap_seedr_task(seedr_data(raw_task))
            if not task:
                continue
            folder_id = seedr_task_folder_id(task)
            if not folder_id:
                continue
            if not task_complete(task):
                active_folder_ids.add(str(folder_id))
            task_meta[str(folder_id)] = task
    except (HTTPException, SeedrError):
        pass

    def task_timestamp(task: dict[str, Any]) -> float:
        for key in (
            "created_at", "createdAt", "created", "added_at", "addedAt",
            "time_added", "timeAdded", "timestamp", "date_added", "dateAdded",
        ):
            value = task.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value if value < 10_000_000_000 else value / 1000)
            if isinstance(value, str) and value.strip():
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return parsed.timestamp()
                except Exception:
                    continue
        return 0.0

    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for index, folder in enumerate(folders):
        folder_id = str(folder.get("folderId") or folder.get("id") or "").strip()
        if not folder_id or folder_id in active_folder_ids:
            continue
        task = task_meta.get(folder_id, {})
        candidates.append((index, task_timestamp(task), folder))

    candidates.sort(key=lambda item: (item[1] if item[1] > 0 else float("inf"), item[0]))

    deleted: list[dict[str, Any]] = []
    for _, _, folder in candidates:
        if remaining >= required and (not force_one or deleted):
            break

        folder_id = str(folder.get("folderId") or folder.get("id") or "").strip()
        folder_name = str(folder.get("torrentName") or folder.get("name") or folder_id).strip()
        folder_size = max(0, int(float(folder.get("totalSize") or 0)))

        try:
            await seedr_request(f"/fs/folder/{quote(folder_id)}", "DELETE")
            deleted.append({
                "folderId": folder_id,
                "name": folder_name,
                "size": folder_size,
            })
            remaining += folder_size
            _seedr_folder_cache.pop(folder_id, None)
        except (HTTPException, SeedrError) as exc:
            logger.warning(
                "Seedr automatic cleanup skipped folder=%s status=%s detail=%s",
                folder_id,
                getattr(exc, "status_code", 0),
                getattr(exc, "detail", str(exc)),
            )

    global _seedr_metadata_cache
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()

    if remaining < required:
        raise SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            413,
            "Seedr does not have enough space for this torrent, even after removing older completed files.",
        )

    logger.info(
        "Seedr automatic cleanup freed=%s required=%s deleted=%s",
        sum(int(item.get("size") or 0) for item in deleted),
        required,
        len(deleted),
    )
    return deleted


@app.post("/api/seedr/add")
async def seedr_add(request: Request):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    # Direct Seedr mode:
    # validate without changing the input, then forward that exact string.
    try:
        payload = await request.json()
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be JSON.")

    raw_magnet = payload.get("magnet")
    if not isinstance(raw_magnet, str) or not raw_magnet:
        raise HTTPException(400, "A magnet link is required.")

    if not info_hash(raw_magnet):
        raise HTTPException(400, "A valid BTIH magnet link is required")

    requested_folder = str(payload.get("folder_id") or "").strip()
    folder = int(requested_folder) if requested_folder.isdigit() else 0
    auto_cleanup = bool(payload.get("auto_cleanup"))
    required_bytes = max(0, int(float(payload.get("required_bytes") or 0)))

    session_id = _seedr_request_session_id.get().strip()
    logger.info(
        "Seedr add start: session=%s mode=%s magnet_length=%s magnet_info_hash=%s "
        "requested_folder=%s resolved_folder=%s",
        _seedr_session_fingerprint(session_id),
        _seedr_session_auth_mode(session_id),
        len(raw_magnet),
        info_hash(raw_magnet),
        requested_folder or "<none>",
        folder,
    )

    # Treat every new Seedr add as replacing the prior unfinished transfer.
    # Do not depend on frontend localStorage/current-notice state: an old task
    # may still exist after a page refresh or backend/container restart.
    replace_task_id = str(payload.get("replace_task_id") or "").strip()
    cancelled_tasks = await _cancel_active_seedr_tasks_for_replacement(replace_task_id)

    deleted_folders: list[dict[str, Any]] = [
        {
            "folderId": str(item.get("folderId") or ""),
            "name": str(item.get("name") or "Cancelled Seedr task"),
            "size": max(0, int(item.get("size") or 0)),
        }
        for item in cancelled_tasks
        if item.get("folderDeleted") and str(item.get("folderId") or "").strip()
    ]
    if auto_cleanup and required_bytes > 0:
        deleted_folders.extend(await _prepare_seedr_space(required_bytes))

    try:
        task = unwrap_seedr_task(await add_task(raw_magnet, folder))
    except SeedrError as exc:
        reason = str(exc.detail or "").lower()
        is_storage_shortage = (
            exc.status_code == 413
            or "not enough space" in reason
            or "insufficient storage" in reason
            or "insufficient space" in reason
        )
        if not (is_storage_shortage and auto_cleanup and required_bytes > 0):
            logger.exception(
                "Seedr add failed: session=%s mode=%s code=%s status=%s detail=%s",
                _seedr_session_fingerprint(session_id),
                _seedr_session_auth_mode(session_id),
                exc.code,
                exc.status_code,
                exc.detail,
            )
            raise

        # Seedr can reject a new magnet even when its quota endpoint appears to
        # have enough free bytes (for example, while an old transfer is stuck
        # reserving space). Cancelled tasks are already removed; now delete at
        # least one oldest completed folder and retry the same requested magnet
        # exactly once.
        logger.warning(
            "Seedr reported insufficient space after preflight; reclaiming one more completed folder before retry: required=%s",
            required_bytes,
        )
        deleted_folders.extend(await _prepare_seedr_space(required_bytes, force_one=True))
        try:
            task = unwrap_seedr_task(await add_task(raw_magnet, folder))
        except SeedrError as retry_exc:
            logger.exception(
                "Seedr retry after storage cleanup failed: session=%s code=%s status=%s detail=%s",
                _seedr_session_fingerprint(session_id),
                retry_exc.code,
                retry_exc.status_code,
                retry_exc.detail,
            )
            raise
    except Exception as exc:
        logger.exception(
            "Seedr add unexpected failure: session=%s mode=%s exception_type=%s exception=%s",
            _seedr_session_fingerprint(session_id),
            _seedr_session_auth_mode(session_id),
            type(exc).__name__,
            str(exc)[:2000],
        )
        raise
    tid = task_id(task)
    if not tid:
        raise HTTPException(502, "Seedr did not return a task id")

    task_folder_id = seedr_task_folder_id(task)
    task_name = seedr_task_name(task) or f"Torrent {tid}"
    schedule_seedr_cleanup(str(tid), task_name, task_folder_id)

    logger.info(
        "Seedr add success: session=%s mode=%s task_id=%s folder_id=%s task_name=%s",
        _seedr_session_fingerprint(session_id),
        _seedr_session_auth_mode(session_id),
        tid,
        task_folder_id or "<none>",
        task_name[:300],
    )

    return {
        "backend": "seedr",
        "task_id": int(tid) if tid.isdigit() else tid,
        "id": int(tid) if tid.isdigit() else tid,
        "torrent_name": task_name,
        "folder_id": task_folder_id,
        "deleted_folders": deleted_folders,
        "cancelled_tasks": cancelled_tasks,
    }

def _seedr_file_folder_id(files: list[dict[str, Any]]) -> str:
    """Return the actual torrent folder id exposed by completed file rows."""
    for file in files:
        folder_id = str(file.get("folderId") or "").strip()
        if folder_id:
            return folder_id
    return ""


def _seedr_effective_task_folder_id(
    task: dict[str, Any],
    files: list[dict[str, Any]] | None = None,
) -> str:
    """Prefer the created torrent folder over the destination parent folder."""
    task_folder_id = seedr_task_folder_id(task)
    file_folder_id = _seedr_file_folder_id(files or [])

    if file_folder_id and (
        not task_folder_id
        or file_folder_id != task_folder_id
        or task_folder_id == "0"
    ):
        return file_folder_id
    return task_folder_id


def _remember_seedr_cleanup_folder(tid: str, folder_id: str) -> None:
    folder_id = str(folder_id or "").strip()
    if not folder_id:
        return
    job = _seedr_cleanup_jobs.get(str(tid).strip())
    if not job:
        return
    if str(job.get("folderId") or "").strip() == folder_id:
        return
    job["folderId"] = folder_id
    _save_seedr_cleanup_jobs()


@app.get("/api/seedr/tasks/{tid}/progress")
async def seedr_task_progress(tid: str):
    """Fast polling endpoint: only fetch task state/progress from Seedr."""
    try:
        raw = seedr_data(await seedr_request(f"/tasks/{quote(tid)}"))
    except HTTPException as exc:
        if exc.status_code == 404:
            return {
                "taskId": tid,
                "status": "not_found",
                "progress": 0,
                "name": "",
                "folderId": "",
            }
        raise

    task = (
        raw.get("task")
        if isinstance(raw, dict) and isinstance(raw.get("task"), dict)
        else (raw if isinstance(raw, dict) else {})
    )
    progress = float(task.get("progress") or 0)
    complete = task_complete(task)
    if complete:
        progress = 100

    state = str(task.get("state") or task.get("status") or "").lower()
    status = "completed" if complete else (
        "waiting" if state in {"queued", "pending", "waiting", "paused", "stopped"} else "downloading"
    )

    task_folder_id = seedr_task_folder_id(task)
    completed_files: list[dict[str, Any]] = []
    if complete:
        try:
            completed_files = await task_contents(tid)
        except (HTTPException, SeedrError):
            completed_files = []

    effective_folder_id = _seedr_effective_task_folder_id(task, completed_files)
    task_id_value = str(tid).strip()

    canonical_name = str(
        _seedr_torrent_names_by_task.get(task_id_value)
        or _seedr_torrent_names.get(effective_folder_id)
        or seedr_task_name(task)
        or ""
    ).strip()

    if effective_folder_id and canonical_name:
        _seedr_torrent_names[effective_folder_id] = canonical_name
        if complete:
            _remember_seedr_cleanup_folder(task_id_value, effective_folder_id)
        # Seedr can expose the folder only after the task starts. Rename at
        # that point, rather than only immediately after /tasks POST.
        # Do not call Seedr folder-management endpoints from the free-account
        # progress path. The task state itself is sufficient for polling.

    seedr_task_display_name = canonical_name
    return {
        "taskId": tid,
        "status": status,
        "progress": progress,
        "name": seedr_task_display_name,
        "folderId": effective_folder_id,
    }

@app.get("/api/seedr/tasks/{tid}")
async def seedr_task(tid: str):
    try:
        raw = seedr_data(await seedr_request(f"/tasks/{quote(tid)}"))
    except HTTPException as exc:
        if exc.status_code == 404:
            return {"taskId": tid, "status": "not_found", "progress": 0, "files": [], "downloadUrl": None}
        raise
    task = raw.get("task") if isinstance(raw, dict) and isinstance(raw.get("task"), dict) else (raw if isinstance(raw, dict) else {})
    progress = float(task.get("progress") or 0)
    complete = task_complete(task)
    if complete:
        progress = 100
    files = await task_contents(tid)
    folder_id = _seedr_effective_task_folder_id(task, files)
    _remember_seedr_cleanup_folder(str(tid).strip(), folder_id)
    task_id_value = str(tid).strip()
    canonical_name = str(
        _seedr_torrent_names_by_task.get(task_id_value)
        or _seedr_torrent_names.get(folder_id)
        or seedr_task_name(task)
        or ""
    ).strip()
    if folder_id and canonical_name:
        previous_name = _seedr_torrent_names.get(folder_id)
        _seedr_torrent_names[folder_id] = canonical_name
        if previous_name != canonical_name:
            try:
                await rename_seedr_folder(folder_id, canonical_name)
            except Exception:
                pass
            global _seedr_metadata_cache
            _seedr_metadata_cache = None
    folderNameValue = canonical_name or (await folder_name(folder_id) if folder_id else "")
    for f in files:
        f["folderPath"] = "/Torrent Studio" + ("/" + folderNameValue if folderNameValue else "")
        # Do not resolve direct Seedr URLs while polling/finalizing a task.
        # A fresh URL is generated only by an explicit download/copy/stream action.
        f["url"] = None
    return {"taskId": tid, "name": str(task.get("title") or task.get("name") or ""), "folderName": folderNameValue, "folderId": folder_id, "status": "completed" if complete else "downloading", "progress": progress, "task": task, "files": files, "downloadUrl": None}

async def seedr_folder_payload(folder_id: str) -> dict[str, Any]:
    """Fetch one Seedr folder level, with a short-lived in-process cache."""
    folder_id = str(folder_id).strip()
    if not folder_id:
        return {}

    now = asyncio.get_running_loop().time()
    cached = _seedr_folder_cache.get(folder_id)
    if cached and now - cached[0] < SEEDR_FOLDER_CACHE_SECONDS:
        return cached[1]

    async with _seedr_folder_semaphore:
        # Re-check after waiting for the semaphore so concurrent callers do
        # not issue duplicate Seedr requests for the same folder.
        now = asyncio.get_running_loop().time()
        cached = _seedr_folder_cache.get(folder_id)
        if cached and now - cached[0] < SEEDR_FOLDER_CACHE_SECONDS:
            return cached[1]

        try:
            # Root access is inconsistent across Seedr API variants/accounts.
            # Prefer the normal bearer-authenticated filesystem endpoint, then
            # fall back to the dedicated root endpoint and finally the legacy
            # list_contents endpoint. A failure in one variant must not be
            # presented as a token failure when another variant works.
            if folder_id == "0":
                root_errors: list[str] = []

                try:
                    payload = seedr_data(await seedr_request("/fs/folder/0/contents"))
                    logger.info("Seedr library root resolved via bearer fs endpoint")
                except (HTTPException, SeedrError) as exc:
                    root_errors.append(f"fs:{getattr(exc, 'status_code', 0)}")
                    try:
                        payload = seedr_data(await seedr_root_request())
                        logger.info("Seedr library root resolved via dedicated root endpoint")
                    except (HTTPException, SeedrError) as root_exc:
                        root_errors.append(f"root:{getattr(root_exc, 'status_code', 0)}")
                        try:
                            payload = seedr_data(await legacy_seedr_list_contents("0"))
                            logger.info("Seedr library root resolved via legacy list_contents endpoint")
                        except (HTTPException, SeedrError) as legacy_exc:
                            root_errors.append(f"legacy:{getattr(legacy_exc, 'status_code', 0)}")
                            logger.warning(
                                "Seedr library root resolution failed across all API variants: %s",
                                ",".join(root_errors),
                            )
                            raise legacy_exc
            else:
                payload = seedr_data(await seedr_request(f"/fs/folder/{quote(folder_id)}/contents"))
        except HTTPException as exc:
            if exc.status_code == 404:
                return {}
            raise

    result = payload if isinstance(payload, dict) else {}
    _seedr_folder_cache[folder_id] = (asyncio.get_running_loop().time(), result)
    return result


def direct_folder_summary(folder_id: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    files = arr(payload, ("files", "items"))
    folders = arr(payload, ("folders", "directories"))
    total_size = 0
    valid_files = 0
    for raw in files:
        if not isinstance(raw, dict):
            continue
        try:
            size = int(float(raw.get("size") or 0))
        except Exception:
            size = 0
        total_size += max(0, size)
        if str(raw.get("id") or raw.get("file_id") or "").strip():
            valid_files += 1

    return {
        "id": str(folder_id),
        "folderId": str(folder_id),
        "name": Path(path.rstrip("/")).name or "Root Files",
        "path": path,
        "filesCount": valid_files,
        "totalSize": total_size,
        "folderCount": len([x for x in folders if isinstance(x, dict)]),
    }


async def build_seedr_metadata_tree(
    folder_id: str,
    path: str,
    folder_name_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Build the first library response from the root and its immediate child
    folders. Child contents are fetched concurrently so folder cards already
    have exact file counts/sizes when the browser receives this response.
    """
    folder_name_overrides = folder_name_overrides or {}
    payload = await seedr_folder_payload(folder_id)
    summary = direct_folder_summary(folder_id, path, payload)

    child_entries: list[tuple[str, str, dict[str, Any]]] = []
    for raw in arr(payload, ("folders", "directories")):
        if not isinstance(raw, dict):
            continue
        child_id = str(raw.get("id") or raw.get("folder_id") or "").strip()
        if not child_id:
            continue
        folder_override = str(folder_name_overrides.get(child_id) or "").strip()
        child_name = (
            folder_override
            or str(raw.get("name") or raw.get("title") or child_id).strip()
            or child_id
        )
        child_path = path.rstrip("/") + "/" + child_name
        child_entries.append((child_id, child_name, raw))

    async def load_child(entry: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
        child_id, child_name, raw = entry
        child_payload = await seedr_folder_payload(child_id)

        # Older Seedr folders can be exposed with their raw 40-character
        # identifier as the visible name. For a single-file torrent we can
        # recover the human title from the actual file name and normalize the
        # folder once, so existing downloads also get fixed on refresh.
        effective_child_name = child_name
        child_files = arr(child_payload, ("files", "items"))
        if (
            re.fullmatch(r"[0-9a-fA-F]{40}", effective_child_name)
            and len(child_files) == 1
            and isinstance(child_files[0], dict)
        ):
            raw_file_name = str(
                child_files[0].get("name")
                or child_files[0].get("title")
                or ""
            ).strip()
            inferred_name = Path(raw_file_name.replace("\\", "/")).name
            inferred_name = Path(inferred_name).stem.strip()
            if inferred_name:
                try:
                    renamed = await rename_seedr_folder(child_id, inferred_name)
                except Exception:
                    renamed = False
                _seedr_torrent_names[child_id] = inferred_name
                effective_child_name = inferred_name
                if renamed:
                    logger.info(
                        "Renamed legacy hash-named Seedr folder %s to %s",
                        child_id,
                        inferred_name,
                    )

        child_summary = direct_folder_summary(
            child_id,
            path.rstrip("/") + "/" + effective_child_name,
            child_payload,
        )

        # Some Seedr responses expose size/count directly on the folder item;
        # use those only when the contents endpoint did not provide a value.
        if child_summary["filesCount"] == 0:
            for key in ("files_count", "file_count", "filesCount", "fileCount", "count"):
                value = raw.get(key)
                if value is not None:
                    try:
                        child_summary["filesCount"] = max(0, int(value))
                        break
                    except (TypeError, ValueError):
                        pass

        if child_summary["totalSize"] == 0:
            for key in ("size", "total_size", "totalSize"):
                value = raw.get(key)
                if value is not None:
                    try:
                        child_summary["totalSize"] = max(0, int(float(value)))
                        break
                    except (TypeError, ValueError):
                        pass

        child_summary["torrentName"] = (
            _seedr_torrent_names.get(child_id)
            or folder_name_overrides.get(child_id)
            or (effective_child_name if re.fullmatch(r"[0-9a-fA-F]{40}", child_name) is None else "")
            or ""
        )
        child_summary["folderCount"] = len(
            [x for x in arr(child_payload, ("folders", "directories")) if isinstance(x, dict)]
        )
        return child_summary

    children: list[dict[str, Any]] = []
    if child_entries:
        results = await asyncio.gather(
            *(load_child(entry) for entry in child_entries),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, dict):
                children.append(result)

    # The top-level library badge represents all files in its visible torrent
    # folders, not just files placed directly in the root.
    summary["filesCount"] += sum(int(item.get("filesCount") or 0) for item in children)
    summary["totalSize"] += sum(int(item.get("totalSize") or 0) for item in children)
    summary["folderCount"] = len(children)

    return summary, children


async def get_seedr_metadata_tree(force_refresh: bool = False) -> dict[str, Any]:
    global _seedr_metadata_cache, _seedr_metadata_task

    if not current_seedr_token():
        return {"configured": False, "root": None, "folders": []}

    now = asyncio.get_running_loop().time()

    if force_refresh:
        # A completed Seedr task can become visible in the filesystem a few
        # seconds after the metadata cache was populated. Clear both caches so
        # the completion flow can see the new folder immediately.
        _seedr_metadata_cache = None
        _seedr_folder_cache.clear()

    if _seedr_metadata_cache and now - _seedr_metadata_cache[0] < SEEDR_METADATA_CACHE_SECONDS:
        return _seedr_metadata_cache[1]

    if _seedr_metadata_task is not None and not _seedr_metadata_task.done():
        return await _seedr_metadata_task

    async def build() -> dict[str, Any]:
        # Seedr's account root is exposed through a dedicated endpoint.
        # Keep SEEDR_LIBRARY_FOLDER_ID as an optional override for deployments
        # that want to start from a specific existing folder.
        root = "0"

        # Resolve human-readable torrent names from Seedr task metadata in one
        # call. The folder contents/counts and task list are independent, so
        # fetch them concurrently without adding a serial round trip.
        folder_name_overrides: dict[str, str] = dict(_seedr_torrent_names)
        task_folders: list[tuple[str, str]] = []
        try:
            tasks_payload = seedr_data(await seedr_request("/tasks"))
            for raw_task in arr(tasks_payload, ("tasks", "torrents", "items")):
                task = unwrap_seedr_task(seedr_data(raw_task))
                if not task:
                    continue

                task_folder_id = seedr_task_folder_id(task)
                task_name = seedr_task_name(task)

                if task_folder_id:
                    task_folders.append((task_folder_id, task_name))

                if task_folder_id and task_name:
                    # The title supplied by our search/add flow is canonical.
                    # Seedr's task title can contain provider/site prefixes
                    # (for example "www.UIndex.org - ..."), so never let task
                    # metadata overwrite an existing canonical title.
                    if task_folder_id not in _seedr_torrent_names:
                        _seedr_torrent_names[task_folder_id] = task_name
                    folder_name_overrides[task_folder_id] = (
                        _seedr_torrent_names.get(task_folder_id) or task_name
                    )
        except (HTTPException, SeedrError):
            # Folder metadata remains usable even when task-name lookup fails.
            pass

        try:
            root_summary, children = await build_seedr_metadata_tree(
                root,
                "/Torrent Studio",
                folder_name_overrides,
            )
        except (HTTPException, SeedrError) as exc:
            # Some Seedr tokens can authenticate and manage transfers while
            # denying the account-root filesystem listing. Do not turn that
            # provider limitation into a library-wide error. Reconstruct the
            # visible library from the task list and each task's own contents.
            if getattr(exc, "status_code", 0) not in {401, 403}:
                raise

            # First collect folder IDs from the task list. Seedr may expose
            # the created folder only after a task has started/completed, so
            # also resolve task contents below when the task object has no
            # folder_created_id.
            task_candidates: list[dict[str, Any]] = []
            try:
                tasks_payload = seedr_data(await seedr_request("/tasks"))
                for raw_task in arr(tasks_payload, ("tasks", "torrents", "items")):
                    task = unwrap_seedr_task(seedr_data(raw_task))
                    if task:
                        task_candidates.append(task)
            except (HTTPException, SeedrError):
                task_candidates = []

            unique_folders: dict[str, str] = {}

            for task in task_candidates:
                tid = task_id(task)
                task_name = (
                    seedr_task_name(task)
                    or (folder_name_overrides.get(seedr_task_folder_id(task)) if seedr_task_folder_id(task) else "")
                    or f"Torrent {tid}"
                ).strip()
                folder_id = seedr_task_folder_id(task)

                # If the task itself does not expose its created folder, the
                # task contents endpoint often does. This is the same endpoint
                # already used by the completion/progress flow.
                if tid and (not folder_id or folder_id == "0"):
                    try:
                        task_files = await task_contents(tid)
                        folder_id = _seedr_file_folder_id(task_files)
                    except (HTTPException, SeedrError):
                        folder_id = ""

                if folder_id and folder_id != root:
                    unique_folders.setdefault(
                        folder_id,
                        folder_name_overrides.get(folder_id)
                        or _seedr_torrent_names.get(folder_id)
                        or task_name
                        or folder_id,
                    )

            # Preserve any folder IDs already learned from task metadata.
            for folder_id, task_name in task_folders:
                if folder_id and folder_id != root:
                    unique_folders.setdefault(
                        folder_id,
                        folder_name_overrides.get(folder_id)
                        or _seedr_torrent_names.get(folder_id)
                        or task_name
                        or folder_id,
                    )

            async def load_task_folder(folder_id: str, name: str) -> dict[str, Any] | None:
                try:
                    payload = await seedr_folder_payload(folder_id)
                except (HTTPException, SeedrError):
                    return None
                if not payload:
                    return None
                summary = direct_folder_summary(
                    folder_id,
                    "/Torrent Studio/" + name,
                    payload,
                )
                summary["torrentName"] = name
                summary["folderCount"] = len(
                    [x for x in arr(payload, ("folders", "directories")) if isinstance(x, dict)]
                )
                return summary

            if unique_folders:
                results = await asyncio.gather(
                    *(load_task_folder(folder_id, name) for folder_id, name in unique_folders.items()),
                    return_exceptions=True,
                )
                children = [result for result in results if isinstance(result, dict)]
            else:
                children = []

            # A denied root listing is a capability limitation, not an
            # authentication failure. Returning an empty but valid library
            # keeps Search/Prepare/Stream usable even for an account with no
            # folders exposed through the token.
            root_summary = {
                "id": root,
                "folderId": root,
                "name": "Torrent Studio",
                "path": "/Torrent Studio",
                "filesCount": sum(int(item.get("filesCount") or 0) for item in children),
                "totalSize": sum(int(item.get("totalSize") or 0) for item in children),
                "folderCount": len(children),
            }

        return {
            "configured": True,
            "root": root_summary,
            "folders": children,
        }

    _seedr_metadata_task = asyncio.create_task(build())
    try:
        result = await _seedr_metadata_task
        _seedr_metadata_cache = (asyncio.get_running_loop().time(), result)
        return result
    finally:
        _seedr_metadata_task = None


@app.get("/api/seedr/library")
async def seedr_library_metadata(fresh: bool = Query(False)):
    return await get_seedr_metadata_tree(force_refresh=fresh)


@app.get("/api/seedr/folders/{folder_id}/contents")
async def seedr_folder_contents(folder_id: str):
    if not current_seedr_token():
        return {"configured": False, "folderId": folder_id, "files": [], "folders": []}

    payload = await seedr_folder_payload(folder_id)
    files: list[dict[str, Any]] = []
    for raw in arr(payload, ("files", "items")):
        file = normalize_file(raw, folder_id)
        file["url"] = None
        files.append(file)

    folders: list[dict[str, Any]] = []
    for raw in arr(payload, ("folders", "directories")):
        if not isinstance(raw, dict):
            continue
        child_id = str(raw.get("id") or raw.get("folder_id") or "").strip()
        if not child_id:
            continue
        child_name = str(raw.get("name") or raw.get("title") or child_id).strip() or child_id
        folders.append({"id": child_id, "folderId": child_id, "name": child_name})

    return {"configured": True, "folderId": folder_id, "files": files, "folders": folders}


@app.get("/api/seedr/files")
async def seedr_files():
    # Backwards-compatible full file endpoint. New UI code uses
    # /api/seedr/library + /api/seedr/folders/{id}/contents instead.
    if not current_seedr_token():
        return {"configured": False, "files": []}

    root = "0"

    result = await collect_folder(root, "/Torrent Studio")
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result:
        item_id = str(item.get("id") or "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            unique.append(item)

    return {"configured": True, "files": unique}

@app.get("/api/seedr/files/{file_id}/download/url")
async def seedr_file_download_url(file_id: str):
    # Resolve a fresh direct Seedr URL only when the user explicitly requests it.
    return await download_url(file_id)

@app.get("/api/seedr/files/{file_id}/download")
async def seedr_file_download(file_id: str):
    # Redirect the browser to Seedr's temporary direct URL so large downloads
    # do not flow through the Render Free instance.
    result = await download_url(file_id)
    return RedirectResponse(result["url"], status_code=307)


@app.get("/api/seedr/files/{file_id}/download/direct")
async def seedr_file_download_direct(
    file_id: str,
    filename: str = Query(""),
):
    """Stream a Seedr file with an explicit browser download filename."""
    result = await download_url(file_id)
    filename_value = _safe_download_filename(
        filename or result.get("name") or "",
        f"seedr-file-{file_id}",
    )
    body, content_type, headers = await _stream_seedr_download(result["url"])
    ascii_name = filename_value.encode("ascii", errors="ignore").decode("ascii").strip() or f"seedr-file-{file_id}"
    headers["Content-Disposition"] = (
        'attachment; filename="' + ascii_name.replace('"', "_") + '"'
        + "; filename*=UTF-8''" + quote(filename_value, safe="")
    )
    return StreamingResponse(
        body(),
        media_type=content_type,
        headers=headers,
    )


async def seedr_folder_download_url(folder_id: str) -> str:
    folder_id = str(folder_id or "").strip()
    if not folder_id or not folder_id.isdigit():
        raise HTTPException(400, "Invalid Seedr folder id")

    # Known-good Torrent Studio implementation:
    # initialize a temporary archive and let Seedr return the signed URL.
    archive_id = str(uuid.uuid4())
    payload = seedr_data(
        await seedr_request(
            f"/download/archive/init/{quote(archive_id)}",
            "PUT",
            {"archive_arr": [{"type": "folder", "id": int(folder_id)}]},
        )
    )

    if isinstance(payload, dict):
        url = str(
            payload.get("url")
            or payload.get("download_url")
            or payload.get("downloadUrl")
            or payload.get("signed_url")
            or payload.get("signedUrl")
            or ""
        ).strip()
    elif isinstance(payload, str):
        url = payload.strip()
    else:
        url = ""

    if not url:
        raise HTTPException(502, "Seedr did not return a folder download URL")
    return url


@app.get("/api/seedr/folders/{folder_id}/download/url")
async def seedr_folder_download_url_api(folder_id: str):
    # Resolve the temporary direct Seedr folder URL only on an explicit click.
    url = await seedr_folder_download_url(folder_id)
    return {"url": url}

@app.get("/api/seedr/folders/{folder_id}/download")
async def seedr_folder_download(folder_id: str):
    # Redirect the browser to Seedr's temporary direct folder URL rather than
    # streaming the archive through Render.
    url = await seedr_folder_download_url(folder_id)
    return RedirectResponse(url, status_code=307)


@app.get("/api/seedr/folders/{folder_id}/download/direct")
async def seedr_folder_download_direct(
    folder_id: str,
    filename: str = Query(""),
):
    """Stream a Seedr folder archive with an explicit .zip filename."""
    url = await seedr_folder_download_url(folder_id)
    filename_value = _safe_download_filename(
        filename,
        f"seedr-folder-{folder_id}.zip",
    )
    if not filename_value.lower().endswith(".zip"):
        filename_value += ".zip"

    body, content_type, headers = await _stream_seedr_download(url)
    ascii_name = filename_value.encode("ascii", errors="ignore").decode("ascii").strip() or f"seedr-folder-{folder_id}.zip"
    headers["Content-Disposition"] = (
        'attachment; filename="' + ascii_name.replace('"', "_") + '"'
        + "; filename*=UTF-8''" + quote(filename_value, safe="")
    )
    return StreamingResponse(
        body(),
        media_type=content_type or "application/zip",
        headers=headers,
    )

# HLS stream sources are kept server-side. The browser receives a same-origin
# manifest URL so the Seedr access token never needs to be exposed to the client.
SEEDR_HLS_CACHE_SECONDS = 600
_seedr_hls_sources: dict[str, tuple[float, str, set[str]]] = {}


def _seedr_media_url(file_id: str, media_type: str) -> str:
    if media_type == "video":
        endpoint = f"/media/hls/{quote(file_id)}"
    elif media_type == "audio":
        endpoint = f"/media/mp3/{quote(file_id)}"
    else:
        raise HTTPException(400, "Unsupported Seedr media type")
    return SEEDR_MEDIA_BASE.rstrip("/") + endpoint + "?access_token=" + quote(current_seedr_token(), safe="")

def seedr_v2_bearer_token() -> str:
    """Accept a raw Seedr PAT or MediaFusion-style base64 JSON token."""
    raw = current_seedr_token().strip()
    if not raw:
        return ""
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        payload = json.loads(decoded)
        token = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
        if token:
            return token
    except Exception:
        pass
    return raw


async def seedr_v2_request(path: str) -> Any:
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")
    bearer = seedr_v2_bearer_token()
    if not bearer:
        raise HTTPException(503, "Seedr access token is empty")
    url = SEEDR_V2_BASE.rstrip("/") + "/" + str(path).lstrip("/")
    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        )
    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw
    if response.status_code >= 400:
        raise HTTPException(
            response.status_code,
            seedr_error_message(response.status_code, data, raw),
        )
    return data

async def seedr_v2_video_url(file_id: str, audio_index: int | None = None) -> str:
    """Use Seedr V2 current presentation URL, with direct-download fallback."""
    if not file_id:
        return ""
    try:
        presentation_path = f"/presentations/file/{quote(file_id)}/video"
        if audio_index is not None and audio_index >= 0:
            presentation_path += "?" + urlencode({"audio": str(audio_index)})
        payload = seedr_data(await seedr_v2_request(presentation_path))
        if isinstance(payload, dict):
            link = payload.get("link")
            link_url = link.get("url") if isinstance(link, dict) else ""
            url = str(payload.get("url") or payload.get("stream_url") or link_url or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    except HTTPException:
        pass

    try:
        payload = seedr_data(await seedr_v2_request(f"/download/file/{quote(file_id)}/url"))
        if isinstance(payload, dict):
            url = str(payload.get("url") or payload.get("download_url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    except HTTPException:
        pass

    return ""


def _absolute_hls_uri(base_url: str, uri: str) -> str:
    absolute = urljoin(base_url, uri)
    # Some HLS manifests use one access query string on the playlist URL and
    # omit it from relative segment/variant URLs. Carry it forward.
    if not urlsplit(uri).query:
        base_query = urlsplit(base_url).query
        if base_query and not urlsplit(absolute).query:
            absolute += "?" + base_query
    return absolute


def _encode_hls_target(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_hls_target(value: str) -> str:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except Exception as exc:
        raise HTTPException(400, "Invalid HLS resource token") from exc


def _rewrite_hls_manifest(file_id: str, manifest_text: str, base_url: str) -> str:
    lines = manifest_text.splitlines()
    rewritten: list[str] = []

    def proxy_url(absolute: str) -> str:
        return "/api/seedr/hls/" + quote(file_id, safe="") + "/resource?u=" + _encode_hls_target(absolute)

    for line in lines:
        current = line
        def replace_uri(match: re.Match[str]) -> str:
            uri = match.group(1)
            return 'URI="' + proxy_url(_absolute_hls_uri(base_url, uri)) + '"'
        current = re.sub(r'URI="([^"]+)"', replace_uri, current)

        stripped = current.strip()
        if stripped and not stripped.startswith("#"):
            current = proxy_url(_absolute_hls_uri(base_url, stripped))
        rewritten.append(current)

    return "\n".join(rewritten) + ("\n" if manifest_text.endswith("\n") else "")


async def _presentation_is_hls(url: str) -> bool:
    """Probe a Seedr presentation URL without downloading the full media file."""
    if not url:
        return False
    try:
        async with _InstrumentedAsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(url, headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,video/*,*/*", "Range": "bytes=0-2047"})
        content_type = str(response.headers.get("content-type") or "").lower()
        prefix = response.content[:2048].decode("utf-8", errors="ignore")
        return "mpegurl" in content_type or "#EXTM3U" in prefix
    except Exception as exc:
        logger.info("Seedr presentation probe failed: %s", exc)
        return False


async def _fetch_seedr_hls_manifest(file_id: str) -> tuple[str, str]:
    """Resolve a browser-playable HLS manifest from Seedr V2, then v1 fallback."""
    candidates: list[str] = []
    presentation_url = await seedr_v2_video_url(file_id)
    if presentation_url:
        candidates.append(presentation_url)
    # Seedr's legacy HLS endpoint explicitly requests a converted browser stream.
    candidates.append(_seedr_media_url(file_id, "video"))

    last_detail = "Seedr did not return an HLS manifest"
    for upstream_url in dict.fromkeys(candidates):
        try:
            async with _InstrumentedAsyncClient(timeout=35, follow_redirects=True) as client:
                response = await client.get(
                    upstream_url,
                    headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*"},
                )
            if response.status_code >= 400:
                last_detail = response.text[:500] or f"Seedr returned HTTP {response.status_code}"
                continue

            content_type = str(response.headers.get("content-type") or "").lower()
            text = response.text
            if "#EXTM3U" not in text[:200]:
                last_detail = f"Seedr returned {content_type or 'non-HLS content'}"
                continue

            final_url = str(response.url)
            host = urlsplit(final_url).hostname or ""
            if not host:
                last_detail = "Seedr returned an invalid HLS URL"
                continue

            allowed_hosts = {host.lower()}
            for raw_uri in re.findall(r'(?:URI="([^"]+)"|^([^#\s][^\r\n]*))', text, flags=re.M):
                uri = raw_uri[0] or raw_uri[1]
                if uri:
                    try:
                        ref_host = urlsplit(_absolute_hls_uri(final_url, uri)).hostname
                        if ref_host:
                            allowed_hosts.add(ref_host.lower())
                    except Exception:
                        pass

            _seedr_hls_sources[file_id] = (
                asyncio.get_running_loop().time() + SEEDR_HLS_CACHE_SECONDS,
                final_url,
                allowed_hosts,
            )
            logger.info("Seedr HLS manifest resolved: file=%s source=%s", file_id, urlsplit(final_url).hostname)
            return text, final_url
        except Exception as exc:
            last_detail = str(exc)
            continue

    raise HTTPException(502, last_detail)

async def _get_hls_source(file_id: str) -> tuple[str, set[str]]:
    now = asyncio.get_running_loop().time()
    cached = _seedr_hls_sources.get(file_id)
    if cached and cached[0] > now:
        return cached[1], cached[2]

    _manifest, final_url = await _fetch_seedr_hls_manifest(file_id)
    cached = _seedr_hls_sources[file_id]
    return final_url, cached[2]


async def resolve_seedr_stream_id(file_id: str, name: str = "") -> str:
    """Resolve the Seedr playback file identifier without requiring HLS."""
    candidate = str(file_id or "").strip()
    if candidate:
        try:
            presentation = await seedr_v2_video_url(candidate)
            if presentation:
                return candidate
        except HTTPException:
            pass
        try:
            await _fetch_seedr_hls_manifest(candidate)
            return candidate
        except HTTPException:
            pass

    if not name:
        raise HTTPException(404, "Seedr playback file could not be resolved")

    try:
        result = seedr_data(await seedr_request(f"/search/fs?query={quote(name)}"))
    except HTTPException as exc:
        raise HTTPException(exc.status_code, "Seedr file lookup failed") from exc

    candidates = arr(result, ("files", "items"))
    wanted_name = Path(name).name.lower()
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        raw_name = str(raw.get("name") or raw.get("title") or "").strip()
        if raw_name.lower() != wanted_name:
            continue

        # Match MediaFusion: Seedr V2 playback uses the actual file id.
        resolved = str(
            raw.get("id")
            or raw.get("file_id")
            or raw.get("folder_file_id")
            or raw.get("folderFileId")
            or ""
        ).strip()
        if not resolved:
            continue

        try:
            await _fetch_seedr_hls_manifest(resolved)
            logger.info("Seedr playback id resolved by filename: %s -> %s", name, resolved)
            return resolved
        except HTTPException:
            continue

    raise HTTPException(404, f"No playable Seedr stream found for {name}")


@app.get("/api/seedr/files/stream")
async def seedr_file_stream(
    file_id: str = Query(""),
    type: str = Query("video"),
    name: str = Query(""),
):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    resolved_id = await resolve_seedr_stream_id(file_id, name)

    if type == "video":
        # Restore the tested direct-browser-stream strategy from
        # fix-seedr-direct-browser-stream:
        #   1) ask Seedr V2 for its presentation URL;
        #   2) if it is a browser-playable direct presentation, proxy it with
        #      Range support;
        #   3) otherwise use the existing HLS proxy.
        presentation_url = await seedr_v2_video_url(resolved_id)
        if presentation_url and not await _presentation_is_hls(presentation_url):
            return {
                "url": "/api/seedr/media/video/" + quote(resolved_id, safe=""),
                "externalUrl": presentation_url,
                "name": name or resolved_id,
                "resolvedFileId": resolved_id,
                "protocol": "direct",
            }

        try:
            await _fetch_seedr_hls_manifest(resolved_id)
            return {
                "url": "/api/seedr/hls/" + quote(resolved_id, safe=""),
                "externalUrl": presentation_url or _seedr_media_url(resolved_id, "video"),
                "name": name or resolved_id,
                "resolvedFileId": resolved_id,
                "protocol": "hls",
            }
        except HTTPException:
            if presentation_url:
                return {
                    "url": "/api/seedr/media/video/" + quote(resolved_id, safe=""),
                    "externalUrl": presentation_url,
                    "name": name or resolved_id,
                    "resolvedFileId": resolved_id,
                    "protocol": "direct",
                }
            raise

    return {
        "url": "/api/seedr/media/audio/" + quote(resolved_id, safe=""),
        "externalUrl": _seedr_media_url(resolved_id, "audio"),
        "name": name or resolved_id,
        "resolvedFileId": resolved_id,
        "protocol": "mp3",
    }

@app.get("/api/seedr/hls/{file_id}")
async def seedr_hls_manifest(file_id: str):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    manifest, final_url = await _fetch_seedr_hls_manifest(file_id)
    cached = _seedr_hls_sources.get(file_id)
    allowed_hosts = cached[2] if cached else {urlsplit(final_url).hostname.lower()}
    rewritten = _rewrite_hls_manifest(file_id, manifest, final_url)

    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/seedr/hls/{file_id}/resource")
async def seedr_hls_resource(request: Request, file_id: str, u: str = Query(...)):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    target = _decode_hls_target(u)
    parsed = urlsplit(target)
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(400, "Invalid HLS target URL")

    try:
        base_url, allowed_hosts = await _get_hls_source(file_id)
    except HTTPException:
        raise

    if host not in allowed_hosts:
        raise HTTPException(403, "HLS resource host is not allowed")

    headers: dict[str, str] = {"Accept": "*/*"}
    if request is not None:
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header

    async with _InstrumentedAsyncClient(timeout=35, follow_redirects=True) as client:
        response = await client.get(target, headers=headers)

    if response.status_code >= 400:
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/octet-stream"),
            headers={"Access-Control-Allow-Origin": "*"},
        )

    content_type = str(response.headers.get("content-type") or "").lower()
    body = response.content
    _record_media_bytes(file_id, len(body))

    if "mpegurl" in content_type or "#EXTM3U" in body[:200].decode("utf-8", errors="ignore"):
        text = body.decode("utf-8", errors="replace")
        final_url = str(response.url)
        final_host = urlsplit(final_url).hostname
        if final_host:
            allowed_hosts.add(final_host.lower())
        _seedr_hls_sources[file_id] = (
            asyncio.get_running_loop().time() + SEEDR_HLS_CACHE_SECONDS,
            final_url,
            allowed_hosts,
        )
        body = _rewrite_hls_manifest(file_id, text, final_url).encode("utf-8")
        content_type = "application/vnd.apple.mpegurl"

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
    }
    for header in ("content-range", "content-length", "etag", "last-modified"):
        if response.headers.get(header):
            response_headers[header.title()] = response.headers[header]

    return Response(
        content=body,
        status_code=response.status_code,
        media_type=content_type or "application/octet-stream",
        headers=response_headers,
    )


async def _seedr_media_source_url(file_id: str) -> str:
    result = await download_url(file_id)
    url = str(result.get("url") or "").strip()
    if not url:
        raise HTTPException(502, "Seedr returned no media URL")
    return url


_SEEDR_FFPROBE_CACHE_SECONDS = 600
_seedr_ffprobe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _ffprobe_seedr_file(file_id: str) -> dict[str, Any]:
    """Inspect a Seedr file and reuse the result for media/subtitle requests.

    The same MKV was previously probed once for track discovery and then probed
    again when the user selected an embedded subtitle. Reusing the probe keeps
    Render Free from doing the expensive remote MKV inspection twice.
    """
    now = asyncio.get_running_loop().time()
    cached = _seedr_ffprobe_cache.get(file_id)
    if cached and cached[0] > now:
        return cached[1]

    source_url = await _seedr_media_source_url(file_id)
    command = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        "-analyzeduration", "10M",
        "-probesize", "20M",
        source_url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        raise HTTPException(504, "Media track inspection timed out") from exc
    except FileNotFoundError as exc:
        raise HTTPException(503, "FFmpeg is not installed on the media server") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise HTTPException(502, detail or "FFprobe could not inspect the Seedr file")

    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise HTTPException(502, "FFprobe returned invalid metadata") from exc

    _seedr_ffprobe_cache[file_id] = (now + _SEEDR_FFPROBE_CACHE_SECONDS, data)
    return data


_SEEDR_MEDIA_INFO_CACHE_SECONDS = 600
_seedr_media_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_SEEDR_EMBEDDED_SUBTITLE_CACHE_SECONDS = 600
_seedr_embedded_subtitle_cache: dict[str, tuple[float, bytes]] = {}


def _stream_tags(stream: dict[str, Any]) -> dict[str, Any]:
    raw = stream.get("tags")
    if not isinstance(raw, dict):
        return {}
    # FFprobe normally emits lowercase Matroska tag keys, but some containers
    # expose alternate casing/names. Normalize them before building labels.
    return {str(key).strip().lower(): value for key, value in raw.items()}


def _track_language(stream: dict[str, Any]) -> str:
    tags = _stream_tags(stream)
    for key in ("language", "lang", "language-eng", "language_ietf"):
        value = str(tags.get(key) or "").strip().lower()
        if value:
            return value.split("-")[0]
    return ""


def _track_title(stream: dict[str, Any], fallback: str) -> str:
    tags = _stream_tags(stream)
    for key in ("title", "handler_name", "name", "track_name"):
        value = str(tags.get(key) or "").strip()
        if value:
            return value
    return fallback


@app.get("/api/seedr/media-info/{file_id}")
async def seedr_media_info(file_id: str):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    cached = _seedr_media_info_cache.get(file_id)
    now = asyncio.get_running_loop().time()
    if cached and cached[0] > now:
        return cached[1]

    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    if not isinstance(streams, list):
        streams = []

    language_names = {
        "en": "English", "eng": "English",
        "hi": "Hindi", "hin": "Hindi",
        "fr": "French", "fra": "French",
        "de": "German", "deu": "German",
        "es": "Spanish", "spa": "Spanish",
        "it": "Italian", "ita": "Italian",
        "pt": "Portuguese", "por": "Portuguese",
        "ru": "Russian", "rus": "Russian",
        "ja": "Japanese", "jpn": "Japanese",
        "ko": "Korean", "kor": "Korean",
        "zh": "Chinese", "zho": "Chinese",
        "ar": "Arabic", "ara": "Arabic",
        "bn": "Bengali", "ben": "Bengali",
    }

    audio_tracks = []
    subtitle_tracks = []
    audio_index = 0
    subtitle_index = 0

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "").lower()
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        lang = _track_language(stream)
        title = _track_title(stream, "")
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        if codec_type == "audio":
            language_label = language_names.get(lang, lang.upper() if lang else "")
            # Keep the raw mix title separate from the language. The frontend
            # combines them into a clearer label such as "English (BD 5.1)".
            label = title or language_label or f"Audio {audio_index + 1}"
            audio_tracks.append({
                "index": audio_index,
                "streamIndex": int(stream.get("index") or 0),
                "language": lang,
                "title": label,
                "codec": str(stream.get("codec_name") or "").upper(),
                "channels": int(stream.get("channels") or 0),
                "default": bool(disposition.get("default")),
            })
            audio_index += 1
        elif codec_type == "subtitle":
            codec = str(stream.get("codec_name") or "").lower()
            # Bitmap subtitle codecs cannot be represented as browser WebVTT.
            if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}:
                continue
            language_label = language_names.get(lang, lang.upper() if lang else "")
            forced = bool(disposition.get("forced"))
            default = bool(disposition.get("default"))
            codec_label = {
                "subrip": "SRT",
                "ass": "ASS",
                "ssa": "SSA",
                "webvtt": "WebVTT",
                "mov_text": "Text",
                "text": "Text",
            }.get(codec, codec.upper() or "Text")
            # Preserve the real Matroska title/language whenever available.
            # Some MKVs contain no subtitle language/title tags at all. In that
            # case the old fallback produced identical entries such as
            # "SRT", making it impossible to tell tracks apart. Give unnamed
            # tracks a stable ordinal while keeping the real language when one
            # is available.
            if title:
                label = title
            elif language_label:
                label = f"{language_label} {subtitle_index + 1}"
            else:
                label = f"{codec_label} {subtitle_index + 1}"

            if forced and "forced" not in label.lower():
                label += " · Forced"
            elif default and "default" not in label.lower():
                label += " · Default"
            stream_index = int(stream.get("index") or 0)
            subtitle_tracks.append({
                "index": subtitle_index,
                "streamIndex": stream_index,
                "language": lang,
                "title": label,
                "codec": str(stream.get("codec_name") or "").upper(),
                "url": f"/api/seedr/media-info/{quote(file_id, safe='')}/subtitle?stream={stream_index}&name={quote(label, safe='')}",
            })
            subtitle_index += 1

    result = {
        "name": str((data.get("format") or {}).get("filename") or file_id) if isinstance(data, dict) else file_id,
        "audioTracks": audio_tracks,
        "subtitleTracks": subtitle_tracks,
    }
    _seedr_media_info_cache[file_id] = (now + _SEEDR_MEDIA_INFO_CACHE_SECONDS, result)
    return result


@app.get("/api/seedr/media-info/{file_id}/subtitle")
async def seedr_embedded_subtitle(
    file_id: str,
    track: int | None = Query(None, ge=0),
    stream: int | None = Query(None, ge=0),
    name: str = Query("subtitle"),
):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    cache_key = f"{file_id}:{track}"
    now = asyncio.get_running_loop().time()
    cached = _seedr_embedded_subtitle_cache.get(cache_key)
    if cached and cached[0] > now:
        return Response(
            content=cached[1],
            media_type="text/vtt; charset=utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "private, max-age=600",
                "Content-Disposition": f'inline; filename="{Path(name).name.replace(chr(34), "_")}.vtt"',
            },
        )

    # Reuses the media-info FFprobe result instead of probing the remote MKV
    # a second time when the user selects an embedded subtitle.
    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    subtitle_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "subtitle"
        and str(stream.get("codec_name") or "").lower() not in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
    ]
    if stream is not None:
        selected = next(
            (
                item for item in subtitle_streams
                if int(item.get("index") or -1) == stream
            ),
            None,
        )
        if selected is None:
            raise HTTPException(404, "Subtitle stream not found")
        stream_index = int(selected.get("index") or 0)
        cache_key = f"{file_id}:stream:{stream_index}"
    else:
        if track is None or track >= len(subtitle_streams):
            raise HTTPException(404, "Subtitle track not found")
        stream_index = int(subtitle_streams[track].get("index") or 0)
        cache_key = f"{file_id}:{track}"

    source_url = await _seedr_media_source_url(file_id)
    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", source_url,
        "-map", f"0:{stream_index}",
        "-c:s", "webvtt",
        "-f", "webvtt",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        raise HTTPException(504, "Subtitle extraction timed out") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise HTTPException(502, detail or "Subtitle extraction failed")

    _seedr_embedded_subtitle_cache[cache_key] = (
        now + _SEEDR_EMBEDDED_SUBTITLE_CACHE_SECONDS,
        stdout,
    )

    return Response(
        content=stdout,
        media_type="text/vtt; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "private, max-age=600",
            "Content-Disposition": f'inline; filename="{Path(name).name.replace(chr(34), "_")}.vtt"',
        },
    )


async def _stream_selected_audio(file_id: str, audio_index: int, start: float = 0.0) -> StreamingResponse:
    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "audio"
    ]
    if audio_index >= len(audio_streams):
        raise HTTPException(404, "Audio track not found")

    source_url = await _seedr_media_source_url(file_id)
    stream = audio_streams[audio_index]
    video_streams = [
        item for item in streams
        if isinstance(item, dict) and str(item.get("codec_type") or "").lower() == "video"
    ]
    video_codec = str((video_streams[0] if video_streams else {}).get("codec_name") or "").lower()

    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        *([ "-ss", str(max(0.0, start)) ] if start > 0 else []),
        "-i", source_url,
        "-map", "0:v:0",
        "-map", f"0:a:{audio_index}",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    if video_codec in {"h264", "avc1"}:
        command[command.index("-c:a"):command.index("-c:a")] = ["-c:v", "copy"]
    else:
        command[command.index("-c:a"):command.index("-c:a")] = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
        ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def body():
        try:
            while True:
                chunk = await process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass

    return StreamingResponse(
        body(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/api/seedr/media/video/{file_id}/stats")
async def seedr_video_media_stats(file_id: str):
    return {
        "bytesPerSecond": round(_media_download_speed(file_id), 2),
        "windowSeconds": _MEDIA_STATS_WINDOW_SECONDS,
    }

@app.get("/api/seedr/media/video/{file_id}")
async def seedr_video_media(
    file_id: str,
    request: Request,
    audio: int | None = Query(None, ge=0),
    start: float = Query(0.0, ge=0.0),
):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    if audio is not None:
        return await _stream_selected_audio(file_id, audio, start=start)

    upstream_url = await seedr_v2_video_url(file_id)
    if not upstream_url:
        raise HTTPException(404, "Seedr returned no video presentation URL")

    headers = {"Accept": "video/*,application/octet-stream,*/*"}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = _InstrumentedAsyncClient(timeout=60, follow_redirects=True)
    try:
        response = await client.send(
            client.build_request("GET", upstream_url, headers=headers),
            stream=True,
        )
    except Exception:
        await client.aclose()
        raise

    if response.status_code >= 400:
        body = await response.aread()
        status = response.status_code
        content_type = response.headers.get("content-type", "application/octet-stream")
        await response.aclose()
        await client.aclose()
        return Response(
            content=body,
            status_code=status,
            media_type=content_type,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "no-store",
    }
    for header in ("content-range", "content-length", "etag", "last-modified"):
        if response.headers.get(header):
            response_headers[header.title()] = response.headers[header]

    async def body_stream():
        try:
            async for chunk in response.aiter_bytes():
                _record_media_bytes(file_id, len(chunk))
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )


@app.get("/api/seedr/media/audio/{file_id}")
async def seedr_audio_media_route(
    file_id: str,
    request: Request,
    track: int = Query(0, ge=0),
    start: float = Query(0.0, ge=0.0),
):
    """Stream one Seedr audio track as browser-compatible fragmented MP4.

    This route is used for alternate-audio playback so the video element never
    has to be replaced. Keeping video on its original timeline avoids the
    shortened-duration and A/V drift caused by remuxing the whole video when a
    user changes audio tracks.
    """
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    data = await _ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "audio"
    ]
    if track >= len(audio_streams):
        raise HTTPException(404, "Audio track not found")

    source_url = await _seedr_media_source_url(file_id)
    source = audio_streams[track]
    codec = str(source.get("codec_name") or "").lower()

    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        *(["-ss", str(max(0.0, start))] if start > 0 else []),
        "-i", source_url,
        "-map", f"0:a:{track}",
    ]

    # Emit a plain MP3 stream for the hidden browser audio element.
    # MP3 is broadly supported by Chrome/Edge/Android and does not require the
    # fragmented MP4/WebM demuxers that can stall on a non-seekable pipe.
    command += [
        "-vn",
        "-sn",
        "-dn",
        "-ac", "2",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-f", "mp3",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, "FFmpeg is not installed on the media server") from exc

    async def body():
        try:
            while True:
                chunk = await process.stdout.read(512 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass

    return StreamingResponse(
        body(),
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )

@app.get("/api/seedr/files/{file_id}/subtitle")
async def seedr_file_subtitle(
    file_id: str,
    filename: str = Query(""),
):
    """Return a Seedr sidecar subtitle as browser-compatible WebVTT."""
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")

    requested_name = Path(filename or "").name
    extension = requested_name.rsplit(".", 1)[-1].lower() if "." in requested_name else ""
    if extension not in {"srt", "vtt"}:
        raise HTTPException(400, "Only SRT and VTT subtitles are supported")

    result = await download_url(file_id)
    upstream_url = str(result.get("url") or "").strip()
    if not upstream_url:
        raise HTTPException(502, "Seedr returned no subtitle URL")

    async with _InstrumentedAsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(upstream_url)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, "Seedr subtitle download failed")

    text = response.content.decode("utf-8-sig", errors="replace")
    if extension == "srt":
        # Convert the common SRT timestamp format to WebVTT.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(
            r"(\d{2}:\d{2}:\d{2}),(\d{3})",
            r"\1.\2",
            text,
        )
        text = "WEBVTT\n\n" + text.lstrip()
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text.lstrip()

    return Response(
        content=text,
        media_type="text/vtt; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
            "Content-Disposition": "inline; filename=\"" + requested_name.replace('"', '_') + "\"",
        },
    )

async def _cancel_seedr_task_and_partial_folder(
    tid: str,
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stop an unfinished Seedr task and remove its partial folder, never a completed download."""
    global _seedr_metadata_cache

    task_id_value = str(tid or "").strip()
    if not task_id_value:
        raise HTTPException(400, "A Seedr task id is required")

    task_found = isinstance(task, dict) and bool(task)
    task_value = task if isinstance(task, dict) else {}
    if not task_found:
        try:
            raw = seedr_data(await seedr_request(f"/tasks/{quote(task_id_value, safe='')}"))
            task_value = unwrap_seedr_task(raw)
            task_found = bool(task_value)
        except (HTTPException, SeedrError) as exc:
            if getattr(exc, "status_code", 0) != 404:
                raise
            task_value = {}

    complete = task_complete(task_value) if task_found else False
    active = task_found and not complete
    task_name = seedr_task_name(task_value) or f"Torrent {task_id_value}"
    folder_id = seedr_task_folder_id(task_value) if task_found else ""
    task_files: list[dict[str, Any]] = []

    # Some Seedr API variants expose the created folder only in task contents.
    if active and not folder_id:
        try:
            task_files = await task_contents(task_id_value)
            folder_id = (
                _seedr_effective_task_folder_id(task_value, task_files)
                or _seedr_file_folder_id(task_files)
            )
        except (HTTPException, SeedrError):
            task_files = []

    folder_size = 0
    for file in task_files:
        if not isinstance(file, dict):
            continue
        try:
            folder_size += max(0, int(float(file.get("size") or 0)))
        except (TypeError, ValueError):
            continue
    task_deleted = False
    if task_found:
        try:
            await seedr_request(f"/tasks/{quote(task_id_value, safe='')}", "DELETE")
            task_deleted = True
        except (HTTPException, SeedrError) as exc:
            status_code = getattr(exc, "status_code", 0)
            if status_code == 405:
                try:
                    await seedr_request(f"/tasks/{quote(task_id_value, safe='')}/delete", "POST")
                    task_deleted = True
                except (HTTPException, SeedrError) as fallback_exc:
                    if getattr(fallback_exc, "status_code", 0) != 404:
                        raise
            elif status_code != 404:
                raise

    folder_deleted = False
    if active and folder_id and folder_id != "0":
        # Seedr can remove the task before its partial folder is ready to be
        # deleted. Retry the folder DELETE with backoff instead of returning an
        # error that forces the user to click Prepare a second time.
        max_delete_attempts = 5
        last_delete_error: Exception | None = None
        for attempt in range(max_delete_attempts):
            try:
                await seedr_request(f"/fs/folder/{quote(folder_id, safe='')}", "DELETE")
                folder_deleted = True
                last_delete_error = None
                break
            except (HTTPException, SeedrError) as exc:
                status_code = getattr(exc, "status_code", 0)
                if status_code == 404:
                    # A previous attempt or Seedr's own task deletion already
                    # removed the transient folder.
                    folder_deleted = True
                    last_delete_error = None
                    break

                last_delete_error = exc
                if attempt + 1 < max_delete_attempts:
                    delay = min(0.75 * (2 ** attempt), 4.0)
                    logger.warning(
                        "Seedr partial-folder delete retry task=%s folder=%s attempt=%s/%s delay_seconds=%.2f status=%s detail=%s",
                        task_id_value,
                        folder_id,
                        attempt + 1,
                        max_delete_attempts,
                        delay,
                        status_code,
                        getattr(exc, "detail", str(exc)),
                    )
                    await asyncio.sleep(delay)

        if not folder_deleted:
            # Do not abort the new Prepare solely because the partial-folder
            # deletion endpoint briefly failed. The old task has already been
            # cancelled; continue to quota cleanup and the requested add, which
            # will trigger the existing storage-recovery retry if necessary.
            logger.error(
                "Seedr partial-folder deletion did not confirm success after retries; continuing with new add task=%s folder=%s status=%s detail=%s",
                task_id_value,
                folder_id,
                getattr(last_delete_error, "status_code", 0),
                getattr(last_delete_error, "detail", str(last_delete_error)),
            )

    _seedr_cleanup_jobs.pop(task_id_value, None)
    _seedr_torrent_names_by_task.pop(task_id_value, None)
    if folder_id:
        _seedr_torrent_names.pop(folder_id, None)
        _seedr_folder_cache.pop(folder_id, None)
    _save_seedr_cleanup_jobs()
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()

    return {
        "taskId": task_id_value,
        "name": task_name,
        "found": task_found,
        "active": active,
        "completed": complete,
        "taskDeleted": task_deleted,
        "folderId": folder_id,
        "folderDeleted": folder_deleted,
        "size": folder_size,
    }


async def _cancel_active_seedr_tasks_for_replacement(
    replace_task_id: str = "",
) -> list[dict[str, Any]]:
    """Cancel all unfinished tasks in the connected Seedr account before a new torrent is added."""
    explicit_id = str(replace_task_id or "").strip()
    tracked_ids = {str(task_id_value).strip() for task_id_value in _seedr_cleanup_jobs}
    tracked_ids.discard("")

    try:
        payload = seedr_data(await seedr_request("/tasks"))
    except (HTTPException, SeedrError) as exc:
        logger.warning("Could not list Seedr tasks before replacement: %s", getattr(exc, "detail", str(exc)))
        raise SeedrError(
            "SEEDR_TASK_LIST_FAILED",
            503,
            "Could not check the current Seedr download before replacing it. Please retry.",
        ) from exc

    cancelled: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_task in arr(payload, ("tasks", "torrents", "items")):
        task = unwrap_seedr_task(seedr_data(raw_task))
        if not task:
            continue
        task_id_value = task_id(task)
        if task_id_value:
            seen_ids.add(task_id_value)
        if not task_id_value or task_complete(task):
            continue
        if task_id_value in {str(item.get("taskId") or "") for item in cancelled}:
            continue

        result = await _cancel_seedr_task_and_partial_folder(task_id_value, task)
        if result.get("active"):
            cancelled.append(result)

    # A tracked or explicitly selected task may be temporarily absent from the
    # list endpoint. Check those IDs directly so stale Seedr tasks do not survive
    # simply because the backend restarted or /tasks omitted a row.
    fallback_ids = tracked_ids | ({explicit_id} if explicit_id else set())
    for task_id_value in sorted(fallback_ids - seen_ids):
        try:
            result = await _cancel_seedr_task_and_partial_folder(task_id_value)
        except (HTTPException, SeedrError) as exc:
            logger.warning(
                "Seedr replacement could not cancel stale task=%s: %s",
                task_id_value,
                getattr(exc, "detail", str(exc)),
            )
            raise
        if result.get("active"):
            cancelled.append(result)

    if cancelled:
        logger.info(
            "Seedr replacement cancelled %s unfinished task(s): %s",
            len(cancelled),
            ",".join(str(item.get("taskId") or "") for item in cancelled),
        )
    else:
        logger.info("Seedr replacement found no unfinished Seedr tasks")
    return cancelled


@app.delete("/api/seedr/tasks/{tid}")
async def seedr_task_delete(tid: str):
    if not current_seedr_token():
        raise HTTPException(503, "Seedr is not configured")
    return await _cancel_seedr_task_and_partial_folder(tid)

@app.delete("/api/seedr/files/{file_id}")
async def seedr_file_delete(file_id: str):
    global _seedr_metadata_cache
    result = await seedr_request(f"/fs/file/{quote(file_id)}", "DELETE")
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()
    return result

@app.delete("/api/seedr/folders/{folder_id}")
async def seedr_folder_delete(folder_id: str):
    global _seedr_metadata_cache
    result = await seedr_request(f"/fs/folder/{quote(folder_id)}", "DELETE")
    _seedr_metadata_cache = None
    _seedr_folder_cache.clear()
    return result

# Compatibility endpoints for the preserved UI. They intentionally do not run
# qBittorrent or maintain local torrent storage; Seedr is the only transfer backend.
@app.get("/api/v2/torrents/info")
async def empty_torrents(filter: str | None = None):
    return []

def _libtorrent_metadata_sync(magnet: str) -> tuple[str, str, list[dict[str, Any]], int]:
    """Resolve only torrent metadata using libtorrent; never request content pieces."""
    tmp = tempfile.mkdtemp(prefix="torrent-metadata-lt-")
    ses = None
    handle = None
    try:
        ses = lt.session()

        # Explicitly enable the discovery paths needed for magnet metadata on
        # a cloud host. Keep UPnP/NAT-PMP/LSd off: they are not useful on
        # Render and can add connection delay. DHT + tracker access remain on.
        ses.apply_settings({
            "enable_dht": True,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_tcp": True,
            "enable_outgoing_utp": True,
            "enable_incoming_tcp": True,
            "enable_incoming_utp": True,
            "listen_interfaces": "0.0.0.0:0",
            "dht_bootstrap_nodes": (
                "router.bittorrent.com:6881,"
                "router.utorrent.com:6881,"
                "dht.transmissionbt.com:6881"
            ),
            "use_dht_as_fallback": False,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connection_speed": 50,
            "handshake_timeout": 10,
        })

        atp = lt.parse_magnet_uri(magnet)
        atp.save_path = tmp

        # upload_mode prevents piece requests. Disable auto-management so the
        # session cannot later take the torrent out of upload mode automatically.
        atp.flags = atp.flags | lt.torrent_flags.upload_mode
        atp.flags = atp.flags & ~lt.torrent_flags.auto_managed

        handle = ses.add_torrent(atp)

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if handle.has_metadata():
                break
            for alert in ses.pop_alerts():
                message = str(alert)
                if "error" in message.lower() or "tracker" in message.lower() or "dht" in message.lower():
                    logger.info("libtorrent alert for %s: %s", info_hash(magnet), message[:500])
            time.sleep(0.15)

        if not handle.has_metadata():
            status = handle.status()
            raise TimeoutError(
                f"libtorrent metadata timeout (state={status.state}, "
                f"peers={status.num_peers}, seeds={status.num_seeds})"
            )

        ti = handle.torrent_file()
        if ti is None:
            raise RuntimeError("libtorrent reported metadata but torrent_file() is empty")

        fs = ti.layout()
        torrent_name = str(ti.name() or "")
        files: list[dict[str, Any]] = []
        for index in range(fs.num_files()):
            path = str(fs.file_path(index))
            size = int(fs.file_size(index))
            if not path:
                continue
            files.append({
                "index": index,
                "name": path,
                "size": size,
                "path": path,
                "type": "file",
                "priority": 1,
            })

        total_size = sum(int(item.get("size") or 0) for item in files)
        return torrent_name, magnet, files, total_size
    finally:
        try:
            if ses is not None and handle is not None and handle.is_valid():
                ses.remove_torrent(handle)
        except Exception:
            pass
        try:
            if ses is not None:
                del ses
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


async def _fetch_remote_torrent_metadata(magnet: str, info_hash_value: str) -> dict[str, Any] | None:
    """Use the public torrent-metadata resolver as a metadata-only fallback."""
    if not TORRENT_METADATA_API_URL:
        return None
    try:
        # This service resolves BEP-9 metadata from a magnet without starting
        # a file download. Give Fly.io enough time to wake a sleeping instance.
        async with _InstrumentedAsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.post(
                TORRENT_METADATA_API_URL.rstrip("/") + "/",
                json={"query": magnet},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Remote metadata service failed for %s: %s", info_hash_value, exc)
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    returned_hash = str(data.get("infoHash") or "").strip().lower()
    if returned_hash and returned_hash != info_hash_value.lower():
        logger.info("Remote metadata hash mismatch: wanted %s, got %s", info_hash_value, returned_hash)
        return None

    raw_files = data.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return None

    files: list[dict[str, Any]] = []
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        if not path:
            continue
        files.append({
            "index": index,
            "name": str(item.get("name") or path),
            "size": int(float(item.get("size") or 0)),
            "path": path,
            "type": "file",
            "priority": 1,
        })
    if not files:
        return None

    result = {
        "name": str(data.get("name") or files[0]["name"]),
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item["size"]) for item in files),
        "source": "remote_torrent_metadata",
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    _metadata_cache[info_hash_value.lower()] = result
    _save_metadata_cache()
    return result


async def _lookup_knaben_by_hash(info_hash_value: str) -> dict[str, Any] | None:
    """Find and parse a Knaben cached descriptor for an exact info-hash."""
    target = info_hash_value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        return None

    payload_base = {
        "search_type": "100%",
        "query": target,
        "from": 0,
        "size": 20,
        "hide_unsafe": False,
        "hide_xxx": False,
    }

    async with _InstrumentedAsyncClient(timeout=6.0, follow_redirects=True) as client:
        for field in ("hash", None):
            payload = dict(payload_base)
            if field:
                payload["search_field"] = field
            try:
                response = await client.post(
                    KNABEN_API_URL,
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                continue

            hits = data.get("hits") if isinstance(data, dict) else None
            if not isinstance(hits, list):
                continue

            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                hit_hash = str(hit.get("hash") or "").strip().lower()
                if hit_hash != target:
                    continue

                descriptor = str(hit.get("link") or "").strip()
                if descriptor:
                    parsed = await _fetch_source_torrent_descriptor(descriptor, target)
                    if parsed:
                        return parsed

                details = str(hit.get("details") or "").strip()
                if details:
                    parsed = await _fetch_source_torrent_descriptor(details, target)
                    if parsed:
                        return parsed
    return None


@app.post("/api/v2/torrents/inspect-magnet")
async def seedr_inspect_magnet(body: dict[str, Any]):
    """Return cached metadata immediately or start a background resolver."""
    raw_magnet = str(body.get("magnet") or body.get("source") or "").strip()
    if not raw_magnet:
        raise HTTPException(400, "A magnet link is required")

    magnet = normalize_magnet(raw_magnet)
    h = info_hash(magnet)
    if not h:
        raise HTTPException(400, "A valid BTIH magnet link is required")
    h = h.lower()

    cached = _metadata_cache.get(h)
    if cached:
        return cached

    # Shared public cache is the fastest metadata path for both pasted magnets
    # and search results. It never starts Seedr or requests payload pieces.
    cached_descriptor = await _fetch_itorrents_metadata(h)
    if cached_descriptor:
        return cached_descriptor

    # Search results can carry a detail URL. When that page exposes a real
    # .torrent descriptor, use it before touching DHT/trackers.
    descriptor_url = str(body.get("descriptorUrl") or "").strip()
    source_url = str(body.get("sourceUrl") or body.get("infoUrl") or "").strip()

    # First use an actual descriptor URL returned by the indexer.
    for candidate_url in (descriptor_url, source_url):
        if candidate_url:
            descriptor_result = await _fetch_source_torrent_descriptor(candidate_url, h)
            if descriptor_result:
                return descriptor_result

    # Search results may have an exact hash cached by Knaben. For a pasted
    # magnet there is no source URL, so do this lookup in the background rather
    # than making the user wait on a second network request before the job starts.
    if descriptor_url or source_url:
        try:
            hash_result = await _lookup_knaben_by_hash(h)
        except Exception as exc:
            logger.info("Knaben hash lookup failed for %s: %s", h, exc)
            hash_result = None
        if hash_result:
            return hash_result

    job_id = h
    existing = _metadata_jobs.get(job_id)
    if existing and existing.get("status") == "ready" and existing.get("result"):
        return existing["result"]

    if not existing or existing.get("status") == "error":
        now = time.time()
        _metadata_jobs[job_id] = {
            "status": "queued",
            "jobId": job_id,
            "hash": h,
            "name": str(parse_qs(urlsplit(magnet).query).get("dn", [""])[0] or "").strip(),
            "startedAt": now,
            "updatedAt": now,
            "deadlineAt": now + TORRENT_METADATA_BACKGROUND_TTL_SECONDS,
            "rounds": 0,
            "error": None,
        }
        asyncio.create_task(_run_metadata_job(job_id, magnet, h))

    # Give a fast first attempt a short window. Slow metadata resolution
    # continues in the background and is polled by the frontend.
    for _ in range(10):
        await asyncio.sleep(0.2)
        job = _metadata_jobs.get(job_id, {})
        if job.get("status") == "ready" and job.get("result"):
            return job["result"]
        if job.get("status") == "error":
            raise HTTPException(502, str(job.get("error") or "Metadata lookup failed"))

    return JSONResponse(
        status_code=202,
        content={
            "status": "resolving",
            "jobId": job_id,
            "hash": h,
            "source": "libtorrent_metadata",
            "message": "Torrent metadata is still resolving. Seedr has not been started.",
        },
    )


@app.get("/api/v2/torrents/metadata-jobs")
async def seedr_metadata_jobs():
    now = time.time()
    jobs: list[dict[str, Any]] = []
    for job in _metadata_jobs.values():
        started = float(job.get("startedAt") or now)
        deadline = float(job.get("deadlineAt") or started)
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        files = result.get("files") if isinstance(result.get("files"), list) else []
        jobs.append({
            "jobId": str(job.get("jobId") or job.get("hash") or ""),
            "hash": str(job.get("hash") or "").lower(),
            "name": str(job.get("name") or result.get("name") or "").strip(),
            "status": str(job.get("status") or "resolving"),
            "rounds": int(job.get("rounds") or 0),
            "startedAt": started,
            "updatedAt": float(job.get("updatedAt") or started),
            "deadlineAt": deadline,
            "elapsedSeconds": max(0, int(now - started)),
            "remainingSeconds": max(0, int(deadline - now)),
            "fileCount": len(files),
            "totalSize": int(result.get("totalSize") or 0),
            "source": str(result.get("source") or "background_metadata"),
            "error": str(job.get("error") or "") or None,
        })
    jobs.sort(key=lambda item: float(item.get("startedAt") or 0), reverse=True)
    return {
        "jobs": jobs,
        "backgroundTtlSeconds": TORRENT_METADATA_BACKGROUND_TTL_SECONDS,
        "retentionSeconds": TORRENT_METADATA_JOB_RETENTION_SECONDS,
    }


@app.get("/api/v2/torrents/inspect-magnet/status")
async def seedr_inspect_magnet_status(jobId: str = Query(...)):
    job = _metadata_jobs.get(jobId.lower())
    if not job:
        cached = _metadata_cache.get(jobId.lower())
        if cached:
            return cached
        raise HTTPException(404, "Metadata job not found")

    if job.get("status") == "ready" and job.get("result"):
        return job["result"]

    if job.get("status") == "error":
        raise HTTPException(502, str(job.get("error") or "Metadata lookup failed"))

    now = time.time()
    started = float(job.get("startedAt") or now)
    deadline = float(job.get("deadlineAt") or started)
    return {
        "status": "resolving",
        "jobId": jobId,
        "hash": jobId,
        "name": str(job.get("name") or ""),
        "rounds": int(job.get("rounds") or 0),
        "startedAt": started,
        "updatedAt": float(job.get("updatedAt") or started),
        "deadlineAt": deadline,
        "elapsedSeconds": max(0, int(now - started)),
        "remainingSeconds": max(0, int(deadline - now)),
        "source": "background_metadata",
        "message": job.get("error") or "Torrent metadata is still resolving in the background. Seedr has not been started.",
    }



@app.get("/api/v2/torrents/files")
async def empty_torrent_files(hash: str):
    return []

@app.post("/api/v2/torrents/pause")
async def noop_pause(body: dict[str, Any]):
    return {}

@app.post("/api/v2/torrents/resume")
async def noop_resume(body: dict[str, Any]):
    return {}

@app.post("/api/v2/torrents/delete")
async def noop_delete(body: dict[str, Any]):
    return {}

@app.post("/api/v2/torrents/filePrio")
async def noop_prio(body: dict[str, Any]):
    return {}

@app.get("/api/files")
async def files_compat(
    folder: str = "/",
    search: str = "",
    type: str = "all",
    folder_id: str = "",
):
    # Only load files for the folder currently open in the UI. Folder metadata
    # is returned separately by /api/folders.
    target_id = folder_id.strip()
    if not target_id:
        target_id = "0" if folder in {"/", "/Torrent Studio"} else ""

    if not target_id.isdigit():
        metadata = await get_seedr_metadata_tree()
        for item in metadata.get("folders", []) if isinstance(metadata, dict) else []:
            if str(item.get("path") or "") == folder:
                target_id = str(item.get("id") or "")
                break

    if not target_id.isdigit():
        return []

    payload = await seedr_folder_payload(target_id)
    result = []
    for raw in arr(payload, ("files", "items")):
        f = normalize_file(raw, target_id)
        name = f["name"]
        if search and search.lower() not in name.lower():
            continue

        lower = name.lower()
        if type != "all":
            if type == "video" and not re.search(r"\.(mkv|mp4|m4v|webm|avi|mov|m3u8|ts)$", lower):
                continue
            if type == "audio" and not re.search(r"\.(mp3|wav|flac|aac|ogg|m4a)$", lower):
                continue
            if type == "document" and not re.search(r"\.(pdf|txt|doc|docx|xls|xlsx|ppt|pptx|csv)$", lower):
                continue
            if type == "archive" and not re.search(r"\.(zip|rar|7z|tar|gz|bz2)$", lower):
                continue

        folder_path = folder if folder not in {"", "/"} else "/Torrent Studio"
        result.append({
            "id": f["id"],
            "name": name,
            "path": folder_path.rstrip("/") + "/" + name,
            "folder": folder_path,
            "size": f["size"],
            "type": "video" if re.search(r"\.(mp4|mkv|webm|avi|mov|m4v|m3u8|ts)$", lower) else "other",
            "mimeType": "video/mp4" if re.search(r"\.mp4$", lower) else "application/octet-stream",
            "createdAt": 0,
            "isStreamable": bool(re.search(r"\.(mp4|mkv|webm|avi|mov|m3u8|ts|mp3|m4a|flac|aac|ogg)$", lower)),
            "ownerId": "seedr",
            "ownerName": "Seedr",
            "downloadUrl": f"/api/seedr/files/{f['id']}/download",
            "streamUrl": "",
        })
    return result


@app.get("/api/folders")
async def folders_compat():
    metadata = await get_seedr_metadata_tree()
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or "Folder"),
            "path": str(item.get("path") or "/"),
            "ownerId": "seedr",
            "ownerName": "Seedr",
            "isShared": False,
            "permissions": {},
            "createdAt": 0,
            "filesCount": int(item.get("filesCount") or 0),
            "totalSize": int(item.get("totalSize") or 0),
        }
        for item in metadata.get("folders", [])
    ] if isinstance(metadata, dict) else []

@app.get("/api/storage/stats")
async def storage_stats():
    q = await seedr_quota()
    metadata = await get_seedr_metadata_tree()
    root = metadata.get("root") if isinstance(metadata, dict) else {}
    used = int(q.get("usedSpace") or 0)
    total = int(q.get("maxSpace") or 0)
    pct = (used / total * 100) if total else 0
    return {
        "totalBytes": total,
        "usedBytes": used,
        "freeBytes": max(0, total-used),
        "usedPercentage": pct,
        "filesCount": int((root or {}).get("filesCount") or 0),
        "torrentsCount": 0,
        "isUnlimited": False,
        "serverCapacityLabel": "Seedr cloud storage",
        "alertLevel": "critical" if pct > 90 else "warning" if pct > 80 else "normal",
    }

@app.get("/api/users")
async def users():
    user = {"id": "seedr-user", "name": "Seedr User", "email": "", "role": "admin", "avatar": ""}
    return {"users": [user], "activeUserId": user["id"], "activeUser": user}

@app.get("/api/logs")
async def logs():
    return []

@app.post("/api/logs/clear")
async def clear_logs():
    return {}

@app.get("/api/notifications")
async def notifications():
    return []

@app.post("/api/notifications/read")
async def notifications_read():
    return {}

@app.post("/api/notifications/test")
async def notifications_test():
    return {}

@app.get("/api/cleanup/settings")
async def cleanup_settings():
    return {"autoCleanCompletedDays": 7, "autoPurgeOrphans": False, "autoCleanTempFiles": False, "storageThresholdPercent": 80}

@app.post("/api/cleanup/settings")
async def update_cleanup_settings(body: dict[str, Any]):
    return body

@app.post("/api/cleanup/run")
async def run_cleanup():
    return {"bytesFreed": 0, "filesRemoved": 0, "tempRemoved": 0, "orphansRemoved": 0}

@app.post("/api/feedback")
async def submit_feedback(body: FeedbackRequest, request: Request):
    feedback_type = str(body.type or "").strip().lower()
    if feedback_type not in {"review", "suggestion", "bug"}:
        raise HTTPException(400, "Invalid feedback type")

    message = str(body.message or "").strip()
    if len(message) < 5:
        raise HTTPException(400, "Feedback is too short")
    if len(message) > 3000:
        raise HTTPException(400, "Feedback is too long")

    name = str(body.name or "").strip()[:80]
    rating = int(body.rating) if body.rating is not None else None
    if feedback_type == "review" and (rating is None or rating < 1 or rating > 5):
        raise HTTPException(400, "A review rating from 1 to 5 is required")
    if feedback_type != "review":
        rating = None

    if not GITHUB_FEEDBACK_TOKEN:
        raise HTTPException(503, "Feedback is not configured yet")

    repo = GITHUB_FEEDBACK_REPO.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise HTTPException(500, "Feedback repository is not configured correctly")

    label = {"review": "Review", "suggestion": "Suggestion", "bug": "Bug report"}[feedback_type]
    title_prefix = {"review": "Review", "suggestion": "Suggestion", "bug": "Bug report"}[feedback_type]
    title_text = message.replace("\\n", " ").strip()
    title_text = re.sub(r"\s+", " ", title_text)[:70] or "New feedback"

    if feedback_type == "review" and rating is not None:
        stars = "⭐" * rating
        title = f"{stars} {rating}/5 Review"
    else:
        title = title_prefix

    if name:
        title += f" — {name}"

    submitted_at = datetime.now(timezone.utc).isoformat()
    forwarded_for = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()

    body_lines = [
        f"# {('⭐' * rating + ' ' + str(rating) + '/5 Review') if feedback_type == 'review' and rating is not None else label}",
        "",
    ]
    if name:
        body_lines.append(f"**👤 Name:** {name}")
    else:
        body_lines.append("**👤 Name:** Anonymous")
    if rating is not None:
        body_lines.append(f"**⭐ Rating:** {rating}/5")
    body_lines.extend([
        f"**🕒 Submitted:** {submitted_at}",
        "",
        "### 💬 Feedback",
        "",
        f"> {message.replace(chr(10), chr(10) + '> ')}",
    ])
    if forwarded_for:
        # Do not persist or expose the visitor IP in the feedback issue.
        pass

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_FEEDBACK_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "Torrent-Studio-Feedback",
    }
    try:
        async with _InstrumentedAsyncClient(timeout=12.0) as client:
            response = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers=headers,
                json={"title": title, "body": "\\n".join(body_lines)},
            )
    except httpx.HTTPError as exc:
        logger.warning("Feedback submission failed: %s", exc)
        raise HTTPException(502, "Feedback service is temporarily unavailable")

    if response.status_code != 201:
        try:
            detail = response.json().get("message")
        except Exception:
            detail = None
        logger.warning("GitHub feedback submission failed: HTTP %s %s", response.status_code, detail or "")
        raise HTTPException(502, "Feedback could not be submitted right now")

    try:
        issue = response.json()
    except Exception:
        issue = {}
    return {"submitted": True, "issueUrl": issue.get("html_url")}


@app.get("/api/qbt/settings")
async def qbt_settings():
    return {"isExternal": False, "host": "", "username": "", "connected": False, "version": "Seedr backend"}

@app.post("/api/qbt/settings")
async def update_qbt_settings(body: dict[str, Any]):
    return {"isExternal": False, "host": "", "username": "", "connected": False, "version": "Seedr backend"}

@app.post("/api/files/delete")
async def delete_file_compat(body: dict[str, Any]):
    return await seedr_file_delete(str(body.get("id") or ""))

@app.post("/api/files/rename")
async def rename_file_compat(body: dict[str, Any]):
    raise HTTPException(501, "Rename is not exposed by this Seedr API deployment")

@app.post("/api/files/move")
async def move_file_compat(body: dict[str, Any]):
    raise HTTPException(501, "Move is not exposed by this Seedr API deployment")

@app.post("/api/files/folder")
async def create_folder_compat(body: dict[str, Any]):
    raise HTTPException(501, "Folder creation is not exposed by this Seedr API deployment")

@app.post("/api/folders/share")
async def share_folder_compat(body: dict[str, Any]):
    raise HTTPException(501, "Sharing is not provided by Seedr")

@app.post("/api/users/switch")
async def switch_user(body: dict[str, Any]):
    return {"activeUser": {"id": "seedr-user", "name": "Seedr User", "email": "", "role": "admin", "avatar": ""}}

@app.post("/api/users/create")
async def create_user(body: dict[str, Any]):
    raise HTTPException(501, "Multi-user accounts are not provided by this deployment")

@app.get("/api/search/torrents/add")
async def search_add_info():
    return JSONResponse({"added": False, "reason": "Use the Seedr magnet action"}, status_code=405)

@app.post("/api/search/torrents/add")
async def search_torrent_add(body: dict[str, Any]):
    source = str(body.get("source") or "")
    magnet = source if source.lower().startswith("magnet:") else ""
    if not magnet and body.get("infoHash"):
        magnet = "magnet:?xt=urn:btih:" + str(body["infoHash"])
    if not magnet:
        return {"added": False, "reason": "no_magnet_or_info_hash"}
    result = await seedr_add(
        MagnetRequest(
            magnet=magnet,
            torrent_name=str(body.get("torrent_name") or "").strip() or None,
        )
    )
    return {"added": True, **result}

# Dedicated episode lookup for the standalone Marvel scanner. Reuse Home's
# existing multi-provider search pipeline rather than scraping 1337x directly.
@app.get("/api/search/episode")
async def search_episode_release(
    title: str = Query(..., min_length=1, max_length=180),
    season: int = Query(..., ge=1, le=99),
    episode: int = Query(..., ge=1, le=999),
):
    query = f"{title.strip()} S{season:02d}E{episode:02d}"
    try:
        candidates = await _search_1337x_uncached(query, allow_series_fallback=False)
    except Exception as exc:
        logger.warning("Episode release search failed for %r: %s", query, exc)
        raise HTTPException(status_code=502, detail="Episode release search temporarily failed")

    token = re.compile(rf"(?i)(?<![A-Z0-9])S{season:02d}E{episode:02d}(?![0-9])")
    rejected_pack = re.compile(r"(?i)\b(complete series|season pack|all seasons|complete season)\b")
    results = []
    seen = set()
    for item in candidates if isinstance(candidates, list) else []:
        name = str(item.get("title") or "").strip()
        if not name or not token.search(name) or rejected_pack.search(name):
            continue
        link = str(item.get("url") or item.get("detailUrl") or item.get("magnet") or "").strip()
        identity = link or name.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        results.append({
            "title": name,
            "url": link,
            "episode": f"S{season:02d}E{episode:02d}",
            "provider": str(item.get("indexer") or item.get("provider") or ""),
            "seeders": item.get("seeders"),
            "size": item.get("size"),
            "infoHash": item.get("infoHash") or item.get("info_hash"),
            "magnet": item.get("magnet") or item.get("magnetUrl"),
        })
        if len(results) >= 8:
            break
    return {
        "status": "found" if results else "not_found",
        "query": query,
        "results": results,
        "providers": sorted({item["provider"] for item in results if item["provider"]}),
    }
