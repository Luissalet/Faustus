import json
import os
import shutil
import sys

import pytest

from src import agent_harness as h
from src import agent_loop as al


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


def test_in_progress_execution_claims_count_as_announcements():
    """Seen live (ronda 6): a worker told to run `python -c "time.sleep(120)"`
    ended its round with 0 tool calls saying "El comando se está ejecutando.
    Esperaré a que termine el proceso" — a claim of an action IN PROGRESS
    that never started. Progressive / waiting phrasing is an announcement."""
    for text in (
        "El comando se está ejecutando. Esperaré a que termine el proceso.",
        "Estoy ejecutando el comando ahora mismo.",
        "El proceso está en marcha, espero a que termine.",
        "The command is running now, I will wait for it to finish.",
        "I am running the tests in the background.",
        "Waiting for the process to complete.",
    ):
        assert h.find_intent_announcement(text), text
    # Past-tense reports of a real run are not announcements (they are claims,
    # handled elsewhere) — and a question still ends a turn legitimately.
    assert h.find_intent_announcement("¿Quieres que ejecute el comando ahora?") is None


def test_negated_or_indirect_progressives_are_statements_not_announcements():
    """Seen live (ronda 6, two-GPU box): "No puedo saber en cuántas GPUs
    estoy corriendo; la capital de Portugal es Lisboa" ended the turn and
    the harness flagged "estoy corriendo" as an announced action — a second
    round at 0.7 tok/s to re-answer a one-liner. A progressive inside a
    negation or an indirect question describes STATE; nothing was announced."""
    for text in (
        "No puedo saber en cuántas GPUs estoy corriendo; la capital de Portugal es Lisboa.",
        "No sé si estoy ejecutando la versión buena del script.",
        "I can't tell how many GPUs I'm running on. The capital of Portugal is Lisbon.",
        "I don't know whether the job is running now.",
        "Nunca estoy ejecutando dos procesos a la vez.",
    ):
        assert h.find_intent_announcement(text) is None, text
    # the same progressive without the negation / question is still caught,
    # and a negation in an EARLIER sentence does not shield it
    assert h.find_intent_announcement("Estoy corriendo los tests ahora.")
    assert h.find_intent_announcement("No hay problema. Estoy ejecutando el comando ahora mismo.")


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
    assert "ui_unverified" in check["reasons"]
    ledger.record("mcp__builtin_browser__browser_snapshot", "", {"output": "ok", "exit_code": 0}, 2)
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


PERMISSION_STALL = (
    "The star IoU failure has now survived three task gates — do you want it "
    "investigated as a real defect, or formally accepted as a known limitation "
    "in the baseline?\n\n"
    "Next in the plan is task 04. Say the word and I'll continue."
)


def test_permission_to_continue_is_a_stall_not_a_question():
    """Seen live: Continue already given, the model asked for a green light
    about a pre-existing test and stopped. A trailing `?` is not a legitimate
    stop when the question is 'may I keep going?'."""
    assert h.find_permission_stall(PERMISSION_STALL)
    assert h.find_permission_stall("Ready for task 04 whenever you want it.")
    assert h.find_permission_stall("Dime y continúo con la siguiente tarea.")
    # Real design questions still end a turn.
    assert h.find_permission_stall("Should I edit projects.js or sessions.js?") is None
    assert h.find_permission_stall("¿Quieres que añada el botón en la tarjeta o en el menú?") is None
    ledger = h.TurnLedger(None, "Continue")
    ledger.record(
        "edit_file", '{"path": "x.py", "old_string": "a", "new_string": "b"}',
        {"output": "Edited x.py", "exit_code": 0}, 1,
    )
    check = ledger.check_completion(PERMISSION_STALL)
    assert "asked_instead_of_continuing" in check["reasons"], check
    msg = ledger.rejection_message(check)
    assert "permission" in msg.lower() and "ask_user" in msg
    assert "Nothing you described has happened" not in msg
    assert "stands" in msg
    # ask_user is the legitimate channel; prose is not.
    ledger.asked_user = True
    check2 = ledger.check_completion(PERMISSION_STALL)
    assert "asked_instead_of_continuing" not in check2["reasons"]
    assert h.TurnLedger.check_is_stall_only(
        {"reasons": ["asked_instead_of_continuing"]}
    )
    assert not h.TurnLedger.check_is_stall_only(
        {"reasons": ["asked_instead_of_continuing", "claimed_paths_untouched"]}
    )
    assert not h.TurnLedger.check_is_stall_only({"reasons": ["claims_without_mutation"]})
    assert not h.TurnLedger.check_is_stall_only({"reasons": []})


