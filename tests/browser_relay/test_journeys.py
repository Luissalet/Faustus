"""Durable journey/capture API and real local encoding, with a simulated WS peer."""
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from PIL import Image
from websockets.asyncio.client import connect

from src.browser_relay.protocol import ORIGIN, EXTENSION_ID, validate_arguments
from src.browser_relay.video import save_video
from tests.browser_relay.test_mcp import StdioClient, ROOT


def jpeg(color='coral'):
    data = io.BytesIO(); Image.new('RGB', (32, 24), color).save(data, format='JPEG')
    return base64.b64encode(data.getvalue()).decode()


@pytest.mark.parametrize('arguments', [
    {'action': 'sessions'}, {'action': 'session_save', 'tab_id': 1, 'name': 'shop'},
    {'action': 'session_open', 'name': 'shop'}, {'action': 'session_forget', 'name': 'shop'},
    {'action': 'adopt_tab', 'tab_id': 1, 'reason': 'Continue here'}, {'action': 'handoff', 'tab_id': 1},
    {'action': 'handoff_status', 'tab_id': 1}, {'action': 'har_start', 'tab_id': 1, 'include_bodies': True},
    {'action': 'har_stop', 'tab_id': 1, 'output_path': '/tmp/network.har'},
    {'action': 'recording_start', 'tab_id': 1, 'cursor': True, 'output_path': '/tmp/tab.webm'},
    {'action': 'recording_stop', 'tab_id': 1}, {'action': 'last_minute', 'tab_id': 1, 'output_path': '/tmp/recent.webm'},
])
def test_capture_and_journey_catalog(arguments):
    assert validate_arguments(arguments) == arguments


@pytest.mark.parametrize('arguments', [
    {'action': 'handoff', 'tab_id': 1, 'continued': True}, {'action': 'continue', 'tab_id': 1},
    {'action': 'session_open'}, {'action': 'session_save', 'name': 'shop'},
    {'action': 'har_stop', 'tab_id': 1}, {'action': 'recording_start', 'tab_id': 1},
    {'action': 'snapshot', 'tab_id': 1, 'cursor': True}, {'action': 'handoff', 'tab_id': 1, 'output_path': '/tmp/x'},
])
def test_no_tool_can_fake_a_human_continue_and_required_targets_are_explicit(arguments):
    with pytest.raises(ValueError): validate_arguments(arguments)


@pytest.mark.asyncio
async def test_actual_encoder_duration_receipt_and_existing_file(tmp_path):
    from src.hoard_link.media.bins import find
    output = tmp_path / 'tab.webm'
    video = {'mime': 'video/webm', 'cursor': True, 'last_minute': True,
             'frames': [{'timestamp': 10, 'data': jpeg()}, {'timestamp': 11, 'data': jpeg('blue')}]}
    receipt = await save_video(video, str(output))
    assert output.read_bytes().startswith(b'\x1aE\xdf\xa3')
    assert receipt['sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert receipt['complete'] and receipt['duration_seconds'] == 1.2
    probe = subprocess.run([find('ffprobe').path, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(output)], capture_output=True, check=True, timeout=15)
    actual = json.loads(probe.stdout)
    assert actual['streams'][0]['codec_name'] == 'vp9'
    assert abs(float(actual['format']['duration']) - 1.2) < .1
    prior = output.read_bytes()
    with pytest.raises(ValueError): await save_video(video, str(output))
    assert output.read_bytes() == prior


@pytest.mark.asyncio
@pytest.mark.parametrize('frames', [[], [None], [{'timestamp': True, 'data': 'bad'}], [{'timestamp': 0, 'data': 'bad'}], [{'timestamp': 2, 'data': ''}, {'timestamp': 1, 'data': ''}]])
async def test_invalid_video_never_publishes_a_complete_file(tmp_path, frames):
    target = tmp_path / 'invalid.webm'
    with pytest.raises(ValueError): await save_video({'mime': 'video/webm', 'frames': frames}, str(target))
    assert not target.exists()


@pytest.mark.asyncio
async def test_cancelled_encoding_reaps_its_real_child_and_publishes_no_output(tmp_path, monkeypatch):
    created, children = asyncio.Event(), []
    original = asyncio.create_subprocess_exec
    async def launch(*args, **kwargs):
        process = await original(*args, **kwargs); children.append(process); created.set(); return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    target = tmp_path / 'cancelled.webm'
    task = asyncio.create_task(save_video({'mime': 'video/webm', 'frames': [{'timestamp': i, 'data': jpeg()} for i in range(20)]}, str(target)))
    await created.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert children[0].returncode is not None and not target.exists()


@pytest.mark.asyncio
async def test_real_mcp_retains_streamed_minute_when_extension_disconnects(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONIOENCODING': 'utf-8'}
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'src.browser_relay.server', '--port', '0', '--peer-wait', '0',
        cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    client, peer = StdioClient(process), None
    try:
        port = int((await asyncio.wait_for(process.stderr.readline(), 60)).decode().strip().rsplit(':', 1)[1])
        await client.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'capture-contract', 'version': '1'}})
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); await process.stdin.drain()
        peer = await connect(f'ws://127.0.0.1:{port}', origin=ORIGIN)
        await peer.send(json.dumps({'type': 'hello', 'protocol': 1, 'extensionId': EXTENSION_ID})); await peer.recv()
        path = tmp_path / 'recording.webm'
        start = asyncio.create_task(client.call({'action': 'recording_start', 'tab_id': 3, 'output_path': str(path)}))
        command = json.loads(await peer.recv())
        await peer.send(json.dumps({'type': 'recording_frame', 'tab_id': 3, 'frame': {'timestamp': time.time(), 'data': jpeg()}}))
        await peer.send(json.dumps({'type': 'result', 'id': command['id'], 'ok': True, 'phase': 'action_confirmed', 'result': {'recording': True}}))
        assert not (await start)['isError']
        click = asyncio.create_task(client.call({'action': 'click', 'tab_id': 3, 'ref': 'current'}))
        await peer.recv(); await peer.close()
        failed = (await click)['structuredContent']
        assert not failed['ok'] and failed['phase'] == 'outcome_unknown'
        assert failed['error']['code'] == 'extension_disconnected'
        assert Path(failed['failure_recording']['path']).is_file()
        assert 'frames' not in json.dumps(failed).replace('"frames": 2', ''), failed
        recent = (await client.call({'action': 'last_minute', 'tab_id': 3, 'output_path': str(tmp_path / 'recent.webm')}))['structuredContent']
        assert recent['ok'] and recent['result']['source'] == 'relay_buffer'
        assert recent['result']['file']['complete'] and not path.exists()
    finally:
        if peer: await peer.close()
        if process.returncode is None:
            process.stdin.close()
            try: await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError: process.kill(); await process.wait()
