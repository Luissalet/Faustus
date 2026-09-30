"""Standing retrieval applies the same validity and secret gates as hybrid."""
from datetime import datetime, timedelta, timezone
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
    yield
    engine.reset_vector_store()


def retrieval(owner='qa', allowed=True):
    return RetrievalRequest(request=ContextRequest(execution=ContextExecution(owner=owner, workspace='workspace'),
        policy=ContextPolicy(allow_personal_memory=allowed)),
        query='', sections=('retrieved_memory',), lanes=('mandatory',))


def add(text, **kwargs):
    return engine.add_item(text, owner=kwargs.pop('owner', 'qa'), project='workspace',
        level='procedural', trust_class='human_explicit', **kwargs)


@pytest.mark.parametrize('kind', ['secret', 'expired', 'future'])
async def test_disallowed_memory_does_not_enter_standing_result(database, kind):
    now = datetime.now(timezone.utc)
    options = {'secret': {'sensitivity': 'secret'},
               'expired': {'valid_until': now-timedelta(days=1)},
               'future': {'valid_from': now+timedelta(days=1)}}[kind]
    add('QA forbidden '+kind, **options)
    add('QA currently valid', valid_from=now-timedelta(days=1), valid_until=now+timedelta(days=1))
    result = (await gather([MemoryEngineSource()], retrieval()))[0]
    assert not result.degraded
    assert [candidate.body for candidate in result.candidates] == ['QA currently valid']


async def test_blank_owner_selects_global_only(database):
    add('QA global standing', owner='')
    add('QA private standing')
    result = (await gather([MemoryEngineSource()], retrieval(owner='')))[0]
    assert [candidate.body for candidate in result.candidates] == ['QA global standing']


async def test_incognito_does_not_open_database(database, monkeypatch):
    monkeypatch.setattr(engine, '_db', lambda: pytest.fail('incognito opened learned memory'))
    result = (await gather([MemoryEngineSource()], retrieval(allowed=False)))[0]
    assert result.candidates == () and not result.degraded
