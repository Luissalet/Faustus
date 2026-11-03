"""recipe_learning.py — recipes learned from finished work (proposal 34).

`src/recipes.py` already stores recipes and can draft a coarse one from a run
(`from_run`: the distinct tool names, in first-seen order). This module adds
what turns a finished piece of work into something reusable on a different
case, on top of that same store and the same skill library:

* **propose** — from a stored trace (a finished run's log, a conversation's
  tool events, a finished workflow run, or steps handed in directly) build a
  parameterised recipe. Literals that came from the request — paths, quoted
  names, ids, URLs, numbers — become named parameters; the order of the steps,
  the tools and the argument templates are kept. The recipe carries its source
  (provenance), the first case as an example and a list of limits. Nothing is
  saved unless the caller asks for a proposal, and nothing is ever promoted by
  proposing.
* **try** — a dry run on a second case. The templates are rendered with the new
  parameters; the report says which steps resolved (no missing parameter, the
  tool exists), which literals of the first case still show up in what was
  rendered (a leak: a value that should have been a parameter and was not) and
  the rendered calls. Tools are not executed unless the caller passes an
  executor; the report says so (`executed: false`).
* **promote** — only after a try that resolved every step, leaked nothing and
  changed every parameter, on the same version of the recipe. A promoted recipe
  is published in the recipe store and added to the skill library as a skill
  tagged ``recipe``, so the existing skill search finds it.

What it does not do: guess which literal "should" be a parameter beyond the
patterns below and the hints the caller gives, follow values that came from a
tool's output into a later call (they stay literal and are listed as a limit),
or run anything on its own.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.log_safety import redact_secrets
from src import recipes as recipes_store
from src.recipes import Recipe

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"[^a-z0-9_]+")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_PARAM_KEY = "$param"

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_QUOTED = re.compile(r"\"([^\"\n]{2,200})\"|'([^'\n]{2,200})'|«([^»\n]{2,200})»|“([^”\n]{2,200})”")
_ABS_PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/]|/)[^\s\"'`<>|;,]*[^\s\"'`<>|;,.:)\]]")
_REL_PATH = re.compile(r"(?<![\w/\\.])[\w.\-]+(?:[\\/][\w.\-]+)+")
_FILE_NAME = re.compile(r"(?<![\w/\\.@-])[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z][A-Za-z0-9]{0,5}(?![\w@/\\-])")
_ID = re.compile(r"\b(?:[A-Za-z]{1,12}[-_#]\d{1,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|#\d+)\b|#\d+")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])")

#: argument keys too generic to name a parameter after
_GENERIC_KEYS = frozenset({"value", "values", "content", "text", "input", "data", "arg", "args", "command",
                           "query", "q", "item", "items", "x", "y", "key", "name_", "param", "params"})

_RUN_STATUS_OK = "ok"
MAX_STEPS = 60
MAX_ARG_CHARS = 8000


class RecipeLearningError(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


# ---------------------------------------------------------------------------
# traces
# ---------------------------------------------------------------------------

def _args_of(raw: Any) -> Any:
    """Arguments of a stored tool event: a JSON object when it parses as one,
    otherwise the raw text under ``content`` (the way text-argument tools
    receive it)."""
    if isinstance(raw, Mapping):
        return copy.deepcopy(dict(raw))
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"content": text}


def _event_ok(ev: Mapping[str, Any]) -> bool:
    code = ev.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return False
    status = str(ev.get("status") or "")
    return status not in ("error", "failed", "blocked", "denied")


def trace_from_events(request: str, events: Iterable[Mapping[str, Any]], *, title: str = "",
                      source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A trace from stored tool events (assistant-message metadata shape:
    ``{tool, command|args, exit_code, status}``). Failed calls are counted and
    left out: a recipe is the path that worked."""
    steps: List[Dict[str, Any]] = []
    skipped = 0
    for ev in events:
        if not isinstance(ev, Mapping) or not ev.get("tool"):
            continue
        if not _event_ok(ev):
            skipped += 1
            continue
        raw = ev.get("args") if ev.get("args") is not None else (ev.get("full_command") or ev.get("command"))
        steps.append({"tool": str(ev["tool"]), "args": _args_of(raw)})
    return {"request": request or "", "steps": steps, "title": title, "skipped_failed": skipped,
            "source": dict(source or {})}


