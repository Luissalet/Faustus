"""src/panel_actions.py -- server actions of a user panel: call a tool or a URL now or on a
schedule and keep the answer in the panel's database or storage, so a panel collects history
while nobody has it open (a GPU sample every 5 minutes, the Ágora counts every hour).

A panel's actions live in DATA_DIR/panels/<id>/actions.json, their last runs in
actions_state.json. One action:

    {"name": "muestra",                       # [a-z0-9][a-z0-9_-]*, unique in the panel
     "label": "Muestra de la GPU",            # optional
     "every_minutes": 5,                      # or "cron": "*/5 * * * *"; neither = only on demand
     "tool": "manage_tasks", "args": {...},   # an agent tool...
     "app": "darwin",                         # ...or that tool of a connected Hoard
     "url": "/api/system/usage",              # ...or a URL: /api/* of Faustus, hub:/api/* of the Hub, http(s)://
     "method": "GET", "body": {...}, "headers": {...},
     "save": {"table": "muestras", "columns": {"ts": "$now", "util": "gpu_pool.util"}, "keep": 2016},
     "enabled": true}

``save`` may be one target or a list of them:

* ``{"table": t, "columns": {col: path}, "rows": path?, "keep": n?}`` -- insert one row (or one per
  item of the list at ``rows``); the table and missing columns are created; ``keep`` prunes the oldest.
* ``{"sql": "INSERT ... VALUES (:a, :b)", "columns": {a: path, b: path}, "rows": path?}`` -- any statement.
* ``{"storage": key, "value": path?, "append": bool?, "keep": n?}`` -- faustus.storage.

A path is dotted (``gpu.0.util``); ``$`` is the whole answer (or the current item with ``rows``),
``$root.x`` reaches the whole answer from an item, ``$now`` is the local time (ISO), ``$ts`` epoch
seconds, ``$date`` the day. A value that is not a string is stored as it is.

Scheduling reuses Faustus' task scheduler (src/task_scheduler.py): each scheduled action is a
ScheduledTask of type ``action`` with the built-in action ``panel_action``, so it shows in
Automations with its runs, survives restarts and is claimed once across workers. A failed run is
recorded here and as a skipped run there, without notifications.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src import panel_db, panels

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
BUILTIN_ACTION = "panel_action"
HISTORY = 20
DEFAULT_TIMEOUT_S = 60.0

_lock = threading.RLock()


def _err(detail: str, cls: str = "panel.action_bad_args") -> panels.PanelError:
    return panels.PanelError(detail, cls)


# ---------------------------------------------------------------------------
# storage of the definitions and their state
# ---------------------------------------------------------------------------
def _actions_path(panel_id: str):
    return panels._existing(panel_id) / "actions.json"


def _state_path(panel_id: str):
    return panels._existing(panel_id) / "actions_state.json"


def _load(path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(panel_id: str) -> List[Dict[str, Any]]:
    data = _load(_actions_path(panel_id))
    items = data.get("actions") if isinstance(data, dict) else data
    return [a for a in (items or []) if isinstance(a, dict) and NAME_RE.match(str(a.get("name") or ""))]


def _save(panel_id: str, actions: List[Dict[str, Any]]) -> None:
    panels._write(_actions_path(panel_id), json.dumps({"actions": actions}, ensure_ascii=False, indent=2))


def _state(panel_id: str) -> Dict[str, Any]:
    data = _load(_state_path(panel_id))
    return data if isinstance(data, dict) else {}


def _save_state(panel_id: str, state: Dict[str, Any]) -> None:
    panels._write(_state_path(panel_id), json.dumps(state, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# the definition
# ---------------------------------------------------------------------------
def cron_for(every_minutes: Any) -> str:
    """A cron expression for "every N minutes". Under an hour: */N past the hour; whole hours and
    days round to the nearest hour or day (cron has no "every 90 minutes")."""
    try:
        n = int(every_minutes)
    except (TypeError, ValueError):
        raise _err("`every_minutes` must be a whole number of minutes")
    if n < 1:
        raise _err("`every_minutes` must be at least 1")
    if n < 60:
        return "* * * * *" if n == 1 else f"*/{n} * * * *"
    if n < 1440:
        h = max(1, round(n / 60))
        return "0 * * * *" if h == 1 else f"0 */{h} * * *"
    d = max(1, round(n / 1440))
    return "0 0 * * *" if d == 1 else f"0 0 */{d} * *"


def _check_cron(expr: str) -> str:
    expr = str(expr or "").strip()
    try:
        from croniter import croniter
        croniter(expr, datetime.now())
    except Exception as exc:  # noqa: BLE001
        raise _err(f"invalid cron expression {expr!r}: {exc}")
    return expr


def _clean_save(save: Any) -> List[Dict[str, Any]]:
    if save in (None, "", []):
        return []
    items = save if isinstance(save, list) else [save]
    out = []
    for i, s in enumerate(items):
        if not isinstance(s, dict):
            raise _err(f"save[{i}] must be an object")
        if s.get("table"):
            if not TABLE_RE.match(str(s["table"])):
                raise _err(f"save[{i}].table must be a plain SQL name (letters, digits, _)")
            cols = s.get("columns")
            if cols is not None and not (isinstance(cols, dict) and all(TABLE_RE.match(str(k)) for k in cols)):
                raise _err(f"save[{i}].columns must map plain column names to paths")
        elif s.get("sql"):
            if not isinstance(s.get("columns", {}), dict):
                raise _err(f"save[{i}].columns must map parameter names to paths")
        elif s.get("storage"):
            if not panels.KEY_RE.match(str(s["storage"])):
                raise _err(f"save[{i}].storage must be a storage key")
        else:
            raise _err(f"save[{i}] needs table, sql or storage")
        if s.get("keep") is not None:
            try:
                s["keep"] = max(1, int(s["keep"]))
            except (TypeError, ValueError):
                raise _err(f"save[{i}].keep must be an integer")
        out.append(s)
    return out


def clean_spec(spec: Dict[str, Any], name: Any = None) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise _err("the action must be an object")
    name = str(name or spec.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        raise _err("`name` is required: lowercase letters, digits, - and _ (max 40)")
    out: Dict[str, Any] = {"name": name}
    if spec.get("label"):
        out["label"] = str(spec["label"])[:80]
    sources = [k for k in ("tool", "url") if spec.get(k)]
    if len(sources) != 1:
        raise _err("give exactly one of `tool` (with optional `app`) or `url`")
    if spec.get("tool"):
        out["tool"] = str(spec["tool"]).strip()
        if spec.get("app"):
            out["app"] = str(spec["app"]).strip()
        args = spec.get("args") or {}
        if not isinstance(args, dict):
            raise _err("`args` must be an object")
        out["args"] = args
    else:
        url = str(spec["url"]).strip()
        if not (url.startswith("/") or url.startswith("hub:") or re.match(r"^https?://", url, re.I)):
            raise _err("`url` must be /api/... (Faustus), hub:/api/... (the Hub) or http(s)://...")
        out["url"] = url
        method = str(spec.get("method") or ("POST" if spec.get("body") is not None else "GET")).upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise _err("`method` must be GET, POST, PUT, PATCH or DELETE")
        out["method"] = method
        if spec.get("body") is not None:
            out["body"] = spec["body"]
        if spec.get("headers"):
            if not isinstance(spec["headers"], dict):
                raise _err("`headers` must be an object")
            out["headers"] = {str(k): str(v) for k, v in spec["headers"].items()}
    if spec.get("cron"):
        out["cron"] = _check_cron(spec["cron"])
    elif spec.get("every_minutes") not in (None, "", 0):
        out["every_minutes"] = int(spec["every_minutes"])
        cron_for(out["every_minutes"])
    if spec.get("timeout_s"):
        try:
            out["timeout_s"] = max(1.0, min(float(spec["timeout_s"]), 600.0))
        except (TypeError, ValueError):
            raise _err("`timeout_s` must be a number")
    out["save"] = _clean_save(spec.get("save"))
    out["enabled"] = spec.get("enabled") is not False
    return out


def schedule_of(spec: Dict[str, Any]) -> Optional[str]:
    if spec.get("cron"):
        return spec["cron"]
    if spec.get("every_minutes"):
        return cron_for(spec["every_minutes"])
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def list_actions(panel_id: str) -> Dict[str, Any]:
    actions = load(panel_id)
    state = _state(panel_id)
    nexts = _next_runs(panel_id)
    rows = []
    for a in actions:
        st = state.get(a["name"]) or {}
        rows.append({**a, "schedule": schedule_of(a), "next_run": nexts.get(a["name"]),
                     "last_run": st.get("last_run"), "last_ok": st.get("last_ok"), "last_error": st.get("last_error"),
                     "last_ms": st.get("last_ms"), "last_saved": st.get("last_saved"), "runs": st.get("runs", 0),
                     "failures": st.get("failures", 0), "last_preview": st.get("last_preview"),
                     "history": st.get("history", [])})
    return {"id": panel_id, "actions": rows}


def set_action(panel_id: str, spec: Dict[str, Any], *, name: Any = None, owner: Optional[str] = None) -> Dict[str, Any]:
    clean = clean_spec(spec, name)
    with _lock:
        actions = [a for a in load(panel_id) if a["name"] != clean["name"]]
        actions.append(clean)
        _save(panel_id, actions)
    sync_schedule(panel_id, owner=owner)
    row = next(a for a in list_actions(panel_id)["actions"] if a["name"] == clean["name"])
    return {"id": panel_id, "action": row}


def remove_action(panel_id: str, name: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        actions = load(panel_id)
        kept = [a for a in actions if a["name"] != name]
        if len(kept) == len(actions):
            raise _err(f"panel {panel_id} has no action {name!r}", "panel.not_found")
        _save(panel_id, kept)
        state = _state(panel_id)
        state.pop(name, None)
        _save_state(panel_id, state)
    sync_schedule(panel_id, owner=owner)
    return {"id": panel_id, "removed": name}


def get_action(panel_id: str, name: str) -> Dict[str, Any]:
    for a in load(panel_id):
        if a["name"] == name:
            return a
    raise _err(f"panel {panel_id} has no action {name!r}", "panel.not_found")


# ---------------------------------------------------------------------------
# calling the source
# ---------------------------------------------------------------------------
_HUB: Dict[str, Any] = {"at": 0.0, "base": "", "headers": {}}
HUB_TTL_S = 300.0
_FAMILY = {"at": 0.0}


def family_ready(force: bool = False) -> None:
    """Point the family client at the Hub (family_services.configure), at most every few minutes:
    the lookup takes seconds and a panel calls Hoard tools several times per load."""
    if not force and _FAMILY["at"] and time.monotonic() - _FAMILY["at"] < HUB_TTL_S:
        return
    from src import family_services
    family_services.configure()
    _FAMILY["at"] = time.monotonic()


def _hub_base() -> Tuple[str, Dict[str, str]]:
    """The Hub's URL and auth header, resolved the way the rest of Faustus does (family_services: the
    Hub connector, then HOARD_HUB_URL / HOARD_HUB_TOKEN_FILE). Resolving reads the database and looks
    for the token file, which takes seconds, so it is kept for a few minutes."""
    from src.hoard_link import family
    with _lock:
        if _HUB["base"] and time.monotonic() - _HUB["at"] < HUB_TTL_S:
            return _HUB["base"], dict(_HUB["headers"])
    family_ready(force=True)
    base, headers = family._hub().rstrip("/"), family._headers()
    with _lock:
        _HUB.update(at=time.monotonic(), base=base, headers=headers)
    return base, dict(headers)


def _resolve_url(url: str) -> Tuple[str, Dict[str, str]]:
    if url.startswith("hub:"):
        base, headers = _hub_base()
        rest = url[4:]
        return base + ("" if rest.startswith("/") else "/") + rest, headers
    if url.startswith("/"):
        from src import constants
        from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
        return constants.internal_api_base().rstrip("/") + url, {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    return url, {}


def fetch(url: Any, *, method: str = "GET", body: Any = None, headers: Optional[Dict[str, str]] = None,
          timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Fetch a URL from the server: /api/* of this Faustus (with its internal token), hub:/api/* of the
    Hub (with its token) or any http(s) URL. The body comes back parsed when it is JSON."""
    import httpx
    url = str(url or "").strip()
    if not (url.startswith("/") or url.startswith("hub:") or re.match(r"^https?://", url, re.I)):
        raise _err("`url` must be /api/... , hub:/api/... or http(s)://...", "panel.fetch_bad_args")
    full, extra = _resolve_url(url)
    hdrs = {**extra, **(headers or {})}
    kwargs: Dict[str, Any] = {}
    if body is not None:
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["content"] = str(body).encode("utf-8")
    from urllib.parse import urlsplit
    local = (urlsplit(full).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True, trust_env=not local) as c:
            r = c.request(str(method or "GET").upper(), full, headers=hdrs, **kwargs)
    except httpx.HTTPError as exc:
        raise _err(f"{type(exc).__name__}: {exc} ({full})", "panel.fetch_failed")
    ctype = r.headers.get("content-type", "")
    data: Any
    try:
        data = r.json() if ("json" in ctype or r.text[:1] in "[{") else r.text
    except ValueError:
        data = r.text
    return {"url": full, "status": r.status_code, "ok": r.is_success, "content_type": ctype, "body": data,
            "ms": int((time.monotonic() - t0) * 1000)}