def test_stderr_to_dev_null_is_not_a_mutation():
    """Seen live: `find … | xargs grep … 2>/dev/null` was recorded as a mutation
    of 'cards.js' (the redirect matched the write pattern) and the turn summary
    listed a file change that never happened."""
    assert not h.shell_command_looks_mutating('find . -name "*.js" | xargs grep -l "cards.js" 2>/dev/null || echo none')
    assert not h.shell_command_looks_mutating("ls > /dev/null")
    assert not h.shell_command_looks_mutating("python -m py_compile x.py 2>&1")
    assert h.shell_command_looks_mutating("echo x > out.txt")
    assert h.shell_command_looks_mutating("cat a >> b.log")


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


def test_progress_list_is_complete_and_tools_since():
    assert h.progress_list_is_complete(None) is False
    assert h.progress_list_is_complete([]) is False
    assert h.progress_list_is_complete([{"content": "a", "status": "completed"}]) is True
    assert h.progress_list_is_complete([
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "in_progress"},
    ]) is False
    ledger = h.TurnLedger(None, "do stuff")
    assert ledger.tools_since_progress() == 0
    ledger.record_progress([{"content": "a", "status": "completed"}], 1)
    assert ledger.tools_since_progress() == 0
    ledger.record("grep", '{"pattern": "x"}', {"output": "ok", "exit_code": 0}, 2)
    assert ledger.tools_since_progress() == 1


def test_detect_language():
    assert h.detect_language("Quiero también botones para borrar los chats") == "es"
    assert h.detect_language("Please add a delete button to the cards") == "en"


@pytest.mark.parametrize("text", [
    "Fix the login bug", "Add a dark mode toggle", "Refactor the parser",
    "Why is the test failing?", "Please run the linter", "Deploy to staging",
    "Write unit tests for the cache layer", "Remove the dead code in utils",
    "The button does not respond on mobile", "Can you check the logs?",
    "Rename this variable", "Update dependencies", "make it faster",
    "install the package", "commit and push", "show me the diff",
    "read the config file", "Set up CI", "Bump the version", "git status",
    "it crashed again", "the server returns 500", "explain the error",
])
def test_english_requests_are_still_english(text):
    """The answers the hand-rolled ES/EN heuristic gave, pinned.

    Delegating to the shared detector must never start wording an English
    developer's harness note in Spanish; every one of these read "en" before.
    """
    assert h.detect_language(text) == "en"


@pytest.mark.parametrize("text", [
    "Quiero también botones para borrar los chats",
    "Arregla el error de login",          # no accent, no stopword the old
    "Borra el codigo muerto en utils",    # list knew: these used to read "en"
    "el servidor devuelve 500",
    "Crea un componente nuevo",
    "¿Puedes revisar el archivo?",
    "El niño juega",
])
def test_spanish_requests_read_as_spanish(text):
    assert h.detect_language(text) == "es"


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


def test_negated_statements_are_not_claims():
    """'No he modificado nada' is the opposite of a claim (seen live with
    qwen3.8: the read-only answer was rejected for it)."""
    for text in (
        "Las tarjetas se renderizan en projects.js. No he modificado nada.",
        "Todavía no he modificado ningún fichero; primero necesito leer el código.",
        "No se ha modificado ningún archivo del proyecto.",
        "I have not modified any files in the repo.",
        "Nothing has been changed in the code yet.",
    ):
        assert h.find_mutation_claims(text) == [], text
    # A leading "No," answering a question does not negate the claim that follows.
    assert h.find_mutation_claims("No, he modificado el fichero server.py como pediste.")
    assert h.find_mutation_claims("He modificado el fichero server.py.")


