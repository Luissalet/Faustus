"""
agent_profiles/contracts.py — the configuration a run started from, written
down before it runs.

The failure this file exists to prevent has two halves, and both of them have
already happened to somebody.

**A run that cannot say what configuration it started from cannot be branched,
compared or reproduced.** Faustus answers "who works" in `src/agent_defs.py`,
and that definition is a FILE: somebody edits it while a queued job waits, and
the job that finally runs is not the job that was queued. Re-reading the file
afterwards is not an answer, it is a reconstruction. So a resolution pins the
revision it used, the model route, the completion mode, the profiles and the
effective permissions, and it does so BEFORE anything runs.

**A completion mode is not a permission.** `CompletionChoice` and
`PermissionEnvelope` live in this module and never touch each other:
`maximalist` handed to a read-only reviewer is a reviewer that looks harder
and still cannot write a byte (§3.3). Nothing in `CompletionChoice` names a
tool, a path, an effect or a work root; nothing in `PermissionEnvelope` reads
a mode. The separation holds because the types have no way to reach each
other, not because a comment asked people to be careful.

Three rules on top of the three every Faustus contract keeps
(`src/contracts/base.py`: a rejection names the field, an unknown key is an
error, nothing is coerced across a type boundary):

1. **`intersect` can only restrict.** Merging two envelopes never yields a
   tool, an effect or a work root that both sides did not already permit.
   Denies are unioned, allowlists intersected, and the ordered permission
   rules keep the receiver's order with the other side's DENIES appended — so
   a deny from either side survives the merge and a child allow cannot
   reopen it.

2. **The prompt is a digest, never the text.** A resolution is serialised into
   runs, branches, board events and logs. A prompt in that record is both a
   leak and dead weight, so `prompt_digest` carries `sha256:<hex>` and nothing
   else (§21, §23); a caller who puts the prompt there is refused by name.

3. **`identity()` is the configuration, not the occasion.** It ignores
   `resolution_id`, `created_at`, the session/run/turn ids and the selection
   trace, so Branching Futures can prove two branches started from the same
   configuration even though every id in them differs.

This module must never import `src.agent_defs`. The dependency runs the other
way: agent_defs reads its completion-mode and capability vocabulary from here,
because there is one list of each in this codebase, and a cycle between the
identity of an agent and the shape of its resolution would be paid for on
every import in the app.

Stdlib plus `src.contracts.base`. Nothing here reads a file, starts anything
or reaches the network.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    SCHEMA_VERSION, ContractError, as_mapping, fingerprint, now_iso,
    reject_unknown, text, timestamp, whole,
)

logger = logging.getLogger(__name__)

# ── the vocabulary, in one place ────────────────────────────────────────────
# Every list below is DATA. A chain of `if`s can answer "greedy" but cannot
# print the ladder it walked, and "why did it explore for forty minutes?" is
# the question this layer exists to answer.

#: How far a mission pushes. Depth only — never authority (§3.2, §3.3).
COMPLETION_MODES: Tuple[str, ...] = ("literal", "professional", "greedy", "maximalist")

#: What an agent IS. The same three `src.agent_defs.MODES` already enforces;
#: a test pins the two together so they can never drift apart.
AGENT_MODES: Tuple[str, ...] = ("coordinator", "worker", "reviewer")

#: What an agent can do, for selection and compatibility only. A capability is
#: NOT a tool and NOT a permission: declaring `browser` gets you considered for
#: browser work, and gets you exactly zero tools you were not already allowed.
CAPABILITIES: Tuple[str, ...] = ("code", "research", "image", "video", "audio", "documents",
                                 "planning", "review", "security", "testing", "browser",
                                 "computer_use", "diagnostics", "automation")

#: The five auxiliary profile families a definition may reference by id.
PROFILE_KINDS: Tuple[str, ...] = ("verification", "context", "budget", "collaboration", "output")

#: Where a resolved value came from, strongest first (§6).
OVERRIDE_SOURCES: Tuple[str, ...] = ("activity_override", "task_override", "run_override",
                                     "agent_default", "project_default", "global_default")

#: §1.6, from strongest to weakest. The order is DATA, not prose: every plan in
#: this family resolves against this one tuple, and a level that only exists in
#: a paragraph is a level each implementation gets to reorder by accident.
PRECEDENCE: Tuple[str, ...] = ("system_policy", "owner_policy", "project_restriction",
                               "activity_policy", "task_override", "agent_default",
                               "project_default", "global_default")

#: Where a definition was loaded from — the loader's own three words
#: (`src.agent_defs.SOURCE_*`). §1.4 of the plan writes `workspace` for what
#: the loader has always called `repo`; one spelling reaches the record.
AGENT_SOURCES: Tuple[str, ...] = ("builtin", "user", "repo")
_SOURCE_ALIASES: Dict[str, str] = {"workspace": "repo"}

#: The six precedence levels have a short spelling in `agent_profiles.completion`
#: (`task`) and a long one in the record (`task_override`). Both name the same
#: level; normalising here is what keeps one vocabulary from becoming two.
_OVERRIDE_ALIASES: Dict[str, str] = {
    "activity": "activity_override", "task": "task_override", "run": "run_override",
    "workflow": "run_override", "agent": "agent_default", "project": "project_default",
    "global": "global_default",
}

#: Digests are written `sha256:<64 lowercase hex>` wherever they appear here.
DIGEST_PREFIX = "sha256:"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
#: The same narrow id `src.contracts.base.ident` accepts: an id ends up in a
#: path, a URL and an event name, so `../../etc` is refused by the contract
#: rather than by whichever of the three notices first.
_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
#: `<effect> <action> <pattern>`; the authority on evaluating one of these is
#: `src.subagent_permissions.decide`, and this module only ever reads the
#: effect word, so that `intersect` can tell a restriction from a grant.
_RULE_EFFECTS: Tuple[str, ...] = ("allow", "deny")


class ProfileError(ContractError):
    """A rejection from this package: which field, what was expected, what was
    actually there. It IS a :class:`ContractError`, so a caller that already
    catches contract failures keeps working; it has its own name so a caller
    that only cares about resolutions can say so."""


@contextmanager
def _profile_errors() -> Iterator[None]:
    """Re-label the typed readers of `contracts.base` as this package's error.

    The readers raise `ContractError`; everything a caller of THIS module sees
    should be a `ProfileError`, or `except ProfileError` would quietly miss
    half the rejections — the half that comes from the shared readers.
    """
    try:
        yield
    except ProfileError:
        raise
    except ContractError as exc:
        if exc.has_got:
            raise ProfileError(exc.path, exc.message, got=exc.got) from None
        raise ProfileError(exc.path, exc.message) from None


# ── value readers, used by BOTH `parse()` and `__post_init__` ───────────────
# Three people are writing against these objects at once, so the dataclasses
# validate what they are handed instead of trusting that everyone came in
# through `parse()`. The checks are membership and shape only: no I/O, no
# registry lookup, nothing that could make constructing a record slow.

def _as_tuple(value: Any, path: str, *, max_items: int = 256, max_len: int = 512) -> Tuple[str, ...]:
    """A list of words → a deduplicated tuple, order preserved.

    A bare string is refused rather than wrapped: `tools="read_file,bash"` is
    either one tool with a comma in its name or two tools, and guessing which
    is how an allowlist silently becomes something nobody wrote.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raise ProfileError(path, "expected a list of strings; a bare string is not a one-item "
                                 "list here (a comma inside it would silently become two entries)",
                           got=value)
    if not isinstance(value, (list, tuple)):
        raise ProfileError(path, "expected a list of strings", got=value)
    if len(value) > max_items:
        raise ProfileError(path, f"has more than {max_items} items", got=len(value))
    out: list = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ProfileError(f"{path}[{i}]", "expected a string", got=item)
        word = item.strip()
        if not word:
            continue
        if len(word) > max_len:
            raise ProfileError(f"{path}[{i}]", f"is longer than {max_len} chars", got=word)
        if word not in out:
            out.append(word)
    return tuple(out)


