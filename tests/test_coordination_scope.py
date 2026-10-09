from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from src.coordination.authority import authorize
from src.coordination.bindings import Binding, CoordinationError, OPERATIONS, StaleBinding
from src.coordination.store import Store


@pytest.fixture
def bound(tmp_path):
    store = Store(tmp_path)
    binding = store.save(Binding(id="b1", owner="owner-a", project_id="project-a", session_id="s1",
                                connector_id="hub-a", actor="faustus-a", operations=tuple(OPERATIONS),
                                resources=("path:Writer/src", "model:principal"), repos=("Writer",), thread_ids=(32,)))
    return store, binding


def message(mid=1, body="abcdef", thread_id=32):
    return {"id": mid, "body": body, "thread_id": thread_id, "author": "claude", "kind": "comment"}


def task(owner="faustus-a", revision=2, **extra):
    return {"id": 7, "repo": "Writer", "paths": ["src/file.ts"], "owner": owner,
            "status": "in_progress", "thread_id": 90, "submission_revision": revision, **extra}


@pytest.mark.parametrize("actor", ["luis", "claude", "cursor", "faustus/claude", None, "faustus-a\n"])
def test_actor_cannot_impersonate_another_participant(bound, actor):
    _, b = bound
    with pytest.raises(CoordinationError):
        replace(b, actor=actor)


@pytest.mark.parametrize("value", ["path:Writer/src2/file", "path:Writer/src/../secret", "path:Writer/src/*",
                                    "path:Writer/src//file", "path:Writer/src/%2e", "path:Writer/src/C:/x",
                                    "app:hub", "path:Other/src/file"])
def test_scope_is_a_normalized_path_boundary_not_a_prefix(bound, value):
    _, b = bound
    assert b.permits_resource("PATH:Writer\\src\\file.ts")
    assert not b.permits_resource(value)


@pytest.mark.parametrize("field,value", [("operations", "poll"), ("operations", ["manage_tokens"]),
                                         ("repos", "Writer"), ("resources", "path:Writer/src"),
                                         ("thread_ids", [True]), ("active", 1), ("revision", True)])
def test_malformed_scope_is_rejected(bound, field, value):
    with pytest.raises(CoordinationError):
        replace(bound[1], **{field: value})


def test_owner_session_actor_and_identity_cannot_drift(bound):
    store, b = bound
    assert store.for_session(b.owner, b.session_id) == b
    with pytest.raises(CoordinationError):
        store.get("owner-b", b.id)
    with pytest.raises(CoordinationError):
        store.save(replace(b, id="b2", session_id="s2"))  # Same active actor.
    with pytest.raises(CoordinationError):
        store.save(replace(b, project_id="another"), expected_revision=1)
    with pytest.raises(StaleBinding):
        store.save(b, expected_revision=0)
    revised = store.save(replace(b, active=False), expected_revision=1)
    assert revised.revision == 2
    for snapshot in (b, revised):
        with pytest.raises(StaleBinding):
            store.cursor(snapshot)


def test_another_owner_cannot_reuse_an_active_connector_identity(bound):
    store, b = bound
    with pytest.raises(CoordinationError, match="active binding"):
        store.save(replace(b, owner="owner-b", session_id="s2", id="b2"))


def test_cursor_and_details_are_atomic_on_invalid_page(bound):
    store, b = bound
    with pytest.raises(CoordinationError):
        store.receive(b, expected_cursor=0, next_cursor=3, messages=[message(), {"id": 2}])
    assert store.cursor(b) == 0
    assert store.pending(b) == []
    store.receive(b, expected_cursor=0, next_cursor=1, messages=[message()])
    assert store.cursor(b) == 1
    assert store.pending(b)[0]["body_excerpt"] == "abcdef"
    with pytest.raises(StaleBinding):
        store.receive(b, expected_cursor=0, next_cursor=2, messages=[message(2)])


def test_poll_response_drop_does_not_lose_pending_details(bound):
    store, b = bound
    store.receive(b, expected_cursor=0, next_cursor=1, messages=[message(body="x" * 1000)])
    reopened = Store(store.path.parent)
    assert reopened.cursor(b) == 1
    assert len(reopened.pending(b)[0]["body_excerpt"]) == 240
    assert reopened.pending(b)[0]["body_chars"] == 1000


def test_partial_and_dropped_detail_response_never_acknowledges(bound):
    store, b = bound
    store.receive(b, expected_cursor=0, next_cursor=1, messages=[message()])
    digest = store.pending(b)[0]["digest"]
    first = store.detail(b, 1, expected_digest=digest, offset=0, length=2)
    assert store.pending(b)  # Fetch alone, or dropped tool response, is not delivery.
    assert not store.confirm_delivery(b, first["receipt"])["complete"]
    last = store.detail(b, 1, expected_digest=digest, offset=4, length=2)
    assert not store.confirm_delivery(b, last["receipt"])["complete"]  # A hole remains.
    middle = store.detail(b, 1, expected_digest=digest, offset=2, length=2)
    result = store.confirm_delivery(b, middle["receipt"])
    assert result == {"message_id": 1, "complete": True, "hub_acknowledged": False}
    assert store.pending(b) == []
    assert store.confirm_delivery(b, middle["receipt"]) == result  # Immediate replay is harmless.


