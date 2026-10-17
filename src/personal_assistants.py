"""Persistent personal assistants point at ordinary, owned Faustus chats.

Profiles own instructions and selected skills. Messages, tools, model, project,
running turns, voice and scheduled work keep their canonical owners.
"""
from pathlib import Path
import json

from src.durable_feature_store import DurableFeatureStore


def store():
    from src.constants import DATA_DIR
    return DurableFeatureStore(str(Path(DATA_DIR) / "personal_assistants.sqlite3"))


def session_for_owner(session_id, owner):
    from core.database import SessionLocal, Session
    from src.auth_helpers import owner_filter
    with SessionLocal() as db:
        row = owner_filter(db.query(Session), Session, owner).filter(Session.id == session_id).first()
        if row is None:
            raise KeyError(session_id)
        return {"id": row.id, "name": row.name, "project_id": row.project_id or "", "archived": bool(row.archived)}


def save(session_id, name, description, owner, *, instructions=None, skills=None, proactivity=None):
    from src.assistant_runtime import validate_config, sync_task, pause
    session = session_for_owner(session_id, owner)
    current = store().get("assistant", session_id, owner=owner) or {}
    instructions = current.get("instructions", "") if instructions is None else instructions
    skills = current.get("skills", []) if skills is None else skills
    config = validate_config(instructions, skills, proactivity)
    name = name.strip()
    if not name:
        raise ValueError("An assistant needs a name.")
    # The session ID is also the assistant ID: adopting a chat twice updates
    # the same assistant rather than forking its history or starting a turn.
    result = store().put("assistant", {"id": session_id, "name": name, "description": description.strip(),
                         "instructions": instructions.strip(), "skills": list(dict.fromkeys(skills)),
                         "quiet": config["quiet"] if config is not None else current.get("quiet", True)},
                         owner=owner, session_id=session_id, project_id=session["project_id"], status="active")
    if current.get("status") == "archived":
        pause(session_id, owner)
    sync_task(session_id, owner, name, config)
    return with_activity(result, owner)


def retire(session_id, owner):
    from src.assistant_runtime import pause
    current = store().get("assistant", session_id, owner=owner)
    if current is None:
        raise KeyError(session_id)
    pause(session_id, owner)
    return store().put("assistant", current, owner=owner, session_id=session_id,
                       project_id=current["project_id"], status="archived")


def with_activity(row, owner):
    from src.assistant_runtime import proactive_state
    p = proactive_state(row["session_id"], owner)
    p["quiet"] = row.get("quiet", True)
    return {**row, "instructions": row.get("instructions", ""), "skills": row.get("skills", []), "proactivity": p}


def list_assistants(owner):
    from core.database import SessionLocal, ChatMessage
    from src import agent_runs
    active = set(agent_runs.active_session_ids())
    rows = store().list("assistant", owner=owner, status="active", limit=1000)
    result = []
    with SessionLocal() as db:
        for row in rows:
            row = with_activity(row, owner)
            sid = row["session_id"]
            try:
                session = session_for_owner(sid, owner)
            except KeyError:
                # A deleted chat is explicitly unavailable; never silently
                # replace it with a fresh conversation.
                result.append({**row, "state": "unavailable", "preview": "", "project_id": ""})
                continue
            last = db.query(ChatMessage).filter(ChatMessage.session_id == sid, ChatMessage.role.in_(["user", "assistant"])).order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc()).first()
            preview = ""
            if last:
                try:
                    content = json.loads(last.content)
                except (ValueError, TypeError):
                    content = last.content
                if isinstance(content, str):
                    preview = content[:400]
                elif isinstance(content, list):
                    preview = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")[:400]
            result.append({**row, "project_id": session["project_id"], "preview": preview,
                           "state": "running" if sid in active or any(r["status"] == "running" for r in row["proactivity"]["runs"]) else "archived" if session["archived"] else "ready",
                           "run_id": agent_runs.get_run_id(sid) if sid in active else None})
    return result
