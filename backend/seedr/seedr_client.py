"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['seedr_data', 'normalize_seedr_token', 'seedr_access_token', 'legacy_seedr_request', 'legacy_seedr_list_contents', 'seedr_root_request', 'seedr_problem', 'seedr_error_message', 'seedr_request', 'arr', 'unwrap_seedr_task', 'seedr_task_folder_id', 'magnet_display_name', 'task_complete', 'seedr_task_name', 'normalize_magnet', 'info_hash', 'task_id', 'rename_seedr_folder', 'add_task', 'normalize_file', 'task_contents', 'folder_name', '_load_seedr_cleanup_jobs', '_save_seedr_cleanup_jobs', 'schedule_seedr_cleanup', '_cleanup_seedr_job', '_seedr_cleanup_worker', 'collect_folder', '_safe_download_filename', '_stream_seedr_download', 'download_url', '_srt_to_webvtt', '_seedr_subtitle_text', 'seedr_file_subtitle']

router = APIRouter()

def seedr_data(value: _main.Any) -> _main.Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value

def normalize_seedr_token(value: str) -> str:
    """Normalize common Seedr API Console token copy formats without logging it."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    for _ in range(2):
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
            continue

        # A copied JSON response may be either an object or a JSON string.
        try:
            parsed = _main.json.loads(raw)
            if isinstance(parsed, str):
                raw = parsed.strip()
                continue
            if isinstance(parsed, dict):
                candidate = (
                    parsed.get("access_token")
                    or parsed.get("token")
                    or parsed.get("personal_access_token")
                )
                if candidate:
                    raw = str(candidate).strip()
                    continue
        except Exception:
            pass
        break

    # Some older Torrent Studio deployments stored the OAuth token as a
    # base64-encoded JSON object.
    try:
        decoded = _main.base64.b64decode(raw, validate=True).decode("utf-8")
        payload = _main.json.loads(decoded)
        if isinstance(payload, dict) and payload.get("access_token"):
            raw = str(payload["access_token"]).strip()
    except Exception:
        pass

    return raw.strip().strip('"').strip("'").strip()

def seedr_access_token() -> str:
    """Access token used only by Seedr's legacy resource.php API."""
    return _main.normalize_seedr_token(_main.current_seedr_token())

