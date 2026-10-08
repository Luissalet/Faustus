"""alternatives.py — CMP-13: isolated, comparable alternatives.

Luis's brief (INFORME §3.12): today, trying two approaches to the same task
means either overwriting the one attempt with the next, or asking the agent
to "remember" which lines came from which idea — both lose work and neither
lets a person actually compare. This module gives an "experiment" a small,
persistent identity: a goal, a `base_ref` (what every alternative started
from), and N alternatives, each isolated so two of them — and the user's own
main copy — can all hold DIFFERENT uncommitted content on the SAME files at
the SAME time without colliding.

Isolation, by what the workspace actually is (contract, INFORME §3.12):
  * a git repo       -> ``worktree``    (`src.git_panel.worktree_add`, a
                         real detached checkout sharing the repo's object
                         store — cheap, and hooks never run there, see that
                         function's own docstring)
  * a plain directory -> ``snapshot_dir`` (`shutil.copytree` of a frozen
                         base snapshot taken at experiment creation, since
                         there is no git history to recover an old revision
                         from once the directory moves on)
  * a single document -> ``doc_version`` (a text snapshot held in this
                         experiment's own record WHILE DRAFTING — an
                         alternative's content never touches the real
                         document until it is applied, the same isolation
                         guarantee worktree/snapshot_dir give a filesystem
                         workspace. W3-D wires ``apply``/``combine`` for
                         this kind to the real Studio document store
                         (``core.database.Document``/``DocumentVersion``)
                         when ``create_doc_experiment`` was given a
                         ``document_id``: the merged result is written as a
                         new, immutable ``DocumentVersion`` tagged
                         ``source="alternative:<exp_id>"`` — the SAME
                         version-history mechanism a normal edit uses
                         (``src.document_comments._apply_document_edit``),
                         never a second, parallel content field. See
                         ``_persist_alternative_as_document_version`` below
                         for the exact provenance recorded.)

``apply``/``combine`` never destroy a manual edit the user made to the main
copy after the experiment started (the decisive test, INFORME §3.12): every
file touched is three-way merged — base / mine (main copy now) / theirs
(the chosen alternative) — via ``git merge-file``, git's own merge
algorithm, the same "reuse a proven mechanism, don't reinvent one" rule
`src.git_panel.merge` already follows for the Source Control panel. A
conflict on ANY file aborts the WHOLE apply/combine before a single byte is
written — never a half-applied, half-manual mix (see ``_plan_merge`` /
``apply_alternative`` below).

Storage: one JSON file per experiment under
``DATA_DIR/alternatives/<experiment_id>.json`` (the ``chat_versions.py``
shape: one file per id, atomic writes, a corrupt/missing file loses only
this experiment, never anything else) plus, for worktree/snapshot_dir
isolation, the actual isolated directories at
``DATA_DIR/alternatives/<experiment_id>/<alternative_id>`` — exactly the
layout the CMP-13 contract names.

Every read is owner-scoped (``_load_experiment`` refuses to return another
owner's experiment — same 404-for-both-"missing"-and-"not-yours" shape
``git_panel.find_repo_meta`` uses); nothing here calls an LLM or has any
effect beyond the isolated copies and (for ``run_tests``) a command the
CALLER supplied, run only inside the alternative's own isolated directory.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.atomic_io import atomic_write_json
from src import git_panel
from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

VERSION = 1
ISOLATION_KINDS = ("worktree", "snapshot_dir", "doc_version")
ALT_STATUSES = ("pending", "ready", "failed", "applied")
DEFAULT_TEST_TIMEOUT = 120.0
MAX_TEST_OUTPUT_BYTES = 60_000
MAX_FILE_BYTES = 5_000_000  # a file bigger than this is treated as binary/unmergeable
_BASE_SNAPSHOT_DIR = "_base"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AlternativesError(Exception):
    error_class = "alternatives.error"

    def __init__(self, message: str, *, error_class: Optional[str] = None):
        super().__init__(message)
        if error_class:
            self.error_class = error_class


class ExperimentNotFoundError(AlternativesError):
    error_class = "alternatives.not_found"


class AlternativeNotFoundError(AlternativesError):
    error_class = "alternatives.alt_not_found"


class InvalidWorkspaceError(AlternativesError):
    error_class = "alternatives.invalid_workspace"


class ApplyConflictError(AlternativesError):
    """Raised by ``apply_alternative``/``combine`` when ANY touched file
    would need a human to resolve it. Nothing is written when this is
    raised — see ``_plan_merge``'s docstring."""

    error_class = "alternatives.apply_conflict"

    def __init__(self, conflicts: List[str]):
        self.conflicts = list(conflicts)
        super().__init__(f"{len(self.conflicts)} file(s) need manual resolution: {', '.join(self.conflicts)}")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _root_dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "alternatives")


def _exp_json_path(exp_id: str) -> str:
    return os.path.join(_root_dir(), f"{exp_id}.json")