def test_duplicate_span_reuses_receipt_and_body_change_invalidates_it(bound):
    store, b = bound
    store.receive(b, expected_cursor=0, next_cursor=1, messages=[message()])
    digest = store.pending(b)[0]["digest"]
    first = store.detail(b, 1, expected_digest=digest)
    assert store.detail(b, 1, expected_digest=digest)["receipt"] == first["receipt"]
    store.confirm_delivery(b, first["receipt"])
    store.receive(b, expected_cursor=1, next_cursor=1, messages=[message()])
    assert store.pending(b) == []
    store.receive(b, expected_cursor=1, next_cursor=1, messages=[message(body="changed")])
    with pytest.raises(CoordinationError):
        store.confirm_delivery(b, first["receipt"])
    assert store.pending(b)[0]["body_excerpt"] == "changed"


def test_thread_filter_owner_and_binding_are_independent(bound):
    store, b = bound
    store.receive(b, expected_cursor=0, next_cursor=10, messages=[message(10)], threads=[32])
    assert store.cursor(b, [32]) == 10
    assert store.cursor(b, [90]) == 0
    assert store.pending(b, [90]) == []
    d = store.pending(b, [32])[0]["digest"]
    detail = store.detail(b, 10, threads=[32], expected_digest=d)
    with pytest.raises(CoordinationError):
        store.confirm_delivery(b, detail["receipt"], threads=[90])
    other = store.save(replace(b, id="b-other", owner="owner-b", session_id="s-other", actor="faustus-b"))
    assert store.pending(other, [32]) == []
    with pytest.raises(CoordinationError):
        store.confirm_delivery(other, detail["receipt"], threads=[32])


def test_only_one_concurrent_poll_advances_a_shared_cursor(bound):
    store, b = bound
    def receive(mid):
        try:
            store.receive(b, expected_cursor=0, next_cursor=mid, messages=[message(mid)])
            return True
        except StaleBinding:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(receive, [1, 2]))
    assert sorted(outcomes) == [False, True]
    assert len(store.pending(b)) == 1
    assert store.pending(b)[0]["id"] == store.cursor(b)


@pytest.mark.parametrize("operation,args", [("heartbeat", {}), ("add", {"title": "x", "repo": "Writer"}),
                                             ("claim", {"task_id": 7}), ("post", {"thread_id": 32, "body": "x"})])
def test_read_only_policy_remains_stronger_than_binding(bound, operation, args):
    with pytest.raises(CoordinationError, match="reads only"):
        authorize(bound[1], operation, args, task=task(), read_only=True)


@pytest.mark.parametrize("forbidden", [{"agent": "luis"}, {"force": True}, {"force": False},
                                        {"exempt": "docs"}, {"connector_id": "evil"}])
def test_unknown_authority_fields_are_not_silently_dropped(bound, forbidden):
    with pytest.raises(CoordinationError):
        authorize(bound[1], "claim", {"task_id": 7, **forbidden}, task=task())


def test_claim_respects_ownership_repo_paths_and_explicit_locks(bound):
    b = bound[1]
    assert authorize(b, "claim", {"task_id": 7}, task=task(owner=None))["locks"] == ["path:Writer/src/file.ts"]
    for observed in (task(owner="claude"), task(repo="Other"), task(paths=["secrets/key"]), task(paths=[])):
        with pytest.raises(CoordinationError):
            authorize(b, "claim", {"task_id": 7}, task=observed)
    with pytest.raises(CoordinationError):
        authorize(b, "claim", {"task_id": 7, "locks": ["app:hub"]}, task=task())


def test_heartbeat_cannot_extend_locks_outside_this_binding(bound):
    b = bound[1]
    authorize(b, "heartbeat", {}, held_locks=[{"owner": b.actor, "resource": "model:principal"}])
    for locks in (None, [{"owner": b.actor, "resource": "app:hub"}]):
        with pytest.raises(CoordinationError):
            authorize(b, "heartbeat", {}, held_locks=locks)


def test_checkpoint_revision_is_observed_and_review_vote_never_refreshes_it(bound):
    b = bound[1]
    authorize(b, "checkpoint", {"task_id": 7, "expected_revision": 0, "payload": {"summary": "x"}}, task=task())
    with pytest.raises(CoordinationError):
        authorize(b, "checkpoint", {"task_id": 7, "expected_revision": 0, "payload": {}},
                  task=task(latest_checkpoint={"revision": 1}))
    vote = {"task_id": 7, "expected_submission_revision": 2, "verdict": "approve", "body": "tested"}
    authorize(b, "review", vote, task=task(owner="claude"))
    for observed in (task(owner="claude", revision=3), task()):
        with pytest.raises(CoordinationError):
            authorize(b, "review", vote, task=observed)


def test_posting_is_bound_to_selected_or_observed_task_threads(bound):
    b = bound[1]
    authorize(b, "post", {"thread_id": 32, "body": "x"})
    assert "task_id" not in authorize(b, "post", {"thread_id": 90, "task_id": 7, "body": "x"}, task=task())
    with pytest.raises(CoordinationError):
        authorize(b, "post", {"thread_id": 91, "task_id": 7, "body": "x"}, task=task())
