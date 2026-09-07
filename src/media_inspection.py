"""Read-only, bounded media metadata inspection for agent tools.

FFprobe is optional. Images and PCM WAV need only the existing Python runtime.
Inputs are local files authorized by the file-tool resolver; never URLs or
playlists. Parsers run in disposable processes with bounded output and time.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

PROBE_TIMEOUT_S = 10
MAX_MEDIA_BYTES = 8 * 1024**3
MAX_PROBE_OUTPUT = 65536
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
DEMUXERS = {
    '.mp4': 'mov', '.mov': 'mov', '.m4v': 'mov', '.m4a': 'mov',
    '.webm': 'matroska', '.mkv': 'matroska', '.mp3': 'mp3',
    '.flac': 'flac', '.ogg': 'ogg', '.opus': 'ogg', '.aac': 'aac', '.wav': 'wav',
}


class MediaInspectionError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


async def _read_bounded(stream, limit):
    chunks = []
    length = 0
    while True:
        chunk = await stream.read(min(8192, limit + 1 - length))
        if not chunk:
            return b''.join(chunks)
        length += len(chunk)
        if length > limit:
            raise MediaInspectionError('Media metadata exceeded the output limit.', 'output_limit')
        chunks.append(chunk)


async def _run_probe(command):
    # stdin is closed and shell=False is inherent in create_subprocess_exec.
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    ))
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        proc = await spawn
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    readers = [asyncio.create_task(_read_bounded(proc.stdout, MAX_PROBE_OUTPUT)),
               asyncio.create_task(_read_bounded(proc.stderr, 8192))]
    async def collect():
        output, _ = await asyncio.gather(*readers)
        await proc.wait()
        return output
    try:
        output = await asyncio.wait_for(collect(), PROBE_TIMEOUT_S)
        if proc.returncode:
            raise MediaInspectionError('The file could not be inspected. It may be damaged or unsupported by the installed decoder.', 'invalid_media')
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError) as exc:
            raise MediaInspectionError('The media probe returned invalid metadata.', 'invalid_metadata') from exc
        if not isinstance(result, dict):
            raise MediaInspectionError('The media probe returned invalid metadata.', 'invalid_metadata')
        return result
    except asyncio.TimeoutError as exc:
        raise MediaInspectionError('Media inspection timed out; no file was changed.', 'timeout') from exc
    finally:
        for reader in readers:
            if not reader.done():
                reader.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        await asyncio.gather(*readers, return_exceptions=True)


def _number(value, *, integer=False):
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        return int(number) if integer else number
    except (ValueError, TypeError, OverflowError):
        return None


def _frame_rate(value):
    try:
        top, bottom = str(value).split('/')
        return _number(float(top) / float(bottom))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def _normalize_ffprobe(raw):
    streams = []
    raw_streams = raw.get('streams') or []
    if not isinstance(raw_streams, list):
        raise MediaInspectionError('The media probe returned invalid streams.', 'invalid_metadata')
    for item in raw_streams[:32]:
        if not isinstance(item, dict):
            continue
        stream = {'type': str(item.get('codec_type') or 'unknown')[:24],
                  'codec': str(item.get('codec_name') or 'unknown')[:64]}
        for source, target in [('width', 'width'), ('height', 'height'), ('channels', 'channels'),
                               ('sample_rate', 'sample_rate_hz'), ('index', 'index')]:
            value = _number(item.get(source), integer=True)
            if value is not None:
                stream[target] = value
        fps = _frame_rate(item.get('avg_frame_rate'))
        if fps is not None:
            stream['fps'] = fps
        for side in item.get('side_data_list') or []:
            if isinstance(side, dict) and 'rotation' in side:
                rotation = _number(side.get('rotation'))
                if rotation is None:
                    # FFprobe commonly reports clockwise rotation as -90.
                    try:
                        rotation = float(side['rotation']) % 360
                    except (TypeError, ValueError, OverflowError):
                        continue
                if math.isfinite(rotation):
                    stream['rotation_degrees'] = rotation % 360
        if 'width' in stream and 'height' in stream:
            swapped = stream.get('rotation_degrees', 0) in (90, 270)
            stream['display_width'] = stream['height'] if swapped else stream['width']
            stream['display_height'] = stream['width'] if swapped else stream['height']
        streams.append(stream)
    container = raw.get('format') or {}
    if not isinstance(container, dict) or not streams:
        raise MediaInspectionError('No supported media streams were found.', 'invalid_media')
    return {
        'kind': 'video' if any(s['type'] == 'video' for s in streams) else 'audio',
        'format': str(container.get('format_name') or 'unknown')[:128],
        'duration_seconds': _number(container.get('duration')),
        'bit_rate': _number(container.get('bit_rate'), integer=True), 'streams': streams,
        'limitations': ['Container/stream metadata only, not a full decode or quality check. Duration and frame rate may be estimates; null means unavailable.'],
    }


def _ffprobe_command(executable, path, demuxer):
    command = [executable, '-v', 'error', '-max_alloc', '67108864',
               '-protocol_whitelist', 'file', '-format_whitelist', demuxer,
               '-f', demuxer, '-probesize', '5000000', '-analyzeduration', '5000000',
               '-max_streams', '32']
    if demuxer == 'mov':
        command += ['-enable_drefs', '0', '-use_absolute_path', '0']
    return command + ['-show_entries',
        'format=format_name,duration,bit_rate:stream=index,codec_type,codec_name,width,height,channels,sample_rate,avg_frame_rate:stream_side_data=rotation',
        '-of', 'json', '-i', path]


async def inspect_media(raw_path: str) -> dict:
    from src.tool_execution import _resolve_tool_path
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise MediaInspectionError('A local file path is required.', 'invalid_path')
    if '://' in raw_path or raw_path.startswith(('\\\\', '//')) or '\x00' in raw_path:
        raise MediaInspectionError('Use a local media file, not a URL, network path or playlist.', 'invalid_path')
    try:
        path = _resolve_tool_path(raw_path)
    except ValueError as exc:
        raise MediaInspectionError(str(exc), 'path_denied') from exc
    suffix = Path(path).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS and suffix not in DEMUXERS:
        raise MediaInspectionError('Unsupported format. Supported: PNG, JPEG, WebP, GIF, BMP, TIFF, WAV, MP3, FLAC, OGG, OPUS, AAC, M4A, MP4, MOV, MKV and WebM. Playlists are not inspected.', 'unsupported_format')
    try:
        before = os.stat(path)
    except OSError as exc:
        raise MediaInspectionError('The media file is missing or unreadable.', 'file_unavailable') from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise MediaInspectionError('Use a non-empty regular media file.', 'invalid_file')
    if before.st_size > MAX_MEDIA_BYTES:
        raise MediaInspectionError('This file exceeds the 8 GiB inspection limit.', 'file_too_large')
    if suffix in IMAGE_EXTENSIONS or suffix == '.wav':
        kind = 'image' if suffix in IMAGE_EXTENSIONS else 'wav'
        try:
            result = await _run_probe([sys.executable, str(Path(__file__).with_name('media_probe_worker.py')), kind, path])
        except MediaInspectionError as exc:
            executable = shutil.which('ffprobe') if suffix == '.wav' else None
            if exc.code != 'invalid_media' or not executable:
                raise
            # IEEE-float and compressed WAV need FFprobe; PCM remains dependency-free.
            result = _normalize_ffprobe(await _run_probe(_ffprobe_command(executable, path, 'wav')))
    else:
        executable = shutil.which('ffprobe')
        if not executable:
            raise MediaInspectionError('FFprobe is not installed or not on the server PATH. Install FFmpeg/FFprobe to inspect compressed audio and video. Images and PCM WAV work without it. No installation was attempted.', 'ffprobe_unavailable')
        result = _normalize_ffprobe(await _run_probe(_ffprobe_command(executable, path, DEMUXERS[suffix])))
    try:
        after = os.stat(path)
    except OSError as exc:
        raise MediaInspectionError('The media file changed or disappeared during inspection. Retry.', 'file_changed') from exc
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise MediaInspectionError('The media file changed during inspection. Retry before using these measurements.', 'file_changed')
    return {**result, 'path': path, 'filename': Path(path).name, 'size_bytes': after.st_size,
            'file_revision': {'size_bytes': after.st_size, 'mtime_ns': after.st_mtime_ns}}