def test_spanish_coding_request_is_not_low_signal():
    """The agent-intent domains are English keywords; 'Arregla el fallo que hay
    al borrar' matched none, was low-signal and got read-only tools even with
    a workspace bound (seen live on the bench)."""
    import src.agent_loop as al
    for text in ("Arregla el fallo que hay al borrar.", "Corrige el problema del contador",
                 "Añade botones para eliminar proyectos"):
        r = al._classify_agent_request([{"role": "user", "content": text}], text)
        assert not r["low_signal"] and "files" in r["domains"], text
    for text in ("hola", "gracias", "Explica qué es un closure"):
        r = al._classify_agent_request([{"role": "user", "content": text}], text)
        assert r["low_signal"], text


def test_workspace_runtime_error_report_is_actionable_coding_intent():
    text = "When I press process: Error: Mesh is not watertight (unpaired: 46728)"
    result = al._classify_agent_request([{"role": "user", "content": text}], text)
    assert result["low_signal"] is False
    assert "files" in result["domains"]


@pytest.mark.parametrize("text", [
    "Sigue",
    "Continúa",
    "Hazlo",
    "Still same fucking problem. THINK",
    "Same error again",
    "Sigue exactamente igual, el mismo fallo",
    "Sigue implementando el plan",
    "Continua implementando el plan del zip",
    "Implementar todo lo posible",
    "Keep going until the plan is finished",
])
def test_problem_followups_inherit_the_active_task(text):
    assert al._is_explicit_continuation(text)


def test_sigue_implementando_is_not_low_signal():
    """Live Silhouettes failure: this follow-up was low_signal with domains=[]."""
    messages = [
        {"role": "user", "content": "Implement the plan === File: x.md ===\nplan body"},
        {"role": "assistant", "content": "Voy a empezar."},
        {"role": "user", "content": "Sigue implementando el plan"},
    ]
    result = al._classify_agent_request(messages, messages[-1]["content"])
    assert result["continuation"] is True
    assert result["low_signal"] is False
    assert "files" in result["domains"]


def test_implementar_todo_lo_posible_is_files_not_notes():
    """Spanish 'todo lo posible' must not match English todos → notes domain."""
    text = "Implementar todo lo posible"
    assert al._looks_like_workspace_coding_request(text)
    result = al._classify_agent_request([{"role": "user", "content": text}], text)
    assert result["low_signal"] is False
    assert "files" in result["domains"]
    assert "notes_calendar_tasks" not in result["domains"]


def test_same_problem_followup_routes_back_to_workspace_tools():
    messages = [
        {"role": "user", "content": "Error: Mesh is not watertight when I press Process"},
        {"role": "assistant", "content": "I will inspect the mesh pipeline."},
        {"role": "user", "content": "Still same fucking problem. THINK"},
    ]
    result = al._classify_agent_request(messages, messages[-1]["content"])
    assert result["continuation"] is True
    assert result["low_signal"] is False
    assert "files" in result["domains"]


def test_whole_file_rewrite_is_noted(tmp_path):
    ledger = h.TurnLedger(str(tmp_path), "arregla el fallo al borrar")
    # a write_file that replaced an existing file and dropped many lines
    ledger.record("write_file", '{"path": "static/js/app.js", "content": "x"}',
                  {"output": "Wrote 10 bytes", "exit_code": 0,
                   "diff": {"text": "...", "added": 1, "removed": 42, "new_file": False, "file": "app.js"}}, 1)
    assert "whole_file_rewrite:static/js/app.js" in ledger.notes
    assert ledger.mutated_paths() == ["static/js/app.js"]  # still real evidence
    # a new file, or a rewrite that keeps the content, is not flagged
    ledger.record("write_file", '{"path": "new.py", "content": "x"}',
                  {"output": "Wrote", "exit_code": 0, "diff": {"added": 3, "removed": 0, "new_file": True}}, 2)
    ledger.record("write_file", '{"path": "kept.js", "content": "x"}',
                  {"output": "Wrote", "exit_code": 0, "diff": {"added": 12, "removed": 1, "new_file": False}}, 3)
    assert [n for n in ledger.notes if n.startswith("whole_file_rewrite")] == ["whole_file_rewrite:static/js/app.js"]


