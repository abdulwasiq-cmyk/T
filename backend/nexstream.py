"""Experimental NexStream resolver and title search used by the isolated test page.

Provider credentials stay on the server. Only approved HTTPS EmbedMaster player
URLs are returned to the browser; no direct media URLs are exposed by this adapter.
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cache_diagnostics import _InstrumentedAsyncClient, _record_cache_event
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
NEXSTREAM_API_BASE = "https://api.codespecters.com"
TMDB_API_BASE = "https://api.themoviedb.org/3"
ALLOWED_EMBED_HOSTS = {"embedmaster.link", "www.embedmaster.link"}
_NEXSTREAM_CACHE_PATH = Path(os.getenv("NEXSTREAM_CACHE_PATH", "/app/data/nexstream_cache.json"))
_NEXSTREAM_CACHE_SEED_PATH = Path(
    os.getenv("NEXSTREAM_CACHE_SEED_PATH", "/app/cache-seed/runtime/nexstream_cache.json")
)
_NEXSTREAM_EPISODE_CACHE_PATH = Path(
    os.getenv("NEXSTREAM_EPISODE_CACHE_PATH", "/app/data/nexstream_episode_metadata_cache.json")
)
_NEXSTREAM_EPISODE_CACHE_SEED_PATH = Path(
    os.getenv(
        "NEXSTREAM_EPISODE_CACHE_SEED_PATH",
        "/app/cache-seed/runtime/nexstream_episode_metadata_cache.json",
    )
)
_NEXSTREAM_CACHE_LOCK = threading.Lock()
_NEXSTREAM_SEARCH_CACHE_TTL = 24 * 60 * 60
_NEXSTREAM_RESOLVE_CACHE_TTL = 5 * 60


def _seed_nexstream_cache() -> None:
    """Restore the bundled Nexstream cache only when persistent data is absent."""
    try:
        if _NEXSTREAM_CACHE_PATH.is_file() and _NEXSTREAM_CACHE_PATH.stat().st_size > 0:
            return
        if not _NEXSTREAM_CACHE_SEED_PATH.is_file() or _NEXSTREAM_CACHE_SEED_PATH.stat().st_size == 0:
            return
        payload = json.loads(_NEXSTREAM_CACHE_SEED_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        _NEXSTREAM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _NEXSTREAM_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except (OSError, ValueError, TypeError):
        # Cache seeding is best-effort and must never prevent API startup.
        return


_seed_nexstream_cache()


def _seed_nexstream_episode_cache() -> None:
    """Restore bundled episode metadata only when persistent data is absent."""
    try:
        if (
            _NEXSTREAM_EPISODE_CACHE_PATH.is_file()
            and _NEXSTREAM_EPISODE_CACHE_PATH.stat().st_size > 0
        ):
            return
        if (
            not _NEXSTREAM_EPISODE_CACHE_SEED_PATH.is_file()
            or _NEXSTREAM_EPISODE_CACHE_SEED_PATH.stat().st_size == 0
        ):
            return
        payload = json.loads(
            _NEXSTREAM_EPISODE_CACHE_SEED_PATH.read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            return
        _NEXSTREAM_EPISODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _NEXSTREAM_EPISODE_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except (OSError, ValueError, TypeError):
        # Cache seeding is best-effort and must never prevent API startup.
        return


_seed_nexstream_episode_cache()


def _read_nexstream_cache() -> dict[str, dict]:
    try:
        payload = json.loads(_NEXSTREAM_CACHE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _get_nexstream_cache(key: str, ttl: int):
    store = "nexstream_search" if key.startswith("search:") else "nexstream_resolve"
    with _NEXSTREAM_CACHE_LOCK:
        entry = _read_nexstream_cache().get(key)
    if not isinstance(entry, dict):
        _record_cache_event(store, "miss")
        return None
    cached_at = entry.get("cachedAt")
    if not isinstance(cached_at, (int, float)) or time.time() - cached_at >= ttl:
        _record_cache_event(store, "expired_miss")
        return None
    value = entry.get("value")
    if not isinstance(value, dict):
        _record_cache_event(store, "invalid_miss")
        return None
    _record_cache_event(store, "persistent_hit")
    if key.startswith("search:"):
        _record_cache_event(store, "result_rows_served", len(value.get("results") or []))
    else:
        _record_cache_event(store, "source_rows_served", len(value.get("sources") or []))
    return value


def _set_nexstream_cache(key: str, value: dict) -> None:
    _NEXSTREAM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _NEXSTREAM_CACHE_LOCK:
        cache = _read_nexstream_cache()
        now = time.time()
        # Keep the persistent cache bounded and discard old/expired records.
        cache = {
            cache_key: entry
            for cache_key, entry in cache.items()
            if isinstance(entry, dict)
            and isinstance(entry.get("cachedAt"), (int, float))
            and now - entry["cachedAt"] < _NEXSTREAM_SEARCH_CACHE_TTL
        }
        cache[key] = {"cachedAt": now, "value": value}
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(_NEXSTREAM_CACHE_PATH.parent),
                prefix="nexstream-cache-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(cache, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, _NEXSTREAM_CACHE_PATH)
        finally:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass


def _env_values(*names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        raw = os.getenv(name, "")
        for item in raw.replace("\\n", "\n").replace(",", "\n").splitlines():
            item = item.strip()
            if item and item not in values:
                values.append(item)
    return values


def nexstream_api_key_configured() -> bool:
    return bool(os.getenv("NEXSTREAM_API_KEY", "").strip())


async def resolve_nexstream_movie(tmdb_id: int) -> dict:
    """Resolve a movie through NexStream, returning only supported player sources."""
    if tmdb_id <= 0:
        raise HTTPException(status_code=404, detail="Unknown movie.")
    api_key = os.getenv("NEXSTREAM_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="NEXSTREAM_API_KEY is not configured on the server.",
        )

    media_type = "movie"
    cache_key = f"resolve:{media_type}:{tmdb_id}:0:0"
    cached = _get_nexstream_cache(cache_key, _NEXSTREAM_RESOLVE_CACHE_TTL)
    if cached is not None:
        return cached

    try:
        async with _InstrumentedAsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(
                f"{NEXSTREAM_API_BASE}/api/movie/{tmdb_id}",
                params={"apikey": api_key},
                headers={"Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to NexStream. Try again later.",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"NexStream returned HTTP {response.status_code}.",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="NexStream returned an invalid response.",
        ) from exc

    if not isinstance(payload, dict) or not payload.get("success"):
        message = payload.get("message") if isinstance(payload, dict) else None
        result = {
            "success": False,
            "type": media_type,
            "tmdbId": str(tmdb_id),
            "message": str(message or "NexStream could not resolve this title."),
            "sources": [],
        }
        _set_nexstream_cache(cache_key, result)
        return result

    safe_sources = []
    sources = payload.get("sources")
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_EMBED_HOSTS:
            continue
        safe_sources.append({
            "name": str(source.get("name") or "NexStream source")[:80],
            "url": url,
        })

    if not safe_sources:
        result = {
            "success": False,
            "type": media_type,
            "tmdbId": str(tmdb_id),
            "message": "NexStream returned no supported EmbedMaster player source.",
            "sources": [],
        }
        _set_nexstream_cache(cache_key, result)
        return result

    result = {
        "success": True,
        "type": media_type,
        "tmdbId": str(tmdb_id),
        "season": None,
        "episode": None,
        "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
        "sources": safe_sources,
    }
    _set_nexstream_cache(cache_key, result)
    return result


@router.get("/api/nexstream/search")
async def search_titles(
    query: str = Query(..., min_length=2, max_length=120),
    media_type: str = Query("movie", alias="type", pattern="^(movie|tv)$"),
):
    """Search TMDB by title so testers don't need to know TMDB IDs."""
    api_keys = _env_values("TMDB_API_KEYS", "TMDB_API_KEY")
    bearer_tokens = _env_values("TMDB_READ_ACCESS_TOKENS", "TMDB_READ_ACCESS_TOKEN", "TMDB_BEARER_TOKEN")
    if not api_keys and not bearer_tokens:
        raise HTTPException(
            status_code=503,
            detail="TMDB credentials are not configured on the test server.",
        )

    endpoint = "movie" if media_type == "movie" else "tv"
    normalized_query = " ".join(query.split()).casefold()
    cache_key = f"search:{media_type}:{normalized_query}"
    cached = _get_nexstream_cache(cache_key, _NEXSTREAM_SEARCH_CACHE_TTL)
    if cached is not None:
        return cached

    params = {"query": query.strip(), "include_adult": "false", "page": "1"}
    payload = None
    last_status = None
    connection_errors = 0

    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(12.0), follow_redirects=True) as client:
        # A broken connection for one credential must not prevent trying the others.
        for key in api_keys:
            try:
                response = await client.get(
                    f"{TMDB_API_BASE}/search/{endpoint}",
                    params={**params, "api_key": key},
                    headers={"Accept": "application/json"},
                )
            except httpx.RequestError:
                connection_errors += 1
                continue

            last_status = response.status_code
            if response.status_code == 200:
                payload = response.json()
                break
            if response.status_code == 429:
                raise HTTPException(status_code=429, detail="TMDB search rate limit reached. Try again shortly.")

        if payload is None:
            for token in bearer_tokens:
                try:
                    response = await client.get(
                        f"{TMDB_API_BASE}/search/{endpoint}",
                        params=params,
                        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                    )
                except httpx.RequestError:
                    connection_errors += 1
                    continue

                last_status = response.status_code
                if response.status_code == 200:
                    payload = response.json()
                    break
                if response.status_code == 429:
                    raise HTTPException(status_code=429, detail="TMDB search rate limit reached. Try again shortly.")

    if payload is None:
        if last_status in (401, 403):
            raise HTTPException(status_code=502, detail="TMDB rejected the configured credentials.")
        if connection_errors and last_status is None:
            raise HTTPException(status_code=502, detail="Could not connect to TMDB title search.")
        raise HTTPException(status_code=502, detail="TMDB title search failed.")

    results = []
    for item in payload.get("results", [])[:12]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        title = item.get("title") if media_type == "movie" else item.get("name")
        results.append({
            "id": int(item["id"]),
            "title": str(title or "Untitled"),
            "date": str((item.get("release_date") if media_type == "movie" else item.get("first_air_date")) or ""),
            "overview": str(item.get("overview") or "")[:400],
            "poster": f"https://image.tmdb.org/t/p/w185{item['poster_path']}" if item.get("poster_path") else None,
            "type": media_type,
        })
    result = {"success": True, "type": media_type, "results": results}
    _set_nexstream_cache(cache_key, result)
    return result




