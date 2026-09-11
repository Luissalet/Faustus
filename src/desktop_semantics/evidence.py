"""ADP-08/09/10 — evidence for a `desktop_act` call.

Rule 4 (no second state store for something that already has one): this
appends to the SAME place the coordinate-based desktop_* control tools
already leave their before/after audit trail —
`src.desktop_control_session.record_action`, which already writes the
in-memory ring buffer AND `data/runtime/desktop-audit.log`
(`src/agent_tools/desktop_tools.py::DesktopTool._execute`, the
`audit_active`/`record_action` block). No new file, no new table.

What this module adds on top: `delivery` and `verified` are kept as
SEPARATE fields (never folded into one boolean) inside the entry's `note` —
`record_action`'s existing signature is untouched, since other callers
(the coordinate tools) already rely on it. A `delivered` action can still
have `verified=False` (the call went through; nobody re-read the control to
confirm) — that distinction has to survive into the trace, per the ADP-10
acceptance criterion "un intento incierto nunca se representa como éxito
comprobado".
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .contracts import ActionResult, Element

_PREFIX = "desktop_act:"

# Never let raw screen pixels leak into this text trail — that is
# capture_evidence/desktop_screenshot's job, not this one's (ADP-10 limit:
# "captura de pantalla no equivale a prueba de efecto externo", and "no
# construir un segundo almacén de resultados" cuts both ways: no pixels here).
_STRIP_KEYS = frozenset({"screenshot", "image", "images", "b64", "data"})


def _sanitize(observed_after: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(observed_after, dict):
        return observed_after
    return {k: v for k, v in observed_after.items() if k not in _STRIP_KEYS}


def record(
    session_id: str,
    *,
    ref: str,
    op: str,
    element: Optional[Element],
    result: ActionResult,
    before_hash: str = "",
    after_hash: str = "",
) -> Dict[str, Any]:
    """Append one evidence entry for a `desktop_act` call. Returns the same
    entry shape `desktop_control_session.record_action` already returns."""
    from src import desktop_control_session as policy

    target = None
    if element is not None:
        target = {
            "role": element.role,
            "name": element.name,
            "automation_id": element.automation_id,
        }
    note = json.dumps(
        {
            "kind": "desktop_semantic_action",
            "ref": ref,
            "op": op,
            "target": target,
            "delivery": result.delivery,
            "verified": result.verified,
            "observed_after": _sanitize(result.observed_after),
        },
        sort_keys=True,
    )
    return policy.record_action(
        session_id, f"{_PREFIX}{op}", before_hash=before_hash, after_hash=after_hash, note=note
    )


def export_trace(session_id: str, *, limit: int = 200) -> Dict[str, Any]:
    """Read-only, selective export of THIS session's `desktop_act` evidence
    — never another session's, never raw capture pixels (see `_sanitize`
    above; ADP-10's "exportar no incluye claves ni capturas no
    seleccionadas"). Reads the SAME log `manage_desktop_control`'s
    `audit_log` action already exposes; adds no new store."""
    from src import desktop_control_session as policy

    entries = policy.audit_log(session_id)
    out = []
    for entry in entries[-limit:]:
        if not str(entry.get("tool", "")).startswith(_PREFIX):
            continue
        try:
            payload = json.loads(entry.get("note") or "{}")
        except (TypeError, ValueError):
            payload = {}
        out.append({
            "session_id": entry.get("session_id", ""),
            "op": str(entry.get("tool", ""))[len(_PREFIX):],
            "at": entry.get("at"),
            "ref": payload.get("ref"),
            "target": payload.get("target"),
            "delivery": payload.get("delivery", "unknown"),
            "verified": bool(payload.get("verified", False)),
            "observed_after": payload.get("observed_after"),
        })
    return {"session_id": str(session_id or ""), "entries": out, "count": len(out)}
