"""The files a request asks the agent to produce, and whether it has.

A long task that must end in a file (``RESPUESTA.md``, ``informe.csv``) can
spend hours gathering before writing a line: exam 29 ran six hours and two
legs without creating its answer file, so every interruption lost
everything. The loop asks once, a while into the turn, for the file to be
created with its structure and what is known so far (draft first, refine
after). Pure functions; the loop owns the timing.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List

_EXTS = r"(?:md|markdown|txt|csv|tsv|json|yaml|yml|html|docx|xlsx|py|js|ts|sql)"
_FILE = r"[\w./\\-]*[\w-]\." + _EXTS + r"\b"
_VERB = (r"(?:escrib\w*|guard\w*|crea\w*|genera\w*|entreg\w*|deja\w*|redact\w*|rellen\w*|"
         r"write|writes|save|create|produce|generate|deliver|put)")
_ASK = re.compile(_VERB + r"[^.\n]{0,80}?\b(" + _FILE + r")", re.IGNORECASE)
_ASK_AFTER = re.compile(r"\b(" + _FILE + r")[^.\n]{0,40}?\b(?:con|with|que contenga|containing)\b", re.IGNORECASE)
_WRITE_TOOLS = {"write_file", "edit_file", "apply_patch", "append_file"}


def requested_files(text: str) -> List[str]:
    """Basenames of the files the text asks to be written, in order."""
    out: List[str] = []
    for rx in (_ASK, _ASK_AFTER):
        for m in rx.finditer(str(text or "")):
            name = re.split(r"[\\/]", m.group(1))[-1]
            if name and name.lower() not in (n.lower() for n in out):
                out.append(name)
    return out


def _command_text(event: Dict[str, Any]) -> str:
    command = event.get("command")
    if isinstance(command, (dict, list)):
        return json.dumps(command, ensure_ascii=False)
    return str(command or "")


def written(files: Iterable[str], tool_events: Iterable[Dict[str, Any]]) -> bool:
    """True when any write-type tool call in `tool_events` names one of `files`."""
    names = [f.lower() for f in files if f]
    if not names:
        return True
    for event in tool_events or ():
        if not isinstance(event, dict) or str(event.get("tool") or "") not in _WRITE_TOOLS:
            continue
        text = _command_text(event).lower()
        if any(n in text for n in names):
            return True
    return False


def draft_first_note(files: List[str]) -> str:
    shown = ", ".join(files[:3])
    return ("[Harness check — automatic runtime message, not a new user request] You have been "
            f"working a while and {shown} does not exist yet. Create it now with the structure of "
            "the final answer and everything you have established so far (mark open points as "
            "pending), then keep filling it in as you go. A long task that is interrupted keeps "
            "what is already in the file.")


def exists_in(workspace: str, files: Iterable[str], max_depth: int = 3) -> bool:
    """True when one of `files` already exists under `workspace` (a previous
    turn may have created it). Shallow walk; never raises."""
    import os
    names = {f.lower() for f in files if f}
    root = str(workspace or "")
    if not names or not root or not os.path.isdir(root):
        return False
    base_depth = root.rstrip("\\/").count(os.sep)
    try:
        for current, dirs, entries in os.walk(root):
            if current.count(os.sep) - base_depth >= max_depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", ".venv")]
            if any(e.lower() in names for e in entries):
                return True
    except OSError:
        return False
    return False
