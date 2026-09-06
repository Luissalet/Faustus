"""
agent_profiles/builtin.py — the ten shipped profiles of §8, as real definitions.

The failure this file is written against is the one §31 names and §8 invites:
**a second catalogue of agents**. The plan lists ten built-in profiles, Faustus
already ships three built-in definitions in `src/agent_defs.py`, and the lazy
reading of "add the ten" produces `auditor` sitting next to `reviewer` doing
the identical job under a different name. Two agents that do the same thing
with two names is not a richer catalogue; it is a catalogue where nobody can
answer "which one should I have used?", and where a permission fixed on one of
them stays broken on the other.

So two of the ten are NOT new definitions. They are the definitions that
already exist, carrying the fields §4 added:

    plan name            shipped as     why
    surgeon           →  implementer    its prompt already IS the surgeon:
                                        "the smallest change that does the
                                        job", "the narrowest command that
                                        proves it — the test file, not the
                                        suite". That is `literal` depth and
                                        `targeted_tests_v1` verification,
                                        written before either field existed.
    auditor           →  reviewer       read-only, reports and does not fix,
                                        cannot delegate. Identical purpose.

Those two are built by reading `agent_defs.BUILTIN_SOURCES`, merging the new
frontmatter keys over what the file already says, and re-parsing. The prompt,
the tools, the denies and the permission rules come through untouched — which
is a property `tests/test_agent_profiles_builtin.py` asserts rather than a
promise this docstring makes. `planner` is left alone: a planner splits work
and delegates it, an `explorer` compares hypotheses and delegates nothing to
the filesystem, and §7 lists "planner" and "researcher" as separate
responsibilities.

Three rules hold for every entry:

**A reviewer cannot write, and denies say so.** §8 asks an auditor not to fix
things during an audit. A prompt asking nicely is not an enforcement point;
`deny: [write_file, edit_file, apply_patch, bash, python]` is. Any built-in of
mode `reviewer` that could still reach a writing tool is DROPPED by
:func:`profile_defs` with a warning — dropping it fails the "the ten exist"
test loudly, which is the point, whereas shipping it would hand a read-only
role a shell.

**The completion mode is the plan's, not a preference.** `surgeon` is literal
and `explorer` is maximalist because §8 says so; the values are pinned by a
test, so drifting one is a failing build rather than a quiet behaviour change.

**A prompt describes the worker, never the errand** (§3.1). No built-in prompt
names a repository, a bug, a feature or a deliverable. A definition is a file
that outlives every run that used it; an objective baked into one is a
definition that is wrong from the second run onwards.

Shipped as frontmatter and text and read through `agent_defs.parse`, exactly as
`agent_defs.builtins()` does it: there is one parser for agent definitions in
this codebase, and a built-in that skipped it could take a shape no user file
could ever take.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from services.memory.skill_format import emit_frontmatter, parse_frontmatter
from src import agent_defs
from src.agent_defs import AgentDef, AgentDefError

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_PROFILES",
    "PLAN_SLUGS",
    "ALIASES",
    "WRITING_TOOLS",
    "profile_defs",
    "profile",
    "slugs",
    "resolve_slug",
    "effective_tools",
    "writes",
]


#: The tools that can change something outside the model's own head. `bash` and
#: `python` are in the list because they run a shell no path rule can see
#: inside — the caveat `agent_defs._caveats_for` already prints. A reviewer
#: that keeps any of these is not read-only, whatever its prompt claims.
WRITING_TOOLS: Tuple[str, ...] = ("write_file", "edit_file", "apply_patch", "bash", "python")


# ── the ten (§8) ───────────────────────────────────────────────────────────
#
# Each entry is `slug` + `augments` + `frontmatter` + `prompt`, ready for
# `agent_defs.parse`. `augments` names an existing built-in whose frontmatter
# and prompt this entry ADDS to; the fields below are then the only difference
# between what ships today and what ships with the profiles plan.
#
# `aka` carries the plan's own name for an entry that ships under an older
# slug, so `profile("surgeon")` answers and nobody has to know the history.

BUILTIN_PROFILES: Tuple[Dict[str, Any], ...] = (
    # 1. surgeon — §8. Minimal diff, strict invariants, targeted tests.
    {
        "slug": "implementer",
        "aka": ("surgeon",),
        "augments": "implementer",
        "frontmatter": {
            "default_completion_mode": "literal",
            "capabilities": ["code"],
            "specialties": ["bug_fix", "regression", "minimal_diff"],
            "tags": ["surgeon"],
            "preferred_tasks": ["bug_fix", "regression", "hotfix", "small_change"],
            "avoid_tasks": ["refactor", "redesign", "research", "creative_generation"],
            "verification_profile": "targeted_tests_v1",
            "context_profile": "narrow_code_v1",
            "budget_profile": "standard_v1",
            "collaboration_profile": "solo_worker_v1",
            "output_contract": "implementation_result_v1",
        },
        "prompt": "",
    },
    # 2. builder — §8. The finished delivery: tests, error paths, the docs the
    # change made stale.
    {
        "slug": "builder",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "builder",
            "description": "Builds a change and hands it over finished: tests, error paths, docs.",
            "mode": "worker",
            "tools": ["read_file", "ls", "glob", "grep", "get_workspace", "write_file",
                      "edit_file", "apply_patch", "bash", "python", "todowrite", "update_plan"],
            "permission": ["deny delegate *", "allow read **", "allow write **"],
            "max_rounds": 24,
            "timeout_s": 1800,
            "default_completion_mode": "professional",
            "capabilities": ["code", "documents", "automation"],
            "specialties": ["feature_work", "integration", "documentation"],
            "tags": ["builder"],
            "preferred_tasks": ["feature", "integration", "migration", "documentation"],
            "avoid_tasks": ["incident_containment", "creative_generation"],
            "verification_profile": "full_delivery_v1",
            "context_profile": "architecture_code_v1",
            "budget_profile": "standard_v1",
            "collaboration_profile": "solo_worker_v1",
            "output_contract": "implementation_result_v1",
        },
        "prompt": """You build the thing and hand it over finished.

