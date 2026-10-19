"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['seedr_session', '_validate_seedr_pat', 'seedr_connect_pat', 'seedr_connect_start', '_extract_seedr_access_token', 'seedr_connect_status', 'seedr_connect_disconnect', 'seedr_token_diagnostic', 'seedr_auth_status', 'seedr_quota', '_prepare_seedr_space', 'seedr_add']

router = APIRouter()

@router.get("/api/seedr/session")
async def seedr_session(request: _main.Request):
    """Initialize the browser session quickly; provider health checks happen on demand."""
    session_id = _main._seedr_request_session_id.get().strip()
    session = _main._seedr_get_session(session_id)
    # Session bootstrap must not wait on Seedr. The CSRF token is needed before
    # the browser can submit a PAT, and a slow provider must not cause POST 403s.
    # Actual token/API failures are reported by the operation the user requests.
    return {
        "connected": bool(_main.current_seedr_token()),
        "csrfToken": str(session.get("csrf_token") or ""),
    }

async def _validate_seedr_pat(token: str) -> None:
    """Validate a Seedr PAT sequentially; avoid bursts of concurrent auth requests."""
    token = _main.normalize_seedr_token(token)
    if not token:
        raise _main.SeedrError("SEEDR_PAT_MISSING", 400, "A Seedr Personal Access Token is required.")

    endpoints = (
        f"{_main.SEEDR_PAT_BASE}/user",
        f"{_main.SEEDR_PAT_BASE}/tasks",
        f"{_main.SEEDR_PAT_BASE}/fs/root/contents",
        f"{_main.SEEDR_V2_BASE}/tasks",
        f"{_main.SEEDR_V2_BASE}/fs/root/contents",
    )
    outcomes: list[str] = []
    saw_unauthorized = False
    timeout = _main.httpx.Timeout(8.0, connect=3.0)

    # The endpoints have been verified to accept the current PAT when called
    # individually. Do not fan out concurrently: concurrent probes previously
    # all failed at the transport layer despite these same endpoints succeeding
    # when tested one at a time. Stop on the first 2xx response.
    async with _main._InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in endpoints:
            try:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except _main.httpx.HTTPError as exc:
                _main.logger.info(
                    "Seedr PAT validation transport error: endpoint=%s error=%s",
                    url,
                    type(exc).__name__,
                )
                outcomes.append(f"{url}=transport:{type(exc).__name__}")
                continue

            if 200 <= response.status_code < 300:
                _main.logger.info(
                    "Seedr PAT validation succeeded: endpoint=%s status=%s",
                    url,
                    response.status_code,
                )
                return

            if response.status_code == 401:
                saw_unauthorized = True
                outcomes.append(f"{url}=401")
                continue

            if response.status_code in {403, 404, 405}:
                outcomes.append(f"{url}={response.status_code}")
                continue

            try:
                payload = response.json() if response.content else {}
                reason = str(
                    payload.get("error_description")
                    or payload.get("reason_phrase")
                    or payload.get("message")
                    or payload.get("error")
                    or ""
                ).strip() if isinstance(payload, dict) else ""
            except (ValueError, TypeError):
                reason = ""

            # Retry the next known API variant for transient/provider-specific
            # errors rather than concluding the token is invalid prematurely.
            outcomes.append(f"{url}=HTTP{response.status_code}")
            _main.logger.info(
                "Seedr PAT validation endpoint returned HTTP %s: endpoint=%s reason=%s",
                response.status_code,
                url,
                reason[:160],
            )

    _main.logger.warning(
        "Seedr PAT validation found no successful endpoint: %s",
        ", ".join(outcomes),
    )
    if saw_unauthorized and not any("transport:" in row for row in outcomes):
        raise _main.SeedrError(
            "SEEDR_PAT_REJECTED",
            401,
            "Seedr rejected the Personal Access Token. Copy a fresh PAT from Seedr and try again.",
        )

    raise _main.SeedrError(
        "SEEDR_PAT_VALIDATION_FAILED",
        502,
        "Could not validate the Seedr token using the account API. Please retry shortly.",
    )

