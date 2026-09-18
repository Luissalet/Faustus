"""Council persistence: the room survives, and nothing happens twice (§20).

The plan's persistence row of the mandatory test matrix, one test each:

* repeating a POST with the same key does not make a second turn or a second
  worker (§15.3) — the store's `claim_idempotency` and the unique index behind
  `create_turn` are the two halves of that;
* a restart during a generation leaves a turn that can be retried safely, and a
  restart during an effect marks uncertainty instead of repeating it (§15.2);
* another owner's room is not visible — not listed, not readable, and not
  leaked through an error message;
* two contradictory orders cannot both apply: a stale `expected_revision` is
  refused with the revision that actually won.

Plus the two properties everything else rests on: a room written, closed and
reopened comes back whole, and two threads writing at once lose nothing.

The blind round gets its own section.  `list_messages(viewer_id=…)` is where
`consult` stops being theatre: if a peer can read an answer it was supposed to
be blind to, the second opinion is the first one wearing a different name.
"""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest

from src.council import contracts as C
from src.council import persistence as P


@pytest.fixture(autouse=True)
def council_db(tmp_path):
    """Every test gets its own database file."""
    P.use_path(str(tmp_path / "council.db"))
    try:
        yield
    finally:
        P.use_path(None)


def _store() -> P.CouncilStore:
    return P.store()


def _session(owner: str = "alice", **over) -> C.CouncilSession:
    payload = {"id": "council_1", "owner": owner, "title": "Implement OAuth",
               "policy": "collaborate", "status": "active",
               "workspace": "D:/LocalAI/odysseus"}
    payload.update(over)
    return _store().create_session(C.CouncilSession.parse(payload))


def _message(**over) -> C.CouncilMessage:
    payload = {"session_id": "council_1", "author_id": "p_a", "author_kind": "model",
               "visibility": "room", "content": "hello"}
    payload.update(over)
    return _store().append_message(C.CouncilMessage.parse(payload))


# ── §15.3  the same key never buys two of anything ─────────────────────────

def test_a_key_is_claimed_once_and_answers_the_same_ref_afterwards():
    _session()
    store = _store()
    first, ref = store.claim_idempotency("council_1", "req-42", kind="turn")
    second, again = store.claim_idempotency("council_1", "req-42", kind="turn")
    third, still = store.claim_idempotency("council_1", "req-42", kind="turn",
                                           ref="a-different-ref")

    assert first is True and ref
    assert (second, again) == (False, ref)
    assert (third, still) == (False, ref)
    assert store.idempotency_ref("council_1", "req-42") == ref


def test_the_same_key_in_another_room_is_a_different_claim():
    _session()
    _session(id="council_2")
    store = _store()
    assert store.claim_idempotency("council_1", "req-42", kind="turn")[0] is True
    assert store.claim_idempotency("council_2", "req-42", kind="turn")[0] is True


def test_a_blank_key_cannot_deduplicate_anything():
    _session()
    with pytest.raises(P.CouncilStoreError):
        _store().claim_idempotency("council_1", "   ", kind="turn")


def test_a_retried_post_does_not_open_a_second_turn():
    """The whole of §15.3 in one flow: claim, create, retry, get the first
    turn back instead of a second one."""
    _session()
    store = _store()

    def submit(key: str):
        fresh, ref = store.claim_idempotency("council_1", key, kind="turn")
        if not fresh:
            return store.get_turn(ref), False
        turn = store.create_turn(C.CouncilTurn.parse({
            "id": ref, "session_id": "council_1", "author_id": "alice",
            "content": "decide and implement", "idempotency_key": key}))
        return turn, True

    first, created_first = submit("req-42")
    second, created_second = submit("req-42")

    assert created_first is True and created_second is False
    assert second is not None and second.id == first.id
    assert len(store.list_turns("council_1")) == 1


