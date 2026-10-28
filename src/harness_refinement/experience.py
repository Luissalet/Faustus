"""EXPERIENCE.md: what earlier harness probes in one domain taught.

A domain is a Faustus project. Its library is one Markdown file, ``EXPERIENCE.md``,
kept in the project's own memory folder (``<workspace>/.faustus/``) so it is
listed in the project's memory index and read by every chat of the project.
A session that belongs to no project, or a project without a bound folder,
uses ``DATA_DIR/harness_proposals/experience/<key>/EXPERIENCE.md``.

Each entry is a lesson in the shape the Harness^2 method (google-research/harness2,
Apache-2.0) keeps in its library::

    ## 2026-10-28 - <cue, shortened>
    - **cue**: the shape of situation the lesson applies to
    - **lesson**: the operation to perform on a harness, naming the component
    - **evidence**: the probe rows it rests on
    - **confidence**: high | medium | low
    - **verdict**: helped | hurt | no observed effect | confounded  (axis:target)

Rules that keep it honest: a lesson supported only by ``confounded`` rows is not
written, a round with no real contrast writes ``NO_ENTRY`` (nothing), the text
goes through the same security pre-scan as any other harness text, and the
proposer reads these entries as hypotheses to check, not as facts.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from core.atomic_io import atomic_write_text
from src.harness_refinement import store, targets

logger = logging.getLogger(__name__)

FILE = "EXPERIENCE.md"
VERDICTS = ("helped", "hurt", "no observed effect", "confounded")
CONFIDENCE = ("high", "medium", "low")
MAX_ENTRIES = 30
FIELD_CHARS = 600
HEADER = (
    "# Harness experience\n\n"
    "Lessons from probing edits to this project's harness (instructions, skills, memories, sub-agent "
    "specs). Each one was measured against the unmodified harness on a repeat of a real task. Treat "
    "them as hypotheses to check on the next task, not as facts.\n"
)


@dataclass
class Domain:
    key: str
    name: str
    project: Optional[Dict[str, Any]] = None


def domain_for(project_id: str = "", project_name: str = "", owner: str = "") -> Domain:
    if project_id:
        project = None
        try:
            project = targets._project_store().get(project_id, owner or None)
        except Exception:  # noqa: BLE001
            logger.debug("project lookup failed for experience domain", exc_info=True)
        return Domain(key=_safe_key(project_id), name=project_name or str((project or {}).get("name") or project_id),
                      project=project)
    return Domain(key="general", name="general")


def _safe_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))[:64] or "general"


def _fallback_path(domain: Domain) -> str:
    return os.path.join(store.root(), "experience", domain.key, FILE)


def location(domain: Domain) -> Dict[str, str]:
    """Where this domain's file lives: ``project`` memory or the ``fallback`` folder."""
    if domain.project:
        try:
            base = targets._project_store().memory_dir(domain.project)
        except Exception:  # noqa: BLE001
            base = ""
        if base:
            return {"kind": "project", "path": os.path.join(base, FILE)}
    return {"kind": "fallback", "path": _fallback_path(domain)}


def read(domain: Domain) -> str:
    loc = location(domain)
    if loc["kind"] == "project":
        try:
            return targets._project_store().read_memory_file(domain.project, FILE)
        except Exception:  # noqa: BLE001 - an unreadable project folder is an empty library
            return ""
    try:
        with open(loc["path"], "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _write(domain: Domain, text: str) -> str:
    loc = location(domain)
    if loc["kind"] == "project":
        targets._project_store().write_memory_file(domain.project, FILE, text)
    else:
        os.makedirs(os.path.dirname(loc["path"]), exist_ok=True)
        atomic_write_text(loc["path"], text)
    return loc["path"]


# -- parsing / rendering ----------------------------------------------------

_ENTRY_SPLIT = re.compile(r"^## ", re.M)
_FIELD_RE = re.compile(r"^- \*\*(cue|lesson|evidence|confidence|verdict)\*\*:\s*(.*)$")


def parse(text: str) -> List[Dict[str, str]]:
    """Entries oldest first: ``{title, cue, lesson, evidence, confidence, verdict}``."""
    out: List[Dict[str, str]] = []
    for block in _ENTRY_SPLIT.split(text or "")[1:]:
        lines = block.splitlines()
        entry: Dict[str, str] = {"title": lines[0].strip() if lines else ""}
        for line in lines[1:]:
            match = _FIELD_RE.match(line.strip())
            if match:
                entry[match.group(1)] = match.group(2).strip()
        if entry.get("lesson"):
            out.append(entry)
    return out


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())[:FIELD_CHARS]


