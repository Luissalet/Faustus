from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.coordination.bindings import CoordinationError, ProjectTemplate, OPERATIONS, StaleBinding
from src.coordination.store import Store
from src.coordination.project_config import origin_slug


@pytest.fixture
def project(tmp_path):
    store = Store(tmp_path)
    template = store.save_template(ProjectTemplate(id="p1", owner="owner", project_id="project", connector_id="hub",
        actor="faustus-project", operations=tuple(OPERATIONS), repos=("Writer",), resources=("path:Writer",)))
    return store, template


def test_new_chats_reuse_configuration_with_separate_cursors_and_receipts(project):
    store, template = project
    a, b = store.materialize(template, "chat-a"), store.materialize(template, "chat-b")
    assert a.actor != b.actor and a.actor.startswith("faustus-")
    assert a.actor == template.for_session("chat-a").actor
    assert a.id != b.id and a.session_id != b.session_id
    assert store.materialize(template, "chat-a") == a
    store.receive(a, expected_cursor=0, next_cursor=1, messages=[{"id": 1, "body": "full", "thread_id": 32}])
    assert store.cursor(b) == 0 and not store.pending(b)
    digest = store.pending(a)[0]["digest"]
    receipt = store.detail(a, 1, expected_digest=digest)["receipt"]
    with pytest.raises(CoordinationError):
        store.confirm_delivery(b, receipt)
    assert not store.template("other-owner", "project")


def test_template_revoke_invalidates_all_current_grants(project):
    store, template = project
    a = store.materialize(template, "chat-a")
    store.save_template(replace(template, active=False), expected_revision=1)
    with pytest.raises(StaleBinding):
        store.live(a)
    with pytest.raises(StaleBinding):
        store.materialize(template, "chat-b")


def test_scope_update_refreshes_next_turn_but_not_task_observations(project):
    store, template = project
    a = store.materialize(template, "chat-a")
    store.receive(a, expected_cursor=0, next_cursor=3, messages=[])
    receipt = store.observe_task(a, {"id": 7, "repo": "Writer"})
    store.confirm_task_delivery(a, receipt)
    newer = store.save_template(replace(template, resources=("path:Writer/tests",)), expected_revision=1)
    with pytest.raises(StaleBinding):
        store.live(a)
    refreshed = store.materialize(newer, "chat-a")
    assert refreshed.id == a.id and refreshed.revision == 2 and store.cursor(refreshed) == 3
    with pytest.raises(CoordinationError):
        store.observed_task(refreshed, 7)


def test_explicit_chat_revocation_is_not_reactivated_by_template(project):
    store, template = project
    a = store.materialize(template, "chat-a")
    revoked = store.save(replace(a, active=False), expected_revision=1)
    assert store.materialize(template, "chat-a") == revoked
    with pytest.raises(CoordinationError):
        store.for_session(a.owner, a.session_id)


def test_connector_switch_starts_a_new_durable_cursor(project):
    store, template = project
    a = store.materialize(template, "chat-a")
    store.receive(a, expected_cursor=0, next_cursor=3, messages=[])
    changed = store.save_template(replace(template, connector_id="other", connector_fingerprint="other"), expected_revision=1)
    new = store.materialize(changed, "chat-a")
    assert new.id != a.id and store.cursor(new) == 0
    with pytest.raises(StaleBinding):
        store.live(a)


def test_concurrent_first_turns_materialize_only_one_binding(project):
    store, template = project
    with ThreadPoolExecutor(max_workers=4) as workers:
        bindings = list(workers.map(lambda _: store.materialize(template, "chat-a"), range(12)))
    assert len({b.id for b in bindings}) == 1
    assert len(store.session_bindings(template.owner, "chat-a")) == 1


@pytest.mark.parametrize("value", ["https://github.com/alookai/alook.git", "git@github.com:alookai/alook.git", "alookai/alook"])
def test_origin_uses_verified_repository_slug(value):
    assert origin_slug(value) == "alookai/alook"
