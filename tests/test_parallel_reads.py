"""CALL-04: parallelism according to declared tool effects, inside
stream_agent_loop's own per-round tool-execution loop (src/agent_loop.py —
the "bucle de ejecucion de tools" this lot owns; tool_execution.py /
execute_tool_block itself is untouched).

A PREFIX of consecutive reads at the top of a round — effects entirely
READ_PUBLIC/READ_WORKSPACE/READ_PRIVATE per src/tool_capabilities.py, on
distinct resources, nothing requiring approval — runs concurrently via
asyncio.gather. A write (or anything else) ends the group right there, so a
mutation never overlaps a read that ran alongside it. Every tool_output SSE
event still comes out in call order regardless.

Revert the `_prefetched`/`_parallel_cap` block in src/agent_loop.py (the one
right before `for i, block in enumerate(tool_blocks):`) to see
`test_three_independent_reads_run_concurrently` fail: three 0.15s reads then
take ~0.45s together (all run one at a time) instead of ~0.15-0.25s, and the
"all started before the first one finished" assertion fails outright.
"""
import asyncio
import time

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events, _patch_common, _scripted_stream


def _timed_exec(delay, log):
    async def _exec(block, *a, **k):
        start = time.monotonic()
        log.append((block.tool_type, block.content, "start", start))
        await asyncio.sleep(delay)
        end = time.monotonic()
        log.append((block.tool_type, block.content, "end", end))
        return (block.tool_type, {"output": f"contents of {block.content}", "exit_code": 0})
    return _exec


def _tool_outputs(events):
    return [e for e in events if e.get("type") == "tool_output"]


def test_three_independent_reads_run_concurrently(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    log = []
    monkeypatch.setattr(al, "execute_tool_block", _timed_exec(0.15, log), raising=False)
    call = "\n".join(f'```read_file\n{{"path": "f{i}.py"}}\n```' for i in range(3))
    _scripted_stream(monkeypatch, [
        (call, "tool_calls"),
        ("Done. No files were changed.", "stop"),
    ])

    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Read the three files"}],
        max_rounds=4, relevant_tools={"read_file"}, workspace=str(tmp_path),
    )
    events = _events(_collect(gen))

    starts = sorted(t for (_tt, _c, kind, t) in log if kind == "start")
    ends = sorted(t for (_tt, _c, kind, t) in log if kind == "end")
    assert len(starts) == 3 and len(ends) == 3
    # The real proof of concurrency: all three started before the FIRST one
    # finished (wall time on the whole turn is not used here — round setup,
    # harness checks etc. add overhead of their own that has nothing to do
    # with whether the reads themselves overlapped).
    assert starts[-1] < ends[0], (starts, ends)
    # And the three calls' own span is close to ONE call's time, not three:
    # sequential would put ~0.45s between the earliest start and latest end.
    span = max(ends) - min(starts)
    assert span < 0.15 * 2, f"reads did not run concurrently: spanned {span:.3f}s"

    # Result order is unaffected by execution order: tool_output events still
    # come out call 0, 1, 2 — same as if every read had run one at a time.
    outputs = _tool_outputs(events)
    assert len(outputs) == 3
    for i, ev in enumerate(outputs):
        assert f'"path": "f{i}.py"' in ev["command"]
        assert f'f{i}.py' in ev["output"]


def test_write_between_two_reads_breaks_the_group(tmp_path, monkeypatch):
    """read(a), read(b), write(w), read(c) in one round: a/b are a genuine
    prefix group of >= 2 and DO run concurrently; the write ends the group
    right there — it only starts once BOTH a and b have finished, and the
    read AFTER it (c) is a separate, un-grouped call that only starts once
    the write itself has finished. Nothing here ever overlaps a write with
    a read on either side. (With keyed dispatch off: the legacy contract.)"""
    _patch_common(monkeypatch)
    _orig_get = al.get_setting
    monkeypatch.setattr(al, "get_setting",
                        lambda k, d=None: False if k == "agent_parallel_writes" else _orig_get(k, d))
    log = []
    monkeypatch.setattr(al, "execute_tool_block", _timed_exec(0.1, log), raising=False)
    call = (
        '```read_file\n{"path": "a.py"}\n```\n'
        '```read_file\n{"path": "b.py"}\n```\n'
        '```write_file\n{"path": "w.py", "content": "x = 1"}\n```\n'
        '```read_file\n{"path": "c.py"}\n```'
    )
    _scripted_stream(monkeypatch, [
        (call, "tool_calls"),
        ("Done.", "stop"),
    ])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Read a and b, write w, then read c"}],
        max_rounds=4, relevant_tools={"read_file", "write_file"}, workspace=str(tmp_path),
        security_gate_bypass=True,
    )
    events = _events(_collect(gen))
    outputs = _tool_outputs(events)
    assert len(outputs) == 4
    # Call order is preserved regardless of which ones ran concurrently.
    assert 'a.py' in outputs[0]["command"]
    assert 'b.py' in outputs[1]["command"]
    assert 'w.py' in outputs[2]["command"]
    assert 'c.py' in outputs[3]["command"]

    by_path = {}
    for tool_type, content, kind, t in log:
        by_path.setdefault(content, {})[kind] = t
    a = by_path['{"path": "a.py"}']
    b = by_path['{"path": "b.py"}']
    w = by_path['{"path": "w.py", "content": "x = 1"}']
    c = by_path['{"path": "c.py"}']

    # a and b ARE a real group: both started before either finished.
    assert max(a["start"], b["start"]) < min(a["end"], b["end"]), (a, b)
    # The write never starts before BOTH reads ahead of it have finished —
    # it is never run alongside a read.
    assert w["start"] >= a["end"] and w["start"] >= b["end"]
    # c — the read AFTER the write — never overlaps it either: the group
    # ends at the write, so c is not folded into anything and only starts
    # once the write has finished.
    assert c["start"] >= w["end"]


