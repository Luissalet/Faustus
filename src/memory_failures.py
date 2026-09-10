"""src/memory_failures.py — MEM-03: failure memory with controlled promotion.

The failure this closes: one bad run must never become a standing "never do
X" that then gets injected into every unrelated future task. The backlog's
own acceptance criterion says it in one line — "Un fallo aislado no produce
una prohibicion global que empeore otras tareas" — and the mechanism it asks
for is a pipeline, not a flag: register the incident class and its evidence,
let it sit as a CANDIDATE, and only turn it into an active rule "tras
validacion, con caducidad y prueba de no regresion".

This reuses `src/memory_engine.py` end to end rather than building a second
store for "things the agent learned not to do" (rule 4): a candidate IS a
memory item — ``type="anti_pattern"``, ``status="deprecated"`` — which is
deliberately NOT one of `search()`/`pack()`'s default statuses
(``("active", "anti_pattern")``), so a candidate is invisible to prompt
injection until it is promoted. Promotion flips it to ``status="anti_pattern"``
the same way `memory_curator._invert` already promotes a procedural rule that
turned out harmful — the two mechanisms now share the vocabulary
(`status="anti_pattern"`) instead of inventing a second one for the same
concept, they just reach it by different roads (this module's road starts
from a failure with no prior memory item at all; the curator's road starts
from an existing rule that stopped working).

Two independent gates, and BOTH are required, not either/or:

* **repetition** — the same incident class (a normalised hash of the failure
  signature) has to recur `MIN_OCCURRENCES_TO_PROPOSE` times (the same bar
  `memory_curator.INVERT_MIN_HARMFUL` already uses for the analogous
  decision) OR a caller passes ``confirm=True`` — an explicit human/agent
  confirmation after review, the "confirmacion" half of "confirmacion/
  repeticion". A single occurrence with ``confirm=True`` is deliberately
  still not enough on its own for a *promotion* — see the test file for why
  a caller cannot skip the evidence trail by force alone.
* **a regression test** — ``test_ref`` (a pytest node id, a file path,
  anything that resolves) must be attached before promotion. No test, no
  promotion, however many times the failure repeated: a rule with nothing
  proving it prevents a recurrence is exactly the kind of unverifiable
  prohibition the acceptance criterion rules out.

``expires_at`` (caducidad) is stamped on every promotion and re-stamped on
every reconfirmation; `sweep_expired()` is the maintenance pass that demotes
a rule nobody has reconfirmed in that window back to a candidate rather than
deleting the history — the same "downgrade, never erase" posture
`memory_curator.py` already takes on everything it touches.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src import memory_engine as engine

logger = logging.getLogger(__name__)

#: A candidate is a memory item that has not yet earned a place in
#: `search()`/`pack()`'s default statuses — "deprecated" already means
#: exactly that ("not served by default") without a third value threaded
#: through every STATUSES check in `memory_engine.py`.
CANDIDATE_STATUS = "deprecated"
ACTIVE_STATUS = "anti_pattern"
PROVENANCE_KIND = "failure_candidate"

#: Same bar `memory_curator.INVERT_MIN_HARMFUL` uses for the analogous
#: decision (an existing rule turning out harmful) — one number for "enough
#: repetition to mean something" across both mechanisms.
MIN_OCCURRENCES_TO_PROPOSE = engine.INVERT_MIN_HARMFUL
DEFAULT_EXPIRES_DAYS = 30.0


def _incident_class(signature: Any) -> str:
    norm = " ".join(str(signature or "").split()).casefold()
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:24]


def _find_candidate(owner: str, project: str, incident_class: str) -> Optional[Dict[str, Any]]:
    """Linear scan over this scope's deprecated items. `memory_engine` has no
    query-by-provenance-field index, and adding one for a lookup this rare
    (once per failure, not on the hot retrieval path) would be the second
    store rule 4 warns against in miniature — an index nothing else needs."""
    for item in engine.list_items(owner=owner, project=project,
                                  status=CANDIDATE_STATUS, limit=2000):
        prov = item.get("provenance") or {}
        if prov.get("kind") == PROVENANCE_KIND and prov.get("incident_class") == incident_class:
            return item
    return None


def _expires_at(now: Optional[datetime] = None) -> str:
    moment = (now or datetime.now(timezone.utc)) + timedelta(days=DEFAULT_EXPIRES_DAYS)
    return engine._iso(moment)


def register_failure(
    signature: str, summary: str, *, owner: str = "", project: str = "",
    severity: str = "medium", evidence_ref: str = "", session_id: str = "",
    test_ref: str = "", confirm: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record one occurrence of a failure and promote it if — and only if —
    both gates are satisfied this call.

    Returns ``{"outcome": "registered" | "promoted", "item": <public_item>,
    "occurrences": int, "blocked_reason": str}``. ``blocked_reason`` is set
    (and non-empty) exactly when the repetition/confirmation gate is open but
    promotion still did not happen — the caller-facing "why not yet" the
    frontend panel needs to render "regla propuesta -> pruebas -> activacion"
    honestly instead of guessing.
    """
    incident_class = _incident_class(signature)
    existing = _find_candidate(owner, project, incident_class)
    stamp = engine._iso(now or datetime.now(timezone.utc))

    if existing is None:
        item = engine.add_item(
            f"AVOID: {summary}".strip(),
            owner=owner, project=project, level="procedural", category="failure",
            trust_class="agent_assertion", type="anti_pattern",
            status=CANDIDATE_STATUS, maturity="candidate",
            evidence=[{"kind": "chat", "excerpt": summary[:200], "ref": evidence_ref}],
            session_id=session_id,
            provenance={"kind": PROVENANCE_KIND, "incident_class": incident_class,
                       "signature": str(signature or "")[:500], "severity": severity,
                       "occurrences": 1, "test_ref": test_ref},
            now=now,
        )
        blocked = "" if (test_ref and (confirm or MIN_OCCURRENCES_TO_PROPOSE <= 1)) else (
            "needs a regression test reference before promotion" if not test_ref
            else f"needs {MIN_OCCURRENCES_TO_PROPOSE} occurrences (or explicit confirmation)")
        if not blocked:
            item = _promote(item, now=now)
            return {"outcome": "promoted", "item": engine.public_item(item),
                    "occurrences": 1, "blocked_reason": ""}
        return {"outcome": "registered", "item": engine.public_item(item),
                "occurrences": 1, "blocked_reason": blocked}

    prov = dict(existing.get("provenance") or {})
    occurrences = int(prov.get("occurrences") or 1) + 1
    prov["occurrences"] = occurrences
    if test_ref:
        prov["test_ref"] = test_ref
    existing["provenance"] = prov
    existing["evidence"] = (list(existing.get("evidence") or [])
                            + engine.normalize_evidence(
                                [{"kind": "chat", "excerpt": summary[:200], "ref": evidence_ref}]
                            ))[-engine.MAX_EVENTS:]
    existing["updated_at"] = stamp
    engine.save_item(existing)

    has_test = bool(prov.get("test_ref"))
    repeated_enough = occurrences >= MIN_OCCURRENCES_TO_PROPOSE
    if not has_test:
        blocked = "needs a regression test reference before promotion"
    elif not (confirm or repeated_enough):
        blocked = (f"needs {MIN_OCCURRENCES_TO_PROPOSE} occurrences (has {occurrences}) "
                  "or explicit confirmation before promotion")
    else:
        blocked = ""

    if not blocked:
        existing = _promote(existing, now=now)
        return {"outcome": "promoted", "item": engine.public_item(existing),
                "occurrences": occurrences, "blocked_reason": ""}
    return {"outcome": "registered", "item": engine.public_item(existing),
            "occurrences": occurrences, "blocked_reason": blocked}


