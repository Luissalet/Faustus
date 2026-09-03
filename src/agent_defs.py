"""agent_defs.py — an agent as a FILE, so the head and the minions can be swapped.

    DATA_DIR/agents/<slug>/AGENT.md          the user's own definitions
    <workspace>/.faustus/agents/<slug>.md    the repo's, behind the trust gate
    BUILTINS                                 three shipped in this module

A dispatch task is five fields (`name, instruction, model, files`, plus
`runner`) and every constraint around it is global to the chat session. There
is no way to say "a reviewer that reads but cannot write", "a planner that may
delegate but not write", or "a worker restricted to src/**". A definition file
is how every other system answered that, and Faustus already has the machinery:
``services/experts.py`` stores ``EXPERT.md`` as frontmatter + body through the
skills loader (``services/memory/skill_format.py``). This module uses the SAME
loader — there is one YAML path in this codebase and this is not a second one.

**Every field here has an enforcement point that exists today.** That is the
rule the file is written to, and it is the reason some obvious fields are not
here: an unenforced permission is worse than no permission, because it is
believed. Where a field is enforced:

    name, description   identity; claim nothing, enforce nothing
    mode                `coordinator` may delegate (subagent_permissions);
                        `reviewer` is the only mode the reviewer slot accepts
    model               SubagentRun.model_override → the generation call
    endpoint_id         resolve_endpoint_by_id → the URL the worker runs on
    runner              src/agent_runners.py; an unknown key refuses to LOAD
    tools / deny        worker_disabled_tools() → the agent loop's denylist,
                        AND the pre-execution guard in subagent_tools, which
                        the workspace tool floor cannot restore past
    permission          the same pre-execution guard, on the file tools' paths
    files               FileLockRegistry.claim() — the worker's default claims
    max_rounds          the round ceiling of that worker's loop
    timeout_s           the wall-clock bound of that worker's run

**What the path rules do NOT govern**, said once here and again on the page:
``bash`` and ``python`` run their own shell, and no path pattern reaches
inside one. A definition that denies writes to a pattern and still allows the
shell carries a CAVEAT saying so; it is not refused, because a definition that
wants a shell may legitimately want one, but nobody may believe the pattern
holds. Deny the tool if you mean the pattern.

**Loading is never fatal and never silent.** A malformed file is skipped and
its reason is returned in ``errors`` — one bad file must not take out the
list, and a file that vanished from the list without a word is worse than one
that never loaded. An unknown tool name is a load error naming the tool: the
alternative, dropping it, would grant less than the author asked for while
telling them it worked.

A definition that lives in a repo is instructions from whoever sent the pull
request, so ``.faustus/agents/*.md`` loads only for a workspace whose
instruction files the user has approved (``src/workspace_trust.py``).

Stdlib only, and nothing here raises into a hot path.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.memory.skill_format import emit_frontmatter, parse_frontmatter, slugify

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

#: Module-level so tests can point the store somewhere disposable, exactly as
#: ``services/experts.py`` does.
DATA_DIR = _DEFAULT_DATA_DIR
AGENTS_DIRNAME = "agents"
DEF_FILENAME = "AGENT.md"
#: Where a repo keeps its own definitions. Read only for a TRUSTED workspace.
REPO_DIR = os.path.join(".faustus", "agents")

MODES: Tuple[str, ...] = ("coordinator", "worker", "reviewer")
ACTIONS: Tuple[str, ...] = ("read", "write", "delegate")
EFFECTS: Tuple[str, ...] = ("allow", "deny")

SOURCE_BUILTIN = "builtin"
SOURCE_USER = "user"
SOURCE_REPO = "repo"

#: The bounds the delegation parser already applies, repeated here so a
#: definition cannot ask for a ceiling the runtime would silently clamp.
MIN_ROUNDS, MAX_ROUNDS = 3, 40
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 60, 7200

#: Tools that run a shell no path pattern can see inside.
SHELL_TOOLS: Tuple[str, ...] = ("bash", "python")

_MAX_FILE_BYTES = 200_000
_MAX_DEFS = 200


class AgentDefError(ValueError):
    """A definition that cannot be loaded. Carries the reason a human needs."""


@dataclass(frozen=True)
class Rule:
    """One permission rule: ``<effect> <action> <pattern>``.

    The list is ORDERED and the LAST match wins, which is what lets a child
    definition be written top-down and still be overridden from outside — see
    :mod:`src.subagent_permissions`, where a parent's denies are appended
    after the child's own rules for exactly that reason.
    """

    action: str
    pattern: str
    effect: str

    def as_text(self) -> str:
        return f"{self.effect} {self.action} {self.pattern}"

    def to_dict(self) -> Dict[str, str]:
        return {"action": self.action, "pattern": self.pattern, "effect": self.effect}


@dataclass
class AgentDef:
    slug: str
    name: str = ""
    description: str = ""
    mode: str = "worker"
    model: str = ""
    endpoint_id: str = ""
    runner: str = ""
    tools: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    permission: Tuple[Rule, ...] = ()
    files: Tuple[str, ...] = ()
    max_rounds: Optional[int] = None
    timeout_s: Optional[float] = None
    prompt: str = ""
    source: str = SOURCE_BUILTIN
    path: str = ""
    #: Things a reader must know that are not refusals. Surfaced by the API
    #: and printed on the page: a caveat nobody sees is the same lie as an
    #: unenforced field.
    caveats: Tuple[str, ...] = ()

    def may_delegate(self) -> bool:
        """Whether this definition ASKS to delegate. Whether it MAY is decided
        by :func:`src.subagent_permissions.derive`, which also weighs the
        parent's restrictions and the depth ceiling."""
        if any(r.action == "delegate" and r.effect == "deny" for r in self.permission):
            return False
        if any(r.action == "delegate" and r.effect == "allow" for r in self.permission):
            return True
        return self.mode == "coordinator"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug, "name": self.name, "description": self.description,
            "mode": self.mode, "model": self.model, "endpoint_id": self.endpoint_id,
            "runner": self.runner, "tools": list(self.tools), "deny": list(self.deny),
            "permission": [r.to_dict() for r in self.permission],
            "files": list(self.files), "max_rounds": self.max_rounds,
            "timeout_s": self.timeout_s, "prompt": self.prompt,
            "source": self.source, "path": self.path,
            "may_delegate": self.may_delegate(), "caveats": list(self.caveats),
        }