async def call_source(spec: Dict[str, Any], owner: Optional[str] = None) -> Any:
    timeout = float(spec.get("timeout_s") or DEFAULT_TIMEOUT_S)
    if spec.get("url"):
        res = await asyncio.to_thread(fetch, spec["url"], method=spec.get("method") or "GET", body=spec.get("body"),
                                      headers=spec.get("headers"), timeout_s=timeout)
        if not res["ok"]:
            b = res["body"]
            msg = (b.get("error") or b.get("detail")) if isinstance(b, dict) else str(b)[:200]
            raise _err(f"HTTP {res['status']} from {res['url']}: {msg}", "panel.action_failed")
        return res["body"]
    tool, args = spec["tool"], dict(spec.get("args") or {})
    if spec.get("app"):
        def call():
            from src.hoard_link import family
            family_ready()
            return family.call(spec["app"], tool, args, timeout=timeout)
        res = await asyncio.to_thread(call)
        if not isinstance(res, dict) or res.get("ok") is False or res.get("error"):
            inner = res.get("result") if isinstance(res, dict) else None
            why = (res.get("error") if isinstance(res, dict) else None) or (inner.get("error") if isinstance(inner, dict) else None)
            raise _err(f"{spec['app']}.{tool}: {why or res}", "panel.action_failed")
        return res.get("result", res)
    from src.agent_tools import TOOL_HANDLERS
    handler = TOOL_HANDLERS.get(tool)
    if handler is None:
        raise _err(f"no agent tool named {tool}", "panel.action_failed")
    out = await asyncio.wait_for(handler(json.dumps(args), {"owner": owner or "", "source": "panel"}), timeout)
    if isinstance(out, dict) and out.get("error"):
        raise _err(f"{tool}: {out['error']}", "panel.action_failed")
    if isinstance(out, dict):
        if out.get("result") is not None:
            return out["result"]
        text = out.get("output") if out.get("output") is not None else out.get("response")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except ValueError:
                return text
        return {k: v for k, v in out.items() if k != "exit_code"}
    return out


