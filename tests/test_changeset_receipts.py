from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src import changesets, changeset_store as history, dispatch
from src.state_mirror.adapters.base import Scope
from src.state_mirror.adapters import workspace as mirror
from routes import changesets_routes, dispatch_routes


def report(**kwargs):
    return changesets.build(intent='implement', owner='alice', project_id='project-a',
                            workspace='D:/synthetic-receipt-workspace',
                            changes={'source': 'checkpoint', 'modified': ['a.py'], 'checkpoint': 'a' * 40},
                            checkpoint='a' * 40,
                            verification={'mode': 'tests', 'ran': True, 'ok': True},
                            **kwargs)


def save(store, cs, **kwargs):
    proof = {**changesets.judge(cs), 'at': 1_700_000_000}
    return store.save(cs, proof, source='turn', source_id='session',
                      completed=kwargs.get('completed', True))


def test_receipt_survives_reopen_is_immutable_and_idempotent(tmp_path):
    path = tmp_path / 'history.db'
    cs = report()
    original = save(history.Store(path), cs)
    assert original['verified'] is True
    assert history.Store(path).get(cs.id, owner='alice') == original
    assert save(history.Store(path), cs) == original
    with pytest.raises(history.ReceiptError, match='different evidence'):
        save(history.Store(path), replace(cs, title='rewritten'))
    assert history.Store(path).get(cs.id, owner='alice') == original


@pytest.mark.parametrize('reader', ['bob', '', None])
def test_receipts_never_treat_missing_owner_as_all_owners(reader):
    cs = report()
    save(history.Store(), cs)
    assert history.Store().get(cs.id, owner=reader) is None
    assert history.Store().list(owner=reader) == []


def test_scope_filters_pagination_and_newest_verified():
    store = history.Store()
    cs = report(run_id='run-1')
    save(store, cs)
    second = report(run_id='run-2')
    save(store, second, completed=False)
    save(store, replace(report(), project_id='project-b'))
    rows = store.list(owner='alice', project_id='project-a', limit=1)
    assert rows[0]['id'] == second.id
    assert store.list(owner='alice', project_id='project-a', before=rows[0]['cursor'])[0]['id'] == cs.id
    assert store.list(owner='alice', project_id='project-a', verified_only=True)[0]['id'] == cs.id
    assert store.list(owner='alice', run_id='run-1')[0]['id'] == cs.id
    assert store.list(owner='alice', workspace='D:/other') == []


def test_absent_history_is_read_only(tmp_path):
    path = tmp_path / 'absent' / 'receipt.db'
    store = history.Store(path)
    assert store.get('missing', owner='alice') is None
    assert store.list(owner='alice') == []
    assert not path.parent.exists()


@pytest.mark.parametrize('corruption', ['payload', 'owner', 'version'])
def test_corrupt_or_future_history_is_refused_without_overwrite(corruption):
    store, cs = history.Store(), report()
    save(store, cs)
    with sqlite3.connect(store.path) as db:
        if corruption == 'payload':
            db.execute("UPDATE receipts SET payload='{}'")
        elif corruption == 'owner':
            db.execute("UPDATE receipts SET owner='bob'")
        else:
            db.execute('PRAGMA user_version=99')
    with pytest.raises(history.ReceiptError):
        store.get(cs.id, owner='bob' if corruption == 'owner' else 'alice')
    with pytest.raises(history.ReceiptError):
        save(store, cs)


@pytest.mark.parametrize('kind', ['inconclusive', 'pre_existing_only', 'failures', 'cancelled', 'unmeasured', 'partial'])
def test_passing_flag_alone_cannot_promote_uncertain_evidence(kind):
    cs = report()
    completed = kind != 'cancelled'
    if kind in {'inconclusive', 'pre_existing_only'}:
        cs = replace(cs, verification=replace(cs.verification, **{kind: True}))
    if kind == 'failures':
        cs = replace(cs, verification=replace(cs.verification, failures=('failed check',)))
    if kind == 'unmeasured':
        cs = replace(cs, files=replace(cs.files, source='none'))
    proof = changesets.judge(cs)
    if kind == 'partial':
        proof['verdict'] = 'partial'
    saved = history.Store().save(cs, proof, source='turn', source_id='s', completed=completed)
    assert saved['verified'] is False
    assert history.Store().list(owner='alice', verified_only=True) == []


