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
from torrents.search_rules import (
    MAX_SEARCH_RESULT_SIZE_BYTES,
    MIN_SEARCH_RESULT_SIZE_BYTES,
    SEARCH_COMPOUND_ALIASES,
    constraint_matches_title as _constraint_matches_title,
    expand_compound_search_title as _expand_compound_search_title,
    media_provider_queries as _media_provider_queries,
    media_provider_query as _media_provider_query,
    media_search_parts as _media_search_parts,
    normalize_title as _normalize_title,
    release_quality_score as _release_quality_score,
    search_media_kind as _search_media_kind,
    search_quality_filter as _search_quality_filter,
    search_query_constraints as _search_query_constraints,
    search_tokens as _search_tokens,
    season_episode_match as _season_episode_match,
    title_relevance as _title_relevance,
)

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
# The bundled frontend is served from the same origin, so Lax is enough and gives
# better CSRF protection. Set SEEDR_SESSION_SAMESITE=none only if the frontend is
# hosted on a different origin (and list that origin in CORS_ORIGINS).
SEEDR_SESSION_SAMESITE = os.getenv("SEEDR_SESSION_SAMESITE", "lax").strip().lower()
if SEEDR_SESSION_SAMESITE not in {"lax", "strict", "none"}:
    SEEDR_SESSION_SAMESITE = "lax"
