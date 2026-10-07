from src.named_tool_names import named_tools_in_request


def test_native_names_and_unique_mcp_short_names_are_resolved():
    available = {'git_status', 'bash', 'mcp__links__reading_plan', 'mcp__links__read_link'}
    assert named_tools_in_request('Use git_status and reading_plan; then READ_LINK.', available) == {
        'git_status', 'mcp__links__reading_plan', 'mcp__links__read_link'}
    assert named_tools_in_request('Talk about bash and a reading planner.', available) == set()


def test_ambiguous_short_names_need_qualification():
    available = {'mcp__one__read_link', 'mcp__two__read_link'}
    assert named_tools_in_request('Call read_link.', available) == set()
    assert named_tools_in_request('Call mcp__one__read_link.', available) == {'mcp__one__read_link'}


def test_disabled_and_disconnected_tools_are_not_promoted():
    available = {'mcp__links__read_link', 'mcp__links__reading_plan'}
    assert named_tools_in_request('read_link reading_plan mcp__gone__read_link', available,
                                  {'mcp__links__read_link'}) == {'mcp__links__reading_plan'}


def test_native_exact_name_keeps_precedence_and_substrings_do_not_match():
    available = {'read_file', 'mcp__other__read_file', 'mcp__links__reading_plan'}
    assert named_tools_in_request('read_file reading_planner reading_plan_extra', available) == {'read_file'}


def test_loop_offers_named_mcp_already_selected_by_caller_without_lookup(tmp_path, monkeypatch):
    import json
    import src.agent_loop as al
    from tests.test_agent_harness_loop import _collect, _events, _patch_common
    tool = 'mcp__links__reading_plan'
    schema = {'type': 'function', 'function': {'name': tool, 'description': 'Plan reading time.',
               'parameters': {'type': 'object', 'properties': {}}}}
    class Manager:
        def get_all_openai_schemas(self, *args): return [schema]
        def get_tool_descriptions_for_prompt(self, *args, **kwargs): return ''
        def has_remote_servers(self): return False
        async def disconnect_server(self, *args): return None
    _patch_common(monkeypatch)
    manager = Manager()
    monkeypatch.setattr(al, 'get_mcp_manager', lambda: manager)
    monkeypatch.setattr(al, '_agent_route_tool_mode', lambda *args, **kwargs: (True, False, True))
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: manager)
    monkeypatch.setattr('src.connector_policy.resolve_allowed_servers_for_session', lambda *args: None)
    seen = []
    async def stream(candidates, messages, **kwargs):
        request = await kwargs['candidate_request_factory'](0, *candidates[0])
        seen.append({s['function']['name'] for s in (request.get('kwargs', {}).get('tools') or [])})
        yield 'data: '+json.dumps({'delta':'{"available":true}'})+'\n\n'
        yield 'data: '+json.dumps({'type':'finish','finish_reason':'stop'})+'\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = _events(_collect(al.stream_agent_loop('http://x/v1','m',
        [{'role':'user','content':'Explain the tool reading_plan without executing it. Return JSON.'}],
        owner='alice',session_id='named-tool',workspace=str(tmp_path),relevant_tools={tool,'lookup_tools','ask_user'},
        max_rounds=1,harness_options={'no_memory':True,'no_skills':True,'repo_map':False,'review_model':'off'})))
    assert tool in seen[0]
    assert not [event for event in events if event.get('type')=='tool_output']


def _capture_named_schema_requests(tmp_path, monkeypatch, *, user, relevant_tools, allowed_servers,
                                   replies):
    import json
    import src.agent_loop as al
    from tests.test_agent_harness_loop import _collect, _events, _patch_common

    tool = 'mcp__links__reading_plan'
    schema = {'type': 'function', 'function': {'name': tool, 'description': 'Read a reading plan.',
              'parameters': {'type': 'object', 'properties': {}}}}

    class Manager:
        def get_all_openai_schemas(self, *args, **kwargs):
            return [schema]
        def get_all_tools(self, *args, **kwargs):
            return []
        def get_tool_descriptions_for_prompt(self, *args, **kwargs):
            return ''
        def has_remote_servers(self):
            return False
        async def disconnect_server(self, *args):
            return None

    class Index:
        def index_mcp_tools(self, *args, **kwargs):
            return None
        def get_tools_for_query(self, *args, **kwargs):
            return {'ask_user', 'lookup_tools'}

    _patch_common(monkeypatch)
    manager = Manager()
    monkeypatch.setattr(al, 'get_mcp_manager', lambda: manager)
    monkeypatch.setattr(al, '_agent_route_tool_mode', lambda *args, **kwargs: (True, False, True))
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: manager)
    monkeypatch.setattr('src.connector_policy.resolve_allowed_servers_for_session',
                        lambda *args: allowed_servers)
    monkeypatch.setattr('src.tool_index.get_tool_index', lambda: Index())

    seen = []
    call_count = {'n': 0}

    async def stream(candidates, messages, **kwargs):
        request = await kwargs['candidate_request_factory'](0, *candidates[0])
        seen.append([item['function']['name'] for item in request.get('kwargs', {}).get('tools') or []])
        idx = min(call_count['n'], len(replies) - 1)
        call_count['n'] += 1
        delta, finish = replies[idx]
        yield 'data: ' + json.dumps({'delta': delta}) + '\n\n'
        yield 'data: ' + json.dumps({'type': 'finish', 'finish_reason': finish}) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = _events(_collect(al.stream_agent_loop(
        'http://x/v1', 'm', [{'role': 'user', 'content': user}],
        owner='alice', session_id='named-tool-policy', max_rounds=len(replies),
        relevant_tools=relevant_tools,
        harness_options={'no_memory': True, 'no_skills': True, 'repo_map': False,
                         'review_model': 'off'},
    )))
    return tool, seen, events


def test_named_connected_mcp_schema_is_hot_and_stable_across_rounds(tmp_path, monkeypatch):
    tool, seen, _events = _capture_named_schema_requests(
        tmp_path, monkeypatch,
        user='Explain the tool reading_plan without executing it. Return JSON.',
        relevant_tools=None,
        allowed_servers={'links'},
        replies=[('La respuesta se continúa.', 'length'), ('{"available":true}', 'stop')],
    )
    assert len(seen) == 2
    assert tool in seen[0]
    assert seen[0] == seen[1]


def test_named_schema_does_not_expand_a_caller_pinned_tool_set(tmp_path, monkeypatch):
    tool, seen, _events = _capture_named_schema_requests(
        tmp_path, monkeypatch,
        user='Use reading_plan to read the current schedule.',
        relevant_tools={'ask_user', 'lookup_tools'},
        allowed_servers={'links'},
        replies=[('{"available":true}', 'stop')],
    )
    assert len(seen) == 1
    assert tool not in seen[0]


def test_named_schema_does_not_bypass_connector_allowlist(tmp_path, monkeypatch):
    tool, seen, _events = _capture_named_schema_requests(
        tmp_path, monkeypatch,
        user='Use reading_plan to read the current schedule.',
        relevant_tools=None,
        allowed_servers=set(),
        replies=[('{"available":false}', 'stop')],
    )
    assert len(seen) == 1
    assert tool not in seen[0]
