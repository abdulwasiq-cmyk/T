#!/usr/bin/env python3
"""Refresh OTT data only for franchise movies whose saved catalogues have no providers.

Run inside the Home container. It bypasses stale in-memory/disk OTT cache reads,
checks the configured availability sources directly, then persists results to both
the shared OTT cache and every matching cached torrent row. Existing good provider
data is first copied across duplicate torrent releases and is never erased by an
empty or failed refresh.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import main


CATALOGUES = (
    ("marvel", main.MARVEL_CATALOGUE_PATH),
    ("marvel-hindi", main._EXTRA_CATALOGUE_PATHS["marvel-hindi"]),
    ("dc-live-action", main._EXTRA_CATALOGUE_PATHS["dc-live-action"]),
    ("dc-live-action-hindi", main._EXTRA_CATALOGUE_PATHS["dc-live-action-hindi"]),
    ("dc-animated", main._EXTRA_CATALOGUE_PATHS["dc-animated"]),
    ("classics", main._EXTRA_CATALOGUE_PATHS["classics"]),
)
THREED_RE = re.compile(r"(?:^|[^a-z0-9])3[ ._-]*d(?:$|[^a-z0-9])", re.I)
EXCLUDED_KEYS = ("avengersconfidentialblackwidowpunisher", "ironmanrisetechnovore")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh missing OTT rows in saved Marvel/DC catalogues.")
    parser.add_argument("--region", default="IN")
    parser.add_argument("--delay", type=float, default=0.75, help="Seconds between unique title lookups.")
    parser.add_argument("--max-titles", type=int, default=0, help="Optional cap; 0 means all missing-provider titles.")
    return parser.parse_args()


def canonical_title(row: dict[str, Any]) -> str:
    for field in ("catalogueTitle", "marvelCatalogueTitle", "mediaTitle", "title"):
        value = str(row.get(field) or "").strip()
        if value:
            # Torrent release suffixes are fallback-only; preserve embedded year
            # tokens which are part of a movie title, such as Wonder Woman 1984.
            value = re.sub(r"\[[^\]]*(?:\]|$)", " ", value)
            value = re.sub(r"\([^)]*(?:\)|$)", " ", value)
            value = re.split(
                r"\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|3d|uhd|hdrip|webrip|web[- .]?dl|"
                r"bluray|blu[- .]?ray|brrip|bdrip|hdtv|x264|x265|h264|h265|hevc|aac|dts)\b",
                value,
                maxsplit=1,
                flags=re.I,
            )[0]
            return re.sub(r"\s+", " ", value).strip(" .-_")
    return ""


def row_year(row: dict[str, Any], title: str) -> int:
    for value in (row.get("year"), str(row.get("releaseDate") or "")[:4]):
        try:
            year = int(value)
            if 1880 <= year <= 2100:
                return year
        except (TypeError, ValueError):
            pass
    matches = re.findall(r"\b((?:19|20)\d{2})\b", title)
    if matches:
        try:
            return int(matches[-1])
        except ValueError:
            pass
    return 0


def identity(title: str, year: int) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.casefold()) + "|" + str(year or "")


def excluded_or_3d(row: dict[str, Any]) -> bool:
    for field in ("quality", "title", "releaseName", "qualityLabel", "resolution"):
        if THREED_RE.search(str(row.get(field) or "")):
            return True
    for field in ("catalogueTitle", "marvelCatalogueTitle", "mediaTitle", "title"):
        raw = str(row.get(field) or "").strip()
        raw = re.sub(r"\[[^\]]*(?:\]|$)|\([^)]*(?:\)|$)", " ", raw)
        normalized = re.sub(r"[^a-z0-9]+", "", raw.casefold())
        if any(normalized.startswith(value) for value in EXCLUDED_KEYS):
            return True
    return False


def item_id(row: dict[str, Any]) -> int | None:
    value = row.get("tmdbId")
    availability = row.get("ottAvailability")
    if not value and isinstance(availability, dict):
        value = availability.get("movieId") or availability.get("tmdbId")
    try:
        result = int(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def provider_group(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for provider in value:
        if isinstance(provider, str):
            name = provider.strip()
            item = {"name": name} if name else None
        elif isinstance(provider, dict):
            name = str(provider.get("name") or provider.get("provider_name") or "").strip()
            item = {**provider, "name": name} if name else None
        else:
            item = None
        if item:
            rows.append(item)
    return rows


def availability_for_row(row: dict[str, Any]) -> dict[str, Any]:
    saved = row.get("ottAvailability")
    if isinstance(saved, dict):
        return saved
    if any(field in row for field in ("streamingProviders", "rentProviders", "buyProviders")):
        return row
    return {}


def has_providers(payload: dict[str, Any]) -> bool:
    return any(provider_group(payload.get(field)) for field in (
        "streamingProviders", "rentProviders", "buyProviders"
    ))


def merge_availability(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    merged = dict(first or {})
    for field in ("streamingProviders", "rentProviders", "buyProviders"):
        rows = provider_group(merged.get(field)) + provider_group((second or {}).get(field))
        by_key: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("providerId") or row.get("name") or "").casefold()
            if key:
                by_key[key] = row
        merged[field] = list(by_key.values())
    for field in ("region", "providerLink", "source", "checkedAt", "providerAttribution", "attribution",
                  "theatricalReleaseDate", "digitalReleaseDate", "movieId"):
        if (second or {}).get(field) not in (None, "", []):
            merged[field] = (second or {})[field]
    streams = merged["streamingProviders"]
    rentals = merged["rentProviders"]
    purchases = merged["buyProviders"]
    merged["status"] = "streaming" if streams else "rent_buy" if rentals or purchases else "not_confirmed"
    return merged


def atomic_save(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".ott-refresh.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


async def run() -> int:
    args = parse_args()
    region = args.region.strip().upper()
    if len(region) != 2 or not region.isalpha() or not region.isascii():
        raise SystemExit("--region must be a two-letter country code.")
    if args.delay < 0 or args.max_titles < 0:
        raise SystemExit("--delay and --max-titles must not be negative.")

    catalogues: list[dict[str, Any]] = []
    records: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, int, str]] = []
    good_by_id: dict[int, dict[str, Any]] = {}
    good_by_identity: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, int]] = {}

    for key, path in CATALOGUES:
        if not path.is_file():
            summaries[key] = {"missing_file": 1}
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[skip] {key}: cannot read JSON: {exc}", flush=True)
            summaries[key] = {"invalid_file": 1}
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            summaries[key] = {"invalid_shape": 1}
            continue

        entry = {"key": key, "path": path, "payload": payload, "changed": False}
        catalogues.append(entry)
        summaries.setdefault(key, {})
        for row in payload["results"]:
            if not isinstance(row, dict) or excluded_or_3d(row):
                continue
            title = canonical_title(row)
            year = row_year(row, title)
            if not title:
                continue
            ident = identity(title, year)
            movie_id = item_id(row)
            avail = availability_for_row(row)
            records.append((entry, payload, row, title, year, ident))
            if has_providers(avail):
                if movie_id:
                    good_by_id[movie_id] = merge_availability(good_by_id.get(movie_id, {}), avail)
                good_by_identity[ident] = merge_availability(good_by_identity.get(ident, {}), avail)

    # First spread any existing good result across all duplicate torrent releases.
    propagated = 0
    for entry, _payload, row, title, year, ident in records:
        movie_id = item_id(row)
        known = good_by_id.get(movie_id) if movie_id else None
        known = known or good_by_identity.get(ident)
        if known and has_providers(known) and not has_providers(availability_for_row(row)):
            row["tmdbId"] = movie_id or known.get("movieId")
            row["ottAvailability"] = dict(known)
            entry["changed"] = True
            propagated += 1

    # Deduplicate missing rows by TMDB ID. If an ID is absent, resolve once per title/year.
    representatives_by_id: dict[int, tuple[str, int, str]] = {}
    identities_without_id: dict[str, tuple[str, int, str]] = {}
    for _entry, _payload, row, title, year, ident in records:
        if has_providers(availability_for_row(row)):
            continue
        movie_id = item_id(row)
        if movie_id:
            if movie_id not in good_by_id:
                representatives_by_id.setdefault(movie_id, (title, year, ident))
        elif ident not in good_by_identity:
            identities_without_id.setdefault(ident, (title, year, ident))

    resolved_ids: dict[str, int | None] = {}
    unresolved = 0
    for ident, (title, year, _identity) in identities_without_id.items():
        try:
            movie_id, resolution = await main._resolve_marvel_tmdb_movie_id(title, year)
        except Exception as exc:
            movie_id, resolution = None, type(exc).__name__
        resolved_ids[ident] = movie_id
        if movie_id:
            if movie_id not in good_by_id:
                representatives_by_id.setdefault(movie_id, (title, year, ident))
        else:
            unresolved += 1
            print(f"[unresolved] {title} ({year or 'year unknown'}): {resolution}", flush=True)

    work = list(representatives_by_id.items())
    if args.max_titles:
        work = work[:args.max_titles]
    print(json.dumps({
        "region": region,
        "catalogues": {entry["key"]: len(entry["payload"]["results"]) for entry in catalogues},
        "uniqueMissingProviderIds": len(representatives_by_id),
        "titlesToRefresh": len(work),
        "duplicatesWithSavedProviderDataPropagated": propagated,
        "unresolvedTitleIdentities": unresolved,
    }), flush=True)

    refreshed_by_id: dict[int, dict[str, Any]] = {}
    stats = {"refreshed": 0, "withProviders": 0, "withoutProviders": 0, "lookupErrors": 0}
    for index, (movie_id, (title, year, _ident)) in enumerate(work, start=1):
        try:
            result = await main._tmdb_fetch_movie_ott_availability(movie_id, region)
            if not isinstance(result, dict):
                raise ValueError("Unexpected OTT availability payload")
            result = dict(result)
            result["region"] = region
            result["movieId"] = movie_id
            if not has_providers(result):
                # Preserve a prior good entry, if another alias/release already supplied one.
                previous_good = good_by_id.get(movie_id)
                if previous_good and has_providers(previous_good):
                    result = previous_good
                else:
                    result["status"] = "not_confirmed"
            else:
                result["status"] = "streaming" if provider_group(result.get("streamingProviders")) else "rent_buy"
                good_by_id[movie_id] = merge_availability(good_by_id.get(movie_id, {}), result)
                stats["withProviders"] += 1

            refreshed_by_id[movie_id] = result
            main._write_tmdb_ott_persistent_cache((movie_id, region), result)
            main._tmdb_ott_cache[(movie_id, region)] = (time.monotonic(), result)
            stats["refreshed"] += 1
            if has_providers(result):
                status = "WITH_PROVIDERS"
            else:
                status = "NO_PROVIDERS_REPORTED"
                stats["withoutProviders"] += 1
            print(f"[{index}/{len(work)}] {status}: {title} ({year or 'year unknown'}) TMDB={movie_id} "
                  f"stream={len(provider_group(result.get('streamingProviders')))} "
                  f"rent={len(provider_group(result.get('rentProviders')))} "
                  f"buy={len(provider_group(result.get('buyProviders')))} "
                  f"source={result.get('source') or 'unknown'}", flush=True)
        except Exception as exc:
            stats["lookupErrors"] += 1
            print(f"[{index}/{len(work)}] LOOKUP_FAILED: {title} ({year or 'year unknown'}) "
                  f"TMDB={movie_id}: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        if args.delay:
            await asyncio.sleep(args.delay)

    # Apply fresh data to every cached copy of that TMDB movie. Existing providers
    # remain if the fresh source returned empty or errored.
    updated_rows = 0
    for entry, _payload, row, title, year, ident in records:
        movie_id = item_id(row) or resolved_ids.get(ident)
        fresh = refreshed_by_id.get(movie_id) if movie_id else None
        known = good_by_id.get(movie_id) if movie_id else good_by_identity.get(ident)
        candidate = fresh if fresh and has_providers(fresh) else known
        if candidate and has_providers(candidate):
            previous = availability_for_row(row)
            if previous != candidate or item_id(row) != movie_id:
                row["tmdbId"] = movie_id
                row["ottAvailability"] = dict(candidate)
                entry["changed"] = True
                updated_rows += 1
        elif fresh and movie_id:
            # Mark completion transparently but never erase provider rows if one was found.
            previous = availability_for_row(row)
            if not has_providers(previous):
                row["tmdbId"] = movie_id
                row["ottAvailability"] = dict(fresh)
                entry["changed"] = True
                updated_rows += 1

    for entry in catalogues:
        if entry["changed"]:
            payload = entry["payload"]
            rows = [row for row in payload["results"] if isinstance(row, dict)]
            payload["results"] = rows
            payload["resultCount"] = len(rows)
            payload["movieCount"] = len({
                main._normalize_title(canonical_title(row)) for row in rows if canonical_title(row)
            })
            atomic_save(entry["path"], payload)

    summary = {
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "region": region,
        "catalogues": {entry["key"]: {"rows": len(entry["payload"]["results"]), "saved": bool(entry["changed"])} for entry in catalogues},
        "duplicateRowsUpdated": updated_rows,
        "duplicatesWithSavedProviderDataPropagated": propagated,
        "unresolvedTitleIdentities": unresolved,
        **stats,
    }
    summary_path = main.TMDB_OTT_CACHE_FILE.parent / "franchise_ott_refresh_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("FINISHED " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
