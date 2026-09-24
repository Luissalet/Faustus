"""handoff_lanes.py -- permissions-as-topology for delegation.

Today, which worker a parent MAY delegate to and which tools that worker
gets is decided implicitly, one call at a time, by
`src/agent_tools/subagent_tools.py` (`worker_disabled_tools`,
`_apply_agent_defs`, `_attach_permissions`) reading `src/agent_defs.py`
and `src/subagent_permissions.py`. That is correct but not INSPECTABLE: an
admin cannot look at one place and see "who may hand off to whom, with
what tools" as a single map.

This module adds that map as an explicit, versioned setting
(`agent_handoff_lanes`, a list of lane dicts) plus a mode
(`agent_handoff_lanes_mode`):

  * ``off`` (default) -- behaviour is byte-for-byte unchanged. Nothing in
    this module runs on the delegation path at all.
  * ``shadow`` -- every delegation is evaluated against the lanes and the
    decision is appended to ``<DATA_DIR>/handoff_lanes/log.jsonl``, but
    nothing is ever blocked or narrowed. For finding out what a proposed
    lane set WOULD do before turning it on.
  * ``enforce`` -- a delegation not covered by any lane is refused with a
    message naming the lanes that would have allowed it (so the fix is
    obvious: add that lane, or use a different one); a covered delegation
    keeps its ``tools_deny`` added to the worker's own disabled-tool set
    and, when the lane states a ``tools_allow``, has any of its requested
    tools not in that list added to the disabled set too (an intersection,
    never a grant -- a lane can only ever narrow what an agent definition
    already allows).

A lane: ``{"id", "from", "to", "tools_allow"?, "tools_deny"?,
"max_depth"?, "note"?}``. ``from``/``to`` are an agent slug (as
`src/agent_defs.py` names it), ``"*"`` (any agent) or ``"main"`` (the
top-level chat, never a worker). ``max_depth`` caps how many delegation
levels deep the lane still applies (depth 1 = a direct child of `from`).

The wiring into the actual delegation path lives in
`src/agent_tools/subagent_tools.py` (`DelegateAgentsTool.execute`, right
after each worker's own permissions are derived and before any of them
starts) -- this module never imports that one, so there is no cycle and no
implicit coupling beyond the single `apply()` call site.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MODES", "DEFAULT_MODE", "Decision", "normalize_mode", "validate",
    "evaluate", "apply", "mermaid", "graph_json", "log_path",
]

MODES: tuple = ("off", "shadow", "enforce")
DEFAULT_MODE = "off"
MAX_LANES = 200
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")


def normalize_mode(value: Any) -> str:
    """A valid mode, or `DEFAULT_MODE` (``off``) for anything else -- an
    unset setting, a typo, or a legacy client that never sends this field."""
    name = str(value or "").strip().lower()
    return name if name in MODES else DEFAULT_MODE


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - settings unavailable outside a real run
        return default


# ── validation ───────────────────────────────────────────────────────────

def _clean_tool_list(raw: Any, field_name: str, lane_id: str) -> List[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"handoff_lanes: lane {lane_id!r} {field_name!r} must be a list of tool names")
    out: List[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name:
            out.append(name)
    return out


def _clean_agent(raw: Any, field_name: str, lane_id: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        raise ValueError(f"handoff_lanes: lane {lane_id!r} {field_name!r} is required")
    if value in ("*", "main"):
        return value
    if not _SLUG_RE.match(value):
        raise ValueError(
            f"handoff_lanes: lane {lane_id!r} {field_name!r} {value!r} is not a valid agent slug, "
            "'*' or 'main'")
    return value


def validate(raw: Any) -> List[Dict[str, Any]]:
    """Normalise and validate a raw `agent_handoff_lanes` setting value.

    Raises `ValueError` with a message naming the offending lane on
    anything malformed; never mutates `raw`. An empty/absent value is a
    valid, empty lane list -- `mode: enforce` with no lanes simply refuses
    every delegation, which is the honest reading of "no lanes are open".
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("handoff_lanes: agent_handoff_lanes must be a list of lane objects")
    if len(raw) > MAX_LANES:
        raise ValueError(f"handoff_lanes: too many lanes ({len(raw)} > {MAX_LANES})")
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"handoff_lanes: lane #{i + 1} must be an object")
        lane_id = str(item.get("id") or "").strip()[:60] or f"lane{i + 1}"
        if lane_id in seen_ids:
            raise ValueError(f"handoff_lanes: duplicate lane id {lane_id!r}")
        seen_ids.add(lane_id)
        frm = _clean_agent(item.get("from"), "from", lane_id)
        to = _clean_agent(item.get("to"), "to", lane_id)
        tools_allow = _clean_tool_list(item.get("tools_allow"), "tools_allow", lane_id) or None
        tools_deny = _clean_tool_list(item.get("tools_deny"), "tools_deny", lane_id)
        max_depth = item.get("max_depth")
        if max_depth not in (None, ""):
            try:
                max_depth = int(max_depth)
            except (TypeError, ValueError):
                raise ValueError(f"handoff_lanes: lane {lane_id!r} max_depth must be an integer")
            if max_depth < 1:
                raise ValueError(f"handoff_lanes: lane {lane_id!r} max_depth must be >= 1")
        else:
            max_depth = None
        note = str(item.get("note") or "").strip()[:300]
        row: Dict[str, Any] = {"id": lane_id, "from": frm, "to": to}
        if tools_allow:
            row["tools_allow"] = tools_allow
        if tools_deny:
            row["tools_deny"] = tools_deny
        if max_depth is not None:
            row["max_depth"] = max_depth
        if note:
            row["note"] = note
        out.append(row)
    return out


