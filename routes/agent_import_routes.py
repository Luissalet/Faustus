"""Agent importer API — /api/agent-import/* (src/agent_importer.py).

  GET  /api/agent-import/preview?agents=claude-code,codex,cursor[&root_<agent>=path]
         every importable item with its status (new / changed / unchanged / exists)
  POST /api/agent-import/apply   {"agents": [...], "keys"?: [...], "roots"?: {agent: path}}
         import the chosen items (default: every new or changed one)
  POST /api/agent-import/resync  {"dry_run"?: bool}
         re-read every agent imported before and import what changed there
  GET  /api/agent-import/manifest   what was imported, from where, into what

Memories and skills are written in-process (the memory store and the skill
library this app owns); MCP servers go through this app's own
``/api/mcp/servers`` with the caller's credentials, so they are registered,
governed and connected exactly as when added by hand; agent definitions are
files under ``DATA_DIR/agents``.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import agent_importer
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

_FORWARD = ("authorization", "cookie", "x-odysseus-internal-token")


class AppTarget:
    """Where an import lands in this running app."""

    def __init__(self, request: Request, memory_manager, memory_vector, skills_manager):
        self.request = request
        self.owner = _owner(request)
        self.memory = memory_manager
        self.vector = memory_vector
        self.skills = skills_manager

    # memories
    def add_memory(self, text: str, pinned: bool, source: str) -> str:
        entry = self.memory.add_entry(text, source, "instruction" if pinned else "fact",
                                      owner=self.owner, lease_days=0)
        if pinned:
            entry["pinned"] = True
        all_mem = self.memory.load_all_for_update()
        all_mem.append(entry)
        self.memory.save(all_mem)
        if self.vector is not None and getattr(self.vector, "healthy", False):
            self.vector.add(entry["id"], entry["text"])
        return entry["id"]

    def remove_memory(self, memory_id: str) -> None:
        all_mem = self.memory.load_all_for_update()
        kept = [m for m in all_mem if not (m.get("id") == memory_id and (m.get("owner") or None) == self.owner)]
        if len(kept) != len(all_mem):
            self.memory.save(kept)
            if self.vector is not None and getattr(self.vector, "healthy", False):
                self.vector.remove(memory_id)

    # MCP servers, through the app's own route
    def _client(self) -> httpx.AsyncClient:
        from src.constants import internal_api_base
        base = str(self.request.base_url).rstrip("/") or internal_api_base()
        headers = {k: v for k, v in self.request.headers.items() if k.lower() in _FORWARD}
        return httpx.AsyncClient(base_url=base, headers=headers, timeout=90, trust_env=False)

    async def add_mcp(self, spec: Dict[str, Any]) -> str:
        import json
        form = {"name": spec["name"], "transport": spec["transport"],
                "args": json.dumps(spec.get("args") or []), "env": json.dumps(spec.get("env") or {})}
        if spec["transport"] == "stdio":
            form["command"] = spec["command"]
        else:
            form["url"] = spec["url"]
        async with self._client() as client:
            r = await client.post("/api/mcp/servers", data=form)
            if r.status_code >= 400:
                raise RuntimeError(f"MCP server {spec['name']}: {r.status_code} {r.text[:200]}")
            server_id = str((r.json() or {}).get("id") or "")
            if spec.get("disabled") and server_id:
                await client.patch(f"/api/mcp/servers/{server_id}", data={"is_enabled": "false"})
        return server_id

    async def remove_mcp(self, server_id: str) -> None:
        async with self._client() as client:
            await client.delete(f"/api/mcp/servers/{server_id}")

    # skills
    def add_skill(self, files: Dict[str, str], source: str) -> str:
        entry = self.skills.import_bundle_from_files(files, owner=self.owner, source_url=source,
                                                     category="imported", status="published")
        return str(entry.get("name") or "")

    def remove_skill(self, name: str) -> None:
        self.skills.delete_skill(name, owner=self.owner)

    # agent definitions
    def write_agent(self, slug: str, text: str) -> str:
        from core.atomic_io import atomic_write_text
        from src import agent_defs
        agent_defs.parse(text, slug=slug)          # refuse a file that would not load
        root = agent_defs.agents_root()
        base, n = slug, 2
        while os.path.exists(os.path.join(root, slug)):
            slug, n = f"{base}-{n}", n + 1
        os.makedirs(os.path.join(root, slug), exist_ok=True)
        atomic_write_text(os.path.join(root, slug, "AGENT.md"), text)
        return slug

    def remove_agent(self, slug: str) -> None:
        from src import agent_defs
        clean = agent_defs.clean_slug(slug)
        path = os.path.join(agent_defs.agents_root(), clean)
        if clean and os.path.isdir(path):
            shutil.rmtree(path)


def _owner(request: Request) -> Optional[str]:
    """The person the import belongs to; a built-in tool's call is the owner's."""
    owner = effective_user(request) or None
    try:
        from src.owner_identity import INTERNAL_TOOL_USER
        if owner == INTERNAL_TOOL_USER:
            from src.chat_bridges.turn import resolve_owner
            owner = resolve_owner("") or None
    except Exception:  # noqa: BLE001
        pass
    return owner


def _existing(skills_manager, owner: Optional[str]) -> Dict[str, set]:
    out: Dict[str, set] = {"mcp": set(), "skill": set()}
    try:
        from core.database import McpServer, SessionLocal
        db = SessionLocal()
        try:
            out["mcp"] = {row.name for row in db.query(McpServer.name).all() if row.name}
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.debug("agent import: MCP server names unavailable", exc_info=True)
    try:
        out["skill"] = {s.get("name") for s in skills_manager.load_all() if s.get("name")}
    except Exception:  # noqa: BLE001
        pass
    return out


def _agents(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = [a for a in raw.split(",") if a.strip()]
    agents = [str(a).strip().lower() for a in (raw or agent_importer.AGENTS)]
    bad = [a for a in agents if a not in agent_importer.AGENTS]
    if bad:
        raise HTTPException(400, f"unknown agent(s) {', '.join(bad)}: use {', '.join(agent_importer.AGENTS)}")
    return agents


def setup_agent_import_routes(memory_manager, memory_vector, skills_manager) -> APIRouter:
    router = APIRouter(prefix="/api/agent-import", tags=["agent-import"])

    @router.get("/preview")
    async def get_preview(request: Request, agents: str = "") -> Dict[str, Any]:
        require_admin(request)
        names = _agents(agents)
        roots = {a: request.query_params.get(f"root_{a}") for a in names if request.query_params.get(f"root_{a}")}
        pv = agent_importer.preview(names, roots, existing=_existing(skills_manager, effective_user(request)))
        return {"sources": pv["sources"]}

    @router.post("/apply")
    async def post_apply(request: Request) -> Dict[str, Any]:
        require_admin(request)
        body = await request.json() if (request.headers.get("content-type") or "").startswith("application/json") else {}
        names = _agents(body.get("agents"))
        roots = {k: str(v) for k, v in (body.get("roots") or {}).items() if k in names and v}
        pv = agent_importer.preview(names, roots, existing=_existing(skills_manager, effective_user(request)))
        keys = body.get("keys")
        target = AppTarget(request, memory_manager, memory_vector, skills_manager)
        return await agent_importer.apply(pv["_items"], target, keys=keys if isinstance(keys, list) else None,
                                          roots={a: roots.get(a, "") for a in names})

    @router.post("/resync")
    async def post_resync(request: Request) -> Dict[str, Any]:
        require_admin(request)
        body = await request.json() if (request.headers.get("content-type") or "").startswith("application/json") else {}
        target = AppTarget(request, memory_manager, memory_vector, skills_manager)
        return await agent_importer.resync(target, existing=_existing(skills_manager, effective_user(request)),
                                           dry_run=bool(body.get("dry_run")))

    @router.get("/manifest")
    async def get_manifest(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return agent_importer.load_manifest()

    return router
