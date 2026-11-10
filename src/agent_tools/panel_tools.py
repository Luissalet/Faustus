"""agent_tools/panel_tools.py -- the eight `panel_*` executors over `src.panels`.

    panel_create    build a new panel (name, html, icon, description)
    panel_update    change a panel's page, name, icon, description or position
    panel_get       read one panel with its HTML
    panel_list      the user's panels
    panel_delete    remove a panel and its versions
    panel_check     render it headless and report console errors, failed requests, screenshot
    panel_versions  list saved versions
    panel_restore   go back to a saved version (written as a new version)

and six over the panel's database, server actions and templates (src/panel_db.py,
src/panel_actions.py, src/panel_templates):

    panel_db_query       rows of one SQL statement (a page at a time); without sql, the tables
    panel_db_exec        change the database: a statement, many parameter sets, a batch or a script
    panel_action_set     create, replace, pause or remove a server action (tool or URL, every N minutes)
    panel_action_list    the actions with their schedule, last run and next run
    panel_action_run     run one now and save its answer
    panel_from_template  create a ready-made panel; without template, the list
    panel_sources        what a panel can read: Faustus routes, Hub routes (the Ágora), Hoards and their tools
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict

from src import panels


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return dict(content)
    raw = (content or "").strip() if isinstance(content, str) else ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


def _err(tool: str, exc: panels.PanelError) -> Dict[str, Any]:
    return {"error": f"{tool}: {exc.detail}", "error_class": exc.error_class, "exit_code": 1}


def _short(meta: Dict[str, Any]) -> str:
    return f"{meta.get('name')} [{meta.get('id')}] v{meta.get('version')} icon={meta.get('icon')}"


def _wrap(tool: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]], render: Callable[[Dict[str, Any]], str]):
    class _Tool:
        async def execute(self, content: Any, ctx: dict) -> dict:
            args = _args(content)
            try:
                data = await asyncio.to_thread(fn, args)
            except panels.PanelError as exc:
                return _err(tool, exc)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{tool}: {type(exc).__name__}: {exc}", "exit_code": 1}
            return {"output": render(data), "exit_code": 0, "result": data}
    _Tool.__name__ = "".join(p.capitalize() for p in tool.split("_")) + "Tool"
    return _Tool


def _need_id(a: Dict[str, Any]) -> str:
    pid = str(a.get("id") or a.get("panel_id") or "").strip()
    if not pid:
        raise panels.PanelError("`id` is required")
    return pid


def _create(a):
    return panels.create(a.get("name"), a.get("html"), icon=a.get("icon"), description=a.get("description", ""),
                         panel_id=a.get("id") or None)


def _update(a):
    return panels.update(_need_id(a), html=a.get("html"), name=a.get("name"), icon=a.get("icon"),
                         description=a.get("description"), order=a.get("order"))


def _get(a):
    return panels.get(_need_id(a))


def _list(a):
    return {"panels": panels.list_panels()}


def _delete(a):
    return panels.delete(_need_id(a))


def _check(a):
    from core.middleware import INTERNAL_TOOL_TOKEN
    return panels.check(_need_id(a), base_url=a.get("base_url") or None, wait_ms=int(a.get("wait_ms") or 1500),
                        token=INTERNAL_TOOL_TOKEN)


def _versions(a):
    return panels.versions(_need_id(a))


def _restore(a):
    return panels.restore(_need_id(a), a.get("version"))


def _r_check(d: Dict[str, Any]) -> str:
    lines = [f"panel {d['id']}: {'OK' if d['ok'] else 'PROBLEMS'} (HTTP {d['status']}, {d['visible_text_chars']} visible characters, title {d['title']!r})"]
    for e in d["console_errors"]:
        lines.append(f"console error: {e['text']}")
    for e in d["page_errors"]:
        lines.append(f"page error: {e}")
    for f in d["failed_requests"]:
        lines.append(f"failed request: {f.get('status') or f.get('error')} {f['url']}")
    lines += [f"note: {n}" for n in d["notes"]]
    if d.get("screenshot"):
        lines.append(f"screenshot: {d['screenshot']}")
    return "\n".join(lines)


PanelCreateTool = _wrap("panel_create", _create, lambda d: f"created {_short(d)}; it now shows in the sidebar under Panels")
PanelUpdateTool = _wrap("panel_update", _update, lambda d: f"updated {_short(d)} (html {'changed' if d.get('html_changed') else 'unchanged'})")
PanelGetTool = _wrap("panel_get", _get, lambda d: f"{_short(d)}\n\n{d.get('html', '')}")
PanelListTool = _wrap("panel_list", _list, lambda d: "\n".join(_short(p) for p in d["panels"]) or "no panels yet")
PanelDeleteTool = _wrap("panel_delete", _delete, lambda d: f"deleted panel {d['deleted']}")
PanelCheckTool = _wrap("panel_check", _check, _r_check)
PanelVersionsTool = _wrap("panel_versions", _versions, lambda d: f"current v{d['current']}\n" + "\n".join(
    f"v{v['version']}  {v['saved']}  {v['bytes']} bytes" for v in d["versions"]))
PanelRestoreTool = _wrap("panel_restore", _restore, lambda d: f"restored v{d['restored_from']} as {_short(d)}")


# ---------------------------------------------------------------------------
# the panel's database, server actions and templates
# ---------------------------------------------------------------------------
def _awrap(tool: str, fn, render: Callable[[Dict[str, Any]], str]):
    """Like _wrap for executors that are coroutines (they call tools or run actions); the ctx owner
    reaches them."""
    class _Tool:
        async def execute(self, content: Any, ctx: dict) -> dict:
            args = _args(content)
            try:
                data = await fn(args, (ctx or {}).get("owner") or None)
            except panels.PanelError as exc:
                return _err(tool, exc)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{tool}: {type(exc).__name__}: {exc}", "exit_code": 1}
            return {"output": render(data), "exit_code": 0, "result": data}
    _Tool.__name__ = "".join(p.capitalize() for p in tool.split("_")) + "Tool"
    return _Tool


def _cell_text(v: Any) -> str:
    text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else ("NULL" if v is None else str(v))
    return text if len(text) <= 120 else text[:117] + "..."


async def _db_query(a, owner):
    from src import panel_db
    pid = _need_id(a)
    if not str(a.get("sql") or "").strip():
        return {"id": pid, **await asyncio.to_thread(panel_db.tables, pid)}
    return {"id": pid, **await asyncio.to_thread(panel_db.query, pid, a.get("sql"), a.get("params"), limit=a.get("limit") or 200,
                                                  offset=a.get("offset") or 0, timeout_s=a.get("timeout_s"))}


def _r_db_query(d: Dict[str, Any]) -> str:
    if "tables" in d:
        if not d["tables"]:
            return f"panel {d['id']}: the database is empty (no tables yet)"
        lines = [f"panel {d['id']}: {len(d['tables'])} tables/views, {d['bytes']} bytes"]
        for t in d["tables"]:
            cols = ", ".join(f"{c['name']}{' ' + c['type'] if c['type'] else ''}" for c in t["columns"])
            lines.append(f"- {t['name']} ({t['type']}, {t['rows']} rows): {cols}")
        return "\n".join(lines)
    if not d["columns"]:
        return f"statement ran: {d.get('changes', 0)} changes"
    lines = [" | ".join(d["columns"])]
    lines += [" | ".join(_cell_text(r.get(c)) for c in d["columns"]) for r in d["rows"]]
    tail = f"\n{d['row_count']} rows" + (f"; more from offset {d['next_offset']}" if d.get("more") else "")
    return "\n".join(lines) + tail


async def _db_exec(a, owner):
    from src import panel_db
    pid = _need_id(a)
    return {"id": pid, **await asyncio.to_thread(panel_db.exec_, pid, a.get("sql"), a.get("params"), many=a.get("many"),
                                                  batch=a.get("batch"), timeout_s=a.get("timeout_s"))}


def _r_db_exec(d: Dict[str, Any]) -> str:
    out = f"panel {d['id']}: done, {d.get('changes', 0)} changes"
    if d.get("last_row_id"):
        out += f", last row id {d['last_row_id']}"
    if d.get("results"):
        out += f", {len(d['results'])} statements"
    return out


def _action_line(a: Dict[str, Any]) -> str:
    src = f"{a.get('app') + '.' if a.get('app') else ''}{a.get('tool')}" if a.get("tool") else f"{a.get('method', 'GET')} {a.get('url')}"
    when = f"every {a['every_minutes']} min" if a.get("every_minutes") else (a.get("cron") or "on demand")
    last = (f"last {a['last_run']} {'ok' if a.get('last_ok') else 'FAILED: ' + str(a.get('last_error'))}"
            if a.get("last_run") else "never run")
    nxt = f", next {a['next_run']}" if a.get("next_run") else ""
    off = "" if a.get("enabled", True) else " [paused]"
    return f"- {a['name']}{off}: {src}; {when}; {last}{nxt}; runs {a.get('runs', 0)}"


async def _action_set(a, owner):
    from src import panel_actions
    pid = _need_id(a)
    name = str(a.get("name") or "").strip()
    if a.get("remove"):
        return await asyncio.to_thread(panel_actions.remove_action, pid, name, owner=owner)
    spec = {k: v for k, v in a.items() if k not in ("id", "panel_id", "remove", "run_now")}
    out = await asyncio.to_thread(panel_actions.set_action, pid, spec, owner=owner)
    if a.get("run_now"):
        try:
            out["run"] = await panel_actions.run_action(pid, out["action"]["name"], owner=owner)
        except panels.PanelError as exc:
            out["run"] = {"ok": False, "error": exc.detail}
    return out


def _r_action_set(d: Dict[str, Any]) -> str:
    if "removed" in d:
        return f"removed action {d['removed']} from panel {d['id']}"
    out = f"panel {d['id']} action saved:\n{_action_line(d['action'])}"
    run = d.get("run")
    if run:
        out += (f"\nfirst run: ok, {run['saved']} saved in {run['ms']} ms" if run.get("ok")
                else f"\nfirst run FAILED: {run.get('error')}")
    return out


async def _action_list(a, owner):
    from src import panel_actions
    return await asyncio.to_thread(panel_actions.list_actions, _need_id(a))


def _r_action_list(d: Dict[str, Any]) -> str:
    return "\n".join([f"panel {d['id']}: {len(d['actions'])} actions"] + [_action_line(a) for a in d["actions"]])


async def _action_run(a, owner):
    from src import panel_actions
    name = str(a.get("name") or a.get("action") or "").strip()
    if not name:
        raise panels.PanelError("`name` of the action is required")
    return await panel_actions.run_action(_need_id(a), name, owner=owner)


async def _from_template(a, owner):
    from src import panel_templates
    tid = str(a.get("template") or "").strip()
    if not tid:
        return {"templates": panel_templates.list_templates()}
    return await panel_templates.create_from_template(tid, name=a.get("name") or None, panel_id=a.get("id") or None,
                                                      owner=owner, first_run=a.get("first_run") is not False)


def _r_from_template(d: Dict[str, Any]) -> str:
    if "templates" in d:
        return "templates:\n" + "\n".join(
            f"- {t['id']}: {t['name']} -- {t['description']}"
            + (f" (actions: {', '.join(x['name'] + ' ' + (x['schedule'] or 'on demand') for x in t['actions'])})" if t["actions"] else "")
            for t in d["templates"])
    runs = "; ".join(f"{r['action']} {'ok, ' + str(r['saved']) + ' saved' if r['ok'] else 'FAILED: ' + r['error']}"
                     for r in d.get("first_runs", []))
    return (f"created {_short(d)} from template {d['template']}; it shows in the sidebar under Panels"
            + (f"; actions: {', '.join(d['actions'])}" if d.get("actions") else "") + (f"; first run: {runs}" if runs else ""))


async def _sources(a, owner):
    from src import panel_sources
    return await asyncio.to_thread(panel_sources.sources, app=a.get("app") or None, probe_path=a.get("probe") or None,
                                   with_tools=a.get("tools") is not False)


def _r_sources(d: Dict[str, Any]) -> str:
    from src import panel_sources
    return panel_sources.render(d)


PanelSourcesTool = _awrap("panel_sources", _sources, _r_sources)
PanelDbQueryTool = _awrap("panel_db_query", _db_query, _r_db_query)
PanelDbExecTool = _awrap("panel_db_exec", _db_exec, _r_db_exec)
PanelActionSetTool = _awrap("panel_action_set", _action_set, _r_action_set)
PanelActionListTool = _awrap("panel_action_list", _action_list, _r_action_list)
PanelActionRunTool = _awrap("panel_action_run", _action_run,
                            lambda d: f"ran {d['action']} of panel {d['id']}: {d['saved']} saved in {d['ms']} ms\nanswer: {d['preview']}")
PanelFromTemplateTool = _awrap("panel_from_template", _from_template, _r_from_template)

HANDLERS = {
    "panel_create": PanelCreateTool, "panel_update": PanelUpdateTool, "panel_get": PanelGetTool,
    "panel_list": PanelListTool, "panel_delete": PanelDeleteTool, "panel_check": PanelCheckTool,
    "panel_versions": PanelVersionsTool, "panel_restore": PanelRestoreTool,
    "panel_db_query": PanelDbQueryTool, "panel_db_exec": PanelDbExecTool,
    "panel_action_set": PanelActionSetTool, "panel_action_list": PanelActionListTool,
    "panel_action_run": PanelActionRunTool, "panel_from_template": PanelFromTemplateTool,
    "panel_sources": PanelSourcesTool,
}