def _as_choice(value: Any, path: str, choices: Sequence[str], *,
               aliases: Optional[Mapping[str, str]] = None, allow_blank: bool = True) -> str:
    """One of `choices`, with the blank meaning "not stated yet"."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProfileError(path, f"expected one of {list(choices)}", got=value)
    word = value.strip().lower()
    word = (aliases or {}).get(word, word)
    if not word:
        if allow_blank:
            return ""
        raise ProfileError(path, f"is required; one of {list(choices)}")
    if word not in choices:
        raise ProfileError(path, f"must be one of {list(choices)}", got=value)
    return word


def _as_id(value: Any, path: str, *, allow_blank: bool = True, max_len: int = 128) -> str:
    """A versioned profile/policy id: `full_delivery_v1`, `greedy_v1`."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProfileError(path, "expected an id string", got=value)
    word = value.strip()
    if not word:
        if allow_blank:
            return ""
        raise ProfileError(path, "is required")
    if len(word) > max_len:
        raise ProfileError(path, f"is longer than {max_len} chars", got=word)
    if not _ID_RE.fullmatch(word):
        raise ProfileError(path, "must be lowercase a-z0-9 separated by . _ or - (it becomes a "
                                 "path, a URL and an event name)", got=word)
    return word


def _as_text(value: Any, path: str, *, max_len: int = 200) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProfileError(path, "expected a string", got=value)
    word = value.strip()
    if len(word) > max_len:
        raise ProfileError(path, f"is longer than {max_len} chars", got=word)
    return word


