"""The pinned start notes (contract criteria, active plan) also survive the two
compaction paths, and a runtime note is never taken for the person's objective
(src/context_compactor.py `maybe_compact`, `compact_with_integrity`,
`_is_user_instruction_message`; src/context_ledger.py `classify`)."""
import asyncio

import pytest

from src import context_compactor as cc
from src.context_compactor import is_pinned_note

CONTRACT = {"role": "user", "_harness_note": True, "metadata": {"contract_criteria": True},
            "content": "[Contract criteria - automatic runtime message] 3 checkable requirements. "
                       "You must never skip C001."}
PLAN = {"role": "user", "_harness_note": True, "metadata": {"plan_note": True},
        "content": "Active plan \"X\" (abc): 0/11 done. Call plan_status."}
TASK = {"role": "user", "content": "Build the meter and show me the proof"}


def _pairs(n=8, size=60):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"question {i} " + "w " * size})
        out.append({"role": "assistant", "content": f"answer {i} " + "w " * size})
    return out


def _probe():
    return [{"role": "system", "content": "preamble"}, CONTRACT, PLAN, TASK] + _pairs()


def test_compact_with_integrity_never_folds_a_pinned_note():
    out, evidence = cc.compact_with_integrity(_probe(), keep_recent=4)
    assert evidence, "the old pairs are folded"
    contents = [m["content"] for m in out]
    assert CONTRACT["content"] in contents and PLAN["content"] in contents
    assert contents.index(CONTRACT["content"]) < contents.index(PLAN["content"])
    # the marker does not quote them either
    marker = next(m for m in out if (m.get("metadata") or {}).get("ctx02_compacted"))
    assert "Contract criteria" not in marker["content"]
    assert [m["role"] for m in out if is_pinned_note(m)] == ["user", "user"]


@pytest.mark.parametrize("mode", ["extract", "llm"])
def test_maybe_compact_sets_the_pinned_notes_aside_and_puts_them_back(monkeypatch, mode):
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: mode)
    monkeypatch.setattr(cc, "get_context_length", lambda *a, **k: 1000)

    async def _summary(rows, **kw):
        assert not any(is_pinned_note(r) for r in rows), "a pinned note reached the summary"
        return "SUMMARY"
    monkeypatch.setattr(cc, "summarize_rows", _summary)
    seen = {}
    real = cc.extractive_digest

    def _digest(rows):
        seen["rows"] = list(rows)
        return real(rows)
    monkeypatch.setattr(cc, "extractive_digest", _digest)

    out, _ctx, was = asyncio.run(cc.maybe_compact(
        None, "http://local/v1", "m", _probe(), {}, persist=False, force=True))
    assert was
    contents = [m["content"] for m in out]
    assert CONTRACT["content"] in contents and PLAN["content"] in contents
    assert contents.index(CONTRACT["content"]) < contents.index(PLAN["content"])
    summary = next(m for m in out if (m.get("metadata") or {}).get("compacted"))
    assert "Contract criteria" not in summary["content"]
    assert "Contract criteria" not in "".join(str(r.get("content")) for r in seen.get("rows", []))
    # the pinned notes come after the summary and before the kept half
    assert contents.index(summary["content"]) < contents.index(CONTRACT["content"])


def test_a_harness_note_is_not_the_compaction_objective_or_a_constraint():
    assert cc._is_user_instruction_message(TASK)
    assert not cc._is_user_instruction_message(CONTRACT)
    assert not cc._is_user_instruction_message(PLAN)
    assert not cc._is_user_instruction_message(
        {"role": "user", "content": "x", "_agent_injected": "nudge"})
    # the A14 objective of a fold that includes the note is the person's request
    out, _ = cc.compact_with_integrity(_probe(), keep_recent=4)
    marker = next(m for m in out if (m.get("metadata") or {}).get("ctx02_compacted"))
    objective = marker["metadata"]["compaction_preserve"]["objective"]
    assert "Build the meter" in objective and "Contract criteria" not in objective
    for c in marker["metadata"]["compaction_preserve"]["constraints"]:
        assert "must never skip" not in str(c)


def test_pinned_notes_count_as_system_in_the_context_ledger():
    from src.context_ledger import classify
    assert classify(CONTRACT) == "system" and classify(PLAN) == "system"
    assert classify(CONTRACT, is_last_user=True) == "system"
    assert classify(TASK, is_last_user=True) == "user"
    assert classify({"role": "user", "content": "plain"}) == "conversation"


def test_the_advisor_transcript_keeps_the_real_request_not_a_runtime_note():
    from src import advisor
    msgs = ([{"role": "system", "content": "s"}, CONTRACT, TASK]
            + _pairs(30, 400))
    out = advisor.build_transcript(msgs, budget_tokens=900)
    assert "Build the meter and show me the proof" in out["text"]
