"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['_synopsis_cache_paths', '_synopsis_row_identity', '_catalogue_metadata_value_present', '_catalogue_metadata_tmdb_id', '_merge_catalogue_metadata_profile', '_merge_saved_catalogue_metadata', '_write_synopsis_cache', '_normalize_synopsis_match_title', '_lookup_watchmode_catalogue_synopsis', '_lookup_catalogue_synopsis', '_enrich_catalogue_synopses', 'api_catalogue_synopsis_enrichment_status', '_warm_tmdb_ott_memory_cache']

router = APIRouter()

def _synopsis_cache_paths() -> list[tuple[str, _main.Path]]:
    paths = [("marvel", _main.MARVEL_CATALOGUE_PATH)]
    paths.extend((key, path) for key, path in _main._EXTRA_CATALOGUE_PATHS.items())
    # De-duplicate aliases that may intentionally point to the same configured path.
    seen: set[str] = set()
    unique: list[tuple[str, Path]] = []
    for name, path in paths:
        identity = str(path.resolve())
        if identity not in seen:
            seen.add(identity)
            unique.append((name, path))
    return unique

def _synopsis_row_identity(item: dict[str, _main.Any]) -> tuple[str, int]:
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
        match = _main.re.search(r"\b(19\d{2}|20\d{2})\b", title)
        if match:
            year = int(match.group(1))
    return _main._normalize_title(title), year

def _catalogue_metadata_value_present(field: str, value: _main.Any) -> bool:
    if value in (None, "", []):
        return False
    # Zero-value metadata should not prevent a useful saved value being reused.
    if field in {"tmdbId", "rating", "synopsisCheckedAt", "synopsisLookupVersion"}:
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False
    return True

def _catalogue_metadata_tmdb_id(item: dict[str, _main.Any]) -> int:
    try:
        return int(item.get("tmdbId") or item.get("id") or 0)
    except (TypeError, ValueError):
        return 0

def _merge_catalogue_metadata_profile(
    target: dict[str, _main.Any],
    source: dict[str, _main.Any],
) -> None:
    for field in _main._CATALOGUE_PERSISTENT_METADATA_FIELDS:
        value = source.get(field)
        if (
            not _main._catalogue_metadata_value_present(field, target.get(field))
            and _main._catalogue_metadata_value_present(field, value)
        ):
            target[field] = value

def _merge_saved_catalogue_metadata(
    payload: dict[str, _main.Any],
    destination_path: _main.Path,
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
        _main._merge_catalogue_metadata_profile(profile, row)

    seen_paths: set[str] = set()
    for _name, cache_path in _main._synopsis_cache_paths():
        try:
            canonical_path = str(cache_path.resolve())
        except OSError:
            canonical_path = str(cache_path)
        if canonical_path in seen_paths:
            continue
        seen_paths.add(canonical_path)

        try:
            saved_payload = _main.json.loads(cache_path.read_text(encoding="utf-8"))
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
                for field in _main._CATALOGUE_PERSISTENT_METADATA_FIELDS
                if _main._catalogue_metadata_value_present(field, saved_row.get(field))
            }
            if not profile:
                continue

            tmdb_id = _main._catalogue_metadata_tmdb_id(saved_row)
            if tmdb_id:
                collect_profile(metadata_by_tmdb_id, tmdb_id, profile)

            identity = _main._synopsis_row_identity(saved_row)
            if identity[0]:
                collect_profile(metadata_by_identity, identity, profile)

    updated_rows = 0
    for row in fresh_rows:
        if not isinstance(row, dict):
            continue

        row_tmdb_id = _main._catalogue_metadata_tmdb_id(row)
        identity = _main._synopsis_row_identity(row)
        profile: dict[str, Any] = {}

        if row_tmdb_id and row_tmdb_id in metadata_by_tmdb_id:
            _main._merge_catalogue_metadata_profile(profile, metadata_by_tmdb_id[row_tmdb_id])

        identity_profile = metadata_by_identity.get(identity) if identity[0] else None
        # If both records already have authoritative but conflicting TMDB IDs,
        # don't transfer the synopsis based on title similarity alone.
        identity_tmdb_id = _main._catalogue_metadata_tmdb_id(identity_profile or {})
        conflicting_ids = bool(row_tmdb_id and identity_tmdb_id and row_tmdb_id != identity_tmdb_id)
        if identity_profile and not conflicting_ids:
            _main._merge_catalogue_metadata_profile(profile, identity_profile)

        before = {
            field: row.get(field)
            for field in _main._CATALOGUE_PERSISTENT_METADATA_FIELDS
        }
        _main._merge_catalogue_metadata_profile(row, profile)
        if any(row.get(field) != before[field] for field in _main._CATALOGUE_PERSISTENT_METADATA_FIELDS):
            updated_rows += 1

    if updated_rows:
        _main.logger.info(
            "Preserved previously saved TMDB metadata for %d rows before rebuilding %s",
            updated_rows,
            destination_path.name,
        )
    return updated_rows

