"""The profile fields on an agent definition (src/agent_defs.py, §4-§5, §16, §22).

What is being pinned, in the order it matters:

* **a definition written before any of this still resolves exactly as it did.**
  The three built-ins are compared field by field against what they meant
  before the fields existed, prompt included. Backwards compatibility is a
  test here, not an intention;
* **an unknown frontmatter key does not pass in silence.** It used to be
  dropped, which meant a file that spelled `capabilties:` loaded, looked right
  on the page and was selected for nothing;
* **`extends` can only restrict.** The parent's denies survive, its allowlist
  cannot be widened, a cycle is refused with the chain named, and a fourth
  parent is refused with the depth named;
* **`prompt_append` appends.** A child never silently replaces the prompt it
  inherited, and a `prompt_append` that this frontmatter dialect would eat is
  refused rather than lost;
* **`revision_of` follows content, not spelling.** Reordering two keys is the
  same definition; reordering two permission rules is NOT, because those are
  last-match-wins and a reordered list is a different policy.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_defs as defs                                    # noqa: E402
from src.agent_profiles import contracts as profiles                  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A disposable DATA_DIR/agents, as tests/test_agent_defs.py does it."""
    monkeypatch.setattr(defs, "DATA_DIR", str(tmp_path))
    return tmp_path / "agents"


def _write(store, slug, text):
    folder = store / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / defs.DEF_FILENAME).write_text(text, encoding="utf-8")
    return folder / defs.DEF_FILENAME


FULL = """---
name: Security reviewer
description: Reviews code and contracts adversarially without modifying them.
mode: reviewer
tools: [read_file, ls, glob, grep, todowrite]
deny: [write_file, edit_file, apply_patch, bash, python]
permission:
  - "deny write **"
default_completion_mode: professional
capabilities: [code, review, security]
specialties: [python, FastAPI, "pdf extraction"]
tags: [audit]
preferred_tasks: [review, "threat model"]
avoid_tasks: [creative_generation]
verification_profile: security_review_v1
context_profile: code_review_v1
budget_profile: reviewer_standard_v1
collaboration_profile: independent_reviewer_v1
output_contract: review_findings_v1
---

You are an adversarial security reviewer.
"""


# ── the new fields ──────────────────────────────────────────────────────────

def test_the_profile_fields_parse_and_are_normalised(store):
    _write(store, "sec", FULL)
    d = defs.get("sec")
    assert d is not None
    assert d.default_completion_mode == "professional"
    assert d.capabilities == ("code", "review", "security")
    # lowercased, spaces and dashes folded to underscores: a catalogue where
    # `FastAPI` and `fastapi` are two specialities matches neither.
    assert d.specialties == ("python", "fastapi", "pdf_extraction")
    assert d.preferred_tasks == ("review", "threat_model")
    assert d.avoid_tasks == ("creative_generation",)
    assert d.tags == ("audit",)
    assert d.verification_profile == "security_review_v1"
    assert d.context_profile == "code_review_v1"
    assert d.budget_profile == "reviewer_standard_v1"
    assert d.collaboration_profile == "independent_reviewer_v1"
    assert d.output_contract == "review_findings_v1"
    # and out through the same emitter, unchanged
    again = defs.parse(defs.to_markdown(d), slug="sec")
    assert again.to_dict() == dict(d.to_dict(), path="", source=again.source)


def test_the_new_vocabulary_has_exactly_one_definition():
    # Two lists of completion modes in one codebase is how a mode ends up
    # valid in the parser and unknown to the resolver.
    assert profiles.AGENT_MODES == defs.MODES
    assert defs.DEFAULT_COMPLETION_MODE in profiles.COMPLETION_MODES


def test_an_unknown_completion_mode_is_refused_by_name(store):
    _write(store, "bad", "---\nname: b\ndefault_completion_mode: thorough\n---\nbody\n")
    result = defs.load_all()
    assert "bad" not in result.by_slug()
    reason = [e["reason"] for e in result.errors if e["slug"] == "bad"][0]
    assert "thorough" in reason and "maximalist" in reason


def test_an_unknown_capability_is_refused_rather_than_dropped(store):
    _write(store, "bad", "---\nname: b\ncapabilities: [code, secutiry]\n---\nbody\n")
    reason = [e["reason"] for e in defs.load_all().errors if e["slug"] == "bad"][0]
    assert "secutiry" in reason and "security" in reason


def test_a_profile_id_that_could_not_be_routed_is_refused(store):
    _write(store, "bad", "---\nname: b\nverification_profile: ../../etc/passwd\n---\nbody\n")
    reason = [e["reason"] for e in defs.load_all().errors if e["slug"] == "bad"][0]
    assert "verification_profile" in reason


