"""subagent_permissions.py — a restriction a human placed on the parent cannot
be laundered by delegating.

That sentence is the whole module. Everything below is the mechanism for it.

Delegation is the obvious hole in every per-agent permission scheme: an agent
that may not write ``src/`` starts a worker that may, hands it the same
instruction, and the restriction is gone without anyone lying about anything.
The shape borrowed here is OpenCode's ``deriveSubagentSessionPermission``,
which they state as *"parent agent restrictions only govern that agent; the
subagent's own permissions determine its capabilities"* — true, and incomplete
in exactly one direction. So the derivation is deliberately ASYMMETRIC:

* **Denies flow down. Allows do not.** The child's own rules come first and
  the parent's denies are appended AFTER them, so the ordered "last match
  wins" evaluation makes a parent deny beat a child allow every time. That
  ordering IS the anti-laundering rule; it is not a detail of the loop.
* **A parent's allowlist flows down as the deny of its complement.** A human
  who restricted a parent to ``[read_file]`` restricted the work, not the
  process: a child that asks for ``bash`` is asking for the tool the human
  took away.
* **A child cannot delegate unless its own definition asks to** — and even
  then only while the depth ceiling has room.
* **The workspace roots are the parent's, unchanged.** A child may narrow what
  it does inside them; it can never name another folder.

Depth. ``agent_subagent_depth`` ships at **1**: the coordinator's workers are
the last generation. This box has one or two GPUs and
``agent_subagent_max_parallel: 2``; a third generation buys no throughput and
multiplies the ways a run becomes unaccountable — nobody reads a transcript
tree three deep, and the file locks are per delegation, so a grandchild's
writes are invisible to its uncle. The ceiling is a setting because someone
with a different machine may disagree; it is 1 because on this one it should be.

Faustus already treats a coordinator's prose as untrusted context. This
extends the same instinct from content to capability.

Stdlib only. Pure: nothing here reads a file, starts anything, or raises
except :class:`DepthExceeded`, which is the answer, not a failure.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from src.agent_defs import AgentDef, Rule

logger = logging.getLogger(__name__)

#: Default ceiling on how many generations of workers one turn may produce.
DEFAULT_MAX_DEPTH = 1
DEPTH_SETTING = "agent_subagent_depth"

#: The tool that starts another worker. Denying it is how "may not delegate"
#: is enforced; the name is here so the two places that must agree read it
#: from one.
DELEGATE_TOOL = "delegate_agents"


class DepthExceeded(ValueError):
    """A delegation deeper than the ceiling. The message names the limit and
    the setting, because "too deep" alone sends the reader source-diving."""


def max_depth() -> int:
    """``agent_subagent_depth``. Never raises; an unreadable settings file
    leaves the ceiling at its default rather than removing it."""
    try:
        from src.settings import get_setting
        value = int(get_setting(DEPTH_SETTING, DEFAULT_MAX_DEPTH))
    except Exception:  # noqa: BLE001 - settings backend unavailable
        return DEFAULT_MAX_DEPTH
    return max(0, min(value, 4))


# ── path patterns ───────────────────────────────────────────────────────────

_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _compile(pattern: str) -> "re.Pattern[str]":
    """Glob → regex, with ``**`` crossing directories and ``*`` not.

    ``fnmatch`` is not usable here: its ``*`` crosses ``/``, so ``src/*`` would
    match ``src/a/b/c.py`` and a rule meant to fence one directory would fence
    a tree. The distinction is the entire reason a reader trusts the pattern.
    """
    cached = _CACHE.get(pattern)
    if cached is not None:
        return cached
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 2] == "**":
                if pattern[i + 2:i + 3] == "/":
                    out.append("(?:.*/)?")      # zero or more whole segments
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    flags = re.IGNORECASE if os.name == "nt" else 0
    compiled = re.compile("^" + "".join(out) + r"\Z", flags)
    if len(_CACHE) < 512:
        _CACHE[pattern] = compiled
    return compiled


