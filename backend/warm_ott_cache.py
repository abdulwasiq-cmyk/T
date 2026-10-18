#!/usr/bin/env python3
"""Manually warm Home's persistent OTT availability cache using Watchmode only.

Catalogue discovery is deliberately offline: it reads the existing persistent
TMDB catalogue cache and never calls TMDB. Watchmode supplies availability
records. The script preserves the established OTT cache file/schema and keeps
existing provider data when Watchmode has no usable result for a title.

Run inside the running Home container:
  docker exec -it home python /app/warm_ott_cache.py
No scheduler is installed; this script runs only when explicitly invoked.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import main


CATALOGUES = (
    "marvel",
    "dc-live-action",
    "dc-animated",
    "popular-hollywood",
    "trending-hollywood",
    "popular-bollywood",
    "trending-bollywood",
    "latest-hollywood",
    "latest-bollywood",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill Home's persistent OTT cache from Watchmode, using saved catalogues only."
    )
    parser.add_argument("--region", default="IN", help="Two-letter region code (default: IN).")
    parser.add_argument(
        "--pages-per-catalogue",
        type=int,
        default=50,
        help="Maximum saved catalogue pages to inspect per catalogue (default: 50).",
    )
    parser.add_argument(
        "--max-titles",
        type=int,
        default=0,
        help="Maximum unique movie IDs to process; use 0 for no limit (default: unlimited).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between Watchmode lookups (default: 1.0).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh Watchmode entries too. Existing provider data is retained if a lookup is empty or fails.",
    )
    parser.add_argument(
        "--include-upcoming",
        action="store_true",
        help="Read saved upcoming catalogue pages instead of released-only pages.",
    )
    return parser.parse_args()


def _providers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for field in ("streamingProviders", "rentProviders", "buyProviders")
        for item in (payload.get(field) or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _merge_provider_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            identity = str(item.get("providerId") or name.casefold())
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(item)
    return merged


def _merge_existing(new_rows: list[dict[str, Any]], old_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Prefer the current Watchmode row for a provider, while retaining older
    # cached providers that Watchmode did not return in this response.
    by_identity: dict[str, dict[str, Any]] = {}
    for row in old_rows:
        if isinstance(row, dict) and str(row.get("name") or "").strip():
            identity = str(row.get("providerId") or row.get("name", "").casefold())
            by_identity[identity] = row
    for row in new_rows:
        if isinstance(row, dict) and str(row.get("name") or "").strip():
            identity = str(row.get("providerId") or row.get("name", "").casefold())
            by_identity[identity] = row
    return list(by_identity.values())


async def _resolve_watchmode_id(
    movie_id: int,
    resolved_ids: dict[int, int | None],
) -> int | None:
    """Resolve a TMDB movie ID to Watchmode's native title ID via Watchmode search."""
    if movie_id in resolved_ids:
        return resolved_ids[movie_id]
    try:
        response = await main._watchmode_get(
            "/search/",
            params={
                "search_field": "tmdb_movie_id",
                "search_value": str(movie_id),
                "types": "movie",
            },
            timeout=httpx.Timeout(12.0, connect=5.0),
        )
        if response is None or response.status_code >= 400:
            status = response.status_code if response is not None else "no response"
            logging.warning("Watchmode ID resolution failed for movie %s (%s)", movie_id, status)
            resolved_ids[movie_id] = None
            return None
        payload = response.json()
        results = payload.get("title_results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            resolved_ids[movie_id] = None
            return None
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
                resolved_ids[movie_id] = watchmode_id
                return watchmode_id
        resolved_ids[movie_id] = None
        return None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logging.warning("Watchmode ID resolution failed for movie %s (%s)", movie_id, type(exc).__name__)
        resolved_ids[movie_id] = None
        return None


async def _watchmode_sources(
    movie_id: int,
    region: str,
    resolved_ids: dict[int, int | None],
) -> list[dict[str, Any]] | None:
    """Fetch sources by TMDB ID, falling back to a resolved Watchmode ID when empty."""
    try:
        response = await main._watchmode_get(
            f"/title/movie-{movie_id}/sources/",
            params={"regions": region},
            timeout=httpx.Timeout(12.0, connect=5.0),
        )
        if response is not None and response.status_code < 400:
            payload = response.json()
            if not isinstance(payload, list):
                logging.warning("Watchmode returned an unexpected payload for movie %s; preserving cache", movie_id)
                return None
            rows = [item for item in payload if isinstance(item, dict)]
            if rows:
                return rows
        elif response is not None and response.status_code != 404:
            logging.warning("Watchmode lookup failed for movie %s (%s); preserving cache", movie_id, response.status_code)
            return None
        elif response is None:
            logging.warning("Watchmode lookup failed for movie %s (no response); preserving cache", movie_id)
            return None

        # A successful empty response (or 404) can mean Watchmode could not
        # resolve the TMDB-formatted ID. Search only on this fallback path.
        watchmode_id = await _resolve_watchmode_id(movie_id, resolved_ids)
        if watchmode_id is None:
            return []
        fallback = await main._watchmode_get(
            f"/title/{watchmode_id}/sources/",
            params={"regions": region},
            timeout=httpx.Timeout(12.0, connect=5.0),
        )
        if fallback is None or fallback.status_code >= 400:
            status = fallback.status_code if fallback is not None else "no response"
            logging.warning("Watchmode source lookup by native ID failed for movie %s (%s)", movie_id, status)
            return None
        payload = fallback.json()
        if not isinstance(payload, list):
            logging.warning("Watchmode returned an unexpected native-ID payload for movie %s", movie_id)
            return None
        return [item for item in payload if isinstance(item, dict)]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logging.warning("Watchmode lookup failed for movie %s (%s); preserving cache", movie_id, type(exc).__name__)
        return None

async def run() -> int:
    args = parse_args()
    region = args.region.strip().upper()
    if len(region) != 2 or not region.isalpha() or not region.isascii():
        raise SystemExit("--region must be a two-letter country code, such as IN.")
    if args.pages_per_catalogue < 1:
        raise SystemExit("--pages-per-catalogue must be at least 1.")
    if args.max_titles < 0 or args.delay < 0:
        raise SystemExit("--max-titles and --delay cannot be negative.")
    if not main.WATCHMODE_API_KEYS:
        raise SystemExit("No Watchmode key found. Set WATCHMODE_API_KEYS in the container environment.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs full request URLs at INFO level; Watchmode keys are query params
    # on the API, so suppress those logs to prevent credential leakage.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cache_path = main.TMDB_OTT_CACHE_FILE
    catalogue_path = main.TMDB_CATALOGUE_CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    released_only = not args.include_upcoming
    suffix = "|released-v4" if released_only else "|upcoming-v3"
    print(f"Region: {region}")
    print(f"Availability source: Watchmode only ({len(main.WATCHMODE_API_KEYS)} keys configured; values hidden)")
    print(f"Catalogue source: existing persistent cache only ({catalogue_path})")
    print(f"Persistent OTT cache: {cache_path}")
    print(f"Catalogues: {len(CATALOGUES)}; maximum saved pages per catalogue: {args.pages_per_catalogue}")
    print(f"Maximum unique titles: {'unlimited' if args.max_titles == 0 else args.max_titles}")
    print(f"Refresh Watchmode entries: {'yes' if args.refresh else 'no'}")
    print("No TMDB API requests will be made.\n")

    ids: dict[int, str] = {}
    pages_loaded = 0
    for key in CATALOGUES:
        found_for_catalogue = 0
        for page in range(1, args.pages_per_catalogue + 1):
            cached = main._read_tmdb_persistent_cache((key + suffix, page))
            if not cached:
                # Stop at the first missing page: catalogue pages are contiguous
                # and the script must not make a live TMDB request to fill gaps.
                if page == 1:
                    print(f"[catalogue missing] No saved pages for {key}; skipping it.")
                break
            payload = cached[1]
            rows = payload.get("results") or []
            if not rows:
                break
            pages_loaded += 1
            found_for_catalogue += 1
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    movie_id = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                if movie_id > 0:
                    ids.setdefault(movie_id, str(row.get("title") or f"Movie {movie_id}"))
            total_pages = int(payload.get("totalPages") or page)
            if page >= total_pages:
                break
        if found_for_catalogue:
            print(f"[catalogue] {key}: loaded {found_for_catalogue} saved page(s)")

    ordered = list(ids.items())
    if args.max_titles:
        ordered = ordered[:args.max_titles]
    print(f"Saved catalogue pages loaded: {pages_loaded}")
    print(f"Unique movie IDs collected: {len(ids)}; this run will inspect: {len(ordered)}")
    if not ordered:
        print("No cached catalogue titles found. Nothing was changed; no TMDB requests were made.")
        return 2

    stats = {
        "cached": 0,
        "updated": 0,
        "preserved": 0,
        "errors": 0,
        "watchmode_streaming": 0,
        "watchmode_rent_buy": 0,
        "not_confirmed": 0,
    }
    total = len(ordered)
    resolved_watchmode_ids: dict[int, int | None] = {}
    for index, (movie_id, title) in enumerate(ordered, start=1):
        cache_key = (movie_id, region)
        disk_entry = main._read_tmdb_ott_persistent_cache(cache_key)
        old_payload: dict[str, Any] = disk_entry[1] if disk_entry else {}
        old_provider_count = len(_providers(old_payload))
        old_source = str(old_payload.get("source") or "").casefold()
        old_age = max(0.0, time.time() - disk_entry[0]) if disk_entry else float("inf")

        if not args.refresh and disk_entry and old_age < main.TMDB_OTT_CACHE_SECONDS:
            # Fresh Watchmode records are already authoritative for this warmer.
            # Recheck fresh TMDB/legacy records so Watchmode can enrich them;
            # if Watchmode returns nothing, the old provider data is retained.
            if old_source == "watchmode":
                stats["cached"] += 1
                continue

        source_rows = await _watchmode_sources(movie_id, region, resolved_watchmode_ids)
        if source_rows is None:
            stats["errors"] += 1
            stats["preserved"] += int(bool(disk_entry))
            if index == 1 or index % 25 == 0:
                print(f"[{index}/{total}] ERROR {title[:65]} (TMDB {movie_id}): Watchmode unavailable; cache preserved")
        else:
            streaming_new = main._watchmode_provider_rows(
                source_rows, {"sub", "free", "ads", "tve", "subscription"}
            )
            rent_new = main._watchmode_provider_rows(source_rows, {"rent", "rental"})
            buy_new = main._watchmode_provider_rows(source_rows, {"buy", "purchase"})
            has_watchmode_data = bool(streaming_new or rent_new or buy_new)

            if not has_watchmode_data and old_provider_count:
                # A valid but empty response can mean the title has no current
                # listing; don't erase provider data already saved in the cache.
                stats["preserved"] += 1
                if index == 1 or index % 25 == 0:
                    print(f"[{index}/{total}] PRESERVED {title[:65]} (TMDB {movie_id}): Watchmode had no provider rows")
            else:
                streaming = _merge_existing(streaming_new, old_payload.get("streamingProviders") or [])
                rent = _merge_existing(rent_new, old_payload.get("rentProviders") or [])
                buy = _merge_existing(buy_new, old_payload.get("buyProviders") or [])
                if streaming:
                    status = "streaming"
                    stats["watchmode_streaming"] += 1
                elif rent or buy:
                    status = "rent_buy"
                    stats["watchmode_rent_buy"] += 1
                else:
                    status = "not_confirmed"
                    stats["not_confirmed"] += 1

                payload = {
                    "movieId": movie_id,
                    "region": region,
                    "status": status,
                    "streamingProviders": streaming,
                    "rentProviders": rent,
                    "buyProviders": buy,
                    "theatricalReleaseDate": old_payload.get("theatricalReleaseDate"),
                    "digitalReleaseDate": old_payload.get("digitalReleaseDate"),
                    "providerLink": old_payload.get("providerLink") or f"https://www.themoviedb.org/movie/{movie_id}/watch?locale={region}",
                    "checkedAt": datetime.now(timezone.utc).isoformat(),
                    "source": "Watchmode",
                    "providerAttribution": "Streaming availability data powered by Watchmode. Previously cached provider data is retained when not returned by Watchmode.",
                    "attribution": old_payload.get("attribution") or "This product uses the TMDB API but is not endorsed or certified by TMDB.",
                }
                main._write_tmdb_ott_persistent_cache(cache_key, payload)
                stats["updated"] += 1
                if index == 1 or index % 10 == 0 or index == total:
                    providers = len(streaming) + len(rent) + len(buy)
                    print(f"[{index}/{total}] {title[:65]} (TMDB {movie_id}): {status}, providers={providers}, source=Watchmode")

        cooldown_until = float(getattr(main, "_WATCHMODE_RATE_LIMITED_UNTIL", 0.0) or 0.0)
        if cooldown_until > time.time():
            wait_seconds = cooldown_until - time.time()
            print(f"[rate limit] Watchmode asked us to pause; waiting {int(wait_seconds) + 1}s.")
            await asyncio.sleep(wait_seconds)
        elif args.delay:
            await asyncio.sleep(args.delay)

    elapsed = int(time.monotonic() - started)
    print("\nWatchmode OTT cache warm-up finished.")
    print(f"Finished at: {datetime.now(timezone.utc).isoformat()}")
    print(f"Fresh entries skipped: {stats['cached']}")
    print(f"Entries updated: {stats['updated']}")
    print(f"Entries preserved after empty/failed lookups: {stats['preserved']}")
    print(f"Watchmode streaming results: {stats['watchmode_streaming']}")
    print(f"Watchmode rent/buy results: {stats['watchmode_rent_buy']}")
    print(f"Not confirmed: {stats['not_confirmed']}")
    print(f"Errors: {stats['errors']}")
    print(f"Elapsed: {elapsed // 60}m {elapsed % 60}s")
    print(f"Cache file: {cache_path}")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
