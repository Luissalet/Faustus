"""src/lifecycle_hooks.py — user-configurable automation on the agent's
lifecycle (lot H).

Lets the user attach small pieces of automation to points in the agent's
turn without touching code: run a shell command and feed its output back to
the model, inject a fixed note, or emit a warning when a pattern matches. The
explicit design constraint is **more capability, zero new bureaucracy**:
hooks augment what the model sees, they never deny or block a tool call and
never require a human approval by themselves (that is `tool_arg_rules`'s job,
a separate module — a hook cannot substitute for it).

Events (`EVENTS`)
------------------
    session_start  first turn of a chat session
    turn_start     every turn, after the user's message is known
    pre_tool       before a tool call executes
    post_tool      after the tool result is available
    turn_end       after the final assistant message of a turn
    pre_compact    right before mid-turn context compaction

Hook record — one entry of the `lifecycle_hooks` setting (a list, default
`[]`):

    {
        "id": "format-py",                 # slug, unique
        "enabled": True,
        "event": "post_tool",
        "name": "Format Python after edit",
        "match": {"tool": "edit_file|write_file", "path": "*.py"},
        "action": "command",               # "command" | "inject" | "warn"
        "command": "ruff format {file}",
        "timeout_s": 20,
        "max_output_chars": 4000,
        "scope": "global",                  # "global" | "project"
        "note": "",
    }

`match` keys (`tool`, `path`, `command`, `text`) are ANDed together; an empty
`match` (`{}`) matches every call of that event. `fnmatch` has no
alternation, so `match.tool` / `match.path` accept a `|`-separated list of
globs (any one matching is enough). `match.command` / `match.text` are
regexes searched (not fullmatched) against the tool's command text / the
user's message.

Public API
----------
    evaluate(event, ctx) -> List[HookHit]
    run(event, ctx, *, emit=None) -> List[HookResult]        (sync)
    run_async(event, ctx, *, emit=None) -> List[HookResult]  (async)
    render_context(results) -> str
    attach_to_result(result_dict, results) -> dict
    validate_hooks(raw) -> list                              (raises HookError)
    PRESETS, preset(preset_id), apply_preset(existing, preset_id)
    recent(limit=50) -> list
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EVENTS = (
    "session_start",
    "turn_start",
    "pre_tool",
    "post_tool",
    "turn_end",
    "pre_compact",
)
ACTIONS = ("command", "inject", "warn")

_MATCH_KEYS = frozenset({"tool", "path", "command", "text"})
_SCOPES = ("global", "project")

DEFAULT_COMMAND_TIMEOUT_S = 20
MAX_COMMAND_TIMEOUT_S = 120
DEFAULT_TOTAL_TIMEOUT_S = 45
DEFAULT_MAX_OUTPUT_CHARS = 4000

# Timeout-safety bound for the "command"/"text" match regex — same reasoning
# as src/tool_arg_policy.py's MAX_REGEX_PATTERN_LEN: no true timeout for
# stdlib `re`, so the pattern's own length is capped at save time.
MAX_MATCH_REGEX_LEN = 300
MAX_MATCH_REGEX_INPUT_LEN = 20000

_TRUNC_MARKER = "\n...[truncated]...\n"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Rotation: same approach as src/project_audit.py — append-only, and past
# ROTATE_MAX_BYTES the file is rewritten with only its newest
# ROTATE_KEEP_LINES lines, atomically (tmp + os.replace).
ROTATE_MAX_BYTES = 2 * 1024 * 1024
ROTATE_KEEP_LINES = 2000

_LOG_LOCK = threading.Lock()


class HookError(ValueError):
    """A hook (or the hooks list) this module refuses to store or run."""


# `evaluate` returns normalized hook dicts (the shape `validate_hooks`
# produces) — a thin alias so callers can type-hint without importing a
# dataclass that carries no behaviour of its own.
HookHit = Dict[str, Any]


@dataclass
class HookResult:
    hook_id: str
    name: str
    event: str
    action: str
    ok: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    level: str = "info"  # "info" | "warn"
    exit_code: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "hook_id": self.hook_id, "name": self.name, "event": self.event,
            "action": self.action, "ok": self.ok, "output": self.output,
            "error": self.error, "duration_ms": self.duration_ms,
            "timed_out": self.timed_out, "level": self.level,
            "exit_code": self.exit_code,
        }


# ---------------------------------------------------------------------------
# settings / data-dir plumbing (lazy imports at call time — see
# src/skill_import_review.py and src/project_audit.py::_dir for the pattern;
# tests set ODYSSEUS_DATA_DIR / monkeypatch src.constants.DATA_DIR)
# ---------------------------------------------------------------------------

def _get_setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - settings unavailable is not fatal
        return default


def _log_dir() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "lifecycle_hooks")


def _log_path() -> str:
    return os.path.join(_log_dir(), "log.jsonl")


def _load_hooks() -> List[dict]:
    raw = _get_setting("lifecycle_hooks", []) or []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict) and item.get("id") and item.get("event") in EVENTS:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HookError(message)


def _validate_match_regex(hook_id: str, key: str, pattern: str) -> None:
    _require(
        len(pattern) <= MAX_MATCH_REGEX_LEN,
        f"hook {hook_id}: match.{key} pattern too long (max {MAX_MATCH_REGEX_LEN} chars)",
    )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise HookError(f"hook {hook_id}: invalid match.{key} regex: {exc}") from exc


def _validate_hook(raw: Any) -> dict:
    _require(isinstance(raw, dict), "each hook must be an object")
    hook_id = str(raw.get("id") or "").strip()
    _require(bool(hook_id), "hook id is required")
    _require(
        bool(_SLUG_RE.match(hook_id)),
        f"hook {hook_id!r}: id must be a slug (lowercase letters, digits, '-' or '_', max 64 chars)",
    )
    event = str(raw.get("event") or "").strip()
    _require(event in EVENTS, f"hook {hook_id}: event must be one of {list(EVENTS)}")
    action = str(raw.get("action") or "").strip()
    _require(action in ACTIONS, f"hook {hook_id}: action must be one of {list(ACTIONS)}")
    name = str(raw.get("name") or hook_id).strip() or hook_id
    enabled = bool(raw.get("enabled", True))

    match_raw = raw.get("match") if raw.get("match") is not None else {}
    _require(isinstance(match_raw, dict), f"hook {hook_id}: match must be an object")
    match: Dict[str, str] = {}
    for key, value in match_raw.items():
        _require(key in _MATCH_KEYS, f"hook {hook_id}: unknown match key {key!r}")
        text = str(value or "").strip()
        if not text:
            continue
        if key in ("command", "text"):
            _validate_match_regex(hook_id, key, text)
        match[key] = text

    command = raw.get("command")
    text_tpl = raw.get("text")
    if action == "command":
        _require(
            isinstance(command, str) and command.strip(),
            f"hook {hook_id}: command is required for action 'command'",
        )
    else:
        _require(
            isinstance(text_tpl, str) and text_tpl.strip(),
            f"hook {hook_id}: text is required for action {action!r}",
        )

    timeout_s = raw.get("timeout_s")
    if timeout_s is not None:
        _require(
            isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool)
            and 1 <= timeout_s <= MAX_COMMAND_TIMEOUT_S,
            f"hook {hook_id}: timeout_s must be between 1 and {MAX_COMMAND_TIMEOUT_S}",
        )
        timeout_s = int(timeout_s)

    max_output_chars = raw.get("max_output_chars")
    if max_output_chars is not None:
        _require(
            isinstance(max_output_chars, (int, float)) and not isinstance(max_output_chars, bool)
            and max_output_chars > 0,
            f"hook {hook_id}: max_output_chars must be a positive number",
        )
        max_output_chars = int(max_output_chars)

    scope = str(raw.get("scope") or "global").strip() or "global"
    _require(scope in _SCOPES, f"hook {hook_id}: scope must be one of {list(_SCOPES)}")

    note = str(raw.get("note") or "")

    return {
        "id": hook_id,
        "enabled": enabled,
        "event": event,
        "name": name,
        "match": match,
        "action": action,
        "command": command if isinstance(command, str) else None,
        "text": text_tpl if isinstance(text_tpl, str) else None,
        "timeout_s": timeout_s,           # None -> falls back to the setting at run time
        "max_output_chars": max_output_chars if max_output_chars is not None else DEFAULT_MAX_OUTPUT_CHARS,
        "scope": scope,
        "note": note,
    }


def validate_hooks(raw: Any) -> list:
    """Validate a whole `lifecycle_hooks` list, raising `HookError` (message
    suitable for a 400 response) on the first problem. Returns the normalized
    list on success."""
    _require(isinstance(raw, list), "lifecycle_hooks must be a list")
    seen: set = set()
    checked = []
    for item in raw:
        hook = _validate_hook(item)
        _require(hook["id"] not in seen, f"duplicate hook id {hook['id']!r}")
        seen.add(hook["id"])
        checked.append(hook)
    return checked


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def _glob_alternatives(pattern: str) -> List[str]:
    return [g for g in (pattern or "").split("|") if g]


def _match_tool(pattern: str, tool: Any) -> bool:
    tool_name = str(tool or "")
    return any(fnmatch.fnmatchcase(tool_name, g) for g in _glob_alternatives(pattern))


def _match_path(pattern: str, paths: Any) -> bool:
    if not paths:
        return False
    globs = _glob_alternatives(pattern)
    if not globs:
        return False
    for raw_path in paths:
        candidate = str(raw_path or "")
        if not candidate:
            continue
        base = os.path.basename(candidate.replace("\\", "/"))
        for glob in globs:
            if fnmatch.fnmatch(candidate, glob) or fnmatch.fnmatch(base, glob):
                return True
    return False


def _match_regex(pattern: str, text: Any) -> bool:
    haystack = text if isinstance(text, str) else ""
    if not haystack or not pattern:
        return False
    if len(pattern) > MAX_MATCH_REGEX_LEN or len(haystack) > MAX_MATCH_REGEX_INPUT_LEN:
        return False
    try:
        return re.search(pattern, haystack) is not None
    except re.error:
        return False


def _hook_matches(hook: dict, ctx: Dict[str, Any]) -> bool:
    if not hook.get("enabled", True):
        return False
    if hook.get("scope") == "project" and not ctx.get("workspace"):
        return False
    match = hook.get("match") or {}
    if "tool" in match and not _match_tool(match["tool"], ctx.get("tool")):
        return False
    if "path" in match and not _match_path(match["path"], ctx.get("paths") or []):
        return False
    if "command" in match and not _match_regex(match["command"], ctx.get("command")):
        return False
    if "text" in match and not _match_regex(match["text"], ctx.get("user_message")):
        return False
    return True


def evaluate(event: str, ctx: Optional[Dict[str, Any]] = None) -> List[HookHit]:
    """Pure, deterministic: the enabled hooks of `event` whose `match` holds
    against `ctx`. `ctx` keys are all optional: `tool`, `content`, `args`,
    `paths`, `command`, `user_message`, `workspace`, `session_id`."""
    ctx = ctx or {}
    if event not in EVENTS:
        return []
    return [hook for hook in _load_hooks() if hook.get("event") == event and _hook_matches(hook, ctx)]


# ---------------------------------------------------------------------------
# template rendering
# ---------------------------------------------------------------------------

def _quote_for_shell(value: str) -> str:
    if not value:
        return value
    if sys.platform.startswith("win"):
        if re.search(r'[\s"^&|<>()]', value):
            return '"' + value.replace('"', '\\"') + '"'
        return value
    return shlex.quote(value)


def _relativize(path: str, workspace: str) -> str:
    if not path:
        return ""
    if workspace:
        try:
            rel = os.path.relpath(path, workspace)
            if not rel.startswith(".."):
                return rel.replace("\\", "/") if not sys.platform.startswith("win") else rel
        except (ValueError, OSError):
            pass
    return path


def _base_values(ctx: Dict[str, Any]) -> Dict[str, Any]:
    workspace = str(ctx.get("workspace") or "")
    raw_paths = [str(p) for p in (ctx.get("paths") or []) if p]
    rel_paths = [_relativize(p, workspace) for p in raw_paths]
    user_message = str(ctx.get("user_message") or "")[:500]
    return {
        "tool": str(ctx.get("tool") or ""),
        "file": rel_paths[0] if rel_paths else "",
        "files": rel_paths,
        "command": str(ctx.get("command") or ""),
        "workspace": workspace,
        "event": str(ctx.get("event") or ""),
        "session_id": str(ctx.get("session_id") or ""),
        "user_message": user_message,
    }


def _render(template: Optional[str], ctx: Dict[str, Any], *, quote_files: bool) -> str:
    """Substitute `{tool}`, `{file}`, `{files}`, `{command}`, `{workspace}`,
    `{event}`, `{session_id}`, `{user_message}` in `template`. Unknown
    placeholders stay literal. `{file}`/`{files}` are shell-quoted when the
    result feeds a `command` template (`quote_files=True`)."""
    if not isinstance(template, str) or not template:
        return ""
    base = _base_values(ctx)
    files_list = base.pop("files")
    if quote_files:
        file_value = _quote_for_shell(base["file"]) if base["file"] else ""
        files_value = " ".join(_quote_for_shell(p) for p in files_list) if files_list else ""
    else:
        file_value = base["file"]
        files_value = " ".join(files_list)
    values = dict(base)
    values["file"] = file_value
    values["files"] = files_value

    def _sub(m: "re.Match[str]") -> str:
        return values.get(m.group(1), m.group(0))

    return _PLACEHOLDER_RE.sub(_sub, template)


def render_command(template: Optional[str], ctx: Dict[str, Any]) -> str:
    return _render(template, ctx, quote_files=True)


def render_text(template: Optional[str], ctx: Dict[str, Any]) -> str:
    return _render(template, ctx, quote_files=False)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head_len = max(0, limit // 2)
    tail_len = max(0, limit - head_len - len(_TRUNC_MARKER))
    return text[:head_len] + _TRUNC_MARKER + text[len(text) - tail_len:]


async def _spawn(command: str, *, cwd: Optional[str]):
    """Start the hook's command with Bash semantics on Windows too — the same
    approach `src/agent_tools/subprocess_tools.py::_create_bash_subprocess`
    uses for the Bash tool (Git Bash when available, otherwise the platform
    default `asyncio.create_subprocess_shell` falls back to, i.e. cmd.exe on
    native Windows)."""
    if sys.platform.startswith("win"):
        try:
            from core.platform_compat import find_bash
            bash = find_bash()
        except Exception:  # noqa: BLE001 - never let discovery break a hook
            bash = None
        if bash:
            return await asyncio.create_subprocess_exec(
                bash, "-c", command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
    )


async def _run_command_subprocess(
    command: str, *, cwd: Optional[str], timeout_s: float, max_output_chars: int,
):
    """Returns (output, spawn_error, exit_code, timed_out, duration_ms)."""
    start = time.monotonic()
    try:
        proc = await _spawn(command, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 - e.g. missing Git Bash, bad cwd
        return "", str(exc), None, False, int((time.monotonic() - start) * 1000)

    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            out_b, err_b = await proc.communicate()
        except Exception:  # noqa: BLE001
            out_b, err_b = b"", b""

    duration_ms = int((time.monotonic() - start) * 1000)
    out = out_b.decode("utf-8", errors="replace") if out_b else ""
    err = err_b.decode("utf-8", errors="replace") if err_b else ""
    combined = out
    if err:
        combined = f"{out}\n[stderr]\n{err}" if out else f"[stderr]\n{err}"
    combined = _truncate(combined, max_output_chars)
    exit_code = None if timed_out else proc.returncode
    return combined, "", exit_code, timed_out, duration_ms


async def _run_one(hook: dict, ctx: Dict[str, Any], default_cmd_timeout: Any) -> HookResult:
    start = time.monotonic()
    hook_id = hook["id"]
    name = hook.get("name") or hook_id
    event = hook.get("event", "")
    action = hook.get("action", "")
    try:
        if action == "command":
            timeout_s = hook.get("timeout_s")
            if timeout_s is None:
                timeout_s = default_cmd_timeout
            try:
                timeout_s = float(timeout_s)
            except (TypeError, ValueError):
                timeout_s = DEFAULT_COMMAND_TIMEOUT_S
            timeout_s = min(max(1.0, timeout_s), float(MAX_COMMAND_TIMEOUT_S))
            command = render_command(hook.get("command"), ctx)
            max_chars = hook.get("max_output_chars") or DEFAULT_MAX_OUTPUT_CHARS
            cwd = ctx.get("workspace") or None
            output, spawn_err, exit_code, timed_out, duration_ms = await _run_command_subprocess(
                command, cwd=cwd, timeout_s=timeout_s, max_output_chars=max_chars,
            )
            if timed_out:
                return HookResult(hook_id, name, event, action, ok=False, output=output,
                                   error=f"timed out after {timeout_s:.0f}s", duration_ms=duration_ms,
                                   timed_out=True, level="warn", exit_code=exit_code)
            if spawn_err:
                return HookResult(hook_id, name, event, action, ok=False, error=spawn_err,
                                   duration_ms=duration_ms, level="warn")
            level = "warn" if exit_code not in (0, None) else "info"
            return HookResult(hook_id, name, event, action, ok=True, output=output,
                               duration_ms=duration_ms, level=level, exit_code=exit_code)

        if action in ("inject", "warn"):
            rendered = render_text(hook.get("text"), ctx)
            duration_ms = int((time.monotonic() - start) * 1000)
            return HookResult(hook_id, name, event, action, ok=True, output=rendered,
                               duration_ms=duration_ms, level="warn" if action == "warn" else "info")

        return HookResult(hook_id, name, event, action, ok=False,
                           error=f"unknown action {action!r}",
                           duration_ms=int((time.monotonic() - start) * 1000))
    except Exception as exc:  # noqa: BLE001 - a hook must never raise into the caller
        return HookResult(hook_id, name, event, action, ok=False, error=str(exc),
                           duration_ms=int((time.monotonic() - start) * 1000), level="warn")


async def run_async(
    event: str, ctx: Optional[Dict[str, Any]] = None, *, emit=None,
) -> List[HookResult]:
    """Execute every enabled hook of `event` whose match holds, in order,
    under a total wall-clock cap (`lifecycle_hooks_total_timeout_seconds`).
    Never raises — a per-hook failure becomes `HookResult(ok=False, ...)` and
    execution continues with the next hook."""
    ctx = dict(ctx or {})
    ctx.setdefault("event", event)

    if not _get_setting("lifecycle_hooks_enabled", True):
        return []

    hooks = evaluate(event, ctx)
    if not hooks:
        return []

    total_timeout = _get_setting("lifecycle_hooks_total_timeout_seconds", DEFAULT_TOTAL_TIMEOUT_S)
    try:
        total_timeout = float(total_timeout)
        if total_timeout <= 0:
            total_timeout = float(DEFAULT_TOTAL_TIMEOUT_S)
    except (TypeError, ValueError):
        total_timeout = float(DEFAULT_TOTAL_TIMEOUT_S)
    default_cmd_timeout = _get_setting("lifecycle_hooks_command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_S)

    deadline = time.monotonic() + total_timeout
    results: List[HookResult] = []
    for hook in hooks:
        if time.monotonic() >= deadline:
            results.append(HookResult(
                hook["id"], hook.get("name") or hook["id"], event, hook.get("action", ""),
                ok=False, error="skipped: lifecycle hooks total timeout exceeded", level="warn",
            ))
            continue
        result = await _run_one(hook, ctx, default_cmd_timeout)
        results.append(result)
        _log_result(result)
        if emit is not None:
            try:
                emit(result)
            except Exception:  # noqa: BLE001 - a bad emit callback must not lose the run
                logger.debug("[lifecycle_hooks] emit callback failed", exc_info=True)
    return results


def run(event: str, ctx: Optional[Dict[str, Any]] = None, *, emit=None) -> List[HookResult]:
    """Sync wrapper around `run_async`. Safe to call whether or not the
    caller is already inside a running event loop (integrator call sites in
    `src/agent_loop.py` are async; some UI/admin call sites are not)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_async(event, ctx, emit=emit))

    # Already inside a running loop: asyncio.run() would raise ("cannot be
    # called from a running event loop"), so hand the coroutine to a fresh
    # loop on its own thread and block for it — hooks are a bounded, capped
    # operation (lifecycle_hooks_total_timeout_seconds), never a long-lived one.
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = asyncio.run(run_async(event, ctx, emit=emit))
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result", [])


