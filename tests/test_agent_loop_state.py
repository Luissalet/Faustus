"""Agent loop state: phases from real stream events, steps and the practice checklist."""
import json

from src import agent_loop_state as loop


def _practice(snapshot, pid):
    return next(p for p in snapshot["practices"] if p["id"] == pid)


def test_phases_follow_events_and_laps_follow_rounds():
    state = loop.LoopState("s1", "r1", 100.0)
    assert state.snapshot(100.0)["phase"] == "plan"
    state.feed({"delta": "thinking about it", "thinking": True}, 101.0)
    state.feed({"type": "tool_start", "tool": "bash", "command": "pytest -q", "round": 1}, 102.0)
    snap = state.snapshot(102.5)
    assert (snap["phase"], snap["tool"], snap["lap"]) == ("act", "bash", 1)
    state.feed({"type": "tool_output", "tool": "bash", "exit_code": 0, "output": "3 passed"}, 104.0)
    assert state.snapshot(104.0)["phase"] == "observe"
    state.feed({"type": "agent_step", "round": 2}, 105.0)
    snap = state.snapshot(105.0)
    assert (snap["phase"], snap["lap"]) == ("observe", 2)
    state.feed({"delta": "\n\n", "thinking": False}, 105.2)
    assert state.snapshot(105.2)["phase"] == "observe"
    state.feed({"delta": "it passed", "thinking": False}, 105.5)
    assert state.snapshot(105.5)["phase"] == "plan"
    state.feed({"type": "metrics", "data": {"input_tokens": 1200, "output_tokens": 80}}, 106.0)
    state.finish("done", 107.0)
    snap = state.snapshot(200.0)
    assert snap["phase"] == "done" and not snap["running"]
    assert snap["elapsed_s"] == 7.0
    assert snap["tokens"] == {"input": 1200, "output": 80}
    assert [s["kind"] for s in snap["steps"]] == ["plan", "tool", "result", "plan", "end"]


def test_no_movement_without_events():
    state = loop.LoopState("s1", "r1", 0.0)
    state.feed({"type": "tool_start", "tool": "bash", "command": "sleep 60"}, 1.0)
    assert state.snapshot(500.0)["phase"] == "act"


def test_practices_marked_from_what_the_calls_ran(monkeypatch):
    monkeypatch.setenv("FAUSTUS_GIT_AUTHOR", "sample-maintainer")
    state = loop.LoopState("s1", "r1", 0.0)
    calls = [
        ("bash", "python -m pytest tests/test_x.py -q", 0),
        ("browser_navigate", "http://127.0.0.1:7012", 0),
        ("edit_file", "FAUSTUS.md", 0),
        ("bash", 'git add src/x.py FAUSTUS.md && git commit --author="sample-maintainer <l@x>" -m "Add x"', 0),
        ("bash", "agora.cmd --space mejoras submit 12 --commits abc", 0),
    ]
    for i, (tool, command, code) in enumerate(calls):
        state.feed({"type": "tool_start", "tool": tool, "command": command}, i + 0.1)
        state.feed({"type": "tool_output", "tool": tool, "exit_code": code}, i + 0.5)
    snap = state.snapshot(10.0)
    assert {p["id"]: p["state"] for p in snap["practices"]} == {
        "tests": "ok", "browser": "ok", "docs": "ok", "commit": "ok", "no_evidence": "ok", "agora": "ok"}
    assert "pytest" in _practice(snap, "tests")["evidence"]
    assert [s["kind"] for s in snap["steps"]].count("commit") == 1
    assert [s["kind"] for s in snap["steps"]].count("submit") == 1


