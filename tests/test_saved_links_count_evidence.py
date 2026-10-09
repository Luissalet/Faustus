import pytest
from src.answer_checks import saved_links_count_evidence


@pytest.mark.parametrize("answer", ["En Links Hoard tienes 12 enlaces guardados.", "Hay 12 links en total.",
    "12", "En total tienes 12 guardados.", "Hay 3 enlaces sin leer, de 12 enlaces guardados."])
def test_matching_count_keeps_correct_paraphrases(answer):
    assert saved_links_count_evidence(answer,12)["ok"]


@pytest.mark.parametrize("answer", ["Tienes 15 enlaces guardados.", "15", "Hay 12 enlaces; el total es 15 enlaces."])
def test_actual_count_discrepancy_is_reported(answer):
    result = saved_links_count_evidence(answer,12)
    assert not result["ok"] and result["reason"] == "saved_links_count"


def test_missing_count_and_missing_source_are_explicit():
    assert saved_links_count_evidence("He consultado tu biblioteca.",12)["reason"] == "saved_links_count_missing"
    assert saved_links_count_evidence("Tienes 12 enlaces.",None)["reason"] == "saved_links_source_missing"
    assert saved_links_count_evidence("Tienes 3 enlaces sin leer.",12)["reason"] == "saved_links_count_missing"