def _as_digest(value: Any, path: str) -> str:
    """`sha256:<64 hex>` or blank. Nothing else, and never the text itself."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProfileError(path, f"expected `{DIGEST_PREFIX}<64 hex chars>`", got=value)
    word = value.strip()
    if not word:
        return ""
    if not word.startswith(DIGEST_PREFIX) or not _HEX64_RE.fullmatch(word[len(DIGEST_PREFIX):]):
        raise ProfileError(
            path,
            f"must be `{DIGEST_PREFIX}` followed by 64 lowercase hex chars. A resolution is "
            "serialised into runs, branches, board events and logs, so the text this digest "
            "stands for may never be stored here (use sha256_digest())",
            got=word,
        )
    return word


def _as_count(value: Any, path: str, *, minimum: int, maximum: int) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(path, "expected a whole number", got=value)
    if value < minimum or value > maximum:
        raise ProfileError(path, f"must be between {minimum} and {maximum}", got=value)
    return value


def _as_rules(value: Any, path: str) -> Tuple[str, ...]:
    """Permission rules as written: `<effect> <action> <pattern>`, ORDERED.

    Only the effect word is read here — `intersect` has to know which rules
    restrict — and the ordering is preserved exactly, because the evaluator
    (`src.subagent_permissions.decide`) is last-match-wins and a reordered
    list is a different policy wearing the same words.
    """
    rules = _as_tuple(value, path, max_items=128)
    for i, rule in enumerate(rules):
        head = rule.split(None, 1)[0].lower() if rule.split() else ""
        if head not in _RULE_EFFECTS:
            raise ProfileError(
                f"{path}[{i}]",
                f"must start with one of {list(_RULE_EFFECTS)} and read "
                "`<effect> <action> <pattern>` (e.g. `deny write src/**`); a rule whose effect "
                "cannot be read would be merged as if it granted something",
                got=rule,
            )
    return rules


def _rule_is_deny(rule: str) -> bool:
    parts = str(rule or "").split(None, 1)
    return bool(parts) and parts[0].lower() == "deny"


def sha256_digest(value: str) -> str:
    """The one way a prompt (or any text) enters a record here: as its digest.

    Returns `sha256:<hex>`; :func:`_as_digest` accepts exactly what this
    produces, so the pair cannot drift.
    """
    return DIGEST_PREFIX + hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()


def new_resolution_id() -> str:
    """`agentres_<16 hex>` — the plan's §1.4 spelling."""
    return "agentres_" + secrets.token_hex(8)


def _set(obj: Any, name: str, value: Any) -> None:
    """Normalise a field of a frozen dataclass in `__post_init__`.

    Frozen is about the object never changing after it exists, not about
    refusing to tidy what it was handed on the way in. A list becomes a tuple
    here so the record stays hashable and its `to_dict()` stable.
    """
    object.__setattr__(obj, name, value)


# ── who, where, on what ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentRef:
    """WHICH definition, at WHICH revision, from WHERE.

    The revision is the point. A slug alone names a file that somebody may
    edit between the moment a job is queued and the moment it runs (§20); the
    revision says the run used the definition as it read at resolution time,
    and `src.agent_defs.revision_of` is what produces it.
    """

    slug: str = ""
    definition_revision: str = ""
    source: str = ""

    _KEYS = ("slug", "definition_revision", "source")

    def __post_init__(self) -> None:
        _set(self, "slug", _as_text(self.slug, "agent.slug", max_len=80))
        _set(self, "definition_revision",
             _as_digest(self.definition_revision, "agent.definition_revision"))
        _set(self, "source", _as_choice(self.source, "agent.source", AGENT_SOURCES,
                                        aliases=_SOURCE_ALIASES))

    @classmethod
    def parse(cls, raw: Any, path: str = "agent") -> "AgentRef":
        with _profile_errors():
            data = as_mapping(raw or {}, path)
            reject_unknown(data, cls._KEYS, path)
            # Blank is allowed HERE — `AgentRef()` is a legitimate placeholder
            # and has to survive its own round trip. What may not be blank is
            # the agent of a stored resolution; that is checked where it means
            # something, in `ResolvedAgentExecution.parse`.
            slug = text(data, "slug", path, required=False, max_len=80)
            return cls(slug=slug,
                       definition_revision=_as_digest(data.get("definition_revision"),
                                                      f"{path}.definition_revision"),
                       source=_as_choice(data.get("source"), f"{path}.source", AGENT_SOURCES,
                                         aliases=_SOURCE_ALIASES))

    def to_dict(self) -> Dict[str, Any]:
        return {"slug": self.slug, "definition_revision": self.definition_revision,
                "source": self.source}


