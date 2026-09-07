"""Owner-scoped live state from :mod:`src.state_mirror`.

The State Mirror remains the store of record.  This adapter asks its public
service for a bounded projection and turns that answer into one mandatory
``current_state`` candidate.  It never opens the mirror database directly and
never refreshes a source on the chat path: stale facts remain visibly stale
and carry their refresh actions instead of making a prompt compilation spend
the machine.

``source_ref`` is ``state:<project_id>`` (or ``state:owner`` without a bound
project).  Fetching it rebuilds the latest projection, so a reference never
pretends an old operational snapshot is still current.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)


class StateMirrorSource(ThreadedSource):
    source_id = "state_mirror"
    sections = ("current_state",)
    handles = ("state:",)

    def available(self) -> bool:
        try:
            import src.state_mirror.service  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            logger.debug("State Mirror unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.wants("current_state"):
            return "current_state was not requested"
        if not req.allows("mandatory"):
            return "the mandatory lane is closed"
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not req.owner:
            return "the request has no owner scope"
        return ""

    @staticmethod
    def _projection(req: RetrievalRequest) -> Dict[str, Any]:
        from src.state_mirror.service import service

        facade = service()
        rows = facade.entities(
            owner=req.owner,
            project_id=req.project_id,
            limit=200,
        )
        refs = [str(row.get("id") or "") for row in rows
                if isinstance(row, Mapping) and str(row.get("id") or "")]
        if not refs:
            return {}
        return dict(facade.project(
            owner=req.owner,
            project_id=req.project_id,
            entity_refs=refs,
            minimum_freshness="informational",
            reason="Context Engine current_state",
        ) or {})

    @staticmethod
    def _body(projection: Mapping[str, Any]) -> str:
        lines: List[str] = []
        fields = projection.get("fields") or {}
        if isinstance(fields, Mapping):
            for key in sorted(fields):
                meta = fields.get(key)
                if not isinstance(meta, Mapping):
                    continue
                value = json.dumps(meta.get("value"), ensure_ascii=False,
                                   sort_keys=True, default=str)
                facts = ", ".join(filter(None, (
                    str(meta.get("freshness") or "unknown"),
                    str(meta.get("epistemic") or ""),
                    f"source={meta.get('source')}" if meta.get("source") else "",
                    f"observed={meta.get('observed_at')}" if meta.get("observed_at") else "",
                )))
                lines.append(f"- {key} = {value} [{facts}]")
        unknown = [str(x) for x in (projection.get("unknown_fields") or ()) if str(x)]
        if unknown:
            lines.append("Unknown fields: " + ", ".join(unknown))
        conflicts = [str(x) for x in (projection.get("conflicts") or ()) if str(x)]
        if conflicts:
            lines.append("Open conflicts: " + ", ".join(conflicts))
        actions = projection.get("refresh_actions") or ()
        if actions:
            lines.append("Refresh required: " + json.dumps(
                list(actions), ensure_ascii=False, sort_keys=True, default=str
            ))
        return "\n".join(lines)

    def _candidate(self, req: RetrievalRequest,
                   projection: Mapping[str, Any]) -> Optional[ContextCandidate]:
        body = self._body(projection)
        if not body:
            return None
        project_key = req.project_id or "owner"
        freshness = str(projection.get("freshness") or "unknown")
        degraded = bool(projection.get("conflicts") or projection.get("refresh_actions"))
        return make_candidate(
            source_type="state",
            source_ref=f"state:{project_key}",
            section="current_state",
            title="Current observed project state",
            body=body,
            lanes=("mandatory",),
            trust_class="observed",
            authority="observed_state",
            source_revision=str(projection.get("revision") or ""),
            observed_at=projection.get("as_of") or "",
            owner=req.owner,
            project_id=req.project_id,
            degraded=degraded,
            meta={
                "projection_id": str(projection.get("projection_id")
                                     or projection.get("id") or ""),
                "freshness": freshness,
                "sufficient": bool(projection.get("sufficient")),
                "entity_count": len(projection.get("entity_refs") or ()),
            },
        )

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        candidate = self._candidate(req, self._projection(req))
        return (candidate,) if candidate is not None else ()

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        if not str(source_ref or "").startswith("state:"):
            return None
        return self._candidate(req, self._projection(req))


__all__ = ["StateMirrorSource"]
