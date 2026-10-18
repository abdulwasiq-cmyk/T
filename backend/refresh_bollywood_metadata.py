#!/usr/bin/env python3
"""Refresh Bollywood/Hindi movie metadata from TMDB into a separate JSON export.

This is intentionally separate from Home's runtime torrent-result caches.
Requires TMDB_READ_ACCESS_TOKENS or TMDB_READ_ACCESS_TOKEN in the environment.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.themoviedb.org/3/discover/movie"
PAGE_LIMIT = max(1, min(500, int(os.getenv("TMDB_REFRESH_MAX_PAGES", "500"))))
REQUEST_TIMEOUT = float(os.getenv("TMDB_REFRESH_TIMEOUT_SECONDS", "30"))
RETRIES = max(1, min(8, int(os.getenv("TMDB_REFRESH_RETRIES", "5"))))


def tokens_from_env() -> list[str]:
    values = []
    for name in ("TMDB_READ_ACCESS_TOKENS", "TMDB_READ_ACCESS_TOKEN", "TMDB_BEARER_TOKEN"):
        raw = os.getenv(name, "").replace("\\n", "\n")
        values.extend(part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip())
    return list(dict.fromkeys(values))


def normalize_title(value: Any) -> str:
    value = str(value or "").casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def year_from_date(value: Any) -> int | None:
    text = str(value or "")
    return int(text[:4]) if re.match(r"^\d{4}", text) else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_key(row: dict[str, Any]) -> tuple[str, int | None]:
    title = row.get("title") or row.get("name") or ""
    year = row.get("year") or year_from_date(row.get("release_date"))
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    return normalize_title(title), year


def load_seed(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Historical seed not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("results") or data.get("movies") or data.get("records") or data.get("items")
    else:
        rows = None
    if not isinstance(rows, list):
        raise ValueError("Historical seed JSON must be a list or an object containing a records/results/movies/items list.")
    return [row for row in rows if isinstance(row, dict) and (row.get("title") or row.get("name"))]


def request_page(client: httpx.Client, token_list: list[str], page: int, today: str) -> dict[str, Any]:
    last_error: str | None = None
    for attempt in range(RETRIES):
        # Rotate across configured credentials without printing or persisting them.
        token = token_list[(page + attempt) % len(token_list)]
        try:
            response = client.get(
                API_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={
                    "include_adult": "false",
                    "include_video": "false",
                    "language": "en-US",
                    "page": page,
                    "sort_by": "primary_release_date.desc",
                    "with_original_language": "hi",
                    "with_origin_country": "IN",
                    "primary_release_date.lte": today,
                    # Keep the library focused on feature films, not short films or TV specials.
                    "with_runtime.gte": 70,
                    "without_genres": "99,10770",
                },
            )
            if response.status_code in (401, 403):
                last_error = f"TMDB rejected a configured token (HTTP {response.status_code}); check token validity and permissions."
                continue
            if response.status_code == 429 or response.status_code >= 500:
                delay = min(30.0, float(response.headers.get("Retry-After", "2")) if response.headers.get("Retry-After", "").replace(".", "", 1).isdigit() else 2.0 * (attempt + 1))
                last_error = f"TMDB transient HTTP {response.status_code}"
                if attempt + 1 < RETRIES:
                    time.sleep(delay)
                    continue
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise ValueError("TMDB returned an unexpected response shape.")
            return body
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < RETRIES:
                time.sleep(min(15.0, 1.5 * (attempt + 1)))
    raise RuntimeError(f"Failed to fetch TMDB page {page}: {last_error}")


def main() -> int:
    token_list = tokens_from_env()
    if not token_list:
        print("ERROR: No TMDB read-access token found. Set TMDB_READ_ACCESS_TOKENS or TMDB_READ_ACCESS_TOKEN in the container environment.", file=sys.stderr)
        return 2

    base_dir = Path(__file__).resolve().parent
    seed_path = Path(os.getenv("BOLLYWOOD_HISTORICAL_SEED", str(base_dir / "cache-seed" / "bollywood_imdb_historical_metadata.json")))
    output_path = Path(os.getenv("BOLLYWOOD_METADATA_OUTPUT", str(Path(os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_tmdb_updated_metadata.json")))
    today = date.today().isoformat()

    try:
        historical = load_seed(seed_path)
    except FileNotFoundError:
        historical = []
        print("Historical seed not bundled; starting from TMDB discovery.")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: Could not load historical metadata seed: {exc}", file=sys.stderr)
        return 2

    merged: dict[tuple[str, int | None], dict[str, Any]] = {}
    for row in historical:
        key = record_key(row)
        if key[0]:
            merged[key] = dict(row)

    fetched = 0
    accepted_tmdb_ids: set[int] = set()
    total_pages = 1
    print(f"Starting TMDB refresh as of {today}; seed records={len(historical)}; page cap={PAGE_LIMIT}.")
    print("Using configured TMDB credentials (values are not logged).")

    try:
        with httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT), follow_redirects=True) as client:
            for page in range(1, PAGE_LIMIT + 1):
                payload = request_page(client, token_list, page, today)
                total_pages = min(PAGE_LIMIT, max(1, int(payload.get("total_pages") or 1)))
                rows = payload["results"]
                for item in rows:
                    if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
                        continue
                    release_date = str(item.get("release_date") or "")
                    year = year_from_date(release_date)
                    if not year or release_date > today:
                        continue
                    countries = item.get("origin_country")
                    # Keep the query constraints as the primary filter, and reject an
                    # explicit conflicting origin list if TMDB ever returns one.
                    if countries and "IN" not in countries:
                        continue
                    record = {
                        "tmdbId": int(item["id"]),
                        "imdbId": None,
                        "title": str(item.get("title") or "").strip(),
                        "year": year,
                        "releaseDate": release_date or None,
                        "overview": str(item.get("overview") or "").strip(),
                        "posterPath": item.get("poster_path"),
                        "backdropPath": item.get("backdrop_path"),
                        "genres": item.get("genre_ids") or [],
                        "originalLanguage": item.get("original_language"),
                        "originCountry": countries or ["IN"],
                        "voteAverage": item.get("vote_average"),
                        "voteCount": item.get("vote_count"),
                        "source": "tmdb_discover_hindi_india_feature_films",
                    }
                    accepted_tmdb_ids.add(int(item["id"]))
                    key = record_key(record)
                    if not key[0]:
                        continue
                    existing = merged.get(key, {})
                    # Keep historical fields (e.g. IMDb ID, director, cast) where
                    # available; TMDB fields refresh the canonical title/summary.
                    existing.update({k: v for k, v in record.items() if v not in (None, "", [])})
                    merged[key] = existing
                    fetched += 1
                print(f"Page {page}/{total_pages}: received {len(rows)} rows; merged unique records={len(merged)}.")
                if page >= total_pages or not rows:
                    break
                # A small pause avoids hammering the API while staying below typical limits.
                time.sleep(0.15)
    except Exception as exc:
        print(f"ERROR: Refresh stopped before export was written: {exc}", file=sys.stderr)
        return 1

    # Drop stale TMDB-backed seed rows that no longer satisfy the feature-film
    # query. Keep unmatched historical IMDb-only rows for backward compatibility.
    filtered_merged = {
        key: row for key, row in merged.items()
        if not row.get("tmdbId") or _safe_int(row.get("tmdbId")) in accepted_tmdb_ids
    }
    records = sorted(
        filtered_merged.values(),
        key=lambda row: (int(row.get("year") or 0), str(row.get("title") or "").casefold()),
        reverse=True,
    )
    output = {
        "schemaVersion": 1,
        "asOfDate": today,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "historical IMDb-derived seed merged with TMDB discover/movie",
        "filters": {
            "with_original_language": "hi",
            "with_origin_country": "IN",
            "primary_release_date_lte": today,
            "include_adult": False,
            "with_runtime.gte": 70,
            "without_genres": [99, 10770],
        },
        "stats": {
            "historicalSeedRecords": len(historical),
            "tmdbRowsMerged": fetched,
            "uniqueTitleYearRecords": len(records),
            "pagesRequestedMax": min(PAGE_LIMIT, total_pages),
        },
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(output_path)
    print(f"SUCCESS: wrote {len(records)} records to {output_path}")
    print(json.dumps(output["stats"], indent=2))
    print("This is a separate metadata export; runtime torrent caches were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
