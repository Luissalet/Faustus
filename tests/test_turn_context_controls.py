import pytest

from src.context_budget import parse_turn_input_budget


@pytest.mark.parametrize('raw', [True, False, -1, 0, 4095, 200001, 'nan', '1.5', {}, []])
def test_turn_budget_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_turn_input_budget(raw)


@pytest.mark.parametrize('raw, expected', [(None, None), ('', None), ('8192', 8192), (6000, 6000), (200000, 200000)])
def test_turn_budget_parses_without_changing_global_defaults(raw, expected):
    assert parse_turn_input_budget(raw) == expected


@pytest.mark.parametrize('live', [True, False])
def test_turn_controls_reach_prompt_trimmer_and_context_engine(monkeypatch, live):
    from src import agent_loop as al, context_compactor, model_context
    from src.context_engine import wiring
    from tests.test_agent_harness_loop import _patch_common, _collect, _scripted_stream
    _patch_common(monkeypatch)
    _scripted_stream(monkeypatch, [('A short answer.', 'stop')])
    monkeypatch.setattr(wiring, 'enabled', lambda: live)
    monkeypatch.setattr(wiring, 'shadow_enabled', lambda: True)
    monkeypatch.setattr(model_context, 'budget_context_for_model', lambda *a, **k: 128000)
    prompts, budgets, compiled = [], [], []

    def prompt(messages, *args, **kwargs):
        prompts.append(kwargs)
        return messages, []

    def trim(messages, budget, **kwargs):
        budgets.append(budget)
        return messages

    async def capture(**kwargs):
        compiled.append(kwargs)
        return None

    monkeypatch.setattr(al, '_build_system_prompt', prompt)
    monkeypatch.setattr(context_compactor, 'trim_for_context', trim)
    monkeypatch.setattr(wiring, 'deliver_round', capture)
    monkeypatch.setattr(wiring, 'shadow_round', capture)
    events = _collect(al.stream_agent_loop('http://127.0.0.1:11434/v1', 'test-model',
        [{'role': 'user', 'content': 'Explain how automatic context recall works.'}],
        max_rounds=1, relevant_tools={'todowrite'},
        harness_options={'no_skills': True, 'input_token_budget': 6000, 'project_id': 'qa'}))
    assert prompts and all(item['suppress_skills'] for item in prompts), events
    assert budgets and all(budget == 6000 for budget in budgets)
    assert compiled and all(item['context_length'] == 6000 for item in compiled)
    assert compiled[0]['request'].execution.project_id == 'qa'
