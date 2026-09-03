# src/settings.py
"""Centralized settings and features management.

Single source of truth for reading/writing data/settings.json and data/features.json.
All modules should import from here instead of accessing files directly.
"""

import json
import time
import logging
from typing import Any

from src.constants import SETTINGS_FILE, FEATURES_FILE

logger = logging.getLogger(__name__)

# Keys retained in the raw settings store for compatibility and rollback, but
# deliberately unavailable through generic settings APIs or agent tools.  They
# must stay in ``DEFAULT_SETTINGS`` so old files continue to load without data
# loss; callers that present or mutate settings should use this set as a
# tombstone boundary.
RETIRED_SETTING_KEYS = frozenset({"default_model_fallbacks"})

# Tiny TTL cache for settings/features. get_setting() is called on hot paths
# (every chat, every preprocess); without this it re-parses the JSON each call.
# Picks up edits within _CACHE_TTL seconds, which is fine for human-edited config.
_CACHE_TTL = 2.0
_settings_cache: tuple[float, dict] | None = None
_features_cache: tuple[float, dict] | None = None

def _invalidate_caches():
    global _settings_cache, _features_cache
    _settings_cache = None
    _features_cache = None

# ── Default values ──

DEFAULT_SETTINGS = {
    # Agent email safety: when True, the MCP send_email / reply_to_email
    # tools don't SMTP directly. They stage the composed message into the
    # scheduled_emails table with status='agent_draft' and return a
    # pending_id + the rendered email so the user can review and approve
    # (or cancel) before it actually goes out. Default ON because models
    # have been observed inventing signatures and sending to real
    # recipients without confirmation.
    "agent_email_confirm": True,
    "image_gen_enabled": False,
    "image_model": "",
    "image_quality": "medium",
    "vision_model": "",
    "vision_enabled": True,
    # Ordered fallback chain for the Vision model (image analysis, OCR, tagging).
    "vision_model_fallbacks": [],
    # Tool-result images (FAUSTUS). A tool that returns an image (an MCP
    # browser screenshot, desktop_screenshot) hands it to the model as an
    # image block when the model can see; the longest side is capped at
    # agent_tool_image_max_px (JPEG q80 when shrunk). Only the last
    # agent_keep_images tool images stay in the prompt; older ones become
    # "[earlier image omitted]" (-1 = keep all).
    "agent_tool_images": True,
    "agent_tool_image_max_px": 1280,
    "agent_keep_images": 1,
    # Desktop input tools (desktop_click/type/key/scroll/focus_window):
    # "ask_each" = approval card on EVERY call, "ask_task" = the normal
    # scoped approval gate, "off" = not offered at all.
    "desktop_control_mode": "ask_each",
    # Destructive command guard (src/command_guard.py): classifies bash/python
    # commands into SAFE/CAUTION/DANGEROUS/CRITICAL. "enforce" = the two
    # destructive tiers need a sealed exact approval before running,
    # "observe" = classify and log receipts only, "off" = no classification.
    "agent_command_guard_mode": "enforce",
    # Which rule packs run: "all" or a comma list of fs, git, db,
    # containers, system.
    "agent_command_guard_packs": "all",
    # Public base URL used to build clickable deep-links in outgoing alerts
    # (e.g., urgency alert email). Example: "https://chat.example.com"
    "app_public_url": "",
    "tts_enabled": True,
    "tts_provider": "disabled",
    "tts_model": "tts-1",
    "tts_voice": "alloy",
    "tts_speed": "1",
    "stt_enabled": False,
    "stt_provider": "disabled",
    "stt_model": "base",
    "stt_language": "",
    "search_provider": "searxng",
    # Default fallback chain — when the primary provider fails or
    # rate-limits, we try DuckDuckGo next. Free, no API key required, so
    # safe to ship on by default for every user.
    "search_fallback_chain": ["duckduckgo"],
    "search_url": "",
    "search_result_count": 5,
    # SafeSearch level applied to every provider that exposes one.
    # "strict"   — apply the provider's strongest filtering level (default;
    #              keeps unrelated low-quality/spam recommendations out)
    # "moderate" — provider-default filtering behavior
    # "off"      — disable filtering entirely (advanced users only)
    #
    # Providers that honor this setting (translated to each provider's native
    # param in src/search/providers.py:_safesearch_for):
    #     SearXNG       safesearch=0/1/2 (JSON API, HTML scrape, news fallback)
    #     Brave Search  safesearch=off/moderate/strict
    #     DuckDuckGo    safesearch=off/moderate/on (library + HTML kp param)
    #     Google PSE    safe=active (omitted for "off"; PSE has no middle tier)
    #     Serper.dev    safe=active (omitted for "off"; proxies Google's `safe`)
    # Providers NOT touched: Tavily (no SafeSearch knob; filters at index time)
    # and any custom backend reached via search_url — they keep whatever the
    # backend itself decides, so operators stay in control of self-hosted /
    # niche search instances.
    "search_safesearch": "strict",
    "brave_api_key": "",
    "google_pse_key": "",
    "google_pse_cx": "",
    "tavily_api_key": "",
    "serper_api_key": "",
    "research_endpoint_id": "",
    "research_model": "",
    "research_search_provider": "",
    "research_max_tokens": 16384,
    "research_extraction_timeout_seconds": 90,
    # Lightweight planning/query LLM calls happen before any search starts.
    # Keep them separately tunable so slow local backends are not capped by
    # the old 30s/60s per-call defaults.
    "research_planning_timeout_seconds": 90,
    "research_query_timeout_seconds": 90,
    "research_extraction_concurrency": 3,
    # Hard wall-clock cap on a single deep-research run. The previous 600s
    # (10 min) default cut off slow local / edge LLMs mid-synthesis; 1800s
    # (30 min) is comfortable for most local setups while still bounding
    # runaway jobs. Set to 0 to disable the cap entirely (unlimited) — only
    # for very long deep-research runs, since a stalled job then runs an
    # unbounded model/API bill. Other values are bounded to [60, 86400].
    # Tune via Settings or by editing data/settings.json.
    "research_run_timeout_seconds": 1800,
    "agent_max_tool_calls": 0,
    "agent_max_rounds": 20,  # per-message agent step cap (clamped 1..200)
    # Soft input-token budget for the agent loop. The DEFAULT value (6000) is the
    # "auto" sentinel: it means "scale the budget to the model's context window"
    # (#1230) — so long-context models aren't capped at 6000. Set ANY OTHER value
    # to enforce an explicit cap (clamped to the window only — hard_max does not
    # apply to explicit budgets, #1230); set 0 to disable soft-trimming. The
    # default is treated as auto because the settings-save path materializes
    # defaults, so a persisted 6000 can't be told apart from a deliberate 6000 —
    # to pin a budget near the default, use a nearby value (e.g. 5999).
    "agent_input_token_budget": 6000,
    # Ceiling on the *auto-derived* input budget; a configurable setting since #1273
    # (the merged #1230 left it a module constant). No effect on an explicit budget
    # — a deliberate value is honoured (#1230). Default matches
    # `src.context_budget.DEFAULT_HARD_MAX`; lower this for
    # cost-paranoid setups, raise it on premium APIs with very large windows you
    # want to actually use (e.g. 900_000 to fill a 1M-context model). See
    # `compute_input_token_budget`.
    "agent_input_token_hard_max": 200_000,
    "agent_stream_timeout_seconds": 300,
    # Local runners (Ollama, llama.cpp…) can sit silent for minutes while they
    # prefill a long prompt: on a local endpoint the per-read inactivity
    # timeout is raised to at least this many seconds (src/agent_loop.py).
    "agent_local_stream_timeout_seconds": 900,
    # Coding turns on a local endpoint run at most at this temperature unless
    # the chat pins one explicitly (FAUSTUS harness). 0 = never cap.
    "agent_local_temperature_cap": 0.4,
    # Thinking watchdog for local thinking models: a round that has produced
    # only reasoning for longer than this is cut off once and retried with
    # think=false for the rest of the turn. 0 disables.
    "agent_local_think_budget_seconds": 240,
    # When the round budget (agent_max_rounds) runs out mid-task the harness
    # injects the "continue" checkpoint itself and grants this many extra
    # cycles of max_rounds before the Continue button appears. 0 = button only.
    "agent_auto_continue_cycles": 1,
    # bash / python tool: a command that prints nothing for this long is killed
    # with its whole process tree (src/agent_tools/subprocess_tools.py). 0 = never.
    "agent_subprocess_idle_timeout_seconds": 300,
    # Workspace coding turns skip personal-memory retrieval (local models weave
    # unrelated facts about the user into the code) — routes/chat_routes.py.
    "agent_workspace_no_memory": True,
    # Tool preflight (src/tool_preflight.py): before the tool list goes out,
    # drop the tools that structurally cannot work in this turn — e.g.
    # `project_context` in a chat that is not inside a project, or the email
    # tools on a box with no mailbox configured. Saves the small local models a
    # round per trap and the schema tokens with it; if a call is made anyway,
    # the model gets the reason instead of a generic "disabled". Only ever
    # removes tools, and never one the workspace floor guarantees. Set false to
    # send every selected tool regardless.
    "agent_tool_preflight": True,
    # ── Reliability harness (src/agent_harness.py and friends) ──
    # Claims-vs-evidence checks, syntax check, fabricated-path detection.
    "agent_harness_checks": True,
    # Shadow snapshot of the workspace before the first change of a turn
    # (src/workspace_checkpoints.py): "restore to before this turn" + per-file
    # diffs without the user's git. Repo size cap and per-file size cap in MB.
    "agent_checkpoints": True,
    "agent_checkpoint_max_repo_mb": 2048,
    "agent_checkpoint_max_file_mb": 8,
    # Run the project's own tests after a turn that changed files
    # (src/project_tests.py): pytest / npm test / cargo / go / make, detected;
    # "related" runs only the test files that name a changed module (pytest),
    # "all" runs the whole suite. One bounded fix round when they fail.
    "agent_project_tests": True,
    "agent_project_tests_scope": "related",
    "agent_project_tests_timeout_seconds": 300,
    "agent_project_tests_fix_rounds": 1,
    # After a failing run, re-run the same test files against the turn's
    # checkpoint to tell new failures from pre-existing ones (no fix round
    # when everything was already failing before the change).
    "agent_project_tests_baseline": True,
    "agent_project_test_command": "",
    # Static analysis of the lines a turn changed, between the syntax check and
    # the project's tests (src/static_checks.py): ruff --select F,E9 / pyflakes,
    # eslint, go vet — correctness rules only, never style, and only findings on
    # lines the turn added (the checkpoint diff decides). "off" | "names" |
    # "types" ("types" also runs tsc --noEmit and cargo check, which are slower).
    # One bounded fix round; with no tool installed it is "unavailable" and
    # costs no round. The override command gets the changed paths appended.
    "agent_static_analysis": "names",
    "agent_static_analysis_command": "",
    # Independent, tool-less review of the turn's diff (src/auto_review.py):
    # "off", "same" (this chat's model) or a model name on the same endpoint.
    "agent_auto_review": "off",
    "agent_auto_review_timeout_seconds": 180,
    "agent_auto_review_fix_round": True,
    "agent_auto_review_fix_rounds": 1,
    # Constrained JSON decoding for the tool-less internal passes that need a
    # JSON answer (today: the diff reviewer). "auto" — when the endpoint is a
    # native Ollama one, the pass sends its JSON Schema in Ollama's `format`
    # and the server masks the logits, so the model cannot emit a token that
    # breaks the schema; a measured 44 % parse rate for the reviewer on a 9B
    # model is what this is for. "off" — no schema is ever sent and every
    # pass falls back to the tolerant text parser it still carries.
    # Never applied to a request that also carries tools (Ollama does not
    # combine `format` with `tools` reliably), so the agent loop is untouched.
    "local_structured_output": "auto",
    # Per-model load defaults for Ollama models (Settings → Local models →
    # Options…, src/model_load_options.py): {"<endpoint_id>|<model>":
    # {"num_ctx", "num_gpu", "keep_alive"}}. Applied under explicit
    # per-request overrides on every Ollama request for that model.
    "model_load_options": {},
    # Standing instructions from the repo (AGENTS.md / CLAUDE.md / …) in the
    # system prompt, and the repository map (files + symbols) before the
    # user's message (src/project_instructions.py, src/repo_map.py).
    "agent_project_instructions": True,
    "agent_project_instructions_max_chars": 6000,
    "agent_repo_map": True,
    "agent_repo_map_tokens": 1500,
    # An un-ranged read_file on a file too big to return whole (src/read_plan.py):
    # instead of the first 20000 characters and nothing else, answer with the
    # line count, the symbol index with line numbers (the same extraction the
    # repo map uses), the first ~80 lines, and the literal call that fetches any
    # other range. Off = the old blind cut off the top. A read that already
    # carries offset/limit is never touched either way.
    "agent_read_outline": True,
    # Share of the model's context window one un-ranged read may occupy. The cap
    # only ever comes DOWN from MAX_READ_CHARS: 20000 characters is a third of an
    # 8k window, so on a small model one unasked-for read evicts the user's own
    # message on the next trim. An unproven window keeps the full ceiling.
    "agent_read_window_fraction": 0.25,
    # Learned memory (src/memory_engine.py): the rules and memories the store
    # scored from turn outcomes, packed into the prompt beside the skills
    # block. Off = nothing is injected and no outcome is attributed; the store
    # keeps whatever it already learned.
    "agent_learned_memory": True,
    "agent_learned_memory_chars": 1800,
    # "@" file mentions from the composer (src/file_mentions.py): the paths the
    # user picked are re-resolved server-side and handed to the model, and small
    # mentioned files ride along inline so it does not spend a round on
    # read_file. 0 inline chars = list the paths only.
    "agent_file_mentions": True,
    "agent_file_mention_inline_chars": 6000,
    # `path:line` references pasted into the message (src/code_refs.py):
    # tracebacks, pytest failures and Node stacks already name the file AND
    # the line, so the lines around each frame ride along with the turn and
    # the model does not spend two rounds of grep + read_file rediscovering
    # what the paste said. 0 chars = list the frames only.
    "agent_code_refs": True,
    "agent_code_ref_chars": 4000,
    # Tails an edit/regenerate would otherwise delete, kept aside so they can
    # be put back (src/chat_versions.py, /versions).
    "chat_versions": True,
    "chat_versions_keep": 10,
    "chat_versions_keep_hours": 168,
    # Per-model scorecard of agent turns (src/scorecard.py, /scorecard).
    "agent_scorecard": True,
    # Detached runs: on-disk replay log (survives restarts) and the task
    # queue — local endpoints share one lane, N runs at a time (1 = one GPU,
    # one generation); 0 = unlimited. API endpoints queue only when their
    # concurrency is > 0.
    "agent_runs_persist": True,
    "agent_runs_keep_hours": 48,
    "agent_queue_local_concurrency": 1,
    "agent_queue_api_concurrency": 0,
    # delegate_agents: add a reviewer worker after the others by default.
    "agent_subagent_reviewer": False,
    # delegate_agents control board: watchdog heartbeat period (s), the idle /
    # loop threshold after which a worker counts as stalled (s), the
    # deterministic supervisor (nudge once, then stop) and how many workers
    # may run at the same time on one GPU (the rest wait, "queued").
    "agent_subagent_tick_seconds": 5,
    "agent_subagent_stall_seconds": 120,
    "agent_subagent_supervisor": True,
    "agent_subagent_max_parallel": 2,
    # Model the workers run on ("" = the coordinator's). Two different models
    # generate at the same time on Ollama; two requests to one model queue on
    # its single slot — pin the worker model to the other card (Local models →
    # Options → main_gpu) and the coordinator and its workers overlap.
    "agent_subagent_worker_model": "",
    # Workers dispatched from OUTSIDE the app (Fable / Claude Desktop through
    # POST /api/dispatch): which endpoint and model they run on. Empty = the
    # utility model, then the default chat model.
    "dispatch_endpoint_id": "",
    "dispatch_model": "",
    # Which card Ollama fills first: -1 = Auto (the freest card, split when
    # nothing fits one), N = pin every model that fits card N to it (a model
    # pinned to a card it does not fit goes to the CPU, so bigger ones stay
    # Auto). src/gpu_policy.py.
    "gpu_placement_prefer": -1,
    # Workers get a lean toolset (no web / memory / skills / background jobs
    # unless the task mentions them): tool schemas were 65 % of a worker's
    # first round on a 9B model.
    "agent_subagent_lean_tools": True,
    # Extra directory roots that read_file / write_file may access, in
    # addition to the built-in project data/ and system temp dirs. Each
    # entry is an absolute path. Sensitive subpaths (.ssh, .gnupg, shell
    # rc files, SSH key files) are always blocked regardless of roots.
    "tool_path_extra_roots": [],
    # ── Built-in browser (Playwright MCP, src/builtin_mcp.py) ──
    # "persistent" keeps cookies/logins in <DATA_DIR>/browser-profile between
    # runs; "isolated" starts every server from a blank in-memory profile.
    "browser_profile": "persistent",
    # Chrome DevTools endpoint of a browser the USER already runs, e.g.
    # "http://127.0.0.1:9222" (start Chrome with --remote-debugging-port=9222).
    # When set, the built-in server attaches to that browser instead of
    # launching its own (headless/profile settings do not apply).
    "browser_cdp_endpoint": "",
    "browser_headless": True,
    # `--caps vision` adds the 6 mouse_*_xy tools, which only make sense with
    # a vision model looking at screenshots; off by default to save schema tokens.
    "browser_vision_caps": False,
    # Budget for the accessibility snapshot text returned to the model by
    # browser_snapshot / browser_navigate (truncated at a line boundary).
    "browser_snapshot_max_chars": 12000,
    # browser_evaluate / browser_run_code_unsafe run model-written JavaScript
    # inside the page; opt-in only. Off → not offered AND denied at dispatch.
    "browser_allow_code_execution": False,
    # After every browser action, capture a viewport frame for the Browser
    # panel in the UI (never sent to the model).
    "browser_live_view": True,
    "task_endpoint_id": "",
    "task_model": "",
    "default_endpoint_id": "",
    "default_model": "",
    # Optional prose style used only for normal document writing/editing.
    # Email replies use email_writing_style instead because greetings,
    # signatures, and mailbox identity rules are medium-specific.
    "document_writing_style": "",
    # Legacy ordered fallback chain for the default chat model. Values remain
    # stored for compatibility and rollback reference, but model routing no
    # longer reads this key.
    "default_model_fallbacks": [],
    # When True, non-admin users inherit the global default model/endpoint when
    # they have no personal defaults. When False, users only use their personal
    # defaults. Default is False.
    "share_defaults_with_users": False,
    "utility_endpoint_id": "",
    "utility_model": "",
    # Ordered fallback chain for the Utility model (summarization, naming,
    # tidy actions, etc.).
    "utility_model_fallbacks": [],
    "teacher_model": "",
    "teacher_enabled": False,
    "teacher_tier2_enabled": False,
    # Skills: minimum self-reported confidence for an auto-written (LLM-authored)
    # DRAFT skill to be injected into the agent prompt. Published skills always
    # qualify. Keeps low-confidence auto-skills out of context until they're
    # vetted/published. 0 disables the gate.
    "skill_autosave_min_confidence": 0.85,
    # Max relevant skills injected into the prompt for one request. The skills
    # library can grow beyond this; cleanup/retirement is an explicit review flow.
    "skill_max_injected": 3,
    # Reminders
    "reminder_channel": "browser",   # "browser" | "email" | "ntfy" | "webhook"
    "reminder_llm_synthesis": False,
    "reminder_llm_persona": "",
    "reminder_ntfy_topic": "Reminders",
    "reminder_email_to": "",
    # Generic outbound webhook channel: pick any saved Integration as the
    # target and supply a JSON payload template. Use {{title}} and {{message}}
    # as placeholders — they are JSON-escaped before substitution, so the
    # rendered string is always valid JSON. Works with Discord, Slack, Teams,
    # ntfy (JSON mode), or any service that accepts a POST with a JSON body.
    "reminder_webhook_integration_id": "",
    "reminder_webhook_payload_template": "",
    # Email triage scanner rules. Running/paused state and schedule live in
    # Tasks via the built-in `check_email_urgency` task.
    "urgent_email_prompt": (
        "Flag as urgent: explicit deadlines, time-sensitive requests, "
        "work-blocking issues, messages from people I report to, or anything "
        "where a delayed reply costs money/trust. Someone waiting outside, "
        "at the door, locked out, or unable to get in is urgent now. "
        "Newsletters, marketing, automated digests, and FYI-only updates are "
        "NOT urgent."
    ),
    # Keyboard shortcuts (action: key combination)
    "keybinds": {
        "search": "ctrl+k",
        "toggle_sidebar": "ctrl+b",
        "new_session": "ctrl+alt+n",
        "star_session": "ctrl+alt+s",
        "delete_session": "ctrl+alt+d",
        "admin_panel": "ctrl+shift+u",
        "cancel": "escape",
    },
}