def test_subagent_worker_chat_is_flagged_busy_while_running(monkeypatch):
    """The worker chat of delegate_agents has no detached run of its own; it is
    marked busy for the sidebar dot while the worker runs and released after."""
    import asyncio
    import json
    import src.agent_loop as al
    from src import agent_runs
    from src.agent_tools import subagent_tools as st

    seen = {}

    async def fake_loop(*args, **kwargs):
        seen["busy_during"] = agent_runs.active_session_ids()
        yield 'data: {"type": "tool_start", "tool": "read_file", "command": "x"}\n\n'
        yield 'data: {"type": "tool_output", "tool": "read_file", "command": "x", "output": "ok", "exit_code": 0}\n\n'
        yield f'data: {json.dumps({"type": "harness_summary", "data": {"stop_reason": "complete", "mutations": []}})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", fake_loop)
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: None)  # no child chat persistence

    events = []

    async def emit(p):
        events.append(p)

    run = st.SubagentRun(0, {"name": "worker", "instruction": "read the code"})
    agent_runs._EXTERNAL_BUSY.clear()
    asyncio.run(st._run_subagent(
        run, endpoint_url="http://x/v1", model="m", headers=None, owner="luis",
        workspace=None, workspace_roots=None, max_rounds=3, shared_context="",
        parent_session_id="parent", emit=emit,
    ))
    assert run.session_id and run.session_id in seen["busy_during"]
    assert run.session_id not in agent_runs.active_session_ids()
    assert [e["event"] for e in events] == ["started", "tool", "tool", "done"]
    assert run.tool_calls == 1 and run.stop_reason == "complete"


def test_target_substitution_requires_an_honest_answer(tmp_path):
    """User names a file that does not exist; the model edits another file and
    reports it as the fix without saying the named file is missing (t4 live)."""
    (tmp_path / "static" / "js").mkdir(parents=True)
    (tmp_path / "static" / "js" / "projects.js").write_text("export function cardHtml(p) { return p.name; }\n", encoding="utf-8")
    led = h.TurnLedger(str(tmp_path), "Arregla el bug de static/js/cards.js que hace que no se muestre el nombre del proyecto")
    assert led.user_missing_paths() == ["static/js/cards.js"]
    # nothing changed yet → nothing to explain
    assert led.check_target_substitution("No he tocado nada, cards.js no existe.") is None
    led.record("edit_file", '{"path": "static/js/projects.js", "old_string": "p.name", "new_string": "p.name || \'x\'"}',
               {"output": "Edited", "exit_code": 0}, 3)
    silent = "He arreglado el bug: ahora cardHtml usa un valor por defecto cuando falta el nombre."
    chk = led.check_target_substitution(silent)
    assert chk and chk["missing"] == ["static/js/cards.js"] and chk["changed"] == ["static/js/projects.js"]
    msg = led.target_substitution_message(chk)
    assert "static/js/cards.js" in msg and "does NOT exist" in msg and "ask_user" in msg
    # honest variants pass
    for ok_text in (
        "static/js/cards.js no existe en el proyecto; las tarjetas se generan en static/js/projects.js, donde he añadido un valor por defecto.",
        "Note: cards.js does not exist — the card markup lives in projects.js, which I changed instead.",
        "I could not find cards.js, so I edited projects.js (the file that renders the cards).",
    ):
        assert led.check_target_substitution(ok_text) is None, ok_text
    # asking the user also counts
    led.record("ask_user", '{"question": "¿Te refieres a projects.js?"}', {"output": "asked", "exit_code": None}, 4)
    assert led.check_target_substitution(silent) is None