@router.post("/api/seedr/connect/pat")
async def seedr_connect_pat(request: _main.Request):
    session_id = _main._seedr_request_session_id.get().strip()
    session = _main._seedr_get_session(session_id)

    try:
        payload = await request.json()
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        raise _main.HTTPException(400, "Request body must be JSON.")

    raw_pat = payload.get("pat") or payload.get("token")
    if not isinstance(raw_pat, str):
        raise _main.HTTPException(400, "A Seedr Personal Access Token is required.")

    pat = _main.normalize_seedr_token(raw_pat)
    if not pat:
        raise _main.HTTPException(400, "A Seedr Personal Access Token is required.")

    await _main._validate_seedr_pat(pat)

    session["access_token"] = pat
    session["refresh_token"] = ""
    session["token_type"] = "Bearer"
    session["auth_mode"] = "pat"
    session["device"] = None
    session["last_seen"] = _main.time.time()

    _main.logger.info(
        "Seedr PAT connected: session=%s token_length=%s",
        _main._seedr_session_fingerprint(session_id),
        len(pat),
    )
    return {"status": "connected", "connected": True}

@router.post("/api/seedr/connect/start")
async def seedr_connect_start(request: _main.Request):
    session_id = _main._seedr_request_session_id.get().strip()
    session = _main._seedr_get_session(session_id)

    _main.logger.info(
        "Seedr connect start: session=%s cookie_present=%s has_device=%s has_token=%s",
        _main._seedr_session_fingerprint(session_id),
        bool(request.cookies.get(_main.SEEDR_SESSION_COOKIE)),
        bool(isinstance(session.get("device"), dict)),
        bool(_main._seedr_session_token(session_id)),
    )

    token = _main.current_seedr_token()
    if token:
        return {"status": "connected", "connected": True}

    try:
        async with _main._InstrumentedAsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(
                _main.SEEDR_DEVICE_CODE_URL,
                params={"client_id": _main.SEEDR_DEVICE_CLIENT_ID},
                headers={"Accept": "application/json"},
            )
        raw = response.text
        try:
            data = response.json() if raw else {}
        except Exception:
            data = {}

        if response.status_code >= 400 or not isinstance(data, dict):
            detail = _main.seedr_error_message(response.status_code, data, raw)
            raise _main.SeedrError("SEEDR_DEVICE_CODE_FAILED", 502, detail)

        device_code = str(data.get("device_code") or data.get("deviceCode") or "").strip()
        user_code = str(data.get("user_code") or data.get("userCode") or "").strip()
        verification_url = str(
            data.get("verification_url")
            or data.get("verificationUrl")
            or "https://www.seedr.cc/devices"
        ).strip()
        expires_in = int(float(data.get("expires_in") or data.get("expiresIn") or 900))

        if not device_code or not user_code:
            raise _main.SeedrError(
                "SEEDR_DEVICE_CODE_INVALID",
                502,
                "Seedr returned an incomplete device authorization response.",
            )

        session["device"] = {
            "device_code": device_code,
            "user_code": user_code,
            "verification_url": verification_url,
            "expires_at": _main.time.time() + max(60, expires_in),
            "created_at": _main.time.time(),
        }
        session["last_seen"] = _main.time.time()

        poll_interval = int(float(data.get("interval") or 5))
        poll_interval = max(2, min(30, poll_interval))
        session["device"]["interval"] = poll_interval
        _main.logger.info(
            "Seedr connect start stored device: session=%s expires_in=%s interval=%s",
            _main._seedr_session_fingerprint(session_id),
            expires_in,
            poll_interval,
        )
        return {
            "status": "pending",
            "connected": False,
            "userCode": user_code,
            "verificationUrl": verification_url,
            "expiresIn": expires_in,
            "interval": poll_interval,
        }
    except _main.SeedrError:
        raise
    except Exception as exc:
        _main.logger.warning("Seedr device-code request failed: %s", exc)
        raise _main.SeedrError(
            "SEEDR_DEVICE_CODE_FAILED",
            502,
            "Could not start Seedr account authorization right now.",
        ) from exc

