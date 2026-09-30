"""H22 route revisions travel with the resolved route, never a later DB lookup."""
import asyncio
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core import database as db
from src import agent_loop as a, endpoint_resolver as r, model_calibration as c
from tests.test_model_switch_calibration_scope import patch_session, temporary_store, NATIVE


def write(rev, model='new', ep='a', ok=True):
    return c.save_scoped_tested(vendor='ollama', model_id=model, endpoint_id=ep,
        endpoint_revision=rev, protocol=c.NATIVE_OLLAMA_PROTOCOL,
        tested={c.TEST_TOOL_CALLING: {'ok': ok}}, announced={'capabilities': {'tools': True}})


@pytest.mark.parametrize('ok', [True, False])
@pytest.mark.parametrize('revision', ['captured', 'other', ''])
def test_helper_uses_only_explicit_matching_revision(ok, revision):
    write('captured', ok=ok)
    result = a.recompute_capabilities_on_model_switch(previous_model='old', previous_endpoint_url=NATIVE,
        new_model='new', new_endpoint_url=NATIVE, new_endpoint_id='a', new_endpoint_revision=revision)
    assert result['capabilities']['tested'] == ({c.TEST_TOOL_CALLING: {'ok': ok}} if revision == 'captured' else {})
    assert set(result['capabilities']) == {'announced', 'tested', 'degraded', 'updated_at'}


@pytest.mark.parametrize('ok', [True, False])
def test_session_patch_captures_row_revision(patch_session, ok):
    call, session, factory = patch_session
    with factory() as connection:
        rev = connection.get(db.ModelEndpoint, 'a').connection_revision
    write(rev, ok=ok)
    result = call()
    assert result['capabilities']['tested'][c.TEST_TOOL_CALLING]['ok'] is ok
    with factory() as connection:
        row = connection.get(db.ModelEndpoint, 'a')
        row.base_url = 'http://127.0.0.1:11435/api/chat'
        connection.commit()
    assert call()['capabilities']['tested'] == {}


def test_resolver_returns_revision_from_same_endpoint_snapshot(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'endpoints.db'))
    db.ModelEndpoint.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(r, 'SessionLocal', factory)
    with factory() as session:
        ep = db.ModelEndpoint(id='a', name='fixture', base_url=NATIVE, api_key=None)
        session.add(ep)
        session.commit()
        revision = ep.connection_revision
    first = r._resolve_endpoint_by_id_with_descriptor('a', model='new')
    assert first[1]['connection_revision'] == revision
    assert first[0][0] == NATIVE
    with factory() as session:
        session.get(db.ModelEndpoint, 'a').base_url = 'http://127.0.0.1:11435/api/chat'
        session.commit()
    second = r._resolve_endpoint_by_id_with_descriptor('a', model='new')
    assert second[1]['connection_revision'] != revision
    assert first[1]['connection_revision'] == revision
    engine.dispose()


def test_runtime_fallback_uses_frozen_deduped_candidate_identity(monkeypatch, tmp_path):
    primary = ('https://selected.example/v1', 'selected-model', {})
    backup = ('https://backup.example/v1', 'backup-model', {})
    descriptors = [
        {'endpoint_id': 'selected', 'connection_revision': 'primary-A'},
        {'endpoint_id': 'duplicate-must-not-win', 'connection_revision': 'duplicate'},
        {'endpoint_id': 'backup', 'connection_revision': 'backup-A'},
    ]
    engine = create_engine('sqlite:///' + str(tmp_path / 'queued.db'))
    db.ModelEndpoint.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(db.ModelEndpoint(id='backup', name='queued', base_url=backup[0], api_key=None))
        session.commit()
        captured_revision = session.get(db.ModelEndpoint, 'backup').connection_revision
    descriptors[2]['connection_revision'] = captured_revision
    observed = []
    monkeypatch.setattr(a, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(a, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(a, 'estimate_tokens', lambda *args, **kwargs: 10)
    monkeypatch.setattr(a, 'blocked_tools_for_owner', lambda owner: set())
    def recompute(**kwargs):
        observed.append(kwargs)
        return {'lost': [], 'capabilities': {}}
    monkeypatch.setattr(a, 'recompute_capabilities_on_model_switch', recompute)
    monkeypatch.setattr(r, 'resolve_route_descriptor', lambda *args, **kwargs: pytest.fail('no lookup after await'))
    monkeypatch.setattr(r, 'resolve_route_descriptor_by_id', lambda *args, **kwargs: pytest.fail('no lookup after await'))
    async def stream(candidates, messages, **kwargs):
        assert candidates == [primary, backup]
        # Simulate queued configuration mutation after the initial snapshot.
        await asyncio.sleep(0)
        descriptors[0]['endpoint_id'] = 'wrong-current'
        descriptors[0]['connection_revision'] = 'primary-B'
        with factory() as session:
            row = session.get(db.ModelEndpoint, 'backup')
            row.base_url = 'https://changed.example/v1'
            session.commit()
            descriptors[2]['connection_revision'] = row.connection_revision
            assert row.connection_revision != captured_revision
        yield 'data: ' + json.dumps({'type': 'fallback', 'answered_by': backup[1], 'candidate_index': 1}) + '\n\n'
        yield 'data: {"delta": "answer"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(a, 'stream_llm_with_fallback', stream)
    async def collect():
        return [chunk async for chunk in a.stream_agent_loop(primary[0], primary[1],
            [{'role': 'user', 'content': 'Run a command after checking the route.'}], headers={}, max_rounds=1,
            relevant_tools={"bash"}, fallbacks=[primary, backup], route_descriptors=descriptors,
            _is_teacher_run=True)]
    asyncio.run(collect())
    assert len(observed) == 1
    assert observed[0]['previous_endpoint_id'] == 'selected'
    assert observed[0]['previous_endpoint_revision'] == 'primary-A'
    assert observed[0]['new_endpoint_id'] == 'backup'
    assert observed[0]['new_endpoint_revision'] == captured_revision
    engine.dispose()


def test_helper_previous_and_new_revision_control_loss():
    write('old-snapshot', model='old', ep='before')
    c.save_scoped_tested(vendor='ollama', model_id='new', endpoint_id='a',
        endpoint_revision='new-snapshot', protocol=c.NATIVE_OLLAMA_PROTOCOL,
        tested={}, announced={'capabilities': {'tools': False}})
    result = a.recompute_capabilities_on_model_switch(previous_model='old', previous_endpoint_url=NATIVE,
        previous_endpoint_id='before', previous_endpoint_revision='old-snapshot',
        new_model='new', new_endpoint_url=NATIVE, new_endpoint_id='a', new_endpoint_revision='new-snapshot')
    assert result['lost'] == ['native tool calling']