def without_retired_settings(settings: dict) -> dict:
    """Return a shallow copy suitable for generic settings interfaces."""
    if not isinstance(settings, dict):
        return {}
    return {
        key: value
        for key, value in settings.items()
        if key not in RETIRED_SETTING_KEYS
    }

DEFAULT_FEATURES = {
    "web_search": True,
    "web_fetch": True,
    "deep_research": False,
    "memory": True,
    "document_editor": True,
    "rag": True,
    "sensitive_filter": True,
    "gallery": True,
}


# ── Settings (data/settings.json) ──

def load_settings() -> dict:
    """Load settings merged with defaults. Always returns a complete dict."""
    global _settings_cache
    now = time.monotonic()
    if _settings_cache and (now - _settings_cache[0]) < _CACHE_TTL:
        return _settings_cache[1]
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("settings must be an object")
        merged = {**DEFAULT_SETTINGS, **saved}
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, ValueError):
        merged = dict(DEFAULT_SETTINGS)
    _settings_cache = (now, merged)
    return merged


def save_settings(settings: dict):
    """Persist settings to disk (atomic; see core.atomic_io)."""
    from core.atomic_io import atomic_write_json
    atomic_write_json(SETTINGS_FILE, settings, indent=2)
    _invalidate_caches()


def get_setting(key: str, default: Any = None) -> Any:
    """Read a single setting value."""
    return load_settings().get(key, default)


