import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import wave

import pytest

from src import media_transforms as media
from src.agent_tools.media_tools import MediaTransformTool


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    # Full-suite collection can replace this module after this file is imported.
    # Bind the same workspace context the production resolver currently uses.
    from src.tool_execution import _active_workspace, _active_workspace_roots, get_active_workspace
    one = _active_workspace.set(str(tmp_path))
    many = _active_workspace_roots.set(())
    assert get_active_workspace() == str(tmp_path)
    yield
    _active_workspace_roots.reset(many)
    _active_workspace.reset(one)


def png(path):
    from PIL import Image
    Image.new('RGBA', (40, 20), (200, 50, 20, 100)).save(path)


def recipe(**kwargs):
    return {'source': 'input.png', 'path': 'output.webp', 'format': 'webp', **kwargs}


@pytest.mark.asyncio
async def test_preflight_reads_without_writes_and_reports_loss(tmp_path):
    png(tmp_path / 'input.png')
    before = list(tmp_path.iterdir())
    plan = await media.plan_media_transform(recipe())
    assert plan['ready_to_attempt'] and plan['source']['width'] == 40
    assert 'lossy' in ' '.join(plan['warnings'])
    assert list(tmp_path.iterdir()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('fmt,extension', [('png', 'png'), ('jpeg', 'jpg'), ('webp', 'webp')])
async def test_real_image_conversion_resize_provenance_and_original(tmp_path, fmt, extension):
    from PIL import Image
    source = tmp_path / 'input.png'
    png(source)
    original = source.read_bytes()
    events = []
    async def progress(event):
        events.append(event)
    result = await media.transform_media(recipe(path=f'output.{extension}', format=fmt,
        max_width=16, **({'background': '#ffffff'} if fmt == 'jpeg' else {})), progress)
    output = Path(result['path'])
    with Image.open(output) as image:
        image.load()
        assert image.size == (16, 8)
        assert image.format == {'png': 'PNG', 'jpeg': 'JPEG', 'webp': 'WEBP'}[fmt]
    assert result['provenance']['source_sha256'] == hashlib.sha256(original).hexdigest()
    assert result['provenance']['output_sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert source.read_bytes() == original
    assert result['original_preserved'] is True
    assert not list(tmp_path.glob('.faustus-media-*'))
    assert [e['phase'] for e in events][0] == 'inspecting'
    assert events[-1]['phase'] == 'validating'


@pytest.mark.asyncio
async def test_exif_orientation_no_upscale_and_metadata_removed(tmp_path):
    from PIL import Image
    original = Image.new('RGB', (40, 20))
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = 'Private description'
    original.save(tmp_path / 'input.jpg', exif=exif)
    result = await media.transform_media(recipe(source='input.jpg', path='output.png', format='png', max_height=1000))
    with Image.open(result['path']) as image:
        assert image.size == (20, 40)
        assert not image.getexif()


@pytest.mark.asyncio
async def test_transparency_is_not_silently_lost(tmp_path):
    png(tmp_path / 'input.png')
    with pytest.raises(media.MediaTransformError, match='background') as error:
        await media.transform_media(recipe(format='jpeg', path='output.jpg'))
    assert error.value.code == 'background_required'
    assert not (tmp_path / 'output.jpg').exists()


@pytest.mark.asyncio
async def test_animation_refused_without_publishing(tmp_path):
    from PIL import Image
    Image.new('RGB', (10, 10), 'red').save(tmp_path / 'input.gif', save_all=True,
        append_images=[Image.new('RGB', (10, 10), 'blue')], duration=100)
    with pytest.raises(media.MediaTransformError) as error:
        await media.transform_media(recipe(source='input.gif'))
    assert error.value.code == 'animated_or_multipage'
    assert not (tmp_path / 'output.webp').exists()
    assert not list(tmp_path.glob('.faustus-media-*'))


@pytest.mark.parametrize('override', [
    {'max_width': True}, {'max_height': 0}, {'max_width': 9000}, {'quality': '90'},
    {'quality': float('nan')}, {'command': 'delete everything'}, {'format': []},
    {'background': 'red'}, {'path': 'out.jpg'}, {'source': 'list.m3u8'},
    {'source': 'https://example.com/a.png'}, {'path': '//server/out.webp'},
    {'path': '../out.webp'}, {'path': '.ssh/out.webp'}, {'path': 'missing/out.webp'},
    {'format': 'wav', 'path': 'out.wav', 'source': 'in.wav', 'max_width': 10},
    {'format': 'png', 'path': 'out.png', 'quality': 90},
])
def test_invalid_recipes_rejected_before_start(tmp_path, override):
    with pytest.raises(media.MediaTransformError):
        media.validate_args(recipe(**override))


@pytest.mark.asyncio
async def test_existing_destination_and_source_not_overwritten(tmp_path):
    png(tmp_path / 'input.png')
    output = tmp_path / 'output.webp'
    output.write_bytes(b'user file')
    for args in [recipe(), recipe(path='input.png', format='png')]:
        with pytest.raises(media.MediaTransformError) as error:
            await media.transform_media(args)
        assert error.value.code == 'destination_exists'
    assert output.read_bytes() == b'user file'


@pytest.mark.asyncio
async def test_concurrent_output_creation_is_not_clobbered(tmp_path, monkeypatch):
    png(tmp_path / 'input.png')
    real_link = os.link
    def competing(source, destination):
        Path(destination).write_bytes(b'other worker')
        real_link(source, destination)
    monkeypatch.setattr(media.os, 'link', competing)
    with pytest.raises(media.MediaTransformError) as error:
        await media.transform_media(recipe())
    assert error.value.code == 'destination_exists'
    assert (tmp_path / 'output.webp').read_bytes() == b'other worker'
    assert not list(tmp_path.glob('.faustus-media-*'))


@pytest.mark.asyncio
async def test_changed_source_and_disk_limit(tmp_path, monkeypatch):
    source = tmp_path / 'input.png'
    png(source)
    before = source.stat()
    with pytest.raises(media.MediaTransformError) as error:
        await media._snapshot(source, tmp_path / 'snapshot.png', {'size_bytes': before.st_size, 'mtime_ns': 0})
    assert error.value.code == 'source_changed'
    monkeypatch.setattr(media.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 1})())
    with pytest.raises(media.MediaTransformError) as error:
        await media.transform_media(recipe())
    assert error.value.code == 'disk_space'


