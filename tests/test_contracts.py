"""The eight contracts (src/contracts) — "policy precedes the tool".

What is checked here is not that a valid manifest parses; that is the easy
half. It is the refusals: a one-letter typo is a rejection and not a default,
a truthy string is not a permission, a manifest that asks for the network but
forgets to declare the approval still gets one, an approval stops covering a
plan the moment a recipient changes, a run cannot go from cancelled back to
running, an artifact filename cannot be a path, a memory view that says it is
degraded has to say what it lost, and a redacted event reports how much it
removed instead of quietly losing a field.
"""
from __future__ import annotations

import pytest

from src import contracts as C
from src.contracts import ContractError


VIDEO_SKILL = {
    "id": "media.video.short-form",
    "version": "1.0.0",
    "title": "Short-form video from a brief",
    "inputs": {"brief": "text", "references": "artifact[]", "duration_seconds": "integer"},
    "outputs": {"video": "artifact:video", "thumbnail": "artifact:image"},
    "memory": {"read_scopes": ["project", "skill"], "write_scopes": ["run"]},
    "permissions": {"network": False, "secrets": [], "backends": ["media_worker"]},
    "approval": {"required_when": ["publish", "cost_over_budget"]},
}


# ── the manifest ───────────────────────────────────────────────────────────

def test_manifest_round_trips_and_fingerprints_the_promise():
    m = C.SkillManifest.parse(VIDEO_SKILL)
    assert m.output_kinds() == ("image", "video")
    again = C.SkillManifest.parse(m.to_dict())
    assert again.fingerprint() == m.fingerprint()

    # description and source are not part of the promise …
    reworded = C.SkillManifest.parse({**VIDEO_SKILL, "description": "different words"})
    assert reworded.fingerprint() == m.fingerprint()

    # … one more permission is.
    wider = C.SkillManifest.parse({
        **VIDEO_SKILL,
        "permissions": {**VIDEO_SKILL["permissions"], "secrets": ["openai"]},
    })
    assert wider.fingerprint() != m.fingerprint()


def test_a_typo_is_a_rejection_that_names_the_key_it_meant():
    with pytest.raises(ContractError) as err:
        C.SkillManifest.parse({**VIDEO_SKILL, "permisions": {"network": True}})
    assert "permisions" in str(err.value)
    assert "did you mean 'permissions'" in str(err.value)


def test_a_truthy_value_is_never_a_permission():
    for truthy in ("yes", 1, "true"):
        with pytest.raises(ContractError) as err:
            C.SkillManifest.parse({
                **VIDEO_SKILL, "permissions": {"network": truthy, "backends": ["x"]},
            })
        assert "permissions.network" in str(err.value)


def test_asking_for_the_network_earns_the_card_even_undeclared():
    """The manifest below never lists `network` under approval.required_when.
    What it asked for is the evidence; what it declared is a claim about it."""
    m = C.SkillManifest.parse({
        **VIDEO_SKILL,
        "permissions": {"network": True, "secrets": ["youtube"], "backends": ["media_worker"]},
        "approval": {"required_when": ["publish"]},
    })
    assert m.implied_approvals() == ("network", "secrets")
    assert m.effective_approvals() == ("network", "publish", "secrets")


def test_a_skill_cannot_write_a_durable_user_preference():
    with pytest.raises(ContractError) as err:
        C.SkillManifest.parse({**VIDEO_SKILL, "memory": {"write_scopes": ["user"]}})
    assert "curator" in str(err.value)


def test_a_bare_string_is_not_a_one_item_list():
    with pytest.raises(ContractError) as err:
        C.SkillManifest.parse({
            **VIDEO_SKILL, "permissions": {"backends": "media_worker,local"},
        })
    assert "one permission or two" in str(err.value)


def test_a_manifest_from_the_future_is_refused_rather_than_half_read():
    with pytest.raises(ContractError) as err:
        C.SkillManifest.parse({**VIDEO_SKILL, "schema_version": C.SCHEMA_VERSION + 1})
    assert "newer Faustus" in str(err.value)


# ── execution ──────────────────────────────────────────────────────────────