def trace_from_run_log(run_id: str, owner: str) -> Dict[str, Any]:
    """The finished run ``run_id`` (``DATA_DIR/runs/<id>.jsonl``) as a trace,
    reading the same log `recipes.from_run` reads, but keeping each call's
    arguments and its place in the order."""
    path = recipes_store._run_log_path(run_id)
    if not os.path.isfile(path):
        raise RecipeLearningError("recipes.run_not_found", f"no run log for {run_id!r}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = redact_secrets(fh.read())
    except OSError as exc:
        raise RecipeLearningError("recipes.run_unreadable", str(exc))
    statuses: List[str] = []
    title = session_id = ""
    starts: Dict[str, Dict[str, Any]] = {}
    order: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("status"), str) and obj["status"]:
            statuses.append(obj["status"])
        if not title and isinstance(obj.get("label"), str) and obj["label"].strip():
            title = obj["label"].strip()
        if not session_id and isinstance(obj.get("session_id"), str):
            session_id = obj["session_id"]
        ev = obj.get("ev")
        if not (isinstance(ev, str) and ev.startswith("data: ")):
            continue
        try:
            payload = json.loads(ev[6:].strip())
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "tool_start":
            entry = {"tool": payload.get("tool") or payload.get("tool_type") or payload.get("name"),
                     "args": payload.get("full_command") or payload.get("command"),
                     "call_id": str(payload.get("call_id") or "")}
            order.append(entry)
            if entry["call_id"]:
                starts[entry["call_id"]] = entry
        elif kind in ("tool_output", "tool_end", "tool_result"):
            entry = starts.get(str(payload.get("call_id") or ""))
            if entry is not None:
                entry["exit_code"] = payload.get("exit_code")
                entry["status"] = payload.get("status")
    if "done" not in statuses:
        raise RecipeLearningError("recipes.run_not_finished",
                                  "the run has no recorded success status; only a finished run becomes a recipe")
    request = ""
    if session_id:
        request = recipes_store._first_user_message(session_id, owner) or ""
    return trace_from_events(redact_secrets(request), order, title=title,
                             source={"kind": "run", "run_id": str(run_id), "session_id": session_id})


def trace_from_session(session_id: str, owner: str, *, turn: int = -1) -> Dict[str, Any]:
    """The tool events stored with a conversation's turn (default the last
    one): the user message that opened it is the request, the tool events of
    the assistant messages that follow it, in order."""
    try:
        from core.database import ChatMessage, Session as DbSession, SessionLocal
    except Exception as exc:  # noqa: BLE001
        raise RecipeLearningError("recipes.no_database", str(exc))
    db = SessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == session_id).first()
        if row is None or (getattr(row, "owner", None) and row.owner != owner):
            raise RecipeLearningError("recipes.session_not_found", f"no conversation {session_id!r} for this owner")
        messages = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.timestamp.asc()).all())
    finally:
        db.close()
    turns: List[Tuple[str, List[Dict[str, Any]]]] = []
    for msg in messages:
        if msg.role == "user":
            turns.append((recipes_store._extract_text(msg.content), []))
        elif msg.role == "assistant" and turns:
            try:
                meta = json.loads(msg.meta_data or "{}")
            except ValueError:
                meta = {}
            events = meta.get("tool_events") if isinstance(meta, dict) else None
            if isinstance(events, list):
                turns[-1][1].extend(e for e in events if isinstance(e, dict))
    turns = [t for t in turns if t[1]]
    if not turns:
        raise RecipeLearningError("recipes.no_tool_calls", "that conversation has no stored tool calls")
    try:
        request, events = turns[turn]
    except IndexError:
        raise RecipeLearningError("recipes.no_such_turn", f"the conversation has {len(turns)} turns with tool calls")
    return trace_from_events(redact_secrets(request), events,
                             source={"kind": "session", "session_id": session_id, "turn": turn})


