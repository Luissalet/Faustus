"""Functional verification inside stream_agent_loop: checkpoint before the
first change, the project's tests after the turn (one fix round), the
independent diff review (one fix round), the scorecard line, and the verified
card carrying all of it. Same mocking pattern as test_agent_harness_loop.py."""

import asyncio
import json
import os

import pytest

import src.agent_loop as al
from src import workspace_checkpoints as wc


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


def _patch_common(monkeypatch, settings=None, tool_exec=None):
    settings = dict(settings or {})
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: settings.get(key, default), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        if tool_exec is not None:
            r = tool_exec(block)
            if r is not None:
                return (block.tool_type, r)
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _scripted_stream(monkeypatch, rounds):
    calls = {"n": 0, "messages": []}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        calls["messages"].append([dict(m) for m in messages])
        text, finish = rounds[i]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return calls


def _run(workspace, user="Arregla la función add en src/calc.py", harness_options=None, max_rounds=8):
    # A trusted workspace (project flag): the second write of a turn is not
    # parked behind the "Allow this task to continue?" gate. Also what these
    # scenarios rely on to make several edits in one turn.
    opts = {"trusted_workspace": os.path.realpath(workspace)}
    opts.update(harness_options or {})
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": user}],
        max_rounds=max_rounds,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=workspace,
        session_id="sess-func",
        harness_options=opts,
    )
    return _events(_collect(gen))


def _edit_call(path, old, new):
    return "```edit_file\n" + json.dumps({"path": path, "old_string": old, "new_string": new}) + "\n```"


