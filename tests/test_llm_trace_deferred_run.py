import asyncio

import pytest

from src import llm_core as lc, llm_trace as tr


@pytest.fixture
def traces(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_traces_dir", lambda: str(tmp_path))
    monkeypatch.setattr(tr, "tracing_enabled", lambda: True)
    tr._SEQ_CACHE.clear()
    yield
    tr.flush_for_tests()


def read():
    tr.flush_for_tests()
    tr._SEQ_CACHE.clear()
    rows = tr.list_calls("session-A")
    full = [tr.get_call("session-A", r["seq"]) for r in rows]
    assert tr.list_calls("session-B") == []
    assert all(r["session_id"] == "session-A" for r in full)
    return rows, full


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["run-A", None])
@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("close_kind", ["explicit", "finalizer"])
async def test_stream_closed_in_new_run_keeps_entry_run_and_session(traces, monkeypatch, initial, error, close_kind):
    async def source(*a, **k):
        yield 'data: {"delta":"partial"}\n\n'
        if error:
            yield 'event: error\ndata: {"error":"fixture"}\n\n'
        yield 'data: {"delta":"unused"}\n\n'
    monkeypatch.setattr(lc, "_stream_llm_traced_source", source)
    token = tr.set_current_run_id(initial)
    try:
        stream = lc.stream_llm("https://fixture.invalid","fixture",[],session_id="session-A")
        await anext(stream)
        if error:
            await anext(stream)
    finally:
        tr.reset_current_run_id(token)
    token = tr.set_current_run_id("run-B")
    try:
        if close_kind == "explicit":
            await stream.aclose()
        else:
            await asyncio.get_running_loop().shutdown_asyncgens()
    finally:
        tr.reset_current_run_id(token)
    rows, full = read()
    assert len(rows) == 1
    assert rows[0]["run_id"] == full[0]["run_id"] == initial


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["run-A", None])
async def test_nonstream_impl_changes_run_without_reassigning_trace(traces, monkeypatch, initial):
    changed = []
    async def impl(*a, **kwargs):
        changed.append(tr.set_current_run_id("run-B"))
        return "answer"
    monkeypatch.setattr(lc, "_llm_call_async_impl", impl)
    token = tr.set_current_run_id(initial)
    try:
        assert await lc.llm_call_async("https://fixture.invalid","fixture",[],session_id="session-A") == "answer"
    finally:
        for inner in reversed(changed):
            tr.reset_current_run_id(inner)
        tr.reset_current_run_id(token)
    rows, full = read()
    assert rows[0]["run_id"] == full[0]["run_id"] == initial


@pytest.mark.parametrize("explicit", ["explicit-run", True, {"historical":"value"}])
def test_record_explicit_run_keeps_legacy_precedence(traces, explicit):
    token = tr.set_current_run_id("run-B")
    try:
        tr.record_call(session_id="session-A",run_id=explicit,_run_snapshot="run-A")
    finally:
        tr.reset_current_run_id(token)
    rows, full = read()
    assert rows[0]["run_id"] == full[0]["run_id"] == explicit


def test_direct_default_legacy_and_captured_unknown_differ(traces):
    token = tr.set_current_run_id("run-B")
    try:
        tr.record_call(session_id="session-A")
        tr.record_call(session_id="session-A",_run_snapshot=None)
    finally:
        tr.reset_current_run_id(token)
    rows, full = read()
    assert [r["run_id"] for r in rows] == ["run-B",None]
    assert [r["run_id"] for r in full] == ["run-B",None]
