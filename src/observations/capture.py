"""From a finished tool call to a queued job, without a model.

Everything here is deterministic and cheap, because it runs inside the agent
loop's ``post_tool`` event: it decides whether a call is worth the small model's
time, and condenses the call (tool, arguments, result) to a few hundred
characters. Nothing here waits, reads a file or opens the network.

The pre-filter drops the calls that cannot teach anything: directory listings,
the agent's own bookkeeping, calls the policy refused, empty results, failed
read-only calls, shell commands that only look around, a call identical to one
made a moment ago in the same chat, and the observation tools themselves (an
observer that observed its own reads would feed on itself).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

ARGS_CHARS = 600
RESULT_CHARS = 1500
ARG_VALUE_CHARS = 200

#: Tools that only list or only keep the agent's own notes.
NOISE_TOOLS = frozenset({
    "ls", "glob", "todowrite", "todoread", "tool_search", "tool_help", "think", "wait",
})
READ_ONLY_TOOLS = frozenset({"read_file", "grep", "glob", "ls", "web_search", "web_fetch", "fetch_url"})
MUTATING_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
SHELL_TOOLS = frozenset({"bash", "python", "powershell"})

#: Shell commands that only look around when they are the whole command.
_LOOKING_AROUND = frozenset({
    "ls", "dir", "pwd", "cd", "echo", "which", "where", "whoami", "date", "tree", "env", "printenv",
    "true", "clear", "history",
})
_GIT_LOOKING = re.compile(r"^git\s+(status|log|branch|remote|rev-parse|diff\s+--stat|stash\s+list)\b")
_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")

MIN_READ_CHARS = 40
REPEAT_WINDOW = 30
MAX_TRACKED_SESSIONS = 64


def _short_tool(tool: str) -> str:
    """`mcp__server__name` -> `name`; anything else unchanged."""
    return tool.rsplit("__", 1)[-1] if tool.startswith("mcp__") else tool


def is_observation_tool(tool: str) -> bool:
    name = str(tool or "")
    return "observations" in name or _short_tool(name).startswith("obs_")


def _parse_args(content: Any) -> Any:
    text = content if isinstance(content, str) else ""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except (ValueError, TypeError):
            return text
    return text


def args_summary(content: Any, limit: int = ARGS_CHARS) -> str:
    """The call's arguments in at most `limit` characters; long values (a file
    body being written, a patch) are clipped each so the other arguments stay."""
    parsed = _parse_args(content)
    if isinstance(parsed, dict):
        parts = []
        for key, value in parsed.items():
            if not isinstance(value, str):
                try:
                    value = json.dumps(value, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    value = str(value)
            value = " ".join(value.split())
            if len(value) > ARG_VALUE_CHARS:
                value = value[: ARG_VALUE_CHARS - 1] + "…"
            parts.append(f"{key}={value}")
        text = "; ".join(parts)
    else:
        text = " ".join(str(parsed).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _result_text(result: Any) -> str:
    """The part of a tool result a person would read."""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    pieces: List[str] = []
    for key in ("stdout", "output", "content", "response", "results", "text", "message", "matches", "diff"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            pieces.append(value)
            break
        if isinstance(value, (list, dict)) and value:
            try:
                pieces.append(json.dumps(value, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                pass
            break
    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        pieces.append("stderr: " + stderr)
    error = result.get("error")
    if error:
        pieces.append("error: " + (error if isinstance(error, str) else str(error)))
    if result.get("success") and result.get("path") and not pieces:
        pieces.append(f"wrote {result.get('path')}")
    return "\n".join(pieces)


def result_excerpt(result: Any, limit: int = RESULT_CHARS) -> str:
    """The result in at most `limit` characters: head and tail when it is long
    (a shell command's verdict is usually at the end)."""
    text = _result_text(result).strip()
    if isinstance(result, dict) and result.get("exit_code") not in (0, None):
        text = f"[exit {result.get('exit_code')}] {text}"
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = limit - head - 5
    return text[:head] + "\n...\n" + text[-tail:]


def _redact(text: str) -> str:
    try:
        from core.log_safety import redact_secrets
        return redact_secrets(text)
    except Exception:  # noqa: BLE001 - redaction trouble must not cost the capture
        return text


def _is_failure(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get("error")) or result.get("exit_code") not in (0, None)


def _is_refused(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("approval_required") or result.get("blocked") or result.get("denied"):
        return True
    return str(result.get("status") or "").lower() in {"blocked", "waiting_approval", "denied", "refused"}


def _only_looks_around(command: str) -> bool:
    segments = [s for s in _SEGMENT_SPLIT.split(command.strip()) if s]
    if not segments:
        return True
    for segment in segments:
        words = segment.split()
        first = words[0].lower() if words else ""
        if first in _LOOKING_AROUND or _GIT_LOOKING.match(segment.strip()):
            continue
        return False
    return True


def _shell_command(content: Any) -> str:
    parsed = _parse_args(content)
    if isinstance(parsed, dict):
        value = parsed.get("command") or parsed.get("cmd") or parsed.get("code") or ""
        return value if isinstance(value, str) else ""
    return parsed if isinstance(parsed, str) else ""


class RepeatTracker:
    """Remembers the last few call fingerprints of each chat, so the same call
    made twice in a row (a retry, a re-read) is observed once."""

    def __init__(self, window: int = REPEAT_WINDOW, max_sessions: int = MAX_TRACKED_SESSIONS):
        self._window = window
        self._max = max_sessions
        self._seen: "OrderedDict[str, Deque[str]]" = OrderedDict()
        self._lock = threading.Lock()

    def seen_before(self, session_id: str, fingerprint: str) -> bool:
        """True when `fingerprint` is already in the chat's window; records it either way."""
        with self._lock:
            window = self._seen.get(session_id)
            if window is None:
                window = deque(maxlen=self._window)
                self._seen[session_id] = window
                while len(self._seen) > self._max:
                    self._seen.popitem(last=False)
            else:
                self._seen.move_to_end(session_id)
            if fingerprint in window:
                return True
            window.append(fingerprint)
            return False

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


REPEATS = RepeatTracker()


def fingerprint_of(tool: str, content: Any, outcome: str = "") -> str:
    """The call and what it returned: the same command with a different output
    (tests that now pass) is a new fact, not a repeat."""
    raw = f"{tool}\x00{content if isinstance(content, str) else ''}\x00{outcome[:400]}"
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def noise_reason(tool: str, content: Any, result: Any, *, session_id: str = "",
                 tracker: Optional[RepeatTracker] = None) -> Optional[str]:
    """Why this call is not worth a model call, or None when it is. The only
    check with a side effect is the repeat check, which records the call
    (the same call with the same outcome, not the same call)."""
    name = _short_tool(str(tool or ""))
    if not name:
        return "no_tool"
    if is_observation_tool(str(tool)):
        return "observation_tool"
    if name in NOISE_TOOLS:
        return "listing_tool"
    if _is_refused(result):
        return "refused"
    failed = _is_failure(result)
    if failed and name in READ_ONLY_TOOLS:
        return "failed_read"
    if name in SHELL_TOOLS and not failed and _only_looks_around(_shell_command(content)):
        return "looking_around"
    text = _result_text(result).strip()
    if name not in MUTATING_TOOLS and not text:
        return "empty_result"
    if name == "read_file" and len(text) < MIN_READ_CHARS and not failed:
        return "tiny_read"
    if (tracker or REPEATS).seen_before(session_id or "", fingerprint_of(str(tool), content, text)):
        return "repeat"
    return None


def files_from_call(tool: str, paths: Any) -> Tuple[List[str], List[str]]:
    """(files read, files modified) as far as the tool name tells: the harness
    already extracted the paths a call targets, this only says which way."""
    name = _short_tool(str(tool or ""))
    cleaned = [p.strip() for p in (paths or []) if isinstance(p, str) and p.strip()]
    if name == "read_file":
        return cleaned, []
    if name in MUTATING_TOOLS:
        return [], cleaned
    return [], []


def build_job(ctx: Dict[str, Any], *, turn_id: str = "", ts: float = 0.0) -> Dict[str, Any]:
    """The queue entry for one post_tool event. Cheap: clips and redacts."""
    tool = str(ctx.get("tool") or "")
    content = ctx.get("content")
    result = ctx.get("result")
    read, modified = files_from_call(tool, ctx.get("paths"))
    return {
        "kind": "observation",
        "ts": ts,
        "owner": str(ctx.get("owner") or ""),
        "session_id": str(ctx.get("session_id") or ""),
        "turn_id": turn_id,
        "workspace": str(ctx.get("workspace") or ""),
        "project": str(ctx.get("workspace") or ctx.get("project_id") or ""),
        "tool": tool,
        "args": _redact(args_summary(content)),
        "result": _redact(result_excerpt(result)),
        "failed": _is_failure(result),
        "files_read": read,
        "files_modified": modified,
    }
