"""self_declared_risk.py -- the model's own risk estimate, as a one-way lever.

Every tool that changes state (writes, shell, network sends, external side
effects) is offered with one extra optional parameter, ``security_risk`` --
``LOW | MEDIUM | HIGH | UNKNOWN`` -- added here, centrally, where the loop
assembles the schemas it sends; no per-tool schema is edited. A model that
knows a call is destructive or leaves the machine can say so.

The declaration can only RAISE the gate, never lower it:

* ``HIGH`` forces the approval card for that call even when the policy would
  have run it, unless the person already chose otherwise (approval mode
  ``full``, or an "allow for this task" answer on a card of this run -- both
  are explicit and stay honoured).
* ``LOW`` / ``MEDIUM`` / ``UNKNOWN`` change nothing: they never skip a check,
  never shorten a card, never turn a denial into an approval.

The parameter is removed from the arguments before the tool runs (an MCP
server never sees it, a sealed approval never contains it). What the model
declared and what the policy thought are recorded per call, so a turn's
disagreements can be counted (``stats()``, ``GET /api/agent/risk/stats``,
``metrics["risk_declarations"]``).
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

PARAM = "security_risk"
LEVELS = ("LOW", "MEDIUM", "HIGH", "UNKNOWN")
_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

DEFAULTS: Dict[str, Any] = {"self_declared_risk": True}

PARAM_DESCRIPTION = (
    "Optional. Your own estimate of how risky this call is: LOW (local, easy to undo), "
    "MEDIUM (changes files or data, recoverable), HIGH (destructive, irreversible, sends "
    "data out, spends money or affects others), UNKNOWN. Declaring HIGH asks the user first."
)

#: Most recent declarations kept for the stats route.
RECENT_LIMIT = 40


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("self_declared_risk", DEFAULTS["self_declared_risk"]))
    except Exception:  # noqa: BLE001 - unreadable settings = the default
        return bool(DEFAULTS["self_declared_risk"])


def normalize(value: Any) -> Optional[str]:
    """``LOW|MEDIUM|HIGH|UNKNOWN`` from whatever the model wrote, else None."""
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    return v if v in LEVELS else None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

def _state_changing_effects() -> frozenset:
    from src.tool_capabilities import ToolEffect
    return frozenset({
        ToolEffect.WRITE_WORKSPACE, ToolEffect.WRITE_PRIVATE, ToolEffect.EXECUTE_CODE,
        ToolEffect.NETWORK_EGRESS, ToolEffect.EXTERNAL_SIDE_EFFECT,
        ToolEffect.ADMIN_CHANGE, ToolEffect.DESTRUCTIVE,
    })


def wants_param(tool_name: Any) -> bool:
    """True for a tool that changes state or whose effects are not classified
    (unclassified fails high everywhere else in the gate)."""
    try:
        from src.tool_capabilities import capabilities_for_tool
        caps = capabilities_for_tool(tool_name)
        return (not caps.known) or bool(caps.effects & _state_changing_effects())
    except Exception:  # noqa: BLE001
        return False


def with_risk_param(schemas: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The schemas with ``security_risk`` added to the state-changing tools.
    Deep-copies only the schemas it edits (the originals are module-level
    singletons shared by every request) and never replaces an existing
    parameter of that name."""
    out: List[Dict[str, Any]] = []
    for schema in list(schemas or []):
        try:
            fn = schema.get("function") if isinstance(schema, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            params = fn.get("parameters") if isinstance(fn, dict) else None
            if (not name or not isinstance(params, dict) or params.get("type") not in (None, "object")
                    or PARAM in (params.get("properties") or {}) or not wants_param(name)):
                out.append(schema)
                continue
            edited = copy.deepcopy(schema)
            eparams = edited["function"]["parameters"]
            eparams.setdefault("type", "object")
            props = eparams.get("properties")
            if not isinstance(props, dict):
                props = eparams["properties"] = {}
            props[PARAM] = {"type": "string", "enum": list(LEVELS), "description": PARAM_DESCRIPTION}
            out.append(edited)
        except Exception:  # noqa: BLE001 - a schema that cannot be edited is sent as it was
            out.append(schema)
    return out


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def strip_from_arguments(arguments: Any) -> Tuple[Any, Optional[str]]:
    """``(arguments without the parameter, declared level or None)``. Text that
    is not a JSON object is returned untouched."""
    if isinstance(arguments, dict):
        if PARAM not in arguments:
            return arguments, None
        cleaned = {k: v for k, v in arguments.items() if k != PARAM}
        return cleaned, normalize(arguments.get(PARAM))
    if not isinstance(arguments, str) or PARAM not in arguments:
        return arguments, None
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return arguments, None
    if not isinstance(parsed, dict) or PARAM not in parsed:
        return arguments, None
    declared = normalize(parsed.pop(PARAM))
    return json.dumps(parsed, ensure_ascii=False), declared


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def policy_risk(tool_name: Any, content: Any, *, gate_allowed: bool = True) -> str:
    """What the policy itself makes of the call: HIGH when the gate already
    refuses it or it is destructive / administrative / leaves the machine,
    MEDIUM for code and writes, LOW for everything else, UNKNOWN when the
    tool is not classified."""
    try:
        from src.tool_capabilities import ToolEffect, capabilities_for_action
        caps = capabilities_for_action(tool_name, content)
    except Exception:  # noqa: BLE001
        return "UNKNOWN"
    if not gate_allowed:
        return "HIGH"
    if not caps.known:
        return "UNKNOWN"
    effects = caps.effects
    if effects & {ToolEffect.DESTRUCTIVE, ToolEffect.ADMIN_CHANGE,
                  ToolEffect.EXTERNAL_SIDE_EFFECT, ToolEffect.NETWORK_EGRESS}:
        return "HIGH"
    if effects & {ToolEffect.EXECUTE_CODE, ToolEffect.WRITE_WORKSPACE, ToolEffect.WRITE_PRIVATE}:
        return "MEDIUM"
    return "LOW"


def raises_gate(declared: Optional[str]) -> bool:
    return declared == "HIGH"


def raise_decision(decision: Any, declared: Optional[str], tool_name: Any, *,
                   approval_mode: str = "ask", gate_bypassed: bool = False) -> Tuple[Any, bool]:
    """``(decision, forced)``. ``forced`` is True only when a HIGH declaration
    turned an allowed decision into a card. A decision that already refuses is
    returned as it is (its own reason keeps showing); the person's explicit
    choices (mode ``full``, an "allow for this task" answer) are honoured."""
    if (not enabled() or not raises_gate(declared) or not getattr(decision, "allowed", False)
            or gate_bypassed or approval_mode == "full"):
        return decision, False
    from src.tool_capabilities import ToolGateDecision
    return ToolGateDecision(False, (
        f"The assistant flagged this call to '{tool_name}' as HIGH risk (destructive, irreversible "
        "or leaving your machine), so it is asking you first. Approve this exact action to run it.")), True


# ---------------------------------------------------------------------------
# Record + counters
# ---------------------------------------------------------------------------

def make_record(tool_name: Any, declared: Optional[str], policy: str, *, forced: bool,
                round_num: Optional[int] = None) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "tool": str(tool_name or ""),
        "declared": declared or "NONE",
        "policy": policy,
        "forced_approval": bool(forced),
        "agreement": agreement(declared, policy),
    }
    if round_num is not None:
        rec["round"] = round_num
    return rec