@dataclass(frozen=True)
class ExecutionScope:
    """Whose run this is, and where it sits.

    Every field is supplied by the runtime and none of them may be read from a
    definition's frontmatter (§1.6): an agent file that could name its own
    owner or project would be a file that grants itself a context.
    """

    owner: str = ""
    project_id: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""

    _KEYS = ("owner", "project_id", "session_id", "run_id", "turn_id")

    def __post_init__(self) -> None:
        for name in self._KEYS:
            _set(self, name, _as_text(getattr(self, name), f"execution_scope.{name}", max_len=128))

    @classmethod
    def parse(cls, raw: Any, path: str = "execution_scope") -> "ExecutionScope":
        with _profile_errors():
            data = as_mapping(raw or {}, path)
            reject_unknown(data, cls._KEYS, path)
            return cls(**{k: _as_text(data.get(k), f"{path}.{k}", max_len=128)
                          for k in cls._KEYS})

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self._KEYS}


@dataclass(frozen=True)
class ModelRoute:
    """The model, the endpoint it runs on, and the runner that drives it.

    Three fields because they fail separately: a model that is not loaded, an
    endpoint that is unreachable and a runner this machine does not have are
    three different sentences to a user, and one `model` string could only
    ever say the first.
    """

    model: str = ""
    endpoint_id: str = ""
    runner: str = ""

    _KEYS = ("model", "endpoint_id", "runner")

    def __post_init__(self) -> None:
        for name in self._KEYS:
            _set(self, name, _as_text(getattr(self, name), f"model_route.{name}", max_len=120))

    @classmethod
    def parse(cls, raw: Any, path: str = "model_route") -> "ModelRoute":
        with _profile_errors():
            data = as_mapping(raw or {}, path)
            reject_unknown(data, cls._KEYS, path)
            return cls(**{k: _as_text(data.get(k), f"{path}.{k}", max_len=120) for k in cls._KEYS})

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self._KEYS}


# ── how far, and why it thinks so ───────────────────────────────────────────

@dataclass(frozen=True)
class CompletionChoice:
    """The effective completion mode and the level that won it.

    **This class grants nothing.** It has no tool, no path, no effect, no work
    root and no rule — and no field one could be written into. That is the
    enforcement of §3.3, and it is why a `maximalist` audit is a deeper audit
    and not a writable one.

    `source` says which precedence level decided, and `reason` says what the
    levels above it had to say. A mode that arrives without either is a run
    that spent an afternoon of GPU for a reason nobody can reconstruct.
    """

    mode: str = ""
    policy_version: str = ""
    source: str = ""
    reason: str = ""

    _KEYS = ("mode", "policy_version", "source", "reason")

    def __post_init__(self) -> None:
        # Blank means "not resolved yet" and is allowed at construction; it is
        # NOT allowed to reach `parse()`, i.e. to reach a stored record.
        _set(self, "mode", _as_choice(self.mode, "completion.mode", COMPLETION_MODES))
        _set(self, "policy_version", _as_id(self.policy_version, "completion.policy_version"))
        _set(self, "source", _as_choice(self.source, "completion.source", OVERRIDE_SOURCES,
                                        aliases=_OVERRIDE_ALIASES))
        _set(self, "reason", _as_text(self.reason, "completion.reason", max_len=400))

    @classmethod
    def parse(cls, raw: Any, path: str = "completion") -> "CompletionChoice":
        with _profile_errors():
            data = as_mapping(raw or {}, path)
            reject_unknown(data, cls._KEYS, path)
            return cls(
                mode=_as_choice(data.get("mode"), f"{path}.mode", COMPLETION_MODES,
                                allow_blank=False),
                policy_version=_as_id(data.get("policy_version"), f"{path}.policy_version"),
                source=_as_choice(data.get("source"), f"{path}.source", OVERRIDE_SOURCES,
                                  aliases=_OVERRIDE_ALIASES),
                reason=_as_text(data.get("reason"), f"{path}.reason", max_len=400),
            )

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self._KEYS}


