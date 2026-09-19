"""Declarative schema for the "Agent & automation" settings UI.

Every ``agent_*`` / ``browser_*`` / ``desktop_*`` key of ``DEFAULT_SETTINGS``
(plus the few agent-adjacent ones in ``EXTRA_KEYS``) is described here once:
which group it belongs to, a short label, a concrete help text, the control
type and the numeric bounds. ``GET /api/agent/settings/schema``
(routes/agent_settings_routes.py) hands this to static/js/agentSettings.js,
which renders the form in Settings → Agent Tools; ``POST /api/auth/settings``
uses :func:`coerce_setting_value` so a value typed into that form (or posted
by hand) lands with the right Python type and inside its bounds.

Parity with ``DEFAULT_SETTINGS`` is enforced by tests/test_agent_settings_schema.py:
a new ``agent_*`` key without an entry here, or an entry for a key that does
not exist, fails the suite — :func:`schema_problems` is what it checks.

Field types: ``bool`` (toggle), ``int`` / ``float`` (number input with
``min`` / ``max`` / ``step``), ``text``, ``secret`` (masked text), ``select``
(``options``: list of ``{value, label}``) and ``list`` (list of strings,
edited comma-separated). ``restart_hint`` marks a key that is only read when
the process starts; every key in this schema is read live today, so the flag
is carried for the UI contract but currently false everywhere. The browser_*
keys are applied on the next browser action instead (src/builtin_mcp.py
compares the argv it would launch with and restarts the server).
"""

from __future__ import annotations

import re
from typing import Any

from src.settings import DEFAULT_SETTINGS, RETIRED_SETTING_KEYS

# Keys the schema MUST cover (besides EXTRA_KEYS): every default matching this.
SCHEMA_KEY_RE = re.compile(r"^(agent_|browser_|desktop_)")
# Agent-adjacent keys that live under other prefixes but belong on this page.
EXTRA_KEYS: tuple[str, ...] = ("tool_path_extra_roots", "vision_enabled", "vision_model",
                               "dispatch_model", "dispatch_endpoint_id", "gpu_placement_prefer",
                               "sandbox_missing_policy")

FIELD_TYPES: tuple[str, ...] = ("bool", "int", "float", "text", "select", "list", "secret")
_NUMERIC_TYPES = ("int", "float")


def _field(key: str, label: str, help: str, type: str, **extra: Any) -> dict[str, Any]:
    field: dict[str, Any] = {
        "key": key,
        "label": label,
        "help": help,
        "type": type,
        "restart_hint": bool(extra.pop("restart_hint", False)),
    }
    if type == "select":
        options = extra.pop("options")
        field["options"] = [
            {"value": o, "label": o} if isinstance(o, str) else dict(o) for o in options
        ]
    for name in ("min", "max", "step", "placeholder"):
        if name in extra:
            field[name] = extra.pop(name)
    if extra:
        raise TypeError(f"{key}: unknown field attributes {sorted(extra)}")
    return field


def _bool(key, label, help, **kw):
    return _field(key, label, help, "bool", **kw)


def _int(key, label, help, lo, hi, **kw):
    return _field(key, label, help, "int", min=lo, max=hi, step=kw.pop("step", 1), **kw)


def _float(key, label, help, lo, hi, step, **kw):
    return _field(key, label, help, "float", min=lo, max=hi, step=step, **kw)


def _text(key, label, help, **kw):
    return _field(key, label, help, "text", **kw)


def _select(key, label, help, options, **kw):
    return _field(key, label, help, "select", options=options, **kw)


def _list(key, label, help, **kw):
    return _field(key, label, help, "list", **kw)