def test_the_host_backend_is_unreachable_without_an_acknowledgement():
    with pytest.raises(ContractError) as err:
        C.ExecutionSpec.parse({"backend": "local", "isolation": "none"})
    assert "never" in str(err.value) and "fallback" in str(err.value)

    ok = C.ExecutionSpec.parse({"backend": "local", "isolation": "none", "attended_ack": True})
    assert ok.isolation == "none"


def test_a_spec_may_be_narrower_than_the_manifest_but_never_wider():
    perms = C.SkillManifest.parse(VIDEO_SKILL).permissions
    narrow = C.ExecutionSpec.parse({"backend": "media_worker", "isolation": "process"})
    assert narrow.grants_beyond(perms) == ()

    wider = C.ExecutionSpec.parse({
        "backend": "media_worker", "isolation": "process",
        "network": True, "secret_names": ["openai"],
    })
    assert set(wider.grants_beyond(perms)) == {"network", "secrets:openai"}

    elsewhere = C.ExecutionSpec.parse({"backend": "docker_workspace", "isolation": "container"})
    assert elsewhere.grants_beyond(perms) == ("backend:docker_workspace",)


# ── the run state machine ──────────────────────────────────────────────────

def test_a_terminal_run_does_not_change_its_mind():
    run = C.Run.parse({"id": "r1", "kind": "chat"}).advanced_to("running")
    cancelled = run.advanced_to("cancelled", reason="user pressed stop")
    assert cancelled.outcome == "cancelled"
    with pytest.raises(ContractError) as err:
        cancelled.advanced_to("running")
    assert "terminal" in str(err.value)


def test_interrupted_is_terminal_and_is_not_a_failure():
    run = C.Run.parse({"id": "r2", "kind": "skill", "skill_id": "a.b",
                       "skill_version": "1.0.0"}).advanced_to("running")
    dead = run.advanced_to("interrupted", reason="the process was restarted")
    assert dead.is_terminal
    assert dead.outcome is None          # unknown, not "panic"
    assert C.Run.parse(dead.to_dict()).status == "interrupted"


def test_a_run_that_names_a_skill_has_to_name_its_version():
    with pytest.raises(ContractError) as err:
        C.Run.parse({"id": "r3", "kind": "skill", "skill_id": "media.video"})
    assert "reproduced or audited" in str(err.value)


def test_a_finished_run_must_carry_when_it_finished():
    with pytest.raises(ContractError):
        C.Run.parse({"id": "r4", "kind": "chat", "status": "completed"})
    with pytest.raises(ContractError):
        C.Run.parse({"id": "r5", "kind": "chat", "status": "running",
                     "started_at": "2026-09-04T00:00:00Z",
                     "ended_at": "2026-09-04T00:01:00Z"})


# ── artifacts ──────────────────────────────────────────────────────────────

def test_an_artifact_filename_is_a_name_and_not_a_path():
    for escape in ("../../data/.app_key", "sub/dir.png", ".."):
        with pytest.raises(ContractError) as err:
            C.Artifact.parse({"id": "a1", "kind": "image", "filename": escape})
        assert "filename" in str(err.value)


def test_an_artifact_reports_the_gaps_in_its_own_provenance():
    bare = C.Artifact.parse({"id": "a2", "kind": "image", "filename": "x.png"})
    assert "model" in bare.provenance_gaps()
    assert "sha256" in bare.provenance_gaps()

    licensed = C.Artifact.parse({
        "id": "a3", "kind": "image", "filename": "y.png", "run_id": "r1",
        "sha256": "0" * 64,
        "provenance": {"model": "sdxl", "backend": "media_worker",
                       "recipe": "poster.v2", "inputs_digest": "a" * 64},
    })
    assert licensed.provenance_gaps() == ("model_license",)


def test_a_retention_of_days_has_to_say_how_many():
    with pytest.raises(ContractError):
        C.Artifact.parse({"id": "a4", "kind": "video", "filename": "v.mp4",
                          "retention": {"policy": "days"}})


# ── approvals ──────────────────────────────────────────────────────────────

