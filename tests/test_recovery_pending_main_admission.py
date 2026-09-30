"""Recovery admission includes observed main usage still absent from its ledger."""
import json

import pytest

from src import agent_loop as al, autonomy_budget as ab, llm_core as lc
from tests.test_autonomy_budget import _patch_common, _collect, _events
from tests.test_degenerate_output import _degenerate_error_chunk
from tests.test_recovery_budget_receipts import usage


@pytest.mark.parametrize('snapshots, tokens, spend', [
    ([{'input_tokens': 800}, {'output_tokens': 200}], 1000, 1000),
    ([{'input_tokens': 800, 'output_tokens': 200}] * 2, 1000, 1000),
    ([{'input_tokens': 800}, {'input_tokens': True, 'output_tokens': -1}], 800, 800),
    ([{'input_tokens': 800}, {'cost_usd': 0.001}], 800, 500),
    ([{'cost_usd': 0.001}, {'output_tokens': 200}], 200, 500),
    ([{'total_tokens': 1000}], 1000, 1000),
    ([{'total_tokens': 1000}, {'input_tokens': 800, 'output_tokens': 400}], 1200, 1200),
    ([{'input_tokens': 800, 'output_tokens': 200}, {'total_tokens': 500}], 1000, 1000),
    ([{'cost_usd': 0.001}], 0, 500),
    ([{}, {'input_tokens': '1000'}], 0, 0),
])
def test_pending_view_observed_only_preserves_snapshots_without_mutating_ledger(snapshots, tokens, spend):
    pending = {}
    ledger = ab.Ledger(tokens=30, remote_spend=50, active_seconds=99)
    for snapshot in snapshots:
        al._pending_main_usage_update(pending, 1, snapshot, endpoint_cost_tracked=True)
    view = al._pending_main_admission_view(ledger, pending)
    assert view.tokens == 30 + tokens and view.remote_spend == pytest.approx(50 + spend)
    assert view.active_seconds == ledger.active_seconds == 99
    assert ledger.tokens == 30 and ledger.remote_spend == 50
    if any('cost_usd' in snapshot for snapshot in snapshots):
        assert pending[1]['usage']['cost_state'] == 'known'


@pytest.mark.parametrize('tokens_credit,spend_credit', [(230, 230), (1000, 230), (230, 1000), (1000, 1000)])
def test_settlement_credits_only_the_actual_addition_by_dimension(tokens_credit, spend_credit):
    pending = {}
    al._pending_main_usage_update(pending, 1, {'total_tokens': 1000}, endpoint_cost_tracked=True)
    ledger = ab.Ledger(tokens=tokens_credit, remote_spend=spend_credit)
    al._pending_main_usage_settle(pending, 1, tokens=tokens_credit, remote_units=spend_credit)
    view = al._pending_main_admission_view(ledger, pending)
    assert view.tokens == 1000 and view.remote_spend == 1000
    assert bool(pending) == (tokens_credit < 1000 or spend_credit < 1000)
    assert ledger.tokens == tokens_credit and ledger.remote_spend == spend_credit


def test_cost_only_and_untracked_remote_policy_are_not_fabricated():
    pending = {}
    al._pending_main_usage_update(pending, 1, {'cost_usd': 0.001}, endpoint_cost_tracked=False)
    view = al._pending_main_admission_view(ab.Ledger(), pending)
    assert view.tokens == view.remote_spend == 0
    al._pending_main_usage_update(pending, 2, {'cost_usd': 0.001}, endpoint_cost_tracked=True)
    al._pending_main_usage_settle(pending, 2, tokens=0, remote_units=500)
    assert al._pending_main_admission_view(ab.Ledger(remote_spend=500), pending).remote_spend == pytest.approx(500)


@pytest.mark.parametrize('trigger', ['degenerate', 'ctx_ack'])
@pytest.mark.parametrize('amount, denied', [(1000, True), (100, False)])
def test_real_loop_checks_pending_main_before_recovery(monkeypatch, trigger, amount, denied):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_tokens=500))
    monkeypatch.setattr('src.endpoint_resolver.resolve_endpoint',
        lambda *a, **k: ('https://utility.invalid/v1', 'utility', {}))
    async def auxiliary(*a, **k):
        return 'Synthetic auxiliary answer.'
    monkeypatch.setattr(lc, 'llm_call_async', auxiliary)
    main_count = 3 if trigger == 'ctx_ack' else 2
    calls, effects = [], []
    async def execute(*a, **k):
        effects.append(True)
        pytest.fail('No effect may run after recovery admission is denied')
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    async def stream(candidates, *a, **k):
        calls.append(ledger.tokens)
        if len(calls) <= main_count:
            yield usage({'input_tokens': amount, 'output_tokens': 0})
            if trigger == 'ctx_ack':
                yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
                yield 'data: [DONE]\n\n'
            else:
                yield _degenerate_error_chunk().replace('started repeating tokens', 'reasoning loop')
        else:
            assert not denied, 'Already observed main usage must reject the auxiliary'
            yield usage({'input_tokens': 40, 'output_tokens': 10})
            yield 'data: {"delta":"Recovered synthetic answer."}\n\n'
            yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = _events(_collect(al.stream_agent_loop('https://fixture.invalid/v1', 'm',
        [{'role': 'user', 'content': 'Answer the fixture'}], max_rounds=4,
        relevant_tools={'read_file'}, owner='admin')))
    assert len(calls) == (1 if denied else main_count + 1) and effects == []
    assert any(e.get('type') == 'budget_exhausted' for e in events) == denied
    assert any(e.get('type') == 'response_replace' and 'Recovered synthetic answer.' in e.get('text', '')
               for e in events) != denied
    if denied:
        # Admission remains read-only; finalization separately settles the
        # observed contribution, without admitting another inference.
        assert ledger.tokens == 1000


def test_settled_previous_real_round_does_not_count_twice_at_recovery(monkeypatch):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_tokens=65))
    async def auxiliary(*a, **k):
        return 'Synthetic auxiliary answer.'
    monkeypatch.setattr(lc, 'llm_call_async', auxiliary)
    calls = []
    async def stream(candidates, *a, **k):
        calls.append(ledger.tokens)
        if len(calls) == 1:
            yield usage({'input_tokens': 20, 'output_tokens': 10})
            yield 'data: '+json.dumps({'delta': '```read_file\n{"path":"synthetic.txt"}\n```'})+'\n\n'
        elif len(calls) <= 3:
            yield usage({'input_tokens': 5, 'output_tokens': 5})
            yield _degenerate_error_chunk().replace('started repeating tokens', 'reasoning loop')
        else:
            yield usage({'input_tokens': 5, 'output_tokens': 5})
            yield 'data: {"delta":"Recovered synthetic answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = _events(_collect(al.stream_agent_loop('https://fixture.invalid/v1', 'm',
        [{'role': 'user', 'content': 'Read then answer the fixture'}], max_rounds=4,
        relevant_tools={'read_file'}, owner='admin')))
    assert calls == [0, 30, 30, 30]
    assert ledger.tokens == 60  # 30 settled main + 20 remaining main + 10 recovery.
    assert any(e.get('type') == 'response_replace' for e in events)
    assert not any(e.get('type') == 'budget_exhausted' for e in events)
