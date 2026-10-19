"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main

__all__ = ['resolve_search_magnet']

router = APIRouter()

@router.post("/api/search/resolve-magnet")
async def resolve_search_magnet(body: dict[str, _main.Any]):
    """Resolve a provider detail page to a magnet only when the user prepares it."""
    info_url = str(body.get("infoUrl") or body.get("info_url") or "").strip()
    if not info_url:
        raise _main.HTTPException(400, "A provider detail URL is required.")

    parsed = _main.urlsplit(info_url)
    allowed_hosts = {
        str(host).lower().removeprefix("www.")
        for host in (*_main.X1337_HOSTS, *_main.LIMETORRENTS_HOSTS)
    }
    hostname = str(parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https" or hostname not in allowed_hosts:
        raise _main.HTTPException(400, "The provider URL is not allowed.")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        # Follow redirects manually so every hop is checked against the
        # allowlist BEFORE it is requested (httpx would otherwise fetch an
        # unapproved redirect target and we would only notice afterwards).
        async with _main._InstrumentedAsyncClient(timeout=8.0, follow_redirects=False, headers=headers) as client:
            current_url = info_url
            response = None
            for _ in range(6):
                response = await client.get(current_url)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    break
                next_url = _main.urljoin(current_url, location)
                next_parts = _main.urlsplit(next_url)
                next_host = str(next_parts.hostname or "").lower().removeprefix("www.")
                if next_parts.scheme != "https" or next_host not in allowed_hosts:
                    raise _main.HTTPException(400, "The provider redirected to an unapproved domain.")
                current_url = next_url
            else:
                raise _main.HTTPException(502, "The torrent provider redirected too many times.")
            response.raise_for_status()
    except _main.HTTPException:
        raise
    except _main.httpx.HTTPError as exc:
        _main.logger.info("Lazy magnet resolution failed for provider URL %s: %s", info_url, exc)
        raise _main.HTTPException(502, "Could not reach the torrent provider. Please try another result.") from exc

    # Prefer a real magnet anchor when the detail page includes one.
    # Parsing hrefs handles HTML-escaped ampersands better than scanning the
    # entire page for arbitrary strings.
    page = _main.BeautifulSoup(response.text or "", "html.parser")
    magnet = ""
    for anchor in page.select('a[href^="magnet:"]'):
        candidate = _main.unescape(str(anchor.get("href") or "")).strip()
        if _main.info_hash(candidate):
            magnet = candidate
            break

    if not magnet:
        match = _main.re.search(
            r"magnet:\?xt=urn:btih:[^\"<\s]+",
            response.text,
            _main.re.IGNORECASE,
        )
        if match:
            candidate = _main.unescape(match.group(0))
            if _main.info_hash(candidate):
                magnet = candidate

    if not magnet:
        # LimeTorrents also exposes a hash on its download-icon/.torrent URL.
        # Recover a standard magnet when the detail page omits a magnet anchor.
        download_anchor = (
            page.select_one("a.csprite_dl14[href]")
            or page.select_one("a[href*='/download/']")
        )
        download_href = str(download_anchor.get("href") or "").strip() if download_anchor else ""
        hash_match = _main.re.search(
            r"(?i)(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
            download_href,
        )
        if hash_match:
            digest = hash_match.group(1).lower()
            title_node = page.select_one("#content h1, h1, .tt-name")
            display_name = (
                title_node.get_text(" ", strip=True)
                if title_node is not None
                else _main.Path(parsed.path).stem.replace("-", " ")
            )
            magnet = f"magnet:?xt=urn:btih:{digest}&dn={_main.quote(display_name, safe='')}"
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet += "&tr=" + _main.quote(tracker, safe="")

    digest = _main.info_hash(magnet)
    if not digest:
        raise _main.HTTPException(404, "The provider did not expose a usable magnet/hash for this result.")
    return {"magnet": magnet, "infoHash": digest}