# ---------------------------------------------------------------------------
# saving the answer
# ---------------------------------------------------------------------------
def extract(expr: Any, item: Any, root: Any, now: Optional[datetime] = None) -> Any:
    if not isinstance(expr, str):
        return expr
    now = now or datetime.now()
    if expr == "$now":
        return now.isoformat(timespec="seconds")
    if expr == "$ts":
        return round(now.timestamp(), 3)
    if expr == "$date":
        return now.date().isoformat()
    if expr in ("$", ""):
        return item
    base, rest = item, expr
    if expr == "$root":
        return root
    if expr.startswith("$root."):
        base, rest = root, expr[6:]
    elif expr.startswith("$."):
        rest = expr[2:]
    return _walk(base, rest.split("."))


def _walk(cur: Any, parts: List[str]) -> Any:
    """Follow a dotted path; `key[]` maps the rest of the path over the list at `key`
    (`resident.models[].model` is the list of model names)."""
    for i, part in enumerate(parts):
        if cur is None:
            return None
        if part.endswith("[]"):
            key = part[:-2]
            seq = cur.get(key) if (key and isinstance(cur, dict)) else (cur if not key else None)
            if not isinstance(seq, list):
                return None
            rest = parts[i + 1:]
            return [_walk(x, rest) if rest else x for x in seq]
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _items(target: Dict[str, Any], root: Any) -> List[Any]:
    if not target.get("rows"):
        return [root]
    got = extract(target["rows"], root, root)
    return got if isinstance(got, list) else ([] if got is None else [got])