# ── an unknown key is an error, never a default ────────────────────────────

def test_an_unknown_key_does_not_pass_in_silence_and_names_the_nearest_one(store):
    _write(store, "good", FULL)
    _write(store, "typo", "---\nname: t\nmode: worker\ncapabilties: [code]\n---\nbody\n")
    result = defs.load_all()
    assert "typo" not in result.by_slug()          # it does NOT come into effect
    assert "good" in result.by_slug()              # and one bad file takes out only itself
    reason = [e["reason"] for e in result.errors if e["slug"] == "typo"][0]
    assert "capabilties" in reason and "capabilities" in reason


def test_every_key_the_page_can_write_is_a_key_the_parser_accepts(store):
    _write(store, "sec", FULL)
    emitted = defs.to_markdown(defs.get("sec"))
    keys = [line.split(":", 1)[0] for line in emitted.splitlines() if ":" in line and
            not line.startswith(("-", " ", "\t")) and not line.startswith("---")]
    assert set(keys) <= set(defs.FRONTMATTER_KEYS)


# ── inheritance ────────────────────────────────────────────────────────────

PARENT = """---
name: Builder
description: Implements and verifies.
mode: worker
tools: [read_file, grep, apply_patch, bash]
deny: [web_search]
permission:
  - "deny write docs/**"
  - "allow write src/**"
default_completion_mode: professional
capabilities: [code, testing]
specialties: [python]
verification_profile: full_delivery_v1
max_rounds: 20
---

Read before you write.
"""


def test_extends_accumulates_denies_and_keeps_what_the_child_never_said(store):
    _write(store, "builder", PARENT)
    _write(store, "fastapi-builder", """---
name: FastAPI builder
extends: builder
specialties: [fastapi]
deny: [python]
prompt_append: "Follow the repository's FastAPI conventions."
---

Prefer dependency injection.
""")
    child = defs.load_all().by_slug()["fastapi-builder"]
    # denies accumulate; neither side loses one
    assert set(child.deny) == {"web_search", "python"}
    # the parent's rules survive, in order, with the child's after them
    assert [r.as_text() for r in child.permission] == ["deny write docs/**", "allow write src/**"]
    # what the child never mentioned is the parent's
    assert child.mode == "worker" and child.max_rounds == 20
    assert child.default_completion_mode == "professional"
    assert child.verification_profile == "full_delivery_v1"
    assert child.tools == ("read_file", "grep", "apply_patch", "bash")
    # descriptive lists append
    assert child.capabilities == ("code", "testing")
    assert child.specialties == ("python", "fastapi")
    # the chain is visible, on the object and in a caveat
    assert child.inherits == ("builder",)
    assert any("materialised from builder" in c for c in child.caveats)


def test_a_child_cannot_widen_its_parents_allowlist(store):
    _write(store, "builder", PARENT)
    _write(store, "greedier", """---
name: Greedier
extends: builder
tools: [read_file, write_file]
---

body
""")
    result = defs.load_all()
    assert "greedier" not in result.by_slug()
    reason = [e["reason"] for e in result.errors if e["slug"] == "greedier"][0]
    assert "write_file" in reason and "builder" in reason
    # narrowing is fine
    _write(store, "greedier", "---\nname: G\nextends: builder\ntools: [read_file]\n---\nbody\n")
    assert defs.load_all().by_slug()["greedier"].tools == ("read_file",)


def test_a_child_cannot_reopen_a_deny_it_inherited(store):
    _write(store, "builder", PARENT)
    _write(store, "docwriter", """---
name: Doc writer
extends: builder
permission:
  - "allow write docs/api/**"
---

body
""")
    reason = [e["reason"] for e in defs.load_all().errors if e["slug"] == "docwriter"][0]
    assert "docs/api/**" in reason and "deny write docs/**" in reason


def test_a_cycle_is_refused_with_the_chain_named(store):
    _write(store, "alpha", "---\nname: a\nextends: beta\n---\nbody\n")
    _write(store, "beta", "---\nname: b\nextends: alpha\n---\nbody\n")
    result = defs.load_all()
    assert "alpha" not in result.by_slug() and "beta" not in result.by_slug()
    reasons = {e["slug"]: e["reason"] for e in result.errors}
    assert "alpha -> beta -> alpha" in reasons["alpha"]
    assert "beta -> alpha -> beta" in reasons["beta"]


def test_a_definition_that_extends_itself_is_refused(store):
    _write(store, "solo", "---\nname: s\nextends: solo\n---\nbody\n")
    reason = [e["reason"] for e in defs.load_all().errors if e["slug"] == "solo"][0]
    assert "extends itself" in reason


