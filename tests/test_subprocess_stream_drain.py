"""Real disposable processes: pipe drainage is independent of line length."""
import asyncio
import os
import subprocess
import sys

import pytest

from src.agent_tools import subprocess_tools as st


async def spawn(code):
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
               else {"start_new_session": True})
    return await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", code, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **options)


async def run(code, **kwargs):
    proc = await spawn(code)
    try:
        return await st._run_subprocess_streaming(proc, timeout=8, idle_timeout=0, **kwargs)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_megabyte_without_newline_completes_and_marks_truncation(channel):
    out, err, rc, timed_out = await run(
        f"import sys; sys.{channel}.write('x' * 1000000); sys.{channel}.flush()")
    captured = out if channel == "stdout" else err
    assert rc == 0 and timed_out is False
    assert captured.startswith("x" * 100)
    assert captured.endswith(st._STREAM_TRUNCATION)
    assert len(captured) <= st.MAX_OUTPUT_CHARS
    assert (err if channel == "stdout" else out) == ""


async def test_small_output_and_exit_are_preserved():
    out, err, rc, timed_out = await run(
        "import sys; sys.stdout.buffer.write(b'first\\n\\nlast\\n'); "
        "sys.stderr.buffer.write(b'warning\\r\\n'); sys.exit(7)")
    assert (out, err, rc, timed_out) == ("first\n\nlast", "warning\r", 7, False)


async def test_both_large_pipes_drain_in_same_process():
    out, err, rc, timed_out = await run(
        "import sys\nfor i in range(100):\n"
        " sys.stdout.write('x'*10000); sys.stdout.flush()\n"
        " sys.stderr.write('y'*10000); sys.stderr.flush()")
    assert rc == 0 and timed_out is False
    assert out.startswith("xxx") and err.startswith("yyy")
    for captured in (out, err):
        assert captured.endswith(st._STREAM_TRUNCATION)
        assert len(captured) <= st.MAX_OUTPUT_CHARS


async def test_incomplete_utf8_at_eof_keeps_replacement_policy():
    out, err, rc, timed_out = await run("import sys; sys.stdout.buffer.write(b'ok\\xf0\\x9f')")
    assert (out, err, rc, timed_out) == ("ok\ufffd", "", 0, False)


async def test_utf8_split_between_reads_is_not_corrupted(monkeypatch):
    monkeypatch.setattr(st, "_STREAM_CHUNK_BYTES", 1)
    text = "á日本🙂"
    out, err, rc, timed_out = await run(f"import sys; sys.stdout.buffer.write({text.encode()!r})")
    assert (out, err, rc, timed_out) == (text, "", 0, False)


async def test_output_without_newline_counts_as_activity():
    proc = await spawn("import sys,time\nfor i in range(15):\n sys.stdout.write('.'); sys.stdout.flush(); time.sleep(.1)")
    try:
        out, err, rc, timed_out = await st._run_subprocess_streaming(proc, timeout=8, idle_timeout=.6)
        assert (out, err, rc, timed_out) == ("." * 15, "", 0, False)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_cancel_kills_only_owned_fixture_and_releases_tracking(tmp_path):
    ready = tmp_path / "ready"
    proc = await spawn(f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(30)")
    task = asyncio.create_task(st._run_subprocess_streaming(proc, timeout=40, idle_timeout=0))
    try:
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(.02)
        assert ready.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode is not None
        assert st.process_ownership.spawn_creation_time(proc.pid) is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_progress_tail_is_bounded_without_newlines(monkeypatch):
    monkeypatch.setattr(st, "PROGRESS_INTERVAL_S", .02)
    events = []

    async def progress(event):
        events.append(event)

    await run("import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(.15)", progress_cb=progress)
    assert events
    assert max(len(event["tail"]) for event in events) <= st._STREAM_TAIL_CHARS


def test_retained_memory_is_bounded_even_after_many_chunks():
    capture = st._BoundedStreamText()
    for _ in range(1000):
        capture.append("x" * 8192)
        assert len(capture.text) <= st.MAX_OUTPUT_CHARS
    assert capture.result().endswith(st._STREAM_TRUNCATION)
