import pytest


@pytest.mark.parametrize('skip', [False, True])
@pytest.mark.parametrize('live', [False, True])
def test_recall_choice_reaches_live_and_shadow_context(monkeypatch, skip, live):
    from src import agent_loop as al
    from src.context_engine import wiring
    from tests.test_agent_harness_loop import _patch_common, _collect, _scripted_stream
    _patch_common(monkeypatch)
    _scripted_stream(monkeypatch, [('A short answer.', 'stop')])
    monkeypatch.setattr(wiring, 'enabled', lambda: live)
    monkeypatch.setattr(wiring, 'shadow_enabled', lambda: True)
    seen = []
    async def capture(**kwargs):
        seen.append(kwargs['request'])
        return None
    monkeypatch.setattr(wiring, 'deliver_round', capture)
    monkeypatch.setattr(wiring, 'shadow_round', capture)
    _collect(al.stream_agent_loop('http://127.0.0.1:11434/v1', 'qwen3.5:9b',
        [{'role': 'user', 'content': 'Explain how automatic memory recall works.'}],
        max_rounds=1, relevant_tools={'todowrite'},
        harness_options={'no_memory': skip, 'project_id': 'project-qa'}))
    assert len(seen) == 1
    for request in seen:
        assert request.policy.allow_personal_memory is (not skip)
        assert request.policy.allow_project_sources is True
        assert request.execution.project_id == 'project-qa'


def test_skip_recall_also_blocks_legacy_learned_memory(monkeypatch):
    from src import agent_loop as al, memory_engine
    from src.context_engine import wiring
    from tests.test_agent_harness_loop import _patch_common
    _patch_common(monkeypatch)
    monkeypatch.setattr(wiring, 'enabled', lambda: False)
    monkeypatch.setattr(memory_engine, 'injection_enabled', lambda: True)
    monkeypatch.setattr(memory_engine, 'injection_budget', lambda: 1000)
    calls = []
    monkeypatch.setattr(memory_engine, 'pack_detail', lambda *a, **k: calls.append(a) or {})
    for skip in [True, False]:
        al._build_system_prompt([{'role': 'user', 'content': 'Recall a preference.'}],
            'test', None, None, set(), relevant_tools={'todowrite'},
            suppress_skills=True, suppress_personal_memory=skip)
        assert len(calls) == (0 if skip else 1)
