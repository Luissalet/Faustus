import asyncio
import gc

import pytest

pytestmark = pytest.mark.usefixtures("code_mode_host_runtime")

from src.code_mode import bridge, runner


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_dispatch", [False, True])
async def test_cancel_reaps_real_owned_child_and_protocol_tasks(tmp_path, monkeypatch, blocked_dispatch):
    guest = tmp_path / "guest.py"
    guest.write_text(
        "import json,sys,time\n"
        "json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type':'call','call_id':'ready','tool':'fixture','args':{}}),flush=True)\n"
        "sys.stdin.readline()\n"
        "time.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_GUEST_PATH", str(guest))
    monkeypatch.setattr(runner, "_limits", lambda: {"timeout_seconds": 60,
        "max_calls": 3, "max_output_bytes": 1000, "max_memory_bytes": 0})
    ready = asyncio.Event()
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def tracked_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    async def dispatch(*args, **kwargs):
        ready.set()  # A real guest has consumed configuration and sent RPC.
        if blocked_dispatch:
            await asyncio.Future()
        return {"output": "ready", "exit_code": 0}

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", tracked_spawn)
    monkeypatch.setattr(bridge, "dispatch_call", dispatch)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(runner.run_code_mode("pass"))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        assert len(processes) == 1
        assert processes[0].returncode is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert task.cancelled()
        assert processes[0].returncode is not None
        remaining = [pending for pending in asyncio.all_tasks() - baseline if not pending.done()]
        assert remaining == []
        gc.collect()
    finally:
        # A failing regression still cleans up only this fixture's process.
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        leftovers = [pending for pending in asyncio.all_tasks() - baseline if not pending.done()]
        for pending in leftovers:
            pending.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)
