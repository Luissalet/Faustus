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

import asyncio
import contextlib
import logging
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
