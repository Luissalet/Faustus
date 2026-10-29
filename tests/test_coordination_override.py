from dataclasses import replace
import pytest
from src.coordination import runtime
from src.coordination.bindings import Binding, ProjectTemplate, CoordinationError, StaleBinding
from src.coordination.store import Store

@pytest.fixture
def scope(tmp_path, monkeypatch):
    from services import projects
    from src import tool_utils
    store = Store(tmp_path)
    template = store.save_template(ProjectTemplate(id='template', owner='owner', project_id='project', connector_id='hub', actor='faustus', operations=('task', 'post'), repos=('FieldDex',), thread_ids=(1890,), connector_fingerprint='fresh'))
    manager = object()
    calls = []
    monkeypatch.setattr(runtime, 'get_store', lambda: store)
    monkeypatch.setattr(runtime, 'storage_owner', lambda owner: owner)
    monkeypatch.setattr(projects, 'project_for_session', lambda session, owner: {'id': 'project', 'enabled': True})
    monkeypatch.setattr(tool_utils, 'get_mcp_manager', lambda: manager)

    def validate(binding, *, owner, session_id, project_id=None, manager=None):
        calls.append(binding)
        if binding.owner != owner or binding.session_id != session_id:
            raise CoordinationError('identity changed')
        if binding.project_id != 'project' or project_id not in (None, 'project'):
            raise CoordinationError('project changed')
        if binding.connector_fingerprint != 'fresh':
            raise CoordinationError('connector configuration changed')
        return {'id': 'project'}
    monkeypatch.setattr(runtime, 'validate', validate)
    return (store, template, manager, calls)

def explicit(store, **changes):
    base = Binding(id='explicit', owner='owner', project_id='project', session_id='chat', connector_id='hub', actor='faustus-chat', operations=('task', 'post'), repos=('FieldDex',), thread_ids=(1890,), connector_fingerprint='fresh')
    return store.save(replace(base, **changes))

def test_explicit_valid_chat_ignores_stale_project_template(scope):
    store, template, manager, calls = scope
    stale = store.save_template(replace(template, connector_fingerprint='old'), expected_revision=1)
    binding = explicit(store)
    before = store.session_bindings('owner', 'chat')
    grant = runtime.resolve_grant('owner', 'chat', project_id='project', manager=manager)
    assert grant.binding == binding and grant.manager is manager
    assert calls == [binding] and store.session_bindings('owner', 'chat') == before
    grant.live()

def test_explicit_chat_is_not_validated_as_a_different_template(scope):
    store, template, manager, calls = scope
    binding = explicit(store)
    assert runtime.resolve_grant('owner', 'chat').binding == binding
    assert calls == [binding]

def test_revoked_chat_is_not_reactivated_by_stale_template(scope):
    store, template, manager, calls = scope
    binding = explicit(store, active=False)
    store.save_template(replace(template, connector_fingerprint='old'), expected_revision=1)
    with pytest.raises(CoordinationError):
        runtime.resolve_grant('owner', 'chat')
    assert store.session_bindings('owner', 'chat') == [binding] and calls == []

def test_invalid_explicit_binding_still_fails_full_validation(scope):
    store, template, manager, calls = scope
    binding = explicit(store, connector_fingerprint='old')
    with pytest.raises(CoordinationError, match='connector configuration changed'):
        runtime.resolve_grant('owner', 'chat')
    assert calls[-1] == binding

def test_changed_project_still_rejects_explicit_binding(scope):
    store, template, manager, calls = scope
    binding = explicit(store)
    with pytest.raises(CoordinationError, match='project changed'):
        runtime.resolve_grant('owner', 'chat', project_id='other')
    assert store.session_bindings('owner', 'chat') == [binding]

def test_new_chat_inherits_valid_project_template(scope):
    store, template, manager, calls = scope
    grant = runtime.resolve_grant('owner', 'chat')
    assert grant.binding.template_id == template.id and grant.binding.template_revision == 1
    assert grant.binding.connector_fingerprint == 'fresh' and len(calls) == 2

def test_new_chat_does_not_inherit_stale_template(scope):
    store, template, manager, calls = scope
    store.save_template(replace(template, connector_fingerprint='old'), expected_revision=1)
    with pytest.raises(CoordinationError, match='connector configuration changed'):
        runtime.resolve_grant('owner', 'chat')
    assert not store.session_bindings('owner', 'chat')

def test_inherited_binding_refreshes_from_valid_changed_template(scope):
    store, template, manager, calls = scope
    prior = store.materialize(template, 'chat')
    new = store.save_template(replace(template, thread_ids=(32,)), expected_revision=1)
    grant = runtime.resolve_grant('owner', 'chat')
    assert grant.binding.id == prior.id and grant.binding.template_revision == 2 and (grant.binding.thread_ids == (32,))
    with pytest.raises(StaleBinding):
        store.live(prior)

def test_inherited_binding_still_requires_fresh_template(scope):
    store, template, manager, calls = scope
    prior = store.materialize(template, 'chat')
    store.save_template(replace(template, connector_fingerprint='old'), expected_revision=1)
    with pytest.raises(CoordinationError, match='connector configuration changed'):
        runtime.resolve_grant('owner', 'chat')
    assert store.session_bindings('owner', 'chat') == [prior]

def test_revoked_template_still_invalidates_inherited_binding(scope):
    store, template, manager, calls = scope
    prior = store.materialize(template, 'chat')
    store.save_template(replace(template, active=False), expected_revision=1)
    with pytest.raises(StaleBinding):
        runtime.resolve_grant('owner', 'chat')
    assert store.session_bindings('owner', 'chat') == [prior]

def test_other_owner_cannot_reuse_explicit_binding(scope):
    store, template, manager, calls = scope
    explicit(store)
    with pytest.raises(CoordinationError):
        runtime.resolve_grant('other-owner', 'chat')
    assert calls == []