@dataclass(frozen=True)
class ProfileSet:
    """The five auxiliary profiles a resolution references, by versioned id.

    Ids, not blobs (§4). The semantics of `architecture_code_v1` belong to the
    Context Engine and the semantics of `full_delivery_v1` belong to
    verification; a resolution that embedded either would be a second copy of
    somebody else's policy, stale from the day it was written.
    """

    context: str = "default"
    verification: str = "default"
    budget: str = "default"
    collaboration: str = "default"
    output: str = ""

    _KEYS = ("context", "verification", "budget", "collaboration", "output")
    #: A blank profile reads back as the family's default rather than staying
    #: blank, so `to_dict()` → `parse()` cannot change what a resolution says.
    _DEFAULTS = {"context": "default", "verification": "default", "budget": "default",
                 "collaboration": "default", "output": ""}

    def __post_init__(self) -> None:
        for name in self._KEYS:
            value = _as_id(getattr(self, name), f"profiles.{name}")
            _set(self, name, value or self._DEFAULTS[name])

    @classmethod
    def parse(cls, raw: Any, path: str = "profiles") -> "ProfileSet":
        with _profile_errors():
            data = as_mapping(raw or {}, path)
            reject_unknown(data, cls._KEYS, path)
            values = {}
            for name in cls._KEYS:
                raw_value = data.get(name)
                values[name] = (cls._DEFAULTS[name] if raw_value in (None, "")
                                else _as_id(raw_value, f"{path}.{name}", allow_blank=False))
            return cls(**values)

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self._KEYS}


# ── what the run may actually touch ─────────────────────────────────────────

def _norm_root(value: str) -> str:
    """One spelling for a work root: forward slashes, no trailing separator.

    Case is folded on Windows for the same reason
    `src.subagent_permissions._compile` folds it: `D:/Repo` and `d:/repo` are
    the same folder there, and a containment test that says otherwise is a
    fence with a gate in it.
    """
    root = str(value or "").strip().replace("\\", "/").rstrip("/")
    return root.lower() if os.name == "nt" else root


def _root_within(child: str, parent: str) -> bool:
    inner, outer = _norm_root(child), _norm_root(parent)
    if not inner or not outer:
        return False
    return inner == outer or inner.startswith(outer + "/")


@dataclass(frozen=True)
class PermissionEnvelope:
    """The effective permissions of one execution. It can only ever shrink.

    **An empty tuple grants NOTHING.** `tools`, `work_roots` and `effects` are
    the enumerated, effective answer — not "unset, so anything". Whoever
    builds an envelope materialises what the run may do; a reader must never
    have to know that an empty list once meant "no allowlist stated, therefore
    everything". `deny` is the one list that reads the other way round,
    because it is a list of refusals: empty means nothing extra is refused.

    That choice is what makes :meth:`intersect` safe to state in one line — no
    combination of two envelopes yields a tool, an effect or a work root that
    both sides did not already permit — and it is the direction §3.4 and §3.5
    require: a lower level may restrict, never reopen.
    """

    tools: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    work_roots: Tuple[str, ...] = ()
    effects: Tuple[str, ...] = ()
    permission_rules: Tuple[str, ...] = ()

    _KEYS = ("tools", "deny", "work_roots", "effects", "permission_rules")

    def __post_init__(self) -> None:
        _set(self, "tools", _as_tuple(self.tools, "permissions.tools", max_len=128))
        _set(self, "deny", _as_tuple(self.deny, "permissions.deny", max_len=128))
        _set(self, "work_roots", _as_tuple(self.work_roots, "permissions.work_roots",
                                           max_items=64, max_len=4096))
        _set(self, "effects", _as_tuple(self.effects, "permissions.effects", max_len=64))
        _set(self, "permission_rules", _as_rules(self.permission_rules,
                                                 "permissions.permission_rules"))

    @classmethod
    def parse(cls, raw: Any, path: str = "permissions") -> "PermissionEnvelope":
        with _profile_errors():
            data = as_mapping(raw or {}, path)
            reject_unknown(data, cls._KEYS, path)
            return cls(
                tools=_as_tuple(data.get("tools"), f"{path}.tools", max_len=128),
                deny=_as_tuple(data.get("deny"), f"{path}.deny", max_len=128),
                work_roots=_as_tuple(data.get("work_roots"), f"{path}.work_roots",
                                     max_items=64, max_len=4096),
                effects=_as_tuple(data.get("effects"), f"{path}.effects", max_len=64),
                permission_rules=_as_rules(data.get("permission_rules"),
                                           f"{path}.permission_rules"),
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"tools": list(self.tools), "deny": list(self.deny),
                "work_roots": list(self.work_roots), "effects": list(self.effects),
                "permission_rules": list(self.permission_rules)}

    # ── the one operation this type exists for ──────────────────────────────

    def intersect(self, other: "PermissionEnvelope") -> "PermissionEnvelope":
        """Fold another envelope's restrictions into this one. It only shrinks.

        * `tools` and `effects`: set intersection. A name survives only if
          BOTH sides listed it.
        * `deny`: union, this side's order first. A refusal from either side
          is a refusal, and §3.5 says an override may not delete one.
        * `work_roots`: containment-aware intersection — `src` ∩ `src/lib` is
          `src/lib`, not the empty set, because a plain string intersection
          would collapse a legitimate narrowing into "no roots at all" and a
          reader would have to guess whether that meant everything or nothing.
          Every root in the result lies inside a root of BOTH sides.
        * `permission_rules`: this side's rules, in order, followed by the
          other side's DENY rules. That is the anti-laundering ordering
          `src.subagent_permissions.derive` already uses — last match wins, so
          an appended deny beats any allow before it, and an allow from the
          other side can never reopen what this one closed.

        Not commutative on the rule list: the receiver's ordering is kept, so
        `a.intersect(b)` and `b.intersect(a)` can refuse different EXTRA
        things. Both are sound — neither can permit anything either side
        refused — and refusing more is the only direction this method is
        allowed to move in.
        """
        if not isinstance(other, PermissionEnvelope):
            raise ProfileError("permissions.intersect", "expected a PermissionEnvelope", got=other)
        mine_deny = set(self.deny)
        mine_rules = set(self.permission_rules)
        their_tools, their_effects = set(other.tools), set(other.effects)
        roots = [r for r in self.work_roots if any(_root_within(r, o) for o in other.work_roots)]
        roots += [r for r in other.work_roots
                  if any(_root_within(r, m) for m in self.work_roots) and r not in roots]
        return PermissionEnvelope(
            tools=tuple(t for t in self.tools if t in their_tools),
            deny=self.deny + tuple(d for d in other.deny if d not in mine_deny),
            work_roots=tuple(roots),
            effects=tuple(e for e in self.effects if e in their_effects),
            permission_rules=self.permission_rules + tuple(
                r for r in other.permission_rules if _rule_is_deny(r) and r not in mine_rules),
        )

    # ── the questions a caller actually asks ────────────────────────────────

    def permits(self, tool: str) -> bool:
        """Whether this envelope allows a tool by name. Deny wins."""
        name = str(tool or "").strip()
        return bool(name) and name not in self.deny and name in self.tools

    def permits_effect(self, effect: str) -> bool:
        name = str(effect or "").strip()
        return bool(name) and name in self.effects

    def permits_root(self, path: str) -> bool:
        """Whether a path lies inside one of the work roots."""
        return any(_root_within(path, root) for root in self.work_roots)


