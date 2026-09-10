"""EVAL-03: adversarial-protocol suite.

Four stimuli the lot names that are NOT already covered elsewhere (the other
two the lot lists — a stream split mid-multibyte-character, and a server
restart mid-research — are marked "(ya)" in the lot text itself and are
exactly what `tests/qa/test_qa_05_json_utf8_fragmentados.py` and
`tests/qa/test_qa_10_reinicio_en_research.py` already prove; this file does
not repeat them). Each test below drives a REAL, existing module — never a
mock standing in for one this lot owns — and checks the one invariant EVAL-03
names: the stimulus never reads as a false "success", and no state already
held is silently discarded.
"""
from __future__ import annotations

import errno
import os

import pytest

import src.agent_tools  # noqa: F401 - import order src/tool_parsing.py needs
                          # to avoid the circular import its own module
                          # docstring does not mention (see the lot report).
from src.tool_parsing import parse_tool_blocks


# ---------------------------------------------------------------------------
# 1. A provider that describes a tool call in prose instead of calling one
# ---------------------------------------------------------------------------

def test_a_prose_description_of_a_tool_call_is_never_parsed_as_one():
    """A model that NARRATES an action ("voy a ejecutar edit_file con
    path=calc.py...") instead of emitting the fenced/JSON/XML syntax
    `src/tool_parsing.py` actually recognises must never be treated as a real
    call — an agent_loop that executed prose would run whatever a model
    merely talked about, with no argument validation at all."""
    prose_en = ("I will now run the bash tool to delete the old files: "
                "bash rm -rf /tmp/x and then confirm it worked.")
    prose_es = ("Voy a ejecutar edit_file con path calc.py, old_string "
                "'return a - b' y new_string 'return a + b' para arreglarlo.")
    assert parse_tool_blocks(prose_en) == []
    assert parse_tool_blocks(prose_es) == []
    # The real, correctly-fenced form of the SAME action is still recognised —
    # proving the empty list above is about the missing syntax, not about
    # `parse_tool_blocks` refusing to work at all.
    fenced = '```edit_file\n{"path": "calc.py", "old_string": "a", "new_string": "b"}\n```'
    real = parse_tool_blocks(fenced)
    assert len(real) == 1 and real[0].tool_type == "edit_file"


# ---------------------------------------------------------------------------
# 2. 429 with an absurd Retry-After
# ---------------------------------------------------------------------------

def test_an_absurd_retry_after_is_capped_not_honoured_verbatim():
    """A provider (or a hostile proxy in front of one) sending
    `Retry-After: 99999999` must not stall a retry loop for over three years:
    `src/retry_policy.py::delay` is the single place that turns a
    Retry-After into a sleep, and it must clamp to `cap` regardless of what
    the header said."""
    from src import retry_policy

    headers = {"Retry-After": "99999999"}
    retry_after = retry_policy.parse_retry_after(headers)
    assert retry_after == 99999999.0  # parsed faithfully — the header is a fact
    waited = retry_policy.delay(1, retry_after=retry_after, cap=60.0)
    assert waited == 60.0  # ...but never HONOURED past the caller's cap
    # And the classification itself is still "retry", never "give up and
    # silently move on" — the caller decides to wait less, not to pretend
    # the request succeeded.
    assert retry_policy.classify_http(status=429) == retry_policy.RetryClass.RETRY_NOW


# ---------------------------------------------------------------------------
# 3. Disk full while writing an artifact
# ---------------------------------------------------------------------------

def test_disk_full_while_publishing_an_artifact_never_reports_success(tmp_path, monkeypatch):
    """`src/artifact_store.py::publish_copy` fsyncs the temp file before it is
    linked into the store; a disk that fills up on that fsync must abort the
    publish (raise), not link a truncated file in and call it published."""
    from src import artifact_store

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "out.txt").write_text("artifact bytes\n", encoding="utf-8")
    store_dir = tmp_path / "store"

    def _enospc(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(artifact_store.os, "fsync", _enospc)

    with pytest.raises(OSError):
        artifact_store.publish_copy(str(source_dir / "out.txt"), str(store_dir), "out.txt")
    # No half-written blob was left behind under a name a reader could find:
    # the temp file this raised out of is cleaned up by publish_copy's own
    # `finally`, and nothing else in the store directory claims to be it.
    assert not any(os.scandir(store_dir)) if store_dir.exists() else True

    # The same failure through `collect()` (what a real run actually calls):
    # a batch that could not be published must not come back as a `Collected`
    # claiming the file was made.
    from src.contracts.execution import ExecutionResult
    result = ExecutionResult(run_id="r1", backend="test", status="done",
                             artifact_filenames=("out.txt",))
    with pytest.raises(OSError):
        artifact_store.collect(result, source_dir=str(source_dir), store_dir=str(store_dir))


# ---------------------------------------------------------------------------
# 4. An MCP server that answers with garbage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_garbage_response_returns_an_error_not_a_false_success(monkeypatch):
    """`McpManager.call_tool` wraps every transport call: a server that
    answers with bytes that do not parse as its protocol must come back as
    `{"error": ..., "exit_code": 1}`, never as a result the agent loop would
    read as a completed tool call."""
    import json as _json

    from src.mcp_manager import McpManager

    mgr = McpManager()

    async def _garbage(self, session, tool_name, arguments):
        # What a JSON-RPC transport raises when a peer sends bytes that do
        # not parse — the realistic shape of "an MCP server answered with
        # garbage", whether that garbage arrived over stdio or SSE.
        raise _json.JSONDecodeError("garbage from the server", "not json", 0)

    mgr._sessions["junk_server"] = object()  # any truthy session: presence is all call_tool checks
    monkeypatch.setattr(McpManager, "_do_call", _garbage)
    result = await mgr.call_tool("mcp__junk_server__whatever", {"a": 1})

    assert isinstance(result, dict)
    assert result.get("error")
    assert result.get("exit_code") == 1
    assert "stdout" not in result  # nothing masquerading as real tool output