def agreement(declared: Optional[str], policy: str) -> str:
    """``none`` (nothing declared), ``same``, ``higher`` (the model saw more
    risk than the policy) or ``lower`` (less), ``unknown`` otherwise."""
    if not declared:
        return "none"
    if declared not in _RANK or policy not in _RANK:
        return "unknown"
    if _RANK[declared] == _RANK[policy]:
        return "same"
    return "higher" if _RANK[declared] > _RANK[policy] else "lower"


class _Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        lock = getattr(self, "_lock", None) or threading.Lock()
        with lock:
            self._lock = lock
            self.calls = 0
            self.declared = 0
            self.forced = 0
            self.by_declared: Dict[str, int] = {}
            self.by_agreement: Dict[str, int] = {}
            self.by_tool: Dict[str, Dict[str, int]] = {}
            self.recent: Deque[Dict[str, Any]] = deque(maxlen=RECENT_LIMIT)

    def note(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self.calls += 1
            if rec["declared"] != "NONE":
                self.declared += 1
            if rec["forced_approval"]:
                self.forced += 1
            self.by_declared[rec["declared"]] = self.by_declared.get(rec["declared"], 0) + 1
            self.by_agreement[rec["agreement"]] = self.by_agreement.get(rec["agreement"], 0) + 1
            row = self.by_tool.setdefault(rec["tool"], {"calls": 0, "declared": 0, "disagreed": 0})
            row["calls"] += 1
            if rec["declared"] != "NONE":
                row["declared"] += 1
            if rec["agreement"] in ("higher", "lower"):
                row["disagreed"] += 1
            self.recent.append(dict(rec))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            disagreed = self.by_agreement.get("higher", 0) + self.by_agreement.get("lower", 0)
            return {
                "calls": self.calls, "declared": self.declared, "forced_approval": self.forced,
                "declaration_rate": round(self.declared / self.calls, 4) if self.calls else None,
                "disagreements": disagreed,
                "by_declared": dict(self.by_declared), "by_agreement": dict(self.by_agreement),
                "by_tool": {k: dict(v) for k, v in self.by_tool.items()},
                "recent": list(self.recent),
            }


_STATS = _Stats()


def record(rec: Dict[str, Any]) -> Dict[str, Any]:
    _STATS.note(rec)
    return rec


def stats() -> Dict[str, Any]:
    out = _STATS.snapshot()
    out["enabled"] = enabled()
    return out


def _reset_stats() -> None:
    _STATS.reset()


__all__ = [
    "DEFAULTS", "LEVELS", "PARAM", "agreement", "enabled", "make_record", "normalize",
    "policy_risk", "raise_decision", "raises_gate", "record", "stats",
    "strip_from_arguments", "wants_param", "with_risk_param",
]
