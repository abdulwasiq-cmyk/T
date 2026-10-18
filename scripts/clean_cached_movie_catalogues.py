#!/usr/bin/env python3
"""Clean, sort, and repair posters in Home's persistent movie catalogue snapshots."""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get("HOME_DATA_DIR", str(Path.home() / "home-data")))
CATALOGUES = (
    "marvel_catalogue.json",
    "marvel_hindi_catalogue.json",
    "dc_live_action_catalogue.json",
    "dc_live_action_hindi_catalogue.json",
    "dc_animated_catalogue.json",
    "latest_hollywood_catalogue.json",
    "latest_bollywood_catalogue.json",
)
FRANCHISE_CATALOGUES = set(CATALOGUES[:5])
EXCLUDED_TITLE_KEYS = (
    "avengersconfidentialblackwidowpunisher",
    "ironmanrisetechnovore",
)
THREED_RE = re.compile(r"(?:^|[^a-z0-9])3[ ._-]*d(?:$|[^a-z0-9])", re.I)
QUALITY_RE = re.compile(
    r"\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|3d|uhd|hdrip|webrip|web[- .]?dl|"
    r"bluray|blu[- .]?ray|brrip|bdrip|hdtv|x264|x265|h264|h265|hevc|aac|dts|"
    r"proper|repack|extended|remastered|dual audio|hindi|english|multi audio|10bit|8bit)\b.*$",
    re.I,
)
POSTER_OVERRIDES = {
    ("theamazingspiderman2", "2014"): "https://image.tmdb.org/t/p/w500/bU7nTmvmy0h3VUP01v1T2imgH6N.jpg",
    ("wonderwoman1984", "2020"): "https://image.tmdb.org/t/p/w500/8UlWHLMpgZm9bx6QYh0NFoq67TZ.jpg",
}


def normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def canonical_title(row: dict) -> str:
    for field in ("catalogueTitle", "marvelCatalogueTitle", "mediaTitle", "title"):
        value = str(row.get(field) or "").strip()
        if value:
            return value
    return ""


def year_for(row: dict) -> int:
    for value in (row.get("year"), str(row.get("releaseDate") or "")[:4]):
        try:
            year = int(value)
            if 1880 <= year <= 2100:
                return year
        except (TypeError, ValueError):
            pass
    source = " ".join(str(row.get(field) or "") for field in (
        "catalogueTitle", "marvelCatalogueTitle", "mediaTitle", "title"
    ))
    years = re.findall(r"\b((?:19|20)\d{2})\b", source)
    if years:
        try:
            return int(years[-1])
        except ValueError:
            pass
    return 0


def has_3d_quality(row: dict) -> bool:
    return any(
        THREED_RE.search(str(row.get(field) or ""))
        for field in ("quality", "title", "releaseName", "qualityLabel", "resolution")
    )


def excluded_franchise_title(row: dict) -> bool:
    for field in ("catalogueTitle", "marvelCatalogueTitle", "mediaTitle", "title"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        cleaned = re.sub(r"\[[^\]]*(?:\]|$)", " ", raw)
        cleaned = re.sub(r"\([^)]*(?:\)|$)", " ", cleaned)
        cleaned = QUALITY_RE.sub("", cleaned)
        clean_key = normalized(cleaned)
        if any(clean_key.startswith(excluded) for excluded in EXCLUDED_TITLE_KEYS):
            return True
    return False


def poster_key_for(row: dict) -> tuple[str, str]:
    title = canonical_title(row)
    year = str(year_for(row) or "")
    title = re.sub(r"\[[^\]]*(?:\]|$)", " ", title)
    title = re.sub(r"\([^)]*(?:\)|$)", " ", title)
    if year:
        title = re.sub(rf"\s*{re.escape(year)}\s*$", "", title)
    title = QUALITY_RE.sub("", title)
    return normalized(title), year


def atomic_write(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    if not BASE.is_dir():
        raise RuntimeError(f"Persistent data directory does not exist: {BASE}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    summary = {
        "dataDirectory": str(BASE),
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "catalogues": {},
        "backups": [],
        "total3dRowsRemoved": 0,
        "totalExcludedMoviesRemoved": 0,
        "totalPosterFixes": 0,
    }
    for filename in CATALOGUES:
        path = BASE / filename
        if not path.is_file():
            summary["catalogues"][filename] = {"status": "missing"}
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            summary["catalogues"][filename] = {"status": "invalid_json", "error": str(exc)}
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            summary["catalogues"][filename] = {"status": "invalid_shape"}
            continue

        rows = [row for row in payload["results"] if isinstance(row, dict)]
        rows_3d_removed = sum(1 for row in rows if has_3d_quality(row))
        remaining = [row for row in rows if not has_3d_quality(row)]
        excluded_removed = 0
        if filename in FRANCHISE_CATALOGUES:
            kept = []
            for row in remaining:
                if excluded_franchise_title(row):
                    excluded_removed += 1
                else:
                    kept.append(row)
            remaining = kept

        poster_fixes = 0
        for row in remaining:
            poster_url = POSTER_OVERRIDES.get(poster_key_for(row))
            if poster_url and row.get("posterUrl") != poster_url:
                row["posterUrl"] = poster_url
                poster_fixes += 1

        remaining.sort(key=lambda row: (
            -year_for(row),
            normalized(canonical_title(row)),
            -(int(float(row.get("seeders") or 0)) if str(row.get("seeders") or "0").replace(".", "", 1).isdigit() else 0),
        ))
        unique_titles = {normalized(canonical_title(row)) for row in remaining if canonical_title(row)}
        payload["results"] = remaining
        payload["resultCount"] = len(remaining)
        payload["movieCount"] = len(unique_titles)

        changed = (
            rows_3d_removed > 0
            or excluded_removed > 0
            or poster_fixes > 0
            or remaining != rows
        )
        backup = ""
        if changed:
            backup_path = path.with_name(path.name + ".maintenance-backup-" + stamp)
            shutil.copy2(path, backup_path)
            summary["backups"].append(str(backup_path))
            atomic_write(path, payload)
            backup = str(backup_path)
            # Prune older rollback points only after the replacement catalogue
            # has been written successfully.
            for old_backup in sorted(
                path.parent.glob(path.name + ".maintenance-backup-*"),
                key=lambda item: (item.stat().st_mtime, item.name),
                reverse=True,
            )[1:]:
                try:
                    old_backup.unlink()
                except OSError as exc:
                    print(
                        f"WARNING: could not remove old backup {old_backup}: {exc}",
                        flush=True,
                    )

        summary["catalogues"][filename] = {
            "status": "updated" if changed else "already_clean",
            "rowsBefore": len(rows),
            "rowsAfter": len(remaining),
            "3dRowsRemoved": rows_3d_removed,
            "excludedRowsRemoved": excluded_removed,
            "posterFixes": poster_fixes,
            "backup": backup or None,
        }
        summary["total3dRowsRemoved"] += rows_3d_removed
        summary["totalExcludedMoviesRemoved"] += excluded_removed
        summary["totalPosterFixes"] += poster_fixes

    output = BASE / "cached_catalogue_maintenance_summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
