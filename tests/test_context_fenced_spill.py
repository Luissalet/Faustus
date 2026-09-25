"""Fenced-mode spill: one "tool execution results" user message per round,
spilled one result at a time, stubs reacquirable, wrapper intact."""
from __future__ import annotations

from src import context_overflow as co
from src import tool_result_segments as trs
from src.context_compactor import _row_fingerprint, spill_large_tool_results
from src.prompt_security import untrusted_context_message

import pytest


@pytest.fixture()
def overflow_root(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "OVERFLOW_DIR", str(tmp_path / "overflow"))
    return tmp_path


def _fenced(texts, round_num):
    joined, spans = trs.escape_results(texts)
    msg = untrusted_context_message("tool execution results", joined)
    trs.annotate(msg, joined, spans, round_num=round_num,
                 tools=["bash"] * len(texts), call_ids=[f"c{round_num}_{i}" for i in range(len(texts))])
    return msg


def _history(rounds=4):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do it"}]
    for r in range(1, rounds + 1):
        msgs.append({"role": "assistant", "content": f"```bash\nstep {r}\n```"})
        msgs.append(_fenced([f"### bash: step {r}\n" + ("out %d\n" % r) * 400, "### ls\nsmall"], r))
    return msgs


def test_old_fenced_results_spill_per_result_and_stay_parseable(overflow_root):
    msgs = _history(4)
    out, rep = spill_large_tool_results(msgs, session_id="s-f", keep_tool_rounds=2, spill_chars=100_000)
    assert rep["changed"] and rep["spilled"] == 2          # rounds 1 and 2, big result only
    for idx, rnd in ((3, 1), (5, 2)):
        msg = out[idx]
        assert trs.is_fenced_results(msg) and msg["metadata"]["overflow_spilled"]
        segs = trs.segments(msg)
        assert len(segs) == 2
        stub = trs.segment_text(msg, segs[0])
        assert stub.startswith("[overflow id=") and f"call_id=c{rnd}_0" in stub
        assert trs.segment_text(msg, segs[1]) == "### ls\nsmall"
        assert msg["content"].rstrip().endswith("<<<END_UNTRUSTED_SOURCE_DATA>>>")
    # recent rounds untouched
    assert out[7] is msgs[7] and out[9] is msgs[9]
    # every stub is reacquirable
    for oid in rep["overflow_ids"]:
        body = co.read_overflow(session_id="s-f", content_sha256=oid)["content"]
        assert body.startswith("### bash: step")
    assert set(rep["overflow_ids"]) <= co.referenced_overflow_ids(out)


def test_oversized_recent_fenced_result_spills_and_second_pass_is_idempotent(overflow_root):
    msgs = _history(1)
    out, rep = spill_large_tool_results(msgs, session_id="s-f", keep_tool_rounds=6, spill_chars=1000)
    assert rep["spilled"] == 1
    again, rep2 = spill_large_tool_results(out, session_id="s-f", keep_tool_rounds=6, spill_chars=1000)
    assert rep2["spilled"] == 0 and again is out


def test_fenced_message_without_offsets_spills_whole_body(overflow_root):
    body = "### bash: x\n" + "y\n" * 2000
    legacy = untrusted_context_message("tool execution results", body)   # no segments
    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "x"}, legacy,
            {"role": "assistant", "content": "y"}]
    out, rep = spill_large_tool_results(msgs, session_id="s-f", keep_tool_rounds=0, spill_chars=100_000)
    assert rep["spilled"] == 1
    span = trs.body_span(out[2])
    assert out[2]["content"][span[0]:span[1]].startswith("[overflow id=")
    assert co.read_overflow(session_id="s-f", content_sha256=rep["overflow_ids"][0])["content"] == body


def test_pinned_fenced_message_is_not_spilled(overflow_root):
    msgs = _history(3)
    pinned = {_row_fingerprint("user", msgs[3]["content"])}
    out, rep = spill_large_tool_results(msgs, session_id="s-f", keep_tool_rounds=0,
                                        spill_chars=100_000, pinned=pinned)
    assert out[3] is msgs[3]
    assert rep["spilled"] == 2


def test_stale_offsets_fall_back_to_whole_message():
    msg = _fenced(["### a\n" + "x" * 50, "### b\nshort"], 1)
    msg["content"] = msg["content"].replace("x" * 10, "z" * 10, 1)
    assert trs.segments(msg) == []
