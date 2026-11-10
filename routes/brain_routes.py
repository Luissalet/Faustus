"""The second brain over HTTP — `/api/brain` (Lot C).

`src/brain/` is a markdown vault Faustus owns: memories, personal notes and
typed entities mirrored into human-editable files, plus free notes, a note
graph, and time-windowed entity relations. This module is the thinnest
possible layer over `src.brain.vault` / `.notes` / `.entities` / `.extract`
/ `.wiki` / `.temporal` — every route below calls straight through to one of
those modules' public functions and shapes the response for
`studio/src/adapters/brain.ts`, which is what decides the exact field names:
built and tested against the routes contract before this file existed, its
readers are defensive about a field being absent, but matching its shapes
exactly (rather than "close enough") is what makes every screen in the Brain
UI actually populate.

**Auth, like the rest of a person's own data**: any authenticated caller
reads and writes their OWN vault (`owner = effective_user(request)`, gated
by `Depends(require_user)` — the same per-user pattern
`routes/board_routes.py` uses for a person's own project board). Nothing
here is admin-only except `PUT /settings`, which changes an install-wide
default (whether the whole brain runs at all, where the vault lives, how
often it syncs) rather than one person's data.

**Errors**: a `ValueError` out of `src.brain.*` (a bad path, an unknown
entity/trash id, a bad date) is a 400 — the caller sent something this vault
will not accept. `FileNotFoundError` (an unknown note path) is a 404.
Anything else is left to bubble to FastAPI's default 500 handler, same as
every other route file.

**Deviations from the routes contract worth naming** (Lot A/B's own wiring
notes call these out; nothing here silently disagrees with what the adapter
actually parses):

* `entities.profile(id)["entity"]` (used for `GET /entities/{id}`, `PATCH`,
  and `POST /entities/merge`) is enriched here with `mentions`/`relations`
  counts before it goes out, matching the shape `list_entities` already
  gives each row — `adapters/brain.ts`'s `entitySummaryFrom` reads either an
  array (uses its length) or a bare count, so this is additive, not a fix
  for a break.
* `POST /extract`'s and `POST /wiki/refresh`'s underlying reports
  (`extract.extract_pending`, `wiki.refresh_stale`/`refresh_entity`) count
  errors as an int, not a list of messages; the adapter's `errors` field
  wants `string[]`. Rather than silently dropping that count (an empty list
  reads as "nothing went wrong"), it is rendered as a one-line message when
  non-zero — real information a zero-then-empty-list would have hidden.
* `vault.sync`'s report carries `notes` as a `list[str]` (sync-phase
  annotations like "delete guard tripped"); the adapter's `SyncReport.notes`
  is typed `string` and reads a non-string value as `""`. The list is
  returned as-is (Studio simply does not render it today) rather than joined
  into a string that would not match what Lot D actually wrote against.
* `brain_context_source` is a BOOLEAN setting (`DEFAULT_SETTINGS`, `src/
  settings.py`); the adapter's `BrainSettings.brain_context_source` is typed
  `string` and its parser (`str(value)`) reads a boolean as `""`. Returned
  here as the real boolean `get_setting` gives, since a route lying about a
  setting's type to match a stale TS annotation would make `PUT /settings`
  round-trip a string back into a store that validates against `bool`.
"""

from __future__ import annotations

from src.brain.graph_ask import GraphAskRequest
from src.brain.temporal_graph import GraphQuery

import asyncio
import contextlib
import logging
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from core.middleware import require_admin
from src.auth_helpers import effective_user, require_user

logger = logging.getLogger(__name__)


def _owner(request: Request) -> str:
    return str(effective_user(request) or "").strip()


def _limit(value: Any, default: int, ceiling: int = 500) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(wanted, ceiling))


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return payload


def _enriched_entity(entity: Dict[str, Any]) -> Dict[str, Any]:
    """`entity` (a bare `entities.get_entity()` row) plus the `mentions`/
    `relations` counts `entities.list_entities()` already attaches to every
    row it returns — so a single-entity read looks the same shape as a row
    in the list, which is what `adapters/brain.ts`'s `entitySummaryFrom`
    expects either way (array-or-count)."""
    from src.brain import entities

    entity_id = str(entity.get("id") or "")
    out = dict(entity)
    try:
        out["mentions"] = entities.sources_for(entity_id)
    except Exception:  # noqa: BLE001 - a count must not break the read
        out.setdefault("mentions", [])
    try:
        out["relations"] = entities.list_relations(
            entity.get("owner") or "", entity_id=entity_id, include_closed=False)
    except Exception:  # noqa: BLE001
        out.setdefault("relations", [])
    return out


