"""The agent loop with contract criteria (src/contract_criteria.py, #1127):
driven by a scripted model like tests/test_unattended_conversation.py.

An unattended task with a long statement: the criteria are extracted at the
start, the model links one with `criteria_check`, the completion audit lists
the open ones, ONE `contract_gap` round follows, then the turn ends and the
summary/gate report what stayed open. Off, attended or short: as before."""
import asyncio
import json

import pytest

import src.agent_loop as al
from src.agent_tools import TOOL_HANDLERS

SPEC = (
    "# Archive tool\n\n"
    "Build a small archive tool for the team. " + "It packs project folders for the nightly backup job. " * 12 + "\n\n"
    "## Rules\n\n"
    "- `pack`, `unpack` and `scan` stream: their memory use must stay under 64 MB for any input size.\n"
    "- A damaged archive fails with `corrupt` and exit code 1.\n"
    "- Every command prints exactly one JSON document on stdout.\n"
    "- The tool must never delete the source files.\n\n"
    "## Background\n\n" + "The previous tool was a shell script that nobody maintained any more. " * 12 + "\n"
)


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _text(text, finish="stop"):
    return [
        f'data: {json.dumps({"delta": text})}\n\n',
        f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n',
        "data: [DONE]\n\n",
    ]


def _tool(name, body):
    return _text(f"```{name}\n{body}\n```", "tool_calls")


@pytest.fixture
def loop(monkeypatch, tmp_path):
    settings = {}
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: settings.get(key, default), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr("src.plan_tracker.PLAN_TRACKER_DIR", str(tmp_path / "tracker"))
    executed = []
    options = []

    async def _fake_exec(block, *a, **k):
        executed.append(block.tool_type)
        options.append(dict(k.get("turn_options") or {}))
        if block.tool_type == "criteria_check":
            ctx = {"contract_book": (k.get("turn_options") or {}).get("contract_book")}
            return block.tool_type, await TOOL_HANDLERS["criteria_check"](block.content, ctx)
        if block.tool_type == "bash":
            return block.tool_type, {"output": '{"error": {"code": "corrupt", "message": "crc"}}', "exit_code": 1}
        return block.tool_type, {"output": "src/arc/__init__.py", "exit_code": 0}
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    def run(rounds, *, unattended=True, user=SPEC, max_rounds=12, messages=None):
        seen = []

        async def _fake_stream(_candidates, messages, **kwargs):
            i = min(len(seen), len(rounds) - 1)
            seen.append([dict(m) for m in messages])
            for chunk in rounds[i]:
                yield chunk
        monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
        gen = al.stream_agent_loop(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b", messages or [{"role": "user", "content": user}],
            max_rounds=max_rounds, relevant_tools={"read_file", "glob", "bash"},
            workspace=str(tmp_path), unattended=unattended,
        )
        return seen, _events(_collect(gen))

    run.settings = settings
    run.executed = executed
    run.options = options
    return run


def _scripted_turn():
    # bash before anything untrusted was read: after that it needs approval.
    return [
        _tool("bash", "python -m arc unpack damaged.arc"),
        _tool("glob", json.dumps({"pattern": "src/**/*.py"})),
        _tool("criteria_check", json.dumps({"links": [{"id": "C04", "command": "unpack damaged.arc"}]})),
        _text("Done: the archive tool is ready."),
        _text("Checked every requirement listed."),
        _text("Final answer: pack/unpack/scan stream; damaged archives fail with corrupt."),
        _text("unexpected extra round"),
    ]


def _summary(events):
    return next(e["data"] for e in reversed(events) if e.get("type") == "harness_summary")


def test_unattended_contract_turn_audits_the_criteria_and_takes_one_gap_round(loop):
    seen, events = loop(_scripted_turn())
    start = [e for e in events if e.get("status") == "contract_criteria" and not e.get("final")]
    assert len(start) == 1 and start[0]["count"] >= 6, events
    assert start[0]["ids"][3] == "C04"
    # the compact list reached the model at the start, once
    first = json.dumps(seen[0], ensure_ascii=False)
    assert "[Contract criteria" in first and "C04 L" in first
    assert "criteria_check" in loop.executed
    # every tool call carries the turn's book and the paths changed so far
    assert all(o.get("contract_book") is not None for o in loop.options)
    assert all(isinstance(o.get("ledger_mutations"), list) for o in loop.options)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1 and audits[0]["criteria"] == start[0]["count"] - 1
    audit_text = seen[4][-1]["content"]
    # the generic audit is kept whole; the open criteria are appended to it
    assert audit_text.startswith(al.COMPLETION_AUDIT_NOTE + "\n\n")
    assert "C01" in audit_text and "[unpack]" in audit_text and "C04 L" not in audit_text
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(gaps) == 1, [e.get("reason") for e in events if e.get("type") == "harness_check"]
    assert gaps[0]["cause"] == "contract_gap" and gaps[0]["family"] == "completion"
    assert "C04" not in gaps[0]["pending"] and "C01" in gaps[0]["pending"]
    assert "still have no accepted evidence" in seen[5][-1]["content"]
    assert len(seen) == 6, len(seen)
    summary = _summary(events)
    crit = summary["criteria"]
    assert crit["proven"] == 1 and crit["ids"]["proven"] == ["C04"]
    assert crit["pending"] == crit["total"] - 1
    assert summary["stop_reason"] == "complete_unverified"
    assert "evidence_grade:criteria_unverified" in summary["notes"]
    assert any(n.startswith("contract_criteria:") for n in summary["notes"])
    final = [e for e in events if e.get("status") == "contract_criteria" and e.get("final")]
    assert final and final[0]["proven"] == 1


