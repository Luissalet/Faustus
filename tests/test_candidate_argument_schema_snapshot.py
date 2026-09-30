"""Validation/repair follows the prepared answering candidate, not live schemas."""
import copy
import json
import pytest
from src import agent_loop as loop, tool_schemas as schemas
from src.tool_schema_receipts import capture_argument_schemas, argument_snapshot_for_answer

NAME = 'write_file'

def definition():
    return copy.deepcopy(next(s for s in schemas.FUNCTION_TOOL_SCHEMAS if s['function']['name'] == NAME))

def capture(prepared=None, index=0, round_num=1, **kwargs):
    return capture_argument_schemas(prepared if prepared is not None else [definition()],
                                    candidate_index=index, round_num=round_num, **kwargs)

def resolve(args, snapshot, status=None, root=None):
    meta = {}
    blocks, native, calls = loop._resolve_tool_blocks('', [{'name': NAME, 'arguments': json.dumps(args)}],
        1, arg_validation=meta, path_roots=[str(root)] if root else None,
        argument_snapshot=snapshot, schema_receipt=status)
    return blocks[0], meta.get(id(blocks[0]))


async def test_live_nested_schema_switch_cannot_repair_valid_content_into_boolean(tmp_path, monkeypatch):
    from src import tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda _: True)
    (tmp_path / '.git').mkdir()
    prepared = [definition()]; snapshot = capture(prepared)
    live = next(s for s in schemas.FUNCTION_TOOL_SCHEMAS if s['function']['name'] == NAME)
    monkeypatch.setitem(live['function']['parameters']['properties'], 'content', {'type': 'boolean'})
    args = {'path': str(tmp_path / 'result.txt'), 'content': 'false'}
    block, meta = resolve(args, snapshot, root=tmp_path)
    assert meta is None
    _, result = await tool_execution.execute_tool_block(block, workspace=str(tmp_path),
        owner='synthetic-admin', security_context=ToolRunSecurityContext())
    assert result['exit_code'] == 0 and (tmp_path / 'result.txt').read_text() == 'false'


def test_preparation_and_decoding_are_isolated_including_nested_arrays():
    prepared = [definition()]
    params = prepared[0]['function']['parameters']
    params['properties']['content']['enum'] = ['first', 'second']
    snapshot = capture(prepared)
    params['required'].append('LATE_REQUIRED')
    params['properties']['content']['enum'].append('LATE_ENUM')
    first = snapshot.parameters_for(NAME)
    first['required'].append('CONSUMER_MUTATION')
    first['properties']['content']['enum'].append('CONSUMER_ENUM')
    second = snapshot.parameters_for(NAME)
    assert 'LATE_REQUIRED' not in second['required']
    assert 'CONSUMER_MUTATION' not in second['required']
    assert second['properties']['content']['enum'] == ['first', 'second']


def test_validation_repair_and_revalidation_use_same_snapshot(monkeypatch):
    prepared = copy.deepcopy(next(s for s in schemas.FUNCTION_TOOL_SCHEMAS if s['function']['name'] == 'read_file'))
    snapshot = capture([prepared])
    live = next(s for s in schemas.FUNCTION_TOOL_SCHEMAS if s['function']['name'] == 'read_file')
    monkeypatch.setitem(live['function']['parameters']['properties'], 'limit', {'type': 'boolean'})
    meta = {}; blocks, _, _ = loop._resolve_tool_blocks('', [
        {'name': 'read_file', 'arguments': json.dumps({'path': 'fixture.txt', 'limit': '50'})}],
        1, arg_validation=meta, argument_snapshot=snapshot)
    assert json.loads(blocks[0].content)['limit'] == 50
    assert not meta[id(blocks[0])]['blocked']
    assert meta[id(blocks[0])]['repairs'][0]['to'] == 50


@pytest.mark.parametrize('case,reason', [('missing', 'candidate_snapshot_missing'),
    ('index', 'stale_candidate_snapshot'), ('round', 'stale_candidate_snapshot'),
    ('text', 'text_only_candidate'), ('duplicate', 'duplicate_schema_name'),
    ('invalid_index', 'candidate_snapshot_missing')])
