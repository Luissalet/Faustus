"""Where the line falls between *claiming* a file and merely *mentioning* it.

`check_completion` used to ask one question — "did anything happen?" — and a
turn that edited cart.py and then reported edits to cart.py *and*
tests/test_cart.py sailed through, because something had indeed happened. The
question it asks now is "did what you say happened, happen to the file you
name?", and that only works if the harness can tell these two apart:

    claim   "He añadido el test a tests/test_cart.py"
    mention "el test está en tests/test_cart.py"

A false accusation costs a whole round of a 20 tok/s local model and teaches the
user to distrust the card, so the mention corpus below is the load-bearing half.
Both corpora are Spanish *and* English: the incident that motivated this was
reported in Spanish.
"""

import json

import pytest

import src.agent_harness as h


# ---------------------------------------------------------------------------
# Corpus 1 — the sentence claims authorship of the named file
# ---------------------------------------------------------------------------

CLAIMS_ES = [
    ("He añadido la función total_con_envio a cart.py.", ["cart.py"]),
    ("He creado tests/test_cart.py con el test del subtotal.", ["tests/test_cart.py"]),
    ("Modifiqué utils.py para exportar el helper.", ["utils.py"]),
    ("Se ha actualizado src/app.py con la nueva ruta.", ["src/app.py"]),
    ("cart.py ha sido modificado para incluir el envío.", ["cart.py"]),
    ("El fichero README.md fue actualizado con las instrucciones.", ["README.md"]),
    ("- Añadido el test en tests/test_cart.py", ["tests/test_cart.py"]),
    ("Escribí el módulo src/shipping.py y edité cart.py.", ["src/shipping.py", "cart.py"]),
    ("Ficheros modificados: cart.py", ["cart.py"]),
    ("✅ cart.py — función añadida", ["cart.py"]),
    # The list header carries the verb; the files arrive underneath it. This is
    # the exact shape of the reported incident.
    ("He completado la tarea. He añadido:\n"
     "- **En cart.py**: la función `total_con_envio(items, envio)`.\n"
     "- **En tests/test_cart.py**: el test `test_total_con_envio()`.",
     ["cart.py", "tests/test_cart.py"]),
    ("Cambios realizados:\n- cart.py: nueva función total_con_envio\n"
     "- tests/test_cart.py: nuevo test", ["cart.py", "tests/test_cart.py"]),
]

CLAIMS_EN = [
    ("I've added the helper to cart.py.", ["cart.py"]),
    ("I created tests/test_cart.py with one test.", ["tests/test_cart.py"]),
    ("utils.py has been updated to export the helper.", ["utils.py"]),
    ("- Updated: static/js/projects.js", ["static/js/projects.js"]),
    ("We modified src/app.py and wrote src/shipping.py.", ["src/app.py", "src/shipping.py"]),
    ("I have now implemented the change in server.py.", ["server.py"]),
    ("Files changed: cart.py, tests/test_cart.py", ["cart.py", "tests/test_cart.py"]),
    ("I wrote the test in tests/test_cart.py.", ["tests/test_cart.py"]),
    ("✅ cart.py — total_con_envio added", ["cart.py"]),
    ("Summary:\n- cart.py: added total_con_envio\n- tests/test_cart.py: added the test",
     ["cart.py", "tests/test_cart.py"]),
]


# ---------------------------------------------------------------------------
# Corpus 2 — the sentence only mentions the file. Never an accusation.
# ---------------------------------------------------------------------------

MENTIONS_ES = [
    "Leí cart.py para entender la estructura.",
    "cart.py ya tenía la función subtotal, así que la reutilicé.",
    "No hizo falta tocar utils.py.",
    "El test está en tests/test_cart.py, junto al resto.",
    "subtotal() está definida en cart.py.",
    "Habría que añadir un test en tests/test_cart.py más adelante.",
    "Puedes ejecutar pytest tests/test_cart.py para comprobarlo.",
    "No he modificado utils.py.",
    "Revisé src/app.py y no encontré el problema.",
    "Según cart.py, el precio se lee de la clave 'price'.",
    "He terminado la revisión de cart.py.",
    "Ficheros revisados:\n- cart.py\n- utils.py",
    "✅ utils.py — sin cambios",
    "El siguiente paso sería crear tests/test_cart.py.",
]

