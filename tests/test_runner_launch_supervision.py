import asyncio
import sys
import time

import pytest

from routes import agent_runner_routes as routes


@pytest.mark.asyncio
async def test_silent_installer_timeout_finishes_and_kills(monkeypatch):
    monkeypatch.setattr(routes, 'LAUNCH_TIMEOUT_S', 0.2)
    start = time.monotonic()
    chunks = [c async for c in routes._launch_stream([sys.executable, '-c', 'import time; time.sleep(60)'], 'unknown-test-runner')]
    assert time.monotonic() - start < 10
    assert any('exceeded its time limit' in c for c in chunks)
    assert chunks[-1].startswith('event: end')


@pytest.mark.asyncio
async def test_output_does_not_reset_installer_hard_timeout(monkeypatch):
    monkeypatch.setattr(routes, 'LAUNCH_TIMEOUT_S', 0.25)
    start = time.monotonic()
    chunks = [c async for c in routes._launch_stream([sys.executable, '-u', '-c', 'import time\nwhile True:\n print("working", flush=True)\n time.sleep(.02)'], 'unknown-test-runner')]
    assert time.monotonic() - start < 10
    assert any('"event": "output"' in c for c in chunks)
    assert any('exceeded its time limit' in c for c in chunks)


@pytest.mark.asyncio
async def test_cancel_during_installer_spawn_reaps(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec
    async def delayed(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        started.set()
        await release.wait()
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)
    async def collect():
        return [c async for c in routes._launch_stream([sys.executable, '-c', 'import time; time.sleep(60)'], 'unknown-test-runner')]
    task = asyncio.create_task(collect())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