def test_unknown_or_stale_answer_never_guesses_primary(case, reason):
    primary = capture(index=0)
    current = capture(index=1)
    index = 1; round_num = 1
    if case == 'missing': current = None
    elif case == 'index': current = primary
    elif case == 'round': round_num = 2
    elif case == 'text': current = capture(index=1, text_only=True)
    elif case == 'duplicate': current = capture([definition(), definition()], index=1)
    elif case == 'invalid_index': index = True
    snapshot, status = argument_snapshot_for_answer({0: {'argument_snapshot': primary},
        1: {'argument_snapshot': current}}, index, round_num=round_num)
    assert snapshot is None and status['status'] == 'legacy' and status['reason'] == reason


def test_fallback_selects_its_own_contract_not_primary():
    primary_schema = definition(); primary_schema['function']['parameters']['properties']['content']['type'] = 'boolean'
    primary = capture([primary_schema], index=0)
    backup = capture(index=1)
    snapshot, status = argument_snapshot_for_answer({0: {'argument_snapshot': primary},
        1: {'argument_snapshot': backup}}, 1, round_num=1)
    block, meta = resolve({'path': 'result.txt', 'content': 'false'}, snapshot, status)
    assert block.content == 'result.txt\nfalse'
    assert meta['schema_validation_receipt']['candidate_index'] == 1
    assert 'parameters' not in json.dumps(meta)


def test_missing_receipt_uses_explicit_legacy_compatibility():
    snapshot, status = argument_snapshot_for_answer({}, 1, round_num=1)
    _, meta = resolve({'path': 'result.txt', 'content': 'false'}, snapshot, status)
    assert meta['schema_validation_receipt']['status'] == 'legacy' and not meta['blocked']


async def test_real_loop_prepared_fallback_survives_live_schema_switch(tmp_path, monkeypatch):
    from src import tool_execution
    monkeypatch.setattr(loop, 'get_setting', lambda key, default=None: False if key in
        ('agent_compact_mode', 'agent_deferral_enabled', 'agent_tool_schema_slim') else default)
    monkeypatch.setattr(loop, '_build_system_prompt', lambda messages, *a, **k: (list(messages), []))
    monkeypatch.setattr(loop, '_detect_admin_intent', lambda _: True)
    monkeypatch.setattr(loop, '_agent_route_tool_mode', lambda *a, **k: (True, False, False))
    monkeypatch.setattr(loop, 'blocked_tools_for_owner', lambda _: set())
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda _: True)
    (tmp_path / '.git').mkdir()
    primary_schema = definition()
    primary_schema['function']['parameters']['properties']['content']['type'] = 'boolean'
    monkeypatch.setattr(loop, 'FUNCTION_TOOL_SCHEMAS', [primary_schema])
    primary = ('https://primary.invalid/v1', 'primary', {})
    backup = ('https://backup.invalid/v1', 'backup', {})
    rounds = 0; requests = []; dispatched = []
    async def stream(candidates, messages, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            requests.append(await kwargs['candidate_request_factory'](0, *primary))
            backup_schema = definition()
            monkeypatch.setattr(loop, 'FUNCTION_TOOL_SCHEMAS', [backup_schema])
            requests.append(await kwargs['candidate_request_factory'](1, *backup))
            # Mutate both original source containers after preparation, as a
            # hot update while the answering model is still streaming.
            backup_schema['function']['parameters']['properties']['content']['type'] = 'boolean'
            live = next(s for s in schemas.FUNCTION_TOOL_SCHEMAS if s['function']['name'] == NAME)
            monkeypatch.setitem(live['function']['parameters']['properties'], 'content', {'type': 'boolean'})
            assert requests[1]['kwargs']['tools'][0]['function']['parameters']['properties']['content']['type'] == 'string'
            yield 'data: ' + json.dumps({'type': 'fallback', 'selected_model': 'primary',
                'answered_by': 'backup', 'candidate_index': 1}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'tool_calls', 'calls': [{'id': 'write-call',
                'name': NAME, 'arguments': json.dumps({'path': str(tmp_path / 'written.txt'), 'content': 'false'})}]}) + '\n\n'
        else:
            yield 'data: {"delta":"Finished."}\n\n'
        yield 'data: [DONE]\n\n'
    real_execute = tool_execution.execute_tool_block
    async def execute(block, **kwargs):
        dispatched.append(block); return await real_execute(block, **kwargs)
    monkeypatch.setattr(loop, 'stream_llm_with_fallback', stream)
    monkeypatch.setattr(loop, 'execute_tool_block', execute)
    chunks = [c async for c in loop.stream_agent_loop(primary[0], primary[1],
        [{'role': 'user', 'content': 'Write the exact word false to the requested fixture file.'}],
        headers={}, max_rounds=2, relevant_tools={NAME}, fallbacks=[backup],
        workspace=str(tmp_path), owner='synthetic-admin', _is_teacher_run=True)]
    assert len(dispatched) == 1 and (tmp_path / 'written.txt').read_text() == 'false'
    events = [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
              if line.startswith('data: {')]
    output = next(e for e in events if e.get('type') == 'tool_output' and e.get('tool') == NAME)
    receipt = output['schema_validation_receipt']
    assert receipt['status'] == 'captured' and receipt['candidate_index'] == 1 and receipt['round_num'] == 1
    assert 'parameters' not in json.dumps(receipt) and 'synthetic-admin' not in json.dumps(receipt)
    metrics = next(e['data'] for e in events if e.get('type') == 'metrics')
    saved = next(e for e in metrics['tool_events'] if e.get('tool') == NAME)
    assert saved['schema_validation_receipt'] == receipt


