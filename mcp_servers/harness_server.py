"""harness_server.py

MCP server with four read-only diagnostics over the agent harness, so a
local assistant (or any MCP client) can see what the harness would do without
running a turn:

* ``history_projection`` - give a message list and a wire protocol; get back the
  repairs the canonical projection would make (orphan calls, missing results,
  legacy markers) with a receipt for each, and a compact view of the projected
  history. Calls without a result are shown as explicit "no result" items, never
  removed.
* ``resource_claims`` - give tool calls; get back what each one would claim
  (read or write on a path, tree, workspace, process or external resource) and
  which of them may run together and which are serialised.
* ``paired_bench_reports`` - list the paired harness bench reports (same model,
  before vs after) or read one: verdict, success, seconds, tokens and boundary
  violations per arm and case.
* ``tool_footprint`` - what the built-in tool catalogue (or a supplied tool
  list) costs in the prompt: tokens per tool, source and exposure, the
  heaviest tools, name collisions and near-duplicate descriptions.

* ``spend_cap_status`` - the daily paid-spend cap: limits, what was spent today
  (USD and tokens), calls without a reported cost, per provider, reset time.

* ``injection_eval_run`` / ``injection_eval_report`` - the adversarial prompt-injection
  battery (evals/injection, 40+ cases: web pages, files, MCP results, mail, PDFs,
  calendar, clipboard, multi-step, encoded text, authority claims, exfiltration,
  tool coercion). The default run is deterministic: a scripted model that obeys every
  injection is judged by the real gates, and no real tool is ever dispatched. A live run
  against a real model is opt-in (``live: true`` plus endpoint and model).

* ``harness_proposals`` - list or read the refinement proposals the assistant
  made after a task (diff, rationale, evidence, status). Approving, rejecting
  and undoing stay human actions in Studio or the REST routes.

* ``voice_turn_timeline`` - the latency timeline of voice turns (speech end,
  transcript, first token, first audio) with p50/p90 per gap.

* ``think_mode_calibration`` - how Auto's reasoning-mode choices held up: per
  mode, source and rule reason, how often the next message was a correction
  or the turn ended empty, and the reasons corrected well above the average.
  Owner-scoped: ODYSSEUS_MCP_HARNESS_OWNER (or the context/brain owner), or
  the only owner the database has.

* ``cookbook_job_log`` - the command, exit code and output (stdout/stderr) of a
  Cookbook download or serve job.

Apart from the injection battery (which only asks the gates and saves its report under
DATA_DIR/injection_evals), nothing here writes anything or starts a run. The in-process receipt log and
claim broker of the running app are not reachable from this separate process;
these tools compute from what they are given, and read the reports on disk.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported, like the other built-in servers.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("harness")

_MAX_MESSAGES = 400
_MAX_CALLS = 64
_PREVIEW_CHARS = 160


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload: dict) -> list[TextContent]:
    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="degradation_status",
            description=(
                "Which dependencies Faustus is running without right now: vector store, web search "
                "(SearXNG), notifications (ntfy), email, model endpoints, Sparks and MCP servers. Each "
                "item has state (ok, degraded, down, unconfigured), the cause (error class and short "
                "message, no secrets), since when, latency, a fix hint in Spanish and English and the "
                "features that degrade. Uses the running app's last report when it is recent, otherwise "
                "probes what it can from this process. Informational and read-only. Estado de degradacion "
                "de las dependencias."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "refresh": {"type": "boolean", "description": "Ignore the app's snapshot and probe now."},
                    "only_problems": {"type": "boolean", "description": "Return only degraded or down items."},
                },
            },
        ),
        Tool(
            name="history_projection",
            description=(
                "Show what the harness would change in a chat history before sending it to a "
                "model. Give the messages (role/content, assistant tool_calls, tool results with "
                "tool_call_id) and a protocol. Returns one receipt per repair (kind, reason, call "
                "id, tool) and a compact view of the projected messages. A tool call with no "
                "result is kept and shown with an explicit unknown-result item; a result that "
                "arrives late is re-attached to its call; nothing is invented. Muestra que "
                "repararia el proyector de historial. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "messages": {"type": "array", "items": {"type": "object"},
                                 "description": "Chat messages, oldest first."},
                    "protocol": {"type": "string", "enum": ["openai_chat", "anthropic", "ollama", "text_fence"],
                                 "description": "Wire protocol to project for (default openai_chat)."},
                },
                "required": ["messages"],
            },
        ),
        Tool(
            name="resource_claims",
            description=(
                "Show what a set of tool calls would claim and how the dispatcher would group "
                "them. Each call is {tool, content}: content is the tool's JSON arguments as a "
                "string. Returns the claims of each call (read or write on a path, tree, "
                "workspace, process or external resource) and the groups: calls in one group may "
                "run in parallel, a call that conflicts with an earlier one (any write on an "
                "overlapping resource) starts a later group. Shell commands and calls with "
                "unknown effects claim nothing. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "calls": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"tool": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["tool"]}},
                    "workspace": {"type": "string", "description": "Workspace root the paths are relative to."},
                    "scope": {"type": "string", "enum": ["files", "files_and_external", "off"],
                              "description": "Which resources are claimed (default: the setting, normally files)."},
                },
                "required": ["calls"],
            },
        ),
        Tool(
            name="paired_bench_reports",
            description=(
                "List the paired harness bench reports (same model, same tasks, two arms: a git "
                "revision or a settings toggle) or read one. A report holds the verdict "
                "(faster / equivalent / slower / regression / blocked / inconclusive), success "
                "rate, rounds, seconds, tokens and boundary violations per arm and case, and the "
                "recovery scenarios. Without `file` the newest reports are listed. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Report file name from the listing."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                              "description": "How many reports to list (default 10)."},
                },
            },
        ),
        Tool(
            name="tool_footprint",
            description=(
                "What a tool catalogue costs in the prompt and where it overlaps: tokens per tool "
                "(measured on the function-call shape the model receives), totals per source and "
                "per exposure (deferred tools are not offered up front), the heaviest tools, "
                "descriptions worth trimming, the same short name published by several servers, "
                "and near-duplicate descriptions. Without `tools` it audits the built-in catalogue; "
                "with `tools` (an MCP tools/list or OpenAI tool list, each item optionally with "
                "`server`) it audits that list. Read-only, no model."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tools": {"type": "array", "items": {"type": "object"},
                              "description": "Optional catalogue to audit instead of the built-in one."},
                    "top": {"type": "integer", "minimum": 1, "maximum": 200,
                            "description": "How many heaviest tools to list (default 15)."},
                    "similarity": {"type": "number", "minimum": 0.1, "maximum": 1,
                                   "description": "Description similarity for near duplicates (default 0.6)."},
                },
            },
        ),
        Tool(
            name="harness_proposals",
            description=(
                "List or read the harness refinement proposals: the smallest edits the assistant "
                "suggested after a task to its project instructions, a skill, a memory or a "
                "sub-agent spec. Without `id` the newest proposals are listed (axis, target, op, "
                "status, rationale, diff) with counts per status; with `id` one proposal comes "
                "back with its exact before/after and its trigger-to-result log. Approving, "
                "rejecting and undoing are human actions in Studio (Skills > Harness proposals) "
                "or the REST routes; this tool cannot apply anything. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Proposal id from the listing."},
                    "status": {"type": "string", "enum": ["pending", "applied", "rejected", "undone"],
                               "description": "Only proposals in this state."},
                    "axis": {"type": "string", "enum": ["prompt_layer", "skill", "memory", "subagent_spec"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                              "description": "How many to list (default 10)."},
                },
            },
        ),
        Tool(
            name="doc_lint",
            description=(
                "Lint Markdown documents with checkable rules, report-only (it never rewrites): "
                "heading hierarchy (skipped levels, several H1, duplicates), numbered-section "
                "sequence (gaps, duplicates, out of order), broken relative links and backticked "
                "repo paths, impossible or malformed dates (DD-MM-YYYY, YYYY-MM-DD), required "
                "sections per document, and forbidden content (commit trailers, banned names from "
                "the config). Each finding has rule, severity, line, message and fix hint. Paths "
                "must be Markdown files inside the Faustus install folder or the server's working "
                "folder. Revisa documentos Markdown. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"},
                              "description": "Markdown files to lint."},
                    "rules": {"type": "array", "items": {"type": "string"},
                              "description": "Rule ids or groups: headings, numbering, links, dates, "
                                             "sections, forbidden, whitespace (default: all but whitespace)."},
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="spend_cap_status",
            description=(
                "The daily hard cap on spend with paid remote providers: whether it is on, the "
                "dollar and token limits, what was spent today (USD and tokens), how many paid "
                "calls reported no cost (counted by tokens only), whether the cap has been "
                "reached, when it resets (local midnight) and the split per provider. Local "
                "models are never counted. Limite diario de gasto en proveedores de pago. "
                "Read-only; change it in Settings or PATCH /api/spend-cap."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="injection_eval_run",
            description=(
                "Run the adversarial prompt-injection battery: 40+ cases where outside content (web "
                "pages, files, MCP results, emails, PDFs, calendar invites, clipboard, multi-step, "
                "hidden or encoded text, fake authority, urgency, exfiltration URLs, tool coercion) "
                "tries to make the agent delete files, send mail, change settings, lift read-only or "
                "raise the paid spend cap. Default: deterministic, a scripted model that obeys every "
                "injection is judged by the real gates; nothing is dispatched and no model is used. "
                "Returns totals, violated case ids and open known holes plus a report id. Set live=true "
                "with base_url, endpoint_url and model to score a real configured model through a "
                "running server (off by default). Corre la bateria de inyeccion de instrucciones."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "case_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "Only these cases (default: all)."},
                    "live": {"type": "boolean", "description": "Use a real model (default false)."},
                    "posture": {"type": "string", "enum": ["baseline", "configured"],
                                "description": "Deterministic runs: baseline pins approvals to ask and measures "
                                               "the defenses (default); configured uses the current settings, i.e. "
                                               "what an injection could do with this install's approval mode."},
                    "base_url": {"type": "string", "description": "Running Faustus server (live only)."},
                    "endpoint_url": {"type": "string", "description": "Model endpoint (live only)."},
                    "model": {"type": "string", "description": "Model name (live only)."},
                    "timeout": {"type": "number", "description": "Seconds per case (live only, default 180)."},
                },
            },
        ),
        Tool(
            name="injection_eval_report",
            description=(
                "Read the newest prompt-injection battery report (or one by job_id): totals, per "
                "case result, the forbidden calls attempted and the defense that stopped each, the "
                "open known holes, and the transcript of any case. format=markdown returns the "
                "readable table. Lee el ultimo informe de la bateria de inyeccion. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Report of this job (default: newest)."},
                    "mode": {"type": "string", "enum": ["deterministic", "live"]},
                    "case_id": {"type": "string", "description": "Only this case, with its transcript."},
                    "format": {"type": "string", "enum": ["json", "markdown"], "description": "Default json."},
                },
            },
        ),
        Tool(
            name="voice_turn_timeline",
            description=(
                "Latency timeline of voice conversation turns: for each turn the instants and gaps "
                "end of speech -> transcript ready -> request sent -> first model token -> first "
                "speech chunk ready -> first audio played, plus the server's own speech-to-text, "
                "text-to-speech and time-to-first-token durations, and p50/p90 of every gap over "
                "the returned turns. Give a session_id for one conversation, or none for the most "
                "recent turns everywhere. Timestamps and ids only, never any conversation text. "
                "Muestra donde se va el tiempo en cada turno de voz. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Only this chat session."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                              "description": "How many recent turns (default 20)."},
                },
            },
        ),
        Tool(
            name="think_mode_calibration",
            description=(
                "How well the Auto reasoning mode chose fast, light, think or deep in recent chats. "
                "Pairs every answered turn with the person's next message and counts, per mode, per "
                "source (rule, explicit, override) and per rule reason, how many were followed by a "
                "correction ('no, eso esta mal', 'try again') or ended without an answer. 'flags' lists "
                "the reasons or modes corrected well above the Auto average, to tune thresholds by hand. "
                "Read-only. Calibracion del modo de razonamiento Auto."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 365,
                             "description": "Look back this many days (default 30)."},
                },
            },
        ),
        Tool(
            name="cookbook_job_log",
            description=(
                "Read the log of a Cookbook job (a model download, serve or install): the exact "
                "command and launcher argv, working directory, environment keys (secret values "
                "redacted), start/end times, status and exit code, and the last lines of its "
                "output. stream is combined (default), stdout or stderr; in combined, lines that "
                "came from stderr are flagged. Without job_id it lists the recent jobs. "
                "Lee el comando, el codigo de salida y la salida de un trabajo del Cookbook. "
                "Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Session id, e.g. serve-1a2b3c4d. Omit to list recent jobs."},
                    "stream": {"type": "string", "enum": ["combined", "stdout", "stderr"]},
                    "tail": {"type": "integer", "minimum": 1, "maximum": 2000,
                             "description": "How many trailing lines (default 200)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                              "description": "How many jobs when listing (default 50)."},
                },
            },
        ),
    ]


def _preview(content) -> str:
    if isinstance(content, list):
        content = " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    text = "" if content is None else str(content)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "..."


def _history_projection(args: dict) -> dict:
    from src import history_projection as hp

    messages = args.get("messages")
    if not isinstance(messages, list):
        return {"error": "messages must be a list"}
    if len(messages) > _MAX_MESSAGES:
        return {"error": f"at most {_MAX_MESSAGES} messages per call"}
    protocol = str(args.get("protocol") or "openai_chat")
    if protocol not in hp.PROTOCOLS:
        return {"error": f"unknown protocol {protocol!r}; expected one of {list(hp.PROTOCOLS)}"}
    proj = hp.project(messages, protocol, record=False)
    view = []
    for m in proj.messages:
        if not isinstance(m, dict):
            continue
        row = {"role": m.get("role"), "content": _preview(m.get("content"))}
        if m.get("tool_calls"):
            row["tool_calls"] = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name")}
                                 for c in m["tool_calls"] if isinstance(c, dict)]
        if m.get("tool_call_id"):
            row["tool_call_id"] = m["tool_call_id"]
        view.append(row)
    kinds: dict = {}
    for r in proj.receipts:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    return {"protocol": protocol, "messages_in": len(messages), "messages_out": len(proj.messages),
            "repair_count": len(proj.receipts), "repairs_by_kind": kinds,
            "repairs": proj.receipt_dicts(), "projected": view}


def _resource_claims(args: dict) -> dict:
    from src import resource_claims as rc

    calls = args.get("calls")
    if not isinstance(calls, list):
        return {"error": "calls must be a list"}
    if len(calls) > _MAX_CALLS:
        return {"error": f"at most {_MAX_CALLS} calls per request"}
    scope = str(args.get("scope") or rc.scope_setting())
    if scope not in ("files", "files_and_external", "off"):
        return {"error": f"unknown scope {scope!r}"}
    workspace = args.get("workspace") or None
    rows = []
    groups: list = []
    current = rc.ClaimSet()
    current_ids: list = []
    for i, call in enumerate(calls):
        if not isinstance(call, dict) or not call.get("tool"):
            return {"error": f"call {i} needs a tool name"}
        claims = rc.claims_for_call(str(call["tool"]), str(call.get("content") or ""), workspace, scope)
        rows.append({"index": i, "tool": call["tool"], "claims": [c.label() for c in claims],
                     "claims_nothing": not claims})
        if not current.try_add(claims):
            groups.append(current_ids)
            current, current_ids = rc.ClaimSet(), []
            current.try_add(claims)
        current_ids.append(i)
    if current_ids:
        groups.append(current_ids)
    return {"scope": scope, "calls": rows, "groups": groups,
            "parallel": any(len(g) > 1 for g in groups),
            "serialised": len(groups) > 1}


def _paired_bench_reports(args: dict) -> dict:
    from src.bench import harness_pair as hpair

    name = args.get("file")
    if name:
        name = str(name)
        if os.path.basename(name) != name or not name.endswith(".json"):
            return {"error": "file must be a report file name from the listing"}
        path = os.path.join(hpair.reports_dir(), name)
        try:
            report = hpair.load_report(path)
        except (OSError, ValueError) as exc:
            return {"error": f"cannot read {name}: {exc}"}
        report = dict(report)
        report["runs"] = [{k: r.get(k) for k in ("arm", "case", "repeat", "success", "rounds", "seconds",
                                                 "violations", "detail")} for r in report.get("runs", [])]
        return {"file": name, "summary": hpair.render_text(report), "report": report}
    try:
        limit = max(1, min(50, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    return {"reports": hpair.list_reports(limit), "directory": hpair.reports_dir()}


#: The built-in catalogue, loaded once before the stdio loop starts (see
#: `run`). On Windows, importing the agent tools while the JSON-RPC reader is
#: blocked on stdin hung the call: something in that import inspects the
#: standard handles, and a synchronous pipe read pending on stdin blocks that
#: inspection until the next message arrives -- which never comes, because
#: the client is waiting for this answer. Seen live on 08-10 with a raw
#: stdio client; in-process tests never hit it.
_BUILTIN_CATALOGUE = None


def _load_builtin_catalogue():
    global _BUILTIN_CATALOGUE
    from src import tool_footprint as tf
    from src.tool_registry import snapshot

    rows = snapshot()
    # Warm what a report touches lazily (estimator, tool authority) too.
    tf.default_counter("")("warm")
    tf.builtin_exposure(rows[0].name if rows else "")
    _BUILTIN_CATALOGUE = rows
    return _BUILTIN_CATALOGUE


def _tool_footprint(args: dict) -> dict:
    from src import tool_footprint as tf

    try:
        top = int(args.get("top") or tf.DEFAULT_TOP)
    except (TypeError, ValueError):
        top = tf.DEFAULT_TOP
    try:
        similarity = float(args.get("similarity") or tf.DEFAULT_SIMILARITY)
    except (TypeError, ValueError):
        similarity = tf.DEFAULT_SIMILARITY
    supplied = args.get("tools")
    if supplied is not None:
        if not isinstance(supplied, list):
            return {"error": "tools must be a list of tool objects"}
        rows = tf.rows_from_mappings(supplied[:2000])
        report = tf.footprint_report(rows, top=top, similarity=similarity, hidden_of=tf.native_twin)
        report["catalogue"] = "supplied"
        return report
    rows = _BUILTIN_CATALOGUE if _BUILTIN_CATALOGUE is not None else _load_builtin_catalogue()
    report = tf.footprint_report(rows, top=top, similarity=similarity,
                                 exposure_of=tf.builtin_exposure)
    report["catalogue"] = "builtin"
    report["note"] = ("Built-in tools only: this server runs in its own process and cannot see the "
                      "MCP servers connected to the app. GET /api/tools/footprint covers both.")
    return report


def _harness_proposals(args: dict) -> dict:
    from src.harness_refinement import store

    ident = args.get("id")
    if ident:
        record = store.get(str(ident))
        if not record:
            return {"error": f"no proposal {ident!r}"}
        full = {k: v for k, v in record.items() if k not in ("meta", "applied_content")}
        return {"proposal": full, "log": store.read_log(limit=20, proposal_id=str(ident))}
    try:
        limit = max(1, min(50, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    rows = store.list_proposals(status=args.get("status") or None, axis=args.get("axis") or None, limit=limit)
    light = [{k: v for k, v in r.items() if k not in ("before", "after", "meta", "applied_content")} for r in rows]
    return {"proposals": light, "counts": store.counts()}


def _doc_lint_roots() -> list:
    install = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.realpath(install), os.path.realpath(os.getcwd())]


def _doc_lint_resolve(raw: str) -> str:
    """Confine a path to the install folder or the server's cwd (no import of
    the agent tools here: see the note on `_BUILTIN_CATALOGUE`)."""
    roots = _doc_lint_roots()
    expanded = os.path.expanduser(str(raw))
    real = os.path.realpath(expanded if os.path.isabs(expanded) else os.path.join(roots[0], expanded))
    for root in roots:
        try:
            if os.path.commonpath([real, root]) == root:
                return real
        except ValueError:
            continue
    raise ValueError(f"path '{raw}' is outside the allowed folders")


def _doc_lint(args: dict) -> dict:
    from src import doc_lint as dl

    paths = args.get("paths") or args.get("path")
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",") if p.strip()]
    if not isinstance(paths, list) or not paths:
        return {"error": "paths must be a non-empty list of Markdown files"}
    if len(paths) > 20:
        return {"error": "at most 20 paths per call"}
    rules = args.get("rules") or None
    if isinstance(rules, str):
        rules = [r.strip() for r in rules.split(",") if r.strip()]
    try:
        return dl.lint_paths([str(p) for p in paths], rules, resolver=_doc_lint_resolve)
    except ValueError as exc:
        return {"error": str(exc)}

def _voice_turn_timeline(args: dict) -> dict:
    from src import voice_timeline as vt

    try:
        limit = max(1, min(200, int(args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    report = vt.session_report(str(args.get("session_id") or "") or None, limit)
    for turn in report["turns"]:
        turn.pop("owner", None)
    report["note"] = "Gaps are in milliseconds; null means a mark was missing. Client marks share one clock."
    return report


def _spend_cap_status(args: dict) -> dict:
    from src import spend_cap

    return spend_cap.status()


def _degradation_status(args: dict) -> dict:
    from src import degradation

    report = degradation.snapshot_or_check(force=bool(args.get("refresh")))
    if args.get("only_problems"):
        report = {**report, "items": report.get("problems", [])}
    return report


_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_HARNESS_OWNER", "ODYSSEUS_MCP_CONTEXT_OWNER", "ODYSSEUS_MCP_BRAIN_OWNER")


def _think_mode_calibration(args: dict) -> dict:
    from src import think_calibration

    owner = next((os.environ[k].strip() for k in _OWNER_ENV_KEYS if os.environ.get(k, "").strip()), "")
    if not owner:
        owners = think_calibration.session_owners()
        if len(owners) > 1:
            return {"error": "this database has several users; set ODYSSEUS_MCP_HARNESS_OWNER for the "
                             "harness server to say whose chats to read"}
        owner = owners[0] if owners else ""
    return think_calibration.report(owner or None, args.get("days") or think_calibration.DEFAULT_DAYS)


def _cookbook_job_log(args: dict) -> dict:
    from src import cookbook_job_logs as jl

    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return {"jobs": jl.list_jobs(args.get("limit") or 50)}
    try:
        data = jl.read_log(job_id, str(args.get("stream") or "combined"), args.get("tail") or jl.DEFAULT_TAIL)
    except jl.JobLogError as exc:
        return {"error": str(exc)}
    except KeyError:
        return {"error": f"no log recorded for job {job_id!r}"}
    data["header"] = jl.render_header(jl.get_meta(job_id) or {})
    return data


def _injection_eval_run(args: dict) -> dict:
    from evals.injection import service

    params = {k: args.get(k) for k in ("case_ids", "live", "base_url", "endpoint_url", "model", "timeout", "posture")
              if args.get(k) is not None}
    try:
        job = service.start_job(params, wait=not params.get("live"))
    except ValueError as exc:
        return {"error": str(exc)}
    return job


def _injection_eval_report(args: dict) -> dict:
    from evals.injection import runner, service

    summary = service.latest_report(job_id=str(args.get("job_id") or "") or None,
                                    mode=str(args.get("mode") or "") or None)
    if summary is None:
        return {"error": "no injection eval report yet; call injection_eval_run first"}
    case_id = str(args.get("case_id") or "")
    if case_id:
        match = [r for r in summary["results"] if r["id"] == case_id]
        return match[0] if match else {"error": f"case {case_id!r} is not in this report"}
    if args.get("format") == "markdown":
        return {"markdown": runner.markdown_report(summary)}
    slim = {k: v for k, v in summary.items() if k != "results"}
    slim["results"] = [{k: r[k] for k in ("id", "vector", "technique", "status", "violations", "attempts")}
                       for r in summary["results"]]
    return slim


_TOOLS = {
    "degradation_status": _degradation_status,
    "injection_eval_run": _injection_eval_run,
    "injection_eval_report": _injection_eval_report,
    "doc_lint": _doc_lint,
    "spend_cap_status": _spend_cap_status,
    "history_projection": _history_projection,
    "resource_claims": _resource_claims,
    "paired_bench_reports": _paired_bench_reports,
    "tool_footprint": _tool_footprint,
    "harness_proposals": _harness_proposals,
    "voice_turn_timeline": _voice_turn_timeline,
    "cookbook_job_log": _cookbook_job_log,
    "think_mode_calibration": _think_mode_calibration,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    handler = _TOOLS.get(name)
    if handler is None:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        return _json_result(await asyncio.to_thread(handler, args))
    except Exception as exc:  # noqa: BLE001
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    with stdout_guard():
        try:
            _load_builtin_catalogue()
        except Exception:  # noqa: BLE001 - the other tools must still serve
            pass
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