def test_the_database_itself_refuses_a_duplicate_key():
    """Not merely this process's lock: a second process racing the first hits a
    unique index, and gets told which turn already has the key."""
    _session()
    store = _store()
    store.create_turn(C.CouncilTurn.parse({
        "id": "turn_1", "session_id": "council_1", "idempotency_key": "req-42"}))
    with pytest.raises(P.DuplicateTurn) as caught:
        store.create_turn(C.CouncilTurn.parse({
            "id": "turn_2", "session_id": "council_1", "idempotency_key": "req-42"}))
    assert caught.value.turn_id == "turn_1"
    assert len(store.list_turns("council_1")) == 1


def test_turns_without_a_key_are_not_deduplicated_against_each_other():
    """The partial index must not collapse every keyless turn into one row."""
    _session()
    store = _store()
    store.create_turn(C.CouncilTurn.parse({"id": "turn_1", "session_id": "council_1"}))
    store.create_turn(C.CouncilTurn.parse({"id": "turn_2", "session_id": "council_1"}))
    assert len(store.list_turns("council_1")) == 2


# ── §4.2  the blind round ──────────────────────────────────────────────────

def test_a_private_answer_does_not_reach_the_other_participants():
    _session(owner="alice")
    _message(id="msg_room", visibility="room", content="the question")
    _message(id="msg_blind", author_id="p_a", visibility="participant",
             audience=["p_coordinator"], content="my independent answer")
    store = _store()

    for_peer = [m.id for m in store.list_messages("council_1", viewer_id="p_b")]
    for_author = [m.id for m in store.list_messages("council_1", viewer_id="p_a")]
    for_addressee = [m.id for m in store.list_messages("council_1",
                                                       viewer_id="p_coordinator")]

    assert for_peer == ["msg_room"]
    assert for_author == ["msg_room", "msg_blind"]
    assert for_addressee == ["msg_room", "msg_blind"]


def test_the_owner_and_the_audit_see_everything():
    """Identities may be hidden between models; never from the user (§4.2)."""
    _session(owner="alice")
    _message(id="msg_blind", author_id="p_a", visibility="participant",
             audience=["p_x"], content="secret")
    store = _store()
    assert [m.id for m in store.list_messages("council_1", viewer_id="alice")] == ["msg_blind"]
    assert [m.id for m in store.list_messages("council_1", audit=True)] == ["msg_blind"]


def test_an_anonymous_reader_gets_the_public_transcript_only():
    _session()
    _message(id="msg_room", visibility="room")
    _message(id="msg_blind", visibility="coordinator", audience=["p_c"])
    assert [m.id for m in _store().list_messages("council_1")] == ["msg_room"]


def test_since_id_walks_forward_without_repeating():
    _session()
    _message(id="msg_1")
    _message(id="msg_2")
    _message(id="msg_3")
    store = _store()
    assert [m.id for m in store.list_messages("council_1", audit=True,
                                              since_id="msg_1")] == ["msg_2", "msg_3"]
    assert store.list_messages("council_1", audit=True, since_id="msg_3") == []


def test_an_unknown_cursor_replays_rather_than_dropping():
    """For a transcript, showing a message twice beats silently losing the ones
    a reconnecting client never saw."""
    _session()
    _message(id="msg_1")
    assert [m.id for m in _store().list_messages("council_1", audit=True,
                                                 since_id="msg_nope")] == ["msg_1"]


def test_a_message_cannot_be_rewritten():
    """Append-only is enforced by absence, which is the only way it survives."""
    store = _store()
    assert not hasattr(store, "update_message")
    assert not hasattr(store, "edit_message")
    assert not hasattr(store, "delete_message")


# ── §8  two contradictory orders cannot both apply ─────────────────────────

def test_a_stale_revision_is_refused_and_names_the_one_that_won():
    session = _session()
    store = _store()
    paused = store.update_session("council_1", {"status": "paused"},
                                  expected_revision=session.revision, owner="alice")
    assert paused.revision == session.revision + 1

    with pytest.raises(P.RevisionConflict) as caught:
        store.update_session("council_1", {"status": "completing"},
                             expected_revision=session.revision, owner="alice")
    assert caught.value.revision == paused.revision
    assert str(paused.revision) in str(caught.value)
    assert store.get_session("council_1", owner="alice").status == "paused"