def test_target_substitution_ignores_files_created_this_turn(tmp_path):
    """'Create utils/helpers.py' — the named path did not exist but the model
    created it: nothing to acknowledge."""
    led = h.TurnLedger(str(tmp_path), "Crea utils/helpers.py con una función slugify")
    led.record("write_file", '{"path": "utils/helpers.py", "content": "def slugify(s): return s"}',
               {"output": "Wrote", "exit_code": 0, "diff": {"new_file": True, "added": 1, "removed": 0, "text": "+x"}}, 1)
    assert led.user_missing_paths() == []
    assert led.check_target_substitution("He creado utils/helpers.py con slugify.") is None


def test_path_tokens_take_the_whole_extension():
    """`data.json` used to be extracted as `data.js` (alternation `js` matched
    first with no trailing boundary) — a phantom missing file that could fail
    the fabricated-path and substituted-target checks (seen live on t3)."""
    assert h.extract_path_tokens("leyendo data.json con _load(). Solo server.py.") == ["data.json", "server.py"]
    assert h.extract_path_tokens("abre config.jsonc, readme.markdown y main.pyc; utils.py sí") == ["config.jsonc", "utils.py"]
    assert h.extract_path_tokens("componentes app.jsx, index.tsx y styles.scss") == ["app.jsx", "index.tsx", "styles.scss"]


def test_at_mention_paths_are_not_read_as_missing_files(tmp_path):
    """`@src/a.py` is the composer's file-mention sigil (src/file_mentions.py).

    Left on the token, every mention looked like a user-named file that does
    not exist, so a mentioned-and-edited file tripped the target-substitution
    round it exists to prevent.
    """
    (tmp_path / "static" / "js").mkdir(parents=True)
    (tmp_path / "static" / "js" / "projects.js").write_text("x\n", encoding="utf-8")
    assert h.extract_path_tokens("arregla @static/js/projects.js ya") == ["static/js/projects.js"]

    led = h.TurnLedger(str(tmp_path), "arregla @static/js/projects.js")
    assert led.user_missing_paths() == []
    led.record("edit_file", '{"path": "static/js/projects.js", "old_string": "x", "new_string": "y"}',
               {"output": "Edited", "exit_code": 0}, 1)
    assert led.check_target_substitution("Hecho, he cambiado projects.js.") is None


def test_a_mention_of_a_file_that_really_is_missing_still_fires(tmp_path):
    (tmp_path / "static" / "js").mkdir(parents=True)
    (tmp_path / "static" / "js" / "projects.js").write_text("x\n", encoding="utf-8")
    led = h.TurnLedger(str(tmp_path), "arregla @static/js/cards.js")
    assert led.user_missing_paths() == ["static/js/cards.js"]
    led.record("edit_file", '{"path": "static/js/projects.js", "old_string": "x", "new_string": "y"}',
               {"output": "Edited", "exit_code": 0}, 1)
    chk = led.check_target_substitution("Hecho, arreglado.")
    assert chk and chk["missing"] == ["static/js/cards.js"]


def test_inlined_attachment_bodies_are_not_user_named_targets(tmp_path):
    """Silhouettes d20e933f round 85: 'Keep implementing the plan' plus a 170k
    spec attachment named files that are not in the workspace (attachment id
    `ec096bce….md`, other FAUSTUS_*.md, manifest.json). Those are context,
    not targets. Treating them as user-named missing files burned a
    target_substituted round on globbing phantoms after task 08 was done."""
    (tmp_path / "static" / "editor").mkdir(parents=True)
    (tmp_path / "static" / "editor" / "interactions2d.js").write_text("export {}\n", encoding="utf-8")
    user = (
        "Keep implementing the plan\n\n"
        "=== ZIP archive: Silhouettes_editor_implementacion_Qwen.zip ===\n"
        "Owner-checked path: D:\\uploads\\d4e516679dda4f8fbd3a7d81778127bd.zip\n"
        "Members: 12\n\n"
        "=== File: ec096bce31424b00969963a9b508d9c6.md ===\n"
        "[Type: markdown, Lines: 4000, Size: 172921 bytes]\n\n"
        "```markdown\n"
        "See FAUSTUS_ARREGLO_SVG_Y_STL.md and FAUSTUS_QUITAR_DIENTES_DE_SIERRA.md.\n"
        "The package ships a manifest.json next to the editor.\n"
        "```\n"
    )
    led = h.TurnLedger(str(tmp_path), user)
    assert led.user_missing_paths() == []
    led.record(
        "write_file",
        "static/editor/interactions2d.js\nexport function drag() {}",
        {"output": "Wrote", "exit_code": 0, "diff": {"new_file": False, "added": 1, "removed": 0, "text": "+x"}},
        73,
    )
    assert led.check_target_substitution(
        "Task 08 is closed. Rewrote static/editor/interactions2d.js."
    ) is None