def test_an_unknown_parent_is_refused_with_the_chain(store):
    _write(store, "orphan", "---\nname: o\nextends: nobody\n---\nbody\n")
    reason = [e["reason"] for e in defs.load_all().errors if e["slug"] == "orphan"][0]
    assert "nobody" in reason and "orphan -> nobody" in reason


def test_three_parents_resolve_and_a_fourth_is_refused_by_depth(store):
    _write(store, "gen0", "---\nname: g0\nmode: worker\ntools: [read_file, grep]\n---\nroot\n")
    for i in range(1, 5):
        _write(store, f"gen{i}",
               f"---\nname: g{i}\nextends: gen{i - 1}\n---\nstep {i}\n")
    result = defs.load_all()
    assert result.by_slug()["gen3"].inherits == ("gen2", "gen1", "gen0")
    assert "gen4" not in result.by_slug()
    reason = [e["reason"] for e in result.errors if e["slug"] == "gen4"][0]
    assert "gen4 -> gen3 -> gen2 -> gen1 -> gen0" in reason
    assert str(defs.MAX_EXTENDS_DEPTH) in reason


def test_the_prompt_is_appended_and_never_replaced(store):
    _write(store, "builder", PARENT)
    _write(store, "child", """---
name: Child
extends: builder
prompt_append: "Then run the FastAPI test module."
---

Prefer dependency injection.
""")
    child = defs.load_all().by_slug()["child"]
    assert child.prompt == ("Read before you write.\n\n"
                            "Prefer dependency injection.\n\n"
                            "Then run the FastAPI test module.")
    # and the append is folded in exactly once, whatever the chain length
    assert child.prompt.count("Then run the FastAPI test module.") == 1


def test_prompt_append_refuses_the_two_ways_its_text_would_be_lost(store):
    _write(store, "builder", PARENT)
    _write(store, "blocky", "---\nname: b\nextends: builder\nprompt_append: |\n  some text\n---\nbody\n")
    _write(store, "lonely", "---\nname: l\nprompt_append: \"orphan text\"\n---\nbody\n")
    reasons = {e["slug"]: e["reason"] for e in defs.load_all().errors}
    assert "block scalar" in reasons["blocky"]
    assert "extends" in reasons["lonely"]


def test_resolve_extends_answers_for_one_slug_and_raises_the_reason(store):
    _write(store, "builder", PARENT)
    _write(store, "child", "---\nname: c\nextends: builder\nspecialties: [fastapi]\n---\nbody\n")
    resolved = defs.resolve_extends("child")
    assert resolved.inherits == ("builder",) and resolved.mode == "worker"
    # materialising an already materialised definition is a no-op, not a
    # second fold of the parent
    assert defs.resolve_extends("child", defs=defs.load_all()).prompt == resolved.prompt
    with pytest.raises(defs.AgentDefError) as caught:
        defs.resolve_extends("no-such-agent")
    assert "no-such-agent" in str(caught.value)


# ── the revision ───────────────────────────────────────────────────────────

BASE_REV = """---
name: rev
mode: worker
tools: [read_file, grep]
permission:
  - "deny write **"
  - "allow write src/**"
max_rounds: 9
---

Do the thing.
"""


def test_a_revision_follows_content_and_not_the_order_of_two_keys(store):
    _write(store, "rev", BASE_REV)
    original = defs.revision_of(defs.get("rev"))
    assert original.startswith("sha256:") and len(original) == 71
    reordered = """---
mode: worker
max_rounds: 9
tools: [read_file, grep]
permission:
  - "deny write **"
  - "allow write src/**"
name: rev
---

Do the thing.
"""
    _write(store, "rev", reordered)
    assert defs.revision_of(defs.get("rev")) == original
    # content changes: a different revision, so a queued run can tell
    _write(store, "rev", BASE_REV.replace("max_rounds: 9", "max_rounds: 10"))
    assert defs.revision_of(defs.get("rev")) != original
    _write(store, "rev", BASE_REV.replace("Do the thing.", "Do the other thing."))
    assert defs.revision_of(defs.get("rev")) != original


def test_reordering_two_permission_rules_is_a_different_revision(store):
    _write(store, "rev", BASE_REV)
    original = defs.revision_of(defs.get("rev"))
    _write(store, "rev", BASE_REV.replace(
        '  - "deny write **"\n  - "allow write src/**"',
        '  - "allow write src/**"\n  - "deny write **"'))
    # last match wins, so this really is a different policy
    assert defs.revision_of(defs.get("rev")) != original


def test_where_a_definition_came_from_is_not_part_of_its_revision(store):
    _write(store, "rev", BASE_REV)
    user_copy = defs.get("rev")
    builtin_copy = defs.parse(BASE_REV, slug="rev", source=defs.SOURCE_BUILTIN, path="")
    assert defs.revision_of(user_copy) == defs.revision_of(builtin_copy)
    assert user_copy.source != builtin_copy.source


