"""Application functions for this feature area."""

import main as _main
from typing import Any

__all__ = ['decode_torrent_metadata', '_save_metadata_cache', '_fetch_itorrents_metadata', '_fetch_source_torrent_descriptor', '_metadata_magnet_with_trackers', '_fetch_fast_test_metadata', '_metadata_from_torrent_bytes', '_get_libtorrent_session', '_metadata_from_libtorrent_sync', '_schedule_metadata_job_expiry', '_run_metadata_job']

def decode_torrent_metadata(raw: bytes) -> dict[bytes, _main.Any]:
    """Decode the small bencoded .torrent metadata file produced by aria2."""
    position = 0

    def parse() -> Any:
        nonlocal position
        if position >= len(raw):
            raise ValueError("unexpected end of bencode")

        marker = raw[position:position + 1]
        if marker == b"i":
            position += 1
            end = raw.find(b"e", position)
            if end < 0:
                raise ValueError("unterminated integer")
            value = int(raw[position:end])
            position = end + 1
            return value

        if marker == b"l":
            position += 1
            value = []
            while position < len(raw) and raw[position:position + 1] != b"e":
                value.append(parse())
            if position >= len(raw):
                raise ValueError("unterminated list")
            position += 1
            return value

        if marker == b"d":
            position += 1
            value: dict[bytes, Any] = {}
            while position < len(raw) and raw[position:position + 1] != b"e":
                key = parse()
                if not isinstance(key, bytes):
                    raise ValueError("dictionary key is not bytes")
                value[key] = parse()
            if position >= len(raw):
                raise ValueError("unterminated dictionary")
            position += 1
            return value

        if marker.isdigit():
            colon = raw.find(b":", position)
            if colon < 0:
                raise ValueError("invalid byte string")
            size = int(raw[position:colon])
            position = colon + 1
            end = position + size
            if end > len(raw):
                raise ValueError("byte string exceeds metadata")
            value = raw[position:end]
            position = end
            return value

        raise ValueError("invalid bencode token")

    value = parse()
    if position != len(raw):
        raise ValueError("trailing bencode data")
    if not isinstance(value, dict):
        raise ValueError("torrent metadata root is not a dictionary")
    return value

def _save_metadata_cache() -> None:
    try:
        _main.TORRENT_METADATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _main.TORRENT_METADATA_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(_main.json.dumps(_main._metadata_cache, ensure_ascii=False), "utf-8")
        tmp.replace(_main.TORRENT_METADATA_CACHE_FILE)
    except Exception as exc:
        _main.logger.info("Metadata cache save skipped: %s", exc)

