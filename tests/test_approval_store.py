"""The approval runtime (src/approval_store) — a yes that stays bound to what
was shown.

The failure this is built against is never a forged approval. It is a plan
that drifts one field after the card was signed, while the stored approval
still reads `granted`. So the tests are mostly about drift: a recipient added,
a secret appended, a version bumped — each has to stop the run and say which
field moved, because "approval expired" sends the user hunting for a bug and
"the recipient changed from a@x to b@y" sends them to the plan.

The other half is the gate. `require_admin` accepts the agent's internal
loopback token by design, so an approval endpoint behind it would let the
model grant its own plan. `require_human` is tested here for exactly that.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import ApprovalRow, Base
from src import approval_store
from src.contracts import ApprovalPlan

PLAN = {
    "action": "deliver", "skill_id": "mail.send", "skill_version": "1.0.0",
    "backend": "docker_workspace", "recipients": ["ana@example.com"],
    "cost_units": 0, "secret_names": ["smtp"], "output_kinds": ["document"],
    "detail": "Send the September report to Ana.",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "approvals.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def test_a_card_starts_pending_and_covers_nothing(store):
    card = approval_store.request(PLAN, owner="luis")
    assert card.status == "pending"
    assert approval_store.check(PLAN, owner="luis")["ok"] is False
    assert approval_store.check(PLAN)["reason"] == "no_approval"


def test_granting_needs_a_name_and_the_name_is_kept(store):
    card = approval_store.request(PLAN, owner="luis")
    assert approval_store.decide(card.id, granted=True, by="")["reason"] == "no_decider"

    out = approval_store.decide(card.id, granted=True, by="luis")
    assert out["ok"] is True
    assert out["approval"]["decided_by"] == "luis"
    assert approval_store.check(PLAN, owner="luis")["ok"] is True


def test_one_new_recipient_stops_it_and_names_the_field(store):
    card = approval_store.request(PLAN, owner="luis")
    approval_store.decide(card.id, granted=True, by="luis")

    drifted = {**PLAN, "recipients": ["ana@example.com", "bob@example.com"]}
    verdict = approval_store.check(drifted, owner="luis")
    assert verdict["ok"] is False
    assert verdict["reason"] == "plan_changed"
    assert [c["field"] for c in verdict["changes"]] == ["recipients"]
    assert verdict["changes"][0]["now"] == ["ana@example.com", "bob@example.com"]
    assert verdict["approval_id"] == card.id      # it points at the card that drifted


@pytest.mark.parametrize("field,value", [
    ("secret_names", ["smtp", "openai"]),
    ("skill_version", "1.1.0"),
    ("cost_units", 500),
    ("backend", "local"),
    ("detail", "Send it to Ana and post it publicly."),
])
def test_every_field_the_masterplan_names_stops_the_run(store, field, value):
    card = approval_store.request(PLAN, owner="luis")
    approval_store.decide(card.id, granted=True, by="luis")
    verdict = approval_store.check({**PLAN, field: value}, owner="luis")
    assert verdict["ok"] is False
    assert [c["field"] for c in verdict["changes"]] == [field]


def test_a_denied_card_is_not_a_granted_one(store):
    card = approval_store.request(PLAN, owner="luis")
    approval_store.decide(card.id, granted=False, by="luis", reason="wrong recipient")
    assert approval_store.check(PLAN, owner="luis")["ok"] is False
    assert approval_store.get(card.id).status == "denied"
    assert approval_store.get(card.id).reason == "wrong recipient"


def test_a_yes_is_spent_once_and_two_runs_cannot_share_it(store):
    card = approval_store.request(PLAN, owner="luis")
    approval_store.decide(card.id, granted=True, by="luis")

    first = approval_store.consume(card.id, PLAN)
    assert first == {"ok": True, "reason": "consumed", "uses_left": 0, "status": "consumed"}

    second = approval_store.consume(card.id, PLAN)
    assert second["ok"] is False
    assert second["reason"] == "status_consumed"
    assert approval_store.check(PLAN, owner="luis")["ok"] is False


def test_consuming_re_checks_the_plan_rather_than_trusting_an_earlier_check(store):
    """A `check()` a second ago is not evidence at the moment of acting."""
    card = approval_store.request(PLAN, owner="luis")
    approval_store.decide(card.id, granted=True, by="luis")
    assert approval_store.check(PLAN, owner="luis")["ok"] is True

    out = approval_store.consume(card.id, {**PLAN, "recipients": ["mallory@example.com"]})
    assert out["ok"] is False
    assert out["reason"] == "plan_changed"
    assert approval_store.get(card.id).uses_left == 1     # nothing was spent


def test_a_deadline_is_absolute_and_not_renewed_by_looking(store):
    card = approval_store.request(PLAN, owner="luis", ttl_seconds=1)
    approval_store.decide(card.id, granted=True, by="luis")
    stored = approval_store.get(card.id)
    assert stored.expires_at and stored.expires_at > stored.requested_at

    later = "2099-01-01T00:00:00Z"
    assert stored.covers(ApprovalPlan.parse(PLAN), now=later)["reason"] == "expired"


def test_expiring_stale_cards_is_idempotent(store):
    approval_store.request(PLAN, owner="luis", ttl_seconds=1)
    approval_store.request({**PLAN, "detail": "another"}, owner="luis", ttl_seconds=None)
    assert approval_store.expire_stale(now="2099-01-01T00:00:00Z") == 1
    assert approval_store.expire_stale(now="2099-01-01T00:00:00Z") == 0
    assert len(approval_store.pending(owner="luis")) == 1     # the one with no deadline


def test_deciding_a_card_twice_explains_rather_than_fails(store):
    card = approval_store.request(PLAN, owner="luis")
    approval_store.decide(card.id, granted=True, by="luis")
    again = approval_store.decide(card.id, granted=False, by="someone-else")
    assert again["ok"] is False
    assert again["reason"] == "already_granted"
    assert "luis" in again["detail"]
    assert approval_store.get(card.id).status == "granted"


def test_an_unknown_card_is_not_found_and_not_an_exception(store):
    assert approval_store.decide("apr_nope", granted=True, by="luis")["reason"] == "not_found"
    assert approval_store.consume("apr_nope", PLAN)["reason"] == "not_found"
    assert approval_store.get("apr_nope") is None


# ── the gate the model must not pass ───────────────────────────────────────

def _request(headers=None, user=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        headers=headers or {},
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def test_the_internal_token_opens_require_admin_and_is_refused_by_require_human():
    """The hole this closes: `require_admin` accepts the agent's loopback
    token by design, so an approval endpoint behind it would let the model
    grant its own plan by calling the same URL the card calls."""
    from fastapi import HTTPException
    from core import middleware

    tooled = _request({middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN})
    assert middleware.require_admin(tooled) is None          # allowed, on purpose

    with pytest.raises(HTTPException) as err:
        middleware.require_human(tooled)
    assert err.value.status_code == 403
    assert "by a person" in err.value.detail


def test_the_stamped_internal_user_is_refused_too():
    from fastapi import HTTPException
    from core import middleware
    from src.owner_identity import INTERNAL_TOOL_USER

    stamped = _request(user=INTERNAL_TOOL_USER)
    assert middleware.require_admin(stamped) is None
    with pytest.raises(HTTPException):
        middleware.require_human(stamped)


def test_a_person_still_gets_through_with_auth_disabled(monkeypatch):
    from core import middleware
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    assert middleware.require_human(_request(user="luis")) is None
