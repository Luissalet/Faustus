"""Tracking a pasted plan must not replace its human-authored contract."""
import pytest

from src import plan_tracker as pt


CONTRACT = (
    "  Ejecuta este plan completo en la carpeta activa:\n\n"
    "## API pública estable\n"
    + "Conserva claves binarias, Unicode y operaciones atómicas.\n" * 110
    + "\n## Persistencia y aceptación\n"
    "Encoder y decoder admiten el mismo dominio; acepta65MiB o rechaza antes del ACK.\n"
    "## CLI estable\n"
    "Usa --key-b64 y --value-b64 con base64 estricto, no --hex.\n"
    "stdout contiene un objeto JSON ASCII exacto.\n"
    "## Entrega y comprobación\n"
    "Prueba recuperación, compactación y CLI. Última condición: 日本語, 🧪.  \n\n"
)


@pytest.fixture(autouse=True)
def tracker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "PLAN_TRACKER_DIR", str(tmp_path / "trackers"))


def tracked(text, scope="own-plan-contract"):
    found = pt.find_plan_in_text(text)
    assert found is not None
    return pt.upsert_from_attachment(scope, *found)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_pasted_contract_is_literal_even_after_task_limit(newline):
    text = CONTRACT.replace("\n", newline)
    tracker = tracked(text)
    assert len(tracker["tasks"]) == 4
    assert len(text) > 6000
    assert pt.replace_pasted(text, tracker, max_task_chars=1) == text


def test_pasted_contract_and_following_attachment_remain_unchanged():
    text = CONTRACT + "\n=== File: notes.txt ===\n  Attachment body\r\n終わり\n"
    assert pt.replace_pasted(text, tracked(CONTRACT)) == text


def test_advancing_tracker_does_not_remove_other_contract_sections():
    scope = "own-plan-advance"
    tracker = tracked(CONTRACT, scope)
    tracker = pt.mark(scope, tracker["hash"], "t01", "done", "API operations confirmed by own test output")
    assert pt.current_task(tracker)["id"] == "t02"
    assert pt.replace_pasted(CONTRACT, tracker) == CONTRACT
    assert pt.progress(tracker)["done"] == 1


@pytest.mark.parametrize("language", ["es", "en"])
def test_brief_describes_tracker_without_denying_full_human_request(language):
    brief = pt.brief(tracked(CONTRACT), language=language)
    assert all(name in brief for name in ("plan_status", "plan_task", "plan_done"))
    assert "full plan is NOT" not in brief
    assert "plan completo NO" not in brief
    assert "must not be requested" not in brief
    assert "no debe volver a pedirse" not in brief


