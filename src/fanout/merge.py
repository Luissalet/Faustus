"""src/fanout/merge.py — turn a fan-out winner into the user's main copy.

Two-step apply, same shape the Studio UI already gives `src.alternatives`
(propose, then confirm): `apply_winner(..., confirm=False)` returns what
WOULD be applied (diff stats, no write); `confirm=True` calls
`alternatives.apply_alternative` for real -- the three-way merge that never
discards a manual edit made to the main copy since the fan-out started, and
that raises `alternatives.ApplyConflictError` (nothing written) rather than
guessing on any conflicting file.
"""

from __future__ import annotations

import difflib
import os
from typing import Any, Dict, List, Optional

from src import alternatives
from src.fanout import runner


class FanoutMergeError(Exception):
    pass


def _candidate_or_error(run_id: str, owner: str, label: str) -> Dict[str, Any]:
    manifest = runner._manifest_or_error(run_id, owner)  # noqa: SLF001 - same package
    cand = runner.load_candidate(run_id, label)
    if cand is None:
        raise FanoutMergeError(f"no candidate {label!r} in run {run_id!r}")
    return cand


def apply_winner(owner: str, run_id: str, label: str, *, confirm: bool = False) -> Dict[str, Any]:
    manifest = runner._manifest_or_error(run_id, owner)  # noqa: SLF001 - same package
    cand = _candidate_or_error(run_id, owner, label)
    exp_id = manifest["exp_id"]
    alt_id = cand["alt_id"]

    if not confirm:
        try:
            compared = alternatives.compare(owner, exp_id)
        except alternatives.AlternativesError as exc:
            raise FanoutMergeError(str(exc)) from exc
        entry = next((a for a in compared["alternatives"] if a["id"] == alt_id), None)
        return {
            "proposed": True, "applied": False, "run_id": run_id, "label": label,
            "exp_id": exp_id, "alternative_id": alt_id,
            "diff_summary": (entry or {}).get("diff_summary"),
            "files": (entry or {}).get("files"),
            "contested_files": compared.get("contested_files"),
            "message": "Nothing written yet -- call again with confirm=true to apply this winner "
                       "into the main copy.",
        }

    try:
        result = alternatives.apply_alternative(owner, exp_id, alt_id)
    except alternatives.AlternativesError as exc:
        raise FanoutMergeError(str(exc)) from exc
    return {"proposed": False, "applied": True, "run_id": run_id, "label": label,
            "exp_id": exp_id, "alternative_id": alt_id, **result}


def _rel_files(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return out


def _read(path: str) -> Optional[str]:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def diff_between(owner: str, run_id: str, label_a: str, label_b: str) -> Dict[str, Any]:
    """Unified diff, per changed file, between two candidates' isolated
    copies directly -- not each one against the base, the winner-vs-runner-up
    comparison `orca`'s own diff-compare view gives a human, just computed
    here instead of read off the filesystem by eye."""
    cand_a = _candidate_or_error(run_id, owner, label_a)
    cand_b = _candidate_or_error(run_id, owner, label_b)
    root_a, root_b = cand_a["path"], cand_b["path"]
    all_rel = sorted(set(_rel_files(root_a)) | set(_rel_files(root_b)))
    files: Dict[str, str] = {}
    for rel in all_rel:
        text_a = _read(os.path.join(root_a, rel))
        text_b = _read(os.path.join(root_b, rel))
        if text_a == text_b:
            continue
        diff_lines = list(difflib.unified_diff(
            (text_a or "").splitlines(keepends=True),
            (text_b or "").splitlines(keepends=True),
            fromfile=f"{label_a}/{rel}", tofile=f"{label_b}/{rel}",
        ))
        if diff_lines:
            files[rel] = "".join(diff_lines)
    return {"run_id": run_id, "label_a": label_a, "label_b": label_b, "files": files}
