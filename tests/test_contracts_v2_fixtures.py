"""Spec v2 contracts (`src/contracts/{tool,task,errors}.py`) against the
package's own fixtures: `docs/spec/v2/examples/*.valid.json` must parse and
round-trip, and every case in `negative_cases.json` must be refused with a
`ContractError` whose `path` names the field the reason describes — not just
that something was rejected, but that the rejection points where the
`reason` says it should.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contracts import ContractError
from src.contracts.event import Event
from src.contracts.task import ApprovalRequest, EventEnvelope, QuestionRequest, TaskState
from src.contracts.tool import EvidenceRef, ToolDescriptor, ToolInvocation, ToolResult

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "spec" / "v2" / "examples"

MODEL_OF = {
    "tool_descriptor": ToolDescriptor,
    "tool_invocation": ToolInvocation,
    "tool_result": ToolResult,
    "evidence_ref": EvidenceRef,
    "task_state": TaskState,
    "question_request": QuestionRequest,
    "approval_request": ApprovalRequest,
    "event_envelope": EventEnvelope,
}


def _load(name: str):
    with open(EXAMPLES / f"{name}.valid.json", encoding="utf-8") as f:
        return json.load(f)


def _negative_cases():
    with open(EXAMPLES / "negative_cases.json", encoding="utf-8") as f:
        cases = json.load(f)
    return [pytest.param(c, id=f"{c['schema']}: {c['reason']}") for c in cases]


@pytest.mark.parametrize("name,cls", sorted(MODEL_OF.items()))
def test_the_packages_own_valid_example_parses_and_round_trips(name, cls):
    raw = _load(name)
    parsed = cls.from_mapping(raw)
    again = cls.from_mapping(parsed.to_mapping())
    assert again.fingerprint() == parsed.fingerprint()
    assert again.to_mapping() == parsed.to_mapping()


@pytest.mark.parametrize("case", _negative_cases())
def test_every_negative_case_is_refused_and_names_the_field(case):
    cls = MODEL_OF[case["schema"]]
    with pytest.raises(ContractError) as err:
        cls.from_mapping(case["instance"])
    # The path always starts with the schema's own root name — proof the
    # rejection is anchored inside the payload the case describes, not a
    # coincidental error raised somewhere else in the call chain.
    assert err.value.path.startswith(case["schema"])


def test_an_unknown_key_names_the_typo_it_probably_is():
    raw = _load("tool_descriptor")
    with pytest.raises(ContractError) as err:
        ToolDescriptor.from_mapping({**raw, "descrption": "typo'd key"})
    assert "descrption" in str(err.value)
    assert "did you mean 'description'" in str(err.value)


def test_schema_version_is_a_literal_const_not_a_compatibility_field():
    raw = _load("tool_result")
    for bad in ("1", 1.0, "1.0.0", None):
        with pytest.raises(ContractError) as err:
            ToolResult.from_mapping({**raw, "schema_version": bad})
        assert err.value.path == "tool_result.schema_version"


# ── ToolResult's status/error/uncertainty coupling, beyond the one example
#    of each negative case already covers ───────────────────────────────────

def test_a_failed_result_must_carry_an_error():
    raw = _load("tool_result")
    stripped = {**raw, "status": "failed", "error": None}
    with pytest.raises(ContractError) as err:
        ToolResult.from_mapping(stripped)
    assert err.value.path == "tool_result.error"


def test_a_denied_result_must_carry_an_error_too():
    raw = _load("tool_result")
    stripped = {**raw, "status": "denied", "error": None}
    with pytest.raises(ContractError) as err:
        ToolResult.from_mapping(stripped)
    assert err.value.path == "tool_result.error"


def test_cancelled_and_partial_are_not_forced_to_carry_either():
    raw = _load("tool_result")
    for status in ("cancelled", "partial"):
        result = ToolResult.from_mapping({**raw, "status": status, "error": None,
                                          "uncertainty": None})
        assert result.status == status
        assert result.error is None and result.uncertainty is None


def test_a_tool_error_code_is_free_text_not_the_obs03_taxonomy():
    """`BASE_REVISION_MISMATCH` (the spec's own example) is not one of the
    ten OBS-03 categories, and `ToolResult` must not force it to be — a
    tool's own vocabulary is real data."""
    raw = _load("tool_result")
    result = ToolResult.from_mapping(raw)
    assert result.error.code == "BASE_REVISION_MISMATCH"


# ── EventEnvelope's adapter onto contracts.event.Event ──────────────────────

def test_from_event_maps_the_pairs_it_is_confident_about():
    ev = Event.parse({"name": "artifact.created", "run_id": "r1"})
    envelope = EventEnvelope.from_event(
        ev, event_id="ev1", stream_id="s1", sequence=1, task_id="task1")
    assert envelope.type == "artifact.ready"
    assert envelope.run_id == "r1"


def test_from_event_refuses_to_guess_an_unmapped_name():
    ev = Event.parse({"name": "tool.progress", "run_id": "r1"})
    with pytest.raises(ContractError) as err:
        EventEnvelope.from_event(ev, event_id="ev1", stream_id="s1", sequence=1, task_id="task1")
    assert "tool.progress" in str(err.value)
    assert "type_override" in str(err.value)


def test_from_event_accepts_an_explicit_override_for_the_unmapped_case():
    ev = Event.parse({"name": "tool.progress", "run_id": "r1"})
    envelope = EventEnvelope.from_event(
        ev, event_id="ev1", stream_id="s1", sequence=1, task_id="task1",
        type_override="tool.started")
    assert envelope.type == "tool.started"


def test_to_event_round_trips_the_one_unambiguous_default():
    ev = Event.parse({"name": "artifact.created", "run_id": "r1", "data": {"artifact_id": "a1"}})
    envelope = EventEnvelope.from_event(
        ev, event_id="ev1", stream_id="s1", sequence=1, task_id="task1")
    back = envelope.to_event()
    assert back.name == "artifact.created"
    assert back.run_id == "r1"
    assert back.data == {"artifact_id": "a1"}


def test_to_event_refuses_to_guess_the_many_to_one_direction():
    envelope = EventEnvelope.from_mapping({
        "schema_version": "1.0", "event_id": "ev1", "stream_id": "s1", "sequence": 1,
        "task_id": "task1", "run_id": "r1", "type": "run.terminal",
        "timestamp": "2026-09-10T00:00:00Z", "visibility": "user", "payload": {},
    })
    with pytest.raises(ContractError) as err:
        envelope.to_event()
    assert "run.terminal" in str(err.value)
    assert "name=" in str(err.value)
    assert envelope.to_event(name="run.completed").name == "run.completed"