def trace_from_workflow_run(run_id: str, owner: str) -> Dict[str, Any]:
    """A finished workflow run as a trace: each node that completed is a step
    (``tool`` = ``workflow.<type>``), the node's config its arguments, and the
    run's inputs the request — those values are what become parameters."""
    from src.workflows.store import WorkflowStore
    store = WorkflowStore()
    found = store.get_run(run_id)
    if found is None or (found["run"].owner and found["run"].owner != owner):
        raise RecipeLearningError("recipes.run_not_found", f"no workflow run {run_id!r} for this owner")
    run, definition = found["run"], found["definition"]
    if run.status != "completed":
        raise RecipeLearningError("recipes.run_not_finished", "only a completed workflow run becomes a recipe")
    node_runs = store.node_runs(run_id)
    steps = []
    for node in definition.nodes:
        nr = node_runs.get(node.id)
        if nr is None or nr.status not in ("completed", "done", "succeeded"):
            continue
        steps.append({"tool": f"workflow.{node.type}", "args": json.loads(json.dumps(dict(node.config), default=str)),
                      "node_id": node.id})
    inputs = dict(run.inputs or {})
    request = "\n".join(f"{k}: {v}" for k, v in inputs.items() if isinstance(v, (str, int, float)))
    return {"request": redact_secrets(request), "steps": steps, "title": definition.title,
            "skipped_failed": 0, "hints": {k: v for k, v in inputs.items() if isinstance(v, (str, int, float))
                                           and not isinstance(v, bool)},
            "source": {"kind": "workflow_run", "run_id": str(run_id), "workflow_id": run.workflow_id,
                       "workflow_version": run.workflow_version}}


# ---------------------------------------------------------------------------
# parameter detection
# ---------------------------------------------------------------------------

def _clean_token(tok: str) -> str:
    return tok.strip().rstrip(".,;:!?)]}»”'\"")


def _candidates(request: str) -> List[Tuple[str, str]]:
    """(value, kind) found in the request text. A match that sits entirely
    inside a longer match (the file name inside its full path, the number
    inside an id) is not a candidate of its own."""
    spans: List[Tuple[int, int, str, str]] = []

    def add(m: "re.Match[str]", kind: str, group: int = 0) -> None:
        raw = m.group(group)
        value = _clean_token(raw)
        if len(value) < 2:
            return
        start = m.start(group)
        spans.append((start, start + len(value), value, kind))

    for m in _URL.finditer(request):
        add(m, "url")
    for m in _QUOTED.finditer(request):
        idx = next(i for i, g in enumerate(m.groups(), 1) if g)
        inner = m.group(idx)
        add(m, "path" if re.search(r"[\\/]|\.[A-Za-z]\w{0,5}$", inner) else "name", idx)
    for m in _ABS_PATH.finditer(request):
        add(m, "path")
    for m in _REL_PATH.finditer(request):
        add(m, "path")
    for m in _FILE_NAME.finditer(request):
        add(m, "path")
    for m in _ID.finditer(request):
        add(m, "id")
    for m in _NUMBER.finditer(request):
        if m.group(0) not in ("0", "1"):
            add(m, "number")
    found: List[Tuple[str, str]] = []
    for start, end, value, kind in sorted(spans, key=lambda t: (t[0], -(t[1] - t[0]))):
        inside = any(os_ <= start and end <= oe and (oe - os_) > (end - start)
                     for os_, oe, _, _ in spans)
        if inside or value in [v for v, _ in found]:
            continue
        found.append((value, kind))
    return found


def _walk_strings(value: Any, path: Tuple = ()) -> Iterable[Tuple[Tuple, Any]]:
    if isinstance(value, Mapping):
        for k, v in value.items():
            yield from _walk_strings(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, path + (i,))
    else:
        yield path, value