def _group(id: str, title: str, help: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": id, "title": title, "help": help, "fields": fields}


_BROWSER_APPLY = " Applies on the next browser action."

GROUPS: list[dict[str, Any]] = [
    _group(
        "loop", "Agent loop",
        "How far one message may go, and when a local model gets cut off.",
        [
            _int("agent_max_rounds", "Max steps per message",
                 "Model rounds (tool call + reply) one message may take before the Continue button appears.",
                 1, 200),
            _int("agent_max_tool_calls", "Tool call limit",
                 "Tool calls allowed in one message. 0 = unlimited.",
                 0, 1000),
            _int("agent_auto_continue_cycles", "Auto-continue cycles",
                 "When the step cap hits mid-task, the harness continues by itself this many times "
                 "(each grants another Max steps) before showing the Continue button. 0 = always ask.",
                 0, 10),
            _bool("agent_auto_continue_on_progress", "Keep going while there is progress",
                  "When the round budget is hit but recent rounds each made progress, extend the budget "
                  "automatically instead of ending the turn, up to the hard ceiling below."),
            _int("agent_auto_continue_max_rounds", "Auto-continue hard ceiling (rounds)",
                 "Highest round number the progress-based auto-continue above may reach before it stops "
                 "extending, even if progress is still happening.",
                 1, 2000),
            _int("agent_empty_round_max_nudges", "Empty-round nudges before asking",
                 "A round with no text and no tool call gets nudged to continue this many times before "
                 "the turn ends with a concrete question instead of silence.",
                 1, 10),
            _int("agent_turn_max_seconds", "Turn wall-clock ceiling (seconds)",
                 "Hard backstop independent of round count: once a turn has run this long in real time, "
                 "it ends with a summary and a question instead of continuing indefinitely.",
                 60, 86400),
            _bool("agent_harness_checks", "Reliability harness",
                  "Claims-vs-evidence check, syntax check and fabricated-path detection after each turn."),
            _bool("agent_tool_rerank", "Rerank tool candidates",
                  "Send your query and public built-in tool descriptions to your configured reranker "
                  "to improve tool selection. Private connector descriptions are never sent. Adds up "
                  "to a short request; failures keep the original selection. Off by default."),
            _bool("agent_tool_preflight", "Tool preflight",
                  "Drop the tools that cannot work in this turn (no project, no mailbox…) before the tool "
                  "list goes out. Saves rounds and schema tokens for small local models."),
            _bool("agent_tool_catalog", "Tool catalog helper",
                  "Keep a compact catalog of extra tools and let the agent load full schemas on demand "
                  "with lookup_tools, instead of sending every domain's schemas every turn."),
            _int("agent_stream_timeout_seconds", "Stream timeout (s)",
                 "Seconds without any output from the model before the request is abandoned.",
                 10, 7200),
            _int("agent_local_stream_timeout_seconds", "Local stream timeout floor (s)",
                 "On local endpoints the stream timeout is raised to at least this: local runners can "
                 "stay silent for minutes while they prefill a long prompt.",
                 0, 7200),
            _float("agent_local_temperature_cap", "Local temperature cap",
                   "Coding turns on a local endpoint run at most at this temperature unless the chat pins "
                   "one. 0 = never cap.",
                   0, 2, 0.05),
            _int("agent_local_think_budget_seconds", "Thinking budget (s, local)",
                 "A local thinking model that has produced only reasoning for this long is cut off once "
                 "and retried with thinking off for the rest of the turn. 0 = no watchdog.",
                 0, 3600),
            _int("agent_subprocess_idle_timeout_seconds", "Command idle timeout (s)",
                 "A bash / python command that prints nothing for this long is killed with its whole "
                 "process tree (a server left in the foreground, a prompt waiting for input). 0 = never.",
                 0, 86400),
            _int("agent_server_idle_timeout_seconds", "Server-command idle timeout (s)",
                 "Cap for commands that look like a server/watcher (python app.py, flask, uvicorn, "
                 "npm start). Not adaptive. 0 = use the normal idle timeout.",
                 0, 3600),
            _bool("agent_adaptive_idle_timeout", "Adaptive idle timeout",
                  "Learn that bound from the last 20 commands (3 x their median, at most 600 s) instead "
                  "of using the fixed one. It only ever grants MORE time, so a box whose builds run "
                  "silent for minutes stops having them killed; a bound below 30 s is honoured as-is."),
            _bool("agent_sandbox_execution", "Run commands in the sandbox",
                  "On Linux/macOS, bash and python go through the Docker sandbox "
                  "(src/sandbox_exec.py) with the workspace mounted at /workspace. "
                  "On native Windows this setting is ignored: commands always run on this PC "
                  "(Git Bash / PowerShell / the project's Python)."),
            _select("agent_sandbox_mode", "When the sandbox cannot serve",
                    "auto = the host runs the command and the result says so — always on native "
                    "Windows (a Linux container has no cmd, powershell, .bat, winget or the "
                    "project's own Python), and elsewhere when the daemon does not answer. "
                    "strict = on POSIX, refuse instead of falling back to the host. Windows "
                    "still runs on the host.",
                    ["auto", "strict"]),
            _bool("agent_sandbox_persistent_session", "Persistent per-session sandbox",
                  "Reuse one long-lived container per session (src/sandbox_provider.py) "
                  "instead of a fresh one per command. Off by default."),
            _select("sandbox_missing_policy", "If the session sandbox disappears",
                    "recreate_empty = start a fresh, empty sandbox and say so (the result "
                    "lists what is known to be lost). fail = refuse the next command instead.",
                    ["recreate_empty", "fail"]),
            _bool("agent_workspace_no_memory", "Skip memory on coding turns",
                  "Do not retrieve personal memories for workspace coding turns; local models weave them "
                  "into the code."),
            _int("agent_input_token_budget", "Input token budget",
                 "Soft cap on prompt tokens per round. 6000 (the default) means auto: scale to the "
                 "model's context window. Any other value is an explicit cap; 0 disables trimming.",
                 0, 2_000_000),
            _int("agent_input_token_hard_max", "Auto budget ceiling",
                 "Ceiling for the auto-derived budget. Raise it on APIs with very large windows you "
                 "actually want to fill; no effect on an explicit budget.",
                 1000, 10_000_000),
            _bool("agent_email_confirm", "Confirm agent emails",
                  "send_email / reply_to_email stage a draft for your approval in the chat instead of "
                  "sending right away."),
            _int("agent_autonomy_max_active_seconds", "Autonomy budget: active time (s)",
                 "TASK-06 ceiling on time actually spent working (model inference + tool execution, "
                 "never idle) in one autonomy-preset turn before it must stop and check in. Scaled by "
                 "the chosen preset (supervised/bounded_autonomous/read_only). 0 = unlimited.",
                 0, 36_000),
            _int("agent_autonomy_max_subagents", "Autonomy budget: sub-agents",
                 "TASK-06 ceiling on workers one autonomy-preset turn may start. Scaled by the chosen "
                 "preset. 0 = unlimited.",
                 0, 100),
            _int("agent_autonomy_max_remote_spend", "Autonomy budget: remote spend",
                 "TASK-06 ceiling (in the same units the run's cost estimate uses) on paid-endpoint "
                 "spend for one autonomy-preset turn. Scaled by the chosen preset. 0 = unlimited.",
                 0, 1_000_000),
            _int("agent_autonomy_max_memory_mb", "Autonomy budget: memory (MB)",
                 "TASK-06 informative ceiling for one autonomy-preset turn; this process has no "
                 "per-turn memory measurement today, so it is carried but never trips. Scaled by the "
                 "chosen preset. 0 = unlimited.",
                 0, 65_536),
        ],
    ),
    _group(
        "verification", "Verification",
        "What runs on the files a turn changed before the turn is reported as done.",
        [
            _bool("agent_project_tests", "Run project tests",
                  "After a turn that changed files, run the project's own tests (pytest, npm test, "
                  "cargo, go, make — detected)."),
            _select("agent_project_tests_scope", "Test scope",
                    "related = only the test files that name a changed module (pytest); all = the whole suite.",
                    ["related", "all"]),
            _int("agent_project_tests_timeout_seconds", "Tests timeout (s)",
                 "Wall-clock cap for one test run.",
                 10, 7200),
            _int("agent_project_tests_fix_rounds", "Test fix rounds",
                 "Bounded extra rounds the model gets to fix failing tests. 0 = report only.",
                 0, 5),
            _bool("agent_project_tests_baseline", "Baseline failing tests",
                  "Re-run the failing test files against the turn's checkpoint to tell new failures "
                  "from pre-existing ones; no fix round when everything already failed before."),
            _text("agent_project_test_command", "Test command override",
                  "Command to run instead of the detected one, e.g. npm run test:unit. Empty = auto-detect.",
                  placeholder="auto-detect"),
            _select("agent_static_analysis", "Static analysis",
                    "Correctness-only checks on the lines a turn added: names = ruff / pyflakes, eslint, "
                    "go vet; types also runs tsc --noEmit and cargo check (slower). A missing tool costs nothing.",
                    ["off", "names", "types"]),
            _text("agent_static_analysis_command", "Static analysis override",
                  "Command run instead of the detected linters; the changed paths are appended. "
                  "Empty = auto-detect.",
                  placeholder="auto-detect"),
            _bool("agent_ui_smoke", "UI smoke test",
                  "After a turn that touches static/templates/*.html/*.js/*.css, start the project's "
                  "own web server and fetch its pages plus every script/stylesheet/ES import, checking "
                  "HTTP status AND Content-Type (catches a `.mjs` served as text/plain, which passes "
                  "every unit test while leaving the whole UI dead)."),
            _int("agent_ui_smoke_timeout_seconds", "UI smoke timeout (s)",
                 "Wall-clock cap for one ui_smoke run (server start + page/asset crawl + optional "
                 "Playwright pass).",
                 10, 600),
            _bool("agent_ui_smoke_playwright", "UI smoke: use Playwright",
                  "When Python playwright + a Chromium build are installed, also load the page "
                  "headless and capture real console errors and 4xx/5xx network responses, plus an "
                  "accessibility + performance audit."),
            _bool("agent_ui_smoke_a11y_blocking", "UI smoke: a11y findings block",
                  "A serious accessibility finding (missing alt text, unlabeled form control, no "
                  "accessible name, low color contrast) fails the smoke result the same way a console "
                  "error does. Off by default: serious findings are still surfaced as quality "
                  "warnings, they just don't block the turn."),
            _text("agent_auto_review", "Diff reviewer",
                  "Independent, tool-less review of the turn's diff: off, same (this chat's model) or a "
                  "model name on the same endpoint.",
                  placeholder="off | same | model name"),
            _int("agent_auto_review_timeout_seconds", "Review timeout (s)",
                 "Wall-clock cap for the review pass.",
                 10, 3600),
            _bool("agent_auto_review_fix_round", "Review fix round",
                  "Let the model act on the reviewer's findings before the turn ends."),
            _int("agent_auto_review_fix_rounds", "Review fix rounds",
                 "How many bounded fix rounds the review findings may trigger.",
                 0, 5),
            _bool("agent_checkpoints", "Workspace checkpoints",
                  "Shadow snapshot of the workspace before the first change of a turn: powers 'restore to "
                  "before this turn', per-file diffs and the test baseline. Needs git."),
            _int("agent_checkpoint_max_repo_mb", "Checkpoint repo cap (MB)",
                 "Workspaces larger than this are not snapshotted.",
                 1, 100_000),
            _int("agent_checkpoint_max_file_mb", "Checkpoint file cap (MB)",
                 "Files larger than this are left out of the snapshot.",
                 1, 1000),
        ],
    ),
    _group(
        "context", "Context",
        "What rides along with the user's message, and how big one read may be.",
        [
            _bool("agent_project_instructions", "Project instructions",
                  "Put the repo's standing instructions (AGENTS.md, CLAUDE.md, …) in the system prompt."),
            _int("agent_project_instructions_max_chars", "Instructions max chars",
                 "Longer instruction files are cut at this size.",
                 0, 200_000),
            _select("agent_workspace_trust", "Instruction file trust",
                    "AGENTS.md travels with a cloned repo and goes into the system prompt. "
                    "'ask' holds an unknown folder's instruction files back until you approve them "
                    "(a folder Faustus already has checkpoints for is approved automatically); "
                    "'strict' approves nothing automatically; 'off' always injects the file.",
                    ["off", "ask", "strict"]),
            _bool("agent_repo_map", "Repository map",
                  "Files + symbols of the workspace before the user's message, so the model does not "
                  "spend rounds on ls / grep."),
            _int("agent_repo_map_tokens", "Repo map tokens",
                 "Token budget of the map.",
                 0, 50_000),
            _bool("agent_read_outline", "Outline big reads",
                  "An un-ranged read_file on a file too big to return whole answers with the line count, "
                  "the symbol index, the first ~80 lines and the call that fetches any range — instead of "
                  "a blind cut at the top."),
            _float("agent_read_window_fraction", "Read window fraction",
                   "Share of the model's context window one un-ranged read may occupy; the cap only ever "
                   "comes down from the fixed maximum.",
                   0.01, 1, 0.05),
            _bool("agent_learned_memory", "Learned rules",
                  "Inject the rules and memories the store learned from turn outcomes (Brain → Learned "
                  "rules), and credit or blame them with the turn's verification result."),
            _int("agent_learned_memory_chars", "Learned rules chars",
                 "Character budget of that block. 0 = never inject it.",
                 0, 20_000),
            _bool("agent_file_mentions", "@ file mentions",
                  "Paths picked with @ in the composer are re-resolved server-side and handed to the model."),
            _int("agent_file_mention_inline_chars", "Mention inline chars",
                 "Mentioned files up to this size ride along inline. 0 = list the paths only.",
                 0, 200_000),
            _bool("agent_code_refs", "path:line references",
                  "Tracebacks and stack frames pasted into the message bring the lines around each frame "
                  "along with the turn."),
            _int("agent_code_ref_chars", "Reference chars",
                 "Budget for those excerpts. 0 = list the frames only.",
                 0, 200_000),
            _bool("agent_tool_images", "Tool result images",
                  "Screenshots returned by tools (browser, desktop) go to the model as image blocks when "
                  "it can see."),
            _int("agent_tool_image_max_px", "Tool image max px",
                 "Longest side of a tool image; larger ones are shrunk (JPEG q80).",
                 256, 8192),
            _int("agent_keep_images", "Tool images kept",
                 "Only the last N tool images stay in the prompt; older ones become "
                 "'[earlier image omitted]'. -1 = keep all.",
                 -1, 100),
            _bool("agent_midturn_compact_enabled", "Mid-turn context compaction",
                  "During a long agent turn, spill old/large tool outputs to disk and "
                  "fold history when the prompt nears the soft ceiling — so overnight "
                  "tasks keep moving instead of thrashing at a full context window."),
            _float("agent_midturn_compact_pct", "Mid-turn soft ceiling",
                   "Fraction of the model context (0.40–0.95) that triggers mid-turn "
                   "spill/compaction. Lower = more aggressive. Default 0.70.",
                   0.40, 0.95, step=0.01),
            _int("agent_midturn_keep_tool_rounds", "Tool rounds kept full",
                 "Most recent tool-call batches kept verbatim before older ones spill. "
                 "Oversized single results still spill.",
                 0, 40),
            _int("agent_midturn_spill_chars", "Spill tool results over (chars)",
                 "A single tool result this large is spilled even if recent.",
                 500, 200_000),
            _int("agent_context_overflow_keep_hours", "Overflow keep (hours)",
                 "How long spilled tool bodies stay under data/context_overflow.",
                 1, 8760),
            _int("agent_tool_result_offload_chars", "Offload tool result over (chars)",
                 "A single tool result whose string fields total more than this is "
                 "stored whole in the artifact store before the model sees a "
                 "truncated preview + artifact_id it can open by range or query.",
                 1_000, 500_000),
            _bool("agent_tool_wall_time", "Wall time on tool results",
                  "Prefix every tool result the model reads with how long the call took, "
                  "how long the turn has been running and the local clock, so it can tell a "
                  "40-minute command from a 40 ms one."),
            _text("agent_run_keep_alive", "Keep-alive during agent run",
                 "Ollama keep_alive while a local agent run is in flight (e.g. 2h). "
                 "Stops the weight unloading mid-bash. Prefer a duration over -1."),
            _bool("agent_run_keep_alive_restore", "Restore keep-alive when the run ends",
                  "Ping Ollama at the end of a run so keep_alive returns to the saved value."),
            _bool("agent_ui_verify", "Require browser verify for UI turns",
                  "A turn that edits HTML/CSS/JS in static/templates/editor cannot close as "
                  "verified without a browser snapshot, evaluate, or navigate."),
            _int("agent_inline_attachment_max_chars", "Inline attachment max chars",
                 "When a user message inlines a spec/file over this size, the model sees "
                 "the title and heading TOC instead of the full body. 0 = never trim.",
                 0, 500_000),
            _bool("agent_project_todos", "Persist todos per project",
                  "todowrite writes the list to the project as well as the chat, so a new "
                  "chat in the same project sees incomplete work."),
            _bool("agent_context_engine", "Context Engine",
                  "One compiler decides what the model is told: a ContextPacket per call with "
                  "a manifest, the omissions and a budget, instead of every subsystem "
                  "concatenating its own block. Off by default — measure with shadow first."),
            _bool("agent_context_engine_shadow", "Context Engine shadow mode",
                  "Compile the packet and do NOT deliver it, recording what the difference "
                  "against the prompt actually sent would have been. Changes nothing the "
                  "model sees; costs one compilation per turn."),
            _int("agent_context_timeout_ms", "Context retrieval timeout (ms)",
                 "Wall clock for the whole retrieval stage — the sources run concurrently, so "
                 "this is what a turn pays when the slowest store is wedged. A voice turn "
                 "gets half of it.",
                 100, 60_000, step=100),
            _int("agent_context_ledger_days", "Context ledger days",
                 "How long a packet's ledger row lives: tokens, section split and omission "
                 "counts, never content. Background maintenance prunes past this.",
                 1, 3650),
            _int("agent_context_maintenance_seconds", "Context maintenance interval (s)",
                 "How often idle deterministic maintenance checks for due work. It yields "
                 "while a conversation is running.",
                 60, 86_400),
            _int("agent_project_context_index_seconds", "Project context index interval (s)",
                 "How quickly queued project documents become searchable. The worker yields "
                 "while a conversation is running and publishes indexes atomically.",
                 5, 3600),
            _int("agent_context_cache_entries", "Context cache entries",
                 "Entries in the working set (manifests, token counts, candidate lists) kept "
                 "per owner+project scope. Everything in it is derived, so losing it costs "
                 "latency and nothing else.",
                 16, 100_000),
        ],
    ),
    _group(
        "subagents", "Sub-agents",
        "delegate_agents: the workers, their watchdog and the control board.",
        [
            _bool("agent_subagent_reviewer", "Add a reviewer",
                  "Append a reviewer worker after the others by default."),
            _int("agent_subagent_max_parallel", "Max parallel workers",
                 "Workers running at the same time; the rest wait as 'queued'. Two requests to the SAME Ollama model queue on its single slot, so this only overlaps work when workers use another model (below) or another server.",
                 1, 32),
            _int("agent_subagent_depth", "Delegation depth ceiling",
                 "How many generations of workers one turn may produce. 1 (the default) means the "
                 "workers you start may not start workers of their own; 0 refuses delegation "
                 "entirely. An agent definition may only narrow this, never widen it, and a task "
                 "that would go deeper is refused BEFORE the worker exists — with the limit and "
                 "this setting's name in the message.",
                 0, 4),
            _text("dispatch_model", "Fable workers: model",
                  "Model for workers dispatched from OUTSIDE the app (POST /api/dispatch with an `agents:dispatch` API token — Fable, Claude Desktop, a script). Empty = the utility model, then the default chat model. Pin it to a card in Local models → Options."),
            _text("dispatch_endpoint_id", "Fable workers: endpoint id",
                  "Endpoint the dispatched workers run on (the id shown in Added Models). Empty = the utility / default endpoint."),
            _text("agent_subagent_worker_model", "Worker model",
                  "Model the workers run on (empty = the coordinator's model; a task's own `model` still wins). With two cards, pin it to the other card in Local models → Options (main_gpu): different models generate at the same time, the same model does not."),
            _int("agent_subagent_stall_seconds", "Stall threshold (s)",
                 "Idle or loop time after which a worker counts as stalled.",
                 10, 3600),
            _int("agent_subagent_tick_seconds", "Watchdog tick (s)",
                 "Heartbeat period of the control board.",
                 1, 60),
            _bool("agent_subagent_supervisor", "Deterministic supervisor",
                  "Nudge a stalled worker once, then stop it."),
            _bool("agent_subagent_lean_tools", "Lean worker toolset",
                  "Workers skip web, memory, skills and background-job tools unless their task mentions them (fewer schema tokens per round)."),
            _int("agent_budget_tokens_per_run", "Run token ceiling",
                 "Total tokens the coordinator plus every worker, reviewer and retry may spend on "
                 "one run (src/budget_account.py). A worker's reservation is rejected BEFORE it is "
                 "launched if it would not fit under what remains. 0 = no ceiling, accounting only "
                 "— GET /api/runs/{run_id}/budget still reports reserved vs. consumed tokens.",
                 0, 50_000_000),
            # R3 (Reach wave): src/fanout/ -- one prompt raced across N
            # candidate models, each in its own isolated alternative.
            _int("agent_fanout_max_parallel", "Fan-out: max parallel candidates",
                 "How many fanout_run candidates run their worker at the same time; the rest wait. "
                 "Candidates that look local to each other are additionally serialised to one at a "
                 "time regardless of this ceiling — two big local models rarely fit one card's VRAM.",
                 1, 16),
            _text("agent_fanout_model_pool", "Fan-out: extra model pool",
                  "Comma-separated extra models fanout_run offers as default candidates, alongside "
                  "the chat's own model and the worker/dispatch models already configured above. "
                  "Empty by default."),
            # A29: src/loop_breaker.py's deterministic escalation ladder (kept
            # in this group rather than a new one so the schema's requested
            # group order/prefix — test_agent_settings_schema.py — is
            # untouched). Wired into src/agent_loop.py's tool-result site.
            _int("agent_loop_breaker_nudge_after", "Loop breaker: nudge after N repeats",
                 "Identical (tool, arguments, result) calls in a row before a system message is "
                 "injected telling the model to try something else.",
                 1, 50),
            _int("agent_loop_breaker_block_after", "Loop breaker: block tool after N repeats",
                 "Further identical repeats (past the nudge) before that tool is hidden from the "
                 "next round's schema.",
                 2, 100),
            _int("agent_loop_breaker_stop_after", "Loop breaker: stop turn after N repeats",
                 "Further identical repeats (past the block) before the turn ends with "
                 "stop_reason \"non_progressing_loop\".",
                 3, 200),
            _bool("agent_loop_breaker_cycle_detection", "Loop breaker: detect oscillation",
                  "Also catch a repeating A/B (or 3-4 step) pattern with no two consecutive "
                  "calls identical — read A, read B, read A, read B... — using the same "
                  "nudge/block/stop ladder as the exact-repeat streak above, counted on its "
                  "own so neither resets the other."),
            _int("agent_loop_breaker_cycle_min_repeats_p2", "Loop breaker: 2-step cycle repeats",
                 "Full A→B→A→B repetitions before a 2-call oscillation is nudged.",
                 2, 50),
            _int("agent_loop_breaker_cycle_min_repeats_long", "Loop breaker: 3-4 step cycle repeats",
                 "Full repetitions of a 3- or 4-call cycle before it is nudged.",
                 2, 50),
        ],
    ),
    _group(
        "runs", "Runs & queue",
        "Detached runs: the replay log on disk and the task queue.",
        [
            _bool("agent_runs_persist", "Persist runs",
                  "On-disk replay log, so a run survives a restart and can be reopened."),
            _int("agent_runs_keep_hours", "Keep runs (hours)",
                 "Finished run logs older than this are swept.",
                 1, 8760),
            _int("agent_queue_local_concurrency", "Local lane concurrency",
                 "Runs at a time on local endpoints (1 = one GPU, one generation). 0 = unlimited.",
                 0, 64),
            _int("agent_queue_api_concurrency", "API lane concurrency",
                 "Runs at a time on API endpoints. 0 = no queue.",
                 0, 64),
            _bool("agent_scorecard", "Model scorecard",
                  "Record per-model reliability metrics of agent turns (/scorecard)."),
        ],
    ),
    _group(
        "browser", "Browser",
        "Built-in Playwright browser. Changes apply on the next browser action — the server is "
        "restarted with the new flags, no app restart needed.",
        [
            _select("browser_profile", "Profile",
                    "persistent keeps cookies and logins in data/browser-profile between runs; isolated "
                    "starts from a blank profile every time." + _BROWSER_APPLY,
                    ["isolated", "persistent"]),
            _bool("browser_headless", "Headless",
                  "Run the browser without a window. Turn off to watch it on the server's desktop." + _BROWSER_APPLY),
            _text("browser_cdp_endpoint", "Attach to your Chrome (CDP)",
                  "DevTools URL of a browser you already run, e.g. http://127.0.0.1:9222. Start Chrome with "
                  "--remote-debugging-port=9222 --user-data-dir=<a separate profile dir>. When set, the "
                  "built-in server attaches to it and Headless / Profile do not apply." + _BROWSER_APPLY,
                  placeholder="http://127.0.0.1:9222"),
            _bool("browser_vision_caps", "Vision tools",
                  "Add the mouse_*_xy tools; only useful with a vision model looking at screenshots." + _BROWSER_APPLY),
            _int("browser_snapshot_max_chars", "Snapshot max chars",
                 "Budget for the accessibility snapshot text returned by browser_snapshot / browser_navigate.",
                 1000, 200_000),
            _bool("browser_allow_code_execution", "Allow page JavaScript",
                  "Offer browser_evaluate / browser_run_code_unsafe: model-written JavaScript runs inside "
                  "the page. Off = not offered and denied."),
            _bool("browser_live_view", "Live view",
                  "Capture a viewport frame after every action for the Browser panel (never sent to the model)."),
        ],
    ),
    _group(
        "desktop", "Desktop control",
        "The agent sees and drives the server's desktop.",
        [
            _select("desktop_control_mode", "Input tools",
                    "desktop_click / type / key / scroll / focus_window: ask_each = approval card on every "
                    "call, ask_task = the normal scoped approval gate, off = not offered at all.",
                    ["ask_each", "ask_task", "off"]),
        ],
    ),
    _group(
        "vision", "Vision",
        "Image analysis for the agent (OCR, tagging, screenshots).",
        [
            _bool("vision_enabled", "Vision",
                  "Let the agent analyse images. Global default; users can override it in their own preferences."),
            _text("vision_model", "Vision model",
                  "Model id used for image analysis. The picker in AI Defaults → Vision lists the available ones.",
                  placeholder="e.g. qwen2.5vl:7b"),
        ],
    ),
    _group(
        "command_guard", "Command guard",
        "Destructive shell commands are classified before they run; the dangerous tiers wait for your approval.",
        [
            _select("agent_command_guard_mode", "Guard mode",
                    "off = no classification; observe = classify and log receipts only; enforce = DANGEROUS/"
                    "CRITICAL bash/python commands need an exact approval card before they run.",
                    ["off", "observe", "enforce"]),
            _text("agent_command_guard_packs", "Rule packs",
                  "Which rule packs classify commands: 'all' or a comma list of fs, git, db, containers, system.",
                  placeholder="all"),
        ],
    ),
    _group(
        "provenance", "Web provenance",
        "What the browser scrapes is anchored to where it came from, so a citation can be checked and "
        "text that changed since the fetch is detectable.",
        [
            _bool("agent_web_provenance", "Anchor page text",
                  "Page text handed to the model carries one invisible HTML comment per block naming the "
                  "URL, the character range in the fetched document and a sha256 of that range, so a "
                  "citation can be checked against the source and a block that changed since the fetch is "
                  "reported as drifted. The anchor is a character range plus a hash, not a pixel "
                  "coordinate — nothing here knows where on the screen the text was. Off = the text is "
                  "passed to the model byte for byte as before."),
        ],
    ),
    _group(
        "storage", "Disk ballast",
        "Preallocated files that can be unlinked instantly when the disk gets tight, and the scoring "
        "of what Faustus itself created and could go. Nothing is ever deleted directly: a candidate is "
        "MOVED to data/_quarantine with an undo, and anything with a .git directory inside it is never "
        "touched at all.",
        [
            _select("agent_disk_ballast", "Ballast mode",
                    "observe = measure, score and report, move nothing (the default); canary = at most "
                    "10 quarantines an hour; enforce = quarantine at the rate asked for. Quarantined "
                    "items are swept 24 hours later, and can be restored until then.",
                    ["observe", "canary", "enforce"]),
        ],
    ),
    _group(
        "gpu", "GPU placement",
        "Which card the local Ollama fills first (two or more GPUs). Per-model pins in Local models → Options always win.",
        [
            _int("gpu_placement_prefer", "Fill this card first",
                 "-1 = Auto (Ollama: the card with the most free memory, split across cards when nothing fits one). "
                 "0, 1, … = pin every model that fits that card to it, with room for its context; bigger models stay "
                 "Auto — a model pinned to a card it does not fit is not split, it goes to the CPU (measured: 10 tok/s "
                 "instead of 20). Also on the Local models page.",
                 -1, 15),
        ],
    ),
    _group(
        "files", "File access",
        "Where the file tools may go besides the project data/ and temp directories.",
        [
            _list("tool_path_extra_roots", "Extra file roots",
                  "Absolute directories read_file / write_file may access, comma-separated. .ssh, .gnupg, "
                  "shell rc files and SSH keys stay blocked regardless.",
                  placeholder="/srv/projects, /home/me/work"),
        ],
    ),
    _group(
        "reliability", "Reliability",
        "Machinery that stops the workers wasting rounds — and stops the reports blaming the model for "
        "what you did on purpose.",
        [
            _bool("agent_fix_round_convergence", "Stop fix rounds on convergence",
                  "A dispatched job (Fable workers, above) stops its verification fix loop as soon as the "
                  "rounds stop producing change — the size, edit distance and similarity of successive "
                  "rounds. `fix_rounds` becomes a maximum instead of an exact count, and a request may ask "
                  "for up to 4 of them. Off = the fixed counter, capped at 2."),
            _bool("agent_fixer_resume", "Resume the worker a fix round is fixing",
                  "When a dispatched job's verification fails, continue the worker that made the "
                  "change — in its own session, with the files it already read — instead of "
                  "building a fresh one from the task plus the failure text. Rebuilding that "
                  "understanding is the expensive half of a fix round. A session that no longer "
                  "has any history degrades silently to a fresh worker. Off = always a fresh one."),
            _bool("agent_worker_state_detection", "Read a worker's state from its output",
                  "Classify each worker's own output while it runs with rule packs — rate limited, "
                  "waiting for input, stuck (the same line over and over), auth error, disk full, out "
                  "of memory — and show the state with the literal that proves it on the board and in "
                  "a dispatched job's progress. A worker in one of those states is REPORTED, never "
                  "killed for it. Off = progress says exactly what it said before."),
            _bool("agent_dispatch_sse", "Live job events",
                  "Stream a dispatched job's board events as they happen (server-sent events) so the "
                  "Workers page fills in while the job runs, with a heartbeat every 15 s and a final "
                  "event carrying the verdict. Off = the page polls every 3 seconds as before and the "
                  "events endpoint answers the same JSON it always did."),
            _bool("agent_dispatch_prove", "Prove what the workers did",
                  "A finished dispatched job carries a proof packet: proved (the verification passed and "
                  "every claimed file really changed), partial (something is unaccounted for), unproved "
                  "(nothing ran that could show it — honest, not a failure) or contradicted (the disk or "
                  "the tests say otherwise), with the confidence and a named reason for every point it "
                  "lost. Off = the job answers exactly what it answered before."),
            _bool("agent_objective_ordering", "Order a job's tasks by objective impact",
                  "When a sequential dispatched job's tasks name objectives (OBJ-1, OBJ-2) and the folder "
                  "has an objectives file, run them in the order the objectives graph already ranks them "
                  "— PageRank, betweenness, what each one blocks, staleness and your priority — instead of "
                  "the order they were typed. A task that names no objective keeps its place, the job "
                  "records what it reordered, and nothing is ordered across jobs. Off = the written order."),
            _bool("agent_crash_recovery", "Find what a power cut interrupted",
                  "At startup, look for jobs and runs whose records all stopped being written at the same "
                  "instant around the last boot — what a power cut leaves behind — and mark them "
                  "interrupted with the reason. It produces a plan that re-pins each job's own model and "
                  "parameters, and resumes nothing by itself; when the machine will not say when it booted "
                  "it does nothing at all. Off = no scan."),
            _bool("agent_health_score", "Honest health score",
                  "Add a health block to the system usage panel where a signal nobody collected counts as "
                  "zero and says \"no data source yet\" instead of a plausible zero — so a machine nothing "
                  "has been collected from does not look healthy by default. Off = the usage endpoint and "
                  "the panel are exactly what they were."),
            _bool("agent_tool_outcomes", "Four-value outcomes",
                  "Record success / expected_error / cancelled / panic for worker runs, tool results and "
                  "scorecard turns. A worker YOU stopped counts as cancelled, not as a failure. "
                  "Off = anything that did not finish counts as an error."),
            _bool("agent_mcp_stdio_guard", "Protect the MCP stdio stream",
                  "While a built-in MCP server is serving, stdout writes from app code in the same process "
                  "go to stderr instead. One stray print() on stdout corrupts the JSON-RPC stream and "
                  "kills the session."),
            _bool("agent_mcp_min_env", "Minimal environment for new MCP servers",
                  "A stdio MCP server is a third-party program. On, a NEWLY added one gets only PATH, "
                  "HOME, TEMP, the Windows essentials and its own declared variables — not every provider "
                  "API key in the app. Servers you already have keep the full environment either way; "
                  "change one on its own card."),
            _int("agent_mcp_prompt_budget_tokens", "MCP prompt budget (tokens)",
                 "The MCP tools block in the prompt is scoped to this turn's selected MCP tools (one "
                 "line each — they already have native schemas) plus one \"N more tools\" line per "
                 "server with nothing selected, capped at this many tokens. lookup_tools and tool-RAG "
                 "still find every MCP tool regardless — this only trims what the prompt text repeats. "
                 "0 turns the block off entirely.",
                 0, 20000),
            _bool("agent_mcp_prompt_full_listing", "Full MCP tool listing every turn",
                  "Restores the old behaviour: every connected server's every tool, full description, "
                  "dumped into the prompt on every turn, instead of the scoped block. Off by default — "
                  "it was costing thousands of prompt tokens on a bare \"hola\" and confusing small "
                  "local models with tools nobody selected."),
        ],
    ),
    _group(
        "runners", "External agent runners",
        "Run an agent Faustus did not write as one of its workers — Claude Code, OpenCode, Qwen "
        "Code, whatever `ollama launch` knows and this machine has installed. Faustus's command "
        "guard CANNOT see inside another agent's own shell, so a job that uses one says so in its "
        "verdict and carries `external_agent_unguarded` in its proof.",
        [
            _bool("agent_external_runners", "Let a job use an external agent",
                  "A dispatched task may name a `runner` (see the Agent runners page) and that "
                  "agent does the work instead of a local sub-agent, inside the same harness: the "
                  "checkpoint before, the diff after, Faustus's own verification and the proof. "
                  "What is NOT the same: nothing sees the commands that agent runs — no "
                  "destructive-command guard, no approval card, no file locks — only what changed "
                  "on disk afterwards. Off (the default, because this starts third-party binaries "
                  "on your machine) = a job naming a runner is refused with that reason."),
            _int("agent_external_runner_timeout_s", "External agent timeout (s)",
                 "How long one external agent may run before its whole process TREE is killed. It "
                 "is the only clock on it: an agent that says it is rate limited or is waiting for "
                 "input is reported on the board, never killed for it.",
                 10, 7200, step=30),
            _text("agent_env_allow", "Variables an external agent may read",
                  "Comma-separated names an external agent CLI is allowed to take from this "
                  "machine's environment, on top of the structural ones (PATH, HOME, TEMP, locale, "
                  "CA bundle) and whatever its own row in the runner table declares. An external "
                  "agent no longer inherits every provider key, cloud credential and repository "
                  "token you have exported — so if one of them genuinely needs a variable, name it "
                  "here. Example: GITHUB_TOKEN, MY_VENDOR_KEY."),
            _bool("agent_env_inherit_all", "Give external agents the whole environment",
                  "The old behaviour, kept as an escape hatch: every variable in this process goes "
                  "to the third-party binary. Each run that uses it is logged. Faustus's own "
                  "internal token is withheld either way — that part is not a setting. Off is the "
                  "default and should stay off unless something is broken without it."),
        ],
    ),
    _group(
        "experts", "Specialist experts",
        "Local specialists with their own corpus: a rubric, your own PDFs on disk, and citations "
        "that resolve back to the page. Nothing is uploaded and there is no size limit beyond the disk.",
        [
            _bool("agent_experts", "Specialist experts",
                  "Let an expert contribute its instructions, its rubric and its top corpus excerpts to a "
                  "turn. Off = no expert block is injected; the experts, their corpora and their indexes "
                  "stay exactly as they are on disk."),
            _int("agent_expert_context_chars", "Expert block budget (chars)",
                 "Hard character budget for one expert's block: its instructions and rubric, its cited "
                 "corpus excerpts ([C1], [C2]…) and its own learned rules.",
                 200, 40_000, step=100),
        ],
    ),
    _group(
        "tournament", "Model tournament",
        "The same prompt to several local models blind and in parallel, then rounds where each one "
        "sees all the previous answers anonymised and weaves the complementary parts into a hybrid. "
        "Two DIFFERENT models really do generate at the same time on this machine; two requests to "
        "the same one queue behind its single slot, so the scheduler serialises those.",
        [
            _bool("agent_tournament", "Model tournament",
                  "Let the Tournament page run a prompt across several models and rank the answers. "
                  "Off = no new run can be started; the runs already recorded stay readable."),
            _int("agent_tournament_max_models", "Models per tournament",
                 "How many models one tournament may enter. Each one is a full generation per round, "
                 "and only DIFFERENT models overlap — more models means a longer round, not a slower "
                 "one per model.",
                 2, 8, step=1),
        ],
    ),
    _group(
        "council", "Council",
        "A durable room where several models think, object and review the same matter, and exactly "
        "one of them owns each task and each file. Unlike the tournament, a council can also WRITE: "
        "in the collaborate and pair policies the designated driver edits the workspace behind a "
        "claim, and reviewers stay read-only.",
        [
            _bool("agent_council", "Council",
                  "Let a council room run turns: several models answering, objecting and — under "
                  "collaborate or pair — editing claimed files. Off is the default because one "
                  "message becomes several models across several rounds, and some of them write. "
                  "Off = a room can still be opened and every room already recorded stays fully "
                  "readable (transcript, ledger, decisions, evidence); only sending a message and "
                  "issuing a command are refused."),
        ],
    ),
    _group(
        "state_mirror", "State Mirror",
        "What is true right now, when we last looked and how we know it: one row per project, "
        "service, model, run and artifact, with the time and the source printed beside every "
        "value. Nothing here is canonical. The mirror is a projection of the systems that own "
        "the facts, and anything about to act revalidates against those systems first.",
        [
            _bool("agent_state_mirror", "State Mirror",
                  "Let the mirror OBSERVE: the sweep re-reads whatever has aged past its guarantee, "
                  "and Refresh probes a source on demand. Off is the default because both of those "
                  "spend this machine on work nobody asked for at that moment. Off = nothing probes "
                  "anything and the refresh and reconcile endpoints refuse and say so; every read "
                  "keeps answering, so what was already observed stays visible with its age on it."),
            _int("agent_state_mirror_sweep_seconds", "Sweep interval (s)",
                 "How often the sweep looks for fields that have aged past their TTL and observes "
                 "them again. Read only while the switch above is on. A longer interval means a "
                 "service that died is noticed later, not that it is noticed less.",
                 5, 3600, step=5),
        ],
    ),
    _group(
        "delta_engine", "Delta Engine",
        "What actually changed between two revisions, and how much of it you asked for. Every "
        "finding carries the method that produced it and how much to believe it, and a property "
        "nobody measured is reported as unknown rather than as preserved: not detected is not "
        "the same as unchanged, and that difference is the whole point of comparing at all.",
        [
            _bool("agent_delta_engine", "Delta Engine",
                  "Let comparisons RUN: compile an intent, read both revisions and produce a "
                  "delta. Off is the default because a comparison re-reads and re-parses both "
                  "ends, which is real work on a large repository. Off = /api/deltas refuses to "
                  "compile or compare and says so; reading a delta already stored keeps working, "
                  "because a conclusion recorded honestly stays readable."),
            _int("agent_delta_engine_max_bytes", "Max bytes per side",
                 "The ceiling on how much of one revision is read into memory. Past it the "
                 "adapter stops and the delta declares the excluded part in its coverage, rather "
                 "than reporting a difference it never looked for.",
                 65_536, 268_435_456, step=65_536),
            _int("agent_delta_engine_max_elements", "Max elements aligned",
                 "How many files, symbols, nodes or fields one comparison aligns before it stops "
                 "and says the rest is uncompared. A big rename is a real request; quietly "
                 "comparing the first few hundred files of it is not an answer to it.",
                 100, 100_000, step=100),
        ],
    ),
    _group(
        "completion", "Completion depth",
        "How far a turn pushes past the literal request. The four modes — literal, professional, "
        "greedy, maximalist — decide DEPTH and never authority: no mode grants a tool, a path or "
        "an effect the run did not already hold. What the engine adds is the account: what was "
        "asked, what was added beyond it, what was refused and why, and whether the turn stopped "
        "because there was nothing left worth doing or because it ran out.",
        [
            _bool("agent_completion_engine", "Completion engine",
                  "Let the engine ACT on the mode: open the professional and bonus layers, run the "
                  "improvements that are local, reversible and in scope, and stop on convergence. "
                  "Off is the default because this changes what a turn does, and a switch that "
                  "changes behaviour should be turned on by someone who meant to."),
            _bool("agent_completion_engine_shadow", "Shadow mode",
                  "Compute the decision the engine WOULD have taken and record it, without "
                  "changing anything the model or you see. This is the measurement that justifies "
                  "turning the switch above on; leaving it on costs one computation per turn."),
            _float("agent_completion_verification_reserve", "Verification reserve",
                   "The share of a turn's budget held back for verifying what it did. Never "
                   "spendable on extra work, in any mode. Lowering it does not buy more "
                   "improvements — it buys less proof that the improvements were safe.",
                   0.0, 0.5, step=0.05),
            _int("agent_completion_max_bonus_rounds", "Max improvement rounds",
                 "How many rounds of finding-and-doing improvements one turn may run past the "
                 "core request. A ceiling on the loop itself: a cheap improvement that spawns "
                 "another cheap improvement forever is a convergence failure, not a bargain.",
                 0, 20, step=1),
        ],
    ),
    _group(
        "teach_mode", "Modo Enséñame",
        "Record a task as semantic tool actions, compile it into a reusable procedure and require "
        "simulation, evidence and explicit approval before installation.",
        [
            _bool("agent_teach_mode", "Modo Enséñame",
                  "Allow demonstrations to record tool actions and compile procedures. Turning it off "
                  "stops capture and lifecycle changes but never hides or deletes prior teaching."),
        ],
    ),
    _group(
        "immune_system", "Immune System",
        "Track whether skills, tools, workflows and connectors are healthy; deduplicate failures, "
        "quarantine unsafe capabilities and gate repair promotion on proof and canary evidence.",
        [
            _bool("agent_immune_system", "Immune System",
                  "Allow health assessments, containment and repair lifecycle changes. Existing health "
                  "and incident history remains readable while disabled."),
        ],
    ),
    _group(
        "branching_futures", "Branching Futures",
        "Compare observed results from equivalent isolated branches before touching real state. A branch "
        "cannot perform real external effects and a commit must revalidate its frozen base.",
        [
            _bool("agent_branching_futures", "Branching Futures",
                  "Allow futures, branch results, evaluation and commits. Existing runs and selection "
                  "receipts remain readable while disabled."),
        ],
    ),
    _group(
        "provenance", "Provenance graph",
        "The audit view over the memory and the workspace: why the agent believes a thing, what is "
        "floating unreferenced, what is said twice, and what breaks if you touch a file.",
        [
            _bool("agent_provenance_graph", "Provenance graph",
                  "Build the 2D graph from DECLARED edges only — a dependency you wrote, a stored "
                  "evidence span, a checkpoint's diff, a citation that resolves to a page, a text "
                  "overlap verified by exact comparison. No edge a model asserted. Off = the "
                  "/api/provenance reads report it as disabled; nothing stored is changed either way."),
            _int("agent_provenance_max_nodes", "Graph node budget",
                 "Hard cap on the nodes one graph may hold. The build stops there and says so instead "
                 "of drawing an illegible blob.",
                 50, 20_000, step=50),
        ],
    ),
    _group(
        "code_mode", "Code Mode",
        "The model writes a short Python program that composes several tool calls in one isolated "
        "subprocess round instead of one model round trip per call (run_code tool). Every tool call "
        "the program makes goes through the same policy gate and approvals an ordinary call would.",
        [
            _bool("agent_code_mode", "Code Mode",
                  "Register and allow the run_code tool. Off by default; the tool refuses to run "
                  "while this is off."),
            _int("agent_code_mode_timeout_seconds", "Wall time limit (s)",
                 "A run_code program is killed and returns a diagnostic receipt after this long.",
                 1, 3600),
            _int("agent_code_mode_max_calls", "Tool call limit",
                 "Max tools.call() invocations one run_code program may make before it is killed.",
                 1, 10_000),
            _int("agent_code_mode_max_output_bytes", "Output size limit (bytes)",
                 "Max captured stdout/stderr one run_code program may produce before it is killed.",
                 1_000, 50_000_000, step=1000),
        ],
    ),
    _group(
        "history", "Imported history",
        "Your conversations from somewhere else, brought here. A ChatGPT or Claude data export, an "
        "LM Studio chat folder or one of Faustus's own JSON exports is normalised into its own "
        "store and searched in two tiers — which needs no model and no network, so a freshly "
        "installed Faustus can search an archive the minute it has imported one.",
        [
            _bool("agent_history_import", "Imported history",
                  "Show the Imported history page and answer the /api/history reads. Off = the page "
                  "hides and the reads report it as disabled; everything already imported stays on "
                  "disk untouched, because turning a switch off is not a delete."),
        ],
    ),
    _group(
        "code_graph", "Code graph",
        "A persistent, per-workspace graph of symbols/calls/imports/routes (code_graph_* tools) "
        "for architecture and call-tracing questions answered from an index instead of reading files.",
        [
            _bool("agent_code_graph_auto_index", "Auto-index on code turns",
                  "Index a workspace's code graph in the background the first time a code turn opens "
                  "it, so code_graph_search/trace/architecture already have a warm index to answer from."),
        ],
    ),
    _group(
        "harness_policy", "Long-implementation policies",
        "Harness policies aimed at a small local model on long, multi-turn implementations "
        "(src/dependency_drift.py, src/rewrite_policy.py, src/test_debt.py): tell it what is not "
        "installed before it runs anything, discourage rewriting the same large file whole several "
        "times in one turn instead of editing it, and keep a persistent journal of tests exempted as "
        "pre-existing so one never stays silently forgiven for days.",
        [
            _bool("agent_dependency_drift", "Dependency drift check",
                  "When a code turn starts, compare the project's requirements.txt/pyproject.toml/"
                  "package.json with what its interpreter and node_modules actually have and tell the "
                  "model what is missing before it runs anything."),
            _bool("agent_auto_install_missing_deps", "Auto-install missing dependencies",
                  "Install the packages the dependency drift check found missing through the same "
                  "approved install_dependencies flow, instead of leaving it for the model to discover "
                  "by traceback."),
            _select("agent_rewrite_policy", "Rewrite policy",
                    "\"off\" never restricts write_file. Any other value enables the ladder below: "
                    "from the Nth qualifying rewrite of an existing, non-trivial file within one turn, "
                    "write_file is refused in favour of edit_file/apply_patch.",
                    ["off", "require_edit"]),
            _int("agent_rewrite_policy_require_edit_after", "Rewrite policy: require edit after N rewrites",
                 "Whole-file write_file rewrites of the same existing, non-trivial file within one "
                 "turn before write_file starts refusing and pointing at edit_file/apply_patch.",
                 1, 20),
            _int("agent_rewrite_policy_block_after", "Rewrite policy: block after N rewrites",
                 "Further whole-file rewrites of the same file (past require-edit) before the refusal "
                 "escalates and the harness round injects a stronger 'read the file and the diff "
                 "first' instruction.",
                 2, 50),
            _int("agent_rewrite_policy_min_lines", "Rewrite policy: file size floor (lines)",
                 "A file at or under this many lines before the write is never restricted — the "
                 "policy only guards files too large for a small model to hold in memory across "
                 "several full rewrites.",
                 0, 5000),
            _bool("agent_test_debt", "Test debt journal",
                  "Track tests seen as pre-existing/exempt across turns per project "
                  "(data/test_debt/<project>.json) instead of only comparing this turn's baseline."),
            _int("agent_test_debt_turns", "Test debt: overdue after N turns",
                 "A test seen as pre-existing/exempt for this many turns becomes 'overdue' and is "
                 "surfaced as a high-priority todo until it is fixed or dismissed with a reason.",
                 1, 50),
            _bool("agent_plan_tracker", "Plan tracker",
                  "Parse a large plan attachment (=== File/ZIP: ... === inlined after the user's "
                  "own text) once per project, persist it and its per-task status under DATA_DIR/"
                  "plan_tracker, and inject a short brief plus only the current task instead of "
                  "reinjecting the whole attachment body in every chat (src/plan_tracker.py)."),
            _int("agent_plan_tracker_min_chars", "Plan tracker: minimum attachment size",
                 "An inlined attachment body shorter than this many characters is never treated as "
                 "a plan to track, no matter how many tasks its structure would parse.",
                 100, 100000),
            _int("agent_plan_tracker_task_chars", "Plan tracker: current-task text cap",
                 "Maximum characters of the current task's own text kept when replacing the full "
                 "attachment in the prompt.",
                 200, 50000),
        ],
    ),
]


def schema_fields() -> list[dict[str, Any]]:
    """Every field of every group, in display order."""
    return [f for g in GROUPS for f in g["fields"]]


def schema_keys() -> list[str]:
    return [f["key"] for f in schema_fields()]


def expected_keys() -> list[str]:
    """The keys the schema must cover: DEFAULT_SETTINGS agent_/browser_/desktop_
    keys plus EXTRA_KEYS (retired keys excluded)."""
    keys = [k for k in DEFAULT_SETTINGS if SCHEMA_KEY_RE.match(k) and k not in RETIRED_SETTING_KEYS]
    keys += [k for k in EXTRA_KEYS if k in DEFAULT_SETTINGS]
    return keys


def _default_matches_type(field: dict[str, Any], default: Any) -> bool:
    t = field["type"]
    if t == "bool":
        return isinstance(default, bool)
    if t == "int":
        return isinstance(default, int) and not isinstance(default, bool)
    if t == "float":
        return isinstance(default, (int, float)) and not isinstance(default, bool)
    if t in ("text", "secret"):
        return isinstance(default, str)
    if t == "select":
        return isinstance(default, str) and any(o["value"] == default for o in field["options"])
    if t == "list":
        return isinstance(default, list)
    return False


def schema_problems() -> list[str]:
    """Human-readable parity/consistency problems; empty when the schema is sound.

    Checked: every expected key has exactly one entry; no entry names an
    unknown key; types are valid and match the default's Python type; numeric
    fields carry min <= default <= max; selects list their default."""
    problems: list[str] = []
    seen: dict[str, int] = {}
    for f in schema_fields():
        seen[f["key"]] = seen.get(f["key"], 0) + 1
    for key, n in seen.items():
        if n > 1:
            problems.append(f"{key}: listed {n} times")
        if key not in DEFAULT_SETTINGS:
            problems.append(f"{key}: in the schema but not in DEFAULT_SETTINGS")
    for key in expected_keys():
        if key not in seen:
            problems.append(f"{key}: in DEFAULT_SETTINGS but missing from the schema")
    for f in schema_fields():
        key = f["key"]
        if f["type"] not in FIELD_TYPES:
            problems.append(f"{key}: unknown type {f['type']!r}")
            continue
        if not f.get("label") or not f.get("help"):
            problems.append(f"{key}: label and help are required")
        if key not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[key]
        if not _default_matches_type(f, default):
            problems.append(f"{key}: type {f['type']} does not match default {default!r}")
        if f["type"] in _NUMERIC_TYPES:
            lo, hi = f.get("min"), f.get("max")
            if lo is None or hi is None or lo > hi:
                problems.append(f"{key}: numeric field needs min <= max")
            elif isinstance(default, (int, float)) and not (lo <= default <= hi):
                problems.append(f"{key}: default {default!r} outside [{lo}, {hi}]")
    return problems


def build_schema() -> dict[str, Any]:
    """Payload of GET /api/agent/settings/schema: ``{"groups": [...], "defaults": {...}}``."""
    return {
        "groups": [
            {"id": g["id"], "title": g["title"], "help": g["help"], "fields": [dict(f) for f in g["fields"]]}
            for g in GROUPS
        ],
        "defaults": {k: DEFAULT_SETTINGS[k] for k in schema_keys() if k in DEFAULT_SETTINGS},
    }


_FIELD_BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in schema_fields()}

