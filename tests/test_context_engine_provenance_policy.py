"""Private memory must not be relabelled into an allowed experience channel."""
import asyncio
from dataclasses import replace

import pytest

from src.context_engine.adapters.provenance import ProvenanceSource, clear_cache
from src.context_engine.candidates import RetrievalRequest, make_candidate
from src.context_engine.contracts import ContextExecution, ContextPolicy, ContextRequest, ContextTask
from src.context_engine import planner, ranking


def request(*, memory=False, owner='alice', project='p1', workspace='/p1', refs=()):
    return ContextRequest(execution=ContextExecution(owner=owner, project_id=project, workspace=workspace),
                          policy=ContextPolicy(allow_personal_memory=memory),
                          task=ContextTask(intent='code_change', phase='verify', query='synthetic'),
                          explicit_refs=refs)


def retrieval(req):
    return RetrievalRequest(request=req, query='synthetic', sections=('past_experiences', 'decisions'), lanes=('graph',))


@pytest.fixture(autouse=True)
def clean_cache():
    clear_cache()
    yield
    clear_cache()


def mixed_graph(monkeypatch):
    from src import provenance_graph as graph
    calls = []
    nodes = [
        {'id': 'memory:private', 'kind': 'memory', 'label': 'synthetic PRIVATE memory', 'meta': {'owner': 'alice'}},
        {'id': 'chat:private', 'kind': 'chat', 'label': 'synthetic PRIVATE chat', 'meta': {'owner': 'alice'}},
        {'id': 'checkpoint:verified', 'kind': 'checkpoint', 'label': 'synthetic observed diff', 'detail': 'tests passed', 'meta': {'owner': 'alice', 'project_id': 'p1'}},
        {'id': 'checkpoint:foreign-owner', 'kind': 'checkpoint', 'label': 'synthetic foreign owner', 'meta': {'owner': 'bob', 'project_id': 'p1'}},
        {'id': 'checkpoint:foreign-project', 'kind': 'checkpoint', 'label': 'synthetic foreign project', 'meta': {'owner': 'alice', 'project_id': 'p2'}},
    ]
    def build(*args, **kwargs):
        calls.append(kwargs)
        # Deliberately mixed: the consumer must not trust a stale index or producer.
        return {'nodes': nodes, 'edges': [{'from': 'memory:private', 'to': 'checkpoint:verified', 'why': 'PRIVATE memory evidence'}], 'sources': {}, 'truncated': False}
    monkeypatch.setattr(graph, 'build', build)
    monkeypatch.setattr(graph, 'ranking_signal', lambda value: {})
    monkeypatch.setattr(graph, 'explain', lambda value, node_id: {'summary': '', 'steps': [{'why': edge['why']} for edge in value['edges']]})
    return calls


def test_no_memory_mixed_graph_search_preserves_project_proof(monkeypatch):
    calls = mixed_graph(monkeypatch)
    candidates = asyncio.run(ProvenanceSource().search(retrieval(request())))
    assert [row.source_ref for row in candidates] == ['prov:checkpoint:verified']
    assert all('PRIVATE' not in row.body for row in candidates)
    assert calls[0]['include_personal_memory'] is False


@pytest.mark.parametrize('ref', ['prov:memory:private', 'prov:chat:private'])
def test_private_exact_reference_does_not_even_build_graph(monkeypatch, ref):
    calls = mixed_graph(monkeypatch)
    req = request(refs=(ref,))
    assert asyncio.run(ProvenanceSource().fetch(ref, retrieval(req))) is None
    assert calls == []


def test_cache_is_separate_by_memory_policy_owner_and_project(monkeypatch):
    calls = mixed_graph(monkeypatch)
    source = ProvenanceSource()
    for req in [request(memory=True), request(), request(owner='bob'), request(project='p2'), request()]:
        asyncio.run(source.search(retrieval(req)))
    assert len(calls) == 4
    assert [call['include_personal_memory'] for call in calls] == [True, False, False, False]


@pytest.mark.parametrize('kind', ['memory', 'chat'])
def test_ranking_refuses_private_origin_even_mislabelled_as_experience(kind):
    row = make_candidate(source_type='experience', source_ref=f'prov:{kind}:synthetic', section='past_experiences',
                         body='synthetic PRIVATE source', owner='alice', project_id='p1')
    assert row is not None
    verdict = ranking.validate(row, request())
    assert not verdict.ok and verdict.reason == 'policy'
    assert ranking.validate(row, request(memory=True)).ok


def test_graph_builder_skips_memory_store_before_any_read(monkeypatch):
    from src import provenance_graph as graph
    memory_reads = []
    monkeypatch.setattr(graph, '_collect_memory', lambda *args: memory_reads.append(True) or ([], set(), set()))
    monkeypatch.setattr(graph, '_collect_checkpoints', lambda *args: None)
    monkeypatch.setattr(graph, '_collect_experts', lambda *args: None)
    graph.build('alice', include_personal_memory=False)
    assert memory_reads == []
    graph.build('alice')
    assert memory_reads == [True]


def test_foreign_proof_is_not_relabelled_as_current_scope(monkeypatch):
    mixed_graph(monkeypatch)
    source = ProvenanceSource()
    for ref in ['prov:checkpoint:foreign-owner', 'prov:checkpoint:foreign-project']:
        assert asyncio.run(source.fetch(ref, retrieval(request(memory=True)))) is None


def test_verified_experience_stays_available_under_no_memory():
    row = make_candidate(source_type='experience', source_ref='experience:verified', section='past_experiences',
                         body='synthetic project verification', owner='alice', project_id='p1',
                         trust_class='observed', authority='validated_experience')
    assert row is not None and ranking.validate(row, request()).ok
    assert planner._source_verdict('experiences', request(), ('past_experiences',))[0]
    assert not ranking.validate(replace(row, owner='bob'), request()).ok
    assert not ranking.validate(replace(row, project_id='p2'), request()).ok