def _run_round(tmp_path, monkeypatch, call, delay=0.1, full_approval=True):
    _patch_common(monkeypatch)
    if full_approval:
        import src.tool_capabilities as _tc
        monkeypatch.setattr(_tc, "tool_approval_mode", lambda: "full")
    log = []
    monkeypatch.setattr(al, "execute_tool_block", _timed_exec(delay, log), raising=False)
    _scripted_stream(monkeypatch, [(call, "tool_calls"), ("Done.", "stop")])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Edit the files"}],
        max_rounds=4, relevant_tools={"read_file", "write_file", "edit_file"}, workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    by_path = {}
    for _tt, content, kind, t in log:
        by_path.setdefault(content, {})[kind] = t
    return events, by_path


def _overlap(x, y):
    return max(x["start"], y["start"]) < min(x["end"], y["end"])


def test_keyed_dispatch_runs_writes_to_different_files_side_by_side(tmp_path, monkeypatch):
    """Two edits to different files and a read of a third file: nothing can
    see anything else's effect, so all three run at once; results still come
    back in call order."""
    call = (
        '```write_file\n{"path": "w1.py", "content": "a = 1"}\n```\n'
        '```write_file\n{"path": "w2.py", "content": "b = 2"}\n```\n'
        '```read_file\n{"path": "r.py"}\n```'
    )
    events, p = _run_round(tmp_path, monkeypatch, call)
    w1 = p['{"path": "w1.py", "content": "a = 1"}']
    w2 = p['{"path": "w2.py", "content": "b = 2"}']
    r = p['{"path": "r.py"}']
    assert _overlap(w1, w2) and _overlap(w1, r), (w1, w2, r)
    outputs = _tool_outputs(events)
    assert [("w1.py" in o["command"], "w2.py" in o["command"], "r.py" in o["command"]) for o in outputs] == [
        (True, False, False), (False, True, False), (False, False, True)]


def test_keyed_dispatch_keeps_order_when_a_write_touches_what_was_read(tmp_path, monkeypatch):
    """read(a.py) then write(a.py): the write must see the file after the
    read, so it never overlaps it."""
    call = (
        '```read_file\n{"path": "a.py"}\n```\n'
        '```write_file\n{"path": "b.py", "content": "x"}\n```\n'
        '```write_file\n{"path": "a.py", "content": "y"}\n```'
    )
    _events_, p = _run_round(tmp_path, monkeypatch, call)
    ra = p['{"path": "a.py"}']
    wb = p['{"path": "b.py", "content": "x"}']
    wa = p['{"path": "a.py", "content": "y"}']
    assert _overlap(ra, wb)
    assert wa["start"] >= ra["end"] and wa["start"] >= wb["end"]


def test_path_overlap_helper():
    import os
    j = os.path.join
    assert al._paths_overlap(j("r", "a.py"), j("r", "a.py"))
    assert al._paths_overlap(j("r", "src"), j("r", "src", "x.py"))
    assert not al._paths_overlap(j("r", "src"), j("r", "srcx", "x.py"))
    assert al._is_parallel_safe_write("edit_file", '{"path": "a.py", "old": "a", "new": "b"}')
    assert not al._is_parallel_safe_write("bash", "echo hi > a.py")


def test_a_write_that_could_need_approval_is_never_run_ahead(tmp_path, monkeypatch):
    """Without blanket approval, a write the gate might stop once earlier
    results are in stays in the serial loop: here the first write's result
    arms the gate, the second write gets its approval card, and it never ran."""
    call = (
        '```write_file\n{"path": "w1.py", "content": "a = 1"}\n```\n'
        '```write_file\n{"path": "w2.py", "content": "b = 2"}\n```'
    )
    events, p = _run_round(tmp_path, monkeypatch, call, full_approval=False)
    assert '{"path": "w2.py", "content": "b = 2"}' not in p
    assert any(e.get("type") == "ask_user" for e in events)
