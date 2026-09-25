"""An answer or deliverable must never say it verified or consulted an
outside source when no tool read one this turn (live, exam run 15: quotation
attributions "verified against the canonical text" with web search off, two
of them wrong)."""
import json

from src.agent_harness import TurnLedger
from src.source_claims import find_source_claims, is_source_tool


def test_claims_of_an_outside_check_are_found_in_both_languages():
    es = ("- **Identificación de las citas**: verificada contra el texto canónico de\n"
          "  Shakespeare (*Hamlet* III.1). Coinciden con los fragmentos visibles.")
    assert find_source_claims(es)
    assert find_source_claims("La fecha se contrastó... y está confirmada según Wikipedia.")
    assert find_source_claims("I verified the quote against the Folger edition online.")
    assert find_source_claims("We looked it up: the line is from Sonnet 60.")
    # a negation earlier in the sentence about something else does not cancel it
    assert find_source_claims("No hay ninguna duda de que he verificado la cita contra el texto original.")
    assert find_source_claims("Without exaggerating, I verified this against the original published edition.")


def test_denials_and_workspace_checks_are_not_claims():
    for text in [
        "1 cable = 185,2 m (conocimiento general, no consultado en web).",
        "No se utilizó consulta web (no habilitada) ni ayuda del evaluador.",
        "Símbolos verificados fila a fila por visión, consistentes con la transcripción del usuario.",
        "The attribution is from memory, not verified against any source.",
        "The numbers were checked with python.",
        "Texto superior confirmado por visión sobre la vista.",
    ]:
        assert find_source_claims(text) == [], text


def test_source_tools():
    assert is_source_tool("web_search") and is_source_tool("reach_read")
    assert is_source_tool("mcp__library__search") and is_source_tool("browser_navigate")
    assert not is_source_tool("read_file") and not is_source_tool("python")


def _ledger_with_written(text, extra_tools=()):
    led = TurnLedger(workspace=None, user_text="resuelve el acertijo")
    led.record("read_file", json.dumps({"path": "ENUNCIADO.md"}), {"output": "x", "exit_code": 0})
    for tool in extra_tools:
        led.record(tool, json.dumps({"query": "sonnet"}), {"output": "results", "exit_code": 0})
    led.record("write_file", json.dumps({"path": "RESPUESTA.md", "content": text}),
               {"output": "Wrote", "exit_code": 0})
    return led


def test_a_claim_in_the_written_deliverable_is_rejected_without_a_source_tool():
    led = _ledger_with_written("## Comprobación\n- Citas verificadas contra el texto canónico.\n")
    check = led.check_completion("He escrito RESPUESTA.md con la solución.")
    assert "unconsulted_sources" in check["reasons"]
    assert check["source_claims"]
    msg = led.rejection_message(check)
    assert "NO tool that reads outside the workspace ran" in msg
    assert "Fix the file you wrote" in msg
    note = led.user_note(check, final=True)
    assert "no está" in note and "verificado" in note


def test_the_same_claim_passes_after_a_web_search():
    led = _ledger_with_written("- Citas verificadas contra el texto canónico.\n", extra_tools=("web_search",))
    check = led.check_completion("He escrito RESPUESTA.md.")
    assert "unconsulted_sources" not in check["reasons"]


def test_a_claim_in_the_final_answer_alone_is_rejected():
    led = TurnLedger(workspace=None, user_text="¿de qué obra es esta cita?")
    led.record("read_file", json.dumps({"path": "notas.md"}), {"output": "x", "exit_code": 0})
    check = led.check_completion("Es de Hamlet; lo he comprobado en la edición canónica.")
    assert check["reasons"] == ["unconsulted_sources"]


def test_verification_of_outside_knowledge_in_any_verb_form():
    """Live, exam run 19: "se cotejó contra el inventario conocido de
    fortificaciones", "las citas se verificaron como Shakespeare (Soneto 29)"
    — with "no se usó web" two sections later."""
    assert find_source_claims("- **Independiente:** la lista de fuertes españoles del anverso se cotejó "
                              "contra el inventario conocido de fortificaciones de Viejo San Juan.")
    assert find_source_claims("- **Independiente:** las citas se verificaron como Shakespeare "
                              "(*Hamlet*, Soneto 29) y Spenser.")
    assert find_source_claims("I verified the attribution: it is Sonnet 60.")


def test_checks_done_with_a_local_instrument_are_not_outside_claims():
    for text in [
        "El PDF no tiene capa de texto (verificado con pypdf: 0 caracteres).",
        "- PIL sobre ambas imágenes: dimensiones confirmadas (anverso 1250×1769).",
        "Verified with python that 3839 × 185.2 m = 710.98 km.",
        "Confirmed the author field in package.json.",
        "Verified the author in the repo metadata.",
        "La obra no está confirmada; es una hipótesis.",
    ]:
        assert find_source_claims(text) == [], text