_NEXSTREAM_EPISODE_CACHE_PATH = Path(os.getenv("NEXSTREAM_EPISODE_CACHE_PATH", "/app/data/nexstream_episode_metadata_cache.json"))
_NEXSTREAM_EPISODE_CACHE_LOCK = threading.Lock()


def _read_episode_cache(tmdb_id: int, season: int):
    try:
        with _NEXSTREAM_EPISODE_CACHE_LOCK:
            if not _NEXSTREAM_EPISODE_CACHE_PATH.is_file():
                _record_cache_event("nexstream_episode_metadata", "miss")
                return None
            data = json.loads(_NEXSTREAM_EPISODE_CACHE_PATH.read_text(encoding="utf-8"))
        show = (data.get("shows") or {}).get(str(tmdb_id)) or {}
        episodes = (show.get("episodesBySeason") or {}).get(str(season))
        if episodes is None:
            _record_cache_event("nexstream_episode_metadata", "miss")
            return None
        _record_cache_event("nexstream_episode_metadata", "persistent_hit")
        _record_cache_event("nexstream_episode_metadata", "episode_rows_served", len(episodes))
        return {"success": True, "tmdbId": tmdb_id, "seasons": show.get("seasons") or [], "selectedSeason": season, "episodes": episodes, "cached": True}
    except Exception:
        _record_cache_event("nexstream_episode_metadata", "read_error_miss")
        return None


