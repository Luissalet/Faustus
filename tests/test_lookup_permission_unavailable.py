"""Unavailable permission authority must not advertise executable schemas."""
import json
import pytest
from src import tool_discovery as discovery, tool_security, tool_serve as serve
from src.agent_tools import TOOL_HANDLERS
from src.tool_policy import ToolPolicy

class UnavailablePolicy:
    def blocks(self, name):
        raise PermissionError('synthetic permission authority unavailable')

@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setattr(tool_security, 'owner_is_admin_or_single_user', lambda _: True)
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: None)
    monkeypatch.setattr('src.tool_index.get_tool_index', lambda: None)

async def lookup(args, policy):
    _, result = await TOOL_HANDLERS['lookup_tools'](json.dumps(args), {'tool_policy': policy})
    payload = json.loads(result['output'])
    assert payload == result['lookup_tools'] and result['promote'] == payload['promote']
    return payload

@pytest.mark.parametrize('detail', ['schema', 'catalog'])
async def test_unavailable_policy_does_not_expose_or_promote(detail):
    payload = await lookup({'names': ['read_file'], 'detail': detail}, UnavailablePolicy())
    assert payload['tools'] == [] and payload['promote'] == []
    assert 'callable this turn' not in payload['hint']

async def test_unavailable_policy_matches_real_dispatch_preflight_no_execution(monkeypatch):
    from src.tool_execution import execute_tool_block
    from src.tool_schemas import function_call_to_tool_block
    from src.tool_capabilities import ToolRunSecurityContext
    async def forbidden(*args): pytest.fail('permission error reached handler')
    monkeypatch.setitem(TOOL_HANDLERS, 'read_file', forbidden)
    policy = UnavailablePolicy()
    with pytest.raises(PermissionError):
        await execute_tool_block(function_call_to_tool_block('read_file', '{"path":"never-read.txt"}'),
                                 tool_policy=policy, security_context=ToolRunSecurityContext())
    payload = await lookup({'names': ['read_file'], 'detail': 'schema'}, policy)
    assert payload['tools'] == []

@pytest.mark.parametrize('category', ['', 'files'])
async def test_category_exposure_is_empty_when_policy_unavailable(category):
    args = {'category': category} if category else {'categories': True}
    payload = await lookup(args, UnavailablePolicy())
    assert not payload.get('categories') and not payload.get('tools') and payload['promote'] == []


def test_audit_fallback_never_includes_unverifiable_tools():
    audit = discovery.audit_selection('read_file', ['read_file'], tool_policy=UnavailablePolicy(),
                                      fallback_pool=['read_file', 'ask_user', 'web_search'])
    assert audit.resolved == [] and audit.fallback == []
    assert 'none is permitted' in audit.reason

@pytest.mark.parametrize('policy', [None, ToolPolicy()])
async def test_absent_or_normal_permitting_policy_preserves_native_schema(policy):
    payload = await lookup({'names': ['read_file'], 'detail': 'schema'}, policy)
    assert payload['promote'] == ['read_file']
    assert payload['tools'][0]['schema']['function']['name'] == 'read_file'

async def test_one_unavailable_tool_does_not_hide_other_permitted_tools():
    class PartialPolicy:
        def blocks(self, name):
            if name == 'read_file': raise PermissionError('synthetic unavailable decision')
            return False
    payload = await lookup({'names': ['read_file', 'ask_user'], 'detail': 'schema'}, PartialPolicy())
    assert payload['promote'] == ['ask_user']
    assert [row['name'] for row in payload['tools']] == ['ask_user']
    audit = discovery.audit_selection('read_file', ['read_file'], tool_policy=PartialPolicy(),
                                      fallback_pool=['read_file', 'ask_user'])
    assert audit.resolved == [] and audit.fallback == ['ask_user']

@pytest.mark.parametrize('name', ['send_email', 'mcp__email__send_email'])
def test_email_aliases_do_not_bypass_unavailable_authority(monkeypatch, name):
    from src.mcp_manager import McpManager
    m = McpManager()
    # Synthetic discoverable remote email server, not the local fence-only builtin.
    monkeypatch.setattr(m, 'is_builtin', lambda _: False)
    m._tools['email'] = [{'name': 'send_email', 'description': 'Fixture email operation',
                          'input_schema': {'type': 'object', 'properties': {'to': {'type': 'string'}}}}]
    m._connections['email'] = {'name': 'Fixture Email', 'status': 'connected'}
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: m)
    assert discovery.is_known_tool(name)
    assert not discovery.is_permitted(name, tool_policy=UnavailablePolicy())
    assert not discovery.is_permitted(name, tool_policy=ToolPolicy(disabled_tools=frozenset({'send_email'})))
