"""A final report keeps its text and evidence without correction-only calls."""
import json

from test_agent_harness_loop_partial_work import _patch, _run, _workspace, EDIT_CART
import src.agent_loop as al
import src.agent_harness as harness


def test_real_missing_artifact_is_warned_without_rewriting_or_extra_calls(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    answer = "He añadido la función a cart.py y he creado export.csv."
    calls = _patch(monkeypatch, [(EDIT_CART, "tool_calls"), (answer, "stop")], workspace)
    events = _run(workspace)
    checks = [e for e in events if e.get("type") == "harness_check"]
    assert calls["n"] == 2
    assert not any(e.get("type") == "response_replace" for e in events)
    assert not any(e.get("status") in ("rejected", "auto_continue", "completion_gated") for e in checks)
    advisory = next(e for e in checks if "fabricated_paths" in (e.get("reasons") or []))
    assert advisory["advisory"] and "export.csv" in advisory["bad_paths"]
    text = "".join(e.get("delta", "") for e in events if not e.get("thinking"))
    assert answer in text
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["harness"]["verification"]["outcome"] != "verified"


def test_test_fixture_report_is_kept_without_claim_warnings(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    answer = "He añadido la función a cart.py. El nuevo test genera input.csv y bulk.txt en tmp_path de pytest."
    calls = _patch(monkeypatch, [(EDIT_CART, "tool_calls"), (answer, "stop")], workspace)
    events = _run(workspace, user="Añade la función a cart.py y explica el caso de prueba.")
    assert calls["n"] == 2
    assert not any(e.get("type") == "response_replace" or e.get("status") == "rejected" for e in events)
    assert not any(e.get("bad_paths") or e.get("untouched_paths") for e in events if e.get("type") == "harness_check")
    assert answer in "".join(e.get("delta", "") for e in events if not e.get("thinking"))


def test_fresh_completion_verifier_does_not_call_another_model(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    calls = _patch(monkeypatch, [(EDIT_CART, "tool_calls"), ("He añadido la función a cart.py.", "stop")], workspace)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: True if key == "agent_verifier_subagent" else default)
    async def forbidden(*args, **kwargs):
        raise AssertionError("ordinary completion evidence must not trigger a model call")
    monkeypatch.setattr(al, "_run_verifier_subagent", forbidden)
    events = _run(workspace)
    assert calls["n"] == 2
    assert any(e.get("reason") == "completion_verifier" and e.get("model_call") is False for e in events)


def test_evidence_only_completion_proposals_do_not_reopen_a_finished_report():
    decision = {"continue_with": ["prove report", "run missed check", "fix failure", "finish requested export"],
                "runnable": [
                    {"title": "prove report", "source": "changeset", "evidence_refs": ["changeset:unproved:unsupported_claim:output.csv"]},
                    {"title": "run missed check", "source": "tests", "evidence_refs": ["tests:ran=false:pytest"]},
                    {"title": "fix failure", "source": "tests", "evidence_refs": ["tests:failure:test_total"]},
                    {"title": "finish requested export", "source": "definition_of_done", "evidence_refs": ["definition_of_done:export"]},
                ]}
    work, notices = harness.completion_work_items(decision)
    assert work == ["fix failure", "finish requested export"]
    assert notices == ["prove report", "run missed check"]


def test_planner_without_candidate_metadata_keeps_its_existing_work():
    assert harness.completion_work_items({"continue_with": ["finish requested step"]}) == (["finish requested step"], [])


def test_factual_advisory_cannot_leave_the_result_marked_verified(tmp_path):
    ledger = harness.TurnLedger(str(tmp_path), "Report the observed result.")
    ledger.record("read_file", json.dumps({"path": "data.csv"}), {"output": "total=8", "exit_code": 0}, 1)
    ledger.check_completion("The total is 80.")
    ledger.record_answer_advisory(["ungrounded_figure"], 2)
    assert ledger.verify_outcome()["outcome"] == "unverified"


def test_wrong_calculation_keeps_report_and_records_exact_evidence(tmp_path, monkeypatch):
    from src import scorecard
    workspace = _workspace(tmp_path)
    answer = "La letra r aparece 4 veces."
    calls = _patch(monkeypatch, [(answer, "stop")], workspace)
    rows = []
    monkeypatch.setattr(scorecard, "record", rows.append)
    events = _run(workspace, user="¿Cuántas veces aparece la letra r en la palabra strawberry?")
    text = "".join(e.get("delta", "") for e in events if not e.get("thinking"))
    assert calls["n"] == 1
    assert text == answer
    assert not any(e.get("type") == "response_replace" for e in events)
    expected = {'kind': 'letter_count', 'value': '3',
                'explanation': "'strawberry' contiene 3 veces la letra 'r'."}
    checks = [e for e in events if e.get('type') == 'harness_check']
    assert any(e.get('advisory') is True and e.get('calculation') == expected
               and ('wrong_calculation' in (e.get('reasons') or []) or e.get('reason') == 'wrong_calculation')
               for e in checks)
    summary = next(e['data'] for e in events if e.get('type') == 'harness_summary')
    assert any(note.startswith('evidence_notice:') and expected['explanation'] in note for note in summary['notes'])
    assert summary['verification']['outcome'] == 'unverified'
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["harness"]["verification"]["outcome"] == "unverified"
    assert rows and rows[0]['verified'] is False and rows[0]['unverified'] is True
    assert rows[0]['evidence_outcome'] == 'unverified'
    assert not any(e.get('status') in ('rejected', 'auto_continue', 'completion_gated') for e in checks)


def test_deterministic_calculation_does_not_exempt_requested_file_work():
    assert al._answer_only_math_request("¿Cuántas veces aparece la letra l en la palabra alhelí?")
    assert not al._answer_only_math_request("Edita cart.py para contar cuántas veces aparece la letra l en la palabra alhelí.")
