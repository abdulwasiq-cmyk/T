"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['_read_extra_catalogue', '_read_extra_catalogue_uncached', '_write_extra_catalogue', '_extra_catalogue_is_stale', '_extra_item_year', '_extra_item_release_date', '_build_extra_catalogue', '_ensure_extra_catalogue_refresh', '_extra_catalogue_monthly_scheduler', '_tmdb_cache_disk_key', '_read_tmdb_persistent_cache', '_write_tmdb_persistent_cache', '_tmdb_payload_needs_synopsis', '_lookup_tmdb_catalogue_synopsis_by_id', '_enrich_tmdb_persistent_catalogue_synopses', '_schedule_tmdb_catalogue_synopsis_enrichment', '_synopsis_enrichment_retry_loop', 'api_tmdb_catalogue_synopsis_enrichment_status', '_tmdb_ott_disk_key', '_tmdb_ott_ttl', '_read_tmdb_ott_persistent_cache', '_write_tmdb_ott_persistent_cache', '_tmdb_watch_provider_rows', '_resolve_watchmode_title_id', '_watchmode_movie_sources', '_watchmode_provider_rows', '_streaming_availability_movie_sources', '_streaming_availability_provider_rows', '_tmdb_fetch_movie_ott_availability', '_tmdb_get_json', '_tmdb_discovered_company_ids', '_tmdb_load_company_ids', '_tmdb_release_date_is_released', '_is_bollywood_comedy_special', '_tmdb_fetch_movie_catalogue']

router = APIRouter()

def _read_extra_catalogue(key: str) -> dict[str, _main.Any] | None:
    return _main._memoized_file_read("extra:" + key, _main._EXTRA_CATALOGUE_PATHS[key], lambda: _main._read_extra_catalogue_uncached(key))

def _read_extra_catalogue_uncached(key: str) -> dict[str, _main.Any] | None:
    path = _main._EXTRA_CATALOGUE_PATHS[key]
    config = _main._EXTRA_CATALOGUES[key]
    try:
        payload = _main.json.loads(path.read_text(encoding="utf-8"))
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
            _main._normalize_title(value)
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
                _main._normalize_title(canonical_title) in excluded_titles
                or _main._catalogue_row_has_3d_quality(item)
                or _main._catalogue_row_is_excluded_franchise_title(item)
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
                    _main._normalize_title(str(
                        item.get("catalogueTitle") or item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or ""
                    ))
                    for item in retained_results
                }),
            }
        sorted_results = _main._sort_cached_catalogue_results(payload["results"])
        if sorted_results != payload["results"]:
            changed = True
        payload["results"] = sorted_results
        payload["resultCount"] = len(sorted_results)
        payload["movieCount"] = len({
            _main._normalize_title(str(item.get("catalogueTitle") or item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or ""))
            for item in sorted_results
        })
        for item in payload["results"]:
            canonical_poster = _main._canonical_catalogue_poster_url(item)
            if canonical_poster and item.get("posterUrl") != canonical_poster:
                item["posterUrl"] = canonical_poster
                changed = True
        if changed:
            _main._write_extra_catalogue(key, payload)
        return payload
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None

def _write_extra_catalogue(key: str, payload: dict[str, _main.Any]) -> None:
    path = _main._EXTRA_CATALOGUE_PATHS[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: _main.Path | None = None
    try:
        with _main.tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=key + "-", suffix=".tmp",
                                         dir=str(path.parent), delete=False) as handle:
            temporary = _main.Path(handle.name)
            _main.json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            _main.os.fsync(handle.fileno())
        _main.os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass

def _extra_catalogue_is_stale(key: str, payload: dict[str, _main.Any] | None) -> bool:
    if not payload:
        return True
    config = _main._EXTRA_CATALOGUES[key]
    try:
        if int(payload.get("version") or 0) != int(config["version"]):
            return True
    except (TypeError, ValueError):
        return True
    days = int(config["refresh_days"])
    if days >= 36500:
        return False
    try:
        built = _main.datetime.fromisoformat(str(payload.get("builtAt") or "").replace("Z", "+00:00"))
        if built.tzinfo is None:
            built = built.replace(tzinfo=_main.timezone.utc)
        return (_main.datetime.now(_main.timezone.utc) - built).total_seconds() >= days * 86400
    except (ValueError, TypeError):
        return True

def _extra_item_year(item: dict[str, _main.Any], fallback: int = 0) -> int:
    try:
        if item.get("year"):
            return int(item["year"])
    except (TypeError, ValueError):
        pass
    match = _main.re.search(r"\b((?:19|20)\d{2})\b", str(item.get("title") or ""))
    return int(match.group(1)) if match else fallback

def _extra_item_release_date(item: dict[str, _main.Any]) -> str:
    """Use full release dates where known; fall back to January 1 of the year."""
    for field in ("releaseDate", "release_date", "releaseDateISO", "firstAirDate"):
        value = str(item.get(field) or "").strip()
        if _main.re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", value):
            try:
                _main.datetime.strptime(value, "%Y-%m-%d")
                return value
            except ValueError:
                pass
    year = _main._extra_item_year(item)
    return f"{year:04d}-01-01" if year else ""

