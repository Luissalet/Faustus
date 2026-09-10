"""
capability_promotion.py — EVAL-06: controlled promotion of capabilities.

A matrix keyed by the EXACT tuple (model, backend, template) — never a
prefix or a fuzzy match. The acceptance line names the failure mode this
guards against directly: "un modelo textual no hereda soporte de vision de
otro con nombre parecido ni un parser antiguo conserva sello compatible
tras romperse." Both halves are properties of how `_key` and `promote` are
written, not something a caller has to remember to check:

  * `_key` builds a plain string from the three fields joined by a
    separator no field is allowed to contain (`KeyError` if a caller tries)
    — "gpt-4-vision" and "gpt-4-vision-preview" are two different keys, full
    stop, never a startswith() or a partial match anywhere in this module.
  * A promotion NEVER reads a stale verdict: `promote` always requires the
    MOST RECENT calibration run recorded for that exact key to have
    `passed=True`, and `record_calibration` is the only way a tuple
    acquires evidence — a broken parser's next run records `passed=False`,
    and the tuple's state is demoted on the spot (see `_STATE_ON_FAILURE`),
    never left at its last-good badge because nobody happened to call
    `promote` again.

States (`STATES`): `not_tested` → `experimental` → `compatible` →
`recommended`, one step at a time — `promote` refuses to skip a state (a
tuple with no history cannot jump straight to `recommended`), which is what
"solo con evidencia vigente" means in practice: evidence for the state
you're already AT, not evidence in general.

This is deliberately a different concept from `src/capability_registry.py`
(which catalogues execution BACKENDS — docker/local/etc — as a whole, not
per model/template) and does not touch it (rule 4: adjacent, not
duplicated).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.contracts.base import now_iso

STATES = ("not_tested", "experimental", "compatible", "recommended")

#: The only single-step transitions `promote` allows, keyed by (from, to).
_ALLOWED_PROMOTIONS = {
    ("not_tested", "experimental"),
    ("experimental", "compatible"),
    ("compatible", "recommended"),
}

#: Where a failing calibration run sends a tuple, keyed by its state at the
#: time — always down, never sideways: a `recommended` tuple that breaks
#: drops all the way to `experimental` (not `compatible`), because the
#: failure itself is evidence the LAST promotion's confidence was
#: unwarranted, not just the most recent one.
_STATE_ON_FAILURE = {
    "not_tested": "not_tested", "experimental": "not_tested",
    "compatible": "experimental", "recommended": "experimental",
}

_SEP = "\x1f"  # a separator no model/backend/template name is expected to contain


class PromotionRefused(ValueError):
    """A promotion was asked for without the evidence or the state it needs."""


def _field_ok(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    if _SEP in value:
        raise ValueError(f"{label} must not contain the reserved separator")
    return value


def _key(model: str, backend: str, template: str) -> str:
    return _SEP.join((_field_ok(model, "model"), _field_ok(backend, "backend"),
                      _field_ok(template, "template")))


@dataclass
class Calibration:
    id: str
    passed: bool
    score: float
    details: str
    recorded_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "passed": self.passed, "score": self.score,
                "details": self.details, "recorded_at": self.recorded_at}


@dataclass
class CapabilityEntry:
    model: str
    backend: str
    template: str
    state: str = "not_tested"
    history: List[Calibration] = field(default_factory=list)
    promotions: List[Dict[str, Any]] = field(default_factory=list)

    def latest_calibration(self) -> Optional[Calibration]:
        return self.history[-1] if self.history else None

    def to_dict(self) -> Dict[str, Any]:
        latest = self.latest_calibration()
        return {"model": self.model, "backend": self.backend, "template": self.template,
                "state": self.state, "latest_calibration": latest.to_dict() if latest else None,
                "calibration_count": len(self.history), "promotions": list(self.promotions)}


class CapabilityMatrix:
    """In-process registry. A caller that wants persistence writes
    `entry.to_dict()` for every row somewhere of its own choosing (a JSON
    file, a DB table) — this class holds the RULES, not a storage
    mechanism, so it stays trivially testable and does not invent a second
    on-disk store for something that has no production caller yet."""

    def __init__(self) -> None:
        self._entries: Dict[str, CapabilityEntry] = {}

    def _entry(self, model: str, backend: str, template: str) -> CapabilityEntry:
        key = _key(model, backend, template)
        entry = self._entries.get(key)
        if entry is None:
            entry = CapabilityEntry(model=model, backend=backend, template=template)
            self._entries[key] = entry
        return entry

    def get(self, model: str, backend: str, template: str) -> CapabilityEntry:
        """Read-only lookup. NEVER falls back to a similarly-named tuple —
        a tuple never recorded here comes back as a fresh `not_tested`
        entry with no history, not as some other tuple's state."""
        key = _key(model, backend, template)
        existing = self._entries.get(key)
        if existing is not None:
            return existing
        return CapabilityEntry(model=model, backend=backend, template=template)

    def record_calibration(self, model: str, backend: str, template: str, *,
                           passed: bool, score: float = 0.0, details: str = "") -> Calibration:
        """The ONLY way a tuple acquires evidence. A failing run demotes the
        tuple immediately (see `_STATE_ON_FAILURE`) — a promotion is never
        left standing on evidence that has since been contradicted."""
        entry = self._entry(model, backend, template)
        calibration = Calibration(id=f"cal_{uuid.uuid4().hex[:12]}", passed=bool(passed),
                                  score=float(score), details=details, recorded_at=now_iso())
        entry.history.append(calibration)
        if not calibration.passed:
            entry.state = _STATE_ON_FAILURE[entry.state]
        return calibration

    def promote(self, model: str, backend: str, template: str, *,
               to_state: str, flag_enabled: bool) -> CapabilityEntry:
        """Move a tuple's state forward ONE step. Refuses (`PromotionRefused`)
        unless ALL of:

          * `flag_enabled` is True — a person turned the gate on for this
            promotion, never inferred from evidence alone;
          * `to_state` is the single next step from the tuple's CURRENT
            state (`_ALLOWED_PROMOTIONS`) — no skipping `experimental`;
          * the MOST RECENT calibration recorded for this exact tuple has
            `passed=True` — "evidencia vigente", not evidence that once
            existed.
        """
        if to_state not in STATES:
            raise ValueError(f"to_state must be one of {STATES}")
        entry = self._entry(model, backend, template)
        if not flag_enabled:
            raise PromotionRefused("promotion flag is not enabled for this tuple")
        if (entry.state, to_state) not in _ALLOWED_PROMOTIONS:
            raise PromotionRefused(
                f"cannot promote {entry.state!r} directly to {to_state!r} "
                f"(one step at a time)")
        latest = entry.latest_calibration()
        if latest is None or not latest.passed:
            raise PromotionRefused(
                "no passing calibration on record for this exact "
                "(model, backend, template) tuple")
        previous_state = entry.state
        entry.state = to_state
        entry.promotions.append({"from": previous_state, "to": to_state,
                                 "evidence_id": latest.id, "at": now_iso()})
        return entry

    def all_entries(self) -> Tuple[CapabilityEntry, ...]:
        return tuple(self._entries.values())
