from src.agent_loop import _usage_bucket, _usage_bucket_summary
from src.agent_tools.subagent_tools import SubagentRun


def bucket(**overrides):
    values = {
        'round_num': 2, 'model': 'local-q8', 'endpoint_id': 'ep-local',
        'endpoint_label': 'Local', 'endpoint_cost_tracked': False,
        'input_tokens': 4000, 'output_tokens': 280,
        'usage_source': 'real', 'route_decision': None,
    }
    values.update(overrides)
    return _usage_bucket(**values)


def test_round_receipt_keeps_backend_phases_and_client_duration_with_provenance():
    record = bucket(
        cached_tokens=1200,
        engine_timings={
            'source': 'llamacpp', 'prompt_ms': 5400.0, 'predicted_ms': 9100.0,
            'prompt_n': 4000, 'predicted_n': 280, 'cache_n': 1200,
        },
        request_duration_ms=15430.125,
    )
    assert record['cached_tokens'] == 1200
    assert record['engine_timings']['prompt_ms'] == 5400.0
    assert record['engine_timings']['predicted_ms'] == 9100.0
    assert record['engine_timings']['source'] == 'llamacpp'
    assert record['request_duration_ms'] == 15430.125
    assert record['request_duration_source'] == 'observed_client'
    assert _usage_bucket_summary([record])['usage_buckets'] == [record]


def test_absent_engine_timings_are_not_invented_for_a_provider_without_them():
    record = bucket(usage_source='estimated')
    assert 'engine_timings' not in record
    assert 'request_duration_ms' not in record
    assert record['usage_source'] == 'estimated'


def test_subagent_report_preserves_actual_round_timings():
    run = SubagentRun(0, {'name': 'worker', 'instruction': 'inspect fixture'})
    for round_num in range(1, 4):
        run.round_activity.append({
            'round': round_num, 'model': 'worker-q8',
            'engine_timings': {'prompt_ms': round_num * 10},
        })
    report = run.report()
    assert [item['round'] for item in report['round_activity']] == [1, 2, 3]
    assert report['round_activity'][-1]['engine_timings']['prompt_ms'] == 30