def _exp_dir(exp_id: str) -> str:
    return os.path.join(_root_dir(), exp_id)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _load_raw(exp_id: str) -> Optional[Dict[str, Any]]:
    path = _exp_json_path(exp_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        logger.warning("alternatives: unreadable experiment file %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _save(exp: Dict[str, Any]) -> None:
    os.makedirs(_root_dir(), exist_ok=True)
    atomic_write_json(_exp_json_path(exp["id"]), exp)


def _load_experiment(owner: str, exp_id: str) -> Dict[str, Any]:
    """Owner-scoped read: an experiment that exists but belongs to a
    DIFFERENT owner answers exactly like one that does not exist — the
    same "discovery is the only path to an id" shape `git_panel.find_repo_meta`
    documents, so a caller can never distinguish "not yours" from "never
    existed" and use that to enumerate other owners' experiments."""
    exp = _load_raw(exp_id)
    if not exp or str(exp.get("owner") or "") != str(owner or ""):
        raise ExperimentNotFoundError(f"no experiment {exp_id!r}")
    return exp


def _alt_or_404(exp: Dict[str, Any], alt_id: str) -> Dict[str, Any]:
    for alt in exp.get("alternatives", []):
        if alt.get("id") == alt_id:
            return alt
    raise AlternativeNotFoundError(f"no alternative {alt_id!r} in experiment {exp['id']!r}")


def list_experiments(owner: str, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    root = _root_dir()
    if not os.path.isdir(root):
        return []
    out: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        exp = _load_raw(name[: -len(".json")])
        if not exp or str(exp.get("owner") or "") != str(owner or ""):
            continue
        if project_id and exp.get("project_id") != project_id:
            continue
        out.append(exp)
    out.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return out


def get_experiment(owner: str, exp_id: str) -> Dict[str, Any]:
    return _load_experiment(owner, exp_id)


def delete_experiment(owner: str, exp_id: str) -> None:
    """Removes every alternative's isolated directory (worktrees via
    ``git worktree remove --force`` when the base was a repo — this is a
    real git operation, so the main repo's own `.git/worktrees/` metadata
    is cleaned up too, not just the folder on disk; plain `shutil.rmtree`
    for snapshot dirs), then the experiment's own directory and JSON file.
    Best-effort per alternative: one failed cleanup does not stop the rest,
    so an experiment can always be removed from the list even if a worktree
    was already deleted by hand outside this module."""
    exp = _load_experiment(owner, exp_id)
    repo_root = exp.get("workspace") if exp.get("base_kind") == "git_sha" else None
    for alt in exp.get("alternatives", []):
        if alt.get("isolation") == "worktree" and repo_root:
            try:
                git_panel.worktree_remove(repo_root, alt["path"], force=True)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("alternatives: worktree_remove failed for %s", alt.get("path"), exc_info=True)
        elif alt.get("isolation") == "snapshot_dir" and alt.get("path"):
            shutil.rmtree(alt["path"], ignore_errors=True)
    shutil.rmtree(_exp_dir(exp_id), ignore_errors=True)
    try:
        os.remove(_exp_json_path(exp_id))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Experiment creation
# ---------------------------------------------------------------------------
def create_experiment(owner: str, project_id: str, goal: str, workspace: str) -> Dict[str, Any]:
    """Start a new experiment rooted at `workspace`.

    `workspace` decides `base_kind`/`base_ref` (CMP-13 contract: "base_ref
    (git sha o snapshot id)"):
      * inside a git repo -> `base_kind="git_sha"`, `workspace` normalised
        to the repo's TOPLEVEL (so every alternative's worktree/diff is
        computed against the same root regardless of which subdirectory was
        passed in), `base_ref` = the repo's current HEAD sha.
      * a plain existing directory -> `base_kind="snapshot"`, `base_ref` =
        a fresh id; a frozen copy of `workspace` is taken immediately into
        `DATA_DIR/alternatives/<id>/_base/` — the only way to recover "what
        it looked like at experiment start" once `workspace` itself keeps
        changing (there's no git history to fall back on).
    """
    goal = (goal or "").strip()
    if not goal:
        raise AlternativesError("`goal` is required", error_class="alternatives.invalid_request")
    workspace = os.path.realpath(str(workspace or "").strip())
    if not workspace or not os.path.isdir(workspace):
        raise InvalidWorkspaceError(f"{workspace!r} is not an existing directory")

    exp_id = _new_id("exp")
    repo_root = git_panel.repo_toplevel(workspace)
    if repo_root:
        head = git_panel.run_git(repo_root, "rev-parse", "HEAD")
        if head.returncode != 0:
            raise InvalidWorkspaceError(
                "this repository has no commits yet -- make an initial commit before starting an experiment"
            )
        base_kind = "git_sha"
        base_ref = head.stdout.strip()
        root_workspace = repo_root
    else:
        base_kind = "snapshot"
        base_ref = f"snap-{uuid.uuid4().hex[:12]}"
        root_workspace = workspace
        base_copy = os.path.join(_exp_dir(exp_id), _BASE_SNAPSHOT_DIR)
        os.makedirs(os.path.dirname(base_copy), exist_ok=True)
        shutil.copytree(workspace, base_copy)

    exp = {
        "id": exp_id,
        "version": VERSION,
        "project_id": project_id,
        "owner": owner,
        "goal": goal,
        "base_kind": base_kind,   # "git_sha" | "snapshot"
        "base_ref": base_ref,
        "workspace": root_workspace,
        "alternatives": [],
        "created_at": time.time(),
        "applied": None,  # {"alternative_id", "applied_at", "files"} once something was applied
    }
    _save(exp)
    return exp


# ---------------------------------------------------------------------------
# Alternatives
# ---------------------------------------------------------------------------
def _default_cost() -> Dict[str, Any]:
    """Contract's own global rule ("precio desconocido = unknown"): an
    alternative carries no run yet at creation time, so its cost is
    unknown, not zero -- `run_tests`/a future attached run is what could
    ever make this a real number."""
    return {"known_usd": "unknown", "unestimable": ["no run attached to this alternative yet"]}


def add_alternative(owner: str, exp_id: str, label: str, *,
                     isolation: Optional[str] = None) -> Dict[str, Any]:
    """Add one isolated alternative to `exp_id`. `isolation` is inferred
    from the experiment's `base_kind` when omitted (`worktree` for a repo,
    `snapshot_dir` otherwise); passing `isolation="doc_version"` explicitly
    creates a content-only alternative with no filesystem isolation at all
    (see `set_doc_version_content`)."""
    exp = _load_experiment(owner, exp_id)
    label = (label or "").strip() or f"Alternative {len(exp['alternatives']) + 1}"
    alt_id = _new_id("alt")

    kind = isolation or ("worktree" if exp["base_kind"] == "git_sha" else "snapshot_dir")
    if kind not in ISOLATION_KINDS:
        raise AlternativesError(f"unknown isolation {kind!r}", error_class="alternatives.invalid_request")

    if kind == "worktree":
        if exp["base_kind"] != "git_sha":
            raise AlternativesError(
                "worktree isolation needs a git-backed experiment", error_class="alternatives.invalid_request"
            )
        alt_dir = os.path.join(_exp_dir(exp_id), alt_id)
        try:
            path = git_panel.worktree_add(exp["workspace"], alt_dir, exp["base_ref"])
        except git_panel.GitWorktreeError as exc:
            raise AlternativesError(f"worktree_add failed: {git_panel.stderr_snippet(exc.stderr)}",
                                     error_class="alternatives.isolation_failed") from exc
    elif kind == "snapshot_dir":
        base_copy = os.path.join(_exp_dir(exp_id), _BASE_SNAPSHOT_DIR)
        if not os.path.isdir(base_copy):
            raise AlternativesError("this experiment has no frozen base snapshot to copy from",
                                     error_class="alternatives.isolation_failed")
        alt_dir = os.path.join(_exp_dir(exp_id), alt_id)
        shutil.copytree(base_copy, alt_dir)
        path = alt_dir
    else:  # doc_version
        path = f"doc_version:{alt_id}"

    alt: Dict[str, Any] = {
        "id": alt_id,
        "label": label,
        "isolation": kind,
        "path": path,
        "run_id": None,
        "status": "ready",
        "cost": _default_cost(),
        "diff_summary": None,
        "tests_result": None,
        "content": "" if kind == "doc_version" else None,  # doc_version's own snapshot
        "created_at": time.time(),
    }
    exp["alternatives"].append(alt)
    _save(exp)
    return alt


def set_doc_version_content(owner: str, exp_id: str, alt_id: str, content: str) -> Dict[str, Any]:
    """Set/replace a `doc_version` alternative's DRAFT text. Always stored
    on the experiment's OWN record, never the real Studio document store —
    that is deliberate isolation, the same guarantee a worktree/snapshot_dir
    alternative gets from its own private directory: an alternative being
    edited must never be visible as the document's live content until a
    human explicitly applies it (`apply_alternative`, which — when this
    experiment carries a `document_id` — IS wired to the real
    `core.database.Document`/`DocumentVersion` store; see that function and
    the module docstring's `doc_version` bullet)."""
    exp = _load_experiment(owner, exp_id)
    alt = _alt_or_404(exp, alt_id)
    if alt["isolation"] != "doc_version":
        raise AlternativesError("set_doc_version_content is only valid for doc_version alternatives",
                                 error_class="alternatives.invalid_request")
    alt["content"] = str(content or "")
    _save(exp)
    return alt


def base_doc_content(exp: Dict[str, Any]) -> str:
    """The base text a `doc_version` experiment started from — stored once,
    at experiment creation, on the experiment record itself."""
    return str(exp.get("doc_base_content") or "")


def _load_live_document(owner: str, document_id: str):
    """Owner-scoped read of a real Studio `Document` row — the same
    404-for-both-"missing"-and-"not-yours" shape `_load_experiment` already
    uses. Imported lazily (module scope, not top-of-file) to avoid a
    core.database <-> src.alternatives import cycle, the same choice
    `src.document_comments._apply_document_edit` documents for itself."""
    from core.database import Document, get_db_session
    with get_db_session() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None or (doc.owner is not None and str(doc.owner) != str(owner or "")):
            raise AlternativesError(f"no document {document_id!r}",
                                     error_class="alternatives.document_not_found")
        return doc.current_content or ""


def _persist_alternative_as_document_version(owner: str, document_id: str, content: str, *,
                                              exp_id: str, alt_id: str, alt_label: str) -> Dict[str, Any]:
    """Materialise an APPLIED `doc_version` alternative onto the real
    Studio document (CMP-13 follow-up, INFORME §3.12: "doc_version cableado
    al almacén real de documentos"). Uses the exact same new-version shape
    `document_comments._apply_document_edit`/`update_document` use for any
    other edit — a bumped `document_count` and one new immutable
    `DocumentVersion` row — so this is never a second source of truth for
    the document's history, only a normal version TAGGED with where it
    came from: `source="alternative:<exp_id>"` names the experiment,
    `summary` names the specific alternative chosen — "procedencia del
    fragmento elegido" made queryable, not just a comment."""
    import uuid as _uuid

    from core.database import Document, DocumentVersion, get_db_session
    with get_db_session() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None or (doc.owner is not None and str(doc.owner) != str(owner or "")):
            raise AlternativesError(f"no document {document_id!r}",
                                     error_class="alternatives.document_not_found")
        if doc.current_content == content:
            return {"document_id": document_id, "version_number": doc.version_count, "unchanged": True}
        new_version = (doc.version_count or 1) + 1
        db.add(DocumentVersion(
            id=str(_uuid.uuid4()),
            document_id=document_id,
            version_number=new_version,
            content=content,
            summary=f"Applied alternative {alt_label!r} ({alt_id}) from experiment {exp_id}",
            source=f"alternative:{exp_id}",
        ))
        doc.version_count = new_version
        doc.current_content = content
        return {"document_id": document_id, "version_number": new_version, "unchanged": False}


def create_doc_experiment(owner: str, project_id: str, goal: str, base_content: str, *,
                           document_id: Optional[str] = None) -> Dict[str, Any]:
    """`create_experiment`'s sibling for a single document with no
    filesystem workspace at all (`base_kind="doc"`).

    `document_id`, when given, names a real Studio document this owner can
    read: the experiment's base text becomes that document's LIVE
    `current_content` (never the possibly-stale `base_content` the caller
    passed alongside it) and every `doc_version` alternative created under
    it can later be `apply_alternative`d straight into that document's own
    version history (see `_persist_alternative_as_document_version`).
    Without `document_id`, `doc_version` keeps working exactly as before:
    a self-contained text experiment with no live document behind it —
    `base_content` is used as given, and `apply_alternative` only ever
    returns the merged text, it writes nothing anywhere."""
    goal = (goal or "").strip()
    if not goal:
        raise AlternativesError("`goal` is required", error_class="alternatives.invalid_request")
    document_id = (document_id or "").strip() or None
    resolved_content = _load_live_document(owner, document_id) if document_id else str(base_content or "")
    exp_id = _new_id("exp")
    exp = {
        "id": exp_id,
        "version": VERSION,
        "project_id": project_id,
        "owner": owner,
        "goal": goal,
        "base_kind": "doc",
        "base_ref": f"doc-{uuid.uuid4().hex[:12]}",
        "workspace": None,
        "document_id": document_id,
        "doc_base_content": resolved_content,
        "alternatives": [],
        "created_at": time.time(),
        "applied": None,
    }
    _save(exp)
    return exp


# ---------------------------------------------------------------------------
# Running tests inside an alternative (no effect outside its own isolation)
# ---------------------------------------------------------------------------
def run_tests(owner: str, exp_id: str, alt_id: str, command: str, *,
              timeout: float = DEFAULT_TEST_TIMEOUT) -> Dict[str, Any]:
    """Run `command` (split with `shlex`, never a shell) with `cwd` set to
    the alternative's own isolated directory — a `doc_version` alternative
    has no directory to run anything in, and is refused up front."""
    exp = _load_experiment(owner, exp_id)
    alt = _alt_or_404(exp, alt_id)
    if alt["isolation"] == "doc_version":
        raise AlternativesError("doc_version alternatives have no directory to run tests in",
                                 error_class="alternatives.invalid_request")
    command = (command or "").strip()
    if not command:
        raise AlternativesError("`command` is required", error_class="alternatives.invalid_request")
    # POSIX splitting (shlex) eats the backslashes of a Windows path
    # (`D:\venv\Scripts\python.exe` -> `D:venvScriptspython.exe`), so on
    # Windows the string goes to CreateProcess as-is, which parses quotes the
    # way the user's shell would; the subprocess is still started without a shell on either side.
    try:
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        raise AlternativesError(f"could not parse command: {exc}", error_class="alternatives.invalid_request") from exc
    if not argv:
        raise AlternativesError("`command` is empty", error_class="alternatives.invalid_request")
    to_run: Any = command if os.name == "nt" else argv

    started = time.time()
    try:
        proc = subprocess.run(
            to_run, cwd=alt["path"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(1.0, min(float(timeout or DEFAULT_TEST_TIMEOUT), 600.0)),
            env=native_host_environment(),
        )
        output = ((proc.stdout or "") + (proc.stderr or ""))[:MAX_TEST_OUTPUT_BYTES]
        result = {
            "command": command, "exit_code": proc.returncode, "ok": proc.returncode == 0,
            "output": output, "ran_at": started, "duration_s": round(time.time() - started, 3),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        result = {
            "command": command, "exit_code": None, "ok": False,
            "output": f"timed out after {timeout:.0f}s", "ran_at": started,
            "duration_s": round(time.time() - started, 3), "timed_out": True,
        }
    except OSError as exc:
        result = {
            "command": command, "exit_code": None, "ok": False,
            "output": f"could not run: {exc}", "ran_at": started,
            "duration_s": round(time.time() - started, 3), "timed_out": False,
        }
    alt["tests_result"] = result
    alt["status"] = "ready" if result["ok"] else "failed"
    _save(exp)
    return result


# ---------------------------------------------------------------------------
# Diffing (against the base; text-only — a binary/too-large file is flagged,
# never diffed byte by byte)
# ---------------------------------------------------------------------------
def _read_text(path: str) -> Optional[str]:
    """None: missing. `""`/text: present. A file over MAX_FILE_BYTES or that
    fails utf-8 decode is treated as missing-for-diff-purposes but flagged
    `binary_or_too_large` by the caller, never silently diffed as empty."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _rel_files(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return out


def _git_show(repo_root: str, ref: str, rel_path: str) -> Optional[str]:
    proc = git_panel.run_git(repo_root, "show", f"{ref}:{rel_path}")
    if proc.returncode != 0:
        return None
    return proc.stdout


def _diff_stat(base_text: Optional[str], other_text: Optional[str]) -> Dict[str, Any]:
    if base_text is None and other_text is None:
        return {"change": "none", "additions": 0, "deletions": 0}
    if base_text is None:
        return {"change": "added", "additions": len((other_text or "").splitlines()), "deletions": 0}
    if other_text is None:
        return {"change": "deleted", "additions": 0, "deletions": len(base_text.splitlines())}
    if base_text == other_text:
        return {"change": "none", "additions": 0, "deletions": 0}
    sm = difflib.SequenceMatcher(a=base_text.splitlines(), b=other_text.splitlines(), autojunk=False)
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1
    return {"change": "modified", "additions": additions, "deletions": deletions}


def _alt_changed_files(exp: Dict[str, Any], alt: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """`{relative_path: diff_stat}` for every file that differs between the
    experiment's base and this alternative's current content."""
    if alt["isolation"] == "doc_version":
        stat = _diff_stat(base_doc_content(exp), str(alt.get("content") or ""))
        return {} if stat["change"] == "none" else {"(document)": stat}

    root = alt["path"]
    changed: Dict[str, Dict[str, Any]] = {}
    if exp["base_kind"] == "git_sha":
        # --no-renames: a rename is reported as the delete + add it is, so
        # every key is a real path (with -M the key came back as
        # "old => new", which names no file `apply` could write).
        proc = git_panel.run_git(root, "diff", "--no-color", "--no-ext-diff", "--no-renames", "--numstat", exp["base_ref"])
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                add_s, del_s, rel = parts
                add = None if add_s == "-" else int(add_s)
                dele = None if del_s == "-" else int(del_s)
                base_text = _git_show(exp["workspace"], exp["base_ref"], rel)
                change = "added" if base_text is None else "modified"
                if not os.path.isfile(os.path.join(root, rel)):
                    change = "deleted"
                changed[rel] = {"change": change, "additions": add, "deletions": dele}
        # A file the alternative created and never `git add`ed is invisible
        # to `git diff <commit>`; it is still part of what the alternative did.
        for rel, change in _git_changed_vs_base(exp, root).items():
            if rel not in changed and change == "added":
                text = _read_text(os.path.join(root, rel))
                changed[rel] = {"change": "added",
                                "additions": None if text is None else len(text.splitlines()),
                                "deletions": None if text is None else 0}
    else:  # snapshot
        base_root = os.path.join(_exp_dir(exp["id"]), _BASE_SNAPSHOT_DIR)
        all_rel = set(_rel_files(base_root)) | set(_rel_files(root))
        for rel in sorted(all_rel):
            base_text = _read_text(os.path.join(base_root, rel))
            other_text = _read_text(os.path.join(root, rel))
            stat = _diff_stat(base_text, other_text)
            if stat["change"] != "none":
                changed[rel] = stat
    return changed


def compare(owner: str, exp_id: str) -> Dict[str, Any]:
    """Diffs of every alternative against the base, plus which files more
    than one alternative touches (the set that `apply`/`combine` will need
    a three-way merge for, or that will conflict between alternatives if
    combined onto the same file) — the CMP-13 contract's "diffs contra base
    y entre alternativas", read as "where would picking different
    alternatives per file actually collide" rather than a full pairwise
    text diff of every alternative against every other (documented scope
    limit, see docs/api/alternatives.md)."""
    exp = _load_experiment(owner, exp_id)
    per_alt: Dict[str, Dict[str, Dict[str, Any]]] = {}
    touch_count: Dict[str, List[str]] = {}
    for alt in exp["alternatives"]:
        changed = _alt_changed_files(exp, alt)
        alt["diff_summary"] = {
            "files_changed": len(changed),
            "additions": sum(v.get("additions") or 0 for v in changed.values()),
            "deletions": sum(v.get("deletions") or 0 for v in changed.values()),
        }
        per_alt[alt["id"]] = changed
        for rel in changed:
            touch_count.setdefault(rel, []).append(alt["id"])
    _save(exp)
    contested = {rel: alts for rel, alts in touch_count.items() if len(alts) > 1}
    return {
        "experiment": exp,
        "base_ref": exp["base_ref"],
        "alternatives": [
            {"id": a["id"], "label": a["label"], "status": a["status"],
             "diff_summary": a["diff_summary"], "tests_result": a["tests_result"],
             "files": per_alt[a["id"]]}
            for a in exp["alternatives"]
        ],
        "contested_files": contested,
    }


# ---------------------------------------------------------------------------
# Pairwise comparison: ALTERNATIVE vs ALTERNATIVE (OBJ-47)
#
# `compare` answers "what did each alternative change against the base, and
# which files do several of them touch". It never answers "how does
# alternative A differ from alternative B", which is the question a person
# actually asks before picking one. `compare_pair` answers it file by file:
# a unified diff from A to B, the added/removed/changed/renamed files, binary
# and oversize files flagged instead of diffed, every cap stated in the
# result (nothing is silently cut), and, for the files BOTH alternatives
# touched relative to the base, whether their edits are identical, would
# merge cleanly, or would conflict -- the same `git merge-file` verdict
# `apply`/`combine` use, so the preview and the real merge cannot disagree.
# Read-only: it writes nothing, not even the experiment record.
# ---------------------------------------------------------------------------
MAX_PAIR_FILE_BYTES = 512_000        # a file above this is listed but never diffed line by line
MAX_PAIR_DIFF_LINES = 1_500          # lines of unified diff kept per file
MAX_PAIR_FILES = 400                 # files listed in one answer
MAX_PAIR_TOTAL_DIFF_CHARS = 600_000  # diff text kept across the whole answer
RENAME_SIMILARITY = 0.6
_RENAME_FUZZY_MAX_BYTES = 64_000
_RENAME_CANDIDATE_CAP = 40
_BINARY_SNIFF_BYTES = 8_000
_PAIR_DOC_PATH = "(document)"
_OVERLAP_RANK = {"conflict": 0, "unknown": 1, "mergeable": 2, "identical": 3, None: 4}


class _Blob:
    """What one alternative holds at one path: size, content hash, and the
    decoded text only when it is small enough and really text."""

    __slots__ = ("size", "sha", "text", "binary", "too_large")

    def __init__(self, size: int, sha: str, text: Optional[str], binary: bool, too_large: bool):
        self.size = size
        self.sha = sha
        self.text = text
        self.binary = binary
        self.too_large = too_large


def _blob_from_bytes(data: bytes, cap: int) -> _Blob:
    binary = b"\0" in data[:_BINARY_SNIFF_BYTES]
    text: Optional[str] = None
    if not binary and len(data) <= cap:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
    return _Blob(len(data), hashlib.sha256(data).hexdigest(), text, binary,
                 too_large=(not binary and text is None))


def _blob_from_file(path: str, cap: int) -> Optional[_Blob]:
    """None: no such file. The file is streamed through the hash, and only
    held in memory when it is within `cap`."""
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        digest = hashlib.sha256()
        head = b""
        parts: Optional[List[bytes]] = [] if size <= cap else None
        with open(path, "rb") as fh:
            first = True
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                if first:
                    head = chunk[:_BINARY_SNIFF_BYTES]
                    first = False
                digest.update(chunk)
                if parts is not None:
                    parts.append(chunk)
    except OSError:
        return None
    binary = b"\0" in head
    text: Optional[str] = None
    if not binary and parts is not None:
        try:
            text = b"".join(parts).decode("utf-8")
        except UnicodeDecodeError:
            binary = True
    return _Blob(size, digest.hexdigest(), text, binary, too_large=(not binary and text is None))


def _git_changed_vs_base(exp: Dict[str, Any], root: str) -> Dict[str, str]:
    """`{path: added|modified|deleted}` for a worktree alternative, renames
    reported as the delete + add they are (a rename is not a path), and
    files the alternative created without `git add` included."""
    out: Dict[str, str] = {}
    proc = git_panel.run_git(root, "diff", "--no-color", "--no-ext-diff", "--no-renames",
                             "--name-status", "-z", exp["base_ref"])
    if proc.returncode == 0:
        tokens = proc.stdout.split("\0")
        i = 0
        while i + 1 < len(tokens):
            code, rel = tokens[i], tokens[i + 1]
            i += 2
            if not rel:
                continue
            out[rel] = {"A": "added", "D": "deleted"}.get(code[:1], "modified")
    other = git_panel.run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if other.returncode == 0:
        for rel in other.stdout.split("\0"):
            if rel and rel not in out:
                out[rel] = "added"
    return out


def _fs_changed_vs_base(base_root: str, root: str, cap: int) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for rel in sorted(set(_rel_files(base_root)) | set(_rel_files(root))):
        before = _blob_from_file(os.path.join(base_root, rel), cap)
        after = _blob_from_file(os.path.join(root, rel), cap)
        if before is None and after is None:
            continue
        if before is None:
            out[rel] = "added"
        elif after is None:
            out[rel] = "deleted"
        elif before.sha != after.sha:
            out[rel] = "modified"
    return out


def _pair_touched(exp: Dict[str, Any], alt: Dict[str, Any], cap: int) -> Dict[str, str]:
    if alt["isolation"] == "doc_version":
        changed = str(alt.get("content") or "") != base_doc_content(exp)
        return {_PAIR_DOC_PATH: "modified"} if changed else {}
    if exp["base_kind"] == "git_sha":
        return _git_changed_vs_base(exp, alt["path"])
    return _fs_changed_vs_base(os.path.join(_exp_dir(exp["id"]), _BASE_SNAPSHOT_DIR), alt["path"], cap)


def _pair_reader(alt: Dict[str, Any], cap: int):
    if alt["isolation"] == "doc_version":
        content = str(alt.get("content") or "")
        return lambda rel: _blob_from_bytes(content.encode("utf-8"), cap) if rel == _PAIR_DOC_PATH else None
    root = alt["path"]
    return lambda rel: _blob_from_file(os.path.join(root, rel), cap)


def _pair_base_text(exp: Dict[str, Any], rel: str) -> Optional[str]:
    if exp["base_kind"] == "doc":
        return base_doc_content(exp)
    if exp["base_kind"] == "git_sha":
        return _git_show(exp["workspace"], exp["base_ref"], rel)
    return _read_text(os.path.join(_exp_dir(exp["id"]), _BASE_SNAPSHOT_DIR, rel))


def _pair_overlap(exp: Dict[str, Any], rel: str, blob_a: Optional[_Blob], blob_b: Optional[_Blob]) -> str:
    """Both alternatives touched `rel` relative to the base: would picking
    both be a no-op (`identical`), a clean three-way merge (`mergeable`) or
    a collision a human has to settle (`conflict`)? `unknown` only when git
    itself is missing and the merge could not be attempted."""
    if blob_a is None and blob_b is None:
        return "identical"
    if blob_a is None or blob_b is None:
        return "conflict"
    if blob_a.sha == blob_b.sha:
        return "identical"
    if blob_a.text is None or blob_b.text is None:
        return "conflict"
    try:
        _merged, had_conflict = _merge_text(_pair_base_text(exp, rel) or "", blob_a.text, blob_b.text)
    except AlternativesError:
        return "unknown"
    return "conflict" if had_conflict else "mergeable"


def _unified_lines(old_path: Optional[str], new_path: Optional[str],
                   old_text: str, new_text: str) -> List[str]:
    """A unified diff as a list of lines without their line endings, headers
    included (`--- a/x` / `+++ b/x`, `/dev/null` for an absent side). A last
    line without a newline gets git's own marker so an end-of-file-only
    difference is not an invisible one."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    out: List[str] = []
    gen = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{old_path}" if old_path is not None else "/dev/null",
        tofile=f"b/{new_path}" if new_path is not None else "/dev/null",
        n=3,
    )
    for line in gen:
        if line.endswith("\n"):
            out.append(line[:-1])
        else:
            out.append(line)
            if line[:1] in ("+", "-", " ") and not line.startswith(("+++", "---")):
                out.append("\\ No newline at end of file")
    return out


def _pair_entry(rel: str, status: str, blob_a: Optional[_Blob], blob_b: Optional[_Blob], *,
                old_path: Optional[str], max_lines: int, chars_left: int) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "path": rel, "status": status, "old_path": old_path,
        "size_a": blob_a.size if blob_a else None, "size_b": blob_b.size if blob_b else None,
        "binary": bool((blob_a and blob_a.binary) or (blob_b and blob_b.binary)),
        "too_large": False, "additions": None, "deletions": None,
        "diff": "", "diff_truncated": False, "omitted_lines": 0, "diff_total_lines": 0,
    }
    if entry["binary"]:
        return entry
    if (blob_a and blob_a.too_large) or (blob_b and blob_b.too_large):
        entry["too_large"] = True
        return entry
    old_text = blob_a.text if blob_a else ""
    new_text = blob_b.text if blob_b else ""
    lines = _unified_lines(
        (old_path or rel) if blob_a is not None else None,
        rel if blob_b is not None else None,
        old_text or "", new_text or "",
    )
    body = lines[2:] if len(lines) >= 2 else lines
    entry["additions"] = sum(1 for ln in body if ln.startswith("+"))
    entry["deletions"] = sum(1 for ln in body if ln.startswith("-"))
    entry["diff_total_lines"] = len(lines)
    kept = lines[:max_lines]
    used = 0
    cut: List[str] = []
    for ln in kept:
        used += len(ln) + 1
        if used > chars_left:
            break
        cut.append(ln)
    entry["diff"] = "\n".join(cut)
    entry["omitted_lines"] = len(lines) - len(cut)
    entry["diff_truncated"] = entry["omitted_lines"] > 0
    return entry


def _pair_renames(removed: Dict[str, _Blob], added: Dict[str, _Blob]) -> Dict[str, str]:
    """`{new_path: old_path}`: a path only A has and a path only B has are
    one rename when their content is identical, or (small text files) at
    least `RENAME_SIMILARITY` alike. Deterministic: sorted, each path used
    once, the exact matches first."""
    pairs: Dict[str, str] = {}
    used_old: set = set()
    by_sha: Dict[str, List[str]] = {}
    for old in sorted(removed):
        by_sha.setdefault(removed[old].sha, []).append(old)
    for new in sorted(added):
        for old in by_sha.get(added[new].sha, []):
            if old not in used_old:
                pairs[new] = old
                used_old.add(old)
                break
    fuzzy_new = [n for n in sorted(added) if n not in pairs
                 and added[n].text is not None and added[n].size <= _RENAME_FUZZY_MAX_BYTES]
    fuzzy_old = [o for o in sorted(removed) if o not in used_old
                 and removed[o].text is not None and removed[o].size <= _RENAME_FUZZY_MAX_BYTES]
    for new in fuzzy_new[:_RENAME_CANDIDATE_CAP]:
        best, best_ratio = None, RENAME_SIMILARITY
        new_text = added[new].text or ""
        for old in fuzzy_old[:_RENAME_CANDIDATE_CAP]:
            if old in used_old:
                continue
            sm = difflib.SequenceMatcher(None, removed[old].text or "", new_text, autojunk=False)
            if sm.real_quick_ratio() < best_ratio or sm.quick_ratio() < best_ratio:
                continue
            ratio = sm.ratio()
            if ratio >= best_ratio:
                best, best_ratio = old, ratio
        if best is not None:
            pairs[new] = best
            used_old.add(best)
    return pairs


def compare_pair(owner: str, exp_id: str, alt_a: str, alt_b: str, *,
                 max_file_bytes: int = MAX_PAIR_FILE_BYTES,
                 max_diff_lines: int = MAX_PAIR_DIFF_LINES,
                 max_files: int = MAX_PAIR_FILES) -> Dict[str, Any]:
    """Alternative `alt_a` against alternative `alt_b`, file by file.

    Direction is A -> B: a file `added` exists only in B, `removed` only in
    A, `renamed` moved (path in B, `old_path` in A), `changed` differs.
    Files identical in both are not listed (they are counted). Every cap is
    stated back in `limits`/`truncation`; a binary or oversize file is
    listed with `binary`/`too_large` and no diff, never diffed as empty.
    Read-only."""
    exp = _load_experiment(owner, exp_id)
    if alt_a == alt_b:
        raise AlternativesError("pick two different alternatives to compare",
                                 error_class="alternatives.invalid_request")
    a = _alt_or_404(exp, alt_a)
    b = _alt_or_404(exp, alt_b)
    if (a["isolation"] == "doc_version") != (b["isolation"] == "doc_version"):
        raise AlternativesError("a document alternative cannot be compared with a file alternative",
                                 error_class="alternatives.invalid_request")
    cap = max(1_000, min(int(max_file_bytes), MAX_FILE_BYTES))
    line_cap = max(20, min(int(max_diff_lines), 20_000))
    file_cap = max(1, min(int(max_files), 5_000))

    touched_a = _pair_touched(exp, a, cap)
    touched_b = _pair_touched(exp, b, cap)
    read_a, read_b = _pair_reader(a, cap), _pair_reader(b, cap)

    identical_files = 0
    differing: Dict[str, Tuple[Optional[_Blob], Optional[_Blob]]] = {}
    for rel in sorted(set(touched_a) | set(touched_b)):
        blob_a, blob_b = read_a(rel), read_b(rel)
        same = (blob_a is None and blob_b is None) or (
            blob_a is not None and blob_b is not None and blob_a.sha == blob_b.sha)
        if same:
            identical_files += 1
        else:
            differing[rel] = (blob_a, blob_b)

    removed = {r: ab[0] for r, ab in differing.items() if ab[1] is None and ab[0] is not None}
    added = {r: ab[1] for r, ab in differing.items() if ab[0] is None and ab[1] is not None}
    renames = _pair_renames(removed, added)  # new -> old

    entries: List[Dict[str, Any]] = []
    overlap_of: Dict[str, str] = {}
    for rel in sorted(set(touched_a) & set(touched_b)):
        blob_a, blob_b = read_a(rel), read_b(rel)
        overlap_of[rel] = _pair_overlap(exp, rel, blob_a, blob_b)

    renamed_old = set(renames.values())
    plan: List[Tuple[str, str, Optional[str], Optional[_Blob], Optional[_Blob]]] = []
    for rel, (blob_a, blob_b) in differing.items():
        if rel in renamed_old:
            continue
        if rel in renames:
            old = renames[rel]
            plan.append((rel, "renamed", old, differing[old][0], blob_b))
        else:
            status = "added" if blob_a is None else "removed" if blob_b is None else "changed"
            plan.append((rel, status, None, blob_a, blob_b))

    def _rank(item):
        rel, _s, old, _a, _b = item
        ranks = [_OVERLAP_RANK[overlap_of.get(p)] for p in (rel, old) if p]
        return (min(ranks), rel)

    plan.sort(key=_rank)
    chars_left = MAX_PAIR_TOTAL_DIFF_CHARS
    budget_hit = False
    for rel, status, old, blob_a, blob_b in plan[:file_cap]:
        entry = _pair_entry(rel, status, blob_a, blob_b, old_path=old, max_lines=line_cap,
                            chars_left=max(0, chars_left))
        chars_left -= len(entry["diff"]) + 1
        if entry["diff_truncated"] and chars_left <= 0:
            budget_hit = True
        sides = [p for p in (rel, old) if p]
        entry["touched_by"] = [k for k, t in (("a", touched_a), ("b", touched_b)) if any(p in t for p in sides)]
        entry["base_change"] = {
            "a": next((touched_a[p] for p in sides if p in touched_a), None),
            "b": next((touched_b[p] for p in sides if p in touched_b), None),
        }
        entry["overlap"] = next((overlap_of[p] for p in sides if p in overlap_of), None)
        entries.append(entry)

    both = sorted(set(touched_a) & set(touched_b))
    only_a = sorted(set(touched_a) - set(touched_b))
    only_b = sorted(set(touched_b) - set(touched_a))
    summary = {
        "files_differing": len(plan),
        "added": sum(1 for p in plan if p[1] == "added"),
        "removed": sum(1 for p in plan if p[1] == "removed"),
        "changed": sum(1 for p in plan if p[1] == "changed"),
        "renamed": sum(1 for p in plan if p[1] == "renamed"),
        "binary": sum(1 for e in entries if e["binary"]),
        "too_large": sum(1 for e in entries if e["too_large"]),
        "additions": sum(e["additions"] or 0 for e in entries),
        "deletions": sum(e["deletions"] or 0 for e in entries),
        "identical_files": identical_files,
        "overlap": {k: sum(1 for v in overlap_of.values() if v == k)
                    for k in ("identical", "mergeable", "conflict", "unknown")},
    }
    truncated_diffs = sum(1 for e in entries if e["diff_truncated"])
    return {
        "experiment_id": exp["id"],
        "base_ref": exp["base_ref"],
        "base_kind": exp["base_kind"],
        "a": {"id": a["id"], "label": a["label"], "isolation": a["isolation"], "files_touched": len(touched_a)},
        "b": {"id": b["id"], "label": b["label"], "isolation": b["isolation"], "files_touched": len(touched_b)},
        "identical": not plan,
        "summary": summary,
        "touched": {
            "a": len(touched_a), "b": len(touched_b), "both": len(both),
            "only_a": len(only_a), "only_b": len(only_b),
            "paths": {"only_a": only_a[:file_cap], "only_b": only_b[:file_cap], "both": both[:file_cap]},
        },
        "files": entries,
        "limits": {"max_file_bytes": cap, "max_diff_lines_per_file": line_cap, "max_files": file_cap,
                   "max_total_diff_chars": MAX_PAIR_TOTAL_DIFF_CHARS},
        "truncation": {
            "any": bool(len(plan) > file_cap or truncated_diffs or summary["too_large"]),
            "files": len(plan) > file_cap,
            "files_omitted": max(0, len(plan) - file_cap),
            "diffs": truncated_diffs,
            "diff_budget_exhausted": budget_hit,
            "files_not_diffed": summary["too_large"] + summary["binary"],
        },
    }


# ---------------------------------------------------------------------------
# Three-way merge (base / mine / theirs) via `git merge-file` — git's own
# merge algorithm, not a reimplementation. `-p` prints the result instead of
# writing `mine` in place, so this call has NO side effect of its own.
# ---------------------------------------------------------------------------
def _merge_text(base_text: str, mine_text: str, theirs_text: str) -> Tuple[str, bool]:
    if not git_panel.git_available():
        raise AlternativesError("git is not installed on this host", error_class="dependency.missing")
    with tempfile.TemporaryDirectory(prefix="faustus-alt-merge-") as tmp:
        mine_p = os.path.join(tmp, "mine")
        base_p = os.path.join(tmp, "base")
        theirs_p = os.path.join(tmp, "theirs")
        for p, text in ((mine_p, mine_text), (base_p, base_text), (theirs_p, theirs_text)):
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", mine_p, base_p, theirs_p],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=native_host_environment(),
        )
    if proc.returncode == 0:
        return proc.stdout, False
    if proc.returncode == 1:
        return proc.stdout, True
    # >1: git couldn't even attempt it (binary content, etc) -- treated as a
    # conflict rather than silently picking a side.
    return proc.stdout or "", True


def _atomic_write_text(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".alt-write-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class _FilePlan:
    __slots__ = ("rel", "action", "content")

    def __init__(self, rel: str, action: str, content: Optional[str] = None):
        self.rel = rel          # relative path, or "(document)" for doc_version
        self.action = action    # "write" | "delete" | "skip"
        self.content = content


def _plan_merge(base_text: Optional[str], mine_text: Optional[str],
                 theirs_text: Optional[str]) -> Tuple[str, Optional[str]]:
    """One file's merge plan: `("write"|"delete"|"skip", content_or_None)`.
    Never guesses on a real collision -- see the branch comments. This is
    the one place conflict-vs-clean is decided, so `apply_alternative`/
    `combine` can collect every file's plan BEFORE writing anything and
    abort the whole operation on the first conflict found (CMP-13's own
    decisive test: a conflict must never leave a partial mix on disk)."""
    if theirs_text == mine_text:
        return "skip", None  # nothing to do either way
    if mine_text == base_text:
        # The user has not touched this file since the experiment started:
        # take the alternative's version outright (fast-forward), deletion
        # included.
        return ("delete", None) if theirs_text is None else ("write", theirs_text)
    if theirs_text == base_text:
        # The alternative never actually changed this file (present in the
        # changed set only because another alternative did) -- nothing of
        # theirs to bring in.
        return "skip", None
    if theirs_text is None:
        return "conflict", None  # alt deleted a file the user has since edited
    if mine_text is None:
        return "conflict", None  # user deleted a file the alt has since edited
    merged, had_conflict = _merge_text(base_text, mine_text, theirs_text)
    return ("conflict", None) if had_conflict else ("write", merged)


def _build_plans(exp: Dict[str, Any], alt: Dict[str, Any],
                  rel_paths: Optional[Sequence[str]] = None) -> Tuple[List[_FilePlan], List[str]]:
    """Plans + conflicting-path list for every changed file between `alt`
    and the base, restricted to `rel_paths` when given (used by `combine`,
    where only some files come from this particular alternative)."""
    changed = _alt_changed_files(exp, alt)
    wanted = set(rel_paths) if rel_paths is not None else set(changed)
    plans: List[_FilePlan] = []
    conflicts: List[str] = []

    if alt["isolation"] == "doc_version":
        if "(document)" in wanted:
            action, content = _plan_merge(base_doc_content(exp), exp.get("_mine_doc_content"), str(alt.get("content") or ""))
            if action == "conflict":
                conflicts.append("(document)")
            elif action != "skip":
                plans.append(_FilePlan("(document)", action, content))
        return plans, conflicts

    for rel in sorted(wanted):
        if rel not in changed:
            continue
        base_text = _git_show(exp["workspace"], exp["base_ref"], rel) if exp["base_kind"] == "git_sha" else \
            _read_text(os.path.join(_exp_dir(exp["id"]), _BASE_SNAPSHOT_DIR, rel))
        # "mine" is always the CURRENT content of the main copy, regardless
        # of base_kind -- the whole point of the merge is to respect
        # whatever the user has done there since the experiment started.
        mine_text = _read_text(os.path.join(exp["workspace"], rel))
        theirs_text = _read_text(os.path.join(alt["path"], rel))
        action, content = _plan_merge(base_text, mine_text, theirs_text)
        if action == "conflict":
            conflicts.append(rel)
        elif action != "skip":
            plans.append(_FilePlan(rel, action, content))
    return plans, conflicts


def apply_alternative(owner: str, exp_id: str, alt_id: str, *,
                       mine_doc_content: Optional[str] = None) -> Dict[str, Any]:
    """Merge `alt_id`'s changes into the main copy. Every touched file is
    planned first; if ANY plan is a conflict, `ApplyConflictError` is
    raised and NOTHING is written -- the main copy is left exactly as the
    user last had it, conflicting or not.

    For a `doc_version` alternative on an experiment with a real
    `document_id`: `mine_doc_content` defaults to that document's LIVE
    `current_content` (fetched fresh, not `base_doc_content(exp)`'s
    creation-time snapshot) when the caller doesn't pass one explicitly —
    "mine" is always the CURRENT content of the main copy, the same rule
    `_build_plans` already documents for the filesystem cases — and, once
    the merge plan is clean, the result is written onto the real document
    as a new `DocumentVersion` (see `_persist_alternative_as_document_version`)
    BEFORE this experiment's own record is marked applied, so a document
    write failure (deleted document, owner mismatch) leaves `alt_id`
    exactly as unapplied as a raised conflict would."""
    exp = _load_experiment(owner, exp_id)
    alt = _alt_or_404(exp, alt_id)
    if alt["isolation"] == "doc_version":
        if mine_doc_content is not None:
            exp["_mine_doc_content"] = mine_doc_content
        elif exp.get("document_id"):
            exp["_mine_doc_content"] = _load_live_document(owner, str(exp["document_id"]))
        else:
            exp["_mine_doc_content"] = base_doc_content(exp)
    plans, conflicts = _build_plans(exp, alt)
    if conflicts:
        raise ApplyConflictError(conflicts)

    applied: List[str] = []
    result_doc_content: Optional[str] = None
    for plan in plans:
        if alt["isolation"] == "doc_version":
            result_doc_content = plan.content if plan.action == "write" else ""
            applied.append("(document)")
            continue
        full = os.path.join(exp["workspace"], plan.rel)
        if plan.action == "delete":
            try:
                os.remove(full)
            except OSError:
                pass
        else:
            _atomic_write_text(full, plan.content or "")
        applied.append(plan.rel)

    document_version: Optional[Dict[str, Any]] = None
    if alt["isolation"] == "doc_version" and result_doc_content is not None and exp.get("document_id"):
        document_version = _persist_alternative_as_document_version(
            owner, str(exp["document_id"]), result_doc_content,
            exp_id=exp_id, alt_id=alt_id, alt_label=str(alt.get("label") or alt_id),
        )

    exp["applied"] = {"alternative_id": alt_id, "applied_at": time.time(), "files": applied}
    alt["status"] = "applied"
    exp.pop("_mine_doc_content", None)
    _save(exp)
    out: Dict[str, Any] = {"applied_files": applied, "skipped_same": True if not applied else False}
    if result_doc_content is not None:
        out["content"] = result_doc_content
    if document_version is not None:
        out["document_version"] = document_version
    return out


def combine(owner: str, exp_id: str, choices: Dict[str, str], *,
            mine_doc_content: Optional[str] = None) -> Dict[str, Any]:
    """`choices`: `{relative_path: alternative_id}` — apply, PER FILE, the
    named alternative's version of that one file. Still all-or-nothing:
    every file's plan is built before anything is written, and a conflict
    on any one of them aborts the whole combine (same guarantee as
    `apply_alternative`, just sliced by file instead of by whole
    alternative)."""
    exp = _load_experiment(owner, exp_id)
    by_alt: Dict[str, List[str]] = {}
    for rel, alt_id in choices.items():
        by_alt.setdefault(alt_id, []).append(rel)

    all_plans: List[_FilePlan] = []
    all_conflicts: List[str] = []
    for alt_id, rels in by_alt.items():
        alt = _alt_or_404(exp, alt_id)
        if alt["isolation"] == "doc_version":
            exp["_mine_doc_content"] = mine_doc_content if mine_doc_content is not None else base_doc_content(exp)
        plans, conflicts = _build_plans(exp, alt, rels)
        all_plans.extend(plans)
        all_conflicts.extend(conflicts)
    if all_conflicts:
        raise ApplyConflictError(all_conflicts)

    applied: List[Dict[str, str]] = []
    for alt_id, rels in by_alt.items():
        for plan in [p for p in all_plans if p.rel in rels]:
            full = os.path.join(exp["workspace"], plan.rel)
            if plan.action == "delete":
                try:
                    os.remove(full)
                except OSError:
                    pass
            else:
                _atomic_write_text(full, plan.content or "")
            applied.append({"path": plan.rel, "from": alt_id})

    exp["applied"] = {"combine": True, "applied_at": time.time(),
                       "files": [a["path"] for a in applied], "sources": choices}
    exp.pop("_mine_doc_content", None)
    _save(exp)
    return {"applied": applied}