def is_setting_overridden(key: str) -> bool:
    """True if ``key`` is explicitly present in the saved settings file.

    ``load_settings`` merges DEFAULT_SETTINGS with the saved file, so a value
    equal to its default is indistinguishable from "never set" via get_setting.
    Callers that must distinguish an explicit user choice from a default read
    the raw saved file via this. (Note: a materialized default is also "present",
    so value-sensitive callers should compare against the default — see
    ``context_budget.budget_is_explicit``.)
    """
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        return isinstance(saved, dict) and key in saved
    except (FileNotFoundError, json.JSONDecodeError):
        return False


# Per-user settings (user prefs override the global admin default). Used for
# keys that a user is allowed to choose individually — currently the vision
# model + image-generation model. The owner argument is the authed username
# resolved by FastAPI deps; an empty/None owner falls through to the global.
_PER_USER_KEYS = {
    "vision_model", "vision_enabled", "vision_model_fallbacks",
    "image_model", "image_gen_enabled", "image_quality",
    # Default chat endpoint / model — without per-user resolution every new
    # account inherited whatever the most-recent admin picked, which then
    # got injected into the chat composer on first open.
    "default_endpoint_id", "default_model",
    "utility_endpoint_id", "utility_model", "utility_model_fallbacks",
    "research_endpoint_id", "research_model",
}