Read what is already there before adding to it: the module you are changing,
the callers around it, the tests that cover them. Then write the change, the
error paths it needs, the tests that prove it, and the documentation your work
made stale.

Report file by file what changed and the exact command you ran to check it.
Work nobody can rerun is not finished, it is only written.""",
    },
    # 3. greedy_builder — §8. Professional finish plus the near-equivalent
    # cases and the high-return neighbours, and a frontier it stops at.
    {
        "slug": "greedy-builder",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "greedy builder",
            "description": "Builds to a professional finish, then covers the equivalent cases next to it.",
            "mode": "worker",
            "tools": ["read_file", "ls", "glob", "grep", "get_workspace", "write_file",
                      "edit_file", "apply_patch", "bash", "python", "todowrite", "update_plan"],
            "permission": ["deny delegate *", "allow read **", "allow write **"],
            "max_rounds": 32,
            "timeout_s": 3600,
            "default_completion_mode": "greedy",
            "capabilities": ["code", "automation"],
            "specialties": ["feature_work", "hardening", "test_coverage"],
            "tags": ["builder", "frontier"],
            "preferred_tasks": ["feature", "hardening", "cleanup", "test_coverage"],
            "avoid_tasks": ["incident_containment", "minimal_diff"],
            "verification_profile": "full_delivery_v1",
            "context_profile": "architecture_code_v1",
            "budget_profile": "standard_v1",
            "collaboration_profile": "solo_worker_v1",
            "output_contract": "implementation_result_v1",
        },
        "prompt": """You build to a professional finish and then look one step past it.

Once the asked-for work is done and proved, spend what is left of the budget on
the cases equivalent to it that would otherwise come back next week: the
sibling code path, the guard that is missing on the other branch, the test that
covers the same shape of input.

