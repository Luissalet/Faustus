"""Shadow receipts do not execute tools or grant authority."""
from copy import deepcopy
import json

import pytest

from src import pdf_tool_contracts as contracts
from src.pdf_call_binding import capture_pdf_call
from src.tool_schema_receipts import capture_candidate, compare_pdf_binding, definition_hashes, observation_for_answer


NAME = "pdf_find_section"


def prepared(default=8):
    definition = contracts.function_definition(NAME)
    definition["parameters"]["properties"]["limit"]["default"] = default
    return [{"type": "function", "function": definition}]


def test_announced_default_change_is_visible_and_capture_is_immutable(monkeypatch):
    schemas = prepared()
    receipt = capture_candidate(schemas, candidate_index=0, round_num=1)
    assert compare_pdf_binding(receipt, capture_pdf_call(NAME).metadata())["status"] == "match"
    schemas[0]["function"]["parameters"]["properties"]["limit"]["default"] = 3
    monkeypatch.setitem(contracts._DEFINITIONS, NAME, schemas[0]["function"])
    result = compare_pdf_binding(receipt, capture_pdf_call(NAME).metadata())
    assert result["status"] == "mismatch" and result["shadow"] is True
    assert result["announced_semantics_sha256"] != result["binding_semantics_sha256"]
    assert result["stage"] == "candidate_prepared"
    assert "parameters" not in json.dumps(result) and "description" not in json.dumps(result)


def test_slimming_descriptions_remains_structurally_compatible():
    from src.tool_slimming import slim_tool_schemas
    schemas = prepared()
    schemas[0]["function"]["description"] *= 10
    _, expected_semantic = definition_hashes(schemas[0]["function"])
    slimmed, report = slim_tool_schemas(schemas, context_length=100, share=0.1)
    assert report["slimmed"]
    receipt = capture_candidate(slimmed, candidate_index=0, round_num=1)
    result = compare_pdf_binding(receipt, capture_pdf_call(NAME).metadata())
    assert receipt.definitions[0][2] == expected_semantic
    assert result["status"] == "compatible_projection"


def test_semantics_preserve_named_description_property_and_default_data():
    definition = {"name": "example", "description": "prose", "parameters": {
        "type": "object", "properties": {"description": {"type": "object", "description": "annotation",
            "default": {"description": "data"}}}}}
    base = definition_hashes(definition)[1]
    changed = deepcopy(definition)
    changed["parameters"]["properties"]["description"]["description"] = "shorter"
    assert definition_hashes(changed)[1] == base
    changed["parameters"]["properties"]["description"]["default"]["description"] = "other data"
    assert definition_hashes(changed)[1] != base
    changed = deepcopy(definition)
    del changed["parameters"]["properties"]["description"]
    assert definition_hashes(changed)[1] != base


def test_fallback_selection_never_substitutes_primary(monkeypatch):
    states = {0: {"schema_receipt": capture_candidate(prepared(8), candidate_index=0, round_num=2)},
              1: {"schema_receipt": capture_candidate(prepared(3), candidate_index=1, round_num=2)}}
    monkeypatch.setitem(contracts._DEFINITIONS, NAME, prepared(3)[0]["function"])
    binding = capture_pdf_call(NAME).metadata()
    assert observation_for_answer(states, 0, binding)["status"] == "mismatch"
    fallback = observation_for_answer(states, 1, binding)
    assert fallback["status"] == "match" and fallback["candidate_index"] == 1
    assert observation_for_answer(states, 2, binding)["reason"] == "candidate_receipt_missing"


def test_old_round_or_wrong_candidate_receipt_cannot_match_current_call():
    from src.pdf_call_binding import capture_pdf_call
    binding = capture_pdf_call(NAME).metadata()
    prior = capture_candidate(prepared(), candidate_index=1, round_num=3)
    assert observation_for_answer({1: {"schema_receipt": prior}}, 1, binding,
                                  round_num=4)["reason"] == "stale_candidate_receipt"
    assert observation_for_answer({0: {"schema_receipt": prior}}, 0, binding,
                                  round_num=3)["reason"] == "stale_candidate_receipt"


def test_textual_missing_and_unannounced_are_honest_observations():
    binding = capture_pdf_call(NAME).metadata()
    assert compare_pdf_binding(None, binding)["status"] == "not_comparable"
    text = capture_candidate([], candidate_index=0, round_num=1, text_only=True)
    assert compare_pdf_binding(text, binding)["reason"] == "no_native_schema_receipt"
    other = capture_candidate([{"type": "function", "function": {"name": "other", "parameters": {}}}], candidate_index=0, round_num=1)
    assert compare_pdf_binding(other, binding)["status"] == "not_advertised"
    duplicate = capture_candidate(prepared() * 2, candidate_index=0, round_num=1)
    assert compare_pdf_binding(duplicate, binding)["reason"] == "duplicate_schema_name"


