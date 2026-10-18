#!/usr/bin/env python3
"""Incrementally build a cached Bollywood catalogue from real torrent search results.

Run weekly or monthly. Each run checks a bounded batch, preserves previous matches,
and atomically publishes only titles with a plausible title/year match. The normal
web request path never performs this bulk work.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import main

BASE_DIR = Path(__file__).resolve().parent
METADATA_PATH = Path(os.getenv(
    "BOLLYWOOD_METADATA_CATALOGUE_PATH",
    str(Path(os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_tmdb_updated_metadata.json"),
))
OUTPUT_PATH = Path(os.getenv(
    "BOLLYWOOD_TORRENT_CATALOGUE_PATH",
    str(Path(os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_torrent_matched_catalogue.json"),
))
STATE_PATH = Path(os.getenv(
    "BOLLYWOOD_TORRENT_CATALOGUE_STATE_PATH",
    str(Path(os.getenv("APP_DATA_DIR", "/app/data")) / "bollywood_torrent_catalogue_state.json"),
))
BATCH_SIZE = max(1, min(1000, int(os.getenv("BOLLYWOOD_TORRENT_REFRESH_BATCH_SIZE", "150"))))
SEARCH_DELAY = max(0.0, min(5.0, float(os.getenv("BOLLYWOOD_TORRENT_SEARCH_DELAY_SECONDS", "0.5"))))
MIN_TITLE_SIMILARITY = max(0.55, min(0.98, float(os.getenv("BOLLYWOOD_TORRENT_MIN_TITLE_SIMILARITY", "0.72"))))


def normalize(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


MAX_CACHED_TORRENT_SIZE_BYTES = 5 * 1024 * 1024 * 1024


def size_bytes(value: Any) -> int:
    """Normalize provider sizes to bytes; unknown sizes are not cacheable."""
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip().casefold().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(b|bytes?|kb|kib|mb|mib|gb|gib|tb|tib)?", text)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2) or "b"
    factors = {"b": 1, "byte": 1, "bytes": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2, "gb": 1000**3, "gib": 1024**3, "tb": 1000**4, "tib": 1024**4}
    return int(amount * factors[unit])


def year_from_title(value: Any) -> int | None:
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


ADULT_RESULT_TERMS = {
    "xxx", "porn", "porno", "pornographic", "erotic", "hentai",
    "blowjob", "handjob", "milf", "deepthroat", "sexvideo",
}


def is_adult_result(result_title: str) -> bool:
    tokens = set(normalize(result_title).split())
    return bool(tokens & ADULT_RESULT_TERMS)


def title_similarity(movie_title: str, result_title: str) -> float:
    movie = normalize(movie_title)
    result = normalize(result_title)
    if not movie or not result:
        return 0.0
    movie_tokens = set(movie.split())
    result_tokens = set(result.split())
    coverage = len(movie_tokens & result_tokens) / max(1, len(movie_tokens))
    sequence = SequenceMatcher(None, movie, result).ratio()

    # A single-word title is highly ambiguous: only accept it when it starts
    # the release name (not when it appears incidentally in unrelated media).
    if len(movie_tokens) == 1:
        if result != movie and not result.startswith(movie + " "):
            return 0.0
        return max(0.90, sequence)

    # Require the movie's words in the same order. Token coverage alone made
    # reordered titles such as "Hari Ka Om" match "Om Ka Hari".
    if movie in result:
        return max(0.90, sequence)
    if coverage == 1.0:
        return sequence
    if coverage >= 0.8:
        return max(0.76, sequence)
    return max(coverage * 0.8, sequence)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


async def run() -> int:
    metadata = load_json(METADATA_PATH, {})
    # Bootstrap from the bundled export on the first run; subsequent runs use the
    # persistent APP_DATA_DIR copy refreshed by the scheduled metadata job.
    if not isinstance(metadata, dict) or not isinstance(metadata.get("records"), list):
        bundled_path = BASE_DIR / "cache-seed" / "bollywood_tmdb_updated_metadata.json"
        metadata = load_json(bundled_path, {})
        if isinstance(metadata, dict) and isinstance(metadata.get("records"), list):
            atomic_write(METADATA_PATH, metadata)
    source_rows = metadata.get("records", []) if isinstance(metadata, dict) else []
    if not isinstance(source_rows, list) or not source_rows:
        print(f"ERROR: metadata catalogue missing or empty: {METADATA_PATH}", file=sys.stderr)
        return 2

    state = load_json(STATE_PATH, {})
    output = load_json(OUTPUT_PATH, {})
    existing = output.get("records", []) if isinstance(output, dict) else []
    matched: dict[str, dict[str, Any]] = {
        str(row.get("tmdbId") or row.get("id")): row
        for row in existing if isinstance(row, dict) and (row.get("tmdbId") or row.get("id"))
    }

    # Refresh stale hits first, then continue through the metadata list in bounded batches.
    now = time.time()
    recheck_after = max(7, int(os.getenv("BOLLYWOOD_TORRENT_RECHECK_DAYS", "30"))) * 86400
    stale_keys = [
        key for key, row in matched.items()
        if now - float(row.get("torrentCheckedAtEpoch") or 0) >= recheck_after
    ]
    cursor = max(0, int(state.get("cursor") or 0)) % len(source_rows)
    scheduled_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for key in stale_keys[: max(1, BATCH_SIZE // 3)]:
        row = matched.get(key)
        if row:
            candidates.append(row)
            scheduled_ids.add(key)
    inspected = 0
    while len(candidates) < BATCH_SIZE and inspected < len(source_rows):
        row = source_rows[cursor]
        cursor = (cursor + 1) % len(source_rows)
        inspected += 1
        if not isinstance(row, dict) or not str(row.get("title") or "").strip():
            continue
        key = str(row.get("tmdbId") or ("historical:" + normalize(row.get("title")) + ":" + str(row.get("year") or "")))
        if key in scheduled_ids:
            continue
        candidates.append(row)
        scheduled_ids.add(key)

    checked = 0
    newly_matched = 0
    failed_searches = 0
    for movie in candidates:
        title = str(movie.get("title") or "").strip()
        year = movie.get("year")
        query = f"{title} {year}" if year else title
        try:
            results = await asyncio.wait_for(main.search_1337x(query, limit=20), timeout=45.0)
        except Exception as exc:
            failed_searches += 1
            print(f"Search failed for {title!r}: {type(exc).__name__}")
            continue

        checked += 1
        qualifying: list[tuple[float, dict[str, Any]]] = []
        seen_releases: set[str] = set()
        for result in results:
            if not isinstance(result, dict):
                continue
            result_title = str(result.get("title") or "").strip()
            result_size = size_bytes(result.get("size"))
            if result_size <= 0 or result_size > MAX_CACHED_TORRENT_SIZE_BYTES:
                continue
            if is_adult_result(result_title):
                continue
            score = title_similarity(title, result_title)
            result_year = year_from_title(result_title)
            if year and result_year and abs(int(year) - result_year) > 1:
                continue
            if score < MIN_TITLE_SIMILARITY:
                continue
            info_hash = str(result.get("infoHash") or "").strip().casefold()
            magnet_url = str(result.get("magnetUrl") or "").strip()
            release_key = info_hash or magnet_url or (normalize(result_title) + ":" + str(result_size))
            if release_key in seen_releases:
                continue
            seen_releases.add(release_key)
            qualifying.append((score, result))

        qualifying.sort(key=lambda pair: (
            pair[0], int(pair[1].get("seeders") or 0), -size_bytes(pair[1].get("size")),
        ), reverse=True)
        releases = []
        for score, result in qualifying:
            releases.append({
                "title": str(result.get("title") or ""),
                "indexer": str(result.get("indexer") or result.get("provider") or ""),
                "infoUrl": str(result.get("infoUrl") or result.get("sourceUrl") or ""),
                "magnetUrl": result.get("magnetUrl"),
                "infoHash": str(result.get("infoHash") or ""),
                "size": size_bytes(result.get("size")),
                "seeders": result.get("seeders"),
                "leechers": result.get("leechers"),
                "matchScore": round(score, 3),
            })

        key = str(movie.get("tmdbId") or ("historical:" + normalize(title) + ":" + str(year or "")))
        if releases:
            record = dict(movie)
            record["torrentMatches"] = releases
            record["torrentMatch"] = releases[0]
            record["torrentCheckedAt"] = datetime.now(timezone.utc).isoformat()
            record["torrentCheckedAtEpoch"] = time.time()
            if key not in matched:
                newly_matched += 1
            matched[key] = record
        else:
            # A zero-result search can be caused by a temporary provider/indexer
            # response. Keep an existing verified match rather than making the
            # live catalogue disappear because one refresh returned no candidates.
            if key in matched:
                matched[key]["torrentLastNoMatchAt"] = datetime.now(timezone.utc).isoformat()
        if SEARCH_DELAY:
            await asyncio.sleep(SEARCH_DELAY)

    # Keep metadata current for retained matches while preserving the matched torrent.
    latest_by_id = {
        str(row.get("tmdbId")): row for row in source_rows
        if isinstance(row, dict) and row.get("tmdbId") is not None
    }
    published = []
    for key, row in matched.items():
        fresh = latest_by_id.get(str(row.get("tmdbId")))
        if fresh:
            torrent = row.get("torrentMatch")
            row = {**fresh, **row, "torrentMatch": torrent}
        published.append(row)
    published.sort(key=lambda row: (
        str(row.get("releaseDate") or ""), str(row.get("title") or "").casefold()
    ), reverse=True)

    generated = datetime.now(timezone.utc).isoformat()
    atomic_write(OUTPUT_PATH, {
        "schemaVersion": 1,
        "generatedAt": generated,
        "source": "TMDB metadata filtered by cached torrent-provider matches",
        "stats": {
            "metadataCandidates": len(source_rows),
            "matchedTitles": len(published),
            "checkedThisRun": checked,
            "newMatchesThisRun": newly_matched,
            "providerSearchFailures": failed_searches,
            "batchSize": BATCH_SIZE,
        },
        "records": published,
    })
    atomic_write(STATE_PATH, {
        "cursor": cursor,
        "lastRunAt": generated,
        "checkedThisRun": checked,
        "providerSearchFailures": failed_searches,
    })
    print(json.dumps({
        "metadataCandidates": len(source_rows),
        "checkedThisRun": checked,
        "newMatchesThisRun": newly_matched,
        "matchedTitles": len(published),
        "providerSearchFailures": failed_searches,
        "nextCursor": cursor,
        "output": str(OUTPUT_PATH),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
