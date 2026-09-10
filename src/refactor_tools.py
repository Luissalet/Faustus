"""refactor_tools.py — EDIT-06: structure-assisted refactoring.

Prefers the code index over a global find/replace: `src.code_index.
find_definition` + `find_callers` (IDX-02) give exact `(path, line)` sites for
a symbol, so a rename only ever touches a line the index says references it —
never an unrelated string, comment or docstring that happens to contain the
same substring, which is EDIT-06's acceptance criterion in one sentence.

A symbol the index has not seen is refused rather than silently downgraded to
a workspace-wide text substitution — that fallback is exactly the accident
this tool exists to prevent, so `apply_rename` never takes it; a caller who
wants a raw-text rename already has `edit_file`/`ApplyPatchTool` for that,
with its own base_revision guard (EDIT-01).

The rename is packaged as one `src.changesets.build()` ChangeSet (COMUN.md
rule 4: reuse the existing evidence vocabulary rather than inventing a
second one) — one rename, one ChangeSet, whatever files it touched.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src import code_index

logger = logging.getLogger(__name__)

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IDENT_PATTERN_CACHE: Dict[str, "re.Pattern[str]"] = {}


class RefactorError(ValueError):
    pass


def _identifier_pattern(name: str) -> "re.Pattern[str]":
    pattern = _IDENT_PATTERN_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        _IDENT_PATTERN_CACHE[name] = pattern
    return pattern


def plan_rename(old_name: str, new_name: str, *, workspace: str = "",
                project_id: str = "", limit: int = 500) -> Dict[str, Any]:
    """Every definition/reference/test site the code index knows for
    `old_name`, without writing anything — EDIT-06's "vista de impacto":
    symbols, call sites and relevant tests, for a human or caller to review
    before `apply_rename`.

    `indexed=False` (empty `definitions` AND `callers`) means the index has
    nothing for this name — most often because `code_index.refresh(workspace)`
    has not run, or the name is simply wrong. `apply_rename` refuses to
    proceed in that state rather than guessing.
    """
    if not old_name or not new_name:
        raise RefactorError("old_name and new_name are required")
    if old_name == new_name:
        raise RefactorError("old_name and new_name are identical")
    if not _VALID_IDENTIFIER.match(new_name):
        raise RefactorError(f"{new_name!r} is not a valid identifier")

    definitions = code_index.find_definition(old_name, workspace=workspace, project_id=project_id)
    callers = code_index.find_callers(old_name, workspace=workspace, project_id=project_id, limit=limit)
    tests = code_index.tests_for(old_name, workspace=workspace, project_id=project_id)

    per_file: Dict[str, List[int]] = {}
    for hit in definitions:
        per_file.setdefault(hit["path"], []).append(hit["start_line"])
    for hit in callers:
        per_file.setdefault(hit["path"], []).append(hit["line"])

    indexed = bool(definitions or callers)
    result: Dict[str, Any] = {
        "old_name": old_name, "new_name": new_name, "indexed": indexed,
        "definitions": definitions, "callers": callers, "tests": tests,
        "files": sorted(per_file.keys()),
        "reference_count": sum(len(v) for v in per_file.values()),
    }
    if not indexed:
        result["reason"] = (
            "not indexed — run code_index.refresh(workspace) first (or the "
            "name is misspelled); refusing to fall back to a workspace-wide "
            "text substitution that could rename an unrelated string")
    return result


def _rename_on_lines(abs_path: str, line_numbers: Sequence[int],
                     old_name: str, new_name: str) -> Optional[str]:
    """Replace `old_name` with `new_name` at identifier (word) boundaries,
    ONLY on the given 1-based line numbers. Returns the new file text, or
    `None` if none of those lines actually contained the symbol (the index
    and the file on disk have drifted since the last refresh) — the caller
    treats that as "skip", never as "rename everywhere instead"."""
    with open(abs_path, "r", encoding="utf-8", newline="") as fh:
        original = fh.read()
    had_crlf = "\r\n" in original
    lines = original.replace("\r\n", "\n").split("\n")
    pattern = _identifier_pattern(old_name)
    changed = False
    for line_no in sorted(set(int(n) for n in line_numbers)):
        i = line_no - 1
        if 0 <= i < len(lines) and pattern.search(lines[i]):
            lines[i] = pattern.sub(new_name, lines[i])
            changed = True
    if not changed:
        return None
    new_text = "\n".join(lines)
    return new_text.replace("\n", "\r\n") if had_crlf else new_text


@dataclass
class RenameResult:
    applied: bool
    files_changed: List[str] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    changeset: Optional[Any] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "applied": self.applied, "files_changed": self.files_changed,
            "skipped": self.skipped, "reason": self.reason,
            "changeset": self.changeset.to_dict() if self.changeset is not None else None,
        }


def apply_rename(old_name: str, new_name: str, *, workspace: str = "",
                 project_id: str = "", owner: str = "", run_id: str = "",
                 title: str = "") -> RenameResult:
    """Apply a rename the code index can see, writing only the lines it
    named, and package the result as one ChangeSet.

    Refuses (returns `applied=False`, no files touched) when `plan_rename`
    reports the symbol is not indexed — see its docstring. This is the one
    control that keeps a rename from ever degrading into a project-wide
    string replace.
    """
    from src.changesets import build as build_changeset

    plan = plan_rename(old_name, new_name, workspace=workspace, project_id=project_id)
    if not plan["indexed"]:
        return RenameResult(applied=False, reason=plan["reason"])

    root = os.path.realpath(os.path.expanduser(workspace)) if workspace else ""
    per_file_lines: Dict[str, List[int]] = {}
    for hit in plan["definitions"]:
        per_file_lines.setdefault(hit["path"], []).append(hit["start_line"])
    for hit in plan["callers"]:
        per_file_lines.setdefault(hit["path"], []).append(hit["line"])

    files_changed: List[str] = []
    skipped: List[Dict[str, str]] = []
    for rel_path, line_numbers in sorted(per_file_lines.items()):
        abs_path = os.path.join(root, rel_path) if root and not os.path.isabs(rel_path) else rel_path
        try:
            new_text = _rename_on_lines(abs_path, line_numbers, old_name, new_name)
        except OSError as exc:
            skipped.append({"path": rel_path, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if new_text is None:
            skipped.append({"path": rel_path,
                            "reason": "indexed line(s) no longer contain the symbol (stale index)"})
            continue
        with open(abs_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        files_changed.append(rel_path)

    changeset = build_changeset(
        # ChangeSet.intent's vocabulary (src/contracts/changeset.py) has no
        # "refactor" — a rename is a structural, non-behavioral edit, which
        # is what "implement" already covers there.
        intent="implement", workspace=workspace,
        changes={"source": "none", "modified": files_changed},
        claims=[{"path": p, "kind": "modified"} for p in files_changed],
        title=title or f"Rename {old_name} -> {new_name}",
        owner=owner, project_id=project_id, run_id=run_id,
    )
    return RenameResult(applied=bool(files_changed), files_changed=files_changed,
                        skipped=skipped, changeset=changeset)