def test_audit_and_gap_round_fire_after_the_round_budget_was_extended(loop):
    # max_rounds is ONE grant (3 here); the turn's real budget grows with every
    # automatic extension. Round 4 is past the first grant, so a gate that still
    # compared with max_rounds would silently skip the audit and the gap round.
    rounds = [
        _tool("bash", "python -m arc unpack damaged.arc"),
        _tool("glob", json.dumps({"pattern": "src/**/*.py"})),
        _tool("glob", json.dumps({"pattern": "tests/**/*.py"})),
        _text("Done: the archive tool is ready."),
        _text("Checked every requirement listed."),
        _text("Final answer."),
        _text("unexpected extra round"),
    ]
    seen, events = loop(rounds, max_rounds=3)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(audits) == 1 and audits[0]["round"] == 4 and audits[0]["criteria"] == 6, events
    assert len(gaps) == 1 and gaps[0]["round"] == 5, [e.get("reason") for e in events if e.get("type") == "harness_check"]
    assert seen[4][-1]["content"].startswith(al.COMPLETION_AUDIT_NOTE)
    assert "still have no accepted evidence" in seen[5][-1]["content"]
    notes = _summary(events)["notes"]
    assert "completion_audit@4" in notes and "contract_gap@5" in notes
    assert any(n.startswith("auto_continue_rounds@") for n in notes), notes


def test_no_gate_of_the_loop_compares_the_round_with_the_first_grant():
    # `max_rounds` never changes; `_rounds_budget` is the turn's real budget
    # (src/agent_loop.py). A comparison with the grant is false after the first
    # automatic extension and turns the guarded recovery paths off.
    import re
    from pathlib import Path
    source = Path(al.__file__).read_text(encoding="utf-8")
    stale = re.findall(r"round_num\s*(?:<=?|>=?|==)\s*max_rounds\b|\bmax_rounds\s*(?:<=?|>=?)\s*round_num\b", source)
    assert stale == [], f"gate(s) compare the round with the first grant: {stale}"
    assert "max_rounds=max_rounds" not in source, "TextRoundState must get the real budget"
    assert "max_rounds=_rounds_budget" in source


def test_mass_not_applicable_keeps_the_audit_the_gap_round_and_the_gate(loop):
    links = [{"id": f"C{i:02d}", "status": "not_applicable", "reason": "covered by design"} for i in range(1, 30)]
    rounds = [
        _tool("glob", json.dumps({"pattern": "*.py"})),
        _tool("criteria_check", json.dumps({"links": links})),
        _text("Done."),
        _text("Checked."),
        _text("Final."),
        _text("unexpected extra round"),
    ]
    seen, events = loop(rounds)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1 and audits[0]["criteria"] == 6
    assert "Claimed not applicable" in seen[3][-1]["content"]
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(gaps) == 1 and gaps[0]["claimed_not_applicable"] == [f"C{i:02d}" for i in range(1, 7)]
    assert len(seen) == 5
    summary = _summary(events)
    assert summary["criteria"]["mass_not_applicable"] and summary["criteria"]["open_total"] == 6
    assert summary["stop_reason"] == "complete_unverified"


