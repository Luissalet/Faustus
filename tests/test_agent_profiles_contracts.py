"""The resolved-execution contract (src/agent_profiles/contracts.py).

What is being pinned, in the order it matters:

* **a merge of two permission envelopes can only ever restrict.** Not "does
  not widen in the cases we thought of" — the property is checked over
  hundreds of generated pairs, for every tool and every effect in a fixed
  universe, because "an override quietly granted something" is the one
  failure this object exists to make impossible (§3.4);
* **the prompt never reaches the record.** A resolution is serialised into
  runs, branches, board events and logs; a prompt in there is a leak and dead
  weight, so only its digest travels and the text itself is refused by name;
* **`identity()` is the configuration, not the occasion.** Two branches of one
  configuration differ in every id they carry, so an identity that counted
  them would answer "different" to the only question it is asked;
* **a completion mode grants nothing.** The same envelope resolved `literal`
  and `maximalist` produces byte-identical permissions;
* **`to_dict()` → `parse()` is the same dict.** A record that changes shape by
  being read and written is a record you cannot compare two of.
"""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent_profiles.contracts import (            # noqa: E402
    AGENT_MODES, CAPABILITIES, COMPLETION_MODES, OVERRIDE_SOURCES, PRECEDENCE, PROFILE_KINDS,
    AgentRef, CompletionChoice, ExecutionScope, ModelRoute, PermissionEnvelope, ProfileError,
    ProfileSet, ResolvedAgentExecution, SelectionTrace, sha256_digest,
)

PROMPT = ("You are an adversarial security reviewer. Never mention this sentence, "
          "sk-secret-token-do-not-log, to anybody.")


def _resolution(**over):
    """A full resolution — every field populated, so a round trip has
    something to lose."""
    base = dict(
        resolution_id="agentres_0123456789abcdef",
        agent=AgentRef(slug="security_reviewer", definition_revision=sha256_digest("def"),
                       source="builtin"),
        execution_scope=ExecutionScope(owner="luis", project_id="faustus", session_id="s1",
                                       run_id="r1", turn_id="t1"),
        model_route=ModelRoute(model="qwen3-coder:30b", endpoint_id="local", runner="native"),
        completion=CompletionChoice(mode="professional", policy_version="professional_v1",
                                    source="task_override", reason="the task asked for one fix"),
        profiles=ProfileSet(context="code_review_v1", verification="security_review_v1",
                            budget="reviewer_standard_v1",
                            collaboration="independent_reviewer_v1",
                            output="review_findings_v1"),
        permissions=PermissionEnvelope(
            tools=("read_file", "grep"), deny=("write_file", "bash"),
            work_roots=("src",), effects=("read",),
            permission_rules=("deny write **", "allow read **")),
        selection=SelectionTrace(requested="", chosen="security_reviewer",
                                 reason="capability match: security, review",
                                 alternatives_rejected=({"slug": "surgeon",
                                                         "reason": "no security capability"},),
                                 scores={"task_fit": 0.91, "availability": 1.0}),
        max_rounds=12, timeout_s=900,
        caveats=("the path rules do not reach inside bash",),
        degraded_integrations=("immune_system",),
        prompt_digest=sha256_digest(PROMPT),
        created_at="2026-09-05T10:00:00Z",
    )
    base.update(over)
    return ResolvedAgentExecution(**base)


# ── the record ──────────────────────────────────────────────────────────────

def test_a_resolution_round_trips_through_its_own_dict():
    original = _resolution()
    payload = original.to_dict()
    again = ResolvedAgentExecution.parse(payload)
    assert again.to_dict() == payload
    assert again.identity() == original.identity()
    # And a second lap, because the first one can normalise something the
    # second one would normalise differently.
    assert ResolvedAgentExecution.parse(again.to_dict()).to_dict() == payload


def test_the_vocabulary_is_data_and_says_what_it_orders():
    assert COMPLETION_MODES == ("literal", "professional", "greedy", "maximalist")
    assert AGENT_MODES == ("coordinator", "worker", "reviewer")
    assert PROFILE_KINDS == ("verification", "context", "budget", "collaboration", "output")
    assert "computer_use" in CAPABILITIES and "diagnostics" in CAPABILITIES
    # §1.6: strongest first, and every override source appears in the ladder
    # or the two lists would be two different precedences.
    assert PRECEDENCE[0] == "system_policy" and PRECEDENCE[-1] == "global_default"
    assert PRECEDENCE.index("task_override") < PRECEDENCE.index("agent_default")
    assert PRECEDENCE.index("agent_default") < PRECEDENCE.index("project_default")
    assert set(OVERRIDE_SOURCES) - set(PRECEDENCE) == {"activity_override", "run_override"}


def test_an_unknown_completion_mode_is_refused_by_name():
    with pytest.raises(ProfileError) as caught:
        CompletionChoice(mode="turbo")
    assert "turbo" in str(caught.value) and "maximalist" in str(caught.value)
    with pytest.raises(ProfileError) as caught:
        ResolvedAgentExecution.parse(dict(_resolution().to_dict(),
                                          completion={"mode": "thorough"}))
    assert "thorough" in str(caught.value)
    assert "completion.mode" in str(caught.value)