# ── why this agent and not another ──────────────────────────────────────────

def _as_rejections(value: Any, path: str) -> Tuple[Tuple[str, str], ...]:
    """`[{"slug": ..., "reason": ...}]` → pairs.

    A slug on its own is refused: a candidate rejected for a reason nobody
    wrote down is a candidate the selector re-proposes next turn, and the
    human reading the trace cannot tell "wrong capability" from "quarantined".
    """
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ProfileError(path, "expected a list of {slug, reason} objects", got=value)
    out: list = []
    for i, item in enumerate(value):
        where = f"{path}[{i}]"
        if isinstance(item, Mapping):
            reject_unknown(as_mapping(item, where), ("slug", "reason"), where)
            slug, reason = item.get("slug"), item.get("reason")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            slug, reason = item[0], item[1]
        else:
            raise ProfileError(where, "expected {slug, reason} (a bare slug does not say why the "
                                      "candidate lost, and an unexplained rejection is proposed "
                                      "again next turn)", got=item)
        name = _as_text(slug, f"{where}.slug", max_len=80)
        if not name:
            raise ProfileError(f"{where}.slug", "is required")
        out.append((name, _as_text(reason, f"{where}.reason", max_len=300)))
    return tuple(out)


def _as_scores(value: Any, path: str) -> Tuple[Tuple[str, float], ...]:
    """`{"task_fit": 0.8}` → pairs, sorted by name so the record is stable."""
    if value is None:
        return ()
    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, (list, tuple)):
        items = []
        for i, pair in enumerate(value):
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ProfileError(f"{path}[{i}]", "expected a (name, number) pair", got=pair)
            items.append((pair[0], pair[1]))
    else:
        raise ProfileError(path, "expected an object of name → number", got=value)
    out: list = []
    for name, score in items:
        key = _as_text(name, f"{path}.{name}", max_len=64)
        if not key:
            raise ProfileError(path, "a score with no name cannot be read back", got=name)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ProfileError(f"{path}.{key}", "expected a number", got=score)
        out.append((key, float(score)))
    return tuple(sorted(out, key=lambda pair: pair[0]))