def test_an_update_without_a_revision_still_bumps_it():
    _session()
    store = _store()
    assert store.update_session("council_1", {"title": "renamed"}).revision == 2


def test_a_status_change_must_be_a_declared_transition():
    _session(status="active")
    store = _store()
    with pytest.raises(C.CouncilError):
        store.update_session("council_1", {"status": "draft"})
    assert store.get_session("council_1").status == "active"


def test_only_the_updatable_fields_are_updatable():
    """Refusing beats ignoring: a caller that thinks it changed `created_at`
    and did not is writing tomorrow's bug report."""
    _session()
    for patch in ({"owner": "mallory"}, {"revision": 99}, {"created_at": ""},
                  {"id": "council_2"}):
        with pytest.raises(P.CouncilStoreError):
            _store().update_session("council_1", patch)


# ── §20  another owner's room does not exist here ──────────────────────────

def test_a_room_belonging_to_someone_else_is_invisible():
    _session(owner="bob", id="council_bob", title="Bob's private negotiation")
    _session(owner="alice", id="council_alice")
    store = _store()

    assert store.get_session("council_bob", owner="alice") is None
    assert [s.id for s in store.list_sessions(owner="alice")] == ["council_alice"]


def test_the_refusal_does_not_leak_the_title():
    _session(owner="bob", id="council_bob", title="Bob's private negotiation")
    with pytest.raises(P.NotFound) as caught:
        _store().update_session("council_bob", {"title": "mine now"}, owner="alice")
    assert "private negotiation" not in str(caught.value)
    assert _store().archive_session("council_bob", owner="alice") is False


def test_an_unscoped_read_is_the_maintenance_path_and_says_so():
    """`owner=""` is recovery, the doctor and tests.  Routes pass the caller."""
    _session(owner="bob", id="council_bob")
    assert _store().get_session("council_bob") is not None


def test_archiving_hides_a_room_without_deleting_its_evidence():
    _session()
    _message(id="msg_1")
    store = _store()
    assert store.archive_session("council_1", owner="alice") is True
    assert store.list_sessions(owner="alice") == []
    assert store.get_session("council_1", owner="alice") is not None
    assert [m.id for m in store.list_messages("council_1", audit=True)] == ["msg_1"]


# ── §3.3  one resource, one owner ──────────────────────────────────────────

def test_two_drivers_cannot_hold_the_same_file():
    _session()
    store = _store()
    store.create_claim(C.CouncilClaim.parse({
        "id": "claim_1", "session_id": "council_1", "kind": "file",
        "resource": "src/auth/github.py", "holder_id": "p_codex", "state": "held"}))

    with pytest.raises(P.ClaimConflict) as caught:
        store.create_claim(C.CouncilClaim.parse({
            "id": "claim_2", "session_id": "council_1", "kind": "file",
            "resource": "src/auth/github.py", "holder_id": "p_claude",
            "state": "requested"}))
    assert caught.value.holder_id == "p_codex"
    assert caught.value.claim_id == "claim_1"
    assert len(store.list_claims("council_1")) == 1


def test_a_released_resource_can_be_claimed_again():
    _session()
    store = _store()
    store.create_claim(C.CouncilClaim.parse({
        "id": "claim_1", "session_id": "council_1", "resource": "src/a.py",
        "holder_id": "p_codex", "state": "held"}))
    store.update_claim("claim_1", {"state": "released"})
    store.create_claim(C.CouncilClaim.parse({
        "id": "claim_2", "session_id": "council_1", "resource": "src/a.py",
        "holder_id": "p_claude", "state": "held"}))
    assert [c.id for c in store.list_claims("council_1", state="held")] == ["claim_2"]


