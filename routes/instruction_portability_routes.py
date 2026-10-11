"""Native instruction export and Markdown-check API, sharing canonical stores."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src import agent_defs, instruction_portability, markdown_hooks, project_rules
from src.auth_helpers import effective_user


class ExportRequest(BaseModel):
    target: str = "codex"
    workspace: str = ""
    destination: str = ""
    skills: list[str] | None = None
    rules: list[str] | None = None
    agents: list[str] | None = None
    overwrite: bool = False


class HookSaveRequest(BaseModel):
    workspace: str
    rule: dict[str, Any]
    overwrite: bool = False


class HookProposeRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    session_id: str = ""


class HookEvaluateRequest(BaseModel):
    workspace: str
    event: str
    context: dict[str, Any] = Field(default_factory=dict)


def collect_sources(skills_manager, workspace: str, owner=None) -> dict:
    skills, warnings = [], []
    for row in skills_manager.load(owner=owner):
        row = dict(row)
        path = row.get("path")
        if path:
            try:
                row["markdown"] = Path(path).read_text(encoding="utf-8")
            except OSError as error:
                warnings.append(f"{row.get('name')}: source file unavailable ({error}); structured skill fields retained.")
        skills.append(row)
    rules = project_rules.project_rules(workspace) if workspace else []
    for rule in rules:
        if rule.get("error"):
            warnings.append(f"{rule['id']}: {rule['error']}")
    # Exporting user-selected text is not dispatching an agent. Read project
    # definitions without changing the runtime's workspace-trust state, then
    # use the canonical parser and inheritance resolver for their actual body.
    loaded = agent_defs.load_all(None)
    warnings.extend(str(error.get("reason") or error) for error in loaded.errors)
    definitions = {agent.slug: agent for agent in loaded.agents}
    if workspace:
        root = Path(workspace).expanduser().resolve()
        folder = root / agent_defs.REPO_DIR
        if folder.is_dir() and folder.resolve().is_relative_to(root):
            for path in sorted(folder.glob("*.md")):
                if not path.resolve().is_relative_to(root):
                    warnings.append(f"{path.name}: linked definition outside the selected project was not read.")
                    continue
                try:
                    definition = agent_defs.parse(path.read_text(encoding="utf8"), slug=path.stem,
                                                  source=agent_defs.SOURCE_REPO, path=str(path))
                    definitions[definition.slug] = definition
                except (OSError, ValueError) as error:
                    warnings.append(f"{path.name}: {error}")
    catalogue = agent_defs.LoadResult(agents=list(definitions.values()))
    agents = []
    for definition in definitions.values():
        try:
            agents.append(agent_defs.resolve_extends(definition.slug, defs=catalogue).to_dict())
        except ValueError as error:
            warnings.append(f"{definition.slug}: {error}")
    return {"skills": skills, "rules": rules, "agents": agents, "warnings": warnings}


def _selected(rows, names, key):
    return rows if names is None else [row for row in rows if row.get(key) in names]


def setup_instruction_portability_routes(skills_manager, session_manager=None) -> APIRouter:
    router = APIRouter(prefix="/api/instruction-portability", tags=["instruction-portability"])

    def preview(request: Request, body: ExportRequest):
        sources = collect_sources(skills_manager, body.workspace, effective_user(request))
        bundle = instruction_portability.preview_bundle(
            _selected(sources["skills"], body.skills, "name"),
            _selected(sources["rules"], body.rules, "id"),
            _selected(sources["agents"], body.agents, "slug"), body.target)
        bundle["warnings"] = sources["warnings"] + bundle["warnings"]
        for kind, key in (("skills", "name"), ("rules", "id"), ("agents", "slug")):
            names = getattr(body, kind)
            if names is not None:
                available = {row.get(key) for row in sources[kind]}
                missing = [name for name in names if name not in available]
                if missing:
                    bundle["warnings"].append(f"{kind}: unavailable selections: {', '.join(missing)}.")
        return bundle

    @router.get("/catalog")
    def catalog(request: Request, workspace: str = ""):
        sources = collect_sources(skills_manager, workspace, effective_user(request))
        return {"targets": list(instruction_portability.TARGETS), "warnings": sources["warnings"],
                "skills": [{"id": row.get("name"), "name": row.get("name"), "description": row.get("description", "")} for row in sources["skills"]],
                "rules": [{"id": row.get("id"), "name": row.get("id"), "description": row.get("description", "")} for row in sources["rules"]],
                "agents": [{"id": row.get("slug"), "name": row.get("name"), "description": row.get("description", "")} for row in sources["agents"]]}

    @router.post("/preview")
    def export_preview(request: Request, body: ExportRequest):
        try:
            return preview(request, body)
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    @router.post("/export")
    def export(request: Request, body: ExportRequest):
        try:
            destination = body.destination or body.workspace
            if not destination:
                raise ValueError("Choose an export destination folder.")
            bundle = preview(request, body)
            return instruction_portability.write_bundle(destination, bundle, overwrite=body.overwrite)
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    @router.get("/hooks")
    def list_hooks(workspace: str):
        return markdown_hooks.load_rules(workspace)

    @router.post("/hooks/save")
    def save_hook(body: HookSaveRequest):
        try:
            return markdown_hooks.save_rule(body.workspace, body.rule, overwrite=body.overwrite)
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    @router.post("/hooks/propose")
    def propose(request: Request, body: HookProposeRequest):
        messages = body.messages
        if body.session_id:
            if session_manager is None:
                raise HTTPException(400, "Session history is unavailable in this context; pass messages instead.")
            try:
                session = session_manager.get_session(body.session_id)
            except (KeyError, ValueError) as error:
                raise HTTPException(404, "Session not found") from error
            owner = effective_user(request)
            if owner is not None and getattr(session, "owner", None) != owner:
                raise HTTPException(404, "Session not found")
            messages = [{"role": item.get("role", "") if isinstance(item, dict) else getattr(item, "role", ""),
                         "content": item.get("content", "") if isinstance(item, dict) else getattr(item, "content", "")} for item in session.history]
        return markdown_hooks.propose_hooks(messages)

    @router.post("/hooks/evaluate")
    def evaluate(body: HookEvaluateRequest):
        loaded = markdown_hooks.load_rules(body.workspace)
        context = {**body.context, "workspace": body.workspace}
        event = markdown_hooks._LIFECYCLE.get(body.event, body.event)
        matches = markdown_hooks.evaluate_rules(loaded["rules"], event, context)
        return {"matched": matches, "warnings": loaded["warnings"], "advisory": True, "blocked": False}

    return router
