"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main

__all__ = ['empty_torrent_files', 'noop_pause', 'noop_resume', 'noop_delete', 'noop_prio', 'files_compat', 'folders_compat', 'storage_stats', 'users', 'logs', 'clear_logs', 'notifications', 'notifications_read', 'notifications_test', 'cleanup_settings', 'update_cleanup_settings', 'run_cleanup', '_feedback_client_key', '_enforce_feedback_rate_limit', 'submit_feedback', 'qbt_settings', 'update_qbt_settings', 'delete_file_compat', 'rename_file_compat', 'move_file_compat', 'create_folder_compat', 'share_folder_compat', 'switch_user', 'create_user', 'search_add_info', 'search_torrent_add', 'search_episode_release']

router = APIRouter()

@router.get("/api/v2/torrents/files")
async def empty_torrent_files(hash: str):
    return []

@router.post("/api/v2/torrents/pause")
async def noop_pause(body: dict[str, _main.Any]):
    return {}

@router.post("/api/v2/torrents/resume")
async def noop_resume(body: dict[str, _main.Any]):
    return {}

@router.post("/api/v2/torrents/delete")
async def noop_delete(body: dict[str, _main.Any]):
    return {}

@router.post("/api/v2/torrents/filePrio")
async def noop_prio(body: dict[str, _main.Any]):
    return {}

@router.get("/api/files")
async def files_compat(
    folder: str = "/",
    search: str = "",
    type: str = "all",
    folder_id: str = "",
):
    # Only load files for the folder currently open in the UI. Folder metadata
    # is returned separately by /api/folders.
    target_id = folder_id.strip()
    if not target_id:
        target_id = "0" if folder in {"/", "/Torrent Studio"} else ""

    if not target_id.isdigit():
        metadata = await _main.get_seedr_metadata_tree()
        for item in metadata.get("folders", []) if isinstance(metadata, dict) else []:
            if str(item.get("path") or "") == folder:
                target_id = str(item.get("id") or "")
                break

    if not target_id.isdigit():
        return []

    payload = await _main.seedr_folder_payload(target_id)
    result = []
    for raw in _main.arr(payload, ("files", "items")):
        f = _main.normalize_file(raw, target_id)
        name = f["name"]
        if search and search.lower() not in name.lower():
            continue

        lower = name.lower()
        if type != "all":
            if type == "video" and not _main.re.search(r"\.(mkv|mp4|m4v|webm|avi|mov|m3u8|ts)$", lower):
                continue
            if type == "audio" and not _main.re.search(r"\.(mp3|wav|flac|aac|ogg|m4a)$", lower):
                continue
            if type == "document" and not _main.re.search(r"\.(pdf|txt|doc|docx|xls|xlsx|ppt|pptx|csv)$", lower):
                continue
            if type == "archive" and not _main.re.search(r"\.(zip|rar|7z|tar|gz|bz2)$", lower):
                continue

        folder_path = folder if folder not in {"", "/"} else "/Torrent Studio"
        result.append({
            "id": f["id"],
            "name": name,
            "path": folder_path.rstrip("/") + "/" + name,
            "folder": folder_path,
            "size": f["size"],
            "type": "video" if _main.re.search(r"\.(mp4|mkv|webm|avi|mov|m4v|m3u8|ts)$", lower) else "other",
            "mimeType": "video/mp4" if _main.re.search(r"\.mp4$", lower) else "application/octet-stream",
            "createdAt": 0,
            "isStreamable": bool(_main.re.search(r"\.(mp4|mkv|webm|avi|mov|m3u8|ts|mp3|m4a|flac|aac|ogg)$", lower)),
            "ownerId": "seedr",
            "ownerName": "Seedr",
            "downloadUrl": f"/api/seedr/files/{f['id']}/download",
            "streamUrl": "",
        })
    return result

@router.get("/api/folders")
async def folders_compat():
    metadata = await _main.get_seedr_metadata_tree()
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or "Folder"),
            "path": str(item.get("path") or "/"),
            "ownerId": "seedr",
            "ownerName": "Seedr",
            "isShared": False,
            "permissions": {},
            "createdAt": 0,
            "filesCount": int(item.get("filesCount") or 0),
            "totalSize": int(item.get("totalSize") or 0),
        }
        for item in metadata.get("folders", [])
    ] if isinstance(metadata, dict) else []

@router.get("/api/storage/stats")
async def storage_stats():
    q = await _main.seedr_quota()
    metadata = await _main.get_seedr_metadata_tree()
    root = metadata.get("root") if isinstance(metadata, dict) else {}
    used = int(q.get("usedSpace") or 0)
    total = int(q.get("maxSpace") or 0)
    pct = (used / total * 100) if total else 0
    return {
        "totalBytes": total,
        "usedBytes": used,
        "freeBytes": max(0, total-used),
        "usedPercentage": pct,
        "filesCount": int((root or {}).get("filesCount") or 0),
        "torrentsCount": 0,
        "isUnlimited": False,
        "serverCapacityLabel": "Seedr cloud storage",
        "alertLevel": "critical" if pct > 90 else "warning" if pct > 80 else "normal",
    }

