"""Application functions for this feature area."""

import main as _main

__all__ = ['_provider_key_id', '_provider_key_available', '_provider_key_failure', '_provider_key_success', '_retry_after_seconds', '_watchmode_get', '_record_media_bytes', '_media_download_speed', '_diagnostic_feature_for_path', '_set_upstream_diagnostic_feature', 'handle_seedr_error']

def _provider_key_id(provider: str, key: str) -> str:
    return provider + ":" + _main.hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

def _provider_key_available(provider: str, key: str) -> bool:
    return _main._PROVIDER_KEY_COOLDOWNS.get(_main._provider_key_id(provider, key), 0.0) <= _main.time.time()

def _provider_key_failure(provider: str, key: str, seconds: float, reason: str) -> None:
    identity = _main._provider_key_id(provider, key)
    _main._PROVIDER_KEY_FAILURES[identity] = _main._PROVIDER_KEY_FAILURES.get(identity, 0) + 1
    _main._PROVIDER_KEY_COOLDOWNS[identity] = max(_main._PROVIDER_KEY_COOLDOWNS.get(identity, 0.0), _main.time.time() + max(1.0, seconds))
    _main.logger.warning("%s credential %s cooling down for %.0fs (%s; failures=%s)",
        provider, identity.rsplit(":", 1)[-1], max(1.0, seconds), reason, _main._PROVIDER_KEY_FAILURES[identity])

def _provider_key_success(provider: str, key: str) -> None:
    identity = _main._provider_key_id(provider, key)
    _main._PROVIDER_KEY_FAILURES.pop(identity, None)
    _main._PROVIDER_KEY_COOLDOWNS.pop(identity, None)

def _retry_after_seconds(response: _main.httpx.Response, default: float = 60.0) -> float:
    try:
        return max(1.0, min(21600.0, float(str(response.headers.get("Retry-After") or "").strip())))
    except (TypeError, ValueError):
        return default