def test_shadow_identity_failure_preserves_existing_binding_metadata(monkeypatch):
    from src import tool_schema_receipts as receipts
    binding = capture_pdf_call(NAME)
    receipt = capture_candidate(prepared(), candidate_index=0, round_num=1)
    def unavailable(*args):
        raise RuntimeError("synthetic observer failure")
    monkeypatch.setattr(receipts, "definition_hashes", unavailable)
    metadata = binding.metadata()
    assert metadata["scope"] == "call" and metadata["descriptor_sha256"]
    assert compare_pdf_binding(receipt, metadata)["reason"] == "binding_identity_missing"
    metadata.update(definition_sha256="SECRET_BODY", schema_semantics_sha256="SECRET_TOKEN")
    assert "SECRET" not in json.dumps(compare_pdf_binding(receipt, metadata))


async def test_real_dispatch_keeps_success_despite_shadow_mismatch(monkeypatch):
    from src import tool_execution
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_schemas import function_call_to_tool_block
    receipt = capture_candidate(prepared(), candidate_index=0, round_num=1)
    monkeypatch.setitem(contracts._DEFINITIONS, NAME, prepared(3)[0]["function"])
    async def synthetic_handler(content, ctx):
        args = contracts.parse_content(NAME, content, definition=ctx["_pdf_call_definition"])
        return {"exit_code": 0, "output": "synthetic", "limit": args["limit"]}
    monkeypatch.setitem(TOOL_HANDLERS, NAME, synthetic_handler)
    block = function_call_to_tool_block(NAME, json.dumps({"path": "unused.pdf", "query": "test"}))
    _, result = await tool_execution.execute_tool_block(block, security_context=ToolRunSecurityContext())
    assert result["exit_code"] == 0 and result["limit"] == 3
    assert compare_pdf_binding(receipt, result["pdf_call_contract"])["status"] == "mismatch"


async def test_real_loop_uses_prepared_fallback_receipt(monkeypatch):
    from src import agent_loop
    from src import tool_schema_receipts as receipts
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *a, **k: (True, False, False))
    monkeypatch.setattr(agent_loop, "_build_system_prompt", lambda messages, *a, **k: (list(messages), []))
    monkeypatch.setattr(agent_loop, "FUNCTION_TOOL_SCHEMAS", prepared(8))
    primary = ("https://primary.invalid/v1", "primary", {})
    backup = ("https://backup.invalid/v1", "backup", {})
    observations = []
    original = receipts.observation_for_answer
    def observed(states, candidate_index, binding, **kwargs):
        result = original(states, candidate_index, binding, **kwargs)
        observations.append(result)
        return result
    monkeypatch.setattr(receipts, "observation_for_answer", observed)
    calls = 0
    async def stream(candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await kwargs["candidate_request_factory"](0, *primary)
            monkeypatch.setattr(agent_loop, "FUNCTION_TOOL_SCHEMAS", prepared(3))
            monkeypatch.setitem(contracts._DEFINITIONS, NAME, prepared(3)[0]["function"])
            await kwargs["candidate_request_factory"](1, *backup)
            yield 'data: ' + json.dumps({"type": "fallback", "selected_model": "primary", "answered_by": "backup", "candidate_index": 1}) + '\n\n'
            yield 'data: ' + json.dumps({"type": "tool_calls", "calls": [{"id": "pdf-call", "name": NAME, "arguments": json.dumps({"path": "unused.pdf", "query": "test"})}]}) + '\n\n'
        else:
            yield 'data: {"delta": "Finished."}\n\n'
        yield 'data: [DONE]\n\n'
    async def synthetic_dispatch(*a, **k):
        return "synthetic", {"exit_code": 0, "output": "synthetic", "pdf_call_contract": capture_pdf_call(NAME).metadata()}
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", synthetic_dispatch)
    chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
        primary[0], primary[1], [{"role": "user", "content": "Find a section using pdf_find_section."}],
        headers={}, max_rounds=2, relevant_tools={NAME}, fallbacks=[backup], _is_teacher_run=True)]
    assert observations
    assert observations[0]["candidate_index"] == 1 and observations[0]["status"] == "match"
    events = [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
              if line.startswith("data: {")]
    output = next(event for event in events if event.get("type") == "tool_output" and event.get("tool") == NAME)
    assert output["schema_receipt_observation"] == observations[0]
    metrics = next(event["data"] for event in events if event.get("type") == "metrics")
    saved_event = next(event for event in metrics["tool_events"] if event.get("tool") == NAME)
    assert saved_event["schema_receipt_observation"] == observations[0]
    assert "parameters" not in json.dumps(observations[0])
