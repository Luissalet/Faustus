"""Shared copilot facade; domain apps own their documents and tools."""
import os
import uuid
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.middleware import require_admin
from core.models import ChatMessage
from services import hoard_copilot as copilot
from src.auth_helpers import storage_owner_for_request


def setup_hoard_copilot_routes(session_manager):
    router = APIRouter(prefix="/api/hoard-copilot")

    def owner(request):
        require_admin(request)
        return storage_owner_for_request(request)

    def session_for(sid, who):
        from core.database import Session as DbSession, SessionLocal
        with SessionLocal() as db:
            row = db.query(DbSession).filter(DbSession.id == sid).first()
            if row is None or (row.owner and who and row.owner != who):
                raise HTTPException(404, "Conversation not found")
        return session_manager.get_session(sid)

    def create(context, who, name=""):
        resolved = copilot.get_store().resolve_project(context, who)
        model = copilot.global_model(who)
        project = context.get("project") or {}
        session = session_manager.create_session(
            session_id=str(uuid.uuid4()), name=name or project.get("title") or context["origin"]["app_id"],
            endpoint_url=model["endpoint_url"], model=model["model"], owner=who,
            project_id=resolved["project_id"], mode="agent",
        )
        copilot.get_store().attach(session.id, context, who, resolved["project_link_id"])
        return {"session_id": session.id, "project_id": resolved["project_id"],
                "project_link_id": resolved["project_link_id"], "context": context,
                "model": session.model, "name": session.name}

    def failure(exc):
        return HTTPException(exc.status, {"code": exc.code, "message": str(exc)})

    async def payload(request):
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "The request must contain valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "The request must be a JSON object")
        return body

    @router.get("/capabilities")
    def capabilities(request: Request):
        owner(request)
        return {"contract_version": "1.0", "panel_tag": "faustus-chat", "panel_path": "/api/hoard-copilot/panel.js",
                "turn_path": "/api/chat_stream", "resume_path": "/api/chat/resume/{session_id}",
                "stop_path": "/api/chat/stop/{session_id}", "history_authority": "faustus",
                "context_field": "hoard_context", "supports_import": True}

    @router.get('/projects/search')
    def project_search(request: Request, query: str = '', app_id: str = '', data_space_id: str = '', after: str = '', before: str = '', offset: int = 0, limit: int = 20):
        from services.hoard_project_recovery import HoardProjectRecovery
        try:
            return HoardProjectRecovery().search(owner(request), query, app_id, data_space_id, after, before, offset, limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/projects/{project_link_id}')
    def project_recall(request: Request, project_link_id: str):
        from services.hoard_project_recovery import HoardProjectRecovery
        try:
            return HoardProjectRecovery().recall(owner(request), project_link_id)
        except copilot.CopilotError as exc:
            raise failure(exc) from exc

    @router.get("/panel.js")
    def panel(request: Request):
        owner(request)
        root = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(root, "studio", "embed", "dist", "faustus-embed.js")
        if not os.path.isfile(path):
            raise HTTPException(503, "Build @faustus/embed before loading the panel")
        return FileResponse(path, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @router.get("/apps/{app_id}/icon")
    async def app_icon(request: Request, app_id: str):
        owner(request)
        import asyncio
        import urllib.request
        from urllib.parse import quote
        from fastapi.responses import Response
        from src.hoard_hub import hub_location
        location = hub_location()
        def read():
            headers = {}
            token_file = location.get("token_file")
            if token_file and os.path.isfile(token_file):
                from pathlib import Path
                headers["Authorization"] = "Bearer " + Path(token_file).read_text(encoding="utf-8").strip()
            query = urllib.request.Request(location["url"].rstrip("/") + "/api/apps/" + quote(app_id, safe="") + "/icon", headers=headers)
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(query, timeout=3) as response:
                return response.read(), response.headers.get("Content-Type", "image/png")
        try:
            data, content_type = await asyncio.to_thread(read)
        except OSError:
            raise HTTPException(404, "Hoard icon unavailable")
        return Response(data, media_type=content_type, headers={"Cache-Control": "no-cache"})

    @router.post("/project-links/resolve")
    async def resolve(request: Request):
        who = owner(request)
        body = await payload(request)
        try:
            return copilot.get_store().resolve_project(body.get("context", body), who)
        except copilot.CopilotError as exc:
            raise failure(exc)

    @router.post("/conversations")
    async def conversations_create(request: Request):
        who = owner(request)
        body = await payload(request)
        try:
            return create(copilot.normalize_context(body.get("context")), who, body.get("name") or "")
        except copilot.CopilotError as exc:
            raise failure(exc)

    @router.get("/conversations")
    def conversations_list(request: Request, app_id: str = "", data_space_id: str = "", external_project_id: str = "",
                           participant_app_id: str = ""):
        who = owner(request)
        result = []
        store = copilot.get_store()
        for item in store.list(who, app_id, data_space_id, external_project_id, participant_app_id):
            try:
                session = session_for(item["session_id"], who)
            except (HTTPException, KeyError):
                continue
            result.append({**item, "name": session.name, "project_id": session.project_id,
                           "archived": session.archived, "message_count": len(session.history),
                           "participants": store.participants(item["session_id"])})
        return {"conversations": result}

    @router.get("/conversations/{sid}")
    def conversation_get(request: Request, sid: str):
        who = owner(request)
        session = session_for(sid, who)
        item = copilot.get_store().get(sid, who)
        from src import agent_runs
        return {"session_id": sid, "name": session.name, "project_id": session.project_id,
                "context": (item or {}).get("context"), "origin": (item or {}).get("origin"),
                "messages": [message.to_dict() for message in session.history],
                "participants": copilot.get_store().participants(sid),
                "run_id": agent_runs.get_run_id(sid) if agent_runs.get_active_run(sid) else None}

    @router.post("/imports")
    async def import_conversation(request: Request):
        who = owner(request)
        body = await payload(request)
        try:
            context = copilot.normalize_context(body.get("context"))
            source_thread_id = str(body.get("source_thread_id") or "")
            messages = body.get("messages")
            if not source_thread_id or not isinstance(messages, list):
                raise copilot.CopilotError("invalid_import", "Thread identity and messages are required")
            if any(not isinstance(m, dict) or not m.get("source_id") or m.get("role") not in ("user", "assistant", "tool", "system") for m in messages):
                raise copilot.CopilotError("invalid_import", "Each imported message needs source_id and a valid role")
            if any(not isinstance(m.get("content", ""), str) or (m.get("metadata") is not None and not isinstance(m["metadata"], dict)) for m in messages):
                raise copilot.CopilotError("invalid_import", "Message content must be text and metadata must be an object")
            store = copilot.get_store()
            with store.lock:
                sid = store.imported_session(who, context, source_thread_id)
                if sid:
                    from core.database import Session as DbSession, SessionLocal
                    with SessionLocal() as db:
                        if db.query(DbSession).filter(DbSession.id == sid).first() is None:
                            # The import mapping is also the tombstone: a host
                            # reopening its legacy history must not undo Delete.
                            return {"session_id": None, "deleted": True, "imported": 0, "message_count": 0}
                if not sid:
                    created = create(context, who, body.get("name") or "")
                    sid = created["session_id"]
                    store.map_import(who, context, source_thread_id, sid)
                session = session_for(sid, who)
                from core.database import ChatMessage as DbMessage, Session as DbSession, SessionLocal
                added = 0
                with SessionLocal() as db:
                    rows = db.query(DbMessage).filter(DbMessage.session_id == sid).all()
                    known = {(json.loads(m.meta_data) if m.meta_data else {}).get("hoard_source_id") for m in rows}
                    timestamps = []
                    for raw in messages:
                        if raw["source_id"] in known:
                            continue
                        metadata = {**(raw.get("metadata") or {}), "hoard_source_id": raw["source_id"],
                                    "hoard_source_thread_id": source_thread_id, "source": "hoard_import"}
                        for key in ("created_at", "tool_calls", "tool_call_id", "attachments"):
                            if key in raw:
                                metadata[key] = raw[key]
                        supplied_time = raw.get("created_at")
                        try:
                            if isinstance(supplied_time, (int, float)):
                                timestamp = datetime.fromtimestamp(supplied_time / 1000 if supplied_time > 100000000000 else supplied_time, timezone.utc)
                            elif supplied_time:
                                timestamp = datetime.fromisoformat(str(supplied_time).replace("Z", "+00:00"))
                            else:
                                timestamp = datetime.now(timezone.utc)
                            timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
                        except (ValueError, OverflowError, OSError) as exc:
                            raise copilot.CopilotError("invalid_import", "Imported message date is invalid") from exc
                        metadata["timestamp"] = timestamp.isoformat()
                        db.add(DbMessage(id=str(uuid.uuid4()), session_id=sid, role=raw["role"],
                                         content=str(raw.get("content") or ""), meta_data=json.dumps(metadata, ensure_ascii=False),
                                         timestamp=timestamp.replace(tzinfo=None)))
                        known.add(raw["source_id"])
                        timestamps.append(timestamp)
                        added += 1
                    row = db.query(DbSession).filter(DbSession.id == sid).first()
                    row.message_count = len(rows) + added
                    if timestamps:
                        row.last_message_at = max(timestamps)
                    db.commit()
                session_manager.sessions.pop(sid, None)
                session = session_manager.get_session(sid)
                return {"session_id": sid, "imported": added, "message_count": len(session.history)}
        except copilot.CopilotError as exc:
            raise failure(exc)

    return router