def _write_synopsis_cache(path: _main.Path, payload: dict[str, _main.Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: _main.Path | None = None
    try:
        with _main.tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=path.stem + "-synopsis-",
            suffix=".tmp", dir=str(path.parent), delete=False,
        ) as handle:
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

def _normalize_synopsis_match_title(value: _main.Any) -> str:
    """Normalize harmless catalogue/provider title variants before matching metadata."""
    normalized = _main._normalize_title(str(value or ""))
    # Some sources omit a leading article and spell numerals as words; normalize
    # these common variants without allowing broad fuzzy matches to unrelated films.
    normalized = _main.re.sub(r"^(?:the|a|an)\s+", "", normalized)
    normalized = _main.re.sub(r"\bfour\b", "4", normalized)
    return normalized

async def _lookup_watchmode_catalogue_synopsis(
    title: str,
    year: int,
    tmdb_id: int = 0,
) -> tuple[dict[str, _main.Any] | None, bool]:
    """Fetch Watchmode plot_overview as a fallback; return whether lookup completed."""
    if not _main.WATCHMODE_API_KEYS:
        return None, False

    query = _main.re.sub(r"\s+", " ", title or "").strip()
    completed = False
    timeout = _main.httpx.Timeout(8.0, connect=3.0)

    def make_metadata(details: dict[str, Any], fallback_tmdb_id: int = 0) -> dict[str, Any] | None:
        overview = _main.re.sub(
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
            year_match = _main.re.match(r"^(\d{4})", release_date)
            if year_match:
                resolved_year = int(year_match.group(1))

        metadata: dict[str, Any] = {
            "overview": overview,
            "synopsisSource": "Watchmode",
            "synopsisCheckedAt": int(_main.time.time()),
            "synopsisLookupVersion": _main.TMDB_SYNOPSIS_LOOKUP_VERSION,
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
            response = await _main._watchmode_get(
                f"/title/{_main.quote(identifier, safe='')}/details/",
                params={"language": "en"},
                timeout=timeout,
            )
        except Exception as exc:
            _main.logger.info("Watchmode synopsis lookup failed for %s: %s", identifier, type(exc).__name__)
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
        response = await _main._watchmode_get(
            "/search/",
            params={
                "search_field": "name",
                "search_value": query,
                "types": "movie,tv_movie,tv_miniseries,tv_special,short_film",
            },
            timeout=timeout,
        )
    except Exception as exc:
        _main.logger.info("Watchmode synopsis title search failed for %r: %s", query, type(exc).__name__)
        return None, False

    if response is None or response.status_code >= 400:
        return None, False

    try:
        data = response.json()
    except (ValueError, TypeError):
        return None, False

    title_results = data.get("title_results", []) if isinstance(data, dict) else []
    target = _main._normalize_synopsis_match_title(query)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for candidate in title_results if isinstance(title_results, list) else []:
        if not isinstance(candidate, dict) or not candidate.get("id"):
            continue
        candidate_title = str(candidate.get("name") or candidate.get("title") or "").strip()
        if not candidate_title or _main._normalize_synopsis_match_title(candidate_title) != target:
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
) -> dict[str, _main.Any] | None:
    """Resolve a synopsis from TMDB first, then use Watchmode's plot overview."""
    query = _main.re.sub(r"\s+", " ", title).strip()
    if not query and not tmdb_id:
        return None

    now = int(_main.time.time())

    def details_to_metadata(
        details: dict[str, Any],
        resolved_id: int,
        fallback_title: str,
        fallback_year: int,
        fallback_row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = fallback_row or {}
        release_date = str(details.get("release_date") or fallback.get("release_date") or "")
        year_match = _main.re.match(r"^(\d{4})", release_date)
        resolved_year = int(year_match.group(1)) if year_match else (fallback_year or None)
        poster_path = str(details.get("poster_path") or fallback.get("poster_path") or "").strip()
        backdrop_path = str(details.get("backdrop_path") or fallback.get("backdrop_path") or "").strip()
        overview = _main.re.sub(r"\s+", " ", str(details.get("overview") or fallback.get("overview") or "")).strip()
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
            "posterUrl": _main.TMDB_IMAGE_BASE + poster_path if poster_path else "",
            "backdropUrl": "https://image.tmdb.org/t/p/w780" + backdrop_path if backdrop_path else "",
            "tmdbUrl": f"https://www.themoviedb.org/movie/{int(details.get('id') or resolved_id)}",
            "releaseDate": release_date,
            "rating": float(details.get("vote_average") or fallback.get("vote_average") or 0),
            "genres": genres if isinstance(genres, list) else [],
            "synopsisSource": "TMDB" if overview else "",
            "synopsisCheckedAt": now,
            "synopsisLookupVersion": _main.TMDB_SYNOPSIS_LOOKUP_VERSION,
        }

    async def watchmode_fallback() -> tuple[dict[str, Any] | None, bool]:
        return await _main._lookup_watchmode_catalogue_synopsis(query, year, tmdb_id)

    if tmdb_id > 0:
        successful_details_response = False
        details: dict[str, Any] = {}
        for language in ("en-US", "en"):
            try:
                response = await _main._tmdb_get_json(
                    f"movie/{tmdb_id}", {"language": language}, request_timeout=6.0
                )
            except Exception as exc:
                _main.logger.info(
                    "Synopsis details lookup failed for %s (%s): %s",
                    tmdb_id, language, type(exc).__name__,
                )
                continue
            successful_details_response = True
            details = response
            if _main.re.sub(r"\s+", " ", str(details.get("overview") or "")).strip():
                break

        if successful_details_response:
            metadata = details_to_metadata(details, tmdb_id, query or str(details.get("title") or ""), year)
            if metadata.get("overview"):
                return metadata
            watchmode_metadata, watchmode_completed = await watchmode_fallback()
            if watchmode_metadata:
                return watchmode_metadata
            if _main.WATCHMODE_API_KEYS and not watchmode_completed:
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
        result = await _main._tmdb_get_json("search/movie", params, request_timeout=6.0)
    except Exception as exc:
        _main.logger.info("Synopsis cache lookup failed for %r (%s): %s", query, year, type(exc).__name__)
        watchmode_metadata, _ = await watchmode_fallback()
        return watchmode_metadata

    rows = [row for row in (result.get("results") or []) if isinstance(row, dict) and row.get("id")]
    target = _main._normalize_synopsis_match_title(query)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        candidate_title = str(row.get("title") or row.get("original_title") or "").strip()
        if _main._normalize_synopsis_match_title(candidate_title) != target:
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
        if _main.WATCHMODE_API_KEYS and not watchmode_completed:
            return None
        return {"synopsisCheckedAt": now, "synopsisLookupVersion": _main.TMDB_SYNOPSIS_LOOKUP_VERSION}

    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    row = candidates[0][2]
    resolved_id = int(row["id"])
    details: dict[str, Any] = row
    overview = _main.re.sub(r"\s+", " ", str(row.get("overview") or "")).strip()

    if not overview:
        successful_details_response = False
        for language in ("en-US", "en"):
            try:
                response = await _main._tmdb_get_json(
                    f"movie/{resolved_id}", {"language": language}, request_timeout=6.0
                )
            except Exception as exc:
                _main.logger.info(
                    "Synopsis details lookup failed for %s (%s): %s",
                    resolved_id, language, type(exc).__name__,
                )
                continue
            details = response
            successful_details_response = True
            overview = _main.re.sub(r"\s+", " ", str(details.get("overview") or "")).strip()
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
    if _main.WATCHMODE_API_KEYS and not watchmode_completed:
        metadata["synopsisLookupVersion"] = 0
    return metadata

async def _enrich_catalogue_synopses() -> None:
    """Fill synopsis metadata into every persistent movie-catalogue JSON without rebuilding torrent rows."""
    state = _main._CATALOGUE_SYNOPSIS_ENRICHMENT_STATE
    paths = _main._synopsis_cache_paths()
    state.update({
        "status": "running", "filesCompleted": 0, "filesTotal": len(paths),
        "titlesCompleted": 0, "titlesTotal": 0, "updatedRows": 0, "error": "",
    })
    if not (_main.TMDB_READ_ACCESS_TOKENS or _main.TMDB_API_KEYS):
        state.update({"status": "waiting_for_credentials", "error": "TMDB credentials are not configured"})
        return

    # First collect unique missing-synopsis titles across every cache. Repeated torrent
    # releases for the same film share one TMDB request and one metadata result.
    jobs: dict[tuple[str, int], tuple[str, int, int]] = {}
    file_rows: list[tuple[str, Path, dict[str, Any]]] = []
    now = int(_main.time.time())
    for name, path in paths:
        try:
            payload = _main.json.loads(path.read_text(encoding="utf-8"))
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
                lookup_version >= _main.TMDB_SYNOPSIS_LOOKUP_VERSION
                and checked_at
                and now - checked_at < _main._CATALOGUE_SYNOPSIS_RECHECK_SECONDS
            ):
                continue
            identity = _main._synopsis_row_identity(item)
            normalized_title = identity[0]
            if not normalized_title:
                continue
            title = str(
                item.get("catalogueTitle") or item.get("marvelCatalogueTitle")
                or item.get("mediaTitle") or item.get("title") or ""
            ).strip()
            tmdb_id = _main._catalogue_metadata_tmdb_id(item)
            existing_job = jobs.get(identity)
            if existing_job is None or (not existing_job[2] and tmdb_id):
                jobs[identity] = (title, identity[1], tmdb_id)

    state["titlesTotal"] = len(jobs)
    metadata_by_identity: dict[tuple[str, int], dict[str, Any]] = {}
    # Conservative pacing avoids a burst against TMDB. Rate-limit responses are
    # respected by _tmdb_get_json and are not bypassed by trying other accounts.
    for identity, (title, year, tmdb_id) in jobs.items():
        metadata = await _main._lookup_catalogue_synopsis(title, year, tmdb_id)
        if metadata:
            metadata_by_identity[identity] = metadata
        state["titlesCompleted"] += 1
        if state["titlesCompleted"] % 25 == 0:
            _main.logger.info("Catalogue synopsis enrichment: %d/%d titles processed",
                state["titlesCompleted"], state["titlesTotal"])
        await _main.asyncio.sleep(0.25)

    # Re-read each file before writing, so a catalogue rebuilt during enrichment
    # keeps its fresh torrent rows. Only synopsis fields are merged into matching rows.
    for name, path, _initial_payload in file_rows:
        try:
            current = _main.json.loads(path.read_text(encoding="utf-8"))
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
            identity = _main._synopsis_row_identity(item)
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
                _main._write_synopsis_cache(path, current)
                _main.logger.info("Saved synopsis enrichment for catalogue cache %s", path.name)
            except OSError as exc:
                _main.logger.warning("Could not save synopsis-enriched cache %s: %s", path.name, exc)
        state["filesCompleted"] += 1

    state["status"] = "completed"
    _main.logger.info(
        "Catalogue synopsis enrichment completed: %d unique titles checked, %d rows enriched across %d caches",
        state["titlesCompleted"], state["updatedRows"], state["filesCompleted"],
    )

@router.get("/api/catalogue/synopsis-enrichment")
async def api_catalogue_synopsis_enrichment_status():
    """Expose progress for the background synopsis fill across persistent catalogue JSON files."""
    return dict(_main._CATALOGUE_SYNOPSIS_ENRICHMENT_STATE)

def _warm_tmdb_ott_memory_cache() -> None:
    """Load persisted OTT entries once so each cache miss does not reparse the entire JSON file."""
    try:
        disk = _main.json.loads(_main.TMDB_OTT_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        _main.logger.warning("Could not warm in-memory OTT cache from %s", _main.TMDB_OTT_CACHE_FILE, exc_info=True)
        return

    items = disk.get("items") if isinstance(disk, dict) else None
    if not isinstance(items, dict) or not items:
        _main.logger.warning("Persistent OTT cache has no readable items at %s", _main.TMDB_OTT_CACHE_FILE)
        return

    now_wall = _main.time.time()
    now_monotonic = _main.time.monotonic()
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
        if movie_id < 1 or not _main.re.fullmatch(r"[A-Z]{2}", region) or saved_at <= 0:
            invalid += 1
            continue

        cache_key = (movie_id, region)
        if cache_key in _main._tmdb_ott_cache:
            continue
        # Translate the saved wall-clock age to monotonic time. Expired entries
        # remain available as stale fallbacks instead of being marked fresh again.
        age_seconds = max(0.0, now_wall - saved_at)
        _main._tmdb_ott_cache[cache_key] = (now_monotonic - age_seconds, row["payload"])
        warmed += 1

    _main.logger.info(
        "Warm-loaded persistent OTT cache into memory: %s entries warmed, %s valid disk entries, %s invalid rows",
        warmed, len(items) - invalid, invalid,
    )
