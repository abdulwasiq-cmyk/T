"""Application functions for this feature area."""

from fastapi import APIRouter

import main as _main
from typing import Any

__all__ = ['_ffprobe_seedr_file', '_stream_tags', '_track_language', '_track_title', 'seedr_media_info', 'seedr_embedded_subtitle', '_stream_selected_audio', 'seedr_video_media_stats', 'seedr_video_media', 'seedr_audio_media_route', 'seedr_file_subtitle', '_cancel_seedr_task_and_partial_folder', '_cancel_active_seedr_tasks_for_replacement', 'seedr_task_delete', 'seedr_file_delete', 'seedr_folder_delete']

router = APIRouter()

async def _ffprobe_seedr_file(file_id: str) -> dict[str, _main.Any]:
    """Inspect a Seedr file and reuse the result for media/subtitle requests.

    The same MKV was previously probed once for track discovery and then probed
    again when the user selected an embedded subtitle. Reusing the probe keeps
    Render Free from doing the expensive remote MKV inspection twice.
    """
    now = _main.asyncio.get_running_loop().time()
    cached = _main._seedr_ffprobe_cache.get(file_id)
    if cached and cached[0] > now:
        return cached[1]

    source_url = await _main._seedr_media_source_url(file_id)
    command = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        "-analyzeduration", "10M",
        "-probesize", "20M",
        source_url,
    ]
    try:
        process = await _main.asyncio.create_subprocess_exec(
            *command,
            stdout=_main.asyncio.subprocess.PIPE,
            stderr=_main.asyncio.subprocess.PIPE,
        )
        stdout, stderr = await _main.asyncio.wait_for(process.communicate(), timeout=45)
    except _main.asyncio.TimeoutError as exc:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        raise _main.HTTPException(504, "Media track inspection timed out") from exc
    except FileNotFoundError as exc:
        raise _main.HTTPException(503, "FFmpeg is not installed on the media server") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise _main.HTTPException(502, detail or "FFprobe could not inspect the Seedr file")

    try:
        data = _main.json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise _main.HTTPException(502, "FFprobe returned invalid metadata") from exc

    _main._seedr_ffprobe_cache[file_id] = (now + _main._SEEDR_FFPROBE_CACHE_SECONDS, data)
    return data

def _stream_tags(stream: dict[str, _main.Any]) -> dict[str, _main.Any]:
    raw = stream.get("tags")
    if not isinstance(raw, dict):
        return {}
    # FFprobe normally emits lowercase Matroska tag keys, but some containers
    # expose alternate casing/names. Normalize them before building labels.
    return {str(key).strip().lower(): value for key, value in raw.items()}

def _track_language(stream: dict[str, _main.Any]) -> str:
    tags = _main._stream_tags(stream)
    for key in ("language", "lang", "language-eng", "language_ietf"):
        value = str(tags.get(key) or "").strip().lower()
        if value:
            return value.split("-")[0]
    return ""

def _track_title(stream: dict[str, _main.Any], fallback: str) -> str:
    tags = _main._stream_tags(stream)
    for key in ("title", "handler_name", "name", "track_name"):
        value = str(tags.get(key) or "").strip()
        if value:
            return value
    return fallback

