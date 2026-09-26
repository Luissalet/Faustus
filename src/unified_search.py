"""Unified search across every kind of thing the owner owns.

Ctrl+K only searches conversations today (``src.session_search`` /
``GET /api/search``). That leaves brain notes, quick notes, documents,
gallery photos, skills and board issues each behind their own search box,
so finding "that thing about X" means guessing which screen it lives on
first. This module fans one query out to every source's *existing* internal
search/list function (never an HTTP round trip), runs them concurrently
with a per-source time budget so one slow or broken source can't stall the
rest, and mixes the results into a single ranked list.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCES: Tuple[str, ...] = (
    "chats", "brain", "notes", "documents", "gallery", "skills", "board",
)

_DEFAULT_TIME_BUDGET = 3.0
_BOARD_PROJECT_CAP = 5


# ---------------------------------------------------------------------------
# Shaping helpers
# ---------------------------------------------------------------------------

def _snippet(text: str, limit: int = 240) -> str:
    """Whitespace-collapsed, hard-capped preview text."""
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def _result(*, source: str, id_: Any, title: str, snippet: str, url: str,
            when: Optional[str]) -> Dict[str, Any]:
    return {
        "type": source,
        "id": str(id_ if id_ is not None else ""),
        "title": title or "",
        "snippet": _snippet(snippet),
        "url": url,
        "when": when,
        "score": 0.0,  # filled in by `_mix`
    }


# ---------------------------------------------------------------------------
# Per-source adapters — each calls straight into the existing internal
# function its own route uses, owner-scoped exactly the same way.
# ---------------------------------------------------------------------------

def _chats(owner: Optional[str], q: str, limit: int) -> List[Dict[str, Any]]:
    """`routes/chat_routes.py` GET /api/search -> `src.session_search`."""
    from src.session_search import search_session_messages

    hits = search_session_messages(
        q, limit=limit, owner=owner, restrict_owner=owner is not None,
        include_legacy_owner=False,
    )
    out: List[Dict[str, Any]] = []
    for hit in hits:
        row = hit.to_dict()
        session_id = str(row.get("session_id") or "")
        message_id = str(row.get("message_id") or "")
        url = f"/studio?s={urllib.parse.quote(session_id, safe='')}"
        if message_id:
            url += f"&m={urllib.parse.quote(message_id, safe='')}"
        out.append(_result(
            source="chats", id_=message_id,
            title=row.get("session_name") or "Untitled",
            snippet=row.get("content_snippet") or "",
            url=url, when=row.get("timestamp"),
        ))
    return out


def _clean_brain_snippet(text: str) -> str:
    """The brain's snippets carry its own markup: `[match]` highlights and
    `%% faustus:generated ... %%` sync comments. A search row wants text."""
    text = re.sub(r"%%.*?(?:%%|$)", " ", text or "", flags=re.S)
    return re.sub(r"\[([^\[\]]{1,120})\]", r"\1", text)


def _brain(owner: Optional[str], q: str, limit: int) -> List[Dict[str, Any]]:
    """`routes/brain_routes.py` GET /api/brain/search -> `src.brain.notes.search`."""
    from src.brain import notes

    rows = notes.search(owner or "", q, limit=limit)
    out: List[Dict[str, Any]] = []
    for row in rows:
        path = str(row.get("path") or "")
        title = row.get("title") or path or "Note"
        url = f"/brain?note={urllib.parse.quote(path, safe='')}"
        out.append(_result(
            source="brain", id_=path, title=title,
            snippet=_clean_brain_snippet(row.get("snippet") or ""), url=url, when=None,
        ))
    return out


def _notes(owner: Optional[str], q: str, limit: int) -> List[Dict[str, Any]]:
    """No server search exists for quick notes: load the owner's notes the
    way `GET /api/notes` does (`routes/note/note_routes.py::list_notes`) and
    filter title/body case-insensitively in Python."""
    from core.database import Note, SessionLocal

    needle = (q or "").casefold()
    db = SessionLocal()
    try:
        query = db.query(Note).filter(Note.archived == False)  # noqa: E712
        if owner is not None:
            query = query.filter(Note.owner == owner)
        rows = query.order_by(
            Note.pinned.desc(), Note.sort_order.asc(), Note.updated_at.desc()
        ).all()

        out: List[Dict[str, Any]] = []
        for note in rows:
            title = note.title or ""
            item_texts: List[str] = []
            if note.items:
                try:
                    items = json.loads(note.items)
                    if isinstance(items, list):
                        item_texts = [
                            str(it.get("text") or "") for it in items
                            if isinstance(it, dict)
                        ]
                except (TypeError, ValueError):
                    item_texts = []
            haystack = " ".join([title, note.content or "", *item_texts]).casefold()
            if needle and needle not in haystack:
                continue
            snippet = note.content or " ".join(item_texts)
            out.append(_result(
                source="notes", id_=note.id, title=title or "(untitled note)",
                snippet=snippet, url=f"/notes?n={note.id}",
                when=note.updated_at.isoformat() if note.updated_at else None,
            ))
            if len(out) >= limit:
                break
        return out
    finally:
        db.close()


def _documents(owner: Optional[str], q: str, limit: int) -> List[Dict[str, Any]]:
    """`routes/document/document_routes.py` GET /api/documents/library
    (search param), scoped with the same `_owner_session_filter`."""
    from core.database import Document, SessionLocal
    from routes.document.document_helpers import _owner_session_filter

    db = SessionLocal()
    try:
        query = db.query(Document).filter(Document.is_active == True)  # noqa: E712
        query = query.filter(
            (Document.archived == False) | (Document.archived.is_(None))  # noqa: E712
        )
        query = _owner_session_filter(query, owner)
        for tok in q.split():
            term = f"%{tok}%"
            query = query.filter(
                Document.title.ilike(term) | Document.current_content.ilike(term)
            )
        rows = query.order_by(Document.updated_at.desc()).limit(limit).all()
        out: List[Dict[str, Any]] = []
        for doc in rows:
            out.append(_result(
                source="documents", id_=doc.id, title=doc.title or "Untitled document",
                snippet=doc.current_content or "",
                url=f"/studio?doc={doc.id}",
                when=doc.updated_at.isoformat() if doc.updated_at else None,
            ))
        return out
    finally:
        db.close()


def _gallery(owner: Optional[str], q: str, limit: int) -> List[Dict[str, Any]]:
    """`routes/gallery/gallery_routes.py` GET /api/gallery/library (search
    param), scoped with the same `_owner_filter`."""
    from sqlalchemy import or_

    from core.database import GalleryImage, SessionLocal
    from routes.gallery.gallery_helpers import _owner_filter

    db = SessionLocal()
    try:
        query = db.query(GalleryImage).filter(GalleryImage.is_active == True)  # noqa: E712
        query = _owner_filter(query, owner, GalleryImage)
        term = f"%{q}%"
        query = query.filter(or_(
            GalleryImage.prompt.ilike(term),
            GalleryImage.tags.ilike(term),
            GalleryImage.ai_tags.ilike(term),
        ))
        rows = query.order_by(GalleryImage.created_at.desc()).limit(limit).all()
        out: List[Dict[str, Any]] = []
        for img in rows:
            title = img.prompt or img.filename or "Image"
            snippet = img.caption or img.prompt or img.tags or ""
            out.append(_result(
                source="gallery", id_=img.id, title=title, snippet=snippet,
                url=f"/library?type=imagen&img={img.id}",
                when=img.created_at.isoformat() if img.created_at else None,
            ))
        return out
    finally:
        db.close()


def _skills(owner: Optional[str], q: str, limit: int) -> List[Dict[str, Any]]:
    """`routes/skills_routes.py` POST /api/skills/search ->
    `skills_manager.get_relevant_skills`."""
    from src.constants import DATA_DIR
    from services.memory.skills import SkillsManager

    manager = SkillsManager(DATA_DIR)
    skills = manager.load(owner=owner)
    matches = manager.get_relevant_skills(q, skills, max_items=limit)
    out: List[Dict[str, Any]] = []
    for sk in matches:
        name = str(sk.get("name") or "")
        out.append(_result(
            source="skills", id_=name, title=name or "Skill",
            snippet=sk.get("description") or "",
            url=f"/skills?skill={urllib.parse.quote(name, safe='')}",
            when=sk.get("last_used"),
        ))
    return out


def _board(owner: Optional[str], q: str, limit: int) -> List[Dict[str, Any]]:
    """List the owner's projects (`services.projects.get_store`, capped to a
    few) and filter each one's issues with the same `q` filter
    `GET /api/projects/{project_id}/board/issues` uses
    (`src.project_board.list_issues`)."""
    from services.projects import get_store
    from src import project_board

    projects = get_store().list(owner=owner)[:_BOARD_PROJECT_CAP]
    out: List[Dict[str, Any]] = []
    for project in projects:
        project_id = str(project.get("id") or "")
        if not project_id:
            continue
        try:
            issues, _next_cursor = project_board.list_issues(project_id, q=q, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one bad project must not sink the rest
            logger.debug("unified_search: board issues failed for %s: %s", project_id, exc)
            continue
        for issue in issues:
            issue_id = str(issue.get("id") or "")
            title = issue.get("title") or issue_id
            out.append(_result(
                source="board", id_=issue_id, title=title, snippet=title,
                url=f"/projects/{project_id}?tab=board&issue={issue_id}",
                when=issue.get("updated_at"),
            ))
            if len(out) >= limit:
                return out[:limit]
    return out[:limit]


_SOURCE_FUNCS = {
    "chats": _chats,
    "brain": _brain,
    "notes": _notes,
    "documents": _documents,
    "gallery": _gallery,
    "skills": _skills,
    "board": _board,
}


# ---------------------------------------------------------------------------
# Ranking — reciprocal rank fusion with a title-match boost
# ---------------------------------------------------------------------------

def _mix(per_source: Dict[str, List[Dict[str, Any]]], query: str) -> List[Dict[str, Any]]:
    needle = query.casefold()
    scored: List[Dict[str, Any]] = []
    for rows in per_source.values():
        for rank, row in enumerate(rows):
            score = 1.0 / (rank + 1)
            if needle and needle in (row.get("title") or "").casefold():
                score += 0.5
            row = dict(row)
            row["score"] = round(score, 6)
            scored.append(row)
    # `list.sort` is stable, so equal scores keep their (source, rank)
    # insertion order rather than being reshuffled.
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def search(
    owner: Optional[str],
    q: str,
    types: Optional[Iterable[str]] = None,
    limit: int = 8,
    *,
    time_budget: float = _DEFAULT_TIME_BUDGET,
) -> Dict[str, Any]:
    """Search every requested source concurrently and return one ranked list.

    ``types`` unknown to :data:`SOURCES` are silently dropped; ``None``
    means every source. An empty/whitespace query returns immediately
    without calling any source.
    """
    started = time.monotonic()
    query = (q or "").strip()

    if types is None:
        wanted: List[str] = list(SOURCES)
    else:
        wanted = [t for t in dict.fromkeys(types) if t in _SOURCE_FUNCS]

    counts: Dict[str, int] = {}
    errors: Dict[str, str] = {}

    if not query or not wanted:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "query": q, "results": [], "counts": counts, "errors": errors,
            "elapsed_ms": elapsed_ms,
        }

    cap = max(1, int(limit or 8))
    per_source: Dict[str, List[Dict[str, Any]]] = {}

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(wanted))
    try:
        futures = {
            pool.submit(_SOURCE_FUNCS[name], owner, query, cap): name
            for name in wanted
        }
        for future, name in futures.items():
            try:
                rows = future.result(timeout=time_budget)
            except concurrent.futures.TimeoutError:
                errors[name] = f"timed out after {time_budget:g}s"
                counts[name] = 0
                continue
            except Exception as exc:  # noqa: BLE001 - one bad source must not sink the rest
                logger.warning("unified_search: source %r failed: %s", name, exc)
                errors[name] = str(exc)[:200] or exc.__class__.__name__
                counts[name] = 0
                continue
            rows = list(rows)[:cap]
            per_source[name] = rows
            counts[name] = len(rows)
    finally:
        # Don't block on a source whose thread is still running past its own
        # timeout — a genuinely slow source stays slow in the background
        # (best-effort cleanup by the interpreter's own atexit join) rather
        # than stalling every future call that shares this thread pool.
        pool.shutdown(wait=False)

    results = _mix(per_source, query)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "query": q,
        "results": results,
        "counts": counts,
        "errors": errors,
        "elapsed_ms": elapsed_ms,
    }


__all__ = ["SOURCES", "search"]
