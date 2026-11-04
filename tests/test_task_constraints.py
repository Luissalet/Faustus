"""Standing no-file / no-plan constraints stay in view during a turn."""
import json

import pytest

from src import agent_harness as h, task_constraints as tc

CURL = json.dumps({"command": "cd /tmp && curl -sS -o a.json https://example.com/a.json"})


def test_negative_phrases_are_found_in_both_languages():
    assert tc.extract("Lee las fuentes en RAM, sin archivos ni copias y sin plan.") == {tc.NO_FILES, tc.NO_PLAN}
    assert tc.extract("Read it in memory only, no files, no todo list.") == {tc.NO_FILES, tc.NO_PLAN}
    assert tc.extract("Compare the two trainers and answer.") == set()


def test_an_explicit_file_request_cancels_the_file_reminder():
    assert tc.NO_FILES not in tc.extract("Sin copias del repo, pero guarda el informe en report.md")


def test_reminder_only_fires_for_a_breaking_call():
    known = {tc.NO_FILES, tc.NO_PLAN}
    assert "no files" in tc.reminder(known, "bash", CURL)
    assert tc.reminder(known, "bash", json.dumps({"command": "curl -sS https://example.com | sha256sum"})) is None
    assert tc.reminder(known, "web_fetch", "{}") is None
    assert "no plan" in tc.reminder(known, "todowrite", "{}")
    assert tc.reminder(set(), "bash", CURL) is None
    assert tc.reminder({tc.NO_FILES}, "write_file", "{}")


def test_ledger_learns_constraints_from_the_coordination_task(tmp_path):
    ledger = h.TurnLedger(str(tmp_path), "Atiende la tarea 1802 de Ágora.")
    assert ledger.task_constraints == set()
    ledger.record("mcp__x__hub_agora_task", "{}",
                  {"output": "Lee fuentes en RAM, sin archivos ni plan.", "exit_code": 0}, 1)
    assert ledger.task_constraints == {tc.NO_FILES, tc.NO_PLAN}
    ledger.record("mcp__x__other_tool", "{}", {"output": "sin archivos", "exit_code": 0}, 2)
    assert tc.reminder(ledger.task_constraints, "bash", CURL)


def test_user_text_alone_sets_constraints(tmp_path):
    ledger = h.TurnLedger(str(tmp_path), "Consulta la fuente sin archivos.")
    assert tc.NO_FILES in ledger.task_constraints
