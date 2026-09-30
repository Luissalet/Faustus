"""H07 / H13: the agent loop's transitions, characterized with a scripted model.

Each scenario scripts the provider's answers round by round, runs the real
`stream_agent_loop` body (only the provider stream, the tool executor and the
settings store are replaced) and asserts two things: the ORDER of actions the
loop took and the state it ended in. Every extra round the loop grants carries
the cause and budget the policy recorded (src/extra_round_policy.py).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

import src.agent_loop as al

USER_ES = "Explica qué hace el fichero server.py"
USER_EDIT = "Añade un botón de borrar en las tarjetas de proyectos"


def _events(chunks):
    out = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    out.append(json.loads(line[6:]))
                except ValueError:
                    pass
    return out


def text(body, finish="stop"):
    return [f'data: {json.dumps({"delta": body})}\n\n',
            f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n']


def native_call(name, arguments=None, preface="Voy."):
    return [f'data: {json.dumps({"delta": preface})}\n\n',
            f'data: {json.dumps({"type": "tool_calls", "calls": [{"name": name, "arguments": json.dumps(arguments or {})}]})}\n\n',
            f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n']


def provider_error(message, status=400):
    return [f'event: error\ndata: {json.dumps({"error": message, "status": status})}\n\n']


class Run:
    def __init__(self, events, seen, executed, exec_kwargs, offered=None):
        self.offered = offered if offered is not None else []
        self.events = events
        self.seen = seen
        self.executed = executed
        self.exec_kwargs = exec_kwargs

    @property
    def calls(self):
        return len(self.seen)

    def actions(self):
        """The loop's decisions, in order, as short strings."""
        out = []
        for event in self.events:
            kind = event.get("type")
            if kind == "round_info":
                out.append(f"round:{event.get('finish_reason')}")
            elif kind == "harness_check":
                status = event.get("status")
                label = event.get("reason") if status == "auto_continue" else status
                cause = f"[{event['cause']}]" if event.get("cause") else ""
                out.append(f"{status}:{label}{cause}" if status == "auto_continue" else f"check:{label}{cause}")
            elif kind == "cancelled":
                out.append("cancelled")
            elif kind in ("tool_event", "tool_output"):
                out.append(f"tool:{event.get('tool')}")
        return out

    def extra_rounds(self):
        return [e for e in self.events if e.get("type") == "harness_check" and e.get("cause")]

    def summary(self):
        found = [e for e in self.events if e.get("type") == "harness_summary"]
        return found[-1]["data"] if found else {}


@pytest.fixture
def drive(monkeypatch, tmp_path):
    def _drive(script, *, user=USER_ES, settings=None, max_rounds=6, tools=None, exec_result=None,
               exec_fn=None, workspace=None, pending_cancel=None, disabled_tools=None, prepare=False,
               retrieved=None):
        seen, executed, exec_kwargs, offered = [], [], [], []
        values = dict(settings or {})

        async def stream(_candidates, messages, **kwargs):
            seen.append([dict(m) for m in messages])
            offered.append([(t.get("function") or {}).get("name") for t in (kwargs.get("tools") or [])])
            if prepare:
                # What the real provider router does first: prepare the request
                # of candidate 0, which is where the step's contract is captured.
                await kwargs["candidate_request_factory"](0, "http://127.0.0.1:11434/v1", "qwen3-coder:30b", {})
            for chunk in script[min(len(seen) - 1, len(script) - 1)]:
                yield chunk
            yield "data: [DONE]\n\n"

        async def execute(block, *args, **kwargs):
            executed.append(block.tool_type)
            exec_kwargs.append(kwargs)
            if exec_fn is not None:
                return exec_fn(block)
            return (block.tool_type, dict(exec_result or {"output": "ok", "exit_code": 0}))

        monkeypatch.setattr(al, "stream_llm_with_fallback", stream, raising=False)
        monkeypatch.setattr(al, "execute_tool_block", execute, raising=False)
        monkeypatch.setattr(al, "get_setting", lambda key, default=None: values.get(key, default), raising=False)
        monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
        monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
        monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
        if prepare:
            monkeypatch.setattr(al, "_agent_route_tool_mode", lambda *a, **k: (True, False, True), raising=False)

        if retrieved is not None:
            class _Index:
                def get_tools_for_query(self, query, k=8, **options):
                    return set(retrieved)

                def index_mcp_tools(self, *args, **kwargs):
                    return None
            import src.tool_index as tool_index
            monkeypatch.setattr(tool_index, "get_tool_index", lambda: _Index())

        async def consume():
            gen = al.stream_agent_loop(
                "http://127.0.0.1:11434/v1", "qwen3-coder:30b", [{"role": "user", "content": user}],
                max_rounds=max_rounds,
                relevant_tools=None if retrieved is not None else set(tools or {"read_file", "edit_file", "glob"}),
                workspace=workspace or str(tmp_path), session_id="s-char",
                pending_cancel=pending_cancel, disabled_tools=disabled_tools)
            return [chunk async for chunk in gen]

        return Run(_events(asyncio.run(consume())), seen, executed, exec_kwargs, offered)

    return _drive