def _real_edit(workspace):
    """A fake edit_file that really rewrites the file (so tests/diffs see it)."""
    def _exec(block):
        if block.tool_type != "edit_file":
            return None
        args = json.loads(block.content)
        p = os.path.join(workspace, args["path"])
        text = open(p, encoding="utf-8").read()
        if args["old_string"] not in text:
            return {"error": "old_string not found", "exit_code": 1}
        open(p, "w", encoding="utf-8").write(text.replace(args["old_string"], args["new_string"], 1))
        return {"output": f"Edited {args['path']} (1 replacement)", "exit_code": 0, "diff": {"added": 1, "removed": 1}}
    return _exec


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A tiny pytest project: src/calc.py + tests/test_calc.py."""
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "data"))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "tests" / "test_calc.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    return str(ws)


def test_checkpoint_then_tests_fail_then_fix_round_then_verified(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": True}, tool_exec=_real_edit(project))
    # Round 1: a wrong "fix" (still broken). Round 2 (after the tests_failed
    # nudge): the real fix. Round 3: done.
    calls = _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a * b"), "tool_calls"),
        ("He corregido src/calc.py.", "stop"),
        (_edit_call("src/calc.py", "return a * b", "return a + b"), "tool_calls"),
        ("He corregido src/calc.py de verdad.", "stop"),
    ])
    events = _run(project)
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "checkpoint" in statuses, statuses
    assert "tests_running" in statuses and "tests_failed" in statuses, statuses
    assert statuses[-1] == "verified", statuses
    failed = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "tests_failed")
    assert failed["tests"]["ok"] is False and failed["tests"]["kind"] == "pytest"
    assert failed["tests"]["scope"] == "related" and failed["tests"]["related_files"] == ["tests/test_calc.py"]
    # The model got the failure text as a runtime message, not as user prose.
    fix_prompt = calls["messages"][2][-1]["content"]
    assert "tests FAILED" in fix_prompt and "test_calc.py::test_add" in fix_prompt
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["tests"]["ok"] is True and verified["checkpoint"]
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["tests"]["ok"] is True and summary["tests_fix_rounds"] == 1
    assert summary["checkpoint"] == verified["checkpoint"]
    assert summary["stop_reason"] == "complete"
    # The checkpoint really holds the pre-turn content and sees the change.
    assert wc.file_at(project, summary["checkpoint"], "src/calc.py").replace(b"\r\n", b"\n") == b"def add(a, b):\n    return a - b\n"
    assert [c["path"] for c in wc.changed_since(project, summary["checkpoint"])] == ["src/calc.py"]
    metrics = next(e for e in events if e.get("type") == "metrics")["data"]
    assert metrics["harness"]["tests"]["ok"] is True and metrics["harness"]["checkpoint"]
    # Scorecard line written.
    from src import scorecard
    rows = scorecard.load()
    assert rows and rows[-1]["model"] == "qwen3-coder:30b" and rows[-1]["tests"] == "pass"
    assert rows[-1]["tests_fix_rounds"] == 1 and rows[-1]["files_changed"] == 1 and rows[-1]["verified"] is True


def test_tests_still_failing_after_fix_round_is_reported_not_looped(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": True}, tool_exec=_real_edit(project))
    calls = _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a * b"), "tool_calls"),
        ("Hecho: he corregido src/calc.py.", "stop"),
        ("Sigo pensando que src/calc.py está bien así.", "stop"),
    ])
    events = _run(project)
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert statuses.count("tests_failed") == 1, statuses
    assert statuses[-1] == "verified"
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["tests"]["ok"] is False
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert any(n.startswith("tests_failed:") for n in summary["notes"])
    assert calls["n"] == 3


def test_no_test_runner_means_no_tests_card(tmp_path, monkeypatch):
    monkeypatch.setattr(__import__("src.constants", fromlist=["DATA_DIR"]), "DATA_DIR", str(tmp_path / "data"), raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch, tool_exec=_real_edit(str(ws)))
    _scripted_stream(monkeypatch, [
        (_edit_call("a.py", "x = 1", "x = 2"), "tool_calls"),
        ("He cambiado a.py.", "stop"),
    ])
    events = _run(str(ws), user="Cambia x a 2 en a.py")
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "tests_running" not in statuses and "tests_failed" not in statuses
    assert statuses[-1] == "verified"
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["tests"] is None


def test_auto_review_flags_a_defect_then_accepts_the_fix(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": False, "agent_auto_review": "same"},
                  tool_exec=_real_edit(project))
    reviews = iter([
        json.dumps({"verdict": "issues", "summary": "wrong operator",
                    "findings": [{"severity": "error", "file": "src/calc.py", "line": 2, "issue": "add() multiplies",
                                  "evidence": "return a * b"}]}),
        json.dumps({"verdict": "ok", "summary": "looks right", "findings": []}),
    ])
    seen_prompts = []

    async def _fake_llm(url, model, messages, **kwargs):
        seen_prompts.append(messages[-1]["content"])
        return next(reviews)
    import src.llm_core as lc
    monkeypatch.setattr(lc, "llm_call_async", _fake_llm, raising=False)
    calls = _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a * b"), "tool_calls"),
        ("He corregido src/calc.py.", "stop"),
        (_edit_call("src/calc.py", "return a * b", "return a + b"), "tool_calls"),
        ("Corregido de verdad.", "stop"),
    ])
    events = _run(project)
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "review_running" in statuses and "review_issues" in statuses and statuses[-1] == "verified", statuses
    issues = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "review_issues")
    assert issues["review"]["findings"][0]["issue"] == "add() multiplies"
    # The reviewer saw the diff of the turn (from the checkpoint), not prose.
    assert "-    return a - b" in seen_prompts[0] and "+    return a * b" in seen_prompts[0]
    fix_prompt = calls["messages"][2][-1]["content"]
    assert "independent review" in fix_prompt and "src/calc.py:2" in fix_prompt
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["review"]["verdict"] == "ok"
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["review_fix_rounds"] == 1 and summary["review"]["model"] == "qwen3-coder:30b"
    from src import scorecard
    assert scorecard.load()[-1]["review"] == "ok"


def test_review_dispute_when_the_agent_disagrees_and_changes_nothing(project, monkeypatch):
    """A grounded error finding gets its fix round; the agent checks, disagrees
    and edits nothing → the review is marked disputed (no red 'defects' note)."""
    _patch_common(monkeypatch, settings={"agent_project_tests": False, "agent_auto_review": "same"},
                  tool_exec=_real_edit(project))
    calls_n = {"n": 0}

    async def _fake_llm(url, model, messages, **kwargs):
        calls_n["n"] += 1
        return json.dumps({"verdict": "issues", "summary": "wrong operator?",
                           "findings": [{"severity": "error", "file": "src/calc.py", "line": 2, "issue": "should subtract",
                                         "evidence": "return a + b"}]})
    import src.llm_core as lc
    monkeypatch.setattr(lc, "llm_call_async", _fake_llm, raising=False)
    _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a + b"), "tool_calls"),
        ("He corregido src/calc.py: ahora suma.", "stop"),
        ("El revisor se equivoca: la petición pide sumar, así que no cambio nada.", "stop"),
    ])
    events = _run(project)
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "review_issues" in statuses and statuses[-1] == "verified"
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["review"]["disputed"] is True and verified["review"]["findings"][0]["grounded"] is True
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["review_fix_rounds"] == 1
    assert any(n.startswith("review_disputed:1") for n in summary["notes"]) and not any(n.startswith("review_defects") for n in summary["notes"])


def test_ungrounded_review_errors_do_not_cost_a_fix_round(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": False, "agent_auto_review": "same"},
                  tool_exec=_real_edit(project))

    async def _fake_llm(url, model, messages, **kwargs):
        return json.dumps({"verdict": "issues", "summary": "imagined",
                           "findings": [{"severity": "error", "file": "src/calc.py", "line": 7, "issue": "a button placed after",
                                         "evidence": "<button>Refresh</button>"}]})
    import src.llm_core as lc
    monkeypatch.setattr(lc, "llm_call_async", _fake_llm, raising=False)
    calls = _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a + b"), "tool_calls"),
        ("He corregido src/calc.py.", "stop"),
    ])
    events = _run(project)
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "review_issues" not in statuses and statuses[-1] == "verified"
    assert calls["n"] == 2   # no fix round was requested
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["review"]["verdict"] == "ok" and verified["review"]["ungrounded"] == 1


def test_review_mode_flag_reaches_the_card_and_metrics(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": False}, tool_exec=_real_edit(project))
    _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a + b"), "tool_calls"),
        ("Listo, src/calc.py corregido.", "stop"),
    ])
    events = _run(project, harness_options={"review_mode": True, "project_id": "p1"})
    verified = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "verified")
    assert verified["review_mode"] is True
    metrics = next(e for e in events if e.get("type") == "metrics")["data"]
    assert metrics["harness"]["review_mode"] is True and metrics["harness"]["project_id"] == "p1"


def test_checkpoints_can_be_disabled_per_project(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": False}, tool_exec=_real_edit(project))
    _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "return a - b", "return a + b"), "tool_calls"),
        ("Listo.", "stop"),
    ])
    events = _run(project, harness_options={"checkpoints": False})
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "checkpoint" not in statuses
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["checkpoint"] is None


def test_repo_map_is_injected_once_before_the_user_message(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": False})
    calls = _scripted_stream(monkeypatch, [("No hace falta cambiar nada.", "stop")])
    _run(project, user="¿Qué hace calc.py?")
    msgs = calls["messages"][0]
    maps = [m for m in msgs if isinstance(m.get("content"), str) and "Repository map of the workspace" in m["content"]]
    assert len(maps) == 1
    assert "src/calc.py: def add" in maps[0]["content"]
    assert msgs.index(maps[0]) < len(msgs) - 1 and msgs[-1]["role"] == "user"  # before the user's message
    assert maps[0].get("metadata", {}).get("tool_gate_untrusted") is False


def test_agents_md_is_part_of_the_system_prompt(project, monkeypatch):
    (os.path.join(project, "AGENTS.md"))
    with open(os.path.join(project, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("# Rules\nRun `pytest -q` before finishing. Never touch tests/.\n")
    _patch_common(monkeypatch, settings={"agent_project_tests": False})
    calls = _scripted_stream(monkeypatch, [("Nada que cambiar.", "stop")])
    _run(project, user="¿Qué hace calc.py?")
    system = "\n".join(m["content"] for m in calls["messages"][0] if m.get("role") == "system" and isinstance(m.get("content"), str))
    assert "Project instructions from AGENTS.md" in system
    assert "Never touch tests/." in system
