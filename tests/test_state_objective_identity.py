"""Objectives are numbered inside a project, not globally for an owner."""
from dataclasses import replace

import pytest

from src.state_mirror.adapters.base import Scope
from src.state_mirror.adapters.objectives import ObjectivesAdapter
from src.state_mirror.persistence import StateStore
from src.state_mirror.ingest import ingest


@pytest.fixture
def adapter(monkeypatch):
    adapter = ObjectivesAdapter()
    monkeypatch.setattr(adapter, '_payload', lambda scope: {
        'objectives': [
            {'id': 'OBJ-1', 'title': scope.project_id or scope.workspace,
             'status': 'done' if scope.project_id == 'project-b' else 'open',
             'priority': 3, 'deps': []},
            {'id': 'OBJ-2', 'title': 'Dependent', 'status': 'open',
             'priority': 3, 'deps': ['OBJ-1']},
        ], 'scores': {}, 'log': [],
    })
    return adapter


@pytest.mark.parametrize('left,right', [
    (Scope(owner='alice', project_id='project-a'), Scope(owner='alice', project_id='project-b')),
    (Scope(owner='alice', workspace='D:/client-a/repo'), Scope(owner='alice', workspace='D:/client-b/repo')),
])
def test_same_objective_number_in_two_scopes_never_collides(adapter, left, right):
    a = {row.id for row in adapter.discover(left)}
    b = {row.id for row in adapter.discover(right)}
    assert a and b and a.isdisjoint(b)
    assert {row.entity_id for row in adapter.observe(left)} == a
    assert {row.entity_id for row in adapter.observe(right)} == b


def test_declared_and_blocking_edges_stay_in_their_project(adapter):
    scopes = [Scope(owner='alice', project_id='project-a'), Scope(owner='alice', project_id='project-b')]
    ids = [{row.id for row in adapter.discover(scope)} for scope in scopes]
    for index, scope in enumerate(scopes):
        edges = adapter.relations(scope)
        assert edges
        for edge in edges:
            assert edge.from_id in ids[index] and edge.to_id in ids[index]
            assert edge.from_id not in ids[1-index] and edge.to_id not in ids[1-index]


def test_scope_identity_is_stable_across_workspace_rebinding(adapter):
    original = Scope(owner='alice', project_id='project-a', workspace='D:/first')
    assert [r.id for r in adapter.discover(original)] == [
        r.id for r in adapter.discover(replace(original, workspace='D:/second'))]


def test_owner_and_simulation_world_remain_isolated(adapter):
    original = Scope(owner='alice', project_id='project-a')
    own = {r.id for r in adapter.discover(original)}
    assert own.isdisjoint({r.id for r in adapter.discover(replace(original, owner='bob'))})
    assert own.isdisjoint({r.id for r in adapter.discover(replace(original, namespace='simulation:trial'))})


def test_two_projects_keep_distinct_materialized_states_after_reopen(adapter, tmp_path):
    path = str(tmp_path / 'state.sqlite3')
    store = StateStore(path=path)
    scopes = [Scope(owner='alice', project_id='project-a'), Scope(owner='alice', project_id='project-b')]
    try:
        for scope in scopes:
            for row in adapter.discover(scope):
                store.upsert_entity(row)
            ingest(adapter.observe(scope), store=store, publisher=lambda *args, **kwargs: None)
        a = store.list_states(owner='alice', project_id='project-a')
        b = store.list_states(owner='alice', project_id='project-b')
        assert len(a) == len(b) == 2
        assert {s.entity_id for s in a}.isdisjoint({s.entity_id for s in b})
    finally:
        store.close()
    reopened = StateStore(path=path)
    try:
        assert len(reopened.list_states(owner='alice', project_id='project-a')) == 2
        assert len(reopened.list_states(owner='alice', project_id='project-b')) == 2
    finally:
        reopened.close()


@pytest.mark.parametrize('field,values', [
    ('project_id', ('project-a', 'project-b')),
    ('workspace', ('D:/client-a/repo', 'D:/client-b/repo')),
])
def test_objective_sweeps_only_retire_their_own_scope(adapter, tmp_path, monkeypatch, field, values):
    from src.state_mirror.reconcile import sweep
    scopes = [Scope(owner='alice', **{field: value}) for value in values]
    store = StateStore(path=str(tmp_path / 'sweep.sqlite3'))
    try:
        for scope in scopes:
            report = sweep(owner='alice', scope=scope, adapters=[adapter], store=store,
                           publisher=lambda *args, **kwargs: None)
            assert not report.retired and not report.errors
        old_ids = {row.id for row in adapter.discover(scopes[0])}
        other_ids = {row.id for row in adapter.discover(scopes[1])}
        monkeypatch.setattr(adapter, '_payload', lambda scope: {'objectives': [], 'scores': {}, 'log': []})
        report = sweep(owner='alice', scope=scopes[0], adapters=[adapter], store=store,
                       publisher=lambda *args, **kwargs: None)
        assert set(report.retired) == old_ids
        assert all(not store.get_entity(ident).retired() for ident in other_ids)
    finally:
        store.close()


def test_objective_read_failure_keeps_existing_state(adapter, tmp_path, monkeypatch):
    from src.state_mirror.reconcile import sweep
    scope = Scope(owner='alice', project_id='project-a')
    store = StateStore(path=str(tmp_path / 'outage.sqlite3'))
    try:
        sweep(owner='alice', scope=scope, adapters=[adapter], store=store,
              publisher=lambda *args, **kwargs: None)
        def unavailable(scope):
            raise PermissionError('Synthetic objective file outage')
        monkeypatch.setattr(adapter, '_payload', unavailable)
        report = sweep(owner='alice', scope=scope, adapters=[adapter], store=store,
                       publisher=lambda *args, **kwargs: None)
        assert report.adapters_failed == ('objectives',)
        assert not report.retired
        assert len(store.list_entities(owner='alice', project_id='project-a')) == 2
    finally:
        store.close()