def _write_episode_cache(tmdb_id: int, seasons: list, season: int, episodes: list) -> None:
    try:
        _NEXSTREAM_EPISODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _NEXSTREAM_EPISODE_CACHE_LOCK:
            data = {"shows": {}}
            if _NEXSTREAM_EPISODE_CACHE_PATH.is_file():
                try:
                    existing = json.loads(_NEXSTREAM_EPISODE_CACHE_PATH.read_text(encoding="utf-8"))
                    if isinstance(existing, dict):
                        data = existing
                except Exception:
                    pass
            shows = data.setdefault("shows", {})
            show = shows.setdefault(str(tmdb_id), {"tmdbId": tmdb_id, "seasons": [], "episodesBySeason": {}})
            show["tmdbId"] = tmdb_id
            show["seasons"] = seasons or show.get("seasons", [])
            show.setdefault("episodesBySeason", {})[str(season)] = episodes
            temp = _NEXSTREAM_EPISODE_CACHE_PATH.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temp.replace(_NEXSTREAM_EPISODE_CACHE_PATH)
    except Exception:
        return

@router.get("/api/nexstream/episodes")
async def get_tv_episodes(
    tmdb_id: int = Query(..., gt=0),
    season: int = Query(1, ge=1),
):
    """Serve persisted episode metadata first; fetch TMDB only on cache misses."""
    cached = _read_episode_cache(tmdb_id, season)
    if cached is not None:
        return cached
    api_keys = _env_values("TMDB_API_KEYS", "TMDB_API_KEY")
    bearer_tokens = _env_values("TMDB_READ_ACCESS_TOKENS", "TMDB_READ_ACCESS_TOKEN", "TMDB_BEARER_TOKEN")
    if not api_keys and not bearer_tokens:
        raise HTTPException(status_code=503, detail="TMDB credentials are not configured on the test server.")

    async with _InstrumentedAsyncClient(timeout=httpx.Timeout(12.0), follow_redirects=True) as client:
        async def tmdb_get(path: str):
            last_status = None
            connection_errors = 0
            for key in api_keys:
                try:
                    response = await client.get(f"{TMDB_API_BASE}{path}", params={"api_key": key}, headers={"Accept": "application/json"})
                except httpx.RequestError:
                    connection_errors += 1
                    continue
                last_status = response.status_code
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 429:
                    raise HTTPException(status_code=429, detail="TMDB rate limit reached. Try again shortly.")
            for token in bearer_tokens:
                try:
                    response = await client.get(f"{TMDB_API_BASE}{path}", headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
                except httpx.RequestError:
                    connection_errors += 1
                    continue
                last_status = response.status_code
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 429:
                    raise HTTPException(status_code=429, detail="TMDB rate limit reached. Try again shortly.")
            if last_status in (401, 403):
                raise HTTPException(status_code=502, detail="TMDB rejected the configured credentials.")
            if connection_errors and last_status is None:
                raise HTTPException(status_code=502, detail="Could not connect to TMDB episode metadata.")
            raise HTTPException(status_code=502, detail=f"TMDB episode metadata failed (HTTP {last_status or 'unknown'}).")

        show = await tmdb_get(f"/tv/{tmdb_id}")
        seasons = [
            {"seasonNumber": int(item.get("season_number", 0)), "name": str(item.get("name") or f"Season {item.get('season_number', 0)}"), "episodeCount": int(item.get("episode_count", 0))}
            for item in show.get("seasons", [])
            if isinstance(item, dict) and int(item.get("season_number", 0)) > 0
        ]
        selected_season = season if any(item["seasonNumber"] == season for item in seasons) else (seasons[0]["seasonNumber"] if seasons else season)
        season_data = await tmdb_get(f"/tv/{tmdb_id}/season/{selected_season}")
    episodes = [
        {
            "episodeNumber": int(item.get("episode_number", 0)),
            "name": str(item.get("name") or ""),
            "overview": str(item.get("overview") or "")[:500],
            "airDate": str(item.get("air_date") or ""),
            "still": f"https://image.tmdb.org/t/p/w300{item['still_path']}" if item.get("still_path") else None,
        }
        for item in season_data.get("episodes", [])
        if isinstance(item, dict) and int(item.get("episode_number", 0)) > 0
    ]
    _write_episode_cache(tmdb_id, seasons, selected_season, episodes)
    return {"success": True, "tmdbId": tmdb_id, "seasons": seasons, "selectedSeason": selected_season, "episodes": episodes}


@router.get("/api/nexstream/resolve")
async def resolve_nexstream(
    media_type: str = Query("movie", alias="type", pattern="^(movie|tv)$"),
    tmdb_id: int = Query(..., gt=0),
    season: int | None = Query(None, ge=1),
    episode: int | None = Query(None, ge=1),
):
    """Resolve a movie or TV episode through NexStream without exposing its key."""
    if media_type == "movie":
        result = await resolve_nexstream_movie(tmdb_id)
        if not result.get("success"):
            raise HTTPException(
                status_code=502,
                detail=str(result.get("message") or "NexStream could not resolve this title."),
            )
        return result

    api_key = os.getenv("NEXSTREAM_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="NEXSTREAM_API_KEY is not configured on the server.",
        )

    cache_key = f"resolve:{media_type}:{tmdb_id}:{season or 0}:{episode or 0}"
    cached = _get_nexstream_cache(cache_key, _NEXSTREAM_RESOLVE_CACHE_TTL)
    if cached is not None:
        return cached

    if media_type == "tv":
        if season is None or episode is None:
            raise HTTPException(
                status_code=422,
                detail="TV playback requires both season and episode.",
            )
        path = f"/api/tv/{tmdb_id}/{season}/{episode}"
    else:
        path = f"/api/movie/{tmdb_id}"

    try:
        async with _InstrumentedAsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(
                f"{NEXSTREAM_API_BASE}{path}",
                params={"apikey": api_key},
                headers={"Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to NexStream. Try again later.",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"NexStream returned HTTP {response.status_code}.",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="NexStream returned an invalid response.",
        ) from exc

    if not isinstance(payload, dict) or not payload.get("success"):
        message = payload.get("message") if isinstance(payload, dict) else None
        raise HTTPException(
            status_code=502,
            detail=str(message or "NexStream could not resolve this title."),
        )

    safe_sources = []
    for source in payload.get("sources", []):
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_EMBED_HOSTS:
            continue
        safe_sources.append({
            "name": str(source.get("name") or "NexStream source")[:80],
            "url": url,
        })

    if not safe_sources:
        raise HTTPException(
            status_code=502,
            detail="NexStream returned no supported EmbedMaster player source.",
        )

    result = {
        "success": True,
        "type": media_type,
        "tmdbId": str(tmdb_id),
        "season": season if media_type == "tv" else None,
        "episode": episode if media_type == "tv" else None,
        "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
        "sources": safe_sources,
    }
    _set_nexstream_cache(cache_key, result)
    return result
