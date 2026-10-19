"""Application functions for this feature area."""

import main as _main
from typing import Any

__all__ = ['_tvmaze_query', '_x1337_rows', 'search_1337x_direct', 'search_yts_movies', 'search_tv_eztv', '_limetorrents_rows', 'search_limetorrents', 'search_knaben', 'search_torrents_csv', 'search_apibay', '_search_cache_key', '_trim_search_cache', '_search_1337x_uncached', 'search_1337x', 'parse_size']

def _tvmaze_query(value: str) -> str:
    value = _main.re.sub(r"\bS\d{1,2}(?:E\d{1,3})?.*$", "", value, flags=_main.re.I)
    value = _main.re.sub(r"\b(?:season|series)\s*\d+\b", "", value, flags=_main.re.I)
    value = _main.re.sub(r"\b(?:19|20)\d{2}\b", "", value)
    return _main.re.sub(r"\s+", " ", value).strip()

def _x1337_rows(html_text: str) -> list[dict[str, str]]:
    """Parse 1337x search rows across mirror HTML variations."""
    soup = _main.BeautifulSoup(html_text or "", "html.parser")
    rows: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for link in soup.find_all("a", href=_main.re.compile(r"^/torrent/")):
        href = str(link.get("href") or "").strip()
        if not href or href in seen_paths:
            continue
        row = link.find_parent("tr")
        if row is None:
            continue
        title = link.get_text(" ", strip=True)
        if not title:
            continue
        cells = row.find_all("td")
        cell_text = [cell.get_text(" ", strip=True) for cell in cells]
        row_text = " ".join(cell_text) or row.get_text(" ", strip=True)
        size = ""
        seeders = "0"
        leechers = "0"
        for cell in cells:
            classes = " ".join(cell.get("class") or []).lower()
            text = cell.get_text(" ", strip=True)
            size_match = _main.re.search(r"([\d.]+\s*[KMGT]i?B)", text, _main.re.I)
            if not size and size_match:
                size = size_match.group(1)
            number = _main.re.search(r"\d[\d,]*", text)
            if number and "seed" in classes:
                seeders = number.group(0).replace(",", "")
            elif number and ("leech" in classes or "leeches" in classes):
                leechers = number.group(0).replace(",", "")
        if not size:
            size_match = _main.re.search(r"([\d.]+\s*[KMGT]i?B)", row_text, _main.re.I)
            if size_match:
                size = size_match.group(1)
        numeric_cells = [_main.re.sub(r"[^\d,]", "", text) for text in cell_text if _main.re.fullmatch(r"\s*[\d,]+\s*", text or "")]
        if seeders == "0" and len(numeric_cells) >= 2:
            seeders = numeric_cells[-2] or "0"
        if leechers == "0" and len(numeric_cells) >= 1:
            leechers = numeric_cells[-1] or "0"
        seen_paths.add(href)
        sub_href = ""
        sub_id = ""
        if cells:
            sub_link = cells[0].find("a", href=_main.re.compile(r"^/sub/"))
            if sub_link is not None:
                sub_href = str(sub_link.get("href") or "").strip()
                sub_match = _main.re.search(r"^/sub/(\d+)/", sub_href)
                if sub_match:
                    sub_id = sub_match.group(1)

        rows.append({
            "title": title,
            "path": href,
            "size": size or "0 B",
            "seeders": seeders,
            "leechers": leechers,
            "subcategory_path": sub_href,
            "subcategory_id": sub_id,
        })
    return rows

