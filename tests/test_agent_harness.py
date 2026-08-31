import json
import os

import pytest

from src import agent_harness as h


SCREENSHOT_CLAIM_ES = (
    "He creado las implementaciones que permiten eliminar proyectos y chats a través de la "
    "interfaz web con botones visuales, pero necesito verificar que todo esté correctamente "
    "integrado:\n- En ProjectCard.vue - Añadí el botón de eliminación de proyecto con confirmación\n"
    "- En SessionCard.vue - Añadí el botón de eliminación de sesión\n"
    "Todo está listo para funcionar según las especificaciones solicitadas."
)
SCREENSHOT_INTENT_ES = (
    "Voy a crear las implementaciones adicionales para los componentes de frontend que "
    "permitan la eliminación visual de proyectos y sesiones. Primero actualizamos el componente "
    "ProjectCard.vue para agregar el botón de eliminación:"
)


def test_mutation_claims_spanish_and_english():
    assert h.find_mutation_claims(SCREENSHOT_CLAIM_ES)
    assert h.find_mutation_claims("I've added the delete button to the card and updated the CSS.")
    assert h.find_mutation_claims("The implementation is now complete.")
    assert h.find_mutation_claims("Los cambios han sido aplicados correctamente.")
    assert h.find_mutation_claims("Done.")


def test_no_claims_in_plain_analysis_or_questions():
    assert not h.find_mutation_claims("The card component lives in static/js/projects.js and renders a grid.")
    assert not h.find_mutation_claims("¿Quieres que añada el botón en la tarjeta o en el menú?")
    assert not h.find_mutation_claims("To add a button you would edit projects.js.")


def test_intent_announcement_spanish_and_english_and_colon_tail():
    assert h.find_intent_announcement(SCREENSHOT_INTENT_ES)
    assert h.find_intent_announcement("Let me check the logs to see what failed.")
    assert h.find_intent_announcement("I'll start by editing the card component.")
    assert h.find_intent_announcement("Ahora necesito revisar el archivo projects.js para entender la interfaz")
    # A colon at the very end introducing content that never came.
    assert h.find_intent_announcement("Here is the updated component:")
    # Questions legitimately end a turn.
    assert h.find_intent_announcement("Should I edit projects.js or sessions.js?") is None
    # Earlier announcement followed by a real closing paragraph is fine.
    assert h.find_intent_announcement("Let me check the file.\n\nThe file has 3 functions and no bugs.") is None


def test_path_token_extraction():
    toks = h.extract_path_tokens(
        "Edited static/js/projects.js and ProjectCard.vue; see docs at https://x.com/a.js and Node.js v1.2 e.g. file."
    )
    assert "static/js/projects.js" in toks
    assert "ProjectCard.vue" in toks
    assert all(t.lower() != "node.js" for t in toks)
    assert not any(t.startswith("x.com") for t in toks)


def test_ledger_rejects_claims_without_mutation(tmp_path):
    ws = tmp_path
    (ws / "static" / "js").mkdir(parents=True)
    (ws / "static" / "js" / "projects.js").write_text("export const x = 1;\n", encoding="utf-8")
    ledger = h.TurnLedger(str(ws), "Quiero botones en las tarjetas para borrar proyectos")
    ledger.record("read_file", '{"path": "ProjectCard.vue"}', {"error": "read_file: not found", "exit_code": 1}, 1)
    check = ledger.check_completion(SCREENSHOT_CLAIM_ES)
    assert not check["ok"]
    assert "claims_without_mutation" in check["reasons"]
    assert "fabricated_paths" in check["reasons"]
    assert "ProjectCard.vue" in check["bad_paths"]
    msg = ledger.rejection_message(check)
    assert "NONE" in msg and "ProjectCard.vue" in msg
    note = ledger.user_note(check, final=True)
    assert "ninguno" in note  # localized to Spanish


def test_ledger_accepts_claims_backed_by_mutation(tmp_path):
    ws = tmp_path
    (ws / "static" / "js").mkdir(parents=True)
    (ws / "static" / "js" / "projects.js").write_text("export const x = 1;\n", encoding="utf-8")
    ledger = h.TurnLedger(str(ws), "add a delete button")
    ledger.record("edit_file", '{"path": "static/js/projects.js", "old_string": "1", "new_string": "2"}',
                  {"output": "Edited static/js/projects.js (1 replacement)", "exit_code": 0}, 1)
    check = ledger.check_completion("I've added the delete button in static/js/projects.js.")
    assert check["ok"], check
    assert ledger.mutated_paths() == ["static/js/projects.js"]


def test_intent_only_round_is_flagged():
    ledger = h.TurnLedger(None, "hazlo")
    check = ledger.check_completion(SCREENSHOT_INTENT_ES)
    assert "intent_without_action" in check["reasons"] or "fabricated_paths" in check["reasons"]


def test_question_with_unknown_paths_is_allowed(tmp_path):
    ledger = h.TurnLedger(str(tmp_path), "help")
    check = ledger.check_completion("Should I create NewWidget.vue for this?")
    assert check["ok"], check