def _extract_seedr_access_token(payload: _main.Any) -> str:
    if isinstance(payload, dict):
        for key in ("access_token", "accessToken", "token"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                normalized = _main.normalize_seedr_token(candidate)
                if normalized:
                    return normalized

        for key in ("data", "result", "response"):
            nested = payload.get(key)
            token = _main._extract_seedr_access_token(nested)
            if token:
                return token
    return ""

@router.get("/api/seedr/connect/status")
async def seedr_connect_status(request: _main.Request):
    session_id = _main._seedr_request_session_id.get().strip()
    session = _main._seedr_get_session(session_id)
    token = _main._seedr_session_token(session_id)
    device = session.get("device")

    _main.logger.info(
        "Seedr connect status: session=%s cookie_present=%s has_device=%s has_token=%s",
        _main._seedr_session_fingerprint(session_id),
        bool(request.cookies.get(_main.SEEDR_SESSION_COOKIE)),
        bool(isinstance(device, dict)),
        bool(token),
    )

    if token:
        try:
            token_ctx = _main._seedr_request_token.set(token)
            try:
                await _main.seedr_request("/user")
            finally:
                _main._seedr_request_token.reset(token_ctx)
            session["device"] = None
            return {"status": "connected", "connected": True}
        except _main.SeedrError as exc:
            if exc.status_code == 401:
                _main._clear_seedr_session_token(session_id)
            else:
                return {"status": "pending", "connected": False}

    device = session.get("device")
    if not isinstance(device, dict):
        return {
            "status": "error",
            "connected": False,
            "code": "SEEDR_SESSION_LOST",
            "message": "The Seedr connection session was lost. Start the connection again.",
        }

    expires_at = float(device.get("expires_at") or 0)
    if expires_at and _main.time.time() >= expires_at:
        session["device"] = None
        return {
            "status": "expired",
            "connected": False,
            "message": "The Seedr authorization code expired. Start a new connection.",
        }

    device_code = str(device.get("device_code") or "").strip()
    if not device_code:
        session["device"] = None
        return {
            "status": "error",
            "connected": False,
            "message": "Seedr did not provide a usable device code.",
        }

    try:
        async with _main._InstrumentedAsyncClient(timeout=10, follow_redirects=True) as client:
            # Seedr's device authorization endpoint expects the original
            # device_code. The current Seedr client implementations pass only
            # device_code here; do not add client_id to this request.
            response = await client.get(
                _main.SEEDR_DEVICE_AUTHORIZE_URL,
                params={"device_code": device_code},
                headers={"Accept": "application/json"},
            )

        raw = response.text
        try:
            data = response.json() if raw else {}
        except Exception:
            data = {}

        access_token = _main._extract_seedr_access_token(data)
        _main.logger.info(
            "Seedr device authorization poll: http=%s token_present=%s error=%s message=%s",
            response.status_code,
            bool(access_token),
            str(data.get("error") or data.get("code") or "")[:120] if isinstance(data, dict) else "",
            str(
                (data.get("error_description") or data.get("message") or data.get("error") or "")
                if isinstance(data, dict) else ""
            )[:200],
        )
        if access_token:
            refresh_token = ""
            token_type = "Bearer"
            if isinstance(data, dict):
                refresh_token = str(data.get("refresh_token") or data.get("refreshToken") or "").strip()
                token_type = str(data.get("token_type") or "Bearer").strip() or "Bearer"
                for key in ("data", "result", "response"):
                    nested = data.get(key)
                    if isinstance(nested, dict):
                        refresh_token = refresh_token or str(
                            nested.get("refresh_token") or nested.get("refreshToken") or ""
                        ).strip()
                        token_type = str(nested.get("token_type") or token_type).strip() or token_type

            session["access_token"] = access_token
            session["refresh_token"] = refresh_token
            session["token_type"] = token_type
            session["device"] = None
            session["last_seen"] = _main.time.time()

            token_ctx = _main._seedr_request_token.set(access_token)
            try:
                await _main.seedr_request("/user")
            except _main.SeedrError:
                _main._clear_seedr_session_token(session_id)
                raise
            finally:
                _main._seedr_request_token.reset(token_ctx)

            return {"status": "connected", "connected": True}

        error_code = ""
        error_message = ""
        if isinstance(data, dict):
            error_code = str(data.get("error") or data.get("code") or "").strip().lower()
            error_message = str(
                data.get("error_description")
                or data.get("message")
                or data.get("error")
                or ""
            ).strip()

        pending = (
            response.status_code in {400, 409, 428}
            and (
                not error_code
                or "pending" in error_code
                or "authorize" in error_code
                or "not_authorized" in error_code
                or "not approved" in error_message.lower()
            )
        )
        if pending:
            return {
                "status": "pending",
                "connected": False,
                "expiresIn": max(0, int(expires_at - _main.time.time())),
                "interval": max(2, min(30, int(float(device.get("interval") or 5)))),
            }

        if response.status_code >= 400:
            if response.status_code == 401:
                session["device"] = None
                return {
                    "status": "error",
                    "connected": False,
                    "message": "Seedr rejected this authorization request.",
                }
            return {
                "status": "error",
                "connected": False,
                "message": error_message or _main.seedr_error_message(response.status_code, data, raw),
            }

        return {
            "status": "pending",
            "connected": False,
            "expiresIn": max(0, int(expires_at - _main.time.time())),
        }
    except _main.SeedrError:
        raise
    except Exception as exc:
        _main.logger.info("Seedr device authorization poll failed: %s", exc)
        return {
            "status": "pending",
            "connected": False,
            "expiresIn": max(0, int(expires_at - _main.time.time())),
        }

@router.post("/api/seedr/connect/disconnect")
async def seedr_connect_disconnect(request: _main.Request):
    session_id = _main._seedr_request_session_id.get().strip()
    _main._clear_seedr_session_token(session_id)
    return {"status": "disconnected", "connected": False}

@router.get("/api/seedr/token-diagnostic")
async def seedr_token_diagnostic():
    if not _main.current_seedr_token():
        return {
            "configured": False,
            "code": "SEEDR_TOKEN_MISSING",
            "tokenLength": 0,
            "tokenFingerprint": None,
            "checks": {},
        }

    token = _main.normalize_seedr_token(_main.current_seedr_token())
    fingerprint = _main.hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else None

    async def check(path: str) -> dict[str, Any]:
        try:
            await _main.seedr_request(path)
            return {"ok": True, "status": 200}
        except _main.SeedrError as exc:
            return {"ok": False, "status": exc.status_code, "code": exc.code, "message": exc.detail}
        except Exception as exc:
            return {"ok": False, "status": 0, "code": "LOCAL_ERROR", "message": str(exc)[:200]}

    result: dict[str, Any] = {
        "configured": True,
        "code": "SEEDR_TOKEN_PRESENT",
        "checks": {
            "user": await check("/tasks" if _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()) == "pat" else "/user"),
        },
    }
    if _main.DEBUG_ENDPOINTS_ENABLED:
        # Token metadata is only returned when debugging is explicitly enabled.
        result["tokenLength"] = len(token)
        result["tokenFingerprint"] = fingerprint
    return result