@dataclass(frozen=True)
class SelectionTrace:
    """How this agent came to be the one running.

    §14: an agent is never chosen for having a nice name, and a self-declared
    success rate is not evidence. What that rule needs in order to be
    auditable is this record — what the caller asked for, what was chosen,
    why, who lost and on what numbers.
    """

    requested: str = ""
    chosen: str = ""
    reason: str = ""
    alternatives_rejected: Tuple[Tuple[str, str], ...] = ()
    scores: Tuple[Tuple[str, float], ...] = ()

    _KEYS = ("requested", "chosen", "reason", "alternatives_rejected", "scores")

    def __post_init__(self) -> None:
        _set(self, "requested", _as_text(self.requested, "selection.requested", max_len=80))
        _set(self, "chosen", _as_text(self.chosen, "selection.chosen", max_len=80))
        _set(self, "reason", _as_text(self.reason, "selection.reason", max_len=400))
        _set(self, "alternatives_rejected",
             _as_rejections(self.alternatives_rejected, "selection.alternatives_rejected"))
        _set(self, "scores", _as_scores(self.scores, "selection.scores"))

    @classmethod
    def parse(cls, raw: Any, path: str = "selection") -> "SelectionTrace":
        with _profile_errors():
            data = as_mapping(raw or {}, path)
            reject_unknown(data, cls._KEYS, path)
            return cls(
                requested=_as_text(data.get("requested"), f"{path}.requested", max_len=80),
                chosen=_as_text(data.get("chosen"), f"{path}.chosen", max_len=80),
                reason=_as_text(data.get("reason"), f"{path}.reason", max_len=400),
                alternatives_rejected=_as_rejections(data.get("alternatives_rejected"),
                                                     f"{path}.alternatives_rejected"),
                scores=_as_scores(data.get("scores"), f"{path}.scores"),
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested": self.requested, "chosen": self.chosen, "reason": self.reason,
            "alternatives_rejected": [{"slug": slug, "reason": why}
                                      for slug, why in self.alternatives_rejected],
            "scores": {name: value for name, value in self.scores},
        }

    def scores_dict(self) -> Dict[str, float]:
        return {name: value for name, value in self.scores}


# ── the one object the runtimes consume ─────────────────────────────────────

