#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import shutil
from difflib import SequenceMatcher
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

BASE = Path("/home/ubuntu/home-data")
ENV_FILE = Path("/home/ubuntu/home/.env.debug")
API_BASE = "http://127.0.0.1:10000"
REGION = "IN"

CATALOGUES = [
    "marvel_catalogue.json",
    "marvel_hindi_catalogue.json",
    "dc_live_action_catalogue.json",
    "dc_live_action_hindi_catalogue.json",
    "dc_animated_catalogue.json",
    "classics_catalogue.json",
]

STATE_FILE = BASE / "ott_enrichment_state.json"
LOG_FILE = BASE / "catalogue_ott_enrichment.jsonl"
SUMMARY_FILE = BASE / "catalogue_ott_enrichment_summary.json"

def now():
    return datetime.now(timezone.utc).isoformat()

def log(event):
    event = {"time": now(), **event}
    line = json.dumps(event, ensure_ascii=False)
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

def read_env(path):
    values = {}
    if not path.exists():
        raise RuntimeError(f"Environment file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "resolved": {}, "completed": {}}

def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)

def request_json(url, token=None, timeout=25):
    headers = {"Accept": "application/json", "User-Agent": "HomeCatalogueOTT/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def norm(value):
    value = str(value or "").lower()
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"\b(1080p|2160p|720p|480p|4k|uhd|hdrip|webrip|web-dl|bluray|brrip|bdrip|hdtv|x264|x265|h264|h265|hevc|aac|dts|proper|repack|extended|remastered|dual audio|hindi|english|multi audio|10bit|8bit)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split()).strip()

def clean_title(row):
    # Prefer catalogue metadata over the noisy torrent release name.
    for key in ("mediaTitle", "marvelCatalogueTitle", "catalogueTitle"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    title = str(row.get("title") or "").strip()
    # Remove common trailing release year / torrent details.
    title = re.sub(r"\s*[\[(]\s*(19|20)\d{2}\s*[\])]", " ", title)
    return re.sub(r"\s+", " ", title).strip()

def get_year(row, title):
    # Check likely title fields before any release-year cleanup.
    for value in (row.get("year"), row.get("mediaTitle"),
                  row.get("marvelCatalogueTitle"), row.get("catalogueTitle"),
                  row.get("title"), title):
        try:
            year = int(value)
            if 1880 <= year <= 2100:
                return year
        except (ValueError, TypeError):
            pass
        match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value or ""))
        if match:
            return int(match.group(1))
    return None

# Explicit TMDB ID overrides for documented alternate display titles that do not
# pass the conservative search-title matcher. Keys include the release year to
# avoid confusing remakes and sequels.
TMDB_ID_OVERRIDES = {
    ("the fantastic four first steps", 2025): 617126,  # TMDB title: The Fantastic 4: First Steps
    ("x2 x men united", 2003): 36658,                # TMDB title: X2
    ("superman the movie", 1978): 1924,              # TMDB title: Superman
    ("battle of the super sons", 2022): 886396,     # TMDB title: Batman and Superman: Battle of the Super Sons
}


def resolve_movie(title, year, tokens):
    """Resolve cautiously: exact/alternate titles first, then very close matches."""
    if not title:
        return None, "empty_title"

    wanted = norm(title)
    if not wanted:
        return None, "empty_title"

    override_id = TMDB_ID_OVERRIDES.get((wanted, year))
    if override_id:
        return override_id, "verified_tmdb_id_override"

    errors = []
    saw_successful_search = False
    candidates_by_id = {}

    # Year-constrained search first; retry without year for regional year differences.
    queries = [title]
    if " (film)" not in title.lower():
        queries.append(title + " (film)")

    for query in queries:
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = str(year)
        urls = ["https://api.themoviedb.org/3/search/movie?" + urllib.parse.urlencode(params)]
        if year:
            params_without_year = dict(params)
            params_without_year.pop("year", None)
            urls.append("https://api.themoviedb.org/3/search/movie?" +
                        urllib.parse.urlencode(params_without_year))

        for url in urls:
            result = None
            for token in tokens:
                try:
                    result = request_json(url, token=token)
                    saw_successful_search = True
                    break
                except urllib.error.HTTPError as exc:
                    errors.append(f"HTTP_{exc.code}")
                    if exc.code not in (401, 403, 429, 500, 502, 503, 504):
                        break
                except Exception as exc:
                    errors.append(type(exc).__name__)
            if result is None:
                continue

            for candidate in result.get("results") or []:
                if not candidate.get("id"):
                    continue
                candidate_year = (candidate.get("release_date") or "")[:4]
                if year and candidate_year.isdigit() and abs(int(candidate_year) - year) > 1:
                    continue

                titles = {
                    norm(candidate.get("title") or ""),
                    norm(candidate.get("original_title") or ""),
                }
                titles.discard("")
                exact = wanted in titles
                similarity = max((SequenceMatcher(None, wanted, value).ratio()
                                  for value in titles), default=0.0)
                # Keep fuzzy matching conservative to avoid wrong movie/provider matches.
                if exact or similarity >= 0.92:
                    candidate_id = int(candidate["id"])
                    score = (
                        1 if exact else 0,
                        1 if year and candidate_year == str(year) else 0,
                        similarity,
                        float(candidate.get("popularity") or 0),
                    )
                    previous = candidates_by_id.get(candidate_id)
                    if previous is None or score > previous[0]:
                        candidates_by_id[candidate_id] = (score, candidate)

    if candidates_by_id:
        best = max(candidates_by_id.values(), key=lambda item: item[0])[1]
        exact = wanted in {norm(best.get("title") or ""), norm(best.get("original_title") or "")}
        return int(best["id"]), "exact_title_match" if exact else "close_title_match"

    if saw_successful_search:
        return None, "no_confident_tmdb_match"
    return None, "tmdb_search_failed:" + ",".join(errors or ["unknown"])

