"""Application functions for this feature area."""

import main as _main

__all__ = ['_seedr_empty_session', '_seedr_new_session', '_seedr_get_session', '_seedr_session_cookie_value', '_seedr_restore_session', 'current_seedr_token', '_seedr_session_token', '_seedr_session_auth_mode', '_seedr_session_fingerprint', '_clear_seedr_session_token', 'attach_seedr_session', '_seedr_session_csrf_ok', 'add_timing_allow_origin']

def _seedr_empty_session() -> dict[str, _main.Any]:
    now = _main.time.time()
    return {
        "csrf_token": _main.secrets.token_urlsafe(32),
        "access_token": "",
        "refresh_token": "",
        "token_type": "Bearer",
        "auth_mode": "legacy",
        "device": None,
        "created_at": now,
        "last_seen": now,
    }

def _seedr_new_session() -> tuple[str, dict[str, _main.Any]]:
    session_id = _main.secrets.token_urlsafe(32)
    session = _main._seedr_empty_session()
    _main._seedr_sessions[session_id] = session
    return session_id, session

def _seedr_get_session(session_id: str) -> dict[str, _main.Any]:
    return _main._seedr_sessions.setdefault(session_id, _main._seedr_empty_session())

def _seedr_session_cookie_value(session_id: str, session: dict[str, _main.Any]) -> str:
    payload = {
        "v": 1,
        "sid": session_id,
        "csrf": str(session.get("csrf_token") or ""),
        "access": str(session.get("access_token") or ""),
        "refresh": str(session.get("refresh_token") or ""),
        "type": str(session.get("token_type") or "Bearer"),
        "mode": str(session.get("auth_mode") or "legacy"),
        "device": session.get("device") if isinstance(session.get("device"), dict) else None,
        "created": float(session.get("created_at") or _main.time.time()),
        "last": _main.time.time(),
    }
    encrypted = _main._seedr_fernet.encrypt(
        _main.json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return "v1." + encrypted.decode("ascii")

def _seedr_restore_session(raw_cookie: str) -> tuple[str, dict[str, _main.Any]] | None:
    if not raw_cookie.startswith("v1."):
        return None
    try:
        payload = _main.json.loads(_main._seedr_fernet.decrypt(raw_cookie[3:].encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        session_id = str(payload.get("sid") or "").strip()
        csrf_token = str(payload.get("csrf") or "").strip()
        if not session_id or not csrf_token:
            return None
        now = _main.time.time()
        last_seen = float(payload.get("last") or 0)
        if not last_seen or now - last_seen > _main.SEEDR_SESSION_TTL_SECONDS:
            return None
        return session_id, {
            "csrf_token": csrf_token,
            "access_token": _main.normalize_seedr_token(str(payload.get("access") or "")),
            "refresh_token": str(payload.get("refresh") or "").strip(),
            "token_type": str(payload.get("type") or "Bearer").strip() or "Bearer",
            "auth_mode": str(payload.get("mode") or "legacy").strip().lower() or "legacy",
            "device": payload.get("device") if isinstance(payload.get("device"), dict) else None,
            "created_at": float(payload.get("created") or now),
            "last_seen": now,
        }
    except (_main.InvalidToken, ValueError, TypeError, _main.json.JSONDecodeError):
        return None

def current_seedr_token() -> str:
    """Return the Seedr token for the current request's connected account."""
    token = _main._seedr_request_token.get().strip()
    if token:
        return _main.normalize_seedr_token(token)
    if _main.ALLOW_LEGACY_SEEDR_TOKEN and _main.SEEDR_TOKEN:
        return _main.normalize_seedr_token(_main.SEEDR_TOKEN)
    return ""

def _seedr_session_token(session_id: str) -> str:
    session = _main._seedr_sessions.get(session_id)
    return _main.normalize_seedr_token(str(session.get("access_token") or "")) if session else ""

def _seedr_session_auth_mode(session_id: str) -> str:
    session = _main._seedr_sessions.get(session_id)
    mode = str(session.get("auth_mode") or "legacy").strip().lower() if session else "legacy"
    return mode if mode in {"legacy", "pat"} else "legacy"

def _seedr_session_fingerprint(session_id: str) -> str:
    """Short non-secret identifier for correlating session logs."""
    if not session_id:
        return "none"
    return _main.hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]

def _clear_seedr_session_token(session_id: str | None = None) -> None:
    sid = session_id or _main._seedr_request_session_id.get().strip()
    if not sid:
        return
    session = _main._seedr_sessions.get(sid)
    if not session:
        return
    session["access_token"] = ""
    session["refresh_token"] = ""
    session["token_type"] = "Bearer"
    session["auth_mode"] = "legacy"
    session["device"] = None
    session["last_seen"] = _main.time.time()

@_main.app.middleware("http")
async def attach_seedr_session(request: _main.Request, call_next):
    raw_cookie = str(request.cookies.get(_main.SEEDR_SESSION_COOKIE) or "").strip()
    restored = _main._seedr_restore_session(raw_cookie)
    is_new = False

    if restored:
        session_id, restored_session = restored
        _main._seedr_sessions[session_id] = restored_session
    elif raw_cookie in _main._seedr_sessions:
        session_id = raw_cookie
    else:
        session_id, _ = _main._seedr_new_session()
        is_new = True

    session = _main._seedr_get_session(session_id)
    session["last_seen"] = _main.time.time()

    token_ctx = _main._seedr_request_token.set(_main._seedr_session_token(session_id))
    session_ctx = _main._seedr_request_session_id.set(session_id)
    try:
        path = request.url.path or ""
        method = request.method.upper()
        # Every state-changing API call needs the session CSRF token, not only
        # /api/seedr/*: compatibility routes such as /api/files/delete and
        # /api/v2/torrents/* also act on the connected Seedr account.
        if path.startswith("/api/") and method not in {"GET", "HEAD", "OPTIONS"}:
            if not _main._seedr_session_csrf_ok(request):
                response = _main.JSONResponse(
                    status_code=403,
                    content={"error": "Seedr session security check failed.", "code": "SEEDR_CSRF_INVALID"},
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
    finally:
        _main._seedr_request_token.reset(token_ctx)
        _main._seedr_request_session_id.reset(session_ctx)

    is_https = (
        request.url.scheme.lower() == "https"
        or str(request.headers.get("x-forwarded-proto") or "").lower() == "https"
    )
    response.set_cookie(
        _main.SEEDR_SESSION_COOKIE,
        _main._seedr_session_cookie_value(session_id, session),
        max_age=_main.SEEDR_SESSION_TTL_SECONDS,
        httponly=True,
        secure=is_https,
        samesite=_main.SEEDR_SESSION_SAMESITE if is_https else "lax",
        path="/",
    )

    now = _main.time.time()
    if len(_main._seedr_sessions) > 2000:
        stale = [
            sid for sid, item in _main._seedr_sessions.items()
            if now - float(item.get("last_seen") or now) > _main.SEEDR_SESSION_TTL_SECONDS
        ]
        for sid in stale:
            _main._seedr_sessions.pop(sid, None)

    return response

def _seedr_session_csrf_ok(request: _main.Request) -> bool:
    session_id = _main._seedr_request_session_id.get().strip()
    session = _main._seedr_sessions.get(session_id)
    if not session:
        return False
    expected = str(session.get("csrf_token") or "")
    supplied = str(request.headers.get("X-Torrent-Studio-CSRF") or "")
    return bool(expected and supplied and _main.secrets.compare_digest(expected, supplied))

@_main.app.middleware("http")
async def add_timing_allow_origin(request: _main.Request, call_next):
    # Allows the frontend to read Resource Timing transfer sizes for the
    # cross-origin Render media stream when the frontend is hosted on Vercel.
    response = await call_next(request)
    response.headers["Timing-Allow-Origin"] = "*"
    return response