@dataclass
class LoadResult:
    agents: List[AgentDef] = field(default_factory=list)
    #: ``{"path", "slug", "reason"}`` per file that would not load. Returned,
    #: never swallowed: a definition that disappears without a word is the
    #: failure this list exists to prevent.
    errors: List[Dict[str, str]] = field(default_factory=list)

    def by_slug(self) -> Dict[str, AgentDef]:
        return {a.slug: a for a in self.agents}


# ── paths ───────────────────────────────────────────────────────────────────

def agents_root() -> str:
    """``DATA_DIR/agents``. Read through a function so a test can repoint
    ``agent_defs.DATA_DIR`` between calls."""
    return os.path.join(DATA_DIR, AGENTS_DIRNAME)


def clean_slug(slug: Any) -> str:
    """A slug that can only ever name a direct child of :func:`agents_root`."""
    text = str(slug or "").strip()
    return slugify(text, fallback="") if text else ""


def def_path(slug: Any) -> str:
    return os.path.join(agents_root(), clean_slug(slug), DEF_FILENAME)


# ── the tool vocabulary a definition may name ───────────────────────────────

def known_tools() -> frozenset:
    """Every tool name a definition may put in ``tools``/``deny``.

    ``src/tool_index.py``'s own registry is the vocabulary: a name that is not
    in it cannot be allowed or denied, because nothing will ever be called by
    that name. Degrades to an EMPTY set when the index cannot be imported,
    and :func:`parse` reads an empty set as "cannot validate" and lets the
    names through — refusing every definition because an unrelated import
    broke would be a worse failure than a name checked later.
    """
    try:
        from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
        return frozenset(BUILTIN_TOOL_DESCRIPTIONS) | frozenset(ALWAYS_AVAILABLE)
    except Exception as exc:  # noqa: BLE001 - a broken import is not a bad definition
        logger.debug("agent_defs: tool vocabulary unavailable: %s", exc)
        return frozenset()


def _known_runner(key: str) -> bool:
    try:
        from src import agent_runners
        return agent_runners.get(key) is not None
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_defs: runner catalogue unavailable: %s", exc)
        return True          # cannot check ⇒ do not refuse; dispatch vets it again