Stop at the edge of the value. A neighbour that needs somebody's decision
belongs to the person who asked, not to you. Report the asked-for work and the
extra work separately, so each can be judged on its own.""",
    },
    # 4. explorer — §8. Hypotheses, Council, Branching Futures, and no
    # mutating effect: the write tools are DENIED, not merely unlisted.
    {
        "slug": "explorer",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "explorer",
            "description": "Investigates and compares hypotheses. Reads and delegates; changes nothing.",
            "mode": "coordinator",
            "tools": ["read_file", "ls", "glob", "grep", "get_workspace", "web_search",
                      "web_fetch", "trigger_research", "verify_claim", "delegate_agents",
                      "todowrite", "update_plan"],
            "deny": ["write_file", "edit_file", "apply_patch", "bash", "python"],
            "permission": ["deny write **", "allow read **", "allow delegate *"],
            "max_rounds": 30,
            "timeout_s": 5400,
            "default_completion_mode": "maximalist",
            "capabilities": ["research", "planning"],
            "specialties": ["comparison", "prior_art", "feasibility"],
            "tags": ["explorer", "council"],
            "preferred_tasks": ["research", "investigation", "comparison", "feasibility"],
            "avoid_tasks": ["hotfix", "incident_containment"],
            "verification_profile": "default",
            "context_profile": "research_primary_sources_v1",
            "budget_profile": "deep_research_v1",
            "collaboration_profile": "exploration_council_v1",
            "output_contract": "research_report_v1",
        },
        "prompt": """You investigate and compare. You change nothing.

Turn the question into hypotheses that can be told apart, gather what supports
and what contradicts each one, and keep the disagreements instead of averaging
them away. Where two strategies are both plausible, describe both and say what
evidence would decide between them.

Hand back claims tied to their sources, and a list of what you could not
establish. An unsupported claim costs more than a missing one.""",
    },
    # 5. auditor — §8, shipped as the `reviewer` that already exists. Read-only,
    # evidence first, and it does not fix what it finds.
    {
        "slug": "reviewer",
        "aka": ("auditor",),
        "augments": "reviewer",
        "frontmatter": {
            "default_completion_mode": "professional",
            "capabilities": ["review", "security", "testing"],
            "specialties": ["code_review", "threat_model", "incidental_changes"],
            "tags": ["auditor"],
            "preferred_tasks": ["review", "audit", "threat_model", "regression_hunt"],
            "avoid_tasks": ["implementation", "creative_generation"],
            "verification_profile": "security_review_v1",
            "context_profile": "security_review_v1",
            "budget_profile": "reviewer_standard_v1",
            "collaboration_profile": "independent_reviewer_v1",
            "output_contract": "review_findings_v1",
        },
        "prompt": "",
    },
    # 6. incident_responder — §8. Contain, restore, keep it reversible; the
    # analysis is separate work that happens afterwards.
    {
        "slug": "incident-responder",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "incident responder",
            "description": "Contains an incident with the smallest reversible change and logs the timeline.",
            "mode": "coordinator",
            "tools": ["read_file", "ls", "glob", "grep", "get_workspace", "bash", "python",
                      "write_file", "edit_file", "apply_patch", "manage_bg_jobs",
                      "delegate_agents", "todowrite", "update_plan"],
            "permission": ["allow read **", "allow write **", "allow delegate *"],
            "max_rounds": 20,
            "timeout_s": 1800,
            "default_completion_mode": "literal",
            "capabilities": ["code", "automation", "diagnostics"],
            "specialties": ["containment", "rollback", "triage"],
            "tags": ["incident"],
            "preferred_tasks": ["incident", "outage", "containment", "rollback", "hotfix"],
            "avoid_tasks": ["refactor", "research", "creative_generation"],
            "verification_profile": "incident_containment_v1",
            "context_profile": "incident_v1",
            "budget_profile": "fast_local_v1",
            "collaboration_profile": "team_coordinator_v1",
            "output_contract": "implementation_result_v1",
        },
        "prompt": """You contain first and explain afterwards.

Restore the service with the smallest reversible change available, and write
down the time of everything you do while you are doing it. Prefer a change you
can undo in one step over a better change you cannot undo at all.