async def _build_extra_catalogue(key: str) -> None:
    if _main.CACHE_ONLY_MODE:
        _main.logger.info("CACHE_ONLY_MODE: skipped build for extra catalogue %s", key)
        return
    config = _main._EXTRA_CATALOGUES[key]
    state = _main._extra_catalogue_states[key]
    fixed_mode = config["mode"] == "fixed"
    jobs = list(config.get("titles", [])) if fixed_mode else []
    semaphore = _main.asyncio.Semaphore(2)
    poster_semaphore = _main.asyncio.Semaphore(2)
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
                            items = await _main.asyncio.wait_for(
                                _main.search_1337x(query, limit=50, allow_series_fallback=False),
                                timeout=18,
                            )
                        except Exception as exc:
                            _main.logger.info("Catalogue %s primary Hindi search failed for %s: %s", key, query, exc)
                            items = []

                        matched_hindi = [
                            item for item in items
                            if isinstance(item, dict)
                            and _main._marvel_result_matches_title(item, title, year)
                            and _main._extra_result_matches_language(item, "hindi")
                        ]
                        if len(matched_hindi) < 3:
                            targeted_queries = ("Hindi", "dual audio", "multi audio")
                            targeted_calls = [
                                _main.asyncio.wait_for(
                                    _main.search_1337x_direct(
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
                            targeted_results = await _main.asyncio.gather(*targeted_calls, return_exceptions=True)
                            for result in targeted_results:
                                if isinstance(result, list):
                                    items.extend(result)
                                elif isinstance(result, Exception):
                                    _main.logger.info(
                                        "Catalogue %s targeted Hindi search failed for %s: %s",
                                        key, title, str(result) or type(result).__name__,
                                    )
                    else:
                        try:
                            items = await _main.asyncio.wait_for(
                                _main.search_1337x(query, limit=50, allow_series_fallback=False),
                                timeout=18,
                            )
                        except Exception as exc:
                            _main.logger.info("Catalogue %s primary search failed for %s: %s", key, query, exc)
                            items = []
                        if len(items) < 2:
                            try:
                                fallback = await _main.asyncio.wait_for(_main.search_yts_movies(query, limit=20), timeout=9)
                                items = list(items or []) + list(fallback or [])
                            except Exception as exc:
                                _main.logger.debug("Catalogue %s YTS fallback failed for %s: %s", key, query, exc)
                else:
                    # Search each known title directly in movie listings instead of
                    # passing it through the broad result filter (which requires year
                    # and all title tokens to occur literally in provider filenames).
                    year_match = _main.re.search(r"\b((?:19|20)\d{2})\b", query)
                    expected_year = int(year_match.group(1)) if year_match else _main.datetime.now(_main.timezone.utc).year
                    provider_query = _main.re.sub(r"\b((?:19|20)\d{2})\b", "", query).strip(" .-_")
                    calls = [
                        _main.asyncio.wait_for(
                            _main.search_1337x_direct(
                                query,
                                limit=50,
                                pages=2,
                                category="Movies",
                                provider_query=provider_query,
                            ),
                            timeout=20,
                        ),
                        _main.asyncio.wait_for(
                            _main.search_limetorrents(query, limit=30, pages=1),
                            timeout=14,
                        ),
                    ]
                    searched = await _main.asyncio.gather(*calls, return_exceptions=True)
                    items = []
                    for value in searched:
                        if isinstance(value, list):
                            items.extend(value)
                        elif isinstance(value, Exception):
                            _main.logger.info(
                                "Catalogue %s provider search failed for %s: %s: %s",
                                key, query, type(value).__name__, str(value) or "<no message>",
                            )

                accepted: list[dict[str, Any]] = []
                for original in items or []:
                    if not isinstance(original, dict):
                        continue
                    item = dict(original)
                    if _main._catalogue_row_has_3d_quality(item) or _main._catalogue_row_is_excluded_franchise_title(item):
                        continue
                    raw_title = str(item.get("title") or "").strip()
                    if not raw_title:
                        continue
                    if fixed_mode:
                        if (
                            not _main._marvel_result_matches_title(item, title, year)
                            or not _main._extra_result_matches_language(item, config.get("language"))
                        ):
                            continue
                        media_title, media_year = title, year
                    else:
                        normalized = _main._normalize_title(_main.unescape(raw_title))
                        query_year_match = _main.re.search(r"\b((?:19|20)\d{2})\b", query)
                        expected_year = int(query_year_match.group(1)) if query_year_match else _main.datetime.now(_main.timezone.utc).year
                        query_title = _main.re.sub(r"\b((?:19|20)\d{2})\b", "", query).strip(" .-_")
                        query_tokens = [
                            token for token in _main._normalize_title(query_title).split()
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
                            or _main.re.search(r"\b(?:S\d{1,2}E\d{1,2}|season\s+\d+|complete\s+series|episode\s+\d+|web[\s.-]?series)\b", raw_title, _main.re.I)
                        ):
                            continue

                        current_year = _main.datetime.now(_main.timezone.utc).year
                        title_years = [int(value) for value in _main.re.findall(r"\b((?:19|20)\d{2})\b", raw_title)]
                        if title_years:
                            media_year = title_years[-1]
                        else:
                            media_year = _main._extra_item_year(item, fallback=expected_year)
                        if media_year < current_year - 1 or media_year > current_year:
                            continue

                        # The query title is the canonical display/group title for all of its torrent variants.
                        media_title = _main.unescape(query_title)
                    try:
                        raw_size = item.get("size_bytes") or item.get("sizeBytes") or item.get("size") or 0
                        if isinstance(raw_size, (int, float)):
                            size = int(raw_size)
                        else:
                            size = _main.parse_size(str(raw_size))
                        seeders = int(float(str(item.get("seeders") or 0).replace(",", "")))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if size < _main.MIN_SEARCH_RESULT_SIZE_BYTES or size > _main.MAX_SEARCH_RESULT_SIZE_BYTES or seeders <= 0:
                        continue
                    if not any(str(item.get(field) or "").strip() for field in ("magnetUrl", "downloadUrl", "sourceUrl", "infoUrl", "infoHash")):
                        continue
                    item["mediaTitle"] = media_title
                    item["year"] = media_year or None
                    if not item.get("posterUrl"):
                        item["posterUrl"] = _main._poster_url_for_release(f"{media_title} {media_year or ''}")
                    accepted.append(item)

                # Resolve one poster per movie group and reuse it across quality variants.
                grouped: dict[str, list[dict[str, Any]]] = {}
                for item in accepted:
                    group_key = _main._normalize_title(str(item.get("mediaTitle") or item.get("title") or ""))
                    grouped.setdefault(group_key, []).append(item)
                for group_key, variants in grouped.items():
                    if not variants:
                        continue
                    poster = str(variants[0].get("posterUrl") or "")
                    if not poster:
                        async with poster_semaphore:
                            try:
                                poster = await _main.asyncio.wait_for(_main._poster_lookup(str(variants[0]["mediaTitle"]), str(variants[0].get("year") or "")), timeout=10)
                            except Exception:
                                poster = ""
                    for item in variants:
                        item["posterUrl"] = poster or _main._poster_url_for_release(str(item.get("mediaTitle") or item.get("title") or ""))
                        item["catalogueKey"] = key
                        item["catalogueTitle"] = str(item.get("mediaTitle") or item.get("title") or "")
                    found_movies.update(group_key + "|" + str(v.get("year") or "") for v in variants for group_key in [_main._normalize_title(str(v.get("mediaTitle") or v.get("title") or ""))])
                return accepted
            finally:
                state["completed"] = int(state.get("completed") or 0) + 1

    try:
        if not fixed_mode:
            jobs = await _discover_latest_catalogue_jobs(key)
            state.update({"status": "building", "completed": 0, "total": len(jobs), "resultCount": 0, "error": ""})
        tasks = [_main.asyncio.create_task(search_job(title, year)) for title, year in jobs]
        for task in _main.asyncio.as_completed(tasks):
            try:
                items = await task
            except Exception as exc:
                _main.logger.info("Catalogue %s search task failed: %s", key, exc)
                items = []
            for item in items:
                digest = str(item.get("infoHash") or "").strip().lower()
                if not _main.re.fullmatch(r"[0-9a-f]{40}", digest):
                    digest = _main.info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
                dedupe = "hash:" + digest if _main.re.fullmatch(r"[0-9a-f]{40}", digest, _main.re.I) else (
                    "title-size:" + _main._normalize_title(str(item.get("title") or "")) + "|" + str(item.get("size") or 0)
                )
                previous = rows.get(dedupe)
                if previous is None or (int(item.get("seeders") or 0), int(item.get("leechers") or 0)) > (int(previous.get("seeders") or 0), int(previous.get("leechers") or 0)):
                    rows[dedupe] = item
            state["resultCount"] = len(rows)
            state["movieCount"] = len(found_movies)

        # Keep cached torrent variants for titles still in the refreshed catalogue
        # when providers return a temporary miss; normalize older title aliases.
        previous_payload = _main._read_extra_catalogue(key)
        canonical_titles = {
            (_main.re.sub(r"[^a-z0-9]+", "", title.casefold()), year): title
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
                old_key = (_main.re.sub(r"[^a-z0-9]+", "", old_title.casefold()), old_year)
                canonical = canonical_titles.get(old_key) or title_aliases.get(old_key)
                if not canonical or _main._catalogue_row_has_3d_quality(old_item) or _main._catalogue_row_is_excluded_franchise_title(old_item):
                    continue
                preserved = dict(old_item)
                preserved["mediaTitle"] = canonical
                preserved["catalogueTitle"] = canonical
                preserved["catalogueKey"] = key
                preserved["year"] = old_year
                digest = str(preserved.get("infoHash") or "").strip().lower()
                if not _main.re.fullmatch(r"[0-9a-f]{40}", digest):
                    digest = _main.info_hash(str(preserved.get("magnetUrl") or preserved.get("downloadUrl") or ""))
                dedupe_key = "hash:" + digest if _main.re.fullmatch(r"[0-9a-f]{40}", digest, _main.re.I) else (
                    "title-size:" + _main._normalize_title(str(preserved.get("title") or "")) + "|" + str(preserved.get("size") or 0)
                )
                existing = rows.get(dedupe_key)
                score = (int(preserved.get("seeders") or 0), int(preserved.get("leechers") or 0))
                existing_score = (int(existing.get("seeders") or 0), int(existing.get("leechers") or 0)) if existing else (-1, -1)
                if existing is None or score > existing_score:
                    rows[dedupe_key] = preserved

        results = list(rows.values())
        movie_count = len({ _main._normalize_title(str(item.get("mediaTitle") or item.get("title") or "")) + "|" + str(item.get("year") or "") for item in results })
        if movie_count < int(config["minimum_movies"]):
            raise RuntimeError(f"Only {movie_count} titles found; keeping the previous catalogue.")
        payload = {"version": config["version"], "builtAt": _main.datetime.now(_main.timezone.utc).isoformat(),
                   "titlesQueried": len(jobs), "movieCount": movie_count, "resultCount": len(results), "results": results}
        # Merge saved metadata before sorting so full release dates are available
        # across Classics, Marvel, DC live-action, DC animated, and language variants.
        _main._merge_saved_catalogue_metadata(payload, _main._EXTRA_CATALOGUE_PATHS[key])
        results.sort(
            key=lambda item: (
                _main._extra_item_release_date(item),
                float(item.get("rating") or 0),
                int(item.get("seeders") or 0),
            ),
            reverse=True,
        )
        _main._write_extra_catalogue(key, payload)
        _main._extra_catalogue_retry_after.pop(key, None)
        state.update({"status": "ready", "completed": len(jobs), "total": len(jobs), "movieCount": movie_count,
                      "resultCount": len(results), "builtAt": payload["builtAt"], "error": ""})
        _main.logger.info("Built %s catalogue: %d titles / %d torrent options", key, movie_count, len(results))
    except Exception as exc:
        _main._extra_catalogue_retry_after[key] = _main.time.monotonic() + 300
        state.update({
            "status": "failed",
            "error": "Catalogue refresh failed; previous cached results were retained.",
            "debugError": f"{type(exc).__name__}: {str(exc)[:240]}",
        })
        _main.logger.exception("Catalogue %s build failed: %s", key, exc)

async def _ensure_extra_catalogue_refresh(key: str) -> None:
    cached = _main._read_extra_catalogue(key)
    if _main._extra_catalogue_is_stale(key, cached):
        if _main.time.monotonic() < _main._extra_catalogue_retry_after.get(key, 0.0):
            return
        task = _main._extra_catalogue_tasks.get(key)
        if task is None or task.done():
            _main._extra_catalogue_tasks[key] = _main.asyncio.create_task(_main._build_extra_catalogue(key))

async def _extra_catalogue_monthly_scheduler() -> None:
    # Do not launch bulk provider traffic during startup. Catalogue endpoints
    # refresh the requested cache on demand; this is only the daily backstop.
    while True:
        await _main.asyncio.sleep(24 * 60 * 60)
        for key, config in _main._EXTRA_CATALOGUES.items():
            if int(config["refresh_days"]) < 36500:
                await _main._ensure_extra_catalogue_refresh(key)

def _tmdb_cache_disk_key(cache_key: tuple[str, int]) -> str:
    return cache_key[0] + ":" + str(cache_key[1])

def _read_tmdb_persistent_cache(cache_key: tuple[str, int]) -> tuple[float, dict[str, _main.Any]] | None:
    try:
        payload = _main.json.loads(_main.TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
        row = (payload.get("catalogues") or {}).get(_main._tmdb_cache_disk_key(cache_key))
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return None
        saved_at = float(row.get("savedAt") or 0)
        if saved_at <= 0:
            return None
        return saved_at, row["payload"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None

def _write_tmdb_persistent_cache(cache_key: tuple[str, int], payload: dict[str, _main.Any]) -> None:
    try:
        _main.TMDB_CATALOGUE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            disk = _main.json.loads(_main.TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(disk, dict):
                disk = {}
        except (OSError, ValueError, TypeError):
            disk = {}
        catalogues = disk.get("catalogues")
        if not isinstance(catalogues, dict):
            catalogues = {}
        catalogues[_main._tmdb_cache_disk_key(cache_key)] = {
            "savedAt": _main.time.time(),
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
        temp_path = _main.TMDB_CATALOGUE_CACHE_FILE.with_name(
            _main.TMDB_CATALOGUE_CACHE_FILE.name + "." + _main.uuid.uuid4().hex + ".tmp"
        )
        temp_path.write_text(_main.json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        _main.os.replace(temp_path, _main.TMDB_CATALOGUE_CACHE_FILE)
    except (OSError, ValueError, TypeError) as exc:
        # Caching is best-effort; it must never turn a successful API response into a failure.
        _main.logger.info("Could not write TMDB persistent cache: %s", exc)

def _tmdb_payload_needs_synopsis(payload: dict[str, _main.Any] | None) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return False
    now = int(_main.time.time())
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
            lookup_version < _main.TMDB_SYNOPSIS_LOOKUP_VERSION
            or not checked_at
            or now - checked_at >= _main._TMDB_CATALOGUE_SYNOPSIS_RECHECK_SECONDS
        ):
            return True
    return False

async def _lookup_tmdb_catalogue_synopsis_by_id(item: dict[str, _main.Any]) -> dict[str, _main.Any] | None:
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
            details = await _main._tmdb_get_json(
                f"movie/{tmdb_id}", {"language": language}, request_timeout=6.0
            )
            successful_response = True
        except Exception as exc:
            _main.logger.info("TMDB details synopsis lookup failed for ID %s (%s): %s",
                tmdb_id, language, type(exc).__name__)
            continue

        overview = _main.re.sub(r"\s+", " ", str(details.get("overview") or "")).strip()
        if overview:
            return {
                "overview": overview,
                "tmdbId": tmdb_id,
                "tmdbUrl": f"https://www.themoviedb.org/movie/{tmdb_id}",
                "synopsisCheckedAt": int(_main.time.time()),
                "synopsisLookupVersion": _main.TMDB_SYNOPSIS_LOOKUP_VERSION,
            }

    # Mark only a successfully checked empty response. Transport failures stay
    # unmarked so the scheduled worker retries them.
    if successful_response:
        return {
            "tmdbId": tmdb_id,
            "tmdbUrl": f"https://www.themoviedb.org/movie/{tmdb_id}",
            "synopsisCheckedAt": int(_main.time.time()),
            "synopsisLookupVersion": _main.TMDB_SYNOPSIS_LOOKUP_VERSION,
        }
    return None

async def _enrich_tmdb_persistent_catalogue_synopses() -> None:
    global _tmdb_catalogue_synopsis_enrichment_task, _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED
    state = _main._TMDB_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE
    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
        state.update({"status": "waiting_for_credentials",
            "error": "TMDB credentials are not configured"})
        return

    state.update({
        "status": "running", "entriesCompleted": 0, "entriesTotal": 0,
        "titlesCompleted": 0, "titlesTotal": 0, "updatedRows": 0, "error": "",
    })
    try:
        try:
            disk = _main.json.loads(_main.TMDB_CATALOGUE_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            disk = {}
        catalogues = disk.get("catalogues", {}) if isinstance(disk, dict) else {}
        if not isinstance(catalogues, dict):
            catalogues = {}

        cache_entries: list[tuple[tuple[str, int], dict[str, Any]]] = []
        jobs: dict[int, dict[str, Any]] = {}
        now = int(_main.time.time())
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
                    lookup_version >= _main.TMDB_SYNOPSIS_LOOKUP_VERSION
                    and checked_at
                    and now - checked_at < _main._TMDB_CATALOGUE_SYNOPSIS_RECHECK_SECONDS
                ):
                    continue
                if tmdb_id > 0:
                    jobs.setdefault(tmdb_id, item)

        state["entriesTotal"] = len(cache_entries)
        state["titlesTotal"] = len(jobs)
        metadata_by_id: dict[int, dict[str, Any]] = {}
        for tmdb_id, item in jobs.items():
            metadata = await _main._lookup_tmdb_catalogue_synopsis_by_id(item)
            if metadata:
                metadata_by_id[tmdb_id] = metadata
            state["titlesCompleted"] += 1
            if state["titlesCompleted"] % 25 == 0:
                _main.logger.info("TMDB persistent catalogue synopsis enrichment: %d/%d titles",
                    state["titlesCompleted"], state["titlesTotal"])
            await _main.asyncio.sleep(0.2)

        # Re-read every entry before merging, preserving any newer catalogue rows
        # written while the metadata lookups were running.
        for cache_key, initial_payload in cache_entries:
            current_entry = _main._read_tmdb_persistent_cache(cache_key)
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
                _main._write_tmdb_persistent_cache(cache_key, payload)
                _main._tmdb_movie_catalogue_cache[cache_key] = (_main.time.monotonic(), payload)
            state["entriesCompleted"] += 1

        state.update({"status": "completed", "error": ""})
        _main.logger.info("TMDB persistent catalogue synopsis enrichment completed: %d titles, %d rows updated",
            state["titlesCompleted"], state["updatedRows"])
    except Exception as exc:
        _main.logger.exception("TMDB persistent catalogue synopsis enrichment failed")
        state.update({"status": "failed", "error": type(exc).__name__ + ": " + str(exc)[:300]})
    finally:
        # If a new cache page arrived while the startup sweep was running, do one
        # more pass after this task releases its slot so that page is not missed.
        if _main._TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED:
            _main._TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED = False
            _main._tmdb_catalogue_synopsis_enrichment_task = _main.asyncio.create_task(
                _main._enrich_tmdb_persistent_catalogue_synopses()
            )

def _schedule_tmdb_catalogue_synopsis_enrichment(payload: dict[str, _main.Any] | None = None) -> None:
    global _tmdb_catalogue_synopsis_enrichment_task, _TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED
    if payload is not None and not _main._tmdb_payload_needs_synopsis(payload):
        return
    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
        _main._TMDB_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE.update({
            "status": "waiting_for_credentials", "error": "TMDB credentials are not configured"})
        return
    if (_main._tmdb_catalogue_synopsis_enrichment_task is not None
            and not _main._tmdb_catalogue_synopsis_enrichment_task.done()):
        _main._TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED = True
        return
    _main._TMDB_CATALOGUE_SYNOPSIS_RESCAN_REQUESTED = False
    _main._tmdb_catalogue_synopsis_enrichment_task = _main.asyncio.create_task(
        _main._enrich_tmdb_persistent_catalogue_synopses()
    )

async def _synopsis_enrichment_retry_loop() -> None:
    """Retry only unresolved synopsis rows on a schedule; completed cache hits stay on disk."""
    global _catalogue_synopsis_enrichment_task
    while True:
        await _main.asyncio.sleep(_main.SYNOPSIS_CACHE_RETRY_INTERVAL_SECONDS)
        if _main._catalogue_synopsis_enrichment_task is None or _main._catalogue_synopsis_enrichment_task.done():
            _main._catalogue_synopsis_enrichment_task = _main.asyncio.create_task(_main._enrich_catalogue_synopses())
        _main._schedule_tmdb_catalogue_synopsis_enrichment()
        tasks = [
            task for task in (
                _main._catalogue_synopsis_enrichment_task,
                _main._tmdb_catalogue_synopsis_enrichment_task,
            )
            if task is not None and not task.done()
        ]
        if tasks:
            await _main.asyncio.gather(*tasks, return_exceptions=True)

@router.get("/api/movies/catalogue-cache/synopsis-enrichment")
async def api_tmdb_catalogue_synopsis_enrichment_status():
    """Return progress for TMDB synopsis enrichment in the persistent catalogue JSON."""
    return dict(_main._TMDB_CATALOGUE_SYNOPSIS_ENRICHMENT_STATE)

def _tmdb_ott_disk_key(cache_key: tuple[int, str]) -> str:
    # Version the persistent key so deployments can deliberately refresh stale
    # provider listings without deleting the historical cache file by hand.
    return "v2:" + str(cache_key[0]) + ":" + cache_key[1]

def _tmdb_ott_ttl(payload: _main.Any) -> float:
    """Providers found -> long cache; nothing found -> short cache so it self-heals."""
    if isinstance(payload, dict) and payload.get("incomplete"):
        # A provider (e.g. Streaming Availability) errored or hit its quota: retry soon.
        return _main.TMDB_OTT_INCOMPLETE_CACHE_SECONDS
    if isinstance(payload, dict) and any(
        payload.get(key) for key in ("streamingProviders", "rentProviders", "buyProviders")
    ):
        return _main.TMDB_OTT_CACHE_SECONDS
    return _main.TMDB_OTT_MISSING_CACHE_SECONDS

def _read_tmdb_ott_persistent_cache(cache_key: tuple[int, str]) -> tuple[float, dict[str, _main.Any]] | None:
    try:
        payload = _main.json.loads(_main.TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
        row = (payload.get("items") or {}).get(_main._tmdb_ott_disk_key(cache_key))
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return None
        saved_at = float(row.get("savedAt") or 0)
        if saved_at <= 0:
            return None
        return saved_at, row["payload"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None

def _write_tmdb_ott_persistent_cache(cache_key: tuple[int, str], payload: dict[str, _main.Any]) -> None:
    try:
        _main.TMDB_OTT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            disk = _main.json.loads(_main.TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(disk, dict):
                disk = {}
        except (OSError, ValueError, TypeError):
            disk = {}
        items = disk.get("items")
        if not isinstance(items, dict):
            items = {}
        items[_main._tmdb_ott_disk_key(cache_key)] = {
            "savedAt": _main.time.time(),
            "payload": payload,
        }
        # Keep every saved OTT lookup permanently. Evicting old entries would
        # cause repeat provider lookups when a previously viewed film is opened.
        disk["items"] = items
        temp_path = _main.TMDB_OTT_CACHE_FILE.with_name(
            _main.TMDB_OTT_CACHE_FILE.name + "." + _main.uuid.uuid4().hex + ".tmp"
        )
        temp_path.write_text(_main.json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        _main.os.replace(temp_path, _main.TMDB_OTT_CACHE_FILE)
    except (OSError, ValueError, TypeError) as exc:
        # A cache write failure must not hide otherwise usable availability data.
        _main.logger.info("Could not write TMDB OTT cache: %s", exc)

def _tmdb_watch_provider_rows(payload: _main.Any) -> list[dict[str, _main.Any]]:
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
    if movie_id in _main._WATCHMODE_RESOLVED_TITLE_IDS:
        return _main._WATCHMODE_RESOLVED_TITLE_IDS[movie_id]
    if not _main.WATCHMODE_API_KEYS:
        return None
    try:
        response = await _main._watchmode_get(
            "/search/",
            params={"search_field": "tmdb_movie_id", "search_value": str(movie_id), "types": "movie"},
            timeout=_main.httpx.Timeout(10.0, connect=4.0),
        )
        if response is None or response.status_code >= 400:
            status = response.status_code if response is not None else "no response"
            _main.logger.info("Watchmode ID resolution failed for TMDB movie %s (%s)", movie_id, status)
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
        _main._WATCHMODE_RESOLVED_TITLE_IDS[movie_id] = resolved
        if resolved is None:
            _main.logger.info("Watchmode has no unambiguous native title ID for TMDB movie %s", movie_id)
        return resolved
    except (_main.httpx.HTTPError, ValueError, TypeError) as exc:
        _main.logger.info("Watchmode ID resolution failed for TMDB movie %s: %s", movie_id, type(exc).__name__)
        return None

async def _watchmode_movie_sources(movie_id: int, region: str) -> list[dict[str, _main.Any]]:
    """Look up sources by TMDB ID, falling back to Watchmode's native title ID."""
    if not _main.WATCHMODE_API_KEYS:
        return []
    try:
        response = await _main._watchmode_get(
            f"/title/movie-{movie_id}/sources/",
            params={"regions": region},
            timeout=_main.httpx.Timeout(10.0, connect=4.0),
        )
        if response is None:
            return []
        if response.status_code < 400:
            payload = response.json()
            if not isinstance(payload, list):
                _main.logger.info("Watchmode returned an unexpected source payload for TMDB movie %s", movie_id)
                return []
            rows = [item for item in payload if isinstance(item, dict)]
            if rows:
                return rows
            # A valid empty response can mean the TMDB-formatted ID did not map
            # to a Watchmode record. Resolve the native ID before concluding empty.
        elif response.status_code != 404:
            _main.logger.info("Watchmode source lookup failed for TMDB movie %s (%s)", movie_id, response.status_code)
            return []

        watchmode_id = await _main._resolve_watchmode_title_id(movie_id)
        if watchmode_id is None:
            return []
        fallback = await _main._watchmode_get(
            f"/title/{watchmode_id}/sources/",
            params={"regions": region},
            timeout=_main.httpx.Timeout(10.0, connect=4.0),
        )
        if fallback is None or fallback.status_code >= 400:
            status = fallback.status_code if fallback is not None else "no response"
            _main.logger.info("Watchmode native-ID source lookup failed for TMDB movie %s (%s)", movie_id, status)
            return []
        payload = fallback.json()
        if not isinstance(payload, list):
            _main.logger.info("Watchmode returned an unexpected native-ID source payload for TMDB movie %s", movie_id)
            return []
        return [item for item in payload if isinstance(item, dict)]
    except (_main.httpx.HTTPError, ValueError, TypeError) as exc:
        _main.logger.info("Watchmode source lookup failed for movie %s in %s: %s", movie_id, region, type(exc).__name__)
        return []

def _watchmode_provider_rows(rows: list[dict[str, _main.Any]], accepted_types: set[str]) -> list[dict[str, _main.Any]]:
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

async def _streaming_availability_movie_sources(movie_id: int, region: str) -> tuple[list[dict[str, _main.Any]], bool]:
    """Return normalized provider rows and whether the lookup failed."""
    if not _main.STREAMING_AVAILABILITY_API_KEY:
        return [], False
    try:
        async with _main._InstrumentedAsyncClient(
            timeout=_main.httpx.Timeout(10.0, connect=4.0),
            follow_redirects=True,
            headers={"Accept": "application/json", "X-API-Key": _main.STREAMING_AVAILABILITY_API_KEY},
        ) as client:
            response = await client.get(
                f"{_main.STREAMING_AVAILABILITY_API_BASE}/shows/movie/{movie_id}",
                params={"country": region.lower(), "series_granularity": "show"},
            )
        if response.status_code >= 300:
            _main.logger.info("Streaming Availability API returned HTTP %s for movie %s", response.status_code, movie_id)
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
    except (_main.httpx.HTTPError, ValueError, TypeError) as exc:
        _main.logger.info("Streaming Availability API lookup failed for movie %s: %s", movie_id, type(exc).__name__)
        return [], True

def _streaming_availability_provider_rows(rows: list[dict[str, _main.Any]], accepted_types: set[str]) -> list[dict[str, _main.Any]]:
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

async def _tmdb_fetch_movie_ott_availability(movie_id: int, region: str) -> dict[str, _main.Any]:
    # Render serves OTT availability from the bundled/persistent cache only.
    # Never spend provider API quota refreshing streaming information at runtime.
    if _main.CACHE_ONLY_MODE:
        cached = _main._tmdb_ott_cache.get((movie_id, region.upper()))
        if cached and isinstance(cached[1], dict):
            _main._record_cache_event("ott", "cache_only_memory_hit")
            return cached[1]
        persistent = _main._read_tmdb_ott_persistent_cache((movie_id, region.upper()))
        if persistent and isinstance(persistent[1], dict):
            _main._record_cache_event("ott", "cache_only_persistent_hit")
            return persistent[1]
        _main._record_cache_event("ott", "cache_only_miss_blocked")
        raise _main.HTTPException(
            status_code=503,
            detail="OTT availability is not present in the local cache; live provider lookups are disabled.",
        )

    _main._record_cache_event("ott", "live_provider_lookup")
    # Prefer Watchmode for legal platform availability when configured. Keep TMDB
    # as a fallback and as the source of regional release-date metadata.
    watchmode_rows, release_result = await _main.asyncio.gather(
        _main._watchmode_movie_sources(movie_id, region),
        _main._tmdb_get_json(f"movie/{movie_id}/release_dates"),
        return_exceptions=True,
    )
    if isinstance(watchmode_rows, Exception):
        watchmode_rows = []
    if isinstance(release_result, Exception):
        _main.logger.info("Could not load digital release dates for TMDB movie %s: %s", movie_id, release_result)
        release_payload: dict[str, Any] = {}
    else:
        release_payload = release_result if isinstance(release_result, dict) else {}

    watchmode_streaming = _main._watchmode_provider_rows(watchmode_rows, {"sub", "free", "ads", "tve", "subscription"})
    watchmode_rent = _main._watchmode_provider_rows(watchmode_rows, {"rent", "rental"})
    watchmode_buy = _main._watchmode_provider_rows(watchmode_rows, {"buy", "purchase"})

    availability_rows: list[dict[str, Any]] = []
    availability_failed = False
    if not (watchmode_streaming or watchmode_rent or watchmode_buy):
        availability_rows, availability_failed = await _main._streaming_availability_movie_sources(movie_id, region)
    availability_streaming = _main._streaming_availability_provider_rows(availability_rows, {"subscription", "free", "addon"})
    availability_rent = _main._streaming_availability_provider_rows(availability_rows, {"rent"})
    availability_buy = _main._streaming_availability_provider_rows(availability_rows, {"buy"})

    # TMDB/JustWatch is the final fallback when neither Watchmode nor Streaming Availability has providers.
    tmdb_provider_result: dict[str, Any] = {}
    tmdb_provider_lookup_failed = False
    if not (watchmode_streaming or watchmode_rent or watchmode_buy or availability_streaming or availability_rent or availability_buy):
        try:
            result = await _main._tmdb_get_json(f"movie/{movie_id}/watch/providers")
            if isinstance(result, dict):
                tmdb_provider_result = result
            else:
                tmdb_provider_lookup_failed = True
        except Exception as exc:
            tmdb_provider_lookup_failed = True
            _main.logger.info("TMDB provider fallback failed for movie %s: %s", movie_id, exc)

    # Don't cache provider outages as an empty availability result.
    if tmdb_provider_lookup_failed and availability_failed and not (watchmode_streaming or watchmode_rent or watchmode_buy):
        raise _main.HTTPException(status_code=503, detail="Streaming availability could not be verified because the availability providers are temporarily unavailable.")
    if tmdb_provider_lookup_failed and not (watchmode_streaming or watchmode_rent or watchmode_buy or availability_streaming or availability_rent or availability_buy):
        raise _main.HTTPException(status_code=503, detail="Streaming availability could not be verified because Watchmode and TMDB fallback are temporarily unavailable.")

    all_regions = (tmdb_provider_result.get("results") or {}) if isinstance(tmdb_provider_result, dict) else {}
    region_payload = all_regions.get(region) or {}
    streaming = watchmode_streaming or availability_streaming or (
        _main._tmdb_watch_provider_rows(region_payload.get("flatrate"))
        + _main._tmdb_watch_provider_rows(region_payload.get("free"))
        + _main._tmdb_watch_provider_rows(region_payload.get("ads"))
    )
    rent = watchmode_rent or availability_rent or _main._tmdb_watch_provider_rows(region_payload.get("rent"))
    buy = watchmode_buy or availability_buy or _main._tmdb_watch_provider_rows(region_payload.get("buy"))
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
            if _main.re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                try:
                    _main.datetime.strptime(day, "%Y-%m-%d")
                    if release_type == "4":
                        digital_dates.append(day)
                    else:
                        # Types 2 and 3 are limited and wide theatrical releases.
                        theatrical_dates.append(day)
                except ValueError:
                    continue

    digital_release_date = min(digital_dates) if digital_dates else None
    theatrical_release_date = min(theatrical_dates) if theatrical_dates else None
    today = _main.datetime.now(_main.timezone.utc).date().isoformat()
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
        "checkedAt": _main.datetime.now(_main.timezone.utc).isoformat(),
        # True when no providers were found AND an upstream lookup failed, so "nothing
        # found" may just be a provider error/quota; it is re-checked within minutes.
        "incomplete": bool(
            not (streaming or rent or buy) and (availability_failed or tmdb_provider_lookup_failed)
        ),
        "source": availability_source,
        "providerAttribution": ("Streaming availability data powered by Watchmode." if availability_source == "Watchmode" else "Streaming availability data powered by Streaming Availability API." if availability_source == "Streaming Availability API" else "Streaming availability data powered by JustWatch via TMDB."),
        "attribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
    }

async def _tmdb_get_json(path: str, params: dict[str, _main.Any] | None = None, *, request_timeout: float = 12.0) -> dict[str, _main.Any]:
    if _main.CACHE_ONLY_MODE:
        raise _main.HTTPException(status_code=503, detail="Cache-only mode: live TMDB lookups are disabled.")
    credentials = [("bearer", token) for token in _main.TMDB_READ_ACCESS_TOKENS] + [("api_key", key) for key in _main.TMDB_API_KEYS]
    if not credentials:
        raise _main.HTTPException(status_code=503, detail="TMDB credentials are not configured. Set TMDB_API_KEYS and/or TMDB_READ_ACCESS_TOKENS.")
    provider = "tmdb"
    start = _main._PROVIDER_KEY_CURSORS.get(provider, 0) % len(credentials)
    ordered = credentials[start:] + credentials[:start]
    _main._PROVIDER_KEY_CURSORS[provider] = (start + 1) % len(credentials)
    query = dict(params or {})
    last_response = None
    transient_failed: list[tuple[str, str]] = []
    last_error = None

    async def attempt(kind: str, credential: str) -> dict[str, Any] | None:
        nonlocal last_response, last_error
        fingerprint = kind + ":" + credential
        identity = _main._provider_key_id(provider, fingerprint)
        if _main._PROVIDER_KEY_COOLDOWNS.get(identity, 0.0) > _main.time.time():
            return None
        headers = {"Accept": "application/json"}
        request_params = dict(query)
        if kind == "bearer":
            headers["Authorization"] = "Bearer " + credential
        else:
            request_params["api_key"] = credential
        try:
            global _TMDB_HTTP_CLIENT
            if _main._TMDB_HTTP_CLIENT is None or _main._TMDB_HTTP_CLIENT.is_closed:
                _main._TMDB_HTTP_CLIENT = _main._InstrumentedAsyncClient(
                    timeout=_main.httpx.Timeout(12.0, connect=4.0),
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                    limits=_main.httpx.Limits(
                        max_connections=8,
                        max_keepalive_connections=4,
                        keepalive_expiry=30.0,
                    ),
                )
            async with _main._TMDB_REQUEST_SEMAPHORE:
                response = await _main._TMDB_HTTP_CLIENT.get(
                    f"{_main.TMDB_API_BASE}/{path.lstrip('/')}",
                    params=request_params,
                    headers=headers,
                    timeout=_main.httpx.Timeout(
                        request_timeout,
                        connect=min(4.0, request_timeout),
                    ),
                )
            last_response = response
            if 200 <= response.status_code < 300:
                _main._provider_key_success(provider, fingerprint)
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                last_error = ValueError("TMDB returned non-object JSON")
                transient_failed.append((kind, credential))
                return None
            if response.status_code == 429:
                # Respect provider quotas instead of switching accounts to bypass them.
                _main._provider_key_failure(provider, fingerprint, _main._retry_after_seconds(response), "HTTP 429")
                raise _main.HTTPException(status_code=503, detail="TMDB rate limit reached; retry after the provider cooldown.")
            if response.status_code == 401:
                _main._provider_key_failure(provider, fingerprint, 3600, "HTTP 401")
                return None
            if response.status_code == 403:
                # A credential may lack permission for this endpoint; cool it down
                # and try the remaining configured credentials before failing.
                _main._provider_key_failure(provider, fingerprint, 3600, "HTTP 403")
                return None
            if response.status_code >= 500:
                transient_failed.append((kind, credential))
                return None
            response.raise_for_status()
            return None
        except _main.httpx.HTTPStatusError as exc:
            raise _main.HTTPException(status_code=502, detail=f"TMDB returned HTTP {exc.response.status_code}.") from exc
        except (_main.httpx.RequestError, ValueError) as exc:
            last_error = exc
            _main.logger.info("TMDB request failed for %s credential %s: %s", path,
                _main._provider_key_id(provider, fingerprint).rsplit(":", 1)[-1], type(exc).__name__)
            transient_failed.append((kind, credential))
            return None

    for kind, credential in ordered:
        payload = await attempt(kind, credential)
        if payload is not None:
            return payload
    if transient_failed:
        await _main.asyncio.sleep(0.75)
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
        raise _main.HTTPException(status_code=503, detail="All eligible TMDB credentials are rate-limited or cooling down; retry shortly.")
    if last_response is not None and last_response.status_code in {401, 403}:
        raise _main.HTTPException(status_code=503, detail="TMDB credentials were rejected or are cooling down. Check the configured keys.")
    _main.logger.warning("TMDB request exhausted available credentials for %s (%s)", path, type(last_error).__name__ if last_error else "no eligible credential")
    raise _main.HTTPException(status_code=503, detail="TMDB is temporarily unavailable or all credentials are cooling down. Cached catalogues will be used when available.")

async def _tmdb_discovered_company_ids(franchise: str) -> list[str]:
    normalized = franchise.strip().lower()
    now = _main.time.monotonic()
    cached = _main._tmdb_company_id_cache.get(normalized)
    if cached and now - cached[0] < _main.TMDB_COMPANY_CACHE_SECONDS:
        _main._record_cache_event("tmdb_company_ids", "memory_hit")
        return cached[1]

    _main._record_cache_event("tmdb_company_ids", "miss")
    task = _main._tmdb_company_inflight.get(normalized)
    if task is None or task.done():
        task = _main.asyncio.create_task(_main._tmdb_load_company_ids(normalized))
        _main._tmdb_company_inflight[normalized] = task
    try:
        return await task
    finally:
        if task.done() and _main._tmdb_company_inflight.get(normalized) is task:
            _main._tmdb_company_inflight.pop(normalized, None)

async def _tmdb_load_company_ids(franchise: str) -> list[str]:
    if franchise == "marvel":
        ids = {"420", "19551", "38679", "2301", "13252"}
        queries = ("Marvel Studios", "Marvel Entertainment", "Marvel Animation", "Marvel Productions")
    else:
        ids = {"9993"}
        queries = ("DC Entertainment", "DC Films", "DC Comics")

    async def search_company(query: str) -> list[str]:
        try:
            payload = await _main._tmdb_get_json("search/company", {"query": query, "page": 1})
        except _main.HTTPException as exc:
            _main.logger.info("TMDB company lookup failed for %s: %s", query, exc.detail)
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

    discovered = await _main.asyncio.gather(*(search_company(query) for query in queries))
    for values in discovered:
        ids.update(values)
    result = sorted(ids, key=lambda value: (value not in {"420", "9993"}, int(value)))[:20]
    _main._tmdb_company_id_cache[franchise] = (_main.time.monotonic(), result)
    return result

def _tmdb_release_date_is_released(value: _main.Any, today: str) -> bool:
    """Return True only when a real ISO release date is on or before today's UTC date."""
    release_text = str(value or "").strip()
    if not _main.re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_text):
        return False
    try:
        release_date = _main.datetime.strptime(release_text, "%Y-%m-%d").date()
        cutoff_date = _main.datetime.strptime(today, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return release_date <= cutoff_date

def _is_bollywood_comedy_special(row: dict[str, _main.Any]) -> bool:
    title = str(row.get("title") or row.get("name") or row.get("original_title") or "").strip()
    title_key = _main.re.sub(r"[^a-z0-9]+", "", title.casefold())
    if title_key in _main._BOLLYWOOD_COMEDY_SPECIAL_TITLES:
        return True
    description = " ".join(
        str(row.get(field) or "")
        for field in ("overview", "tagline", "description")
    )
    return bool(_main._BOLLYWOOD_COMEDY_SPECIAL_DESCRIPTION.search(description))

async def _tmdb_fetch_movie_catalogue(catalogue_key: str, page: int, released_only: bool = True) -> dict[str, _main.Any]:
    today = _main.datetime.now(_main.timezone.utc).date().isoformat()
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
        company_ids = await _main._tmdb_discovered_company_ids(franchise)
        params["with_companies"] = "|".join(company_ids)
        if catalogue_key == "dc-animated":
            params["with_genres"] = "16"
        elif catalogue_key == "dc-live-action":
            params["without_genres"] = "16"
        payload = await _main._tmdb_get_json("discover/movie", params)
    elif catalogue_key in regional_modes:
        language_code, mode = regional_modes[catalogue_key]
        if mode == "trending":
            # TMDB's trending feed uses a different ranking signal from Popular.
            # Filter the feed by original language so the two menus stay distinct.
            payload = await _main._tmdb_get_json("trending/movie/week", {
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
                recent_cutoff = _main.datetime.fromtimestamp(_main.time.time() - 5 * 365 * 86400, _main.timezone.utc).date()
                params["release_date.gte"] = recent_cutoff.isoformat()
            payload = await _main._tmdb_get_json("discover/movie", params)
            if mode == "latest" and not (payload.get("results") or []):
                # TMDB's primary_release_date filter can return an empty page
                # for English-language films with regional-only release dates.
                # Retry using the broader release_date fields while retaining
                # the language and five-year window.
                fallback_params = dict(params)
                fallback_params.pop("primary_release_date.gte", None)
                fallback_params["release_date.gte"] = recent_cutoff.isoformat()
                fallback_params["sort_by"] = "release_date.desc"
                fallback = await _main._tmdb_get_json("discover/movie", fallback_params)
                if fallback.get("results"):
                    payload = fallback
                else:
                    # Last fallback: popularity ranking inside the same date
                    # window. This avoids a permanently empty catalogue when
                    # the release-date sort/filter combination is sparse.
                    fallback_params["sort_by"] = "popularity.desc"
                    fallback = await _main._tmdb_get_json("discover/movie", fallback_params)
                    if fallback.get("results"):
                        payload = fallback
    else:
        raise _main.HTTPException(status_code=404, detail="Unknown movie catalogue.")

    raw_results = payload.get("results") or []

    # TMDB's company-based Marvel Discover feed can omit this special even
    # though it is an officially released Marvel title. Resolve it by exact
    # title/year and merge it into page 1 so it is visible in the main catalogue.
    if catalogue_key == "marvel" and page == 1:
        def punisher_special_key(value: Any) -> str:
            key = _main.re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
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
                special_search = await _main._tmdb_get_json("search/movie", {
                    "query": "The Punisher: One Last Kill",
                    "year": 2026,
                    "include_adult": "false",
                    "language": "en-US",
                    "page": 1,
                })
            except _main.HTTPException as exc:
                _main.logger.info("Could not resolve The Punisher: One Last Kill for Marvel catalogue: %s", exc.detail)
            else:
                special_item = next((
                    item for item in (special_search.get("results") or [])
                    if isinstance(item, dict)
                    and punisher_special_key(item.get("title") or item.get("original_title")) == expected_punisher_key
                    and _main.re.match(r"^2026", str(item.get("release_date") or ""))
                    and (not released_only or _main._tmdb_release_date_is_released(item.get("release_date"), today))
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
        if catalogue_key == "latest-bollywood" and _main._is_bollywood_comedy_special(item):
            continue
        release_date = str(item.get("release_date") or "").strip()
        if released_only and not _main._tmdb_release_date_is_released(release_date, today):
            continue
        year_match = _main.re.match(r"^(\d{4})", release_date)
        poster_path = str(item.get("poster_path") or "").strip()
        backdrop_path = str(item.get("backdrop_path") or "").strip()
        results.append({
            "id": item.get("id"),
            "title": title,
            "year": int(year_match.group(1)) if year_match else None,
            "releaseDate": release_date,
            "overview": str(item.get("overview") or "").strip(),
            "posterUrl": _main.TMDB_IMAGE_BASE + poster_path if poster_path else "",
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