def test_failures_and_unsettled_identity():
    calls = [
        {"tool": "bash", "command": "npm test", "ok": False},
        {"tool": "bash", "command": "git add shot.png && git commit -m wip", "ok": True},
    ]
    marks = {p["id"]: p for p in loop.judge_practices(calls)}
    assert marks["tests"]["state"] == "fail"
    assert marks["no_evidence"]["state"] == "fail"
    assert marks["commit"]["state"] == "unknown"
    assert marks["browser"]["state"] == "pending" and marks["agora"]["state"] == "pending"
    other = loop.judge_practices([{"tool": "bash", "command": "git commit --author='Someone <s@x>' -m x", "ok": True}], author="sample-maintainer")
    assert next(p for p in other if p["id"] == "commit")["state"] == "fail"
    trailer = loop.judge_practices([{"tool": "bash", "command": "git commit -m 'x\n\nCo-Authored-By: Bot'", "ok": True}])
    assert next(p for p in trailer if p["id"] == "commit")["state"] == "fail"


def test_registry_keeps_last_loop_and_runs_feed_it():
    from src import agent_runs

    state = loop.begin("sess-loop", "run-1", 10.0)
    run = agent_runs._Run()
    run.loop = state
    agent_runs._observe_activity(run, "data: " + json.dumps({"type": "tool_start", "tool": "bash", "command": "ls"}) + "\n\n")
    assert loop.snapshot("sess-loop")["phase"] == "act"
    agent_runs._observe_activity(run, "data: " + json.dumps({"delta": "ok", "thinking": False}) + "\n\n")
    assert loop.snapshot("sess-loop")["phase"] == "plan"
    loop.finish(state, "done")
    assert loop.snapshot("sess-loop")["running"] is False
    assert loop.snapshot("never-ran") is None


def _run(calls, prompt="", answer="", outputs=None):
    """Feed a turn through a LoopState: (tool, command, exit_code) calls, then the answer."""
    state = loop.LoopState("s", "r", 0.0, prompt)
    for i, (tool, command, code) in enumerate(calls):
        state.feed({"type": "tool_start", "tool": tool, "command": command}, i + 0.1)
        state.feed({"type": "tool_output", "tool": tool, "exit_code": code,
                    "output": (outputs or {}).get(i, "")}, i + 0.6)
    if answer:
        state.feed({"delta": answer, "thinking": False}, 50.0)
    state.finish("done", 60.0)
    return state.snapshot(60.0)


def test_kind_follows_what_the_turn_did():
    assert loop.classify([]) == "chat"
    assert loop.classify([{"tool": "read_file", "command": '{"path": "src/x.py"}', "ok": True}]) == "chat"
    assert loop.classify([{"tool": "web_search", "command": '{"query": "bitcoin"}', "ok": True}]) == "research"
    assert loop.classify([{"tool": "web_search", "command": "{}", "ok": True},
                          {"tool": "edit_file", "command": '{"path": "src/a.py"}', "ok": True}]) == "code"
    assert loop.classify([{"tool": "write_file", "command": '{"path": "out/informe.docx"}', "ok": True}]) == "files"
    assert loop.classify([{"tool": "bash", "command": "pandoc a.md -o informe.pdf", "ok": True}]) == "files"
    assert loop.classify([{"tool": "bash", "command": "git status", "ok": True}]) == "code"
    assert loop.classify([{"tool": "bash", "command": "docker restart web", "ok": True}]) == "ops"
    assert loop.classify([{"tool": "bash", "command": "agora.cmd --space x claim 12", "ok": True}]) == "code"


def test_a_web_search_turn_is_judged_as_research_not_as_code():
    snap = _run([("web_search", '{"query": "precio del bitcoin hoy"}', 0),
                 ("web_fetch", '{"url": "https://www.coindesk.com/price/bitcoin"}', 0)],
                prompt="¿Cuál es el precio del bitcoin hoy?",
                answer="El bitcoin cotiza a 91.000 $ el 17 de noviembre de 2026 según "
                       "[CoinDesk](https://www.coindesk.com/price/bitcoin).",
                outputs={0: "1. https://www.coindesk.com/price/bitcoin  2. https://es.investing.com/crypto"})
    assert snap["kind"] == "research"
    marks = {p["id"]: p["state"] for p in snap["practices"]}
    assert marks == {"sources_opened": "ok", "sources_cited": "ok", "dated": "ok", "verified": "ok", "language": "ok"}
    assert "tests" not in marks and snap["practices_summary"] == {"ok": 5, "fail": 0, "open": 0}