def normalise_path(path: Any, workspace: Optional[str] = None) -> str:
    """The spelling a pattern is matched against: forward slashes, relative to
    the workspace when it is inside one.

    A rule is written the way the repo is read (``src/**``), and the tool call
    that has to be judged may carry an absolute path, a Windows path, or a
    walk through ``..``. Judging the two spellings apart is how a fence gets
    walked around, so both ends are brought to one spelling here.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    root = str(workspace or "").strip()
    try:
        if root:
            full = text if os.path.isabs(text) else os.path.join(root, text)
            real = os.path.realpath(full)
            rel = os.path.relpath(real, os.path.realpath(root))
            if not rel.startswith(".."):
                text = rel
            else:
                text = real
        elif os.path.isabs(text):
            text = os.path.realpath(text)
        else:
            text = os.path.normpath(text)
    except (OSError, ValueError):
        pass
    return text.replace("\\", "/").lstrip("./") or text.replace("\\", "/")


def path_matches(pattern: str, path: str) -> bool:
    if not pattern:
        return False
    if pattern in ("*", "**"):
        return True
    return bool(_compile(pattern).match(path or ""))


def decide(rules: Sequence[Rule], action: str, path: str = "*") -> str:
    """``"allow"`` or ``"deny"`` for one action on one path.

    Ordered, LAST MATCH WINS, and the default is ``allow``: a definition with
    no rule about writing is not a definition that forbids writing, and
    pretending otherwise would make every existing worker stop working the day
    this module shipped. What restricts is written down.
    """
    verdict = "allow"
    for rule in rules or ():
        if rule.action != action:
            continue
        if action == "delegate" or path_matches(rule.pattern, path):
            verdict = rule.effect
    return verdict


def matching_rule(rules: Sequence[Rule], action: str, path: str = "*") -> Optional[Rule]:
    """The rule that actually decided, so a refusal can quote it."""
    found: Optional[Rule] = None
    for rule in rules or ():
        if rule.action != action:
            continue
        if action == "delegate" or path_matches(rule.pattern, path):
            found = rule
    return found


# ── the derived permissions of one child ────────────────────────────────────

@dataclass(frozen=True)
class ChildPermissions:
    """What ONE worker may do. Built by :func:`derive`, never by hand."""

    slug: str = ""
    label: str = ""
    #: Ordered; last match wins. The parent's denies are the tail.
    rules: Tuple[Rule, ...] = ()
    denied_tools: FrozenSet[str] = frozenset()
    #: None means "no allowlist" — every tool not denied is available. An
    #: EMPTY frozenset is a real answer and means no tool at all.
    allowed_tools: Optional[FrozenSet[str]] = None
    may_delegate: bool = False
    depth: int = 0
    workspace_roots: Tuple[str, ...] = ()
    workspace: str = ""
    caveats: Tuple[str, ...] = ()

    def tool_denied(self, tool: str) -> bool:
        name = str(tool or "")
        if not name:
            return False
        if name in self.denied_tools:
            return True
        return self.allowed_tools is not None and name not in self.allowed_tools

    def why_tool_denied(self, tool: str) -> str:
        name = str(tool or "")
        who = f"agent `{self.slug}`" if self.slug else "this worker"
        if name in self.denied_tools:
            return f"{name} is on {who}'s deny list"
        return f"{who} may only use: " + (", ".join(sorted(self.allowed_tools or ())) or "no tools at all")

    def path_denied(self, action: str, path: str) -> bool:
        return decide(self.rules, action, path) == "deny"

    def why_path_denied(self, action: str, path: str) -> str:
        rule = matching_rule(self.rules, action, path)
        who = f"agent `{self.slug}`" if self.slug else "this worker"
        return (f"{who} is denied `{rule.as_text()}`" if rule
                else f"{who} may not {action} {path}")

    def restricts_action(self, action: str) -> bool:
        """Whether ANY rule denies this action anywhere — the question the
        fail-closed branch asks when a tool's target cannot be determined."""
        return any(r.action == action and r.effect == "deny" for r in self.rules)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug, "label": self.label, "depth": self.depth,
            "rules": [r.to_dict() for r in self.rules],
            "denied_tools": sorted(self.denied_tools),
            "allowed_tools": None if self.allowed_tools is None else sorted(self.allowed_tools),
            "may_delegate": self.may_delegate,
            "workspace_roots": list(self.workspace_roots),
            "caveats": list(self.caveats),
        }


def _as_permissions(parent_rules: Any) -> Optional[ChildPermissions]:
    """Accept a :class:`ChildPermissions`, a bare rule sequence, or nothing."""
    if parent_rules is None:
        return None
    if isinstance(parent_rules, ChildPermissions):
        return parent_rules
    if isinstance(parent_rules, (list, tuple)):
        rules = tuple(r for r in parent_rules if isinstance(r, Rule))
        return ChildPermissions(rules=rules, may_delegate=True)
    return None