def test_concurrent_writers_and_duplicate_retries(tmp_path):
    path = tmp_path / 'history.db'
    reports = [report() for _ in range(32)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda cs: save(history.Store(path), cs), reports * 2))
    assert len(results) == 64
    assert len(history.Store(path).list(owner='alice', limit=100)) == 32


def test_turn_hook_incognito_and_io_failure_never_break_response(monkeypatch):
    cs = report()
    result = history.record_turn(cs, changesets.judge(cs), session_id='s', completed=True, incognito=True)
    assert result == {'stored': False, 'storage_reason': 'incognito'}
    assert history.Store().list(owner='alice') == []
    def fail(*args, **kwargs):
        raise OSError('private path that should not appear in response')
    monkeypatch.setattr(history.Store, 'save', fail)
    assert history.record_turn(cs, changesets.judge(cs), session_id='s', completed=True) == {
        'stored': False, 'storage_reason': 'unavailable'}


def test_turn_hook_returns_dereferenceable_id():
    cs = report()
    result = history.record_turn(cs, changesets.judge(cs), session_id='s', completed=True)
    assert result['stored'] is True and result['receipt_url'].endswith(cs.id)
    assert history.Store().get(cs.id, owner='alice')['source_id'] == 's'


def test_dispatch_envelope_preserves_real_evidence_and_proof(monkeypatch):
    cs = report()
    job = dispatch.DispatchJob('alice', {}, cs.workspace, '', '', None, 'A task')
    job.status = 'done'
    job.finished = time.time()
    job.changes = cs.files.to_dict()
    job.changes['checkpoint'] = cs.checkpoint
    job.verification = cs.verification.to_dict()
    job.proof = {**changesets.judge(cs), 'verdict': 'partial', 'worker_note': 'unguarded native client'}
    job.result = {'subagents': [{'name': 'worker', 'status': 'done', 'mutations': ['unseen.py']}]}
    history.record_dispatch(job)
    saved = history.Store().get(f'chg_dispatch_{job.id}', owner='alice')
    assert saved is not None
    assert saved['changeset']['files']['modified'] == ['a.py']
    assert saved['changeset']['claims'][0]['path'] == 'unseen.py'
    assert saved['proof']['worker_note'] == 'unguarded native client'
    assert saved['verified'] is False
    history.record_dispatch(job)
    assert len(history.Store().list(owner='alice')) == 1


def test_turn_conversion_preserves_inconclusive():
    cs = changesets.from_turn({'mutations': ['a.py'], 'checkpoint': 'a' * 40,
                              'tests': {'ran': True, 'ok': True, 'inconclusive': True}})
    assert cs.verification.inconclusive is True
    assert save(history.Store(), cs)['verified'] is False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(changesets_routes, 'require_admin', lambda request: None)
    monkeypatch.setattr(dispatch_routes, '_is_admin', lambda owner: True)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, call_next):
        request.state.current_user = request.headers.get('x-fixture-user', 'alice')
        if request.headers.get('x-fixture-token'):
            request.state.api_token = True
            request.state.api_token_owner = request.headers.get('x-fixture-owner')
            request.state.api_token_scopes = request.headers.get('x-fixture-scope', '').split()
        return await call_next(request)
    app.include_router(changesets_routes.setup_changesets_routes())
    with TestClient(app) as client:
        yield client


def test_http_receipts_are_owner_scoped_and_preview_never_writes(client):
    cs = report()
    save(history.Store(), cs)
    url = f'/api/changesets/receipts/{cs.id}'
    assert client.get(url).json()['changeset']['id'] == cs.id
    assert client.get(url, headers={'x-fixture-user': 'bob'}).status_code == 404
    assert client.get('/api/changesets/receipts', headers={'x-fixture-user': 'bob'}).json()['receipts'] == []
    assert client.post('/api/changesets/build', json={'intent': 'explore', 'owner': 'bob'}).status_code == 200
    assert len(history.Store().list(owner='alice')) == 1
    assert history.Store().list(owner='bob') == []
    assert client.post('/api/changesets/receipts', json=cs.to_dict()).status_code == 405