def test_research_flags_unseen_sources_and_a_wrong_language():
    snap = _run([("web_search", '{"query": "bitcoin"}', 0)], prompt="¿Cuál es el precio del bitcoin hoy?",
                answer="It trades at 91,000 dollars, see https://made-up.example/btc for the details.",
                outputs={0: "https://www.coindesk.com/price/bitcoin"})
    marks = {p["id"]: p for p in snap["practices"]}
    assert marks["sources_opened"]["state"] == "pending"
    assert marks["verified"]["state"] == "fail" and "made-up.example" in marks["verified"]["evidence"]
    assert marks["language"]["state"] == "fail"
    assert marks["dated"]["state"] == "pending"


def test_files_check_the_output_and_the_requested_format():
    snap = _run([("write_file", '{"path": "out/informe.md", "content": "x"}', 0),
                 ("read_file", '{"path": "out/informe.md"}', 0)],
                prompt="Hazme un informe en PDF con las ventas", answer="Aquí tienes el informe de las ventas.")
    marks = {p["id"]: p["state"] for p in snap["practices"]}
    assert snap["kind"] == "files"
    assert marks == {"output_checked": "ok", "format": "fail", "language": "ok"}
    plain = _run([("generate_image", '{"prompt": "a red fox"}', 0)], prompt="draw me a fox")
    assert [p["id"] for p in plain["practices"]] == ["output_checked", "language"]   # no format asked


def test_ops_wants_the_command_checked_after_running():
    snap = _run([("bash", "docker restart web", 0), ("bash", "docker ps", 0)], prompt="reinicia el contenedor web",
                answer="Reiniciado; el contenedor está en marcha.")
    marks = {p["id"]: p["state"] for p in snap["practices"]}
    assert snap["kind"] == "ops" and marks == {"ran_ok": "ok", "checked": "ok", "language": "ok"}
    unchecked = _run([("bash", "docker restart web", 1)])
    marks = {p["id"]: p["state"] for p in unchecked["practices"]}
    assert marks == {"ran_ok": "fail", "checked": "pending"}            # no prompt: language does not apply


def test_a_plain_conversation_has_no_checklist():
    snap = _run([], prompt="hola, ¿qué tal?", answer="¡Hola! Todo bien.")
    assert snap["kind"] == "chat" and snap["practices"] == []
    assert snap["practices_summary"] == {"ok": 0, "fail": 0, "open": 0}


def test_steps_read_as_tool_names_and_argument_summaries():
    snap = _run([("web_search", '{"query": "precio del bitcoin hoy", "max_results": 5}', 0),
                 ("bash", "pytest -q tests/test_x.py", 2)])
    tools = [s for s in snap["steps"] if s["kind"] == "tool"]
    results = [s for s in snap["steps"] if s["kind"] == "result"]
    assert tools[0]["label"] == "Search the web: precio del bitcoin hoy" and "{" not in tools[0]["label"]
    assert tools[0]["title"] == "Search the web" and tools[0]["detail"] == "precio del bitcoin hoy"
    assert tools[1]["label"] == "Terminal: pytest -q tests/test_x.py"
    assert results[0]["ok"] is True and results[0]["ms"] == 500 and results[0]["label"].endswith("ok · 0.5 s")
    assert results[1]["ok"] is False and "error (exit 2)" in results[1]["label"]
    assert loop.args_summary('{"url": "https://a.example/x"}') == "https://a.example/x"
    assert loop.tool_title("mcp__playwright__browser_navigate") == "Browser · navigate"
    assert loop.language("busca el precio del bitcoin") == "es" and loop.language("find the price of gold") == "en"
