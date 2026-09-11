"""agent_tools/requirement_tools.py — versioned requirements tools for the
agent (ADP-18/19/20).

Five thin executors over ``src.requirements``, the same shape
``board_tools.py`` gives the project board:

    req_list     read   this project's requirements, filtered
    req_get      read   one requirement's full detail (text, acceptance,
                         links)
    req_propose  write  file a new requirement -- ALWAYS `proposed_by:
                         'model'`, `status: 'proposed'` when called by the
                         agent; a human decides acceptance elsewhere
                         (the route), never here
    req_link     write  attach an implements/tests/evidences/issue link
    req_matrix   read   the linked/implemented/tested/verified/stale
                         coverage for one requirement or the whole project

No ``req_accept``/``req_reject``: ADP-18's own limit ("no tratar una
propuesta de modelo como decisión humana") means accepting or rejecting a
requirement is not something this file offers the agent at all -- it is a
human action, made through ``PATCH .../requirements/{key}`` with
``by: 'human'`` from the UI, never a tool call the model can reach for
itself. ``req_propose``/``req_link`` cannot be talked into skipping that:
``src.requirements.store.Store.update``/``create`` enforce it independently
of this file (`requirements.model_cannot_decide`), the same defense-in-depth
``board_tools.py`` leaves to ``project_board.Store`` itself.

The project is resolved from ``ctx["project_id"]`` -- the same context key
``board_tools.py``/``git_tools.py`` already read. A chat with no project
bound has no spec to act on: every tool here refuses up front with
``error_class`` ``requirements.no_project``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from src import requirements as req

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument parsing (same permissive JSON-object shape every action-dispatched
# tool in this codebase accepts -- see board_tools.py / git_tools.py)
# ---------------------------------------------------------------------------
def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _owner(ctx: dict) -> str:
    return str((ctx or {}).get("owner") or "")


def _project_id(ctx: dict) -> Optional[str]:
    pid = str((ctx or {}).get("project_id") or "").strip()
    return pid or None


def _workspace(ctx: dict) -> str:
    """This chat's project workspace, resolved the same way
    `git_tools._resolve_repo_root` does -- through `services.projects`, not
    a second copy of project state kept in `ctx`."""
    project_id = _project_id(ctx)
    if not project_id:
        return ""
    try:
        from services.projects import get_store
        project = get_store().get(project_id, _owner(ctx) or None)
    except Exception:  # noqa: BLE001 - a tool must never crash the turn over this
        logger.debug("requirement_tools._workspace: could not resolve project", exc_info=True)
        return ""
    return (project or {}).get("workspace") or ""


def _no_project(tool: str) -> Dict[str, Any]:
    return {
        "error": f"{tool}: this chat has no project -- requirements belong to a project. "
                 "Open or create a project for this chat first.",
        "exit_code": 1, "error_class": "requirements.no_project",
    }


def _req_error(tool: str, exc: req.RequirementsError) -> Dict[str, Any]:
    return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": exc.error_class}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
class ReqListTool:
    """`req_list`: this project's requirements, filtered by status/source
    and/or a text search `q` over title and text."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("req_list")
        items = req.list_requirements(
            project_id, status=str(args.get("status") or ""),
            source=str(args.get("source") or ""), q=str(args.get("q") or ""),
        )
        lines = [f"{r['key']} [{r['status']}] {r['title']}" for r in items]
        return {"output": "\n".join(lines) or "(no requirements)", "exit_code": 0, "requirements": items}


class ReqGetTool:
    """`req_get`: one requirement's full detail -- text, acceptance
    criteria, links. Read-only."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("req_get")
        key = str(args.get("key") or args.get("id") or "").strip()
        if not key:
            return {"error": "req_get: `key` is required", "exit_code": 1}
        item = req.get(project_id, key)
        if item is None:
            return {"error": f"req_get: {key} not found in this project", "exit_code": 1,
                     "error_class": "requirements.not_found"}
        lines = [f"{item['key']} [{item['status']}] {item['title']}"]
        if item.get("text"):
            lines.append("")
            lines.append(item["text"])
        for c in item.get("acceptance") or []:
            lines.append(f"- {c}")
        return {"output": "\n".join(lines), "exit_code": 0, "requirement": item}


class ReqMatrixTool:
    """`req_matrix`: linked/implemented/tested/verified/stale coverage for
    one requirement (`key` given) or every requirement of this project
    (`key` omitted). Read-only -- may refresh a link's cached `state` to
    match what is on disk right now, never anything else."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("req_matrix")
        workspace = _workspace(ctx)
        key = str(args.get("key") or args.get("id") or "").strip()
        if key:
            try:
                row = req.matrix(project_id, key, workspace=workspace)
            except req.RequirementsError as exc:
                return _req_error("req_matrix", exc)
            dims = ", ".join(f"{k}={row[k]}" for k in ("linked", "implemented", "tested", "verified", "stale"))
            return {"output": f"{key}: {dims}", "exit_code": 0, "matrix": row}
        rows = req.project_matrix(project_id, workspace=workspace)
        lines = [
            f"{r['key']}: linked={r['linked']} implemented={r['implemented']} "
            f"tested={r['tested']} verified={r['verified']} stale={r['stale']}"
            for r in rows
        ]
        return {"output": "\n".join(lines) or "(no requirements)", "exit_code": 0, "matrix": rows}


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------
class ReqProposeTool:
    """`req_propose`: file a new requirement as a MODEL PROPOSAL. Always
    born `proposed_by: 'model'`, `status: 'proposed'` regardless of what is
    asked -- `src.requirements.store.Store.create` enforces this
    independently, this tool cannot override it. Returns the new key; the
    agent should cite it back ("Propuesto como REQ-4, pendiente de que lo
    aceptes"), never invent one."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("req_propose")
        title = str(args.get("title") or "").strip()
        if not title:
            return {"error": "req_propose: `title` is required", "exit_code": 1}
        acceptance = args.get("acceptance")
        try:
            item = req.create(
                project_id, title=title, text=str(args.get("text") or ""),
                source=str(args.get("source") or "human"),
                acceptance=acceptance if isinstance(acceptance, list) else [],
                proposed_by="model", created_by=_owner(ctx) or "agent",
            )
        except req.RequirementsError as exc:
            return _req_error("req_propose", exc)
        return {"output": f"Proposed {item['key']} (status: proposed, awaiting human acceptance): {item['title']}",
                "exit_code": 0, "requirement": item}


class ReqLinkTool:
    """`req_link`: attach an `implements`/`tests`/`evidences`/`issue` link
    to a requirement. A filesystem target (`implements`/`tests`) is
    validated against this project's workspace -- a target that resolves
    outside it is refused (`requirements.path_outside_workspace`), never
    silently accepted."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("req_link")
        key = str(args.get("key") or args.get("id") or "").strip()
        kind = str(args.get("kind") or "").strip().lower()
        target = str(args.get("target") or "").strip()
        if not key or not kind or not target:
            return {"error": "req_link: `key`, `kind` and `target` are required", "exit_code": 1}
        if req.get(project_id, key) is None:
            return {"error": f"req_link: {key} not found in this project", "exit_code": 1,
                     "error_class": "requirements.not_found"}
        try:
            link = req.link_evidence(
                project_id, key, kind=kind, target=target, workspace=_workspace(ctx),
                revision=str(args.get("revision") or ""), created_by=_owner(ctx) or "agent",
            )
        except req.RequirementsError as exc:
            return _req_error("req_link", exc)
        return {"output": f"Linked {key} {kind} -> {target}", "exit_code": 0, "link": link}