def test_attachment_id_filenames_are_not_path_tokens():
    tokens = h.extract_path_tokens(
        "See ec096bce31424b00969963a9b508d9c6.md and static/editor/interactions2d.js"
    )
    assert "static/editor/interactions2d.js" in tokens
    assert all(not t.startswith("ec096bce") for t in tokens)


# ---------------------------------------------------------------------------
# Post-mutation static checks (regressions)
# ---------------------------------------------------------------------------

def _big_js(tmp_path, name, tail=""):
    """A syntactically VALID ES module well over the old 200 000-char cap.

    The bulk sits inside one function so that cutting the file at 200 000
    chars leaves an unterminated block — exactly what happened to the repo's
    own chat.js / document.js ("Unexpected end of input")."""
    body = "\n".join(f"  const a{i} = {i};" for i in range(20000))
    p = tmp_path / name
    p.write_text("export const first = 1;\nexport function pad() {\n" + body
                 + "\n  return first;\n}\n" + tail, encoding="utf-8")
    assert p.stat().st_size > 250_000
    return p


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_large_es_module_is_not_reported_as_a_syntax_error(tmp_path):
    """Regression: the .mjs copy used for `node --check` was truncated at
    200 000 chars, so every larger .js file was cut mid-file and node reported
    a bogus 'Unexpected end of input' (9 real files in this repo, chat.js
    among them) — one wasted mandatory fix round on a perfect file."""
    _big_js(tmp_path, "big.js")
    res = h.static_check_files(["big.js"], str(tmp_path))
    assert len(res) == 1
    assert res[0]["ok"] is True, res[0]["error"]
    assert h._check_javascript(str(tmp_path / "big.js")) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_large_es_module_with_a_real_syntax_error_still_fails(tmp_path):
    _big_js(tmp_path, "broken.js", tail="function broken( {\n")
    res = h.static_check_files(["broken.js"], str(tmp_path))
    assert len(res) == 1 and res[0]["ok"] is False and res[0]["error"]


def test_javascript_over_the_safety_cap_is_not_checkable_not_broken(tmp_path):
    """Above the safety cap the file is 'not checkable' (None), never an error."""
    huge = tmp_path / "huge.js"
    huge.write_bytes(b"export const a = ;\n" + (b"const filler = 1;\n" * 350_000))
    assert huge.stat().st_size > 5_000_000
    assert h._check_javascript(str(huge)) is None


def test_static_checks_are_skipped_in_a_frozen_build(tmp_path, monkeypatch):
    """In the PyInstaller build `sys.executable` is Faustus.exe: it ignores
    `-m py_compile` and relaunches the whole app (splash + tray + a second
    server), so py_compile never ran and EVERY edited .py was reported broken.
    Frozen → 'not checkable' (None), never a syntax error."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert h.host_python() is None

    def _never(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError(f"subprocess must not be spawned in a frozen build: {a}")

    monkeypatch.setattr(h.subprocess, "run", _never)
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
    assert h._check_python(str(bad)) is None
    res = h.static_check_files(["bad.py"], str(tmp_path))
    assert len(res) == 1 and res[0]["ok"] is True and res[0]["error"] is None


def test_host_python_is_the_interpreter_when_not_frozen():
    assert h.host_python() == sys.executable


def test_json_with_a_utf8_bom_is_valid(tmp_path):
    """PowerShell 5.1 (Out-File / Set-Content) and Notepad write a BOM; a
    perfectly valid package.json came back as 'invalid JSON: Unexpected UTF-8
    BOM' and cost a fix round."""
    p = tmp_path / "package.json"
    p.write_bytes(b"\xef\xbb\xbf" + b'{"name": "demo", "version": "1.0.0"}')
    assert h._check_json(str(p)) is None
    res = h.static_check_files(["package.json"], str(tmp_path))
    assert len(res) == 1 and res[0]["ok"] is True, res[0]
    # …and a BOM does not make broken JSON valid.
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xef\xbb\xbf" + b"{oops}")
    assert h._check_json(str(bad))


