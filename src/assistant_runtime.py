"""Assistant configuration on the native skill, chat and scheduler stores."""
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_PROACTIVITY = {"enabled": False, "schedule": "daily", "time": "09:00",
                       "day": 0, "timezone": "Europe/Madrid", "cron": "0 9 * * *",
                       "prompt": "", "notifications": True, "quiet": True}
NO_UPDATE = "[ASSISTANT_NO_UPDATE]"


def task_id(session_id, owner):
    return "assistant-" + hashlib.sha256(json.dumps([owner, session_id]).encode()).hexdigest()[:32]


def is_assistant_task(task):
    return bool(getattr(task, "session_id", None)) and getattr(task, "id", None) == task_id(task.session_id, task.owner)


def skill_catalog(owner):
    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR
    # Drafts are not executable procedures. Project skills remain available
    # through the project's normal activation and disclosure mechanism.
    return [s for s in SkillsManager(DATA_DIR).load(owner=owner) if s.get("status") == "published"]


def validate_config(instructions, skills, proactivity):
    if not isinstance(instructions, str) or len(instructions) > 30000:
        raise ValueError("Instructions must be text, up to 30000 characters.")
    if not isinstance(skills, list) or any(not isinstance(s, str) or not s.strip() for s in skills):
        raise ValueError("Skills must be a list of names.")
    if proactivity is None:
        return None
    if not isinstance(proactivity, dict) or set(proactivity) - set(DEFAULT_PROACTIVITY):
        raise ValueError("Unknown proactivity fields.")
    p = {**DEFAULT_PROACTIVITY, **proactivity}
    for key in ("enabled", "notifications", "quiet"):
        if not isinstance(p[key], bool):
            raise ValueError(f"{key} must be true or false.")
    if p["schedule"] not in ("hourly", "daily", "weekly", "cron"):
        raise ValueError("Choose hourly, daily, weekly or cron.")
    if not isinstance(p["time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", p["time"]):
        raise ValueError("Time must use HH:MM.")
    if type(p["day"]) is not int or not 0 <= p["day"] <= 6:
        raise ValueError("Weekday must be between 0 (Monday) and 6 (Sunday).")
    try:
        ZoneInfo(p["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("Choose a valid IANA timezone.") from None
    if not isinstance(p["prompt"], str) or len(p["prompt"]) > 30000:
        raise ValueError("The proactive mission must be text, up to 30000 characters.")
    if p["enabled"] and not p["prompt"].strip():
        raise ValueError("Describe what the assistant should do proactively.")
    if p["schedule"] == "cron":
        from croniter import croniter
        if not isinstance(p["cron"], str) or not croniter.is_valid(p["cron"]) or len(p["cron"].split()) != 5:
            raise ValueError("Use a five-field cron expression.")
    if p["enabled"]:
        from src.task_scheduler import compute_next_run
        schedule = "cron" if p["schedule"] == "hourly" else p["schedule"]
        cron = f"{int(p['time'].split(':')[1])} * * * *" if p["schedule"] == "hourly" else p["cron"]
        if compute_next_run(schedule, p["time"], p["day"], tz_name=p["timezone"], cron_expression=cron) is None:
            raise ValueError("This schedule has no next occurrence.")
    return p


def sync_task(session_id, owner, name, config):
    from core.database import SessionLocal, ScheduledTask
    from src.task_scheduler import compute_next_run
    with SessionLocal() as db:
        row = db.query(ScheduledTask).filter_by(id=task_id(session_id, owner), owner=owner).first()
        if row is None:
            if config is None:
                return
            row = ScheduledTask(id=task_id(session_id, owner), owner=owner, session_id=session_id)
            db.add(row)
        row.name = name
        if config is not None:
            row.prompt = config["prompt"].strip()
            row.schedule = "cron" if config["schedule"] == "hourly" else config["schedule"]
            row.scheduled_time = config["time"]
            row.scheduled_day = config["day"]
            row.timezone = config["timezone"]
            row.cron_expression = (f"{int(config['time'].split(':')[1])} * * * *" if config["schedule"] == "hourly"
                                   else config["cron"] if row.schedule == "cron" else None)
            row.status = "active" if config["enabled"] else "paused"
            row.next_run = compute_next_run(row.schedule, row.scheduled_time, row.scheduled_day,
                                           tz_name=row.timezone, cron_expression=row.cron_expression) if config["enabled"] else None
            row.notifications_enabled = config["notifications"]
        row.task_type = "llm"
        row.trigger_type = "schedule"
        row.output_target = "session"
        row.email_results = False
        db.commit()


def proactive_state(session_id, owner):
    from core.database import SessionLocal, ScheduledTask, TaskRun
    with SessionLocal() as db:
        row = db.query(ScheduledTask).filter_by(id=task_id(session_id, owner), owner=owner).first()
        if row is None:
            return {**DEFAULT_PROACTIVITY, "task_id": None, "runs": []}
        runs = db.query(TaskRun).filter_by(task_id=row.id).order_by(TaskRun.started_at.desc()).limit(5).all()
        def iso(value):
            return value.isoformat() + "Z" if value else None
        return {**DEFAULT_PROACTIVITY, "task_id": row.id, "enabled": row.status == "active",
                "schedule": "hourly" if row.schedule == "cron" and re.fullmatch(r"\d+ \* \* \* \*", row.cron_expression or "") else row.schedule,
                "time": row.scheduled_time, "day": row.scheduled_day,
                "timezone": row.timezone, "cron": row.cron_expression or DEFAULT_PROACTIVITY["cron"],
                "prompt": row.prompt or "", "notifications": bool(row.notifications_enabled),
                "next_run": iso(row.next_run), "last_run": iso(row.last_run),
                "runs": [{"id": r.id, "status": r.status, "started_at": iso(r.started_at),
                          "finished_at": iso(r.finished_at), "result": r.result, "error": r.error} for r in runs]}


def pause(session_id, owner):
    from core.database import SessionLocal, ScheduledTask
    with SessionLocal() as db:
        row = db.query(ScheduledTask).filter_by(id=task_id(session_id, owner), owner=owner).first()
        if row:
            row.status = "paused"
            row.next_run = None
            db.commit()


def context_messages(session_id, owner, *, skills_enabled=True):
    """Read each turn, outside the shared base-prompt cache."""
    from src.personal_assistants import store, session_for_owner
    from src.owner_identity import effective_storage_owner
    owner = effective_storage_owner(owner)
    if not session_id or not owner:
        return []
    profile = store().get("assistant", session_id, owner=owner)
    if not profile or profile.get("status") != "active":
        return []
    session_for_owner(session_id, owner)
    result = []
    instructions = profile.get("instructions", "").strip()
    if instructions:
        # This is the owner's explicit assistant configuration, rather than
        # instructions encountered in a retrieved document or tool response.
        result.append({"role": "system", "content": "Assistant working instructions configured by the user:\n" + instructions})
    selected = profile.get("skills", [])
    if skills_enabled and selected:
        from src.skills_runtime.disclosure import render_level1, message_receipt
        from src.constants import DATA_DIR
        catalog = {s["name"]: s for s in skill_catalog(owner)}
        available = [catalog[name] for name in selected if name in catalog]
        missing = [name for name in selected if name not in catalog]
        # Explicitly selected bodies are all disclosed. Normal model context
        # compaction still applies; relevance thresholds cannot drop a pin.
        disclosure = render_level1(available, budget_tokens=2**31, source_root=str(DATA_DIR) + "/skills")
        text = "Skills selected for this assistant:\n" + disclosure.text
        if missing:
            text += "\nUnavailable skills (do not claim to have used them): " + ", ".join(missing)
        message = {"role": "user", "content": text,
                   "metadata": {"source": "assistant_selected_skills", "trusted": False}}
        message["metadata"]["skill_disclosure"] = message_receipt(message, disclosure)
        result.append(message)
    for message in result:
        message["_agent_injected"] = "context"
    return result


def apply_chat_profile(messages, session_id, owner, *, skills_enabled=True):
    """Plain Chat uses the same profile before candidate context compaction."""
    from routes.prefs_routes import _load_for_user
    from src.owner_identity import effective_storage_owner
    owner = effective_storage_owner(owner)
    enabled = skills_enabled and (_load_for_user(owner) or {}).get("skills_enabled", True)
    context = context_messages(session_id, owner, skills_enabled=enabled)
    if not context:
        return messages
    result = list(messages)
    prefix_end = next((i for i, m in enumerate(result) if m.get("role") != "system"), len(result))
    result[prefix_end:prefix_end] = [m for m in context if m["role"] == "system"]
    user_index = next((i for i in range(len(result) - 1, -1, -1) if result[i].get("role") == "user"), len(result))
    result[user_index:user_index] = [m for m in context if m["role"] != "system"]
    return result


def prepare_turn(task, db):
    """Validate a scheduled wake against the current chat and live profile."""
    from core.database import Session, ChatMessage
    from src.personal_assistants import store
    from src.builtin_actions import TaskNoop, TaskDeferred
    from src import agent_runs
    from src.auth_helpers import owner_filter
    from services.hoard_copilot import global_model
    profile = store().get("assistant", task.session_id, owner=task.owner)
    session = owner_filter(db.query(Session), Session, task.owner).filter(Session.id == task.session_id).first()
    if not profile or profile.get("status") != "active" or not session or session.archived:
        raise TaskNoop("Assistant or chat is unavailable or archived")
    if task.session_id in agent_runs.active_session_ids():
        raise TaskDeferred("Assistant chat has a turn in progress")
    selected = global_model(task.owner)
    if not selected["endpoint_url"] or not selected["model"]:
        raise RuntimeError("The configured global model is unavailable")
    task.endpoint_url, task.model = selected["endpoint_url"], selected["model"]
    task._assistant_headers = selected.get("headers", {})
    task._external_global_event = True  # native global-only routing; no local fallback
    history = []
    for message in db.query(ChatMessage).filter_by(session_id=task.session_id).filter(ChatMessage.role.in_(["user", "assistant"])).order_by(ChatMessage.timestamp, ChatMessage.id).all():
        try:
            content = json.loads(message.content)
        except (ValueError, TypeError):
            content = message.content
        history.append({"role": message.role, "content": content})
    task._assistant_history = history
    task._assistant_quiet = profile.get("quiet", True)
    task._assistant_project_id = session.project_id
    return profile


def wake_prompt(task):
    prompt = task.prompt or ""
    if getattr(task, "_assistant_quiet", False):
        prompt += ("\nThis is a proactive wake in your ongoing chat. Check the conversation and act on the mission. "
                   "If there is no meaningful change, completion, failure or required user action, "
                   f"respond with exactly {NO_UPDATE}. Do not repeat previous updates.")
    return prompt