@router.get("/api/seedr/auth-status")
async def seedr_auth_status():
    if not _main.current_seedr_token():
        return {
            "configured": False,
            "authenticated": False,
            "code": "SEEDR_TOKEN_MISSING",
            "message": "Seedr API token is not configured in Render.",
        }

    try:
        # The old working integration treats a successful /user call as the
        # authentication test. Do the same here; /user is explicitly documented
        # by Seedr for Bearer-authenticated requests.
        await _main.seedr_request(
            "/tasks" if _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()) == "pat" else "/user"
        )
        return {
            "configured": True,
            "authenticated": True,
            "code": "SEEDR_AUTH_OK",
        }
    except _main.SeedrError as exc:
        return {
            "configured": True,
            "authenticated": False,
            "code": exc.code,
            "message": exc.detail,
        }

@router.get("/api/seedr/quota")
async def seedr_quota():
    if not _main.current_seedr_token():
        return {"configured": False, "maxSpace": 0, "usedSpace": 0, "remainingSpace": 0}

    try:
        is_pat = _main._seedr_session_auth_mode(_main._seedr_request_session_id.get().strip()) == "pat"
        result = _main.seedr_data(await _main.seedr_request("/fs/root/contents" if is_pat else "/user"))
        storage = result.get("account", {}).get("storage", {}) if isinstance(result, dict) else {}
        if not isinstance(storage, dict):
            storage = result.get("storage", {}) if isinstance(result, dict) else {}
        result_dict = result if isinstance(result, dict) else {}
        if is_pat:
            max_space = int(float(
                result_dict.get("space_max", 0)
                or result_dict.get("max_space", 0)
                or result_dict.get("maxSpace", 0)
                or storage.get("limit", 0)
            ))
            used = int(float(
                result_dict.get("space_used", 0)
                or result_dict.get("used_space", 0)
                or result_dict.get("usedSpace", 0)
                or storage.get("used", 0)
            ))
        else:
            max_space = int(float(
                storage.get("limit")
                or storage.get("max_space")
                or storage.get("maxSpace")
                or result_dict.get("max_space", 0)
                or result_dict.get("space_max", 0)
                or 0
            ))
            used = int(float(
                storage.get("used")
                or storage.get("used_space")
                or storage.get("usedSpace")
                or result_dict.get("used_space", 0)
                or result_dict.get("space_used", 0)
                or 0
            ))
    except _main.SeedrError:
        raise
    except Exception as exc:
        _main.logger.warning("Seedr quota parsing failed: %s", exc)
        raise _main.SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            503,
            "Seedr account storage information is temporarily unavailable.",
        ) from exc

    if not (max_space > 0) or used < 0 or used > max_space:
        raise _main.SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            503,
            "Seedr account storage information is temporarily unavailable.",
        )

    return {
        "configured": True,
        "maxSpace": max_space,
        "usedSpace": used,
        "remainingSpace": max(0, max_space - used),
    }