# ── frontmatter ↔ AgentDef ──────────────────────────────────────────────────

def _as_str_list(value: Any, fieldname: str) -> List[str]:
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in items:
        if not isinstance(item, (str, int, float)):
            raise AgentDefError(f"{fieldname}: expected a list of words, got {type(item).__name__}")
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def parse_rule(raw: Any) -> Rule:
    """``"deny write src/**"`` → :class:`Rule`.

    The compact string form is the one the frontmatter loader can actually
    carry: its YAML subset has scalars, inline lists and block lists, and no
    nested mappings. Writing ``[{action: …, pattern: …}]`` would have needed a
    second parser, which is precisely what this module was told not to build.
    The pattern is the whole remainder, so a path with spaces survives.
    """
    if not isinstance(raw, str):
        raise AgentDefError(f"permission: expected `<effect> <action> <pattern>`, got {type(raw).__name__}")
    parts = raw.strip().split(None, 2)
    if len(parts) != 3:
        raise AgentDefError(f"permission: `{raw.strip()}` is not `<effect> <action> <pattern>` "
                            f"(e.g. `deny write src/**`)")
    effect, action, pattern = parts[0].lower(), parts[1].lower(), parts[2].strip()
    if effect not in EFFECTS:
        raise AgentDefError(f"permission: effect `{parts[0]}` is not one of {', '.join(EFFECTS)}")
    if action not in ACTIONS:
        raise AgentDefError(f"permission: action `{parts[1]}` is not one of {', '.join(ACTIONS)}")
    if not pattern:
        raise AgentDefError(f"permission: `{raw.strip()}` has no pattern")
    return Rule(action=action, pattern=pattern, effect=effect)


def _int_or_error(value: Any, fieldname: str, lo: int, hi: int, caveats: List[str]) -> Optional[int]:
    if value in (None, "", []):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AgentDefError(f"{fieldname}: `{value}` is not a whole number")
    clamped = max(lo, min(number, hi))
    if clamped != number:
        caveats.append(f"{fieldname}: {number} is outside {lo}–{hi} and runs as {clamped}")
    return clamped


def parse(text: str, *, slug: str, source: str = SOURCE_USER, path: str = "") -> AgentDef:
    """One AGENT.md → one definition, or :class:`AgentDefError` saying why not."""
    fm, body = parse_frontmatter(text or "")
    if not isinstance(fm, dict) or not fm:
        raise AgentDefError("no frontmatter: an AGENT.md starts with a `---` block")

    caveats: List[str] = []
    mode = str(fm.get("mode") or "worker").strip().lower()
    if mode not in MODES:
        raise AgentDefError(f"mode: `{mode}` is not one of {', '.join(MODES)}")

    runner = str(fm.get("runner") or "").strip()
    if runner and not _known_runner(runner):
        # Refused rather than dropped: a definition that names a runner Faustus
        # cannot start would run on the built-in worker instead, which is a
        # different agent doing the work under the same name.
        raise AgentDefError(f"runner: `{runner}` is not an agent runner this machine knows "
                            f"(see GET /api/agent-runners)")

    vocabulary = known_tools()
    tools = _as_str_list(fm.get("tools"), "tools")
    deny = _as_str_list(fm.get("deny"), "deny")
    if vocabulary:
        for name in tools:
            if name not in vocabulary:
                raise AgentDefError(f"tools: `{name}` is not a tool this build has. Dropping it would "
                                    f"grant less than this file asks for, so the file does not load.")
        for name in deny:
            if name not in vocabulary:
                raise AgentDefError(f"deny: `{name}` is not a tool this build has. Keeping it would "
                                    f"read as a restriction that is not one, so the file does not load.")

    permission = tuple(parse_rule(r) for r in _as_str_list(fm.get("permission"), "permission"))
    files = tuple(_as_str_list(fm.get("files"), "files")[:40])

    definition = AgentDef(
        slug=slug,
        name=str(fm.get("name") or slug).strip()[:80],
        description=str(fm.get("description") or "").strip()[:400],
        mode=mode,
        model=str(fm.get("model") or "").strip()[:120],
        endpoint_id=str(fm.get("endpoint_id") or "").strip()[:120],
        runner=runner,
        tools=tuple(tools),
        deny=tuple(deny),
        permission=permission,
        files=files,
        max_rounds=_int_or_error(fm.get("max_rounds"), "max_rounds", MIN_ROUNDS, MAX_ROUNDS, caveats),
        timeout_s=_int_or_error(fm.get("timeout_s"), "timeout_s", MIN_TIMEOUT_S, MAX_TIMEOUT_S, caveats),
        prompt=(body or "").strip(),
        source=source,
        path=path,
    )
    definition.caveats = tuple(caveats + _caveats_for(definition))
    return definition