def test_real_loop_provider_receives_complete_pasted_contract_and_tracker_brief(tmp_path, monkeypatch):
    import httpx
    import requests
    from src import model_context, run_model_pin
    from tests.test_agent_harness_functional import _patch_common, _scripted_stream, _run

    attempted_http = []
    def no_http(*args, **kwargs):
        attempted_http.append(True)
        raise AssertionError("CPU provider fixture must not perform HTTP")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)
    monkeypatch.setattr(model_context, "_query_context_length", lambda *args, **kwargs: (131072, True))
    # A CPU peer has no engine whose keep_alive needs restoring on completion.
    monkeypatch.setattr(run_model_pin, "restore_keep_alive", lambda *args, **kwargs: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common(monkeypatch, settings={
        "agent_project_tests": False, "agent_ui_smoke": False,
        "agent_plan_autocontinue": False,
    })
    calls = _scripted_stream(monkeypatch, [
        ('```plan_status\n{}\n```', "tool_calls"),
        ("El estado del plan está consultado; las tareas siguen pendientes.", "stop"),
    ])
    events = _run(str(workspace), user=CONTRACT, harness_options={"project_id": "own-contract"})
    assert any(e.get("type") == "plan_tracker" and e.get("total") == 4 for e in events)
    for messages in calls["messages"]:
        humans = [m for m in messages if m.get("role") == "user" and not m.get("_harness_note")]
        assert any(m.get("content") == CONTRACT for m in humans)
    # The brief travels as a user-role harness note (some model families ignore
    # mid-prompt system messages) flagged so context trimming keeps it.
    assert any(m.get("role") == "user" and m.get("_harness_note") and (m.get("metadata") or {}).get("plan_note")
               and "plan_status" in m.get("content", "") for m in calls["messages"][0])
    assert attempted_http == []


@pytest.mark.parametrize("successful_read", [True, False, "plan_only"])
def test_readonly_progress_reconciles_the_same_plan_only_with_tool_evidence(
        tmp_path, monkeypatch, successful_read):
    import json
    import httpx
    import requests
    import src.agent_loop as al
    from src import model_context, run_model_pin
    from src.agent_tools import TOOL_HANDLERS
    from tests.test_agent_harness_functional import _patch_common, _scripted_stream, _collect, _events
    from tests.test_contract_criteria import _R4_OPERATIONAL_1797

    titles = ("Leer contexto StaticText", "Leer botón Cerrar", "Leer confirmación release")
    user = "Ejecuta este plan de observación del navegador:\n" + "\n".join(
        f"{i}. {title}" for i, title in enumerate(titles, 1))
    attempted_http = []

    def no_http(*args, **kwargs):
        attempted_http.append(True)
        raise AssertionError("CPU fixture must not perform HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)
    monkeypatch.setattr(model_context, "_query_context_length", lambda *a, **k: (131072, True))
    monkeypatch.setattr(run_model_pin, "restore_keep_alive", lambda *a, **k: None)
    # The stand-in executor does not bind tool_execution's contextvar itself.
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: str(tmp_path))
    _patch_common(monkeypatch, settings={
        "agent_project_tests": False, "agent_ui_smoke": False, "agent_auto_review": "off",
        "agent_contract_criteria": "off", "agent_project_todos": False,
    })
    executed = []
    session = "cpu-plan1797-" + str(successful_read).lower()

    async def tool_exec(block, *args, **kwargs):
        executed.append(block.tool_type)
        if block.tool_type == "plan_status":
            result = await TOOL_HANDLERS["plan_status"](block.content, {
                "session_id": session, "workspace": str(tmp_path),
            })
            return block.tool_type, result
        if block.tool_type == "read_artifact":
            if successful_read is True:
                return block.tool_type, {"output": json.dumps(_R4_OPERATIONAL_1797), "exit_code": 0}
            return block.tool_type, {"error": "saved_receipt_unavailable", "exit_code": 1}
        if block.tool_type == "todowrite":
            # The normal handler persists in conftest's private DATA_DIR and
            # returns the ordinary todos for TurnLedger to annotate itself.
            result = await TOOL_HANDLERS["todowrite"](block.content, {"session_id": session})
            return block.tool_type, result
        raise AssertionError("unexpected scripted tool: " + block.tool_type)

    monkeypatch.setattr(al, "execute_tool_block", tool_exec)
    todos = [{"content": title, "status": "completed"} for title in titles]
    first_tool = "plan_status" if successful_read == "plan_only" else "read_artifact"
    first_args = {} if first_tool == "plan_status" else {
        "artifact_id": "occ_0d0207d3e3445d3798389dc0f70d1d5f",
    }
    calls = _scripted_stream(monkeypatch, [
        ('```' + first_tool + '\n' + json.dumps(first_args) + '\n```', "tool_calls"),
        ('```todowrite\n' + json.dumps({"todos": todos}) + '\n```', "tool_calls"),
        ("La observación de los recibos ha terminado.", "stop"),
    ])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "cpu-scripted", [{"role": "user", "content": user}],
        relevant_tools={"read_artifact", "todowrite", "plan_status"}, workspace=str(tmp_path), session_id=session,
        harness_options={"trusted_workspace": str(tmp_path)}, max_rounds=5,
    )))
    summary = next(e["data"] for e in events if e.get("type") == "harness_summary")
    scope = pt.scope_for(None, str(tmp_path), session)
    tracker = pt.active(scope)
    assert tracker is not None and pt.progress(tracker)["total"] == 3
    assert executed == [first_tool, "todowrite"]
    assert attempted_http == []
    assert summary["mutations"] == [] and summary["effects"] == 0
    if successful_read is True:
        assert all(item["verified"] for item in summary["progress"])
        assert pt.progress(tracker)["done"] == 3
        assert calls["n"] == 3
        assert not any(e.get("status") == "plan_continue" for e in events)
        assert any(e.get("type") == "plan_tracker" and e.get("done") == 3 for e in events)
        assert "Plan tracker: 3/3 done" in json.dumps(calls["messages"][2], ensure_ascii=False)
    else:
        assert pt.progress(tracker)["done"] == 0
        if successful_read == "plan_only":
            # The incumbent flag can credit a metadata call; that is not a
            # reason to auto-close operational plan steps from its own status.
            assert all(item["verified"] for item in summary["progress"])
            assert summary["failed_calls"] == 0
        else:
            assert not any(item["verified"] for item in summary["progress"])
            assert summary["failed_calls"] == 1