# -- the turn that simply answers ---------------------------------------------
def test_a_text_only_answer_ends_after_one_round_without_extra_rounds(drive):
    run = drive([text("server.py arranca la aplicación y registra las rutas.")])
    assert run.calls == 1
    assert run.actions()[0] == "round:stop"
    assert not run.extra_rounds() and not any(a.startswith("auto_continue") for a in run.actions())
    assert run.executed == []
    assert "server.py arranca" in json.dumps(run.events)


# -- a call to a tool that does not exist -----------------------------------------
def test_a_nonexistent_tool_is_corrected_once_and_the_turn_then_answers(drive):
    run = drive([native_call("no_such_tool_xyz"), text("Hecho: no había nada que cambiar.")])
    assert run.calls == 2
    assert run.actions()[:3] == ["round:tool_calls", "check:unknown_tool[hallucinated_tool]", "round:stop"]
    check = run.extra_rounds()[0]
    assert (check["cause"], check["family"], check["budget_used"], check["budget_limit"]) == (
        "hallucinated_tool", "correction", 1, 2)
    assert run.executed == []
    correction = json.dumps(run.seen[1][-1])
    assert "no_such_tool_xyz" in correction and "does not exist" in correction


def test_the_correction_budget_of_a_nonexistent_tool_is_two_then_the_turn_moves_on(drive):
    run = drive([native_call("no_such_tool_xyz")])
    causes = [(e["cause"], e["budget_used"]) for e in run.extra_rounds()]
    assert causes == [("hallucinated_tool", 1), ("hallucinated_tool", 2)]
    assert run.calls == 3  # two corrections, then the third answer is accepted as it is


# -- context limit ----------------------------------------------------------------
CTX_ERR = "This model's maximum context length is 8192 tokens, however you requested 9000 tokens"


def test_a_context_limit_error_compacts_once_and_redoes_the_round(drive):
    run = drive([provider_error(CTX_ERR), text("La respuesta completa.")], user="What is two plus two?")
    assert run.calls == 2
    assert run.actions()[0] == "auto_continue:context_overflow_compact[context_overflow_compact]"
    event = run.extra_rounds()[0]
    assert (event["family"], event["budget_used"], event["budget_limit"]) == ("transport", 1, 1)
    assert not any(e.get("type") == "agent_terminal" for e in run.events)


def test_a_second_context_limit_error_in_a_row_ends_the_turn_with_the_reason(drive):
    run = drive([provider_error(CTX_ERR)], user="What is two plus two?")
    assert run.calls == 2
    assert len(run.extra_rounds()) == 1
    assert any(e.get("error_class") == "context_length_exceeded" for e in run.events)


# -- permission denied --------------------------------------------------------------
def test_a_denied_tool_call_is_reported_to_the_model_and_not_retried(drive):
    denied = {"error": "Tool 'edit_file' blocked by external-context policy.", "exit_code": 1,
              "blocked": True, "policy": "external_untrusted_context"}
    run = drive([native_call("edit_file", {"path": "a.txt", "old_string": "a", "new_string": "b"}),
                 text("No pude editar a.txt: la herramienta quedó bloqueada.")],
                user=USER_EDIT, exec_result=denied)
    assert run.executed == ["edit_file"]
    assert run.calls == 2
    assert run.actions()[:2] == ["round:tool_calls", "tool:edit_file"] or run.actions()[0] == "round:tool_calls"
    assert not any(e.get("status") == "verified" for e in run.events if e.get("type") == "harness_check")
    assert "bloqueada" in json.dumps(run.events)