def _caveats_for(d: AgentDef) -> List[str]:
    """What a reader of this definition would otherwise believe wrongly."""
    out: List[str] = []
    path_rules = [r for r in d.permission if r.action in ("read", "write") and r.effect == "deny"]
    allowed = set(d.tools)
    shells = [t for t in SHELL_TOOLS
              if t not in d.deny and (not d.tools or t in allowed)]
    if path_rules and shells:
        out.append(
            f"the path rules do not reach inside {' or '.join(shells)}: a shell can read and write "
            f"anything this process can. Deny the tool if the pattern must hold."
        )
    if d.mode == "reviewer" and d.files:
        out.append("`files` on a reviewer claims those paths against the other workers; a reviewer "
                   "that only reads does not need them")
    if d.runner and (d.tools or d.deny or d.permission):
        out.append(f"`{d.runner}` runs its own loop in its own shell: the tool and path rules here "
                   f"govern Faustus's built-in worker and do not reach it")
    return out


def to_markdown(d: AgentDef) -> str:
    """The definition as an AGENT.md — the same emitter ``EXPERT.md`` uses."""
    fm: Dict[str, Any] = {
        "name": d.name or d.slug,
        "description": d.description,
        "mode": d.mode,
        "model": d.model,
        "endpoint_id": d.endpoint_id,
        "runner": d.runner,
        "tools": list(d.tools),
        "deny": list(d.deny),
        "permission": [r.as_text() for r in d.permission],
        "files": list(d.files),
        "max_rounds": d.max_rounds,
        "timeout_s": d.timeout_s,
    }
    return f"---\n{emit_frontmatter(fm)}\n---\n\n{d.prompt.strip()}\n"


def from_dict(raw: Any) -> Optional[AgentDef]:
    """Rebuild a definition from its :meth:`AgentDef.to_dict` form.

    The resolved definition rides on the delegation payload as JSON — the
    dispatch path serialises the task list and parses it again inside the tool
    — so it has to come back as an object without a second trip to disk. A
    field this build does not recognise is dropped rather than guessed at, and
    a rule that no longer parses is dropped rather than assumed to allow.
    """
    if not isinstance(raw, dict) or not str(raw.get("slug") or "").strip():
        return None
    rules: List[Rule] = []
    for item in raw.get("permission") or ():
        try:
            if isinstance(item, dict):
                rules.append(parse_rule(f"{item.get('effect')} {item.get('action')} {item.get('pattern')}"))
            else:
                rules.append(parse_rule(item))
        except AgentDefError:
            continue
    mode = str(raw.get("mode") or "worker")
    return AgentDef(
        slug=str(raw.get("slug")), name=str(raw.get("name") or ""),
        description=str(raw.get("description") or ""),
        mode=mode if mode in MODES else "worker",
        model=str(raw.get("model") or ""), endpoint_id=str(raw.get("endpoint_id") or ""),
        runner=str(raw.get("runner") or ""),
        tools=tuple(str(t) for t in (raw.get("tools") or ())),
        deny=tuple(str(t) for t in (raw.get("deny") or ())),
        permission=tuple(rules),
        files=tuple(str(f) for f in (raw.get("files") or ())),
        max_rounds=raw.get("max_rounds") or None, timeout_s=raw.get("timeout_s") or None,
        prompt=str(raw.get("prompt") or ""), source=str(raw.get("source") or SOURCE_USER),
        path=str(raw.get("path") or ""),
        caveats=tuple(str(c) for c in (raw.get("caveats") or ())),
    )


