"""Standing "do not" constraints of a task, kept in view while the turn runs.

A task that arrives through a coordination space often says "read the sources
in memory, no files, no plan" only in the task body, which the agent reads
after its first prompt. By then the shell is one `curl -o` away. This module
finds those negatives in the user's words and in what coordination tools
return, and gives back a short reminder when a later call would break one.

It only advises: nothing is refused, nothing needs an approval, and a request
that positively asks for a file or a plan is never reminded against.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Set

NO_FILES = "no_files"
NO_PLAN = "no_plan"

_NEG = r"(?:sin|no|without|never|nunca|ni)"
_NO_FILES_RE = re.compile(
    r"\b" + _NEG + r"\s+(?:crear\s+|create\s+|write\s+|escribir\s+|guardar\s+|save\s+)?"
    r"(?:archivos?|ficheros?|files?|copias?|copies|copy|temporales?|temp(?:orary)?\s+files?|disk|disco)\b"
    r"|\b(?:en|in)\s+(?:la\s+)?RAM\b"
    r"|\b(?:en|in)\s+(?:la\s+)?(?:memoria|memory)\s+(?:only|solo|solamente|[uú]nicamente)\b"
    r"|\bin[- ]memory\s+only\b|\bmemory[- ]only\b|\bsolo\s+en\s+(?:RAM|memoria)\b",
    re.IGNORECASE,
)
_NO_PLAN_RE = re.compile(
    r"\b" + _NEG + r"\s+(?:crear\s+|hacer\s+|make\s+|create\s+|write\s+)?"
    r"(?:(?:un\s+)?planes?|plans?|to-?do[- ]?(?:list|lists)|todowrite|lista\s+de\s+tareas|task\s+list)\b",
    re.IGNORECASE,
)
# An explicit ask for a file or a plan overrides the matching reminder.
_WANTS_FILE_RE = re.compile(
    r"\b(?:guarda(?:r)?|descarga(?:r)?|exporta(?:r)?|save|download|export)\b[^.\n]{0,60}"
    r"\b(?:en|a|to|as|como|into)\b[^.\n]{0,40}(?:\.\w{1,5}\b|[\\/])",
    re.IGNORECASE,
)

_SHELL_TOOLS = {"bash", "python", "powershell"}
_FILE_TOOLS = {"write_file", "edit_file", "apply_patch", "create_document"}
_SHELL_WRITE_RE = re.compile(
    r"\bcurl\b[^\n|;&]*?\s(?:-[a-zA-Z]*[oO]\b|--output\b|--remote-name\b)"
    r"|\bwget\b|\bInvoke-WebRequest\b[^\n]*-OutFile|\bOut-File\b|\bSet-Content\b|\bAdd-Content\b"
    r"|\btee\b|(?<![<>=\-\d2])\s>>?\s*(?!/dev/null|\$null|&)[\w./\\~$\"'-]+",
    re.IGNORECASE,
)


def extract(text: str) -> Set[str]:
    """Constraints named by `text`, minus those the same text positively overrides."""
    found: Set[str] = set()
    body = str(text or "")
    if not body:
        return found
    if _NO_FILES_RE.search(body) and not _WANTS_FILE_RE.search(body):
        found.add(NO_FILES)
    if _NO_PLAN_RE.search(body):
        found.add(NO_PLAN)
    return found


def merge(known: Set[str], *texts: str) -> Set[str]:
    for text in texts:
        known |= extract(text)
    return known


def is_coordination_read(tool: str) -> bool:
    name = str(tool or "").lower()
    return "hub_agora_" in name or name.startswith("agora_")


def reminder(known: Iterable[str], tool: str, content: str) -> Optional[str]:
    """One-line reminder if this call would break a standing constraint."""
    known = set(known or ())
    tool = str(tool or "")
    content = str(content or "")
    notes = []
    if NO_FILES in known:
        if tool in _FILE_TOOLS or (tool in _SHELL_TOOLS and _SHELL_WRITE_RE.search(content)):
            notes.append("the task said no files or copies: read sources with web_fetch or "
                         "read_artifact (they stay in memory) instead of writing to disk")
    if NO_PLAN in known and tool == "todowrite":
        notes.append("the task said no plan or task list: skip todowrite and answer directly")
    if not notes:
        return None
    return "[Task constraint] " + "; ".join(notes) + ". Do not claim a cleanup you have not run."