def test_a_source_read_in_an_earlier_turn_counts():
    from src.agent_harness import TurnLedger
    claim = "Las citas se verificaron contra la edición canónica de la obra."
    fresh = TurnLedger(None, "¿y la segunda cita?")
    assert fresh.unconsulted_source_claims(claim)

    ledger = TurnLedger(None, "¿y la segunda cita?")
    ledger.note_prior_message({"role": "user", "content": "busca la cita"})
    ledger.note_prior_message({"role": "assistant", "content": "hecho", "metadata": {
        "tool_events": [{"tool": "web_search", "exit_code": 0}]}})
    assert ledger.prior_sources
    assert ledger.unconsulted_source_claims(claim) == []


def test_a_failed_or_local_prior_tool_does_not_count():
    from src.agent_harness import TurnLedger
    ledger = TurnLedger(None, "x")
    ledger.note_prior_message({"role": "assistant", "content": "", "metadata": {
        "tool_events": [{"tool": "web_fetch", "exit_code": 1}, {"tool": "read_file", "exit_code": 0}]}})
    ledger.note_prior_message({"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "python"}}]})
    assert not ledger.prior_sources
    ledger.note_prior_message({"role": "tool", "name": "web_fetch", "content": "page"})
    assert ledger.prior_sources


def test_pages_listed_as_opened_after_a_search_only_turn_are_a_claim():
    from src.agent_harness import TurnLedger
    deliverable = (
        "## 5. Herramientas y fuentes\n\n"
        "**Fuentes externas realmente abiertas:**\n"
        "- Folger Shakespeare Library: Hamlet\n"
        "- Project Gutenberg: Hamlet text\n"
    )
    ledger = TurnLedger(None, "resuelve")
    ledger.events.append({"tool": "web_search", "ok": True, "kind": "read"})
    claims = ledger.unconsulted_source_claims(deliverable)
    assert claims and "realmente abiertas" in claims[0]
    assert ledger.source_claims_kind == "search_only"
    check = ledger.check_completion(deliverable)
    assert "unconsulted_sources" in check["reasons"] and check["source_claims_kind"] == "search_only"

    opened = TurnLedger(None, "resuelve")
    opened.events += [{"tool": "web_search", "ok": True, "kind": "read"}, {"tool": "web_fetch", "ok": True, "kind": "read"}]
    assert opened.unconsulted_source_claims(deliverable) == []


def test_an_empty_or_denied_opened_list_and_local_reading_are_not_claims():
    from src.source_claims import find_opened_claims
    assert find_opened_claims("**Fuentes externas realmente abiertas:**\n- Ninguna (sólo resultados de búsqueda)") == []
    assert find_opened_claims("Fuentes externas realmente abiertas: ninguna.") == []
    assert find_opened_claims("No se abrió ninguna página; sólo resultados de búsqueda.") == []
    assert find_opened_claims("Páginas leídas del PDF: 1 a 3 de 6a.pdf") == []
    assert find_opened_claims("I opened the pages on the Folger site to compare the lines.")
    assert find_opened_claims("Sources actually opened: en.wikipedia.org/wiki/Cable_length")


def test_files_written_before_an_approval_pause_count_for_the_resumed_leg():
    from src.agent_harness import TurnLedger
    ledger = TurnLedger("/w", "crea stats.py y ejecuta los tests")
    ledger.note_prior_message({"role": "user", "content": "crea stats.py y ejecuta los tests"})
    ledger.note_prior_message({"role": "assistant", "content": "Allow this task to continue?",
                               "metadata": {"harness": {"stop_reason": "awaiting_user",
                                                        "mutations": ["stats.py", "test_stats.py"]}}})
    assert ledger.prior_leg_paths == ["stats.py", "test_stats.py"]
    assert "stats.py" in ledger.summary()["mutations"]
    assert ledger.claimed_untouched_paths("He creado `stats.py` y `test_stats.py`.") == []

    # A new request starts a new task: earlier legs no longer count.
    ledger.note_prior_message({"role": "user", "content": "ahora otra cosa"})
    assert ledger.prior_leg_paths == []

    # A leg that ended normally is not part of the resumed task.
    other = TurnLedger("/w", "x")
    other.note_prior_message({"role": "assistant", "content": "hecho",
                              "metadata": {"harness": {"stop_reason": "complete", "mutations": ["a.py"]}}})
    assert other.prior_leg_paths == []