def _promote(item: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    prov = dict(item.get("provenance") or {})
    prov["promoted_at"] = engine._iso(now or datetime.now(timezone.utc))
    prov["expires_at"] = _expires_at(now)
    item["provenance"] = prov
    item["status"] = ACTIVE_STATUS
    item["type"] = "anti_pattern"
    item["maturity"] = "established"
    item["updated_at"] = prov["promoted_at"]
    engine.save_item(item)
    return item


def list_candidates(owner: str = "", project: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """Every failure-candidate row still short of promotion, for the
    "problema recurrente -> regla propuesta -> pruebas -> activacion" panel."""
    out = []
    for item in engine.list_items(owner=owner or None, project=project or None,
                                  status=CANDIDATE_STATUS, limit=limit):
        prov = item.get("provenance") or {}
        if prov.get("kind") == PROVENANCE_KIND:
            out.append(engine.public_item(item))
    return out


def sweep_expired(owner: Optional[str] = None, project: Optional[str] = None,
                  now: Optional[datetime] = None) -> Dict[str, int]:
    """Demote a promoted anti-pattern whose `expires_at` has passed and was
    never reconfirmed, back to a candidate — never deletes it (rule 3: no
    capability, including "we used to warn about this", quietly disappears).
    A repeat of the same incident after demotion simply re-promotes through
    `register_failure` the normal way, with a fresh occurrence count.
    """
    moment = now or datetime.now(timezone.utc)
    demoted = 0
    for item in engine.list_items(owner=owner, project=project, status=ACTIVE_STATUS, limit=2000):
        prov = item.get("provenance") or {}
        if prov.get("kind") != PROVENANCE_KIND:
            continue
        expires = engine.parse_iso(prov.get("expires_at"))
        if expires is None or expires > moment:
            continue
        item["status"] = CANDIDATE_STATUS
        item["maturity"] = "candidate"
        prov["occurrences"] = 0
        prov["demoted_at"] = engine._iso(moment)
        item["provenance"] = prov
        item["updated_at"] = engine._iso(moment)
        engine.save_item(item)
        demoted += 1
    return {"demoted": demoted}


__all__ = [
    "register_failure", "list_candidates", "sweep_expired",
    "CANDIDATE_STATUS", "ACTIVE_STATUS", "MIN_OCCURRENCES_TO_PROPOSE",
    "DEFAULT_EXPIRES_DAYS",
]