PLAN = {
    "action": "deliver", "skill_id": "mail.send", "skill_version": "1.0.0",
    "backend": "docker_workspace", "recipients": ["ana@example.com"],
    "cost_units": 0, "secret_names": ["smtp"], "output_kinds": ["document"],
    "detail": "Send the September report to Ana.",
}


def _granted(plan=None):
    return C.Approval.parse({
        "id": "ap1", "plan": plan or PLAN, "status": "granted",
        "decided_at": "2026-09-04T00:00:00Z", "decided_by": "luis",
    })


def test_an_approval_covers_exactly_the_plan_it_was_shown():
    assert _granted().covers(C.ApprovalPlan.parse(PLAN))["ok"] is True


def test_one_new_recipient_expires_the_yes_and_says_which_field_moved():
    verdict = _granted().covers(C.ApprovalPlan.parse({
        **PLAN, "recipients": ["ana@example.com", "bob@example.com"],
    }))
    assert verdict["ok"] is False
    assert verdict["reason"] == "plan_changed"
    assert [c["field"] for c in verdict["changes"]] == ["recipients"]
    assert verdict["changes"][0]["approved"] == ["ana@example.com"]


@pytest.mark.parametrize("field,value", [
    ("cost_units", 500),
    ("secret_names", ["smtp", "openai"]),
    ("skill_version", "1.1.0"),
    ("backend", "local"),
    ("output_kinds", ["document", "video"]),
    ("detail", "Send the September report to Ana and post it publicly."),
])
def test_every_field_the_masterplan_names_invalidates_the_approval(field, value):
    verdict = _granted().covers(C.ApprovalPlan.parse({**PLAN, field: value}))
    assert verdict["ok"] is False
    assert [c["field"] for c in verdict["changes"]] == [field]


def test_an_approval_is_single_use_unless_someone_asked_otherwise():
    once = _granted()
    assert once.uses_left == 1
    spent = once.consumed()
    assert spent.status == "consumed"
    assert spent.covers(C.ApprovalPlan.parse(PLAN))["reason"] == "status_consumed"


def test_an_expired_approval_says_when_it_expired():
    card = C.Approval.parse({
        "id": "ap2", "plan": PLAN, "status": "granted",
        "decided_at": "2026-09-04T00:00:00Z", "expires_at": "2026-09-04T00:10:00Z",
    })
    verdict = card.covers(C.ApprovalPlan.parse(PLAN), now="2026-09-04T00:11:00Z")
    assert verdict["reason"] == "expired"
    assert verdict["expired_at"] == "2026-09-04T00:10:00Z"


def test_approving_a_skill_without_a_version_is_refused():
    with pytest.raises(ContractError) as err:
        C.ApprovalPlan.parse({**PLAN, "skill_version": ""})
    assert "whatever it becomes after the next update" in str(err.value)


# ── events ─────────────────────────────────────────────────────────────────

def test_redaction_removes_the_secret_and_says_how_many_it_removed():
    ev = C.Event.parse({
        "name": "tool.progress", "run_id": "r1",
        "data": {"cmd": "curl -H 'Authorization: Bearer sk-live-9d8f7a6b5c'",
                 "api_key": "sk-live-9d8f7a6b5c",
                 "note": "nothing secret here"},
    })
    safe = ev.redact(("sk-live-9d8f7a6b5c",))
    assert "sk-live-9d8f7a6b5c" not in safe.sse()
    assert safe.data["api_key"] == "<redacted>"
    assert safe.data["note"] == "nothing secret here"
    assert safe.redactions == 2          # one by key name, one by value


def test_a_short_secret_does_not_blank_ordinary_prose():
    ev = C.Event.parse({"name": "tool.progress", "data": {"note": "the run is at 12%"}})
    assert ev.redact(("12",)).data["note"] == "the run is at 12%"


def test_an_sse_frame_is_unnamed_so_onmessage_actually_hears_it():
    frame = C.emit("run.created", run_id="r1", label="x").sse()
    assert frame.startswith("data: ")
    assert "event:" not in frame
    assert frame.endswith("\n\n")


def test_an_event_nobody_routes_on_is_refused():
    with pytest.raises(ContractError) as err:
        C.Event.parse({"name": "run.almost_done"})
    assert "reaches no hook" in str(err.value)


