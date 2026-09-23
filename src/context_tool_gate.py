"""
context_tool_gate.py — the engine's own read-only tools versus the external-context gate.

`ToolRunSecurityContext.decision_for` (src/tool_capabilities.py) asks for a
human go-ahead on any READ_PRIVATE tool once untrusted content has entered the
run. The live Context Engine packet is itself untrusted context (it carries
memories, documents and files, and it is marked `tool_gate_untrusted`), so as
soon as `agent_context_engine` delivers a packet the gate is armed — and the
packet's own follow-up tool, `context_recall`, lands on an approval card the
first time the model asks for something the packet had listed as "omitted for
budget". That card protects nothing: the model already holds the item's title
and reference, the tool only returns content of the same trust class from the
same owner-scoped store, and it writes nothing and sends nothing anywhere.

So a small allow-rule table names exactly which calls are exempt from that one
step of the gate:

* ``context_recall`` — only when EVERY id in the call was offered by a packet
  delivered in THIS turn (the report's ``recallable`` list). An id the turn
  never offered — invented, pasted from an earlier turn, or injected by a
  document — keeps today's behaviour (a card in "ask" mode). The ids are parsed
  with the tool's own parser, so what is checked is what would run.

Each rule is one line. A rule can only ever lift a READ_PRIVATE block: calls
whose capabilities include any other gated effect (writes, execution,
egress…), shell tools behind the destructive-command guard and per-call
desktop tools are never exempt, whatever the table says. Everything after the
gate (argument policy, tool policy, ownership, workspace confinement) still
applies, and any write or egress the model attempts next still meets the gate.

`ContextAwareSecurityContext` is the run's security context with this table
consulted after the normal decision; with an empty offer set and no matching
rule it answers exactly like its parent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Set, Tuple

from src.tool_capabilities import (
    ALWAYS_APPROVE_TOOLS,
    GUARD_SHELL_TOOLS,
    POST_EXTERNAL_BLOCKED_EFFECTS,
    ToolEffect,
    ToolGateDecision,
    ToolRunSecurityContext,
    capabilities_for_action,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateAllowRule:
    """One read-only call shape that may pass the external-context gate.

    ``actions``: empty = any call of ``tool``; otherwise the call's ``action``
    argument must be one of these. ``offered_ids_only``: every id the call
    names must have been offered by a packet delivered in this turn."""

    tool: str
    actions: Tuple[str, ...] = ()
    offered_ids_only: bool = False
    why: str = ""


#: The table. Add a read-only context tool here with one line.
ALLOW_RULES: Tuple[GateAllowRule, ...] = (
    GateAllowRule("context_recall", offered_ids_only=True,
                  why="recalls an item this turn's packet listed as omitted"),
)

#: The only gated effect a rule may lift (see the module docstring).
LIFTABLE_EFFECTS = frozenset({ToolEffect.READ_PRIVATE})


def rule_for(tool_name: Any, rules: Iterable[GateAllowRule] = ALLOW_RULES
             ) -> Optional[GateAllowRule]:
    if not isinstance(tool_name, str):
        return None
    for rule in rules:
        if rule.tool == tool_name:
            return rule
    return None


def _arguments(content: Any) -> Mapping[str, Any]:
    if isinstance(content, Mapping):
        return content
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _action(content: Any) -> str:
    value = _arguments(content).get("action")
    return value.strip().lower() if isinstance(value, str) else ""


def recall_ids(content: Any) -> Tuple[str, ...]:
    """The normalised ids a `context_recall` call would recall — the tool's
    own parser, so the gate checks exactly what would run."""
    try:
        from src.agent_tools.context_recall_tools import _ids
        from src.context_engine.recall import normalize_id
    except Exception:  # noqa: BLE001 - no parser, no exemption
        return ()
    raw = _ids(content)
    ids = [normalize_id(value) for value in raw]
    if not ids or any(not ident for ident in ids):
        return ()
    # `recall()` ignores anything past its cap; the gate still insists that
    # every id named is known, so a padded call cannot smuggle one through.
    return tuple(ids)


def _liftable(tool_name: str, content: Any) -> bool:
    if tool_name in GUARD_SHELL_TOOLS or tool_name in ALWAYS_APPROVE_TOOLS:
        return False
    try:
        capabilities = capabilities_for_action(tool_name, content)
    except Exception:  # noqa: BLE001 - unknown is never exempt
        return False
    if not capabilities.known:
        return False
    blocked = capabilities.effects & POST_EXTERNAL_BLOCKED_EFFECTS
    return blocked <= LIFTABLE_EFFECTS


def allows(tool_name: Any, content: Any = None, *,
           offered_ids: Iterable[str] = (),
           rules: Iterable[GateAllowRule] = ALLOW_RULES) -> bool:
    """Does the table exempt this exact call from the external-context gate?"""
    rule = rule_for(tool_name, rules)
    if rule is None or not _liftable(rule.tool, content):
        return False
    if rule.actions and _action(content) not in rule.actions:
        return False
    if rule.offered_ids_only:
        ids = recall_ids(content)
        offered = set(offered_ids or ())
        if not ids or not offered or any(ident not in offered for ident in ids):
            return False
    return True


@dataclass
class ContextAwareSecurityContext(ToolRunSecurityContext):
    """The run's security context plus the ids this turn's packets offered.

    `decision_for` is the parent's, untouched, except that a call the parent
    would deny is let through when `allows` says the table exempts it."""

    offered_context_ids: Set[str] = field(default_factory=set)

    def offer_context_ids(self, ids: Iterable[Any]) -> None:
        """Record the `recallable` ids of a packet delivered in this turn."""
        try:
            from src.context_engine.recall import normalize_id
        except Exception:  # noqa: BLE001
            return
        for raw in ids or ():
            ident = normalize_id(raw)
            if ident:
                self.offered_context_ids.add(ident)

    def decision_for(self, tool_name: Any, content: Any = None) -> ToolGateDecision:
        decision = super().decision_for(tool_name, content)
        if decision.allowed:
            return decision
        try:
            if allows(tool_name, content, offered_ids=self.offered_context_ids):
                logger.info("[gate] %s passes: read-only context call covered by "
                            "the allow-rule table", tool_name)
                return ToolGateDecision(True)
        except Exception:  # noqa: BLE001 - a failing exemption keeps the denial
            logger.debug("context tool gate check failed", exc_info=True)
        return decision


__all__ = [
    "GateAllowRule", "ALLOW_RULES", "LIFTABLE_EFFECTS", "rule_for", "recall_ids",
    "allows", "ContextAwareSecurityContext",
]