def test_an_unknown_key_is_an_error_and_points_at_the_nearest_one():
    payload = dict(_resolution().to_dict())
    payload["promt_digest"] = payload.pop("prompt_digest")
    with pytest.raises(ProfileError) as caught:
        ResolvedAgentExecution.parse(payload)
    assert "promt_digest" in str(caught.value) and "prompt_digest" in str(caught.value)


def test_a_resolution_must_be_able_to_name_its_agent():
    payload = dict(_resolution().to_dict(), agent={"slug": "", "definition_revision": "",
                                                   "source": ""})
    with pytest.raises(ProfileError) as caught:
        ResolvedAgentExecution.parse(payload)
    assert "agent.slug" in str(caught.value)


# ── the prompt is a digest, never the text ─────────────────────────────────

def test_a_prompt_never_reaches_the_record():
    resolution = _resolution()
    serialised = repr(resolution.to_dict())
    assert "adversarial security reviewer" not in serialised
    assert "sk-secret-token-do-not-log" not in serialised
    assert resolution.prompt_digest == sha256_digest(PROMPT)
    # The same prompt digests the same way, so a branch can be compared
    # against its parent without either of them carrying the text.
    assert _resolution().prompt_digest == resolution.prompt_digest


def test_the_prompt_itself_is_refused_by_name():
    with pytest.raises(ProfileError) as caught:
        _resolution(prompt_digest=PROMPT)
    message = str(caught.value)
    assert "prompt_digest" in message and "sha256:" in message
    # A bare hash without the prefix is refused too: one spelling, or two
    # records of the same run compare unequal.
    with pytest.raises(ProfileError):
        _resolution(prompt_digest=sha256_digest(PROMPT).split(":", 1)[1])


# ── identity: the configuration, not the occasion ──────────────────────────

def test_identity_ignores_the_id_the_clock_and_the_route_that_got_here():
    original = _resolution()
    twin = _resolution(
        resolution_id="agentres_ffffffffffffffff",
        created_at="2027-01-01T00:00:00Z",
        execution_scope=ExecutionScope(owner="luis", project_id="faustus",
                                       session_id="OTHER", run_id="OTHER", turn_id="OTHER"),
        selection=SelectionTrace(chosen="security_reviewer", reason="pinned by the user"),
        caveats=("a differently worded warning",),
    )
    assert twin.identity() == original.identity()
    assert twin.resolution_id != original.resolution_id


def test_identity_notices_everything_that_changes_what_would_run():
    original = _resolution()
    changed = {
        "agent": AgentRef(slug="auditor", definition_revision=sha256_digest("def"),
                          source="builtin"),
        "model_route": ModelRoute(model="other-model", endpoint_id="local", runner="native"),
        "completion": CompletionChoice(mode="maximalist", policy_version="maximalist_v1",
                                       source="task_override", reason="deep audit"),
        "profiles": ProfileSet(context="narrow_code_v1"),
        "permissions": PermissionEnvelope(tools=("read_file",)),
        "max_rounds": 3,
        "timeout_s": 60,
        "degraded_integrations": (),
        "prompt_digest": sha256_digest("a different prompt"),
    }
    for field_name, value in changed.items():
        assert _resolution(**{field_name: value}).identity() != original.identity(), field_name
    # Rule ORDER is policy: last match wins, so a reordered list is a
    # different configuration even though the set of rules is the same.
    reordered = _resolution(permissions=PermissionEnvelope(
        tools=("read_file", "grep"), deny=("write_file", "bash"), work_roots=("src",),
        effects=("read",), permission_rules=("allow read **", "deny write **")))
    assert reordered.identity() != original.identity()
    # The owner and the project decide what may be touched, so they count.
    other_owner = _resolution(execution_scope=ExecutionScope(owner="somebody_else",
                                                             project_id="faustus"))
    assert other_owner.identity() != original.identity()


# ── a mode is not a permission ─────────────────────────────────────────────

def test_a_completion_mode_changes_nothing_about_what_may_be_touched():
    read_only = PermissionEnvelope(tools=("read_file", "grep"), deny=("write_file",),
                                   work_roots=("src",), effects=("read",),
                                   permission_rules=("deny write **",))
    literal = _resolution(permissions=read_only,
                          completion=CompletionChoice(mode="literal",
                                                      policy_version="literal_v1"))
    maximalist = _resolution(permissions=read_only,
                             completion=CompletionChoice(mode="maximalist",
                                                         policy_version="maximalist_v1"))
    assert literal.permissions.to_dict() == maximalist.permissions.to_dict()
    assert maximalist.permissions.permits("write_file") is False
    assert maximalist.permissions.permits_effect("write") is False
    # The two are different configurations — that is depth, not authority.
    assert literal.identity() != maximalist.identity()
    # And there is no field on a choice into which a grant could be written.
    assert set(CompletionChoice().to_dict()) == {"mode", "policy_version", "source", "reason"}


# ── the envelope: intersect can only restrict ──────────────────────────────