@pytest.mark.asyncio
async def test_timeout_and_cancellation_reap_converter(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()
    async def tracked(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        started.set()
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', tracked)
    monkeypatch.setattr(media, 'TIMEOUT_S', .05)
    command = [sys.executable, '-c', 'import time; time.sleep(30)']
    with pytest.raises(media.MediaTransformError) as error:
        await media._run(command, tmp_path / 'output', None, time.monotonic())
    assert error.value.code == 'timeout' and children[0].returncode is not None
    monkeypatch.setattr(media, 'TIMEOUT_S', 30)
    started.clear()
    task = asyncio.create_task(media._run(command, tmp_path / 'output', None, time.monotonic()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[-1].returncode is not None


def wav(path):
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        writer.writeframes(b'\x00' * 8000)


@pytest.mark.asyncio
async def test_missing_audio_engine_is_reported_without_install(tmp_path, monkeypatch):
    wav(tmp_path / 'input.wav')
    monkeypatch.setattr(media.shutil, 'which', lambda _: None)
    args = recipe(source='input.wav', path='out.wav', format='wav')
    plan = await media.plan_media_transform(args)
    assert plan['engine'] == 'FFmpeg' and not plan['ready_to_attempt']
    with pytest.raises(media.MediaTransformError) as error:
        await media.transform_media(args)
    assert error.value.code == 'engine_unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('fmt', ['wav', 'mp3'])
async def test_real_audio_extract(tmp_path, fmt):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Optional installed FFmpeg/FFprobe required')
    wav(tmp_path / 'input.wav')
    result = await media.transform_media(recipe(source='input.wav', path=f'out.{fmt}', format=fmt))
    assert result['media']['kind'] == 'audio'
    assert result['media']['duration_seconds'] == pytest.approx(.5, abs=.15)
    assert result['recipe'] == 'audio_extract_v1'


@pytest.mark.asyncio
async def test_real_video_extracts_audio_only(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Optional installed FFmpeg/FFprobe required')
    source = tmp_path / 'clip.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x48:r=10',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=8000', '-t', '0.5',
                    '-c:v', 'mpeg4', '-c:a', 'aac', '-metadata', 'title=private metadata', str(source)],
                   check=True, capture_output=True, timeout=20,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    result = await media.transform_media(recipe(source='clip.mp4', path='audio.wav', format='wav'))
    assert result['media']['kind'] == 'audio'
    assert result['media']['channels'] == 2 and result['media']['sample_rate_hz'] == 48000
    assert result['media']['duration_seconds'] == pytest.approx(.5, abs=.2)
    assert 'private metadata' not in json.dumps(result)


@pytest.mark.asyncio
async def test_cancellation_and_bad_output_remove_staging_without_publication(tmp_path, monkeypatch):
    png(tmp_path / 'input.png')
    async def cancelled(command, output, *rest):
        output.write_bytes(b'partial file')
        raise asyncio.CancelledError()
    monkeypatch.setattr(media, '_run', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await media.transform_media(recipe())
    assert not (tmp_path / 'output.webp').exists()
    assert not list(tmp_path.glob('.faustus-media-*'))
    async def broken(command, output, *rest):
        output.write_bytes(b'not valid media')
    monkeypatch.setattr(media, '_run', broken)
    from src.media_inspection import MediaInspectionError
    with pytest.raises(MediaInspectionError):
        await media.transform_media(recipe())
    assert not (tmp_path / 'output.webp').exists()
    assert not list(tmp_path.glob('.faustus-media-*'))


def test_audio_recipe_does_not_accept_network_tracks_or_shell():
    args = media._audio_command('ffmpeg', Path('source.mp4'), Path('out.wav'), 'wav')
    for key, value in [('-protocol_whitelist', 'file'), ('-f', 'mov'),
                       ('-enable_drefs', '0'), ('-use_absolute_path', '0'), ('-map', '0:a:0')]:
        assert args[args.index(key) + 1] == value
    assert '-n' in args and '-y' not in args


@pytest.mark.asyncio
async def test_native_fenced_dispatch_permissions_and_path_locks(tmp_path, monkeypatch):
    from src import tool_execution as execution
    from src.agent_tools import TOOL_TAGS, function_call_to_tool_block, parse_tool_blocks
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS
    from src.tool_capabilities import ToolEffect, capabilities_for_tool, ToolRunSecurityContext, _write_targets
    from src.agent_tools import subagent_tools as workers
    from src.subagent_permissions import coordinator_permissions
    from src.agent_defs import parse_rule
    names = {'plan_media_transform', 'transform_media'}
    assert names <= TOOL_TAGS & NON_ADMIN_BLOCKED_TOOLS
    assert 'plan_media_transform' in PLAN_MODE_READONLY_TOOLS
    assert 'transform_media' not in PLAN_MODE_READONLY_TOOLS
    assert capabilities_for_tool('transform_media').effects == {ToolEffect.READ_WORKSPACE, ToolEffect.WRITE_WORKSPACE}
    content = json.dumps(recipe())
    assert _write_targets('transform_media', content) == ['output.webp']
    assert not ToolRunSecurityContext(external_untrusted_context_seen=True).decision_for('transform_media', content).allowed
    assert parse_tool_blocks('```transform_media\n' + content + '\n```')
    block = function_call_to_tool_block('transform_media', content)
    assert json.loads(block.content) == recipe()
    png(tmp_path / 'input.png')
    monkeypatch.setattr(execution, 'owner_is_admin_or_single_user', lambda _: True)
    _, result = await execution.execute_tool_block(block, owner='alice', workspace=str(tmp_path), security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    assert result['exit_code'] == 0
    monkeypatch.setattr(execution, 'owner_is_admin_or_single_user', lambda _: False)
    _, result = await execution.execute_tool_block(block, owner='public', workspace=str(tmp_path), security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    assert result['exit_code'] == 1
    for rule, tool, args in [
        ('deny read **', 'transform_media', recipe()),
        ('deny write **', 'transform_media', recipe()),
        ('deny read **', 'plan_media_transform', recipe()),
        ('deny read **', 'inspect_media', {'path': 'input.png'}),
    ]:
        token = workers._PERMS_CTX.set(coordinator_permissions([parse_rule(rule)], workspace=str(tmp_path)))
        try:
            assert workers.permission_block_reason(tool, json.dumps(args))
        finally:
            workers._PERMS_CTX.reset(token)


@pytest.mark.asyncio
async def test_handler_errors_are_structured():
    for content in ['[]', '{}', '{broken', 'null']:
        result = await MediaTransformTool().execute(content, {})
        assert result['exit_code'] == 1 and result['error_code']
