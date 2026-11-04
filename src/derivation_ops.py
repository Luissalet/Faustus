"""derivation_ops.py — one dispatcher for the chat tools, the HTTP routes and
the MCP server over `src/recipe_learning.py` and `src/derivation_graph.py`.

Each ``run_*`` takes the owner and a plain argument dict and returns a JSON-able
dict; a failure is ``{"error": ..., "error_class": ..., "exit_code": 1}``, never
an exception. Paths that name files to hash are confined the way ``read_file``
and the code graph confine theirs (``tool_execution._resolve_search_root``)
unless ``confine=False`` (a Python caller that already trusts its paths).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Mapping, Optional

from src import derivation_graph as dg
from src import recipe_execution as rex
from src import recipe_learning as rl
from src import recipes as recipes_store

logger = logging.getLogger(__name__)

RECIPE_ACTIONS = ("propose", "try", "execute", "verification", "promote", "search", "show", "list", "screen_draft", "from_screen", "delete")
GRAPH_ACTIONS = ("define", "remove", "show", "list", "plan", "rebuild", "complete", "base_dir", "atlas")
MAP_ACTIONS = ("add", "link", "unlink", "remove", "show", "affected_by")


def _err(error_class: str, detail: str) -> Dict[str, Any]:
    return {"error": detail, "error_class": error_class, "exit_code": 1}


def _confine(path: str) -> str:
    from src.tool_execution import _resolve_search_root
    return _resolve_search_root(path or "")


def _guard(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return fn()
    except (rl.RecipeLearningError, dg.DerivationError) as exc:
        return _err(exc.error_class, exc.detail)
    except (ValueError, PermissionError, OSError) as exc:
        return _err("derivations.refused", str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
        logger.warning("derivation op failed", exc_info=True)
        return _err("derivations.error", f"{type(exc).__name__}: {exc}")


def _recipe_row(r: recipes_store.Recipe) -> Dict[str, Any]:
    """One line of the recipe list: what it is and how its last real run ended
    (the execution record, not a promise: ``last_status`` is None when it was
    never executed for real)."""
    last = r.executions[-1] if r.executions else {}
    return {"id": r.id, "title": r.title, "status": r.status, "learned": bool(r.step_templates),
            "params": [p["name"] for p in r.params], "steps": len(r.step_templates),
            "criteria": len(r.exit_criteria), "executions": len(r.executions),
            "last_status": last.get("status"), "last_at": last.get("at"),
            "last_verified": bool(last.get("verified")) if last else None}


def _s(args: Mapping[str, Any], key: str) -> str:
    return str(args.get(key) or "").strip()


# ---------------------------------------------------------------------------

def _wants_execution(args: Mapping[str, Any]) -> bool:
    return _s(args, "action") == "execute" or (_s(args, "action") == "try" and _s(args, "mode") == rex.MODE_EXECUTE)


def _workspace_for(args: Mapping[str, Any]) -> str:
    from src.tool_execution import get_active_workspace, vet_workspace
    raw = _s(args, "workspace")
    if raw:
        ws = vet_workspace(raw)
        if not ws:
            raise ValueError(f"workspace {raw!r} is not a usable folder")
        active = get_active_workspace()
        if active and not rex._inside(ws, active):
            raise PermissionError("workspace must be the active workspace or a folder inside it")
        return ws
    return get_active_workspace() or ""


async def run_recipe_async(owner: str, args: Mapping[str, Any], *, ctx: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """``run_recipe`` for callers on the server's event loop (chat tool, HTTP,
    MCP). A real execution gets an executor over the normal dispatcher with
    the caller's session, security context and tool policy; the steps are
    scheduled back on this loop from the worker thread the rebuild runs in."""
    import asyncio
    if not _wants_execution(args):
        return await asyncio.to_thread(run_recipe, owner, args)
    try:
        workspace = _workspace_for(args)
    except (ValueError, PermissionError) as exc:
        return _err("recipes.bad_workspace", str(exc))
    if not workspace:
        return _err("recipes.no_workspace", "a real execution needs a workspace: open a project folder or pass workspace")
    c = ctx or {}
    executor = rex.make_dispatch_executor(
        owner, workspace=workspace, session_id=c.get("session_id"), security_context=c.get("security_context"),
        tool_policy=c.get("tool_policy"), disabled_tools=c.get("disabled_tools"), loop=asyncio.get_running_loop())
    return await asyncio.to_thread(run_recipe, owner, {**dict(args), "workspace": workspace}, executor=executor)


def run_recipe(owner: str, args: Mapping[str, Any], *, executor: Optional[rex.Executor] = None) -> Dict[str, Any]:
    action = _s(args, "action") or ("propose" if (args.get("run_id") or args.get("session_id") or args.get("steps")
                                                  or args.get("workflow_run_id")) else "list")
    if action not in RECIPE_ACTIONS:
        return _err("recipes.bad_action", f"action must be one of {', '.join(RECIPE_ACTIONS)}")

    def go() -> Dict[str, Any]:
        if action in ("screen_draft", "from_screen"):
            from src import screen_recipe as sr
            try:
                draft = args.get("draft")
                if not isinstance(draft, Mapping):
                    events = args.get("events") if isinstance(args.get("events"), list) else None
                    draft = sr.fetch_draft(start=_s(args, "start"), end=_s(args, "end"), events=events, title=_s(args, "title"),
                                           locale=_s(args, "locale") or "es")
                if action == "screen_draft":
                    return {"draft": sr.check_draft(draft), "summary": sr.summary(draft)}
                return sr.propose(owner, draft, args.get("mapping"), title=_s(args, "title"))
            except sr.ScreenRecipeError as exc:
                return _err(exc.error_class, exc.detail)
        if action == "propose":
            recipe = rl.propose(
                owner, run_id=_s(args, "run_id"), session_id=_s(args, "session_id"),
                workflow_run_id=_s(args, "workflow_run_id"), request=str(args.get("request") or ""),
                steps=args.get("steps") if isinstance(args.get("steps"), list) else None, title=_s(args, "title"),
                params_hint=args.get("params_hint") if isinstance(args.get("params_hint"), dict) else None,
                turn=int(args.get("turn", -1)))
            return {"recipe": recipe.to_dict(),
                    "next": "Run recipe_learn action=try with the parameters of a second, different case; "
                            "promotion needs a try with every step resolved and no literal of the first case left."}
        if action in ("try", "execute"):
            params = args.get("params")
            if not isinstance(params, dict):
                return _err("recipes.bad_params", "params must be an object {name: value} for the second case")
            mode = _s(args, "mode") or (rex.MODE_EXECUTE if action == "execute" else rex.MODE_DRY_RUN)
            if mode not in rex.MODES:
                return _err("recipes.bad_mode", f"mode must be one of {', '.join(rex.MODES)}")
            if mode == rex.MODE_DRY_RUN:
                return rl.try_recipe(_s(args, "recipe_id"), owner, params)
            workspace = _s(args, "workspace")
            if not workspace:
                from src.tool_execution import get_active_workspace
                workspace = get_active_workspace() or ""
            run_with = executor
            if run_with is None:
                if not workspace:
                    return _err("recipes.no_workspace", "a real execution needs a workspace")
                run_with = rex.make_dispatch_executor(owner, workspace=workspace)
            return rex.execute(_s(args, "recipe_id"), owner, params, executor=run_with, workspace=workspace,
                               criteria=args.get("criteria"),
                               inputs=args.get("inputs") if isinstance(args.get("inputs"), list) else None,
                               job_timeout=args.get("job_timeout"), rerun=bool(args.get("rerun")))
        if action == "verification":
            return rex.verification_status(_s(args, "recipe_id"), owner)
        if action == "promote":
            return rl.promote(_s(args, "recipe_id"), owner)
        if action == "search":
            return {"recipes": rl.search(_s(args, "query"), owner, limit=int(args.get("limit") or 10))}
        if action == "delete":
            gone = recipes_store.delete_recipe(owner, _s(args, "recipe_id"))
            return gone or _err("recipes.not_found", "no recipe of yours with that id (built-in recipes cannot be deleted)")
        if action == "show":
            r = recipes_store.get_recipe(_s(args, "recipe_id"), owner)
            return {"recipe": r.to_dict()} if r else _err("recipes.not_found", "no such recipe")
        return {"recipes": [_recipe_row(r) for r in recipes_store.list_recipes(owner)]}
    return _guard(go)


def _confine_specs(base: str, specs: Any, confine: bool) -> None:
    if not confine:
        return
    for item in specs or []:
        path = item if isinstance(item, str) else (item.get("path") or item.get("file")) if isinstance(item, Mapping) else None
        if path:
            _confine(os.path.join(base, path) if base and not os.path.isabs(path) else path)


def run_graph(owner: str, args: Mapping[str, Any], *, confine: bool = True,
              runner: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, Any]:
    action = _s(args, "action") or "show"
    graph = _s(args, "graph") or "default"
    if action not in GRAPH_ACTIONS:
        return _err("derivations.bad_action", f"action must be one of {', '.join(GRAPH_ACTIONS)}")

    def go() -> Dict[str, Any]:
        if action == "list":
            return {"graphs": dg.list_graphs(owner)}
        if action == "show":
            return dg.show(owner, graph)
        if action == "base_dir":
            return dg.set_base_dir(owner, graph, _confine(_s(args, "base_dir")) if confine else _s(args, "base_dir"))
        if action == "define":
            op = args.get("op")
            if not isinstance(op, dict):
                return _err("derivations.bad_op", "op must be {kind, name, version}")
            base = _s(args, "base_dir")
            if confine:
                if base:
                    base = _confine(base)
                elif not dg.get_graph(owner, graph).get("base_dir"):
                    dg.set_base_dir(owner, graph, _confine(""))
                root = base or dg.get_graph(owner, graph).get("base_dir") or ""
                _confine_specs(root, args.get("inputs"), True)
                _confine_specs(root, args.get("outputs"), True)
            return dg.define_node(owner, graph, _s(args, "node"), op=op, inputs=args.get("inputs"),
                                  params=args.get("params") if isinstance(args.get("params"), dict) else None,
                                  outputs=args.get("outputs"), base_dir=base,
                                  provenance=args.get("provenance") if isinstance(args.get("provenance"), dict) else None)
        if action == "remove":
            return dg.remove_node(owner, graph, _s(args, "node"))
        if action == "plan":
            return dg.plan(owner, graph, args.get("target"))
        if action == "rebuild":
            return dg.rebuild(owner, graph, args.get("target"), runner=runner, atlas=args.get("atlas") is not False)
        if action == "complete":
            if confine:
                _confine_specs(dg.get_graph(owner, graph).get("base_dir") or "", args.get("outputs"), True)
            return dg.complete_node(owner, graph, _s(args, "node"), outputs=args.get("outputs"),
                                    value=args.get("value"), error=_s(args, "error"))
        # atlas
        project = args.get("project_id")
        if project is not None:
            dg.set_atlas(owner, graph, str(project))
        return dg.sync_atlas(owner, graph) if args.get("sync", True) and (project is None or project) else {"atlas": {}}
    return _guard(go)


def run_map(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    action = _s(args, "action") or "show"
    graph = _s(args, "graph") or "default"
    if action not in MAP_ACTIONS:
        return _err("derivations.bad_action", f"action must be one of {', '.join(MAP_ACTIONS)}")

    def go() -> Dict[str, Any]:
        if action == "add":
            return dg.map_add(owner, graph, _s(args, "id"), _s(args, "kind"), _s(args, "title"),
                              note=_s(args, "note"), path=_s(args, "path"))
        if action == "link":
            return dg.map_link(owner, graph, _s(args, "from"), _s(args, "to"), _s(args, "relation"))
        if action == "unlink":
            return dg.map_unlink(owner, graph, _s(args, "from"), _s(args, "to"))
        if action == "remove":
            return dg.map_remove(owner, graph, _s(args, "id"))
        if action == "affected_by":
            return dg.affected_by(owner, graph, _s(args, "change"), invalidate=bool(args.get("invalidate")))
        return dg.map_show(owner, graph)
    return _guard(go)
