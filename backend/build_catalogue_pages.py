#!/usr/bin/env python3
"""Pre-split bundled catalogues into small gzipped pages (run at image build).

Parsing the multi-MB catalogue files on a 0.1 CPU free instance takes many
seconds, so the first page view used to be slow. This script does that work
once at build time: cache-seed/pages/<key>/<n>.json.gz holds each page of 18
titles (all torrent rows of a title stay together), ready to be sent as-is.
"""
import gzip, json, re, sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SEED = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "cache-seed")
PAGE_SIZE = 18
SOURCES = {
    "marvel": "marvel_catalogue.json",
    "marvel-hindi": "marvel_hindi_catalogue.json",
    "dc-live-action": "dc_live_action_catalogue.json",
    "dc-live-action-hindi": "dc_live_action_hindi_catalogue.json",
    "dc-animated": "dc_animated_catalogue.json",
    "classics": "classics_catalogue.json",
}
CHRONOLOGICAL_CATALOGUES = {
    "marvel",
    "dc-live-action",
    "dc-live-action-hindi",
    "dc-animated",
    "classics",
}
EXCLUDED_TITLES = {
    "marvel": {"nextavengersheroesoftomorrow"},
    "dc-live-action": {
        "supermaniithericharddonnercut",
        "watchmenchapteri",
        "watchmenchapterii",
    },
    "dc-live-action-hindi": {
        "supermaniithericharddonnercut",
        "watchmenchapteri",
        "watchmenchapterii",
    },
}


def group_key(item, index=0):
    """Return a stable title key, without collapsing unrelated untitled rows."""
    title = re.sub(r"[^a-z0-9]+", "", str(
        item.get("catalogueTitle") or item.get("marvelCatalogueTitle")
        or item.get("mediaTitle") or item.get("title") or "").casefold())
    if title:
        return title

    # Some legacy rows have no display title but still carry a stable media/release ID.
    for field in ("tmdbId", "tmdb_id", "movieId", "movie_id", "imdbId", "imdb_id",
                  "infoHash", "info_hash", "magnet", "id"):
        value = str(item.get(field) or "").strip().casefold()
        if value:
            return field + ":" + value

    # Never group every malformed row under the same empty-string key.
    return "__untitled_row_" + str(index)


def group_sort_key(rows):
    movie = next((row for row in rows if row.get("releaseDate") or row.get("release_date")), rows[0])
    raw_date = str(movie.get("releaseDate") or movie.get("release_date") or "").strip()
    try:
        release_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        year_match = re.search(r"\b((?:19|20)\d{2})\b", raw_date)
        try:
            year = int(movie.get("year") or (year_match.group(1) if year_match else 0))
        except (TypeError, ValueError):
            year = 0
        release_date = date(year, 1, 1) if 1 <= year <= 9999 else date.min
    title = str(
        movie.get("catalogueTitle") or movie.get("marvelCatalogueTitle")
        or movie.get("mediaTitle") or movie.get("title") or ""
    )
    return -release_date.toordinal(), re.sub(r"[^a-z0-9]+", "", title.casefold())


def load_poster_map():
    path = SEED / "poster_map.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Poster map must be a JSON list: {path}")
    posters = {}
    for row in payload:
        if not isinstance(row, list) or len(row) != 3:
            continue
        title, year, url = (str(value or "").strip() for value in row)
        if title and url.startswith(("https://", "http://")):
            key = re.sub(r"[^a-z0-9]+", "", title.casefold()) + "|" + year
            posters.setdefault(key, url)
    return posters


def apply_bundled_poster(row, posters):
    current = str(row.get("posterUrl") or "").strip()
    if current.startswith(("https://", "http://")):
        return

    title = ""
    year = ""
    try:
        parts = urlsplit(current)
        if parts.path.rstrip("/") == "/api/poster":
            query = parse_qs(parts.query)
            title = (query.get("title") or [""])[0].strip()
            year = (query.get("year") or [""])[0].strip()
    except ValueError:
        return

    if not title:
        title = str(
            row.get("catalogueTitle") or row.get("marvelCatalogueTitle")
            or row.get("mediaTitle") or row.get("title") or ""
        ).strip()
        year = str(row.get("year") or "")
    key = re.sub(r"[^a-z0-9]+", "", title.casefold()) + "|" + year
    if poster := posters.get(key):
        row["posterUrl"] = poster


def main():
    posters = load_poster_map()
    for key, name in SOURCES.items():
        path = SEED / name
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = [r for r in payload.get("results", []) if isinstance(r, dict)]
        excluded_titles = EXCLUDED_TITLES.get(key, set())
        rows = [
            row for row in rows
            if re.sub(r"[^a-z0-9]+", "", str(
                row.get("catalogueTitle") or row.get("marvelCatalogueTitle")
                or row.get("mediaTitle") or row.get("title") or ""
            ).casefold()) not in excluded_titles
        ]
        for row in rows:
            apply_bundled_poster(row, posters)
        groups = {}
        for index, row in enumerate(rows):
            groups.setdefault(group_key(row, index), []).append(row)
        keys = list(groups)
        if key in CHRONOLOGICAL_CATALOGUES:
            keys.sort(key=lambda group: group_sort_key(groups[group]))
        total_pages = max(1, -(-len(keys) // PAGE_SIZE))
        out = SEED / "pages" / key
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.json.gz"):
            old.unlink()
        for page in range(1, total_pages + 1):
            page_rows = [r for k in keys[(page - 1) * PAGE_SIZE: page * PAGE_SIZE] for r in groups[k]]
            envelope = {
                "status": "ready", "builtAt": payload.get("builtAt"),
                "completed": int(payload.get("titlesQueried") or 0),
                "total": int(payload.get("titlesQueried") or 0),
                "resultCount": len(rows), "movieCount": len(keys), "refreshing": False,
                "results": page_rows, "page": page, "pageSize": PAGE_SIZE,
                "totalPages": total_pages, "totalMovies": len(keys),
            }
            (out / f"{page}.json.gz").write_bytes(gzip.compress(
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9))
        print(f"{key}: {len(keys)} titles -> {total_pages} pages")


if __name__ == "__main__":
    main()
