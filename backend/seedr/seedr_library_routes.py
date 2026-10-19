"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['_seedr_file_folder_id', '_seedr_effective_task_folder_id', '_remember_seedr_cleanup_folder', 'seedr_task_progress', 'seedr_task', 'seedr_folder_payload', 'direct_folder_summary', 'build_seedr_metadata_tree', 'get_seedr_metadata_tree', 'seedr_library_metadata', 'seedr_folder_contents', 'seedr_files', 'seedr_file_download_url', 'seedr_file_download', 'seedr_file_download_direct', 'seedr_folder_download_url', 'seedr_folder_download_url_api', 'seedr_folder_download', 'seedr_folder_download_direct', '_seedr_media_url', 'seedr_v2_bearer_token', 'seedr_v2_request', 'seedr_v2_video_url', '_absolute_hls_uri', '_encode_hls_target', '_decode_hls_target', '_rewrite_hls_manifest', '_presentation_is_hls', '_fetch_seedr_hls_manifest', '_get_hls_source', 'resolve_seedr_stream_id', 'seedr_file_stream', 'seedr_hls_manifest', 'seedr_hls_resource', '_seedr_media_source_url']

router = APIRouter()

def _seedr_file_folder_id(files: list[dict[str, _main.Any]]) -> str:
    """Return the actual torrent folder id exposed by completed file rows."""
    for file in files:
        folder_id = str(file.get("folderId") or "").strip()
        if folder_id:
            return folder_id
    return ""

def _seedr_effective_task_folder_id(
    task: dict[str, _main.Any],
    files: list[dict[str, _main.Any]] | None = None,
) -> str:
    """Prefer the created torrent folder over the destination parent folder."""
    task_folder_id = _main.seedr_task_folder_id(task)
    file_folder_id = _main._seedr_file_folder_id(files or [])

    if file_folder_id and (
        not task_folder_id
        or file_folder_id != task_folder_id
        or task_folder_id == "0"
    ):
        return file_folder_id
    return task_folder_id

def _remember_seedr_cleanup_folder(tid: str, folder_id: str) -> None:
    folder_id = str(folder_id or "").strip()
    if not folder_id:
        return
    job = _main._seedr_cleanup_jobs.get(str(tid).strip())
    if not job:
        return
    if str(job.get("folderId") or "").strip() == folder_id:
        return
    job["folderId"] = folder_id
    _main._save_seedr_cleanup_jobs()

@router.get("/api/seedr/tasks/{tid}/progress")
async def seedr_task_progress(tid: str):
    """Fast polling endpoint: only fetch task state/progress from Seedr."""
    try:
        raw = _main.seedr_data(await _main.seedr_request(f"/tasks/{_main.quote(tid)}"))
    except _main.HTTPException as exc:
        if exc.status_code == 404:
            return {
                "taskId": tid,
                "status": "not_found",
                "progress": 0,
                "name": "",
                "folderId": "",
            }
        raise

    task = (
        raw.get("task")
        if isinstance(raw, dict) and isinstance(raw.get("task"), dict)
        else (raw if isinstance(raw, dict) else {})
    )
    progress = float(task.get("progress") or 0)
    complete = _main.task_complete(task)
    if complete:
        progress = 100

    state = str(task.get("state") or task.get("status") or "").lower()
    status = "completed" if complete else (
        "waiting" if state in {"queued", "pending", "waiting", "paused", "stopped"} else "downloading"
    )

    task_folder_id = _main.seedr_task_folder_id(task)
    completed_files: list[dict[str, Any]] = []
    if complete:
        try:
            completed_files = await _main.task_contents(tid)
        except (_main.HTTPException, _main.SeedrError):
            completed_files = []

    effective_folder_id = _main._seedr_effective_task_folder_id(task, completed_files)
    task_id_value = str(tid).strip()

    canonical_name = str(
        _main._seedr_torrent_names_by_task.get(task_id_value)
        or _main._seedr_torrent_names.get(effective_folder_id)
        or _main.seedr_task_name(task)
        or ""
    ).strip()

    if effective_folder_id and canonical_name:
        _main._seedr_torrent_names[effective_folder_id] = canonical_name
        if complete:
            _main._remember_seedr_cleanup_folder(task_id_value, effective_folder_id)
        # Seedr can expose the folder only after the task starts. Rename at
        # that point, rather than only immediately after /tasks POST.
        # Do not call Seedr folder-management endpoints from the free-account
        # progress path. The task state itself is sufficient for polling.

    seedr_task_display_name = canonical_name
    return {
        "taskId": tid,
        "status": status,
        "progress": progress,
        "name": seedr_task_display_name,
        "folderId": effective_folder_id,
    }

