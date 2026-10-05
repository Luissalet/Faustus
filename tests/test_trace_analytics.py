import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from src import trace_analytics as a


@pytest.fixture
def records(monkeypatch):
    raw = [{'seq': 1, 'run_id': 'run', 'ts': 1000.0, 'model': 'local', 'duration_ms': 250,
            'request': {'messages': ['PRIVATE PROMPT']}, 'response_text': 'PRIVATE ANSWER',
            'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'usage_source': 'reported_engine'}},
           {'seq': 2, 'run_id': 'run', 'ts': 1001.0, 'model': 'local', 'duration_ms': 100,
            'error': 'PRIVATE ERROR', 'usage': {'input_tokens': 10, 'output_tokens': 2}}]
    monkeypatch.setattr(a.llm_trace, '_iter_records', lambda _: iter(raw))
    return raw, a.read('session')[0]


def test_summary_avoids_content_and_token_double_counting(records):
    _, rows = records
    summary = a.summarize(rows)
    assert summary['tokens'] == {'input_tokens': 30, 'output_tokens': 7}
    assert summary['duration_ms'] == {'observed': 2, 'p50': 100, 'p95': 250}
    assert summary['error_rate'] == .5
    assert 'PRIVATE' not in json.dumps(summary)


def test_exports_real_completion_time_and_otlp_scalar_types(records):
    _, rows = records
    chrome = a.export(rows, 'chrome')
    event = chrome['traceEvents'][0]
    assert event['ts'] == 999750000 and event['dur'] == 250000
    otlp = a.export(rows, 'otlp')
    spans = otlp['resourceSpans'][0]['scopeSpans'][0]['spans']
    assert int(spans[0]['endTimeUnixNano']) - int(spans[0]['startTimeUnixNano']) == 250000000
    assert len(spans[0]['traceId']) == 32 and len(spans[0]['spanId']) == 16
    assert spans[0]['traceId'] == spans[1]['traceId'] and spans[0]['spanId'] != spans[1]['spanId']
    assert spans[1]['status']['code'] == 2 and spans[0]['kind'] == 3
    assert all(isinstance(attr['value'].get('intValue', ''), str) for attr in spans[0]['attributes'])
    assert 'PRIVATE' not in json.dumps(otlp) + json.dumps(chrome)


def test_missing_metrics_and_truncated_logs_never_pass_budget(records, monkeypatch):
    raw, rows = records
    budgets = a.TraceBudgets(max_calls=3, max_error_rate=.1, max_p95_ms=500)
    out = a.evaluate(rows, budgets)
    assert [c['status'] for c in out['checks']] == ['pass', 'fail', 'pass']
    raw[1]['duration_ms'] = float('nan')
    raw[1]['usage'] = {}
    rows, _ = a.read('session')
    out = a.evaluate(rows, a.TraceBudgets(max_p95_ms=500, max_input_tokens=100))
    assert not out['passed'] and all(c['status'] == 'insufficient_data' for c in out['checks'])
    assert not a.evaluate(rows, a.TraceBudgets(max_calls=10), truncated=True)['passed']
    assert not a.evaluate([], a.TraceBudgets(max_calls=10))['passed']
    monkeypatch.setattr(a, 'MAX_RECORDS', 1)
    assert a.read('session')[1]


def test_new_routes_keep_session_ownership_and_static_order(records, monkeypatch):
    import routes.llm_trace_routes as routes
    checks = []
    monkeypatch.setattr(routes, 'require_user', lambda _: checks.append('auth'))
    def owner(_, session):
        checks.append(session)
        if session == 'someone-else': raise HTTPException(403, 'wrong owner')
    monkeypatch.setattr(routes, '_verify_session_owner', owner)
    app = FastAPI()
    app.include_router(routes.setup_llm_trace_routes())
    with TestClient(app) as client:
        assert client.get('/api/llm-traces/session/analytics').status_code == 200
        result = client.get('/api/llm-traces/session/export?format=otlp')
        assert result.status_code == 200 and 'attachment' in result.headers['content-disposition']
        assert client.get('/api/llm-traces/someone-else/export').status_code == 403
        assert client.post('/api/llm-traces/session/evaluate', json={'max_calls': 3}).json()['passed']
        assert client.get('/api/llm-traces/session/export?format=bad').status_code == 422
    assert checks[:2] == ['auth', 'session']
