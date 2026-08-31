"""project_instructions.py — standing instructions the project keeps in its own files.

Coding agents have converged on a convention: a Markdown file at the root of
the repository (AGENTS.md, CLAUDE.md, …) that tells the agent how the project
works — conventions, how to run the tests, what not to touch. Faustus injects
that file into the system prompt of every turn that has a workspace, so a
local model does not have to rediscover (or invent) the rules each time.

Lookup order (first existing file wins, unless the setting lists otherwise):
    AGENTS.md, CLAUDE.md, .odysseus/INSTRUCTIONS.md, ODYSSEUS.md,
    .cursorrules, CONVENTIONS.md, .github/copilot-instructions.md

The block is byte-identical across turns until the file changes (KV-cache
friendly), capped at `agent_project_instructions_max_chars`, and cached by
mtime. Stdlib only, never raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_FILES = (
    "AGENTS.md", "CLAUDE.md", os.path.join(".odysseus", "INSTRUCTIONS.md"), "ODYSSEUS.md",
    ".cursorrules", "CONVENTIONS.md", os.path.join(".github", "copilot-instructions.md"),
)
DEFAULT_MAX_CHARS = 6000
_CACHE: Dict[str, Tuple[float, Optional[str], float, str]] = {}   # root → (checked_at, path, mtime, block)
_LOCK = threading.Lock()
_TTL_S = 5.0


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def candidate_files() -> List[str]:
    raw = _setting("agent_project_instructions_files", None)
    if isinstance(raw, str) and raw.strip():
        return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if isinstance(raw, (list, tuple)) and raw:
        return [str(p).strip() for p in raw if str(p).strip()]
    return list(DEFAULT_FILES)


def find_file(workspace: str) -> Optional[str]:
    """Absolute path of the first instructions file that exists, or None."""
    if not workspace:
        return None
    root = os.path.realpath(os.path.expanduser(workspace))
    for rel in candidate_files():
        p = os.path.join(root, rel)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


def read(workspace: str) -> Dict[str, Any]:
    """{"path", "rel", "text", "truncated", "chars"} or an empty dict."""
    p = find_file(workspace)
    if not p:
        return {}
    try:
        limit = int(_setting("agent_project_instructions_max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    limit = max(500, min(limit, 60_000))
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(limit + 1)
    except OSError:
        return {}
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    root = os.path.realpath(os.path.expanduser(workspace))
    return {
        "path": p,
        "rel": os.path.relpath(p, root).replace(os.sep, "/"),
        "text": text.replace("\r\n", "\n").strip(),
        "truncated": truncated,
        "chars": len(text),
    }


def block(workspace: str) -> str:
    """The system-prompt section, '' when the feature is off or no file exists."""
    if not workspace or not bool(_setting("agent_project_instructions", True)):
        return ""
    root = os.path.realpath(os.path.expanduser(workspace))
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(root)
    if cached and now - cached[0] < _TTL_S:
        return cached[3]
    p = find_file(root)
    mtime = 0.0
    if p:
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0.0
    if cached and cached[1] == p and cached[2] == mtime:
        with _LOCK:
            _CACHE[root] = (now, p, mtime, cached[3])
        return cached[3]
    info = read(root) if p else {}
    text = ""
    if info.get("text"):
        note = " (truncated — read the file for the rest)" if info.get("truncated") else ""
        text = (
            f"\n\n## Project instructions from {info['rel']}{note}\n"
            "These are the project's standing rules, written by its maintainers. Follow them "
            "(conventions, how to run tests, what not to touch) unless the user says otherwise.\n"
            f"{info['text']}"
        )
    with _LOCK:
        _CACHE[root] = (now, p, mtime, text)
    if text:
        logger.debug("[instructions] injecting %s (%d chars)", info.get("rel"), len(info.get("text") or ""))
    return text


def invalidate(workspace: Optional[str] = None) -> None:
    with _LOCK:
        if workspace:
            _CACHE.pop(os.path.realpath(os.path.expanduser(workspace)), None)
        else:
            _CACHE.clear()