def test_a_claim_cannot_be_repointed_at_another_resource():
    _session()
    store = _store()
    store.create_claim(C.CouncilClaim.parse({
        "id": "claim_1", "session_id": "council_1", "resource": "src/a.py",
        "holder_id": "p_codex", "state": "held"}))
    with pytest.raises(P.CouncilStoreError):
        store.update_claim("claim_1", {"resource": "src/b.py"})


# ── §8  a turn moves along its graph, in storage ───────────────────────────

def test_a_turn_cannot_skip_the_graph():
    _session()
    store = _store()
    store.create_turn(C.CouncilTurn.parse({"id": "turn_1", "session_id": "council_1"}))
    store.update_turn("turn_1", {"state": "classified"})
    with pytest.raises(C.CouncilError):
        store.update_turn("turn_1", {"state": "completed"})
    assert store.get_turn("turn_1").state == "classified"


def test_re_asserting_the_same_state_is_a_no_op_not_an_error():
    _session()
    store = _store()
    store.create_turn(C.CouncilTurn.parse({"id": "turn_1", "session_id": "council_1",
                                           "state": "running"}))
    assert store.update_turn("turn_1", {"state": "running"}).state == "running"


def test_a_turns_author_and_key_are_not_rewritable():
    _session()
    store = _store()
    store.create_turn(C.CouncilTurn.parse({
        "id": "turn_1", "session_id": "council_1", "author_id": "alice",
        "idempotency_key": "req-42"}))
    for patch in ({"author_id": "p_codex"}, {"idempotency_key": "req-43"}):
        with pytest.raises(P.CouncilStoreError):
            store.update_turn("turn_1", patch)


# ── §12  a stored decision is not edited either ────────────────────────────

def test_the_store_offers_no_way_to_edit_a_decision():
    store = _store()
    assert not hasattr(store, "update_decision")
    assert "update_decision" not in P.__all__


def test_superseding_writes_both_rows_in_one_go():
    _session()
    store = _store()
    store.create_decision(C.CouncilDecision.parse({
        "id": "decision_1", "session_id": "council_1", "status": "decided",
        "question": "where is the state validated?", "chosen": "client side",
        "supporters": ["p_codex"], "dissenters": ["p_claude"]}))
    retired, current = store.supersede_decision("decision_1", C.CouncilDecision.parse({
        "id": "decision_2", "status": "decided", "chosen": "server side",
        "supporters": ["p_codex", "p_claude"]}))

    assert retired.status == "superseded" and current.supersedes == "decision_1"
    stored = {d.id: d for d in store.list_decisions("council_1")}
    assert stored["decision_1"].status == "superseded"
    assert stored["decision_1"].chosen == "client side"   # the old text survives
    assert stored["decision_1"].dissenters == ("p_claude",)
    assert stored["decision_2"].session_id == "council_1"


def test_a_status_move_follows_the_decision_graph():
    _session()
    store = _store()
    store.create_decision(C.CouncilDecision.parse({
        "id": "decision_1", "session_id": "council_1", "status": "proposed"}))
    assert store.set_decision_status("decision_1", "decided").status == "decided"
    with pytest.raises(C.CouncilError):
        store.set_decision_status("decision_1", "proposed")
    with pytest.raises(P.CouncilStoreError):
        store.set_decision_status("decision_1", "superseded")


def test_a_blocking_objection_is_answerable_from_storage():
    _session()
    store = _store()
    store.create_objection(C.CouncilObjection.parse({
        "id": "obj_1", "session_id": "council_1", "target_kind": "task",
        "target_id": "task_1", "severity": "blocking", "status": "open",
        "claim": "the state is never invalidated"}))
    assert C.has_blocking(store.list_objections("council_1")) is True

    store.update_objection("obj_1", {"status": "resolved"})
    assert C.has_blocking(store.list_objections("council_1")) is False
    assert store.get_objection("obj_1").resolved_at        # stamped, not invented


