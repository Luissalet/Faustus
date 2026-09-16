"""harness_evolution/evaluator.py — compare against baseline on source AND
held-out tasks (A27).

`evaluate()` never promotes anything and never decides pass/fail on its
own opinion — it runs a caller-supplied `runner` (the "external task
runner": production wires this to the real eval harness; a test wires it to
a deterministic fake) over two task sets and records exactly what came
back. The shape it returns is the one the contract asks for:

    evaluation = {"source": {task_id: bool}, "held_out": {task_id: bool},
                  "cost": float, "notes": str}

plus two summary booleans (`source_pass`, `held_out_pass`) so
`promotion.py`/`service.py` never has to re-derive "did the source task
pass" from the same dict two different ways. Passing SOURCE alone is
explicitly NOT enough to promote (A27's whole point) — `eligible` is True
only when both summaries are True.

The evaluation is returned as a plain dict specifically so it can be stored
verbatim on `CandidatePatch.evaluation` and read back later — "comparison
evidence preserved" (A27) means a rejected-for-held-out-failure candidate's
full per-task record survives, not just a boolean.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Sequence

TaskRunner = Callable[[str], bool]


@dataclass(frozen=True)
class EvaluationOutcome:
    source: Dict[str, bool]
    held_out: Dict[str, bool]
    cost: float
    notes: str
    source_pass: bool
    held_out_pass: bool

    @property
    def eligible(self) -> bool:
        """A26's schema gate and A27's held-out gate are two different
        refusals; this is the second one. `not held_out` (an empty
        held-out set) never counts as a pass — a candidate with nothing
        evaluated on held-out tasks is not eligible either, so an evaluator
        cannot be "passed" by giving it no held-out tasks to fail."""
        return bool(self.source) and self.source_pass and bool(self.held_out) and self.held_out_pass

    def to_dict(self) -> Dict[str, object]:
        return {"source": dict(self.source), "held_out": dict(self.held_out),
                "cost": self.cost, "notes": self.notes,
                "source_pass": self.source_pass, "held_out_pass": self.held_out_pass,
                "eligible": self.eligible}


def _run_all(runner: TaskRunner, task_ids: Sequence[str]) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    for task_id in task_ids:
        results[task_id] = bool(runner(task_id))
    return results


def evaluate(*, runner: TaskRunner, source_task_ids: Sequence[str],
            held_out_task_ids: Sequence[str], notes: str = "") -> EvaluationOutcome:
    """Run `runner` over every source and held-out task id, real per-call
    timing folded into `cost` (wall-clock seconds — a stand-in a caller may
    override by wrapping `runner`; this module does not invent a token/
    dollar cost model that belongs to `src/scorecard.py`, not here)."""
    started = time.monotonic()
    source = _run_all(runner, source_task_ids)
    held_out = _run_all(runner, held_out_task_ids)
    cost = time.monotonic() - started
    return EvaluationOutcome(
        source=source, held_out=held_out, cost=cost, notes=notes,
        source_pass=bool(source) and all(source.values()),
        held_out_pass=bool(held_out) and all(held_out.values()),
    )


def outcome_from_dict(data: Mapping[str, object]) -> EvaluationOutcome:
    """Read a previously-stored `CandidatePatch.evaluation` back into an
    `EvaluationOutcome` — used to re-check `.eligible` without re-running
    anything, e.g. when `service.py` loads a patch from the store."""
    source = dict(data.get("source") or {})
    held_out = dict(data.get("held_out") or {})
    return EvaluationOutcome(
        source=source, held_out=held_out, cost=float(data.get("cost") or 0.0),
        notes=str(data.get("notes") or ""),
        source_pass=bool(source) and all(source.values()),
        held_out_pass=bool(held_out) and all(held_out.values()),
    )
