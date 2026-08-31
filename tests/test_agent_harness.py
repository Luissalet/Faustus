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
