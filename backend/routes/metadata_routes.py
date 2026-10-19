"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['api_search_metadata', '_search_metadata_cache_candidate', '_read_persistent_search_metadata_cache', '_write_persistent_search_metadata_cache', '_persist_search_metadata_cache_rows', '_persist_search_metadata_overview', '_lookup_search_metadata_by_tmdb_id', '_read_search_metadata_catalogue_rows', '_imdb_rating_cache_key', '_parse_imdb_rating_title', '_load_imdb_rating_cache', '_save_imdb_rating_cache_record', '_fetch_imdb_rating', '_get_imdb_rating_record', 'api_catalogue_imdb_ratings', 'api_search_metadata_batch', 'api_search', 'api_regional_movie_prebuilt']

router = APIRouter()

@router.get("/api/search/metadata")
async def api_search_metadata(q: str = _main.Query(..., min_length=2, max_length=160)):
    """Return poster, synopsis, release year and TMDB link for fast search cards."""
    query = _main.re.sub(r"\s+", " ", q).strip()
    cache_key = query.casefold()
    now = _main.time.time()
    cached = _main._TMDB_SEARCH_METADATA_CACHE.get(cache_key)
    if cached and now - cached[0] < _main._TMDB_SEARCH_METADATA_CACHE_TTL:
        _main._record_cache_event("search_metadata", "memory_hit")
        return cached[1]
    _main._record_cache_event("search_metadata", "miss")

    # Disk-backed query envelopes survive process/container restarts. The per-title
    # cache below remains durable indefinitely and is refreshed whenever a new
    # successful lookup supplies better data.
    persistent_cache = _main._read_persistent_search_metadata_cache()
    query_entry = (persistent_cache.get("queries") or {}).get(cache_key)
    try:
        query_saved_at = int((query_entry or {}).get("savedAt") or 0)
    except (TypeError, ValueError):
        query_saved_at = 0
    if (
        isinstance(query_entry, dict)
        and now - query_saved_at < _main.TMDB_SEARCH_METADATA_QUERY_TTL_SECONDS
    ):
        # Read newer reference-only envelopes; continue to support any legacy
        # envelope from a previous deployment.
        cached_response = query_entry.get("response")
        if isinstance(cached_response, dict) and cached_response.get("status") == "ready":
            _main._record_cache_event("search_metadata", "persistent_query_hit")
            _main._TMDB_SEARCH_METADATA_CACHE[cache_key] = (now, cached_response)
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
            candidate = _main._search_metadata_cache_candidate(raw)
            if not candidate:
                continue
            row_key = (
                _main._normalize_title(candidate["title"]),
                int(candidate.get("year") or 0),
                str(candidate.get("mediaType") or "movie"),
                str(candidate.get("tmdbId") or ""),
            )
            if row_key in requested_keys:
                cached_results.append(candidate)
        if cached_results:
            _main._record_cache_event("search_metadata", "persistent_title_hit")
            _main._record_cache_event("search_metadata", "persistent_rows_served", len(cached_results))
            cached_response = {
                "status": "ready",
                "query": query,
                "source": "Persistent synopsis cache",
                "results": cached_results[:30],
            }
            _main._TMDB_SEARCH_METADATA_CACHE[cache_key] = (now, cached_response)
            return cached_response

    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
        return {"status": "unavailable", "query": query, "source": "TMDB", "results": []}

    try:
        # Give the helper time to try each configured token on key-specific
        # failures. Use a shorter per-token timeout for this non-blocking UI lookup.
        payload = await _main.asyncio.wait_for(
            _main._tmdb_get_json("search/multi", {
                "query": query,
                "include_adult": "false",
                "language": "en-US",
                "page": 1,
            }, request_timeout=3.5),
            timeout=14.0,
        )
    except Exception as exc:
        _main.logger.info("TMDB quick metadata search failed for %r: %s", query, type(exc).__name__)
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
        year_match = _main.re.match(r"^(\d{4})", release_date)
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
            "posterUrl": _main.TMDB_IMAGE_BASE + poster_path if poster_path else "",
            "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
            "rating": float(item.get("vote_average") or 0),
            "tmdbUrl": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
            "source": "TMDB",
        })

    target = _main._availability_normalize_title(query)
    results.sort(key=lambda item: (
        _main._availability_normalize_title(item["title"]) == target,
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
        _main._persist_search_metadata_cache_rows(results[:30], query=query, query_response=response)
        if len(_main._TMDB_SEARCH_METADATA_CACHE) > 300:
            expired_keys = [
                key for key, value in _main._TMDB_SEARCH_METADATA_CACHE.items()
                if now - value[0] >= _main._TMDB_SEARCH_METADATA_CACHE_TTL
            ]
            for key in expired_keys:
                _main._TMDB_SEARCH_METADATA_CACHE.pop(key, None)
            if len(_main._TMDB_SEARCH_METADATA_CACHE) > 300:
                _main._TMDB_SEARCH_METADATA_CACHE.pop(next(iter(_main._TMDB_SEARCH_METADATA_CACHE)), None)
        _main._TMDB_SEARCH_METADATA_CACHE[cache_key] = (now, response)
    return response

def _search_metadata_cache_candidate(raw: dict[str, _main.Any]) -> dict[str, _main.Any] | None:
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
    if not year and _main.re.match(r"^\d{4}", release_date):
        year = int(release_date[:4])
    if not year:
        year_match = _main.re.search(r"\b((?:19|20)\d{2})\b\s*\)?\s*$", title)
        if year_match:
            year = int(year_match.group(1))

    canonical_title = _main.re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", title).strip()
    normalized = _main._normalize_title(canonical_title)
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
    overview = _main.re.sub(r"\s+", " ", str(raw.get("overview") or "")).strip()
    # Keep rows with a known TMDB ID even when overview is empty. The batch
    # endpoint can hydrate those rows from TMDB's movie-details endpoint.

    def _number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    poster_url = str(raw.get("posterUrl") or raw.get("poster_url") or "")
    if not poster_url and str(raw.get("poster_path") or "").strip():
        poster_url = _main.TMDB_IMAGE_BASE + str(raw["poster_path"]).strip()
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

def _read_persistent_search_metadata_cache() -> dict[str, _main.Any]:
    try:
        payload = _main.json.loads(_main.TMDB_SEARCH_METADATA_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("results", [])
            payload.setdefault("queries", {})
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "results": [], "queries": {}}

def _write_persistent_search_metadata_cache(payload: dict[str, _main.Any]) -> None:
    # The same atomic/fsync writer used by the catalogue snapshots keeps this
    # cache safe across container restarts and interrupted writes.
    _main._write_synopsis_cache(_main.TMDB_SEARCH_METADATA_CACHE_FILE, payload)

def _persist_search_metadata_cache_rows(
    raw_rows: list[dict[str, _main.Any]],
    *,
    query: str | None = None,
    query_response: dict[str, _main.Any] | None = None,
) -> int:
    """Persist per-title metadata permanently without duplicating synopsis text in query envelopes."""
    disk = _main._read_persistent_search_metadata_cache()
    rows = disk.get("results", [])
    if not isinstance(rows, list):
        rows = []
    index: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        candidate = _main._search_metadata_cache_candidate(raw)
        if not candidate:
            continue
        key = (
            _main._normalize_title(candidate["title"]),
            int(candidate.get("year") or 0),
            str(candidate.get("mediaType") or "movie"),
            str(candidate.get("tmdbId") or ""),
        )
        index[key] = {**raw, **candidate}

    now = int(_main.time.time())
    added = 0
    query_keys: list[dict[str, Any]] = []
    seen_query_keys: set[tuple[str, int, str, str]] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        candidate = _main._search_metadata_cache_candidate(raw)
        if not candidate:
            continue
        key = (
            _main._normalize_title(candidate["title"]),
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
        queries[_main.re.sub(r"\s+", " ", query).strip().casefold()] = {
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
        _main._write_persistent_search_metadata_cache(disk)
    except OSError as exc:
        _main.logger.warning("Could not persist search-card synopsis cache: %s", exc)
    return added

def _persist_search_metadata_overview(tmdb_id: int, overview: str) -> int:
    """Persist a newly fetched overview into any matching runtime catalogue row."""
    overview = _main.re.sub(r"\s+", " ", str(overview or "")).strip()
    if tmdb_id < 1 or not overview:
        return 0

    updated_rows = 0
    now = int(_main.time.time())
    for _name, path in _main._synopsis_cache_paths():
        try:
            payload = _main.json.loads(path.read_text(encoding="utf-8"))
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
            row["synopsisLookupVersion"] = _main.TMDB_SYNOPSIS_LOOKUP_VERSION
            changed = True
            updated_rows += 1
        if changed:
            try:
                _main._write_synopsis_cache(path, payload)
                _main.logger.info("Saved fetched TMDB synopsis for ID %s into %s", tmdb_id, path.name)
            except OSError as exc:
                _main.logger.warning("Could not persist synopsis for TMDB ID %s in %s: %s", tmdb_id, path.name, exc)

    # Popular/Trending/Latest responses are stored in a separate persistent file.
    try:
        disk = _main.json.loads(_main.TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
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
                    row["synopsisLookupVersion"] = _main.TMDB_SYNOPSIS_LOOKUP_VERSION
                    updated_rows += 1
                    disk_changed = True

    if disk_changed:
        temporary: _main.Path | None = None
        try:
            _main.TMDB_CATALOGUE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _main.tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                prefix=_main.TMDB_CATALOGUE_CACHE_FILE.stem + "-synopsis-",
                suffix=".tmp", dir=str(_main.TMDB_CATALOGUE_CACHE_FILE.parent), delete=False,
            ) as handle:
                temporary = _main.Path(handle.name)
                _main.json.dump(disk, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                _main.os.fsync(handle.fileno())
            _main.os.replace(temporary, _main.TMDB_CATALOGUE_CACHE_FILE)
        except (OSError, ValueError, TypeError) as exc:
            _main.logger.warning("Could not persist dynamic catalogue synopsis for TMDB ID %s: %s", tmdb_id, exc)
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    # Keep in-memory catalogue responses consistent with the disk cache.
    for cache_key, entry in list(_main._tmdb_movie_catalogue_cache.items()):
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
                row["synopsisLookupVersion"] = _main.TMDB_SYNOPSIS_LOOKUP_VERSION
    return updated_rows

async def _lookup_search_metadata_by_tmdb_id(item: dict[str, _main.Any]) -> dict[str, _main.Any] | None:
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
            details = await _main.asyncio.wait_for(
                _main._tmdb_get_json(
                    f"movie/{tmdb_id}",
                    {"language": language},
                    request_timeout=3.0,
                ),
                timeout=4.5,
            )
        except Exception as exc:
            _main.logger.info("On-demand synopsis lookup failed for TMDB ID %s (%s): %s",
                tmdb_id, language, type(exc).__name__)
            continue
        overview = _main.re.sub(r"\s+", " ", str(details.get("overview") or "")).strip()
        if overview:
            return {
                "overview": overview,
                "tmdbId": tmdb_id,
                "tmdbUrl": f"https://www.themoviedb.org/movie/{tmdb_id}",
                "rating": float(details.get("vote_average") or item.get("rating") or 0),
                "posterUrl": (
                    _main.TMDB_IMAGE_BASE + str(details.get("poster_path")).strip()
                    if str(details.get("poster_path") or "").strip()
                    else str(item.get("posterUrl") or "")
                ),
                "backdropUrl": (
                    "https://image.tmdb.org/t/p/w780" + str(details.get("backdrop_path")).strip()
                    if str(details.get("backdrop_path") or "").strip()
                    else str(item.get("backdropUrl") or "")
                ),
                "synopsisCheckedAt": int(_main.time.time()),
                "synopsisLookupVersion": _main.TMDB_SYNOPSIS_LOOKUP_VERSION,
            }
        # Only issue the alternate-language request if TMDB answered but had no overview.
    return None

def _read_search_metadata_catalogue_rows() -> list[dict[str, _main.Any]]:
    """Read metadata from the persistent VPS JSON snapshots used by the catalogues."""
    candidates: list[dict[str, Any]] = []
    for _name, path in _main._synopsis_cache_paths():
        try:
            payload = _main.json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if isinstance(rows, list):
            candidates.extend(row for row in rows if isinstance(row, dict))

    # Query search results and successful synopses are kept separately so arbitrary
    # searches survive restarts even when they are not in a prebuilt catalogue.
    persistent_metadata = _main._read_persistent_search_metadata_cache()
    persistent_rows = persistent_metadata.get("results", [])
    if isinstance(persistent_rows, list):
        candidates.extend(row for row in persistent_rows if isinstance(row, dict))

    # Popular, Trending and Latest are stored separately from franchise snapshots.
    try:
        disk = _main.json.loads(_main.TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
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
    return f"{resolved_type}|{_main._normalize_synopsis_match_title(title)}|{int(year or 0)}"

def _parse_imdb_rating_title(value: str) -> tuple[str, int]:
    raw = _main.re.sub(r"\s+", " ", str(value or "")).strip()
    match = _main.re.search(r"\s*\(?((?:18|19|20|21)\d{2})\)?\s*$", raw)
    year = int(match.group(1)) if match else 0
    title = raw[:match.start()].strip() if match else raw
    if not title or not 1870 <= year <= 2100:
        return raw, 0
    return title, year

def _load_imdb_rating_cache() -> dict[str, dict[str, _main.Any]]:
    global _IMDB_RATING_CACHE
    if _main._IMDB_RATING_CACHE is not None:
        return _main._IMDB_RATING_CACHE
    cache: dict[str, dict[str, Any]] = {}
    try:
        payload = _main.json.loads(_main.IMDB_RATING_CACHE_FILE.read_text(encoding="utf-8"))
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
                key = str(row.get("cacheKey") or (_main._imdb_rating_cache_key(title, year) if title else ""))
                if key:
                    cache[key] = row
    except (OSError, ValueError, TypeError):
        pass
    _main._IMDB_RATING_CACHE = cache
    return cache

async def _save_imdb_rating_cache_record(record: dict[str, _main.Any]) -> None:
    key = str(record.get("cacheKey") or "")
    if not key:
        return
    async with _main._IMDB_RATING_CACHE_LOCK:
        cache = _main._load_imdb_rating_cache()
        cache[key] = record
        payload = {
            "version": 1,
            "updatedAt": int(_main.time.time()),
            "results": list(cache.values())[-10000:],
        }
        try:
            _main._write_synopsis_cache(_main.IMDB_RATING_CACHE_FILE, payload)
        except OSError as exc:
            _main.logger.warning("Could not persist IMDb rating cache: %s", exc)

async def _fetch_imdb_rating(
    title: str,
    year: int,
    cache_key: str,
    media_type: str = "movie",
    tmdb_id: int = 0,
) -> dict[str, _main.Any]:
    global _OMDB_HTTP_CLIENT, _OMDB_RATE_LIMITED_UNTIL
    keys = _main._split_env_keys(_main.os.getenv("OMDB_API_KEYS", ""), _main.os.getenv("OMDB_API_KEY", ""))
    now = int(_main.time.time())
    normalized_type = "tv" if str(media_type or "movie").casefold() in {"tv", "series"} else "movie"
    omdb_type = "series" if normalized_type == "tv" else "movie"
    base = {
        "cacheKey": cache_key,
        "title": title,
        "year": year or None,
        "mediaType": normalized_type,
        "checkedAt": now,
        "lookupVersion": _main.IMDB_RATING_LOOKUP_VERSION,
        "tmdbIdTried": tmdb_id or None,
    }
    if not keys:
        return {**base, "status": "unavailable", "reason": "OMDb API key is not configured"}
    if _main.time.time() < _main._OMDB_RATE_LIMITED_UNTIL:
        return {**base, "status": "rate_limited", "cacheable": False}

    provider = "omdb"
    start_index = _main._PROVIDER_KEY_CURSORS.get(provider, 0) % len(keys)
    ordered = keys[start_index:] + keys[:start_index]
    _main._PROVIDER_KEY_CURSORS[provider] = (start_index + 1) % len(keys)
    timeout = _main.httpx.Timeout(5.0, connect=2.5)
    target = _main._normalize_synopsis_match_title(title)

    async def call_omdb(key: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        global _OMDB_HTTP_CLIENT, _OMDB_RATE_LIMITED_UNTIL
        try:
            if _main._OMDB_HTTP_CLIENT is None or _main._OMDB_HTTP_CLIENT.is_closed:
                _main._OMDB_HTTP_CLIENT = _main._InstrumentedAsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    limits=_main.httpx.Limits(
                        max_connections=8,
                        max_keepalive_connections=4,
                        keepalive_expiry=30.0,
                    ),
                )
            async with _main._OMDB_REQUEST_SEMAPHORE:
                response = await _main._OMDB_HTTP_CLIENT.get(
                    "https://www.omdbapi.com/",
                    params={"apikey": key, **params, "r": "json"},
                    timeout=timeout,
                )
        except _main.httpx.HTTPError as exc:
            _main.logger.info("OMDb rating lookup failed for %r (%s): %s", title, type(exc).__name__)
            return None, "transient"

        if response.status_code == 429:
            _main._provider_key_failure(provider, key, _main._retry_after_seconds(response), "HTTP 429")
            _main._OMDB_RATE_LIMITED_UNTIL = _main.time.time() + 1800
            return None, "rate_limited"
        if response.status_code in {401, 403}:
            _main._provider_key_failure(provider, key, 3600, f"HTTP {response.status_code}")
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
                _main._OMDB_RATE_LIMITED_UNTIL = _main.time.time() + 3600
                return None, "rate_limited"
            if "invalid api key" in lowered_error:
                _main._provider_key_failure(provider, key, 3600, "invalid API key")
                return None, "invalid_key"
        return payload, "ok"

    async def lookup_by_tmdb_imdb_id(key: str) -> dict[str, Any] | None:
        """Use TMDB's IMDb ID as an authoritative fallback when title search misses."""
        if tmdb_id < 1:
            return None
        try:
            if normalized_type == "tv":
                tmdb_details = await _main._tmdb_get_json(
                    f"tv/{tmdb_id}/external_ids", {}, request_timeout=3.0,
                )
            else:
                tmdb_details = await _main._tmdb_get_json(
                    f"movie/{tmdb_id}",
                    {"language": "en-US"},
                    request_timeout=3.0,
                )
        except Exception as exc:
            _main.logger.info("TMDB IMDb-ID fallback failed for %s (%s): %s",
                tmdb_id, type(exc).__name__, title)
            return None

        imdb_id = str(tmdb_details.get("imdb_id") or "").strip()
        if not _main.re.fullmatch(r"tt\d+", imdb_id):
            return None
        details, state = await call_omdb(key, {"i": imdb_id})
        if state == "rate_limited":
            return {**base, "status": "rate_limited", "reason": "OMDb rate limit reached", "cacheable": False}
        if state != "ok" or not details or str(details.get("Response") or "").casefold() == "false":
            return None

        returned_id = str(details.get("imdbID") or "").strip()
        returned_year_match = _main.re.search(r"\b((?:18|19|20|21)\d{2})\b", str(details.get("Year") or ""))
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
        if not _main._provider_key_available(provider, key):
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
                    if not candidate_title or not _main.re.fullmatch(r"tt\d+", candidate_id):
                        continue
                    if _main._normalize_synopsis_match_title(candidate_title) != target:
                        continue
                    year_match = _main.re.search(r"\b((?:18|19|20|21)\d{2})\b", str(row.get("Year") or ""))
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
        returned_year_match = _main.re.search(r"\b((?:18|19|20|21)\d{2})\b", str(payload.get("Year") or ""))
        returned_year = int(returned_year_match.group(1)) if returned_year_match else 0
        candidate = _main._normalize_synopsis_match_title(returned_title)
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
            "imdbId": imdb_id if _main.re.fullmatch(r"tt\d+", imdb_id) else None,
            "imdbUrl": f"https://www.imdb.com/title/{imdb_id}/" if _main.re.fullmatch(r"tt\d+", imdb_id) else None,
        }
        _main._provider_key_success(provider, key)
        return record

    return {**base, "status": "unavailable", "reason": "No usable OMDb key or transient upstream error", "cacheable": False}

async def _get_imdb_rating_record(
    title: str, year: int, media_type: str = "movie", tmdb_id: int = 0
) -> dict[str, _main.Any]:
    normalized_type = "tv" if str(media_type or "movie").casefold() in {"tv", "series"} else "movie"
    cache_key = _main._imdb_rating_cache_key(title, year, normalized_type)
    cached = _main._load_imdb_rating_cache().get(cache_key)
    now = int(_main.time.time())
    if _main.CACHE_ONLY_MODE:
        # Never call OMDb/TMDB; serve whatever is stored, however old.
        _main._record_cache_event("imdb_rating", "hit" if cached else "miss")
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
                _main.IMDB_RATING_LOOKUP_VERSION
                if cached.get("status") != "found" or cached.get("imdbRating") is None
                else 4
            )
            and checked_at
            and now - checked_at < (
                _main.IMDB_RATING_NOT_FOUND_TTL_SECONDS
                if cached.get("status") != "found" or cached.get("imdbRating") is None
                else _main.IMDB_RATING_CACHE_TTL_SECONDS
            )
        ):
            _main._record_cache_event("imdb_rating", "hit")
            return cached

    _main._record_cache_event("imdb_rating", "miss")
    task = _main._IMDB_RATING_INFLIGHT.get(cache_key)
    if task is None or task.done():
        async def fetch_and_save() -> dict[str, Any]:
            result = await _main._fetch_imdb_rating(title, year, cache_key, normalized_type, tmdb_id)
            if result.get("status") in {"found", "not_found"} and result.get("cacheable", True):
                result.pop("cacheable", None)
                await _main._save_imdb_rating_cache_record(result)
            return result

        task = _main.asyncio.create_task(fetch_and_save())
        _main._IMDB_RATING_INFLIGHT[cache_key] = task
    try:
        return await task
    finally:
        if task.done() and _main._IMDB_RATING_INFLIGHT.get(cache_key) is task:
            _main._IMDB_RATING_INFLIGHT.pop(cache_key, None)

@router.get("/api/catalogue/imdb-ratings")
async def api_catalogue_imdb_ratings(
    titles: list[str] = _main.Query(..., min_length=1, max_length=80),
    media_types: list[str] = _main.Query(default=[]),
    tmdb_ids: list[int] = _main.Query(default=[]),
):
    """Return cached IMDb ratings, using TMDB's IMDb ID to resolve ambiguous titles."""
    requested: dict[str, tuple[str, int, str, str, int]] = {}
    for index, raw in enumerate(titles[:80]):
        title, year = _main._parse_imdb_rating_title(raw)
        if not title:
            continue
        supplied_type = media_types[index] if index < len(media_types) else "movie"
        media_type = "tv" if str(supplied_type or "movie").casefold() in {"tv", "series"} else "movie"
        try:
            tmdb_id = max(0, int(tmdb_ids[index])) if index < len(tmdb_ids) else 0
        except (TypeError, ValueError):
            tmdb_id = 0
        key = _main._imdb_rating_cache_key(title, year, media_type)
        requested.setdefault(key, (title, year, media_type, raw, tmdb_id))
    if not requested:
        return {"status": "ready", "results": []}

    results = await _main.asyncio.gather(
        *(
            _main._get_imdb_rating_record(title, year, media_type, tmdb_id)
            for title, year, media_type, _raw, tmdb_id in requested.values()
        ),
        return_exceptions=True,
    )
    output = []
    for (title, year, media_type, raw, _tmdb_id), result in zip(requested.values(), results):
        if isinstance(result, Exception):
            _main.logger.info("IMDb rating lookup failed for %r (%s)", title, type(result).__name__)
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

@router.get("/api/search/metadata/batch")
async def api_search_metadata_batch(
    titles: list[str] = _main.Query(..., min_length=2, max_length=180),
):
    """Resolve search-card metadata from persistent VPS catalogue JSON by exact title/year."""
    requested = list(dict.fromkeys(
        _main.re.sub(r"\s+", " ", str(value or "")).strip()
        for value in titles[:80] if str(value or "").strip()
    ))
    if not requested:
        return {"status": "ready", "source": "VPS catalogue cache", "results": []}

    candidates = []
    seen_rows: set[tuple[str, int, str, str]] = set()
    for raw in _main._read_search_metadata_catalogue_rows():
        item = _main._search_metadata_cache_candidate(raw)
        if not item:
            continue
        identity = (
            _main._normalize_title(item["title"]),
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
        year_match = _main.re.search(r"\s*\(?((?:19|20)\d{2})\)?\s*$", requested_title)
        requested_year = int(year_match.group(1)) if year_match else 0
        clean_title = _main.re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", requested_title).strip()
        target = _main._normalize_title(clean_title)
        if not target:
            continue
        options = [item for item in candidates if _main._normalize_title(item["title"]) == target]
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
            _main._normalize_title(best["title"]),
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
    semaphore = _main.asyncio.Semaphore(4)
    async def hydrate_missing(item_id: int, item: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        async with semaphore:
            metadata = await _main._lookup_search_metadata_by_tmdb_id(item)
            return item_id, metadata

    selected_missing = list(missing_synopsis_candidates.items())[:12]
    if selected_missing:
        hydrated = await _main.asyncio.gather(
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
            _main._persist_search_metadata_cache_rows([persisted_item])
            saved_rows = _main._persist_search_metadata_overview(item_id, metadata["overview"])
            _main.logger.info("Hydrated missing search-card synopsis for TMDB ID %s; persisted rows=%s",
                item_id, saved_rows)

    # Permanently save successful overview hits from any of the catalogue sources.
    matched = [item for item in matched if str(item.get("overview") or "").strip()]
    if matched:
        _main._persist_search_metadata_cache_rows(matched)

    # Never send blank synopsis entries if no details lookup could recover them;
    # this keeps the response list focused on usable saved metadata.
    return {
        "status": "ready",
        "source": "VPS catalogue cache",
        "requested": len(requested),
        "matched": len(matched),
        "results": matched,
    }

@router.get("/api/search")
async def api_search(request: _main.Request, q: str = _main.Query(..., min_length=1), limit: int = _main.Query(50, ge=1, le=50)):
    prepare_intent = request.headers.get("x-prepare-intent", "missing")
    prepare_trace = request.headers.get("x-prepare-trace", "missing")
    if prepare_intent != "missing" or prepare_trace != "missing":
        _main.logger.info(
            "PREPARE_TRACE event=live-search-request intent=%s trace=%s query=%r",
            prepare_intent, prepare_trace, q,
        )
    # Live torrent-provider access is explicitly scoped to this user-triggered endpoint.
    token = _main._MANUAL_TORRENT_SEARCH.set(True)
    try:
        return await _main.search_1337x(q, limit)
    finally:
        _main._MANUAL_TORRENT_SEARCH.reset(token)

@router.get("/api/regional-movies/prebuilt")
async def api_regional_movie_prebuilt(
    request: _main.Request,
    title: str = _main.Query(..., min_length=1, max_length=200),
    year: str = _main.Query("", max_length=4),
):
    """Serve precomputed regional catalogue and torrent details from persistent VPS JSON."""
    prepare_intent = request.headers.get("x-prepare-intent", "missing")
    prepare_trace = request.headers.get("x-prepare-trace", "missing")
    _main.logger.info(
        "PREPARE_TRACE event=prebuilt-request intent=%s trace=%s title=%r year=%s referer=%r",
        prepare_intent, prepare_trace, title, year, request.headers.get("referer", ""),
    )

    def cache_title_key(value: Any) -> str:
        return _main.re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    try:
        payload = _main.json.loads(_main.REGIONAL_MOVIE_PREBUILT_CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "cache_missing", "movie": None, "torrentDetails": None}
    except Exception as exc:
        _main.logger.warning("Could not read regional movie cache: %s", exc)
        return {"status": "cache_unavailable", "movie": None, "torrentDetails": None}
    movies = payload.get("movies") if isinstance(payload, dict) else {}
    key = cache_title_key(title) + "|" + str(year or "")
    entry = (movies or {}).get(key)
    if not entry and not year:
        matches = [value for item_key, value in (movies or {}).items()
                   if item_key.startswith(cache_title_key(title) + "|")]
        entry = matches[0] if len(matches) == 1 else None
    if not isinstance(entry, dict):
        _main._record_cache_event("regional_movie_prebuilt", "miss")
        _main.logger.info(
            "PREPARE_TRACE event=prebuilt-result intent=%s trace=%s cache=miss title=%r year=%s",
            prepare_intent, prepare_trace, title, year,
        )
        return {"status": "not_cached", "movie": None, "torrentDetails": None,
                "builtAt": payload.get("builtAt")}
    _main._record_cache_event("regional_movie_prebuilt", "hit")
    cached_details = entry.get("torrentDetails")
    cached_rows = cached_details.get("results") if isinstance(cached_details, dict) else cached_details
    _main.logger.info(
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
                if _main.MIN_SEARCH_RESULT_SIZE_BYTES <= size_bytes <= _main.MAX_SEARCH_RESULT_SIZE_BYTES:
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
            if _main.MIN_SEARCH_RESULT_SIZE_BYTES <= size_bytes <= _main.MAX_SEARCH_RESULT_SIZE_BYTES:
                kept_results.append(result)
        torrent_details = kept_results

    return {
        "status": "ready",
        "builtAt": payload.get("builtAt"),
        "movie": entry.get("movie"),
        "catalogues": entry.get("catalogues", []),
        "torrentDetails": torrent_details,
    }
