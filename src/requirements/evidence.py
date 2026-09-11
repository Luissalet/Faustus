"""requirements/evidence.py — the coverage matrix (ADP-20).

Four INDEPENDENT dimensions per requirement, never folded into one score
(ADP-20 limits: "no inventar cobertura de comportamiento a partir de
porcentajes de enlaces"):

  linked       any link at all exists (implements/tests/evidences/issue)
  implemented  an `implements` link currently resolves: the target path
               exists inside the workspace and, if a symbol was named
               (`path@symbol` or a pytest-style `path::symbol`), that symbol
               is still found there via `src.repo_map.symbol_lines` -- the
               SAME extractor `read_plan`'s outline uses, not a second parser
  tested       a `tests` link currently resolves the same way
  verified     an `evidences` link exists AND was recorded against the
               requirement's CURRENT revision -- an `implements` link (or a
               comment `@implements REQ-N` detected by
               :func:`scan_implements_comments`) NEVER sets this, by
               construction: only `kind == "evidences"` counts

`stale` is a fifth, cross-cutting flag: true when a resolved link's live
state differs from what was recorded at link time -- the target file's
content changed (`renombrar símbolo → stale`), or the requirement gained a
newer revision since an `evidences` link was recorded
(`cambiar requisito tras verificar → stale`). A target that no longer exists
at all is NOT "stale" -- it is `unknown` (evidence is simply gone, "borrar
test → degrada"): staleness means something changed, not that the model is
guessing why.

A target path that resolves outside the given workspace is rejected at link
creation time (:func:`link_evidence` raises ``requirements.path_outside_workspace``)
-- it is never silently accepted as `unknown` later; ADP-20's own limit
("no afirmar que una prueba pasada demuestra todo el requisito") is honoured
by keeping the matrix a set of facts, not a verdict.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from src import repo_map
from src.requirements import store as req_store

logger = logging.getLogger(__name__)

#: `@implements REQ-N` in a source comment -- detected as a LINK candidate
#: only. Nothing that calls this may treat its result as `verified`; the
#: matrix only ever sets `verified` from a `kind == "evidences"` link (see
#: module docstring). English + Spanish spelling both scanned for symmetry
#: with `project_board._CLOSE_RE`.
_IMPLEMENTS_COMMENT_RE = re.compile(r"@implements\s+(REQ-\d+)", re.IGNORECASE)


def scan_implements_comments(text: str) -> List[str]:
    """`REQ-N` keys mentioned via an `@implements REQ-N` comment in `text`.

    Pure, stdlib-only text scan -- no AST, works on any language. Callers may
    use this to PROPOSE an `implements` link (via :func:`link_evidence`); the
    result must never be passed to anything that sets `verified` directly."""
    if not text:
        return []
    seen: List[str] = []
    for m in _IMPLEMENTS_COMMENT_RE.finditer(text):
        key = m.group(1).upper()
        if key not in seen:
            seen.append(key)
    return seen


def _split_target(target: str) -> Tuple[str, str]:
    """`path[@symbol]` or pytest-style `path::symbol` -> `(path, symbol)`.
    `symbol` is `''` when the target names a whole file/test with no inner
    reference."""
    target = str(target or "")
    if "::" in target:
        path, _, symbol = target.partition("::")
        return path, symbol
    if "@" in target:
        path, _, symbol = target.partition("@")
        return path, symbol
    return target, ""


def _resolve_in_workspace(workspace: str, rel_path: str) -> Optional[str]:
    """Absolute path of `rel_path` inside `workspace`, or `None` if it is
    absolute, empty, or escapes the workspace root (`../..`, a symlink out,
    etc). Same confinement shape `filesystem_tools.py`'s glob confinement
    uses (`os.path.realpath` + `os.path.commonpath`), duplicated here rather
    than imported because that helper is private to that module."""
    rel_path = str(rel_path or "").strip()
    if not workspace or not rel_path or os.path.isabs(rel_path):
        return None
    base = os.path.realpath(workspace)
    cand = os.path.realpath(os.path.join(base, rel_path))
    if cand != base:
        try:
            common = os.path.commonpath([base, cand])
        except ValueError:  # different drives on Windows, etc.
            return None
        if common != base:
            return None
    return cand


def _file_hash(abs_path: str) -> Optional[str]:
    try:
        with open(abs_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _symbol_present(abs_path: str, rel_path: str, symbol: str) -> bool:
    """Whether `symbol` is still found in the file, via the SAME extractor
    `read_plan`'s outline uses (`repo_map.symbol_lines`) -- never a second,
    divergent parser. Matches a bare method name against `Class.method` too,
    since a caller citing `@implements` from inside a method rarely spells
    the class prefix."""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    lang = repo_map.lang_for_path(rel_path)
    names = [name for name, _line in repo_map.symbol_lines(text, lang)]
    for name in names:
        ident = name
        for prefix in ("async def ", "def ", "class "):
            if ident.startswith(prefix):
                ident = ident[len(prefix):]
                break
        if ident == symbol:
            return True
        if "." in ident and ident.rsplit(".", 1)[1] == symbol:
            return True
    return False


def link_evidence(
    project_id: str, key: str, *, kind: str, target: str, workspace: str = "",
    revision: str = "", created_by: str = "",
) -> Dict[str, Any]:
    """Create a requirement link, validating filesystem targets up front.

    For `kind in ('implements', 'tests')`: the path half of `target` MUST
    resolve inside `workspace`, or this raises
    `requirements.path_outside_workspace` -- "fichero fuera del workspace →
    rechazado" (ADP-20 acceptance), never silently stored as an `unknown`
    link to be discovered later. The file's current content hash is recorded
    at link time so a later change is detectable as `stale` rather than
    silently still counting as `implemented`/`tested`.

    For `kind in ('evidences', 'issue')`: no filesystem target, no hash --
    `target` is a run/test-result id or a board issue key."""
    kind = str(kind or "").strip().lower()
    content_hash: Optional[str] = None
    if kind in ("implements", "tests"):
        path, _symbol = _split_target(target)
        abs_path = _resolve_in_workspace(workspace, path)
        if abs_path is None:
            raise req_store.RequirementsError(
                f"link target {target!r} is outside the project workspace",
                "requirements.path_outside_workspace",
            )
        if os.path.isfile(abs_path):
            content_hash = _file_hash(abs_path)
        # A target that does not exist YET (evidence proposed ahead of the
        # code landing) is allowed to be linked -- it simply starts life
        # `unknown` at the next matrix computation, same as a target deleted
        # later; only an out-of-workspace target is a hard rejection.
    return req_store.add_link(
        project_id, key, kind=kind, target=target, revision=revision,
        content_hash=content_hash, created_by=created_by,
    )


def resolve_link_state(link: Dict[str, Any], requirement: Dict[str, Any], *, workspace: str) -> Tuple[str, Dict[str, Any]]:
    """The link's state RIGHT NOW: `('linked'|'stale'|'unknown'|'needs_review', meta)`.

    Never mutates anything -- pure function of the link row, the parent
    requirement, and the filesystem. `matrix()` is the only caller that
    persists the result."""
    kind = link["kind"]
    if kind in ("implements", "tests"):
        path, symbol = _split_target(link["target"])
        abs_path = _resolve_in_workspace(workspace, path)
        if abs_path is None:
            return "unknown", {"reason": "path_outside_workspace"}
        if not os.path.isfile(abs_path):
            return "unknown", {"reason": "target_missing"}
        if symbol and not _symbol_present(abs_path, path, symbol):
            return "stale", {"reason": "symbol_not_found", "symbol": symbol}
        current_hash = _file_hash(abs_path)
        recorded_hash = link.get("content_hash")
        if recorded_hash and current_hash and current_hash != recorded_hash:
            return "stale", {"reason": "content_changed"}
        return "linked", {}
    if kind == "evidences":
        recorded_rev = link.get("req_revision_at_link")
        current_rev = requirement.get("current_revision")
        if recorded_rev is not None and current_rev is not None and recorded_rev != current_rev:
            return "stale", {"reason": "requirement_changed_since_verification",
                              "verified_at_revision": recorded_rev, "current_revision": current_rev}
        return "linked", {}
    if kind == "issue":
        return "linked", {}
    return "unknown", {"reason": "unsupported_kind"}


def matrix(project_id: str, key: str, *, workspace: str = "", persist: bool = True) -> Dict[str, Any]:
    """The coverage matrix for one requirement. Raises
    `req_store.NotFoundError` for an unknown key -- callers must not
    fabricate a "no evidence" row for an id that does not exist (ADP-19/20
    "id desconocido produce un resultado explícito, no uno inventado")."""
    requirement = req_store.get_or_raise(project_id, key)
    resolved: List[Dict[str, Any]] = []
    for link in requirement["links"]:
        state, meta = resolve_link_state(link, requirement, workspace=workspace)
        row = dict(link)
        row["live_state"] = state
        row["live_meta"] = meta
        resolved.append(row)
        if persist and state != link.get("state"):
            try:
                req_store.Store().set_link_state(link["id"], state)
            except req_store.RequirementsError:
                logger.debug("evidence.matrix: could not persist link state for %s", link["id"], exc_info=True)

    def _any(kind: str) -> bool:
        return any(l["kind"] == kind and l["live_state"] == "linked" for l in resolved)

    return {
        "key": key,
        "project_id": project_id,
        "linked": bool(resolved),
        "implemented": _any("implements"),
        "tested": _any("tests"),
        "verified": _any("evidences"),
        "stale": any(l["live_state"] == "stale" for l in resolved),
        "current_revision": requirement["current_revision"],
        "links": resolved,
    }


def project_matrix(project_id: str, *, workspace: str = "") -> List[Dict[str, Any]]:
    """The matrix for every requirement of this project -- `GET .../matrix`
    with no key filters to one row."""
    out = []
    for key in req_store.all_requirement_keys(project_id):
        out.append(matrix(project_id, key, workspace=workspace))
    return out