def test_every_call_is_handed_the_snapshot_of_the_step_that_made_it(drive):
    """H06 wiring: the executor receives the contract the answering round announced."""
    from src.step_snapshot import StepSnapshot
    run = drive([native_call("read_file", {"path": "a.txt"}), text("Listo.")], user="Lee a.txt", prepare=True)
    assert run.executed == ["read_file"]
    snapshot = run.exec_kwargs[0]["step_snapshot"]
    assert isinstance(snapshot, StepSnapshot)
    assert snapshot.round_num == 1 and snapshot.candidate_index == 0 and snapshot.session_id == "s-char"
    assert snapshot.announced("read_file") is not None
    assert snapshot.contract_for("read_file")["parameters"]["required"] == ["path"]


def test_a_second_round_call_carries_the_second_rounds_snapshot(drive):
    run = drive([native_call("read_file", {"path": "a.txt"}), native_call("glob", {"pattern": "*.py"}),
                 text("Listo.")], user="Lee a.txt y busca los .py", prepare=True)
    rounds = [kw["step_snapshot"].round_num for kw in run.exec_kwargs]
    assert run.executed == ["read_file", "glob"] and rounds == [1, 2]
    ids = {kw["step_snapshot"].snapshot_id for kw in run.exec_kwargs}
    assert len(ids) == 2


def test_no_snapshot_is_captured_when_the_check_is_switched_off(drive):
    run = drive([native_call("read_file", {"path": "a.txt"}), text("Listo.")], user="Lee a.txt",
                settings={"agent_step_snapshot_mode": "off"}, prepare=True)
    assert [kw["step_snapshot"] for kw in run.exec_kwargs] == [None]


# -- failing tests ---------------------------------------------------------------------
def test_failing_project_tests_give_exactly_one_fix_round_with_its_budget(drive, tmp_path, monkeypatch):
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

    def edit(block):
        args = json.loads(block.content)
        path = os.path.join(str(ws), args["path"])
        body = open(path, encoding="utf-8").read()
        open(path, "w", encoding="utf-8").write(body.replace(args["old_string"], args["new_string"], 1))
        return (block.tool_type, {"output": f"Edited {args['path']} (1 replacement)", "exit_code": 0,
                                  "diff": {"added": 1, "removed": 1}})

    def call(old, new):
        return native_call("edit_file", {"path": "src/calc.py", "old_string": old, "new_string": new})

    run = drive([call("return a - b", "return a * b"), text("He corregido src/calc.py."),
                 call("return a * b", "return a + b"), text("He corregido src/calc.py de verdad.")],
                user="Arregla la función add de src/calc.py", settings={"agent_project_tests": True},
                exec_fn=edit, workspace=str(ws), tools={"read_file", "edit_file", "glob", "bash"})
    statuses = [e["status"] for e in run.events if e.get("type") == "harness_check"]
    assert "tests_failed" in statuses and statuses[-1] == "verified"
    failed = next(e for e in run.extra_rounds() if e["status"] == "tests_failed")
    assert (failed["cause"], failed["family"], failed["budget_used"], failed["budget_limit"]) == (
        "tests_fix", "verification", 1, 1)
    assert run.summary()["tests_fix_rounds"] == 1 and run.summary()["stop_reason"] == "complete"


# -- partial result ------------------------------------------------------------------------
def test_a_half_done_report_is_rejected_within_its_budget_then_annotated(drive, tmp_path):
    (tmp_path / "cart.py").write_text("def subtotal(items):\n    return 1\n", encoding="utf-8")
    half = ("He completado la tarea. He añadido la función en cart.py y el test en tests/test_cart.py "
            "que verifica el resultado.")
    edit = native_call("edit_file", {"path": "cart.py", "old_string": "return 1", "new_string": "return 2"})
    run = drive([edit, text(half)], user="Añade una función a cart.py y escribe también su test",
                exec_result={"output": "Edited cart.py (1 replacement)", "exit_code": 0, "diff": {"added": 1, "removed": 1}})
    rejected = [e for e in run.extra_rounds() if e["status"] == "rejected"]
    # Two rejections spend the budget; the harness then grants one fresh
    # execution cycle (which resets the rejection counter) and rejects twice
    # more before it accepts the answer and annotates it.
    order = [(e["cause"], e.get("budget_used"), e.get("budget_limit")) for e in run.extra_rounds()]
    assert order == [("claim_rejection", 1, 2), ("claim_rejection", 2, 2), ("execution_recovery", 1, 1),
                     ("claim_rejection", 1, 2), ("claim_rejection", 2, 2),
                     ("configured_cycle", None, None)]  # the step budget itself ran out
    assert rejected[0]["cause"] == "claim_rejection"
    assert run.summary()["stop_reason"] != "complete"


