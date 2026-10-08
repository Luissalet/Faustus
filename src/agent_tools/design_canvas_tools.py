"""agent_tools/design_canvas_tools.py — the `design_canvas` tool executor.

OBJ-30. The canvas pass existed (`src/design_canvas_pass.py`) but only code
could call it, so in practice nothing did: the model went straight from the
request to the first edit, which is the habit the canvas exists to break.

One tool, deliberately:

    design_canvas  write  declare requirements, entities, approach,
                          structure, operations, norms and safeguards for a
                          piece of work, and file the result in the project
                          graph as a `decision`.

Same shape as `project_concepts_tools.py`: a permissive JSON payload, the
project resolved from `ctx["project_id"]`/`ctx["workspace"]` and never from
text the model wrote, and a thin call into the module that does the work.

It is a WRITE tool even though it only reads code, because it stores a
concept the next session will read as settled. `design_canvas_pass.draft`
does not fail open for the same reason: an empty canvas on record is worse
than no canvas.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        # A model that wrote the goal as bare prose still meant the goal.
        return {"goal": str(raw)}
    return parsed if isinstance(parsed, dict) else {}


def _project_id(ctx: dict) -> Optional[str]:
    pid = str((ctx or {}).get("project_id") or "").strip()
    return pid or None


def _workspace(ctx: dict) -> str:
    project_id = _project_id(ctx)
    if project_id:
        try:
            from services.projects import get_store
            project = get_store().get(project_id, str((ctx or {}).get("owner") or "") or None)
            ws = (project or {}).get("workspace") or ""
            if ws:
                return ws
        except Exception:
            logger.debug("design_canvas_tools._workspace: project lookup failed", exc_info=True)
    explicit = str((ctx or {}).get("workspace") or "")
    if explicit:
        return explicit
    # The tool context carries no `workspace` key; the folder the chat is
    # working in travels as the active workspace of the turn. Without this a
    # canvas drafted in a plain workspace chat (no project) was never filed,
    # and with nothing filed there is no canvas for the closure check to
    # write its verdict into.
    try:
        from src.tool_execution import get_active_workspace
        return str(get_active_workspace() or "")
    except Exception:  # noqa: BLE001
        return ""


def _canvas_model(ctx: dict) -> Optional[str]:
    """Which model writes the canvas.

    Order: the `agent_design_canvas_model` setting when somebody set one, then
    the model THIS turn is running on, then whatever the pass resolves.

    The middle step is the one that was missing. Left to resolve its own
    "auto", the pass took the global default model -- on this install the
    small helper -- which spent 180 s under the canvas grammar and returned an
    empty completion. A canvas is a seven-field object and the grammar and the
    reasoning come out of one token budget, so a small model is exactly the
    wrong one for it. The turn's model is the one the user chose for the work
    and the one already resident.
    """
    try:
        from src.settings import get_setting
        configured = str(get_setting("agent_design_canvas_model", "") or "").strip()
    except Exception:  # noqa: BLE001
        configured = ""
    if configured and configured.lower() != "auto":
        return configured
    return str((ctx or {}).get("turn_model") or "").strip() or None


class DesignCanvasTool:
    """`design_canvas` {goal, context?, name?, save?}: declare the design
    before writing code — requirements, entities, approach, structure,
    operations, norms and safeguards — and file it in the project graph so
    the next session can read what this was meant to do. Write tool."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src import design_canvas
        from src import design_canvas_pass
        from src.design_canvas import DesignCanvasError

        args = _args(content)
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return {"error": "design_canvas: `goal` is required — say what is being designed",
                    "exit_code": 1}

        try:
            drafted = await design_canvas_pass.draft(
                goal,
                context=str(args.get("context") or ""),
                owner=str((ctx or {}).get("owner") or "") or None,
                model=_canvas_model(ctx),
            )
        except DesignCanvasError as exc:
            # Deliberately not fail-open: see the module docstring.
            return {"error": f"design_canvas: {exc}", "exit_code": 1,
                    "error_class": "design_canvas.no_canvas"}

        canvas = drafted["canvas"]
        project_id = _project_id(ctx)
        workspace = _workspace(ctx)
        result: Dict[str, Any] = {
            "output": drafted["markdown"],
            "exit_code": 0,
            "canvas": canvas,
            "paths": drafted["paths"],
            "elapsed_ms": drafted["elapsed_ms"],
        }

        if args.get("save") is False:
            result["saved"] = False
            _arm_closure(result, ctx, goal, canvas, None, project_id, workspace)
            return result

        if not project_id and not workspace:
            # The canvas is still worth having in the turn; only the filing
            # needs a project. Say so rather than throwing the work away.
            result["saved"] = False
            result["note"] = ("not filed: no project or workspace is bound to this chat, "
                              "so there is no graph to store the design in")
            _arm_closure(result, ctx, goal, canvas, None, project_id, workspace)
            return result

        name = str(args.get("name") or "").strip() or design_canvas.summarise(canvas, goal)[:80]
        try:
            concept = design_canvas_pass.file_as_concept(
                canvas, goal, name=name,
                project_id=project_id, workspace=workspace or None,
                concept_id=str(args.get("concept_id") or "").strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            result["saved"] = False
            result["note"] = f"not filed: {exc}"
            _arm_closure(result, ctx, goal, canvas, None, project_id, workspace)
            return result

        result["saved"] = True
        result["concept"] = concept
        result["output"] = f"{drafted['markdown']}\n\nFiled as concept `{concept.get('id')}`."
        _arm_closure(result, ctx, goal, canvas, concept.get("id"), project_id, workspace)
        return result


def _arm_closure(result: Dict[str, Any], ctx: dict, goal: str, canvas: Dict[str, Any],
                 concept_id: Optional[str], project_id: Optional[str], workspace: str) -> None:
    """Remember this canvas for the end of the work (OBJ-47).

    `src/design_canvas_check.py` compares what the work did against it when
    the turn finishes and writes the verdict into the canvas. Best effort: a
    canvas that cannot be armed is still a canvas, so this never fails the
    tool.
    """
    session_id = str((ctx or {}).get("session_id") or "").strip()
    if not session_id:
        return
    try:
        from src import design_canvas_check
        armed = design_canvas_check.register(
            session_id, goal=goal, canvas=canvas, concept_id=concept_id,
            project_id=project_id, workspace=workspace or None,
            turn_id=str((ctx or {}).get("turn_id") or ""))
    except Exception:  # noqa: BLE001
        logger.debug("design_canvas_tools: could not arm the closure check", exc_info=True)
        return
    if armed:
        result["closure"] = "armed"
        result["output"] = (f"{result['output']}\n\nWhen the work is done the harness compares it "
                            "with this canvas and records a verdict per item.")