# Diagnostic endpoints that reveal server internals are off unless explicitly enabled.
DEBUG_ENDPOINTS_ENABLED = os.getenv("DEBUG_ENDPOINTS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

# Simple in-memory sliding-window limiter for the public feedback endpoint.
FEEDBACK_RATE_LIMIT = int(os.getenv("FEEDBACK_RATE_LIMIT", "5"))
FEEDBACK_RATE_WINDOW_SECONDS = int(os.getenv("FEEDBACK_RATE_WINDOW_SECONDS", "3600"))
FEEDBACK_GLOBAL_RATE_LIMIT = int(os.getenv("FEEDBACK_GLOBAL_RATE_LIMIT", "100"))
_feedback_hits: dict[str, deque] = {}
_feedback_global_hits: deque = deque()
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


_PREBUILT_PAGE_SIZE = 18
_PREBUILT_PAGE_MEMO: dict[str, bytes] = {}
_DATE_SORTED_EXTRA_CATALOGUES = {
    "dc-live-action",
    "dc-live-action-hindi",
    "dc-animated",
    "classics",
}


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


_load_metadata_cache()


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


LIMETORRENTS_HOSTS = tuple(
    host.strip()
    for host in os.getenv(
        "LIMETORRENTS_HOSTS",
        "www.limetorrents.fun,www.limetorrents.lol,limetorrents.fun,limetorrents.lol",
    ).split(",")
    if host.strip()
)


_POSTER_CACHE_SECONDS = 6 * 60 * 60
_POSTER_NEGATIVE_CACHE_SECONDS = 2 * 60
_CINEMETA_BASE_URL = "https://v3-cinemeta.strem.io"
_poster_cache: dict[str, tuple[float, str | None]] = {}
_poster_inflight: dict[str, asyncio.Task[str | None]] = {}


_EXCLUDED_FRANCHISE_TITLE_KEYS = {
    "avengersconfidentialblackwidowpunisher",
    "ironmanrisetechnovore",
    "nextavengersheroesoftomorrow",
}


_BUNDLED_POSTERS: dict[str, str] | None = None


# Return a lightweight redirect to the image CDN instead of proxying image bytes
# through the Render instance. The latter can time out on CDN connections and caused
# every poster to remain blank. Keep size selection here so w185 can load first.


@app.on_event("startup")
async def start_seedr_cleanup_worker():
    global _seedr_cleanup_worker_task
    _load_seedr_cleanup_jobs()
    if _seedr_cleanup_worker_task is None or _seedr_cleanup_worker_task.done():
        _seedr_cleanup_worker_task = asyncio.create_task(_seedr_cleanup_worker())


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


_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE: dict[str, Any] = {
    "status": "idle", "filesCompleted": 0, "filesTotal": 0,
    "titlesCompleted": 0, "titlesTotal": 0, "updatedRows": 0, "error": "",
}
_catalogue_synopsis_enrichment_task: asyncio.Task | None = None
_CATALOGUE_SYNOPSIS_RECHECK_SECONDS = 30 * 24 * 60 * 60


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


async def _start_tmdb_catalogue_synopsis_enrichment() -> None:
    # Registered after cache initialization; launch the backfill without holding
    # application startup open while hundreds of TMDB details are fetched.
    _schedule_tmdb_catalogue_synopsis_enrichment()

# Register only after the callback above exists; earlier startup hooks are defined
# further up the module and must not reference this function prematurely.
app.router.add_event_handler("startup", _start_tmdb_catalogue_synopsis_enrichment)


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


_REGIONAL_TV_PAGE_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_REGIONAL_TV_PAGE_CACHE_TTL = 6 * 60 * 60
_REGIONAL_TV_DISCOVERY_CACHE = _REGIONAL_TV_PAGE_CACHE
REGIONAL_TV_PREBUILT_CACHE_FILE = Path(os.getenv("REGIONAL_TV_PREBUILT_CACHE_FILE", "/app/data/regional_tv_prebuilt_cache.json"))
_INDIAN_LINEAR_TV_NETWORKS = {
    "starplus", "colors", "zee tv", "zeetv", "sony entertainment television",
    "sony sab", "sab tv", "dd national", "andtv", "&tv", "star bharat",
    "sony pal", "dangal tv", "sun tv", "star utsav",
}


_classics_ott_enrichment_task: asyncio.Task | None = None


async def _warm_extra_catalogues_on_startup() -> None:
    # Catalogue builds are deliberately lazy. The old startup warm-up launched
    # all stale/missing catalogues together and flooded every upstream provider.
    asyncio.create_task(_extra_catalogue_monthly_scheduler())


app.router.add_event_handler("startup", _warm_extra_catalogues_on_startup)


async def _start_regional_movie_catalogue_scheduler() -> None:
    asyncio.create_task(_regional_movie_catalogue_weekly_scheduler())


app.router.add_event_handler("startup", _start_regional_movie_catalogue_scheduler)


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


# Small, independent TMDB lookup for the quick search cards. This intentionally
# does not wait for torrent metadata or Watchmode source resolution.
_TMDB_SEARCH_METADATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TMDB_SEARCH_METADATA_CACHE_TTL = 1800.0


REGIONAL_MOVIE_PREBUILT_CACHE_PATH = Path(os.getenv("REGIONAL_MOVIE_PREBUILT_CACHE_PATH", "/app/data/regional_movie_prebuilt_cache.json"))


# HLS stream sources are kept server-side. The browser receives a same-origin
# manifest URL so the Seedr access token never needs to be exposed to the client.
SEEDR_HLS_CACHE_SECONDS = 600
_seedr_hls_sources: dict[str, tuple[float, str, set[str]]] = {}


_SEEDR_FFPROBE_CACHE_SECONDS = 600
_seedr_ffprobe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


_SEEDR_MEDIA_INFO_CACHE_SECONDS = 600
_seedr_media_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_SEEDR_EMBEDDED_SUBTITLE_CACHE_SECONDS = 600
_seedr_embedded_subtitle_cache: dict[str, tuple[float, bytes]] = {}


# Compatibility endpoints for the preserved UI. They intentionally do not run
# qBittorrent or maintain local torrent storage; Seedr is the only transfer backend.


# Dedicated episode lookup for the standalone Marvel scanner. Reuse Home's
# existing multi-provider search pipeline rather than scraping 1337x directly.


from integrations.provider_integrations import *
from catalogues.catalogue_responses import *
from seedr.seedr_sessions import *
from torrents.torrent_metadata import *
from seedr.seedr_client import *
from torrents.torrent_search import *
from catalogues.poster_service import *
from torrents.torrent_inspection import *
from catalogues.franchise_catalogues import *
from catalogues.catalogue_enrichment import *
from catalogues.movie_catalogues import *
from catalogues.tv_catalogues import *
from catalogues.regional_catalogues import *
from routes.discovery_routes import *
from routes.metadata_routes import *
from routes.cache_routes import *
from seedr.seedr_auth_routes import *
from seedr.seedr_library_routes import *
from seedr.seedr_media_routes import *
from torrents.torrent_routes import *
from routes.compatibility_routes import *


from seedr.seedr_client import router as _seedr_client_routes
from catalogues.poster_service import router as _poster_routes
from torrents.torrent_inspection import router as _torrent_inspection_routes
from catalogues.catalogue_enrichment import router as _catalogue_enrichment_routes
from catalogues.movie_catalogues import router as _movie_catalogue_routes
from catalogues.tv_catalogues import router as _tv_catalogue_routes
from catalogues.regional_catalogues import router as _regional_catalogue_routes
from routes.discovery_routes import router as _discovery_routes
from routes.metadata_routes import router as _metadata_routes
from routes.cache_routes import router as _cache_routes
from seedr.seedr_auth_routes import router as _seedr_auth_routes
from seedr.seedr_library_routes import router as _seedr_library_routes
from seedr.seedr_media_routes import router as _seedr_media_routes
from torrents.torrent_routes import router as _torrent_routes
from routes.compatibility_routes import router as _compatibility_routes


app.include_router(_seedr_client_routes)
app.include_router(_poster_routes)
app.include_router(_torrent_inspection_routes)
app.include_router(_catalogue_enrichment_routes)
app.include_router(_movie_catalogue_routes)
app.include_router(_tv_catalogue_routes)
app.include_router(_regional_catalogue_routes)
app.include_router(_discovery_routes)
app.include_router(_metadata_routes)
app.include_router(_cache_routes)
app.include_router(_seedr_auth_routes)
app.include_router(_seedr_library_routes)
app.include_router(_seedr_media_routes)
app.include_router(_torrent_routes)
app.include_router(_compatibility_routes)