async def _prepare_seedr_space(
    required_bytes: int,
    *,
    force_one: bool = False,
) -> list[dict[str, _main.Any]]:
    """Free old completed Seedr folders; optionally force at least one deletion after a quota rejection."""
    required = max(0, int(required_bytes or 0))
    if required <= 0:
        return []

    quota = await _main.seedr_quota()
    remaining = int(quota.get("remainingSpace") or 0)
    if remaining >= required and not force_one:
        return []

    library = await _main.get_seedr_metadata_tree(force_refresh=True)
    folders = [
        folder for folder in (library.get("folders") or [])
        if isinstance(folder, dict)
        and str(folder.get("folderId") or folder.get("id") or "").strip()
        and str(folder.get("folderId") or folder.get("id") or "").strip() != "0"
    ]

    # Prefer actual Seedr task timestamps so the oldest completed torrent is
    # removed first. Fall back to the library order when a provider response
    # does not expose a timestamp.
    task_meta: dict[str, dict[str, Any]] = {}
    active_folder_ids: set[str] = set()
    try:
        tasks_payload = _main.seedr_data(await _main.seedr_request("/tasks"))
        for raw_task in _main.arr(tasks_payload, ("tasks", "torrents", "items")):
            task = _main.unwrap_seedr_task(_main.seedr_data(raw_task))
            if not task:
                continue
            folder_id = _main.seedr_task_folder_id(task)
            if not folder_id:
                continue
            if not _main.task_complete(task):
                active_folder_ids.add(str(folder_id))
            task_meta[str(folder_id)] = task
    except (_main.HTTPException, _main.SeedrError):
        pass

    def task_timestamp(task: dict[str, Any]) -> float:
        for key in (
            "created_at", "createdAt", "created", "added_at", "addedAt",
            "time_added", "timeAdded", "timestamp", "date_added", "dateAdded",
        ):
            value = task.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value if value < 10_000_000_000 else value / 1000)
            if isinstance(value, str) and value.strip():
                try:
                    parsed = _main.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return parsed.timestamp()
                except Exception:
                    continue
        return 0.0

    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for index, folder in enumerate(folders):
        folder_id = str(folder.get("folderId") or folder.get("id") or "").strip()
        if not folder_id or folder_id in active_folder_ids:
            continue
        task = task_meta.get(folder_id, {})
        candidates.append((index, task_timestamp(task), folder))

    candidates.sort(key=lambda item: (item[1] if item[1] > 0 else float("inf"), item[0]))

    deleted: list[dict[str, Any]] = []
    for _, _, folder in candidates:
        if remaining >= required and (not force_one or deleted):
            break

        folder_id = str(folder.get("folderId") or folder.get("id") or "").strip()
        folder_name = str(folder.get("torrentName") or folder.get("name") or folder_id).strip()
        folder_size = max(0, int(float(folder.get("totalSize") or 0)))

        try:
            await _main.seedr_request(f"/fs/folder/{_main.quote(folder_id)}", "DELETE")
            deleted.append({
                "folderId": folder_id,
                "name": folder_name,
                "size": folder_size,
            })
            remaining += folder_size
            _main._seedr_folder_cache.pop(folder_id, None)
        except (_main.HTTPException, _main.SeedrError) as exc:
            _main.logger.warning(
                "Seedr automatic cleanup skipped folder=%s status=%s detail=%s",
                folder_id,
                getattr(exc, "status_code", 0),
                getattr(exc, "detail", str(exc)),
            )

    global _seedr_metadata_cache
    _main._seedr_metadata_cache = None
    _main._seedr_folder_cache.clear()

    if remaining < required:
        raise _main.SeedrError(
            "SEEDR_QUOTA_UNAVAILABLE",
            413,
            "Seedr does not have enough space for this torrent, even after removing older completed files.",
        )

    _main.logger.info(
        "Seedr automatic cleanup freed=%s required=%s deleted=%s",
        sum(int(item.get("size") or 0) for item in deleted),
        required,
        len(deleted),
    )
    return deleted