@router.get("/api/seedr/tasks/{tid}")
async def seedr_task(tid: str):
    try:
        raw = _main.seedr_data(await _main.seedr_request(f"/tasks/{_main.quote(tid)}"))
    except _main.HTTPException as exc:
        if exc.status_code == 404:
            return {"taskId": tid, "status": "not_found", "progress": 0, "files": [], "downloadUrl": None}
        raise
    task = raw.get("task") if isinstance(raw, dict) and isinstance(raw.get("task"), dict) else (raw if isinstance(raw, dict) else {})
    progress = float(task.get("progress") or 0)
    complete = _main.task_complete(task)
    if complete:
        progress = 100
    files = await _main.task_contents(tid)
    folder_id = _main._seedr_effective_task_folder_id(task, files)
    _main._remember_seedr_cleanup_folder(str(tid).strip(), folder_id)
    task_id_value = str(tid).strip()
    canonical_name = str(
        _main._seedr_torrent_names_by_task.get(task_id_value)
        or _main._seedr_torrent_names.get(folder_id)
        or _main.seedr_task_name(task)
        or ""
    ).strip()
    if folder_id and canonical_name:
        previous_name = _main._seedr_torrent_names.get(folder_id)
        _main._seedr_torrent_names[folder_id] = canonical_name
        if previous_name != canonical_name:
            try:
                await _main.rename_seedr_folder(folder_id, canonical_name)
            except Exception:
                pass
            global _seedr_metadata_cache
            _main._seedr_metadata_cache = None
    folderNameValue = canonical_name or (await _main.folder_name(folder_id) if folder_id else "")
    for f in files:
        f["folderPath"] = "/Torrent Studio" + ("/" + folderNameValue if folderNameValue else "")
        # Do not resolve direct Seedr URLs while polling/finalizing a task.
        # A fresh URL is generated only by an explicit download/copy/stream action.
        f["url"] = None
    return {"taskId": tid, "name": str(task.get("title") or task.get("name") or ""), "folderName": folderNameValue, "folderId": folder_id, "status": "completed" if complete else "downloading", "progress": progress, "task": task, "files": files, "downloadUrl": None}

async def seedr_folder_payload(folder_id: str) -> dict[str, _main.Any]:
    """Fetch one Seedr folder level, with a short-lived in-process cache."""
    folder_id = str(folder_id).strip()
    if not folder_id:
        return {}

    now = _main.asyncio.get_running_loop().time()
    cached = _main._seedr_folder_cache.get(folder_id)
    if cached and now - cached[0] < _main.SEEDR_FOLDER_CACHE_SECONDS:
        return cached[1]

    async with _main._seedr_folder_semaphore:
        # Re-check after waiting for the semaphore so concurrent callers do
        # not issue duplicate Seedr requests for the same folder.
        now = _main.asyncio.get_running_loop().time()
        cached = _main._seedr_folder_cache.get(folder_id)
        if cached and now - cached[0] < _main.SEEDR_FOLDER_CACHE_SECONDS:
            return cached[1]

        try:
            # Root access is inconsistent across Seedr API variants/accounts.
            # Prefer the normal bearer-authenticated filesystem endpoint, then
            # fall back to the dedicated root endpoint and finally the legacy
            # list_contents endpoint. A failure in one variant must not be
            # presented as a token failure when another variant works.
            if folder_id == "0":
                root_errors: list[str] = []

                try:
                    payload = _main.seedr_data(await _main.seedr_request("/fs/folder/0/contents"))
                    _main.logger.info("Seedr library root resolved via bearer fs endpoint")
                except (_main.HTTPException, _main.SeedrError) as exc:
                    root_errors.append(f"fs:{getattr(exc, 'status_code', 0)}")
                    try:
                        payload = _main.seedr_data(await _main.seedr_root_request())
                        _main.logger.info("Seedr library root resolved via dedicated root endpoint")
                    except (_main.HTTPException, _main.SeedrError) as root_exc:
                        root_errors.append(f"root:{getattr(root_exc, 'status_code', 0)}")
                        try:
                            payload = _main.seedr_data(await _main.legacy_seedr_list_contents("0"))
                            _main.logger.info("Seedr library root resolved via legacy list_contents endpoint")
                        except (_main.HTTPException, _main.SeedrError) as legacy_exc:
                            root_errors.append(f"legacy:{getattr(legacy_exc, 'status_code', 0)}")
                            _main.logger.warning(
                                "Seedr library root resolution failed across all API variants: %s",
                                ",".join(root_errors),
                            )
                            raise legacy_exc
            else:
                payload = _main.seedr_data(await _main.seedr_request(f"/fs/folder/{_main.quote(folder_id)}/contents"))
        except _main.HTTPException as exc:
            if exc.status_code == 404:
                return {}
            raise

    result = payload if isinstance(payload, dict) else {}
    _main._seedr_folder_cache[folder_id] = (_main.asyncio.get_running_loop().time(), result)
    return result

def direct_folder_summary(folder_id: str, path: str, payload: dict[str, _main.Any]) -> dict[str, _main.Any]:
    files = _main.arr(payload, ("files", "items"))
    folders = _main.arr(payload, ("folders", "directories"))
    total_size = 0
    valid_files = 0
    for raw in files:
        if not isinstance(raw, dict):
            continue
        try:
            size = int(float(raw.get("size") or 0))
        except Exception:
            size = 0
        total_size += max(0, size)
        if str(raw.get("id") or raw.get("file_id") or "").strip():
            valid_files += 1

    return {
        "id": str(folder_id),
        "folderId": str(folder_id),
        "name": _main.Path(path.rstrip("/")).name or "Root Files",
        "path": path,
        "filesCount": valid_files,
        "totalSize": total_size,
        "folderCount": len([x for x in folders if isinstance(x, dict)]),
    }