def _plain(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else (int(v) if isinstance(v, bool) else v)


def save_result(panel_id: str, targets: List[Dict[str, Any]], result: Any) -> int:
    """Write the answer to every target; returns how many rows/values were written."""
    now = datetime.now()
    written = 0
    for t in targets:
        if t.get("storage"):
            value = extract(t.get("value", "$"), result, result, now)
            key = t["storage"]
            if t.get("append"):
                cur = panels.storage_get(panel_id, key)
                cur = cur if isinstance(cur, list) else []
                cur.append({"at": now.isoformat(timespec="seconds"), "value": value})
                if t.get("keep"):
                    cur = cur[-int(t["keep"]):]
                panels.storage_set(panel_id, key, cur)
            else:
                panels.storage_set(panel_id, key, value)
            written += 1
            continue
        items = _items(t, result)
        if t.get("sql"):
            cols = t.get("columns") or {}
            params = [{k: _plain(extract(p, it, result, now)) for k, p in cols.items()} for it in items]
            if params:
                panel_db.exec_(panel_id, t["sql"], many=params)
            written += len(params)
            continue
        table = t["table"]
        cols = t.get("columns") or {"ts": "$now", "data": "$"}
        rows = [[_plain(extract(p, it, result, now)) for p in cols.values()] for it in items]
        q = panel_db.quote_ident(table)
        existing = {r["name"] for r in panel_db.query(panel_id, f"PRAGMA table_info({q})")["rows"]}
        steps: List[Dict[str, Any]] = []
        if not existing:
            defs = ", ".join(panel_db.quote_ident(c) for c in cols)
            steps.append({"sql": f"CREATE TABLE IF NOT EXISTS {q} (id INTEGER PRIMARY KEY AUTOINCREMENT, {defs})"})
        else:
            for c in cols:
                if c not in existing:
                    steps.append({"sql": f"ALTER TABLE {q} ADD COLUMN {panel_db.quote_ident(c)}"})
        names = ", ".join(panel_db.quote_ident(c) for c in cols)
        marks = ", ".join("?" for _ in cols)
        steps += [{"sql": f"INSERT INTO {q} ({names}) VALUES ({marks})", "params": r} for r in rows]
        if t.get("keep"):
            steps.append({"sql": f"DELETE FROM {q} WHERE rowid <= (SELECT rowid FROM {q} ORDER BY rowid DESC "
                                 f"LIMIT 1 OFFSET {int(t['keep'])})"})
        if steps:
            panel_db.exec_(panel_id, batch=steps)
        written += len(rows)
    return written


def _preview(result: Any) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return text[:600]


def _record(panel_id: str, name: str, *, ok: bool, ms: int, saved: int = 0, error: str = "", result: Any = None,
            trigger: str = "manual") -> Dict[str, Any]:
    with _lock:
        state = _state(panel_id)
        st = state.get(name) or {}
        at = datetime.now().isoformat(timespec="seconds")
        st.update(last_run=at, last_ok=ok, last_ms=ms, last_error=error or None, runs=int(st.get("runs") or 0) + 1)
        if ok:
            st["last_saved"] = saved
            st["last_preview"] = _preview(result)
        else:
            st["failures"] = int(st.get("failures") or 0) + 1
        hist = list(st.get("history") or [])
        hist.append({"at": at, "ok": ok, "ms": ms, "saved": saved, "error": error or None, "trigger": trigger})
        st["history"] = hist[-HISTORY:]
        state[name] = st
        _save_state(panel_id, state)
        return st


async def run_action(panel_id: str, name: str, *, owner: Optional[str] = None, trigger: str = "manual") -> Dict[str, Any]:
    """Call the action's source once, save the answer, record the run. Raises PanelError on failure
    (after recording it)."""
    spec = get_action(panel_id, name)
    t0 = time.monotonic()
    try:
        result = await call_source(spec, owner)
        saved = await asyncio.to_thread(save_result, panel_id, spec.get("save") or [], result)
    except panels.PanelError as exc:
        _record(panel_id, name, ok=False, ms=int((time.monotonic() - t0) * 1000), error=exc.detail, trigger=trigger)
        raise
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        _record(panel_id, name, ok=False, ms=int((time.monotonic() - t0) * 1000), error=msg, trigger=trigger)
        raise _err(msg, "panel.action_failed")
    ms = int((time.monotonic() - t0) * 1000)
    _record(panel_id, name, ok=True, ms=ms, saved=saved, result=result, trigger=trigger)
    return {"id": panel_id, "action": name, "ok": True, "ms": ms, "saved": saved, "preview": _preview(result)}


# ---------------------------------------------------------------------------
# the scheduler: one ScheduledTask per scheduled action
# ---------------------------------------------------------------------------
def task_id(panel_id: str, name: str) -> str:
    return f"panel-{panel_id}--{name}"


def _task_rows(db, panel_id: Optional[str] = None):
    from core.database import ScheduledTask
    rows = db.query(ScheduledTask).filter(ScheduledTask.action == BUILTIN_ACTION).all()
    out = []
    for r in rows:
        try:
            ref = json.loads(r.prompt or "{}")
        except ValueError:
            ref = {}
        if panel_id is None or ref.get("panel") == panel_id:
            out.append((r, ref))
    return out


def _next_runs(panel_id: str) -> Dict[str, Optional[str]]:
    try:
        from core.database import SessionLocal
        db = SessionLocal()
        try:
            return {ref.get("action"): (r.next_run.isoformat() + "Z" if r.next_run else None)
                    for r, ref in _task_rows(db, panel_id) if r.status == "active"}
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return {}


def sync_schedule(panel_id: str, *, owner: Optional[str] = None, remove_all: bool = False) -> Dict[str, Any]:
    """Make the scheduler's tasks for this panel match its actions: one active task per enabled
    action with a schedule, none for the rest."""
    from core.database import ScheduledTask, SessionLocal, TaskRun
    from src.task_scheduler import _utcnow, compute_next_run
    wanted: Dict[str, Tuple[Dict[str, Any], str]] = {}
    name = panel_id
    if not remove_all:
        meta = panels.get(panel_id, with_html=False)
        name = meta.get("name") or panel_id
        for a in load(panel_id):
            cron = schedule_of(a)
            if cron and a.get("enabled", True):
                wanted[a["name"]] = (a, cron)
    created, updated, removed = [], [], []
    db = SessionLocal()
    try:
        for row, ref in _task_rows(db, panel_id):
            if ref.get("action") not in wanted:
                db.query(TaskRun).filter(TaskRun.task_id == row.id).delete(synchronize_session=False)
                db.delete(row)
                removed.append(ref.get("action"))
        db.flush()
        for act, (spec, cron) in wanted.items():
            tid = task_id(panel_id, act)
            row = db.query(ScheduledTask).filter(ScheduledTask.id == tid).first()
            label = f"Panel {name} · {spec.get('label') or act}"[:120]
            prompt = json.dumps({"panel": panel_id, "action": act})
            if row is None:
                row = ScheduledTask(id=tid, owner=owner or None, name=label, prompt=prompt, task_type="action",
                                    action=BUILTIN_ACTION, trigger_type="schedule", schedule="cron",
                                    cron_expression=cron, status="active", output_target="none",
                                    notifications_enabled=False, trigger_counter=0,
                                    next_run=compute_next_run("cron", None, cron_expression=cron, after=_utcnow()))
                db.add(row)
                created.append(act)
            else:
                changed = row.cron_expression != cron or row.status != "active"
                row.name, row.prompt, row.task_type, row.action = label, prompt, "action", BUILTIN_ACTION
                row.schedule, row.cron_expression, row.status = "cron", cron, "active"
                row.output_target, row.notifications_enabled = "none", False
                if owner and not row.owner:
                    row.owner = owner
                if changed or row.next_run is None:
                    row.next_run = compute_next_run("cron", None, cron_expression=cron, after=_utcnow())
                updated.append(act)
        db.commit()
    finally:
        db.close()
    return {"created": created, "updated": updated, "removed": removed}


def _pause_orphan(panel_id: str, name: str) -> None:
    """The panel or the action is gone: stop its task from firing again (it stays visible, paused)."""
    try:
        from core.database import ScheduledTask, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(ScheduledTask).filter(ScheduledTask.id == task_id(panel_id, name)).first()
            if row is not None:
                row.status = "paused"
                db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


async def scheduled_entry(owner: str = "", prompt: str = "", **_kw) -> Tuple[str, bool]:
    """The built-in scheduler action `panel_action`: run the panel action named in the prompt.
    A failure is recorded in the panel and reported as a skipped run (no notification, and it does
    not count against the unattended-failure breaker: a Hoard that is down is not Faustus failing)."""
    from src.builtin_actions import TaskNoop
    try:
        ref = json.loads(prompt or "{}")
        panel_id, name = str(ref["panel"]), str(ref["action"])
    except (ValueError, KeyError, TypeError):
        raise TaskNoop("panel action without a valid {panel, action} reference")
    try:
        out = await run_action(panel_id, name, owner=owner, trigger="schedule")
    except panels.PanelError as exc:
        if exc.error_class in ("panel.not_found", "panel.bad_id"):
            _pause_orphan(panel_id, name)
        raise TaskNoop(f"panel {panel_id} · {name}: {exc.detail}")
    return f"panel {panel_id} · {name}: {out['saved']} saved in {out['ms']} ms", True
