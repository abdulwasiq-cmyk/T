"""Application functions for this feature area."""

import main as _main
from typing import Any

__all__ = ['_read_marvel_catalogue', '_read_marvel_catalogue_uncached', '_write_marvel_catalogue', '_merge_bundled_marvel_catalogue_cache', '_marvel_result_matches_title', '_extra_result_matches_language', '_build_marvel_catalogue', '_seed_bundled_runtime_caches', '_seed_bundled_catalogue_caches']

def _read_marvel_catalogue() -> dict[str, _main.Any] | None:
    return _main._memoized_file_read("marvel", _main.MARVEL_CATALOGUE_PATH, _main._read_marvel_catalogue_uncached)

def _read_marvel_catalogue_uncached() -> dict[str, _main.Any] | None:
    try:
        payload = _main.json.loads(_main.MARVEL_CATALOGUE_PATH.read_text(encoding="utf-8"))
        if (
            isinstance(payload, dict)
            # Reuse the previous curated snapshot across catalogue-version
            # changes. A version bump must not discard thousands of usable
            # cached torrent rows and trigger a full provider crawl.
            and isinstance(payload.get("version"), int)
            and payload.get("version") <= _main.MARVEL_CATALOGUE_VERSION
            and isinstance(payload.get("results"), list)
            and payload["results"]
        ):
            changed = False
            retained_results = [
                item for item in payload["results"]
                if isinstance(item, dict)
                and not _main._catalogue_row_has_3d_quality(item)
                and not _main._catalogue_row_is_excluded_franchise_title(item)
            ]
            if len(retained_results) != len(payload["results"]):
                changed = True
            sorted_results = _main._sort_cached_catalogue_results(retained_results)
            if sorted_results != payload["results"]:
                changed = True
            payload["results"] = sorted_results
            payload["resultCount"] = len(sorted_results)
            payload["movieCount"] = len({
                _main.re.sub(r"[^a-z0-9]+", "", str(item.get("catalogueTitle") or item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or "").casefold())
                for item in sorted_results
            })
            for item in payload["results"]:
                canonical_poster = _main._canonical_catalogue_poster_url(item)
                if canonical_poster and item.get("posterUrl") != canonical_poster:
                    item["posterUrl"] = canonical_poster
                    changed = True
            if changed:
                _main._write_marvel_catalogue(payload)
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None

