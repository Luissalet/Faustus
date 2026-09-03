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
                               "dispatch_model", "dispatch_endpoint_id", "gpu_placement_prefer")

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
            _bool("agent_harness_checks", "Reliability harness",
                  "Claims-vs-evidence check, syntax check and fabricated-path detection after each turn."),
            _bool("agent_tool_preflight", "Tool preflight",
                  "Drop the tools that cannot work in this turn (no project, no mailbox…) before the tool "
                  "list goes out. Saves rounds and schema tokens for small local models."),
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
            _bool("agent_adaptive_idle_timeout", "Adaptive idle timeout",
                  "Learn that bound from the last 20 commands (3 x their median, at most 600 s) instead "
                  "of using the fixed one. It only ever grants MORE time, so a box whose builds run "
                  "silent for minutes stops having them killed; a bound below 30 s is honoured as-is."),
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
