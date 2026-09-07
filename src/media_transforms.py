"""Small typed media recipes, not model-authored commands or filter graphs.

Outputs are validated in private staging and published without replacing an
existing file. Provenance is returned to the chat; this does not migrate the
artifact store or claim durable cross-artifact identity.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from src.media_inspection import DEMUXERS, IMAGE_EXTENSIONS, MediaInspectionError, _read_bounded, inspect_media

MAX_INPUT_BYTES = 256 * 1024**2
MAX_OUTPUT_BYTES = 128 * 1024**2
TIMEOUT_S = 120
IMAGE_FORMATS = {'png': {'.png'}, 'jpeg': {'.jpg', '.jpeg'}, 'webp': {'.webp'}}
AUDIO_FORMATS = {'wav': {'.wav'}, 'mp3': {'.mp3'}}
FIELDS = {'source', 'path', 'format', 'max_width', 'max_height', 'quality', 'background'}


class MediaTransformError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _path(raw):
    from src.tool_execution import _resolve_tool_path
    if not isinstance(raw, str) or not raw.strip() or '://' in raw or raw.startswith(('\\\\', '//')) or '\x00' in raw:
        raise MediaTransformError('Use a local workspace file, not a URL or network path.', 'invalid_path')
    try:
        resolved = Path(_resolve_tool_path(raw))
    except ValueError as exc:
        raise MediaTransformError(str(exc), 'path_denied') from exc
    if str(resolved).startswith(('\\\\', '//')):
        raise MediaTransformError('Network paths are not supported.', 'invalid_path')
    return resolved


def validate_args(args):
    if not isinstance(args, dict) or set(args) - FIELDS:
        raise MediaTransformError('Unknown media recipe fields. Commands and arbitrary filters are not accepted.', 'invalid_arguments')
    fmt = args.get('format')
    if not isinstance(fmt, str) or fmt not in {**IMAGE_FORMATS, **AUDIO_FORMATS}:
        raise MediaTransformError('Choose png, jpeg, webp, wav or mp3.', 'invalid_format')
    for field, low, high in [('max_width', 1, 8192), ('max_height', 1, 8192), ('quality', 1, 100)]:
        if field in args and (type(args[field]) is not int or not low <= args[field] <= high):
            raise MediaTransformError(f'{field} must be an integer from {low} to {high}.', 'invalid_arguments')
    if 'background' in args and (not isinstance(args['background'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', args['background'])):
        raise MediaTransformError('background must be an explicit #RRGGBB color.', 'invalid_arguments')
    if fmt in AUDIO_FORMATS and set(args) & {'max_width', 'max_height', 'quality', 'background'}:
        raise MediaTransformError('Image options cannot be used for audio extraction.', 'invalid_arguments')
    if (fmt == 'png' and 'quality' in args) or (fmt != 'jpeg' and 'background' in args):
        raise MediaTransformError('quality applies to JPEG/WebP; background applies only to JPEG.', 'invalid_arguments')
    source, destination = _path(args.get('source')), _path(args.get('path'))
    if source == destination or os.path.lexists(destination):
        raise MediaTransformError('The destination already exists or is the original. Choose a new filename; nothing is overwritten.', 'destination_exists')
    if not destination.parent.is_dir():
        raise MediaTransformError('The destination folder must already exist.', 'missing_directory')
    if destination.suffix.lower() not in {**IMAGE_FORMATS, **AUDIO_FORMATS}[fmt]:
        raise MediaTransformError('The destination extension must match the requested format.', 'extension_mismatch')
    if source.suffix.lower() not in (IMAGE_EXTENSIONS if fmt in IMAGE_FORMATS else DEMUXERS):
        raise MediaTransformError('This source cannot use the selected recipe. Images convert to images; audio/video can extract audio.', 'unsupported_conversion')
    return source, destination


async def plan_media_transform(args):
    source, destination = validate_args(args)
    info = await inspect_media(str(source))
    if info['size_bytes'] > MAX_INPUT_BYTES:
        raise MediaTransformError('This recipe accepts inputs up to 256 MiB.', 'input_limit')
    fmt = args['format']
    warnings = ['Embedded metadata is not copied. The original is preserved.']
    if fmt in IMAGE_FORMATS:
        if info.get('kind') != 'image' or info.get('width', 0) * info.get('height', 0) > 16_000_000:
            raise MediaTransformError('Image recipes accept at most 16 million pixels.', 'pixel_limit')
        if fmt == 'jpeg' and info.get('has_alpha_channel') and 'background' not in args:
            raise MediaTransformError('JPEG has no transparency. Choose a background color explicitly, or use PNG/WebP.', 'background_required')
        warnings += ['Single-frame images only; animation and multipage files are refused during conversion.',
                     'Resizing preserves aspect ratio and does not upscale. Color profiles are not preserved; this is not a color-managed print workflow.']
        if fmt in {'jpeg', 'webp'}:
            warnings.append('This recipe uses lossy compression; quality is not a target file size.')
        engine, available = 'Pillow', True  # successful image inspection used this decoder
    else:
        streams = info.get('streams') or []
        if info.get('kind') != 'audio' and not any(s.get('type') == 'audio' for s in streams):
            raise MediaTransformError('The source has no audio track.', 'missing_audio')
        duration = info.get('duration_seconds')
        if not isinstance(duration, (float, int)) or not 0 < duration <= 600:
            raise MediaTransformError('Audio extraction requires a known duration of at most 10 minutes.', 'duration_limit')
        warnings += ['Extracts only the first audio track, stereo at 48 kHz. Video, subtitles, other tracks and chapters are omitted.',
                     'MP3 uses lossy 192 kbit/s encoding.' if fmt == 'mp3' else 'WAV uses 16-bit PCM; bit depth and sample rate may change.']
        engine, available = 'FFmpeg', bool(shutil.which('ffmpeg'))
    return {'recipe': 'image_convert_v1' if fmt in IMAGE_FORMATS else 'audio_extract_v1',
            'source': info, 'path': str(destination), 'options': {k: v for k, v in args.items() if k not in {'source', 'path'}},
            'engine': engine, 'engine_executable_available': available,
            'ready_to_attempt': available, 'warnings': warnings,
            'limits': {'timeout_seconds': TIMEOUT_S, 'input_bytes': MAX_INPUT_BYTES, 'output_bytes': MAX_OUTPUT_BYTES},
            'note': 'Read-only preflight, not a completed conversion. Codec support and full decoding are checked during execution.'}


async def _progress(callback, phase, began):
    if callback:
        try:
            await asyncio.wait_for(callback({'elapsed_s': round(time.monotonic() - began, 1),
                                            'tail': phase, 'phase': phase}), 1)
        except Exception:
            pass  # progress transport must not change the outcome


async def _snapshot(source, target, expected):
    digest = hashlib.sha256()
    size = 0
    with source.open('rb') as reader, target.open('xb') as writer:
        while chunk := reader.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_INPUT_BYTES:
                raise MediaTransformError('The input grew beyond the recipe limit.', 'input_limit')
            writer.write(chunk)
            digest.update(chunk)
            await asyncio.sleep(0)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (expected['size_bytes'], expected['mtime_ns']) or size != after.st_size:
        raise MediaTransformError('The source changed during preparation. Retry.', 'source_changed')
    return digest.hexdigest()


async def _hash_output(path):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as reader:
        while chunk := reader.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_OUTPUT_BYTES:
                raise MediaTransformError('The output exceeded the size limit.', 'output_limit')
            digest.update(chunk)
            await asyncio.sleep(0)
    return digest.hexdigest()


def _audio_command(executable, source, destination, fmt):
    demuxer = DEMUXERS[source.suffix.lower()]
    command = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
               '-max_alloc', '67108864', '-threads', '2', '-protocol_whitelist', 'file',
               '-format_whitelist', demuxer, '-f', demuxer, '-probesize', '5000000', '-analyzeduration', '5000000']
    if demuxer == 'mov':
        command += ['-enable_drefs', '0', '-use_absolute_path', '0']
    command += ['-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1',
                '-map_chapters', '-1', '-ac', '2', '-ar', '48000', '-threads', '2', '-fs', str(MAX_OUTPUT_BYTES + 1)]
    command += ['-c:a', 'pcm_s16le', '-f', 'wav'] if fmt == 'wav' else ['-c:a', 'libmp3lame', '-b:a', '192k', '-f', 'mp3']
    return command + [str(destination)]


async def _run(command, output, callback, began):
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        proc = await spawn
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    readers = [asyncio.create_task(_read_bounded(proc.stdout, 8192)), asyncio.create_task(_read_bounded(proc.stderr, 16384))]
    async def collect():
        stdout, _ = await asyncio.gather(*readers)
        await proc.wait()
        return stdout
    waiter = asyncio.create_task(collect())
    async def supervise():
        while not waiter.done():
            await asyncio.wait({waiter}, timeout=1)
            if output.exists() and output.stat().st_size > MAX_OUTPUT_BYTES:
                raise MediaTransformError('The output exceeded the recipe size limit.', 'output_limit')
            if not waiter.done():
                await _progress(callback, 'converting', began)
        stdout = await waiter
        if proc.returncode:
            try:
                code = json.loads(stdout).get('error_code')
            except (ValueError, AttributeError):
                code = None
            if code == 'animated_or_multipage':
                raise MediaTransformError('Animation and multipage images are not flattened silently. Use a dedicated animation workflow.', code)
            raise MediaTransformError('Conversion failed: damaged/unsupported media or unavailable codec. No output was published.', 'conversion_failed')
    try:
        await asyncio.wait_for(supervise(), TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise MediaTransformError('Conversion timed out; no output was published.', 'timeout') from exc
    finally:
        waiter.cancel()
        for reader in readers:
            reader.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        await asyncio.gather(waiter, *readers, return_exceptions=True)


async def transform_media(args, progress_cb=None):
    began = time.monotonic()
    await _progress(progress_cb, 'inspecting', began)
    plan = await plan_media_transform(args)
    if not plan['ready_to_attempt']:
        raise MediaTransformError('FFmpeg is not installed on the server PATH. No installation was attempted.', 'engine_unavailable')
    source, destination = validate_args(args)
    if shutil.disk_usage(destination.parent).free < plan['source']['size_bytes'] + MAX_OUTPUT_BYTES + 16 * 1024**2:
        raise MediaTransformError('Not enough free disk space for a safe staged conversion.', 'disk_space')
    # Staging stays on the destination volume: hard-link publication is atomic
    # and no-clobber. Unsupported filesystems fail closed, not copy-overwrite.
    with tempfile.TemporaryDirectory(prefix='.faustus-media-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        snapshot = staging / ('source' + source.suffix.lower())
        output = staging / ('result' + destination.suffix.lower())
        await _progress(progress_cb, 'preparing', began)
        source_hash = await _snapshot(source, snapshot, plan['source']['file_revision'])
        if args['format'] in IMAGE_FORMATS:
            command = [sys.executable, str(Path(__file__).with_name('media_transform_worker.py')),
                       str(snapshot), str(output), json.dumps(plan['options'])]
        else:
            command = _audio_command(shutil.which('ffmpeg'), snapshot, output, args['format'])
        await _run(command, output, progress_cb, began)
        await _progress(progress_cb, 'validating', began)
        if not output.is_file() or not 0 < output.stat().st_size <= MAX_OUTPUT_BYTES:
            raise MediaTransformError('The generated output was empty or exceeded the size limit.', 'invalid_output')
        info = await inspect_media(str(output))
        if args['format'] in IMAGE_FORMATS:
            if info.get('format') != {'png': 'PNG', 'jpeg': 'JPEG', 'webp': 'WEBP'}[args['format']]:
                raise MediaTransformError('The output format did not match the recipe.', 'invalid_output')
        else:
            duration = info.get('duration_seconds')
            if not isinstance(duration, (float, int)) or abs(duration - plan['source']['duration_seconds']) > max(0.25, plan['source']['duration_seconds'] * .01):
                raise MediaTransformError('The extracted audio duration did not match the source. No output was published.', 'invalid_output')
        output_hash = await _hash_output(output)
        # Recheck confinement immediately before publication, after all awaits.
        if _path(args['path']) != destination:
            raise MediaTransformError('The destination changed during conversion.', 'path_changed')
        try:
            os.link(output, destination)
        except FileExistsError as exc:
            raise MediaTransformError('The destination was created by another task. It has not been overwritten.', 'destination_exists') from exc
        except OSError as exc:
            raise MediaTransformError('This filesystem could not safely publish the result without overwriting. Choose a local NTFS or hard-link-capable destination.', 'publish_failed') from exc
        info = {**info, 'path': str(destination), 'filename': destination.name}
        # No await after publication: cancellation cannot report a cancelled job
        # after creating its final output. Staging cleanup removes only our files.
        return {'path': str(destination), 'media': info, 'recipe': plan['recipe'],
                'provenance': {'source': str(source), 'source_sha256': source_hash,
                               'output_sha256': output_hash, 'options': plan['options']},
                'warnings': plan['warnings'], 'original_preserved': True,
                'elapsed_seconds': round(time.monotonic() - began, 2)}
