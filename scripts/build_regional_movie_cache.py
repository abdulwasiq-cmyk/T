#!/usr/bin/env python3
"""Build persistent regional movie and web-series catalogues plus movie torrent searches.

Each movie/series catalogue is filled independently to 50 unique eligible entries
when the API can supply them. Titles may intentionally occur in multiple catalogues.
A failed page does not abort other catalogues, and prior good snapshots are retained.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.getenv("HOME_DATA_DIR", "/home/ubuntu/home-data"))
API = os.getenv("HOME_API_BASE", "http://127.0.0.1:10000").rstrip("/")
CACHE = BASE / "regional_movie_prebuilt_cache.json"
TV_CACHE = BASE / "regional_tv_prebuilt_cache.json"
STATE = BASE / "regional_movie_prebuilt_state.json"
CATALOGUES = (
    "popular-bollywood", "trending-bollywood", "latest-bollywood",
    "popular-hollywood", "trending-hollywood", "latest-hollywood",
)
TARGET_PER_CATALOGUE = int(os.getenv("REGIONAL_CACHE_MOVIES_PER_CATALOGUE", "50"))
PAGE_LIMIT = int(os.getenv("REGIONAL_CACHE_PAGES", "15"))
TORRENT_LIMIT = 50
MAX_SIZE = 5 * 1024**3
BAD_QUALITY = re.compile(r"(?:camrip|hdcam|telesync|telecine|dvdscr|screener|workprint)", re.I)


def request(path, timeout=90):
    req = urllib.request.Request(API + path, headers={"Accept": "application/json", "User-Agent": "HomeRegionalCacheBuilder/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").casefold())


def movie_key(movie):
    return norm(movie.get("title")) + "|" + str(movie.get("year") or "")


def verified_bollywood_latest_pages(pages):
    return [
        {
            **saved_page,
            "results": [
                movie for movie in saved_page.get("results", [])
                if isinstance(movie, dict)
                and movie.get("nexStreamPlayable") is True
            ],
        }
        for saved_page in pages
        if isinstance(saved_page, dict)
    ]


def main():
    old = {}
    if CACHE.exists():
        try:
            old = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    cache = {
        "version": 2,
        "builtAt": old.get("builtAt"),
        "catalogues": old.get("catalogues", {}),
        "movies": old.get("movies", {}),
        "status": "building",
        "targetPerCatalogue": TARGET_PER_CATALOGUE,
    }
    failed_catalogues = []

    # Build each catalogue independently. Do not deduplicate between catalogues:
    # a movie may correctly belong to Popular and Trending, for example.
    for cat in CATALOGUES:
        prior = cache["catalogues"].get(cat, {})
        pages = []
        seen = set()
        count = 0
        total_pages = PAGE_LIMIT
        failed = False

        for page in range(1, PAGE_LIMIT + 1):
            data = None
            error = None
            for attempt in range(1, 4):
                try:
                    data = request("/api/movies/catalogue/" + cat + "?page=" + str(page) + "&released_only=true")
                    break
                except Exception as exc:
                    error = str(exc)
                    if attempt < 3:
                        time.sleep(attempt)
            if data is None:
                print(json.dumps({"event": "catalogue_error", "catalogue": cat, "page": page, "error": error}), flush=True)
                failed = True
                break

            rows = data.get("results", [])
            pages.append({"page": page, "results": rows, "totalPages": data.get("totalPages", 1)})
            for movie in rows:
                if not isinstance(movie, dict) or not str(movie.get("title") or "").strip():
                    continue
                key = movie_key(movie)
                if key not in seen:
                    seen.add(key)
                    count += 1
            try:
                total_pages = max(1, int(data.get("totalPages") or 1))
            except (TypeError, ValueError):
                total_pages = PAGE_LIMIT
            print(json.dumps({"event": "catalogue_page", "catalogue": cat, "page": page, "pageMovies": len(rows), "uniqueMovies": count, "target": TARGET_PER_CATALOGUE}), flush=True)
            if count >= TARGET_PER_CATALOGUE or page >= total_pages:
                break
            time.sleep(float(os.getenv("REGIONAL_CACHE_DELAY_SECONDS", "0.4")))

        if failed:
            failed_catalogues.append(cat)
            # Keep the previous known-good catalogue if refresh failed at any page.
            if prior.get("pages"):
                pages = prior["pages"]
                if cat == "latest-bollywood":
                    pages = verified_bollywood_latest_pages(pages)
                print(json.dumps({"event": "catalogue_preserved", "catalogue": cat, "movies": sum(len(p.get("results", [])) for p in pages)}), flush=True)
            elif not pages:
                print(json.dumps({"event": "catalogue_incomplete", "catalogue": cat, "movies": count, "target": TARGET_PER_CATALOGUE}), flush=True)
            else:
                print(json.dumps({"event": "catalogue_partial_preserved", "catalogue": cat, "pages": len(pages), "movies": count}), flush=True)

        cache["catalogues"][cat] = {
            "builtAt": datetime.now(timezone.utc).isoformat(),
            "targetMovies": TARGET_PER_CATALOGUE,
            "movieCount": len({movie_key(m) for p in pages for m in p.get("results", []) if isinstance(m, dict) and m.get("title")}),
            "pages": pages,
            "status": "ready" if len({movie_key(m) for p in pages for m in p.get("results", []) if isinstance(m, dict) and m.get("title")}) >= TARGET_PER_CATALOGUE else "partial",
        }
        atomic_json(CACHE, cache)
        print(json.dumps({"event": "catalogue_cached", "catalogue": cat, "movies": cache["catalogues"][cat]["movieCount"], "status": cache["catalogues"][cat]["status"]}), flush=True)

    # Prebuild six Indian/Hollywood TV-series catalogues separately from movies.
    # These pages are served by the backend as a persistent snapshot, so browsing
    # the section does not require live TMDB requests on every visit.
    tv_old = {}
    if TV_CACHE.exists():
        try:
            candidate = json.loads(TV_CACHE.read_text(encoding="utf-8"))
            if int(candidate.get("version") or 0) >= 3:
                tv_old = candidate
        except Exception:
            tv_old = {}
    tv_cache = {
        "version": 3,
        "builtAt": tv_old.get("builtAt"),
        "catalogues": tv_old.get("catalogues", {}),
        "targetPerCatalogue": TARGET_PER_CATALOGUE,
        "status": "building",
    }
    tv_catalogues = (
        "indian-popular", "indian-trending", "indian-latest",
        "hollywood-popular", "hollywood-trending", "hollywood-latest",
    )
    failed_tv_catalogues = []
    for cat in tv_catalogues:
        prior = tv_cache["catalogues"].get(cat, {})
        pages = []
        seen = set()
        total_pages = PAGE_LIMIT
        failed = False
        for page in range(1, PAGE_LIMIT + 1):
            data = None
            error = None
            for attempt in range(1, 4):
                try:
                    data = request("/api/tv/catalogue/" + cat + "?page=" + str(page) + "&refresh=true")
                    break
                except Exception as exc:
                    error = str(exc)
                    if attempt < 3:
                        time.sleep(attempt)
            if data is None:
                print(json.dumps({"event": "tv_catalogue_error", "catalogue": cat, "page": page, "error": error}), flush=True)
                failed = True
                break
            rows = data.get("results", [])
            pages.append({
                "page": page,
                "results": rows,
                "totalPages": data.get("totalPages", 1),
                "totalResults": data.get("totalResults", 0),
            })
            for show in rows:
                if not isinstance(show, dict) or not str(show.get("title") or "").strip():
                    continue
                seen.add(str(show.get("id") or (norm(show.get("title")) + "|" + str(show.get("year") or ""))))
            try:
                total_pages = max(1, int(data.get("totalPages") or 1))
            except (TypeError, ValueError):
                total_pages = PAGE_LIMIT
            print(json.dumps({"event": "tv_catalogue_page", "catalogue": cat, "page": page, "pageSeries": len(rows), "uniqueSeries": len(seen), "target": TARGET_PER_CATALOGUE}), flush=True)
            if len(seen) >= TARGET_PER_CATALOGUE or page >= total_pages:
                break
            time.sleep(float(os.getenv("REGIONAL_CACHE_DELAY_SECONDS", "0.4")))
        if failed:
            failed_tv_catalogues.append(cat)
            if prior.get("pages"):
                pages = prior["pages"]
                print(json.dumps({"event": "tv_catalogue_preserved", "catalogue": cat, "pages": len(pages)}), flush=True)
        count = len({str(show.get("id") or (norm(show.get("title")) + "|" + str(show.get("year") or ""))) for p in pages for show in p.get("results", []) if isinstance(show, dict) and show.get("title")})
        tv_cache["catalogues"][cat] = {
            "builtAt": datetime.now(timezone.utc).isoformat(),
            "targetSeries": TARGET_PER_CATALOGUE,
            "seriesCount": count,
            "totalResults": max((int(p.get("totalResults") or 0) for p in pages), default=count),
            "pages": pages,
            "status": "ready" if count >= TARGET_PER_CATALOGUE else "partial",
        }
        tv_cache["builtAt"] = datetime.now(timezone.utc).isoformat()
        tv_cache["status"] = "building"
        atomic_json(TV_CACHE, tv_cache)
        print(json.dumps({"event": "tv_catalogue_cached", "catalogue": cat, "series": count, "status": tv_cache["catalogues"][cat]["status"]}), flush=True)
    tv_cache["builtAt"] = datetime.now(timezone.utc).isoformat()
    tv_cache["status"] = "partial" if failed_tv_catalogues or any(v.get("status") != "ready" for v in tv_cache["catalogues"].values()) else "ready"
    tv_cache["failedCatalogues"] = failed_tv_catalogues
    atomic_json(TV_CACHE, tv_cache)
    print(json.dumps({"event": "tv_catalogues_complete", "status": tv_cache["status"], "catalogues": {k: v.get("seriesCount", 0) for k, v in tv_cache["catalogues"].items()}, "file": str(TV_CACHE)}), flush=True)

    # Merge movie records for title-to-torrent lookup. This shared index may be
    # deduplicated; the per-catalogue snapshots above remain independent.
    movies = {}
    for cat, snapshot in cache["catalogues"].items():
        for page in snapshot.get("pages", []):
            for movie in page.get("results", []):
                if not isinstance(movie, dict):
                    continue
                key = movie_key(movie)
                if key == "|":
                    continue
                entry = movies.setdefault(key, {"movie": movie, "catalogues": []})
                if cat not in entry["catalogues"]:
                    entry["catalogues"].append(cat)

    cache["movies"] = movies
    total = len(movies)
    for idx, (key, entry) in enumerate(movies.items(), 1):
        title = str(entry["movie"].get("title") or "").strip()
        year = entry["movie"].get("year")
        if not title:
            continue
        query = title + (" " + str(year) if year else "")
        try:
            path = "/api/search?q=" + urllib.parse.quote(query) + "&limit=" + str(TORRENT_LIMIT)
            response = request(path, timeout=150)
            rows = response if isinstance(response, list) else response.get("results", response.get("torrents", []))
            target = norm(title)
            good = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("title") or "")
                media = str(row.get("mediaTitle") or "")
                try:
                    size = int(row.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                has_source = bool(row.get("magnetUrl") or row.get("downloadUrl") or row.get("sourceUrl"))
                if not has_source or size > MAX_SIZE or BAD_QUALITY.search(name):
                    continue
                if target and (target in norm(name) or target in norm(media)):
                    good.append(row)
            entry["torrentDetails"] = {"query": query, "results": good, "scannedAt": datetime.now(timezone.utc).isoformat(), "status": "ready" if good else "empty"}
            state.setdefault("completed", {})[key] = entry["torrentDetails"]
        except Exception as exc:
            print(json.dumps({"event": "torrent_error", "title": title, "error": str(exc)}), flush=True)
            entry["torrentDetails"] = old.get("movies", {}).get(key, {}).get("torrentDetails", {"query": query, "results": [], "status": "unavailable"})

        if idx % 5 == 0 or idx == total:
            cache["movies"] = movies
            cache["builtAt"] = datetime.now(timezone.utc).isoformat()
            cache["status"] = "building"
            atomic_json(CACHE, cache)
            atomic_json(STATE, state)
            print(json.dumps({"event": "progress", "completed": idx, "total": total}), flush=True)
        time.sleep(float(os.getenv("REGIONAL_CACHE_DELAY_SECONDS", "0.4")))

    cache["movies"] = movies
    cache["builtAt"] = datetime.now(timezone.utc).isoformat()
    cache["status"] = "partial" if failed_catalogues or any(v.get("status") != "ready" for v in cache["catalogues"].values()) else "ready"
    cache["movieCount"] = len(movies)
    cache["torrentMovieCount"] = sum(bool((x.get("torrentDetails") or {}).get("results")) for x in movies.values())
    cache["failedCatalogues"] = failed_catalogues
    atomic_json(CACHE, cache)
    print(json.dumps({"event": "complete", "status": cache["status"], "failedCatalogues": failed_catalogues, "failedTvCatalogues": failed_tv_catalogues, "catalogues": {k: v.get("movieCount", 0) for k, v in cache["catalogues"].items()}, "movieCount": cache["movieCount"], "torrentMovieCount": cache["torrentMovieCount"], "file": str(CACHE)}), flush=True)
    if failed_catalogues or failed_tv_catalogues:
        sys.exit(1)


if __name__ == "__main__":
    main()