@router.post("/api/seedr/add")
async def seedr_add(request: _main.Request):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    # Direct Seedr mode:
    # validate without changing the input, then forward that exact string.
    try:
        payload = await request.json()
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        raise _main.HTTPException(400, "Request body must be JSON.")

    raw_magnet = payload.get("magnet")
    if not isinstance(raw_magnet, str) or not raw_magnet:
        raise _main.HTTPException(400, "A magnet link is required.")

    if not _main.info_hash(raw_magnet):
        raise _main.HTTPException(400, "A valid BTIH magnet link is required")

    requested_folder = str(payload.get("folder_id") or "").strip()
    folder = int(requested_folder) if requested_folder.isdigit() else 0
    auto_cleanup = bool(payload.get("auto_cleanup"))
    required_bytes = max(0, int(float(payload.get("required_bytes") or 0)))

    session_id = _main._seedr_request_session_id.get().strip()
    _main.logger.info(
        "Seedr add start: session=%s mode=%s magnet_length=%s magnet_info_hash=%s "
        "requested_folder=%s resolved_folder=%s",
        _main._seedr_session_fingerprint(session_id),
        _main._seedr_session_auth_mode(session_id),
        len(raw_magnet),
        _main.info_hash(raw_magnet),
        requested_folder or "<none>",
        folder,
    )

    # Treat every new Seedr add as replacing the prior unfinished transfer.
    # Do not depend on frontend localStorage/current-notice state: an old task
    # may still exist after a page refresh or backend/container restart.
    replace_task_id = str(payload.get("replace_task_id") or "").strip()
    cancelled_tasks = await _main._cancel_active_seedr_tasks_for_replacement(replace_task_id)

    deleted_folders: list[dict[str, Any]] = [
        {
            "folderId": str(item.get("folderId") or ""),
            "name": str(item.get("name") or "Cancelled Seedr task"),
            "size": max(0, int(item.get("size") or 0)),
        }
        for item in cancelled_tasks
        if item.get("folderDeleted") and str(item.get("folderId") or "").strip()
    ]
    if auto_cleanup and required_bytes > 0:
        deleted_folders.extend(await _main._prepare_seedr_space(required_bytes))

    try:
        task = _main.unwrap_seedr_task(await _main.add_task(raw_magnet, folder))
    except _main.SeedrError as exc:
        reason = str(exc.detail or "").lower()
        is_storage_shortage = (
            exc.status_code == 413
            or "not enough space" in reason
            or "insufficient storage" in reason
            or "insufficient space" in reason
        )
        if not (is_storage_shortage and auto_cleanup and required_bytes > 0):
            _main.logger.exception(
                "Seedr add failed: session=%s mode=%s code=%s status=%s detail=%s",
                _main._seedr_session_fingerprint(session_id),
                _main._seedr_session_auth_mode(session_id),
                exc.code,
                exc.status_code,
                exc.detail,
            )
            raise

        # Seedr can reject a new magnet even when its quota endpoint appears to
        # have enough free bytes (for example, while an old transfer is stuck
        # reserving space). Cancelled tasks are already removed; now delete at
        # least one oldest completed folder and retry the same requested magnet
        # exactly once.
        _main.logger.warning(
            "Seedr reported insufficient space after preflight; reclaiming one more completed folder before retry: required=%s",
            required_bytes,
        )
        deleted_folders.extend(await _main._prepare_seedr_space(required_bytes, force_one=True))
        try:
            task = _main.unwrap_seedr_task(await _main.add_task(raw_magnet, folder))
        except _main.SeedrError as retry_exc:
            _main.logger.exception(
                "Seedr retry after storage cleanup failed: session=%s code=%s status=%s detail=%s",
                _main._seedr_session_fingerprint(session_id),
                retry_exc.code,
                retry_exc.status_code,
                retry_exc.detail,
            )
            raise
    except Exception as exc:
        _main.logger.exception(
            "Seedr add unexpected failure: session=%s mode=%s exception_type=%s exception=%s",
            _main._seedr_session_fingerprint(session_id),
            _main._seedr_session_auth_mode(session_id),
            type(exc).__name__,
            str(exc)[:2000],
        )
        raise
    tid = _main.task_id(task)
    if not tid:
        raise _main.HTTPException(502, "Seedr did not return a task id")

    task_folder_id = _main.seedr_task_folder_id(task)
    task_name = _main.seedr_task_name(task) or f"Torrent {tid}"
    _main.schedule_seedr_cleanup(str(tid), task_name, task_folder_id)

    _main.logger.info(
        "Seedr add success: session=%s mode=%s task_id=%s folder_id=%s task_name=%s",
        _main._seedr_session_fingerprint(session_id),
        _main._seedr_session_auth_mode(session_id),
        tid,
        task_folder_id or "<none>",
        task_name[:300],
    )

    return {
        "backend": "seedr",
        "task_id": int(tid) if tid.isdigit() else tid,
        "id": int(tid) if tid.isdigit() else tid,
        "torrent_name": task_name,
        "folder_id": task_folder_id,
        "deleted_folders": deleted_folders,
        "cancelled_tasks": cancelled_tasks,
    }