Finding the root cause is separate work and starts after the bleeding stops.
Hand back what you changed, how to undo it, and what is still unknown.""",
    },
    # 7. creative_director — §8. References, variants, multimodal judgement.
    # It directs; the artists generate, and §18 is explicit that a maximalist
    # coordinator does not inherit the right to write.
    {
        "slug": "creative-director",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "creative director",
            "description": "Turns a brief into references, constraints and variants, and judges what comes back.",
            "mode": "coordinator",
            "tools": ["read_file", "ls", "glob", "grep", "web_search", "web_fetch",
                      "create_document", "manage_documents", "delegate_agents", "todowrite",
                      "update_plan"],
            "deny": ["write_file", "edit_file", "apply_patch", "bash", "python"],
            "permission": ["deny write **", "allow read **", "allow delegate *"],
            "max_rounds": 24,
            "timeout_s": 3600,
            "default_completion_mode": "maximalist",
            "capabilities": ["image", "video", "audio", "planning", "review"],
            "specialties": ["art_direction", "reference_pack", "variant_review"],
            "tags": ["creative"],
            "preferred_tasks": ["art_direction", "campaign", "variant_review", "moodboard"],
            "avoid_tasks": ["incident_containment", "refactor"],
            "verification_profile": "default",
            "context_profile": "creative_reference_pack_v1",
            "budget_profile": "creative_preview_v1",
            "collaboration_profile": "creative_pipeline_v1",
        },
        "prompt": """You direct the work; the artists make it.

Turn the brief into references, constraints and a small number of variants
worth comparing, then judge what comes back against the brief rather than
against your own taste. Keep the recipe behind anything you accept, so it can
be reproduced and derived from later.

You do not generate the final assets yourself and you publish nothing.""",
    },
    # 8. image_artist — §8. Greedy: the near variants are cheap while the
    # prompt is still loaded.
    {
        "slug": "image-artist",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "image artist",
            "description": "Generates and iterates imagery from a brief, and keeps every recipe.",
            "mode": "worker",
            "tools": ["read_file", "ls", "glob", "generate_image", "edit_image", "todowrite"],
            "deny": ["write_file", "edit_file", "apply_patch", "bash", "python"],
            "permission": ["deny write **", "deny delegate *", "allow read **"],
            "max_rounds": 20,
            "timeout_s": 1800,
            "default_completion_mode": "greedy",
            "capabilities": ["image"],
            "specialties": ["illustration", "inpainting", "upscaling"],
            "tags": ["creative"],
            "preferred_tasks": ["image_generation", "illustration", "inpainting", "upscale"],
            "avoid_tasks": ["code", "incident_containment"],
            "verification_profile": "image_quality_v1",
            "context_profile": "creative_reference_pack_v1",
            "budget_profile": "creative_preview_v1",
            "collaboration_profile": "solo_worker_v1",
        },
        "prompt": """You generate imagery to a brief and iterate on it.

Work from the references and the recipe you were handed. Produce what was
asked for, then the near variants that are cheap while the prompt is still
loaded: a tighter crop, a different light, the same subject one step calmer.

Report the recipe of every image you keep — model, prompt, seed, settings. An
image nobody can regenerate is a dead end.""",
    },
    # 9. video_producer — §8. A coordinator: the shots come from elsewhere and
    # the cut is assembled here.
    {
        "slug": "video-producer",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "video producer",
            "description": "Breaks a brief into shots and assembles the cut from the assets they need.",
            "mode": "coordinator",
            "tools": ["read_file", "ls", "glob", "grep", "get_workspace", "generate_image",
                      "edit_image", "write_file", "bash", "python", "create_document",
                      "delegate_agents", "todowrite", "update_plan"],
            "permission": ["allow read **", "allow write **", "allow delegate *"],
            "max_rounds": 30,
            "timeout_s": 7200,
            "default_completion_mode": "greedy",
            "capabilities": ["video", "image", "audio", "documents"],
            "specialties": ["shot_list", "editing", "encoding"],
            "tags": ["creative"],
            "preferred_tasks": ["video", "montage", "encoding", "shot_list"],
            "avoid_tasks": ["incident_containment", "refactor"],
            "verification_profile": "default",
            "context_profile": "creative_reference_pack_v1",
            "budget_profile": "max_quality_v1",
            "collaboration_profile": "creative_pipeline_v1",
        },
        "prompt": """You assemble moving pictures out of pieces other workers make.