def _lanes_from_settings() -> List[Dict[str, Any]]:
    try:
        return validate(_setting("agent_handoff_lanes", []))
    except ValueError as exc:
        logger.warning("handoff_lanes: stored lanes failed validation, treating as empty: %s", exc)
        return []


# ── evaluation ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    allowed: bool
    lane_id: Optional[str]
    #: Tools this delegation asked for that the lane takes back away --
    #: added to the worker's OWN disabled-tool set, never a grant.
    disabled_tools: FrozenSet[str]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed, "lane_id": self.lane_id,
            "disabled_tools": sorted(self.disabled_tools), "reason": self.reason,
        }


def _matches_agent(pattern: str, value: str) -> bool:
    if pattern == "*":
        return True
    if pattern == "main":
        return value in ("", "main")
    return pattern == value


def evaluate(from_agent: str, to_agent: str, requested_tools: Iterable[str], depth: int,
             *, lanes: Optional[Sequence[Dict[str, Any]]] = None) -> Decision:
    """Would a delegation from `from_agent` to `to_agent`, at delegation
    `depth` (1 = a direct child), asking for `requested_tools`, be allowed?

    The first lane (in stored order) whose `from`/`to` match and whose
    `max_depth` (if any) is not exceeded wins. A lane that matches
    `from`/`to` but whose `max_depth` IS exceeded is remembered and named
    in the refusal, since it explains WHY a seemingly-matching lane did not
    apply. `lanes=None` reads the live `agent_handoff_lanes` setting.
    """
    from_agent = str(from_agent or "main").strip().lower() or "main"
    to_agent = str(to_agent or "*").strip().lower() or "*"
    depth = max(1, int(depth or 1))
    requested = frozenset(str(t) for t in (requested_tools or ()) if str(t or "").strip())
    if lanes is None:
        lanes = _lanes_from_settings()

    depth_blocked: List[Dict[str, Any]] = []
    for lane in lanes:
        if not _matches_agent(lane["from"], from_agent):
            continue
        if not _matches_agent(lane["to"], to_agent):
            continue
        max_depth = lane.get("max_depth")
        if max_depth is not None and depth > max_depth:
            depth_blocked.append(lane)
            continue
        tools_deny = frozenset(lane.get("tools_deny") or ())
        tools_allow = lane.get("tools_allow")
        disabled = set(tools_deny & requested)
        if tools_allow is not None:
            disabled |= requested - frozenset(tools_allow)
        return Decision(True, lane["id"], frozenset(disabled),
                        f"lane {lane['id']!r} allows {from_agent!r} -> {to_agent!r}")

    if depth_blocked:
        names = ", ".join(sorted(l["id"] for l in depth_blocked))
        return Decision(
            False, None, requested,
            f"delegation from {from_agent!r} to {to_agent!r} at depth {depth} exceeds the max_depth "
            f"of the only matching lane(s): {names}")

    candidates = sorted({
        l["id"] for l in lanes
        if _matches_agent(l["to"], to_agent) or _matches_agent(l["from"], from_agent)
    })
    hint = (" lanes that would allow a related handoff: " + ", ".join(candidates) + "."
            if candidates else " no lane names either side of this pair at all.")
    return Decision(
        False, None, requested,
        f"no handoff lane covers {from_agent!r} -> {to_agent!r}." + hint)