async def _fetch_itorrents_metadata(info_hash_value: str) -> dict[str, _main.Any] | None:
    """Try the public iTorrents descriptor cache before network metadata discovery."""
    target = info_hash_value.strip().lower()
    if not _main.TORRENT_METADATA_ITORRENTS_BASE_URL or not _main.re.fullmatch(r"[0-9a-f]{40}", target):
        return None

    url = f"{_main.TORRENT_METADATA_ITORRENTS_BASE_URL}/{target}.torrent"
    try:
        async with _main._InstrumentedAsyncClient(
            timeout=_main.TORRENT_METADATA_ITORRENTS_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Torrent-Studio/1.0"},
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            body = response.content
    except _main.httpx.HTTPError as exc:
        _main.logger.info("iTorrents cache lookup failed for %s: %s", target, exc)
        return None

    if len(body) > 8 * 1024 * 1024 or not body.startswith(b"d") or b"4:info" not in body:
        return None

    try:
        result = _main._metadata_from_torrent_bytes(body, target, "itorrents_cache")
    except (ValueError, TypeError) as exc:
        _main.logger.info("iTorrents returned invalid metadata for %s: %s", target, exc)
        return None
    _main.logger.info("Metadata cache hit for %s via iTorrents", target)
    return result

async def _fetch_source_torrent_descriptor(source_url: str, info_hash_value: str) -> dict[str, _main.Any] | None:
    """Try to obtain a real .torrent descriptor from a trusted search detail URL."""
    if not source_url:
        return None

    try:
        parsed = _main.urlsplit(source_url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return None

    # Search results currently expose Knaben detail URLs. Keep this narrowly
    # scoped rather than turning the endpoint into an arbitrary URL fetcher.
    allowed_hosts = {
        "knaben.org", "www.knaben.org", "api.knaben.org",
        "knaben.eu", "www.knaben.eu",
        "knaben.xyz", "www.knaben.xyz",
    }
    if host not in allowed_hosts:
        return None

    candidates = [source_url]
    try:
        async with _main._InstrumentedAsyncClient(
            timeout=8.0,
            follow_redirects=True,
            headers={"User-Agent": "Torrent-Studio/1.0"},
        ) as client:
            response = await client.get(source_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()

            if "bittorrent" in content_type or source_url.lower().split("?", 1)[0].endswith(".torrent"):
                candidates = [str(response.url)]
                raw = response.content
            else:
                html = response.text
                soup = _main.BeautifulSoup(html, "html.parser")
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "").strip()
                    absolute = _main.urljoin(str(response.url), href)
                    if absolute.lower().split("?", 1)[0].endswith(".torrent"):
                        candidates.append(absolute)

                # Some indexers expose a download attribute without a .torrent
                # suffix in the visible URL.
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "").strip()
                    absolute = _main.urljoin(str(response.url), href)
                    label = " ".join(anchor.stripped_strings).lower()
                    if "torrent" in label and absolute not in candidates:
                        candidates.append(absolute)

                raw = None

            for candidate in candidates[:8]:
                try:
                    if raw is None or candidate != str(response.url):
                        descriptor = await client.get(candidate)
                        descriptor.raise_for_status()
                        candidate_type = descriptor.headers.get("content-type", "").lower()
                        body = descriptor.content
                    else:
                        candidate_type = content_type
                        body = raw

                    if len(body) > 8 * 1024 * 1024:
                        continue
                    if body.startswith(b"d") and b"4:info" in body:
                        return _main._metadata_from_torrent_bytes(
                            body,
                            info_hash_value,
                            "search_torrent_descriptor",
                        )
                except Exception:
                    continue
    except (_main.httpx.HTTPError, ValueError) as exc:
        _main.logger.info("Direct torrent descriptor lookup failed: %s", exc)

    return None

def _metadata_magnet_with_trackers(magnet: str) -> str:
    """Add the known-good HTTP trackers used by the fast WebTorrent test."""
    try:
        parsed = _main.urlsplit(magnet)
        params = _main.parse_qs(parsed.query, keep_blank_values=True)
        rebuilt: list[tuple[str, str]] = []
        for key in ("xt", "dn"):
            for value in params.get(key, []):
                if value:
                    rebuilt.append((key, str(value)))
        existing = {str(value).strip() for value in params.get("tr", []) if value}
        for tracker in _main.FAST_SEARCH_TRACKERS:
            if tracker not in existing:
                rebuilt.append(("tr", tracker))
        return "magnet:?" + _main.urlencode(rebuilt, doseq=True)
    except Exception:
        return magnet

async def _fetch_fast_test_metadata(magnet: str, info_hash_value: str) -> dict[str, _main.Any] | None:
    """Use the proven WebTorrent resolver service as a metadata-only race participant."""
    if not _main.FAST_SEARCH_TEST_URL:
        return None

    try:
        async with _main._InstrumentedAsyncClient(
            timeout=4.0,
            follow_redirects=True,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as client:
            response = await client.post(
                _main.FAST_SEARCH_TEST_URL + "/api/metadata",
                json={"magnet": magnet},
            )
            response.raise_for_status()
            payload = response.json()
    except (_main.httpx.HTTPError, ValueError, TypeError) as exc:
        _main.logger.info("Fast WebTorrent metadata service failed for %s: %s", info_hash_value, exc)
        return None

    if not isinstance(payload, dict):
        return None

    if str(payload.get("status") or "").lower() == "completed" and isinstance(payload.get("files"), list):
        metadata = payload
    else:
        job_id = str(payload.get("jobId") or "").strip()
        if not job_id:
            return None

        deadline = _main.time.monotonic() + min(
            12.0,
            max(0.0, _main.TORRENT_METADATA_JOB_TIMEOUT_SECONDS),
        )
        metadata = None
        while _main.time.monotonic() < deadline:
            await _main.asyncio.sleep(0.75)
            remaining = max(0.5, deadline - _main.time.monotonic())
            try:
                async with _main._InstrumentedAsyncClient(
                    timeout=min(3.0, remaining),
                    follow_redirects=True,
                    headers={"Accept": "application/json"},
                ) as poll_client:
                    poll_response = await poll_client.get(
                        _main.FAST_SEARCH_TEST_URL + "/api/metadata-jobs/" + _main.quote(job_id, safe=""),
                    )
                    if poll_response.status_code == 404:
                        return None
                    poll_response.raise_for_status()
                    poll = poll_response.json()
            except (_main.httpx.HTTPError, ValueError, TypeError):
                continue

            job = poll.get("job") if isinstance(poll, dict) else None
            if not isinstance(job, dict):
                continue
            if str(job.get("status") or "").lower() == "completed":
                candidate = job.get("metadata")
                if isinstance(candidate, dict) and isinstance(candidate.get("files"), list):
                    metadata = candidate
                    break
            if str(job.get("status") or "").lower() == "failed":
                return None

        if metadata is None:
            return None

    returned_hash = str(metadata.get("infoHash") or metadata.get("hash") or "").strip().lower()
    if returned_hash and returned_hash != info_hash_value.lower():
        _main.logger.info(
            "Fast WebTorrent metadata hash mismatch: wanted %s, got %s",
            info_hash_value,
            returned_hash,
        )
        return None

    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return None

    files: list[dict[str, Any]] = []
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        if not path:
            continue
        files.append({
            "index": int(item.get("index") if item.get("index") is not None else index),
            "name": str(item.get("name") or path),
            "size": int(float(item.get("size") or 0)),
            "path": path,
            "type": "file",
            "priority": 1,
        })
    if not files:
        return None

    result = {
        "name": str(metadata.get("name") or files[0]["name"]),
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item["size"]) for item in files),
        "source": "webtorrent_fast_test",
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    return result

def _metadata_from_torrent_bytes(raw: bytes, info_hash_value: str, source: str) -> dict[str, _main.Any]:
    meta = _main.decode_torrent_metadata(raw)
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent metadata has no info dictionary")

    def btext(value: Any) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")

    name = btext(info.get(b"name")) or f"Torrent {info_hash_value[:8]}"
    files: list[dict[str, Any]] = []
    multi = info.get(b"files")
    if isinstance(multi, list):
        for index, item in enumerate(multi):
            if not isinstance(item, dict):
                continue
            parts = item.get(b"path") or []
            relative = "/".join(btext(part) for part in parts) if isinstance(parts, list) else btext(parts)
            files.append({
                "index": index,
                "name": relative or name,
                "size": int(item.get(b"length") or 0),
                "path": relative or name,
                "type": "file",
                "priority": 1,
            })
    else:
        files.append({
            "index": 0,
            "name": name,
            "size": int(info.get(b"length") or 0),
            "path": name,
            "type": "file",
            "priority": 1,
        })

    result = {
        "name": name,
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item.get("size") or 0) for item in files),
        "source": source,
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    _main._metadata_cache[info_hash_value.lower()] = result
    _main._save_metadata_cache()
    return result