@router.get("/api/seedr/media-info/{file_id}")
async def seedr_media_info(file_id: str):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    cached = _main._seedr_media_info_cache.get(file_id)
    now = _main.asyncio.get_running_loop().time()
    if cached and cached[0] > now:
        return cached[1]

    data = await _main._ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    if not isinstance(streams, list):
        streams = []

    language_names = {
        "en": "English", "eng": "English",
        "hi": "Hindi", "hin": "Hindi",
        "fr": "French", "fra": "French",
        "de": "German", "deu": "German",
        "es": "Spanish", "spa": "Spanish",
        "it": "Italian", "ita": "Italian",
        "pt": "Portuguese", "por": "Portuguese",
        "ru": "Russian", "rus": "Russian",
        "ja": "Japanese", "jpn": "Japanese",
        "ko": "Korean", "kor": "Korean",
        "zh": "Chinese", "zho": "Chinese",
        "ar": "Arabic", "ara": "Arabic",
        "bn": "Bengali", "ben": "Bengali",
    }

    audio_tracks = []
    subtitle_tracks = []
    audio_index = 0
    subtitle_index = 0

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "").lower()
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        lang = _main._track_language(stream)
        title = _main._track_title(stream, "")
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        if codec_type == "audio":
            language_label = language_names.get(lang, lang.upper() if lang else "")
            # Keep the raw mix title separate from the language. The frontend
            # combines them into a clearer label such as "English (BD 5.1)".
            label = title or language_label or f"Audio {audio_index + 1}"
            audio_tracks.append({
                "index": audio_index,
                "streamIndex": int(stream.get("index") or 0),
                "language": lang,
                "title": label,
                "codec": str(stream.get("codec_name") or "").upper(),
                "channels": int(stream.get("channels") or 0),
                "default": bool(disposition.get("default")),
            })
            audio_index += 1
        elif codec_type == "subtitle":
            codec = str(stream.get("codec_name") or "").lower()
            # Bitmap subtitle codecs cannot be represented as browser WebVTT.
            if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}:
                continue
            language_label = language_names.get(lang, lang.upper() if lang else "")
            forced = bool(disposition.get("forced"))
            default = bool(disposition.get("default"))
            codec_label = {
                "subrip": "SRT",
                "ass": "ASS",
                "ssa": "SSA",
                "webvtt": "WebVTT",
                "mov_text": "Text",
                "text": "Text",
            }.get(codec, codec.upper() or "Text")
            # Preserve the real Matroska title/language whenever available.
            # Some MKVs contain no subtitle language/title tags at all. In that
            # case the old fallback produced identical entries such as
            # "SRT", making it impossible to tell tracks apart. Give unnamed
            # tracks a stable ordinal while keeping the real language when one
            # is available.
            if title:
                label = title
            elif language_label:
                label = f"{language_label} {subtitle_index + 1}"
            else:
                label = f"{codec_label} {subtitle_index + 1}"

            if forced and "forced" not in label.lower():
                label += " · Forced"
            elif default and "default" not in label.lower():
                label += " · Default"
            stream_index = int(stream.get("index") or 0)
            subtitle_tracks.append({
                "index": subtitle_index,
                "streamIndex": stream_index,
                "language": lang,
                "title": label,
                "codec": str(stream.get("codec_name") or "").upper(),
                "url": f"/api/seedr/media-info/{_main.quote(file_id, safe='')}/subtitle?stream={stream_index}&name={_main.quote(label, safe='')}",
            })
            subtitle_index += 1

    result = {
        "name": str((data.get("format") or {}).get("filename") or file_id) if isinstance(data, dict) else file_id,
        "audioTracks": audio_tracks,
        "subtitleTracks": subtitle_tracks,
    }
    _main._seedr_media_info_cache[file_id] = (now + _main._SEEDR_MEDIA_INFO_CACHE_SECONDS, result)
    return result