def _note_path_for_source(owner: str, source: str) -> Optional[str]:
    """The vault note mirroring `source` (e.g. `ent:<id>`), or None — used
    for `GET /entities/{id}`'s `path` field so the UI can jump from an
    entity's profile straight to its file."""
    try:
        from src.brain import db

        with db.db() as conn:
            row = conn.execute(
                "SELECT path FROM notes WHERE owner=? AND source=?", (owner, source),
            ).fetchone()
        return row["path"] if row else None
    except Exception:  # noqa: BLE001 - a missing cross-link is not an error
        return None


def _extract_report(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`extract.extract_pending`'s report, with `errors` (an int count in
    the data layer) also given as the `string[]` the adapter's
    `extractReportFrom` reads — see the module docstring."""
    out = dict(raw)
    error_count = int(raw.get("errors") or 0)
    out["errors"] = ([f"{error_count} item(s) failed to extract"] if error_count else [])
    return out


def _wiki_refresh_report(status: str) -> Dict[str, Any]:
    """One `wiki.refresh_entity` outcome, in `WikiRefreshReport` shape."""
    refreshed = 1 if status in ("updated", "fallback") else 0
    skipped = 1 if status in ("locked", "unchanged", "disabled", "deferred") else 0
    errors = [f"entity wiki refresh: {status}"] if status in ("error", "not_found") else []
    return {"refreshed": refreshed, "skipped": skipped, "errors": errors, "status": status}


def _wiki_stale_report(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`wiki.refresh_stale`'s report, in `WikiRefreshReport` shape."""
    error_count = int(raw.get("errors") or 0)
    return {
        "refreshed": int(raw.get("updated") or 0),
        "skipped": int(raw.get("skipped") or 0),
        "errors": ([f"{error_count} entity summary refresh(es) failed"] if error_count else []),
        "checked": int(raw.get("checked") or 0),
        # Deferred = the utility model was busy or not loaded; the entity keeps
        # its plain summary and a later sweep retries (never loads a model).
        "deferred": int(raw.get("deferred") or 0),
        "llm_skipped": str(raw.get("llm_skipped") or ""),
    }


_mirror_lock = threading.Lock()
_mirror_pending: Dict[str, set] = {}
_mirror_running = False


def _mirror_entities_later(owner: str, entity_ids) -> None:
    """Write the vault pages of `entity_ids` in the background. Rendering one
    page builds the whole export plan (seconds on a large vault), so a
    request never waits for it and a burst of writes shares one pass."""
    global _mirror_running
    with _mirror_lock:
        _mirror_pending.setdefault(owner, set()).update(str(i) for i in entity_ids if i)
        if _mirror_running:
            return
        _mirror_running = True
    threading.Thread(target=_mirror_worker, name="brain-entity-mirror", daemon=True).start()


def _mirror_worker() -> None:
    global _mirror_running
    from src.brain import vault

    while True:
        with _mirror_lock:
            owner = next((o for o, ids in _mirror_pending.items() if ids), None)
            if owner is None:
                _mirror_running = False
                return
            ids = sorted(_mirror_pending.pop(owner))
        try:
            run = vault._Run(owner, vault._new_report())
            try:
                vault._export_sources(run, [f"ent:{i}" for i in ids])
            finally:
                run.flush()
        except Exception:  # noqa: BLE001 - the next sync writes the pages anyway
            logger.debug("brain: entity pages not mirrored", exc_info=True)


_SETTINGS_KEYS = (
    "memory_temporal_parse", "memory_temporal_supersede", "brain_enabled",
    "brain_vault_dir", "brain_vault_sync_seconds", "brain_entity_extraction",
    "brain_llm_extraction", "brain_wiki_summaries", "brain_context_source",
    "owner_display_name",
)


def _settings_snapshot() -> Dict[str, Any]:
    from src.settings import get_setting

    return {key: get_setting(key) for key in _SETTINGS_KEYS}


