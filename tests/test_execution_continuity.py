from src.execution_continuity import carry_execution_receipts, offload_readers, SOURCE


def test_large_create_result_keeps_identity_and_failed_result_is_not_success():
    import json
    events = [
        {"tool": "deck_create", "command": '{"title":"TFM"}', "exit_code": 0,
         "output": json.dumps({"id": "deck-real", "brief": "x" * 40000})},
        {"tool": "source_add", "command": '{}', "exit_code": 1, "output": "file missing"},
        {"tool": "deck_create", "blocked": True, "output": "not executed"},
    ]
    out = carry_execution_receipts([{"role": "user", "content": "Make a deck"}], events)
    assert 'deck-real' in out[-1]['content']
    assert 'exit=1 result=file missing' in out[-1]['content']
    assert 'not executed' not in out[-1]['content']
    assert out[-1]['metadata']['trusted'] is False


def test_repeated_compaction_replaces_receipts_without_mutating_history():
    original = [{"role": "user", "content": "Use the template"}]
    events = [{"tool": "library_read", "command": '{"page":71}', "exit_code": 0,
               "output": '{"document_id":3,"citation":"TFM p.71"}'}]
    first = carry_execution_receipts(original, events)
    again = carry_execution_receipts(first, events)
    assert len(original) == 1
    assert sum(m.get('metadata', {}).get('source') == SOURCE for m in again) == 1
    assert 'TFM p.71' in again[-1]['content']


def test_latest_executions_survive_budget():
    events = [{"tool": "read", "command": str(i), "output": "x" * 300} for i in range(50)]
    out = carry_execution_receipts([], events, limit=1000)
    assert 'read 49' in out[-1]['content']
    assert 'read 0\n' not in out[-1]['content']


def test_offload_retrieval_is_offered_only_for_a_real_stub_and_respects_denials():
    from src.tool_policy import ToolPolicy
    stub = {'_tool_result_offload': True, 'artifact_id': 'occ_actual'}
    assert offload_readers(stub, policy=ToolPolicy(mode='mcp_only')) == {'read_artifact', 'artifact_search'}
    assert offload_readers(stub, {'read_artifact'}, ToolPolicy(disabled_tools=frozenset({'artifact_search'}))) == set()
    assert offload_readers({'artifact_id': 'external-text'}) == set()
    assert offload_readers({'_tool_result_offload': True}) == set()