# ---------------------------------------------------------------------------
# Mutation detection in shell commands (regressions)
# ---------------------------------------------------------------------------

def test_arrows_inside_arguments_are_not_redirections():
    """`grep -rn "old -> new" src/app.py` was counted as a mutation: the
    unanchored redirect alternative matched the `>` of `->`. A read-only turn
    then 'proved' a false 'I modified src/app.py' and triggered the syntax
    check, the whole test suite and an LLM review pass."""
    assert not h.shell_command_looks_mutating('grep -rn "old -> new" src/app.py')
    assert not h.shell_command_looks_mutating('rg "foo=>bar" x.py')
    assert not h.shell_command_looks_mutating("""python -c 'print("a -> b")'""")
    assert not h.shell_command_looks_mutating('grep -n "() =>" static/js/chat.js')
    assert not h.shell_command_looks_mutating("grep -rn 'a -> b' .")
    # Text inside quotes is an argument, not a command.
    assert not h.shell_command_looks_mutating('grep -rn "echo x > out.txt" docs/')
    # The cases that already worked keep working.
    assert h.shell_command_looks_mutating("echo x > out.txt")
    assert h.shell_command_looks_mutating("cat a >> b.log")
    assert h.shell_command_looks_mutating("sed -i 's/a/b/' x.py")
    assert h.shell_command_looks_mutating("git commit -am msg")
    assert not h.shell_command_looks_mutating("ls > /dev/null")
    assert not h.shell_command_looks_mutating("python -m py_compile x.py 2>&1")
    assert not h.shell_command_looks_mutating("ls -la src")