def _contains(arg: Any, value: str, kind: str) -> bool:
    if isinstance(arg, bool):
        return False
    if isinstance(arg, (int, float)):
        return kind == "number" and _num_equal(arg, value)
    if isinstance(arg, str):
        if kind == "number":
            return re.search(r"(?<![\w.])" + re.escape(value) + r"(?![\w])", arg) is not None
        return value in arg
    return False


def _num_equal(arg: Any, value: str) -> bool:
    try:
        return float(arg) == float(value.replace(",", "."))
    except ValueError:
        return False


def _param_name(preferred: str, kind: str, used: Dict[str, str], value: str) -> str:
    base = _NAME_RE.sub("_", (preferred or "").lower()).strip("_")
    if not base or base in _GENERIC_KEYS or base[0].isdigit():
        base = {"path": "file" if (value and not re.search(r"[\\/]", value)) else "path", "name": "name", "id": "id", "url": "url", "number": "number"}.get(kind, "value")
    name, n = base, 2
    while name in used and used[name] != value:
        name = f"{base}_{n}"
        n += 1
    used[name] = value
    return name


def _typed(value: str, kind: str) -> Any:
    if kind != "number":
        return value
    try:
        num = float(value.replace(",", "."))
    except ValueError:
        return value
    return int(num) if num.is_integer() and "." not in value and "," not in value else num