def setup_brain_routes() -> APIRouter:
    router = APIRouter(prefix="/api/brain", tags=["brain"])

    @router.get("/facts")
    async def get_facts(request: Request, entity_id: str = "", rel: str = "", as_of: str = "",
                        known_at: str = "", include_closed: Optional[bool] = None,
                        limit: int = 200, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Bitemporal relations: current by default, `as_of` = world time,
        `known_at` = what the brain believed on that date (recorded time)."""
        from src.brain.bitemporal import facts
        try:
            return await asyncio.to_thread(
                facts, _owner(request), entity_id=entity_id or None, rel=rel or None,
                as_of=as_of or None, known_at=known_at or None,
                include_closed=include_closed, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/fact-history")
    async def get_fact_history(request: Request, subject: str = "", attribute: str = "",
                               limit: int = 20, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Current value and linked change chain of one subject + attribute
        (events with supersedes / superseded_by, dates and source)."""
        from src.brain.fact_history import fact_history
        try:
            return await asyncio.to_thread(fact_history, _owner(request), subject, attribute,
                                           limit=_limit(limit, 20, 200))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/temporal-graph")
    async def post_temporal_graph(request: Request, body: GraphQuery, _u: str = Depends(require_user)):
        from src.brain.temporal_graph import retrieve
        try:
            return await asyncio.to_thread(retrieve, _owner(request), body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/ask")
    async def post_ask(request: Request, body: GraphAskRequest, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Plan a multi-hop question over the owner's graph and its source texts
        (`src.brain.graph_ask`). Always 200 with a `status`; the graph is never written."""
        from src.brain import graph_ask
        return await graph_ask.ask(_owner(request), body.question, max_steps=body.max_steps, top_k=body.top_k,
                                   alpha=body.alpha, plan_only=body.plan_only)

    # ── status & sync ────────────────────────────────────────────────────

    @router.get("/status")
    async def get_status(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import entities, extract, notes, vault
        from src.settings import get_setting

        owner = _owner(request)
        note_count = len(notes.tree(owner).get("notes") or [])
        try:
            stats = entities.stats(owner)
        except Exception:  # noqa: BLE001 - entities is Lot B's own store
            stats = {"entities": 0, "relations_total": 0}
        return {
            "enabled": bool(get_setting("brain_enabled", True)),
            "vault_dir": vault.vault_root(owner),
            "notes": note_count,
            "entities": int(stats.get("entities") or 0),
            "relations": int(stats.get("relations_total") or 0),
            "last_sync": vault.last_sync(owner),
            "extraction": {
                "pending": extract.count_pending(owner),
                "llm_enabled": bool(get_setting("brain_llm_extraction", True)),
            },
            "wiki": {"enabled": bool(get_setting("brain_wiki_summaries", True))},
        }

    @router.post("/sync")
    async def post_sync(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import vault

        owner = _owner(request)
        return await asyncio.to_thread(vault.sync, owner)

    # ── notes ────────────────────────────────────────────────────────────

    @router.get("/tree")
    async def get_tree(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes

        return await asyncio.to_thread(notes.tree, _owner(request))

    @router.get("/note")
    async def get_note(request: Request, path: str = "",
                       _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes, vault

        try:
            return await asyncio.to_thread(notes.read_note, _owner(request), path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such note")
        except vault.VaultPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.put("/note")
    async def put_note(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes, vault

        payload = await _json_body(request)
        path = str(payload.get("path") or "")
        content = payload.get("content")
        if not path or content is None:
            raise HTTPException(status_code=400, detail="path and content are required")
        try:
            return await asyncio.to_thread(notes.write_note, _owner(request), path, str(content))
        except vault.VaultPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/note")
    async def post_note(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes, vault

        payload = await _json_body(request)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        folder = str(payload.get("folder") or "Notes")
        content = str(payload.get("content") or "")
        try:
            return await asyncio.to_thread(
                notes.create_note, _owner(request), title, folder=folder, content=content)
        except vault.VaultPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/note/rename")
    async def post_note_rename(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes, vault

        payload = await _json_body(request)
        path = str(payload.get("path") or "")
        new_title = str(payload.get("new_title") or "")
        update_links = bool(payload.get("update_links", True))
        if not path or not new_title:
            raise HTTPException(status_code=400, detail="path and new_title are required")
        try:
            return await asyncio.to_thread(
                notes.rename_note, _owner(request), path, new_title, update_links=update_links)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such note")
        except (vault.VaultPathError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.delete("/note")
    async def delete_note(request: Request, path: str = "",
                          _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes, vault

        try:
            return await asyncio.to_thread(notes.delete_note, _owner(request), path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such note")
        except vault.VaultPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ── trash ────────────────────────────────────────────────────────────

    @router.get("/trash")
    async def get_trash(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes

        items = await asyncio.to_thread(notes.list_trash, _owner(request))
        return {"items": items}

    @router.post("/trash/{trash_id}/restore")
    async def post_trash_restore(trash_id: str, request: Request,
                                 _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes

        try:
            return await asyncio.to_thread(notes.restore, _owner(request), trash_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ── search, graph, tags, unresolved links ───────────────────────────

    @router.get("/search")
    async def get_search(request: Request, q: str = "", limit: int = 20,
                         _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes

        results = await asyncio.to_thread(
            notes.search, _owner(request), q, limit=_limit(limit, 20, 200))
        return {"results": results}

    @router.get("/graph")
    async def get_graph(request: Request, center: str = "", depth: int = 1,
                        kinds: str = "", scope: str = "notes",
                        _u: str = Depends(require_user)) -> Dict[str, Any]:
        owner = _owner(request)
        wanted = [k.strip() for k in kinds.split(",") if k.strip()] or None
        if scope == "entities":
            from src.brain import entities

            return await asyncio.to_thread(entities.graph, owner, limit=500)
        from src.brain import notes, vault

        try:
            return await asyncio.to_thread(
                notes.graph, owner, center=(center or None), depth=max(0, min(5, depth)),
                kinds=wanted, limit=600)
        except vault.VaultPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ── JSON Canvas (src/brain/canvas.py) ───────────────────────────────

    def _canvas_export_kwargs(payload: Dict[str, Any]) -> Dict[str, Any]:
        def as_list(value: Any) -> Optional[list]:
            if isinstance(value, str):
                value = [v for v in value.split(",") if v.strip()]
            return [str(v) for v in value] if isinstance(value, list) and value else None

        return {
            "scope": str(payload.get("scope") or "notes"),
            "center": payload.get("center") or None,
            "depth": _limit(payload.get("depth", 1), 1, 6) if payload.get("depth") not in (0, "0") else 0,
            "kinds": as_list(payload.get("kinds")),
            "tags": as_list(payload.get("tags")),
            "group_by": payload.get("group_by") or None,
            "layout": str(payload.get("layout") or "auto"),
            "mode": str(payload.get("mode") or "file"),
            "include_unresolved": bool(payload.get("include_unresolved", True)),
            "limit": _limit(payload.get("limit", 300), 300, 2000),
            "excerpt_chars": _limit(payload.get("excerpt_chars", 600), 600, 4000),
            "save": bool(payload.get("save", False)),
            "name": str(payload.get("name") or ""),
        }

    @router.post("/canvas/export")
    async def post_canvas_export(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """The notes or entities graph (or a subgraph) as a JSON Canvas 1.0
        document; `save` also writes it to `Canvas/<name>.canvas` in the vault."""
        from src.brain import canvas

        payload = await _json_body(request)
        try:
            return await asyncio.to_thread(canvas.export_canvas, _owner(request), **_canvas_export_kwargs(payload))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/canvas/export.canvas")
    async def get_canvas_file(request: Request, scope: str = "notes", center: str = "", depth: int = 1,
                              kinds: str = "", tags: str = "", group_by: str = "", mode: str = "file",
                              limit: int = 300, name: str = "", _u: str = Depends(require_user)):
        """The same export as a file download (`<name>.canvas`)."""
        from fastapi.responses import Response
        from urllib.parse import quote
        from src.brain import canvas

        kwargs = _canvas_export_kwargs({"scope": scope, "center": center, "depth": depth, "kinds": kinds,
                                        "tags": tags, "group_by": group_by, "mode": mode, "limit": limit,
                                        "name": name})
        try:
            out = await asyncio.to_thread(canvas.export_canvas, _owner(request), **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return Response(canvas.dumps(out["canvas"]), media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(out['file_name'])}"})

    @router.post("/canvas/import")
    async def post_canvas_import(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Write a `.canvas` into the vault as notes and wikilinks, idempotent
        by node id. Body: `canvas` (object or JSON text), `name`, `folder`,
        `dry_run`, `prune`."""
        from src.brain import canvas

        payload = await _json_body(request)
        if payload.get("canvas") is None:
            raise HTTPException(status_code=400, detail="canvas is required")
        try:
            return await asyncio.to_thread(
                canvas.import_canvas, _owner(request), payload["canvas"],
                name=str(payload.get("name") or "Canvas"), folder=str(payload.get("folder") or "Canvas"),
                dry_run=bool(payload.get("dry_run", False)), prune=bool(payload.get("prune", False)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.delete("/canvas/import")
    async def delete_canvas_import(request: Request, name: str = "", folder: str = "Canvas", purge: bool = False,
                                   _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Undo an import: its notes to the trash (or gone with `purge`)."""
        from src.brain import canvas

        try:
            return await asyncio.to_thread(canvas.remove_import, _owner(request), name, folder=folder, purge=purge)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/canvas/validate")
    async def post_canvas_validate(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Check a document against JSON Canvas 1.0."""
        from src.brain import canvas

        payload = await _json_body(request)
        try:
            doc = canvas.load(payload.get("canvas"))
        except ValueError as exc:
            return {"ok": False, "errors": [str(exc)]}
        errors = canvas.validate(doc)
        return {"ok": not errors, "errors": errors, "nodes": len(doc.get("nodes") or []),
                "edges": len(doc.get("edges") or [])}

    # ── Obsidian Bases (src/brain/bases.py) ─────────────────────────────
    def _bases_call(owner: str, args: Dict[str, Any]) -> Dict[str, Any]:
        from src.brain import bases

        out = bases.run(owner, args)
        if out.get("error"):
            status = 404 if str(out.get("error_class", "")).endswith("not_found") else 400
            raise HTTPException(status_code=status, detail=out["error"])
        return out

    @router.get("/bases")
    async def get_bases(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Every `.base` file in the vault with its views and whether it is valid."""
        return await asyncio.to_thread(_bases_call, _owner(request), {"action": "list"})

    @router.get("/bases/file")
    async def get_base_file(request: Request, path: str, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """One base: its YAML and validation."""
        return await asyncio.to_thread(_bases_call, _owner(request), {"action": "get", "path": path})

    @router.put("/bases/file")
    async def put_base_file(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Write a base (`path`, and `yaml` or a form `spec`) after validating it."""
        payload = await _json_body(request)
        return await asyncio.to_thread(_bases_call, _owner(request), {**payload, "action": "save"})

    @router.delete("/bases/file")
    async def delete_base_file(request: Request, path: str, _u: str = Depends(require_user)) -> Dict[str, Any]:
        return await asyncio.to_thread(_bases_call, _owner(request), {"action": "delete", "path": path})

    @router.post("/bases/query")
    async def post_base_query(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Evaluate a base (`path` or inline `yaml`, optional `view`) against the notes."""
        payload = await _json_body(request)
        return await asyncio.to_thread(_bases_call, _owner(request), {**payload, "action": "query"})

    @router.post("/bases/validate")
    async def post_base_validate(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        payload = await _json_body(request)
        return await asyncio.to_thread(_bases_call, _owner(request), {**payload, "action": "validate"})

    @router.get("/tags")
    async def get_tags(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes

        return {"tags": await asyncio.to_thread(notes.tags, _owner(request))}

    @router.get("/unresolved")
    async def get_unresolved(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes

        return {"links": await asyncio.to_thread(notes.unresolved, _owner(request))}

    # ── daily note ───────────────────────────────────────────────────────

    @router.get("/daily")
    async def get_daily(request: Request, date: str = "",
                        _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import notes

        try:
            return await asyncio.to_thread(notes.daily_note, _owner(request), date or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ── entities ─────────────────────────────────────────────────────────

    @router.get("/entities")
    async def get_entities(request: Request, q: str = "", type: str = "",  # noqa: A002
                           _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import entities

        found = await asyncio.to_thread(
            entities.list_entities, _owner(request), q=q, type=type, limit=200)
        return {"entities": found}

    @router.post("/entities")
    async def post_entity(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        """Create-or-find an entity by name or alias and, optionally, cite
        sources for it and assert relations from it. Idempotent: the same
        body twice leaves one entity, one mention per source and one edge per
        relation. The vault pages are written in the background, so `path` is
        null until then. Body: `name` (required), `type`, `aliases`, `summary`,
        `mentions` (source refs such as `note:<path>`), `relations`
        (`[{rel, to | to_id | value, to_type?, valid_from?, evidence?}]`)."""
        from src.brain import entities

        owner = _owner(request)
        payload = await _json_body(request)
        name = " ".join(str(payload.get("name") or "").split())
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        relations_in = payload.get("relations") or []
        mentions_in = payload.get("mentions") or []
        if not isinstance(relations_in, list) or not isinstance(mentions_in, list):
            raise HTTPException(status_code=400, detail="relations and mentions must be lists")

        def _apply() -> Dict[str, Any]:
            known = entities.list_entities(owner, q=name, limit=200, include_hidden=True)
            before = {e["id"] for e in known}
            entity = entities.upsert_entity(
                owner, name, type=str(payload.get("type") or "other"),
                aliases=[str(a) for a in (payload.get("aliases") or []) if str(a).strip()])
            created = entity["id"] not in before and entity.get("created_at") == entity.get("updated_at")
            summary = str(payload.get("summary") or "").strip()
            if summary and not entity.get("summary_locked") and summary != (entity.get("summary") or ""):
                entity = entities.update_entity(entity["id"], summary=summary) or entity
            for ref in mentions_in:
                entities.add_mention(owner, entity["id"], str(ref or "").strip())
            touched = [entity["id"]]
            relations_out = []
            for item in relations_in:
                if not isinstance(item, dict):
                    raise entities.BrainEntityError("each relation must be an object")
                rel = entities.canonical_relation(item.get("rel")) or str(item.get("rel") or "")
                dst_id = str(item.get("to_id") or "")
                if dst_id:
                    target = entities.get_entity(dst_id)
                    if not target or (target.get("owner") or "") != owner:
                        raise entities.BrainEntityError("relation target not found")
                elif str(item.get("to") or "").strip():
                    target = entities.upsert_entity(owner, item["to"], type=str(item.get("to_type") or "other"))
                    dst_id = target["id"]
                if dst_id and dst_id == entity["id"]:
                    continue
                evidence = [str(r) for r in (item.get("evidence") or mentions_in) if str(r or "").strip()]
                relations_out.append(entities.add_relation(
                    owner, entity["id"], rel, dst_id=dst_id or None, dst_value=str(item.get("value") or ""),
                    valid_from=item.get("valid_from") or None, evidence=evidence,
                    confidence=0.9, method="rule"))
                if dst_id:
                    for ref in evidence:
                        entities.add_mention(owner, dst_id, ref)
                    touched.append(dst_id)
            _mirror_entities_later(owner, touched)
            fresh = entities.get_entity(entity["id"]) or entity
            return {"entity": _enriched_entity(fresh), "created": created, "relations": relations_out,
                    "path": _note_path_for_source(owner, f"ent:{entity['id']}")}

        try:
            return await asyncio.to_thread(_apply)
        except ValueError as exc:  # BrainEntityError included: a bad name, relation or date
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/entities/{entity_id}")
    async def get_entity(entity_id: str, request: Request, as_of: str = "",
                         _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import entities

        owner = _owner(request)

        def _load() -> Dict[str, Any]:
            profile = entities.profile(entity_id, as_of=(as_of or None))
            entity = profile.get("entity") or {}
            if entity.get("owner") and owner and entity["owner"] != owner:
                raise entities.BrainEntityError("entity not found")
            profile = dict(profile)
            profile["entity"] = _enriched_entity(entity)
            profile["path"] = _note_path_for_source(owner, f"ent:{entity_id}")
            return profile

        try:
            return await asyncio.to_thread(_load)
        except entities.BrainEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.patch("/entities/{entity_id}")
    async def patch_entity(entity_id: str, request: Request,
                           _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import entities, vault

        owner = _owner(request)
        payload = await _json_body(request)
        existing = entities.get_entity(entity_id)
        if not existing or (existing.get("owner") and owner and existing["owner"] != owner):
            raise HTTPException(status_code=404, detail="No such entity")
        fields = {k: payload[k] for k in ("name", "type", "aliases", "summary", "hidden")
                 if k in payload}
        try:
            updated = await asyncio.to_thread(entities.update_entity, entity_id, **fields)
        except entities.BrainEntityError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if updated is None:
            raise HTTPException(status_code=404, detail="No such entity")
        with contextlib.suppress(Exception):
            await asyncio.to_thread(vault._export_single, owner, f"ent:{entity_id}")
        return _enriched_entity(updated)

    @router.post("/entities/merge")
    async def post_entities_merge(request: Request,
                                  _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import entities, vault

        owner = _owner(request)
        payload = await _json_body(request)
        keep = str(payload.get("keep") or "")
        merge = str(payload.get("merge") or "")
        keep_entity = entities.get_entity(keep)
        merge_entity = entities.get_entity(merge)
        if not keep_entity or not merge_entity or \
           (keep_entity.get("owner") and owner and keep_entity["owner"] != owner) or \
           (merge_entity.get("owner") and owner and merge_entity["owner"] != owner):
            raise HTTPException(status_code=404, detail="No such entity")
        try:
            merged = await asyncio.to_thread(entities.merge_entities, keep, merge)
        except entities.BrainEntityError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        with contextlib.suppress(Exception):
            await asyncio.to_thread(vault._export_single, owner, f"ent:{keep}")
        return _enriched_entity(merged)

    # ── timeline ─────────────────────────────────────────────────────────

    @router.get("/timeline")
    async def get_timeline(request: Request, entity: str = "", q: str = "",
                           limit: int = 200, _u: str = Depends(require_user)) -> Dict[str, Any]:
        owner = _owner(request)
        cap = _limit(limit, 200, 1000)
        if entity:
            from src.brain import entities

            try:
                profile = await asyncio.to_thread(entities.profile, entity)
            except entities.BrainEntityError:
                raise HTTPException(status_code=404, detail="No such entity")
            if profile.get("entity", {}).get("owner") and owner and \
               profile["entity"]["owner"] != owner:
                raise HTTPException(status_code=404, detail="No such entity")
            events = list(profile.get("timeline") or [])[-cap:][::-1]
            return {"events": events}
        from src.brain import temporal

        events = await asyncio.to_thread(temporal.timeline, owner, query=q, limit=cap)
        return {"events": events}

    # ── background passes, triggered by hand ───────────────────────────

    @router.post("/extract")
    async def post_extract(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import extract

        payload: Dict[str, Any] = {}
        with contextlib.suppress(Exception):
            payload = await request.json() or {}
        limit = payload.get("limit") if isinstance(payload, dict) else None
        owner = _owner(request)
        with contextlib.suppress(Exception):
            from src.brain import entities
            await asyncio.to_thread(entities.revalidate_if_needed, owner)
        report = await extract.extract_pending(
            owner, limit=(int(limit) if limit else None))
        return _extract_report(report)

    @router.post("/wiki/refresh")
    async def post_wiki_refresh(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.brain import entities, wiki

        owner = _owner(request)
        payload: Dict[str, Any] = {}
        with contextlib.suppress(Exception):
            payload = await request.json() or {}
        entity_id = str(payload.get("entity_id") or "") if isinstance(payload, dict) else ""
        if entity_id:
            existing = entities.get_entity(entity_id)
            if not existing or (existing.get("owner") and owner and existing["owner"] != owner):
                raise HTTPException(status_code=404, detail="No such entity")
            result = await wiki.refresh_entity(entity_id)
            return _wiki_refresh_report(str(result.get("status") or "error"))
        report = await wiki.refresh_stale(owner)
        return _wiki_stale_report(report)

    # ── settings ─────────────────────────────────────────────────────────

    @router.get("/settings")
    async def get_settings(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        return _settings_snapshot()

    @router.put("/settings")
    async def put_settings(request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src.settings import SettingsError, update_settings

        payload = await _json_body(request)
        patch = {k: v for k, v in payload.items() if k in _SETTINGS_KEYS}
        if not patch:
            return _settings_snapshot()
        try:
            update_settings(patch)
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _settings_snapshot()

    return router


__all__ = ["setup_brain_routes"]