def test_new_todo_cannot_reuse_an_operational_receipt_before_previous_progress(tmp_path, monkeypatch):
    import json
    import httpx
    import requests
    import src.agent_loop as al
    from src import model_context, run_model_pin
    from src.agent_tools import TOOL_HANDLERS
    from tests.test_agent_harness_functional import _patch_common, _scripted_stream, _collect, _events
    from tests.test_contract_criteria import _R4_OPERATIONAL_1797

    attempted_http = []

    def no_http(*args, **kwargs):
        attempted_http.append(True)
        raise AssertionError("CPU fixture must not perform HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)
    monkeypatch.setattr(model_context, "_query_context_length", lambda *a, **k: (131072, True))
    monkeypatch.setattr(run_model_pin, "restore_keep_alive", lambda *a, **k: None)
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: str(tmp_path))
    _patch_common(monkeypatch, settings={
        "agent_project_tests": False, "agent_ui_smoke": False, "agent_auto_review": "off",
        "agent_contract_criteria": "off", "agent_project_todos": False,
        "agent_plan_autocontinue": False,
    })
    session = "cpu-plan1797-temporal"
    scope = pt.scope_for(None, str(tmp_path), session)
    titles = ("Leer primer recibo operativo", "Leer segundo recibo operativo")
    plan_body = "\n\n".join("## " + title + "\n" for title in titles)
    initial = pt.upsert_from_attachment(scope, "Plan temporal readonly", plan_body)
    assert initial is not None and pt.progress(initial) == {
        "done": 0, "total": 2, "skipped": 0, "pending": 2,
    }
    first = [{"content": titles[0], "status": "completed"},
             {"content": titles[1], "status": "pending"}]
    second = [{"content": title, "status": "completed"} for title in titles]
    executed = []
    metadata = []

    async def tool_exec(block, *args, **kwargs):
        executed.append(block.tool_type)
        if block.tool_type == "read_artifact":
            return block.tool_type, {"output": json.dumps(_R4_OPERATIONAL_1797[0]), "exit_code": 0}
        if block.tool_type in {"todowrite", "plan_status"}:
            result = await TOOL_HANDLERS[block.tool_type](block.content, {
                "session_id": session, "workspace": str(tmp_path),
            })
            if block.tool_type == "plan_status":
                metadata.append(json.loads(json.dumps(result)))
            return block.tool_type, result
        raise AssertionError("unexpected scripted tool: " + block.tool_type)

    monkeypatch.setattr(al, "execute_tool_block", tool_exec)
    _scripted_stream(monkeypatch, [
        ('```read_artifact\n{"artifact_id":"occ_0d0207d3e3445d3798389dc0f70d1d5f"}\n```', "tool_calls"),
        ('```todowrite\n' + json.dumps({"todos": first}) + '\n```', "tool_calls"),
        ('```plan_status\n{}\n```', "tool_calls"),
        ('```todowrite\n' + json.dumps({"todos": second}) + '\n```', "tool_calls"),
        ("La secuencia de observación de recibos ha terminado.", "stop"),
    ])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "cpu-scripted",
        [{"role": "user", "content": "Sigue con el plan guardado de observación de recibos."}],
        relevant_tools={"read_artifact", "todowrite", "plan_status"}, workspace=str(tmp_path),
        session_id=session, harness_options={"trusted_workspace": str(tmp_path)}, max_rounds=6,
    )))
    summary = next(e["data"] for e in events if e.get("type") == "harness_summary")
    progress = [e for e in events if e.get("type") == "progress_update"]
    tracker_events = [e for e in events if e.get("type") == "plan_tracker"]
    final = pt.active(scope)
    observations = {
        "executed": executed, "metadata": metadata, "progress": progress,
        "tracker_events": tracker_events, "final": final,
        "failed_calls": summary["failed_calls"], "effects": summary["effects"],
        "mutations": summary["mutations"], "attempted_http": attempted_http,
    }
    detail = json.dumps(observations, ensure_ascii=False, default=str)
    # Existing normal APIs and the first operation must be healthy before P1.
    assert executed == ["read_artifact", "todowrite", "plan_status", "todowrite"], detail
    assert len(metadata) == 1 and metadata[0]["progress"]["done"] == 1, detail
    assert metadata[0]["progress"]["total"] == 2, detail
    assert len(progress) == 2, detail
    assert progress[0]["todos"][0]["verified"] is True, detail
    assert progress[0]["todos"][1]["status"] == "pending", detail
    assert all(t["verified"] is True for t in progress[1]["todos"]), detail
    assert attempted_http == [] and summary["failed_calls"] == 0, detail
    assert summary["mutations"] == [] and summary["effects"] == 0, detail
    assert final is not None and pt.progress(final)["total"] == 2, detail
    # A metadata call in the NEW window cannot borrow the previous read.
    assert pt.progress(final)["done"] == 1, detail
    assert final["state"][final["tasks"][0]["id"]]["status"] == "done", detail
    assert final["state"][final["tasks"][1]["id"]]["status"] == "pending", detail
