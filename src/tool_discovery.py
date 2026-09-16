"""Permission-aware, audited tool discovery (FAUSTUS paridad A08/A09).

`src/tool_serve.py`'s `lookup_tools` already searches the tool-RAG index
(`src/tool_index.py`) and hands back a compact catalog the agent loop
promotes into the next round's native schema list — the "third path"
described at the top of that module. What it does NOT do on its own:

  * narrow the result to the EXACT set this turn is actually PERMITTED to
    run. It only subtracts the flat `disabled_tools` set; it never consults
    `tool_policy` (the composed per-turn policy `src/agent_loop.py` and
    `src/tool_execution.py` use to gate real execution — see
    `tests/test_agent_loop_offer_execute_coherence.py` for why "offered but
    then refused" is a documented trap this must not reopen), nor the
    non-admin denylist. A discovered tool that would be refused on the very
    next call is worse than no discovery at all.
  * leave an auditable trail of the decision. A log line is not evidence a
    test — or an operator — can read back.
  * bound what it says when nothing matches. An empty `tools: []` plus a
    generic hint is not wrong, but it is not the acceptance contract either:
    A09 wants an EXPLICIT, BOUNDED (N=5) list of the closest tools by name or
    capability, never a fabricated call and never the whole catalog dumped
    to compensate.

This module is that layer. It is deliberately independent of `tool_serve`
and `tool_execution` (owned by other lots in this pass) — it only reads
their public outputs (the `promote` list `execute_lookup` already returns)
and applies the same permission predicate execution itself uses, so the
result agrees with what would actually happen on the next call.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

#: A09 — bounded fallback size. Never the whole catalog.
FALLBACK_N = 5


def _all_known_tool_names() -> List[str]:
    """Every built-in + natively-schema'd tool name this install knows about.

    Best-effort: any import failure just shrinks the pool, it never raises —
    discovery degrading gracefully beats discovery crashing a turn.
    """
    names: Set[str] = set()
    try:
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        names.update(BUILTIN_TOOL_DESCRIPTIONS.keys())
    except Exception:
        logger.debug("tool_discovery: builtin descriptions unavailable", exc_info=True)
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        for entry in FUNCTION_TOOL_SCHEMAS:
            fn = entry.get("function") if isinstance(entry, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                names.add(str(fn["name"]))
    except Exception:
        logger.debug("tool_discovery: native schema names unavailable", exc_info=True)
    return sorted(names)


def is_known_tool(name: str) -> bool:
    """Does `name` actually map to a real, schema'd tool?

    A09's trigger is a search that returns no match — but `names=[...]`
    exact lookups in `src/tool_serve.py::search_catalog` never validate the
    name against anything; they echo whatever string the model sent back as
    a "found" entry with no schema. Left unchecked, that turns a query for a
    tool that does not exist into a false "resolved", i.e. exactly the
    fabricated call A09 forbids. Checked against built-ins, native schemas,
    and (best-effort) currently-connected MCP tools.
    """
    if not name:
        return False
    try:
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        if name in BUILTIN_TOOL_DESCRIPTIONS:
            return True
    except Exception:
        logger.debug("tool_discovery: builtin descriptions unavailable", exc_info=True)
    try:
        from src.tool_serve import schema_for
        if schema_for(name) is not None:
            return True
    except Exception:
        logger.debug("tool_discovery: schema lookup failed for %s", name, exc_info=True)
    return False


def is_permitted(
    name: str,
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    tool_policy: Any = None,
    admin: bool = True,
) -> bool:
    """Would THIS turn actually be allowed to run `name`?

    First requires `name` to be a REAL tool (`is_known_tool`) — discovery
    must never resolve a name into something that will be called but does
    not exist. Then mirrors the precedence `src.agent_loop._denial_for_tool`
    uses for real execution: `tool_policy` first (the composed per-turn
    policy), then the flat `disabled_tools` denylist, then the non-admin
    denylist — every check tried against every policy-equivalent spelling of
    the name (an MCP-qualified alias and its bare name are the same
    authorization decision). A name this returns False for must never be
    promoted into a native schema; a name it returns True for is the "exact
    permitted schema" A08 asks for.
    """
    if not name or not is_known_tool(name):
        return False
    try:
        from src.tool_security import email_tool_policy_names
        policy_names = sorted(email_tool_policy_names(name)) or [name]
    except Exception:
        policy_names = [name]

    if tool_policy is not None:
        try:
            if any(tool_policy.blocks(n) for n in policy_names):
                return False
        except Exception:
            logger.debug("tool_discovery: tool_policy.blocks failed for %s", name, exc_info=True)

    blocked = {str(n) for n in (disabled_tools or ()) if n}
    if any(n in blocked for n in policy_names):
        return False

    if not admin:
        try:
            from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
            if name in NON_ADMIN_BLOCKED_TOOLS or name.startswith("mcp__"):
                return False
        except Exception:
            logger.debug("tool_discovery: admin denylist check failed for %s", name, exc_info=True)

    return True


def permitted_names(
    candidates: Iterable[str],
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    tool_policy: Any = None,
    admin: bool = True,
) -> List[str]:
    """`candidates` narrowed to the exact permitted set, order preserved,
    de-duplicated."""
    out: List[str] = []
    for raw in candidates:
        name = str(raw)
        if name and name not in out and is_permitted(
            name, disabled_tools=disabled_tools, tool_policy=tool_policy, admin=admin,
        ):
            out.append(name)
    return out


def nearest_by_name_or_capability(
    query: str,
    *,
    pool: Optional[Iterable[str]] = None,
    n: int = FALLBACK_N,
) -> List[str]:
    """A09 — the N tools whose NAME or CAPABILITY (one-line description)
    text is closest to `query`. Bounded to `n`; never the whole catalog.

    Pure ranking, no side effects, no execution — this is what the "explicit
    bounded fallback" is built from when nothing in the index actually
    matched.
    """
    names = sorted({str(x) for x in (pool or _all_known_tool_names()) if x})
    if not names:
        return []
    if not query.strip():
        return names[: max(1, n)]

    ql = query.lower().strip()
    try:
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    except Exception:
        BUILTIN_TOOL_DESCRIPTIONS = {}

    tokens = [t for t in ql.split() if len(t) > 2]
    scored: List[Tuple[float, str]] = []
    for name in names:
        name_score = difflib.SequenceMatcher(None, ql, name.lower()).ratio()
        desc = str(BUILTIN_TOOL_DESCRIPTIONS.get(name) or "").lower()
        cap_score = 0.0
        if desc and tokens:
            hits = sum(1 for t in tokens if t in desc)
            cap_score = hits / len(tokens)
        scored.append((max(name_score, cap_score), name))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [name for _score, name in scored[: max(1, n)]]


@dataclass
class DiscoveryAudit:
    """The `tool_discovery` audit record: {query, candidates, resolved, reason}.

    `candidates` is what the index/catalog returned before the permission
    filter; `resolved` is what actually got promoted (a subset, possibly
    empty); `reason` explains the gap between the two in one sentence, or
    (when `resolved` is empty because nothing matched at all) names the
    bounded fallback that was offered instead.
    """

    query: str
    candidates: List[str] = field(default_factory=list)
    resolved: List[str] = field(default_factory=list)
    reason: str = ""
    fallback: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "candidates": list(self.candidates),
            "resolved": list(self.resolved),
            "reason": self.reason,
            "fallback": list(self.fallback),
        }


def audit_selection(
    query: str,
    raw_candidates: Sequence[str],
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    tool_policy: Any = None,
    admin: bool = True,
    fallback_pool: Optional[Iterable[str]] = None,
) -> DiscoveryAudit:
    """The full A08/A09 decision in one call.

    1. Permission-filter `raw_candidates` (what the index/catalog already
       found) down to what this turn may actually run — A08.
    2. If nothing survives, attach a bounded (N=5) "closest by name or
       capability" fallback and an explicit reason — A09 — instead of an
       empty result or (the failure mode this exists to prevent) a dump of
       every schema so the model can "just find it itself".

    Never executes anything and never invents a tool name: `resolved` is
    always a subset of `raw_candidates`, and `fallback` is always a subset
    of the known catalog (`nearest_by_name_or_capability`).
    """
    candidates = [str(c) for c in raw_candidates if c]
    resolved = permitted_names(
        candidates, disabled_tools=disabled_tools, tool_policy=tool_policy, admin=admin,
    )
    if resolved:
        return DiscoveryAudit(
            query=query, candidates=candidates, resolved=resolved,
            reason="exact permitted schema loaded for the resolved tool(s)",
        )

    near = nearest_by_name_or_capability(query, pool=fallback_pool, n=FALLBACK_N)
    unknown = [c for c in candidates if not is_known_tool(c)]
    denied = [c for c in candidates if c not in unknown]
    if unknown and not denied:
        # A09: the model named something that is not a real tool at all —
        # never resolve/promote it (that would be the fabricated call this
        # exists to prevent).
        reason = (
            f"no such tool ({', '.join(unknown)}); "
            f"{len(near)} closest tool(s) by name/capability offered as a "
            "bounded fallback instead — no tool was invented and none was called"
            if near else
            f"no such tool ({', '.join(unknown)}) and no close tool name/capability found"
        )
    elif candidates:
        reason = (
            "candidate(s) found but none is permitted for this turn "
            "(disabled_tools/tool_policy/admin denylist); "
            f"{len(near)} closest tool(s) by name/capability offered instead"
        )
    else:
        reason = (
            f"no match for the query; {len(near)} closest tool(s) by "
            "name/capability offered as a bounded fallback — no tool was "
            "invented and none was called"
            if near else
            "no match for the query and no close tool name/capability found"
        )
    return DiscoveryAudit(query=query, candidates=candidates, resolved=[], reason=reason, fallback=near)