@router.get("/api/seedr/media-info/{file_id}/subtitle")
async def seedr_embedded_subtitle(
    file_id: str,
    track: int | None = _main.Query(None, ge=0),
    stream: int | None = _main.Query(None, ge=0),
    name: str = _main.Query("subtitle"),
):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    cache_key = f"{file_id}:{track}"
    now = _main.asyncio.get_running_loop().time()
    cached = _main._seedr_embedded_subtitle_cache.get(cache_key)
    if cached and cached[0] > now:
        return _main.Response(
            content=cached[1],
            media_type="text/vtt; charset=utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "private, max-age=600",
                "Content-Disposition": f'inline; filename="{_main.Path(name).name.replace(chr(34), "_")}.vtt"',
            },
        )

    # Reuses the media-info FFprobe result instead of probing the remote MKV
    # a second time when the user selects an embedded subtitle.
    data = await _main._ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    subtitle_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "subtitle"
        and str(stream.get("codec_name") or "").lower() not in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
    ]
    if stream is not None:
        selected = next(
            (
                item for item in subtitle_streams
                if int(item.get("index") or -1) == stream
            ),
            None,
        )
        if selected is None:
            raise _main.HTTPException(404, "Subtitle stream not found")
        stream_index = int(selected.get("index") or 0)
        cache_key = f"{file_id}:stream:{stream_index}"
    else:
        if track is None or track >= len(subtitle_streams):
            raise _main.HTTPException(404, "Subtitle track not found")
        stream_index = int(subtitle_streams[track].get("index") or 0)
        cache_key = f"{file_id}:{track}"

    source_url = await _main._seedr_media_source_url(file_id)
    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", source_url,
        "-map", f"0:{stream_index}",
        "-c:s", "webvtt",
        "-f", "webvtt",
        "pipe:1",
    ]

    try:
        process = await _main.asyncio.create_subprocess_exec(
            *command,
            stdout=_main.asyncio.subprocess.PIPE,
            stderr=_main.asyncio.subprocess.PIPE,
        )
        stdout, stderr = await _main.asyncio.wait_for(process.communicate(), timeout=90)
    except _main.asyncio.TimeoutError as exc:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        raise _main.HTTPException(504, "Subtitle extraction timed out") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise _main.HTTPException(502, detail or "Subtitle extraction failed")

    _main._seedr_embedded_subtitle_cache[cache_key] = (
        now + _main._SEEDR_EMBEDDED_SUBTITLE_CACHE_SECONDS,
        stdout,
    )

    return _main.Response(
        content=stdout,
        media_type="text/vtt; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "private, max-age=600",
            "Content-Disposition": f'inline; filename="{_main.Path(name).name.replace(chr(34), "_")}.vtt"',
        },
    )