# ── nothing here changes a definition that does not use it ─────────────────

LEGACY = {
    "reviewer": {
        "mode": "reviewer", "model": "", "endpoint_id": "", "runner": "",
        "tools": ["read_file", "ls", "glob", "grep", "todowrite"],
        "deny": ["write_file", "edit_file", "apply_patch", "bash", "python"],
        "permission": ["deny write **", "deny delegate *", "allow read **"],
        "files": [], "max_rounds": 12, "timeout_s": None,
        "may_delegate": False, "caveats": [],
    },
    "planner": {
        "mode": "coordinator", "model": "", "endpoint_id": "", "runner": "",
        "tools": ["read_file", "ls", "glob", "grep", "delegate_agents", "todowrite",
                  "update_plan"],
        "deny": ["write_file", "edit_file", "apply_patch"],
        "permission": ["deny write **", "allow read **", "allow delegate *"],
        "files": [], "max_rounds": 16, "timeout_s": None,
        "may_delegate": True, "caveats": [],
    },
    "implementer": {
        "mode": "worker", "model": "", "endpoint_id": "", "runner": "",
        "tools": ["read_file", "ls", "glob", "grep", "write_file", "edit_file", "apply_patch",
                  "bash", "python", "todowrite"],
        "deny": [],
        "permission": ["deny delegate *", "allow read **", "allow write **"],
        "files": [], "max_rounds": 20, "timeout_s": 1500,
        "may_delegate": False, "caveats": [],
    },
}

NEW_KEYS = {"default_completion_mode", "capabilities", "specialties", "tags", "preferred_tasks",
            "avoid_tasks", "verification_profile", "context_profile", "budget_profile",
            "collaboration_profile", "output_contract", "extends", "prompt_append", "inherits"}
OLD_KEYS = {"slug", "name", "description", "mode", "model", "endpoint_id", "runner", "tools",
            "deny", "permission", "files", "max_rounds", "timeout_s", "prompt", "source", "path",
            "may_delegate", "caveats"}


def test_the_builtins_resolve_exactly_as_they_did_before_the_profile_fields():
    catalogue = {d.slug: d for d in defs.builtins()}
    assert set(catalogue) == set(LEGACY)
    for slug, pinned in LEGACY.items():
        d = catalogue[slug]
        got = d.to_dict()
        assert [r.as_text() for r in d.permission] == pinned["permission"], slug
        for key, value in pinned.items():
            if key == "permission":
                continue
            assert got[key] == value, f"{slug}.{key}"
        # the prompt is still the body of the file, byte for byte
        assert d.prompt == defs.BUILTIN_SOURCES[slug].split("---", 2)[2].strip(), slug
        # and every field this plan added is at its default, so a definition
        # that does not use them cannot behave differently because they exist
        assert d.default_completion_mode == defs.DEFAULT_COMPLETION_MODE
        assert d.capabilities == d.specialties == d.tags == () == d.preferred_tasks
        assert d.avoid_tasks == () and d.inherits == ()
        assert d.verification_profile == "default" and d.context_profile == "default"
        assert d.budget_profile == "default" and d.collaboration_profile == "default"
        assert d.output_contract == "" and d.extends == "" and d.prompt_append == ""


def test_the_payload_a_task_carries_gains_the_new_fields_and_loses_nothing(store):
    task = {"agent": "implementer", "name": "t", "instruction": "do it"}
    assert defs.resolve_task(task) is None
    assert set(task) == {"agent", "name", "instruction", "agent_def", "system_prompt",
                         "max_rounds", "timeout_s"}
    assert set(task["agent_def"]) == OLD_KEYS | NEW_KEYS
    # and it survives the trip back through the dispatch payload.
    # Compared against what the definition itself declares, not against the
    # module default: `implementer` is now shipped as the literal-mode surgeon
    # profile, and asserting the default here would only be testing that no
    # definition ever declares a mode.
    rebuilt = defs.from_dict(task["agent_def"])
    assert rebuilt.default_completion_mode == defs.get("implementer").default_completion_mode
    assert rebuilt.default_completion_mode in defs.COMPLETION_MODES
    assert rebuilt.to_dict() == task["agent_def"]


def test_a_definition_from_a_payload_keeps_the_profile_fields(store):
    _write(store, "sec", FULL)
    original = defs.get("sec")
    rebuilt = defs.from_dict(original.to_dict())
    assert rebuilt.to_dict() == original.to_dict()
    assert defs.revision_of(rebuilt) == defs.revision_of(original)
