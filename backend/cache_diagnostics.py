"""Shared cache and outbound HTTP diagnostics for the FastAPI backend."""
import contextvars
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx


# Process-local cache/API diagnostics. Counts reset on each worker restart.
_CACHE_DIAGNOSTICS_STARTED_AT = time.time()
_CACHE_DIAGNOSTICS: dict[str, Any] = {
    "cache_events": {},
    "upstream_requests": 0,
    "upstream_failures": 0,
    "upstream_by_provider": {},
    "upstream_by_endpoint": {},
    "upstream_by_feature": {},
    "upstream_status_codes": {},
}


_UPSTREAM_FEATURE = contextvars.ContextVar("cache_diagnostics_feature", default="background")


def _record_cache_event(store: str, event: str, count: int = 1) -> None:
    """Count cache decisions, not the number of rows inside a cache file."""
    stores = _CACHE_DIAGNOSTICS["cache_events"]
    events = stores.setdefault(str(store), {})
    key = str(event)
    events[key] = int(events.get(key) or 0) + max(0, int(count))


def _upstream_provider_name(host: str) -> str:
    host = (host or "").casefold().strip(".")
    known = (
        (("api.themoviedb.org",), "tmdb"),
        (("image.tmdb.org",), "tmdb_images"),
        (("api.watchmode.com",), "watchmode"),
        (("api.movieofthenight.com",), "streaming_availability"),
        (("api.codespecters.com",), "nexstream"),
        (("v3.sg.media-imdb.com",), "imdb_suggestions"),
        (("itunes.apple.com",), "apple_itunes"),
        (("www.omdbapi.com", "omdbapi.com"), "omdb"),
        (("api.tvmaze.com",), "tvmaze"),
        (("seedr.cc", "www.seedr.cc", "v2.seedr.cc"), "seedr"),
        (("api.knaben.org",), "knaben"),
        (("itorrents.net", "www.itorrents.net"), "itorrents"),
        (("cinemeta-live.vercel.app",), "cinemeta"),
    )
    for hosts, label in known:
        if host in hosts:
            return label
    if "yts" in host or "accel.li" in host:
        return "yts"
    if "1337x" in host or "1337.to" in host:
        return "1337x"
    if "limetorrents" in host:
        return "limetorrents"
    if "thepiratebay" in host or host == "apibay.org":
        return "thepiratebay"
    if host.endswith(".onrender.com") or host.endswith(".fly.dev") or host.endswith(".railway.app"):
        return "hosted_service:" + host
    return host or "unknown_host"


def _upstream_endpoint_group(path: str) -> str:
    # Keep endpoint names useful for diagnosis without recording queries or IDs.
    cleaned = re.sub(r"/movie-\d+(?=/|$)", "/movie-{id}", path or "/")
    cleaned = re.sub(r"(?<=/)\d+(?=/|$)", "{id}", cleaned)
    cleaned = re.sub(r"/[0-9a-fA-F]{32,}(?=/|$)", "/{token-or-hash}", cleaned)
    pieces = [piece for piece in cleaned.split("/") if piece]
    return "/" + "/".join(pieces[:4]) if pieces else "/"


def _record_upstream_request(url: Any, status_code: int | None = None, failed: bool = False) -> None:
    try:
        parsed = urlsplit(str(url))
        provider = _upstream_provider_name(parsed.hostname or "")
        endpoint = _upstream_endpoint_group(parsed.path)
        feature_key = str(_UPSTREAM_FEATURE.get()) + " | " + provider + " " + endpoint
        by_feature = _CACHE_DIAGNOSTICS["upstream_by_feature"]
        by_feature[feature_key] = int(by_feature.get(feature_key) or 0) + 1
        _CACHE_DIAGNOSTICS["upstream_requests"] += 1
        if failed:
            _CACHE_DIAGNOSTICS["upstream_failures"] += 1
        by_provider = _CACHE_DIAGNOSTICS["upstream_by_provider"]
        by_provider[provider] = int(by_provider.get(provider) or 0) + 1
        by_endpoint = _CACHE_DIAGNOSTICS["upstream_by_endpoint"]
        endpoint_key = provider + " " + endpoint
        by_endpoint[endpoint_key] = int(by_endpoint.get(endpoint_key) or 0) + 1
        status_key = "network_error" if failed else (
            str(status_code) if status_code is not None else "no_status"
        )
        statuses = _CACHE_DIAGNOSTICS["upstream_status_codes"]
        statuses[status_key] = int(statuses.get(status_key) or 0) + 1
    except Exception:
        # Observability must never break the request that it measures.
        return


class _InstrumentedAsyncClient(httpx.AsyncClient):
    """Count backend HTTP sends, including calls made through client.stream()."""

    async def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        try:
            response = await super().send(request, *args, **kwargs)
        except Exception:
            _record_upstream_request(request.url, failed=True)
            raise
        _record_upstream_request(request.url, status_code=response.status_code)
        return response