def render_entry(entry: Mapping[str, str]) -> str:
    title = _one_line(entry.get("title")) or f"{time.strftime('%Y-%m-%d')} - {_one_line(entry.get('cue'))[:90]}"
    return (f"## {title}\n"
            f"- **cue**: {_one_line(entry.get('cue'))}\n"
            f"- **lesson**: {_one_line(entry.get('lesson'))}\n"
            f"- **evidence**: {_one_line(entry.get('evidence'))}\n"
            f"- **confidence**: {_one_line(entry.get('confidence'))}\n"
            f"- **verdict**: {_one_line(entry.get('verdict'))}\n")


def render_all(entries: List[Mapping[str, str]]) -> str:
    return HEADER + "\n" + "\n".join(render_entry(e) for e in entries[-MAX_ENTRIES:])


def entries(domain: Domain) -> List[Dict[str, str]]:
    return parse(read(domain))


def for_prompt(domain: Domain, max_chars: int = 2500) -> str:
    """The newest lessons, in a form the proposer prompt can embed. Empty when there are none."""
    chosen: List[str] = []
    used = 0
    for entry in reversed(entries(domain)):
        line = (f"- when: {entry.get('cue', '')} | do: {entry.get('lesson', '')} "
                f"| seen: {entry.get('verdict', '')}, confidence {entry.get('confidence', '')}")
        if used + len(line) > max_chars:
            break
        chosen.append(line)
        used += len(line)
    return "\n".join(chosen)


# -- writing a lesson ---------------------------------------------------------

def check_lesson(raw: Any, verdicts: List[str]) -> Dict[str, Any]:
    """Validate a model-written lesson. Returns ``{"entry": {...}}`` or ``{"skipped": reason}``.

    ``verdicts`` are the verdicts of the edit rows the lesson rests on."""
    if not isinstance(raw, Mapping):
        return {"skipped": "no_entry"}
    entry = {k: _one_line(raw.get(k)) for k in ("cue", "lesson", "evidence")}
    if not all(entry.values()):
        return {"skipped": "incomplete_entry"}
    confidence = _one_line(raw.get("confidence")).lower().split(" ")[0].strip(",.;:")
    entry["confidence"] = confidence if confidence in CONFIDENCE else "low"
    usable = [v for v in verdicts if v in VERDICTS and v != "confounded"]
    if not usable:
        return {"skipped": "only_confounded"}
    entry["verdict"] = usable[0]
    try:
        targets._scan(" ".join(entry.values()), "markdown")
    except Exception:  # noqa: BLE001 - HarnessError: the scan found something critical
        return {"skipped": "security_scan"}
    return {"entry": entry}


def append(domain: Domain, entry: Mapping[str, str], *, label: str = "") -> Dict[str, Any]:
    """Add one entry unless a near-identical lesson is already there."""
    from src.memory import get_text_similarity
    current = entries(domain)
    for old in current:
        if get_text_similarity(str(entry.get("lesson", "")), old.get("lesson", "")) >= 0.85:
            return {"written": False, "reason": "duplicate"}
    item = dict(entry)
    if label:
        item["verdict"] = f"{item.get('verdict', '')} ({label})".strip()
    path = _write(domain, render_all(current + [item]))
    return {"written": True, "path": path, "entries": min(len(current) + 1, MAX_ENTRIES)}


# -- listing, for the routes and the MCP tool -------------------------------------

def list_domains(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every domain that has a library: projects with an EXPERIENCE.md plus the fallback folders."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        for project in targets._project_store().list(owner or None):
            domain = Domain(key=_safe_key(project.get("id", "")), name=str(project.get("name") or ""),
                            project=project)
            found = entries(domain)
            if found:
                out.append({"key": domain.key, "name": domain.name, "kind": location(domain)["kind"],
                            "entries": len(found)})
                seen.add(domain.key)
    except Exception:  # noqa: BLE001
        logger.debug("listing project experience failed", exc_info=True)
    base = os.path.join(store.root(), "experience")
    if os.path.isdir(base):
        for key in sorted(os.listdir(base)):
            if key in seen or not os.path.isfile(os.path.join(base, key, FILE)):
                continue
            found = parse(_read_file(os.path.join(base, key, FILE)))
            if found:
                out.append({"key": key, "name": key, "kind": "fallback", "entries": len(found)})
    return out


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def find_domain(key: str, owner: Optional[str] = None) -> Optional[Domain]:
    """The domain with this key (a project id, or ``general``), or None."""
    if not key or key == "general":
        return Domain(key="general", name="general")
    try:
        project = targets._project_store().get(key, owner or None)
    except Exception:  # noqa: BLE001
        project = None
    if project:
        return Domain(key=_safe_key(key), name=str(project.get("name") or key), project=project)
    if os.path.isfile(os.path.join(store.root(), "experience", _safe_key(key), FILE)):
        return Domain(key=_safe_key(key), name=key)
    return None