def test_no_gap_round_when_every_criterion_is_settled(loop):
    # lenient evidence: any real call of the turn proves; one not_applicable
    # (under a quarter) is accepted as settled.
    loop.settings["agent_contract_criteria_strict_evidence"] = False
    links = [{"id": f"C{i:02d}", "command": "unpack damaged"} for i in range(1, 6)]
    links.append({"id": "C06", "status": "not_applicable", "reason": "the tool never touches sources"})
    rounds = [
        _tool("bash", "python -m arc unpack damaged.arc"),
        _tool("criteria_check", json.dumps({"links": links})),
        _text("Done: the archive tool is ready."),
        _text("Checked every requirement listed."),
        _text("unexpected extra round"),
    ]
    seen, events = loop(rounds)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1 and "criteria" not in audits[0]
    assert seen[3][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert not [e for e in events if e.get("reason") == "contract_gap"]
    assert len(seen) == 4
    summary = _summary(events)
    assert summary["criteria"]["open_total"] == 0 and summary["criteria"]["proven"] == 5
    assert summary["stop_reason"] == "complete"


def test_gap_rounds_setting_zero_reports_only(loop):
    loop.settings["agent_contract_criteria_extra_rounds"] = 0
    seen, events = loop(_scripted_turn())
    assert len([e for e in events if e.get("reason") == "completion_audit"]) == 1
    assert not [e for e in events if e.get("reason") == "contract_gap"]
    assert len(seen) == 5
    assert _summary(events)["criteria"]["pending"] > 0


def test_gap_rounds_setting_is_clamped(loop):
    loop.settings["agent_contract_criteria_extra_rounds"] = 50
    rounds = _scripted_turn()[:5] + [_text("again")] * 8
    seen, events = loop(rounds, max_rounds=20)
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(gaps) == 3 and gaps[-1]["budget_limit"] == 3


def test_setting_off_keeps_the_generic_audit_and_no_criteria(loop):
    loop.settings["agent_contract_criteria"] = "off"
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds)
    assert not [e for e in events if e.get("status") == "contract_criteria" or e.get("reason") == "contract_gap"]
    assert seen[2][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert "[Contract criteria" not in json.dumps(seen[0])
    assert len(seen) == 3
    assert "criteria" not in _summary(events)


def test_short_message_changes_nothing(loop):
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds, user="Make /app/solve.py pass eval.py. It must exit 0. It must never print a trace.")
    assert not [e for e in events if e.get("status") == "contract_criteria"]
    assert seen[2][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert len(seen) == 3


def test_extraction_over_its_time_budget_skips_the_feature_for_the_turn(loop, monkeypatch):
    import time as _time
    from src import contract_criteria as cc

    def _slow(*a, **k):
        _time.sleep(0.6)
        return None
    monkeypatch.setattr(cc, "TIME_BUDGET_S", 0.05)
    monkeypatch.setattr(cc, "build_for_turn", _slow)
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds)
    skipped = [e for e in events if e.get("status") == "contract_criteria"]
    assert len(skipped) == 1 and skipped[0]["skipped"] == "timeout"
    assert seen[2][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert "contract_criteria:skipped_timeout" in _summary(events)["notes"]


def test_the_latest_message_is_read_even_with_list_content(loop):
    old = SPEC.replace("archive tool", "legacy tool").replace("`corrupt`", "`broken`")
    msgs = [{"role": "user", "content": old}, {"role": "assistant", "content": "Done."},
            {"role": "user", "content": [{"type": "text", "text": SPEC}]}]
    seen, events = loop(_scripted_turn(), messages=msgs)
    first = json.dumps(seen[0], ensure_ascii=False)
    assert "`corrupt`" in first.split("[Contract criteria", 1)[1][:3000]
    assert _summary(events)["criteria"]["ids"]["proven"] == ["C04"]


def test_attended_turn_extracts_with_always_but_takes_no_extra_round_and_no_gate(loop):
    loop.settings["agent_contract_criteria"] = "always"
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("unexpected extra round")]
    seen, events = loop(rounds, unattended=False)
    assert [e for e in events if e.get("status") == "contract_criteria" and not e.get("final")]
    assert not [e for e in events if e.get("reason") in ("completion_audit", "contract_gap")]
    assert "[Contract criteria" not in json.dumps(seen[0])
    assert len(seen) == 2
    summary = _summary(events)
    assert summary["criteria"]["open_total"] == 6 and not summary["criteria"]["shown"]
    assert "evidence_grade:criteria_unverified" not in summary["notes"]


def test_attended_turn_with_the_default_mode_is_untouched(loop):
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("unexpected extra round")]
    seen, events = loop(rounds, unattended=False)
    assert not [e for e in events if e.get("status") == "contract_criteria"]
    assert len(seen) == 2


def test_a_continue_turn_resumes_the_book_but_proven_must_be_earned_again(loop):
    loop.settings["agent_contract_criteria_extra_rounds"] = 0
    first_seen, first = loop(_scripted_turn())
    assert _summary(first)["criteria"]["ids"]["proven"] == ["C04"]
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds, user="continúa")
    start = [e for e in events if e.get("status") == "contract_criteria" and not e.get("final")]
    assert len(start) == 1 and start[0]["resumed"] and start[0]["open"] == start[0]["count"]
    crit = _summary(events)["criteria"]
    assert crit["ids"]["proven"] == [] and "C04" in crit["ids"]["pending"]