def test_shell_mutation_detection():
    assert h.shell_command_looks_mutating("sed -i 's/a/b/' x.py")
    assert h.shell_command_looks_mutating("git commit -am msg")
    assert h.shell_command_looks_mutating("echo hi > out.txt")
    assert not h.shell_command_looks_mutating("ls -la src")
    assert not h.shell_command_looks_mutating("git status --short")
    assert not h.shell_command_looks_mutating("python -m py_compile app.py")


def test_suggest_paths_and_not_found_error(tmp_path):
    ws = tmp_path
    (ws / "static" / "js").mkdir(parents=True)
    (ws / "static" / "js" / "projects.js").write_text("x", encoding="utf-8")
    (ws / "static" / "js" / "sessions.js").write_text("x", encoding="utf-8")
    (ws / "node_modules" / "junk").mkdir(parents=True)
    (ws / "node_modules" / "junk" / "ProjectCard.vue").write_text("x", encoding="utf-8")
    sugg = h.suggest_paths(str(ws), "src/components/Projects.vue")
    assert "static/js/projects.js" in sugg
    assert not any("node_modules" in s for s in sugg)
    err = h.not_found_error("read_file", "Projects.vue", str(ws / "Projects.vue"), str(ws))
    assert "does not exist" in err and "static/js/projects.js" in err and "glob" in err


def test_progress_verification():
    ledger = h.TurnLedger(None, "do stuff")
    todos = [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}]
    ledger.record_progress(todos, 1)
    # No tool ran → marking 'a' completed is unverified
    out = ledger.record_progress([{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}], 2)
    assert out[0]["verified"] is False
    ledger.record("edit_file", '{"path": "x.py", "old_string": "a", "new_string": "b"}', {"output": "ok", "exit_code": 0}, 3)
    out = ledger.record_progress([{"content": "a", "status": "completed"}, {"content": "b", "status": "completed"}], 3)
    assert out[1]["verified"] is True and out[1]["mutation_backed"] is True
    assert out[0]["verified"] is False  # keeps its original verdict


def test_detect_language():
    assert h.detect_language("Quiero también botones para borrar los chats") == "es"
    assert h.detect_language("Please add a delete button to the cards") == "en"


def test_qwen_coder_function_markup_is_parsed_and_stripped():
    from src.agent_tools import parse_tool_blocks, strip_tool_blocks
    leaked = (
        "Let me check the existing skills first:\n\n"
        "<function=manage_skills>\n<parameter=action>\nlist\n</parameter>\n</function>\n</tool_call>"
    )
    blocks = parse_tool_blocks(leaked, skip_fenced=True)
    assert len(blocks) == 1 and blocks[0].tool_type == "manage_skills"
    assert "list" in blocks[0].content
    shown = strip_tool_blocks(leaked, skip_fenced=True)
    assert "<function" not in shown and "</tool_call>" not in shown
    # Real path-tool call with typed params
    leaked2 = (
        "<tool_call>\n<function=read_file>\n<parameter=path>\nstatic/js/projects.js\n</parameter>\n"
        "<parameter=offset>\n10\n</parameter>\n<parameter=limit>\n40\n</parameter>\n</function>\n</tool_call>"
    )
    blocks2 = parse_tool_blocks(leaked2, skip_fenced=True)
    assert len(blocks2) == 1 and blocks2[0].tool_type == "read_file"
    assert "static/js/projects.js" in blocks2[0].content


def test_static_check_files(tmp_path):
    good = tmp_path / "ok.py"
    good.write_text("x = 1\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n  pass\n", encoding="utf-8")
    badjson = tmp_path / "c.json"
    badjson.write_text("{oops}", encoding="utf-8")
    other = tmp_path / "notes.md"
    other.write_text("# hi", encoding="utf-8")
    res = h.static_check_files(["ok.py", "bad.py", "c.json", "notes.md", "missing.py"], str(tmp_path))
    by = {r["path"]: r for r in res}
    assert by["ok.py"]["ok"] is True
    assert by["bad.py"]["ok"] is False and by["bad.py"]["error"]
    assert by["c.json"]["ok"] is False
    assert "notes.md" not in by and "missing.py" not in by


def test_delegation_args_parsing():
    from src.agent_tools.subagent_tools import parse_delegation_args, MAX_SUBAGENTS
    args = parse_delegation_args('{"tasks": [{"name": "backend", "instruction": "add route"}, "write tests for it"], "parallel": false}')
    assert len(args["tasks"]) == 2 and args["parallel"] is False
    assert args["tasks"][1]["name"].startswith("write tests")
    many = parse_delegation_args(json.dumps({"tasks": [f"t{i}" for i in range(10)]}))
    assert len(many["tasks"]) == MAX_SUBAGENTS
    with pytest.raises(ValueError):
        parse_delegation_args('{"tasks": []}')
    with pytest.raises(ValueError):
        parse_delegation_args('not json')