@router.get("/api/users")
async def users():
    user = {"id": "seedr-user", "name": "Seedr User", "email": "", "role": "admin", "avatar": ""}
    return {"users": [user], "activeUserId": user["id"], "activeUser": user}

@router.get("/api/logs")
async def logs():
    return []

@router.post("/api/logs/clear")
async def clear_logs():
    return {}

@router.get("/api/notifications")
async def notifications():
    return []

@router.post("/api/notifications/read")
async def notifications_read():
    return {}

@router.post("/api/notifications/test")
async def notifications_test():
    return {}

@router.get("/api/cleanup/settings")
async def cleanup_settings():
    return {"autoCleanCompletedDays": 7, "autoPurgeOrphans": False, "autoCleanTempFiles": False, "storageThresholdPercent": 80}

@router.post("/api/cleanup/settings")
async def update_cleanup_settings(body: dict[str, _main.Any]):
    return body

@router.post("/api/cleanup/run")
async def run_cleanup():
    return {"bytesFreed": 0, "filesRemoved": 0, "tempRemoved": 0, "orphansRemoved": 0}

def _feedback_client_key(request: _main.Request) -> str:
    # Render sits behind a proxy; the left-most X-Forwarded-For entry is the client.
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"

def _enforce_feedback_rate_limit(request: _main.Request) -> None:
    now = _main.time.monotonic()
    cutoff = now - _main.FEEDBACK_RATE_WINDOW_SECONDS

    while _main._feedback_global_hits and _main._feedback_global_hits[0] < cutoff:
        _main._feedback_global_hits.popleft()
    if len(_main._feedback_global_hits) >= _main.FEEDBACK_GLOBAL_RATE_LIMIT:
        raise _main.HTTPException(429, "Feedback is temporarily unavailable. Please try again later.")

    key = _main._feedback_client_key(request)
    hits = _main._feedback_hits.setdefault(key, _main.deque())
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= _main.FEEDBACK_RATE_LIMIT:
        raise _main.HTTPException(429, "You have sent feedback too many times. Please try again later.")

    hits.append(now)
    _main._feedback_global_hits.append(now)

    # Keep the table bounded.
    if len(_main._feedback_hits) > 5000:
        for stale_key in [k for k, v in _main._feedback_hits.items() if not v or v[-1] < cutoff]:
            _main._feedback_hits.pop(stale_key, None)

@router.post("/api/feedback")
async def submit_feedback(body: _main.FeedbackRequest, request: _main.Request):
    feedback_type = str(body.type or "").strip().lower()
    if feedback_type not in {"review", "suggestion", "bug"}:
        raise _main.HTTPException(400, "Invalid feedback type")

    message = str(body.message or "").strip()
    if len(message) < 5:
        raise _main.HTTPException(400, "Feedback is too short")
    if len(message) > 3000:
        raise _main.HTTPException(400, "Feedback is too long")

    name = str(body.name or "").strip()[:80]
    rating = int(body.rating) if body.rating is not None else None
    if feedback_type == "review" and (rating is None or rating < 1 or rating > 5):
        raise _main.HTTPException(400, "A review rating from 1 to 5 is required")
    if feedback_type != "review":
        rating = None

    if not _main.GITHUB_FEEDBACK_TOKEN:
        raise _main.HTTPException(503, "Feedback is not configured yet")

    repo = _main.GITHUB_FEEDBACK_REPO.strip()
    if not _main.re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise _main.HTTPException(500, "Feedback repository is not configured correctly")

    _main._enforce_feedback_rate_limit(request)

    label = {"review": "Review", "suggestion": "Suggestion", "bug": "Bug report"}[feedback_type]
    title_prefix = {"review": "Review", "suggestion": "Suggestion", "bug": "Bug report"}[feedback_type]
    title_text = message.replace("\\n", " ").strip()
    title_text = _main.re.sub(r"\s+", " ", title_text)[:70] or "New feedback"

    if feedback_type == "review" and rating is not None:
        stars = "⭐" * rating
        title = f"{stars} {rating}/5 Review"
    else:
        title = title_prefix

    if name:
        title += f" — {name}"

    submitted_at = _main.datetime.now(_main.timezone.utc).isoformat()

    body_lines = [
        f"# {('⭐' * rating + ' ' + str(rating) + '/5 Review') if feedback_type == 'review' and rating is not None else label}",
        "",
    ]
    if name:
        body_lines.append(f"**👤 Name:** {name}")
    else:
        body_lines.append("**👤 Name:** Anonymous")
    if rating is not None:
        body_lines.append(f"**⭐ Rating:** {rating}/5")
    body_lines.extend([
        f"**🕒 Submitted:** {submitted_at}",
        "",
        "### 💬 Feedback",
        "",
        f"> {message.replace(chr(10), chr(10) + '> ')}",
    ])
    # The visitor IP is only used for rate limiting; it is never written to the issue.

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {_main.GITHUB_FEEDBACK_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "Torrent-Studio-Feedback",
    }
    try:
        async with _main._InstrumentedAsyncClient(timeout=12.0) as client:
            response = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers=headers,
                json={"title": title, "body": "\n".join(body_lines)},
            )
    except _main.httpx.HTTPError as exc:
        _main.logger.warning("Feedback submission failed: %s", exc)
        raise _main.HTTPException(502, "Feedback service is temporarily unavailable")

    if response.status_code != 201:
        try:
            detail = response.json().get("message")
        except Exception:
            detail = None
        _main.logger.warning("GitHub feedback submission failed: HTTP %s %s", response.status_code, detail or "")
        raise _main.HTTPException(502, "Feedback could not be submitted right now")

    try:
        issue = response.json()
    except Exception:
        issue = {}
    return {"submitted": True, "issueUrl": issue.get("html_url")}

