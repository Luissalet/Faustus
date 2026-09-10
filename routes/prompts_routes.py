"""Prompt library routes (P1 UX-10): reusable templates with typed variables.

Backed by a single JSON file under ``DATA_DIR`` (the same durability pattern
as :class:`src.preset_manager.PresetManager` — atomic write, corruption falls
back to an empty store rather than 500ing the whole route). Everything lives
in this one module on purpose: lot 49a owns only this file for UX-10, not a
new ``src/`` module.

Rendering a template (``POST /api/prompts/{id}/render``) only ever returns
text. It never calls a model and never sends a chat turn — "guardar
selección como plantilla ... sin enviar automáticamente" in the spec. Sending
what comes back is entirely a client decision (Composer.tsx pastes it into
the draft).

Import/export deliberately never carries an ``owner`` or a raw ``project_id``:
those are local identities the destination install does not share. A
project-scoped template instead travels with the *workspace path* of the
project it came from. On import, a path that does not match any project the
importing owner can see is refused and returned as ``needs_resolution``
rather than silently reattached to whatever project used to sit at that id —
the id could easily belong to someone else's project by now.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from core.middleware import require_admin
from src.auth_helpers import effective_user
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

PROMPTS_FILE = os.path.join(DATA_DIR, "prompts.json")
_ALLOWED_VAR_TYPES = ("string", "number", "boolean", "choice")
_VAR_TOKEN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_MAX_HISTORY = 20
_MAX_LIST = 500

_lock = threading.RLock()


# ── storage ──────────────────────────────────────────────────────────────

def _load() -> Dict[str, Any]:
    if not os.path.exists(PROMPTS_FILE):
        return {"templates": {}, "favorites": {}}
    try:
        with open(PROMPTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("templates"), dict):
            logger.error("prompts.json malformed (not a {templates: {}} object); treating as empty")
            return {"templates": {}, "favorites": {}}
        data.setdefault("favorites", {})
        return data
    except Exception as e:
        logger.error("Failed to load prompts.json: %s", e)
        return {"templates": {}, "favorites": {}}


def _save(data: Dict[str, Any]) -> None:
    from core.atomic_io import atomic_write_json
    atomic_write_json(PROMPTS_FILE, data, indent=2)


def _owned(row: Dict[str, Any], owner: Optional[str]) -> bool:
    """Personal templates are visible only to their owner; project templates
    are visible to anyone (project membership isn't this module's concern —
    the project routes already gate who can see a given project_id)."""
    if row.get("scope") == "personal":
        return bool(owner) and row.get("owner") == owner
    return True


# ── request/response models ─────────────────────────────────────────────

class PromptVariable(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: str = Field("string")
    required: bool = True
    default: Optional[Any] = None
    choices: Optional[List[str]] = None
    help: str = Field("", max_length=500)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in _ALLOWED_VAR_TYPES:
            raise ValueError(f"Unsupported variable type: {v}. Use one of {_ALLOWED_VAR_TYPES}.")
        return v


class PromptExample(BaseModel):
    label: str = Field("", max_length=200)
    values: Dict[str, Any] = Field(default_factory=dict)


class PromptTemplateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    body: str = Field(..., min_length=1, max_length=20000)
    variables: List[PromptVariable] = Field(default_factory=list)
    scope: str = Field("personal", pattern="^(personal|project)$")
    project_id: Optional[str] = Field(None, max_length=200)
    tags: List[str] = Field(default_factory=list)
    examples: List[PromptExample] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: List[str]) -> List[str]:
        return [t.strip()[:64] for t in v if t and t.strip()][:20]


class PromptRenderIn(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


class PromptImportIn(BaseModel):
    templates: List[Dict[str, Any]] = Field(default_factory=list)
    # Maps a template's exported `source_project_workspace` to the project_id
    # to attach it to in THIS install. Only entries the caller explicitly
    # resolved are ever written under a project.
    resolve: Dict[str, str] = Field(default_factory=dict)


def _undeclared_and_missing_vars(body: str, variables: List[PromptVariable]) -> List[str]:
    """Every `{{name}}` token in the body that no declared variable covers.
    A template that references a variable it never declares silently renders
    the literal `{{typo}}` into the sent text — this makes that a save-time
    error instead of a surprise the user notices only after sending."""
    declared = {v.name for v in variables}
    used = set(_VAR_TOKEN.findall(body))
    return sorted(used - declared)


def _next_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_SECRET_LIKE = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _scrub(text: str) -> str:
    """Defense in depth: a long opaque token embedded in a template (an API
    key pasted into an example value, say) never leaves this module. Real
    words and sentences are far shorter than 24 unbroken alnum/`_`/`-`
    characters, so this only ever touches token-shaped runs."""
    return _SECRET_LIKE.sub("[redacted]", text)


def _public(row: Dict[str, Any], owner: Optional[str]) -> Dict[str, Any]:
    favorites = set()  # filled by caller when needed
    out = {k: v for k, v in row.items() if k != "history"}
    out["history_count"] = len(row.get("history", []))
    return out


def setup_prompt_routes() -> APIRouter:
    router = APIRouter(prefix="/api/prompts", tags=["prompts"])

    @router.get("")
    async def list_prompts(request: Request, q: str = "", project_id: str = "",
                            scope: str = "", _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        with _lock:
            data = _load()
            fav_ids = set(data.get("favorites", {}).get(owner or "", []))
            rows = [r for r in data["templates"].values() if _owned(r, owner)]
        if project_id:
            rows = [r for r in rows if r.get("project_id") == project_id]
        if scope in ("personal", "project"):
            rows = [r for r in rows if r.get("scope") == scope]
        needle = q.strip().lower()
        if needle:
            rows = [r for r in rows if needle in r.get("name", "").lower()
                    or needle in r.get("description", "").lower()
                    or any(needle in t.lower() for t in r.get("tags", []))]
        rows = sorted(rows, key=lambda r: (r.get("id") not in fav_ids, r.get("name", "").lower()))[:_MAX_LIST]
        for r in rows:
            r = r  # rows are dicts from storage; mark favorite without mutating storage
        out = []
        for r in rows:
            item = _public(r, owner)
            item["favorite"] = r.get("id") in fav_ids
            out.append(item)
        return {"templates": out}

    @router.post("")
    async def create_prompt(payload: PromptTemplateIn, request: Request,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        missing = _undeclared_and_missing_vars(payload.body, payload.variables)
        if missing:
            raise HTTPException(400, f"Body references undeclared variables: {', '.join(missing)}")
        if payload.scope == "project" and not payload.project_id:
            raise HTTPException(400, "A project-scoped template needs a project_id")
        row = payload.model_dump()
        row["body"] = _scrub(row["body"])
        row["id"] = _next_id()
        row["owner"] = owner
        row["version"] = 1
        row["history"] = []
        row["created_at"] = _now()
        row["updated_at"] = row["created_at"]
        with _lock:
            data = _load()
            data["templates"][row["id"]] = row
            _save(data)
        return _public(row, owner)

    @router.get("/{prompt_id}")
    async def get_prompt(prompt_id: str, request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        with _lock:
            row = _load()["templates"].get(prompt_id)
        if not row or not _owned(row, owner):
            raise HTTPException(404, "Prompt template not found")
        return _public(row, owner)

    @router.put("/{prompt_id}")
    async def update_prompt(prompt_id: str, payload: PromptTemplateIn, request: Request,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        missing = _undeclared_and_missing_vars(payload.body, payload.variables)
        if missing:
            raise HTTPException(400, f"Body references undeclared variables: {', '.join(missing)}")
        with _lock:
            data = _load()
            row = data["templates"].get(prompt_id)
            if not row or not _owned(row, owner):
                raise HTTPException(404, "Prompt template not found")
            if row.get("scope") == "personal" and row.get("owner") != owner:
                raise HTTPException(403, "Not your template")
            history = row.get("history", [])
            history.append({
                "version": row.get("version", 1), "body": row.get("body", ""),
                "variables": row.get("variables", []), "updated_at": row.get("updated_at"),
            })
            row.update(payload.model_dump())
            row["body"] = _scrub(row["body"])
            row["history"] = history[-_MAX_HISTORY:]
            row["version"] = row.get("version", 1) + 1
            row["updated_at"] = _now()
            _save(data)
        return _public(row, owner)

    @router.delete("/{prompt_id}")
    async def delete_prompt(prompt_id: str, request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        with _lock:
            data = _load()
            row = data["templates"].get(prompt_id)
            if not row or not _owned(row, owner):
                raise HTTPException(404, "Prompt template not found")
            if row.get("scope") == "personal" and row.get("owner") != owner:
                raise HTTPException(403, "Not your template")
            del data["templates"][prompt_id]
            for uid, ids in data.get("favorites", {}).items():
                data["favorites"][uid] = [i for i in ids if i != prompt_id]
            _save(data)
        return {"ok": True}

    @router.post("/{prompt_id}/favorite")
    async def toggle_favorite(prompt_id: str, request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            data = _load()
            row = data["templates"].get(prompt_id)
            if not row or not _owned(row, owner):
                raise HTTPException(404, "Prompt template not found")
            favs = data.setdefault("favorites", {}).setdefault(owner, [])
            if prompt_id in favs:
                favs.remove(prompt_id)
                on = False
            else:
                favs.append(prompt_id)
                on = True
            _save(data)
        return {"favorite": on}

    @router.post("/{prompt_id}/render")
    async def render_prompt(prompt_id: str, payload: PromptRenderIn, request: Request,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        with _lock:
            row = _load()["templates"].get(prompt_id)
        if not row or not _owned(row, owner):
            raise HTTPException(404, "Prompt template not found")
        values = dict(payload.values)
        missing = []
        for var in row.get("variables", []):
            name = var["name"]
            if name in values:
                continue
            if var.get("default") is not None:
                values[name] = var["default"]
            elif var.get("required", True):
                missing.append(name)
        if missing:
            raise HTTPException(400, f"Missing required variable(s): {', '.join(missing)}")
        for var in row.get("variables", []):
            if var["type"] == "choice" and var.get("choices") and var["name"] in values:
                if str(values[var["name"]]) not in var["choices"]:
                    raise HTTPException(400, f"'{values[var['name']]}' is not a valid choice for {var['name']}")

        def _sub(m: "re.Match[str]") -> str:
            return str(values.get(m.group(1), m.group(0)))

        text = _VAR_TOKEN.sub(_sub, row["body"])
        return {"text": text}

    @router.get("/export/batch")
    async def export_prompts(request: Request, ids: str = "", _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        wanted = {i for i in ids.split(",") if i}
        with _lock:
            data = _load()
        rows = [r for r in data["templates"].values() if _owned(r, owner) and (not wanted or r["id"] in wanted)]
        from services.projects import get_store
        projects = {p["id"]: p for p in get_store().list(owner)}
        out = []
        for r in rows:
            item = {k: v for k, v in r.items() if k not in ("owner", "id", "history", "project_id")}
            if r.get("scope") == "project" and r.get("project_id") in projects:
                item["source_project_workspace"] = projects[r["project_id"]].get("workspace", "")
            out.append(item)
        return {"templates": out}

    @router.post("/import")
    async def import_prompts(payload: PromptImportIn, request: Request,
                              _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request)
        from services.projects import get_store
        by_workspace = {p.get("workspace"): p["id"] for p in get_store().list(owner) if p.get("workspace")}
        valid_project_ids = {p["id"] for p in get_store().list(owner)}

        imported: List[str] = []
        needs_resolution: List[Dict[str, Any]] = []
        with _lock:
            data = _load()
            for tpl in payload.templates:
                tpl = dict(tpl)
                source_ws = tpl.pop("source_project_workspace", None)
                scope = tpl.get("scope", "personal")
                project_id = None
                if scope == "project":
                    resolved = payload.resolve.get(source_ws or "")
                    if resolved and resolved in valid_project_ids:
                        project_id = resolved
                    elif source_ws and source_ws in by_workspace:
                        project_id = by_workspace[source_ws]
                    else:
                        # Refuse to guess: writing this under a stale/foreign
                        # project id would attach it to whatever project now
                        # happens to hold that id. Ask the caller to resolve it.
                        needs_resolution.append({
                            "name": tpl.get("name", ""),
                            "source_project_workspace": source_ws or "",
                        })
                        continue
                try:
                    variables = [PromptVariable(**v) for v in tpl.get("variables", [])]
                except Exception as e:
                    needs_resolution.append({"name": tpl.get("name", ""), "error": f"Invalid variables: {e}"})
                    continue
                body = str(tpl.get("body", ""))
                missing_vars = _undeclared_and_missing_vars(body, variables)
                if missing_vars:
                    needs_resolution.append({
                        "name": tpl.get("name", ""),
                        "error": f"Body references undeclared variables: {', '.join(missing_vars)}",
                    })
                    continue
                row = {
                    "id": _next_id(), "owner": owner, "version": 1, "history": [],
                    "name": str(tpl.get("name", "Imported prompt"))[:200],
                    "description": str(tpl.get("description", ""))[:2000],
                    "body": _scrub(body),
                    "variables": [v.model_dump() for v in variables],
                    "scope": scope if scope in ("personal", "project") else "personal",
                    "project_id": project_id,
                    "tags": [str(t)[:64] for t in tpl.get("tags", [])][:20],
                    "examples": tpl.get("examples", []),
                    "created_at": _now(), "updated_at": _now(),
                }
                data["templates"][row["id"]] = row
                imported.append(row["id"])
            if imported:
                _save(data)
        return {"imported": imported, "needs_resolution": needs_resolution}

    return router
