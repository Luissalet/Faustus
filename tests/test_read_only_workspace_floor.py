"""read_only must stay read-only when a workspace floor is active.

These tests run the production agent loop with a fake native provider and a
fake executor. The workspace is temporary; no tool implementation touches it.
"""

import asyncio
import json

import pytest

import src.agent_tools  # noqa: F401 - register schemas before importing the loop
import src.agent_loop as agent_loop
import src.tool_capabilities as tool_capabilities
import src.tool_index as tool_index


API_ENDPOINT = "http://127.0.0.1:11434/v1"

_ARGS = {
    "bash": {"command": "echo isolated"},
    "python": {"code": "print(1)"},
    "edit_file": {"path": "input.txt", "old_string": "old", "new_string": "new"},
    "apply_patch": {"patch": "*** Begin Patch\n*** End Patch\n"},
    "read_file": {"path": "input.txt"},
    "inspect_deliverable": {"path": "input.txt"},
}


@pytest.fixture(autouse=True)
def _isolate_agent_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("FAUSTUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "odysseus"))
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(
        agent_loop,
        "_agent_route_tool_mode",
        lambda *a, **k: (True, False, True),
        raising=False,
    )
    monkeypatch.setattr(tool_capabilities, "tool_approval_mode", lambda: "full", raising=False)
    # Caller-pinned selections below avoid retrieval. This also makes any
    # accidental index initialization fail immediately instead of using cache
    # or attempting network access.
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)


def _run_turn(
    monkeypatch,
    workspace,
    *,
    autonomy_preset,
    relevant_tools,
    calls=(),
    forced_tools=None,
    plan_mode=False,
):
    offered = []
    executed = []
    output_events = []
    pending_calls = list(calls)
    request_count = 0

    async def fake_provider(candidates, messages, **kwargs):
        nonlocal request_count
        schemas = kwargs.get("tools") or []
        if request_count == 0:
            offered.extend(
                schema.get("function", {}).get("name")
                for schema in schemas
                if schema.get("function")
            )
            batch = list(pending_calls)
            pending_calls.clear()
            if batch:
                yield "data: " + json.dumps({
                    "type": "tool_calls",
                    "calls": [
                        {"name": name, "arguments": json.dumps(_ARGS.get(name, {}))}
                        for name in batch
                    ],
                }) + "\n\n"
            else:
                yield "data: " + json.dumps({"delta": "Done."}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "Done."}) + "\n\n"
        request_count += 1
        yield "data: [DONE]\n\n"

    async def fake_executor(block, *args, **kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {"output": "stubbed", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_provider, raising=False)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_executor, raising=False)

    async def drive():
        stream = agent_loop.stream_agent_loop(
            endpoint_url=API_ENDPOINT,
            model="fake-provider",
            messages=[{"role": "user", "content": "Inspect the isolated workspace."}],
            headers={},
            workspace=str(workspace),
            owner="admin",
            session_id="read-only-workspace-floor-test",
            max_rounds=2,
            context_length=32768,
            relevant_tools=set(relevant_tools),
            forced_tools=set(forced_tools or ()),
            autonomy_preset=autonomy_preset,
            plan_mode=plan_mode,
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
        )
        chunks = [chunk async for chunk in stream]
        for chunk in chunks:
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    event = json.loads(chunk[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "tool_output":
                    output_events.append(event)

    asyncio.run(drive())
    return {
        "offered": set(offered),
        "executed": executed,
        "tool_outputs": output_events,
    }


def test_read_only_workspace_floor_does_not_offer_pinned_or_forced_mutators(
    monkeypatch, tmp_path
):
    (tmp_path / "input.txt").write_text("unchanged\n", encoding="utf-8")
    names = {"bash", "python", "edit_file", "apply_patch", "read_file", "inspect_deliverable"}

    result = _run_turn(
        monkeypatch,
        tmp_path,
        autonomy_preset="read_only",
        relevant_tools=names,
        forced_tools={"bash", "python", "edit_file", "apply_patch"},
    )

    assert {"bash", "python", "edit_file", "apply_patch"}.isdisjoint(result["offered"])
    assert {"read_file", "inspect_deliverable"} <= result["offered"]


def test_read_only_rejects_native_python_call_before_executor(monkeypatch, tmp_path):
    result = _run_turn(
        monkeypatch,
        tmp_path,
        autonomy_preset="read_only",
        relevant_tools={"python"},
        calls=["python"],
        forced_tools={"python"},
    )

    assert "python" not in result["offered"]
    assert "python" not in result["executed"]
    denied = [event for event in result["tool_outputs"] if event.get("tool") == "python"]
    assert denied and (denied[0].get("blocked") or denied[0].get("error"))


def test_read_only_keeps_workspace_inspection_tools_usable(monkeypatch, tmp_path):
    (tmp_path / "input.txt").write_text("safe to inspect\n", encoding="utf-8")
    result = _run_turn(
        monkeypatch,
        tmp_path,
        autonomy_preset="read_only",
        relevant_tools={"read_file", "inspect_deliverable"},
        calls=["read_file", "inspect_deliverable"],
    )

    assert {"read_file", "inspect_deliverable"} <= result["offered"]
    assert {"read_file", "inspect_deliverable"} <= set(result["executed"])


def test_supervised_workspace_floor_still_offers_and_runs_mutators(monkeypatch, tmp_path):
    result = _run_turn(
        monkeypatch,
        tmp_path,
        autonomy_preset="supervised",
        relevant_tools={"python", "edit_file"},
        calls=["python", "edit_file"],
    )

    assert {"python", "edit_file"} <= result["offered"]
    assert {"python", "edit_file"} <= set(result["executed"])


def test_read_only_blocks_bash_native_alias_even_when_unoffered(monkeypatch, tmp_path):
    result = _run_turn(
        monkeypatch,
        tmp_path,
        autonomy_preset="read_only",
        relevant_tools={"bash"},
        calls=["shell"],
    )

    assert "bash" not in result["offered"]
    assert "bash" not in result["executed"]
    denied = [event for event in result["tool_outputs"] if event.get("tool") == "bash"]
    assert denied and (denied[0].get("blocked") or denied[0].get("error"))