async def _get_libtorrent_session() -> _main.Any:
    global _libtorrent_session, _libtorrent_session_lock
    if _main._libtorrent_session is not None:
        return _main._libtorrent_session
    if _main._libtorrent_session_lock is None:
        _main._libtorrent_session_lock = _main.asyncio.Lock()
    async with _main._libtorrent_session_lock:
        if _main._libtorrent_session is not None:
            return _main._libtorrent_session
        session = _main.lt.session()
        session.apply_settings({
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_tcp": True,
            "enable_outgoing_utp": True,
            "enable_incoming_tcp": True,
            "enable_incoming_utp": True,
            "listen_interfaces": "0.0.0.0:0",
            "dht_bootstrap_nodes": "",
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connection_speed": 50,
            "handshake_timeout": 10,
        })
        _main._libtorrent_session = session
        _main.logger.info("Started persistent libtorrent metadata session")
        return session

def _metadata_from_libtorrent_sync(magnet: str, info_hash_value: str) -> dict[str, _main.Any]:
    tmp = _main.tempfile.mkdtemp(prefix="torrent-metadata-lt-")
    session = _main._libtorrent_session
    if session is None:
        raise RuntimeError("libtorrent session is not initialized")

    handle = None
    try:
        atp = _main.lt.parse_magnet_uri(_main._metadata_magnet_with_trackers(magnet))
        atp.save_path = tmp
        atp.flags = atp.flags | _main.lt.torrent_flags.upload_mode
        atp.flags = atp.flags & ~_main.lt.torrent_flags.auto_managed
        handle = session.add_torrent(atp)

        deadline = _main.time.monotonic() + _main.TORRENT_METADATA_JOB_TIMEOUT_SECONDS
        while _main.time.monotonic() < deadline:
            if handle.has_metadata():
                ti = handle.torrent_file()
                if ti is None:
                    raise RuntimeError("libtorrent returned metadata without torrent info")
                fs = ti.layout()
                files = []
                for index in range(fs.num_files()):
                    path = str(fs.file_path(index))
                    files.append({
                        "index": index,
                        "name": path,
                        "size": int(fs.file_size(index)),
                        "path": path,
                        "type": "file",
                        "priority": 1,
                    })
                return {
                    "name": str(ti.name() or f"Torrent {info_hash_value[:8]}"),
                    "hash": info_hash_value.lower(),
                    "files": files,
                    "totalSize": sum(int(item["size"]) for item in files),
                    "source": "libtorrent_metadata",
                    "pending": False,
                    "createdPreview": False,
                    "message": "Torrent metadata loaded without starting Seedr.",
                }

            for alert in session.pop_alerts():
                message = str(alert)
                if "error" in message.lower() or "tracker" in message.lower():
                    _main.logger.info("libtorrent metadata alert: %s", message[:500])
            _main.time.sleep(0.2)

        status = handle.status()
        raise TimeoutError(
            f"Metadata is still resolving (state={status.state}, "
            f"peers={status.num_peers}, seeds={status.num_seeds})"
        )
    finally:
        try:
            if handle is not None and handle.is_valid():
                session.remove_torrent(handle)
        except Exception:
            pass
        _main.shutil.rmtree(tmp, ignore_errors=True)

