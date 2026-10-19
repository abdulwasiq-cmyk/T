"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['empty_torrents', '_libtorrent_metadata_sync', '_fetch_remote_torrent_metadata', '_lookup_knaben_by_hash', 'seedr_inspect_magnet', 'seedr_metadata_jobs', 'seedr_inspect_magnet_status']

router = APIRouter()

@router.get("/api/v2/torrents/info")
async def empty_torrents(filter: str | None = None):
    return []

def _libtorrent_metadata_sync(magnet: str) -> tuple[str, str, list[dict[str, _main.Any]], int]:
    """Resolve only torrent metadata using libtorrent; never request content pieces."""
    tmp = _main.tempfile.mkdtemp(prefix="torrent-metadata-lt-")
    ses = None
    handle = None
    try:
        ses = _main.lt.session()

        # Explicitly enable the discovery paths needed for magnet metadata on
        # a cloud host. Keep UPnP/NAT-PMP/LSd off: they are not useful on
        # Render and can add connection delay. DHT + tracker access remain on.
        ses.apply_settings({
            "enable_dht": True,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_tcp": True,
            "enable_outgoing_utp": True,
            "enable_incoming_tcp": True,
            "enable_incoming_utp": True,
            "listen_interfaces": "0.0.0.0:0",
            "dht_bootstrap_nodes": (
                "router.bittorrent.com:6881,"
                "router.utorrent.com:6881,"
                "dht.transmissionbt.com:6881"
            ),
            "use_dht_as_fallback": False,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connection_speed": 50,
            "handshake_timeout": 10,
        })

        atp = _main.lt.parse_magnet_uri(magnet)
        atp.save_path = tmp

        # upload_mode prevents piece requests. Disable auto-management so the
        # session cannot later take the torrent out of upload mode automatically.
        atp.flags = atp.flags | _main.lt.torrent_flags.upload_mode
        atp.flags = atp.flags & ~_main.lt.torrent_flags.auto_managed

        handle = ses.add_torrent(atp)

        deadline = _main.time.monotonic() + 30.0
        while _main.time.monotonic() < deadline:
            if handle.has_metadata():
                break
            for alert in ses.pop_alerts():
                message = str(alert)
                if "error" in message.lower() or "tracker" in message.lower() or "dht" in message.lower():
                    _main.logger.info("libtorrent alert for %s: %s", _main.info_hash(magnet), message[:500])
            _main.time.sleep(0.15)

        if not handle.has_metadata():
            status = handle.status()
            raise TimeoutError(
                f"libtorrent metadata timeout (state={status.state}, "
                f"peers={status.num_peers}, seeds={status.num_seeds})"
            )

        ti = handle.torrent_file()
        if ti is None:
            raise RuntimeError("libtorrent reported metadata but torrent_file() is empty")

        fs = ti.layout()
        torrent_name = str(ti.name() or "")
        files: list[dict[str, Any]] = []
        for index in range(fs.num_files()):
            path = str(fs.file_path(index))
            size = int(fs.file_size(index))
            if not path:
                continue
            files.append({
                "index": index,
                "name": path,
                "size": size,
                "path": path,
                "type": "file",
                "priority": 1,
            })

        total_size = sum(int(item.get("size") or 0) for item in files)
        return torrent_name, magnet, files, total_size
    finally:
        try:
            if ses is not None and handle is not None and handle.is_valid():
                ses.remove_torrent(handle)
        except Exception:
            pass
        try:
            if ses is not None:
                del ses
        except Exception:
            pass
        _main.shutil.rmtree(tmp, ignore_errors=True)

