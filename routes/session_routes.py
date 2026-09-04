# routes/session_routes.py
import io
import os
import re
import json
import uuid
import zipfile
from datetime import datetime
from urllib.parse import quote as _urlquote
from fastapi import APIRouter, Form, HTTPException, Response, Request
import logging

from core.session_manager import SessionManager
from core.models import ChatMessage
from src.request_models import SessionResponse
from core.database import Session as DbSession, SessionLocal, Document, GalleryImage, utcnow_naive
from src.auth_helpers import effective_user, _auth_disabled, owner_filter
from src.session_image_cleanup import _generated_image_path_for_cleanup, session_image_refs
from src.session_actions import is_session_recently_active
from src.upload_handler import reserve_message_upload_references


def _sanitize_export_filename(name: str) -> str:
    """Return a conservative filename safe for Content-Disposition."""
    name = name if isinstance(name, str) else ""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:128]


def _export_download_name(name: str) -> str:
    """Strip everything that could break out of the header or the filesystem
    while KEEPING non-ASCII characters intact.

    The ASCII-only ``_sanitize_export_filename`` is still what guards the
    user-supplied ``?filename=`` parameter. This one guards the name the
    renderer *derived* (usually from the chat title), which legitimately
    contains accents, spaces and CJK; those survive here and are carried by
    the RFC 5987 ``filename*`` parameter instead of being mangled to "_".
    """
    name = name if isinstance(name, str) else ""
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)       # CR/LF header injection
    name = re.sub(r'[\\/"]', "_", name)               # path + quote escapes
    name = name.strip().lstrip(".")
    return name[:180] or "export"


def _content_disposition(name: str) -> str:
    """An ``attachment`` header both halves of the world can read.

    Old code emitted a bare, unquoted, unencoded ``filename=`` — a chat named
    "Informe 2026" produced ``filename=Informe 2026.md``, whose unquoted space
    truncates the name at "Informe" in every browser, and a non-Latin-1 byte
    made Starlette raise outright. RFC 6266 says: quote the ASCII fallback and
    add ``filename*=UTF-8''<pct-encoded>`` for the real name.
    """
    safe = _export_download_name(name)
    ascii_fallback = _sanitize_export_filename(safe) or "export"
    return (
        'attachment; filename="%s"; filename*=UTF-8\'\'%s'
        % (ascii_fallback, _urlquote(safe, safe=""))
    )