# ── §15.2  what a restart is allowed to do ─────────────────────────────────

def _room_mid_flight():
    """A room exactly as a power cut would leave it: active, a turn running, a
    file held, a delegated task in flight."""
    _session(status="active")
    store = _store()
    store.create_turn(C.CouncilTurn.parse({
        "id": "turn_1", "session_id": "council_1", "state": "running",
        "author_id": "alice", "content": "implement it"}))
    store.create_turn(C.CouncilTurn.parse({
        "id": "turn_0", "session_id": "council_1", "state": "completed",
        "stop_reason": "completed"}))
    store.create_turn(C.CouncilTurn.parse({
        "id": "turn_2", "session_id": "council_1", "state": "awaiting_approval"}))
    store.create_claim(C.CouncilClaim.parse({
        "id": "claim_1", "session_id": "council_1", "kind": "file",
        "resource": "src/auth/github.py", "holder_id": "p_codex", "state": "held",
        "task_id": "task_1"}))
    store.create_task(C.CouncilTask.parse({
        "id": "task_1", "session_id": "council_1", "title": "OAuth callback",
        "owner_participant_id": "p_codex", "status": "running", "run_id": "run_5"}))
    return store


def test_a_restart_interrupts_what_was_in_flight_and_releases_its_claims():
    store = _room_mid_flight()

    report = store.recover(now="2026-09-06T12:00:00Z")

    assert store.get_turn("turn_1").state == C.TURN_INTERRUPTED
    assert store.get_claim("claim_1").state == "released"
    assert store.get_session("council_1").status == "interrupted"
    assert report["turns_interrupted"] == ["turn_1"]
    assert report["claims_released"] == ["claim_1"]
    assert report["sessions_interrupted"] == ["council_1"]
    assert report["at"] == "2026-09-06T12:00:00Z"


def test_a_restart_leaves_what_was_at_rest_alone():
    """A finished turn is finished; a pending approval waits for a person, and
    people survive restarts (§15.2, §20)."""
    store = _room_mid_flight()
    store.recover()

    assert store.get_turn("turn_0").state == "completed"
    assert store.get_turn("turn_0").stop_reason == "completed"
    assert store.get_turn("turn_2").state == "awaiting_approval"


def test_a_restart_invents_no_stop_reason():
    """"The process died" is not a reason the room reached a conclusion."""
    store = _room_mid_flight()
    store.recover()
    assert store.get_turn("turn_1").stop_reason == ""


def test_a_restart_repeats_no_effect_and_reports_what_needs_reconciling():
    """§25: a delegated task with an uncertain outcome is handed back, not
    re-run.  Its status and its `run_id` are untouched."""
    store = _room_mid_flight()
    before = store.stats()["tables"]

    report = store.recover()

    task = store.get_task("task_1")
    assert task.status == "running" and task.run_id == "run_5"
    assert report["tasks_needing_reconciliation"] == ["task_1"]
    assert report["effects_repeated"] == []
    assert store.stats()["tables"] == before      # nothing was created or dropped


def test_recovery_is_idempotent():
    store = _room_mid_flight()
    store.recover()
    second = store.recover()
    assert second["counts"] == {"sessions_interrupted": 0, "turns_interrupted": 0,
                                "claims_released": 0,
                                "tasks_needing_reconciliation": 1}


def test_recovery_bumps_the_revision_so_a_dead_process_cannot_land_a_write():
    store = _room_mid_flight()
    stale = store.get_session("council_1").revision
    store.recover()
    with pytest.raises(P.RevisionConflict):
        store.update_session("council_1", {"status": "active"}, expected_revision=stale)


def test_a_released_claim_says_why_it_was_released():
    store = _room_mid_flight()
    store.recover(now="2026-09-06T12:00:00Z")
    assert "recovery" in store.get_claim("claim_1").note


# ── durability and concurrency ─────────────────────────────────────────────