async def _stream_selected_audio(file_id: str, audio_index: int, start: float = 0.0) -> _main.StreamingResponse:
    data = await _main._ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "audio"
    ]
    if audio_index >= len(audio_streams):
        raise _main.HTTPException(404, "Audio track not found")

    source_url = await _main._seedr_media_source_url(file_id)
    stream = audio_streams[audio_index]
    video_streams = [
        item for item in streams
        if isinstance(item, dict) and str(item.get("codec_type") or "").lower() == "video"
    ]
    video_codec = str((video_streams[0] if video_streams else {}).get("codec_name") or "").lower()

    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        *([ "-ss", str(max(0.0, start)) ] if start > 0 else []),
        "-i", source_url,
        "-map", "0:v:0",
        "-map", f"0:a:{audio_index}",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    if video_codec in {"h264", "avc1"}:
        command[command.index("-c:a"):command.index("-c:a")] = ["-c:v", "copy"]
    else:
        command[command.index("-c:a"):command.index("-c:a")] = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
        ]

    process = await _main.asyncio.create_subprocess_exec(
        *command,
        stdout=_main.asyncio.subprocess.PIPE,
        stderr=_main.asyncio.subprocess.PIPE,
    )

    async def body():
        try:
            while True:
                chunk = await process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass

    return _main.StreamingResponse(
        body(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )

@router.get("/api/seedr/media/video/{file_id}/stats")
async def seedr_video_media_stats(file_id: str):
    return {
        "bytesPerSecond": round(_main._media_download_speed(file_id), 2),
        "windowSeconds": _main._MEDIA_STATS_WINDOW_SECONDS,
    }

@router.get("/api/seedr/media/video/{file_id}")
async def seedr_video_media(
    file_id: str,
    request: _main.Request,
    audio: int | None = _main.Query(None, ge=0),
    start: float = _main.Query(0.0, ge=0.0),
):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    if audio is not None:
        return await _main._stream_selected_audio(file_id, audio, start=start)

    upstream_url = await _main.seedr_v2_video_url(file_id)
    if not upstream_url:
        raise _main.HTTPException(404, "Seedr returned no video presentation URL")

    headers = {"Accept": "video/*,application/octet-stream,*/*"}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = _main._InstrumentedAsyncClient(timeout=60, follow_redirects=True)
    try:
        response = await client.send(
            client.build_request("GET", upstream_url, headers=headers),
            stream=True,
        )
    except Exception:
        await client.aclose()
        raise

    if response.status_code >= 400:
        body = await response.aread()
        status = response.status_code
        content_type = response.headers.get("content-type", "application/octet-stream")
        await response.aclose()
        await client.aclose()
        return _main.Response(
            content=body,
            status_code=status,
            media_type=content_type,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "no-store",
    }
    for header in ("content-range", "content-length", "etag", "last-modified"):
        if response.headers.get(header):
            response_headers[header.title()] = response.headers[header]

    async def body_stream():
        try:
            async for chunk in response.aiter_bytes():
                _main._record_media_bytes(file_id, len(chunk))
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return _main.StreamingResponse(
        body_stream(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )

@router.get("/api/seedr/media/audio/{file_id}")
async def seedr_audio_media_route(
    file_id: str,
    request: _main.Request,
    track: int = _main.Query(0, ge=0),
    start: float = _main.Query(0.0, ge=0.0),
):
    """Stream one Seedr audio track as browser-compatible fragmented MP4.

    This route is used for alternate-audio playback so the video element never
    has to be replaced. Keeping video on its original timeline avoids the
    shortened-duration and A/V drift caused by remuxing the whole video when a
    user changes audio tracks.
    """
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    data = await _main._ffprobe_seedr_file(file_id)
    streams = data.get("streams") if isinstance(data, dict) else []
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type") or "").lower() == "audio"
    ]
    if track >= len(audio_streams):
        raise _main.HTTPException(404, "Audio track not found")

    source_url = await _main._seedr_media_source_url(file_id)
    source = audio_streams[track]
    codec = str(source.get("codec_name") or "").lower()

    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        *(["-ss", str(max(0.0, start))] if start > 0 else []),
        "-i", source_url,
        "-map", f"0:a:{track}",
    ]

    # Emit a plain MP3 stream for the hidden browser audio element.
    # MP3 is broadly supported by Chrome/Edge/Android and does not require the
    # fragmented MP4/WebM demuxers that can stall on a non-seekable pipe.
    command += [
        "-vn",
        "-sn",
        "-dn",
        "-ac", "2",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-f", "mp3",
        "pipe:1",
    ]

    try:
        process = await _main.asyncio.create_subprocess_exec(
            *command,
            stdout=_main.asyncio.subprocess.PIPE,
            stderr=_main.asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise _main.HTTPException(503, "FFmpeg is not installed on the media server") from exc

    async def body():
        try:
            while True:
                chunk = await process.stdout.read(512 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass

    return _main.StreamingResponse(
        body(),
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )

@router.get("/api/seedr/files/{file_id}/subtitle")
async def seedr_file_subtitle(
    file_id: str,
    filename: str = _main.Query(""),
):
    """Return a Seedr sidecar subtitle as browser-compatible WebVTT."""
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")

    requested_name = _main.Path(filename or "").name
    extension = requested_name.rsplit(".", 1)[-1].lower() if "." in requested_name else ""
    if extension not in {"srt", "vtt"}:
        raise _main.HTTPException(400, "Only SRT and VTT subtitles are supported")

    result = await _main.download_url(file_id)
    upstream_url = str(result.get("url") or "").strip()
    if not upstream_url:
        raise _main.HTTPException(502, "Seedr returned no subtitle URL")

    async with _main._InstrumentedAsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(upstream_url)
        if response.status_code >= 400:
            raise _main.HTTPException(response.status_code, "Seedr subtitle download failed")

    text = response.content.decode("utf-8-sig", errors="replace")
    if extension == "srt":
        # Convert the common SRT timestamp format to WebVTT.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _main.re.sub(
            r"(\d{2}:\d{2}:\d{2}),(\d{3})",
            r"\1.\2",
            text,
        )
        text = "WEBVTT\n\n" + text.lstrip()
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text.lstrip()

    return _main.Response(
        content=text,
        media_type="text/vtt; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
            "Content-Disposition": "inline; filename=\"" + requested_name.replace('"', '_') + "\"",
        },
    )

