"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['_load_persistent_tv_catalogue', '_save_persistent_tv_catalogue', '_tv_streaming_providers', 'api_tmdb_marvel_tv_catalogue', 'api_tmdb_dc_live_action_tv_catalogue', '_load_bundled_classics_tv_cache', '_get_classics_tv_catalogue', 'api_tmdb_classics_web_series_catalogue', 'api_tmdb_classics_anime_catalogue', '_read_regional_tv_prebuilt_page', '_save_regional_tv_prebuilt_pages', 'api_tmdb_regional_tv_catalogue']

router = APIRouter()

def _load_persistent_tv_catalogue(cache: dict[str, _main.Any], path: _main.Path) -> dict[str, _main.Any] | None:
    """Load a completed TV catalogue from persistent storage; it has no time-based expiry."""
    store = "tv_catalogue:" + path.stem
    if cache.get("payload"):
        _main._record_cache_event(store, "memory_hit")
        return cache["payload"]
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = _main.json.load(handle)
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("results"), list)
            and payload["results"]
            # Provider enrichment was added after the original permanent cache.
            # Rebuild older cache files once so cards gain streaming-service data.
            and all(isinstance(item, dict) and isinstance(item.get("streamingProviders"), list) for item in payload["results"])
        ):
            cache.update({"expiresAt": float("inf"), "payload": payload})
            _main._record_cache_event(store, "persistent_hit")
            _main._record_cache_event(store, "persistent_rows_served", len(payload["results"]))
            _main.logger.info("Loaded persistent TV catalogue cache %s (%d titles)", path.name, len(payload["results"]))
            return payload
    except (OSError, _main.json.JSONDecodeError) as exc:
        _main.logger.debug("Persistent TV catalogue cache unavailable at %s: %s", path, exc)
    _main._record_cache_event(store, "miss")
    return None