def test_a_room_closed_and_reopened_comes_back_whole():
    path = P.db_path()
    first = P.CouncilStore(path=path)
    session = first.create_session(C.CouncilSession.parse({
        "id": "council_1", "owner": "alice", "title": "Implement OAuth",
        "policy": "debate", "status": "active", "participants": ["p_a", "p_b"],
        "budgets": {"max_rounds": 2, "max_parallel": 0},
        "activity_completion_mode": "greedy"}))
    first.add_participant("council_1", C.CouncilParticipant.parse({
        "id": "p_a", "display_name": "Claude", "roles": ["critic"],
        "tool_profile": "read_only", "capabilities": ["code"]}))
    first.append_message(C.CouncilMessage.parse({
        "id": "msg_1", "session_id": "council_1", "author_id": "p_a",
        "message_type": "critique", "visibility": "participant",
        "audience": ["p_b"], "content": "not like that", "metadata": {"tokens": 12}}))

    reopened = P.CouncilStore(path=path)

    assert reopened.get_session("council_1", owner="alice") == session
    assert reopened.list_participants("council_1")[0].roles == ("critic",)
    restored = reopened.list_messages("council_1", audit=True)[0]
    assert restored.audience == ("p_b",) and restored.metadata == {"tokens": 12}
    assert reopened.get_session("council_1").budgets.max_parallel == 0


def test_two_threads_writing_at_once_lose_nothing():
    _session()
    store = _store()
    errors = []

    def write(tag: str):
        try:
            for i in range(25):
                store.append_message(C.CouncilMessage.parse({
                    "id": f"msg_{tag}_{i}", "session_id": "council_1",
                    "author_id": f"p_{tag}", "visibility": "room",
                    "content": f"{tag}-{i}"}))
        except Exception as exc:  # noqa: BLE001 - the assertion is on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stored = store.list_messages("council_1", audit=True, limit=500)
    assert len(stored) == 50
    assert len({m.id for m in stored}) == 50
    # Each writer's own messages stay in the order it wrote them: the sequence
    # is the transcript's order, and a lost or reordered row would show here.
    for tag in ("a", "b"):
        mine = [m.id for m in stored if m.author_id == f"p_{tag}"]
        assert mine == [f"msg_{tag}_{i}" for i in range(25)]


# ── §20  the read path never raises ────────────────────────────────────────

def test_every_getter_answers_instead_of_raising_when_there_is_nothing():
    store = _store()
    assert store.get_session("council_nope") is None
    assert store.get_participant("council_nope", "p_nope") is None
    assert store.get_message("msg_nope") is None
    assert store.get_turn("turn_nope") is None
    assert store.get_task("task_nope") is None
    assert store.get_claim("claim_nope") is None
    assert store.get_objection("obj_nope") is None
    assert store.get_decision("decision_nope") is None
    assert store.list_sessions() == []
    assert store.list_messages("council_nope") == []
    assert store.list_turns("council_nope") == []
    assert store.list_claims("council_nope") == []
    assert store.idempotency_ref("council_nope", "req") == ""


def test_a_corrupt_file_is_quarantined_and_the_read_path_still_answers():
    """WAL on a Windows box that lost power.  Losing the file is bad; a store
    that merely looks empty forever is worse, so the wreck is kept as
    `.corrupt` and the reads keep working."""
    _session()
    path = P.db_path()
    P.use_path(path)                       # drop the cached store and its handles
    with open(path, "wb") as handle:
        handle.write(b"this is not a database\n" * 200)

    store = P.store()
    assert store.list_sessions(owner="alice") == []
    assert store.get_session("council_1") is None
    assert os.path.exists(path + ".corrupt")

    # And it is usable again afterwards: the schema was recreated.
    _session()
    assert [s.id for s in store.list_sessions(owner="alice")] == ["council_1"]