@dataclass(frozen=True)
class ResolvedAgentExecution:
    """The whole effective configuration of one execution, pinned before it runs.

    §1.4 and §21. Every runtime — a single worker, Dispatch, a Consejo seat, a
    workflow node, a branch — consumes THIS and nothing else, which is what
    makes "the same order resolves the same way by voice and by text" (§1.8.9)
    a testable claim rather than an intention.

    What it deliberately does not carry: the prompt (only its digest), any
    secret, and any authority derived from the completion mode.
    """

    resolution_id: str = field(default_factory=new_resolution_id)
    agent: AgentRef = field(default_factory=AgentRef)
    execution_scope: ExecutionScope = field(default_factory=ExecutionScope)
    model_route: ModelRoute = field(default_factory=ModelRoute)
    completion: CompletionChoice = field(default_factory=CompletionChoice)
    profiles: ProfileSet = field(default_factory=ProfileSet)
    permissions: PermissionEnvelope = field(default_factory=PermissionEnvelope)
    selection: SelectionTrace = field(default_factory=SelectionTrace)
    max_rounds: Optional[int] = None
    timeout_s: Optional[int] = None
    #: Things a reader must know that are not refusals — the same idea as
    #: `AgentDef.caveats`, carried through so a caveat cannot be lost between
    #: the definition and the run that used it.
    caveats: Tuple[str, ...] = ()
    #: Integrations that were NOT available at resolution time (§1.3). An
    #: absent Immune System degrades to "unknown health", named here; it never
    #: degrades to an invented `healthy`.
    degraded_integrations: Tuple[str, ...] = ()
    prompt_digest: str = ""
    created_at: str = field(default_factory=now_iso)
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("resolution_id", "agent", "execution_scope", "model_route", "completion",
             "profiles", "permissions", "selection", "max_rounds", "timeout_s", "caveats",
             "degraded_integrations", "prompt_digest", "created_at", "schema_version")

    def __post_init__(self) -> None:
        _set(self, "resolution_id", _as_text(self.resolution_id, "resolution_id", max_len=64))
        _set(self, "caveats", _as_tuple(self.caveats, "caveats", max_items=64))
        _set(self, "degraded_integrations",
             _as_tuple(self.degraded_integrations, "degraded_integrations", max_items=32,
                       max_len=128))
        _set(self, "prompt_digest", _as_digest(self.prompt_digest, "prompt_digest"))
        _set(self, "max_rounds", _as_count(self.max_rounds, "max_rounds", minimum=1, maximum=1000))
        _set(self, "timeout_s", _as_count(self.timeout_s, "timeout_s", minimum=1, maximum=604800))

    @classmethod
    def parse(cls, raw: Any, path: str = "resolution") -> "ResolvedAgentExecution":
        with _profile_errors():
            data = as_mapping(raw, path)
            reject_unknown(data, cls._KEYS, path)
            agent = AgentRef.parse(data.get("agent"), f"{path}.agent")
            if not agent.slug:
                raise ProfileError(f"{path}.agent.slug", "is required (a resolution that cannot "
                                                         "name its agent cannot be reproduced)")
            return cls(
                resolution_id=text(data, "resolution_id", path, required=False, max_len=64),
                agent=agent,
                execution_scope=ExecutionScope.parse(data.get("execution_scope"),
                                                     f"{path}.execution_scope"),
                model_route=ModelRoute.parse(data.get("model_route"), f"{path}.model_route"),
                completion=CompletionChoice.parse(data.get("completion"), f"{path}.completion"),
                profiles=ProfileSet.parse(data.get("profiles"), f"{path}.profiles"),
                permissions=PermissionEnvelope.parse(data.get("permissions"),
                                                     f"{path}.permissions"),
                selection=SelectionTrace.parse(data.get("selection"), f"{path}.selection"),
                max_rounds=_as_count(data.get("max_rounds"), f"{path}.max_rounds",
                                     minimum=1, maximum=1000),
                timeout_s=_as_count(data.get("timeout_s"), f"{path}.timeout_s",
                                    minimum=1, maximum=604800),
                caveats=_as_tuple(data.get("caveats"), f"{path}.caveats", max_items=64),
                degraded_integrations=_as_tuple(data.get("degraded_integrations"),
                                                f"{path}.degraded_integrations",
                                                max_items=32, max_len=128),
                prompt_digest=_as_digest(data.get("prompt_digest"), f"{path}.prompt_digest"),
                created_at=timestamp(data, "created_at", path, default=now_iso()),
                schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                     minimum=1),
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resolution_id": self.resolution_id,
            "agent": self.agent.to_dict(),
            "execution_scope": self.execution_scope.to_dict(),
            "model_route": self.model_route.to_dict(),
            "completion": self.completion.to_dict(),
            "profiles": self.profiles.to_dict(),
            "permissions": self.permissions.to_dict(),
            "selection": self.selection.to_dict(),
            "max_rounds": self.max_rounds,
            "timeout_s": self.timeout_s,
            "caveats": list(self.caveats),
            "degraded_integrations": list(self.degraded_integrations),
            "prompt_digest": self.prompt_digest,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    def identity(self) -> str:
        """A stable fingerprint of the effective CONFIGURATION.

        Excluded on purpose, and each for a reason a branch comparison would
        otherwise get wrong:

        * `resolution_id` and `created_at` — the occasion, not the setup;
        * `session_id`, `run_id`, `turn_id` — two branches of the same
          configuration differ in all three by definition, so including them
          would make the identity useless for the one job it has;
        * `selection` — the route by which we arrived. Two branches that
          reached the same configuration by different reasoning started from
          the same configuration;
        * `caveats` — human prose derived from the fields above. Rewording a
          warning must not read as a different setup.

        Included and easy to miss: `owner` and `project_id` (they decide what
        the run may touch) and `degraded_integrations` (a branch resolved with
        a verifier missing did not start from the same place as one that had
        it). `permission_rules` is joined into one string rather than passed
        as a list, because `fingerprint` sorts lists and rule ORDER is policy.
        """
        return DIGEST_PREFIX + fingerprint([
            ("agent_slug", self.agent.slug),
            ("agent_revision", self.agent.definition_revision),
            ("agent_source", self.agent.source),
            ("owner", self.execution_scope.owner),
            ("project_id", self.execution_scope.project_id),
            ("model", self.model_route.model),
            ("endpoint_id", self.model_route.endpoint_id),
            ("runner", self.model_route.runner),
            ("completion_mode", self.completion.mode),
            ("policy_version", self.completion.policy_version),
            ("profiles", self.profiles.to_dict()),
            ("tools", list(self.permissions.tools)),
            ("deny", list(self.permissions.deny)),
            ("work_roots", list(self.permissions.work_roots)),
            ("effects", list(self.permissions.effects)),
            ("permission_rules", "\n".join(self.permissions.permission_rules)),
            ("max_rounds", self.max_rounds),
            ("timeout_s", self.timeout_s),
            ("prompt_digest", self.prompt_digest),
            ("degraded_integrations", list(self.degraded_integrations)),
            ("schema_version", self.schema_version),
        ])


__all__ = [
    "AGENT_MODES", "AGENT_SOURCES", "CAPABILITIES", "COMPLETION_MODES", "DIGEST_PREFIX",
    "OVERRIDE_SOURCES", "PRECEDENCE", "PROFILE_KINDS",
    "AgentRef", "CompletionChoice", "ExecutionScope", "ModelRoute", "PermissionEnvelope",
    "ProfileError", "ProfileSet", "ResolvedAgentExecution", "SelectionTrace",
    "new_resolution_id", "sha256_digest",
]