async def _watchmode_get(path: str, *, params: dict[str, _main.Any] | None = None,
    timeout: _main.httpx.Timeout | float = 10.0, auth_header: bool = True) -> _main.httpx.Response | None:
    """Rotate keys when available, but honor Watchmode's shared rate-limit cooldown."""
    global _WATCHMODE_RATE_LIMITED_UNTIL
    if _main.CACHE_ONLY_MODE or not _main.WATCHMODE_API_KEYS:
        return None
    # Watchmode rate limits and monthly credits are plan/account governed. Once any
    # credential receives 429, pause all credentials in this process instead of
    # immediately retrying the same account through another key.
    if _main._WATCHMODE_RATE_LIMITED_UNTIL > _main.time.time():
        _main.logger.debug("Watchmode request skipped during shared rate-limit cooldown (%s)", path)
        return None
    provider = "watchmode"
    request_params = dict(params or {})
    start = _main._PROVIDER_KEY_CURSORS.get(provider, 0) % len(_main.WATCHMODE_API_KEYS)
    ordered = _main.WATCHMODE_API_KEYS[start:] + _main.WATCHMODE_API_KEYS[:start]
    _main._PROVIDER_KEY_CURSORS[provider] = (start + 1) % len(_main.WATCHMODE_API_KEYS)
    last_response = None
    transient_failed: list[str] = []
    eligible = 0

    async def attempt(key: str) -> tuple[httpx.Response | None, bool]:
        nonlocal last_response, eligible
        global _WATCHMODE_RATE_LIMITED_UNTIL
        if not _main._provider_key_available(provider, key):
            return None, False
        eligible += 1
        headers = {"Accept": "application/json"}
        query = dict(request_params)
        if auth_header:
            headers["X-API-Key"] = key
            query.pop("apiKey", None)
        else:
            query["apiKey"] = key
        try:
            global _WATCHMODE_HTTP_CLIENT
            if _main._WATCHMODE_HTTP_CLIENT is None or _main._WATCHMODE_HTTP_CLIENT.is_closed:
                _main._WATCHMODE_HTTP_CLIENT = _main._InstrumentedAsyncClient(
                    timeout=_main.httpx.Timeout(10.0, connect=4.0),
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                    limits=_main.httpx.Limits(
                        max_connections=6,
                        max_keepalive_connections=3,
                        keepalive_expiry=30.0,
                    ),
                )
            async with _main._WATCHMODE_REQUEST_SEMAPHORE:
                response = await _main._WATCHMODE_HTTP_CLIENT.get(
                    f"{_main.WATCHMODE_API_BASE}{path}",
                    params=query,
                    headers=headers,
                    timeout=timeout,
                )
            last_response = response
            if response.status_code < 300:
                _main._provider_key_success(provider, key)
                return response, True
            if response.status_code == 429:
                # Watchmode documents plan-level request limits and account-level
                # quota. Treat 429 as a provider-wide cooldown, not a per-key issue.
                retry_after = _main._retry_after_seconds(response)
                _main._WATCHMODE_RATE_LIMITED_UNTIL = max(
                    _main._WATCHMODE_RATE_LIMITED_UNTIL, _main.time.time() + retry_after
                )
                _main._provider_key_failure(provider, key, retry_after, "HTTP 429 (shared cooldown)")
                _main.logger.warning(
                    "Watchmode shared cooldown activated for %.0fs on %s (limit=%s remaining=%s quota_used=%s)",
                    retry_after, path,
                    response.headers.get("X-RateLimit-Limit", "unknown"),
                    response.headers.get("X-RateLimit-Remaining", "unknown"),
                    response.headers.get("X-Account-Quota-Used", "unknown"),
                )
                return response, True
            if response.status_code == 401:
                _main._provider_key_failure(provider, key, 3600, "HTTP 401")
                return None, False
            if response.status_code == 403:
                # A plan/access restriction is not a transient credential failure.
                _main._provider_key_failure(provider, key, 3600, "HTTP 403")
                return response, True
            if response.status_code >= 500:
                transient_failed.append(key)
                return None, False
            return response, True
        except _main.httpx.HTTPError as exc:
            _main.logger.info("Watchmode transport failure for %s credential %s: %s",
                path, _main._provider_key_id(provider, key).rsplit(":", 1)[-1], type(exc).__name__)
            transient_failed.append(key)
            return None, False

    for key in ordered:
        response, terminal = await attempt(key)
        if response is not None and terminal:
            return response
    if transient_failed:
        await _main.asyncio.sleep(0.75)
        retry_keys = list(dict.fromkeys(transient_failed))
        transient_failed.clear()
        for key in retry_keys:
            response, terminal = await attempt(key)
            if response is not None and terminal:
                return response
        # Transport failures and HTTP 5xx responses do not prove the key is invalid.
        # Keep the key eligible; explicit 401/403/429 responses have their own policy.
    if last_response is not None:
        return last_response
    if not eligible:
        _main.logger.info("All Watchmode keys are cooling down for %s", path)
    return None

def _record_media_bytes(file_id: str, amount: int) -> None:
    if amount <= 0:
        return
    now = _main.time.monotonic()
    bucket = _main._media_download_stats.setdefault(file_id, _main.deque())
    bucket.append((now, amount))
    cutoff = now - _main._MEDIA_STATS_WINDOW_SECONDS
    while bucket and bucket[0][0] < cutoff:
        bucket.popleft()

def _media_download_speed(file_id: str) -> float:
    now = _main.time.monotonic()
    bucket = _main._media_download_stats.get(file_id)
    if not bucket:
        return 0.0
    cutoff = now - _main._MEDIA_STATS_WINDOW_SECONDS
    while bucket and bucket[0][0] < cutoff:
        bucket.popleft()
    if not bucket:
        return 0.0
    total = sum(amount for _, amount in bucket)
    span = max(0.5, now - bucket[0][0])
    return total / span

def _diagnostic_feature_for_path(path: str) -> str:
    cleaned = _main.re.sub(r"/movie-\d+(?=/|$)", "/movie-{id}", path or "/")
    cleaned = _main.re.sub(r"(?<=/)\d+(?=/|$)", "{id}", cleaned)
    return "request:" + cleaned[:120]

@_main.app.middleware("http")
async def _set_upstream_diagnostic_feature(request: _main.Request, call_next):
    token = _main._UPSTREAM_FEATURE.set(_main._diagnostic_feature_for_path(request.url.path))
    try:
        return await call_next(request)
    finally:
        _main._UPSTREAM_FEATURE.reset(token)

@_main.app.exception_handler(_main.SeedrError)
async def handle_seedr_error(request: _main.Request, exc: _main.SeedrError):
    return _main.JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "detail": exc.detail,
            "code": exc.code,
            "provider": "seedr",
        },
    )
