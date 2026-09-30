"""The lexical-only adapter policy must prevent semantic store access."""
import pytest
from src import memory_engine as engine
from src.context_engine.adapters.memory import MemoryEngineSource
from src.context_engine.candidates import RetrievalRequest, gather
from src.context_engine.contracts import ContextRequest, ContextExecution, ContextPolicy


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(engine, 'memory_conflict_detection_enabled', lambda: False)
    engine.set_vector_store(None)
    row = engine.add_item('Always run QA tests', owner='qa', project='workspace',
        level='procedural', trust_class='human_explicit')
    yield row
    engine.reset_vector_store()


def retrieval(semantic=False, allowed=True):
    return RetrievalRequest(request=ContextRequest(execution=ContextExecution(owner='qa', workspace='workspace'),
        policy=ContextPolicy(allow_personal_memory=allowed)), query='Always run QA tests',
        sections=('retrieved_memory',), lanes=('lexical', 'semantic') if semantic else ('lexical',))


class Vectors:
    healthy = True
    def __init__(self, memory_id, failed=False):
        self.memory_id, self.failed = memory_id, failed
        self.calls = []
    def search(self, query, k=8):
        self.calls.append((query,k))
        if self.failed: raise OSError('QA vector failure')
        return [{'memory_id':self.memory_id,'score':.8}]


async def test_lexical_only_actual_adapter_skips_recording_vector_store(database):
    vectors = Vectors(database['id'])
    engine.set_vector_store(vectors)
    result = (await gather([MemoryEngineSource()], retrieval()))[0]
    assert vectors.calls == []
    assert len(result.candidates) == 1 and not result.degraded
    candidate = result.candidates[0]
    assert candidate.scores['semantic'] == 0 and 'semantic' not in candidate.lanes
    assert candidate.scores['relevance'] == round(.90*candidate.scores['lexical'], 6)


async def test_policy_disabled_never_resolves_semantics_or_lazy_store(database, monkeypatch):
    def forbidden(*a, **k): pytest.fail('disabled semantic lane was consulted')
    monkeypatch.setattr(engine, '_semantic_scores', forbidden)
    monkeypatch.setattr(engine, 'vector_store', forbidden)
    result = (await gather([MemoryEngineSource()], retrieval()))[0]
    assert result.candidates and not result.degraded
    direct = engine.search('Always run QA tests', owner='qa', project='workspace',
                           semantic_enabled=False, touch_hits=False)
    assert direct[0]['semantic'] == 0 and direct[0]['degraded'] is False


async def test_semantic_allowed_still_queries_and_reports_lane(database):
    vectors = Vectors(database['id'])
    engine.set_vector_store(vectors)
    result = (await gather([MemoryEngineSource()], retrieval(semantic=True)))[0]
    assert len(vectors.calls) == 1 and result.candidates
    assert result.candidates[0].scores['semantic'] == .8
    assert 'semantic' in result.candidates[0].lanes and not result.degraded


@pytest.mark.parametrize('failed', [False, True])
def test_default_semantic_contract_preserves_legacy_degradation(database, failed):
    vectors = Vectors(database['id'], failed=True) if failed else None
    engine.set_vector_store(vectors)
    rows = engine.search('Always run QA tests', owner='qa', project='workspace', touch_hits=False)
    assert rows[0]['degraded'] is True and rows[0]['semantic'] == 0
    assert rows[0]['relevance'] == round(.90*rows[0]['lexical'], 6)
    if failed: assert len(vectors.calls) == 1


def test_default_enabled_scores_match_explicit_enabled(database):
    vectors = Vectors(database['id'])
    engine.set_vector_store(vectors)
    default = engine.search('Always run QA tests', owner='qa', project='workspace', touch_hits=False)
    explicit = engine.search('Always run QA tests', owner='qa', project='workspace', touch_hits=False,
                             semantic_enabled=True)
    assert default == explicit
    assert default[0]['relevance'] == round(.45*default[0]['lexical']+.45*.8, 6)


async def test_incognito_never_reads_db_or_semantic_store(database, monkeypatch):
    def forbidden(*a, **k): pytest.fail('incognito read learned store')
    monkeypatch.setattr(engine, '_db', forbidden)
    monkeypatch.setattr(engine, 'vector_store', forbidden)
    result = (await gather([MemoryEngineSource()], retrieval(semantic=True, allowed=False)))[0]
    assert result.candidates == () and not result.degraded