# ── the built-ins ───────────────────────────────────────────────────────────
# Shipped as text and read through the SAME loader as any other file, so the
# parser is exercised by the app on every start and a built-in cannot drift
# into a shape a user's file could not have. A user's own
# DATA_DIR/agents/<slug>/AGENT.md with the same slug replaces the built-in.

BUILTIN_SOURCES: Dict[str, str] = {
    "reviewer": """---
name: reviewer
description: Reads the whole change and reports on it. Cannot write, cannot delegate.
mode: reviewer
tools: [read_file, ls, glob, grep, todowrite]
deny: [write_file, edit_file, apply_patch, bash, python]
permission:
  - "deny write **"
  - "deny delegate *"
  - "allow read **"
max_rounds: 12
---

You are reviewing work that other workers have already finished. You cannot
change anything, and that is the point: your report is the only thing you
produce, so it has to be worth reading.

Read the files that changed and the code around them. Look for the defects a
diff hides: a caller that was not updated, a name that means two things now, an
error path that swallows what it should report, a test that asserts the
behaviour it just implemented rather than the behaviour that was asked for.

Finish with three lists and nothing else: what you verified and how, what is
wrong (file and line, and what the failure would look like), and what you could
not check from reading alone.
""",
    "planner": """---
name: planner
description: Splits the work and delegates it. Cannot write files itself.
mode: coordinator
tools: [read_file, ls, glob, grep, delegate_agents, todowrite, update_plan]
deny: [write_file, edit_file, apply_patch]
permission:
  - "deny write **"
  - "allow read **"
  - "allow delegate *"
max_rounds: 16
---

You plan, you do not implement. Read enough of the codebase to know what the
work really is, then split it into tasks that do not touch the same files —
overlapping tasks are the failure mode this whole system is built around.

Give every task the files it owns. A task with no declared files takes what it
writes first, which works for one worker and fights for two.

Say what you do not know. A plan that hides its uncertainty gets workers stuck
on it one at a time instead of once, at the start, where it is cheap.
""",
    "implementer": """---
name: implementer
description: Writes the change and verifies it. One task, its own files, no delegation.
mode: worker
tools: [read_file, ls, glob, grep, write_file, edit_file, apply_patch, bash, python, todowrite]
permission:
  - "deny delegate *"
  - "allow read **"
  - "allow write **"
max_rounds: 20
timeout_s: 1500
---

You have one task. Do that task and nothing next to it.

Read before you write: the file you are about to change, its callers, and the
test that covers it. Make the smallest change that does the job, then run the
narrowest command that proves it — the test file, not the suite.

Report what you changed, file by file, and what you ran to check it. If a file
you needed belongs to another worker, do not write it: describe the change it
needs in your report and let the coordinator place it.
""",
}


def builtins() -> List[AgentDef]:
    """The shipped definitions. A broken one is skipped and logged rather than
    taking the module down with it — but that is a bug in this file, not in a
    user's, so it is logged at WARNING."""
    out: List[AgentDef] = []
    for slug, text in BUILTIN_SOURCES.items():
        try:
            out.append(parse(text, slug=slug, source=SOURCE_BUILTIN, path=""))
        except AgentDefError as exc:
            logger.warning("agent_defs: built-in definition %r does not load: %s", slug, exc)
    return out


# ── loading ─────────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(_MAX_FILE_BYTES)


def _load_user(result: LoadResult, seen: Dict[str, int]) -> None:
    root = agents_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return                     # no store yet: the built-ins are the list
    for name in names[:_MAX_DEFS]:
        slug = clean_slug(name)
        if not slug or name.startswith(".") or slug != name:
            continue
        path = os.path.join(root, name, DEF_FILENAME)
        if not os.path.isfile(path):
            continue
        _absorb(result, seen, path, slug, SOURCE_USER)


