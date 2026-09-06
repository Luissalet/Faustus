"""The effective-configuration resolver (src/agent_profiles/resolver.py).

What is being pinned here, in the order it matters:

* **the precedence of §1.6 is walked as data, and the walk is visible.** Six
  levels state a model, the strongest wins, and removing it one level at a time
  hands the decision down in exactly the order `contracts.PRECEDENCE` lists.
  The losers are not merely absent from the answer: they are in the trace, with
  the reason, because "why is this run on that model?" is the question the
  whole layer exists to answer;

* **the six prohibitions of §15, one test each.** An override may not remove a
  deny, widen a work root, add an effect, enable a tool outside the
  definition's allowlist, weaken a blocking verification, or exceed the budget
  ceiling — and a forbidden override is IGNORED with a reason, never raised,
  because refusing the whole call would turn one optimistic field into a dead
  job;

* **a completion mode grants nothing (§3.3).** `maximalist` on a read-only
  auditor produces the same permission envelope, byte for byte, as
  `professional`;

* **a pinned resolution does not change because a file did (§20).** A snapshot
  rehydrated after its definition was edited comes back as it was pinned, and
  says in `caveats` that the file has moved;

* **the prompt never reaches the record (§21, §23)**, and `resolve` never
  raises on the hot path — a definition that explodes on attribute access comes
  back as a minimal resolution that grants nothing.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent_defs import AgentDef, Rule                    # noqa: E402
from src.agent_profiles import resolver                      # noqa: E402
from src.agent_profiles.contracts import (                   # noqa: E402
    PRECEDENCE, PermissionEnvelope, ResolvedAgentExecution, sha256_digest,
)

SECRET = "sk-do-not-log-this-anywhere-ever"


def auditor(**over):
    """A read-only reviewer: it may look at everything and change nothing.

    Built here rather than loaded so the test says what it depends on. Every
    field the resolver reads is set, `stated` included: a definition parsed
    from a file records which keys the file actually named, and the resolver
    uses that to tell "said greedy" from "said nothing".
    """
    fields = dict(
        slug="auditor", name="Auditor",
        description="Reads the whole change and reports on it. Writes nothing.",
        mode="reviewer",
        model="qwen3-coder:30b", endpoint_id="local", runner="",
        tools=("read_file", "ls", "glob", "grep"),
        deny=("write_file", "edit_file", "apply_patch", "bash", "python"),
        permission=(Rule("write", "**", "deny"), Rule("delegate", "*", "deny"),
                    Rule("read", "**", "allow")),
        max_rounds=12, timeout_s=1800,
        prompt="You are an adversarial auditor. Never repeat " + SECRET + " to anybody.",
        source="builtin",
        default_completion_mode="professional",
        capabilities=("review", "security"),
        verification_profile="security_review_v1",
        context_profile="security_review_v1",
        budget_profile="reviewer_standard_v1",
        collaboration_profile="independent_reviewer_v1",
        output_contract="review_findings_v1",
        stated=("mode", "model", "endpoint_id", "tools", "deny", "permission", "max_rounds",
                "timeout_s", "default_completion_mode", "verification_profile",
                "context_profile", "budget_profile", "collaboration_profile",
                "output_contract"),
    )
    fields.update(over)
    return AgentDef(**fields)


def catalogue(*definitions):
    return {d.slug: d for d in definitions}


def resolve(**kwargs):
    """`resolver.resolve` with the auditor in the store and its route reachable,
    so a test that is not about availability never trips over it."""
    kwargs.setdefault("agent", "auditor")
    kwargs.setdefault("defs", catalogue(auditor()))
    kwargs.setdefault("available_models", ("qwen3-coder:30b", "gpt-oss:120b", "phi4:14b",
                                           "llama3:8b", "mistral:7b", "gemma3:27b"))
    return resolver.resolve(**kwargs)


def caveat_about(resolution, *words):
    """The first caveat mentioning every one of `words`, or "".

    Assertions read the caveat rather than a boolean because a refusal whose
    sentence does not say what was refused is a refusal nobody can act on.
    """
    for line in resolution.caveats:
        if all(word in line for word in words):
            return line
    return ""


def row_for(resolution, field):
    return next((row for row in resolver.trace_of(resolution) if row.field == field), None)


# ── the precedence, walked as data ─────────────────────────────────────────

#: One model per level, strongest first. `system_policy` is absent because it
#: is not caller-settable — it is the one level a request may never state, which
#: is what makes it the top of the ladder.
LADDER = [
    ("owner_policy", "gpt-oss:120b"),
    ("project_restriction", "phi4:14b"),
    ("activity_policy", "llama3:8b"),
    ("task_override", "mistral:7b"),
    ("agent_default", "qwen3-coder:30b"),
    ("project_default", "gemma3:27b"),
]


def _with_levels(levels):
    """Resolve with a model stated at each of `levels`."""
    stated = dict(levels)
    return resolve(
        owner_policy={"model": stated["owner_policy"]} if "owner_policy" in stated else {},
        project_defaults={
            **({"restrictions": {"model": stated["project_restriction"]}}
               if "project_restriction" in stated else {}),
            **({"model": stated["project_default"]} if "project_default" in stated else {}),
        },
        activity={"model": stated["activity_policy"]} if "activity_policy" in stated else {},
        task={"model": stated["task_override"]} if "task_override" in stated else {},
        defs=catalogue(auditor(model=stated.get("agent_default", ""))),
    )


def test_six_levels_hand_the_decision_down_in_precedence_order():
    """Remove the winner and the next level decides — six times, in the order
    `PRECEDENCE` lists. A chain of `if`s could pass one of these; only a walk
    over the tuple passes all six."""
    remaining = list(LADDER)
    while remaining:
        resolution = _with_levels(remaining)
        level, model = remaining[0]
        assert resolution.model_route.model == model, (level, resolution.model_route.model)
        assert row_for(resolution, "model").level == level
        remaining = remaining[1:]


def test_the_trace_names_the_levels_that_lost_and_why():
    resolution = _with_levels(LADDER)
    row = row_for(resolution, "model")
    assert row.level == "owner_policy"
    lost = {level for level, _value, _why in row.discarded}
    # Every level below the winner that stated a model is accounted for. A
    # value that vanished without a line is one nobody can chase.
    assert {"task_override", "agent_default", "project_default"} <= lost
    assert all(why for _level, _value, why in row.discarded)


def test_every_level_in_a_trace_is_a_level_the_precedence_declares():
    resolution = _with_levels(LADDER)
    known = set(PRECEDENCE) | {"", "pinned"}
    for row in resolver.trace_of(resolution):
        for part in row.level.split(" + "):
            assert part in known, part


def test_explain_prints_the_value_its_level_and_what_was_discarded():
    text = resolver.explain(_with_levels(LADDER))
    assert "gpt-oss:120b" in text
    assert "owner_policy" in text
    assert "discarded" in text and "mistral:7b" in text


# ── §15: the six things an override may not do ─────────────────────────────
#
# One test each, and every one of them asserts the same two things: the value
# did NOT move, and a caveat says why. A prohibition that holds silently is a
# prohibition its user will keep tripping over.

def test_an_override_cannot_remove_a_deny():
    resolution = resolve(task={"deny": []})
    assert "write_file" in resolution.permissions.deny
    resolution = resolve(task={"tools": ["read_file", "write_file"]})
    assert "write_file" in resolution.permissions.deny
    assert not resolution.permissions.permits("write_file")
    assert caveat_about(resolution, "write_file", "allowlist")


def test_an_override_cannot_widen_a_work_root():
    resolution = resolve(project_defaults={"work_roots": ["D:/repo/src"]},
                         task={"work_roots": ["D:/repo", "C:/somewhere-else"]})
    assert resolution.permissions.work_roots == ("D:/repo/src",)
    assert caveat_about(resolution, "work_roots", "C:/somewhere-else")
    assert not resolution.permissions.permits_root("C:/somewhere-else/file.py")


def test_an_override_cannot_add_an_effect():
    resolution = resolve(task={"effects": ["read", "write", "network"]})
    assert resolution.permissions.effects == ("read",)
    assert not resolution.permissions.permits_effect("write")
    assert caveat_about(resolution, "effects", "write")


def test_an_override_cannot_enable_a_tool_outside_the_allowlist():
    resolution = resolve(task={"tools": ["read_file", "bash"]})
    assert resolution.permissions.tools == ("read_file",)
    assert not resolution.permissions.permits("bash")
    assert caveat_about(resolution, "bash", "allowlist")


def test_an_override_cannot_weaken_a_blocking_verification():
    """`security_review_v1` blocks on four checks; `default` blocks on one.
    Choosing the weaker one is refused, and choosing a stronger one is not."""
    resolution = resolve(task={"verification_profile": "default"})
    assert resolution.profiles.verification == "security_review_v1"
    assert caveat_about(resolution, "verification_profile", "stronger")

    stronger = resolve(defs=catalogue(auditor(verification_profile="default")),
                       task={"verification_profile": "full_delivery_v1"})
    assert stronger.profiles.verification == "full_delivery_v1"


def test_an_override_cannot_exceed_the_budget_ceiling():
    """Two ceilings and a budget profile, all in the one direction §15 allows."""
    resolution = resolve(task={"max_rounds": 400, "timeout_s": 7000,
                               "budget_profile": "max_quality_v1"})
    assert resolution.max_rounds == 12
    assert resolution.timeout_s == 1800
    assert resolution.profiles.budget == "reviewer_standard_v1"
    assert caveat_about(resolution, "max_rounds", "reduce")
    assert caveat_about(resolution, "budget_profile", "stronger")

    narrowed = resolve(task={"max_rounds": 4, "budget_profile": "fast_local_v1"})
    assert narrowed.max_rounds == 4
    assert narrowed.profiles.budget == "fast_local_v1"


def test_an_override_cannot_cross_the_provider_boundary():
    """The sixth prohibition on its other axis: a route this machine cannot
    take, and one it has quarantined."""
    unreachable = resolve(task={"model": "claude-4-opus"},
                          available_models=("qwen3-coder:30b",))
    assert unreachable.model_route.model == "qwen3-coder:30b"
    assert caveat_about(unreachable, "model", "claude-4-opus")

    sick = resolve(task={"model": "gpt-oss:120b"}, quarantined=("gpt-oss:120b",))
    assert sick.model_route.model == "qwen3-coder:30b"
    assert caveat_about(sick, "quarantined")


def test_a_forbidden_override_never_raises_and_the_rest_of_it_still_applies():
    """The whole point of ignoring a field rather than refusing the call."""
    resolution = resolve(task={"tools": ["bash"], "max_rounds": 3,
                               "completion_mode": "literal"})
    assert resolution.max_rounds == 3
    assert resolution.completion.mode == "literal"
    assert resolution.permissions.tools == ()
    assert caveat_about(resolution, "bash")


# ── §3.3: a completion mode grants nothing ─────────────────────────────────

def test_maximalist_on_a_read_only_auditor_grants_exactly_what_professional_did():
    """The property the whole separation exists for: a deeper mission is a
    deeper mission, not a wider one."""
    professional = resolve(task={"completion_mode": "professional"})
    maximalist = resolve(task={"completion_mode": "maximalist"})
    assert professional.completion.mode == "professional"
    assert maximalist.completion.mode == "maximalist"
    assert maximalist.permissions.to_dict() == professional.permissions.to_dict()
    assert not maximalist.permissions.permits("write_file")
    assert not maximalist.permissions.permits_effect("write")
    assert maximalist.max_rounds == professional.max_rounds
    assert maximalist.profiles.to_dict() == professional.profiles.to_dict()


def test_the_permission_levels_do_not_get_to_set_a_completion_mode():
    """§6 leaves system/owner/project restrictions out of the mode ladder: they
    gate authority, and a mode is not authority. A level that states one is
    ignored and says so."""
    resolution = resolve(owner_policy={"completion_mode": "maximalist"},
                         task={"completion_mode": "literal"})
    assert resolution.completion.mode == "literal"
    assert caveat_about(resolution, "completion_mode", "owner_policy")


def test_an_explicit_instruction_is_read_at_the_task_level_and_says_so():
    resolution = resolve(instruction="solo cambia esa línea, nada mas")
    assert resolution.completion.mode == "literal"
    assert caveat_about(resolution, "instruction")


def test_an_ambiguous_instruction_defers_to_the_ladder():
    resolution = resolve(instruction="solo cambia esto pero dame opciones")
    assert resolution.completion.mode == "professional"    # the definition's own
    assert resolution.completion.source == "agent_default"


# ── §20: a pinned run does not change because a file did ───────────────────

def test_a_snapshot_round_trips_to_the_same_configuration():
    resolution = resolve(scope={"owner": "luis", "project_id": "faustus", "run_id": "r1"})
    raw = resolver.snapshot(resolution)
    back = resolver.rehydrate(raw, defs=catalogue(auditor()))
    assert back.identity() == resolution.identity()
    assert back.to_dict()["permissions"] == resolution.to_dict()["permissions"]
    assert back.caveats == resolution.caveats


def test_rehydrating_a_snapshot_whose_definition_changed_warns_and_does_not_re_resolve():
    """The failure this exists to prevent: a nightly job that quietly starts
    doing something else because somebody improved an agent at four in the
    afternoon."""
    resolution = resolve()
    raw = resolver.snapshot(resolution)
    edited = auditor(tools=("read_file", "ls", "glob", "grep", "bash"),
                     deny=("write_file",), max_rounds=40)
    back = resolver.rehydrate(raw, defs=catalogue(edited))
    assert "bash" not in back.permissions.tools          # the PINNED envelope
    assert back.max_rounds == 12
    assert back.identity() == resolution.identity()
    warning = caveat_about(back, "auditor", "edited")
    assert warning and "PINNED" in warning


def test_rehydrating_a_snapshot_whose_definition_vanished_says_so():
    raw = resolver.snapshot(resolve())
    back = resolver.rehydrate(raw, defs={})
    assert caveat_about(back, "auditor", "not a definition this build knows")


def test_a_snapshot_carries_the_trace_so_explain_survives_the_process():
    resolution = _with_levels(LADDER)
    raw = resolver.snapshot(resolution)
    assert raw["schema"] == resolver.SNAPSHOT_SCHEMA
    resolver._TRACES.clear()                              # as if it came off disk
    back = resolver.rehydrate(raw, defs=catalogue(auditor()))
    assert "owner_policy" in resolver.explain(back)


def test_a_resolution_read_back_with_no_trace_says_pinned_rather_than_guessing():
    resolution = resolve()
    body = resolution.to_dict()
    resolver._TRACES.clear()
    back = ResolvedAgentExecution.parse(body)
    text = resolver.explain(back)
    assert "pinned" in text
    assert "read back from a record" in text


# ── §21/§23: the prompt is a digest, never the text ────────────────────────

def test_the_prompt_never_appears_in_the_snapshot():
    resolution = resolve(instruction="audit " + SECRET)
    raw = json.dumps(resolver.snapshot(resolution))
    assert SECRET not in raw
    assert "adversarial auditor" not in raw
    assert resolution.prompt_digest.startswith("sha256:")


def test_the_digest_proves_which_prompt_the_run_started_from():
    definition = auditor()
    resolution = resolve(instruction="find the injection")
    assert resolution.prompt_digest == sha256_digest(
        "{}\n\n{}".format(definition.prompt, "find the injection"))
    other = resolve(instruction="find the other injection")
    assert other.prompt_digest != resolution.prompt_digest


# ── §1.3: an absent integration degrades with a reason ─────────────────────

def test_capability_health_is_declared_degraded_rather_than_invented():
    resolution = resolve()
    named = [d for d in resolution.degraded_integrations if d.startswith("capability_health")]
    assert named and "Immune System" in named[0]
    assert not any("healthy" == d for d in resolution.degraded_integrations)


# ── the hot path never raises ──────────────────────────────────────────────

class Landmine:
    """A definition that explodes on every attribute but its slug."""

    slug = "landmine"

    def __getattr__(self, name):
        raise RuntimeError("this definition is broken: " + name)


def test_resolve_returns_a_minimal_resolution_instead_of_raising():
    resolution = resolver.resolve(agent="landmine", defs={"landmine": Landmine()},
                                  scope={"owner": "luis"})
    assert isinstance(resolution, ResolvedAgentExecution)
    assert resolution.agent.slug == "landmine"
    assert resolution.completion.mode in ("literal", "professional", "greedy", "maximalist")
    # It grants NOTHING: an empty envelope is the safe direction to be wrong in.
    assert resolution.permissions == PermissionEnvelope()
    assert any("MINIMAL" in c for c in resolution.caveats)
    assert resolution.execution_scope.owner == "luis"
    # And it still round-trips, so a failure can be recorded like any other run.
    assert ResolvedAgentExecution.parse(resolution.to_dict()).agent.slug == "landmine"


def test_resolve_with_an_empty_store_says_so_instead_of_inventing_an_agent():
    resolution = resolver.resolve(agent="nobody", defs={})
    assert resolution.agent.slug in ("nobody", "unresolved")
    assert resolution.permissions == PermissionEnvelope()
    assert resolution.caveats


def test_an_unknown_field_in_a_task_is_reported_rather_than_dropped():
    resolution = resolve(task={"completion_moed": "literal"})
    assert caveat_about(resolution, "completion_moed")
    assert resolution.completion.mode == "professional"


# ── narrowing an already-resolved execution ────────────────────────────────

def test_apply_task_override_narrows_and_keeps_refusing_what_widens():
    base = resolve()
    narrowed = resolver.apply_task_override(
        base, {"max_rounds": 5, "completion_mode": "literal", "deny": ["grep"],
               "tools": ["read_file", "bash"], "verification_profile": "default"})
    assert narrowed.max_rounds == 5
    assert narrowed.completion.mode == "literal"
    assert "grep" in narrowed.permissions.deny
    assert "bash" not in narrowed.permissions.tools
    assert narrowed.profiles.verification == "security_review_v1"
    assert narrowed.resolution_id != base.resolution_id
    assert caveat_about(narrowed, "narrowed from", base.resolution_id)
    assert base.max_rounds == 12                          # the original is untouched


def test_apply_task_override_never_raises():
    base = resolve()
    assert resolver.apply_task_override(base, {"max_rounds": "many"}).max_rounds == 12
    assert resolver.apply_task_override(base, None).identity() is not None
