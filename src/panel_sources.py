"""src/panel_sources.py -- what a user panel can read, so whoever builds one never guesses a path.

* Faustus' own routes (``faustus.api('/api/...')``) with the shape of their answer;
* the Hoard Hub's routes (``faustus.hub('/api/...')``): the Ágora with its spaces and tasks, the
  connected apps, placement. The Ágora is the Hub's, not Faustus';
* every connected Hoard with its tools (``faustus.tool(name, args, app)``), read live from the Hub;
* ``probe``: fetch one Faustus or Hub path now and describe the shape of its answer.

The shapes below were read from the live routes; ``probe`` checks any of them (or any other).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from src import panels

HOW = {
    "faustus": "faustus.api('/api/...') -- this Faustus' routes (JSON)",
    "hub": "faustus.hub('/api/...', {method, body}) -- the Hoard Hub's routes; the Ágora (spaces, tasks, agents, locks) lives there",
    "hoard": "faustus.tool(name, args, app) -- a connected Hoard's tool; the answer's result field holds what the tool returned",
    "web": "faustus.fetch(url) -- any other http(s) URL, fetched by the server ({status, ok, body})",
    "own": "faustus.db (SQLite of the panel), faustus.storage (key-value), faustus.actions (scheduled tool/URL calls that fill them)",
}

FAUSTUS_ROUTES: List[Dict[str, str]] = [
    {"path": "/api/system/usage", "what": "GPUs, loaded models, Ollama, CPU and RAM right now",
     "shape": "{gpu:[{index,name,util %,mem_used MiB,mem_total MiB,temp,power,power_limit,models:[{name,bytes}]}], gpu_pool:{name,util,util_avg,mem_used,mem_total,power,temp}, resident:{models:[{model,engine,loaded_by,bytes,gpus:[{index,bytes}],context_length,generating}]}, ollama:{reachable,base,models}, cpu:{percent,count}, ram:{used,total,percent}, health:{score,grade,components}}"},
    {"path": "/api/usage/recap?days=30", "what": "conversations, turns, tokens and tools over N days",
     "shape": "{days,since,chats,total:{turns,input_tokens,output_tokens,tool_calls,time_s,cache_hit_percent}, by_model:{<model>:{turns,input_tokens,output_tokens,tool_calls,time_s}}, top_tools:[[name,count]], busiest_days:[[date,turns]]}"},
    {"path": "/api/sessions", "what": "chats, newest first",
     "shape": "[{id,name,model,folder,archived,is_important,total_tokens,created_at,updated_at,last_message_at}]"},
    {"path": "/api/tasks", "what": "scheduled tasks (Automations) with their next and last run",
     "shape": "{tasks:[{id,name,task_type,action,schedule,cron_expression,status,next_run,last_run,run_count}]}"},
    {"path": "/api/projects", "what": "Faustus projects",
     "shape": "[{id,name,folder,workspace,enabled,pinned,archived,created_at,updated_at}]"},
    {"path": "/api/memory", "what": "saved memories", "shape": "{memory:[{id,text,timestamp,source,category,uses}]}"},
    {"path": "/api/skills", "what": "the skill library", "shape": "{skills:[{id,name,description,category,tags,status}], count}"},
    {"path": "/api/models", "what": "model endpoints and the models each serves",
     "shape": "{items:[{endpoint_id,endpoint_name,url,models,models_vision,category}]}"},
    {"path": "/api/calendar/events?start=YYYY-MM-DD&end=YYYY-MM-DD", "what": "calendar events in a range", "shape": "{events:[...]}"},
    {"path": "/api/notes", "what": "notes", "shape": "{notes:[...]}"},
    {"path": "/api/loop-cards", "what": "Loop Cards (metric, noise, verdicts)", "shape": "{cards:[...]}"},
    {"path": "/api/panels", "what": "the panels themselves", "shape": "{panels:[{id,name,icon,description,version}]}"},
]

HUB_ROUTES: List[Dict[str, str]] = [
    {"path": "/api/agora/spaces", "what": "Ágora spaces (one per project or Hoard) with task counts",
     "shape": "{spaces:[{id,title,description,workspace,members,counts:{open,active,review,blocked,waiting},last_activity}]}"},
    {"path": "/api/agora/tasks?space_id=<id>&status=<status>&owner=<agent>&limit=200",
     "what": "tasks of one space; status: active (claimed..blocked), open, claimed, in_progress, review, changes, approved, blocked, done, dropped",
     "shape": "{tasks:[{id,title,body,status,kind,priority,owner,reviewer,created_by,acceptance,result,commits,labels,paths,created,updated,closed_at,space_id}], next_cursor}; times are epoch seconds"},
    {"path": "/api/agora/tasks/<id>?space_id=<id>", "what": "one task with its discussion", "shape": "{task:{...}, messages:[...]}"},
    {"path": "/api/agora/agents", "what": "agents and what they are doing", "shape": "{agents:[{id,name,kind,state,doing,last_seen,quota}]}"},
    {"path": "/api/agora/locks", "what": "paths locked by agents", "shape": "{locks:[{resource,owner,task_id,note,expires}]}"},
    {"path": "/api/agora/digest?hours=12", "what": "what each agent did lately",
     "shape": "{hours,since,agents:{<agent>:{opened,claimed,submitted,done,reviews,messages}}}"},
    {"path": "/api/apps", "what": "connected Hoards with port, purpose and capabilities",
     "shape": "{apps:[{id,name,purpose,url,port,capabilities,kind}]}"},
    {"path": "/api/apps/<id>/tools", "what": "one Hoard's tools with their input schemas",
     "shape": "{tools:[{name,description,inputSchema}]}"},
    {"path": "/api/placement", "what": "where Faustus runs (PC or Sparks) and the last move",
     "shape": "{location,node,since,job:{running,to,step,log}}"},
    {"path": "/api/health", "what": "the Hub itself", "shape": "{ok,service,version,apps,url}"},
]


def _fetch(url: str, timeout: float = 10.0) -> Dict[str, Any]:
    from src import panel_actions
    return panel_actions.fetch(url, timeout_s=timeout)


def shape(value: Any, depth: int = 0, max_depth: int = 3) -> str:
    """A compact description of a JSON value: keys and types, the first item of lists with their length."""
    if depth >= max_depth:
        return "…"
    if isinstance(value, dict):
        items = list(value.items())
        inner = ", ".join(f"{k}: {shape(v, depth + 1, max_depth)}" for k, v in items[:16])
        return "{" + inner + (", …" if len(items) > 16 else "") + "}"
    if isinstance(value, list):
        return f"[{shape(value[0], depth + 1, max_depth) if value else ''}] ({len(value)} items)"
    if isinstance(value, str):
        return f"str {value[:40]!r}" if depth > 0 else "str"
    return type(value).__name__ if value is not None else "null"


def probe(path: str) -> Dict[str, Any]:
    """Fetch a Faustus (/api/...) or Hub (hub:/api/...) path and describe its answer."""
    path = str(path or "").strip()
    if not (path.startswith("/") or path.startswith("hub:") or path.startswith("http")):
        raise panels.PanelError("`probe` is /api/... (Faustus) or hub:/api/... (the Hub)", "panel.bad_args")
    res = _fetch(path, 20.0)
    return {"path": path, "status": res["status"], "ok": res["ok"], "shape": shape(res["body"])[:3000]}


def hoards(tools: bool = True) -> Dict[str, Any]:
    try:
        apps = (_fetch("hub:/api/apps", 10.0).get("body") or {}).get("apps") or []
    except panels.PanelError as exc:
        return {"error": exc.detail, "apps": []}
    apps = [a for a in apps if isinstance(a, dict) and a.get("id") and (a.get("kind") or "app") == "app"]

    def tools_of(app: Dict[str, Any]) -> Dict[str, Any]:
        row = {"id": app["id"], "name": app.get("name"), "purpose": str(app.get("purpose") or "")[:200]}
        if not tools:
            return row
        try:
            res = _fetch(f"hub:/api/apps/{app['id']}/tools", 5.0)
            body = res.get("body") if isinstance(res.get("body"), dict) else {}
            row["tools"] = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
            if not res["ok"] or not row["tools"]:
                row["note"] = "not answering now" if not res["ok"] else "no tools listed"
        except panels.PanelError:
            row["tools"], row["note"] = [], "not answering now"
        return row

    with ThreadPoolExecutor(max_workers=16) as pool:
        return {"apps": list(pool.map(tools_of, apps))}


def app_tools(app: str) -> Dict[str, Any]:
    res = _fetch(f"hub:/api/apps/{app}/tools", 15.0)
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    if not res["ok"]:
        raise panels.PanelError(f"the Hub has no tools for {app!r}: {body.get('error') if isinstance(body, dict) else res['status']}",
                                "panel.not_found")
    out = []
    for t in body.get("tools") or []:
        props = ((t.get("inputSchema") or {}).get("properties") or {})
        out.append({"name": t.get("name"), "description": str(t.get("description") or "").split("\n")[0][:300],
                    "args": {k: (v.get("type") or ("|".join(x.get("type", "?") for x in v.get("anyOf", [])) or "any"))
                             for k, v in props.items()}})
    return {"app": app, "call": f"faustus.tool('<name>', {{...args}}, '{app}')", "tools": out}


def sources(*, app: Optional[str] = None, probe_path: Optional[str] = None, with_tools: bool = True) -> Dict[str, Any]:
    if probe_path:
        return {"probe": probe(probe_path)}
    if app:
        return app_tools(str(app).strip())
    return {"how": HOW, "faustus_routes": FAUSTUS_ROUTES, "hub_routes": HUB_ROUTES, "hoards": hoards(with_tools)}


def render(d: Dict[str, Any]) -> str:
    if "probe" in d:
        p = d["probe"]
        return f"{p['path']} -> HTTP {p['status']}\n{p['shape']}"
    if "tools" in d and "app" in d:
        lines = [f"{d['app']}: {len(d['tools'])} tools, call with {d['call']}"]
        lines += [f"- {t['name']}({', '.join(f'{k}: {v}' for k, v in t['args'].items())}): {t['description']}" for t in d["tools"]]
        return "\n".join(lines)
    lines = ["HOW A PANEL READS DATA"] + [f"- {k}: {v}" for k, v in d["how"].items()]
    lines.append("\nFAUSTUS ROUTES -- faustus.api(path)")
    lines += [f"- {r['path']}: {r['what']}\n    {r['shape']}" for r in d["faustus_routes"]]
    lines.append("\nHUB ROUTES -- faustus.hub(path); the Ágora (spaces, tasks) is here, not in Faustus")
    lines += [f"- {r['path']}: {r['what']}\n    {r['shape']}" for r in d["hub_routes"]]
    h = d["hoards"]
    lines.append("\nCONNECTED HOARDS -- faustus.tool(name, args, app); panel_sources app=<id> gives arguments")
    if h.get("error"):
        lines.append(f"(the Hub did not answer: {h['error']})")
    for a in h.get("apps", []):
        tools = a.get("tools")
        tail = (", ".join(tools[:15]) + (f", … ({len(tools)} in all)" if len(tools) > 15 else "")) if tools else (a.get("note") or "")
        lines.append(f"- {a['id']} ({a.get('name')}): {a['purpose'][:120]}" + (f"\n    tools: {tail}" if tail else ""))
    lines.append("\nUse probe=<path> (or hub:<path>) to see the live shape of any route before writing the page.")
    return "\n".join(lines)