# ── the delegation hook (called from subagent_tools.py) ─────────────────

def _requested_tools_for(run: Any) -> FrozenSet[str]:
    """The tools a run is asking for, best-effort: a named agent
    definition's own `tools` list when it has one, otherwise whatever an
    already-derived `run.permissions.allowed_tools` says, otherwise empty
    (a lane's `tools_deny` still applies to nothing named, `tools_allow`
    is simply moot until something is)."""
    agent_def = getattr(run, "agent_def", None)
    if isinstance(agent_def, dict):
        tools = agent_def.get("tools")
        if isinstance(tools, (list, tuple)) and tools:
            return frozenset(str(t) for t in tools if str(t or "").strip())
    permissions = getattr(run, "permissions", None)
    allowed = getattr(permissions, "allowed_tools", None) if permissions is not None else None
    if allowed is not None:
        return frozenset(str(t) for t in allowed)
    return frozenset()


def apply(runs: Sequence[Any], from_agent: str, depth: int) -> str:
    """Evaluate every run in `runs` against the configured lanes and mode.

    `off` (the default) returns `""` immediately without reading anything
    else -- a byte-for-byte no-op. `shadow` logs every decision and always
    returns `""`. `enforce` returns the first refusal's message (a
    delegation this call refused nothing has started yet, so the whole
    call is cleanly refused rather than half of it); a covered run has
    `lane_id` and `lane_disabled_tools` set on it for
    `worker_disabled_tools`'s caller to fold in.

    Never raises: a lane evaluation failing is logged and treated as
    `mode: off` for that call, because a bug in this module must never be
    the reason a delegation the rest of the app approved does not run.
    """
    try:
        mode = normalize_mode(_setting("agent_handoff_lanes_mode", DEFAULT_MODE))
        if mode == "off":
            return ""
        lanes = _lanes_from_settings()
        from_agent = str(from_agent or "main")
        depth = int(depth or 0) + 1
        for run in runs:
            to_agent = str(getattr(run, "agent", "") or "*")
            requested = _requested_tools_for(run)
            decision = evaluate(from_agent, to_agent, requested, depth, lanes=lanes)
            _log_decision(mode, from_agent, to_agent, depth, decision)
            if mode == "shadow":
                continue
            if not decision.allowed:
                return f"handoff_lanes: {decision.reason}"
            try:
                run.lane_id = decision.lane_id
                existing = set(getattr(run, "lane_disabled_tools", None) or ())
                run.lane_disabled_tools = existing | set(decision.disabled_tools)
            except Exception:  # noqa: BLE001 - a SubagentRun always accepts attrs
                pass
        return ""
    except Exception as exc:  # noqa: BLE001 - never block a delegation on a bug here
        logger.warning("handoff_lanes: apply() failed, treating as mode=off: %s", exc)
        return ""


# ── logging (shadow + enforce) ───────────────────────────────────────────

def log_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "handoff_lanes", "log.jsonl")