async def _schedule_metadata_job_expiry(job_id: str) -> None:
    try:
        await _main.asyncio.sleep(_main.TORRENT_METADATA_JOB_RETENTION_SECONDS)
    except _main.asyncio.CancelledError:
        return
    job = _main._metadata_jobs.get(job_id)
    if job and job.get("status") in {"ready", "error"}:
        _main._metadata_jobs.pop(job_id, None)

async def _run_metadata_job(job_id: str, magnet: str, info_hash_value: str) -> None:
    """Keep retrying independent metadata sources for up to the configured background TTL."""
    job = _main._metadata_jobs[job_id]
    started_at = float(job.get("startedAt") or _main.time.time())
    deadline_at = float(job.get("deadlineAt") or (started_at + _main.TORRENT_METADATA_BACKGROUND_TTL_SECONDS))
    job.update({
        "status": "resolving",
        "startedAt": started_at,
        "updatedAt": _main.time.time(),
        "deadlineAt": deadline_at,
        "rounds": int(job.get("rounds") or 0),
    })

    async def resolve_libtorrent() -> dict[str, Any] | None:
        session = await _main._get_libtorrent_session()
        return await _main.asyncio.to_thread(
            _main._metadata_from_libtorrent_sync,
            magnet,
            info_hash_value,
        )

    while _main.time.time() < deadline_at:
        job["rounds"] = int(job.get("rounds") or 0) + 1
        job["updatedAt"] = _main.time.time()

        # Shared public cache is the fastest path and costs no torrent peer work.
        cached_result = await _main._fetch_itorrents_metadata(info_hash_value)
        if cached_result:
            job.update({
                "status": "ready",
                "result": cached_result,
                "error": None,
                "updatedAt": _main.time.time(),
            })
            _main.logger.info("Metadata job %s resolved via iTorrents cache on round %s", job_id, job["rounds"])
            _main.asyncio.create_task(_main._schedule_metadata_job_expiry(job_id))
            return

        remote_task = _main.asyncio.create_task(
            _main._fetch_remote_torrent_metadata(magnet, info_hash_value)
        )
        knaben_task = _main.asyncio.create_task(_main._lookup_knaben_by_hash(info_hash_value))
        webtorrent_task = _main.asyncio.create_task(
            _main._fetch_fast_test_metadata(
                _main._metadata_magnet_with_trackers(magnet),
                info_hash_value,
            )
        )
        libtorrent_task = _main.asyncio.create_task(resolve_libtorrent())
        tasks = (remote_task, knaben_task, webtorrent_task, libtorrent_task)
        errors: list[str] = []
        winner: dict[str, Any] | None = None

        try:
            # Race the independent remote/indexer/libtorrent paths. The first
            # usable file list wins; the libtorrent worker finishes before a
            # retry round begins so the same torrent is never added twice.
            for task in _main.asyncio.as_completed(tasks):
                try:
                    result = await task
                except TimeoutError as exc:
                    errors.append(str(exc))
                    continue
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                if result and isinstance(result, dict) and result.get("files"):
                    winner = result
                    break

            if winner:
                _main._metadata_cache[info_hash_value.lower()] = winner
                _main._save_metadata_cache()
                job.update({
                    "status": "ready",
                    "result": winner,
                    "error": None,
                    "updatedAt": _main.time.time(),
                })
                _main.logger.info(
                    "Metadata job %s resolved via %s on round %s",
                    job_id,
                    winner.get("source", "unknown"),
                    job["rounds"],
                )
                return
        finally:
            for task in (remote_task, knaben_task, webtorrent_task):
                if not task.done():
                    task.cancel()
            await _main.asyncio.gather(
                remote_task,
                knaben_task,
                webtorrent_task,
                return_exceptions=True,
            )

            # asyncio.to_thread cannot force-stop the libtorrent worker.
            # When another resolver already won, let that worker finish in the
            # shared session without delaying the successful response. When the
            # round failed, wait for it before starting the next retry round so
            # the same torrent is never added twice concurrently.
            if winner is None:
                try:
                    await libtorrent_task
                except Exception as exc:
                    errors.append(str(exc))

        if _main.time.time() >= deadline_at:
            break
        delay = min(
            max(0, _main.TORRENT_METADATA_BACKGROUND_RETRY_SECONDS),
            max(0, deadline_at - _main.time.time()),
        )
        job["updatedAt"] = _main.time.time()
        if delay > 0:
            await _main.asyncio.sleep(delay)

    job.update({
        "status": "error",
        "error": "Torrent metadata could not be resolved within 6 hours.",
        "updatedAt": _main.time.time(),
    })
    _main.logger.warning("Metadata job %s exhausted its background deadline", job_id)
    _main.asyncio.create_task(_main._schedule_metadata_job_expiry(job_id))