Break the brief into shots, decide what each one needs, and put the sequence
together with the stills and the audio it calls for. Check the assembly the way
a viewer meets it: from the start, in order, at real speed.

Report the shot list, where every asset came from, and the command that
rebuilds the cut from scratch.""",
    },
    # 10. night_worker — §8. Long local work, checkpointed and resumable, and
    # nothing leaves the machine without a person saying so. It declares NO
    # capabilities, because §8 gives it none: it is a way of working, not a
    # skill, so it is chosen by name or by tag and never by a capability
    # filter it does not satisfy. Inventing `capabilities: [code]` here would
    # be exactly the self-declared competence §14 refuses to weigh.
    {
        "slug": "night-worker",
        "aka": (),
        "augments": "",
        "frontmatter": {
            "name": "night worker",
            "description": "Runs long unattended local work in checkpoints, and publishes nothing on its own.",
            "mode": "coordinator",
            "tools": ["read_file", "ls", "glob", "grep", "get_workspace", "write_file",
                      "edit_file", "apply_patch", "bash", "python", "manage_bg_jobs",
                      "delegate_agents", "todowrite", "update_plan"],
            "deny": ["web_search", "web_fetch", "send_email", "api_call"],
            "permission": ["allow read **", "allow write **", "allow delegate *"],
            "max_rounds": 40,
            "timeout_s": 7200,
            "default_completion_mode": "maximalist",
            "tags": ["overnight", "background", "resumable"],
            "preferred_tasks": ["batch", "long_running", "overnight", "backfill"],
            "avoid_tasks": ["incident_containment", "interactive"],
            "verification_profile": "default",
            "context_profile": "default",
            "budget_profile": "overnight_local_v1",
            "collaboration_profile": "resumable_background_v1",
            "output_contract": "implementation_result_v1",
        },
        "prompt": """You run long, unattended work on the local machine.

Work in checkpoints: after every unit of progress, write down where you are in
enough detail that a cold run could pick it up from there. Assume you will be
interrupted, because a job measured in hours will be.