def _write_marvel_catalogue(payload: dict[str, _main.Any]) -> None:
    _main.MARVEL_CATALOGUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: _main.Path | None = None
    try:
        with _main.tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="marvel-catalogue-",
            suffix=".tmp",
            dir=str(_main.MARVEL_CATALOGUE_PATH.parent),
            delete=False,
        ) as handle:
            temporary_path = _main.Path(handle.name)
            _main.json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            _main.os.fsync(handle.fileno())
        _main.os.replace(temporary_path, _main.MARVEL_CATALOGUE_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass

def _merge_bundled_marvel_catalogue_cache(
    payload: dict[str, _main.Any] | None,
) -> dict[str, _main.Any] | None:
    """Restore curated, OTT-enriched torrent rows from the bundled snapshot.

    The VPS's persistent cache can be older and narrower than the bundled
    snapshot. Merge only titles in the current curated list, while leaving the
    cache version old so the normal background refresh still runs.
    """
    if payload and (
        int(payload.get("version") or 0) >= _main.MARVEL_CATALOGUE_VERSION
        or int(payload.get("seedMigrationVersion") or 0) >= _main.MARVEL_CATALOGUE_VERSION
    ):
        return payload
    seed_path = _main._CACHE_SEED_DIR / "marvel_catalogue.json"
    try:
        seed_payload = _main.json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return payload

    if not isinstance(seed_payload, dict) or not isinstance(seed_payload.get("results"), list):
        return payload
    if payload and (
        int(payload.get("version") or 0) >= _main.MARVEL_CATALOGUE_VERSION
        or int(payload.get("seedMigrationVersion") or 0) >= _main.MARVEL_CATALOGUE_VERSION
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
        (_main.re.sub(r"[^a-z0-9]+", "", title.casefold()), year): title
        for title, year in _main.MARVEL_MOVIE_SEARCHES
    }

    def row_key(item: dict[str, Any]) -> str:
        digest = str(item.get("infoHash") or "").strip().lower()
        if not _main.re.fullmatch(r"[0-9a-f]{40}", digest):
            digest = _main.info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
        if _main.re.fullmatch(r"[0-9a-f]{40}", digest, _main.re.I):
            return "hash:" + digest.lower()
        normalized = _main._normalize_title(str(item.get("title") or ""))
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
            or _main._catalogue_row_has_3d_quality(seed_item)
            or _main._catalogue_row_is_excluded_franchise_title(seed_item)
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
            _main.re.sub(r"[^a-z0-9]+", "", old_title.casefold()),
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
            _main.re.sub(r"[^a-z0-9]+", "", canonical.casefold()),
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

    if not changed and int(migrated.get("seedMigrationVersion") or 0) >= _main.MARVEL_CATALOGUE_VERSION:
        return migrated

    migrated["results"] = _main._sort_cached_catalogue_results(rows)
    migrated["resultCount"] = len(migrated["results"])
    migrated["movieCount"] = len({
        (
            _main.re.sub(
                r"[^a-z0-9]+", "",
                str(item.get("marvelCatalogueTitle") or item.get("mediaTitle") or item.get("title") or "").casefold(),
            ),
            int(item.get("year") or 0),
        )
        for item in migrated["results"]
        if isinstance(item, dict)
    })
    migrated["titlesQueried"] = len(_main.MARVEL_MOVIE_SEARCHES)
    migrated["seedMigrationVersion"] = _main.MARVEL_CATALOGUE_VERSION
    _main._write_marvel_catalogue(migrated)
    _main.logger.info(
        "Restored %d torrent variants across %d curated Marvel titles from bundled snapshot %s",
        restored_rows,
        len(restored_titles),
        seed_path.name,
    )
    return migrated

def _marvel_result_matches_title(item: dict[str, _main.Any], title: str, year: int) -> bool:
    """Prevent wrong-title and 3D releases from entering shared franchise caches."""
    raw_title = str(item.get("title") or "").strip()
    if not raw_title or _main._catalogue_row_has_3d_quality(item) or _main._catalogue_row_is_excluded_franchise_title(item):
        return False

    actual_tokens = set(_main._search_tokens(raw_title))
    expected_tokens = _main._search_tokens(title)
    if expected_tokens and expected_tokens[0] in {"the", "a", "an"}:
        expected_tokens = expected_tokens[1:]
    if not expected_tokens or not all(token in actual_tokens for token in expected_tokens):
        return False

    # If a release explicitly carries a year, don't cross-wire remakes/sequels.
    title_years = {
        int(value) for value in _main.re.findall(r"\b((?:19|20)\d{2})\b", raw_title)
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

def _extra_result_matches_language(item: dict[str, _main.Any], language: str | None) -> bool:
    """Separate English-original and Hindi/dubbed torrent releases for shared caches."""
    normalized_language = str(language or "").strip().lower()
    if normalized_language not in {"english", "hindi"}:
        return True

    release_text = " ".join(
        str(item.get(field) or "")
        for field in ("title", "quality", "audio", "language", "languages", "audioLanguage", "releaseName")
    )
    hindi_marker = _main.re.compile(
        r"\b(?:hindi|hin|dual[\s._-]*audio|multi[\s._-]*(?:audio|language)|hindi[\s._-]*(?:dubbed|audio))\b",
        _main.re.I,
    )
    other_language_marker = _main.re.compile(
        r"\b(?:hindi|hin|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|"
        r"dual[\s._-]*audio|multi[\s._-]*audio|multi[\s._-]*language|dubbed)\b",
        _main.re.I,
    )
    if normalized_language == "hindi":
        return bool(hindi_marker.search(release_text))
    return not bool(other_language_marker.search(release_text))

async def _build_marvel_catalogue() -> None:
    if _main.CACHE_ONLY_MODE:
        _main.logger.info("CACHE_ONLY_MODE: skipped Marvel catalogue build; using bundled snapshot.")
        return
    global _marvel_catalogue_retry_after
    semaphore = _main.asyncio.Semaphore(_main._MARVEL_CATALOGUE_CONCURRENCY)
    poster_semaphore = _main.asyncio.Semaphore(2)
    rows_by_hash: dict[str, dict[str, Any]] = {}
    found_movies: set[str] = set()

    async def resolve_movie_poster(title: str, year: int) -> str:
        async with poster_semaphore:
            try:
                poster = await _main.asyncio.wait_for(
                    _main._poster_lookup(title, str(year)),
                    timeout=14.0,
                )
                if poster:
                    return poster
            except Exception as exc:
                _main.logger.debug("Marvel poster lookup failed for '%s (%s)': %s", title, year, exc)
        return _main._poster_url_for_release(f"{title} {year}")

    async def search_movie(title: str, year: int) -> list[dict[str, Any]]:
        async with semaphore:
            try:
                query = f"{title} {year}"
                # Use Home's full provider fan-out first. Some providers return
                # zero rows for older/animated Marvel titles, so fall back to
                # the dedicated YTS catalogue search for those titles.
                try:
                    primary = await _main.asyncio.wait_for(
                        _main.search_1337x(query, limit=50, allow_series_fallback=False),
                        timeout=16.0,
                    )
                except Exception as exc:
                    _main.logger.info("Marvel provider search timed out for '%s': %s", query, exc)
                    primary = []

                def validate_items(items: Any) -> list[dict[str, Any]]:
                    accepted: list[dict[str, Any]] = []
                    if not isinstance(items, list):
                        return accepted
                    for original in items:
                        if (
                            not isinstance(original, dict)
                            or not _main._marvel_result_matches_title(original, title, year)
                            or not _main._extra_result_matches_language(original, "english")
                        ):
                            continue
                        item = dict(original)
                        try:
                            size = int(float(item.get("size") or 0))
                            seeders = int(float(item.get("seeders") or 0))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if size < _main.MIN_SEARCH_RESULT_SIZE_BYTES or size > _main.MAX_SEARCH_RESULT_SIZE_BYTES or seeders <= 0:
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
                            item["posterUrl"] = _main._poster_url_for_release(f"{title} {year}")
                        accepted.append(item)
                    return accepted

                accepted = validate_items(primary)
                if len(accepted) < 3:
                    try:
                        yts_items = await _main.asyncio.wait_for(
                            _main.search_yts_movies(query, limit=20),
                            timeout=8.0,
                        )
                        accepted.extend(validate_items(yts_items))
                    except Exception as exc:
                        _main.logger.info("Marvel YTS fallback failed for '%s': %s", query, exc)

                # Deduplicate alternatives per movie before enriching shared poster
                # metadata. Keep the strongest copy of the same info hash.
                per_movie: dict[str, dict[str, Any]] = {}
                for item in accepted:
                    digest = str(item.get("infoHash") or "").strip().lower()
                    if not _main.re.fullmatch(r"[0-9a-f]{40}", digest):
                        digest = _main.info_hash(str(item.get("magnetUrl") or item.get("downloadUrl") or ""))
                    key = "hash:" + digest if _main.re.fullmatch(r"[0-9a-f]{40}", digest, _main.re.I) else (
                        f"title-size:{_main._normalize_title(str(item.get('title') or ''))}|{int(item.get('size') or 0)}"
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
                    found_movies.add(f"{_main._normalize_title(title)}|{year}")
                    poster_url = await resolve_movie_poster(title, year)
                    for item in accepted:
                        item["mediaTitle"] = title
                        item["year"] = year
                        item["posterUrl"] = poster_url
                        item["marvelCatalogueTitle"] = title

                return accepted
            except Exception as exc:
                _main.logger.info("Marvel catalogue search failed for '%s (%s)': %s", title, year, exc)
                return []
            finally:
                _main._marvel_catalogue_state["completed"] = int(
                    _main._marvel_catalogue_state.get("completed") or 0
                ) + 1

    try:
        _main._marvel_catalogue_state.update({
            "status": "building",
            "completed": 0,
            "total": len(_main.MARVEL_MOVIE_SEARCHES),
            "resultCount": 0,
            "movieCount": 0,
            "error": "",
        })
        tasks = [
            _main.asyncio.create_task(search_movie(title, year))
            for title, year in _main.MARVEL_MOVIE_SEARCHES
        ]
        for task in _main.asyncio.as_completed(tasks):
            try:
                items = await task
            except Exception as exc:
                _main.logger.info("Marvel catalogue worker failed: %s", exc)
                items = []

            for item in items:
                digest = str(item.get("infoHash") or "").strip().lower()
                if not _main.re.fullmatch(r"[0-9a-f]{40}", digest):
                    source = str(item.get("magnetUrl") or item.get("downloadUrl") or "")
                    digest = _main.info_hash(source)
                if _main.re.fullmatch(r"[0-9a-f]{40}", digest, _main.re.I):
                    key = "hash:" + digest.lower()
                else:
                    normalized = _main._normalize_title(str(item.get("title") or ""))
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
            _main._marvel_catalogue_state["resultCount"] = len(rows_by_hash)
            _main._marvel_catalogue_state["movieCount"] = len(found_movies)

        # Preserve valid variants for current curated Marvel titles if a provider misses them
        # during this rebuild; new search hits are merged into the existing cache.
        previous_payload = _main._read_marvel_catalogue()
        current_titles = {
            (_main.re.sub(r"[^a-z0-9]+", "", title.casefold()), year): title
            for title, year in _main.MARVEL_MOVIE_SEARCHES
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
                    (_main.re.sub(r"[^a-z0-9]+", "", old_title.casefold()), old_year),
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
                if not _main.re.fullmatch(r"[0-9a-f]{40}", digest):
                    digest = _main.info_hash(str(preserved.get("magnetUrl") or preserved.get("downloadUrl") or ""))
                dedupe_key = "hash:" + digest if _main.re.fullmatch(r"[0-9a-f]{40}", digest, _main.re.I) else (
                    f"title-size:{_main._normalize_title(str(preserved.get('title') or ''))}|{int(preserved.get('size') or 0)}"
                )
                existing = rows_by_hash.get(dedupe_key)
                score = (int(preserved.get("seeders") or 0), int(preserved.get("leechers") or 0))
                existing_score = (int(existing.get("seeders") or 0), int(existing.get("leechers") or 0)) if existing else (-1, -1)
                if existing is None or score > existing_score:
                    rows_by_hash[dedupe_key] = preserved

        results = list(rows_by_hash.values())
        movie_count = len({
            f"{_main._normalize_title(str(item.get('mediaTitle') or item.get('title') or ''))}|{item.get('year') or ''}"
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
            embedded_year = _main.re.search(r"\b((?:19|20)\d{2})\b", source)
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
            "version": _main.MARVEL_CATALOGUE_VERSION,
            "builtAt": _main.datetime.now(_main.timezone.utc).isoformat(),
            "titlesQueried": len(_main.MARVEL_MOVIE_SEARCHES),
            "movieCount": movie_count,
            "resultCount": len(results),
            "results": results,
        }
        _main._merge_saved_catalogue_metadata(payload, _main.MARVEL_CATALOGUE_PATH)
        _main._write_marvel_catalogue(payload)
        _main._marvel_catalogue_state.update({
            "status": "ready",
            "completed": len(_main.MARVEL_MOVIE_SEARCHES),
            "total": len(_main.MARVEL_MOVIE_SEARCHES),
            "resultCount": len(results),
            "movieCount": movie_count,
            "error": "",
            "builtAt": payload["builtAt"],
        })
        _main.logger.info(
            "Built persistent Marvel catalogue: %d movie titles, %d torrent options from %d title searches",
            movie_count,
            len(results),
            len(_main.MARVEL_MOVIE_SEARCHES),
        )
    except Exception as exc:
        _main._marvel_catalogue_retry_after = _main.time.time() + 60
        _main._marvel_catalogue_state.update({
            "status": "failed",
            "error": "The first Marvel catalogue build failed. Please try again in a minute.",
        })
        _main.logger.warning("Marvel catalogue build failed: %s", exc)

def _seed_bundled_runtime_caches() -> None:
    seed_dir = _main._CACHE_SEED_DIR / "runtime"
    for filename, destination in _main._RUNTIME_CACHE_SEED_FILES.items():
        source = seed_dir / filename
        if not source.is_file() or source.stat().st_size == 0:
            _main.logger.info("No bundled runtime snapshot for %s at %s", filename, source)
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)

            # OTT is a shared, append-only lookup cache. Merge the bundled VPS
            # snapshot into the runtime file even when the latter already exists;
            # otherwise an old/non-empty Render file can silently hide thousands
            # of useful entries shipped with the deployment.
            if filename == "tmdb_ott_availability_cache.json":
                bundled = _main.json.loads(source.read_text(encoding="utf-8"))
                bundled_items = bundled.get("items") if isinstance(bundled, dict) else None
                if not isinstance(bundled_items, dict):
                    _main.logger.warning("Ignoring invalid bundled OTT cache snapshot: %s", source)
                    continue

                try:
                    runtime = _main.json.loads(destination.read_text(encoding="utf-8"))
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
                    temp_path = destination.with_name(destination.name + "." + _main.uuid.uuid4().hex + ".tmp")
                    temp_path.write_text(_main.json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
                    _main.os.replace(temp_path, destination)
                    _main.logger.info(
                        "Merged bundled OTT cache snapshot: %s added, %s newer entries applied, %s total entries",
                        added, replaced, len(runtime_items),
                    )
                else:
                    _main.logger.info("OTT cache snapshot already represented in runtime cache (%s entries)", len(runtime_items))
                continue

            if destination.is_file() and destination.stat().st_size > 0:
                continue
            _main.shutil.copy2(source, destination)
            _main.logger.info("Seeded runtime cache %s from GitHub snapshot", filename)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            _main.logger.warning("Could not seed runtime cache %s: %s", filename, exc)

def _seed_bundled_catalogue_caches() -> None:
    destinations = {
        "marvel_catalogue.json": _main.MARVEL_CATALOGUE_PATH,
        "marvel_web_series_catalogue.json": _main.Path(_main.os.getenv("MARVEL_WEB_SERIES_CATALOGUE_PATH", "/app/data/marvel_web_series_catalogue.json")),
        **{config["file"]: _main._EXTRA_CATALOGUE_PATHS[key] for key, config in _main._EXTRA_CATALOGUES.items()},
        "bollywood_torrent_matched_catalogue.json": _main.Path(_main.os.getenv(
            "BOLLYWOOD_TORRENT_CATALOGUE_PATH",
            str(_main.Path(_main.os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_torrent_matched_catalogue.json"),
        )),
        "regional_tv_prebuilt_cache.json": _main.REGIONAL_TV_PREBUILT_CACHE_FILE,
    }
    for filename in _main._CACHE_SEED_FILES:
        source = _main._CACHE_SEED_DIR / filename
        destination = destinations.get(filename)
        if destination is None or not source.is_file() or source.stat().st_size == 0:
            continue
        try:
            if destination.is_file() and destination.stat().st_size > 0:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            _main.shutil.copy2(source, destination)
            _main.logger.info("Seeded catalogue cache %s from bundled snapshot", filename)
        except OSError as exc:
            _main.logger.warning("Could not seed catalogue cache %s: %s", filename, exc)