async def legacy_seedr_request(
    func: str,
    method: str = "POST",
    body: dict[str, _main.Any] | None = None,
) -> _main.Any:
    """Call Seedr's documented legacy resource API using form data."""
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    access_token = _main.seedr_access_token()
    if not access_token:
        raise _main.HTTPException(503, "Seedr access token is empty")

    url = "https://www.seedr.cc/oauth_test/resource.php"
    params = {
        "access_token": access_token,
        "func": str(func),
    }
    async with _main._InstrumentedAsyncClient(timeout=_main.httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.request(
            method,
            url,
            params=params,
            data=body or {},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        raise _main.SeedrError(
            *_main.seedr_problem(response.status_code, data, raw)
        )

    if isinstance(data, dict):
        raw_error = str(data.get("error") or "").strip().lower()
        error_detail = str(
            data.get("error_description") or data.get("message") or data.get("error") or ""
        ).strip()
        normalized_error = error_detail.lower().replace("_", " ")
        if raw_error not in ("", "0"):
            if (
                "not enough space" in normalized_error
                or "insufficient space" in normalized_error
                or "not enough storage" in normalized_error
                or "storage full" in normalized_error
                or "not_enough_space" in raw_error
                or "insufficient_space" in raw_error
                or "not_enough_space" in normalized_error
                or normalized_error.startswith("not enough space")
                or normalized_error.startswith("insufficient space")
            ):
                raise _main.SeedrError(
                    "SEEDR_INSUFFICIENT_SPACE",
                    413,
                    "Seedr does not have enough free space for this torrent.",
                )
            if "access_denied" in raw_error or "access denied" in normalized_error:
                raise _main.SeedrError(
                    "SEEDR_LIBRARY_ACCESS_DENIED",
                    403,
                    "Seedr denied this account operation.",
                )
            if "unauthor" in raw_error or "invalid token" in normalized_error:
                raise _main.SeedrError(
                    "SEEDR_TOKEN_REJECTED",
                    401,
                    "Seedr rejected the API token.",
                )
            # Seedr's legacy API often reports operation failures inside a 200
            # JSON response. Preserve the provider's error text instead of
            # treating that as a successful task creation.
            raise _main.SeedrError(
                "SEEDR_API_ERROR",
                502,
                error_detail or "Seedr rejected the torrent.",
            )

    return data

async def legacy_seedr_list_contents(folder_id: str = "0") -> _main.Any:
    """List a Seedr folder using the legacy resource endpoint.
    
    The free account/token used by Torrent Studio can deny the modern root
    filesystem endpoint while still permitting list_contents through Seedr's
    legacy resource API.
    """
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    access_token = _main.seedr_access_token()
    if not access_token:
        raise _main.HTTPException(503, "Seedr access token is empty")

    url = "https://www.seedr.cc/oauth_test/resource.php"
    params = {
        "access_token": access_token,
        "func": "list_contents",
    }
    form = {
        "content_type": "folder",
        "content_id": str(folder_id),
    }
    async with _main._InstrumentedAsyncClient(timeout=_main.httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.post(
            url,
            params=params,
            data=form,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
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

    if isinstance(data, dict) and str(data.get("error") or "").strip() not in ("", "0"):
        raw_error = str(data.get("error") or "").strip().lower()
        if "access_denied" in raw_error or "access denied" in raw_error:
            raise _main.SeedrError(
                "SEEDR_LIBRARY_ACCESS_DENIED",
                403,
                "Seedr denied access to the account library. The token is recognized, but this library operation is not permitted.",
            )
        if "unauthor" in raw_error or "invalid token" in raw_error:
            raise _main.SeedrError(
                "SEEDR_TOKEN_REJECTED",
                401,
                "Seedr rejected the API token. Generate a fresh Seedr API token and update SEEDR_API_TOKEN in Render.",
            )
        raise _main.SeedrError(
            "SEEDR_API_ERROR",
            502,
            f"Seedr legacy library request failed: {data.get('error')}",
        )

    return data

async def seedr_root_request() -> _main.Any:
    """Fetch the Seedr account root using Seedr's dedicated root endpoint."""
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    url = "https://www.seedr.cc/api/folder"
    headers = {"Accept": "application/json"}
    params = {"access_token": _main.seedr_access_token()}

    async with _main._InstrumentedAsyncClient(timeout=_main.httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)

    raw = response.text
    try:
        data = response.json() if raw else None
    except Exception:
        data = raw

    if response.status_code >= 400:
        detail = raw
        if isinstance(data, dict):
            detail = (
                data.get("error_description")
                or data.get("reason_phrase")
                or data.get("message")
                or data.get("error")
                or raw
            )
        raise _main.HTTPException(response.status_code, str(detail or "Seedr root request failed"))

    return data

def seedr_problem(status_code: int, data: _main.Any, raw: str) -> tuple[str, int, str]:
    reason = ""
    if isinstance(data, dict):
        reason = str(
            data.get("reason_phrase")
            or data.get("error_description")
            or data.get("message")
            or data.get("error")
            or data.get("detail")
            or ""
        ).strip()

    normalized = reason.lower().replace("_", " ").strip()

    if status_code == 401 or "unauthorized" in normalized or normalized in {"invalid token", "token expired", "token invalid"}:
        return (
            "SEEDR_TOKEN_REJECTED",
            401,
            "Seedr rejected the API token. Generate a fresh Seedr API token and update SEEDR_API_TOKEN in Render.",
        )

    if status_code == 403 or normalized in {"access denied", "forbidden", "access_denied"}:
        return (
            "SEEDR_LIBRARY_ACCESS_DENIED",
            403,
            "Seedr denied access to this library operation. The API token is valid, but this operation/folder is not permitted.",
        )

    if status_code == 413 or normalized == "not enough space" or "not enough space" in normalized:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            413,
            "Seedr reports insufficient storage space for this operation.",
        )

    if status_code == 429:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            429,
            "Seedr rate limit reached. Please wait a moment and try again.",
        )

    if status_code >= 500:
        return (
            "SEEDR_QUOTA_UNAVAILABLE",
            status_code,
            "Seedr is temporarily unavailable. Please try again in a moment.",
        )

    return (
        "SEEDR_API_ERROR",
        status_code,
        reason or raw[:500] or f"Seedr API request failed (HTTP {status_code})",
    )

def seedr_error_message(status_code: int, data: _main.Any, raw: str) -> str:
    return _main.seedr_problem(status_code, data, raw)[2]

async def seedr_request(path: str, method: str = "GET", body: _main.Any = None, form: bool = False, base_url: str = _main.SEEDR_BASE) -> _main.Any:
    if not _main.current_seedr_token():
        raise _main.SeedrError(
            "SEEDR_TOKEN_MISSING",
            503,
            "No Seedr account is connected to this browser session.",
        )

    request_path = str(path).lstrip("/")
    selected_base_url = base_url
    if base_url == _main.SEEDR_BASE and _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()) == "pat":
        selected_base_url = _main.SEEDR_PAT_BASE
    url = f"{str(selected_base_url).rstrip("/")}/{request_path}"
    token = _main.normalize_seedr_token(_main.current_seedr_token())

    kwargs: dict[str, Any] = {}
    if body is not None:
        if form:
            headers_base = {"Content-Type": "application/x-www-form-urlencoded"}
            kwargs["data"] = body
        else:
            headers_base = {"Content-Type": "application/json"}
            kwargs["json"] = body
    else:
        headers_base = {}

    async with _main._InstrumentedAsyncClient(timeout=_main.httpx.Timeout(35.0, connect=8.0), follow_redirects=True) as client:
        tried_tokens: list[str] = []
        # Never fall back from one browser session to another Seedr credential.
        candidates = [token]

        last_status = 0
        last_data: Any = None
        last_raw = ""

        for candidate in candidates:
            if not candidate or candidate in tried_tokens:
                continue
            tried_tokens.append(candidate)

            headers = {
                "Authorization": f"Bearer {candidate}",
                "Accept": "application/json",
                **headers_base,
            }
            request_started = _main.time.monotonic()
            _main.logger.info(
                "Seedr API request start: session=%s mode=%s method=%s url=%s "
                "token_present=%s token_length=%s token_fingerprint=%s body_keys=%s",
                _main._seedr_session_fingerprint(_main._seedr_request_session_id.get().strip()),
                _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()),
                method,
                url,
                bool(token),
                len(token),
                _main.hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "none",
                sorted(body.keys()) if isinstance(body, dict) else [],
            )
            try:
                response = await client.request(method, url, headers=headers, **kwargs)
            except Exception as exc:
                elapsed_ms = int((_main.time.monotonic() - request_started) * 1000)
                _main.logger.exception(
                    "Seedr API transport failure: session=%s mode=%s method=%s url=%s "
                    "elapsed_ms=%s exception_type=%s exception=%s",
                    _main._seedr_session_fingerprint(_main._seedr_request_session_id.get().strip()),
                    _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()),
                    method,
                    url,
                    elapsed_ms,
                    type(exc).__name__,
                    str(exc)[:1000],
                )
                raise _main.SeedrError(
                    "SEEDR_NETWORK_ERROR",
                    502,
                    f"Seedr request could not be completed: {type(exc).__name__}: {str(exc)[:500]}",
                ) from exc

            elapsed_ms = int((_main.time.monotonic() - request_started) * 1000)
            raw = response.text
            try:
                data = response.json() if raw else None
            except Exception:
                data = raw

            response_request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("x-correlation-id")
                or response.headers.get("cf-ray")
                or ""
            )
            _main.logger.info(
                "Seedr API response: session=%s mode=%s method=%s path=%s status=%s "
                "elapsed_ms=%s response_bytes=%s content_type=%s provider_request_id=%s",
                _main._seedr_session_fingerprint(_main._seedr_request_session_id.get().strip()),
                _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()),
                method,
                request_path,
                response.status_code,
                elapsed_ms,
                len(raw.encode("utf-8", errors="ignore")),
                response.headers.get("content-type", ""),
                response_request_id[:200],
            )

            if response.status_code < 400:
                if isinstance(data, dict):
                    soft = str(data.get("reason_phrase") or "").strip().lower()
                    if soft == "not_enough_space":
                        raise _main.SeedrError(
                            "SEEDR_QUOTA_UNAVAILABLE",
                            413,
                            "Seedr reports insufficient storage space for this operation.",
                        )
                return data

            last_status, last_data, last_raw = response.status_code, data, raw
            _main.logger.warning(
                "Seedr API request failed: session=%s mode=%s method=%s path=%s url=%s "
                "status=%s elapsed_ms=%s token_length=%s token_fingerprint=%s "
                "provider_request_id=%s response_json=%s response_body=%s",
                _main._seedr_session_fingerprint(_main._seedr_request_session_id.get().strip()),
                _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()),
                method,
                request_path,
                url,
                response.status_code,
                elapsed_ms,
                len(token),
                _main.hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "none",
                response_request_id[:200],
                _main.json.dumps(data, ensure_ascii=False, default=str)[:4000] if isinstance(data, (dict, list)) else "<not-json>",
                raw[:4000] if raw else "<empty>",
            )

            break

    code, status_code, detail = _main.seedr_problem(last_status, last_data, last_raw)
    raise _main.SeedrError(code, status_code, detail)