async def _cancel_seedr_task_and_partial_folder(
    tid: str,
    task: dict[str, _main.Any] | None = None,
) -> dict[str, _main.Any]:
    """Stop an unfinished Seedr task and remove its partial folder, never a completed download."""
    global _seedr_metadata_cache

    task_id_value = str(tid or "").strip()
    if not task_id_value:
        raise _main.HTTPException(400, "A Seedr task id is required")

    task_found = isinstance(task, dict) and bool(task)
    task_value = task if isinstance(task, dict) else {}
    if not task_found:
        try:
            raw = _main.seedr_data(await _main.seedr_request(f"/tasks/{_main.quote(task_id_value, safe='')}"))
            task_value = _main.unwrap_seedr_task(raw)
            task_found = bool(task_value)
        except (_main.HTTPException, _main.SeedrError) as exc:
            if getattr(exc, "status_code", 0) != 404:
                raise
            task_value = {}

    complete = _main.task_complete(task_value) if task_found else False
    active = task_found and not complete
    task_name = _main.seedr_task_name(task_value) or f"Torrent {task_id_value}"
    folder_id = _main.seedr_task_folder_id(task_value) if task_found else ""
    task_files: list[dict[str, Any]] = []

    # Some Seedr API variants expose the created folder only in task contents.
    if active and not folder_id:
        try:
            task_files = await _main.task_contents(task_id_value)
            folder_id = (
                _main._seedr_effective_task_folder_id(task_value, task_files)
                or _main._seedr_file_folder_id(task_files)
            )
        except (_main.HTTPException, _main.SeedrError):
            task_files = []

    folder_size = 0
    for file in task_files:
        if not isinstance(file, dict):
            continue
        try:
            folder_size += max(0, int(float(file.get("size") or 0)))
        except (TypeError, ValueError):
            continue
    task_deleted = False
    if task_found:
        try:
            await _main.seedr_request(f"/tasks/{_main.quote(task_id_value, safe='')}", "DELETE")
            task_deleted = True
        except (_main.HTTPException, _main.SeedrError) as exc:
            status_code = getattr(exc, "status_code", 0)
            if status_code == 405:
                try:
                    await _main.seedr_request(f"/tasks/{_main.quote(task_id_value, safe='')}/delete", "POST")
                    task_deleted = True
                except (_main.HTTPException, _main.SeedrError) as fallback_exc:
                    if getattr(fallback_exc, "status_code", 0) != 404:
                        raise
            elif status_code != 404:
                raise

    folder_deleted = False
    if active and folder_id and folder_id != "0":
        # Seedr can remove the task before its partial folder is ready to be
        # deleted. Retry the folder DELETE with backoff instead of returning an
        # error that forces the user to click Prepare a second time.
        max_delete_attempts = 5
        last_delete_error: Exception | None = None
        for attempt in range(max_delete_attempts):
            try:
                await _main.seedr_request(f"/fs/folder/{_main.quote(folder_id, safe='')}", "DELETE")
                folder_deleted = True
                last_delete_error = None
                break
            except (_main.HTTPException, _main.SeedrError) as exc:
                status_code = getattr(exc, "status_code", 0)
                if status_code == 404:
                    # A previous attempt or Seedr's own task deletion already
                    # removed the transient folder.
                    folder_deleted = True
                    last_delete_error = None
                    break

                last_delete_error = exc
                if attempt + 1 < max_delete_attempts:
                    delay = min(0.75 * (2 ** attempt), 4.0)
                    _main.logger.warning(
                        "Seedr partial-folder delete retry task=%s folder=%s attempt=%s/%s delay_seconds=%.2f status=%s detail=%s",
                        task_id_value,
                        folder_id,
                        attempt + 1,
                        max_delete_attempts,
                        delay,
                        status_code,
                        getattr(exc, "detail", str(exc)),
                    )
                    await _main.asyncio.sleep(delay)

        if not folder_deleted:
            # Do not abort the new Prepare solely because the partial-folder
            # deletion endpoint briefly failed. The old task has already been
            # cancelled; continue to quota cleanup and the requested add, which
            # will trigger the existing storage-recovery retry if necessary.
            _main.logger.error(
                "Seedr partial-folder deletion did not confirm success after retries; continuing with new add task=%s folder=%s status=%s detail=%s",
                task_id_value,
                folder_id,
                getattr(last_delete_error, "status_code", 0),
                getattr(last_delete_error, "detail", str(last_delete_error)),
            )

    _main._seedr_cleanup_jobs.pop(task_id_value, None)
    _main._seedr_torrent_names_by_task.pop(task_id_value, None)
    if folder_id:
        _main._seedr_torrent_names.pop(folder_id, None)
        _main._seedr_folder_cache.pop(folder_id, None)
    _main._save_seedr_cleanup_jobs()
    _main._seedr_metadata_cache = None
    _main._seedr_folder_cache.clear()

    return {
        "taskId": task_id_value,
        "name": task_name,
        "found": task_found,
        "active": active,
        "completed": complete,
        "taskDeleted": task_deleted,
        "folderId": folder_id,
        "folderDeleted": folder_deleted,
        "size": folder_size,
    }