def detect_params(request: str, steps: Sequence[Mapping[str, Any]], *,
                  hints: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Parameters of a trace: request literals that appear in the steps'
    arguments (plus explicit hints and absolute paths the steps use that the
    request did not spell out), each with a name, a kind and the value of the
    first case."""
    used: Dict[str, str] = {}
    params: List[Dict[str, Any]] = []
    seen_values: Dict[str, str] = {}

    def register(value: str, kind: str, preferred: str, source: str) -> None:
        if value in seen_values:
            return
        name = _param_name(preferred, kind, used, value)
        seen_values[value] = name
        params.append({"name": name, "kind": kind, "example": _typed(value, kind), "source": source,
                       "required": True})

    for name, value in (hints or {}).items():
        if isinstance(value, bool) or value is None:
            continue
        text = str(value)
        kind = "number" if isinstance(value, (int, float)) else (
            "url" if _URL.fullmatch(text) else "path" if re.search(r"[\\/]", text) else "name")
        if text in seen_values:
            continue
        nm = _NAME_RE.sub("_", str(name).lower()).strip("_") or "value"
        used[nm] = text
        seen_values[text] = nm
        params.append({"name": nm, "kind": kind, "example": value, "source": "hint", "required": True})

    for value, kind in _candidates(request):
        if value in seen_values:
            continue
        preferred = ""
        hit = False
        for step in steps:
            for path, leaf in _walk_strings(step.get("args")):
                if _contains(leaf, value, kind):
                    hit = True
                    key = path[-1] if path and isinstance(path[-1], str) else ""
                    if (isinstance(leaf, str) and leaf == value) or (
                            kind == "number" and not isinstance(leaf, str)):
                        preferred = preferred or key
                    break
            if hit:
                break
        if hit:
            register(value, kind, preferred, "request")

    for step in steps:
        for path, leaf in _walk_strings(step.get("args")):
            if isinstance(leaf, str) and leaf not in seen_values and _ABS_PATH.fullmatch(leaf) \
                    and len(leaf) >= 4 and not leaf.startswith("//"):
                key = path[-1] if path and isinstance(path[-1], str) else ""
                register(leaf, "path", key, "arguments")
    return params


def _template(value: Any, replacements: List[Tuple[str, str, str]]) -> Any:
    """Replace each parameter's value in ``value`` with a reference. ``replacements``
    is [(value, kind, name)], longest value first."""
    if isinstance(value, Mapping):
        return {k: _template(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [_template(v, replacements) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        for text, kind, name in replacements:
            if kind == "number" and _num_equal(value, text):
                return {_PARAM_KEY: name}
        return value
    if not isinstance(value, str):
        return value
    for text, kind, name in replacements:
        if kind != "number" and value == text:
            return {_PARAM_KEY: name}
    out = value
    for text, kind, name in replacements:
        if kind == "number":
            out = re.sub(r"(?<![\w.])" + re.escape(text) + r"(?![\w])", "{{" + name + "}}", out)
        else:
            out = out.replace(text, "{{" + name + "}}")
    return out


def _describe_args(args: Any) -> str:
    if isinstance(args, Mapping):
        parts = []
        for k, v in args.items():
            if isinstance(v, Mapping) and _PARAM_KEY in v:
                parts.append(f"{k}={{{{{v[_PARAM_KEY]}}}}}")
            elif isinstance(v, str) and _PLACEHOLDER.search(v):
                parts.append(f"{k}={v[:80]}")
        return ", ".join(parts)
    return ""


def _fingerprint(recipe: Recipe) -> str:
    body = {"params": [{k: p.get(k) for k in ("name", "kind")} for p in recipe.params],
            "steps": recipe.step_templates}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return _NAME_RE.sub("-", text.lower()).strip("-")[:40] or "recipe"


def propose_from_trace(trace: Mapping[str, Any], owner: str, *, title: str = "",
                       params_hint: Optional[Mapping[str, Any]] = None, save: bool = True) -> Recipe:
    """Turn a trace into a draft learned recipe. ``save=False`` returns it
    without writing (what a caller that only wants to look uses)."""
    raw_steps = [s for s in (trace.get("steps") or []) if isinstance(s, Mapping) and s.get("tool")]
    if not raw_steps:
        raise RecipeLearningError("recipes.no_steps", "the trace has no successful tool calls to learn from")
    if len(raw_steps) > MAX_STEPS:
        raise RecipeLearningError("recipes.too_long", f"more than {MAX_STEPS} steps; learn from a shorter piece of work")
    request = redact_secrets(str(trace.get("request") or ""))
    steps = []
    for s in raw_steps:
        args = _args_of(s.get("args"))
        text = json.dumps(args, ensure_ascii=False, default=str)
        if len(text) > MAX_ARG_CHARS:
            raise RecipeLearningError("recipes.argument_too_large",
                                      f"a call to {s['tool']} has arguments over {MAX_ARG_CHARS} characters")
        steps.append({"tool": str(s["tool"]), "args": json.loads(redact_secrets(text))})

    hints = dict(trace.get("hints") or {})
    hints.update(params_hint or {})
    params = detect_params(request, steps, hints=hints)
    replacements = sorted(((str(p["example"]) if p["kind"] != "number" else _number_text(p["example"], request),
                            p["kind"], p["name"]) for p in params), key=lambda t: -len(t[0]))
    templates = []
    for i, s in enumerate(steps, 1):
        templates.append({"index": i, "tool": s["tool"], "args": _template(s["args"], replacements)})

    used = {m for t in templates for m in _used_params(t["args"])}
    params = [p for p in params if p["name"] in used]
    for p in params:
        p["description"] = f"{p['kind']} taken from the first case ({_short(p['example'])})"
    limits = [
        "Only literals that appear in the request, in the hints or as absolute paths in the arguments become "
        "parameters; any other value is kept as it was in the first case.",
        "A value that a tool returned and a later call reused stays literal; the recipe does not follow data between calls.",
        "Learned from one case; promotion requires a second case with different parameters (recipe_try).",
    ]
    if trace.get("skipped_failed"):
        limits.append(f"{trace['skipped_failed']} failed call(s) in the source were left out.")
    if not params:
        limits.append("No parameter was detected: the steps are the same literals as the first case.")

    rid = f"draft-{_slug(title or str(trace.get('title') or ''))}-{uuid.uuid4().hex[:8]}"
    recipe = Recipe(
        id=rid,
        title=title or str(trace.get("title") or "") or "Learned recipe",
        inputs=[f"{p['name']} ({p['kind']})" for p in params],
        steps=[f"use `{t['tool']}`" + (f" with {d}" if (d := _describe_args(t["args"])) else "") for t in templates],
        tools=list(dict.fromkeys(t["tool"] for t in templates)),
        success_conditions=["each step's result is checked the way the source work was checked"],
        status="draft",
        source={**dict(trace.get("source") or {}), "owner": str(owner), "created_at": time.time(),
                "learned": True},
        params=params,
        step_templates=templates,
        examples=[{"case": 1, "request": request[:600],
                   "params": {p["name"]: p["example"] for p in params}}],
        limits=limits,
        version="0.1.0",
    )
    if save:
        recipes_store.save_recipe(owner, recipe)
    return recipe


def _number_text(value: Any, request: str) -> str:
    """The text of a numeric example as the request spelled it, if it did."""
    text = str(value)
    for m in _NUMBER.finditer(request):
        if _num_equal(value, m.group(0)):
            return m.group(0)
    return text


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _used_params(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, Mapping):
        if _PARAM_KEY in value and len(value) == 1:
            return [str(value[_PARAM_KEY])]
        for v in value.values():
            out.extend(_used_params(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_used_params(v))
    elif isinstance(value, str):
        out.extend(m.group(1) for m in _PLACEHOLDER.finditer(value))
    return out


def propose(owner: str, *, run_id: str = "", session_id: str = "", workflow_run_id: str = "",
            request: str = "", steps: Optional[Sequence[Mapping[str, Any]]] = None, title: str = "",
            params_hint: Optional[Mapping[str, Any]] = None, turn: int = -1) -> Recipe:
    """Entry point: exactly one source (``run_id``, ``session_id``,
    ``workflow_run_id`` or explicit ``steps`` with their ``request``)."""
    chosen = [bool(run_id), bool(session_id), bool(workflow_run_id), bool(steps)]
    if sum(chosen) != 1:
        raise RecipeLearningError("recipes.bad_source",
                                  "give exactly one of run_id, session_id, workflow_run_id or steps")
    if run_id:
        trace = trace_from_run_log(run_id, owner)
    elif session_id:
        trace = trace_from_session(session_id, owner, turn=turn)
    elif workflow_run_id:
        trace = trace_from_workflow_run(workflow_run_id, owner)
    else:
        trace = {"request": request, "steps": list(steps or []), "title": title, "skipped_failed": 0,
                 "source": {"kind": "steps"}}
    return propose_from_trace(trace, owner, title=title, params_hint=params_hint)


# ---------------------------------------------------------------------------
# rendering and try
# ---------------------------------------------------------------------------

def render(value: Any, params: Mapping[str, Any], missing: set) -> Any:
    """Fill a template with ``params``; names with no value go to ``missing``."""
    if isinstance(value, Mapping):
        if _PARAM_KEY in value and len(value) == 1:
            name = str(value[_PARAM_KEY])
            if name not in params:
                missing.add(name)
                return None
            return params[name]
        return {k: render(v, params, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, params, missing) for v in value]
    if isinstance(value, str):
        def sub(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name not in params:
                missing.add(name)
                return m.group(0)
            return str(params[name])
        return _PLACEHOLDER.sub(sub, value)
    return value


def _coerce(param: Mapping[str, Any], value: Any) -> Tuple[Any, str]:
    if param.get("kind") == "number":
        if isinstance(value, bool):
            return value, "must be a number"
        if isinstance(value, (int, float)):
            return value, ""
        try:
            num = float(str(value).replace(",", "."))
        except ValueError:
            return value, "must be a number"
        return (int(num) if num.is_integer() else num), ""
    if value is None or isinstance(value, (Mapping, list)):
        return value, "must be text"
    return str(value), ""


def _tool_known(name: str) -> bool:
    if name.startswith("mcp__") or name.startswith("workflow."):
        return True
    try:
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        if name in BUILTIN_TOOL_DESCRIPTIONS:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        return any((s.get("function") or {}).get("name") == name for s in FUNCTION_TOOL_SCHEMAS)
    except Exception:  # noqa: BLE001
        return False


def _literal_text(rendered: Any, case2_values: Sequence[str]) -> str:
    """Everything the rendered arguments say, minus the second case's own
    values: what is left is what the recipe fixed by itself."""
    text = json.dumps(rendered, ensure_ascii=False, sort_keys=True, default=str)
    for v in sorted({x for x in case2_values if x}, key=len, reverse=True):
        text = text.replace(json.dumps(v, ensure_ascii=False)[1:-1], "\x00")
        text = text.replace(v, "\x00")
    return text


def _leaks_in(text: str, value: Any, kind: str) -> bool:
    s = str(value)
    if not s or len(s) < 2 and kind != "number":
        return False
    if kind == "number":
        return re.search(r"(?<![\w.])" + re.escape(s) + r"(?![\w])", text) is not None
    return json.dumps(s, ensure_ascii=False)[1:-1] in text or s in text


def try_recipe(recipe_id: str, owner: str, params: Mapping[str, Any], *,
               executor: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
               record: bool = True) -> Dict[str, Any]:
    """Dry-run ``recipe_id`` on a second case (see the module docstring)."""
    recipe = recipes_store.get_recipe(recipe_id, owner)
    if recipe is None or not recipe.step_templates:
        raise RecipeLearningError("recipes.not_found", f"no learned recipe {recipe_id!r}")
    case1 = {p["name"]: p.get("example") for p in recipe.params}
    problems: List[str] = []
    values: Dict[str, Any] = {}
    by_name = {p["name"]: p for p in recipe.params}
    for name, p in by_name.items():
        if name not in params:
            problems.append(f"missing parameter `{name}`")
            continue
        value, err = _coerce(p, params[name])
        if err:
            problems.append(f"parameter `{name}` {err}")
        values[name] = value
    unknown = sorted(k for k in params if k not in by_name)

    step_reports: List[Dict[str, Any]] = []
    rendered_all: List[Dict[str, Any]] = []
    case2_text = [str(v) for v in values.values()]
    leaks: List[Dict[str, Any]] = []
    for t in recipe.step_templates:
        missing: set = set()
        args = render(t.get("args"), values, missing)
        known = _tool_known(str(t["tool"]))
        status = "resolved" if known and not missing else "unresolved"
        reason = []
        if missing:
            reason.append("no value for " + ", ".join(sorted(missing)))
        if not known:
            reason.append("tool not found in the catalogue")
        report: Dict[str, Any] = {"index": t.get("index"), "tool": t["tool"], "status": status,
                                  "reason": "; ".join(reason), "args": args}
        text = _literal_text(args, case2_text)
        for name, p in by_name.items():
            if name in values and _same(values[name], case1.get(name)):
                continue
            if _leaks_in(text, case1.get(name), p.get("kind", "text")):
                leaks.append({"step": t.get("index"), "tool": t["tool"], "param": name,
                              "value": case1.get(name)})
        if executor is not None and status == "resolved":
            try:
                report["output"] = executor(str(t["tool"]), args if isinstance(args, dict) else {"content": args})
            except Exception as exc:  # noqa: BLE001
                report["status"] = "failed"
                report["reason"] = f"{type(exc).__name__}: {exc}"
        step_reports.append(report)
        rendered_all.append({"tool": t["tool"], "args": args})

    untested = sorted(n for n in values if _same(values[n], case1.get(n)))
    resolved = sum(1 for r in step_reports if r["status"] in ("resolved",))
    ok = (not problems and resolved == len(step_reports) and not leaks and not untested)
    result = {
        "recipe_id": recipe.id, "version": recipe.version, "fingerprint": _fingerprint(recipe),
        "executed": executor is not None, "params": values, "unknown_params": unknown,
        "problems": problems,
        "steps": step_reports, "resolved": resolved, "total": len(step_reports),
        "leaks": leaks, "untested_params": untested, "promotable": ok,
        "outputs": rendered_all,
    }
    if not ok:
        result["why_not_promotable"] = (
            problems + [f"step {r['index']} {r['status']}: {r['reason']}" for r in step_reports
                        if r["status"] != "resolved"]
            + [f"literal of the first case still in step {l['step']} ({l['param']}={_short(l['value'])})" for l in leaks]
            + ([f"second case did not change: {', '.join(untested)}"] if untested else []))
    if record:
        recipe.tries.append({"at": time.time(), "fingerprint": result["fingerprint"], "params": values,
                             "resolved": resolved, "total": len(step_reports), "leaks": len(leaks),
                             "untested": untested, "promotable": ok, "executed": executor is not None})
        recipe.tries = recipe.tries[-10:]
        recipes_store.save_recipe(owner, recipe)
    return result


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return float(a) == float(b)
    return str(a) == str(b)


# ---------------------------------------------------------------------------
# promote and search
# ---------------------------------------------------------------------------

def promote(recipe_id: str, owner: str) -> Dict[str, Any]:
    recipe = recipes_store.get_recipe(recipe_id, owner)
    if recipe is None or not recipe.step_templates:
        raise RecipeLearningError("recipes.not_found", f"no learned recipe {recipe_id!r}")
    if recipe.status == "published":
        raise RecipeLearningError("recipes.already_published", "that recipe is already promoted")
    if not recipe.tries:
        raise RecipeLearningError("recipes.not_tried", "run recipe_try on a second case first")
    last = recipe.tries[-1]
    if not last.get("promotable"):
        raise RecipeLearningError("recipes.try_not_clean",
                                  "the last second-case run left unresolved steps, leaked literals or reused values")
    if last.get("fingerprint") != _fingerprint(recipe):
        raise RecipeLearningError("recipes.changed_since_try", "the recipe changed after the last try; try it again")
    recipe.status = "published"
    recipe.version = "1.0.0"
    recipe.source = {**(recipe.source or {}), "promoted_at": time.time()}
    recipes_store.save_recipe(owner, recipe)
    skill = _add_to_skill_library(recipe, owner)
    return {"recipe": recipe.to_dict(), "skill": skill}


def _add_to_skill_library(recipe: Recipe, owner: str) -> Dict[str, Any]:
    try:
        from src.skill_library import _skills_manager
        mgr = _skills_manager()
        procedure = [f"{i}. {s}" for i, s in enumerate(recipe.steps, 1)]
        record = mgr.add_skill(
            name=f"recipe-{_slug(recipe.title)}", title=recipe.title,
            description=f"Learned recipe: {recipe.title}. Parameters: "
                        + ", ".join(p["name"] for p in recipe.params),
            when_to_use=recipe.examples[0].get("request", "") if recipe.examples else recipe.title,
            procedure=procedure, tags=["recipe", f"recipe:{recipe.id}"], category="recipes",
            source="user", owner=owner, status="published", dedupe=False,
            verification=recipe.success_conditions)
        return {"name": record.get("name"), "added": True}
    except Exception as exc:  # noqa: BLE001 - the recipe is promoted either way
        logger.debug("[recipe_learning] skill library add failed", exc_info=True)
        return {"added": False, "reason": f"{type(exc).__name__}: {exc}"}


def search(query: str, owner: str, *, limit: int = 10) -> List[Dict[str, Any]]:
    """Recipes (built-in, drafts and promoted) matching ``query``, lexically,
    with the same tokenizer the skill search uses."""
    from services.memory.skills import _jaccard, _tokenize
    q = _tokenize(query)
    if not q:
        return []
    hits = []
    for r in recipes_store.list_recipes(owner):
        text = " ".join([r.title, r.id, " ".join(r.steps), " ".join(r.inputs), " ".join(r.tools),
                         " ".join(p.get("name", "") for p in r.params),
                         " ".join(e.get("request", "") for e in r.examples)])
        toks = _tokenize(text)
        overlap = len(q & toks) / max(1, len(q))
        score = max(overlap, _jaccard(q, toks))
        if score > 0:
            hits.append((score, r))
    hits.sort(key=lambda t: -t[0])
    return [{"id": r.id, "title": r.title, "status": r.status, "tools": r.tools,
             "params": [p["name"] for p in r.params], "score": round(s, 3)} for s, r in hits[:limit]]
