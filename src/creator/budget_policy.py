"""budget_policy.py — WP09: per-project spend policy over ``budget_account``.

Sits ABOVE ``src.budget_account`` — the existing per-run token/cost ledger
(A31), unchanged here (CONTRATO.md rule 1: no parallel authority) — and
answers one question `preflight.py` needs: given an estimate, may this
Creator operation proceed outright (``allow``), does it need a human yes
first (``ask``), or is it refused outright (``deny``) against the project's
own policy? ``decide()`` is pure: it reads nothing and writes nothing. The
real writes — reserving a slice of the ledger before an operation starts,
reconciling it once real usage is known — are `reserve`/`reconcile` below,
thin wrappers over ``budget_account.reserve``/``budget_account.reconcile``
that this module's own callers (the execution path, a later WP) use; nothing
in `run_preflight` or `decide()` calls them.

**One ledger per project.** `budget_account`'s own unit is a `run_id`; a
Creator project's whole production shares one (`run_id_for`), the same way a
coordinator's whole delegated run shares one ledger in A31 — an operation
inside the same project is a child reservation on that project's row, not a
new run.

**Modes**, read off the project's Creator profile (WP02's
``CreatorProfile``, passed in by the caller — this module does not import
``src.creator.profile`` itself, to stay decoupled from that store's own
concurrency model):

* ``observe`` (default) — accounting only, never asks, never denies.
* ``warn`` — asks for approval once the estimate crosses the project's
  ceiling, but never refuses outright.
* ``limit`` — refuses outright once the estimate crosses the ceiling.

Independent of mode, an estimate that crosses the project's
``approval_threshold_tokens`` always asks (a ceiling is a hard stop; a
threshold is "tell a human", and the two are separate knobs on purpose — a
project can want to be told about anything over $5 without capping spend
outright). A cloud engine always asks too, regardless of tokens — the
``cloud_model`` `ApprovalPlan.action` (see `src.contracts.approval`), not
`cost_over_budget`, because leaving the machine is its own reason to ask,
even when the estimate itself is fine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

__all__ = ["MODES", "Decision", "decide", "run_id_for", "reserve", "reconcile", "snapshot"]

MODES = ("observe", "warn", "limit")
DEFAULT_MODE = "observe"
DEFAULT_APPROVAL_THRESHOLD_TOKENS: Optional[int] = None  # None = never ask on tokens alone
DEFAULT_CEILING_TOKENS = 0  # 0 = no ceiling


def run_id_for(project_id: str) -> str:
    """The ``budget_account`` run_id a project's whole Creator spend is
    booked under — one per project, never one per operation."""
    return f"creator:{str(project_id or '').strip()}"


@dataclass(frozen=True)
class Decision:
    verdict: str              # "allow" | "ask" | "deny"
    reason: str
    #: The `ApprovalPlan.action` to use when `verdict == "ask"` — "" otherwise.
    action: str
    mode: str
    threshold_tokens: Optional[int]
    ceiling_tokens: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict, "reason": self.reason, "action": self.action,
            "mode": self.mode, "threshold_tokens": self.threshold_tokens,
            "ceiling_tokens": self.ceiling_tokens,
        }


def _profile_settings(profile: Mapping[str, Any]) -> Dict[str, Any]:
    profile = profile or {}
    mode = str(profile.get("budget_mode") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        mode = DEFAULT_MODE

    threshold_raw = profile.get("approval_threshold_tokens")
    try:
        threshold = int(threshold_raw) if threshold_raw is not None else DEFAULT_APPROVAL_THRESHOLD_TOKENS
    except (TypeError, ValueError):
        threshold = DEFAULT_APPROVAL_THRESHOLD_TOKENS
    if threshold is not None:
        threshold = max(0, threshold)

    ceiling_raw = profile.get("production_ceiling_tokens")
    try:
        ceiling = int(ceiling_raw) if ceiling_raw is not None else DEFAULT_CEILING_TOKENS
    except (TypeError, ValueError):
        ceiling = DEFAULT_CEILING_TOKENS
    ceiling = max(0, ceiling)

    return {"mode": mode, "threshold_tokens": threshold, "ceiling_tokens": ceiling}


def decide(*, project_id: str, engine: str, estimated_tokens: Optional[int],
           cloud_engine: bool = False, profile: Optional[Mapping[str, Any]] = None) -> Decision:
    """Pure — no store read, no store write. See module docstring for the
    mode/threshold/ceiling rules this implements."""
    settings = _profile_settings(profile or {})
    mode, threshold, ceiling = settings["mode"], settings["threshold_tokens"], settings["ceiling_tokens"]

    if cloud_engine:
        return Decision(
            verdict="ask", reason=f"{engine!r} is a cloud engine; inference would leave this machine",
            action="cloud_model", mode=mode, threshold_tokens=threshold, ceiling_tokens=ceiling,
        )

    over_threshold = bool(threshold and estimated_tokens is not None and estimated_tokens > threshold)
    over_ceiling = bool(ceiling and estimated_tokens is not None and estimated_tokens > ceiling)

    if mode == "limit" and over_ceiling:
        return Decision(
            verdict="deny",
            reason=f"estimated {estimated_tokens} tokens exceeds the production ceiling of {ceiling}",
            action="", mode=mode, threshold_tokens=threshold, ceiling_tokens=ceiling,
        )

    if over_threshold:
        return Decision(
            verdict="ask",
            reason=f"estimated {estimated_tokens} tokens exceeds the approval threshold of {threshold}",
            action="cost_over_budget", mode=mode, threshold_tokens=threshold, ceiling_tokens=ceiling,
        )

    if mode == "warn" and over_ceiling:
        return Decision(
            verdict="ask",
            reason=(f"estimated {estimated_tokens} tokens exceeds the production ceiling of "
                    f"{ceiling} (budget_mode=warn)"),
            action="cost_over_budget", mode=mode, threshold_tokens=threshold, ceiling_tokens=ceiling,
        )

    if mode == "observe":
        return Decision(verdict="allow", reason="budget_mode=observe: accounting only, never gates",
                         action="", mode=mode, threshold_tokens=threshold, ceiling_tokens=ceiling)

    return Decision(verdict="allow", reason="within budget", action="",
                     mode=mode, threshold_tokens=threshold, ceiling_tokens=ceiling)


# ── execution-time writes (used by the execution path, not by preflight) ──

def reserve(project_id: str, child_id: str, tokens: int, cost: Optional[float] = None,
            *, ceiling_tokens: Optional[int] = None):
    """Reserve `tokens` against `project_id`'s ledger before an operation
    starts. Opens the run (idempotent) first if it is not open yet, so a
    project's first-ever reservation does not have to be preceded by a
    separate `open()` call by the caller."""
    from src import budget_account

    run_id = run_id_for(project_id)
    budget_account.open(run_id, ceiling_tokens=ceiling_tokens or 0, project_id=project_id)
    return budget_account.reserve(run_id, child_id, tokens, cost)


def reconcile(project_id: str, child_id: str, used_tokens: int,
              used_cost: Optional[float] = None) -> Dict[str, Any]:
    """Record what `child_id` actually used, freeing the reservation's
    surplus back to the project's ledger."""
    from src import budget_account

    return budget_account.reconcile(run_id_for(project_id), child_id, used_tokens, used_cost)


def snapshot(project_id: str) -> Dict[str, Any]:
    from src import budget_account

    return budget_account.snapshot(run_id_for(project_id))