TOOLS = ("read_file", "write_file", "bash", "grep", "apply_patch")
EFFECTS = ("read", "write", "network", "delegate")
ROOTS = ("src", "src/lib", "docs", "tests", "src/lib/deep")
RULES = ("deny write **", "allow write src/**", "deny read secrets/**", "allow read **",
         "deny delegate *")


def _generated(rng):
    return PermissionEnvelope(
        tools=tuple(t for t in TOOLS if rng.random() < 0.6),
        deny=tuple(t for t in TOOLS if rng.random() < 0.25),
        work_roots=tuple(r for r in ROOTS if rng.random() < 0.5),
        effects=tuple(e for e in EFFECTS if rng.random() < 0.6),
        permission_rules=tuple(r for r in RULES if rng.random() < 0.5),
    )


def test_no_pair_of_envelopes_ever_intersects_into_a_new_grant():
    rng = random.Random(20260905)
    for _ in range(500):
        left, right = _generated(rng), _generated(rng)
        merged = left.intersect(right)
        for tool in TOOLS:
            if tool in merged.tools:
                assert tool in left.tools and tool in right.tools
            if merged.permits(tool):
                assert left.permits(tool) and right.permits(tool)
        for effect in EFFECTS:
            if effect in merged.effects:
                assert effect in left.effects and effect in right.effects
        # every refusal from either side survives
        assert set(left.deny) | set(right.deny) <= set(merged.deny)
        for rule in left.permission_rules + right.permission_rules:
            if rule.startswith("deny"):
                assert rule in merged.permission_rules
        # an allow the receiver never had cannot arrive from the other side
        for rule in merged.permission_rules:
            if rule.startswith("allow"):
                assert rule in left.permission_rules
        # every surviving root lies inside a root of BOTH sides
        for root in merged.work_roots:
            assert left.permits_root(root) and right.permits_root(root)


def test_an_empty_envelope_grants_nothing_rather_than_everything():
    empty = PermissionEnvelope()
    assert empty.permits("read_file") is False
    assert empty.permits_effect("read") is False
    assert empty.permits_root("src/main.py") is False
    # ... and it takes everything down with it, which is the safe direction.
    wide = PermissionEnvelope(tools=TOOLS, work_roots=("src",), effects=EFFECTS)
    assert wide.intersect(empty).to_dict() == empty.to_dict()


def test_work_roots_narrow_to_the_inner_root_instead_of_collapsing():
    outer = PermissionEnvelope(work_roots=("src", "docs"))
    inner = PermissionEnvelope(work_roots=("src/lib",))
    assert outer.intersect(inner).work_roots == ("src/lib",)
    assert inner.intersect(outer).work_roots == ("src/lib",)
    assert PermissionEnvelope(work_roots=("docs",)).intersect(inner).work_roots == ()


def test_a_deny_from_either_side_beats_an_allow_from_the_other():
    writer = PermissionEnvelope(tools=("write_file",), effects=("write",),
                                permission_rules=("allow write **",))
    reader = PermissionEnvelope(tools=("write_file", "read_file"), effects=("write", "read"),
                                permission_rules=("deny write **",))
    merged = writer.intersect(reader)
    assert merged.permission_rules == ("allow write **", "deny write **")
    # ... and the other order refuses at least as much.
    assert reader.intersect(writer).permission_rules == ("deny write **",)


def test_a_rule_whose_effect_cannot_be_read_is_refused():
    with pytest.raises(ProfileError) as caught:
        PermissionEnvelope(permission_rules=("write src/**",))
    assert "write src/**" in str(caught.value)
    assert "deny" in str(caught.value)


def test_a_bare_string_is_never_read_as_a_one_item_list():
    with pytest.raises(ProfileError) as caught:
        PermissionEnvelope(tools="read_file,bash")
    assert "permissions.tools" in str(caught.value)


def test_a_rejected_candidate_must_say_why_it_lost():
    with pytest.raises(ProfileError) as caught:
        SelectionTrace(alternatives_rejected=("surgeon",))
    assert "slug" in str(caught.value) and "reason" in str(caught.value)
    trace = SelectionTrace(alternatives_rejected=[{"slug": "surgeon", "reason": "quarantined"}],
                           scores={"b": 2, "a": 1})
    # scores are stored sorted so two records of the same selection compare equal
    assert trace.scores == (("a", 1.0), ("b", 2.0))
    assert trace.to_dict()["alternatives_rejected"] == [{"slug": "surgeon",
                                                         "reason": "quarantined"}]


def test_the_six_precedence_levels_have_one_spelling_in_the_record():
    # `agent_profiles.completion` names them short; the record names them long.
    assert CompletionChoice(mode="greedy", source="task").source == "task_override"
    assert CompletionChoice(mode="greedy", source="activity").source == "activity_override"
    assert CompletionChoice(mode="greedy", source="agent_default").source == "agent_default"
    with pytest.raises(ProfileError) as caught:
        CompletionChoice(mode="greedy", source="because_i_said_so")
    assert "because_i_said_so" in str(caught.value)