MENTIONS_EN = [
    "I read cart.py to understand the structure.",
    "cart.py already had subtotal, so I reused it.",
    "utils.py needed no changes.",
    "The test lives in tests/test_cart.py.",
    "A test should be added to tests/test_cart.py later.",
    "Run pytest tests/test_cart.py to check it.",
    "I did not modify utils.py.",
    "subtotal() is defined in cart.py.",
    "I checked src/app.py and found nothing wrong.",
    "The new function uses subtotal from cart.py.",
    "I have completed the review of cart.py.",
    "Files reviewed: cart.py, utils.py",
]


@pytest.mark.parametrize("text,expected", CLAIMS_ES + CLAIMS_EN)
def test_authorship_claims_name_their_files(text, expected):
    got = h.find_claimed_paths(text)
    assert sorted(p.lower() for p in got) == sorted(p.lower() for p in expected), got


@pytest.mark.parametrize("text", MENTIONS_ES + MENTIONS_EN)
def test_innocent_mentions_are_never_claims(text):
    assert h.find_claimed_paths(text) == [], text


def test_both_corpora_cover_both_languages():
    """Guard against someone trimming the corpus down to English."""
    assert len(CLAIMS_ES) >= 8 and len(CLAIMS_EN) >= 8
    assert len(MENTIONS_ES) >= 8 and len(MENTIONS_EN) >= 8


def test_a_file_named_only_as_the_source_of_borrowed_code_is_not_claimed():
    """"…in cart.py using the helper from utils.py" claims cart.py alone."""
    assert h.find_claimed_paths("He añadido la lógica en cart.py usando el helper de utils.py.") == ["cart.py"]
    assert h.find_claimed_paths("I've added the function to cart.py using the helper from utils.py.") == ["cart.py"]
    # …and a file read before the edit belongs to the reading.
    assert h.find_claimed_paths("Después de leer cart.py, he modificado shipping.py.") == ["shipping.py"]
    assert h.find_claimed_paths("After reading cart.py, I updated shipping.py.") == ["shipping.py"]


def test_code_fences_are_not_claims():
    """A model that pastes the test it *would* write must not be read as having
    written the file the snippet is headed with."""
    text = ("```python\n# tests/test_cart.py\ndef test_total_con_envio():\n    pass\n```\n"
            "He añadido la función a cart.py.")
    assert h.find_claimed_paths(text) == ["cart.py"]


# ---------------------------------------------------------------------------
# Ledger level: claimed vs. actually mutated
# ---------------------------------------------------------------------------

def _ledger_with_cart_edit(tmp_path, user="Añade a cart.py total_con_envio y escribe también su test."):
    (tmp_path / "cart.py").write_text("def subtotal(items):\n    return 0\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_cart.py").write_text("def test_subtotal():\n    pass\n", encoding="utf-8")
    ledger = h.TurnLedger(str(tmp_path), user)
    ledger.record("edit_file", json.dumps({"path": "cart.py", "old_string": "a", "new_string": "b"}),
                  {"output": "Edited cart.py (1 replacement)", "exit_code": 0}, 1)
    return ledger


INCIDENT_ANSWER = (
    "He completado la tarea. He añadido:\n"
    "- **En cart.py**: la función `total_con_envio(items, envio)` que calcula el subtotal y le suma el envío.\n"
    "- **En tests/test_cart.py**: el test `test_total_con_envio()` que verifica el resultado.\n"
)


def test_half_done_turn_is_rejected_even_though_one_file_really_changed(tmp_path):
    """The reported bug: one real edit opened the gate for a second, invented one."""
    ledger = _ledger_with_cart_edit(tmp_path)
    check = ledger.check_completion(INCIDENT_ANSWER)
    assert not check["ok"], check
    assert check["reasons"] == ["claimed_paths_untouched"], check["reasons"]
    assert check["untouched_paths"] == ["tests/test_cart.py"]
    # cart.py was really edited — it can never be part of the accusation.
    assert "cart.py" not in check["untouched_paths"]
    # It costs exactly what a claim with no effects costs: a rejection round…
    msg = ledger.rejection_message(check)
    assert "tests/test_cart.py" in msg and "cart.py" in msg
    assert "Nothing you described has happened" not in msg  # …but told truthfully
    # …and the same user-facing note, naming the file instead of "made none".
    note = ledger.user_note(check, final=True)
    assert "Verificación del harness" in note
    assert "`tests/test_cart.py`" in note and "`cart.py`" in note
    assert "ninguno" not in note