@router.get("/api/qbt/settings")
async def qbt_settings():
    return {"isExternal": False, "host": "", "username": "", "connected": False, "version": "Seedr backend"}

@router.post("/api/qbt/settings")
async def update_qbt_settings(body: dict[str, _main.Any]):
    return {"isExternal": False, "host": "", "username": "", "connected": False, "version": "Seedr backend"}

@router.post("/api/files/delete")
async def delete_file_compat(body: dict[str, _main.Any]):
    return await _main.seedr_file_delete(str(body.get("id") or ""))

@router.post("/api/files/rename")
async def rename_file_compat(body: dict[str, _main.Any]):
    raise _main.HTTPException(501, "Rename is not exposed by this Seedr API deployment")

@router.post("/api/files/move")
async def move_file_compat(body: dict[str, _main.Any]):
    raise _main.HTTPException(501, "Move is not exposed by this Seedr API deployment")

@router.post("/api/files/folder")
async def create_folder_compat(body: dict[str, _main.Any]):
    raise _main.HTTPException(501, "Folder creation is not exposed by this Seedr API deployment")

@router.post("/api/folders/share")
async def share_folder_compat(body: dict[str, _main.Any]):
    raise _main.HTTPException(501, "Sharing is not provided by Seedr")

@router.post("/api/users/switch")
async def switch_user(body: dict[str, _main.Any]):
    return {"activeUser": {"id": "seedr-user", "name": "Seedr User", "email": "", "role": "admin", "avatar": ""}}

@router.post("/api/users/create")
async def create_user(body: dict[str, _main.Any]):
    raise _main.HTTPException(501, "Multi-user accounts are not provided by this deployment")

@router.get("/api/search/torrents/add")
async def search_add_info():
    return _main.JSONResponse({"added": False, "reason": "Use the Seedr magnet action"}, status_code=405)

@router.post("/api/search/torrents/add")
async def search_torrent_add(body: dict[str, _main.Any]):
    source = str(body.get("source") or "")
    magnet = source if source.lower().startswith("magnet:") else ""
    if not magnet and body.get("infoHash"):
        magnet = "magnet:?xt=urn:btih:" + str(body["infoHash"])
    if not magnet:
        return {"added": False, "reason": "no_magnet_or_info_hash"}
    result = await _main.seedr_add(
        _main.MagnetRequest(
            magnet=magnet,
            torrent_name=str(body.get("torrent_name") or "").strip() or None,
        )
    )
    return {"added": True, **result}

@router.get("/api/search/episode")
async def search_episode_release(
    title: str = _main.Query(..., min_length=1, max_length=180),
    season: int = _main.Query(..., ge=1, le=99),
    episode: int = _main.Query(..., ge=1, le=999),
):
    query = f"{title.strip()} S{season:02d}E{episode:02d}"
    try:
        candidates = await _main._search_1337x_uncached(query, allow_series_fallback=False)
    except Exception as exc:
        _main.logger.warning("Episode release search failed for %r: %s", query, exc)
        raise _main.HTTPException(status_code=502, detail="Episode release search temporarily failed")

    token = _main.re.compile(rf"(?i)(?<![A-Z0-9])S{season:02d}E{episode:02d}(?![0-9])")
    rejected_pack = _main.re.compile(r"(?i)\b(complete series|season pack|all seasons|complete season)\b")
    results = []
    seen = set()
    for item in candidates if isinstance(candidates, list) else []:
        name = str(item.get("title") or "").strip()
        if not name or not token.search(name) or rejected_pack.search(name):
            continue
        link = str(item.get("url") or item.get("detailUrl") or item.get("magnet") or "").strip()
        identity = link or name.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        results.append({
            "title": name,
            "url": link,
            "episode": f"S{season:02d}E{episode:02d}",
            "provider": str(item.get("indexer") or item.get("provider") or ""),
            "seeders": item.get("seeders"),
            "size": item.get("size"),
            "infoHash": item.get("infoHash") or item.get("info_hash"),
            "magnet": item.get("magnet") or item.get("magnetUrl"),
        })
        if len(results) >= 8:
            break
    return {
        "status": "found" if results else "not_found",
        "query": query,
        "results": results,
        "providers": sorted({item["provider"] for item in results if item["provider"]}),
    }