# -- cancellation -----------------------------------------------------------------------------
def test_a_stop_request_ends_the_turn_before_the_next_round(drive):
    polls = {"n": 0}

    def pending_cancel():
        polls["n"] += 1
        return "user_stop" if run_started.get("tool_ran") else None

    run_started = {}

    def execute_and_mark(block):
        run_started["tool_ran"] = True
        return (block.tool_type, {"output": "ok", "exit_code": 0})

    run = drive([native_call("read_file", {"path": "a.txt"}), text("No debería llegar aquí.")],
                user="Lee a.txt", exec_fn=execute_and_mark, pending_cancel=pending_cancel)
    assert run.calls == 1, "no further model call after the stop"
    assert run.actions().count("cancelled") == 1
    cancelled = next(e for e in run.events if e.get("type") == "cancelled")
    assert cancelled["reason"] == "user_stop"
    assert "No debería llegar aquí." not in json.dumps(run.events)


# -- the other extra rounds: one receipt each ------------------------------------------------------
def test_a_cut_off_reply_is_continued_twice_at_most_with_the_budget_reported(drive):
    run = drive([text("Primera parte del texto", "length"), text(" segunda parte", "length"),
                 text(" tercera parte", "length"), text(" final.")])
    used = [(e["cause"], e["budget_used"], e["budget_limit"]) for e in run.extra_rounds()]
    assert used == [("length", 1, 2), ("length", 2, 2)]
    assert run.calls == 3


def test_an_empty_round_in_a_workspace_turn_is_nudged_within_its_budget(drive):
    run = drive([text(""), text("Hecho: no cambié nada.")], user=USER_EDIT)
    empty = [e for e in run.extra_rounds() if e["status"] == "empty_round"]
    assert empty and empty[0]["cause"] == "empty_completion" and empty[0]["budget_used"] == 1


def test_an_answer_that_repeats_the_approval_card_is_bounced_twice_at_most(drive):
    card = al._APPROVAL_CARD_QUESTION
    run = drive([text(card)], user="Continúa con la tarea anterior")
    echoes = [e for e in run.extra_rounds() if e.get("reason") == "approval_echo"]
    assert [(e["cause"], e["budget_used"], e["budget_limit"]) for e in echoes] == [
        ("approval_echo", 1, 2), ("approval_echo", 2, 2)]


# -- the policy is the only grantor, and is switchable ---------------------------------------------------
def test_the_legacy_mode_keeps_the_same_rounds_without_the_receipts(drive):
    script = [text("Primera parte del texto", "length"), text(" final.")]
    receipts = drive(script)
    legacy = drive(script, settings={"agent_extra_round_policy": "legacy"})
    assert [a.split("[")[0] for a in receipts.actions()] == [a.split("[")[0] for a in legacy.actions()]
    assert receipts.extra_rounds() and not legacy.extra_rounds()
    assert receipts.calls == legacy.calls == 2


def test_the_shadow_mode_grants_what_legacy_grants_and_records_the_decision(drive):
    run = drive([text("Primera parte del texto", "length"), text(" final.")],
                settings={"agent_extra_round_policy": "shadow"})
    assert run.calls == 2 and run.extra_rounds()[0]["cause"] == "length"


# -- H17: exposure decides which retrieved tools carry a schema -----------------------
def _system_text(run):
    return "\n".join(str(m.get("content") or "") for m in run.seen[0] if m.get("role") == "system")


def test_a_deferred_tool_retrieval_picked_without_evidence_is_listed_not_loaded(drive):
    run = drive([text("Claro.")], user="hola, qué tal, cuéntame algo", retrieved={"read_file", "code_graph_search"}, prepare=True,
                settings={"agent_tool_exposure": True})
    assert "code_graph_search" not in run.offered[0] and "read_file" in run.offered[0]
    assert "code_graph_search" in _system_text(run) and "More tools (catalog)" in _system_text(run)


def test_the_same_tool_keeps_its_schema_when_the_request_speaks_of_it(drive):
    run = drive([text("Claro.")], user="search the code graph for the callers of this function", retrieved={"read_file", "code_graph_search"},
                prepare=True, settings={"agent_tool_exposure": True})
    assert "code_graph_search" in run.offered[0]


def test_switching_exposure_off_restores_the_previous_offer(drive):
    run = drive([text("Claro.")], user="hola, qué tal, cuéntame algo", retrieved={"read_file", "code_graph_search"}, prepare=True,
                settings={"agent_tool_exposure": False})
    assert "code_graph_search" in run.offered[0]