async def _fetch_remote_torrent_metadata(magnet: str, info_hash_value: str) -> dict[str, _main.Any] | None:
    """Use the public torrent-metadata resolver as a metadata-only fallback."""
    if not _main.TORRENT_METADATA_API_URL:
        return None
    try:
        # This service resolves BEP-9 metadata from a magnet without starting
        # a file download. Give Fly.io enough time to wake a sleeping instance.
        async with _main._InstrumentedAsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.post(
                _main.TORRENT_METADATA_API_URL.rstrip("/") + "/",
                json={"query": magnet},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (_main.httpx.HTTPError, ValueError) as exc:
        _main.logger.info("Remote metadata service failed for %s: %s", info_hash_value, exc)
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    returned_hash = str(data.get("infoHash") or "").strip().lower()
    if returned_hash and returned_hash != info_hash_value.lower():
        _main.logger.info("Remote metadata hash mismatch: wanted %s, got %s", info_hash_value, returned_hash)
        return None

    raw_files = data.get("files")
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
            "index": index,
            "name": str(item.get("name") or path),
            "size": int(float(item.get("size") or 0)),
            "path": path,
            "type": "file",
            "priority": 1,
        })
    if not files:
        return None

    result = {
        "name": str(data.get("name") or files[0]["name"]),
        "hash": info_hash_value.lower(),
        "files": files,
        "totalSize": sum(int(item["size"]) for item in files),
        "source": "remote_torrent_metadata",
        "pending": False,
        "createdPreview": False,
        "message": "Torrent metadata loaded without starting Seedr.",
    }
    _main._metadata_cache[info_hash_value.lower()] = result
    _main._save_metadata_cache()
    return result