def _load_repo(result: LoadResult, seen: Dict[str, int], workspace: str) -> None:
    """``<workspace>/.faustus/agents/*.md``, and only for a folder the user has
    approved.

    A definition that travels with a clone is instructions from whoever sent
    the pull request — the same class of input ``src/workspace_trust.py`` was
    built for, and a far sharper one: an AGENTS.md can suggest a command, an
    AGENT.md can hand a worker the shell and a pattern that says it is safe.
    """
    try:
        from src import workspace_trust
        if not workspace_trust.instructions_trusted(workspace):
            result.errors.append({
                "path": os.path.join(workspace, REPO_DIR), "slug": "",
                "reason": "this folder's own instruction files are not approved, so the agent "
                          "definitions it carries were not loaded (Workspace trust)",
            })
            return
    except Exception as exc:  # noqa: BLE001 - no gate available ⇒ do not load
        logger.debug("agent_defs: trust gate unavailable for %s: %s", workspace, exc)
        return
    root = os.path.join(workspace, REPO_DIR)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names[:_MAX_DEFS]:
        if not name.lower().endswith(".md") or name.startswith("."):
            continue
        slug = clean_slug(os.path.splitext(name)[0])
        if not slug:
            continue
        _absorb(result, seen, os.path.join(root, name), slug, SOURCE_REPO)


def _absorb(result: LoadResult, seen: Dict[str, int], path: str, slug: str, source: str) -> None:
    """Parse one file into the result, replacing a lower-precedence definition
    of the same slug. One unreadable or malformed file never stops the walk."""
    try:
        definition = parse(_read(path), slug=slug, source=source, path=path)
    except AgentDefError as exc:
        result.errors.append({"path": path, "slug": slug, "reason": str(exc)})
        return
    except OSError as exc:
        result.errors.append({"path": path, "slug": slug, "reason": f"could not be read: {exc}"})
        return
    except Exception as exc:  # noqa: BLE001 - a bad file is data, not a crash
        result.errors.append({"path": path, "slug": slug, "reason": f"{type(exc).__name__}: {exc}"[:300]})
        return
    index = seen.get(slug)
    if index is None:
        seen[slug] = len(result.agents)
        result.agents.append(definition)
    else:
        result.agents[index] = definition


def load_all(workspace: Optional[str] = None) -> LoadResult:
    """Every definition, in precedence order: built-in < user < repo.

    Never raises. A workspace whose instruction files are not approved
    contributes nothing but an entry in ``errors`` saying so.
    """
    result = LoadResult()
    seen: Dict[str, int] = {}
    for definition in builtins():
        seen[definition.slug] = len(result.agents)
        result.agents.append(definition)
    try:
        _load_user(result, seen)
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_defs: user store unreadable: %s", exc)
    folder = str(workspace or "").strip()
    if folder and os.path.isdir(folder):
        try:
            _load_repo(result, seen, folder)
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent_defs: repo definitions unreadable in %s: %s", folder, exc)
    return result


def get(slug: Any, workspace: Optional[str] = None) -> Optional[AgentDef]:
    """One definition by slug, or None. Never raises."""
    key = clean_slug(slug)
    if not key:
        return None
    return load_all(workspace).by_slug().get(key)


# ── resolving a definition onto a dispatch task ─────────────────────────────

#: Keys a resolved task carries in ADDITION to today's four. Every one of them
#: is read by src/agent_tools/subagent_tools.py; a key nothing reads would be
#: a field with no enforcement point wearing a different hat.
RESOLVED_KEYS: Tuple[str, ...] = ("agent", "agent_def", "system_prompt", "max_rounds",
                                  "timeout_s", "endpoint_id", "runner")


def resolve_task(task: Dict[str, Any], *, workspace: Optional[str] = None,
                 defs: Optional[LoadResult] = None) -> Optional[Dict[str, str]]:
    """Fill one task in place from the definition it names. Returns the error
    to report, or None.

    **Anything the task states explicitly wins.** The definition supplies what
    the caller left blank, so a coordinator that names an agent AND a model
    gets its model — an agent definition is a default, not a cage the caller
    cannot see out of. (What the caller cannot widen is the PERMISSIONS; those
    are derived, not merged — see :mod:`src.subagent_permissions`.)

    Idempotent: a task already carrying ``agent_def`` is left alone, because
    the delegation payload is parsed twice on the dispatch path (once to build
    the job, once inside the tool) and resolving twice must not double-apply.
    """
    if not isinstance(task, dict):
        return None
    slug = clean_slug(task.get("agent"))
    if not slug:
        return None
    if task.get("agent_def"):
        return None
    catalogue = defs if defs is not None else load_all(workspace)
    definition = catalogue.by_slug().get(slug)
    if definition is None:
        known = ", ".join(sorted(catalogue.by_slug())[:12])
        return {"agent": slug, "reason": f"unknown agent definition `{slug}`" + (f". Known: {known}" if known else "")}
    if definition.mode == "reviewer" and not task.get("_reviewer_slot"):
        # Allowed, and worth a caveat rather than a refusal: a read-only
        # reviewer run as an ordinary task is a legitimate thing to want.
        pass
    task["agent"] = definition.slug
    task["agent_def"] = definition.to_dict()
    if definition.prompt:
        task["system_prompt"] = definition.prompt
    if not str(task.get("model") or "").strip() and definition.model:
        task["model"] = definition.model
    if not task.get("files") and definition.files:
        task["files"] = list(definition.files)
    if not str(task.get("runner") or "").strip() and definition.runner:
        task["runner"] = definition.runner
    if definition.endpoint_id:
        task["endpoint_id"] = definition.endpoint_id
    if definition.max_rounds:
        task["max_rounds"] = definition.max_rounds
    if definition.timeout_s:
        task["timeout_s"] = definition.timeout_s
    return None


