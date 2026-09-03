"""
workers_server.py — "Faustus workers" MCP server for an OUTSIDE coordinator.

Add it to Claude Desktop / Cowork / Claude Code and the expensive model can
hand mechanical work to Faustus's local workers (Ollama on this machine) and
read back a compact result — it never sees a tool transcript, so its own
tokens go to planning and review:

    {
      "mcpServers": {
        "faustus-workers": {
          "command": "D:/LocalAI/odysseus/venv/Scripts/python.exe",
          "args": ["D:/LocalAI/odysseus/mcp_servers/workers_server.py"],
          "env": {"FAUSTUS_URL": "http://127.0.0.1:7000",
                  "FAUSTUS_API_TOKEN": "ody_...",      # Settings → API tokens → profile "fable_workers"
                  "FAUSTUS_MCP_FORMAT": "toon"}        # toon (default) | text
        }
      }
    }

Tools: workers_guide (read first), dispatch_workers (start a job),
workers_wait (block until done, then the compact result), workers_status,
workers_events, workers_cancel, workers_list, objectives_list/objectives_apply,
guard_explain, and memory_pack (what this machine has already learned). It
talks HTTP to the running
Faustus (routes/dispatch_routes.py) — nothing runs in this process, so a crash
here cannot take a worker with it. A dispatch carries an Idempotency-Key, so
the one retry after a connection error can never start a second job.

**Output format.** The tools whose answer is ROWS — `objectives_list`,
`guard_explain`, `workers_status` — ask the endpoint for `?format=toon` and
hand the coordinator that text as it comes: the standard envelope
(src/robot_envelope.py) carrying the lean projection of the payload
(src/robot_projection.py) in TOON, where a uniform array is one header line
plus one line per row instead of every key repeated per row. Measured end to
end against the plain JSON body of the same read: 0.40 for `workers_status`
and 0.41 for `objectives_list` (tests/test_robot_projection.py). The
projection is lossy by design — it drops what a coordinator does not act on,
such as the task instructions it sent itself — never summarised prose.
`guard_explain` has no projection: one classification of one command is
already scalars, so it travels as it stands.
``FAUSTUS_MCP_FORMAT=text`` goes back to the human wording below, which is
also the automatic fallback whenever an older Faustus (or a hiccup) does not
answer the robot-mode call. The tools whose answer is NOT rows keep their
human rendering in both modes: `memory_pack` returns a prose block of learned
rules (TOON would escape its newlines into one line), and `workers_events`
renders a deliberate tail — the last 80 of up to 400 events, clipped — which
passing the raw answer through would undo. Tool names and arguments never
change with the format.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# stdout belongs to the JSON-RPC stream: one print() from the app code this
# server imports would corrupt it and kill the session. The guard
# (src/stdio_guard.py) sends stdout writes to stderr while the session runs.
try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("faustus-workers")

BASE = (os.environ.get("FAUSTUS_URL") or "http://127.0.0.1:7000").rstrip("/")
TOKEN = (os.environ.get("FAUSTUS_API_TOKEN") or "").strip()
_TIMEOUT = 30.0
_MAX_WAIT = 600.0
DEFAULT_FORMAT = "toon"


def mcp_format() -> str:
    """"toon" (default — the compact envelope the endpoints render) or "text"
    (the human wording below). Read per call so the operator can flip it
    without rebuilding anything."""
    value = (os.environ.get("FAUSTUS_MCP_FORMAT") or DEFAULT_FORMAT).strip().lower()
    return "text" if value == "text" else "toon"


def _text_request(method: str, path: str, timeout: float = _TIMEOUT) -> str:
    """The endpoint's own body, decoded and unparsed — for the robot-mode reads
    whose TOON text is handed to the coordinator as it comes."""
    return _request(method, path, None, timeout, as_text=True)


def _toon(path: str, timeout: float = _TIMEOUT) -> Optional[str]:
    """The robot-mode answer of `path` as TOON text, or None when this Faustus
    does not do robot mode (or anything else went wrong) — the caller then
    renders the human form instead."""
    if mcp_format() != "toon":
        return None
    joiner = "&" if "?" in path else "?"
    try:
        text = _text_request("GET", f"{path}{joiner}format=toon", timeout)
    except Exception:  # noqa: BLE001 - the human rendering is the fallback
        return None
    text = (text or "").strip()
    # An older Faustus ignores the parameter and answers JSON: only a body that
    # actually starts with the envelope's first line is TOON.
    return text if text.startswith("ok: ") else None


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = _TIMEOUT,
             retries: int = 1, as_text: bool = False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    idem = uuid.uuid4().hex if method == "POST" and path == "/api/dispatch" else None
    attempt = 0
    while True:
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/plain, application/json" if as_text else "application/json")
        if TOKEN:
            req.add_header("Authorization", f"Bearer {TOKEN}")
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — the operator's own server
                raw = resp.read().decode("utf-8") or "{}"
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:600]
            except Exception:
                pass
            hint = ""
            if e.code == 401:
                hint = (" — no FAUSTUS_API_TOKEN in this server's env" if not TOKEN
                        else " — the FAUSTUS_API_TOKEN is not accepted (revoked? another Faustus?)")
            elif e.code == 403:
                hint = " — the token needs the agents:dispatch scope (profile 'fable_workers') and an admin owner"
            raise RuntimeError(f"Faustus answered HTTP {e.code} for {method} {path}: {detail}{hint}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # one retry: a dispatch carries its Idempotency-Key, so a POST
            # that did go through returns the same job instead of a second one
            if attempt < retries and (method != "POST" or idem):
                attempt += 1
                continue
            reason = getattr(e, "reason", e)
            raise RuntimeError(f"Faustus is not reachable at {BASE}: {reason}")
    if as_text:
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Faustus returned non-JSON for {method} {path}")


def render(job: Dict[str, Any]) -> str:
    """The compact result as text the coordinator can read in one glance."""
    res = job.get("result") or {}
    status = str(job.get("status") or "")
    lines = [f"job {job.get('id')} · {status} · {job.get('title') or ''}".rstrip(" ·")]
    if job.get("verdict"):
        lines.append(f"verdict: {job['verdict']}")
    if job.get("error"):
        lines.append(f"error: {job['error']}")
    if job.get("workspace"):
        lines.append(f"workspace: {job['workspace']} · model: {job.get('model') or '?'} · {job.get('duration_s')} s")
    if job.get("chat_url"):
        lines.append(f"board: {BASE}{job['chat_url']}")
    if status in ("interrupted", "cancelled", "cancelling"):
        lines.append("this job did not finish — read the changes below, then re-dispatch the remaining work as a narrower task")
    if job.get("phase") and status in _LIVE_STATUSES:
        lines.append(f"phase: {job['phase']}")
    prog = job.get("progress") or {}
    if prog and status in _LIVE_STATUSES:
        for name, p in prog.items():
            bits = [f"{name}: {p.get('last_event')}"]
            if p.get("round") is not None:
                bits.append(f"round {p['round']}")
            if p.get("last_tool") or p.get("tool"):
                bits.append(f"tool {p.get('last_tool') or p.get('tool')}")
            if p.get("elapsed_s") is not None:
                bits.append(f"{p['elapsed_s']} s")
            if p.get("stalled"):
                bits.append(f"STALLED ({p.get('stall_reason') or '?'})")
            lines.append("  " + " · ".join(str(b) for b in bits))
    if status in _LIVE_STATUSES and job.get("wait_again"):
        lines.append(f"still running — call workers_wait again (up to {job.get('ceiling_s') or '?'} s more); do not re-dispatch")
    ch = res.get("changes")
    if isinstance(ch, dict):
        parts = [f"{k} " + ", ".join(ch.get(k) or []) for k in ("added", "modified", "deleted") if ch.get(k)]
        lines.append(f"changed on disk ({ch.get('source')}): " + ("; ".join(parts) if parts else "nothing")
                     + (" (list truncated)" if ch.get("truncated") else ""))
        git = ch.get("git") or {}
        if git.get("shortstat"):
            lines.append(f"  git now: {git['shortstat']}")
    if res.get("claimed_only"):
        lines.append("claimed but NOT changed: " + ", ".join(res["claimed_only"]))
    v = res.get("verification")
    if isinstance(v, dict):
        if v.get("ran"):
            state = "passed" if v.get("ok") else ("inconclusive" if v.get("inconclusive") else "FAILED")
            extra = f"{v.get('command')}" + (f", {v['attempts']} attempts" if v.get("attempts") else "")
            lines.append(f"verification: {state} — {v.get('summary')} ({extra})")
            for f in (v.get("failures") or [])[:8]:
                pre = " (pre-existing)" if f in (v.get("pre_existing") or []) else ""
                lines.append(f"  - {f}{pre}")
            if not v.get("ok") and v.get("output_tail"):
                lines.append("  output tail: " + str(v["output_tail"])[-600:].replace("\n", "\n    "))
        else:
            lines.append(f"verification: not run — {v.get('summary')}")
    for w in res.get("workers") or []:
        head = (f"[{w.get('name')}] {w.get('status')}" + (f" ({w.get('stop_reason')})" if w.get("stop_reason") and w.get("stop_reason") != "complete" else "")
                + f" · {w.get('rounds')} rounds · {w.get('tool_calls')} tools ({w.get('failed_calls')} failed)"
                + f" · {w.get('input_tokens')}/{w.get('output_tokens')} tok")
        lines.append(head)
        if w.get("error"):
            lines.append(f"  error: {w['error']}")
        if w.get("files_changed"):
            lines.append("  claims: " + ", ".join(w["files_changed"][:20]))
        sc = w.get("static_checks")
        if isinstance(sc, dict) and sc.get("failed"):
            lines.append(f"  static checks: {json.dumps(sc['failed'])[:300]}")
        elif sc and not isinstance(sc, dict):
            lines.append(f"  static checks: {json.dumps(sc)[:300]}")
        if w.get("summary"):
            lines.append("  says: " + w["summary"])
    if res.get("lock_conflicts"):
        lines.append("lock conflicts: " + "; ".join(res["lock_conflicts"]))
    if res.get("dropped_tasks"):
        lines.append(f"NOTE: {res['dropped_tasks']} task(s) were not run (max 4 per job) — dispatch them again.")
    t = res.get("totals") or {}
    if t:
        lines.append(f"totals: {t.get('tool_calls')} tool calls, {t.get('rounds')} rounds, "
                     f"{t.get('input_tokens')}/{t.get('output_tokens')} local tokens, {t.get('errors')} errors")
    return "\n".join(lines)


_LIVE_STATUSES = ("queued", "running", "verifying", "cancelling")


def _resolve_project(ident: str) -> Dict[str, Any]:
    """Resolve a project by id, sidebar folder or name (case-insensitive)."""
    ident = (ident or "").strip()
    if not ident:
        raise RuntimeError("give the project id, its sidebar folder, or its name")
    rows = _request("GET", "/api/projects")
    if not isinstance(rows, list):
        rows = rows.get("projects") or []
    key = ident.casefold()
    for row in rows:
        if str(row.get("id") or "") == ident:
            return row
    for row in rows:
        if str(row.get("folder") or "").strip().casefold() == key \
                or str(row.get("name") or "").strip().casefold() == key:
            return row
    known = ", ".join(f"{r.get('name')} ({r.get('id')})" for r in rows) or "none"
    raise RuntimeError(f"no project matches '{ident}' — known projects: {known}")


def render_objectives(project: Dict[str, Any], data: Dict[str, Any]) -> str:
    """The objectives dashboard as text the coordinator can read in one glance."""
    objs = data.get("objectives") or []
    scores = data.get("scores") or {}
    lines = [f"project {project.get('name')} ({project.get('id')}) · {len(objs)} objective(s)"]
    for o in objs:
        oid = o.get("id")
        line = f"{oid} [{o.get('status')}] (P{o.get('priority')}) {o.get('title')}"
        deps = [d for d in o.get("deps") or []]
        if deps:
            line += " · deps: " + ", ".join(deps)
        s = scores.get(oid) or {}
        if s.get("score") is not None:
            line += f" · impact {s['score']}"
        if s.get("hint"):
            line += f" · HINT: {s['hint']}"
        lines.append(line)
    if not objs:
        lines.append("no objectives yet — objectives_apply with ADD deltas creates them")
    return "\n".join(lines)


def render_pack(data: Dict[str, Any]) -> str:
    """The learned-memory block exactly as a local worker would receive it."""
    block = str(data.get("pack") or "").strip()
    if not data.get("enabled", True):
        head = "learned memory: injection is OFF (Settings -> Agent -> Learned rules)"
    elif not block:
        return ("nothing learned yet for this scope — Faustus fills this store from turn "
                "outcomes, and the owner can add rules in the Brain page")
    else:
        head = f"learned memory · {data.get('chars')} of {data.get('budget')} chars"
    if data.get("degraded"):
        head += " · semantic lane unavailable (lexical retrieval only)"
    return head + ("\n" + block if block else "")


def render_apply(result: Dict[str, Any]) -> str:
    lines = []
    applied = result.get("applied") or []
    conflicts = result.get("conflicts") or []
    lines.append(f"{len(applied)} delta(s) applied · {len(conflicts)} conflict(s)")
    for a in applied:
        lines.append(f"  {a.get('op')} {a.get('id')} · {json.dumps(a.get('fields') or {}, ensure_ascii=False)[:200]}")
    for c in conflicts:
        lines.append(f"  CONFLICT {c.get('op') or '?'} {c.get('id') or ''}: {c.get('reason')}")
    for o in (result.get("state") or {}).get("objectives") or []:
        if o.get("status") != "dropped":
            lines.append(f"{o.get('id')} [{o.get('status')}] (P{o.get('priority')}) {o.get('title')}")
    return "\n".join(lines)


TOOLS: List[Tool] = [
    Tool(
        name="dispatch_workers",
        description=(
            "Hand mechanical work to Faustus's LOCAL workers (Ollama on this machine) and get back a compact "
            "result — never a transcript. Give 1–4 self-contained tasks (each an instruction, optionally files "
            "and a model), the workspace folder they are confined to (required), the command that proves the "
            "job is done (`verify`; Faustus runs it itself after the workers, auto-detects the test runner when "
            "omitted, and sends one fixer worker with the failure output when it fails), and whether the tasks "
            "may run in parallel. Returns the job id and the Faustus chat where the control board lives. Then "
            "call workers_wait (again, while it says wait_again)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array", "minItems": 1, "maxItems": 4,
                    "items": {"anyOf": [
                        {"type": "string"},
                        {"type": "object", "properties": {
                            "instruction": {"type": "string"},
                            "name": {"type": "string"},
                            "files": {"type": "array", "items": {"type": "string"}},
                            "model": {"type": "string"},
                        }, "required": ["instruction"]},
                    ]},
                    "description": "Self-contained tasks; say exactly what 'done' means (tests to pass, files to change).",
                },
                "workspace": {"type": "string", "description": "Absolute folder the workers are confined to (required)."},
                "context": {"type": "string", "description": "Shared context every worker gets (short)."},
                "verify": {"type": "string", "description": "Shell command run by Faustus in the workspace after the workers to prove the job ('pytest -q', 'npm test'…). 'auto' (default) detects the project's test runner; 'none' skips."},
                "verify_scope": {"type": "string", "enum": ["related", "all"], "default": "related",
                                 "description": "auto mode: the tests related to the changed files, or the whole suite."},
                "fix_rounds": {"type": "integer", "minimum": 0, "maximum": 4, "default": 1,
                               "description": "When the verification fails: at MOST how many times one fixer worker gets the failure output before Faustus gives up (status `partial`). Faustus stops earlier on its own when the rounds stop changing anything (convergence). The server clamps values above its own cap (2 with the convergence detector off, 4 with it on)."},
                "parallel": {"type": "boolean", "default": True},
                "reviewer": {"type": "boolean", "default": False, "description": "Add a reviewer worker after the others."},
                "model": {"type": "string", "description": "Model on the dispatch endpoint (default: the configured worker model)."},
                "max_rounds": {"type": "integer", "minimum": 3, "maximum": 40},
                "timeout_s": {"type": "integer", "minimum": 60, "maximum": 7200},
            },
            "required": ["tasks", "workspace"],
        },
    ),
    Tool(
        name="workers_wait",
        description="Block until a dispatched job finishes (up to timeout_s, default 300) and return its compact result: verdict, what changed on disk, the verification Faustus ran, and each worker's status. If it answers `still running`, call it again — never re-dispatch the same task.",
        inputSchema={"type": "object", "properties": {
            "job_id": {"type": "string"},
            "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600, "default": 300},
        }, "required": ["job_id"]},
    ),
    Tool(
        name="workers_status",
        description="Current status of a dispatched job (progress per worker while it runs; the compact result when done).",
        inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    Tool(
        name="workers_events",
        description="The control-board events of a job so far (last 400) — for diagnosing a stuck worker.",
        inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    Tool(
        name="workers_cancel",
        description="Stop a dispatched job.",
        inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    Tool(
        name="workers_guide",
        description="How to use these workers well: what to dispatch, how to write a task, how to read a result. Read it once per session before the first dispatch.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="workers_list",
        description="Recent dispatched jobs (id, status, title).",
        inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    ),
    Tool(
        name="objectives_list",
        description=(
            "The objectives dashboard of a Faustus project: every objective with status, priority, "
            "dependencies and a structural impact score. Mention the relevant OBJ-id in dispatched task "
            "instructions so the outcome is recorded as evidence on that objective."
        ),
        inputSchema={"type": "object", "properties": {
            "project": {"type": "string", "description": "Project id, sidebar folder or name."},
        }, "required": ["project"]},
    ),
    Tool(
        name="guard_explain",
        description=(
            "Pre-check a shell command against Faustus's destructive-command guard BEFORE dispatching "
            "workers that would run it: returns the tier (SAFE/CAUTION/DANGEROUS/CRITICAL), the rule "
            "that matched, whether an allowlist entry covers it, the current guard mode, and the full "
            "classification trace. DANGEROUS/CRITICAL commands stop for a per-command approval card in "
            "enforce mode, so a coordinator should either avoid them, allowlist a reviewed pattern, or "
            "expect the run to wait for the owner."
        ),
        inputSchema={"type": "object", "properties": {
            "command": {"type": "string", "description": "The exact command to classify."},
        }, "required": ["command"]},
    ),
    Tool(
        name="memory_pack",
        description=(
            "What Faustus has LEARNED and would put in a local worker's prompt right now: the "
            "procedural rules it scored from earlier turn outcomes, the memories relevant to a "
            "query, and the anti-patterns it inverted after they kept causing failures. Read it "
            "before writing task instructions so you do not re-teach what this machine already "
            "knows — or contradict a rule it learned the hard way."
        ),
        inputSchema={"type": "object", "properties": {
            "project": {"type": "string", "description": "Project id, sidebar folder or name; omit for the unscoped rules."},
            "query": {"type": "string", "description": "What the work is about; drives the 'Relevant memories' section."},
        }},
    ),
    Tool(
        name="objectives_apply",
        description=(
            "Update a Faustus project's objectives with TYPED DELTAS (never a rewrite). Each delta: "
            "{op: ADD|EDIT|KILL, id (EDIT/KILL), title, status: open|in_progress|blocked|done|dropped, "
            "priority: 1-4 (1 highest), notes, deps: [OBJ-ids], rationale}. Conflicts are reported "
            "in-band; valid deltas in the same batch still apply."
        ),
        inputSchema={"type": "object", "properties": {
            "project": {"type": "string", "description": "Project id, sidebar folder or name."},
            "deltas": {"type": "array", "minItems": 1, "items": {"type": "object"},
                       "description": "Typed ADD/EDIT/KILL deltas as described above."},
        }, "required": ["project", "deltas"]},
    ),
]


@server.list_tools()
async def list_tools() -> List[Tool]:
    return TOOLS


def _text(s: str) -> List[TextContent]:
    return [TextContent(type="text", text=s)]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    args = arguments or {}
    try:
        if name == "objectives_list":
            project = await asyncio.to_thread(_resolve_project, str(args.get("project") or ""))
            path = f"/api/projects/{project.get('id')}/objectives"
            compact = await asyncio.to_thread(_toon, path)
            if compact:
                return _text(f"project {project.get('name')} ({project.get('id')})\n{compact}")
            data = await asyncio.to_thread(_request, "GET", path)
            return _text(render_objectives(project, data))
        if name == "objectives_apply":
            project = await asyncio.to_thread(_resolve_project, str(args.get("project") or ""))
            body = {"deltas": args.get("deltas") or []}
            result = await asyncio.to_thread(
                _request, "POST", f"/api/projects/{project.get('id')}/objectives/deltas", body)
            return _text(render_apply(result))
        if name == "memory_pack":
            project = str(args.get("project") or "")
            if project:
                # A known project resolves to the folder the store scopes by;
                # anything else is passed through as a literal scope filter.
                try:
                    row = await asyncio.to_thread(_resolve_project, project)
                    project = str(row.get("workspace") or row.get("folder") or project)
                except RuntimeError:
                    pass
            query = urllib.parse.urlencode({"project": project,
                                            "query": str(args.get("query") or "")})
            # Always the human form: /pack answers with a PROSE block of rules
            # the worker would be given, not rows — TOON would fold its
            # newlines into one escaped line and cost more, not less.
            data = await asyncio.to_thread(_request, "GET", f"/api/memory-engine/pack?{query}")
            return _text(render_pack(data))
        if name == "guard_explain":
            command = str(args.get("command") or "")
            if not command.strip():
                return _text("Error: give the exact command to classify")
            quoted = urllib.parse.quote(command, safe="")
            path = f"/api/command-guard/explain?command={quoted}"
            compact = await asyncio.to_thread(_toon, path)
            if compact:
                return _text(compact)
            data = await asyncio.to_thread(_request, "GET", path)
            lines = [
                f"tier: {data.get('tier')} · rule: {data.get('rule_id') or '-'} · mode: {data.get('mode')}",
                f"command: {data.get('command_head')}",
            ]
            if data.get("matched"):
                lines.append(f"matched: {data['matched']}")
            if data.get("allowlisted"):
                entry = data["allowlisted"]
                lines.append(
                    f"allowlisted: {entry.get('kind')} {entry.get('pattern')!r}"
                    + (f" (reason: {entry.get('reason')})" if entry.get("reason") else "")
                )
            if data.get("fail_open"):
                lines.append("NOTE: classification hit its budget — fail-open verdict")
            for step in data.get("trace") or []:
                lines.append("  " + str(step)[:200])
            lines.append(f"rules tested: {data.get('rules_tested')} · packs: {', '.join(data.get('packs') or [])}")
            return _text("\n".join(lines))
        if name == "dispatch_workers":
            body = {k: v for k, v in args.items() if v is not None}
            job = await asyncio.to_thread(_request, "POST", "/api/dispatch", body)
            return _text(render(job) + "\n(call workers_wait with this job_id)")
        job_id = str(args.get("job_id") or "").strip()
        if name == "workers_wait":
            t = min(_MAX_WAIT, max(5.0, float(args.get("timeout_s") or 300)))
            job = await asyncio.to_thread(_request, "GET", f"/api/dispatch/{job_id}/wait?timeout={int(t)}", None, t + 15)
            return _text(render(job))
        if name == "workers_status":
            compact = await asyncio.to_thread(_toon, f"/api/dispatch/{job_id}")
            if compact:
                return _text(compact)
            return _text(render(await asyncio.to_thread(_request, "GET", f"/api/dispatch/{job_id}")))
        if name == "workers_events":
            # Not the robot-mode body: this render is a deliberate TAIL (the
            # last 80 of up to 400 events, 300 chars each). Passing the
            # endpoint's answer through would hand the coordinator five times
            # the events untruncated — the opposite of the point.
            data = await asyncio.to_thread(_request, "GET", f"/api/dispatch/{job_id}/events")
            evs = data.get("events") or []
            lines = [f"job {data.get('id')} · {data.get('status')} · {len(evs)} events"]
            for ev in evs[-80:]:
                lines.append("  " + json.dumps(ev, ensure_ascii=False)[:300])
            return _text("\n".join(lines))
        if name == "workers_cancel":
            data = await asyncio.to_thread(_request, "POST", f"/api/dispatch/{job_id}/cancel", {})
            return _text(f"job {data.get('id')}: {'cancelled' if data.get('cancelled') else 'was not running'} ({data.get('status')})")
        if name == "workers_guide":
            data = await asyncio.to_thread(_request, "GET", "/api/dispatch/guide")
            return _text(str(data.get("guide") or ""))
        if name == "workers_list":
            data = await asyncio.to_thread(_request, "GET", f"/api/dispatch?limit={int(args.get('limit') or 20)}")
            rows = data.get("jobs") or []
            if not rows:
                return _text("no dispatched jobs yet")
            return _text("\n".join(f"{j.get('id')} · {j.get('status')} · {j.get('title')}" for j in rows))
        return _text(f"unknown tool: {name}")
    except Exception as e:  # noqa: BLE001 — the coordinator needs the reason, not a stack
        return _text(f"Error: {e}")


async def main() -> None:
    # The guard goes up INSIDE stdio_server(): that context manager wraps the
    # real sys.stdout.buffer when it is entered, so the protocol keeps the
    # handle and everything else is diverted to stderr.
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