def arr(value: _main.Any, keys: tuple[str, ...]) -> list[_main.Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return []

def unwrap_seedr_task(value: _main.Any) -> dict[str, _main.Any]:
    """Normalize task responses that may be wrapped in data/task objects."""
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return {}
        nested = current.get("task")
        if isinstance(nested, dict):
            current = nested
            continue
        return current
    return current if isinstance(current, dict) else {}

def seedr_task_folder_id(task: dict[str, _main.Any]) -> str:
    nested_torrent = task.get("torrent") if isinstance(task.get("torrent"), dict) else {}
    payload = task.get("torrent_payload") if isinstance(task.get("torrent_payload"), dict) else {}

    return str(
        task.get("folder_created_id")
        or task.get("folder_created")
        or task.get("folder_id")
        or task.get("folderId")
        or nested_torrent.get("folder_created_id")
        or nested_torrent.get("folder_id")
        or nested_torrent.get("folderId")
        or payload.get("folder_created_id")
        or payload.get("folder_id")
        or payload.get("folderId")
        or ""
    ).strip()

def magnet_display_name(value: _main.Any) -> str:
    """Extract the torrent display name (dn) from a magnet-like value."""
    raw = str(value or "").strip()
    if not raw.lower().startswith("magnet:?"):
        return ""
    try:
        params = _main.parse_qs(_main.urlsplit(raw).query, keep_blank_values=True)
        return str((params.get("dn") or [""])[0]).strip()
    except Exception:
        return ""

def task_complete(task: dict[str, _main.Any]) -> bool:
    """Return True only when Seedr reports a task as finished."""
    if not isinstance(task, dict):
        return False

    # Explicit completion flags are authoritative.
    for key in ("completed", "complete", "finished", "done", "is_completed", "isComplete"):
        value = task.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, (int, float)) and value == 1:
            return True

    state = str(
        task.get("state")
        or task.get("status")
        or task.get("phase")
        or ""
    ).strip().lower().replace("-", "_")

    try:
        progress = float(task.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0.0

    # A new Prepare must be able to replace stuck, queued, paused, or
    # metadata/seed-discovery tasks, even if the provider calls them "seeding".
    # Keep these as unfinished unless Seedr's explicit completion flags above
    # confirm completion. This avoids treating "Collecting Seeds" as completed.
    active_states = {
        "paused", "paused_dl", "pauseddl", "paused_download",
        "queued", "queued_dl", "queueddl", "queued_download",
        "downloading", "download", "collecting_seeds", "collecting seeds",
        "waiting", "metadata", "fetching_metadata", "checking",
    }
    if state in active_states:
        return False

    completed_states = {"complete", "completed", "finished", "done", "success"}
    if state in completed_states:
        return True

    # "seeding" normally means the payload is complete, but some Seedr task
    # variants use it for a stalled seed-discovery state without progress.
    if state == "seeding":
        return progress >= 100

    # A numeric 100% is authoritative even when the state field is absent or
    # uses a provider-specific value.
    return progress >= 100

def seedr_task_name(task: dict[str, _main.Any]) -> str:
    nested_torrent = task.get("torrent") if isinstance(task.get("torrent"), dict) else {}
    payload = task.get("torrent_payload") if isinstance(task.get("torrent_payload"), dict) else {}
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    nested_link = (
        task.get("torrent_magnet")
        or task.get("magnet")
        or task.get("magnet_url")
        or nested_torrent.get("torrent_magnet")
        or nested_torrent.get("magnet")
        or payload.get("torrent_magnet")
        or payload.get("magnet")
        or ""
    )
    magnet_name = _main.magnet_display_name(nested_link)

    # A magnet's dn is the closest match to the title the user selected from
    # search. Prefer it before Seedr's own generated/provider title.
    return str(
        task.get("torrent_name")
        or magnet_name
        or nested_torrent.get("torrent_name")
        or payload.get("torrent_name")
        or task.get("torrent_title")
        or task.get("torrentTitle")
        or nested_torrent.get("title")
        or nested_torrent.get("name")
        or payload.get("title")
        or payload.get("name")
        or task.get("title")
        or task.get("name")
        or meta.get("torrent_name")
        or meta.get("title")
        or ""
    ).strip()

def normalize_magnet(magnet: str) -> str:
    """Normalize the BTIH while preserving the magnet's tracker and name parameters."""
    value = _main.re.sub(r"[\r\n\t]+", "", str(magnet or "").strip())
    decoded = _main.unquote(value)
    if decoded.lower().startswith("magnet:?"):
        value = decoded
    if not value.lower().startswith("magnet:?"):
        return value

    try:
        parsed = _main.urlsplit(value)
        params = _main.parse_qs(parsed.query, keep_blank_values=True)
        valid_hash = ""
        for raw in params.get("xt", []):
            raw = _main.unquote(raw)
            match = _main.re.fullmatch(r"urn:btih:([A-Za-z0-9]{32,40})", raw, _main.re.I)
            if not match:
                continue
            candidate = match.group(1)
            if len(candidate) == 32:
                candidate = _main.base64.b32decode(
                    candidate.upper() + "=" * ((8 - len(candidate) % 8) % 8)
                ).hex()
            if len(candidate) == 40 and _main.re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
                valid_hash = candidate.lower()
                break

        if not valid_hash:
            return value

        # Keep the human-readable name and all supplied trackers. Some
        # metadata-only peers are reachable only through the trackers present
        # in the original magnet.
        rebuilt: list[tuple[str, str]] = [("xt", "urn:btih:" + valid_hash)]
        for key in ("dn", "tr"):
            for item in params.get(key, []):
                text_value = str(item or "").strip()
                if text_value:
                    rebuilt.append((key, text_value))

        return "magnet:?" + _main.urlencode(rebuilt, doseq=True)
    except Exception:
        return value

def info_hash(magnet: str) -> str:
    for _ in range(3):
        m = _main.re.search(r"(?:urn:btih:|btih:)([A-Za-z0-9]{32,40})", magnet, _main.re.I)
        if m:
            h = m.group(1)
            if len(h) == 40 and _main.re.fullmatch(r"[0-9a-fA-F]{40}", h):
                return h.lower()
            if len(h) == 32:
                try:
                    return _main.base64.b32decode(h.upper() + "=" * ((8-len(h)%8)%8)).hex()
                except Exception:
                    pass
        magnet = _main.unquote(magnet)
    return ""

def task_id(task: dict[str, _main.Any]) -> str:
    return str(task.get("user_torrent_id") or task.get("id") or task.get("task_id") or "").strip()

async def rename_seedr_folder(folder_id: str, name: str) -> bool:
    # Folder rename is deliberately not part of the free-account download path.
    # Seedr can expose the generated folder name asynchronously.
    return False

async def add_task(magnet: str, folder_id: int = 0) -> dict[str, _main.Any]:
    # Forward the exact magnet URI supplied by the caller.
    # Do not parse, rebuild, decode, lowercase, or add/remove query parameters.
    _main.logger.info(
        "Seedr direct add: POST /tasks folder_id=%s magnet_length=%s",
        int(folder_id),
        len(magnet),
    )
    result = await _main.seedr_request(
        "/tasks",
        method="POST",
        body={
            "torrent_magnet": magnet,
            "folder_id": int(folder_id),
        },
    )
    if not isinstance(result, dict):
        raise _main.HTTPException(502, "Seedr did not return a valid task response")
    return result

def normalize_file(item: _main.Any, folder_id: str = "") -> dict[str, _main.Any]:
    if not isinstance(item, dict):
        return {"id": "", "name": "Unnamed file", "size": 0, "folderId": folder_id}
    return {
        "id": str(item.get("id") or item.get("file_id") or ""),
        # Seedr V2 presentation endpoints use the canonical file id.
        # Keep legacy folder_file_id only as a fallback for older API shapes.
        "streamId": str(
            item.get("id")
            or item.get("file_id")
            or item.get("folder_file_id")
            or item.get("folderFileId")
            or ""
        ),
        "name": str(item.get("name") or item.get("title") or "Unnamed file"),
        "size": int(float(item.get("size") or 0)),
        "folderId": str(item.get("folder_id") or item.get("folderId") or folder_id),
    }

async def task_contents(tid: str) -> list[dict[str, _main.Any]]:
    payload = _main.seedr_data(await _main.seedr_request(f"/tasks/{_main.quote(tid)}/contents"))
    if not isinstance(payload, dict):
        return []
    files = [_main.normalize_file(x, str(payload.get("folder_created_id") or "")) for x in _main.arr(payload, ("files", "items"))]
    folder = str(payload.get("folder_created_id") or "").strip()
    if folder and (not files or any(not x["id"] for x in files)):
        try:
            folder_payload = _main.seedr_data(await _main.seedr_request(f"/fs/folder/{_main.quote(folder)}/contents"))
            folder_files = _main.arr(folder_payload, ("files", "items"))
            if folder_files:
                files = [_main.normalize_file(x, folder) for x in folder_files]
        except _main.HTTPException:
            pass
    return files

async def folder_name(folder_id: str) -> str:
    for endpoint in (f"/fs/folder/{_main.quote(folder_id)}", f"/fs/folder/{_main.quote(folder_id)}/contents"):
        try:
            payload = _main.seedr_data(await _main.seedr_request(endpoint))
            if isinstance(payload, dict):
                for key in ("name", "title", "folder_name", "folderName", "path"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        return _main.Path(value.rstrip("/")).name
                for key in ("folder", "directory"):
                    child = payload.get(key)
                    if isinstance(child, dict):
                        for name_key in ("name", "title", "folder_name", "folderName", "path"):
                            value = str(child.get(name_key) or "").strip()
                            if value:
                                return _main.Path(value.rstrip("/")).name
        except _main.HTTPException:
            continue
    return ""

def _load_seedr_cleanup_jobs() -> None:
    global _seedr_cleanup_jobs
    try:
        raw = _main.SEEDR_CLEANUP_FILE.read_text(encoding="utf-8")
        data = _main.json.loads(raw)
        if isinstance(data, dict):
            _main._seedr_cleanup_jobs = {
                str(k): v for k, v in data.items()
                if isinstance(v, dict) and float(v.get("deleteAt") or 0) > 0
            }
    except Exception:
        _main._seedr_cleanup_jobs = {}

def _save_seedr_cleanup_jobs() -> None:
    try:
        _main.SEEDR_CLEANUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _main.SEEDR_CLEANUP_FILE.with_suffix(".tmp")
        tmp.write_text(_main.json.dumps(_main._seedr_cleanup_jobs, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_main.SEEDR_CLEANUP_FILE)
    except Exception as exc:
        _main.logger.info("Could not persist Seedr cleanup schedule: %s", exc)

def schedule_seedr_cleanup(
    task_id_value: str,
    torrent_name: str,
    folder_id: str = "",
    added_at: float | None = None,
) -> None:
    tid = str(task_id_value or "").strip()
    if not tid:
        return

    now = _main.time.time()
    started = float(added_at or now)
    _main._seedr_cleanup_jobs[tid] = {
        "taskId": tid,
        "torrentName": str(torrent_name or "").strip() or f"Torrent {tid}",
        "folderId": str(folder_id or "").strip(),
        "addedAt": started,
        "deleteAt": started + _main.SEEDR_AUTO_DELETE_SECONDS,
    }
    _main._save_seedr_cleanup_jobs()

async def _cleanup_seedr_job(tid: str, job: dict[str, _main.Any]) -> bool:
    folder_id = str(job.get("folderId") or "").strip()
    torrent_name = str(job.get("torrentName") or f"Torrent {tid}").strip()

    # Seedr may expose the folder only after the task starts.
    if not folder_id:
        try:
            raw = _main.seedr_data(await _main.seedr_request(f"/tasks/{_main.quote(tid)}"))
            task = _main.unwrap_seedr_task(raw)
            folder_id = _main.seedr_task_folder_id(task)
            if folder_id:
                job["folderId"] = folder_id
                _main._save_seedr_cleanup_jobs()
        except _main.HTTPException as exc:
            if exc.status_code == 404:
                _main._seedr_cleanup_jobs.pop(tid, None)
                _main._save_seedr_cleanup_jobs()
                return True
            return False
        except Exception:
            return False

    if not folder_id or not folder_id.isdigit() or folder_id == "0":
        return False

    # Stop the Seedr task first so an active transfer cannot keep rebuilding
    # files while the folder is being removed.
    try:
        await _main.seedr_request(f"/tasks/{_main.quote(tid)}", "DELETE")
    except _main.HTTPException as exc:
        if exc.status_code != 404:
            _main.logger.info("Seedr task cleanup failed for %s: HTTP %s", tid, exc.status_code)
            return False
    except Exception as exc:
        _main.logger.info("Seedr task cleanup failed for %s: %s", tid, exc)
        return False

    try:
        await _main.seedr_folder_delete(folder_id)
    except _main.HTTPException as exc:
        if exc.status_code != 404:
            _main.logger.info("Seedr folder cleanup failed for %s (%s): HTTP %s", torrent_name, folder_id, exc.status_code)
            return False
    except Exception as exc:
        _main.logger.info("Seedr folder cleanup failed for %s (%s): %s", torrent_name, folder_id, exc)
        return False

    _main._seedr_cleanup_jobs.pop(tid, None)
    _main._save_seedr_cleanup_jobs()
    _main.logger.info("Auto-deleted Seedr torrent after 2 hours: %s (task=%s folder=%s)", torrent_name, tid, folder_id)
    return True

async def _seedr_cleanup_worker() -> None:
    while True:
        try:
            now = _main.time.time()
            due = [
                (tid, job)
                for tid, job in list(_main._seedr_cleanup_jobs.items())
                if float(job.get("deleteAt") or 0) <= now
            ]
            for tid, job in due:
                await _main._cleanup_seedr_job(tid, job)
        except _main.asyncio.CancelledError:
            raise
        except Exception as exc:
            _main.logger.info("Seedr cleanup worker error: %s", exc)
        await _main.asyncio.sleep(30)

async def collect_folder(folder_id: str, path: str = "/", depth: int = 0) -> list[dict[str, _main.Any]]:
    if depth > 8:
        return []

    async with _main._seedr_folder_semaphore:
        try:
            payload = _main.seedr_data(await _main.seedr_request(f"/fs/folder/{_main.quote(folder_id)}/contents"))
        except _main.HTTPException as exc:
            if exc.status_code == 404:
                return []
            raise

    if not isinstance(payload, dict):
        return []

    files: list[dict[str, Any]] = []
    for raw in _main.arr(payload, ("files", "items")):
        item = _main.normalize_file(raw, folder_id)
        item["folderPath"] = path
        item["url"] = None
        files.append(item)

    child_jobs: list[_main.asyncio.Future] = []
    for raw in _main.arr(payload, ("folders", "directories")):
        child_id = str(raw.get("id") or raw.get("folder_id") or "") if isinstance(raw, dict) else ""
        if not child_id:
            continue

        child_name = (
            str(raw.get("name") or raw.get("title") or child_id)
            if isinstance(raw, dict)
            else child_id
        )
        child_path = path.rstrip("/") + "/" + child_name
        child_jobs.append(_main.collect_folder(child_id, child_path, depth + 1))

    if child_jobs:
        children = await _main.asyncio.gather(*child_jobs, return_exceptions=True)
        for child in children:
            if isinstance(child, list):
                files.extend(child)

    return files

def _safe_download_filename(filename: str, fallback: str) -> str:
    value = str(filename or "").strip().replace("\r", "").replace("\n", "")
    value = _main.re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = _main.re.sub(r"\s+", " ", value).strip(" .")
    return value[:240] or fallback

async def _stream_seedr_download(url: str):
    client = _main._InstrumentedAsyncClient(timeout=None, follow_redirects=True)
    request = client.build_request("GET", url, headers={"Accept": "*/*"})
    response = await client.send(request, stream=True)

    if response.status_code >= 400:
        try:
            detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
        finally:
            await response.aclose()
            await client.aclose()
        raise _main.HTTPException(response.status_code, detail or "Seedr download request failed")

    content_type = response.headers.get("content-type") or "application/octet-stream"
    content_length = response.headers.get("content-length")
    content_range = response.headers.get("content-range")

    async def body():
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": "",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "no-store",
    }
    if content_length:
        headers["Content-Length"] = content_length
    if content_range:
        headers["Content-Range"] = content_range

    return body, content_type, headers

async def download_url(file_id: str) -> dict[str, str]:
    payload = _main.seedr_data(await _main.seedr_request(f"/download/file/{_main.quote(file_id)}/url"))
    if isinstance(payload, dict):
        url = str(payload.get("url") or payload.get("download_url") or payload.get("downloadUrl") or payload.get("direct_url") or "")
        name = str(payload.get("name") or payload.get("filename") or "")
    else:
        url, name = str(payload or ""), ""
    if not url:
        raise _main.HTTPException(502, "Seedr did not return a download URL")
    return {"url": url, "name": name}

def _srt_to_webvtt(text: str) -> str:
    """Convert a UTF-8/legacy SRT subtitle into browser-compatible WebVTT."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = normalized.split("\n")
    output = ["WEBVTT", ""]
    for line in lines:
        if "-->" in line:
            line = _main.re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", line)
        output.append(line)
    result = "\n".join(output)
    return result if result.endswith("\n") else result + "\n"

async def _seedr_subtitle_text(file_id: str, filename: str) -> tuple[str, str]:
    result = await _main.download_url(file_id)
    url = result["url"]
    lower = (filename or result.get("name") or "").lower()
    async with _main._InstrumentedAsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        raw = response.content

    if lower.endswith(".vtt"):
        text = raw.decode("utf-8-sig", errors="replace")
        if not text.lstrip().startswith("WEBVTT"):
            text = _main._srt_to_webvtt(text)
    elif lower.endswith(".srt"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        text = _main._srt_to_webvtt(text)
    else:
        raise _main.HTTPException(415, "Only SRT and WebVTT subtitle files are supported")
    return text, "text/vtt; charset=utf-8"

@router.get("/api/seedr/files/{file_id}/subtitle")
async def seedr_file_subtitle(
    file_id: str,
    filename: str = _main.Query(""),
):
    """Proxy a Seedr SRT/VTT file as WebVTT for the browser <track> element."""
    text, content_type = await _main._seedr_subtitle_text(file_id, filename)
    return _main.Response(
        content=text,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )
