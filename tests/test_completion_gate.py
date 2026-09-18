"""Hard completion gate (Silhouettes measure #1).

The forensic series had ≥14 of 24 chats sealed `complete` with a red test,
a `contradicted` changeset or a UI that did not load. Whatever the model
writes, such a turn now closes `complete_unverified`. Pure-function tests
plus one end-to-end turn through `stream_agent_loop`.
"""

from __future__ import annotations

import json

from src import agent_harness as h


def _summary(**kw):
    base = {"stop_reason": "complete", "mutations": ["src/a.py"], "tests": None, "ui_smoke": None}
    base.update(kw)
    return base


def test_clean_turn_is_not_gated():
    assert h.completion_gate(_summary(tests={"ran": True, "ok": True}), {"verdict": "proved"}) is None
    assert h.completion_gate(_summary(mutations=[]), None) is None


def test_new_test_failures_never_close_complete():
    tests = {"ran": True, "ok": False, "inconclusive": False, "pre_existing_only": False}
    assert h.completion_gate(_summary(tests=tests), None) == "tests_failed"


def test_pre_existing_only_and_inconclusive_are_not_gated():
    assert h.completion_gate(_summary(tests={"ran": True, "ok": False, "pre_existing_only": True}), None) is None
    assert h.completion_gate(_summary(tests={"ran": True, "ok": False, "inconclusive": True}), None) is None
    assert h.completion_gate(_summary(tests={"ran": False, "ok": None}), None) is None


def test_ui_smoke_failure_is_gated():
    assert h.completion_gate(_summary(ui_smoke={"ran": True, "ok": False}), None) == "ui_smoke_failed"
    assert h.completion_gate(_summary(ui_smoke={"ran": False}), None) is None


def test_contradicted_changeset_is_gated_and_unproved_only_with_claims():
    assert h.completion_gate(_summary(), {"verdict": "contradicted"}) == "changeset_contradicted"
    assert h.completion_gate(_summary(mutations=[]), {"verdict": "unproved", "unsupported_claims": [{"path": "x"}]}) == "changeset_unproved"
    # `unproved` with nothing claimed is the honest "nothing to prove", not a failure.
    assert h.completion_gate(_summary(mutations=[]), {"verdict": "unproved", "unsupported_claims": []}) is None
    assert h.completion_gate(_summary(), {"verdict": "partial"}) is None


def test_gate_only_ever_removes_a_complete():
    tests = {"ran": True, "ok": False}
    assert h.completion_gate(_summary(stop_reason="awaiting_user", tests=tests), None) is None
    assert h.completion_gate(_summary(stop_reason="complete_unverified", tests=tests), None) is None


def test_gate_never_raises_on_garbage():
    assert h.completion_gate({"stop_reason": "complete", "tests": "nope", "ui_smoke": 3}, "x") is None


def test_red_test_turn_closes_complete_unverified_end_to_end(tmp_path, monkeypatch):
    """The #13/#8 shape: one red test the model shrugs off. The `verified`
    card carries the gate reason and the summary is `complete_unverified`."""
    from tests.test_agent_harness_functional import (
        _patch_common, _scripted_stream, _run, _real_edit, _edit_call,
    )
    monkeypatch.setenv("FAUSTUS_DATA_DIR", str(tmp_path / "data"))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    project = str(ws)
    _patch_common(monkeypatch, settings={"agent_project_tests": True, "agent_ui_smoke": False},
                  tool_exec=_real_edit(project))
    _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a * b"), "tool_calls"),
        ("Hecho: he corregido src/calc.py.", "stop"),
        ("Sigo pensando que src/calc.py está bien así.", "stop"),
    ])
    events = _run(project)
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["tests"]["ok"] is False
    assert verified["gate"] == "tests_failed"
    gated = [e for e in events if e.get("type") == "harness_check" and e["status"] == "completion_gated"]
    assert len(gated) == 1 and gated[0]["reason"] == "tests_failed"
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["stop_reason"] == "complete_unverified"
    assert "hard_gate:tests_failed" in summary["notes"]
    assert json.dumps(summary)  # serialisable


def test_agent_loop_calls_the_gate_before_recording_the_changeset():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("src", "agent_loop.py").read_text(encoding="utf-8")
    gate_at = src.index("_harness.completion_gate(_hsum")
    record_at = src.index("from src.changeset_store import record_turn as _record_changeset")
    assert gate_at < record_at