def _env_int(key: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(key, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Batch-export ceilings. A 500-chat PDF export is a process-killer: every
# renderer output is held in RAM at once while the zip is built, so both the
# count and the accumulated payload are capped and refused with a 400 rather
# than being allowed to OOM the server.
EXPORT_BATCH_MAX_SESSIONS = _env_int("EXPORT_BATCH_MAX_SESSIONS", 100)
EXPORT_BATCH_MAX_BYTES = _env_int("EXPORT_BATCH_MAX_BYTES", 200 * 1024 * 1024)


def _unique_zip_name(name: str, taken: set) -> str:
    """Two chats can share a title, and a zip entry can't. Suffix -2, -3, …
    before the extension so 'Notes.pdf' and 'Notes.pdf' become
    'Notes.pdf' and 'Notes-2.pdf' rather than one silently overwriting the
    other (zipfile happily writes duplicate members; readers keep the last)."""
    candidate = _export_download_name(name)
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    stem, dot, ext = candidate.rpartition(".")
    if not dot:
        stem, ext = candidate, ""
    n = 2
    while True:
        alt = f"{stem}-{n}{dot}{ext}" if dot else f"{stem}-{n}"
        if alt not in taken:
            taken.add(alt)
            return alt
        n += 1


def _md_cell(value) -> str:
    """Escape a value for a Markdown table cell."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|").strip() or "—"


def _build_export_index(entries, failures, fmt: str) -> str:
    """The zip's index.md: what is inside, and what failed to render."""
    lines = [
        "# Exported conversations",
        "",
        f"*{len(entries)} conversation(s) exported as `{fmt}` on "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.*",
        "",
        "| # | Conversation | Model | Last activity | Messages | File |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, e in enumerate(entries, 1):
        lines.append(
            "| {n} | {name} | {model} | {date} | {count} | `{file}` |".format(
                n=i,
                name=_md_cell(e.get("name")),
                model=_md_cell(e.get("model")),
                date=_md_cell(e.get("date")),
                count=_md_cell(e.get("message_count")),
                file=_md_cell(e.get("filename")),
            )
        )
    if failures:
        lines += [
            "",
            "## Could not be exported",
            "",
            f"{len(failures)} conversation(s) failed to render as `{fmt}`. The "
            "zip carries a `.txt` with the error in place of each one, so the "
            "rest of the batch still made it out.",
            "",
        ]
        for f in failures:
            lines.append(
                "- **{name}** → `{file}`: {err}".format(
                    name=_md_cell(f.get("name")),
                    file=_md_cell(f.get("filename")),
                    err=_md_cell(f.get("error")),
                )
            )
    lines.append("")
    return "\n".join(lines)


# Blind-compare helper sessions are created with this name prefix. Their real
# model must never surface in the session list / sidebar — otherwise a blind
# comparison can be de-anonymized before the user votes (issue #1285).
COMPARE_SESSION_PREFIX = "[CMP] "


def _public_model(name: str, model: str) -> str:
    """Blank out the real model of blind-compare helper sessions so the
    session list can't be used to map a neutral pane label ("Model A") back
    to its model. The Compare UI tracks models client-side, so hiding it here
    costs the sidebar nothing. See issue #1285."""
    if (name or "").startswith(COMPARE_SESSION_PREFIX):
        return ""
    return model


def _content_to_text(content) -> str:
    """Flatten a message's content to plain text for text-based exports.

    History entries carry three shapes: a plain string, a multimodal list of
    content blocks (vision/image attachments), or None (assistant turns that
    persisted only native tool_calls). The txt/html/md exporters join and
    string-munge this value, so a list crashed the export (TypeError on join,
    AttributeError on .replace) and None rendered as the literal "None".
    Coerce to the text blocks, returning "" for anything without text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("text")
        )
    return ""


def _message_role(message) -> str:
    if isinstance(message, ChatMessage):
        return message.role or ""
    if isinstance(message, dict):
        return message.get("role", "") or ""
    return getattr(message, "role", "") or ""


def _message_text(message) -> str:
    if isinstance(message, ChatMessage):
        content = message.content
    elif isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    return _content_to_text(content)


def _message_metadata(message) -> dict:
    if isinstance(message, ChatMessage):
        metadata = message.metadata
    elif isinstance(message, dict):
        metadata = message.get("metadata")
    else:
        metadata = getattr(message, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _reject_compact_during_active_run(session_id: str) -> None:
    from src import agent_runs
    if agent_runs.is_active(session_id):
        raise HTTPException(409, "Session has an active run; try compacting after it finishes")


def _stop_runs_for_deleted_sessions(session_ids=None) -> int:
    """Stop the agent runs of sessions that are about to be deleted.

    A deleted chat used to leave its run going: it kept executing tools and
    writing files, it held its queue-lane slot (with
    `agent_queue_local_concurrency=1` that blocks every other chat) and it was
    unstoppable from the UI, because /api/chat/activity and /api/chat/stop 404
    once the session no longer exists.

    `session_ids=None` means "every run there is" — the delete-all route.
    Best effort: a chat must still be deletable if this fails.
    """
    try:
        from src import agent_runs
    except Exception as exc:                       # pragma: no cover - import guard
        logger.warning("Could not import agent_runs to stop deleted sessions: %s", exc)
        return 0
    ids = list(agent_runs.active_session_ids()) if session_ids is None else list(session_ids)
    stopped = 0
    for sid in ids:
        try:
            if agent_runs.stop_for_session(sid, reason="session_deleted"):
                stopped += 1
        except Exception as exc:
            logger.warning("Could not stop the run of deleted session %s: %s", sid, exc)
    return stopped


def _verify_session_owner(request: Request, session_id: str, session_manager=None):
    """Verify the current user owns the session, honoring single-user modes.

    Authenticated requests must match the stored DB or in-memory owner. When
    auth is disabled and no user is present, treat the app as single-user mode:
    verify that the session exists, but do not compare its stored owner. This
    keeps QA/dev instances with AUTH_ENABLED=false from rejecting owner-stamped
    rows created while auth was previously enabled.
    """
    user = effective_user(request)
    if not user and not _auth_disabled():
        raise HTTPException(401, "Authentication required")
    db = SessionLocal()
    try:
        row = db.query(DbSession.owner).filter(DbSession.id == session_id).first()
    finally:
        db.close()
    if row is not None:
        if user and row.owner != user:
            raise HTTPException(404, f"Session {session_id} not found")
        return
    # No DB row — allow the caller to act on an in-memory ghost they own.
    if session_manager is not None:
        ghost = getattr(session_manager, "sessions", {}).get(session_id)
        if ghost is not None and (not user or getattr(ghost, "owner", None) == user):
            return
    raise HTTPException(404, f"Session {session_id} not found")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["sessions"])

def _current_user_is_admin(request: Request, user: str | None) -> bool:
    if not user:
        return False
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    is_admin = getattr(auth_mgr, "is_admin", None)
    if not callable(is_admin):
        return False
    try:
        return bool(is_admin(user))
    except Exception:
        return False


def _reject_raw_endpoint_url_for_non_admin(
    request: Request,
    user: str | None,
    endpoint_id: str | None,
    endpoint_url: str | None,
) -> None:
    """Require registered endpoints for signed-in non-admin session changes."""
    if endpoint_id and endpoint_id.strip():
        return
    if not endpoint_url:
        return
    # Raw URLs make the server dial whatever host the request supplies. For
    # non-admin users, require a saved endpoint row so normal owner scoping and
    # endpoint validation have already happened.
    if user and not _current_user_is_admin(request, user):
        raise HTTPException(403, "Choose a registered model endpoint")


def _persist_session_headers(session_id: str, headers: dict | None) -> None:
    """Persist endpoint auth headers for DB-backed session metadata."""
    db = SessionLocal()
    try:
        db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
        if db_session:
            db_session.headers = headers or {}
            db_session.updated_at = utcnow_naive()
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


_HIDDEN_SYSTEM_SESSION_NAMES = {
    "[Task] Chat Sessions Tidy",
    "[Task] Documents Tidy",
    "[Task] Memory Tidy",
    "[Task] Research Tidy",
    "[Task] Email Mark Boundaries",
    "[Task] Email Tags",
    "[Task] Skills Audit",
}


def _pick_endpoint_for_sort(owner=None):
    """Pick model endpoint for auto-sort LLM call — uses utility endpoint setting, falls back to default."""
    from src.endpoint_resolver import resolve_endpoint
    # Try utility endpoint first (what the user configured for background tasks)
    url, model, headers = resolve_endpoint("utility", owner=owner)
    if url and model:
        return url, model, headers
    # Fall back to task endpoint
    try:
        from src.task_endpoint import resolve_task_endpoint
        url, model, headers = resolve_task_endpoint(owner=owner)
        if url and model:
            return url, model, headers
    except Exception:
        pass
    # Fall back to default
    url, model, headers = resolve_endpoint("default", owner=owner)
    if url and model:
        return url, model, headers
    return None, None, None

def setup_session_routes(
    session_manager: SessionManager,
    config: dict,
    webhook_manager=None,
    upload_handler=None,
):
    """Setup session routes with the provided manager and config"""

    REQUEST_TIMEOUT = config.get("REQUEST_TIMEOUT", 20)
    SESSION_MODEL_VALIDATION_TIMEOUT = min(float(REQUEST_TIMEOUT or 20), 3.0)
    OPENAI_API_KEY = config.get("OPENAI_API_KEY")
    SESSIONS_FILE = config.get("SESSIONS_FILE")
    
    @router.get("/sessions")
    def list_sessions(request: Request):
        user = effective_user(request)
        active_incognito_id = str(request.query_params.get("active_incognito_id") or "").strip()
        # Lazy purge: incognito sessions are ephemeral by design — wipe leftovers
        # from the DB and session_manager so they vanish on the next page refresh.
        # BUT: skip sessions that were created within the last 10 minutes.
        # Without that guard, the purge nukes the active "Nobody" session on the
        # very first /api/sessions call after creation, killing the in-flight
        # chat. The frontend's own _cleanupIncognitoSessions handler knows which
        # session is current and won't delete the live one — this server-side
        # purge exists only to catch ghosts the frontend missed (tab close,
        # crash). Only clean up rows old enough to be definitely orphaned.
        try:
            from datetime import timedelta as _td
            _cutoff = utcnow_naive() - _td(minutes=10)
            _purge_db = SessionLocal()
            try:
                from core.database import ChatMessage as _DbMsg
                _ghosts = _purge_db.query(DbSession).filter(
                    DbSession.name.in_(("Nobody", "Incognito")),
                    DbSession.created_at < _cutoff,
                ).all()
                for _g in _ghosts:
                    if active_incognito_id and _g.id == active_incognito_id:
                        continue
                    _purge_db.query(_DbMsg).filter(_DbMsg.session_id == _g.id).delete()
                    _purge_db.delete(_g)
                    if hasattr(session_manager, "delete_session"):
                        try:
                            session_manager.delete_session(_g.id)
                        except Exception:
                            pass
                if _ghosts:
                    _purge_db.commit()
            finally:
                _purge_db.close()
        except Exception:
            pass
        user_sessions = session_manager.get_sessions_for_user(user)
        # Fetch folder info from DB for each session
        db = SessionLocal()
        try:
            folder_map = {}
            token_map = {}
            important_map = {}
            created_map = {}
            updated_map = {}
            last_msg_map = {}
            mode_map = {}
            msg_count_map = {}
            q = db.query(DbSession.id, DbSession.folder, DbSession.total_input_tokens, DbSession.total_output_tokens, DbSession.is_important, DbSession.created_at, DbSession.updated_at, DbSession.last_message_at, DbSession.mode, DbSession.message_count).filter(DbSession.archived == False)
            q = owner_filter(q, DbSession, user)
            rows = q.all()
            for row in rows:
                folder_map[row.id] = row.folder
                token_map[row.id] = (row.total_input_tokens or 0) + (row.total_output_tokens or 0)
                important_map[row.id] = row.is_important or False
                created_map[row.id] = row.created_at.isoformat() if row.created_at else None
                updated_map[row.id] = row.updated_at.isoformat() if row.updated_at else None
                # Fall back to updated_at then created_at so sessions that
                # predate the column (or have no messages) still sort sanely.
                last_msg_map[row.id] = (
                    row.last_message_at.isoformat() if row.last_message_at
                    else (row.updated_at.isoformat() if row.updated_at
                          else (row.created_at.isoformat() if row.created_at else None))
                )
                mode_map[row.id] = row.mode
                msg_count_map[row.id] = row.message_count or 0
            # Sessions with active documents that have content
            from sqlalchemy import func
            doc_session_ids = set(
                r[0] for r in owner_filter(
                    db.query(Document.session_id)
                    .filter(Document.is_active == True,
                            Document.current_content != None,
                            func.trim(Document.current_content) != ""),
                    Document, user)
                .distinct().all()
            )
            img_session_ids = set(
                r[0] for r in owner_filter(
                    db.query(GalleryImage.session_id)
                    .filter(GalleryImage.session_id != None),
                    GalleryImage, user)
                .distinct().all()
            )
        finally:
            db.close()

        sessions = [{"id": s.id, "name": s.name, "model": _public_model(s.name, s.model),
                     "endpoint_url": s.endpoint_url, "rag": s.rag,
                     "archived": s.archived, "folder": folder_map.get(s.id),
                     "total_tokens": token_map.get(s.id, 0),
                     "is_important": important_map.get(s.id, False),
                     "created_at": created_map.get(s.id),
                     "updated_at": updated_map.get(s.id),
                     "last_message_at": last_msg_map.get(s.id),
                     "has_documents": s.id in doc_session_ids,
                     "has_images": s.id in img_session_ids,
                     "mode": mode_map.get(s.id),
                     "message_count": msg_count_map.get(s.id, 0)}
                    for s in user_sessions.values()
                    if not s.archived
                    and (s.name or "").strip() not in ("Nobody", "Incognito")
                    and (s.name or "").strip() not in _HIDDEN_SYSTEM_SESSION_NAMES]

        return sessions
    
    @router.post("/session", response_model=SessionResponse)
    def create_session(
        request: Request,
        name: str = Form(""),
        endpoint_url: str = Form(""),
        model: str = Form(""),
        rag: str = Form(None),
        skip_validation: str = Form(None),
        api_key: str = Form(""),
        endpoint_id: str = Form(""),
    ):
        skip_val = str(skip_validation).lower() == "true"
        user = effective_user(request)
        endpoint_api_key = ""
        endpoint_base_url = ""
        _reject_raw_endpoint_url_for_non_admin(request, user, endpoint_id, endpoint_url)
        if endpoint_id and endpoint_id.strip():
            from core.database import ModelEndpoint
            from src.auth_helpers import owner_filter
            from src.endpoint_resolver import build_chat_url, normalize_base
            _db = SessionLocal()
            try:
                q = _db.query(ModelEndpoint).filter(
                    ModelEndpoint.id == endpoint_id.strip(),
                    ModelEndpoint.is_enabled == True,
                )
                if user:
                    q = owner_filter(q, ModelEndpoint, user)
                endpoint_row = q.first()
                if not endpoint_row:
                    raise HTTPException(400, "Model endpoint no longer exists")
                endpoint_base_url = endpoint_row.base_url or ""
                endpoint_api_key = endpoint_row.api_key or ""
                endpoint_url = build_chat_url(normalize_base(endpoint_base_url))
            finally:
                _db.close()

        if not endpoint_url and not skip_val:
            raise HTTPException(400, "endpoint_url is required (choose from /api/models)")

        model_to_use = model
        request_api_key = api_key.strip() if api_key else ""
        effective_api_key = request_api_key or endpoint_api_key
        validation_headers = None
        if effective_api_key:
            from src.endpoint_resolver import build_headers
            validation_headers = build_headers(effective_api_key, endpoint_base_url or endpoint_url)

        if skip_val:
            # skip_validation = trust the caller and do NOT probe /v1/models.
            # Used for custom endpoints AND for bare placeholder sessions with no
            # model at all (e.g. an email reply draft just needs a session to live
            # in). Probing here was 400-ing those with "Cannot reach /v1/models".
            pass
        elif not model_to_use:
            from src.llm_core import list_model_ids
            ids = list_model_ids(
                endpoint_url,
                timeout=SESSION_MODEL_VALIDATION_TIMEOUT,
                headers=validation_headers,
                owner=user,
                endpoint_id=endpoint_id.strip() if endpoint_id else None,
            )
            if not ids:
                raise HTTPException(400, "Cannot reach /v1/models")
            # Default to the first CHAT model — endpoints often list embedding/
            # tts/whisper models first (e.g. text-embedding-ada-002), which
            # can't hold a conversation.
            _NON_CHAT = ("text-embedding", "embedding", "tts-", "whisper",
                         "text-moderation", "moderation-", "dall-e", "rerank")
            chat_ids = [m for m in ids if not any(p in m.lower() for p in _NON_CHAT)]
            model_to_use = (chat_ids or ids)[0]
        else:
            from src.llm_core import list_model_ids
            import os as _os
            req_base = _os.path.basename(model_to_use.rstrip("/"))
            avail = list_model_ids(
                endpoint_url,
                timeout=SESSION_MODEL_VALIDATION_TIMEOUT,
                headers=validation_headers,
                owner=user,
                endpoint_id=endpoint_id.strip() if endpoint_id else None,
            )
            if not avail:
                raise HTTPException(400, "Cannot reach /v1/models")
            if model_to_use not in avail:
                found = None
                for a in avail:
                    if _os.path.basename(a.rstrip("/")) == req_base:
                        found = a
                        break
                if not found:
                    raise HTTPException(400,
                                        f"Model not found at server. Available: {', '.join(avail)}")
                model_to_use = found
        
        sid = str(uuid.uuid4())
        user = effective_user(request)
        session = session_manager.create_session(
            session_id=sid,
            name=name or "",
            endpoint_url=endpoint_url or "",
            model=model_to_use,
            rag=str(rag).lower() == "true" if rag else False,
            owner=user,
        )
        # Set auth headers for custom API-key endpoints
        resolved_key = request_api_key
        resolved_base = endpoint_url
        if not resolved_key and endpoint_api_key:
            resolved_key = endpoint_api_key
            resolved_base = endpoint_base_url
        if resolved_key:
            from src.endpoint_resolver import build_headers
            session.headers = build_headers(resolved_key, resolved_base)
            _persist_session_headers(sid, session.headers)
        # Fire webhook (sync-safe)
        if webhook_manager:
            webhook_manager.fire_and_forget("session.created", {
                "session_id": sid, "name": session.name, "model": model_to_use,
            })
        # Fire event for automation tasks
        from src.event_bus import fire_event
        fire_event("session_created", user)
        return SessionResponse(
            id=sid,
            name=session.name,
            model=model_to_use,
            rag=str(rag).lower() == "true" if rag else False,
            archived=False
        )    
    @router.patch("/session/{sid}")
    def rename_session(
        request: Request, sid: str,
        name: str = Form(None), folder: str = Form(None),
        model: str = Form(None), endpoint_url: str = Form(None),
        endpoint_id: str = Form(None),
    ):
        _verify_session_owner(request, sid)
        try:
            session = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        result = {"id": sid}
        if name is not None:
            session_manager.update_session_name(sid, name)
            result["name"] = name
        # Update folder assignment
        if folder is not None:
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == sid).first()
                if db_session:
                    db_session.folder = folder if folder else None
                    db_session.updated_at = utcnow_naive()
                    db.commit()
                    result["folder"] = folder if folder else None
            finally:
                db.close()
        # Switch model/endpoint mid-session
        if model is not None and endpoint_url is not None:
            user = effective_user(request)
            _reject_raw_endpoint_url_for_non_admin(request, user, endpoint_id, endpoint_url)
            endpoint_api_key = ""
            endpoint_base_url = ""
            if endpoint_id:
                from core.database import ModelEndpoint
                from src.auth_helpers import owner_filter
                from src.endpoint_resolver import build_chat_url, normalize_base
                _db = SessionLocal()
                try:
                    q = _db.query(ModelEndpoint).filter(
                        ModelEndpoint.id == endpoint_id,
                        ModelEndpoint.is_enabled == True,
                    )
                    if user:
                        q = owner_filter(q, ModelEndpoint, user)
                    ep = q.first()
                    if not ep:
                        raise HTTPException(400, "Model endpoint no longer exists")
                    endpoint_base_url = ep.base_url or ""
                    endpoint_api_key = ep.api_key or ""
                    endpoint_url = build_chat_url(normalize_base(endpoint_base_url))
                finally:
                    _db.close()
            session.model = model
            session.endpoint_url = endpoint_url
            # Update auth headers from the endpoint's stored API key
            if endpoint_api_key:
                from src.endpoint_resolver import build_headers
                session.headers = build_headers(endpoint_api_key, endpoint_base_url)
            else:
                session.headers = {}
            # Persist to DB
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == sid).first()
                if db_session:
                    db_session.model = model
                    db_session.endpoint_url = endpoint_url
                    db_session.headers = session.headers or {}
                    db_session.updated_at = utcnow_naive()
                    db.commit()
            finally:
                db.close()
            result["model"] = model
            result["endpoint_url"] = endpoint_url
        return result
    
    @router.post("/session/{sid}/inject_messages")
    async def inject_messages(request: Request, sid: str):
        """Bulk-inject messages into a session's history (for group chat sync)."""
        _verify_session_owner(request, sid)
        try:
            sess = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        body = await request.json()
        messages = body.get("messages", [])
        from core.models import ChatMessage
        owner = effective_user(request)
        try:
            for message in messages:
                missing_id = reserve_message_upload_references(
                    upload_handler,
                    owner,
                    message.get("content"),
                    message.get("metadata"),
                )
                if missing_id:
                    raise HTTPException(
                        409,
                        f"Referenced upload is no longer available: {missing_id}",
                    )
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(400, "Invalid message attachment metadata") from exc
        for m in messages:
            sess.add_message(ChatMessage(m["role"], m["content"], metadata=m.get("metadata")))
        session_manager.save_sessions()
        return {"ok": True, "count": len(messages)}

    @router.post("/session/{sid}/delete")
    def delete_session_beacon(request: Request, sid: str):
        """Delete session via POST (for navigator.sendBeacon on page close)."""
        return delete_session(request, sid)

    @router.post("/sessions/bulk-delete")
    async def bulk_delete_sessions(request: Request):
        """Delete multiple sessions (for compare cleanup via sendBeacon)."""
        from core.database import ChatMessage as _CM
        try:
            body = await request.json()
            ids = body.get("ids", [])
        except Exception:
            ids = []
        deleted_count = 0
        for sid in ids:
            try:
                _verify_session_owner(request, sid, session_manager)
                
                # Enforce "starred" protection consistent with single-session delete
                db = SessionLocal()
                try:
                    db_sess = db.query(DbSession).filter(DbSession.id == sid).first()
                    if db_sess and db_sess.is_important:
                        continue
                finally:
                    db.close()

                # The chat is going away: its run must not keep running.
                _stop_runs_for_deleted_sessions([sid])
                if session_manager.delete_session(sid):
                    deleted_count += 1
            except Exception:
                pass
        return {"deleted": deleted_count}

    @router.delete("/session/{sid}")
    def delete_session(request: Request, sid: str):
        """Permanently delete a session and all its messages."""
        _verify_session_owner(request, sid, session_manager)
        try:
            # Block deletion of starred/favorited sessions
            db = SessionLocal()
            try:
                db_sess = db.query(DbSession).filter(DbSession.id == sid).first()
                if db_sess and db_sess.is_important:
                    raise HTTPException(
                        status_code=403,
                        detail={"error": "SESSION_STARRED", "message": "Unstar the session before deleting it"}
                    )
            finally:
                db.close()

            # Stop the run FIRST: once the session row is gone the run is
            # unreachable from the UI but still executing tools and holding
            # its queue lane.
            _stop_runs_for_deleted_sessions([sid])

            # Delete the session and all its messages
            if session_manager.delete_session(sid):
                return {"status": "deleted"}
            else:
                raise HTTPException(404, "Session not found")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting session {sid}: {e}")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "SESSION_DELETE_ERROR",
                    "message": "Failed to delete session"
                }
            )
    
    @router.delete("/sessions/all")
    def delete_all_sessions(request: Request):
        """Admin only: permanently delete ALL sessions and their messages."""
        from core.middleware import require_admin
        require_admin(request)

        # Every chat is going away — stop every run first (None = all of them,
        # which also covers in-memory-only sessions and sub-agent workers).
        _stop_runs_for_deleted_sessions()

        db = SessionLocal()
        try:
            from core.database import ChatMessage as DbChatMessage
            session_ids = [row[0] for row in db.query(DbSession.id).all()]
            count = db.query(DbSession).count()
            image_ids: set[str] = set()
            filenames: set[str] = set()
            for sid in session_ids:
                ids, names = session_image_refs(db, sid)
                image_ids.update(ids)
                filenames.update(names)
            image_query = db.query(GalleryImage).filter(GalleryImage.session_id.in_(session_ids)) if session_ids else db.query(GalleryImage).filter(False)
            if image_ids or filenames:
                from sqlalchemy import or_
                clauses = []
                if session_ids:
                    clauses.append(GalleryImage.session_id.in_(session_ids))
                if image_ids:
                    clauses.append(GalleryImage.id.in_(list(image_ids)))
                if filenames:
                    clauses.append(GalleryImage.filename.in_(list(filenames)))
                image_query = db.query(GalleryImage).filter(or_(*clauses))
            images = image_query.all()
            removed_images = 0
            for img in images:
                img.is_active = False
                if img.filename:
                    path = _generated_image_path_for_cleanup(img.filename)
                    if path and path.exists():
                        try:
                            path.unlink()
                        except Exception as exc:
                            logger.warning("Could not remove generated image %s during all-session delete: %s", img.filename, exc)
                removed_images += 1
            db.query(DbChatMessage).delete()
            db.query(DbSession).delete()
            db.commit()
            session_manager.sessions.clear()
            logger.info(f"Admin deleted all {count} sessions and {removed_images} linked images")
            return {"status": "deleted", "count": count, "images_deleted": removed_images}
        except Exception as e:
            db.rollback()
            logger.error(f"Error deleting all sessions: {e}")
            raise HTTPException(500, "Failed to delete sessions")
        finally:
            db.close()

    @router.post("/session/{sid}/archive")
    def archive_session(request: Request, sid: str):
        """Archive a session, keeping its data but removing it from active sessions."""
        _verify_session_owner(request, sid)
        try:
            # First check if session exists
            session_manager.get_session(sid)
            
            # Archive the session
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == sid).first()
                if db_session:
                    db_session.archived = True
                    db_session.updated_at = utcnow_naive()
                    db.commit()
                    
                    # Update in memory if it exists
                    if sid in session_manager.sessions:
                        session_manager.sessions[sid].archived = True
                        
                    logger.info(f"Archived session {sid}")
                    return {"status": "archived"}
                else:
                    raise HTTPException(404, f"Session {sid} not found")
                    
            except HTTPException:
                raise
            except Exception as e:
                db.rollback()
                logger.error(f"Error archiving session {sid}: {e}")
                raise HTTPException(500, "Failed to archive session")
            finally:
                db.close()

        except KeyError:
            raise HTTPException(404, f"Session '{sid}' not found")
    
    @router.post("/session/{sid}/unarchive")
    def unarchive_session(request: Request, sid: str):
        """Restore an archived session back to the active session list."""
        _verify_session_owner(request, sid)
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == sid).first()
            if not db_session:
                raise HTTPException(404, f"Session {sid} not found")
            db_session.archived = False
            db_session.updated_at = utcnow_naive()
            db.commit()
            # Reload into session manager so it appears in the active list
            try:
                if sid in session_manager.sessions:
                    session_manager.sessions[sid].archived = False
                else:
                    session_manager._load_session_from_db(sid)
            except Exception:
                pass  # Non-fatal — session will load on next access
            return {"status": "unarchived"}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Error unarchiving session {sid}: {e}")
            raise HTTPException(500, "Failed to unarchive session")
        finally:
            db.close()

    @router.get("/sessions/archived")
    def list_archived_sessions(request: Request, search: str = "", offset: int = 0, limit: int = 20, sort: str = "recent", model: str = ""):
        """List archived sessions for the archive browser."""
        user = effective_user(request)
        db = SessionLocal()
        try:
            q = db.query(DbSession).filter(DbSession.archived == True)
            if not user and not _auth_disabled():
                raise HTTPException(403, "Authentication required")
            if user:
                q = q.filter(DbSession.owner == user)
            if search:
                safe_search = search.replace('%', r'\%').replace('_', r'\_')
                q = q.filter(DbSession.name.ilike(f"%{safe_search}%", escape='\\'))
            if model:
                # Contains match (mirrors the name filter above). The old
                # f"%{model}" was a SUFFIX-only match, so filtering by "gpt-4"
                # dropped "gpt-4o" and over-matched on shared suffixes; it also
                # left LIKE wildcards in the user value unescaped.
                safe_model = model.replace('%', r'\%').replace('_', r'\_')
                q = q.filter(DbSession.model.ilike(f"%{safe_model}%", escape='\\'))
            total = q.count()
            sort_map = {
                "recent": DbSession.updated_at.desc(),
                "oldest": DbSession.updated_at.asc(),
                "most-messages": DbSession.message_count.desc().nulls_last(),
                "alpha": DbSession.name.asc(),
            }
            order = sort_map.get(sort, DbSession.updated_at.desc())
            rows = q.order_by(order).offset(offset).limit(limit).all()
            sessions = []
            for s in rows:
                sessions.append({
                    "id": s.id,
                    "name": s.name,
                    "model": s.model,
                    "message_count": s.message_count or 0,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    "is_important": s.is_important,
                })
            return {"sessions": sessions, "total": total}
        finally:
            db.close()

    def _export_module():
        """Import the exporter lazily.

        Renderers pull in optional heavy deps (reportlab, python-docx); doing
        this at module import would make every session route depend on them.
        """
        from src import chat_export
        return chat_export

    def _resolve_fmt(fmt: str, chat_export):
        """Normalise and validate ?fmt=. An unknown format is a 400 naming the
        valid ones — the old route silently fell through to markdown, so
        `?fmt=pdf` (before PDF existed) handed back a .md file pretending to
        be what was asked for."""
        candidate = (fmt or "").strip().lower().lstrip(".")
        if not candidate:
            candidate = "md"
        if candidate not in chat_export.SUPPORTED_FORMATS:
            raise HTTPException(
                400,
                "Unsupported export format '%s'. Valid formats: %s"
                % (candidate, ", ".join(chat_export.SUPPORTED_FORMATS)),
            )
        return candidate

    def _render_one(chat_export, session, fmt: str, filename: str = ""):
        """build_transcript + render, with ExportUnavailable left to the caller."""
        transcript = chat_export.build_transcript(session)
        return chat_export.render(transcript, fmt, filename=filename)

    @router.get("/session/{sid}/export")
    def export_session(request: Request, sid: str, fmt: str = "md", filename: str = ""):
        """Export one conversation as a downloadable file.

        Formats: md, txt, json, html, pdf, docx — all rendered by
        ``src/chat_export.py`` from the shared block model, so every format
        sees the same transcript (tool calls and attachments included) instead
        of the four hand-rolled string builders that used to live here.
        """
        _verify_session_owner(request, sid)
        try:
            session = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")

        chat_export = _export_module()
        fmt = _resolve_fmt(fmt, chat_export)
        # The caller-supplied name stays ASCII-sanitised (it lands in a header
        # and, on the client, on a filesystem). The name the renderer derives
        # from the chat title does not — see _content_disposition.
        requested = _sanitize_export_filename(filename)

        try:
            result = _render_one(chat_export, session, fmt, requested)
        except chat_export.ExportUnavailable as e:
            # A missing optional dependency is not a server fault: 503 with the
            # message verbatim, which names the package to install.
            raise HTTPException(503, str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Export of session %s as %s failed", sid, fmt)
            raise HTTPException(500, f"Could not export this conversation as {fmt}: {e}")

        out_name = result.filename or requested or f"conversation_{sid}.{fmt}"
        return Response(
            content=result.content,
            media_type=result.media_type or "application/octet-stream",
            headers={"Content-Disposition": _content_disposition(out_name)},
        )

    @router.get("/sessions/export")
    def export_sessions_batch(
        request: Request,
        fmt: str = "md",
        project: str = "",
        folder: str = "",
        ids: str = "",
        filename: str = "",
    ):
        """Export a whole project / folder / id-list as one .zip.

        The zip carries one file per conversation plus an `index.md` listing
        them. Membership is the same rule the project routes use (a chat is in
        a project when its `folder` column equals the project's folder), and
        the owner scope is the same `owner_filter` the session list uses — no
        second implementation of either.
        """
        user = effective_user(request)
        if not user and not _auth_disabled():
            raise HTTPException(401, "Authentication required")

        chat_export = _export_module()
        fmt = _resolve_fmt(fmt, chat_export)

        id_list = [p.strip() for p in (ids or "").split(",") if p.strip()]
        project_id = (project or "").strip()
        folder_name = (folder or "").strip()
        if not (project_id or folder_name or id_list):
            raise HTTPException(
                400, "Nothing selected: pass project=<id>, folder=<name> or ids=a,b,c"
            )
        # Cap the id list before it becomes an IN (...) of unbounded width; the
        # row cap below would catch it anyway, but not before the query ran.
        if len(id_list) > EXPORT_BATCH_MAX_SESSIONS:
            raise HTTPException(
                400,
                "Too many conversations selected (%d); the cap is %d per export."
                % (len(id_list), EXPORT_BATCH_MAX_SESSIONS),
            )

        scope_label = "chats"
        if project_id:
            from services.projects import get_store
            row = get_store().get(project_id, user)
            if not row:
                raise HTTPException(404, f"Project {project_id} not found")
            # Same membership rule as DELETE /api/projects/{id}/sessions.
            folder_name = row.get("folder") or ""
            if not folder_name:
                # Without a folder there is no membership rule to apply, and
                # falling through would silently export every chat the user has.
                raise HTTPException(
                    400, f"Project {project_id} has no folder, so it groups no chats"
                )
            scope_label = row.get("name") or folder_name
        elif folder_name:
            scope_label = folder_name

        db = SessionLocal()
        try:
            q = db.query(
                DbSession.id, DbSession.name, DbSession.model, DbSession.folder,
                DbSession.message_count, DbSession.updated_at, DbSession.created_at,
                DbSession.last_message_at,
            ).filter(DbSession.archived == False)  # noqa: E712
            q = owner_filter(q, DbSession, user)
            if folder_name:
                q = q.filter(DbSession.folder == folder_name)
            if id_list:
                q = q.filter(DbSession.id.in_(id_list))
            rows = q.order_by(DbSession.updated_at.desc()).all()
        finally:
            db.close()

        if not rows:
            raise HTTPException(404, "No conversations matched this selection")
        if len(rows) > EXPORT_BATCH_MAX_SESSIONS:
            raise HTTPException(
                400,
                "This selection has %d conversations; a single export is capped at "
                "%d. Narrow the selection (or raise EXPORT_BATCH_MAX_SESSIONS)."
                % (len(rows), EXPORT_BATCH_MAX_SESSIONS),
            )

        entries, failures = [], []
        taken = set()
        total_bytes = 0
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for row in rows:
                title = row.name or row.id
                when = row.last_message_at or row.updated_at or row.created_at
                error = ""
                try:
                    session = session_manager.get_session(row.id)
                    result = _render_one(chat_export, session, fmt)
                    payload = result.content
                    member = _unique_zip_name(result.filename or f"{title}.{fmt}", taken)
                except chat_export.ExportUnavailable as e:
                    # Global, not per-chat: reportlab/python-docx is missing, so
                    # every remaining conversation would fail the same way. Fail
                    # the whole batch with the installable message instead of
                    # zipping N identical error notes.
                    raise HTTPException(503, str(e))
                except Exception as e:  # noqa: BLE001 - one bad chat must not sink the batch
                    logger.warning("Batch export: session %s failed as %s: %s", row.id, fmt, e)
                    error = f"{type(e).__name__}: {e}"
                    member = _unique_zip_name(f"{title}.error.txt", taken)
                    payload = (
                        f"{title}\n\nThis conversation could not be exported as "
                        f"{fmt}.\n\n{error}\n"
                    ).encode("utf-8")

                total_bytes += len(payload)
                if total_bytes > EXPORT_BATCH_MAX_BYTES:
                    raise HTTPException(
                        400,
                        "This export exceeds the %d MB limit for a single batch. "
                        "Export fewer conversations, or pick a lighter format."
                        % (EXPORT_BATCH_MAX_BYTES // (1024 * 1024)),
                    )
                zf.writestr(member, payload)
                if error:
                    failures.append({"name": title, "filename": member, "error": error})
                else:
                    entries.append({
                        "name": title,
                        "model": _public_model(row.name, row.model),
                        "date": when.strftime("%Y-%m-%d %H:%M") if when else "",
                        "message_count": row.message_count or 0,
                        "filename": member,
                    })
            zf.writestr("index.md", _build_export_index(entries, failures, fmt))

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = (
            _sanitize_export_filename(filename)
            or _export_download_name(f"{scope_label}_{fmt}_{stamp}.zip")
        )
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": _content_disposition(out_name)},
        )
    
    @router.post("/sessions/save")
    def sessions_save_now(request: Request):
        user = effective_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        session_manager.save_sessions()
        return {"ok": True, "path": SESSIONS_FILE}
    
    @router.post("/session/openai")
    def create_session_openai(
        request: Request,
        name: str = Form("New Chat (OpenAI)"),
        model: str = Form("gpt-4o"),
        rag: str = Form(None)
    ):
        if not OPENAI_API_KEY:
            raise HTTPException(400, "Server missing OPENAI_API_KEY")
        sid = str(uuid.uuid4())
        user = effective_user(request)
        session = session_manager.create_session(
            session_id=sid,
            name="",
            endpoint_url="https://api.openai.com/v1/chat/completions",
            model=model,
            rag=str(rag).lower() == "true",
            owner=user,
        )
        session.headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        session_manager.save_sessions()
        from src.event_bus import fire_event
        fire_event("session_created", user)
        return {"id": sid, "name": "", "model": model}
    
    @router.post("/session/{session_id}/important")
    async def mark_session_important(request: Request, session_id: str, important: bool = Form(True)):
        """Mark a session as important to protect it from automatic cleanup."""
        _verify_session_owner(request, session_id)
        try:
            # Validate session exists
            session_manager.get_session(session_id)

            # Update in database
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                if db_session:
                    db_session.is_important = important
                    db_session.updated_at = utcnow_naive()
                    db.commit()

                    # Update in memory if it exists
                    if session_id in session_manager.sessions:
                        session_manager.sessions[session_id].is_important = important

                    return {"status": "success", "is_important": important}
                else:
                    raise HTTPException(404, f"Session {session_id} not found")

            except HTTPException:
                raise
            except Exception as e:
                db.rollback()
                logger.error(f"Error updating session {session_id} importance: {e}")
                raise HTTPException(500, "Failed to update session importance")
            finally:
                db.close()

        except KeyError:
            raise HTTPException(404, f"Session {session_id} not found")

    @router.post("/session/{session_id}/compact")
    async def compact_session(request: Request, session_id: str):
        """Summarize older messages into one compacted history entry."""
        _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"Session {session_id} not found")
        _reject_compact_during_active_run(session_id)

        history = list(session.history or [])
        if len(history) < 6:
            raise HTTPException(400, "Not enough messages to compact")

        # Keep a small recent tail verbatim. The prior half-chat/20-message
        # tail made manual compaction look like it did nothing on normal chats.
        recent_keep = min(8, max(4, len(history) // 4))
        older = history[:-recent_keep]
        recent = history[-recent_keep:]
        if not older:
            raise HTTPException(400, "Nothing old enough to compact")

        from src.context_compactor import SELF_SUMMARY_SYSTEM_PROMPT
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async

        owner = getattr(session, "owner", None) or effective_user(request)
        url, model, headers = resolve_endpoint("utility", owner=owner)
        if not url or not model:
            url, model, headers = session.endpoint_url, session.model, session.headers
        if not url or not model:
            raise HTTPException(400, "No model configured for compaction")

        prior_compactions = sum(
            1 for m in history
            if _message_metadata(m).get("compacted") or "[Conversation summary" in _message_text(m)
        )
        prompt = SELF_SUMMARY_SYSTEM_PROMPT.replace(
            "{count}", str(len(older))
        ).replace(
            "{n}", str(prior_compactions + 1)
        )
        convo_text = "\n".join(
            f"{_message_role(m).upper()}: {_message_text(m)[:2000]}"
            for m in older
        )
        try:
            summary = await llm_call_async(
                url,
                model,
                [{"role": "system", "content": prompt}, {"role": "user", "content": convo_text}],
                temperature=0.2,
                max_tokens=1024,
                headers=headers,
                timeout=60,
            )
        except Exception as e:
            logger.error("Manual compaction failed: %s", e)
            raise HTTPException(500, "Compaction failed")

        summary_msg = ChatMessage(
            role="system",
            content=f"[Conversation summary]\n{summary}",
            metadata={
                "compacted": True,
                "summarized_count": len(older),
                "timestamp": utcnow_naive().isoformat(),
            },
        )
        new_history = [summary_msg] + recent
        if not session_manager.replace_messages(session_id, new_history):
            raise HTTPException(500, "Failed to save compacted history")

        return {
            "ok": True,
            "summarized": len(older),
            "kept": len(recent),
            "message_count": len(new_history),
        }

    @router.post("/sessions/auto-sort")
    def auto_sort_sessions(request: Request, skip_llm: bool = False):
        """Use AI to categorize all sessions into folders.

        Phase 1 deletes empty/throwaway sessions and Phase 2 asks the LLM
        to assign folders. When `skip_llm=true` the endpoint returns
        after Phase 1 — used by the "Tidy (no AI)" UI affordance so
        users can clean junk without spending tokens.
        """
        from src.llm_core import llm_call
        user = effective_user(request)
        single_user_mode = not user and _auth_disabled()
        user_sessions = session_manager.get_sessions_for_user(user)

        # Delete empty and throwaway sessions before sorting
        from core.database import ChatMessage as DbMsg
        db = SessionLocal()
        deleted_empty = 0
        deleted_throwaway = 0
        # Names that indicate a throwaway/test session (case-insensitive exact or prefix match)
        _THROWAWAY_NAMES = {
            "test", "testing", "asdf", "asd", "hello", "hi", "hey",
            "yo", "sup", "hola", "hii", "hiii", "heyo",
            "foo", "bar", "baz", "tmp", "temp", "scratch", "untitled",
            "new chat", "delete", "remove", "junk", "trash", "xxx",
            "abc", "qwerty", "blah", "stuff", "whatever", "idk",
            "ok", "lol", "bruh", "hmm", "hm", "meh",
        }
        _THROWAWAY_MAX_MESSAGES = 4  # only delete if <= this many messages
        try:
            rows_q = db.query(DbSession).filter(DbSession.archived == False)
            if user:
                rows_q = rows_q.filter(DbSession.owner == user)
            elif not single_user_mode:
                rows_q = rows_q.filter(DbSession.owner == user)
            rows = rows_q.limit(2000).all()
            folder_map = {r.id: r.folder for r in rows}
            # Precompute per-session message counts in TWO aggregate queries
            # instead of 1–3 queries PER session — with many chats the per-row
            # loop was doing thousands of round-trips and blowing the timeout.
            from sqlalchemy import func as _sa_func
            _counts = dict(db.query(DbMsg.session_id, _sa_func.count(DbMsg.id)).group_by(DbMsg.session_id).all())
            _asst_counts = dict(
                db.query(DbMsg.session_id, _sa_func.count(DbMsg.id))
                .filter(DbMsg.role == "assistant").group_by(DbMsg.session_id).all()
            )
            cleanup_now = utcnow_naive()
            for row in rows:
                # Never delete important sessions
                if getattr(row, 'is_important', False):
                    continue
                # Always delete incognito sessions during cleanup
                if (row.name or "").strip() == "Incognito":
                    should_delete = True
                    deleted_throwaway += 1
                    db.delete(row)
                    if hasattr(session_manager, 'delete_session'):
                        session_manager.delete_session(row.id)
                    continue
                if is_session_recently_active(row, now=cleanup_now):
                    continue
                msg_count = _counts.get(row.id, 0)
                should_delete = False
                if msg_count == 0:
                    should_delete = True
                    deleted_empty += 1
                elif msg_count <= _THROWAWAY_MAX_MESSAGES:
                    name = (row.name or "").strip().lower()
                    # Check first user message content (AI renames sessions, so
                    # "hi" becomes "Casual Greeting Exchange" — name alone won't match)
                    first_msg = db.query(DbMsg.content).filter(
                        DbMsg.session_id == row.id, DbMsg.role == "user"
                    ).order_by(DbMsg.timestamp).first()
                    first_text = (first_msg[0] or "").strip().lower() if first_msg else ""
                    # Count assistant messages — if user sent something but AI never replied, it's dead
                    assistant_count = _asst_counts.get(row.id, 0)
                    if name in _THROWAWAY_NAMES or name.startswith("chat:") or first_text in _THROWAWAY_NAMES:
                        should_delete = True
                        deleted_throwaway += 1
                    # Single user message with no AI response = dead session
                    elif msg_count == 1 and assistant_count == 0:
                        should_delete = True
                        deleted_throwaway += 1
                    # Short phrase (1-3 words) with no real AI conversation (<=2 msgs)
                    elif msg_count <= 2 and first_text and len(first_text.split()) <= 3 and len(first_text) <= 40:
                        should_delete = True
                        deleted_throwaway += 1
                if should_delete:
                    db.delete(row)
                    if hasattr(session_manager, 'delete_session'):
                        session_manager.delete_session(row.id)
            if deleted_empty or deleted_throwaway:
                db.commit()
                logger.info(f"Auto-sort: deleted {deleted_empty} empty + {deleted_throwaway} throwaway sessions")
        finally:
            db.close()

        # Re-fetch after cleanup
        if deleted_empty or deleted_throwaway:
            user_sessions = session_manager.get_sessions_for_user(user)

        # Short-circuit when the caller only wanted the cleanup phase
        # (the "Tidy (no AI)" path). Shape mirrors the post-Phase-1
        # branch below so the frontend can render the same toast.
        if skip_llm:
            return {
                "status": "ok",
                "updated": 0,
                "folders": [],
                "deleted_empty": deleted_empty,
                "deleted_throwaway": deleted_throwaway,
                "unfiled_remaining": 0,
                "skipped_llm": True,
            }

        # Tidy works in batches: only sessions that don't already have a
        # folder, capped at TIDY_BATCH_SIZE (most recent first). Sending
        # all 100+ chats to one LLM call blows the context window, makes
        # the request slow, and re-bills the same tokens every click for
        # already-sorted chats. Skipping sessions with `current_folder`
        # means each Tidy press only handles new unfiled chats.
        TIDY_BATCH_SIZE = 15
        all_candidates = []
        for s in user_sessions.values():
            if s.archived or s.name == "Incognito":
                continue
            if folder_map.get(s.id):
                # Already in a folder — skip on this pass.
                continue
            name = s.name or "(unnamed)"
            all_candidates.append({
                "id": s.id,
                "name": name,
                "updated_at": getattr(s, "updated_at", None) or getattr(s, "created_at", None) or "",
                "current_folder": None,
            })

        # Most-recent first, then take the top N for this batch.
        all_candidates.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        unfiled_total = len(all_candidates)
        session_list = all_candidates[:TIDY_BATCH_SIZE]

        if len(session_list) < 2:
            if deleted_empty or deleted_throwaway:
                return {
                    "status": "ok",
                    "updated": 0,
                    "folders": [],
                    "deleted_empty": deleted_empty,
                    "deleted_throwaway": deleted_throwaway,
                    "unfiled_remaining": unfiled_total,
                }
            return {"status": "skipped", "reason": "No unfiled sessions to sort"}

        # Pick an endpoint — prefer admin-configured task endpoint
        from src.task_endpoint import resolve_task_endpoint
        url, model, headers = resolve_task_endpoint(owner=user)
        if not url:
            url, model, headers = _pick_endpoint_for_sort(owner=user)
        if not url:
            raise HTTPException(503, "No available model endpoint for auto-sort")

        # Build prompt
        names_text = "\n".join(f'  "{s["id"][:8]}": "{s["name"]}"' for s in session_list)
        prompt = (
            "You are a session organizer. Group these chat sessions into folders by topic.\n\n"
            "Rules:\n"
            "- Be aggressive about grouping — put EVERY session in a folder\n"
            "- Use short folder names (2-4 words max)\n"
            "- Use the 8-char ID prefixes exactly as given\n"
            "- Output ONLY raw JSON, no markdown fences, no explanation\n\n"
            "Required JSON format:\n"
            '{"folders": {"Folder Name": ["id_prefix1", "id_prefix2"], "Other Folder": ["id_prefix3"]}}\n\n'
            f"Sessions (id_prefix: name):\n{{\n{names_text}\n}}"
        )

        try:
            logger.info(f"Auto-sort: using model={model} at {url}")
            # 16384 (was 4096): with many chats the folder JSON is large, and a
            # reasoning model spends tokens thinking first — 4096 truncated the
            # JSON mid-output, so it never parsed ("invalid JSON for auto-sort").
            raw = llm_call(url, model, [{"role": "user", "content": prompt}],
                           temperature=0.3, max_tokens=16384, headers=headers, timeout=120)
            logger.info(f"Auto-sort raw response ({len(raw)} chars): {raw[:300]}")
            # Extract JSON from response — handle markdown fences, leading text,
            # reasoning-model <think> blocks, and trailing commas.
            text = raw.strip()
            # Reasoning models emit <think>…</think> (often containing { } that
            # would derail the brace scan) before the answer — drop it first.
            text = re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', text, flags=re.I).strip()

            def _loads_lenient(s):
                """Parse JSON, retrying once with trailing commas stripped."""
                if not s:
                    return None
                for cand in (s, re.sub(r',(\s*[}\]])', r'\1', s)):
                    try:
                        return json.loads(cand)
                    except json.JSONDecodeError:
                        continue
                return None

            result = _loads_lenient(text)
            # Markdown code fence
            if result is None:
                fence_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)```', text)
                if fence_match:
                    result = _loads_lenient(fence_match.group(1).strip())
            # First { … last } block
            if result is None:
                brace_start = text.find('{')
                brace_end = text.rfind('}')
                if brace_start >= 0 and brace_end > brace_start:
                    result = _loads_lenient(text[brace_start:brace_end + 1])
            if result is None:
                logger.error(f"Auto-sort: could not parse JSON from: {text[:500]}")
                raise HTTPException(502, "AI returned invalid JSON for auto-sort — the model may not follow JSON instructions; try a different utility model in Settings.")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Auto-sort LLM call failed: {e}")
            raise HTTPException(502, f"Auto-sort failed: {str(e)}")

        folders = result.get("folders", {})
        if not folders:
            return {"status": "skipped", "reason": "AI found no groupings"}

        # Build id -> folder map
        id_prefix_map = {s["id"][:8]: s["id"] for s in session_list}
        assignments = {}
        for folder_name, ids in folders.items():
            for sid_or_prefix in ids:
                # Match by full ID or prefix
                full_id = None
                if sid_or_prefix in id_prefix_map.values():
                    full_id = sid_or_prefix
                else:
                    # Try prefix match
                    prefix = sid_or_prefix.rstrip(".").rstrip(" ")
                    if prefix in id_prefix_map:
                        full_id = id_prefix_map[prefix]
                    else:
                        # Fuzzy prefix match
                        for p, fid in id_prefix_map.items():
                            if fid.startswith(prefix) or prefix.startswith(p):
                                full_id = fid
                                break
                if full_id:
                    assignments[full_id] = folder_name

        # Apply folder assignments
        updated = 0
        db = SessionLocal()
        try:
            for sid, folder_name in assignments.items():
                db_session_q = db.query(DbSession).filter(DbSession.id == sid)
                if user:
                    db_session_q = db_session_q.filter(DbSession.owner == user)
                elif not single_user_mode:
                    db_session_q = db_session_q.filter(DbSession.owner == user)
                db_session = db_session_q.first()
                if db_session:
                    db_session.folder = folder_name
                    db_session.updated_at = utcnow_naive()
                    updated += 1
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Auto-sort DB update failed: {e}")
            raise HTTPException(500, "Failed to apply folder assignments")
        finally:
            db.close()

        # How many unfiled chats are left after this batch — the
        # frontend uses this to decide whether to show "Tidy more" or
        # "All sorted!" in the toast.
        unfiled_remaining_after = max(0, unfiled_total - updated)
        return {
            "status": "ok",
            "folders": list(folders.keys()),
            "updated": updated,
            "deleted_empty": deleted_empty,
            "deleted_throwaway": deleted_throwaway,
            "unfiled_remaining": unfiled_remaining_after,
        }

    @router.get("/session/{session_id}/context_info")
    async def get_context_info(request: Request, session_id: str):
        """Get the real context length for a session's model from the endpoint."""
        _verify_session_owner(request, session_id)
        session = session_manager.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if not session.endpoint_url or not session.model:
            return {"context_length": None}
        try:
            from src.model_context import get_context_length
            ctx = get_context_length(session.endpoint_url, session.model)
            return {"context_length": ctx, "model": session.model}
        except Exception:
            return {"context_length": None}

    return router