def test_ledger_counts_worker_mutations_as_evidence(tmp_path):
    ledger = h.TurnLedger(str(tmp_path), "haz esto con varios agentes")
    ledger.record("delegate_agents", '{"tasks": ["a"]}', {
        "output": "report", "exit_code": 0,
        "subagents": [{"name": "backend", "mutations": ["server.py"]}, {"name": "ui", "mutations": []}],
    }, 1)
    assert "server.py" in ledger.mutated_paths()
    check = ledger.check_completion("Los workers han modificado server.py y el frontend queda pendiente.")
    assert check["ok"], check


def test_narrative_first_person_is_not_a_claim():
    story = ("He creado un personaje que vive en un faro. Al final hemos terminado la cena y "
             "todo está listo para la boda; ella dijo: hecho.")
    assert h.find_mutation_claims(story) == []
    # …but the same verbs about code still count
    assert h.find_mutation_claims("He creado la función renderCounter en el fichero projects.js.")
    assert h.find_mutation_claims("I've added the handler to the component.")


def test_unknown_paths_after_real_discovery_are_noted_not_rejected(tmp_path):
    (tmp_path / "server.py").write_text("x", encoding="utf-8")
    ledger = h.TurnLedger(str(tmp_path), "revisa el backend")
    ledger.record("read_file", "server.py", {"output": "import os", "exit_code": 0}, 1)
    check = ledger.check_completion("server.py define las rutas. Se podría añadir un utils.py para helpers.")
    assert check["ok"], check
    assert any(n.startswith("unverified_mentions:") and "utils.py" in n for n in ledger.notes)
    # …but presenting it as done without a mutation is still rejected.
    check2 = ledger.check_completion("He creado utils.py con los helpers.")
    assert "claims_without_mutation" in check2["reasons"] and "fabricated_paths" in check2["reasons"]


def test_bare_done_after_readonly_tool_work_is_a_report_not_a_claim():
    """'Done.' after a successful non-mutating tool (bash printf, read_file…)
    is honest; only a change *description* without a write is rejected."""
    ledger = h.TurnLedger(None, "Run one tool and report back.")
    ledger.record("bash", '{"command": "printf ok"}', {"output": "ok", "exit_code": 0}, 1)
    assert ledger.check_completion("done")["ok"]
    assert ledger.check_completion("Hecho.")["ok"]
    # …but describing edits that never happened is still a lie.
    bad = ledger.check_completion("Done. I've added the button in the component.")
    assert "claims_without_mutation" in bad["reasons"]


def test_bare_done_with_no_tool_activity_is_still_rejected():
    ledger = h.TurnLedger(None, "add the delete button")
    check = ledger.check_completion("Done.")
    assert "claims_without_mutation" in check["reasons"]
    # A failed tool call is not evidence either.
    ledger.record("edit_file", '{"path": "x.js", "old_string": "a", "new_string": "b"}', {"error": "not found", "exit_code": 1}, 1)
    assert "claims_without_mutation" in ledger.check_completion("Listo.")["reasons"]


def test_finishing_an_investigation_is_not_a_change_claim():
    """'I have completed the review' / 'He terminado el análisis' end a
    read-only task; only completing an *implementation* is a mutation claim."""
    for text in (
        "He terminado la revisión del código: el botón ya existe en projects.js.",
        "I have completed the analysis of the code. No changes were needed.",
        "Completé la lectura de los ficheros del proyecto.",
        "Terminé de revisar el código; no hay cambios.",
    ):
        assert h.find_mutation_claims(text) == [], text
    for text in (
        "I have completed the implementation of the delete button.",
        "He terminado de implementar el endpoint en server.py.",
    ):
        assert h.find_mutation_claims(text), text


def test_rejection_message_offers_already_exists_branch():
    ledger = h.TurnLedger(None, "add the delete button")
    ledger.record("read_file", '{"path": "static/js/projects.js"}', {"output": "x", "exit_code": 0}, 1)
    check = ledger.check_completion("I've added the delete button in the component.")
    msg = ledger.rejection_message(check)
    assert "ALREADY exists" in msg and "citing the file and lines" in msg


def test_delegation_report_flags_files_touched_by_two_workers(tmp_path):
    from src.agent_tools.subagent_tools import SubagentRun, _build_report_text
    a = SubagentRun(0, {"name": "backend", "instruction": "add route"})
    b = SubagentRun(1, {"name": "frontend", "instruction": "add button"})
    a.mutations = ["server.py", "static/js/app.js"]
    b.mutations = ["static\\js\\app.js"]  # same file, Windows separators
    a.stop_reason = b.stop_reason = "complete"
    text = _build_report_text([a, b], None)
    assert "MORE THAN ONE worker" in text
    assert "static/js/app.js: backend, frontend" in text
    assert "server.py" not in text.split("WARNING")[1]  # only the shared file is listed
    # no overlap → no warning
    b.mutations = ["README.md"]
    assert "MORE THAN ONE" not in _build_report_text([a, b], None)