async def search_1337x_direct(
    query: str,
    limit: int = 50,
    pages: int = 2,
    category: str | None = None,
    provider_query: str | None = None,
) -> list[dict[str, _main.Any]]:
    if not _main._MANUAL_TORRENT_SEARCH.get():
        _main._record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Primary 1337x search using media categories and cheap listing-page scraping.

    The unsorted category route is the reliable base path. The server-side
    seeders-sorted route is fetched as an optional second listing because 1337x
    integrations document that sorted keyword searches can fail under load.
    """
    q, season, episode = _main._media_search_parts(query)
    if not q:
        return []
    q = _main.re.sub(r"\s+", " ", provider_query or _main._media_provider_query(query)).strip()
    if not q:
        return []

    encoded = _main.quote(q, safe="").replace("%20", "+")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # Apply the shared configured search-size window.
    minimum_size, maximum_size = _main.MIN_SEARCH_RESULT_SIZE_BYTES, _main.MAX_SEARCH_RESULT_SIZE_BYTES
    categories = (
        [category]
        if category in {"Movies", "TV"}
        else (["TV"] if season is not None or episode is not None else ["Movies", "TV"])
    )

    async with _main._InstrumentedAsyncClient(
        timeout=_main.SEARCH_SOURCE_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:

        async def find_category_page(category_name: str) -> tuple[str, str]:
            # Probe only the normal category-search route. One cheap route is
            # enough to identify a usable mirror; do not serially probe both
            # normal and sorted routes across every mirror.
            path = f"/category-search/{encoded}/{category_name}/1/"
            for host in _main.X1337_HOSTS:
                try:
                    async with _main._X1337_REQUEST_SEMAPHORE:
                        response = await client.get(f"https://{host}{path}")
                    if response.status_code < 400 and _main._x1337_rows(response.text):
                        return f"https://{host}", response.text
                except _main.httpx.HTTPError:
                    continue
            return "", ""

        category_pages = await _main.asyncio.gather(
            *(find_category_page(name) for name in categories),
            return_exceptions=False,
        )

        # Generic search is also queried because language searches can surface
        # Hindi/dual-audio releases there that are not near the top of a
        # category search. Every generic row is classified through /sub/<id>/
        # so non-media categories cannot leak into the app.
        generic_base = next((base for base, _ in category_pages if base), "")
        generic_pages: list[str] = []
        if generic_base:
            generic_urls = [f"{generic_base}/search/{encoded}/1/"]
            if pages > 1:
                generic_urls.append(f"{generic_base}/search/{encoded}/2/")

            async def fetch_generic(url: str) -> str:
                try:
                    async with _main._X1337_REQUEST_SEMAPHORE:
                        response = await client.get(url)
                    if response.status_code < 400 and _main._x1337_rows(response.text):
                        return response.text
                except _main.httpx.HTTPError:
                    pass
                return ""

            generic_pages = [
                value for value in await _main.asyncio.gather(
                    *(fetch_generic(url) for url in generic_urls),
                    return_exceptions=False,
                )
                if value
            ]

        async def fetch_category(
            category_name: str,
            base: str,
            first_page: str,
        ) -> list[dict[str, str]]:
            if not base or not first_page:
                return []

            # Normal page 1 + sorted page 1 are fetched together. Sorted is
            # treated as enrichment, never as a requirement.
            paths = [
                ("normal", first_page),
                (
                    "sorted",
                    f"{base}/sort-category-search/{encoded}/{category_name}/seeders/desc/1/",
                ),
            ]
            if pages > 1:
                paths.append((
                    "normal2",
                    f"{base}/category-search/{encoded}/{category_name}/2/",
                ))

            async def fetch_one(kind: str, value: str):
                if kind == "normal":
                    return value
                try:
                    async with _main._X1337_REQUEST_SEMAPHORE:
                        response = await client.get(value)
                    if response.status_code < 400 and _main._x1337_rows(response.text):
                        return response.text
                except _main.httpx.HTTPError:
                    pass
                return ""

            fetched = await _main.asyncio.gather(
                *(fetch_one(kind, value) for kind, value in paths),
                return_exceptions=False,
            )
            pages_html = [text for text in fetched if isinstance(text, str) and text]
            return pages_html

        fetched_by_category = await _main.asyncio.gather(
            *(
                fetch_category(name, base, first_page)
                for name, (base, first_page) in zip(categories, category_pages)
            ),
            return_exceptions=False,
        )

        kind_hint = "tv" if (season is not None or episode is not None) else "both"

        async def parse_candidates(
            category_name: str,
            html_pages: list[str],
            generic_search: bool = False,
        ) -> list[dict[str, str]]:
            target = _main._media_provider_query(query)
            tokens = _main._search_tokens(target)
            compact_query = "".join(tokens)
            candidates, seen_paths = [], set()

            for html_text in html_pages:
                for row in _main._x1337_rows(html_text):
                    path = row.get("path") or ""
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)

                    normalized = _main._normalize_title(row["title"])
                    compact_normalized = normalized.replace(" ", "")
                    if tokens and not (
                        all(token in normalized for token in tokens)
                        or (compact_query and compact_query in compact_normalized)
                    ):
                        continue
                    if not _main._season_episode_match(row["title"], season, episode):
                        continue

                    if generic_search:
                        sub_id = str(row.get("subcategory_id") or "")
                        if sub_id not in _main.X1337_MEDIA_SUBCATEGORIES:
                            continue
                        if kind_hint == "tv" and sub_id not in _main.X1337_TV_SUBCATEGORIES:
                            continue

                    size_match = _main.re.match(
                        r"([\d.]+)\s*([KMGT]i?B)",
                        row.get("size", ""),
                        _main.re.IGNORECASE,
                    )
                    if not size_match:
                        continue
                    units = {
                        "KB": 1024, "KIB": 1024,
                        "MB": 1024**2, "MIB": 1024**2,
                        "GB": 1024**3, "GIB": 1024**3,
                        "TB": 1024**4, "TIB": 1024**4,
                    }
                    size = int(
                        float(size_match.group(1))
                        * units[size_match.group(2).upper()]
                    )
                    if not minimum_size <= size <= maximum_size:
                        continue

                    row["size_bytes"] = str(size)
                    row["media_category"] = category_name
                    candidates.append(row)

            candidates.sort(
                key=lambda row: (
                    _main._title_relevance(row["title"], query)[0],
                    int(row.get("seeders") or 0),
                    int(row.get("leechers") or 0),
                ),
                reverse=True,
            )
            return candidates

        candidate_lists = await _main.asyncio.gather(
            *(
                parse_candidates(name, html_pages)
                for name, html_pages in zip(categories, fetched_by_category)
            ),
            return_exceptions=False,
        )

        candidates = [
            row
            for values in candidate_lists
            for row in values
        ]

        if generic_pages:
            candidates.extend(
                await parse_candidates(
                    "TV" if kind_hint == "tv" else "Movies",
                    generic_pages,
                    generic_search=True,
                )
            )

        unique: dict[str, dict[str, str]] = {}
        for row in candidates:
            key = row.get("path") or _main._normalize_title(row.get("title", ""))
            unique.setdefault(key, row)

        candidates = list(unique.values())
        candidates.sort(
            key=lambda row: (
                _main._title_relevance(row["title"], query)[0],
                int(row.get("seeders") or 0),
                int(row.get("leechers") or 0),
            ),
            reverse=True,
        )
        # Detail pages are the expensive part. Keep lookup fan-out bounded
        # on Render Free while letting DVD/HD/HEVC/dual-audio/h.264 and other
        # valid 1337x release types compete.
        candidates = candidates[:min(max(limit, 1), 20)]

        async def fetch_detail(row):
            # Keep useful listing results even when the provider's detail page
            # is slow or blocks server-side requests. Magnets are resolved on
            # demand by /api/search/resolve-magnet when a user clicks Prepare.
            detail_url = base_for_row(row) + row["path"]
            return {
                "guid": "1337x-" + _main.hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:20],
                "title": row["title"],
                "size": int(row["size_bytes"]),
                "seeders": int(row.get("seeders") or 0),
                "leechers": int(row.get("leechers") or 0),
                "indexer": "1337x",
                "protocol": "torrent",
                "publishDate": "",
                "magnetUrl": None,
                "infoHash": "",
                "downloadUrl": None,
                "infoUrl": detail_url,
                "sourceUrl": detail_url,
                "descriptorUrl": "",
                "category": row.get("media_category") or "Video",
            }

        # Preserve the base URL selected for each category without doing a
        # second mirror probe before opening detail pages.
        row_bases: dict[str, str] = {}
        for category_name, (base, _) in zip(categories, category_pages):
            if base:
                for row in candidate_lists[categories.index(category_name)]:
                    row_bases[row["path"]] = base

        def base_for_row(row: dict[str, str]) -> str:
            return row_bases.get(row["path"], category_pages[0][0] if category_pages else "")

        fetched = await _main.asyncio.gather(
            *(fetch_detail(row) for row in candidates),
            return_exceptions=True,
        )
    return [item for item in fetched if isinstance(item, dict)]

async def search_yts_movies(query: str, limit: int = 50) -> list[dict[str, _main.Any]]:
    if not _main._MANUAL_TORRENT_SEARCH.get():
        _main._record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Search YTS directly so movie searches are not lost in aggregate ranking."""
    movie_query = _main._media_provider_query(query)
    if not movie_query:
        return []

    payload = None
    async with _main._InstrumentedAsyncClient(timeout=12, follow_redirects=True) as client:
        for host in _main.YTS_API_HOSTS:
            try:
                async with _main._YTS_REQUEST_SEMAPHORE:
                    response = await client.get(
                        f"https://{host}/api/v2/list_movies.json",
                        params={"query_term": movie_query, "limit": "50"},
                        headers={"Accept": "application/json"},
                    )
                response.raise_for_status()
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
                    break
            except (_main.httpx.HTTPError, ValueError):
                continue

    if not isinstance(payload, dict):
        return []

    target_tokens = _main._search_tokens(movie_query)
    results: list[dict[str, Any]] = []
    for movie in (payload.get("data") or {}).get("movies") or []:
        if not isinstance(movie, dict):
            continue
        title = str(movie.get("title_long") or movie.get("title") or "").strip()
        if not title:
            continue
        title_tokens = _main._normalize_title(title)
        if target_tokens and not all(token in title_tokens for token in target_tokens):
            continue

        released = movie.get("date_uploaded_unix")
        try:
            from datetime import datetime, timezone
            published = datetime.fromtimestamp(
                int(released), tz=timezone.utc
            ).isoformat() if released else ""
        except Exception:
            published = ""

        for torrent in movie.get("torrents") or []:
            if not isinstance(torrent, dict):
                continue
            h = str(torrent.get("hash") or "").strip().lower()
            if not _main.re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            quality = str(torrent.get("quality") or "").strip()
            kind = str(torrent.get("type") or "").strip()
            suffix = " ".join(x for x in (quality, kind) if x)
            display_title = f"{title} [{suffix}]" if suffix else title
            magnet = (
                f"magnet:?xt=urn:btih:{h}&dn={_main.quote(display_title, safe='')}"
            )
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet += "&tr=" + _main.quote(tracker, safe="")

            results.append({
                "guid": f"yts-{h}",
                "title": display_title,
                "mediaTitle": title,
                "year": int(movie.get("year") or 0) or None,
                "rating": float(movie.get("rating") or 0) or None,
                "genres": [str(g).strip() for g in (movie.get("genres") or []) if str(g).strip()],
                "posterUrl": f"/api/poster?title={_main.quote(title, safe='')}&year={_main.quote(str(movie.get('year') or ''), safe='')}",
                "quality": suffix,
                "size": int(float(torrent.get("size_bytes") or 0)),
                "seeders": int(torrent.get("seeds") or 0),
                "leechers": int(torrent.get("peers") or 0),
                "indexer": "yts.mx",
                "protocol": "torrent",
                "publishDate": published,
                "magnetUrl": magnet,
                "infoHash": h,
                "downloadUrl": magnet,
                "infoUrl": "",
                "sourceUrl": "",
            })

    results.sort(
        key=lambda row: (row["seeders"] + row["leechers"], row["publishDate"]),
        reverse=True,
    )
    return results[:limit]