def _denies(rules: Iterable[Rule]) -> Tuple[Rule, ...]:
    return tuple(r for r in rules or () if r.effect == "deny")


def derive(parent_rules: Any, child_def: Optional[AgentDef], *, parent_depth: int = 0,
           workspace_roots: Optional[Sequence[str]] = None,
           workspace: str = "", vocabulary: Optional[Iterable[str]] = None) -> ChildPermissions:
    """The permissions of a worker the parent is about to start.

    Raises :class:`DepthExceeded` when the delegation would go past
    ``agent_subagent_depth`` — refused BEFORE the worker exists, because a
    grandchild that runs and is then disowned has already done whatever it did.
    """
    parent = _as_permissions(parent_rules)
    depth = int(parent_depth) + 1
    ceiling = max_depth()
    if depth > ceiling:
        raise DepthExceeded(
            f"delegation would run {depth} level(s) deep and the ceiling is {ceiling} "
            f"({DEPTH_SETTING}={ceiling}). The worker that asked for this is itself a worker; "
            f"report what you need to the coordinator instead of starting another one."
        )

    child_rules: Tuple[Rule, ...] = tuple(child_def.permission) if child_def else ()
    parent_denies = _denies(parent.rules) if parent else ()
    # The parent's denies go LAST so that "last match wins" cannot be talked
    # out of them by a child rule written after — this line is the contract.
    rules = child_rules + parent_denies

    denied = set(child_def.deny) if child_def else set()
    if parent is not None:
        denied |= set(parent.denied_tools)
        if parent.allowed_tools is not None:
            # An allowlist on the parent is a deny of everything else, and a
            # deny is what flows down. Without this, a parent pinned to
            # [read_file] could start a child with bash and the pin is gone.
            names = set(vocabulary or ()) | set(child_def.tools if child_def else ()) | set(denied)
            denied |= {t for t in names if t not in parent.allowed_tools}

    allowed: Optional[FrozenSet[str]] = frozenset(child_def.tools) if (child_def and child_def.tools) else None

    wants = bool(child_def.may_delegate()) if child_def else False
    parent_lets = parent.may_delegate if parent is not None else True
    room = depth < ceiling
    may_delegate = bool(wants and parent_lets and room)
    if not may_delegate:
        denied.add(DELEGATE_TOOL)

    caveats: List[str] = list(child_def.caveats) if child_def else []
    if wants and not room:
        caveats.append(f"asks to delegate and cannot: it is already {depth} level(s) deep and "
                       f"{DEPTH_SETTING} is {ceiling}")
    if wants and not parent_lets:
        caveats.append("asks to delegate and cannot: the agent that started it may not delegate either")

    roots = tuple(str(r) for r in (workspace_roots if workspace_roots is not None
                                   else (parent.workspace_roots if parent else ())) if r)
    return ChildPermissions(
        slug=(child_def.slug if child_def else ""),
        label=(child_def.name or child_def.slug) if child_def else "",
        rules=rules,
        denied_tools=frozenset(denied),
        allowed_tools=allowed,
        may_delegate=may_delegate,
        depth=depth,
        workspace_roots=roots,
        workspace=str(workspace or (parent.workspace if parent else "")),
        caveats=tuple(caveats),
    )


def coordinator_permissions(rules: Optional[Sequence[Rule]] = None, *,
                            workspace_roots: Optional[Sequence[str]] = None,
                            workspace: str = "") -> ChildPermissions:
    """The parent's own standing at depth 0: whatever a human put on it, and
    the right to delegate. This is the argument :func:`derive` takes for the
    first generation; a turn with no restrictions passes None instead."""
    return ChildPermissions(
        rules=tuple(rules or ()), may_delegate=True, depth=0,
        workspace_roots=tuple(str(r) for r in (workspace_roots or ()) if r),
        workspace=str(workspace or ""),
    )


__all__ = [
    "DEFAULT_MAX_DEPTH", "DELEGATE_TOOL", "DEPTH_SETTING", "ChildPermissions", "DepthExceeded",
    "coordinator_permissions", "decide", "derive", "matching_rule", "max_depth",
    "normalise_path", "path_matches",
]
