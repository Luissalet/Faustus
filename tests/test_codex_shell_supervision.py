import asyncio
import sys

import pytest

from routes import codex_routes
from src.agent_tools import subprocess_tools


@pytest.mark.asyncio
async def test_cookbook_shell_uses_bash_syntax_on_windows_too():
    result = await codex_routes._run_shell("if [ 1 = 1 ]; then printf 'working'; fi")
    assert result == {'exit_code': 0, 'stdout': 'working', 'stderr': ''}


@pytest.fixture
def inert_process(monkeypatch):
    """Replace only the shell launcher with our owned inert Python process."""
    processes = []
    started = asyncio.Event()
    async def launch(source, **kwargs):
        if sys.platform != 'win32':
            kwargs['start_new_session'] = True
        proc = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', source, **kwargs)
        processes.append(proc)
        started.set()
        return proc
    monkeypatch.setattr(subprocess_tools, '_create_bash_subprocess', launch)
    return processes, started


@pytest.mark.asyncio
async def test_cookbook_shell_bounds_output_and_reaps(inert_process, monkeypatch):
    monkeypatch.setattr(codex_routes, 'SHELL_OUTPUT_BYTES', 1024)
    result = await codex_routes._run_shell('import time; print("x" * 5000); time.sleep(60)')
    assert result['exit_code'] == -1 and 'limit' in result['stderr']
    assert inert_process[0][0].returncode is not None


@pytest.mark.asyncio
async def test_cookbook_shell_timeout_reaps(inert_process):
    result = await codex_routes._run_shell('import time; time.sleep(60)', timeout=.1)
    assert result['stderr'] == 'timed out'
    assert inert_process[0][0].returncode is not None


@pytest.mark.asyncio
async def test_cookbook_shell_cancel_reaps(inert_process):
    task = asyncio.create_task(codex_routes._run_shell('import time; time.sleep(60)'))
    await inert_process[1].wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inert_process[0][0].returncode is not None


@pytest.mark.asyncio
async def test_cookbook_shell_cancel_during_spawn_keeps_handle(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    processes = []
    async def launch(source, **kwargs):
        if sys.platform != 'win32':
            kwargs['start_new_session'] = True
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        processes.append(proc)
        started.set()
        await release.wait()
        return proc
    monkeypatch.setattr(subprocess_tools, '_create_bash_subprocess', launch)
    task = asyncio.create_task(codex_routes._run_shell('unused'))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
