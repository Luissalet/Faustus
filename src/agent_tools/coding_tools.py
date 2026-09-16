import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.constants import DATA_DIR


_TODO_DIR = os.path.join(DATA_DIR, "agent_todos")
_INCOMPLETE_STATUSES = frozenset({"pending", "in_progress"})


def _safe_session_id(value: str) -> str:
    value = value or "current"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "current"


def _project_file(project_id: str) -> str:
    pid = _safe_session_id(str(project_id or "").strip())
    return os.path.join(_TODO_DIR, f"project-{pid}.json")


def _load_project_doc(project_id: str) -> Dict[str, Any]:
    pid = str(project_id or "").strip()
    if not pid:
        return {}
    try:
        with open(_project_file(pid), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dump_project_doc(project_id: str, data: Dict[str, Any]) -> None:
    pid = str(project_id or "").strip()
    if not pid:
        return
    os.makedirs(_TODO_DIR, exist_ok=True)
    payload = dict(data or {})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(_project_file(pid), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def save_project_todos(project_id: str, todos: List[Dict[str, Any]]) -> None:
    """Merge `todos` into data/agent_todos/project-<id>.json."""
    pid = str(project_id or "").strip()
    if not pid:
        return
    data = _load_project_doc(pid)
    data["todos"] = list(todos or [])
    _dump_project_doc(pid, data)


def load_project_todos(project_id: str) -> List[Dict[str, Any]]:
    todos = _load_project_doc(project_id).get("todos")
    return todos if isinstance(todos, list) else []


def incomplete_todos(todos: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in todos or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "pending") in _INCOMPLETE_STATUSES:
            out.append(item)
    return out


def save_project_working_set(
    project_id: str,
    *,
    last_files: Optional[List[str]] = None,
    last_tools: Optional[List[Dict[str, Any]]] = None,
    last_error: str = "",
) -> None:
    """Merge last files/tools/error into the project todo file without wiping todos."""
    pid = str(project_id or "").strip()
    if not pid:
        return
    data = _load_project_doc(pid)
    data["last_files"] = [str(p) for p in (last_files or []) if str(p).strip()][:12]
    tools: List[Dict[str, Any]] = []
    for item in (last_tools or [])[-8:]:
        if not isinstance(item, dict):
            continue
        tools.append({
            "tool": str(item.get("tool") or ""),
            "ok": bool(item.get("ok")),
            "paths": [str(p) for p in (item.get("paths") or []) if p][:6],
        })
    data["last_tools"] = tools
    data["last_error"] = str(last_error or "")[:400]
    _dump_project_doc(pid, data)


def load_project_working_set(project_id: str) -> Dict[str, Any]:
    data = _load_project_doc(project_id)
    todos = data.get("todos") if isinstance(data.get("todos"), list) else []
    files = data.get("last_files") if isinstance(data.get("last_files"), list) else []
    tools = data.get("last_tools") if isinstance(data.get("last_tools"), list) else []
    return {
        "todos": todos,
        "last_files": [str(p) for p in files if p],
        "last_tools": [t for t in tools if isinstance(t, dict)],
        "last_error": str(data.get("last_error") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def save_todos(session_id: str, todos: List[Dict[str, Any]]) -> None:
    """Persist an (annotated) todo list for a chat — used by the agent loop to
    store the harness 'verified' flags alongside the model's statuses so the
    Progress panel restores exactly what was shown."""
    sid = _safe_session_id(str(session_id or "current"))
    os.makedirs(_TODO_DIR, exist_ok=True)
    path = os.path.join(_TODO_DIR, f"{sid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"todos": todos}, f, ensure_ascii=False, indent=2)


def load_todos(session_id: str) -> List[Dict[str, Any]]:
    """Return the last persisted todowrite list for this chat, or []."""
    sid = _safe_session_id(str(session_id or "current"))
    path = os.path.join(_TODO_DIR, f"{sid}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    todos = data.get("todos") if isinstance(data, dict) else None
    return todos if isinstance(todos, list) else []


class TodoWriteTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {"todos": []}
        except (json.JSONDecodeError, TypeError):
            return {"error": "todowrite: JSON object required", "exit_code": 1}
        todos = args.get("todos")
        if not isinstance(todos, list):
            return {"error": "todowrite: todos must be a list", "exit_code": 1}

        normalized: List[Dict[str, Any]] = []
        allowed_statuses = {"pending", "in_progress", "completed"}
        allowed_priorities = {"low", "medium", "high"}
        active_count = 0
        for item in todos:
            if not isinstance(item, dict):
                return {"error": "todowrite: each todo must be an object", "exit_code": 1}
            content_text = str(item.get("content") or item.get("text") or "").strip()
            if not content_text:
                return {"error": "todowrite: todo content required", "exit_code": 1}
            status = str(item.get("status") or "pending").strip()
            if status not in allowed_statuses:
                return {"error": f"todowrite: invalid status {status!r}", "exit_code": 1}
            if status == "in_progress":
                active_count += 1
            priority = str(item.get("priority") or "medium").strip()
            if priority not in allowed_priorities:
                priority = "medium"
            normalized.append({
                "content": content_text,
                "status": status,
                "priority": priority,
            })
        if active_count > 1:
            return {"error": "todowrite: only one todo can be in_progress", "exit_code": 1}

        session_id = _safe_session_id(str(ctx.get("session_id") or args.get("session_id") or "current"))
        os.makedirs(_TODO_DIR, exist_ok=True)
        path = os.path.join(_TODO_DIR, f"{session_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"todos": normalized}, f, ensure_ascii=False, indent=2)
        pid = str((ctx or {}).get("project_id") or "").strip()
        if pid:
            try:
                from src.settings import get_setting
                if bool(get_setting("agent_project_todos", True)):
                    save_project_todos(pid, normalized)
            except Exception:
                pass

        lines = []
        for item in normalized:
            marker = {"pending": " ", "in_progress": ">", "completed": "x"}[item["status"]]
            lines.append(f"[{marker}] {item['content']} ({item['priority']})")
        return {
            "output": "Updated todo list:\n" + ("\n".join(lines) if lines else "(empty)"),
            "exit_code": 0,
            "todos": normalized,
        }