_TRUE_WORDS = ("true", "1", "yes", "on", "enable", "enabled")
_FALSE_WORDS = ("false", "0", "no", "off", "disable", "disabled", "")


def field_for(key: str) -> dict[str, Any] | None:
    return _FIELD_BY_KEY.get(key)


def _clamp(value, field):
    lo, hi = field.get("min"), field.get("max")
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


def coerce_setting_value(key: str, value: Any) -> Any:
    """Coerce ``value`` to the type the schema declares for ``key`` and clamp
    numbers to their bounds. Keys outside the schema pass through untouched.
    Raises ``ValueError`` with a short message for a value that cannot be read."""
    field = _FIELD_BY_KEY.get(key)
    if field is None:
        return value
    t = field["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        word = str(value if value is not None else "").strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ValueError("must be true or false")
    if t == "int":
        if isinstance(value, bool):
            raise ValueError("must be an integer")
        try:
            if isinstance(value, str):
                value = value.strip()
                number = float(value) if value else None
            else:
                number = float(value)
        except (TypeError, ValueError):
            number = None
        if number is None or number != number or number != int(number):
            raise ValueError("must be an integer")
        return int(_clamp(int(number), field))
    if t == "float":
        if isinstance(value, bool):
            raise ValueError("must be a number")
        try:
            number = float(str(value).strip()) if isinstance(value, str) else float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if number != number:
            raise ValueError("must be a number")
        return float(_clamp(number, field))
    if t in ("text", "secret"):
        return "" if value is None else str(value).strip()
    if t == "select":
        word = "" if value is None else str(value).strip()
        allowed = [o["value"] for o in field["options"]]
        if word not in allowed:
            raise ValueError("must be one of " + ", ".join(allowed))
        return word
    if t == "list":
        if value is None:
            return []
        if isinstance(value, str):
            items = re.split(r"[,\n]", value)
        elif isinstance(value, (list, tuple)):
            items = value
        else:
            raise ValueError("must be a list")
        out = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError("must be a list of strings")
            item = item.strip()
            if item:
                out.append(item)
        return out
    return value