def _save_persistent_tv_catalogue(path: _main.Path, payload: dict[str, _main.Any]) -> None:
    """Atomically persist TV metadata so container restarts never trigger a full TMDB scan."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            _main.json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            _main.os.fsync(handle.fileno())
        _main.os.replace(temporary, path)
        _main.logger.info("Saved persistent TV catalogue cache %s (%d titles)", path.name, len(payload.get("results") or []))
    except OSError as exc:
        _main.logger.warning("Could not persist TV catalogue cache %s: %s", path, exc)

async def _tv_streaming_providers(tmdb_id: int) -> list[dict[str, _main.Any]]:
    """Resolve India streaming/rent/buy providers for one TMDB TV series."""
    if _main.CACHE_ONLY_MODE:
        _main._record_cache_event("tv_streaming_availability", "cache_only_miss_blocked")
        return []
    _main._record_cache_event("tv_streaming_availability", "live_provider_lookup")
    try:
        payload = await _main._tmdb_get_json(f"tv/{tmdb_id}/watch/providers", {})
    except Exception as exc:
        _main.logger.info("TMDB TV provider lookup failed for %s: %s", tmdb_id, exc)
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

@router.get("/api/tv/catalogue/marvel")
async def api_tmdb_marvel_tv_catalogue(page: int = _main.Query(1, ge=1, le=500)):
    """Return cards for the curated Marvel TV/web-series list, enriched from TMDB."""
    cached_payload = _main._load_persistent_tv_catalogue(_main._MARVEL_TV_CATALOGUE_CACHE, _main.MARVEL_TV_CATALOGUE_CACHE_FILE)
    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS) and not cached_payload:
        raise _main.HTTPException(status_code=503, detail="TMDB credentials are not configured.")

    semaphore = _main.asyncio.Semaphore(3)

    async def resolve_series(title: str, year: int) -> dict[str, Any]:
        fallback = {
            "id": f"marvel-tv:{_main.re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')}:{year}",
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
                payload = await _main._tmdb_get_json("search/tv", {
                    "query": title,
                    "first_air_date_year": year,
                    "include_adult": "false",
                    "language": "en-US",
                    "page": 1,
                })
            except _main.HTTPException as exc:
                _main.logger.info("TMDB Marvel TV lookup failed for %s (%s): %s", title, year, exc.detail)
                return fallback
            target = _main._normalize_title(title)
            candidates = [row for row in (payload.get("results") or []) if isinstance(row, dict) and row.get("id")]
            candidates.sort(key=lambda row: (
                _main._normalize_title(str(row.get("title") if is_anime_film else row.get("name") or "")) == target,
                str(row.get("release_date" if is_anime_film else "first_air_date") or "").startswith(str(year)),
                float(row.get("popularity") or 0),
            ), reverse=True)
            if not candidates:
                return fallback
            item = candidates[0]
            air_date = str(item.get("release_date" if is_anime_film else "first_air_date") or "").strip()
            year_match = _main.re.match(r"^(\d{4})", air_date)
            poster_path = str(item.get("poster_path") or "").strip()
            backdrop_path = str(item.get("backdrop_path") or "").strip()
            tmdb_id = int(item["id"])
            return {
                "id": tmdb_id,
                "title": title,
                "year": int(year_match.group(1)) if year_match else year,
                "releaseDate": air_date,
                "overview": str(item.get("overview") or "").strip(),
                "posterUrl": _main.TMDB_IMAGE_BASE + poster_path if poster_path else "",
                "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
                "rating": float(item.get("vote_average") or 0),
                "tmdbUrl": f"https://www.themoviedb.org/tv/{tmdb_id}",
                "streamingProviders": await _main._tv_streaming_providers(tmdb_id),
                "providerLink": "",
                "source": "TMDB",
            }

    if cached_payload:
        # Extend existing persistent catalogues additively. Never discard existing
        # entries: official Marvel titles missing from an older cache are appended.
        results = list(cached_payload.get("results") or [])
        known_titles = {_main._normalize_title(str(row.get("title") or "")) for row in results if isinstance(row, dict)}
        missing = [
            (title, year) for title, year in _main.MARVEL_WEB_SERIES
            if _main._normalize_title(title) not in known_titles
        ]
        if missing and (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
            additions = await _main.asyncio.gather(*(resolve_series(title, year) for title, year in missing))
            results.extend(additions)
            cached_payload = {**cached_payload, "results": results, "totalResults": len(results),
                              "provider": "TMDB + curated Marvel series list"}
            _main._save_persistent_tv_catalogue(_main.MARVEL_TV_CATALOGUE_CACHE_FILE, cached_payload)
        _main._MARVEL_TV_CATALOGUE_CACHE.update({"expiresAt": float("inf"), "payload": cached_payload})
        return cached_payload

    results = await _main.asyncio.gather(*(resolve_series(title, year) for title, year in _main.MARVEL_WEB_SERIES))
    result = {
        "catalogue": "marvel-web-series",
        "page": 1,
        "totalPages": 1,
        "totalResults": len(results),
        "provider": "TMDB + curated Marvel series list",
        "results": results,
    }
    _main._MARVEL_TV_CATALOGUE_CACHE.update({"expiresAt": float("inf"), "payload": result})
    _main._save_persistent_tv_catalogue(_main.MARVEL_TV_CATALOGUE_CACHE_FILE, result)
    return result

@router.get("/api/tv/catalogue/dc-live-action")
async def api_tmdb_dc_live_action_tv_catalogue(page: int = _main.Query(1, ge=1, le=500)):
    """Return curated live-action DC TV series cards enriched from TMDB."""
    cached_payload = _main._load_persistent_tv_catalogue(_main._DC_TV_CATALOGUE_CACHE, _main.DC_TV_CATALOGUE_CACHE_FILE)
    if cached_payload:
        return cached_payload
    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
        raise _main.HTTPException(status_code=503, detail="TMDB credentials are not configured.")
    semaphore = _main.asyncio.Semaphore(3)
    async def resolve_series(title: str, year: int) -> dict[str, Any]:
        fallback = {"id": f"dc-tv:{_main.re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')}:{year}", "title": title, "year": year, "releaseDate": str(year), "overview": "", "posterUrl": "", "backdropUrl": "", "rating": 0.0, "tmdbUrl": "", "streamingProviders": [], "providerLink": "", "source": "Curated DC live-action series list"}
        async with semaphore:
            try:
                payload = await _main._tmdb_get_json("search/tv", {"query": title, "first_air_date_year": year, "include_adult": "false", "language": "en-US", "page": 1})
            except _main.HTTPException as exc:
                _main.logger.info("TMDB DC TV lookup failed for %s (%s): %s", title, year, exc.detail)
                return fallback
            target = _main._normalize_title(title)
            candidates = [row for row in (payload.get("results") or []) if isinstance(row, dict) and row.get("id")]
            candidates.sort(key=lambda row: (_main._normalize_title(str(row.get("name") or "")) == target, str(row.get("first_air_date") or "").startswith(str(year)), float(row.get("popularity") or 0)), reverse=True)
            if not candidates: return fallback
            item = candidates[0]
            air_date = str(item.get("first_air_date") or "").strip()
            year_match = _main.re.match(r"^(\d{4})", air_date)
            poster_path = str(item.get("poster_path") or "").strip()
            backdrop_path = str(item.get("backdrop_path") or "").strip()
            tmdb_id = int(item["id"])
            return {"id": tmdb_id, "title": title, "year": int(year_match.group(1)) if year_match else year, "releaseDate": air_date, "overview": str(item.get("overview") or "").strip(), "posterUrl": _main.TMDB_IMAGE_BASE + poster_path if poster_path else "", "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "", "rating": float(item.get("vote_average") or 0), "tmdbUrl": f"https://www.themoviedb.org/tv/{tmdb_id}", "streamingProviders": await _main._tv_streaming_providers(tmdb_id), "providerLink": "", "source": "TMDB"}
    results = await _main.asyncio.gather(*(resolve_series(title, year) for title, year in _main.DC_WEB_SERIES))
    result = {"catalogue": "dc-live-action-web-series", "page": 1, "totalPages": 1, "totalResults": len(results), "provider": "TMDB + curated DC live-action series list", "results": results}
    _main._DC_TV_CATALOGUE_CACHE.update({"expiresAt": float("inf"), "payload": result})
    _main._save_persistent_tv_catalogue(_main.DC_TV_CATALOGUE_CACHE_FILE, result)
    return result

def _load_bundled_classics_tv_cache(catalogue_key: str) -> dict[str, _main.Any] | None:
    filename = _main._CLASSICS_TV_CACHE_FILES.get(catalogue_key)
    if not filename:
        return None
    candidates = (
        _main._CACHE_SEED_DIR / filename,
        _main.Path(_main.os.getenv("APP_DATA_DIR", "/app/data")) / filename,
    )
    for path in candidates:
        try:
            payload = _main.json.loads(path.read_text(encoding="utf-8"))
            if payload.get("catalogue") == catalogue_key and isinstance(payload.get("results"), list):
                return payload
        except (OSError, ValueError, AttributeError):
            continue
    return None

async def _get_classics_tv_catalogue(catalogue_key: str) -> dict[str, _main.Any]:
    titles = _main._CLASSICS_TV_CATALOGUES.get(catalogue_key)
    if titles is None:
        raise _main.HTTPException(status_code=404, detail="Unknown Classics TV catalogue.")
    cached = _main._CLASSICS_TV_CATALOGUE_CACHE.get(catalogue_key)
    if cached is not None:
        _main._record_cache_event("classics_tv:" + catalogue_key, "memory_hit")
        return cached
    bundled = _main._load_bundled_classics_tv_cache(catalogue_key)
    if bundled is not None:
        _main._record_cache_event("classics_tv:" + catalogue_key, "bundled_hit")
        _main._CLASSICS_TV_CATALOGUE_CACHE[catalogue_key] = bundled
        return bundled
    _main._record_cache_event("classics_tv:" + catalogue_key, "miss")
    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
        raise _main.HTTPException(status_code=503, detail="TMDB credentials are not configured.")
    semaphore = _main.asyncio.Semaphore(3)

    async def resolve_series(title: str, year: int) -> dict[str, Any]:
        fallback = {
            "id": f"classics-tv:{_main.re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')}:{year}",
            "title": title, "year": year, "releaseDate": str(year), "overview": "",
            "posterUrl": "", "backdropUrl": "", "rating": 0.0, "tmdbUrl": "",
            "streamingProviders": [], "providerLink": "", "source": "Curated Classics list",
        }
        async with semaphore:
            try:
                is_anime_film = title == "Akira"
                payload = await _main._tmdb_get_json("search/movie" if is_anime_film else "search/tv", {
                    "query": title,
                    ("year" if is_anime_film else "first_air_date_year"): year,
                    "include_adult": "false", "language": "en-US", "page": 1,
                })
            except _main.HTTPException as exc:
                _main.logger.info("TMDB Classics TV lookup failed for %s (%s): %s", title, year, exc.detail)
                return fallback
            target = _main._normalize_title(title)
            candidates = [row for row in (payload.get("results") or []) if isinstance(row, dict) and row.get("id")]
            candidates.sort(key=lambda row: (
                _main._normalize_title(str(row.get("name") or "")) == target,
                str(row.get("first_air_date") or "").startswith(str(year)),
                float(row.get("popularity") or 0),
            ), reverse=True)
            if not candidates:
                return fallback
            item = candidates[0]
            air_date = str(item.get("first_air_date") or "").strip()
            year_match = _main.re.match(r"^(\d{4})", air_date)
            poster_path = str(item.get("poster_path") or "").strip()
            backdrop_path = str(item.get("backdrop_path") or "").strip()
            tmdb_id = int(item["id"])
            return {
                "id": tmdb_id, "title": title,
                "year": int(year_match.group(1)) if year_match else year,
                "releaseDate": air_date, "overview": str(item.get("overview") or "").strip(),
                "posterUrl": _main.TMDB_IMAGE_BASE + poster_path if poster_path else "",
                "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
                "rating": float(item.get("vote_average") or 0),
                "tmdbUrl": f"https://www.themoviedb.org/{'movie' if is_anime_film else 'tv'}/{tmdb_id}",
                "streamingProviders": [] if is_anime_film else await _main._tv_streaming_providers(tmdb_id),
                "providerLink": "", "source": "TMDB",
            }

    results = await _main.asyncio.gather(*(resolve_series(title, year) for title, year in titles))
    results.sort(key=lambda row: (str(row.get("releaseDate") or row.get("year") or ""), str(row.get("title") or "")), reverse=True)
    result = {
        "catalogue": catalogue_key, "page": 1, "totalPages": 1,
        "totalResults": len(results), "provider": "TMDB + curated Classics list", "results": results,
    }
    _main._CLASSICS_TV_CATALOGUE_CACHE[catalogue_key] = result

    # Persist the resolved catalogue so the manual GitHub snapshot workflow can
    # copy it from APP_DATA_DIR and seed ephemeral deployments with a warm cache.
    filename = _main._CLASSICS_TV_CACHE_FILES[catalogue_key]
    cache_path = _main.Path(_main.os.getenv("APP_DATA_DIR", "/app/data")) / filename
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temporary_path.write_text(_main.json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary_path.replace(cache_path)
        _main.logger.info("Persisted Classics TV catalogue cache %s (%s titles)", filename, len(results))
    except OSError as exc:
        _main.logger.warning("Could not persist Classics TV catalogue cache %s: %s", filename, exc)

    return result

@router.get("/api/tv/catalogue/classics-web-series")
async def api_tmdb_classics_web_series_catalogue():
    return await _main._get_classics_tv_catalogue("classics-web-series")

@router.get("/api/tv/catalogue/classics-anime")
async def api_tmdb_classics_anime_catalogue():
    return await _main._get_classics_tv_catalogue("classics-anime")

def _read_regional_tv_prebuilt_page(catalogue_key: str, page: int) -> dict[str, _main.Any] | None:
    """Read one page from the persistent TV catalogue built by scripts/build_regional_movie_cache.py."""
    try:
        if not _main.REGIONAL_TV_PREBUILT_CACHE_FILE.exists():
            return None
        payload = _main.json.loads(_main.REGIONAL_TV_PREBUILT_CACHE_FILE.read_text(encoding="utf-8"))
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
                        if _main.re.sub(r"[^a-z0-9]+", "", str(row.get("title") or row.get("name") or "").casefold()) not in excluded_titles
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
        _main.logger.warning("Could not read regional TV prebuilt cache for %s page %s: %s", catalogue_key, page, exc)
    return None

def _save_regional_tv_prebuilt_pages(catalogue_key: str, rows: list[dict[str, _main.Any]]) -> None:
    """Store three 18-item pages in the persistent VPS TV catalogue."""
    try:
        _main.REGIONAL_TV_PREBUILT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schemaVersion": 3, "catalogues": {}}
        if _main.REGIONAL_TV_PREBUILT_CACHE_FILE.exists():
            try:
                existing = _main.json.loads(_main.REGIONAL_TV_PREBUILT_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload = existing
            except Exception:
                _main.logger.warning("Regional TV cache unreadable; rebuilding its document")
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
        temporary = _main.REGIONAL_TV_PREBUILT_CACHE_FILE.with_suffix(".json.tmp")
        temporary.write_text(_main.json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(_main.REGIONAL_TV_PREBUILT_CACHE_FILE)
    except Exception as exc:
        _main.logger.warning("Could not save regional TV catalogue %s: %s", catalogue_key, exc)

@router.get("/api/tv/catalogue/{catalogue_key}")
async def api_tmdb_regional_tv_catalogue(catalogue_key: str, page: int = _main.Query(1, ge=1, le=500)):
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
        raise _main.HTTPException(status_code=404, detail="Unknown TV catalogue.")
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
        return _main.re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

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
            name = _main.re.sub(r"[^a-z0-9]+", " ", str(network.get("name") or "").casefold()).strip()
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
                        if air_date and _main.datetime.strptime(air_date[:10], "%Y-%m-%d").date() >= _main.datetime(2015, 1, 1).date():
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
                details = await _main._tmdb_get_json(f"tv/{tmdb_id}", {})
                networks = details.get("networks") if isinstance(details.get("networks"), list) else []
                genres = details.get("genres") if isinstance(details.get("genres"), list) else []
                titles = (details.get("name"), details.get("original_name"), row.get("title"), row.get("name"))
                series_type = str(details.get("type") or row.get("seriesType") or "").casefold()
                if series_type not in {"scripted", "miniseries"}:
                    continue
                air_date = str(details.get("first_air_date") or row.get("releaseDate") or row.get("first_air_date") or "").strip()
                try:
                    if not air_date or _main.datetime.strptime(air_date[:10], "%Y-%m-%d").date() < _main.datetime(2015, 1, 1).date():
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
                _main.logger.info("Skipping cached TV item %s because its metadata could not be verified: %s", row.get("id"), type(exc).__name__)
                continue
            filtered.append(row)
        result = dict(result)
        result["results"] = filtered
        result["totalResults"] = len(filtered)
        return result

    # Use only pages explicitly marked as verified by this endpoint.
    prebuilt = _main._read_regional_tv_prebuilt_page(catalogue_key, page)
    if prebuilt is not None:
        _main._record_cache_event("regional_tv_catalogue", "prebuilt_hit")
        return prebuilt
    _main._record_cache_event("regional_tv_catalogue", "prebuilt_miss")

    country, language, sort_by = configs[catalogue_key]
    cache_key = (catalogue_key, page)
    cached = _main._REGIONAL_TV_DISCOVERY_CACHE.get(cache_key)
    cache_ttl = 900 if catalogue_key.endswith("-trending") else 21600
    if cached and _main.time.monotonic() - cached[0] < cache_ttl and not exclude_indian_broadcast_networks:
        _main._record_cache_event("regional_tv_catalogue", "memory_hit")
        return await filter_cached_networks(cached[1])
    _main._record_cache_event("regional_tv_catalogue", "discovery_miss")
    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
        raise _main.HTTPException(status_code=503, detail="TMDB credentials are not configured.")

    today = _main.datetime.now(_main.timezone.utc).date().isoformat()
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
            payload = await _main._tmdb_get_json("discover/tv", {
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
            tv_lookup_semaphore = _main.asyncio.Semaphore(8)

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
                    parsed_air_date = _main.datetime.strptime(air_date[:10], "%Y-%m-%d").date()
                    if parsed_air_date < _main.datetime(2015, 1, 1).date() or parsed_air_date > _main.datetime.now(_main.timezone.utc).date():
                        return None
                except ValueError:
                    return None

                async with tv_lookup_semaphore:
                    try:
                        details = await _main._tmdb_get_json(f"tv/{tmdb_id}", {})
                    except Exception as exc:
                        _main.logger.info("Skipping TV item %s because series type could not be verified: %s", tmdb_id, type(exc).__name__)
                        return None
                    series_type = str(details.get("type") or item.get("type") or "").casefold()
                    if series_type not in {"scripted", "miniseries"}:
                        return None

                    networks = details.get("networks") if isinstance(details.get("networks"), list) else []
                    genres = details.get("genres") if isinstance(details.get("genres"), list) else []
                    title_candidates = (details.get("name"), details.get("original_name"), item.get("name"), item.get("original_name"))
                    title_keys = {
                        _main.re.sub(r"[^a-z0-9]+", "", str(title or "").casefold())
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
                        providers = await _main._tv_streaming_providers(tmdb_id)
                    except Exception as exc:
                        _main.logger.info("Skipping TV item %s because OTT availability could not be verified: %s", tmdb_id, type(exc).__name__)
                        return None
                    providers = [
                        provider for provider in providers
                        if str(provider.get("type") or "").casefold() in {"subscription", "free", "ads"}
                    ]
                    if not providers:
                        return None

                poster = str(item.get("poster_path") or details.get("poster_path") or "").strip()
                backdrop = str(item.get("backdrop_path") or details.get("backdrop_path") or "").strip()
                year_match = _main.re.match(r"^(\d{4})", air_date)
                return {
                    "id": tmdb_id,
                    "title": str(item.get("name") or details.get("name") or "").strip(),
                    "year": int(year_match.group(1)) if year_match else None,
                    "releaseDate": air_date,
                    "overview": str(item.get("overview") or details.get("overview") or "").strip(),
                    "posterUrl": _main.TMDB_IMAGE_BASE + poster if poster else "",
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

            verified_rows = await _main.asyncio.gather(
                *(verify_tv_item(item) for item in payload.get("results", [])),
                return_exceptions=True,
            )
            for row in verified_rows:
                if isinstance(row, dict) and row.get("id") not in seen_ids:
                    eligible_rows.append(row)
                    seen_ids.add(int(row["id"]))
            source_page += 1
    except _main.HTTPException:
        raise

    _main._save_regional_tv_prebuilt_pages(catalogue_key, eligible_rows)
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
    _main._REGIONAL_TV_DISCOVERY_CACHE[cache_key] = (_main.time.monotonic(), result)
    return result
