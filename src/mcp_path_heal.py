"""mcp_path_heal.py — a saved MCP server that points into an old install.

Live (25-09): two hoards were saved as the bundled REST bridge started from
the app's previous install folder (`<old root>\\venv\\Scripts\\python.exe
<old root>/bridges/rest_mcp/server.py`). The app had since moved to a new
folder; the old one no longer existed, and both servers failed on every
start with "[WinError 2] The system cannot find the file specified".

`heal(command, args)` repairs only that case, and only in memory: a script
argument that does not exist is re-rooted in this app's own folder when the
same trailing path (at least three parts, e.g. `bridges/rest_mcp/server.py`)
exists there; and when that happened and the interpreter it named is gone
too, the interpreter running this app takes its place. A path that exists,
or one that does not lead back into this app, is left exactly as it was.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

APP_ROOT = Path(__file__).resolve().parents[1]

_SCRIPT_RE = re.compile(r"\.(?:py|js|mjs|cjs|ts)$", re.IGNORECASE)
_PYTHON_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?(?:\.exe)?$", re.IGNORECASE)


def _exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except (OSError, ValueError):
        return False


def _command_exists(command: str) -> bool:
    if not command:
        return False
    if _exists(command):
        return True
    has_sep = ("/" in command) or ("\\" in command)
    return (not has_sep) and shutil.which(command) is not None


def _rerooted(arg: str, root: Path) -> str:
    """`arg` re-rooted under `root` by its longest trailing path (3+ parts,
    so a generic `src/main.py` of another program never lands in this app)
    that exists there, or ""."""
    parts = [p for p in re.split(r"[\\/]+", arg) if p and not p.endswith(":")]
    for start in range(1, max(1, len(parts) - 1)):
        tail = parts[start:]
        if len(tail) < 3:
            break
        candidate = root.joinpath(*tail)
        if candidate.is_file():
            return str(candidate)
    return ""


def heal(command: str, args: List[str], root: Path = APP_ROOT) -> Tuple[str, List[str], List[str]]:
    """(command, args, notes): the repaired launch line and what changed."""
    notes: List[str] = []
    new_args: List[str] = []
    rerooted_any = False
    for arg in args or []:
        s = str(arg)
        looks_like_path = ("/" in s or "\\" in s) and bool(_SCRIPT_RE.search(s))
        if looks_like_path and not _exists(s):
            fixed = _rerooted(s, root)
            if fixed:
                notes.append(f"script {s} does not exist; using {fixed}")
                new_args.append(fixed)
                rerooted_any = True
                continue
        new_args.append(s)
    new_command = command
    if rerooted_any and not _command_exists(command or ""):
        base = re.split(r"[\\/]", command or "")[-1]
        if _PYTHON_RE.match(base or ""):
            new_command = sys.executable
            notes.append(f"interpreter {command} does not exist; using {sys.executable}")
    return new_command, new_args, notes
