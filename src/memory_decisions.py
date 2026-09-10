"""src/memory_decisions.py — MEM-04: continuidad de proyecto y decisiones.

The acceptance criterion in one line: "Cambiar de modelo conserva el motivo
de una eleccion tecnica sin arrastrar todo el transcript." A decision has to
outlive the session/model that made it, and it has to be small enough that
recovering it does not mean re-reading the conversation that produced it.

Reused rather than reinvented (rule 4): `src/memory_engine.py` already has
``type="decision"`` in its vocabulary (MEM-01) and never used it for
anything — this module is that missing writer/reader pair. A decision is one
more scoped memory item (owner/project, searchable, survives a model swap or
a fresh session because it was never attached to either), not a new store,
and it inherits everything that store already guarantees: owner isolation,
MEM-02 forget/correct with a tombstone, and the Curator's dedupe pass.

``invalidate_decision`` is the one operation worth a whole function instead
of a bare `add_feedback`/status flip: the backlog is explicit that changing a
decision's premise must not erase it — ``status="deprecated"`` already means
"excluded from default search/pack, never deleted" throughout
`memory_engine.py`, so invalidating a decision reuses that exact semantics
instead of adding a fourth status meaning almost the same thing. The reason
and (optionally) the id of whatever decision replaces it are recorded in
``provenance`` so a timeline can render "superseded by X because Y" without a
second table to join against.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from src import memory_engine as engine

logger = logging.getLogger(__name__)

DECISION_TYPE = "decision"
MAX_ALTERNATIVES = 20
MAX_ALTERNATIVE_CHARS = 300
MAX_ARTIFACT_REFS = 50


def _clip_list(values: Any, max_items: int, max_len: int) -> List[str]:
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for v in values[:max_items]:
        text = str(v or "").strip()[:max_len]
        if text:
            out.append(text)
    return out


def record_decision(
    text: str, *, owner: str = "", project: str = "",
    alternatives: Sequence[str] = (), scope: str = "project",
    artifact_refs: Sequence[str] = (), session_id: str = "",
    trust_class: str = "human_explicit", now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record one technical decision with its discarded alternatives and the
    artifacts/changes it is tied to.

    ``scope`` is descriptive (kept in `provenance`, not a new column) —
    "project"/"module"/"api" or whatever the caller wants — so a timeline can
    group decisions without this module having to enumerate every scope a
    project might use. ``trust_class`` defaults to ``human_explicit`` (0.85):
    a recorded decision is, by construction, the thing that was decided, not
    an agent's guess at one.
    """
    alts = _clip_list(alternatives, MAX_ALTERNATIVES, MAX_ALTERNATIVE_CHARS)
    artifacts = _clip_list(artifact_refs, MAX_ARTIFACT_REFS, 512)
    item = engine.add_item(
        text, owner=owner, project=project, level="semantic", category="decision",
        trust_class=trust_class, type=DECISION_TYPE, status="active",
        evidence=[{"kind": "chat", "excerpt": str(text or "")[:200]}],
        session_id=session_id,
        provenance={"decision_scope": str(scope or "project"),
                   "alternatives_discarded": alts, "artifact_refs": artifacts,
                   "invalidated": False},
        now=now,
    )
    return engine.public_item(item)


def invalidate_decision(
    decision_id: str, reason: str, *, superseded_by: str = "",
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Its premise changed: keep the row (rule 3 — nothing that recorded WHY
    a choice was made is ever deleted for changing its mind), mark it
    ``deprecated`` (excluded from default search/pack, same as everywhere
    else in this store) and say why and by what, if anything, it was
    replaced. Returns None when the id does not resolve to a decision.
    """
    real_id = engine.resolve_id(decision_id) or str(decision_id or "")
    item = engine.get_item(real_id)
    if not item or item.get("type") != DECISION_TYPE:
        return None
    prov = dict(item.get("provenance") or {})
    prov["invalidated"] = True
    prov["invalidated_reason"] = str(reason or "")[:1000]
    if superseded_by:
        prov["superseded_by"] = str(superseded_by)
    item["provenance"] = prov
    item["status"] = "deprecated"
    item["maturity"] = "deprecated"
    item["updated_at"] = engine._iso(now or engine._utcnow())
    engine.save_item(item)
    return engine.public_item(item)


def list_decisions(
    owner: str = "", project: str = "", *,
    include_invalidated: bool = False, limit: int = 200,
) -> List[Dict[str, Any]]:
    """A timeline: oldest first (append order is decision order), every
    decision of this type in scope, invalidated ones included only on
    request — the default view is "what is still true", the full view is
    the actual timeline the frontend's "linked to artifacts and changes"
    panel wants.
    """
    statuses = ("active", "deprecated") if include_invalidated else ("active",)
    out: List[Dict[str, Any]] = []
    for status in statuses:
        for item in engine.list_items(owner=owner or None, project=project or None,
                                      status=status, limit=limit):
            if item.get("type") == DECISION_TYPE:
                out.append(engine.public_item(item))
    out.sort(key=lambda d: d.get("created_at") or "")
    return out[:limit]


__all__ = ["record_decision", "invalidate_decision", "list_decisions", "DECISION_TYPE"]