async def search_tv_eztv(query: str, limit: int = 30) -> list[dict[str, _main.Any]]:
    tv_query, _season, _episode = _main._media_search_parts(query)
    tv_query = _main._tvmaze_query(tv_query)
    if not tv_query:
        return []

    try:
        async with _main._InstrumentedAsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                "https://api.tvmaze.com/search/shows",
                params={"q": tv_query},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            matches = response.json()
    except (_main.httpx.HTTPError, ValueError):
        return []

    if not isinstance(matches, list):
        return []

    target = _main._normalize_title(tv_query)
    imdb_id = ""
    for match in matches[:10]:
        show = match.get("show") if isinstance(match, dict) else None
        if not isinstance(show, dict):
            continue
        name = str(show.get("name") or "").strip()
        external = show.get("externals")
        candidate = str(external.get("imdb") or "").strip() if isinstance(external, dict) else ""
        if candidate and _main._normalize_title(name) == target:
            imdb_id = candidate
            break

    if not imdb_id:
        return []

    payload: dict[str, Any] | None = None
    async with _main._InstrumentedAsyncClient(timeout=15, follow_redirects=True) as client:
        for host in ("eztv.yt", "eztvx.to"):
            try:
                response = await client.get(
                    f"https://{host}/api/get-torrents",
                    params={"imdb_id": imdb_id, "limit": "100", "page": "1"},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
                    break
            except (_main.httpx.HTTPError, ValueError):
                continue

    if not payload:
        return []

    results: list[dict[str, Any]] = []
    for item in payload.get("torrents") or []:
        if not isinstance(item, dict):
            continue
        h = str(item.get("hash") or "").strip().lower()
        if not _main.re.fullmatch(r"[0-9a-f]{40}", h):
            continue
        title = str(item.get("filename") or item.get("title") or "").strip()
        if not title:
            continue
        magnet = str(item.get("magnet_url") or "").strip()
        if not magnet:
            magnet = f"magnet:?xt=urn:btih:{h}&dn={_main.quote(title, safe='')}"
        try:
            from datetime import datetime, timezone
            published = datetime.fromtimestamp(
                int(item.get("date_released_unix") or 0), tz=timezone.utc
            ).isoformat() if item.get("date_released_unix") else ""
        except Exception:
            published = ""
        results.append({
            "guid": f"eztv-{h}",
            "title": title,
            "size": int(float(item.get("size_bytes") or 0)),
            "seeders": int(item.get("seeds") or 0),
            "leechers": int(item.get("peers") or 0),
            "indexer": "eztv.yt",
            "protocol": "torrent",
            "publishDate": published,
            "magnetUrl": magnet,
            "infoHash": h,
            "downloadUrl": magnet,
            "infoUrl": "",
            "sourceUrl": "",
        })
    _, season, episode = _main._media_search_parts(query)
    if season is not None:
        results = [
            row for row in results
            if _main._season_episode_match(row["title"], season, episode)
        ]

    results.sort(
        key=lambda row: (row["seeders"] + row["leechers"], row["publishDate"]),
        reverse=True,
    )
    return results[:limit]

def _limetorrents_rows(html_text: str, base_url: str) -> list[dict[str, str]]:
    """Parse LimeTorrents rows using the current table structure."""
    soup = _main.BeautifulSoup(html_text or "", "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    # LimeTorrents result rows may be direct children of table.table2
    # rather than tbody children, and may not carry a bgcolor attribute.
    # Avoid relying on either HTML detail.
    for row in soup.select("table.table2 tr"):
        # Prefer the visible title link over the empty download-icon link.
        title_anchor = row.select_one(".tt-name a[href*='-torrent-']")
        if title_anchor is None:
            title_anchor = next(
                (
                    anchor
                    for anchor in row.select(".tt-name a[href]")
                    if anchor.get_text(" ", strip=True)
                    and str(anchor.get("href") or "").lower().endswith(".html")
                ),
                None,
            )
        if title_anchor is None:
            continue

        title = title_anchor.get_text(" ", strip=True)
        detail_href = str(title_anchor.get("href") or "").strip()
        if not title or not detail_href:
            continue

        # LimeTorrents exposes the BTIH hash on the small download-icon
        # link in the listing row. Prefer this hash so Prepare can submit a
        # real magnet to Seedr without relying only on a detail-page scrape.
        download_anchor = (
            row.select_one(".tt-name a.csprite_dl14[href]")
            or row.select_one(".tt-name a[href*='/download/']")
        )
        download_href = str(download_anchor.get("href") or "").strip() if download_anchor else ""
        hash_match = _main.re.search(
            r"(?i)(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
            download_href,
        )
        torrent_hash = hash_match.group(1).lower() if hash_match else ""
        magnet_url = ""
        if torrent_hash:
            magnet_url = f"magnet:?xt=urn:btih:{torrent_hash}&dn={_main.quote(title, safe='')}"
            for tracker in (
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://open.stealth.si:80/announce",
            ):
                magnet_url += "&tr=" + _main.quote(tracker, safe="")

        seed_cell = row.select_one(".tdseed")
        leech_cell = row.select_one(".tdleech")
        seeders = "0"
        leechers = "0"
        if seed_cell is not None:
            match = _main.re.search(r"\d[\d,]*", seed_cell.get_text(" ", strip=True))
            if match:
                seeders = match.group(0).replace(",", "")
        if leech_cell is not None:
            match = _main.re.search(r"\d[\d,]*", leech_cell.get_text(" ", strip=True))
            if match:
                leechers = match.group(0).replace(",", "")

        cells = row.find_all("td")
        size = ""
        if len(cells) >= 3:
            match = _main.re.search(r"([\d.]+\s*[KMGT]i?B)", cells[2].get_text(" ", strip=True), _main.re.I)
            if match:
                size = match.group(1)
        if not size:
            match = _main.re.search(r"([\d.]+\s*[KMGT]i?B)", row.get_text(" ", strip=True), _main.re.I)
            if match:
                size = match.group(1)

        source_url = _main.urljoin(base_url.rstrip("/") + "/", detail_href.lstrip("/"))
        key = source_url.lower()
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "title": title,
            "detail_url": source_url,
            "size": size,
            "seeders": seeders,
            "leechers": leechers,
            "info_hash": torrent_hash,
            "magnet_url": magnet_url,
        })

    return rows

