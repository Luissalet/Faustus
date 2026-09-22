"""src/agent_tools/instinct_tools.py — the `manage_instincts` tool handler.

Mirrors `src/tools/system.py::do_manage_skills` end to end: same call
shape (`content: str, owner: Optional[str] = None`), same JSON-args parsing
via `src.tools._common._parse_tool_args`, same "no new gate, only reads and
small owner-scoped writes" posture. The actual behaviour lives in
`src.instincts`; this module is only the tool-call ⇄ function-call
adapter.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.tools._common import _parse_tool_args

__all__ = ["do_manage_instincts"]

_VALID_ACTIONS = (
    "list", "view", "status", "confirm", "contradict", "add", "retire",
    "promote", "evolve", "export", "import",
)


async def do_manage_instincts(content: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """Handle manage_instincts tool calls.

    Actions:
      list {project, min_confidence}         — active instincts for owner.
      view {id}                               — one instinct's full record.
      status {project}                        — counts + top 5 + pending promotions.
      confirm {id, evidence}                  — this instinct worked; raise confidence.
      contradict {id, evidence}                — this instinct was wrong; lower confidence.
      add {trigger, action, domain, scope,
           project, project_name}             — manual instinct (source="manual").
      retire {id}                             — mark inactive (kept on disk).
      promote {id, dry_run}                    — merge a project instinct seen widely into global.
      evolve {project, generate}               — cluster instincts into a draft skill/command/agent suggestion.
      export {}                                — full JSON dump of this owner's instincts.
      import {json}                            — validated, dedup-by-id import of that shape.
    """
    from src import instincts

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    action = (args.get("action") or "").strip().lower()
    if not action:
        return {"error": f"action is required ({'|'.join(_VALID_ACTIONS)})", "exit_code": 1}
    if action not in _VALID_ACTIONS:
        return {"error": f"unknown action {action!r} ({'|'.join(_VALID_ACTIONS)})", "exit_code": 1}

    try:
        if action == "list":
            items = instincts.list_instincts(
                owner,
                project=args.get("project"),
                include_global=bool(args.get("include_global", True)),
                min_confidence=float(args.get("min_confidence", 0.0) or 0.0),
            )
            if not items:
                return {"results": "No instincts yet."}
            lines = []
            for it in items:
                pct = int(round(it["effective_confidence"] * 100))
                label = "project" if it.get("scope") == "project" else "global"
                lines.append(f"- [{it['id']}] [{label} {pct}%] {it.get('trigger','')}: {it.get('action','')}")
            return {"results": "\n".join(lines), "count": len(items)}

        if action == "view":
            iid = (args.get("id") or "").strip()
            if not iid:
                return {"error": "id is required for view", "exit_code": 1}
            item = instincts.get(owner, iid)
            if item is None:
                return {"error": f"instinct {iid!r} not found", "exit_code": 1}
            return {"results": item}

        if action == "status":
            return {"results": instincts.status(owner, project=args.get("project"))}

        if action == "confirm":
            iid = (args.get("id") or "").strip()
            if not iid:
                return {"error": "id is required for confirm", "exit_code": 1}
            try:
                return {"results": instincts.confirm(owner, iid, evidence=args.get("evidence"))}
            except KeyError:
                return {"error": f"instinct {iid!r} not found", "exit_code": 1}

        if action == "contradict":
            iid = (args.get("id") or "").strip()
            if not iid:
                return {"error": "id is required for contradict", "exit_code": 1}
            try:
                return {"results": instincts.contradict(owner, iid, evidence=args.get("evidence"))}
            except KeyError:
                return {"error": f"instinct {iid!r} not found", "exit_code": 1}

        if action == "add":
            # `action` is already consumed above as the tool-call discriminator
            # (list|view|...|add), so the instinct's own action text travels
            # under a different field: `do` (preferred) or `action_text`.
            trigger = (args.get("trigger") or "").strip()
            act = (args.get("do") or args.get("action_text") or "").strip()
            if not trigger:
                return {"error": "trigger is required for add", "exit_code": 1}
            if not act:
                return {"error": "do (the instinct's action text) is required for add", "exit_code": 1}
            record = instincts.add(
                owner, trigger=trigger, action=act,
                domain=(args.get("domain") or "other"),
                scope=(args.get("scope") or "project"),
                project=(args.get("project") or ""),
                project_name=(args.get("project_name") or ""),
            )
            return {"results": record}

        if action == "retire":
            iid = (args.get("id") or "").strip()
            if not iid:
                return {"error": "id is required for retire", "exit_code": 1}
            try:
                return {"results": instincts.retire(owner, iid)}
            except KeyError:
                return {"error": f"instinct {iid!r} not found", "exit_code": 1}

        if action == "promote":
            return {"results": instincts.promote(
                owner, id=args.get("id"), dry_run=bool(args.get("dry_run", False)),
            )}

        if action == "evolve":
            return {"results": instincts.evolve(
                owner, project=args.get("project"),
                min_cluster=int(args.get("min_cluster", 2) or 2),
                generate=bool(args.get("generate", False)),
            )}

        if action == "export":
            return {"results": instincts.export_json(owner)}

        if action == "import":
            payload = args.get("json")
            if not payload:
                return {"error": "json is required for import", "exit_code": 1}
            if not isinstance(payload, str):
                import json as _json
                payload = _json.dumps(payload)
            try:
                return {"results": instincts.import_json(owner, payload, scope_override=args.get("scope_override"))}
            except ValueError as e:
                return {"error": str(e), "exit_code": 1}

    except Exception as e:  # noqa: BLE001 - a tool call must never 500 the turn
        return {"error": f"manage_instincts: {e}", "exit_code": 1}

    return {"error": f"unhandled action {action!r}", "exit_code": 1}