async def _cancel_active_seedr_tasks_for_replacement(
    replace_task_id: str = "",
) -> list[dict[str, _main.Any]]:
    """Cancel all unfinished tasks in the connected Seedr account before a new torrent is added."""
    explicit_id = str(replace_task_id or "").strip()
    tracked_ids = {str(task_id_value).strip() for task_id_value in _main._seedr_cleanup_jobs}
    tracked_ids.discard("")

    try:
        payload = _main.seedr_data(await _main.seedr_request("/tasks"))
    except (_main.HTTPException, _main.SeedrError) as exc:
        _main.logger.warning("Could not list Seedr tasks before replacement: %s", getattr(exc, "detail", str(exc)))
        raise _main.SeedrError(
            "SEEDR_TASK_LIST_FAILED",
            503,
            "Could not check the current Seedr download before replacing it. Please retry.",
        ) from exc

    cancelled: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_task in _main.arr(payload, ("tasks", "torrents", "items")):
        task = _main.unwrap_seedr_task(_main.seedr_data(raw_task))
        if not task:
            continue
        task_id_value = _main.task_id(task)
        if task_id_value:
            seen_ids.add(task_id_value)
        if not task_id_value or _main.task_complete(task):
            continue
        if task_id_value in {str(item.get("taskId") or "") for item in cancelled}:
            continue

        result = await _main._cancel_seedr_task_and_partial_folder(task_id_value, task)
        if result.get("active"):
            cancelled.append(result)

    # A tracked or explicitly selected task may be temporarily absent from the
    # list endpoint. Check those IDs directly so stale Seedr tasks do not survive
    # simply because the backend restarted or /tasks omitted a row.
    fallback_ids = tracked_ids | ({explicit_id} if explicit_id else set())
    for task_id_value in sorted(fallback_ids - seen_ids):
        try:
            result = await _main._cancel_seedr_task_and_partial_folder(task_id_value)
        except (_main.HTTPException, _main.SeedrError) as exc:
            _main.logger.warning(
                "Seedr replacement could not cancel stale task=%s: %s",
                task_id_value,
                getattr(exc, "detail", str(exc)),
            )
            raise
        if result.get("active"):
            cancelled.append(result)

    if cancelled:
        _main.logger.info(
            "Seedr replacement cancelled %s unfinished task(s): %s",
            len(cancelled),
            ",".join(str(item.get("taskId") or "") for item in cancelled),
        )
    else:
        _main.logger.info("Seedr replacement found no unfinished Seedr tasks")
    return cancelled

@router.delete("/api/seedr/tasks/{tid}")
async def seedr_task_delete(tid: str):
    if not _main.current_seedr_token():
        raise _main.HTTPException(503, "Seedr is not configured")
    return await _main._cancel_seedr_task_and_partial_folder(tid)

@router.delete("/api/seedr/files/{file_id}")
async def seedr_file_delete(file_id: str):
    global _seedr_metadata_cache
    result = await _main.seedr_request(f"/fs/file/{_main.quote(file_id)}", "DELETE")
    _main._seedr_metadata_cache = None
    _main._seedr_folder_cache.clear()
    return result

@router.delete("/api/seedr/folders/{folder_id}")
async def seedr_folder_delete(folder_id: str):
    global _seedr_metadata_cache
    result = await _main.seedr_request(f"/fs/folder/{_main.quote(folder_id)}", "DELETE")
    _main._seedr_metadata_cache = None
    _main._seedr_folder_cache.clear()
    return result