def test_the_same_answer_passes_when_both_files_were_really_edited(tmp_path):
    ledger = _ledger_with_cart_edit(tmp_path)
    ledger.record("edit_file", json.dumps({"path": "tests/test_cart.py", "old_string": "a", "new_string": "b"}),
                  {"output": "Edited tests/test_cart.py", "exit_code": 0}, 2)
    assert ledger.check_completion(INCIDENT_ANSWER)["ok"]


def test_mentioning_a_file_you_only_read_is_not_an_offence(tmp_path):
    ledger = _ledger_with_cart_edit(tmp_path)
    ledger.record("read_file", json.dumps({"path": "tests/test_cart.py"}),
                  {"output": "def test_subtotal(): pass", "exit_code": 0}, 1)
    text = ("He añadido `total_con_envio` a cart.py. Los tests existentes viven en "
            "tests/test_cart.py y no ha hecho falta tocarlos.")
    assert ledger.check_completion(text)["ok"]


def test_english_half_done_turn_is_caught_too(tmp_path):
    ledger = _ledger_with_cart_edit(tmp_path, user="Add total_con_envio to cart.py and write its test too.")
    check = ledger.check_completion(
        "Done. I've added `total_con_envio` to cart.py and written the test "
        "`test_total_con_envio` in tests/test_cart.py."
    )
    assert check["untouched_paths"] == ["tests/test_cart.py"]
    assert "claimed_paths_untouched" in check["reasons"]
    note = ledger.user_note(check, final=True)
    assert "never touched it" in note and "`tests/test_cart.py`" in note


def test_a_basename_match_is_enough_to_clear_a_claim(tmp_path):
    """The model writes `./tests/test_cart.py`, reports `test_cart.py` (or the
    other way round). Same lenient rule user_missing_paths() already uses."""
    ledger = _ledger_with_cart_edit(tmp_path)
    ledger.record("write_file", json.dumps({"path": "./tests/test_cart.py"}),
                  {"output": "written", "exit_code": 0}, 2)
    assert ledger.check_completion("He creado test_cart.py con el nuevo test.")["ok"]


# ---------------------------------------------------------------------------
# Silence where the ledger cannot know
# ---------------------------------------------------------------------------

def test_a_mutation_with_no_identifiable_target_silences_the_check(tmp_path):
    """`make` / `npm run build` count as mutations but name no file: the set of
    changed paths is incomplete, so nothing can be called untouched."""
    ledger = _ledger_with_cart_edit(tmp_path)
    ledger.record("bash", json.dumps({"command": "make build"}), {"output": "ok", "exit_code": 0}, 2)
    assert ledger.check_completion("He regenerado dist/bundle.js y actualizado cart.py.")["ok"]


def test_a_script_that_writes_files_silences_the_check_for_those_paths(tmp_path):
    """A python heredoc that opens a file for writing does not look mutating to
    the shell regex; the harness must not call that file untouched."""
    ledger = _ledger_with_cart_edit(tmp_path)
    ledger.record("python", json.dumps({"command": 'open("tests/test_cart.py", "w").write(src)'}),
                  {"output": "", "exit_code": 0}, 2)
    check = ledger.check_completion(INCIDENT_ANSWER)
    assert check["ok"], check
    # A read-only command naming the same file is NOT a write hint, though.
    other = _ledger_with_cart_edit(tmp_path)
    other.record("bash", json.dumps({"command": "cat tests/test_cart.py"}),
                 {"output": "def test_subtotal(): pass", "exit_code": 0}, 2)
    assert other.check_completion(INCIDENT_ANSWER)["untouched_paths"] == ["tests/test_cart.py"]


def test_delegated_work_silences_the_check(tmp_path):
    """A coordinator reports what its workers did; their file lists are theirs."""
    ledger = _ledger_with_cart_edit(tmp_path)
    ledger.record("delegate_agents", "{}",
                  {"output": "ok", "exit_code": 0,
                   "subagents": [{"name": "w1", "mutations": ["cart.py"]}]}, 2)
    assert ledger.check_completion(INCIDENT_ANSWER)["ok"]


def test_a_fabricated_path_is_reported_once_not_twice(tmp_path):
    """A claimed file that does not exist at all stays with fabricated_paths —
    the two reasons must not both name it."""
    ledger = _ledger_with_cart_edit(tmp_path)
    check = ledger.check_completion("He añadido total_con_envio a cart.py y he creado tests/test_envio.py.")
    assert "fabricated_paths" in check["reasons"]
    assert "tests/test_envio.py" in check["bad_paths"]
    assert check["untouched_paths"] == []
