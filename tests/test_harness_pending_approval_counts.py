from src.agent_harness import TurnLedger


def test_permission_wait_is_not_an_execution_failure_or_success():
    ledger = TurnLedger()
    ledger.record('delegate_agents', '{}', {'exit_code': None, 'approval_required': True})
    ledger.record('read_file', '{}', {'exit_code': 1, 'error': 'missing'})
    summary = ledger.summary()
    assert summary['tool_calls'] == 2
    assert summary['failed_calls'] == 1
    assert summary['waiting_approval_calls'] == 1
    assert not any(event['ok'] for event in ledger.events)
    assert not ledger.mutations and not ledger.effects