def get_user_setting(key: str, owner: str = "", default: Any = None) -> Any:
    """Resolve `key` from the caller's per-user prefs first, falling back to
    the global setting. Only the small whitelist in `_PER_USER_KEYS` is
    eligible — for any other key this is equivalent to `get_setting(key)`.

    Falls back gracefully if the prefs module can't be imported (cycle/early
    boot) — admin-global settings keep working.
    """
    if owner and key in _PER_USER_KEYS:
        try:
            from routes.prefs_routes import _load_for_user
            prefs = _load_for_user(owner) or {}
            if key in prefs and prefs[key] not in (None, ""):
                return prefs[key]
        except Exception:
            pass
    return get_setting(key, default)


# ── Features (data/features.json) ──

def load_features() -> dict:
    """Load feature flags merged with defaults."""
    global _features_cache
    now = time.monotonic()
    if _features_cache and (now - _features_cache[0]) < _CACHE_TTL:
        return _features_cache[1]
    try:
        with open(FEATURES_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("features must be an object")
        merged = {**DEFAULT_FEATURES, **saved}
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, ValueError):
        merged = dict(DEFAULT_FEATURES)
    _features_cache = (now, merged)
    return merged


def save_features(features: dict):
    """Persist feature flags to disk (atomic)."""
    from core.atomic_io import atomic_write_json
    atomic_write_json(FEATURES_FILE, features, indent=2)
    _invalidate_caches()
