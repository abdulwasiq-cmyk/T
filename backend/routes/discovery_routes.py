"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['api_search_saved_ott', 'api_marvel_catalogue', '_availability_normalize_title', '_availability_title_is_relevant', '_availability_canonical_type', '_fallback_tvmaze_search', '_fallback_omdb_search', 'api_search_availability', '_api_search_availability_impl']

router = APIRouter()

@router.get("/api/catalogue/ott/search")
async def api_search_saved_ott(titles: list[str] = _main.Query(..., min_length=1, max_length=80)):
    """Look up saved OTT enrichment by title from persistent franchise catalogue caches only."""
    _main._record_cache_event("saved_ott_title_search", "lookup")
    # Read the persistent OTT JSON once for the entire title search. The cache
    # file may contain over a thousand rows; parsing the complete file once per
    # catalogue row turns a cache-only lookup into unnecessary repeated I/O.
    try:
        ott_disk = _main.json.loads(_main.TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
        ott_items = ott_disk.get("items", {}) if isinstance(ott_disk, dict) else {}
        if not isinstance(ott_items, dict):
            ott_items = {}
    except (OSError, ValueError, TypeError):
        ott_items = {}

    source_paths = [_main.MARVEL_CATALOGUE_PATH, *_main._EXTRA_CATALOGUE_PATHS.values()]
    saved_rows: list[dict[str, Any]] = []
    seen_rows: set[tuple[str, str]] = set()
    for source_path in source_paths:
        try:
            payload = _main.json.loads(source_path.read_text(encoding="utf-8"))
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
            identity = (_main._normalize_title(_main.re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", title)), year)
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
                cached = _main._tmdb_ott_cache.get((tmdb_id, "IN"))
                if cached and isinstance(cached[1], dict):
                    availability = cached[1]
                if not isinstance(availability, dict):
                    envelope = ott_items.get(_main._tmdb_ott_disk_key((tmdb_id, "IN")))
                    persistent_payload = envelope.get("payload") if isinstance(envelope, dict) else None
                    if isinstance(persistent_payload, dict):
                        availability = persistent_payload
                        _main._tmdb_ott_cache[(tmdb_id, "IN")] = (_main.time.monotonic(), availability)
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
    for row in _main._read_search_metadata_catalogue_rows():
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
            envelope = ott_items.get(_main._tmdb_ott_disk_key((tmdb_id, "IN")))
            availability = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(availability, dict):
            continue
        year_value = row.get("year")
        title_identity = (
            _main._normalize_title(_main.re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", raw_title)),
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
        title = _main.re.sub(r"\s+", " ", str(raw or "")).strip()
        if title:
            requested.append({"title": title, "normalized": _main._normalize_title(_main.re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", title))})

    matches = []
    for request in requested:
        candidates = [row for row in saved_rows
            if _main._normalize_title(_main.re.sub(r"\s*\(?((?:19|20)\d{2})\)?\s*$", "", row["title"])) == request["normalized"]]
        if candidates:
            matches.append({"query": request["title"], "found": True, "entries": candidates[:3]})
        else:
            matches.append({"query": request["title"], "found": False, "entries": []})
    matched_count = sum(1 for item in matches if item.get("found"))
    _main._record_cache_event("saved_ott_title_search", "title_hit" if matched_count else "title_miss")
    _main._record_cache_event("saved_ott_title_search", "matched_rows", sum(len(item.get("entries") or []) for item in matches))
    return {"status": "ready", "results": matches}

@router.get("/api/catalogue/marvel")
async def api_marvel_catalogue(request: _main.Request, page: int = _main.Query(0, ge=0), page_size: int = _main.Query(0, ge=0, le=200)):
    global _marvel_catalogue_task
    if _main.CACHE_ONLY_MODE:
        prebuilt = _main._prebuilt_catalogue_page(request, "marvel", page, page_size)
        if prebuilt is not None:
            return prebuilt
    cached = _main._merge_bundled_marvel_catalogue_cache(_main._read_marvel_catalogue())
    _main._record_cache_event("marvel_catalogue", "hit" if cached else "miss")
    if cached and _main.CACHE_ONLY_MODE:
        return _main._fast_json_response(request, "marvel-page:%d:%d" % (page, page_size), _main._file_signature(_main.MARVEL_CATALOGUE_PATH), lambda: _main._page_catalogue_payload({
            "status": "ready",
            "builtAt": cached.get("builtAt"),
            "completed": int(cached.get("titlesQueried") or len(_main.MARVEL_MOVIE_SEARCHES)),
            "total": int(cached.get("titlesQueried") or len(_main.MARVEL_MOVIE_SEARCHES)),
            "resultCount": int(cached.get("resultCount") or len(cached["results"])),
            "movieCount": int(cached.get("movieCount") or 0),
            "results": cached["results"],
            "refreshing": False,
        }, page, page_size, sort_by_release_date=True))
    if cached:
        # Keep the current cache available immediately while rebuilding it once
        # when its version predates the updated MCU film list.
        if int(cached.get("version") or 0) < _main.MARVEL_CATALOGUE_VERSION:
            if _main._marvel_catalogue_task is None or _main._marvel_catalogue_task.done():
                if not (_main._marvel_catalogue_state.get("status") == "failed" and _main.time.time() < _main._marvel_catalogue_retry_after):
                    _main._marvel_catalogue_state.update({
                        "status": "building",
                        "completed": 0,
                        "total": len(_main.MARVEL_MOVIE_SEARCHES),
                        "resultCount": int(cached.get("resultCount") or len(cached["results"])),
                        "movieCount": int(cached.get("movieCount") or 0),
                        "error": "",
                    })
                    _main._marvel_catalogue_task = _main.asyncio.create_task(_main._build_marvel_catalogue())
        response_payload = {
            "status": "ready",
            "builtAt": cached.get("builtAt"),
            "completed": int(cached.get("titlesQueried") or len(_main.MARVEL_MOVIE_SEARCHES)),
            "total": int(cached.get("titlesQueried") or len(_main.MARVEL_MOVIE_SEARCHES)),
            "resultCount": int(cached.get("resultCount") or len(cached["results"])),
            "movieCount": int(cached.get("movieCount") or 0),
            "results": cached["results"],
            "refreshing": int(cached.get("version") or 0) < _main.MARVEL_CATALOGUE_VERSION,
        }
        if page > 0 and page_size > 0:
            return _main._fast_json_response(
                request,
                "marvel-page:%d:%d" % (page, page_size),
                _main._file_signature(_main.MARVEL_CATALOGUE_PATH),
                lambda: _main._page_catalogue_payload(response_payload, page, page_size, sort_by_release_date=True),
            )
        return response_payload

    if _main._marvel_catalogue_task is None or _main._marvel_catalogue_task.done():
        if _main._marvel_catalogue_state.get("status") == "failed" and _main.time.time() < _main._marvel_catalogue_retry_after:
            return {**_main._marvel_catalogue_state, "results": []}
        _main._marvel_catalogue_state.update({
            "status": "building",
            "completed": 0,
            "total": len(_main.MARVEL_MOVIE_SEARCHES),
            "resultCount": 0,
            "error": "",
        })
        _main._marvel_catalogue_task = _main.asyncio.create_task(_main._build_marvel_catalogue())

    return {
        **_main._marvel_catalogue_state,
        "total": len(_main.MARVEL_MOVIE_SEARCHES),
        "results": [],
    }

def _availability_normalize_title(value: _main.Any) -> str:
    return _main.re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

def _availability_title_is_relevant(query: str, title: _main.Any) -> bool:
    """Reject distant partial-title matches while allowing modest subtitle/region suffixes."""
    target = _main._availability_normalize_title(query)
    candidate = _main._availability_normalize_title(title)
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

def _availability_canonical_type(value: _main.Any) -> str:
    kind = str(value or "").casefold().replace("-", "_").replace(" ", "_")
    if any(token in kind for token in ("tv", "series", "show", "episode")):
        return "tv"
    if any(token in kind for token in ("movie", "film", "feature")):
        return "movie"
    return kind or "unknown"

async def _fallback_tvmaze_search(query: str, timeout: _main.httpx.Timeout) -> list[dict[str, _main.Any]]:
    """Metadata-only fallback used only when Watchmode and TMDB find no title."""
    cache_key = query.casefold()
    cached = _main._TVMAZE_SEARCH_CACHE.get(cache_key)
    if cached and _main.time.time() - cached[0] < _main._WATCHMODE_SEARCH_CACHE_TTL:
        return cached[1]
    try:
        async with _main._InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get("https://api.tvmaze.com/search/shows", params={"q": query})
        if response.status_code != 200:
            return []
        payload = response.json()
    except (_main.httpx.HTTPError, ValueError, TypeError):
        return []
    target = _main._availability_normalize_title(query)
    matches = []
    for row in payload if isinstance(payload, list) else []:
        show = row.get("show") if isinstance(row, dict) else None
        if not isinstance(show, dict) or not show.get("id"):
            continue
        title = str(show.get("name") or "").strip()
        if not title:
            continue
        normalized = _main._availability_normalize_title(title)
        if not _main._availability_title_is_relevant(query, title):
            continue
        premiered = str(show.get("premiered") or "")
        network = show.get("network") or show.get("webChannel") or {}
        matches.append({
            "id": str(show["id"]), "title": title, "year": premiered[:4] or None,
            "type": "tv", "providers": [], "status": "not_confirmed", "source": "TVmaze",
            "tvmazeId": show["id"], "imdbId": (show.get("externals") or {}).get("imdb"),
            "overview": _main.re.sub(r"<[^>]+>", " ", str(show.get("summary") or "")).strip(),
            "network": network.get("name") if isinstance(network, dict) else None,
            "url": show.get("url"),
        })
    matches.sort(key=lambda item: (
        _main._availability_normalize_title(item.get("title")) == target,
        bool(item.get("year")),
    ), reverse=True)
    matches = matches[:5]
    _main._TVMAZE_SEARCH_CACHE[cache_key] = (_main.time.time(), matches)
    return matches

async def _fallback_omdb_search(query: str, timeout: _main.httpx.Timeout) -> list[dict[str, _main.Any]]:
    """OMDb metadata fallback; OMDb does not provide streaming availability."""
    keys = _main._split_env_keys(_main.os.getenv("OMDB_API_KEYS", ""), _main.os.getenv("OMDB_API_KEY", ""))
    if _main.CACHE_ONLY_MODE or not keys:
        return []
    target = _main._availability_normalize_title(query)
    for key in keys:
        try:
            async with _main._InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get("https://www.omdbapi.com/", params={"apikey": key, "s": query})
            if response.status_code != 200:
                continue
            payload = response.json()
        except (_main.httpx.HTTPError, ValueError, TypeError):
            continue
        matches = []
        for row in payload.get("Search") or []:
            title = str(row.get("Title") or "").strip()
            imdb_id = str(row.get("imdbID") or "").strip()
            if not title or not imdb_id:
                continue
            normalized = _main._availability_normalize_title(title)
            if not _main._availability_title_is_relevant(query, title):
                continue
            kind = "tv" if str(row.get("Type") or "").casefold() == "series" else "movie"
            matches.append({
                "id": imdb_id, "title": title, "year": row.get("Year"), "type": kind,
                "providers": [], "status": "not_confirmed", "source": "OMDb",
                "imdbId": imdb_id, "poster": row.get("Poster"),
            })
        if matches:
            matches.sort(key=lambda item: _main._availability_normalize_title(item["title"]) == target, reverse=True)
            return matches[:5]
        # Search returned no titles; try the next configured key only on auth errors
        # is intentionally avoided here so rate limits cannot be bypassed.
        if payload.get("Response") == "False":
            return []
    return []

@router.get("/api/search/availability")
async def api_search_availability(q: str = _main.Query(..., min_length=2, max_length=160)):
    """Bound streaming lookups so slow provider APIs cannot leave the UI scanning forever."""
    query = _main.re.sub(r"\s+", " ", q).strip()
    try:
        # Upstream providers have their own timeouts and retries; apply one total
        # budget to the whole lookup so nested retries cannot accumulate indefinitely.
        return await _main.asyncio.wait_for(_main._api_search_availability_impl(query), timeout=12.0)
    except _main.asyncio.TimeoutError:
        _main.logger.info("Streaming availability lookup timed out for query %r", query)
        return {
            "status": "unavailable",
            "query": query,
            "title": query,
            "year": None,
            "matches": [],
            "providers": [],
            "message": "Streaming availability timed out. Torrent results are still available.",
        }

async def _api_search_availability_impl(q: str) -> dict[str, _main.Any]:
    """Search Watchmode and TMDB first; use TVmaze/OMDb for metadata only if neither finds a title."""
    query = _main.re.sub(r"\s+", " ", q).strip()
    cache_key = query.casefold()
    now = _main.time.time()
    cached = _main._watchmode_search_cache.get(cache_key)
    if cached and now - cached[0] < _main._WATCHMODE_SEARCH_CACHE_TTL:
        _main._record_cache_event("availability_search", "hit")
        return cached[1]

    _main._record_cache_event("availability_search", "miss")
    if _main.CACHE_ONLY_MODE:
        # This endpoint previously searched Watchmode/TMDB directly, bypassing the
        # movie-ID OTT cache-only guard. Reuse saved, title-matched OTT enrichment.
        try:
            saved_payload = await _main.api_search_saved_ott(titles=[query])
        except Exception:
            saved_payload = {"results": []}
        saved_results = saved_payload.get("results") if isinstance(saved_payload, dict) else []
        saved_match = next((
            item for item in saved_results
            if isinstance(item, dict) and _main._availability_normalize_title(item.get("query")) == _main._availability_normalize_title(query)
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
        _main._watchmode_search_cache[cache_key] = (_main.time.time(), result)
        _main._record_cache_event("availability_search", "cache_only_saved_hit" if matches else "cache_only_miss_blocked")
        return result

    timeout = _main.httpx.Timeout(8.0, connect=3.0)
    target = _main._availability_normalize_title(query)
    primary_matches: list[dict[str, Any]] = []
    primary_errors = []

    # Primary source 1: Watchmode. Search both films and TV series automatically.
    if _main.WATCHMODE_API_KEYS:
        try:
            response = await _main._watchmode_get("/search/", params={
                "search_field": "name", "search_value": query, "types": "movie,tv_series,tv_miniseries,tv_movie"
            }, timeout=timeout)
            if response is not None and response.status_code < 400:
                data = response.json()
                titles = data.get("title_results", []) if isinstance(data, dict) else []
                titles = [item for item in titles if isinstance(item, dict) and item.get("id")
                    and _main._availability_title_is_relevant(query, item.get("name"))]
                titles.sort(key=lambda item: (
                    _main._availability_normalize_title(item.get("name")) == target,
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
                        resp = await _main._watchmode_get(
                            f"/title/{_main.quote(wid, safe='')}/sources/",
                            params={"regions": "IN"}, timeout=timeout)
                        raw = resp.json() if resp is not None and resp.status_code < 400 else []
                    except (_main.httpx.HTTPError, ValueError, TypeError):
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

                primary_matches.extend(await _main.asyncio.gather(*(resolve_watchmode(item) for item in selected)))
            else:
                primary_errors.append("Watchmode unavailable")
        except (_main.httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            primary_errors.append("Watchmode " + type(exc).__name__)

    # Primary source 2: search movies and TV concurrently. A movie miss or a
    # movie with no providers must not delay or suppress the TV-series lookup.
    if _main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS:
        media_specs = (
            ("movie", "search/movie", "title", "release_date"),
            ("tv", "search/tv", "name", "first_air_date"),
        )

        async def search_tmdb_media(spec: tuple[str, str, str, str]) -> tuple[str, str, str, list[dict[str, Any]]]:
            media_type, search_path, title_key, date_key = spec
            try:
                payload = await _main._tmdb_get_json(search_path, {
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
                if not title or not _main._availability_title_is_relevant(query, title):
                    continue
                rows.append(row)
            rows.sort(key=lambda row: (
                _main._availability_normalize_title(row.get(title_key)) == target,
                bool(row.get(date_key)),
                float(row.get("popularity") or 0),
            ), reverse=True)
            return media_type, title_key, date_key, rows[:3]

        searched_media = await _main.asyncio.gather(*(search_tmdb_media(spec) for spec in media_specs))

        async def resolve_tmdb_match(
            media_type: str, title_key: str, date_key: str, row: dict[str, Any]
        ) -> dict[str, Any]:
            tmdb_id = int(row["id"])
            try:
                provider_data = await _main._tmdb_get_json(f"{media_type}/{tmdb_id}/watch/providers", {})
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
            primary_matches.extend(await _main.asyncio.gather(*(
                resolve_tmdb_match(media_type, title_key, date_key, row)
                for media_type, title_key, date_key, row in tmdb_candidates
            )))

    # De-duplicate same title/media/ID across sources; merge their provider entries.
    deduped: list[dict[str, Any]] = []
    index_by_identity: dict[tuple[str, str], int] = {}
    for match in primary_matches:
        identity = (_main._availability_canonical_type(match.get("type")),
                    _main._availability_normalize_title(match.get("title")))
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
        fallback_matches = await _main._fallback_tvmaze_search(query, timeout)
        if not fallback_matches:
            fallback_matches = await _main._fallback_omdb_search(query, timeout)
        if fallback_matches:
            result = {"status": "not_confirmed", "query": query, "title": fallback_matches[0]["title"],
                "year": fallback_matches[0].get("year"), "matches": fallback_matches, "providers": [],
                "source": fallback_matches[0].get("source"),
                "message": "Metadata match found, but no streaming provider could be confirmed by Watchmode or TMDB."}
        else:
            result = {"status": "not_found" if not primary_errors else "unavailable", "query": query,
                "title": query, "year": None, "matches": [], "providers": [],
                "message": "No title match found in Watchmode, TMDB, TVmaze, or OMDb."}
        _main._watchmode_search_cache[cache_key] = (_main.time.time(), result)
        return result

    deduped.sort(key=lambda item: (
        _main._availability_normalize_title(item.get("title")) == target,
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
    _main._watchmode_search_cache[cache_key] = (_main.time.time(), result)
    return result
