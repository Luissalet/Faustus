"""delegation_routes.py — named routes a coordinator picks for each sub-agent.

The owner keeps a short list in Settings (Agentes › Rutas de delegación, or
``GET/PUT /api/delegation-routes``): each route has a NAME and a one-line
DESCRIPTION, and behind them what the worker runs on — a model (optionally on
another endpoint), an effort, an agent definition (the "profile": its system
prompt, tools and permissions, src/agent_defs.py), a tool category and the
skills it should load first.

Only the names and descriptions reach the model: ``delegate_agents`` gains a
per-task ``route`` property whose enum is the names, in the owner's order, and
whose description lists ``name: description``. The model never sees a model id
or an endpoint, so it cannot invent one; a name that is not on the list refuses
the delegation before any worker starts. A task that names no route (and no
model, tier, endpoint, agent or team member of its own) runs on the default
route when one is set, otherwise as before. Every worker report carries the
route it ran on.

Stored in two settings: ``agent_subagent_routes`` (the list) and
``agent_subagent_route_default`` (a name or "").
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

SETTING = "agent_subagent_routes"
DEFAULT_SETTING = "agent_subagent_route_default"
EFFORTS = ("low", "medium", "high", "max")
FIELDS = ("name", "description", "model", "endpoint_id", "effort", "agent", "category", "skills")
_NAME = re.compile(r"^[\w.-]{1,40}$", re.UNICODE)
MAX_ROUTES = 24


def _get(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def _skills(value: Any) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    return [str(s).strip()[:80] for s in value if str(s).strip()][:12]


def clean(route: Any) -> Dict[str, Any]:
    """One route, normalised; raises ValueError naming what is wrong."""
    if not isinstance(route, dict):
        raise ValueError("each route must be an object")
    name = str(route.get("name") or "").strip()
    if not _NAME.match(name):
        raise ValueError(f"route name {name!r}: 1-40 letters, digits, '.', '-' or '_', no spaces")
    desc = " ".join(str(route.get("description") or "").split())
    if not desc:
        raise ValueError(f"route {name}: a one-line description is required (it is what the model reads)")
    effort = str(route.get("effort") or "").strip().lower()
    if effort and effort not in EFFORTS:
        raise ValueError(f"route {name}: effort must be one of {', '.join(EFFORTS)}")
    out = {
        "name": name, "description": desc[:200],
        "model": str(route.get("model") or "").strip()[:120],
        "endpoint_id": str(route.get("endpoint_id") or "").strip()[:120],
        "effort": effort,
        "agent": str(route.get("agent") or "").strip()[:60],
        "category": str(route.get("category") or "").strip()[:120],
        "skills": _skills(route.get("skills")),
    }
    if not (out["model"] or out["endpoint_id"] or out["agent"] or out["effort"] or out["category"] or out["skills"]):
        raise ValueError(f"route {name}: say what it runs on (a model, an endpoint, an agent profile, an effort, a category or skills)")
    return out


def validate(routes: Any, default: str = "") -> Dict[str, Any]:
    if not isinstance(routes, list):
        raise ValueError("routes must be a list")
    if len(routes) > MAX_ROUTES:
        raise ValueError(f"at most {MAX_ROUTES} routes")
    cleaned, seen = [], set()
    for r in routes:
        c = clean(r)
        if c["name"].lower() in seen:
            raise ValueError(f"route {c['name']} is listed twice")
        seen.add(c["name"].lower())
        cleaned.append(c)
    default = str(default or "").strip()
    if default and default.lower() not in seen:
        raise ValueError(f"default route {default!r} is not in the list")
    return {"routes": cleaned, "default": next((c["name"] for c in cleaned if c["name"].lower() == default.lower()), "")}


def load() -> Dict[str, Any]:
    """The saved routes; a malformed entry is skipped rather than breaking delegation."""
    raw = _get(SETTING, []) or []
    routes: List[Dict[str, Any]] = []
    for r in raw if isinstance(raw, list) else []:
        try:
            routes.append(clean(r))
        except ValueError:
            continue
    default = str(_get(DEFAULT_SETTING, "") or "").strip()
    if default.lower() not in {r["name"].lower() for r in routes}:
        default = ""
    return {"routes": routes, "default": default}


def save(routes: Any, default: str = "") -> Dict[str, Any]:
    data = validate(routes, default)
    from src.settings import update_settings
    update_settings({SETTING: data["routes"], DEFAULT_SETTING: data["default"]})
    return data


def find(name: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    data = data or load()
    low = str(name or "").strip().lower()
    return next((r for r in data["routes"] if r["name"].lower() == low), None)


def schema_property(data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The ``route`` property for a delegate_agents task, or None with no routes."""
    data = data or load()
    if not data["routes"]:
        return None
    lines = "; ".join(f"{r['name']}: {r['description']}" for r in data["routes"])
    tail = (f" Omit it to use `{data['default']}`." if data["default"]
            else " Omit it to run on the coordinator's own model.")
    return {"type": "string", "enum": [r["name"] for r in data["routes"]],
            "description": "Named route for this worker, chosen by what the task needs — " + lines + "." + tail}


def decorate_schemas(schemas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add the ``route`` property to delegate_agents' task items when routes exist.
    Returns the same list object untouched when there are none."""
    prop = schema_property()
    if prop is None:
        return schemas
    out = []
    for s in schemas:
        fn = s.get("function") if isinstance(s, dict) else None
        if not fn or fn.get("name") != "delegate_agents":
            out.append(s)
            continue
        s = copy.deepcopy(s)
        try:
            s["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["route"] = prop
        except (KeyError, TypeError):
            pass
        out.append(s)
    return out


def skills_note(skills: List[str]) -> str:
    if not skills:
        return ""
    names = ", ".join(f"`{s}`" for s in skills)
    return (f"\n\nBefore starting, load these skills with manage_skills (action `view`) and follow them: {names}.")


def apply(task: Dict[str, Any], row: Dict[str, Any], agent: str, data: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the task's route into ``row`` (in place) and return the agent slug to use.

    What the task states itself still wins over the route. Raises ValueError for a
    route that is not on the list."""
    data = data or load()
    asked = str(task.get("route") or task.get("option") or "").strip()
    if asked:
        route = find(asked, data)
        if route is None:
            names = ", ".join(r["name"] for r in data["routes"]) or "none configured"
            raise ValueError(f"delegate_agents: route {asked!r} is not configured (routes: {names})")
    else:
        own_choice = any(str(task.get(k) or "").strip() for k in ("model", "tier", "endpoint_id", "team_member", "agent"))
        route = find(data["default"], data) if data["default"] and not own_choice else None
    if route is None:
        return agent
    row["route"] = route["name"]
    if route["model"] and not row.get("model"):
        row["model"] = route["model"]
    if route["endpoint_id"] and not row.get("endpoint_id"):
        row["endpoint_id"] = route["endpoint_id"]
    if route["effort"] and not row.get("effort"):
        from src import effort_profile
        effort = effort_profile.normalize(route["effort"])
        if effort:
            row["effort"] = effort
    if route["category"] and not row.get("category"):
        from src.agent_tools.subagent_tools import normalize_tool_categories
        cats = normalize_tool_categories(route["category"])
        if cats:
            row["category"] = cats
    if route["skills"]:
        row["instruction"] = (row["instruction"] + skills_note(route["skills"]))[:8600]
    return agent or route["agent"]