async def _lookup_knaben_by_hash(info_hash_value: str) -> dict[str, _main.Any] | None:
    """Find and parse a Knaben cached descriptor for an exact info-hash."""
    target = info_hash_value.strip().lower()
    if not _main.re.fullmatch(r"[0-9a-f]{40}", target):
        return None

    payload_base = {
        "search_type": "100%",
        "query": target,
        "from": 0,
        "size": 20,
        "hide_unsafe": False,
        "hide_xxx": False,
    }

    async with _main._InstrumentedAsyncClient(timeout=6.0, follow_redirects=True) as client:
        for field in ("hash", None):
            payload = dict(payload_base)
            if field:
                payload["search_field"] = field
            try:
                response = await client.post(
                    _main.KNABEN_API_URL,
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
            except (_main.httpx.HTTPError, ValueError):
                continue

            hits = data.get("hits") if isinstance(data, dict) else None
            if not isinstance(hits, list):
                continue

            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                hit_hash = str(hit.get("hash") or "").strip().lower()
                if hit_hash != target:
                    continue

                descriptor = str(hit.get("link") or "").strip()
                if descriptor:
                    parsed = await _main._fetch_source_torrent_descriptor(descriptor, target)
                    if parsed:
                        return parsed

                details = str(hit.get("details") or "").strip()
                if details:
                    parsed = await _main._fetch_source_torrent_descriptor(details, target)
                    if parsed:
                        return parsed
    return None

@router.post("/api/v2/torrents/inspect-magnet")
async def seedr_inspect_magnet(body: dict[str, _main.Any]):
    """Return cached metadata immediately or start a background resolver."""
    raw_magnet = str(body.get("magnet") or body.get("source") or "").strip()
    if not raw_magnet:
        raise _main.HTTPException(400, "A magnet link is required")

    magnet = _main.normalize_magnet(raw_magnet)
    h = _main.info_hash(magnet)
    if not h:
        raise _main.HTTPException(400, "A valid BTIH magnet link is required")
    h = h.lower()

    cached = _main._metadata_cache.get(h)
    if cached:
        return cached

    # Shared public cache is the fastest metadata path for both pasted magnets
    # and search results. It never starts Seedr or requests payload pieces.
    cached_descriptor = await _main._fetch_itorrents_metadata(h)
    if cached_descriptor:
        return cached_descriptor

    # Search results can carry a detail URL. When that page exposes a real
    # .torrent descriptor, use it before touching DHT/trackers.
    descriptor_url = str(body.get("descriptorUrl") or "").strip()
    source_url = str(body.get("sourceUrl") or body.get("infoUrl") or "").strip()

    # First use an actual descriptor URL returned by the indexer.
    for candidate_url in (descriptor_url, source_url):
        if candidate_url:
            descriptor_result = await _main._fetch_source_torrent_descriptor(candidate_url, h)
            if descriptor_result:
                return descriptor_result

    # Search results may have an exact hash cached by Knaben. For a pasted
    # magnet there is no source URL, so do this lookup in the background rather
    # than making the user wait on a second network request before the job starts.
    if descriptor_url or source_url:
        try:
            hash_result = await _main._lookup_knaben_by_hash(h)
        except Exception as exc:
            _main.logger.info("Knaben hash lookup failed for %s: %s", h, exc)
            hash_result = None
        if hash_result:
            return hash_result

    job_id = h
    existing = _main._metadata_jobs.get(job_id)
    if existing and existing.get("status") == "ready" and existing.get("result"):
        return existing["result"]

    if not existing or existing.get("status") == "error":
        now = _main.time.time()
        _main._metadata_jobs[job_id] = {
            "status": "queued",
            "jobId": job_id,
            "hash": h,
            "name": str(_main.parse_qs(_main.urlsplit(magnet).query).get("dn", [""])[0] or "").strip(),
            "startedAt": now,
            "updatedAt": now,
            "deadlineAt": now + _main.TORRENT_METADATA_BACKGROUND_TTL_SECONDS,
            "rounds": 0,
            "error": None,
        }
        _main.asyncio.create_task(_main._run_metadata_job(job_id, magnet, h))

    # Give a fast first attempt a short window. Slow metadata resolution
    # continues in the background and is polled by the frontend.
    for _ in range(10):
        await _main.asyncio.sleep(0.2)
        job = _main._metadata_jobs.get(job_id, {})
        if job.get("status") == "ready" and job.get("result"):
            return job["result"]
        if job.get("status") == "error":
            raise _main.HTTPException(502, str(job.get("error") or "Metadata lookup failed"))

    return _main.JSONResponse(
        status_code=202,
        content={
            "status": "resolving",
            "jobId": job_id,
            "hash": h,
            "source": "libtorrent_metadata",
            "message": "Torrent metadata is still resolving. Seedr has not been started.",
        },
    )

@router.get("/api/v2/torrents/metadata-jobs")
async def seedr_metadata_jobs():
    now = _main.time.time()
    jobs: list[dict[str, Any]] = []
    for job in _main._metadata_jobs.values():
        started = float(job.get("startedAt") or now)
        deadline = float(job.get("deadlineAt") or started)
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        files = result.get("files") if isinstance(result.get("files"), list) else []
        jobs.append({
            "jobId": str(job.get("jobId") or job.get("hash") or ""),
            "hash": str(job.get("hash") or "").lower(),
            "name": str(job.get("name") or result.get("name") or "").strip(),
            "status": str(job.get("status") or "resolving"),
            "rounds": int(job.get("rounds") or 0),
            "startedAt": started,
            "updatedAt": float(job.get("updatedAt") or started),
            "deadlineAt": deadline,
            "elapsedSeconds": max(0, int(now - started)),
            "remainingSeconds": max(0, int(deadline - now)),
            "fileCount": len(files),
            "totalSize": int(result.get("totalSize") or 0),
            "source": str(result.get("source") or "background_metadata"),
            "error": str(job.get("error") or "") or None,
        })
    jobs.sort(key=lambda item: float(item.get("startedAt") or 0), reverse=True)
    return {
        "jobs": jobs,
        "backgroundTtlSeconds": _main.TORRENT_METADATA_BACKGROUND_TTL_SECONDS,
        "retentionSeconds": _main.TORRENT_METADATA_JOB_RETENTION_SECONDS,
    }

@router.get("/api/v2/torrents/inspect-magnet/status")
async def seedr_inspect_magnet_status(jobId: str = _main.Query(...)):
    job = _main._metadata_jobs.get(jobId.lower())
    if not job:
        cached = _main._metadata_cache.get(jobId.lower())
        if cached:
            return cached
        raise _main.HTTPException(404, "Metadata job not found")

    if job.get("status") == "ready" and job.get("result"):
        return job["result"]

    if job.get("status") == "error":
        raise _main.HTTPException(502, str(job.get("error") or "Metadata lookup failed"))

    now = _main.time.time()
    started = float(job.get("startedAt") or now)
    deadline = float(job.get("deadlineAt") or started)
    return {
        "status": "resolving",
        "jobId": jobId,
        "hash": jobId,
        "name": str(job.get("name") or ""),
        "rounds": int(job.get("rounds") or 0),
        "startedAt": started,
        "updatedAt": float(job.get("updatedAt") or started),
        "deadlineAt": deadline,
        "elapsedSeconds": max(0, int(now - started)),
        "remainingSeconds": max(0, int(deadline - now)),
        "source": "background_metadata",
        "message": job.get("error") or "Torrent metadata is still resolving in the background. Seedr has not been started.",
    }
