"""Standing query receipts use actual SQLite memories/conflicts and compiler."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import sqlite3
import pytest
from src import memory_engine as memory, memory_conflicts
from src.context_engine import compiler, wiring, store
from src.context_engine.cache import WorkingSet
from src.context_engine.adapters.memory import MemoryEngineSource
from src.context_engine.contracts import ContextRequest, ContextExecution, ContextTask, ContextPolicy, ContextCandidate


@pytest.fixture
def live(tmp_path, monkeypatch):
    data = tmp_path/'memories'; data.mkdir()
    monkeypatch.setattr(memory, 'DATA_DIR', str(data))
    monkeypatch.setattr(memory, 'memory_conflict_detection_enabled', lambda: True)
    memory.set_vector_store(None)
    with memory._db(): pass
    with memory_conflicts._db(): pass
    request = ContextRequest(request_id='learned-query', execution=ContextExecution(owner='qa', workspace='workspace', project_id='different-project', session_id='qa-session'),
        task=ContextTask(query='', intent='chat'), policy=ContextPolicy(max_items_per_source=2))
    store.use_path(str(tmp_path/'ledger.sqlite3'))
    source = MemoryEngineSource()
    engine = compiler.ContextCompiler(sources=[source], cache=WorkingSet())
    real_compile = engine.compile
    async def compile_baseline(request, **kwargs):
        baseline = ContextCandidate(source_ref='fixture:baseline', source_type='memory', section='project_rules',
            body='Stable mandatory baseline', trust_class='human_explicit', authority='binding_decision')
        return await real_compile(request, mandatory=(baseline,), **kwargs)
    monkeypatch.setattr(engine, 'compile', compile_baseline)
    monkeypatch.setattr(compiler, 'compiler', lambda: engine)
    monkeypatch.setattr(wiring, 'enabled', lambda: True)
    monkeypatch.setattr(wiring, '_live_budget', lambda *a, **k: 6000)
    monkeypatch.setattr(wiring, '_remember_omitted', lambda *a: [])
    yield request, data, source
    memory.reset_vector_store()
    store.use_path(None)


def add(text='Always run original QA tests', **kwargs):
    return memory.add_item(text, owner=kwargs.pop('owner', 'qa'), project=kwargs.pop('project', 'workspace'),
        level='procedural', trust_class='human_explicit', **kwargs)


async def deliver(request, previous=None):
    return await wiring.deliver_round(request=request, messages=[], previous=previous)


async def test_same_revision_body_update_invalidates(live):
    request, _, _ = live
    row = add()
    first = await deliver(request)
    assert 'original QA' in str(first['message'])
    with sqlite3.connect(memory.db_path()) as conn:
        before = conn.execute('SELECT updated_at FROM items WHERE id=?', (row['id'],)).fetchone()[0]
        conn.execute('UPDATE items SET text=? WHERE id=?', ('Always run changed QA tests', row['id']))
        assert conn.execute('SELECT updated_at FROM items WHERE id=?', (row['id'],)).fetchone()[0] == before
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'changed QA' in str(second['message'])


async def test_absence_then_new_member_invalidates(live):
    request, _, _ = live
    first = await deliver(request)
    add('Always run new QA tests')
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'new QA' in str(second['message'])


async def test_unchanged_reuses_ledger_without_writer_or_vector(live, monkeypatch):
    request, _, _ = live
    add()
    first = await deliver(request)
    assert first['_standing_memory_reuse_receipts']
    def forbidden(*a, **kw): pytest.fail('validation invoked writer/vector initializer')
    for name in ('_db', '_open', '_connect', '_quarantine', 'vector_store', 'touch'):
        monkeypatch.setattr(memory, name, forbidden)
    monkeypatch.setattr(memory_conflicts, '_db', forbidden)
    second = await deliver(request, first)
    assert second['report']['reused'] and second['message'] is first['message']
    with sqlite3.connect(memory.db_path()) as conn:
        assert conn.execute('SELECT access_count FROM items').fetchone()[0] == 0
    with store.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM context_packets').fetchone()[0] == 1


async def test_conflict_changes_and_deleted_membership_invalidate(live):
    request, _, _ = live
    row = add()
    first = await deliver(request)
    with sqlite3.connect(memory.db_path()) as conn:
        conn.execute("INSERT INTO memory_conflicts(id,owner,new_id,old_id,status) VALUES(?,?,?,?,?)",
                     ('qa-conflict','qa','new',row['id'],'open'))
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'contradicted by a newer memory' in str(second['message'])
    with sqlite3.connect(memory.db_path()) as conn:
        conn.execute('DELETE FROM items WHERE id=?', (row['id'],))
    third = await deliver(request, second)
    assert not third['report'].get('reused') and 'original QA' not in str(third['message'])


async def test_owner_workspace_globals_and_session_filter_contract(live):
    request, _, _ = live
    add('Always global QA', owner='', project='')
    add('Always scoped QA', session_id='different-session')
    add('Always hidden owner', owner='other')
    add('Always hidden project', project='different-project')
    first = await deliver(request)
    assert 'global QA' in str(first['message']) and 'scoped QA' in str(first['message'])
    assert 'hidden owner' not in str(first['message']) and 'hidden project' not in str(first['message'])
    with sqlite3.connect(memory.db_path()) as conn:
        conn.execute("UPDATE items SET text='Always hidden changed' WHERE owner='other'")
    assert (await deliver(request, first))['report']['reused']


async def test_top_membership_change_invalidates(live):
    request, _, _ = live
    add('Always initial one'); add('Always initial two')
    first = await deliver(request)
    new = add('Always promoted new member')
    with sqlite3.connect(memory.db_path()) as conn:
        conn.execute('UPDATE items SET trust=1 WHERE id=?', (new['id'],))
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'promoted new member' in str(second['message'])


async def test_current_clock_rejects_expired_rule(live, monkeypatch):
    from src.context_engine.adapters import memory as adapter
    request, _, _ = live
    now = datetime.now(timezone.utc)
    add('Always temporary QA', valid_until=now+timedelta(days=1))
    first = await deliver(request)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None): return now+timedelta(days=2)
    monkeypatch.setattr(adapter, 'datetime', Later)
    second = await deliver(request, first)
    assert not second['report'].get('reused') and 'temporary QA' not in str(second['message'])


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'schema'])
async def test_validation_failure_never_repairs_store(live, kind):
    request, _, _ = live
    add()
    first = await deliver(request)
    path = Path(memory.db_path())
    if kind == 'missing':
        path.unlink()
    elif kind == 'corrupt':
        path.write_bytes(b'corrupt SQLite QA bytes')
    else:
        with sqlite3.connect(path) as conn: conn.execute('DROP TABLE memory_conflicts')
    before = path.read_bytes() if path.exists() else None
    assert not await wiring._standing_memory_reuse_is_current(request, first)
    assert (path.read_bytes() if path.exists() else None) == before
    assert not list(path.parent.glob('*.corrupt*'))


async def test_database_path_drift_refuses_reading_other_store(live, monkeypatch):
    request, data, _ = live
    add()
    first = await deliver(request)
    elsewhere = data/'never-created'
    monkeypatch.setattr(memory, 'DATA_DIR', str(elsewhere))
    monkeypatch.setattr(memory, 'context_snapshot', lambda *a, **k: pytest.fail('drift read different DB'))
    assert not await wiring._standing_memory_reuse_is_current(request, first)
    assert not elsewhere.exists()


async def test_worker_path_drift_is_rejected_before_sqlite_read(live, monkeypatch):
    request, data, _ = live
    add()
    first = await deliver(request)
    original_scope = MemoryEngineSource._scope
    elsewhere = data/'never-created-in-worker'
    def change_path(retrieval):
        memory.DATA_DIR = str(elsewhere)
        return original_scope(retrieval)
    monkeypatch.setattr(MemoryEngineSource, '_scope', staticmethod(change_path))
    monkeypatch.setattr(memory, 'context_snapshot', lambda *a, **k: pytest.fail('worker opened changed DB'))
    assert not await wiring._standing_memory_reuse_is_current(request, first)
    assert not elsewhere.exists()


async def test_hybrid_render_remains_unknown_and_no_validation_vector(live, monkeypatch):
    request, _, _ = live
    add()
    request = replace(request, task=replace(request.task, query='Always QA tests', intent='research'))
    first = await deliver(request)
    assert first['_standing_memory_reuse_receipts'] is None
    monkeypatch.setattr(memory, 'vector_store', lambda: pytest.fail('validation built vector store'))
    assert not await wiring._standing_memory_reuse_is_current(request, first)


async def test_initial_legacy_fallback_render_is_unknown(live, monkeypatch):
    request, _, _ = live
    add()
    monkeypatch.setattr(memory, 'context_snapshot', lambda *a, **k: (_ for _ in ()).throw(OSError('strict snapshot failed')))
    first = await deliver(request)
    assert 'original QA' in str(first['message'])
    assert first['_standing_memory_reuse_receipts'] is None


async def test_incognito_no_db_access(live, monkeypatch):
    request, _, _ = live
    off = replace(request, policy=replace(request.policy, allow_personal_memory=False))
    monkeypatch.setattr(memory, 'context_snapshot', lambda *a, **k: pytest.fail('off read DB'))
    monkeypatch.setattr(memory, '_db', lambda: pytest.fail('off initialized DB'))
    first = await deliver(off)
    assert first['_standing_memory_reuse_receipts'] == ()
    assert (await deliver(off, first))['report']['reused']


def test_unknown_manual_and_bounded_overflow():
    from src.context_engine import memory_engine_reuse as reuse
    from types import SimpleNamespace
    packet = SimpleNamespace(sections=[SimpleNamespace(kind='retrieved_memory',
        items=[SimpleNamespace(source_ref='mem:manual')])])
    with reuse.capture_queries() as state: reuse.record_query(None, (), False)
    assert reuse.captured(state, packet) is None
    with reuse.capture_queries() as state:
        for _ in range(40): reuse.record_query(None, (), True)
    assert len(state['receipts']) == reuse.MAX_QUERIES+1 and reuse.captured(state) is None