def test_an_unreadable_row_costs_that_row_and_not_the_transcript():
    """One hand-edited cell must not empty a room."""
    _session()
    _message(id="msg_1", content="good")
    _message(id="msg_2", content="also good")
    with sqlite3.connect(P.db_path()) as conn:
        conn.execute("UPDATE council_messages SET visibility = 'shouting' WHERE id = ?",
                     ("msg_1",))
    assert [m.id for m in _store().list_messages("council_1", audit=True)] == ["msg_2"]


def test_stats_describes_the_store_without_raising():
    _session()
    stats = _store().stats()
    assert stats["path"].endswith("council.db")
    assert "council" in stats["schemas"]
    assert stats["tables"]["council_sessions"] == 1
    assert stats["bytes"] > 0


# ── §11.1  a seat can be narrowed, and the narrowing is stored ─────────────

def test_narrowing_a_participants_profile_is_a_stored_fact():
    """A policy may lower a profile between activities.  If the narrowing lived
    only in memory it would evaporate at the next restart, which is exactly the
    moment a room is least able to notice."""
    _session()
    store = _store()
    store.add_participant("council_1", C.CouncilParticipant.parse({
        "id": "p_codex", "display_name": "Codex", "kind": "model",
        "model": "openai/codex", "roles": ["driver"], "tool_profile": "scoped_write",
        "capabilities": ["code"]}))

    narrowed = store.update_participant("council_1", "p_codex",
                                        {"tool_profile": "read_only", "status": "paused"})
    assert narrowed.may_write() is False

    reread = store.get_participant("council_1", "p_codex")
    assert reread.tool_profile == "read_only" and reread.status == "paused"
    assert reread.display_name == "Codex" and reread.roles == ("driver",)
    assert reread.capabilities == ("code",)


def test_a_seat_cannot_be_renamed_or_invented():
    _session()
    store = _store()
    store.add_participant("council_1", C.CouncilParticipant.parse({"id": "p_codex"}))
    with pytest.raises(P.CouncilStoreError):
        store.update_participant("council_1", "p_codex", {"id": "p_someone_else"})
    with pytest.raises(P.NotFound):
        store.update_participant("council_1", "p_nobody", {"status": "available"})


def test_the_same_participant_sits_in_two_rooms_independently():
    _session(id="council_1")
    _session(id="council_2")
    store = _store()
    for room, profile in (("council_1", "scoped_write"), ("council_2", "read_only")):
        store.add_participant(room, C.CouncilParticipant.parse({
            "id": "p_codex", "roles": ["driver"], "tool_profile": profile}))
    assert store.get_participant("council_1", "p_codex").may_write() is True
    assert store.get_participant("council_2", "p_codex").may_write() is False


# ── §7.4  a task keeps its owner, its reviewer and its run ─────────────────

def test_a_task_moves_through_storage_without_losing_its_owner_or_its_run():
    _session()
    store = _store()
    store.create_task(C.CouncilTask.parse({
        "id": "task_1", "session_id": "council_1", "title": "OAuth callback",
        "owner_participant_id": "p_codex", "reviewer_participant_id": "p_claude",
        "status": "pending", "acceptance": ["rejects an invalid state"],
        "depends_on": ["task_0"]}))

    updated = store.update_task("task_1", {"status": "review", "run_id": "run_5",
                                           "proof_id": "proof_9"})

    assert updated.status == "review" and updated.proof_id == "proof_9"
    assert updated.owner_participant_id == "p_codex"
    assert updated.reviewer_participant_id == "p_claude"
    assert updated.acceptance == ("rejects an invalid state",)
    assert [t.id for t in store.list_tasks("council_1", status="review")] == ["task_1"]
    assert [t.id for t in store.list_tasks("council_1",
                                           owner_participant_id="p_codex")] == ["task_1"]
    assert store.list_tasks("council_1", status="done") == []


def test_a_task_cannot_be_moved_to_another_room():
    _session()
    store = _store()
    store.create_task(C.CouncilTask.parse({"id": "task_1", "session_id": "council_1"}))
    with pytest.raises(P.CouncilStoreError):
        store.update_task("task_1", {"session_id": "council_2"})