async def build_seedr_metadata_tree(
    folder_id: str,
    path: str,
    folder_name_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, _main.Any], list[dict[str, _main.Any]]]:
    """
    Build the first library response from the root and its immediate child
    folders. Child contents are fetched concurrently so folder cards already
    have exact file counts/sizes when the browser receives this response.
    """
    folder_name_overrides = folder_name_overrides or {}
    payload = await _main.seedr_folder_payload(folder_id)
    summary = _main.direct_folder_summary(folder_id, path, payload)

    child_entries: list[tuple[str, str, dict[str, Any]]] = []
    for raw in _main.arr(payload, ("folders", "directories")):
        if not isinstance(raw, dict):
            continue
        child_id = str(raw.get("id") or raw.get("folder_id") or "").strip()
        if not child_id:
            continue
        folder_override = str(folder_name_overrides.get(child_id) or "").strip()
        child_name = (
            folder_override
            or str(raw.get("name") or raw.get("title") or child_id).strip()
            or child_id
        )
        child_path = path.rstrip("/") + "/" + child_name
        child_entries.append((child_id, child_name, raw))

    async def load_child(entry: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
        child_id, child_name, raw = entry
        child_payload = await _main.seedr_folder_payload(child_id)

        # Older Seedr folders can be exposed with their raw 40-character
        # identifier as the visible name. For a single-file torrent we can
        # recover the human title from the actual file name and normalize the
        # folder once, so existing downloads also get fixed on refresh.
        effective_child_name = child_name
        child_files = _main.arr(child_payload, ("files", "items"))
        if (
            _main.re.fullmatch(r"[0-9a-fA-F]{40}", effective_child_name)
            and len(child_files) == 1
            and isinstance(child_files[0], dict)
        ):
            raw_file_name = str(
                child_files[0].get("name")
                or child_files[0].get("title")
                or ""
            ).strip()
            inferred_name = _main.Path(raw_file_name.replace("\\", "/")).name
            inferred_name = _main.Path(inferred_name).stem.strip()
            if inferred_name:
                try:
                    renamed = await _main.rename_seedr_folder(child_id, inferred_name)
                except Exception:
                    renamed = False
                _main._seedr_torrent_names[child_id] = inferred_name
                effective_child_name = inferred_name
                if renamed:
                    _main.logger.info(
                        "Renamed legacy hash-named Seedr folder %s to %s",
                        child_id,
                        inferred_name,
                    )

        child_summary = _main.direct_folder_summary(
            child_id,
            path.rstrip("/") + "/" + effective_child_name,
            child_payload,
        )

        # Some Seedr responses expose size/count directly on the folder item;
        # use those only when the contents endpoint did not provide a value.
        if child_summary["filesCount"] == 0:
            for key in ("files_count", "file_count", "filesCount", "fileCount", "count"):
                value = raw.get(key)
                if value is not None:
                    try:
                        child_summary["filesCount"] = max(0, int(value))
                        break
                    except (TypeError, ValueError):
                        pass

        if child_summary["totalSize"] == 0:
            for key in ("size", "total_size", "totalSize"):
                value = raw.get(key)
                if value is not None:
                    try:
                        child_summary["totalSize"] = max(0, int(float(value)))
                        break
                    except (TypeError, ValueError):
                        pass

        child_summary["torrentName"] = (
            _main._seedr_torrent_names.get(child_id)
            or folder_name_overrides.get(child_id)
            or (effective_child_name if _main.re.fullmatch(r"[0-9a-fA-F]{40}", child_name) is None else "")
            or ""
        )
        child_summary["folderCount"] = len(
            [x for x in _main.arr(child_payload, ("folders", "directories")) if isinstance(x, dict)]
        )
        return child_summary

    children: list[dict[str, Any]] = []
    if child_entries:
        results = await _main.asyncio.gather(
            *(load_child(entry) for entry in child_entries),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, dict):
                children.append(result)

    # The top-level library badge represents all files in its visible torrent
    # folders, not just files placed directly in the root.
    summary["filesCount"] += sum(int(item.get("filesCount") or 0) for item in children)
    summary["totalSize"] += sum(int(item.get("totalSize") or 0) for item in children)
    summary["folderCount"] = len(children)

    return summary, children

async def get_seedr_metadata_tree(force_refresh: bool = False) -> dict[str, _main.Any]:
    global _seedr_metadata_cache, _seedr_metadata_task

    if not _main.current_seedr_token():
        return {"configured": False, "root": None, "folders": []}

    now = _main.asyncio.get_running_loop().time()

    if force_refresh:
        # A completed Seedr task can become visible in the filesystem a few
        # seconds after the metadata cache was populated. Clear both caches so
        # the completion flow can see the new folder immediately.
        _main._seedr_metadata_cache = None
        _main._seedr_folder_cache.clear()

    if _main._seedr_metadata_cache and now - _main._seedr_metadata_cache[0] < _main.SEEDR_METADATA_CACHE_SECONDS:
        return _main._seedr_metadata_cache[1]

    if _main._seedr_metadata_task is not None and not _main._seedr_metadata_task.done():
        return await _main._seedr_metadata_task

    async def build() -> dict[str, Any]:
        # Seedr's account root is exposed through a dedicated endpoint.
        # Keep SEEDR_LIBRARY_FOLDER_ID as an optional override for deployments
        # that want to start from a specific existing folder.
        root = "0"

        # Resolve human-readable torrent names from Seedr task metadata in one
        # call. The folder contents/counts and task list are independent, so
        # fetch them concurrently without adding a serial round trip.
        folder_name_overrides: dict[str, str] = dict(_main._seedr_torrent_names)
        task_folders: list[tuple[str, str]] = []
        try:
            tasks_payload = _main.seedr_data(await _main.seedr_request("/tasks"))
            for raw_task in _main.arr(tasks_payload, ("tasks", "torrents", "items")):
                task = _main.unwrap_seedr_task(_main.seedr_data(raw_task))
                if not task:
                    continue

                task_folder_id = _main.seedr_task_folder_id(task)
                task_name = _main.seedr_task_name(task)

                if task_folder_id:
                    task_folders.append((task_folder_id, task_name))

                if task_folder_id and task_name:
                    # The title supplied by our search/add flow is canonical.
                    # Seedr's task title can contain provider/site prefixes
                    # (for example "www.UIndex.org - ..."), so never let task
                    # metadata overwrite an existing canonical title.
                    if task_folder_id not in _main._seedr_torrent_names:
                        _main._seedr_torrent_names[task_folder_id] = task_name
                    folder_name_overrides[task_folder_id] = (
                        _main._seedr_torrent_names.get(task_folder_id) or task_name
                    )
        except (_main.HTTPException, _main.SeedrError):
            # Folder metadata remains usable even when task-name lookup fails.
            pass

        try:
            root_summary, children = await _main.build_seedr_metadata_tree(
                root,
                "/Torrent Studio",
                folder_name_overrides,
            )
        except (_main.HTTPException, _main.SeedrError) as exc:
            # Some Seedr tokens can authenticate and manage transfers while
            # denying the account-root filesystem listing. Do not turn that
            # provider limitation into a library-wide error. Reconstruct the
            # visible library from the task list and each task's own contents.
            if getattr(exc, "status_code", 0) not in {401, 403}:
                raise

            # First collect folder IDs from the task list. Seedr may expose
            # the created folder only after a task has started/completed, so
            # also resolve task contents below when the task object has no
            # folder_created_id.
            task_candidates: list[dict[str, Any]] = []
            try:
                tasks_payload = _main.seedr_data(await _main.seedr_request("/tasks"))
                for raw_task in _main.arr(tasks_payload, ("tasks", "torrents", "items")):
                    task = _main.unwrap_seedr_task(_main.seedr_data(raw_task))
                    if task:
                        task_candidates.append(task)
            except (_main.HTTPException, _main.SeedrError):
                task_candidates = []

            unique_folders: dict[str, str] = {}

            for task in task_candidates:
                tid = _main.task_id(task)
                task_name = (
                    _main.seedr_task_name(task)
                    or (folder_name_overrides.get(_main.seedr_task_folder_id(task)) if _main.seedr_task_folder_id(task) else "")
                    or f"Torrent {tid}"
                ).strip()
                folder_id = _main.seedr_task_folder_id(task)

                # If the task itself does not expose its created folder, the
                # task contents endpoint often does. This is the same endpoint
                # already used by the completion/progress flow.
                if tid and (not folder_id or folder_id == "0"):
                    try:
                        task_files = await _main.task_contents(tid)
                        folder_id = _main._seedr_file_folder_id(task_files)
                    except (_main.HTTPException, _main.SeedrError):
                        folder_id = ""

                if folder_id and folder_id != root:
                    unique_folders.setdefault(
                        folder_id,
                        folder_name_overrides.get(folder_id)
                        or _main._seedr_torrent_names.get(folder_id)
                        or task_name
                        or folder_id,
                    )

            # Preserve any folder IDs already learned from task metadata.
            for folder_id, task_name in task_folders:
                if folder_id and folder_id != root:
                    unique_folders.setdefault(
                        folder_id,
                        folder_name_overrides.get(folder_id)
                        or _main._seedr_torrent_names.get(folder_id)
                        or task_name
                        or folder_id,
                    )

            async def load_task_folder(folder_id: str, name: str) -> dict[str, Any] | None:
                try:
                    payload = await _main.seedr_folder_payload(folder_id)
                except (_main.HTTPException, _main.SeedrError):
                    return None
                if not payload:
                    return None
                summary = _main.direct_folder_summary(
                    folder_id,
                    "/Torrent Studio/" + name,
                    payload,
                )
                summary["torrentName"] = name
                summary["folderCount"] = len(
                    [x for x in _main.arr(payload, ("folders", "directories")) if isinstance(x, dict)]
                )
                return summary

            if unique_folders:
                results = await _main.asyncio.gather(
                    *(load_task_folder(folder_id, name) for folder_id, name in unique_folders.items()),
                    return_exceptions=True,
                )
                children = [result for result in results if isinstance(result, dict)]
            else:
                children = []

            # A denied root listing is a capability limitation, not an
            # authentication failure. Returning an empty but valid library
            # keeps Search/Prepare/Stream usable even for an account with no
            # folders exposed through the token.
            root_summary = {
                "id": root,
                "folderId": root,
                "name": "Torrent Studio",
                "path": "/Torrent Studio",
                "filesCount": sum(int(item.get("filesCount") or 0) for item in children),
                "totalSize": sum(int(item.get("totalSize") or 0) for item in children),
                "folderCount": len(children),
            }

        return {
            "configured": True,
            "root": root_summary,
            "folders": children,
        }

    _main._seedr_metadata_task = _main.asyncio.create_task(build())
    try:
        result = await _main._seedr_metadata_task
        _main._seedr_metadata_cache = (_main.asyncio.get_running_loop().time(), result)
        return result
    finally:
        _main._seedr_metadata_task = None

@router.get("/api/seedr/library")
async def seedr_library_metadata(fresh: bool = _main.Query(False)):
    return await _main.get_seedr_metadata_tree(force_refresh=fresh)

@router.get("/api/seedr/folders/{folder_id}/contents")
async def seedr_folder_contents(folder_id: str):
    if not _main.current_seedr_token():
        return {"configured": False, "folderId": folder_id, "files": [], "folders": []}

    payload = await _main.seedr_folder_payload(folder_id)
    files: list[dict[str, Any]] = []
    for raw in _main.arr(payload, ("files", "items")):
        file = _main.normalize_file(raw, folder_id)
        file["url"] = None
        files.append(file)

    folders: list[dict[str, Any]] = []
    for raw in _main.arr(payload, ("folders", "directories")):
        if not isinstance(raw, dict):
            continue
        child_id = str(raw.get("id") or raw.get("folder_id") or "").strip()
        if not child_id:
            continue
        child_name = str(raw.get("name") or raw.get("title") or child_id).strip() or child_id
        folders.append({"id": child_id, "folderId": child_id, "name": child_name})

    return {"configured": True, "folderId": folder_id, "files": files, "folders": folders}

@router.get("/api/seedr/files")
async def seedr_files():
    # Backwards-compatible full file endpoint. New UI code uses
    # /api/seedr/library + /api/seedr/folders/{id}/contents instead.
    if not _main.current_seedr_token():
        return {"configured": False, "files": []}

    root = "0"

    result = await _main.collect_folder(root, "/Torrent Studio")
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result:
        item_id = str(item.get("id") or "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            unique.append(item)

    return {"configured": True, "files": unique}

@router.get("/api/seedr/files/{file_id}/download/url")
async def seedr_file_download_url(file_id: str):
    # Resolve a fresh direct Seedr URL only when the user explicitly requests it.
    return await _main.download_url(file_id)

@router.get("/api/seedr/files/{file_id}/download")
async def seedr_file_download(file_id: str):
    # Redirect the browser to Seedr's temporary direct URL so large downloads
    # do not flow through the Render Free instance.
    result = await _main.download_url(file_id)
    return _main.RedirectResponse(result["url"], status_code=307)

@router.get("/api/seedr/files/{file_id}/download/direct")
async def seedr_file_download_direct(
    file_id: str,
    filename: str = _main.Query(""),
):
    """Stream a Seedr file with an explicit browser download filename."""
    result = await _main.download_url(file_id)
    filename_value = _main._safe_download_filename(
        filename or result.get("name") or "",
        f"seedr-file-{file_id}",
    )
    body, content_type, headers = await _main._stream_seedr_download(result["url"])
    ascii_name = filename_value.encode("ascii", errors="ignore").decode("ascii").strip() or f"seedr-file-{file_id}"
    headers["Content-Disposition"] = (
        'attachment; filename="' + ascii_name.replace('"', "_") + '"'
        + "; filename*=UTF-8''" + _main.quote(filename_value, safe="")
    )
    return _main.StreamingResponse(
        body(),
        media_type=content_type,
        headers=headers,
    )

async def seedr_folder_download_url(folder_id: str) -> str:
    folder_id = str(folder_id or "").strip()
    if not folder_id or not folder_id.isdigit():
        raise _main.HTTPException(400, "Invalid Seedr folder id")

    # Known-good Torrent Studio implementation:
    # initialize a temporary archive and let Seedr return the signed URL.
    archive_id = str(_main.uuid.uuid4())
    payload = _main.seedr_data(
        await _main.seedr_request(
            f"/download/archive/init/{_main.quote(archive_id)}",
            "PUT",
            {"archive_arr": [{"type": "folder", "id": int(folder_id)}]},
        )
    )

    if isinstance(payload, dict):
        url = str(
            payload.get("url")
            or payload.get("download_url")
            or payload.get("downloadUrl")
            or payload.get("signed_url")
            or payload.get("signedUrl")
            or ""
        ).strip()
    elif isinstance(payload, str):
        url = payload.strip()
    else:
        url = ""

    if not url:
        raise _main.HTTPException(502, "Seedr did not return a folder download URL")
    return url

@router.get("/api/seedr/folders/{folder_id}/download/url")
async def seedr_folder_download_url_api(folder_id: str):
    # Resolve the temporary direct Seedr folder URL only on an explicit click.
    url = await _main.seedr_folder_download_url(folder_id)
    return {"url": url}

@router.get("/api/seedr/folders/{folder_id}/download")
async def seedr_folder_download(folder_id: str):
    # Redirect the browser to Seedr's temporary direct folder URL rather than
    # streaming the archive through Render.
    url = await _main.seedr_folder_download_url(folder_id)
    return _main.RedirectResponse(url, status_code=307)

@router.get("/api/seedr/folders/{folder_id}/download/direct")
async def seedr_folder_download_direct(
    folder_id: str,
    filename: str = _main.Query(""),
):
    """Stream a Seedr folder archive with an explicit .zip filename."""
    url = await _main.seedr_folder_download_url(folder_id)
    filename_value = _main._safe_download_filename(
        filename,
        f"seedr-folder-{folder_id}.zip",
    )
    if not filename_value.lower().endswith(".zip"):
        filename_value += ".zip"

    body, content_type, headers = await _main._stream_seedr_download(url)
    ascii_name = filename_value.encode("ascii", errors="ignore").decode("ascii").strip() or f"seedr-folder-{folder_id}.zip"
    headers["Content-Disposition"] = (
        'attachment; filename="' + ascii_name.replace('"', "_") + '"'
        + "; filename*=UTF-8''" + _main.quote(filename_value, safe="")
    )
    return _main.StreamingResponse(
        body(),
        media_type=content_type or "application/zip",
        headers=headers,
    )

def _seedr_media_url(file_id: str, media_type: str) -> str:
    if media_type == "video":
        endpoint = f"/media/hls/{_main.quote(file_id)}"
    elif media_type == "audio":
        endpoint = f"/media/mp3/{_main.quote(file_id)}"
    else:
        raise _main.HTTPException(400, "Unsupported Seedr media type")
    return _main.SEEDR_MEDIA_BASE.rstrip("/") + endpoint + "?access_token=" + _main.quote(_main.current_seedr_token(), safe="")

def seedr_v2_bearer_token() -> str:
    """Accept a raw Seedr PAT or MediaFusion-style base64 JSON token."""
    raw = _main.current_seedr_token().strip()
    if not raw:
        return ""
    try:
        decoded = _main.base64.b64decode(raw, validate=True).decode("utf-8")
        payload = _main.json.loads(decoded)
        token = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
        if token:
            return token
    except Exception:
        pass
    return raw

async def seedr_v2_request(path: str) -> _main.Any:
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")
    bearer = _main.seedr_v2_bearer_token()
    if not bearer:
        raise _main.HTTPException(503, "Seedr access token is empty")
    url = _main.SEEDR_V2_BASE.rstrip("/") + "/" + str(path).lstrip("/")
    async with _main._InstrumentedAsyncClient(timeout=_main.httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        )
    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw
    if response.status_code >= 400:
        raise _main.HTTPException(
            response.status_code,
            _main.seedr_error_message(response.status_code, data, raw),
        )
    return data

async def seedr_v2_video_url(file_id: str, audio_index: int | None = None) -> str:
    """Use Seedr V2 current presentation URL, with direct-download fallback."""
    if not file_id:
        return ""
    try:
        presentation_path = f"/presentations/file/{_main.quote(file_id)}/video"
        if audio_index is not None and audio_index >= 0:
            presentation_path += "?" + _main.urlencode({"audio": str(audio_index)})
        payload = _main.seedr_data(await _main.seedr_v2_request(presentation_path))
        if isinstance(payload, dict):
            link = payload.get("link")
            link_url = link.get("url") if isinstance(link, dict) else ""
            url = str(payload.get("url") or payload.get("stream_url") or link_url or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    except _main.HTTPException:
        pass

    try:
        payload = _main.seedr_data(await _main.seedr_v2_request(f"/download/file/{_main.quote(file_id)}/url"))
        if isinstance(payload, dict):
            url = str(payload.get("url") or payload.get("download_url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    except _main.HTTPException:
        pass

    return ""

def _absolute_hls_uri(base_url: str, uri: str) -> str:
    absolute = _main.urljoin(base_url, uri)
    # Some HLS manifests use one access query string on the playlist URL and
    # omit it from relative segment/variant URLs. Carry it forward.
    if not _main.urlsplit(uri).query:
        base_query = _main.urlsplit(base_url).query
        if base_query and not _main.urlsplit(absolute).query:
            absolute += "?" + base_query
    return absolute

def _encode_hls_target(url: str) -> str:
    return _main.base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")

def _decode_hls_target(value: str) -> str:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return _main.base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except Exception as exc:
        raise _main.HTTPException(400, "Invalid HLS resource token") from exc

def _rewrite_hls_manifest(file_id: str, manifest_text: str, base_url: str) -> str:
    lines = manifest_text.splitlines()
    rewritten: list[str] = []

    def proxy_url(absolute: str) -> str:
        return "/api/seedr/hls/" + _main.quote(file_id, safe="") + "/resource?u=" + _main._encode_hls_target(absolute)

    for line in lines:
        current = line
        def replace_uri(match: _main.re.Match[str]) -> str:
            uri = match.group(1)
            return 'URI="' + proxy_url(_main._absolute_hls_uri(base_url, uri)) + '"'
        current = _main.re.sub(r'URI="([^"]+)"', replace_uri, current)

        stripped = current.strip()
        if stripped and not stripped.startswith("#"):
            current = proxy_url(_main._absolute_hls_uri(base_url, stripped))
        rewritten.append(current)

    return "\n".join(rewritten) + ("\n" if manifest_text.endswith("\n") else "")

async def _presentation_is_hls(url: str) -> bool:
    """Probe a Seedr presentation URL without downloading the full media file."""
    if not url:
        return False
    try:
        async with _main._InstrumentedAsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(url, headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,video/*,*/*", "Range": "bytes=0-2047"})
        content_type = str(response.headers.get("content-type") or "").lower()
        prefix = response.content[:2048].decode("utf-8", errors="ignore")
        return "mpegurl" in content_type or "#EXTM3U" in prefix
    except Exception as exc:
        _main.logger.info("Seedr presentation probe failed: %s", exc)
        return False

async def _fetch_seedr_hls_manifest(file_id: str) -> tuple[str, str]:
    """Resolve a browser-playable HLS manifest from Seedr V2, then v1 fallback."""
    candidates: list[str] = []
    presentation_url = await _main.seedr_v2_video_url(file_id)
    if presentation_url:
        candidates.append(presentation_url)
    # Seedr's legacy HLS endpoint explicitly requests a converted browser stream.
    candidates.append(_main._seedr_media_url(file_id, "video"))

    last_detail = "Seedr did not return an HLS manifest"
    for upstream_url in dict.fromkeys(candidates):
        try:
            async with _main._InstrumentedAsyncClient(timeout=35, follow_redirects=True) as client:
                response = await client.get(
                    upstream_url,
                    headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*"},
                )
            if response.status_code >= 400:
                last_detail = response.text[:500] or f"Seedr returned HTTP {response.status_code}"
                continue

            content_type = str(response.headers.get("content-type") or "").lower()
            text = response.text
            if "#EXTM3U" not in text[:200]:
                last_detail = f"Seedr returned {content_type or 'non-HLS content'}"
                continue

            final_url = str(response.url)
            host = _main.urlsplit(final_url).hostname or ""
            if not host:
                last_detail = "Seedr returned an invalid HLS URL"
                continue

            allowed_hosts = {host.lower()}
            for raw_uri in _main.re.findall(r'(?:URI="([^"]+)"|^([^#\s][^\r\n]*))', text, flags=_main.re.M):
                uri = raw_uri[0] or raw_uri[1]
                if uri:
                    try:
                        ref_host = _main.urlsplit(_main._absolute_hls_uri(final_url, uri)).hostname
                        if ref_host:
                            allowed_hosts.add(ref_host.lower())
                    except Exception:
                        pass

            _main._seedr_hls_sources[file_id] = (
                _main.asyncio.get_running_loop().time() + _main.SEEDR_HLS_CACHE_SECONDS,
                final_url,
                allowed_hosts,
            )
            _main.logger.info("Seedr HLS manifest resolved: file=%s source=%s", file_id, _main.urlsplit(final_url).hostname)
            return text, final_url
        except Exception as exc:
            last_detail = str(exc)
            continue

    raise _main.HTTPException(502, last_detail)

async def _get_hls_source(file_id: str) -> tuple[str, set[str]]:
    now = _main.asyncio.get_running_loop().time()
    cached = _main._seedr_hls_sources.get(file_id)
    if cached and cached[0] > now:
        return cached[1], cached[2]

    _manifest, final_url = await _main._fetch_seedr_hls_manifest(file_id)
    cached = _main._seedr_hls_sources[file_id]
    return final_url, cached[2]

async def resolve_seedr_stream_id(file_id: str, name: str = "") -> str:
    """Resolve the Seedr playback file identifier without requiring HLS."""
    candidate = str(file_id or "").strip()
    if candidate:
        try:
            presentation = await _main.seedr_v2_video_url(candidate)
            if presentation:
                return candidate
        except _main.HTTPException:
            pass
        try:
            await _main._fetch_seedr_hls_manifest(candidate)
            return candidate
        except _main.HTTPException:
            pass

    if not name:
        raise _main.HTTPException(404, "Seedr playback file could not be resolved")

    try:
        result = _main.seedr_data(await _main.seedr_request(f"/search/fs?query={_main.quote(name)}"))
    except _main.HTTPException as exc:
        raise _main.HTTPException(exc.status_code, "Seedr file lookup failed") from exc

    candidates = _main.arr(result, ("files", "items"))
    wanted_name = _main.Path(name).name.lower()
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        raw_name = str(raw.get("name") or raw.get("title") or "").strip()
        if raw_name.lower() != wanted_name:
            continue

        # Match MediaFusion: Seedr V2 playback uses the actual file id.
        resolved = str(
            raw.get("id")
            or raw.get("file_id")
            or raw.get("folder_file_id")
            or raw.get("folderFileId")
            or ""
        ).strip()
        if not resolved:
            continue

        try:
            await _main._fetch_seedr_hls_manifest(resolved)
            _main.logger.info("Seedr playback id resolved by filename: %s -> %s", name, resolved)
            return resolved
        except _main.HTTPException:
            continue

    raise _main.HTTPException(404, f"No playable Seedr stream found for {name}")

@router.get("/api/seedr/files/stream")
async def seedr_file_stream(
    file_id: str = _main.Query(""),
    type: str = _main.Query("video"),
    name: str = _main.Query(""),
):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    resolved_id = await _main.resolve_seedr_stream_id(file_id, name)

    if type == "video":
        # Restore the tested direct-browser-stream strategy from
        # fix-seedr-direct-browser-stream:
        #   1) ask Seedr V2 for its presentation URL;
        #   2) if it is a browser-playable direct presentation, proxy it with
        #      Range support;
        #   3) otherwise use the existing HLS proxy.
        presentation_url = await _main.seedr_v2_video_url(resolved_id)
        if presentation_url and not await _main._presentation_is_hls(presentation_url):
            return {
                "url": "/api/seedr/media/video/" + _main.quote(resolved_id, safe=""),
                "externalUrl": presentation_url,
                "name": name or resolved_id,
                "resolvedFileId": resolved_id,
                "protocol": "direct",
            }

        try:
            await _main._fetch_seedr_hls_manifest(resolved_id)
            return {
                "url": "/api/seedr/hls/" + _main.quote(resolved_id, safe=""),
                "externalUrl": presentation_url or _main._seedr_media_url(resolved_id, "video"),
                "name": name or resolved_id,
                "resolvedFileId": resolved_id,
                "protocol": "hls",
            }
        except _main.HTTPException:
            if presentation_url:
                return {
                    "url": "/api/seedr/media/video/" + _main.quote(resolved_id, safe=""),
                    "externalUrl": presentation_url,
                    "name": name or resolved_id,
                    "resolvedFileId": resolved_id,
                    "protocol": "direct",
                }
            raise

    return {
        "url": "/api/seedr/media/audio/" + _main.quote(resolved_id, safe=""),
        "externalUrl": _main._seedr_media_url(resolved_id, "audio"),
        "name": name or resolved_id,
        "resolvedFileId": resolved_id,
        "protocol": "mp3",
    }

@router.get("/api/seedr/hls/{file_id}")
async def seedr_hls_manifest(file_id: str):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    manifest, final_url = await _main._fetch_seedr_hls_manifest(file_id)
    cached = _main._seedr_hls_sources.get(file_id)
    allowed_hosts = cached[2] if cached else {_main.urlsplit(final_url).hostname.lower()}
    rewritten = _main._rewrite_hls_manifest(file_id, manifest, final_url)

    return _main.Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
        },
    )

@router.get("/api/seedr/hls/{file_id}/resource")
async def seedr_hls_resource(request: _main.Request, file_id: str, u: str = _main.Query(...)):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    target = _main._decode_hls_target(u)
    parsed = _main.urlsplit(target)
    host = (parsed.hostname or "").lower()
    if not host:
        raise _main.HTTPException(400, "Invalid HLS target URL")

    try:
        base_url, allowed_hosts = await _main._get_hls_source(file_id)
    except _main.HTTPException:
        raise

    if host not in allowed_hosts:
        raise _main.HTTPException(403, "HLS resource host is not allowed")

    headers: dict[str, str] = {"Accept": "*/*"}
    if request is not None:
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header

    async with _main._InstrumentedAsyncClient(timeout=35, follow_redirects=True) as client:
        response = await client.get(target, headers=headers)

    if response.status_code >= 400:
        return _main.Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/octet-stream"),
            headers={"Access-Control-Allow-Origin": "*"},
        )

    content_type = str(response.headers.get("content-type") or "").lower()
    body = response.content
    _main._record_media_bytes(file_id, len(body))

    if "mpegurl" in content_type or "#EXTM3U" in body[:200].decode("utf-8", errors="ignore"):
        text = body.decode("utf-8", errors="replace")
        final_url = str(response.url)
        final_host = _main.urlsplit(final_url).hostname
        if final_host:
            allowed_hosts.add(final_host.lower())
        _main._seedr_hls_sources[file_id] = (
            _main.asyncio.get_running_loop().time() + _main.SEEDR_HLS_CACHE_SECONDS,
            final_url,
            allowed_hosts,
        )
        body = _main._rewrite_hls_manifest(file_id, text, final_url).encode("utf-8")
        content_type = "application/vnd.apple.mpegurl"

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
    }
    for header in ("content-range", "content-length", "etag", "last-modified"):
        if response.headers.get(header):
            response_headers[header.title()] = response.headers[header]

    return _main.Response(
        content=body,
        status_code=response.status_code,
        media_type=content_type or "application/octet-stream",
        headers=response_headers,
    )

async def _seedr_media_source_url(file_id: str) -> str:
    result = await _main.download_url(file_id)
    url = str(result.get("url") or "").strip()
    if not url:
        raise _main.HTTPException(502, "Seedr returned no media URL")
    return url