def resolve_tasks(tasks: Sequence[Dict[str, Any]], *,
                  workspace: Optional[str] = None) -> List[Dict[str, str]]:
    """:func:`resolve_task` over a task list. Returns the errors, in order.

    Loads the catalogue ONCE for the whole list: a four-task job used to be
    four directory walks and four trust checks.
    """
    rows = [t for t in tasks or () if isinstance(t, dict) and t.get("agent")]
    if not rows:
        return []
    catalogue = load_all(workspace)
    errors: List[Dict[str, str]] = []
    for task in rows:
        problem = resolve_task(task, workspace=workspace, defs=catalogue)
        if problem:
            errors.append(problem)
    return errors


# ── what a definition may and may not do, in words ──────────────────────────

def explain(d: AgentDef, *, tools: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """The resolved rules as sentences, for the page and the API.

    The page shows THIS, not the raw YAML: a reader who has to compile an
    allowlist and an ordered rule list in their head to know whether an agent
    can write to ``src/`` will get it wrong, and the whole point of the file is
    that they do not have to.
    """
    out: List[Dict[str, str]] = []
    vocabulary = sorted(tools) if tools is not None else sorted(known_tools())
    if d.tools:
        out.append({"effect": "allow", "what": "tools",
                    "detail": "may use only: " + ", ".join(sorted(d.tools))})
        if vocabulary:
            blocked = [t for t in vocabulary if t not in set(d.tools)]
            if blocked:
                out.append({"effect": "deny", "what": "tools",
                            "detail": f"every other tool is refused ({len(blocked)} of them)"})
    if d.deny:
        out.append({"effect": "deny", "what": "tools",
                    "detail": "never: " + ", ".join(sorted(d.deny))})
    for rule in d.permission:
        if rule.action == "delegate":
            continue
        out.append({"effect": rule.effect, "what": rule.action,
                    "detail": f"{rule.action} {rule.pattern}"})
    out.append({"effect": "allow" if d.may_delegate() else "deny", "what": "delegate",
                "detail": ("may split its work between further workers, up to the depth ceiling"
                           if d.may_delegate() else "cannot start another worker")})
    if d.files:
        out.append({"effect": "allow", "what": "files",
                    "detail": "claims, so no other worker may write them: " + ", ".join(d.files)})
    if d.runner:
        out.append({"effect": "allow", "what": "runner",
                    "detail": f"runs as `{d.runner}`, an agent Faustus did not write"})
    if d.max_rounds:
        out.append({"effect": "deny", "what": "rounds", "detail": f"stops after {d.max_rounds} rounds"})
    if d.timeout_s:
        out.append({"effect": "deny", "what": "time", "detail": f"stops after {int(d.timeout_s)}s"})
    return out


__all__ = [
    "ACTIONS", "AgentDef", "AgentDefError", "EFFECTS", "LoadResult", "MODES", "REPO_DIR",
    "RESOLVED_KEYS", "Rule", "SOURCE_BUILTIN", "SOURCE_REPO", "SOURCE_USER",
    "agents_root", "builtins", "clean_slug", "def_path", "explain", "from_dict", "get",
    "known_tools",
    "load_all", "parse", "parse_rule", "resolve_task", "resolve_tasks", "to_markdown",
]