async def search_limetorrents(
    query: str,
    limit: int = 50,
    pages: int = 2,
) -> list[dict[str, _main.Any]]:
    if not _main._MANUAL_TORRENT_SEARCH.get():
        _main._record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Search LimeTorrents concurrently across mirrors; resolve magnets on demand."""
    _title_query, season, episode = _main._media_search_parts(query)
    provider_query = _main._media_provider_query(query)

    # Keep language terms in the provider query so "thor hindi" searches the
    # Hindi result pool directly instead of searching only for "thor".
    _, languages = _main._search_query_constraints(query)
    language_terms = [
        language
        for language in languages
        if language in {
            "hindi", "tamil", "telugu", "malayalam", "kannada",
            "bengali", "marathi", "punjabi", "gujarati", "urdu",
        }
    ]
    provider_query = _main.re.sub(
        r"\s+",
        " ",
        f"{provider_query} {' '.join(language_terms)}".strip(),
    )
    if not provider_query:
        return []

    category = "tv" if season is not None or episode is not None else "movies"
    encoded = _main.quote(provider_query, safe="")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with _main._InstrumentedAsyncClient(
        timeout=_main.SEARCH_SOURCE_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:
        async def fetch_host(host: str) -> list[tuple[str, str]]:
            base = f"https://{host}"
            urls = [f"{base}/search/all/{encoded}/seeds/1/"]
            if pages > 1:
                urls.append(f"{base}/search/all/{encoded}/seeds/2/")

            async def fetch_page(url: str) -> str:
                try:
                    async with _main._LIMETORRENTS_REQUEST_SEMAPHORE:
                        response = await client.get(url)
                    if response.status_code < 400 and response.text:
                        return response.text
                except _main.httpx.HTTPError:
                    pass
                return ""

            bodies = await _main.asyncio.gather(
                *(fetch_page(url) for url in urls),
                return_exceptions=False,
            )
            return [(base, body) for body in bodies if body]

        # Probe all mirrors together. Previously a dead first mirror could
        # consume the timeout sequentially before a healthy mirror was tried.
        mirror_pages = await _main.asyncio.gather(
            *(fetch_host(host) for host in _main.LIMETORRENTS_HOSTS),
            return_exceptions=True,
        )
        pages_html: list[tuple[str, str]] = []
        for value in mirror_pages:
            if isinstance(value, list):
                pages_html.extend(value)

        raw_rows: list[dict[str, str]] = []
        for base, html_text in pages_html:
            raw_rows.extend(_main._limetorrents_rows(html_text, base))

        if not raw_rows:
            _main.logger.warning(
                "LimeTorrents unavailable for '%s' (hosts=%s)",
                query,
                ",".join(_main.LIMETORRENTS_HOSTS),
            )
            return []

        units = {
            "KB": 1024, "KIB": 1024,
            "MB": 1024**2, "MIB": 1024**2,
            "GB": 1024**3, "GIB": 1024**3,
            "TB": 1024**4, "TIB": 1024**4,
        }

        # Merge repeated rows from mirror domains, retaining the highest
        # reported seed/peer counts for each title and size.
        unique_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in raw_rows:
            size_match = _main.re.match(r"([\d.]+)\s*([KMGT]i?B)", row.get("size", ""), _main.re.I)
            if not size_match:
                continue
            size = int(float(size_match.group(1)) * units[size_match.group(2).upper()])
            key = (_main._normalize_title(str(row.get("title") or "")), str(size))
            candidate = {**row, "size_bytes": size}
            previous = unique_rows.get(key)
            if previous is None or (
                int(candidate.get("seeders") or 0), int(candidate.get("leechers") or 0)
            ) > (
                int(previous.get("seeders") or 0), int(previous.get("leechers") or 0)
            ):
                unique_rows[key] = candidate

        parsed_rows = list(unique_rows.values())
        parsed_rows.sort(
            key=lambda row: (
                int(row.get("seeders") or 0),
                int(row.get("leechers") or 0),
            ),
            reverse=True,
        )

        results: list[dict[str, Any]] = []
        for row in parsed_rows[:min(max(limit, 1), 30)]:
            detail_url = str(row.get("detail_url") or "").strip()
            if not detail_url:
                continue
            results.append({
                "guid": "limetorrents-" + _main.hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:20],
                "title": str(row.get("title") or ""),
                "size": int(row.get("size_bytes") or 0),
                "seeders": int(row.get("seeders") or 0),
                "leechers": int(row.get("leechers") or 0),
                "indexer": "LimeTorrents",
                "protocol": "torrent",
                "publishDate": "",
                "magnetUrl": str(row.get("magnet_url") or "").strip() or None,
                "infoHash": str(row.get("info_hash") or "").strip(),
                "downloadUrl": str(row.get("magnet_url") or "").strip() or None,
                "infoUrl": detail_url,
                "sourceUrl": detail_url,
                "descriptorUrl": "",
                "category": "TV" if category == "tv" else "Movies",
            })

    _main.logger.warning(
        "LimeTorrents listing '%s': %d results; top seeders=%s",
        query,
        len(results),
        ",".join(str(item.get("seeders") or 0) for item in results[:5]),
    )
    return results[:limit]

async def search_knaben(
    query: str,
    limit: int = 100,
    provider_query: str | None = None,
) -> list[dict[str, _main.Any]]:
    if not _main._MANUAL_TORRENT_SEARCH.get():
        _main._record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Search Knaben API v2 using its edge-cached GET interface."""
    title_query, season, episode = _main._media_search_parts(query)
    title_query = _main.re.sub(r"\s+", " ", provider_query or _main._media_provider_query(query)).strip()
    if not title_query:
        return []

    target_tokens = _main._search_tokens(title_query)
    # Knaben API v2 moved search to GET /v2/search and renamed the parameters.
    # Accept an older /v1 environment override but transparently upgrade it.
    endpoint = _main.KNABEN_API_URL.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3] + "/v2/search"
    elif endpoint.endswith("/v2"):
        endpoint += "/search"
    elif endpoint in {"https://api.knaben.org", "http://api.knaben.org"}:
        endpoint += "/v2/search"

    params = {
        "q": title_query,
        "sf": "title",
        "o": "seeders",
        "d": "desc",
        "s": min(max(int(limit or 100), 1), 150),
        "f": 0,
        "seen": 604800,
    }

    try:
        # v2 is edge-cached, but give it a little more than the old 2.25s v1
        # budget: the latter was consistently timing out in Home's live logs.
        timeout = _main.httpx.Timeout(max(7.0, _main.SEARCH_SOURCE_TIMEOUT_SECONDS), connect=6.0)
        async with _main._InstrumentedAsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with _main._KNABEN_REQUEST_SEMAPHORE:
                response = await client.get(
                    endpoint,
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "HomeMovieSearch/1.0"},
                )
            response.raise_for_status()
            payload = response.json()
            raw_hits = payload if isinstance(payload, list) else (
                payload.get("hits", []) if isinstance(payload, dict) else []
            )
            _main.logger.info(
                "Knaben v2 HTTP %s for '%s': %d hits",
                response.status_code,
                query,
                len(raw_hits) if isinstance(raw_hits, list) else 0,
            )
    except (_main.httpx.HTTPError, ValueError) as exc:
        # Include the exception type because timeout exceptions often have an
        # empty string, which made the previous production logs unhelpful.
        _main.logger.warning(
            "Knaben v2 search failed for '%s': %s: %r",
            query,
            type(exc).__name__,
            str(exc),
        )
        return []

    if not isinstance(raw_hits, list):
        return []

    results: list[dict[str, Any]] = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue

        title = str(hit.get("title") or "").strip()
        category = str(hit.get("category") or "").strip()
        if not title:
            continue

        normalized_title = _main._normalize_title(title)
        if target_tokens and not all(token in normalized_title for token in target_tokens):
            continue
        if category and any(
            blocked in category.lower()
            for blocked in ("anime", "games", "music", "software", "books", "porn", "xxx", "adult")
        ):
            continue
        if category and not any(
            allowed in category.lower()
            for allowed in ("video", "movie", "tv", "television", "series")
        ):
            continue
        if season is not None and not _main._season_episode_match(title, season, episode):
            continue

        magnet = str(hit.get("magnetUrl") or hit.get("magnet_url") or "").strip()
        h = str(hit.get("hash") or hit.get("infoHash") or hit.get("info_hash") or "").strip().lower()
        if not _main.re.fullmatch(r"[0-9a-f]{40}", h):
            h = _main.info_hash(magnet)
        if not magnet and _main.re.fullmatch(r"[0-9a-f]{40}", h):
            magnet = f"magnet:?xt=urn:btih:{h}&dn={_main.quote(title, safe='')}"

        try:
            size = int(hit.get("bytes") or hit.get("size_bytes") or hit.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            seeders = int(hit.get("seeders") or hit.get("seeds") or 0)
        except (TypeError, ValueError):
            seeders = 0
        try:
            leechers = int(hit.get("peers") or hit.get("leechers") or 0)
        except (TypeError, ValueError):
            leechers = 0

        detail_url = str(hit.get("details") or hit.get("infoUrl") or "").strip()
        descriptor_url = str(hit.get("link") or hit.get("descriptorUrl") or "").strip()
        tracker = str(hit.get("cachedOrigin") or hit.get("tracker") or hit.get("trackerId") or "Knaben").strip()
        published = str(hit.get("date") or hit.get("publishDate") or "").strip()

        results.append({
            "guid": f"knaben-{h or hit.get('id') or title}",
            "title": title,
            "size": size,
            "seeders": seeders,
            "leechers": leechers,
            "indexer": tracker,
            "protocol": "torrent",
            "publishDate": published,
            "magnetUrl": magnet or None,
            "infoHash": h if _main.re.fullmatch(r"[0-9a-f]{40}", h, _main.re.I) else "",
            "downloadUrl": magnet or None,
            "infoUrl": detail_url,
            "sourceUrl": detail_url or descriptor_url,
            "descriptorUrl": descriptor_url,
            "category": category,
        })

    results.sort(
        key=lambda row: (
            1 if _main._normalize_title(str(row["title"])).startswith(_main._normalize_title(title_query)) else 0,
            int(row.get("seeders") or 0),
        ),
        reverse=True,
    )
    _main.logger.info("Knaben v2 search '%s': %d relevant results after title/category filtering", query, len(results))
    return results[:limit]

async def search_torrents_csv(query: str, limit: int) -> dict[str, _main.Any]:
    if not _main._MANUAL_TORRENT_SEARCH.get():
        _main._record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return {"source": "torrents-csv", "elapsedMs": 0, "results": []}
    started = _main.time.monotonic()
    try:
        async with _main._InstrumentedAsyncClient(timeout=_main.SEARCH_SOURCE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(
                "https://torrents-csv.com/service/search",
                params={
                    "q": _main._media_provider_query(query),
                    "size": min(limit, 50),
                    "type": "torrent",
                },
                headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
            )
            response.raise_for_status()
            payload = response.json()

        rows = payload if isinstance(payload, list) else (
            payload.get("torrents", []) if isinstance(payload, dict) else []
        )
        results: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            h = str(item.get("infohash") or "").strip().lower()
            if not _main.re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            title = str(item.get("name") or "Untitled").strip()
            if not title:
                continue
            results.append({
                "guid": f"torrents-csv-{h}",
                "title": title,
                "size": int(float(item.get("size_bytes") or 0)),
                "seeders": int(item.get("seeders") or 0),
                "leechers": int(item.get("leechers") or 0),
                "indexer": "torrents-csv",
                "protocol": "torrent",
                "publishDate": (
                    _main.datetime.fromtimestamp(
                        int(item.get("created_unix") or 0), tz=_main.timezone.utc
                    ).isoformat()
                    if item.get("created_unix") else ""
                ),
                "infoHash": h,
                "magnetUrl": f"magnet:?xt=urn:btih:{h}&dn={_main.quote(title, safe='')}",
                "downloadUrl": f"magnet:?xt=urn:btih:{h}&dn={_main.quote(title, safe='')}",
                "infoUrl": "",
                "sourceUrl": "",
                "descriptorUrl": "",
            })
        return {"source": "torrents-csv", "elapsedMs": round((_main.time.monotonic() - started) * 1000), "results": results}
    except (_main.httpx.HTTPError, ValueError, TypeError) as exc:
        _main.logger.info("Torrents-CSV search failed for '%s': %s", query, exc)
        return {"source": "torrents-csv", "elapsedMs": round((_main.time.monotonic() - started) * 1000), "results": [], "error": str(exc)}

async def search_apibay(query: str, limit: int) -> dict[str, _main.Any]:
    if not _main._MANUAL_TORRENT_SEARCH.get():
        _main._record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return {"source": "apibay", "elapsedMs": 0, "results": []}
    started = _main.time.monotonic()
    try:
        async with _main._InstrumentedAsyncClient(timeout=_main.SEARCH_SOURCE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(
                "https://apibay.org/q.php",
                params={"q": _main._media_provider_query(query), "cat": "0"},
                headers={"Accept": "application/json", "User-Agent": "TorrentStudio/1.0"},
            )
            response.raise_for_status()
            payload = response.json()

        rows = payload if isinstance(payload, list) else []
        results: list[dict[str, Any]] = []
        for item in rows[:limit]:
            if not isinstance(item, dict):
                continue
            h = str(item.get("info_hash") or "").strip().lower()
            if not _main.re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            title = str(item.get("name") or "Untitled").strip()
            if not title:
                continue
            results.append({
                "guid": f"apibay-{h}",
                "title": title,
                "size": int(float(item.get("size") or 0)),
                "seeders": int(item.get("seeders") or 0),
                "leechers": int(item.get("leechers") or 0),
                "indexer": "apibay",
                "protocol": "torrent",
                "publishDate": "",
                "infoHash": h,
                "magnetUrl": f"magnet:?xt=urn:btih:{h}&dn={_main.quote(title, safe='')}",
                "downloadUrl": f"magnet:?xt=urn:btih:{h}&dn={_main.quote(title, safe='')}",
                "infoUrl": "",
                "sourceUrl": "",
                "descriptorUrl": "",
            })
        return {"source": "apibay", "elapsedMs": round((_main.time.monotonic() - started) * 1000), "results": results}
    except (_main.httpx.HTTPError, ValueError, TypeError) as exc:
        _main.logger.info("APIBay search failed for '%s': %s", query, exc)
        return {"source": "apibay", "elapsedMs": round((_main.time.monotonic() - started) * 1000), "results": [], "error": str(exc)}

def _search_cache_key(query: str) -> str:
    return _main.re.sub(r"\s+", " ", query.strip()).lower()

def _trim_search_cache() -> None:
    while len(_main._search_cache) > max(10, _main.SEARCH_CACHE_MAX_ENTRIES):
        oldest_key = min(
            _main._search_cache.items(),
            key=lambda pair: pair[1][0],
        )[0]
        _main._search_cache.pop(oldest_key, None)

async def _search_1337x_uncached(
    query: str,
    allow_series_fallback: bool = True,
) -> list[dict[str, _main.Any]]:
    if not _main._MANUAL_TORRENT_SEARCH.get():
        _main._record_cache_event("torrent_search", "blocked_prefilled_live_lookup")
        return []
    """Perform one bounded live media search.

    This is intentionally conservative for Render Free: only the primary
    1337x/Knaben pair runs on the first pass. More expensive alternate queries
    and specialist providers are activated only when fewer than 8 usable
    results survive the hard media and configured size gates.
    """
    limit = 50
    kind = _main._search_media_kind(query)
    variants = _main._media_provider_queries(query)
    primary_provider_query = variants[0] if variants else _main._media_provider_query(query)

    async def run_1337x(provider_query: str):
        try:
            return await _main.asyncio.wait_for(
                _main.search_1337x_direct(
                    query,
                    limit=20,
                    pages=2,
                    category="TV" if kind == "tv" else None,
                    provider_query=provider_query,
                ),
                timeout=max(8.0, _main.SEARCH_TOTAL_TIMEOUT_SECONDS + 4.0),
            )
        except Exception as exc:
            _main.logger.warning("1337x search failed for '%s' using '%s': %s: %r", query, provider_query, type(exc).__name__, str(exc))
            return []

    async def run_lime(provider_query: str):
        try:
            lime_results = await _main.asyncio.wait_for(
                _main.search_limetorrents(
                    query,
                    limit=20,
                    pages=1,
                ),
                timeout=10.0,
            )
            _main.logger.warning(
                "LimeTorrents primary source '%s': %d results",
                query,
                len(lime_results),
            )
            return lime_results
        except Exception as exc:
            _main.logger.warning(
                "LimeTorrents search failed for '%s' using '%s': %s: %s",
                query,
                provider_query,
                type(exc).__name__,
                str(exc) or "<no message>",
            )
            return []

    async def run_knaben(provider_query: str):
        try:
            return await _main.asyncio.wait_for(
                _main.search_knaben(query, 50, provider_query=provider_query),
                timeout=8.0,
            )
        except Exception as exc:
            _main.logger.info("Knaben search failed for '%s' using '%s': %s", query, provider_query, exc)
            return []

    yts_task = (
        _main.asyncio.create_task(
            _main.asyncio.wait_for(
                _main.search_yts_movies(query, min(limit, 20)),
                timeout=_main.SEARCH_SOURCE_TIMEOUT_SECONDS + 0.75,
            )
        )
        if kind == "both"
        else None
    )

    primary_1337x, primary_lime, primary_knaben = await _main.asyncio.gather(
        run_1337x(primary_provider_query),
        run_lime(primary_provider_query),
        run_knaben(primary_provider_query),
        return_exceptions=False,
    )

    yts_primary: list[dict[str, Any]] = []
    if yts_task is not None:
        try:
            yts_primary = await yts_task
        except Exception as exc:
            _main.logger.warning("YTS primary search failed for '%s': %s: %r", query, type(exc).__name__, str(exc))

    _main.logger.warning(
        "Raw search source counts for '%s': 1337x=%d, LimeTorrents=%d, Knaben=%d, YTS=%d",
        query,
        len(primary_1337x),
        len(primary_lime),
        len(primary_knaben),
        len(yts_primary),
    )

    results: list[dict[str, Any]] = []

    def add_filtered(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if _main._search_quality_filter(item, query):
                results.append(item)

    add_filtered(primary_1337x)
    add_filtered(primary_lime)
    add_filtered(primary_knaben)
    add_filtered(yts_primary)

    yts_metadata: dict[str, dict[str, Any]] = {}
    for item in yts_primary:
        media_title = _main._normalize_title(str(item.get("mediaTitle") or ""))
        if media_title:
            yts_metadata.setdefault(media_title, item)

    def enrich_movie_metadata(items: list[dict[str, Any]]) -> None:
        for item in items:
            provider_title = str(item.get("mediaTitle") or item.get("title") or "")
            media_title = _main._normalize_title(_main._media_provider_query(provider_title))
            if not media_title:
                continue
            metadata = yts_metadata.get(media_title)
            if not metadata:
                compact = media_title.replace(" ", "")
                metadata = next(
                    (
                        candidate for key, candidate in yts_metadata.items()
                        if compact and key.replace(" ", "") == compact
                    ),
                    None,
                )
            if metadata:
                for field in ("mediaTitle", "year", "rating", "genres", "posterUrl"):
                    value = metadata.get(field)
                    if value not in (None, "", []):
                        item[field] = value

    enrich_movie_metadata(results)

    # When the primary title spelling cannot get enough usable hits, retry
    # with the alternate spelling and a short anchor in parallel. This is the
    # cheap way to handle Spider-Man/Spiderman-style provider differences
    # without always doubling Render traffic.
    if allow_series_fallback and len(results) < min(8, limit):
        secondary_queries = variants[1:3]

        variant_tasks = [
            _main.asyncio.create_task(run_1337x(provider_query))
            for provider_query in secondary_queries
        ]
        variant_tasks.extend(
            _main.asyncio.create_task(run_knaben(provider_query))
            for provider_query in secondary_queries
        )

        fallback_tasks: list[_main.asyncio.Task] = [
            _main.asyncio.create_task(
                _main.search_torrents_csv(
                    query,
                    min(limit, 50),
                )
            ),
            _main.asyncio.create_task(
                _main.search_apibay(
                    query,
                    min(limit, 50),
                )
            ),
        ]

        if kind in {"tv", "both"}:
            fallback_tasks.append(
                _main.asyncio.create_task(
                    _main.asyncio.wait_for(
                        _main.search_tv_eztv(query, min(limit, 20)),
                        timeout=_main.SEARCH_SOURCE_TIMEOUT_SECONDS + 0.5,
                    )
                )
            )
        if kind == "both":
            fallback_tasks.append(
                _main.asyncio.create_task(
                    _main.asyncio.wait_for(
                        _main.search_yts_movies(query, min(limit, 20)),
                        timeout=_main.SEARCH_SOURCE_TIMEOUT_SECONDS + 0.5,
                    )
                )
            )

        values = await _main.asyncio.gather(
            *(variant_tasks + fallback_tasks),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, list):
                add_filtered(value)
            elif isinstance(value, dict):
                add_filtered(value.get("results", []))

        enrich_movie_metadata(results)

    merged: dict[str, dict[str, Any]] = {}
    for item in results:
        digest = str(item.get("infoHash") or "").strip().lower()
        if not _main.re.fullmatch(r"[0-9a-f]{40}", digest):
            digest = _main.info_hash(str(item.get("magnetUrl") or ""))

        if _main.re.fullmatch(r"[0-9a-f]{40}", digest, _main.re.I):
            key = "hash:" + digest.lower()
        else:
            # Listing-page results now resolve magnets lazily, so use a
            # normalized title + size to collapse the same release returned
            # by more than one provider.
            normalized_title = _main._normalize_title(str(item.get("title") or ""))
            if not normalized_title:
                continue
            size = int(item.get("size") or 0)
            key = f"title-size:{normalized_title}|{size}"

        previous = merged.get(key)
        if previous is None or (
            int(item.get("seeders") or 0),
            int(item.get("leechers") or 0),
            1 if item.get("magnetUrl") else 0,
        ) > (
            int(previous.get("seeders") or 0),
            int(previous.get("leechers") or 0),
            1 if previous.get("magnetUrl") else 0,
        ):
            merged[key] = item

    results = list(merged.values())
    enrich_movie_metadata(results)

    source_counts: dict[str, int] = {}
    for item in results:
        source_name = str(item.get("indexer") or "unknown").strip() or "unknown"
        source_counts[source_name] = source_counts.get(source_name, 0) + 1
    _main.logger.warning(
        "Search result sources for '%s': %s",
        query,
        ", ".join(f"{name}={count}" for name, count in sorted(source_counts.items())),
    )

    # Poster artwork is resolved lazily by /api/poster so search latency is
    # unaffected. Every normal movie result gets a cleaned poster URL,
    # regardless of whether it came from YTS, Knaben, 1337x, or another source.
    if kind == "both":
        for item in results:
            if not str(item.get("posterUrl") or "").strip():
                poster_url = _main._poster_url_for_release(str(item.get("title") or ""))
                if poster_url:
                    item["posterUrl"] = poster_url

    # Do not let questionable seed counts from one indexer dominate the list.
    # Rank direct title matches first, then recognizable release quality, and
    # use swarm size/date as secondary signals within comparable results.
    results.sort(
        key=lambda item: (
            _main._title_relevance(str(item.get("title") or ""), query)[0],
            _main._release_quality_score(str(item.get("title") or "")),
            min(int(item.get("seeders") or 0), 5000),
            int(item.get("leechers") or 0),
            str(item.get("publishDate") or ""),
        ),
        reverse=True,
    )

    return results[:limit]

async def search_1337x(
    query: str,
    limit: int = 50,
    allow_series_fallback: bool = True,
) -> list[dict[str, _main.Any]]:
    """Fast media-only search with stale cache and in-flight request sharing."""
    query = _main.re.sub(r"\s+", " ", query.strip())
    if not query:
        return []

    limit = min(max(int(limit or 50), 1), 50)
    cache_key = _main._search_cache_key(query)
    now = _main.time.monotonic()

    cached = _main._search_cache.get(cache_key)
    if cached and len(cached[1]) < _main.SEARCH_CACHE_MIN_RESULTS:
        _main._search_cache.pop(cache_key, None)
        cached = None

    if cached:
        cached_at, cached_results = cached
        age = max(0.0, now - cached_at)
        ttl = _main.SEARCH_CACHE_SECONDS

        if age < ttl:
            _main._record_cache_event("torrent_search", "hit")
            return cached_results[:limit]

        if age < _main.SEARCH_CACHE_STALE_SECONDS:
            _main._record_cache_event("torrent_search", "stale_hit")
            # Serve stale results immediately and refresh only once per query.
            existing = _main._search_inflight.get(cache_key)
            if existing is None or existing.done():
                refresh = _main.asyncio.create_task(
                    _main._search_1337x_uncached(
                        query,
                        allow_series_fallback=allow_series_fallback,
                    )
                )
                _main._search_inflight[cache_key] = refresh

                def _finish_refresh(task: _main.asyncio.Task, key: str = cache_key):
                    _main._search_inflight.pop(key, None)
                    if task.cancelled():
                        return
                    try:
                        refreshed = task.result()
                    except Exception as exc:
                        _main.logger.info("Background search refresh failed for '%s': %s", query, exc)
                        return
                    if len(refreshed) >= _main.SEARCH_CACHE_MIN_RESULTS:
                        _main._search_cache[key] = (_main.time.monotonic(), refreshed)
                        _main._trim_search_cache()

                refresh.add_done_callback(_finish_refresh)
            return cached_results[:limit]

    # Share the same live request among concurrent users. This is particularly
    # useful on Render Free where multiple browser tabs/devices can otherwise
    # fan out identical provider requests.
    _main._record_cache_event("torrent_search", "miss")
    task = _main._search_inflight.get(cache_key)
    if task is None or task.done():
        task = _main.asyncio.create_task(
            _main._search_1337x_uncached(
                query,
                allow_series_fallback=allow_series_fallback,
            )
        )
        _main._search_inflight[cache_key] = task

    try:
        results = await task
    except _main.asyncio.CancelledError:
        # The provider task may still be useful to other callers; do not leave
        # a dead task in the inflight map.
        if _main._search_inflight.get(cache_key) is task:
            _main._search_inflight.pop(cache_key, None)
        raise
    except Exception as exc:
        if _main._search_inflight.get(cache_key) is task:
            _main._search_inflight.pop(cache_key, None)
        _main.logger.warning("Media search failed for '%s': %s", query, exc)
        return cached[1][:limit] if cached else []

    if _main._search_inflight.get(cache_key) is task:
        _main._search_inflight.pop(cache_key, None)

    if len(results) >= _main.SEARCH_CACHE_MIN_RESULTS:
        _main._search_cache[cache_key] = (_main.time.monotonic(), results)
        _main._trim_search_cache()
    return results[:limit]

def parse_size(value: str) -> int:
    m = _main.re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)", value or "", _main.re.I)
    if not m:
        return 0
    n = float(m.group(1))
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(n * units[m.group(2).upper()])
