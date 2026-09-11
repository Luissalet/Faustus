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
                         experiment's own record — NOT wired to the real
                         Studio document store; see the module-level note
                         near ``set_doc_version_content`` for the scope this
                         stops at and why)

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
    """Set/replace a `doc_version` alternative's text. Scope note: this
    stores the text on the experiment's OWN record, not inside the real
    Studio document store (`src/document_*.py`) — wiring a `doc_version`
    alternative to an actual live document (so editing it in the document
    editor and editing it here are the same text) is future integration
    work, not done here; what IS complete is the data model and the
    apply/compare contract for this isolation kind, exercised end-to-end by
    this module's own tests against exactly this text-snapshot form."""
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


def create_doc_experiment(owner: str, project_id: str, goal: str, base_content: str) -> Dict[str, Any]:
    """`create_experiment`'s sibling for a single document with no
    filesystem workspace at all (`base_kind="doc"`)."""
    goal = (goal or "").strip()
    if not goal:
        raise AlternativesError("`goal` is required", error_class="alternatives.invalid_request")
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
        "doc_base_content": str(base_content or ""),
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
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise AlternativesError(f"could not parse command: {exc}", error_class="alternatives.invalid_request") from exc
    if not argv:
        raise AlternativesError("`command` is empty", error_class="alternatives.invalid_request")

    started = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=alt["path"], capture_output=True, text=True, encoding="utf-8", errors="replace",
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
        proc = git_panel.run_git(root, "diff", "--no-color", "--no-ext-diff", "-M", "--numstat", exp["base_ref"])
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
    user last had it, conflicting or not."""
    exp = _load_experiment(owner, exp_id)
    alt = _alt_or_404(exp, alt_id)
    if alt["isolation"] == "doc_version":
        exp["_mine_doc_content"] = mine_doc_content if mine_doc_content is not None else base_doc_content(exp)
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

    exp["applied"] = {"alternative_id": alt_id, "applied_at": time.time(), "files": applied}
    alt["status"] = "applied"
    exp.pop("_mine_doc_content", None)
    _save(exp)
    out: Dict[str, Any] = {"applied_files": applied, "skipped_same": True if not applied else False}
    if result_doc_content is not None:
        out["content"] = result_doc_content
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
