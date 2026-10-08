import json

import pytest

from src.agent_harness import TurnLedger


REQUEST = json.dumps({"requests": [{"item_id": "perdido-qa", "quantity": 1}]})
RESULT = {"output": json.dumps({"inventory_version": 4, "ready": False, "totals_complete": False,
                               "items": [{"item_id": "perdido-qa", "requested": 1,
                                          "available": None, "status": "not_found"}]}), "exit_code": 0}
CLAIM = "He añadido la unidad `perdido-qa` a la misma lista. Resultado de home_check_list: no encontrado."


@pytest.mark.parametrize("text", [CLAIM, "I've added perdido-qa to the same list. home_check_list returns not_found."])
@pytest.mark.parametrize("output_field", ["output", "stdout"])
def test_computed_entry_does_not_claim_a_persistent_write(tmp_path, text, output_field):
    ledger = TurnLedger(str(tmp_path), "Comprueba la lista añadiendo perdido-qa, sin cambiar inventario.")
    result = {output_field: RESULT["output"], "exit_code": 0}
    event = ledger.record("mcp__homeqa__home_check_list", REQUEST, result)
    assert event["kind"] == "read" and event["computed_list"] is True
    assert not ledger.effects and not ledger.mutations
    assert "claims_without_mutation" not in ledger.check_completion(text)["reasons"]


@pytest.mark.parametrize("text", [
    "He añadido la unidad `perdido-qa` al inventario. home_check_list devuelve not_found.",
    "He añadido la unidad perdido-qa a la lista y la he guardado en la base de datos.",
    "He añadido la unidad `perdido-qa` a la lista de compra. home_check_list devolvió not_found.",
    "He añadido la unidad `perdido-qa` a la lista de la compra. home_check_list devolvió not_found.",
    "I've added `perdido-qa` to the list of groceries. home_check_list returned not_found.",
    "He añadido la unidad perdido-qa a la lista. He modificado el archivo inventario.json.",
])
def test_checking_a_list_does_not_verify_persistence_claims(tmp_path, text):
    ledger = TurnLedger(str(tmp_path), "Comprueba la lista.")
    ledger.record("mcp__homeqa__home_check_list", REQUEST, RESULT)
    assert "claims_without_mutation" in ledger.check_completion(text)["reasons"]


@pytest.mark.parametrize("tool,result", [
    ("mcp__homeqa__home_inventory_status", RESULT),
    ("mcp__homeqa__home_check_list", {"error": "invalid", "exit_code": 1}),
    ("mcp__homeqa__home_check_list", {"output": "{}", "exit_code": 0}),
    ("mcp__homeqa__home_check_list", {"output": RESULT["output"].replace("perdido-qa", "different"), "exit_code": 0}),
])
def test_a_missing_failed_or_mismatched_check_is_not_evidence(tmp_path, tool, result):
    ledger = TurnLedger(str(tmp_path), "Comprueba la lista.")
    ledger.record(tool, REQUEST, result)
    assert not any(e.get("computed_list") for e in ledger.events)
    assert "claims_without_mutation" in ledger.check_completion(CLAIM)["reasons"]
