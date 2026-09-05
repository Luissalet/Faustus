"""B-007 — one place that answers "can this tool run right now?".

The trap this closes: a tool is offered in the turn's schema list and then
refuses itself when called. With `data/skills/ai-integration-setup` installed,
`suggest_document` was offered on turns with no open document, answered "No
active document to suggest on", and the model tried again — the same refusal,
a wasted round, and thirteen tests failing in one tree and no other.

The fix does NOT belong in the preflight. Preflight runs once, at the start of
the turn, and a document can be created *during* it: pruning the tool there
would remove a call that becomes legitimate two rounds later. That fix was
written, passed 89 tests, broke two in `test_external_context_tool_gate.py`,
and was reverted on purpose.

So availability is evaluated **at the point of use**, by the tool itself,
through `evaluate()`. When the answer is no, the tool returns
`unavailable_result()` instead of a bare error: a refusal that says which tool,
why, what would make it available, and — the part that ends the loop — that it
must not simply be retried. `src/agent_loop.py` reads that marker, drops the
tool from the NEXT round's schema list, and puts it back the moment a tool in
`restored_by` succeeds. The state transition is explicit in both directions.

Adding a rule is one entry in `_RULES`. Keep them cheap: this runs on the hot
path, inside the tool call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Tuple

#: The key a refusal carries so the loop can recognise it without string
#: matching on the message.
UNAVAILABLE_KEY = "tool_unavailable"


@dataclass(frozen=True)
class Availability:
    """Whether one tool can run right now, and what would change that."""

    tool: str
    available: bool
    reason: str = ""
    remedy: str = ""
    #: Tools whose SUCCESSFUL execution makes this one available again.
    restored_by: FrozenSet[str] = field(default_factory=frozenset)

    def as_result(self) -> Dict[str, Any]:
        """The refusal a tool returns instead of running.

        ``error`` keeps the old wording so anything that matched on it still
        matches; the rest is what makes the refusal actionable.
        """
        return {
            "error": self.reason,
            "exit_code": 1,
            UNAVAILABLE_KEY: True,
            "tool": self.tool,
            "state": "unavailable",
            "reason": self.reason,
            "remedy": self.remedy,
            "retry": False,
            "restored_by": sorted(self.restored_by),
        }


@dataclass(frozen=True)
class _Rule:
    #: Returns "" when the tool can run, or the reason it cannot.
    blocked_because: Callable[[Mapping[str, Any]], str]
    remedy: str
    restored_by: FrozenSet[str] = field(default_factory=frozenset)


def _no_document_target(ctx: Mapping[str, Any]) -> str:
    """Document tools need a target: the call's own, or the open editor."""
    if ctx.get("doc_id"):
        return ""
    if ctx.get("active_document_id"):
        return ""
    return "No active document to suggest on"


_DOCUMENT_OPENERS = frozenset({"create_document", "manage_documents"})

_RULES: Dict[str, _Rule] = {
    "suggest_document": _Rule(
        blocked_because=_no_document_target,
        remedy=(
            "Open or create a document first (create_document / "
            "manage_documents), or answer in chat. Calling suggest_document "
            "again with nothing open will fail the same way."
        ),
        restored_by=_DOCUMENT_OPENERS,
    ),
}


def evaluate(tool_name: Any, ctx: Optional[Mapping[str, Any]] = None) -> Availability:
    """Can `tool_name` run with this context? Never raises.

    A tool with no rule is available: this module says what is KNOWN to be
    conditional, and silence is not a refusal.
    """
    name = str(tool_name or "")
    rule = _RULES.get(name)
    if rule is None:
        return Availability(tool=name, available=True)
    try:
        reason = rule.blocked_because(ctx or {})
    except Exception:  # noqa: BLE001 - a broken rule must not block a tool
        return Availability(tool=name, available=True)
    if not reason:
        return Availability(tool=name, available=True)
    return Availability(tool=name, available=False, reason=reason,
                        remedy=rule.remedy, restored_by=rule.restored_by)


def unavailable_result(tool_name: Any,
                       ctx: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The refusal dict when the tool cannot run now, else None."""
    verdict = evaluate(tool_name, ctx)
    return None if verdict.available else verdict.as_result()


def is_unavailable(result: Any) -> bool:
    """Whether a tool result is one of these refusals."""
    return isinstance(result, dict) and bool(result.get(UNAVAILABLE_KEY))


def withdrawn_tool(result: Any) -> str:
    """The tool name a refusal names, or ""."""
    if not is_unavailable(result):
        return ""
    return str(result.get("tool") or "")


def restored_by(tool_name: Any) -> FrozenSet[str]:
    """Tools whose success makes `tool_name` available again."""
    rule = _RULES.get(str(tool_name or ""))
    return rule.restored_by if rule else frozenset()


def conditional_tools() -> Tuple[str, ...]:
    """Every tool whose availability depends on the moment. For tests and docs."""
    return tuple(sorted(_RULES))
