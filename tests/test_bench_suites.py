"""src/bench/suites.py — INF-04 A5.

Pure tests: no model, no network. `load_suite`/`list_suites` against the
real `config/benchmark_suites/*.json` files this lote ships, and
`run_checks` against every `CHECK_KINDS` member with a positive and a
negative case, per CONTRATO_INF04's own instruction.
"""
from __future__ import annotations

import pytest

from src.bench import suites
from src.contracts.inference import BenchmarkCase, CHECK_KINDS


# ── load_suite / list_suites ─────────────────────────────────────────────────

def test_list_suites_finds_the_three_shipped_suites():
    found = {s["id"] for s in suites.list_suites()}
    assert found == {"es_conversation", "tools_json", "long_documents"}


@pytest.mark.parametrize("suite_id", ["es_conversation", "tools_json", "long_documents"])
def test_load_suite_has_six_to_eight_cases_and_a_valid_objective(suite_id):
    suite = suites.load_suite(suite_id)
    assert suite["id"] == suite_id
    assert suite["version"]
    assert 6 <= len(suite["cases"]) <= 8
    for case in suite["cases"]:
        assert isinstance(case, BenchmarkCase)
        assert case.prompt or case.messages
        assert case.checks  # every shipped case actually asserts something


def test_load_suite_raises_for_unknown_id():
    with pytest.raises(suites.SuiteNotFound):
        suites.load_suite("does_not_exist")


def test_load_suite_rejects_path_traversal():
    with pytest.raises(suites.SuiteNotFound):
        suites.load_suite("../../etc/passwd")


def test_long_documents_suite_embeds_a_long_document_with_facts_at_each_position():
    suite = suites.load_suite("long_documents")
    labels = set()
    for case in suite["cases"]:
        labels.update(case.tags)
        assert len(case.prompt) > 8000  # ~3-4k tokens of embedded document
    # facts are spread across the document, not clustered at one spot
    assert any("principio" in l for l in labels)
    assert any("final" in l for l in labels)


# ── run_checks: one CHECK_KINDS member at a time, positive and negative ────

def _case(checks):
    return BenchmarkCase.parse({"id": "c", "suite": "s", "prompt": "hi", "checks": checks})


def test_run_checks_contains_positive_and_negative():
    case = _case([{"kind": "contains", "arg": "París"}])
    assert suites.run_checks(case, "La capital es París.")["passed"] is True
    result = suites.run_checks(case, "La capital es Madrid.")
    assert result["passed"] is False
    assert result["failed_checks"] == ["contains"]


def test_run_checks_regex_positive_and_negative():
    case = _case([{"kind": "regex", "arg": r"\b4\b"}])
    assert suites.run_checks(case, "La respuesta es 4.")["passed"] is True
    assert suites.run_checks(case, "La respuesta es cuatro.")["passed"] is False


def test_run_checks_json_valid_positive_and_negative():
    case = _case([{"kind": "json_valid"}])
    assert suites.run_checks(case, '{"a": 1}')["passed"] is True
    assert suites.run_checks(case, "not json at all")["passed"] is False


def test_run_checks_json_valid_recovers_from_code_fence_and_prose():
    case = _case([{"kind": "json_valid"}])
    fenced = "```json\n{\"a\": 1}\n```"
    assert suites.run_checks(case, fenced)["passed"] is True
    prosed = 'Sure, here you go: {"a": 1} — hope that helps!'
    assert suites.run_checks(case, prosed)["passed"] is True


def test_run_checks_json_has_keys_positive_and_negative():
    case = _case([{"kind": "json_has_keys", "arg": ["name", "age"]}])
    assert suites.run_checks(case, '{"name": "Ana", "age": 30}')["passed"] is True
    result = suites.run_checks(case, '{"name": "Ana"}')
    assert result["passed"] is False
    assert result["failed_checks"] == ["json_has_keys"]


def test_run_checks_max_words_positive_and_negative():
    case = _case([{"kind": "max_words", "arg": 3}])
    assert suites.run_checks(case, "uno dos tres")["passed"] is True
    assert suites.run_checks(case, "uno dos tres cuatro")["passed"] is False


def test_run_checks_language_es_positive_and_negative():
    case = _case([{"kind": "language_es"}])
    assert suites.run_checks(case, "El gato está en la casa porque hace frío.")["passed"] is True
    assert suites.run_checks(case, "The cat is on the roof today.")["passed"] is False


def test_run_checks_no_tool_leak_positive_and_negative():
    case = _case([{"kind": "no_tool_leak"}])
    assert suites.run_checks(case, "La respuesta es 42.")["passed"] is True
    assert suites.run_checks(case, "<tool_call>{\"name\": \"x\"}</tool_call>")["passed"] is False


def test_run_checks_multiple_checks_reports_every_failure():
    case = _case([
        {"kind": "contains", "arg": "nope"},
        {"kind": "max_words", "arg": 1},
    ])
    result = suites.run_checks(case, "this has more than one word")
    assert result["passed"] is False
    assert set(result["failed_checks"]) == {"contains", "max_words"}


def test_run_checks_no_checks_passes_trivially():
    case = BenchmarkCase.parse({"id": "c", "suite": "s", "prompt": "hi", "checks": []})
    result = suites.run_checks(case, "anything")
    assert result == {"passed": True, "failed_checks": []}


def test_all_check_kinds_are_exercised_above():
    # A tripwire: if CHECK_KINDS grows, this file must grow a case for it.
    exercised = {"contains", "regex", "json_valid", "json_has_keys", "max_words",
                 "language_es", "no_tool_leak"}
    assert exercised == set(CHECK_KINDS)