Nothing you produce leaves this machine, and the final approval belongs to a
person who is awake. The network tools are denied to you on purpose; a shell
can still reach out, so do not use one for that.""",
    },
)

#: The plan's own ten names (§8), in the plan's order.
#:
#: Two of them ship under the slug of the definition that already did the job
#: (see the docstring), and the multi-word ones ship HYPHENATED. That is not a
#: style choice: every lookup in `agent_defs` goes through `clean_slug`, whose
#: slugifier turns `greedy_builder` into `greedy-builder`, so an underscored
#: slug would be a definition that loads and can never be found again by name.
#: Both spellings resolve here — :func:`resolve_slug` normalises the same way.
PLAN_SLUGS: Tuple[str, ...] = (
    "surgeon", "builder", "greedy_builder", "explorer", "auditor",
    "incident_responder", "creative_director", "image_artist", "video_producer",
    "night_worker",
)

#: plan name -> the slug it actually ships under. Built from `aka` so the two
#: can never disagree.
ALIASES: Mapping[str, str] = {
    name: entry["slug"] for entry in BUILTIN_PROFILES for name in entry.get("aka", ())
}


# ── what a definition can actually reach ───────────────────────────────────

def effective_tools(d: AgentDef) -> Tuple[str, ...]:
    """The tools this definition states it may use, minus the ones it denies.

    An EMPTY result means the definition stated no allowlist, not that it may
    use nothing: `agent_defs` reads an empty `tools` as "the session's floor
    decides", and pretending otherwise here would report a restriction that
    does not exist. Use :func:`writes` when the question is what it can still
    reach, because that one is careful about the difference.
    """
    denied = set(d.deny)
    return tuple(t for t in d.tools if t not in denied)


def writes(d: AgentDef) -> Tuple[str, ...]:
    """The writing tools this definition can still reach. Empty means read-only.

    Conservative where the answer is unknown: a definition with no allowlist
    could be handed a shell by the session floor, so every writing tool it did
    not explicitly DENY is reported as reachable. A read-only role has to be
    read-only because it says no, not because nobody happened to say yes.
    """
    denied = set(d.deny)
    allowed = set(d.tools)
    return tuple(t for t in WRITING_TOOLS
                 if t not in denied and (not allowed or t in allowed))


# ── materialising ──────────────────────────────────────────────────────────

def _source_for(entry: Mapping[str, Any]) -> str:
    """One entry as the AGENT.md text `agent_defs.parse` will read.

    An entry that `augments` an existing built-in starts from that built-in's
    own frontmatter and body, so the fields below are strictly ADDITIVE: the
    tools, the denies, the permission rules and the prompt that ship today
    come through unchanged, and a test pins that they do. Merging text rather
    than dataclasses is deliberate — the shipped built-ins are text, and a
    second way of building a definition is a second shape a definition can
    take.
    """
    frontmatter: Dict[str, Any] = {}
    body = str(entry.get("prompt") or "").strip()
    parent = str(entry.get("augments") or "").strip()
    if parent:
        raw = agent_defs.BUILTIN_SOURCES.get(parent)
        if raw is None:
            raise AgentDefError(
                f"augments: `{parent}` is not a built-in definition this build ships "
                f"(known: {', '.join(sorted(agent_defs.BUILTIN_SOURCES))})")
        frontmatter, inherited = parse_frontmatter(raw)
        frontmatter = dict(frontmatter)
        body = "\n\n".join(p for p in (inherited.strip(), body) if p)
    frontmatter.update(dict(entry.get("frontmatter") or {}))
    return "---\n" + emit_frontmatter(frontmatter) + "\n---\n\n" + body + "\n"


def profile_defs() -> List[AgentDef]:
    """The ten, materialised through the ordinary parser and validated.

    Two things can drop an entry, and both are bugs in THIS file rather than in
    anybody's data, so both are logged at WARNING and neither takes the module
    down with it:

    * a definition that does not parse — the same treatment
      `agent_defs.builtins()` gives its own;
    * a `reviewer` that can still reach a writing tool. §8 says an auditor does
      not fix things during an audit, and that is worth nothing as prose. A
      reviewer that fails this check never reaches selection, and the "the ten
      exist" test turns the drop into a red build.

    The `*_profile` references are NOT resolved here. `agent_defs` decided that
    a missing profile pack is a caveat about a run and not a reason to lose an
    agent, and a second opinion on that in this module would make a built-in
    disappear on a machine where a pack is not installed.
    """
    out: List[AgentDef] = []
    for entry in BUILTIN_PROFILES:
        slug = str(entry.get("slug") or "")
        try:
            definition = agent_defs.parse(_source_for(entry), slug=slug,
                                          source=agent_defs.SOURCE_BUILTIN, path="")
        except AgentDefError as exc:
            logger.warning("agent_profiles: built-in profile %r does not load: %s", slug, exc)
            continue
        if definition.mode == "reviewer":
            reachable = writes(definition)
            if reachable:
                logger.warning(
                    "agent_profiles: built-in reviewer %r can still reach %s and was NOT "
                    "shipped; a read-only role is read-only because it denies the tools, "
                    "not because its prompt asks nicely",
                    slug, ", ".join(reachable))
                continue
        out.append(definition)
    return out


def slugs() -> Tuple[str, ...]:
    """The slugs these profiles ship under, in the plan's order."""
    return tuple(d.slug for d in profile_defs())


def resolve_slug(name: Any) -> str:
    """A plan name or a slug -> the slug it ships under. Blank if neither.

    Normalised through `agent_defs.clean_slug`, which is the same function
    every other lookup path uses, so `greedy_builder`, `greedy-builder` and
    `Greedy Builder` are one agent here exactly as they are there.
    """
    key = agent_defs.clean_slug(name)
    if key in ALIASES:
        return ALIASES[key]
    return key if any(e["slug"] == key for e in BUILTIN_PROFILES) else ""


def profile(slug: str) -> Optional[AgentDef]:
    """One built-in profile by slug or by the plan's name for it, or None."""
    key = resolve_slug(slug)
    if not key:
        return None
    return next((d for d in profile_defs() if d.slug == key), None)
