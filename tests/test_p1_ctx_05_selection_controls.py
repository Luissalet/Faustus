"""CTX-05 — recuperacion selectiva con control del usuario.

`src/context_selection.py` stores three kinds of per-owner control
(exclude / exclude_prefix / pin) at three separate scopes (global, project,
session) and `src.context_engine.ranking.validate()` is the one place that
enforces them. This file proves both the enforcement (a candidate whose ref
is excluded is rejected with a recoverable=False "user_excluded" omission,
one whose ref is pinned via `explicit_refs` is let through even when also
excluded) and the isolation the acceptance criterion asks for: excluding a
ref for session A must not touch session B, and must never touch the
underlying source (the control table, not the file).

`test_end_to_end_through_build_request` exercises the real production path:
`context_selection.set_control()` -> `context_engine.wiring.build_request()`
-> `ranking.validate()`, with no monkeypatching of the wiring itself.
"""

from datetime import datetime, timezone

import pytest

from src.context_engine.contracts import ContextCandidate, ContextRequest
from src.context_engine.ranking import validate
from src import context_selection as selection

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def ce_store(tmp_path):
    from src.context_engine import store
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def _request(*, owner="luis", project_id="faustus", excluded_refs=(),
            excluded_prefixes=(), explicit_refs=()):
    return ContextRequest.parse({
        "request_id": "ctxreq_test",
        "execution": {"owner": owner, "project_id": project_id},
        "task": {"query": "what does this say"},
        "policy": {"excluded_refs": list(excluded_refs),
                  "excluded_prefixes": list(excluded_prefixes)},
        "explicit_refs": list(explicit_refs),
    })


def _candidate(ref, **overrides):
    payload = {
        "candidate_id": f"cand::{ref}", "source_type": "file", "source_ref": ref,
        "title": ref, "body": "some retrievable text", "owner": "luis",
        "project_id": "faustus", "observed_at": "2026-09-09T12:00:00Z",
        "trust_class": "observed", "authority": "agent_claim",
    }
    payload.update(overrides)
    return ContextCandidate.parse(payload)


# ── enforcement in ranking.validate ─────────────────────────────────────────

def test_excluded_ref_is_rejected_not_recoverable():
    req = _request(excluded_refs=("file:secret.md",))
    verdict = validate(_candidate("file:secret.md"), req, now=NOW)
    assert verdict.ok is False
    assert verdict.reason == "user_excluded"


def test_excluded_prefix_matches_a_folder():
    req = _request(excluded_prefixes=("file:private/",))
    verdict = validate(_candidate("file:private/notes.md"), req, now=NOW)
    assert verdict.ok is False and verdict.reason == "user_excluded"
    # A sibling outside the folder is untouched.
    verdict2 = validate(_candidate("file:public/notes.md"), req, now=NOW)
    assert verdict2.ok is True


def test_explicit_pin_overrides_a_standing_exclusion():
    req = _request(excluded_refs=("file:secret.md",), explicit_refs=("file:secret.md",))
    verdict = validate(_candidate("file:secret.md"), req, now=NOW)
    assert verdict.ok is True


def test_exclusion_never_touches_an_unrelated_ref():
    req = _request(excluded_refs=("file:secret.md",))
    verdict = validate(_candidate("file:other.md"), req, now=NOW)
    assert verdict.ok is True


# ── the store: scope isolation ──────────────────────────────────────────────

def test_session_exclusion_does_not_leak_to_another_session(ce_store):
    selection.set_control("luis", selection.KIND_EXCLUDE, "file:a.md",
                          project_id="faustus", session_id="s1")
    here = selection.policy_overrides("luis", project_id="faustus", session_id="s1")
    other = selection.policy_overrides("luis", project_id="faustus", session_id="s2")
    assert "file:a.md" in here["excluded_refs"]
    assert "file:a.md" not in other["excluded_refs"]


def test_project_exclusion_reaches_every_session_of_that_project(ce_store):
    selection.set_control("luis", selection.KIND_EXCLUDE, "file:b.md", project_id="faustus")
    s1 = selection.policy_overrides("luis", project_id="faustus", session_id="s1")
    s2 = selection.policy_overrides("luis", project_id="faustus", session_id="s2")
    other_project = selection.policy_overrides("luis", project_id="other", session_id="s1")
    assert "file:b.md" in s1["excluded_refs"]
    assert "file:b.md" in s2["excluded_refs"]
    assert "file:b.md" not in other_project["excluded_refs"]


def test_unset_control_removes_it_and_nothing_else(ce_store):
    selection.set_control("luis", selection.KIND_EXCLUDE, "file:c.md", project_id="faustus")
    selection.set_control("luis", selection.KIND_EXCLUDE, "file:d.md", project_id="faustus")
    assert selection.unset_control("luis", selection.KIND_EXCLUDE, "file:c.md",
                                   project_id="faustus") is True
    overrides = selection.policy_overrides("luis", project_id="faustus")
    assert "file:c.md" not in overrides["excluded_refs"]
    assert "file:d.md" in overrides["excluded_refs"]


def test_pin_is_a_separate_kind_from_exclude(ce_store):
    selection.set_control("luis", selection.KIND_PIN, "file:e.md", project_id="faustus")
    overrides = selection.policy_overrides("luis", project_id="faustus")
    assert "file:e.md" in overrides["pinned_refs"]
    assert "file:e.md" not in overrides["excluded_refs"]


# ── end to end: the real production factory ─────────────────────────────────

def test_end_to_end_through_build_request(ce_store):
    from src.context_engine.wiring import build_request

    selection.set_control("luis", selection.KIND_EXCLUDE, "file:secret.md",
                          project_id="proj1", session_id="sess1")
    selection.set_control("luis", selection.KIND_PIN, "file:pinned.md",
                          project_id="proj1", session_id="sess1")

    request = build_request(owner="luis", session_id="sess1", model="gpt-x",
                            project_id="proj1",
                            messages=[{"role": "user", "content": "hello"}])
    assert "file:secret.md" in request.policy.excluded_refs
    assert "file:pinned.md" in request.explicit_refs

    verdict = validate(_candidate("file:secret.md", owner="luis", project_id="proj1"),
                       request, now=NOW)
    assert verdict.ok is False and verdict.reason == "user_excluded"

    # A DIFFERENT session for the same owner/project never sees sess1's
    # session-scoped exclusion.
    other = build_request(owner="luis", session_id="sess2", model="gpt-x",
                          project_id="proj1", messages=[{"role": "user", "content": "hi"}])
    assert "file:secret.md" not in other.policy.excluded_refs