def main():
    if not BASE.exists():
        raise RuntimeError(f"Data directory not found: {BASE}")

    env = read_env(ENV_FILE)
    raw_tokens = env.get("TMDB_READ_ACCESS_TOKENS", "")
    # Accept comma, semicolon, or whitespace-separated token lists.
    tokens = [t.strip() for t in re.split(r"[,;\s]+", raw_tokens) if t.strip()]
    if not tokens:
        raise RuntimeError("TMDB_READ_ACCESS_TOKENS is empty in .env.debug")

    state = load_state()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    catalogues = []

    # Validate every input and back up all catalogues before changing any.
    for name in CATALOGUES:
        path = BASE / name
        if not path.exists():
            log({"status": "FILE_MISSING", "file": name})
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            log({"status": "INVALID_CATALOGUE_SHAPE", "file": name})
            continue
        backup = path.with_name(path.name + ".ott-backup-" + stamp)
        shutil.copy2(path, backup)
        # Keep the newest rollback point for this catalogue; older timestamped
        # copies otherwise accumulate on every scheduled enrichment run.
        for old_backup in sorted(
            path.parent.glob(path.name + ".ott-backup-*"),
            key=lambda item: (item.stat().st_mtime, item.name),
            reverse=True,
        )[1:]:
            try:
                old_backup.unlink()
            except OSError as exc:
                log({"status": "OLD_BACKUP_CLEANUP_FAILED", "file": str(old_backup),
                     "error": str(exc)[:200]})
        catalogues.append((name, path, data))
        log({"status": "BACKUP_CREATED", "file": name, "backup": str(backup),
             "entries": len(data["results"])})

    if not catalogues:
        raise RuntimeError("No valid catalogues found; nothing changed.")

    counts = {}
    total = sum(len(data["results"]) for _, _, data in catalogues)
    processed = 0
    # Share work between duplicate releases in the same run.
    resolution_cache = {}
    availability_cache = {}
    # Cache completed work by normalized title + year across the entire run.
    # Catalogue files can contain duplicate rows; reuse the first result instead
    # of emitting duplicate FETCHED logs and sleeping for every repeated row.
    processed_identity_cache = {}

    for filename, path, data in catalogues:
        changed = False
        for row in data["results"]:
            processed += 1
            if not isinstance(row, dict):
                continue

            title = clean_title(row)
            year = get_year(row, title)
            identity = norm(title) + "|" + str(year or "")
            existing = row.get("ottAvailability")

            # Reuse a result already handled earlier in this run. This is important
            # because the source catalogues may contain repeated copies of a title.
            cached_identity = processed_identity_cache.get(identity)
            if cached_identity is not None:
                cached_tmdb_id, cached_availability = cached_identity
                if cached_tmdb_id:
                    row["tmdbId"] = int(cached_tmdb_id)
                row["ottAvailability"] = cached_availability
                changed = True
                counts["DUPLICATE_REUSED"] = counts.get("DUPLICATE_REUSED", 0) + 1
                continue

            # Keep successful enrichment. Retry only entries without a successful result.
            if isinstance(existing, dict) and existing.get("status") in (
                "streaming", "rent_buy", "no_providers"
            ):
                processed_identity_cache[identity] = (row.get("tmdbId"), existing)
                counts["ALREADY_ENRICHED"] = counts.get("ALREADY_ENRICHED", 0) + 1
                continue

            saved = state.get("completed", {}).get(identity)
            if isinstance(saved, dict) and saved.get("status") in (
                "streaming", "rent_buy", "no_providers"
            ):
                row["ottAvailability"] = saved["availability"]
                row["tmdbId"] = saved["tmdbId"]
                processed_identity_cache[identity] = (saved["tmdbId"], saved["availability"])
                changed = True
                counts["RESTORED_FROM_PROGRESS"] = counts.get("RESTORED_FROM_PROGRESS", 0) + 1
                continue

            tmdb_id = state.get("resolved", {}).get(identity)
            if identity in resolution_cache:
                tmdb_id, resolution = resolution_cache[identity]
                if not tmdb_id:
                    counts[resolution] = counts.get(resolution, 0) + 1
                    continue
            elif not tmdb_id:
                tmdb_id, resolution = resolve_movie(title, year, tokens)
                resolution_cache[identity] = (tmdb_id, resolution)
                if tmdb_id:
                    state.setdefault("resolved", {})[identity] = tmdb_id
                    save_state(state)
                else:
                    counts[resolution] = counts.get(resolution, 0) + 1
                    log({"status": "RESOLUTION_FAILED", "file": filename,
                         "title": title, "year": year, "reason": resolution})
                    continue

            try:
                if tmdb_id in availability_cache:
                    payload = availability_cache[tmdb_id]
                else:
                    payload = request_json(
                        f"{API_BASE}/api/movies/ott/{tmdb_id}?region={REGION}",
                        timeout=30,
                    )
                    if not isinstance(payload, dict) or payload.get("movieId") is None:
                        raise ValueError("Unexpected OTT API response")
                    availability_cache[tmdb_id] = payload
                if not isinstance(payload, dict) or payload.get("movieId") is None:
                    raise ValueError("Unexpected OTT API response")

                streams = payload.get("streamingProviders") or []
                rents = payload.get("rentProviders") or []
                buys = payload.get("buyProviders") or []

                if streams:
                    status = "streaming"
                    log_status = "FETCHED"
                elif rents or buys:
                    status = "rent_buy"
                    log_status = "FETCHED"
                else:
                    status = "no_providers"
                    log_status = "NO_PROVIDERS"

                availability = {
                    "region": REGION,
                    "status": status,
                    "streamingProviders": streams,
                    "rentProviders": rents,
                    "buyProviders": buys,
                    "providerLink": payload.get("providerLink"),
                    "source": payload.get("source"),
                    "checkedAt": payload.get("checkedAt"),
                    "providerAttribution": payload.get("providerAttribution"),
                    "attribution": payload.get("attribution"),
                }

                row["tmdbId"] = int(tmdb_id)
                row["ottAvailability"] = availability
                processed_identity_cache[identity] = (int(tmdb_id), availability)
                changed = True
                state.setdefault("completed", {})[identity] = {
                    "tmdbId": int(tmdb_id),
                    "availability": availability,
                }
                save_state(state)

                counts[log_status] = counts.get(log_status, 0) + 1
                log({"status": log_status, "file": filename, "title": title,
                     "year": year, "tmdbId": tmdb_id, "streaming": [p.get("name") for p in streams],
                     "rent": [p.get("name") for p in rents],
                     "buy": [p.get("name") for p in buys], "source": payload.get("source")})
            except Exception as e:
                counts["API_FAILED"] = counts.get("API_FAILED", 0) + 1
                log({"status": "API_FAILED", "file": filename, "title": title,
                     "year": year, "tmdbId": tmdb_id,
                     "error": f"{type(e).__name__}: {str(e)[:250]}"})
                # Do not overwrite an existing good value on failure.

            if processed % 25 == 0:
                log({"status": "PROGRESS", "processed_entries": processed,
                     "total_entries": total, "counts": counts})
            time.sleep(0.15)

        if changed:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
            log({"status": "CATALOGUE_SAVED", "file": filename,
                 "entries": len(data["results"])})

    summary = {
        "finishedAt": now(),
        "region": REGION,
        "counts": counts,
        "catalogues": {name: len(data["results"]) for name, _, data in catalogues},
        "logFile": str(LOG_FILE),
        "stateFile": str(STATE_FILE),
    }
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    log({"status": "FINISHED", **summary})
    print("\nSummary:", json.dumps(summary, indent=2), flush=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user. Progress is saved; rerun the same command to resume.")
        sys.exit(130)
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