@pytest.mark.parametrize('headers,status', [
    ({'x-fixture-token': '1', 'x-fixture-owner': 'alice', 'x-fixture-scope': 'agents:dispatch'}, 200),
    ({'x-fixture-token': '1', 'x-fixture-owner': 'bob', 'x-fixture-scope': 'agents:dispatch'}, 404),
    ({'x-fixture-token': '1', 'x-fixture-owner': 'alice'}, 403),
    ({'x-fixture-token': '1', 'x-fixture-scope': 'agents:dispatch'}, 403),
])
def test_receipt_tokens_need_scope_and_exact_owner(client, headers, status):
    cs = report()
    save(history.Store(), cs)
    assert client.get(f'/api/changesets/receipts/{cs.id}', headers=headers).status_code == status


def test_http_corruption_is_explicit_unavailability_not_empty_history(client):
    store = history.Store()
    save(store, report())
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE receipts SET payload='{}'")
    response = client.get('/api/changesets/receipts')
    assert response.status_code == 503
    assert str(store.path) not in response.text


def test_http_dispatch_report_uses_real_envelope_and_preserves_worker_uncertainty(client, monkeypatch):
    cs = report()
    job = dispatch.DispatchJob('alice', {}, cs.workspace, '', '', None, 'Delegated fix')
    job.changes = cs.files.to_dict()
    job.verification = cs.verification.to_dict()
    job.status = 'partial'
    job.proof = {**changesets.judge(cs), 'verdict': 'partial', 'worker_note': 'worker cancelled'}
    monkeypatch.setattr(dispatch, 'get', lambda job_id: job)
    result = client.get('/api/changesets/from-dispatch/job').json()
    assert result['changeset']['files']['modified'] == ['a.py']
    assert result['changeset']['owner'] == 'alice'
    assert result['proof']['worker_note'] == 'worker cancelled'
    assert result['proof']['verdict'] == 'partial'


def test_real_dispatch_finalization_and_turn_stream_are_wired_without_blocking_io():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    loop = (root / 'src/agent_loop.py').read_text(encoding='utf-8')
    tail = loop.split('# --- Harness summary:', 1)[1].split('# --- Final metrics', 1)[0]
    assert 'await asyncio.to_thread(\n                    _record_changeset' in tail
    assert 'incognito=bool(_hopts.get("incognito"))' in tail
    code = (root / 'src/dispatch.py').read_text(encoding='utf-8')
    finalization = code.split('job.finished = time.time()', 1)[1].split('async def _verify', 1)[0]
    assert finalization.index('job._persist()') < finalization.index('await asyncio.to_thread(record_dispatch, job)')


def test_final_metrics_keep_receipt_and_round_count_for_history_reload():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'src/agent_loop.py').read_text(encoding='utf-8'))
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and isinstance(node.value, ast.DictComp)
                      and any(ast.unparse(target) == "metrics['harness']" for target in node.targets))
    summary = {'changeset': {'id': 'chg_kept', 'stored': True, 'evidence_verified': False},
               'round_count': 3, 'mutations': ['a.py']}
    actual = eval(compile(ast.Expression(assignment.value), '<actual harness projection>', 'eval'), {'_hsum': summary})
    assert actual['changeset'] == summary['changeset']
    assert actual['round_count'] == 3


def test_oversized_or_nonfinite_receipts_are_refused_before_database_write():
    cs = report()
    for extra in ({'detail': 'a' * 2_000_001}, {'score': float('nan')}):
        with pytest.raises(history.ReceiptError):
            history.Store().save(cs, {**changesets.judge(cs), **extra},
                                 source='turn', source_id='s', completed=True)
    assert history.Store().list(owner='alice') == []


@pytest.mark.parametrize('query', ['limit=0', 'limit=201', 'before=-1', 'limit=bad'])
def test_http_pagination_is_bounded(client, query):
    assert client.get('/api/changesets/receipts?' + query).status_code in {400, 422}


def test_mirror_receipts_do_not_leak_through_workspace_cache_or_simulations(monkeypatch):
    cs = report()
    save(history.Store(), cs)
    monkeypatch.setattr(mirror, 'read', lambda ws: ('2026-09-07T12:00:00Z', {'available': True}))
    adapter = mirror.WorkspaceAdapter()
    own = Scope(owner='alice', project_id='project-a', workspace=cs.workspace)
    def receipt_values(scope):
        return [row.state['last_verified_changeset'] for row in adapter.observe(scope)
                if 'last_verified_changeset' in row.state]
    assert receipt_values(own) == [cs.id]
    assert receipt_values(replace(own, owner='bob')) == []
    assert receipt_values(replace(own, project_id='other')) == []
    assert receipt_values(replace(own, namespace='simulation:qa')) == []