# ---------------------------------------------------------------------------
# integrator-facing glue
# ---------------------------------------------------------------------------

def render_context(results: List[HookResult]) -> str:
    """The untrusted text block for `session_start`/`turn_start`: one section
    per hook that produced output, empty string when there is none."""
    sections = []
    for r in results:
        text = r.output if r.output else r.error
        if not text:
            continue
        sections.append(f"### {r.name} ({r.action})\n{text}")
    return "\n\n".join(sections)


def attach_to_result(result_dict: dict, results: List[HookResult]) -> dict:
    """For `pre_tool`/`post_tool`: adds `result_dict["hook_notes"]` (via
    `setdefault`, so it is a no-op if that key is already present) — never
    touches any other existing key."""
    if not isinstance(result_dict, dict) or not results:
        return result_dict
    notes = [
        {
            "hook_id": r.hook_id, "name": r.name, "level": r.level,
            "output": r.output if r.output else r.error,
        }
        for r in results
    ]
    result_dict.setdefault("hook_notes", notes)
    return result_dict


# ---------------------------------------------------------------------------
# event log (<DATA_DIR>/lifecycle_hooks/log.jsonl)
# ---------------------------------------------------------------------------

def _rotate_if_needed(path: str) -> None:
    try:
        if ROTATE_MAX_BYTES <= 0 or os.path.getsize(path) <= ROTATE_MAX_BYTES:
            return
    except OSError:
        return
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max(1, int(ROTATE_KEEP_LINES)))
        with open(tmp, "w", encoding="utf-8") as out:
            for line in tail:
                out.write(line if line.endswith("\n") else line + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except (OSError, ValueError) as exc:
        logger.debug("[lifecycle_hooks] log rotation failed: %s", exc)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _log_result(result: HookResult) -> None:
    entry = result.as_dict()
    entry["ts"] = int(time.time())
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _rotate_if_needed(path)
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("[lifecycle_hooks] log write failed: %s", exc)


def recent(limit: int = 50) -> List[Dict[str, Any]]:
    """The newest `limit` logged hook runs, newest first."""
    path = _log_path()
    if not os.path.isfile(path):
        return []
    keep = deque(maxlen=max(1, int(limit)) * 4 or 1)
    # Read the whole tail window generously (4x limit) so a burst of one
    # event's hooks does not push an older, still-relevant row out before the
    # trim below — cheap: the log itself is capped at ROTATE_KEEP_LINES.
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    keep.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    rows = list(keep)
    rows.reverse()
    return rows[: max(1, int(limit))]


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------

PRESETS: List[dict] = [
    {
        "preset_id": "format_python_after_edit",
        "description": "Format a Python file with ruff (falling back to black) right after it is edited.",
        "id": "format_python_after_edit",
        "enabled": True,
        "event": "post_tool",
        "name": "Format Python after edit",
        "match": {"tool": "edit_file|write_file|apply_patch", "path": "*.py"},
        "action": "command",
        "command": "ruff format {file} || black -q {file}",
        "text": None,
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "Silently a no-op if neither ruff nor black is installed.",
    },
    {
        "preset_id": "format_web_after_edit",
        "description": "Format a TS/JS/CSS file with prettier right after it is edited.",
        "id": "format_web_after_edit",
        "enabled": True,
        "event": "post_tool",
        "name": "Format web files after edit",
        "match": {"tool": "edit_file|write_file|apply_patch", "path": "*.ts|*.tsx|*.js|*.jsx|*.css"},
        "action": "command",
        "command": "npx --no-install prettier --write {file}",
        "text": None,
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "Requires prettier as a project devDependency.",
    },
    {
        "preset_id": "typecheck_ts_after_edit",
        "description": "Run the TypeScript compiler in --noEmit mode after a .ts/.tsx edit.",
        "id": "typecheck_ts_after_edit",
        "enabled": True,
        "event": "post_tool",
        "name": "Typecheck TS after edit",
        "match": {"tool": "edit_file|write_file|apply_patch", "path": "*.ts|*.tsx"},
        "action": "command",
        "command": "npx --no-install tsc --noEmit -p . 2>&1 | head -n 40",
        "text": None,
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "Requires a tsconfig.json at the workspace root.",
    },
    {
        "preset_id": "git_context_on_session_start",
        "description": "Show short git status and recent commits at the start of a project session.",
        "id": "git_context_on_session_start",
        "enabled": True,
        "event": "session_start",
        "name": "Git context on session start",
        "match": {},
        "action": "command",
        "command": "git status --short | head -n 40 && git log --oneline -n 8",
        "text": None,
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "project",
        "note": "Only fires when a workspace is set (scope=project).",
    },
    {
        "preset_id": "console_log_warning",
        "description": "Nudge to remove debug logging when a JS/TS file is edited.",
        "id": "console_log_warning",
        "enabled": True,
        "event": "post_tool",
        "name": "console.log warning",
        "match": {"path": "*.ts|*.tsx|*.js|*.jsx"},
        "action": "warn",
        "command": None,
        "text": "A file with console.log statements was edited; remove debug logging before finishing.",
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "",
    },
    {
        "preset_id": "build_finished_note",
        "description": "Remind the model to check a build command's exit code once it has run.",
        "id": "build_finished_note",
        "enabled": True,
        "event": "post_tool",
        "name": "Build finished note",
        "match": {"tool": "bash", "command": r"(npm|pnpm|yarn) run build|cargo build|go build|vite build"},
        "action": "inject",
        "command": None,
        "text": "Build command completed — check the exit code above and surface failures to the user.",
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "",
    },
    {
        "preset_id": "dev_server_warning",
        "description": "Warn before launching a foreground dev server that could hang the turn.",
        "id": "dev_server_warning",
        "enabled": True,
        "event": "pre_tool",
        "name": "Foreground dev server warning",
        "match": {"tool": "bash", "command": r"(npm|pnpm|yarn) (run )?dev\b|vite\b(?!.*build)|uvicorn .* --reload"},
        "action": "warn",
        "command": None,
        "text": "This looks like a foreground dev server; prefer a detached/background launch so the turn does not hang.",
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "",
    },
    {
        "preset_id": "no_verify_warning",
        "description": "Warn when a command bypasses a verification hook with --no-verify.",
        "id": "no_verify_warning",
        "enabled": True,
        "event": "pre_tool",
        "name": "--no-verify warning",
        "match": {"command": r"--no-verify"},
        "action": "warn",
        "command": None,
        "text": "This command bypasses a safety/verification hook (--no-verify) — confirm this is intentional before running it.",
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "",
    },
    {
        "preset_id": "compact_note",
        "description": "Keep a short checklist alive across mid-turn context compaction.",
        "id": "compact_note",
        "enabled": True,
        "event": "pre_compact",
        "name": "Compaction note",
        "match": {},
        "action": "inject",
        "command": None,
        "text": "Before compaction: keep the task list, file paths touched and pending verification steps.",
        "timeout_s": None,
        "max_output_chars": DEFAULT_MAX_OUTPUT_CHARS,
        "scope": "global",
        "note": "",
    },
]


def preset(preset_id: str) -> dict:
    for p in PRESETS:
        if p["preset_id"] == preset_id:
            return dict(p)
    raise HookError(f"unknown preset {preset_id!r}")


def apply_preset(existing: Optional[list], preset_id: str) -> list:
    """Add `preset_id`'s hook to `existing` (a `lifecycle_hooks`-shaped list)
    and return the normalized, validated result. Idempotent by id: a hook
    already carrying that id is left untouched."""
    p = preset(preset_id)
    hook = {k: v for k, v in p.items() if k not in ("preset_id", "description")}
    hooks = list(existing or [])
    if any(isinstance(h, dict) and h.get("id") == hook["id"] for h in hooks):
        return validate_hooks(hooks)
    hooks.append(hook)
    return validate_hooks(hooks)
