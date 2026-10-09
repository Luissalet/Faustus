"""Native MCP policy fields must reach the same compiler policy as chat flags."""
import asyncio

import pytest

from src.context_engine.contracts import ContextRequest, ContractError
import mcp_servers.context_engine_server as server


def test_native_compile_and_explain_advertise_recall_policy():
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in ['context_compile', 'context_explain']:
        props = tools[name].inputSchema['properties']
        assert props['allow_personal_memory']['type'] == 'boolean'
        assert props['allow_semantic_lane']['type'] == 'boolean'


def test_native_mcp_request_pins_owner_and_recall_flags(monkeypatch):
    monkeypatch.setattr(server, '_engine', {'ContextRequest': ContextRequest})
    request = server._request('trusted-owner', {'owner': 'foreign', 'allow_personal_memory': False,
                                               'allow_semantic_lane': False, 'project_id': 'p1'})
    assert request.execution.owner == 'trusted-owner'
    assert request.execution.project_id == 'p1'
    assert not request.policy.allow_personal_memory
    assert not request.policy.allow_semantic_lane
    assert server._request('trusted-owner', {}).policy.allow_personal_memory


@pytest.mark.parametrize('key', ['allow_personal_memory', 'allow_semantic_lane'])
def test_mcp_does_not_coerce_string_false_to_true(monkeypatch, key):
    monkeypatch.setattr(server, '_engine', {'ContextRequest': ContextRequest})
    with pytest.raises(ContractError):
        server._request('trusted-owner', {key: 'false'})
