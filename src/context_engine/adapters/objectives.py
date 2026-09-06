"""
context_engine/adapters/objectives.py — the goal is not a retrieval result.

``services/objectives.py`` is the only store in this package that answers a
mandatory section.  ``active_goal`` and ``decisions`` are in
``MANDATORY_SECTIONS``: they are placed by policy and never ranked, because a
live objective does not compete for a slot against a semantically similar
memory — either the model is told what it is working on or the packet is
degraded.  That is why every candidate from here carries the ``mandatory``
lane rather than a score-bearing one, and why ``authority`` is
``binding_decision``: an objective is what the project has *committed to*, and
§14.2 needs it to win a contradiction against a memory that recalls an older
plan.

Two splits this module makes, both from the objective's own ``status``:

* ``open`` / ``in_progress`` / ``blocked`` -> ``active_goal``.  The work.
* ``done`` **with a note** -> ``decisions``.  A finished objective with no note
  records that something was done, not what was decided; injecting it costs
  tokens and teaches the model nothing.  ``dropped`` never appears at all —
  ``render_lines`` skips it too, for the same reason.

The project record is resolved in the order the runtime can be trusted about
it: an explicit ``project_id`` first, then the session's folder binding, then a
bare workspace path.  Both ``objectives.objectives_dir`` and
``projects.ProjectStore.memory_dir`` read only ``project["workspace"]``, so the
last of those three is a complete record for this purpose — verified by
reading them, not assumed.

``source_ref`` scheme: ``objective:OBJ-3``, resolvable through
``load_state()["objectives"]``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

#: Statuses that mean "this is the work".
ACTIVE_STATUSES: Tuple[str, ...] = ("in_progress", "blocked", "open")
#: Statuses that mean "this was settled".
SETTLED_STATUSES: Tuple[str, ...] = ("done",)


def resolve_project(req: RetrievalRequest) -> Optional[Dict[str, Any]]:
    """The project record for this request, or None.

    Never raises: ``services/projects.py`` runs on the chat hot path and has
    the same posture, and a broken ``projects.json`` must cost the objectives
    section rather than the turn.
    """
    owner = str(req.owner or "") or None
    if req.project_id:
        try:
            from services.projects import get_store
            project = get_store().get(req.project_id, owner)
            if project:
                return project
        except Exception as exc:                               # noqa: BLE001
            logger.debug("project %s not resolvable: %s", req.project_id, exc)
    if req.session_id:
        try:
            from services.projects import project_for_session
            project = project_for_session(req.session_id, owner)
            if project:
                return project
        except Exception as exc:                               # noqa: BLE001
            logger.debug("project_for_session(%s) failed: %s", req.session_id, exc)
    if req.workspace:
        # Enough for every function this package calls: they read `workspace`
        # and nothing else off the record.
        return {"workspace": req.workspace}
    return None


class ObjectivesSource(ThreadedSource):
    """``services/objectives.py`` — the live goal and the decisions behind it."""

    source_id = "objectives"
    sections = ("active_goal", "decisions")
    handles = ("objective:",)

    def available(self) -> bool:
        try:
            import services.objectives  # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("services.objectives unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not (req.wants("active_goal") or req.wants("decisions")):
            return "neither active_goal nor decisions requested"
        if not req.allows("mandatory") and not req.allows("temporal"):
            return "neither the mandatory nor the temporal lane is open"
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        import services.objectives as objectives

        project = resolve_project(req)
        if not project:
            return ()
        state = objectives.load_state(project)
        payload = objectives.serialize_state(state)
        records = list(payload.get("objectives") or [])
        if not records:
            return ()

        limit = req.top()
        out: List[ContextCandidate] = []

        if req.wants("active_goal") and req.allows("mandatory"):
            active = [r for r in records if r.get("status") in ACTIVE_STATUSES]
            # Priority 1 is highest; ties break on the objective's own number,
            # which is creation order, so the list is stable between turns and
            # the prompt stays cacheable.
            active.sort(key=lambda r: (int(r.get("priority") or 3), str(r.get("id") or "")))
            for record in active[:limit]:
                candidate = self._candidate(record, req, "active_goal", ("mandatory",))
                if candidate is not None:
                    out.append(candidate)

        if req.wants("decisions") and req.allows("temporal"):
            settled = [r for r in records
                       if r.get("status") in SETTLED_STATUSES
                       and str(r.get("notes") or "").strip()]
            settled.sort(key=lambda r: (str(r.get("updated_at") or ""),
                                        str(r.get("id") or "")), reverse=True)
            for record in settled[:limit]:
                candidate = self._candidate(record, req, "decisions", ("temporal",))
                if candidate is not None:
                    out.append(candidate)

        return tuple(out)

    def _candidate(self, record: Mapping[str, Any], req: RetrievalRequest,
                   section: str, lanes: Tuple[str, ...]) -> Optional[ContextCandidate]:
        oid = str(record.get("id") or "")
        if not oid:
            return None
        status = str(record.get("status") or "open")
        priority = record.get("priority", 3)
        deps = [str(d) for d in (record.get("deps") or [])]
        lines = [f"{oid} [{status}] (P{priority}) {record.get('title') or ''}".rstrip()]
        if deps:
            lines.append("depends on: " + ", ".join(deps))
        notes = str(record.get("notes") or "").strip()
        if notes:
            lines.append(notes)
        return make_candidate(
            source_type="objective" if section == "active_goal" else "decision",
            source_ref=f"objective:{oid}",
            section=section,
            title=f"{oid}: {record.get('title') or ''}".strip(),
            body="\n".join(lines),
            lanes=lanes,
            scores={"priority": float(priority if isinstance(priority, (int, float)) else 3)},
            # A human (or an agent acting under one) wrote the objective, and
            # the project is bound by it until a typed delta says otherwise.
            trust_class="human_explicit",
            authority="binding_decision",
            source_revision=str(record.get("updated_at") or ""),
            observed_at=record.get("updated_at") or record.get("created_at") or "",
            owner=req.owner,
            project_id=req.project_id,
            meta={"status": status, "priority": priority, "deps": deps,
                  "last_actor": str(record.get("last_actor") or "")},
        )

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        import services.objectives as objectives

        ref = str(source_ref or "")
        if not ref.startswith("objective:"):
            return None
        project = resolve_project(req)
        if not project:
            return None
        record = (objectives.load_state(project).get("objectives") or {}).get(ref[10:])
        if not record:
            return None
        section = "decisions" if record.get("status") in SETTLED_STATUSES else "active_goal"
        return self._candidate(record, req, section, ("exact",))


__all__ = ["ObjectivesSource", "resolve_project", "ACTIVE_STATUSES", "SETTLED_STATUSES"]
