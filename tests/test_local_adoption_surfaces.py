"""HTTP/agent parity, owner propagation and the existing gates on new local tools."""
import asyncio
import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


@pytest.mark.parametrize('path,tool,body', [
    ('browser', 'reach_browser', {'action': 'status'}),
    ('crawl', 'reach_crawl', {'action': 'status', 'run_id': 'a' * 32}),
    ('recipe', 'reach_recipe', {'action': 'list'}),
])
def test_reach_http_and_tool_owner_parity(monkeypatch, path, tool, body):
    import routes.reach_routes as routes
    from src.agent_tools import TOOL_HANDLERS
    observed = []
    def plain(request, *, owner):
        observed.append((request.action, owner))
        return {'results': [{'owner': owner}], 'untrusted_content': True}
    async def async_call(request, *, owner): return plain(request, owner=owner)
    module, func = {'browser': ('src.reach.browser', 'run_browser'), 'crawl': ('src.reach.crawl', 'run_crawl'),
                    'recipe': ('src.reach.recipes', 'run_recipe')}[path]
    monkeypatch.setattr(module + '.' + func, plain if path == 'crawl' else async_call)
    monkeypatch.setattr(routes, 'require_admin', lambda _: None)
    monkeypatch.setattr(routes, 'effective_user', lambda _: 'alice')
    app = FastAPI()
    app.include_router(routes.setup_reach_routes())
    with TestClient(app) as client:
        response = client.post('/api/reach/' + path, json=body)
        assert response.status_code == 200
        out = asyncio.run(TOOL_HANDLERS[tool](json.dumps(body), {'owner': 'alice'}))
        assert json.loads(out['output']) == response.json()
        def refuse(_): raise HTTPException(403, 'admin required')
        monkeypatch.setattr(routes, 'require_admin', refuse)
        assert client.post('/api/reach/' + path, json=body).status_code == 403
    assert observed == [(body['action'], 'alice')] * 2


def test_local_status_operations_do_not_declare_network():
    from src.tool_capabilities import capabilities_for_action, ToolEffect
    for tool, action in [('reach_browser', 'status'), ('reach_browser', 'close'), ('reach_crawl', 'status'),
                         ('reach_crawl', 'export'), ('reach_crawl', 'cancel'), ('reach_recipe', 'list'), ('reach_recipe', 'delete')]:
        caps = capabilities_for_action(tool, {'action': action})
        assert caps.known and ToolEffect.NETWORK_EGRESS not in caps.effects
    assert ToolEffect.NETWORK_EGRESS in capabilities_for_action('reach_browser', {'action': 'capture'}).effects
    assert ToolEffect.WRITE_PRIVATE in capabilities_for_action('reach_recipe', {'action': 'learn'}).effects


def test_graph_http_and_agent_time_contract(monkeypatch):
    import routes.brain_routes as routes
    from src.agent_tools.brain_graph_tool import BrainGraphTool
    from src.brain.temporal_graph import GraphQuery
    seen = []
    def retrieve(owner, request):
        assert isinstance(request, GraphQuery)
        seen.append((owner, request.known_at, request.as_of))
        return {'nodes': [{'id': 'node'}], 'edges': [], 'recorded_from': '2026-01-01'}
    monkeypatch.setattr('src.brain.temporal_graph.retrieve', retrieve)
    monkeypatch.setattr(routes, '_owner', lambda _: 'alice')
    app = FastAPI()
    app.dependency_overrides[routes.require_user] = lambda: 'alice'
    app.include_router(routes.setup_brain_routes())
    body = {'known_at': '2026-02-01', 'as_of': '2025-01-01'}
    with TestClient(app) as client:
        response = client.post('/api/brain/temporal-graph', json=body)
        assert response.status_code == 200 and response.json()['nodes'] == [{'id': 'node'}]
    out = asyncio.run(BrainGraphTool().execute(json.dumps(body), {'owner': 'alice'}))
    assert json.loads(out['output'])['edge_count'] == 0
    assert seen == [('alice', '2026-02-01', '2025-01-01')] * 2