def test_removed_live_definition_does_not_drop_prepared_validation(monkeypatch):
    prepared = definition()
    prepared['function']['parameters']['properties']['content']['enum'] = ['allowed']
    snapshot = capture([prepared])
    monkeypatch.setattr(schemas, 'FUNCTION_TOOL_SCHEMAS', [
        s for s in schemas.FUNCTION_TOOL_SCHEMAS if s['function']['name'] != NAME])
    _, meta = resolve({'path': 'result.txt', 'content': 'false'}, snapshot,
                     {'stage': 'candidate_prepared', 'status': 'captured'})
    assert meta['blocked'] and meta['errors'][0]['field'] == 'content'
    assert meta['schema_validation_receipt']['status'] == 'captured'


def test_unadvertised_tool_uses_explicit_legacy_without_claiming_capture():
    snapshot = capture([])
    _, meta = resolve({'path': 'result.txt', 'content': 'false'}, snapshot,
                     {'stage': 'candidate_prepared', 'status': 'captured'})
    assert not meta['blocked']
    assert meta['schema_validation_receipt']['status'] == 'legacy'
    assert meta['schema_validation_receipt']['reason'] == 'tool_not_in_prepared_schemas'


def test_pdf_and_mcp_keep_separate_contract_paths():
    snapshot = capture()
    from src.tool_schemas import function_call_to_tool_block
    args = {'value': 'false'}
    block = function_call_to_tool_block('mcp__synthetic__tool', json.dumps(args))
    _, meta = loop._validate_native_tool_call('mcp__synthetic__tool', args, block,
        argument_snapshot=snapshot, schema_receipt={'status': 'captured'})
    assert meta['schema_validation_receipt']['status'] == 'legacy'
    assert meta['schema_validation_receipt']['reason'] == 'separate_tool_contract'
    assert not meta['blocked'] and not meta['repairs']


def test_prepared_unknown_schema_does_not_create_new_validation_surface():
    from src.agent_tools import ToolBlock
    name = 'synthetic_unregistered'
    snapshot = capture([{'type': 'function', 'function': {'name': name,
        'parameters': {'type': 'object', 'required': ['injected']}}}])
    _, meta = loop._validate_native_tool_call(name, {}, ToolBlock(name, '{}'),
        argument_snapshot=snapshot, schema_receipt={'status': 'captured'})
    assert not meta['blocked'] and not meta['errors']
    assert meta['schema_validation_receipt']['reason'] == 'separate_tool_contract'
