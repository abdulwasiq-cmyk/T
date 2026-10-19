"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['_cache_file_snapshot', 'api_cache_report', 'api_health']

router = APIRouter()

def _cache_file_snapshot(name: str, path: _main.Path, *, bundled: bool = False) -> dict[str, _main.Any]:
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
        row["modifiedAt"] = _main.datetime.fromtimestamp(stat.st_mtime, _main.timezone.utc).isoformat()
        payload = _main.json.loads(path.read_text(encoding="utf-8"))
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

@router.get("/api/cache/report")
async def api_cache_report():
    """Inventory saved cache data and report process-local cache/use counters."""
    if not _main.DEBUG_ENDPOINTS_ENABLED:
        # Reveals server file paths and internal counters; hidden unless enabled.
        raise _main.HTTPException(404, "Not found")
    runtime_paths: dict[str, _main.Path] = {
        name: path for name, path in _main._RUNTIME_CACHE_SEED_FILES.items()
    }
    runtime_paths["marvel_catalogue.json"] = _main.MARVEL_CATALOGUE_PATH
    runtime_paths["marvel_web_series_catalogue.json"] = _main.Path(
        _main.os.getenv("MARVEL_WEB_SERIES_CATALOGUE_PATH", "/app/data/marvel_web_series_catalogue.json")
    )
    runtime_paths["bollywood_torrent_matched_catalogue.json"] = _main.Path(
        _main.os.getenv("BOLLYWOOD_TORRENT_CATALOGUE_PATH", str(_main.Path(_main.os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_torrent_matched_catalogue.json"))
    )
    runtime_paths["torrent_metadata_cache"] = _main.TORRENT_METADATA_CACHE_FILE
    runtime_paths["regional_tv_prebuilt_cache.json"] = _main.REGIONAL_TV_PREBUILT_CACHE_FILE
    runtime_paths["regional_movie_prebuilt_cache.json"] = _main.REGIONAL_MOVIE_PREBUILT_CACHE_PATH
    runtime_paths["classics_web_series_tv_catalogue.json"] = _main.Path(_main.os.getenv("APP_DATA_DIR", "/app/data")) / "classics_web_series_tv_catalogue.json"
    runtime_paths["classics_anime_tv_catalogue.json"] = _main.Path(_main.os.getenv("APP_DATA_DIR", "/app/data")) / "classics_anime_tv_catalogue.json"
    runtime_paths["nexstream_cache.json"] = _main.Path(_main.os.getenv("NEXSTREAM_CACHE_PATH", "/app/data/nexstream_cache.json"))
    runtime_paths["nexstream_episode_metadata_cache.json"] = _main.Path(_main.os.getenv("NEXSTREAM_EPISODE_CACHE_PATH", "/app/data/nexstream_episode_metadata_cache.json"))
    for key, path in _main._EXTRA_CATALOGUE_PATHS.items():
        runtime_paths["catalogue:" + key] = path
    runtime_files = [
        _main._cache_file_snapshot(name, path)
        for name, path in sorted(runtime_paths.items())
    ]

    bundled_paths: dict[str, _main.Path] = {
        name: _main._CACHE_SEED_DIR / name for name in _main._CACHE_SEED_FILES
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
        bundled_paths["runtime/" + name] = _main._CACHE_SEED_DIR / "runtime" / name
    bundled_files = [
        _main._cache_file_snapshot(name, path, bundled=True)
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
        for store, events in sorted(_main._CACHE_DIAGNOSTICS["cache_events"].items())
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
        "requests": int(_main._CACHE_DIAGNOSTICS["upstream_requests"]),
        "failures": int(_main._CACHE_DIAGNOSTICS["upstream_failures"]),
        "byProvider": dict(sorted(_main._CACHE_DIAGNOSTICS["upstream_by_provider"].items())),
        "byEndpoint": dict(sorted(
            _main._CACHE_DIAGNOSTICS["upstream_by_endpoint"].items(),
            key=lambda pair: pair[1],
            reverse=True,
        )[:100]),
        "byFeature": dict(sorted(
            _main._CACHE_DIAGNOSTICS["upstream_by_feature"].items(),
            key=lambda pair: pair[1],
            reverse=True,
        )[:150]),
        "statusCodes": dict(sorted(_main._CACHE_DIAGNOSTICS["upstream_status_codes"].items())),
    }
    now = _main.time.time()
    return {
        "status": "ok",
        "measurementStartedAt": _main.datetime.fromtimestamp(_main._CACHE_DIAGNOSTICS_STARTED_AT, _main.timezone.utc).isoformat(),
        "measurementScope": "This worker process only; counters reset on worker restart and are not aggregated across Render instances.",
        "ottCacheOnly": _main.CACHE_ONLY_MODE,
        "uptimeSeconds": max(0, int(now - _main._CACHE_DIAGNOSTICS_STARTED_AT)),
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

@router.get("/api/health")
async def api_health():
    return {"name": _main.APP_NAME, "status": "ok"}
