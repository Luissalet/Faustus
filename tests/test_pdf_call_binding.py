"""Falsifiable per-call capture and live revocation tests; no real PDF I/O."""
import asyncio
import json

import pytest

from src import pdf_tool_contracts as contracts
from src import tool_execution as execution
import importlib
from src.pdf_call_binding import capture_pdf_call
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_schemas import function_call_to_tool_block


NAME = "pdf_find_section"


def handlers():
    """The handler table dispatch reads now: other tests reload src.agent_tools,
    so a table imported when this module loaded can be a stale copy."""
    return importlib.import_module("src.agent_tools").TOOL_HANDLERS


def block():
    return function_call_to_tool_block(NAME, json.dumps({"path": "unused.pdf", "query": "chapter"}))


async def invoke(**kwargs):
    return (await execution.execute_tool_block(block(), security_context=ToolRunSecurityContext(), **kwargs))[1]


def handler(label):
    async def run(content, ctx):
        args = contracts.parse_content(NAME, content, definition=ctx.get("_pdf_call_definition"))
        return {"exit_code": 0, "output": label, "limit": args["limit"]}
    return run


def pause_dispatch(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    real = execution._execute_tool_block_impl

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(execution, "_execute_tool_block_impl", paused)
    return entered, release


async def test_concurrent_calls_keep_callable_and_parser_snapshot(monkeypatch):
    from src import tool_registry
    monkeypatch.setattr(tool_registry, "snapshot", lambda *a, **k: pytest.fail("full catalogue built"))
    monkeypatch.setitem(handlers(), NAME, handler("old"))
    entered, release = pause_dispatch(monkeypatch)
    first = asyncio.create_task(invoke())
    await entered.wait()
    replacement = contracts.function_definition(NAME)
    replacement["parameters"]["properties"]["limit"]["default"] = 3
    monkeypatch.setitem(contracts._DEFINITIONS, NAME, replacement)
    monkeypatch.setitem(handlers(), NAME, handler("new"))
    second = asyncio.create_task(invoke())
    await asyncio.sleep(0)
    release.set()
    old, new = await asyncio.gather(first, second)
    assert (old["output"], old["limit"]) == ("old", 8)
    assert (new["output"], new["limit"]) == ("new", 3)
    assert old["pdf_call_contract"]["descriptor_sha256"] != new["pdf_call_contract"]["descriptor_sha256"]
    assert old["pdf_call_contract"]["scope"] == "call"


@pytest.mark.parametrize("revocation", ["registration", "disabled", "policy"])
async def test_live_revocation_wins_over_capture(monkeypatch, revocation):
    class Policy:
        denied = False

        def blocks(self, name):
            return self.denied

    async def forbidden(*args):
        pytest.fail("revoked call reached handler")

    monkeypatch.setitem(handlers(), NAME, forbidden)
    disabled, policy = set(), Policy()
    entered, release = pause_dispatch(monkeypatch)
    task = asyncio.create_task(invoke(disabled_tools=disabled, tool_policy=policy))
    await entered.wait()
    if revocation == "registration":
        monkeypatch.delitem(handlers(), NAME)
    elif revocation == "disabled":
        disabled.add(NAME)
    else:
        policy.denied = True
    release.set()
    result = await task
    assert result["exit_code"] == 1 and result["error"]
    assert "pdf_call_contract" not in result


async def test_missing_registration_at_capture_cannot_be_granted_by_catalogue(monkeypatch):
    monkeypatch.delitem(handlers(), NAME)
    entered, release = pause_dispatch(monkeypatch)
    task = asyncio.create_task(invoke())
    await entered.wait()
    monkeypatch.setitem(handlers(), NAME, handler("too late"))
    release.set()
    result = await task
    assert result["exit_code"] == 1 and result["blocked"]


async def test_real_pdf_handler_consumes_captured_default(monkeypatch):
    from src import pdf_tree
    observed = []

    def fake_find(path, query, limit):
        observed.append(limit)
        return []

    monkeypatch.setattr(pdf_tree, "find_in_tree", fake_find)
    entered, release = pause_dispatch(monkeypatch)
    task = asyncio.create_task(invoke())
    await entered.wait()
    replacement = contracts.function_definition(NAME)
    replacement["parameters"]["properties"]["limit"]["default"] = 3
    monkeypatch.setitem(contracts._DEFINITIONS, NAME, replacement)
    release.set()
    result = await task
    assert result["exit_code"] == 0 and observed == [8]


def test_binding_exports_do_not_alias_snapshot():
    binding = capture_pdf_call(NAME)
    before = binding.metadata()
    exported = binding.definition()
    exported["parameters"]["properties"]["limit"]["default"] = 0
    before["descriptor_sha256"] = "mutated"
    assert binding.definition()["parameters"]["properties"]["limit"]["default"] == 8
    assert binding.metadata()["descriptor_sha256"] != "mutated"
    assert capture_pdf_call("not_registered") is None
