"""One policy for every round the agent loop spends beyond a model's own tool call (H13).

A round the model did not ask for is an EXTRA round: the runtime grants it to
repair an answer, recover a transport, continue a cut-off reply, verify a
change or extend an exhausted budget. Each one has to be able to say why it
happened and how much of its budget it used, and a reason the runtime does not
know has to be refused rather than silently granted.

This module is pure. It imports no settings, no loop state and no I/O. The
loop hands it the counter it already keeps for the cause and the limit that
cause already has; the policy answers whether a further round is granted and
what to record.

    decide_extra_round(cause, used=0, limit=2)
      -> ExtraRoundDecision(granted, cause, family, used, limit, remaining, reason)

`ExtraRounds` keeps the turn's receipts and applies the mode:

    legacy   the loop's own `used < limit` decides and nothing is recorded;
    shadow   the same, plus the policy's decision is computed, compared and
             recorded (a divergence is logged and counted);
    enforce  the policy decides.

Causes live in `CAUSES`, one place. A cause the registry does not know is
never granted in `enforce` mode; `legacy`/`shadow` keep the loop's own answer
but count it as unknown so the gap shows up in the receipt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

MODES = ("legacy", "shadow", "enforce")

#: family -> what kind of extra round it is
FAMILIES = {
    "budget": "the turn's own step budget is extended",
    "transport": "the model call failed or was cut off and is redone",
    "correction": "the model's answer is sent back to be corrected",
    "verification": "a verification of the work found something to fix",
    "completion": "the completion checks found work still missing",
}


@dataclass(frozen=True)
class Cause:
    family: str
    description: str
    #: True when the round is added on top of the step budget instead of
    #: consuming one of the rounds the turn was given.
    outside_budget: bool = False


#: The closed set of causes. Adding a cause is adding a line here.
CAUSES: Mapping[str, Cause] = {
    # the step budget
    "configured_cycle": Cause("budget", "a configured continuation cycle extends the exhausted step budget"),
    "progress": Cause("budget", "the turn is still making progress, so the exhausted step budget is extended"),
    # transport
    "engine_lost_recovered": Cause("transport", "the local engine died mid-round and was restarted", True),
    "image_input_refused": Cause("transport", "the model refused image input; retried without the images", True),
    "context_overflow_compact": Cause("transport", "the request was too long; compacted harder and retried", True),
    "degenerate_output_retry": Cause("transport", "the model produced degenerate output; the round is redone", True),
    "think_cutoff": Cause("transport", "thinking hit its time budget; the round is redone with less thinking", True),
    "empty_completion": Cause("transport", "the round returned nothing; one nudge before giving up"),
    "length": Cause("transport", "the reply was cut off by the token limit and is continued"),
    # corrections
    "approval_echo": Cause("correction", "the model repeated the approval card instead of answering"),
    "local_no_cost_stop": Cause("correction", "the model invented a billing stop this runtime does not have"),
    "hallucinated_tool": Cause("correction", "the model called a tool that does not exist"),
    "claim_rejection": Cause("correction", "the answer claimed work the tools did not show"),
    "execution_recovery": Cause("correction", "a fresh execution cycle after the rejections were used up"),
    "language_mismatch": Cause("correction", "the reply came back in another language than the request"),
    "intent_nudge": Cause("correction", "the model announced an action and stopped without taking it"),
    "memory_lookup_dropped": Cause("correction", "a memory lookup the request did not ask for was dropped and the turn answers"),
    "context_echo_nudge": Cause("correction", "the reply was only the context separator; one nudge to answer"),
    "context_echo_retry": Cause("correction", "the separator came back twice; one retry with the untrusted context removed"),
    "no_action_nudge": Cause("correction", "a workspace request was answered in prose with no tool call"),
    "objective_unavailable": Cause("correction", "the model claimed objective changes the turn could not make"),
    "objective_apply": Cause("correction", "the user asked to change the project objectives and no apply call ran"),
    "answer_rewrite": Cause("correction", "the answer failed a factual check (weekday, calculation, figures) and is rewritten"),
    "target_substitution": Cause("correction", "other files were changed while the named file does not exist"),
    "advisor_final": Cause("correction", "the advisor found something to correct before the final answer"),
    "loop_recovery": Cause("correction", "a stalled investigation was skipped and the turn is redirected to a real change"),
    # verification
    "syntax_fix": Cause("verification", "the changed files do not parse"),
    "ui_smoke_fix": Cause("verification", "the smoke crawl of the changed page failed"),
    "static_fix": Cause("verification", "static analysis of the changed files found errors"),
    "tests_fix": Cause("verification", "the project's tests failed after the change"),
    "review_fix": Cause("verification", "the automatic review found blocking issues"),
    # completion
    "completion_continue": Cause("completion", "the completion engine found requirements not yet satisfied"),
    "plan_coverage": Cause("completion", "the plan has steps the turn did not cover"),
}


@dataclass(frozen=True)
class ExtraRoundDecision:
    granted: bool
    cause: str
    family: str
    used: int
    limit: int
    remaining: int
    reason: str  # ok | budget_exhausted | unknown_cause | bad_budget

    def fields(self) -> Dict[str, Any]:
        """What an event carries so the round can be audited afterwards."""
        return {"cause": self.cause, "family": self.family, "budget_used": self.used,
                "budget_limit": self.limit, "budget_remaining": self.remaining}


def _as_count(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    return None


def decide_extra_round(cause: Any, *, used: Any, limit: Any) -> ExtraRoundDecision:
    """Is a further extra round of `cause` granted?

    `used` is the number of extra rounds of this cause already granted in the
    turn and `limit` the most it may have. A cause the registry does not know,
    or a budget that is not a non-negative integer, is refused.
    """
    name = cause if isinstance(cause, str) else ""
    known = CAUSES.get(name)
    used_n, limit_n = _as_count(used), _as_count(limit)
    if known is None:
        return ExtraRoundDecision(False, name or "unknown", "unknown", used_n or 0, limit_n or 0, 0, "unknown_cause")
    if used_n is None or limit_n is None:
        return ExtraRoundDecision(False, name, known.family, used_n or 0, limit_n or 0, 0, "bad_budget")
    remaining = max(0, limit_n - used_n)
    if used_n >= limit_n:
        return ExtraRoundDecision(False, name, known.family, used_n, limit_n, 0, "budget_exhausted")
    return ExtraRoundDecision(True, name, known.family, used_n, limit_n, remaining, "ok")


class ExtraRounds:
    """The turn's extra-round receipts and the mode that applies the policy."""

    def __init__(self, mode: str = "enforce") -> None:
        self.mode = mode if mode in MODES else "enforce"
        self.records: List[Dict[str, Any]] = []
        self.refusals: List[Dict[str, Any]] = []
        self.divergences: List[Dict[str, Any]] = []
        self.unknown_causes: List[str] = []

    # -- asking ---------------------------------------------------------------
    def permits(self, cause: str, used: Any, limit: Any) -> bool:
        """May the loop take another extra round of `cause`? No side effects
        on the receipts except a divergence or an unknown cause being noted."""
        decision = decide_extra_round(cause, used=used, limit=limit)
        legacy = self._legacy(used, limit)
        if decision.reason == "unknown_cause" and cause not in self.unknown_causes:
            self.unknown_causes.append(str(cause))
            logger.warning("[extra-rounds] unknown cause %r", cause)
        if self.mode == "legacy":
            return legacy
        if self.mode == "shadow":
            if decision.granted != legacy and len(self.divergences) < 20:
                self.divergences.append({"cause": str(cause), "used": used, "limit": limit,
                                         "policy": decision.granted, "legacy": legacy})
                logger.warning("[extra-rounds] policy and legacy disagree: %s", self.divergences[-1])
            return legacy
        if not decision.granted and decision.reason in ("unknown_cause", "bad_budget"):
            self.refusals.append({"cause": str(cause), "reason": decision.reason})
        return decision.granted

    @staticmethod
    def _legacy(used: Any, limit: Any) -> bool:
        try:
            return int(used) < int(limit)
        except (TypeError, ValueError):
            return False

    # -- recording ------------------------------------------------------------
    def preview(self, cause: str, *, used: Any, limit: Any) -> Dict[str, Any]:
        """The fields an event would carry, without recording a round. For an
        event announced before the round it belongs to is actually started."""
        if self.mode == "legacy":
            return {}
        known = CAUSES.get(cause if isinstance(cause, str) else "")
        used_n, limit_n = _as_count(used) or 0, _as_count(limit) or 0
        return {"cause": cause if known else "unknown", "family": known.family if known else "unknown",
                "budget_used": used_n, "budget_limit": limit_n, "budget_remaining": max(0, limit_n - used_n)}

    def event(self, cause: str, *, used: Any, limit: Any, round_num: Optional[int] = None) -> Dict[str, Any]:
        """Record a granted extra round and return the fields its event carries.

        `used` is the counter AFTER this round was counted. In `legacy` mode
        nothing is recorded and nothing is added to the event.
        """
        if self.mode == "legacy":
            return {}
        known = CAUSES.get(cause if isinstance(cause, str) else "")
        used_n, limit_n = _as_count(used) or 0, _as_count(limit) or 0
        fields = {"cause": cause if known else "unknown", "family": known.family if known else "unknown",
                  "budget_used": used_n, "budget_limit": limit_n, "budget_remaining": max(0, limit_n - used_n)}
        self.records.append({**fields, "round": round_num})
        fields["extra_rounds_total"] = len(self.records)
        return fields

    def extension_event(self, reason: Any, *, cycles_left: int, rounds_granted: int,
                        round_num: Optional[int] = None) -> Dict[str, Any]:
        """Record a step-budget extension and return the fields its event carries.

        `reason` is the pure extension decision's own reason. Only the two
        budget causes are extensions; anything else is recorded as unknown so
        the audit shows it. `cycles_left` is the configured cycles still
        available after this one (-1 = unlimited).
        """
        if self.mode == "legacy":
            return {}
        known = CAUSES.get(reason) if isinstance(reason, str) else None
        cause = reason if known is not None and known.family == "budget" else "unknown"
        fields = {"cause": cause, "family": "budget" if cause != "unknown" else "unknown",
                  "cycles_left": int(cycles_left), "rounds_granted": int(rounds_granted)}
        self.records.append({**fields, "round": round_num})
        fields["extra_rounds_total"] = len(self.records)
        return fields

    # -- reading --------------------------------------------------------------
    def total(self) -> int:
        return len(self.records)

    def by_cause(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for record in self.records:
            out[record["cause"]] = out.get(record["cause"], 0) + 1
        return out

    def summary(self) -> Dict[str, Any]:
        return {"mode": self.mode, "total": self.total(), "by_cause": self.by_cause(),
                "refused": list(self.refusals), "divergences": list(self.divergences),
                "unknown_causes": list(self.unknown_causes)}