def test_a_readonly_grep_does_not_verify_a_false_mutation_claim(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    ledger = h.TurnLedger(str(tmp_path), "busca 'old -> new' en el proyecto")
    ledger.record("bash", json.dumps({"command": 'grep -rn "old -> new" src/app.py'}),
                  {"output": "12: old -> new", "exit_code": 0}, 1)
    assert ledger.mutated_paths() == []
    check = ledger.check_completion("He modificado src/app.py para renombrar la función.")
    assert not check["ok"]
    assert "claims_without_mutation" in check["reasons"]


def test_needs_ui_verify_on_editor_js(tmp_path):
    led = h.TurnLedger(str(tmp_path), "arregla el canvas")
    led.record("edit_file", '{"path": "static/editor/viewport2d.js", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    assert led.needs_ui_verify("arregla el canvas")
    assert not led.has_browser_evidence()
    check = led.check_completion("Fixed the canvas drag.")
    assert "ui_unverified" in check["reasons"]
    msg = led.rejection_message(check)
    assert "browser_navigate" in msg and "#!bg" in msg


def test_browser_snapshot_satisfies_ui_verify(tmp_path):
    led = h.TurnLedger(str(tmp_path), "arregla el canvas")
    led.record("edit_file", '{"path": "static/editor/layer_tree.js", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    led.record("mcp__builtin_browser__browser_snapshot", "", {"output": "ok", "exit_code": 0}, 2)
    assert led.has_browser_evidence()
    check = led.check_completion("Fixed the layer tree.")
    assert "ui_unverified" not in check["reasons"]


def test_todowrite_verify_item_not_mutation_backed_without_browser(tmp_path):
    led = h.TurnLedger(str(tmp_path), "fix the editor")
    led.record("edit_file", '{"path": "static/editor/editor.css", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    out = led.record_progress([
        {"content": "Verify all fixes in browser", "status": "completed", "priority": "high"}
    ], 2)
    assert out[0]["verified"] is False


def test_ui_verify_status_missing_without_browser(tmp_path):
    led = h.TurnLedger(str(tmp_path), "arregla el canvas")
    led.record("edit_file", '{"path": "static/editor/viewport2d.js", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    assert led.ui_verify_status() == "missing"
    assert led.summary()["ui_verify"] == "missing"


def test_ui_verify_status_ok_with_snapshot(tmp_path):
    led = h.TurnLedger(str(tmp_path), "arregla el canvas")
    led.record("edit_file", '{"path": "static/editor/layer_tree.js", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    led.record("mcp__builtin_browser__browser_snapshot", "", {"output": "ok", "exit_code": 0}, 2)
    assert led.ui_verify_status() == "ok"


def test_ui_verify_status_skipped_when_setting_off(tmp_path, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: False if k == "agent_ui_verify" else d)
    led = h.TurnLedger(str(tmp_path), "arregla el canvas")
    led.record("edit_file", '{"path": "static/editor/editor.css", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    assert led.ui_verify_status() == "skipped"


# --- UI verify: the contract binds only where a browser was on the table ---


def test_python_under_editor_dir_is_not_a_ui_path(tmp_path):
    led = h.TurnLedger(str(tmp_path), "fix the parser")
    led.record("edit_file", '{"path": "src/editor/parser.py", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    assert led.mutated_ui_paths() == []
    assert not led.needs_ui_verify()
    assert led.ui_verify_status() == "skipped"
    assert "ui_unverified" not in led.check_completion("Fixed the parser.")["reasons"]


def test_layout_words_without_any_mutation_owe_no_screenshot(tmp_path):
    led = h.TurnLedger(str(tmp_path), "why does the canvas look wrong in the browser?")
    led.record("read_file", '{"path": "static/editor/viewport2d.js"}', {"output": "...", "exit_code": 0}, 1)
    assert not led.needs_ui_verify()
    assert led.ui_verify_status() == "skipped"


def test_visual_studio_is_not_layout_intent(tmp_path):
    led = h.TurnLedger(str(tmp_path), "open the project in Visual Studio and visualize the data")
    led.record("edit_file", '{"path": "src/data.py", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    assert not led.needs_ui_verify()


def test_layout_intent_plus_backend_mutation_still_needs_a_look(tmp_path):
    led = h.TurnLedger(str(tmp_path), "make the canvas bigger")
    led.record("edit_file", '{"path": "src/viewport.py", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    assert led.needs_ui_verify()
    assert led.ui_verify_status() == "missing"


def test_navigate_alone_is_not_evidence(tmp_path):
    led = h.TurnLedger(str(tmp_path), "arregla el canvas")
    led.record("edit_file", '{"path": "static/editor/viewport2d.js", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    led.record("mcp__builtin_browser__browser_navigate", '{"url": "http://127.0.0.1:5000/editor"}',
               {"output": "ok", "exit_code": 0}, 2)
    assert not led.has_browser_evidence()
    led.record("mcp__builtin_browser__browser_take_screenshot", "", {"output": "ok", "exit_code": 0}, 3)
    assert led.has_browser_evidence()


def test_no_browser_offered_means_skipped_not_missing(tmp_path):
    """The built-in browser is off on this box: the model could not have
    looked, so the card says skipped and the check does not fail the turn."""
    led = h.TurnLedger(str(tmp_path), "arregla el canvas")
    led.record("edit_file", '{"path": "static/editor/viewport2d.js", "old_string": "a", "new_string": "b"}',
               {"output": "Edited", "exit_code": 0}, 1)
    led.browser_tools_offered = False
    assert led.ui_verify_status() == "skipped"
    assert "ui_unverified" not in led.check_completion("Fixed the canvas drag.")["reasons"]
    led.browser_tools_offered = True
    assert led.ui_verify_status() == "missing"
