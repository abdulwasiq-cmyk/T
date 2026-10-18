#!/usr/bin/env python3
"""Resolve the curated Marvel theatrical list to TMDB IDs for human review.

This script only queries TMDB and writes a separate review manifest. It does not
call Watchmode and does not read, clear, or modify the OTT availability cache.

Run inside the Home container:
  docker exec -it home python /app/resolve_marvel_ids.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import main


DEFAULT_OUTPUT = Path("/app/data/marvel_tmdb_id_review.json")


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


async def resolve_one(title: str, year: int, retries: int = 4) -> dict[str, Any]:
    params = {"query": title, "year": year, "include_adult": "false", "language": "en-US", "page": 1}
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            payload = await main._tmdb_get_json("/search/movie", params)
            break
        except Exception as exc:
            last_exc = exc
            # main._tmdb_get_json places transiently failing credentials into a
            # 30-second cooldown. Wait for that cooldown instead of immediately
            # marking every remaining title as a lookup error.
            if attempt < retries:
                logging.warning(
                    "TMDB lookup failed for %s (%s); retry %d/%d in 31 seconds",
                    title, type(exc).__name__, attempt + 1, retries,
                )
                await asyncio.sleep(31)
    else:
        exc = last_exc or RuntimeError("TMDB lookup failed")
        return {
            "title": title, "year": year, "status": "lookup_error",
            "tmdbId": None, "matchedTitle": None, "releaseDate": None,
            "candidates": [], "error": type(exc).__name__,
        }

    results = payload.get("results") if isinstance(payload, dict) else None
    candidates = []
    exact = []
    expected = normalize(title)
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        candidate_title = str(item.get("title") or "")
        release_date = str(item.get("release_date") or "")
        candidate_year = int(release_date[:4]) if re.match(r"^\d{4}", release_date) else None
        candidate = {
            "id": item.get("id"),
            "title": candidate_title,
            "releaseDate": release_date or None,
            "year": candidate_year,
            "originalTitle": item.get("original_title"),
            "originalLanguage": item.get("original_language"),
            "popularity": item.get("popularity"),
        }
        candidates.append(candidate)
        if normalize(candidate_title) == expected and candidate_year == year:
            exact.append(candidate)

    candidates.sort(key=lambda row: (
        normalize(str(row.get("title") or "")) != expected,
        row.get("year") != year,
        -(float(row.get("popularity") or 0)),
    ))
    if len(exact) == 1:
        chosen = exact[0]
        return {
            "title": title, "year": year, "status": "exact_match",
            "tmdbId": chosen["id"], "matchedTitle": chosen["title"],
            "releaseDate": chosen["releaseDate"], "candidates": candidates[:5],
        }
    if len(exact) > 1:
        return {
            "title": title, "year": year, "status": "needs_review",
            "tmdbId": None, "matchedTitle": None, "releaseDate": None,
            "candidates": exact[:5],
        }
    return {
        "title": title, "year": year, "status": "needs_review" if candidates else "no_match",
        "tmdbId": None, "matchedTitle": None, "releaseDate": None,
        "candidates": candidates[:5],
    }


async def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Review JSON output path")
    parser.add_argument("--delay", type=float, default=0.25, help="Delay between TMDB requests")
    args = parser.parse_args()
    if args.delay < 0:
        raise SystemExit("--delay cannot be negative")
    if not (main.TMDB_READ_ACCESS_TOKENS or main.TMDB_API_KEYS):
        raise SystemExit("No TMDB credentials configured in the Home container.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    output = Path(args.output)
    previous: dict[tuple[str, int], dict[str, Any]] = {}
    if output.exists():
        try:
            saved = json.loads(output.read_text(encoding="utf-8"))
            for item in saved.get("movies", []):
                if isinstance(item, dict) and item.get("title") and item.get("year"):
                    previous[(str(item["title"]), int(item["year"]))] = item
            logging.info("Loaded %d existing rows from %s", len(previous), output)
        except (OSError, ValueError, TypeError) as exc:
            logging.warning("Could not read existing report; starting fresh: %s", type(exc).__name__)

    rows = []
    total = len(main.MARVEL_MOVIE_SEARCHES)
    for index, (title, year) in enumerate(main.MARVEL_MOVIE_SEARCHES, 1):
        saved_row = previous.get((title, year))
        if saved_row and saved_row.get("status") != "lookup_error":
            row = saved_row
            logging.info("[%d/%d] %s (%s): preserved %s", index, total, title, year, row.get("status"))
        else:
            row = await resolve_one(title, year)
            logging.info("[%d/%d] %s (%s): %s%s", index, total, title, year,
                         row["status"], f" -> {row['tmdbId']}" if row.get("tmdbId") else "")
        rows.append(row)
        # Save after each title so interruption never loses completed work.
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = {
            "schemaVersion": 1,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "main.MARVEL_MOVIE_SEARCHES",
            "sourceCount": total,
            "regionForLaterAvailability": "IN",
            "summary": {
                "exactMatches": sum(r.get("status") == "exact_match" for r in rows),
                "needsReview": sum(r.get("status") == "needs_review" for r in rows),
                "noMatch": sum(r.get("status") == "no_match" for r in rows),
                "lookupErrors": sum(r.get("status") == "lookup_error" for r in rows),
            },
            "note": "Review this manifest before running any Watchmode availability refresh. This script never modifies the OTT cache.",
            "movies": rows + [
                previous[(t, y)] for t, y in main.MARVEL_MOVIE_SEARCHES[index:]
                if (t, y) in previous and (t, y) not in {(r.get("title"), r.get("year")) for r in rows}
            ],
        }
        output.write_text(json.dumps(partial, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
        if index < total and args.delay:
            await asyncio.sleep(args.delay)

    exact_count = sum(row["status"] == "exact_match" for row in rows)
    review_count = sum(row["status"] == "needs_review" for row in rows)
    missing_count = sum(row["status"] == "no_match" for row in rows)
    error_count = sum(row["status"] == "lookup_error" for row in rows)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schemaVersion": 1,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "main.MARVEL_MOVIE_SEARCHES",
        "sourceCount": total,
        "regionForLaterAvailability": "IN",
        "summary": {
            "exactMatches": exact_count,
            "needsReview": review_count,
            "noMatch": missing_count,
            "lookupErrors": error_count,
        },
        "note": "Review this manifest before running any Watchmode availability refresh. This script never modifies the OTT cache.",
        "movies": rows,
    }
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote review manifest: {output}")
    print(f"Exact matches: {exact_count}; needs review: {review_count}; no match: {missing_count}; lookup errors: {error_count}")
    print("No Watchmode calls made. OTT cache was not changed.")
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
