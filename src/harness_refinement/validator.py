"""Coherence checks for one harness edit, run before it is probed and again
before it is adopted.

``targets.validate_edit`` already filters size, destination and safety. What it
cannot see is whether the edit leaves the harness *consistent*, which is what
the method of Harness^2 (google-research/harness2, Apache-2.0) checks with its
hard validator before any rollout: every skill the instructions name exists,
every script they cite exists, and no template placeholder is left broken. A
harness that fails goes back to the base, it is never "repaired" by the model.

Only what the edit *introduces* is checked: a reference that was already in the
text before the edit is not this edit's fault and is not re-judged.

Nothing here calls a model.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List, Optional, Set

from src.harness_refinement import targets

logger = logging.getLogger(__name__)

#: Jinja-style placeholders and statement tags: a doubled brace that is not a
#: placeholder the runtime fills in kills a rendering step downstream.
_TEMPLATE_RE = re.compile(r"\{\{|\}\}|\{%|%\}")

#: A quoted name followed by the word "skill": "load the `deploy-notes` skill".
_SKILL_REF_RE = re.compile(r"[`'\"]([A-Za-z0-9][A-Za-z0-9._-]{1,100})[`'\"]\s+skill\b", re.I)

#: A path with a scripts/ directory in it: "scripts/check_totals.py".
_SCRIPT_PATH_RE = re.compile(
    r"(?<![\w./\\-])((?:[\w.-]+/)*scripts?/[\w.-]+\.(?:py|sh|ps1|js|mjs|ts|bat|cmd))(?![\w.-])")
#: An interpreter followed by a script: "python tools/x.py", "bash run.sh".
_SCRIPT_RUN_RE = re.compile(
    r"\b(?:python3?|py|bash|sh|node|pwsh|powershell(?:\.exe)?)\s+(?:-[\w-]+\s+)*[`'\"]?"
    r"([\w./\\:-]+\.(?:py|sh|ps1|js|mjs))[`'\"]?")


def skill_refs(text: Optional[str]) -> Set[str]:
    return {m.group(1).lower() for m in _SKILL_REF_RE.finditer(text or "")}


def script_refs(text: Optional[str]) -> Set[str]:
    refs = {m.group(1) for m in _SCRIPT_PATH_RE.finditer(text or "")}
    refs |= {m.group(1) for m in _SCRIPT_RUN_RE.finditer(text or "")}
    return {r.replace("\\", "/") for r in refs}


def _skill_exists(name: str, owner: str) -> Optional[bool]:
    """True / False, or None when the skills store cannot be asked."""
    try:
        return bool(targets._skills_manager().read_skill_md(name, owner=owner or None))
    except Exception:  # noqa: BLE001 - an unreadable store is "cannot verify", not "missing"
        logger.debug("skill existence check failed for %s", name, exc_info=True)
        return None


def _roots(axis: str, target: str, owner: str) -> List[str]:
    """Folders a relative script path may resolve against."""
    roots: List[str] = []
    try:
        ident = targets.parse_target(axis, target)
    except Exception:  # noqa: BLE001
        return roots
    try:
        if axis == "prompt_layer":
            workspace = str(targets._project(ident, owner).get("workspace") or "")
            if workspace:
                roots.append(workspace)
        elif axis == "skill":
            path = targets._skill_path(ident, owner)
            if path:
                roots.append(os.path.dirname(path))
    except Exception:  # noqa: BLE001
        logger.debug("script roots failed", exc_info=True)
    return roots


def _script_exists(ref: str, roots: Iterable[str]) -> bool:
    if os.path.isabs(ref) or re.match(r"^[A-Za-z]:/", ref):
        return os.path.isfile(ref)
    for root in roots:
        if os.path.isfile(os.path.join(root, ref)):
            return True
    return False


def check_edit(axis: str, target: str, op: str, before: Optional[str], after: Optional[str],
               owner: str = "") -> List[str]:
    """Problems this edit introduces; an empty list means it is coherent."""
    if op == "delete" or after is None:
        return []
    old = before or ""
    problems: List[str] = []

    if len(_TEMPLATE_RE.findall(after)) > len(_TEMPLATE_RE.findall(old)):
        problems.append("it adds a template placeholder ({{ }} or {% %}) that nothing fills in")

    for name in sorted(skill_refs(after) - skill_refs(old)):
        exists = _skill_exists(name, owner)
        if exists is False:
            problems.append(f"it tells the agent to load the skill {name!r}, which does not exist")

    new_scripts = sorted(script_refs(after) - script_refs(old))
    if new_scripts:
        roots = _roots(axis, target, owner)
        for ref in new_scripts:
            if not _script_exists(ref, roots):
                problems.append(f"it cites the script {ref!r}, which does not exist")
    return problems
