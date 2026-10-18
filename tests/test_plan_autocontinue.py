"""Plan import from any source + in-turn continuation (OBJ-16, 01-10-2026).

The user pastes a plan (or attaches it as .md / inside a .zip) and used to have
to type "continua" again and again. Now a pasted or attached plan becomes the
chat's tracked plan, and the agent keeps working inside the SAME turn until
every task is closed with evidence, or a stop condition hits.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src import plan_tracker as pt


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "data"))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(pt, "PLAN_TRACKER_DIR", str(tmp_path / "data" / "plan_tracker"), raising=False)
    from src.agent_tools import coding_tools as ct
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path / "data" / "agent_todos"), raising=False)


PASTED = """Ejecuta este plan en la carpeta de trabajo:

1. Crear a.txt con el texto "uno" y comprobar que se lee
2. Crear b.txt con el texto "dos" y comprobar que se lee
3. Crear c.txt con el texto "tres" y comprobar que se lee
4. Listar la carpeta y confirmar que hay tres ficheros
5. Escribir un resumen de una linea
"""


# ---------------------------------------------------------------- find_plan_in_text

def test_a_pasted_five_task_plan_is_imported_with_its_intro_left_out_of_the_tasks():
    found = pt.find_plan_in_text(PASTED)
    assert found is not None
    title, body = found
    assert title.startswith("pasted plan: Ejecuta este plan")
    spec = pt.parse_plan(title, body)
    assert len(spec.tasks) == 5
    assert spec.tasks[0].title.startswith("Crear a.txt")
    assert "Ejecuta este plan" not in body


def test_the_same_pasted_plan_gets_the_same_hash_whatever_the_intro():
    a = pt.find_plan_in_text(PASTED)
    b = pt.find_plan_in_text(PASTED.replace("Ejecuta este plan en la carpeta de trabajo:", "Implementa esto, por favor:"))
    assert pt.parse_plan(*a).hash == pt.parse_plan(*b).hash


@pytest.mark.parametrize("message", [
    "Tengo dudas sobre esto:\n- que framework uso\n- que base de datos\n- como lo despliego",
    "\u00bfQue prefieres para el proyecto?\n- opcion a con mucho detalle y mucho texto aqui\n- opcion b\n- opcion c",
    "Estas son mis tres preguntas sobre el proyecto:\n1. \u00bfPor que falla el login?\n2. \u00bfQue hace el worker?\n3. \u00bfComo se despliega?",
    "Dame ideas:\n1. algo\n2. algo mas\n3. otra cosa",
    "hola, gracias por lo de ayer",
    "",
])
def test_an_ordinary_message_is_not_imported_as_a_plan(message):
    assert pt.find_plan_in_text(message) is None


def test_three_bullets_with_an_execute_verb_but_no_plan_vocabulary_are_not_a_plan():
    assert pt.find_plan_in_text("Haz una compra con:\n- leche\n- huevos\n- pan") is None


def test_a_long_deliberate_task_list_is_imported_without_an_execute_verb():
    body = "Plan de trabajo para esta semana\n\n" + "\n".join(
        f"{i}. Tarea numero {i}: revisar el modulo {i} y dejar las pruebas en verde, con notas de lo que se toca"
        for i in range(1, 8))
    assert len(body) > 600
    found = pt.find_plan_in_text(body)
    assert found and len(pt.parse_plan(*found).tasks) == 7


@pytest.mark.parametrize("policy", [
    "1. Atiende las revisiones pendientes.\n2. Si no hay nada, elige una tarea abierta.\n3. Si tampoco, revisa otra entrega o queda en idle.",
    "1. Handle pending reviews.\n2. If none, choose an open task.\n3. Otherwise, review another submission or stay idle.",
])
def test_priority_branches_are_not_three_mandatory_plan_items(policy):
    message = "Tareas y prioridades del agente.\n" + ("Contexto de coordinación disponible. " * 24) + "\n" + policy
    assert pt.find_plan_in_text(message) is None


def test_a_historical_priority_import_is_preserved_but_not_injected_as_active():
    body = "1. Atiende revisiones.\n2. Si no hay nada, elige una tarea.\n3. Si tampoco, queda libre."
    historical = pt.upsert_from_attachment("policy-project", "old inferred policy", body)
    before = pt.load("policy-project", historical["hash"])
    assert pt.active("policy-project") is None
    assert pt.load("policy-project", historical["hash"]) == before
    assert pt.progress(before)["done"] == 0


def test_conditions_within_actual_work_items_remain_a_real_plan():
    plan = "Ejecuta este plan:\n1. Crear a.txt\n2. Si no hay errores, comprobar a.txt\n3. Si no hay errores, crear b.txt\n"
    found = pt.find_plan_in_text(plan)
    assert found and len(pt.parse_plan(*found).tasks) == 3


@pytest.mark.parametrize("message", [
    "Consulta el estado del plan anterior sin ejecutar sus tareas.",
    "Lee el plan, no lo ejecutes.",
    "Read the plan status; do not execute it.",
    "Show the plan without running its steps.",
    "Read the plan; don't run it.",
])
def test_negated_execution_verbs_do_not_authorize_a_plan(message):
    assert not pt.looks_like_execute_request(message)
    assert not pt.strict_execute_request(message)


def test_a_read_only_list_of_instructions_is_not_imported_as_execution():
    request = "Read this plan; do not execute it:\n1. Run the tests\n2. Build the package\n3. Start the server\n"
    assert not pt.find_plan_in_text(request)
    assert not pt.looks_like_execute_request(request)
    assert not pt.find_plan_attachment("Read the plan; do not execute it:\n=== File: plan.md ===\n" + THREE)


def test_positive_execution_with_a_restricted_step_remains_authorized():
    assert pt.looks_like_execute_request("Ejecuta el plan, pero no ejecutes acciones destructivas.")
    assert pt.strict_execute_request("Execute the plan, but do not run destructive commands.")


def test_independent_conditional_queue_work_is_not_a_priority_ladder():
    plan = ("Ejecuta este plan, comprobando por separado cada cola:\n"
            "1. Comprobar las tres colas y guardar el diagnóstico.\n"
            "2. Si no hay tareas en la cola A, crear una tarea de prueba en A y comprobarla.\n"
            "3. Si no hay tareas en la cola B, crear una tarea de prueba en B y comprobarla.")
    assert pt.find_plan_in_text(plan)
    assert pt.find_plan_attachment("Ejecuta este plan:\n=== File: plan.md ===\n" + plan)


def test_a_false_priority_import_does_not_hide_an_older_genuine_plan():
    genuine = pt.upsert_from_attachment("resume", "real.md", "1. Crear a.txt\n2. Leer a.txt\n3. Probar a.txt")
    policy = pt.upsert_from_attachment("resume", "policy", "1. Atiende revisiones.\n2. Si no hay nada, elige tarea.\n3. Si tampoco, queda libre.")
    assert pt.active("resume")["hash"] == genuine["hash"]
    assert pt.load("resume", policy["hash"]) and pt.progress(pt.load("resume", genuine["hash"]))["done"] == 0


def test_a_long_list_below_the_paste_minimum_is_ignored_unless_it_is_an_execute_request():
    short = "Plan:\n1. uno\n2. dos\n3. tres\n"
    assert pt.find_plan_in_text(short) is None
    assert pt.find_plan_in_text("Implementa este plan:\n1. uno\n2. dos\n3. tres\n") is not None
    assert pt.find_plan_in_text(PASTED, min_chars=0) is None  # 0 = pasted-plan import off


def test_attachments_after_a_pasted_plan_are_kept_by_replace_pasted():
    found = pt.find_plan_in_text(PASTED)
    tracker = pt.upsert_from_attachment("proj", *found)
    text = PASTED + "\n=== File: notes.txt ===\nhello"
    out = pt.replace_pasted(text, tracker)
    assert out == text
    assert "=== File: notes.txt ===" in out and "Crear b.txt" in out


# ---------------------------------------------------------------- attachments

class _Uploads:
    def __init__(self, uploads):
        self.uploads = uploads

    def resolve_upload(self, fid, owner=None):
        return self.uploads.get(fid)

    def _inside_upload_dir(self, path):
        return True

    def is_image_file(self, display_name, mime):
        return False

    def is_audio_file(self, display_name, mime):
        return False

    def is_document_file(self, display_name, mime):
        return True


def _plan_md(n_tasks: int, pad_lines: int = 0) -> str:
    out = ["# Plan grande", ""]
    for i in range(1, n_tasks + 1):
        out.append(f"## Tarea {i}: modulo numero {i}")
        out.extend(f"Detalle {i}.{j}: mantener los invariantes y ejecutar las pruebas." for j in range(pad_lines))
        out.append(f"Criterios: el modulo {i} pasa sus pruebas.")
        out.append("")
    return "\n".join(out)


def test_a_60kb_plan_attachment_reaches_the_tracker_whole(tmp_path):
    import src.document_processor as dp
    body = _plan_md(40, pad_lines=24)
    assert len(body) > 55000
    f = tmp_path / "plan.md"
    f.write_text(body, encoding="utf-8")
    content = dp.build_user_content(
        "Sigue implementando el plan", ["a"], str(tmp_path),
        _Uploads({"a": {"path": str(f), "name": "plan.md", "mime": "text/markdown"}}), owner="t")
    assert isinstance(content, str)
    # the prompt copy IS truncated (shared 24000-character budget) ...
    assert "Tarea 40" not in content and len(content) < 26000
    # ... but the tracker gets every task through the stashed full text
    found = pt.find_plan_attachment(content)
    assert found is not None
    spec = pt.parse_plan(*found)
    assert len(spec.tasks) == 40
    assert spec.tasks[-1].title.startswith("Tarea 40")


def test_an_inline_copy_without_a_stash_still_works_as_before():
    inline = "Sigue implementando\n=== File: plan.md ===\n" + _plan_md(6, pad_lines=40)
    found = pt.find_plan_attachment(inline)
    assert found and len(pt.parse_plan(*found).tasks) == 6


def _zip(tmp_path: Path, members: dict, name="paquete.zip") -> Path:
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return z


def test_a_plan_inside_a_zip_is_visible_to_the_tracker(tmp_path):
    import src.document_processor as dp
    z = _zip(tmp_path, {
        "docs/plan.md": PASTED.replace("Ejecuta este plan en la carpeta de trabajo:", "# Plan pequeno"),
        "notes.txt": "nada que ver",
        "src/app.py": "print(1)",
    })
    content = dp.build_user_content(
        "Ejecuta el plan del zip", ["z"], str(tmp_path),
        _Uploads({"z": {"path": str(z), "name": "paquete.zip", "mime": "application/zip"}}), owner="t")
    assert "=== ZIP archive: paquete.zip ===" in content
    assert "notes.txt" not in content            # the archive stays compact: only plans are inlined
    assert "=== File: paquete.zip/docs/plan.md ===" in content and "Plan-Id:" in content
    found = pt.find_plan_attachment(content)
    assert found is not None
    assert found[0] == "paquete.zip/docs/plan.md"
    assert len(pt.parse_plan(*found).tasks) == 5


def test_a_zip_without_plans_stays_as_compact_as_before(tmp_path):
    import src.document_processor as dp
    z = _zip(tmp_path, {"README.md": "# Titulo\nhola\n", "a.txt": "x", "b.md": "# B\ntexto"})
    text = dp._process_zip_file(str(z), "x.zip")
    assert "README.md" not in text and "=== File:" not in text
    assert pt.find_plan_attachment(text) is None


def test_zip_members_are_read_in_memory_only_and_unsafe_names_are_ignored(tmp_path):
    import src.document_processor as dp
    z = _zip(tmp_path, {
        "../evil.md": PASTED, "/abs.md": PASTED, "C:/drive.md": PASTED, "a/../b.md": PASTED,
        "__MACOSX/._plan.md": PASTED, "ok/plan.md": PASTED,
    })
    members = dp._zip_text_members(zipfile.ZipFile(z), zipfile.ZipFile(z).infolist())
    assert [n for n, _t in members] == ["ok/plan.md"]
    assert not (tmp_path.parent / "evil.md").exists()


def test_big_and_encrypted_zip_members_are_skipped(tmp_path, monkeypatch):
    import src.document_processor as dp
    monkeypatch.setattr(dp, "ZIP_MEMBER_MAX_BYTES", 2000)
    z = _zip(tmp_path, {"big.md": "x" * 5000, "small.md": "# t\nx"})
    members = dp._zip_text_members(zipfile.ZipFile(z), zipfile.ZipFile(z).infolist())
    assert [n for n, _t in members] == ["small.md"]


# ---------------------------------------------------------------- decision

def _tracker(done=0, total=3):
    t = pt.upsert_from_attachment("sc", "plan.md", "\n".join(f"{i}. Tarea {i}" for i in range(1, total + 1)))
    for i in range(1, done + 1):
        t = pt.mark("sc", t["hash"], f"t0{i}", "done", "evidence of at least twenty chars")
    return t


def test_the_decision_covers_every_stop_condition():
    t = _tracker(done=1)
    d = pt.autocontinue_decision
    assert d(t)["action"] == "continue" and d(t)["task"]["id"] == "t02"
    assert d(t, enabled=False) == {"action": "none", "reason": "disabled"}
    assert d(None)["reason"] == "no_plan"
    assert d(t, armed=False)["reason"] == "not_armed"
    assert d(t, awaiting_user=True)["action"] == "none"          # an approval card is waiting
    assert d(t, recovery_active=True) == {"action": "stop", "reason": "loop_recovery", "task": t["tasks"][1]}
    assert d(t, used=40, cap=40)["reason"] == "cap"
    assert d(t, fruitless=3)["reason"] == "no_progress" and d(t, fruitless=2)["action"] == "continue"
    assert d(_tracker(done=3))["reason"] == "done"


def test_the_continuation_note_names_the_next_task_and_the_way_to_close_it():
    note = pt.continuation_note(_tracker(done=1))
    assert "1/3 tasks done" in note and "Next task: Tarea 2" in note
    assert "plan_task" in note and "plan_done" in note and "plan_skip" in note
    assert "do NOT ask the user whether to continue" in note


def test_the_policy_registry_knows_the_new_cause():
    from src.extra_round_policy import CAUSES, decide_extra_round
    assert CAUSES["plan_continue"].family == "completion"
    assert decide_extra_round("plan_continue", used=39, limit=40).granted
    assert not decide_extra_round("plan_continue", used=40, limit=40).granted


def test_the_settings_exist_with_their_defaults_and_schema_entries():
    from src.settings import DEFAULT_SETTINGS
    from src.agent_settings_schema import schema_keys
    assert DEFAULT_SETTINGS["agent_plan_autocontinue"] is True
    assert DEFAULT_SETTINGS["agent_plan_autocontinue_max"] == 40
    assert DEFAULT_SETTINGS["agent_plan_tracker_paste_min_chars"] == 600
    for key in ("agent_plan_autocontinue", "agent_plan_autocontinue_max", "agent_plan_tracker_paste_min_chars"):
        assert key in schema_keys()


# ---------------------------------------------------------------- the loop

import os  # noqa: E402

import src.agent_loop as al  # noqa: E402
from tests.test_agent_harness_functional import _patch_common, _scripted_stream, _collect, _events  # noqa: E402

SETTINGS = {"agent_project_tests": False, "agent_ui_smoke": False}
THREE = "Ejecuta este plan:\n\n1. Crear a.txt con uno\n2. Crear b.txt con dos\n3. Crear c.txt con tres\n"
SEVEN = "Ejecuta este plan:\n\n" + "\n".join(f"{i}. Crear f{i}.txt" for i in range(1, 8)) + "\n"


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    return str(w)


def _plan_exec(scope):
    """plan_* tools backed by the real tracker; every other tool answers ok."""
    def _exec(block):
        if block.tool_type in ("plan_done", "plan_skip"):
            args = json.loads(block.content)
            tracker = pt.active(scope)
            status = "done" if block.tool_type == "plan_done" else "skipped"
            pt.mark(scope, tracker["hash"], args["id"], status, args.get("evidence") or args.get("reason") or "")
            return {"output": f"{args['id']} marked {status}", "exit_code": 0}
        if block.tool_type == "plan_task":
            return {"output": "task text", "exit_code": 0}
        return None
    return _exec


def _done(task, what="file created and read back with the expected text"):
    return (f'```plan_done\n{json.dumps({"id": task, "evidence": what})}\n```', "tool_calls")


def _task(task):
    return (f'```plan_task\n{json.dumps({"id": task})}\n```', "tool_calls")


def _run_loop(ws, user, project="proj-ac", max_rounds=40, history=(), **kwargs):
    opts = {"trusted_workspace": os.path.realpath(ws), "project_id": project}
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b", [*history, {"role": "user", "content": user}],
        max_rounds=max_rounds, relevant_tools={"read_file", "edit_file", "glob", "ask_user"},
        workspace=ws, session_id="sess-func", harness_options=opts, **kwargs)
    return _events(_collect(gen))


def _checks(events, status):
    return [e for e in events if e.get("type") == "harness_check" and e.get("status") == status]


def test_the_model_stops_after_task_one_and_the_turn_carries_on_until_three_of_three(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    calls = _scripted_stream(monkeypatch, [
        _task("t01"), _done("t01"),
        ("Hecho 1 de 3. \u00bfContin\u00fao con la siguiente?", "stop"),     # asks instead of going on
        _task("t02"), _done("t02"),
        ("Tarea 2 hecha.", "stop"),                                        # stops without asking
        _task("t03"), _done("t03"),
        ("Plan completado: las tres tareas est\u00e1n hechas.", "stop"),
    ])
    events = _run_loop(ws, THREE)

    cont = _checks(events, "plan_continue")
    assert len(cont) == 2, [e.get("status") for e in events if e.get("type") == "harness_check"]
    assert [c["attempt"] for c in cont] == [1, 2] and all(c["max_attempts"] == 40 for c in cont)
    assert all(c["cause"] == "plan_continue" and c["family"] == "completion" for c in cont)
    assert cont[0]["detail"].startswith("1/3") and cont[1]["detail"].startswith("2/3")
    progress = [e for e in events if e.get("type") == "plan_tracker"]
    assert progress[0]["done"] == 0 and progress[0]["total"] == 3
    assert progress[-1]["done"] == 3 and progress[-1]["total"] == 3
    assert pt.progress(pt.active(scope)) == {"done": 3, "total": 3, "skipped": 0, "pending": 0}
    assert calls["n"] == 9
    assert not [e for e in events if e.get("type") == "ask_user"]
    # the model was told which task is next, and that the plan is the go-ahead
    notes = [m for m in calls["messages"][3] if m.get("_harness_note") and "Next task" in str(m.get("content"))]
    assert notes and "Crear b.txt" in notes[-1]["content"]
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert any(n.startswith("plan_continue@") for n in summary["notes"])
    # "\u00bfContin\u00fao?" was dropped from what the person reads
    replaced = [e for e in events if e.get("type") == "response_replace"]
    assert not any("\u00bfContin\u00fao" in e.get("text", "") for e in replaced)


def test_three_fruitless_continuations_preserve_pending_work_without_a_user_gate(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    calls = _scripted_stream(monkeypatch, [("Sigo dandole vueltas, de momento no he tocado nada.", "stop")])
    events = _run_loop(ws, THREE)
    assert len(_checks(events, "plan_continue")) == 3
    stop = _checks(events, "plan_continue_stop")
    assert len(stop) == 1 and stop[0]["detail"] == "no_progress"
    assert not [e for e in events if e.get("type") == "ask_user"]
    advisory = _checks(events, "advisory")
    assert len(advisory) == 1
    assert "0/3" in advisory[0]["detail"] and "Crear a.txt" in advisory[0]["detail"]
    assert advisory[0]["requires_user_input"] is False
    assert calls["n"] == 4                       # 1 + 3 continuations, then it stops
    assert pt.progress(pt.active(scope))["done"] == 0


def test_a_task_closed_between_continuations_resets_the_no_progress_count(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    _scripted_stream(monkeypatch, [
        ("nada", "stop"), ("nada", "stop"),            # 2 fruitless continuations
        _done("t01"), ("uno", "stop"),                 # closes one: counter back to 0
        ("nada", "stop"), ("nada", "stop"),            # 2 more fruitless
        _done("t02"), _done("t03"), _done("t04"), _done("t05"), _done("t06"), _done("t07"),
        ("todo hecho", "stop"),
    ])
    events = _run_loop(ws, SEVEN)
    assert not _checks(events, "plan_continue_stop")
    assert pt.progress(pt.active(scope))["done"] == 7
    assert not [e for e in events if e.get("type") == "ask_user"]


def test_the_per_turn_cap_reports_an_advisory_without_asking(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings={**SETTINGS, "agent_plan_autocontinue_max": 2}, tool_exec=_plan_exec(scope))
    _scripted_stream(monkeypatch, [
        _done("t01"), ("uno", "stop"), _done("t02"), ("dos", "stop"), _done("t03"), ("tres", "stop"),
    ])
    events = _run_loop(ws, SEVEN)
    assert len(_checks(events, "plan_continue")) == 2
    stop = _checks(events, "plan_continue_stop")
    assert len(stop) == 1 and stop[0]["detail"] == "cap"
    assert stop[0]["max_attempts"] == 2
    assert not [e for e in events if e.get("type") == "ask_user"]
    notice = _checks(events, "advisory")[0]
    assert "3/7" in notice["detail"] and notice["plan"]["cap"] == 2
    assert pt.progress(pt.active(scope))["done"] == 3


def test_with_the_setting_off_the_turn_ends_as_it_always_did(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings={**SETTINGS, "agent_plan_autocontinue": False}, tool_exec=_plan_exec(scope))
    calls = _scripted_stream(monkeypatch, [_done("t01"), ("Hecho 1 de 3.", "stop"), _done("t02"), ("x", "stop")])
    events = _run_loop(ws, THREE)
    assert calls["n"] == 2
    assert not _checks(events, "plan_continue") and not _checks(events, "plan_continue_stop")
    assert pt.progress(pt.active(scope))["done"] == 1


def test_stop_during_a_continuation_ends_the_turn_at_once(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    calls = _scripted_stream(monkeypatch, [("Hecho 1.", "stop")])
    peer = {"finished": False}
    scripted_stream = al.stream_llm_with_fallback

    async def terminal_peer(*args, **kwargs):
        async for chunk in scripted_stream(*args, **kwargs):
            if '"finish_reason": "stop"' in chunk:
                peer["finished"] = True
            yield chunk

    monkeypatch.setattr(al, "stream_llm_with_fallback", terminal_peer)

    def pending_cancel():
        # Preparation also polls: cancel after the peer's terminal response,
        # not after an arbitrary number of polls before the first generation.
        return "stop" if peer["finished"] else None

    events = _run_loop(ws, THREE, pending_cancel=pending_cancel)
    assert [e for e in events if e.get("type") == "cancelled"]
    assert calls["n"] == 1
    assert peer["finished"]


def test_a_steer_arriving_at_the_end_of_a_round_goes_first_and_the_plan_goes_on_after_it(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    calls = _scripted_stream(monkeypatch, [("Hecho 1.", "stop"), _done("t01"), ("ok", "stop")])
    # the queue is read at the top of a round and again once the reply is in:
    # the steer lands right after the model ended its round
    queue = [[], [], [{"text": "usa mejor el nombre d.txt para el primero"}]]

    def pending_user_messages():
        return queue.pop(0) if queue else []

    events = _run_loop(ws, THREE, pending_user_messages=pending_user_messages)
    assert [e for e in events if e.get("type") == "steer"]
    # the person's words reach the very next model call, as the last message ...
    second_round = calls["messages"][1]
    assert "d.txt" in str(second_round[-1].get("content")) and second_round[-1].get("role") == "user"
    # ... and the plan is still carried on afterwards, once that round ends without tools
    assert _checks(events, "plan_continue")
    assert any(m.get("_harness_note") and "Next task" in str(m.get("content")) for m in calls["messages"][3])


def test_a_question_to_the_user_waits_for_the_answer_instead_of_continuing(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    ask = '```ask_user\n{"question": "Uso a.txt o a.md?", "options": ["a.txt", "a.md"]}\n```'

    def _exec(block):
        if block.tool_type == "ask_user":
            return {"output": "asked", "exit_code": 0, "ask_user": {
                "question": "Uso a.txt o a.md?", "options": [{"label": "a.txt"}, {"label": "a.md"}],
                "question_id": "qst_test"}}
        return _plan_exec(scope)(block)

    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_exec)
    calls = _scripted_stream(monkeypatch, [(ask, "tool_calls"), ("no deberia llegar aqui", "stop")])
    events = _run_loop(ws, THREE)
    assert [e for e in events if e.get("type") == "ask_user"]
    assert calls["n"] == 1                          # the card waits; nothing is sent on its behalf
    assert not _checks(events, "plan_continue") and not _checks(events, "plan_continue_stop")


def test_a_stale_plan_from_another_chat_does_not_drag_an_unrelated_turn_along(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    pt.upsert_from_attachment(scope, "old.md", "\n".join(f"{i}. Tarea vieja {i}" for i in range(1, 5)))
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    calls = _scripted_stream(monkeypatch, [("Un conejo entra en un bar...", "stop")])
    events = _run_loop(ws, "hola, gracias por lo de ayer")
    assert calls["n"] == 1
    assert not _checks(events, "plan_continue")


def test_reading_plan_status_does_not_authorize_execution_of_an_old_plan(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    tracker = pt.upsert_from_attachment(scope, "old.md", "1. Crear a.txt\n2. Crear b.txt\n3. Crear c.txt")
    def read_status(block):
        if block.tool_type == "plan_status":
            return {"output": "3 tareas pendientes", "exit_code": 0}
        return _plan_exec(scope)(block)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=read_status)
    calls = _scripted_stream(monkeypatch, [
        ('```plan_status\n{}\n```', "tool_calls"), ("Hay tres tareas pendientes.", "stop"),
    ])
    events = _run_loop(ws, "Consulta el estado del plan anterior sin ejecutar sus tareas.")
    assert calls["n"] == 2
    assert not _checks(events, "plan_continue") and not _checks(events, "plan_continue_stop")
    assert not [e for e in events if e.get("type") == "ask_user"]
    assert pt.progress(pt.load(scope, tracker["hash"]))["done"] == 0


def test_a_failed_plan_mutator_does_not_arm_a_consult_only_turn(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    tracker = pt.upsert_from_attachment(scope, "old.md", "1. Crear a.txt\n2. Crear b.txt\n3. Crear c.txt")
    def fail_mutation(block):
        if block.tool_type == "plan_done":
            return {"error": "unknown task", "exit_code": 1}
        return _plan_exec(scope)(block)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=fail_mutation)
    calls = _scripted_stream(monkeypatch, [
        ('```plan_done\n{"id":"unknown","evidence":"no task was changed by this failed call"}\n```', "tool_calls"),
        ("La llamada no cambió ninguna tarea.", "stop"),
    ])
    events = _run_loop(ws, "Consulta el estado del plan sin ejecutarlo.")
    assert calls["n"] == 2 and not _checks(events, "plan_continue")
    assert not [e for e in events if e.get("type") == "ask_user"]
    assert pt.progress(pt.load(scope, tracker["hash"]))["done"] == 0


def test_a_new_request_does_not_reimport_a_historical_attachment(ws, monkeypatch):
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(pt.scope_for("proj-ac", ws)))
    calls = _scripted_stream(monkeypatch, [("Resumen solicitado del estado de este proyecto.", "stop")])
    old = "Ejecuta este plan:\n=== File: old-plan.md ===\n" + THREE.split("\n\n", 1)[1]
    history = [{"role": "user", "content": old}, {"role": "assistant", "content": "Recibido."}]
    events = _run_loop(ws, "Dame un resumen sin ejecutar ninguna tarea.", history=history)
    # The independent no-action check may request a source read once; it must
    # not import the old attachment or continue its plan as the current job.
    assert calls["n"] <= 2
    assert pt.active(pt.scope_for("proj-ac", ws)) is None
    assert not [e for e in events if e.get("type") in {"plan_tracker", "ask_user"}]
    assert not _checks(events, "plan_continue")


def test_a_continue_message_resumes_the_stored_plan_inside_the_turn(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    pt.upsert_from_attachment(scope, "plan.md", "\n".join(f"{i}. Tarea {i}" for i in range(1, 4)))
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    _scripted_stream(monkeypatch, [_done("t01"), ("uno", "stop"), _done("t02"), _done("t03"), ("fin", "stop")])
    events = _run_loop(ws, "contin\u00faa")
    assert len(_checks(events, "plan_continue")) == 1
    assert pt.progress(pt.active(scope))["done"] == 3


def test_no_plan_no_change(ws, monkeypatch):
    _patch_common(monkeypatch, settings=SETTINGS)
    calls = _scripted_stream(monkeypatch, [("Hola, \u00bfen qu\u00e9 te ayudo?", "stop")])
    events = _run_loop(ws, "hola")
    assert calls["n"] == 1
    assert not _checks(events, "plan_continue")
    assert not [e for e in events if e.get("type") == "plan_tracker"]


def test_a_plan_attached_in_a_zip_is_continued_inside_the_turn(ws, monkeypatch, tmp_path):
    import src.document_processor as dp
    z = _zip(tmp_path, {"plan.md": "# Plan\n\n1. Crear a.txt con uno\n2. Crear b.txt con dos\n3. Crear c.txt con tres\n"})
    content = dp.build_user_content(
        "Ejecuta el plan del zip", ["z"], str(tmp_path),
        _Uploads({"z": {"path": str(z), "name": "plan.zip", "mime": "application/zip"}}), owner="t")
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings=SETTINGS, tool_exec=_plan_exec(scope))
    _scripted_stream(monkeypatch, [
        _done("t01"), ("uno", "stop"), _done("t02"), ("dos", "stop"), _done("t03"), ("fin", "stop")])
    events = _run_loop(ws, content)
    assert len(_checks(events, "plan_continue")) == 2
    assert pt.progress(pt.active(scope))["done"] == 3


def test_the_final_progress_reaches_the_ui_even_when_the_model_closed_every_task_itself(ws, monkeypatch):
    scope = pt.scope_for("proj-ac", ws)
    _patch_common(monkeypatch, settings={**SETTINGS, "agent_plan_autocontinue": False}, tool_exec=_plan_exec(scope))
    _scripted_stream(monkeypatch, [
        _done("t01"), _done("t02"), _done("t03"), ("Las tres tareas est\u00e1n hechas.", "stop"),
    ])
    events = _run_loop(ws, THREE)
    progress = [e for e in events if e.get("type") == "plan_tracker"]
    assert progress[0]["done"] == 0
    assert progress[-1]["done"] == 3 and progress[-1]["total"] == 3
    assert not _checks(events, "plan_continue")