def _log_decision(mode: str, from_agent: str, to_agent: str, depth: int, decision: Decision) -> None:
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        row = {
            "ts": time.time(), "mode": mode, "from": from_agent, "to": to_agent, "depth": depth,
            **decision.to_dict(),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - the log is a diagnostic, never load-bearing
        logger.debug("handoff_lanes: could not append to the decision log", exc_info=True)


# ── graph / diagram (Studio) ─────────────────────────────────────────────

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_]")


def _safe_id(raw: Any, used: Dict[str, str]) -> str:
    key = str(raw)
    if key in used:
        return used[key]
    base = _UNSAFE_ID_CHARS.sub("_", key) or "n"
    if base[0].isdigit():
        base = "n_" + base
    candidate, taken, suffix = base, set(used.values()), 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used[key] = candidate
    return candidate


def _escape_label(text: Any) -> str:
    value = str(text or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    value = value.replace("&", "#38;").replace('"', "#quot;")
    value = value.replace("[", "#91;").replace("]", "#93;").replace("|", "#124;")
    value = value.replace("<", "#lt;").replace(">", "#gt;")
    return value.strip()


def _agent_slugs() -> List[str]:
    """Every agent slug known to this machine (builtins + library + user),
    plus the two pseudo-nodes lanes may reference (`main`, `*`)."""
    slugs = ["main"]
    try:
        from src import agent_defs
        for d in agent_defs.builtins():
            slugs.append(d.slug)
        for d in agent_defs._library_defs():
            slugs.append(d.slug)
        result = agent_defs.load_all(None)
        for d in getattr(result, "agents", None) or ():
            slugs.append(d.slug)
    except Exception:  # noqa: BLE001 - a graph with fewer nodes beats no graph
        logger.debug("handoff_lanes: could not enumerate agent definitions", exc_info=True)
    seen: List[str] = []
    for s in slugs:
        if s and s not in seen:
            seen.append(s)
    return seen


def mermaid(lanes: Optional[Sequence[Dict[str, Any]]] = None) -> str:
    """A `flowchart LR` of every known agent plus one edge per lane. `*` is
    drawn as its own node so a wildcard lane is still visible as an edge."""
    if lanes is None:
        lanes = _lanes_from_settings()
    used: Dict[str, str] = {}
    nodes = list(_agent_slugs())
    for lane in lanes:
        for side in (lane["from"], lane["to"]):
            if side not in nodes:
                nodes.append(side)
    lines = ["flowchart LR"]
    for slug in nodes:
        safe = _safe_id(slug, used)
        shape = ('(("%s"))' if slug in ("main", "*") else '["%s"]') % _escape_label(slug)
        lines.append(f"    {safe}{shape}")
    if not lanes:
        lines.append("    %% no handoff lanes configured")
    for lane in lanes:
        a, b = used[lane["from"]], used[lane["to"]]
        bits = []
        if lane.get("tools_allow"):
            bits.append("allow: " + ",".join(lane["tools_allow"][:4]))
        if lane.get("tools_deny"):
            bits.append("deny: " + ",".join(lane["tools_deny"][:4]))
        if lane.get("max_depth"):
            bits.append(f"depth<={lane['max_depth']}")
        label = _escape_label(lane["id"] + ((" (" + "; ".join(bits) + ")") if bits else ""))
        lines.append(f'    {a} -->|"{label}"| {b}')
    return "\n".join(lines)


def graph_json(lanes: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """`{"nodes": [...], "edges": [lane dicts]}` -- the same data `mermaid()`
    draws, for a Studio panel that wants the structure rather than text."""
    if lanes is None:
        lanes = _lanes_from_settings()
    nodes = list(_agent_slugs())
    for lane in lanes:
        for side in (lane["from"], lane["to"]):
            if side not in nodes:
                nodes.append(side)
    return {
        "nodes": [{"id": n, "kind": "pseudo" if n in ("main", "*") else "agent"} for n in nodes],
        "edges": list(lanes),
        "mode": normalize_mode(_setting("agent_handoff_lanes_mode", DEFAULT_MODE)),
    }