# ── memory ─────────────────────────────────────────────────────────────────

def test_scope_is_a_wall_and_not_a_label():
    entry = C.MemoryEntry.parse({"id": "m1", "scope": "project", "body": "brand voice",
                                 "project_id": "campaign-a"})
    assert entry.readable_by(("project",), project_id="campaign-a")
    assert not entry.readable_by(("project",), project_id="campaign-b")
    assert not entry.readable_by(("user", "run"), project_id="campaign-a")


def test_an_anti_pattern_has_to_name_the_rule_it_came_from():
    with pytest.raises(ContractError) as err:
        C.MemoryEntry.parse({"id": "m2", "scope": "user", "body": "AVOID: x",
                             "trust": "anti_pattern"})
    assert "inverted_from" in str(err.value)


def test_a_degraded_view_has_to_say_what_it_lost():
    with pytest.raises(ContractError) as err:
        C.MemoryView.parse({"run_id": "r1", "degraded": True})
    assert "worse than no flag at all" in str(err.value)


def test_a_view_records_what_it_dropped_and_why():
    view = C.MemoryView.parse({
        "run_id": "r1", "scopes": ["project"], "entry_ids": ["m1"],
        "dropped": [{"id": "m9", "reason": "budget", "detail": "over 4k chars"}],
        "budget_chars": 4000, "used_chars": 3900,
    })
    assert view.dropped[0]["reason"] == "budget"
    assert C.MemoryView.parse(view.to_dict()).fingerprint() == view.fingerprint()


# ── external identity ──────────────────────────────────────────────────────

def test_a_fresh_binding_can_do_nothing():
    binding = C.ExternalIdentity.parse({
        "id": "x1", "provider": "telegram", "external_id": "12345",
    })
    assert binding.active is False
    assert binding.may("chat")["reason"] == "not_granted"


def test_a_capability_with_no_local_user_behind_it_is_refused():
    with pytest.raises(ContractError) as err:
        C.ExternalIdentity.parse({"id": "x2", "provider": "telegram",
                                  "external_id": "1", "capabilities": ["chat"]})
    assert "no policy behind it" in str(err.value)


def test_revocation_is_a_fact_on_the_record_with_a_reason():
    with pytest.raises(ContractError):
        C.ExternalIdentity.parse({"id": "x3", "provider": "slack", "external_id": "1",
                                  "revoked_at": "2026-09-04T00:00:00Z"})
    revoked = C.ExternalIdentity.parse({
        "id": "x3", "provider": "slack", "external_id": "1", "owner": "luis",
        "capabilities": ["chat"], "revoked_at": "2026-09-04T00:00:00Z",
        "revoked_reason": "phone lost",
    })
    assert revoked.active is False
    assert revoked.may("chat") == {"ok": False, "reason": "revoked",
                                   "at": "2026-09-04T00:00:00Z", "detail": "phone lost"}


# ── the fingerprint rule ───────────────────────────────────────────────────

def test_a_field_boundary_cannot_be_forged_by_re_splitting():
    """The length-prefix rule from `prove.identity_of`: `ab` + `c` and `a` +
    `bc` are different identities, so a transport that re-splits a list cannot
    make one plan look like another."""
    a = C.fingerprint([("to", ["ab", "c"])])
    b = C.fingerprint([("to", ["a", "bc"])])
    assert a != b


def test_the_fingerprint_does_not_depend_on_list_order():
    a = C.fingerprint([("to", ["ana@x", "bob@x"])])
    b = C.fingerprint([("to", ["bob@x", "ana@x"])])
    assert a == b


def test_a_row_that_says_cancelled_and_success_is_a_contradiction():
    run = C.Run.parse({"id": "r6", "kind": "chat"}).advanced_to("running").advanced_to("cancelled")
    payload = run.to_dict()
    assert payload["outcome"] == "cancelled"
    payload["outcome"] = "success"
    with pytest.raises(ContractError) as err:
        C.Run.parse(payload)
    assert "contradicts status 'cancelled'" in str(err.value)
