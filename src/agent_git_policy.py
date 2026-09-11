"""agent_git_policy.py -- OBJ-4 (Lote 82): how the agent itself behaves
inside a git repository it happens to be working in. Luis's own framing:
"does it use a separate branch? commit? push? -- and depending on that, it
creates branches or not, commits or not, pushes or not, at the user's
preference."

Two levels, like every other per-repo override in this codebase: a global
default (`src.settings` DEFAULT_SETTINGS) and an optional per-repo override
(`DATA_DIR/git_repo_policies.json`, owner+repo_id keyed). `effective_policy`
merges them; an all-off effective policy is the default, and this module
never touches a repo when it is (see `before_turn`/`after_turn` below).

Why the setting is named `git_agent_policy` rather than the `agent_git_policy`
the OBJ-4 contract text uses: `src/agent_settings_schema.py` requires every
`agent_*`/`browser_*`/`desktop_*` key in `DEFAULT_SETTINGS` to carry a schema
field of one of its scalar `FIELD_TYPES` (bool/int/float/text/select/list/
secret) -- checked by `tests/test_agent_settings_schema.py`'s parity tests,
which this lote does not own and must leave green. There is no "object"
field type, and this policy is a five-key nested dict edited through its own
dedicated route (`GET/PUT /api/git/policy`, `GET/PUT
/api/git/repos/{id}/policy`) and its own Studio card -- never through the
generic per-field settings form `agent_settings_schema.py` renders. Renaming
it out of that prefix is the smaller, more honest fix than teaching the
schema parity check about a key it structurally cannot describe.

Two hooks, wired from `routes/chat_routes.py`:

* `before_turn(workspace, session_id, owner)` -- called once per agent turn
  that has a bound `workspace`, right before that turn is handed to
  `stream_agent_loop` (`chat_stream`'s `stream_with_save` generator, the
  natural "a turn is about to run against this workspace" point -- see that
  call site's comment for exactly where). Creates the agent's working branch
  when `use_branch` is on. Idempotent by construction and without any
  session-branch table to maintain or go stale: it only creates a branch
  when HEAD is NOT already on one matching `branch_prefix`, so once turn 1
  switches onto e.g. `faustus/abc123-260911`, every later turn in the same
  session finds HEAD already there and does nothing.

* `after_turn(workspace, session_id, owner, touched_files, summary)` --
  called from `_record_turn_side_effects` in `routes/chat_routes.py`, which
  already knows exactly which files the turn's harness wrote/edited/deleted.
  Stages exactly those files (`src.git_panel.stage`, never `-A` over the
  whole tree), commits with the repo's OWN configured identity (never one
  this module injects -- a repo with none gets a `git.no_identity` event and
  no commit, same rule `git_panel.commit` already enforces for the manual
  Source Control panel), and -- only after that commit succeeds -- pushes.

Both hooks return `git_policy` event dicts
(`{"action": "branch"|"commit"|"push", "ok": bool, "branch"?, "sha"?,
"detail"?, "skipped"?}`) for `routes/chat_routes.py` to forward as SSE so the
Studio transcript can render a chip. Nothing here ever force-pushes
(`git_panel.push` has no force flag to begin with) or commits/pushes over a
conflicted working tree.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from src import git_panel
from src import settings as settings_mod

logger = logging.getLogger(__name__)

#: See the module docstring for why this isn't `agent_git_policy`.
GLOBAL_SETTING_KEY = "git_agent_policy"

DEFAULT_POLICY: Dict[str, Any] = {
    "use_branch": False,
    "branch_prefix": "faustus/",
    "commit": False,
    "commit_message_prefix": "faustus: ",
    "push": False,
    "push_set_upstream": True,
}

_POLICY_KEYS = frozenset(DEFAULT_POLICY)
_LOCK = threading.RLock()


class GitPolicyError(Exception):
    """A policy patch this module refuses: an unknown field, or a value of
    the wrong type. `code` is the short tag routes turn into `git.<code>`."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    checked: Dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key == "inherit":
            continue
        if key not in _POLICY_KEYS:
            raise GitPolicyError("unknown_field", f"unknown policy field {key!r}")
        default = DEFAULT_POLICY[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise GitPolicyError("bad_type", f"{key} expects a boolean")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise GitPolicyError("bad_type", f"{key} expects a string")
        checked[key] = value
    return checked


def _normalize(policy: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULT_POLICY)
    for key in _POLICY_KEYS:
        if key in (policy or {}):
            out[key] = policy[key]
    return out


# ---------------------------------------------------------------------------
# Global policy (src/settings.py)
# ---------------------------------------------------------------------------
def get_global_policy() -> Dict[str, Any]:
    raw = settings_mod.get_setting(GLOBAL_SETTING_KEY, DEFAULT_POLICY)
    return _normalize(raw if isinstance(raw, dict) else {})


def set_global_policy(patch: Dict[str, Any]) -> Dict[str, Any]:
    checked = _validate_patch(patch)
    with _LOCK:
        merged = _normalize({**get_global_policy(), **checked})
        settings_mod.update_settings({GLOBAL_SETTING_KEY: merged})
    return merged


# ---------------------------------------------------------------------------
# Per-repo override (DATA_DIR/git_repo_policies.json, owner+repo_id keyed)
# ---------------------------------------------------------------------------
def _repo_policies_file() -> str:
    from src.constants import DATA_DIR  # lazy: a test can repoint DATA_DIR first
    return os.path.join(DATA_DIR, "git_repo_policies.json")


def _owner_key(owner: Optional[str]) -> str:
    return str(owner or "").strip() or "_"


def _load_all(path: Optional[str] = None) -> Dict[str, Any]:
    import json

    target = path or _repo_policies_file()
    if not os.path.isfile(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        logger.error("agent_git_policy: store %s unreadable: %s", target, e)
        return {}
    return raw if isinstance(raw, dict) else {}


def get_repo_override(owner: Optional[str], repo_id: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    bucket = _load_all(path).get(_owner_key(owner))
    entry = bucket.get(repo_id) if isinstance(bucket, dict) else None
    return dict(entry) if isinstance(entry, dict) else None


def set_repo_override(owner: Optional[str], repo_id: str, patch: Dict[str, Any],
                      path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`{"inherit": true}` clears the override (returns None); anything else
    is validated and merged into the existing override (creating one if
    there wasn't any)."""
    target = path or _repo_policies_file()
    if patch.get("inherit"):
        with _LOCK:
            all_data = _load_all(path)
            bucket = all_data.get(_owner_key(owner))
            if isinstance(bucket, dict) and repo_id in bucket:
                bucket.pop(repo_id, None)
                atomic_write_json(target, all_data, indent=2)
        return None

    checked = _validate_patch(patch)
    with _LOCK:
        all_data = _load_all(path)
        owner_key = _owner_key(owner)
        bucket = all_data.get(owner_key)
        if not isinstance(bucket, dict):
            bucket = {}
            all_data[owner_key] = bucket
        current = bucket.get(repo_id)
        current = dict(current) if isinstance(current, dict) else {}
        merged = {**current, **checked}
        bucket[repo_id] = merged
        atomic_write_json(target, all_data, indent=2)
    return dict(merged)


def effective_policy(owner: Optional[str], repo_id: str, path: Optional[str] = None) -> Dict[str, Any]:
    override = get_repo_override(owner, repo_id, path)
    base = get_global_policy()
    effective = _normalize({**base, **override}) if override else base
    return {"effective": effective, "overridden": bool(override), "override": override}


# ---------------------------------------------------------------------------
# before_turn: create/reuse the agent's working branch
# ---------------------------------------------------------------------------
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _session_slug(session_id: str) -> str:
    slug = _SLUG_RE.sub("-", str(session_id or "").strip().lower()).strip("-")
    return (slug or "session")[:24]


def _branch_name(prefix: str, session_id: str) -> str:
    prefix = str(prefix or "").strip() or "faustus/"
    if not prefix.endswith("/"):
        prefix += "/"
    stamp = datetime.now(timezone.utc).strftime("%y%m%d")
    return f"{prefix}{_session_slug(session_id)}-{stamp}"


def _is_dirty(status: Dict[str, Any]) -> bool:
    return bool(status.get("staged") or status.get("unstaged") or
                status.get("untracked") or status.get("conflicts"))


def before_turn(workspace: str, session_id: str, owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """One `git_policy` event, or None when there's nothing to report (no
    repo at `workspace`, `use_branch` is off, or HEAD is already on an
    agent branch from an earlier turn in this session)."""
    workspace = str(workspace or "").strip()
    if not workspace:
        return None
    repo_root = git_panel.repo_toplevel(workspace)
    if not repo_root:
        return None
    repo_id = git_panel.compute_repo_id(repo_root)
    policy = effective_policy(owner, repo_id)["effective"]
    if not policy.get("use_branch"):
        return None

    branch, detached = git_panel.current_branch(repo_root)
    prefix = policy.get("branch_prefix") or "faustus/"
    if not detached and branch and branch.startswith(prefix):
        return None  # already on an agent branch from an earlier turn -- reuse it

    status = git_panel.repo_status(repo_root)
    if _is_dirty(status):
        return {"action": "branch", "ok": False, "skipped": "dirty", "branch": branch}

    new_branch = _branch_name(prefix, session_id)
    try:
        git_panel.create_branch(repo_root, new_branch, checkout=True)
    except git_panel.GitCommandError as e:
        stderr = e.stderr or e.stdout or ""
        if "already exists" in stderr:
            try:
                git_panel.checkout_branch(repo_root, new_branch)
            except git_panel.GitCommandError as e2:
                return {"action": "branch", "ok": False, "branch": new_branch,
                        "detail": git_panel.stderr_snippet(e2.stderr or e2.stdout)}
            else:
                return {"action": "branch", "ok": True, "branch": new_branch}
        return {"action": "branch", "ok": False, "branch": new_branch,
                "detail": git_panel.stderr_snippet(stderr)}
    return {"action": "branch", "ok": True, "branch": new_branch}


# ---------------------------------------------------------------------------
# after_turn: stage the touched files, commit, push
# ---------------------------------------------------------------------------
def _summary_line(summary: str) -> str:
    text = (summary or "").strip()
    line = text.splitlines()[0].strip() if text else ""
    return (line or "agent turn")[:100]


def after_turn(workspace: str, session_id: str, owner: Optional[str],
               touched_files: List[str], summary: str = "") -> List[Dict[str, Any]]:
    """`touched_files`: the paths (absolute, or relative to `workspace`) the
    turn's harness reported as written/edited/deleted. Returns 0-2
    `git_policy` events (commit, then push -- push only after a successful
    commit in THIS call)."""
    events: List[Dict[str, Any]] = []
    workspace = str(workspace or "").strip()
    if not workspace or not touched_files:
        return events
    repo_root = git_panel.repo_toplevel(workspace)
    if not repo_root:
        return events
    repo_id = git_panel.compute_repo_id(repo_root)
    policy = effective_policy(owner, repo_id)["effective"]
    if not policy.get("commit") and not policy.get("push"):
        return events  # all-off: never touch the repo

    status = git_panel.repo_status(repo_root)
    if status.get("conflicts"):
        # Never commit/push over an unresolved conflict, same as the manual
        # panel's own `checkout`/`create_branch` refuse to clobber changes.
        events.append({"action": "commit", "ok": False, "detail": "conflicts"})
        return events

    rel_paths = sorted({
        rel for rel in (git_panel.safe_rel_path(repo_root, str(f)) for f in touched_files) if rel
    })
    branch, _detached = git_panel.current_branch(repo_root)
    committed_sha: Optional[str] = None

    if policy.get("commit"):
        if not rel_paths:
            events.append({"action": "commit", "ok": False, "detail": "no_files"})
        else:
            user = git_panel.repo_user(repo_root)
            if not user.get("name") or not user.get("email"):
                events.append({"action": "commit", "ok": False, "detail": "git.no_identity"})
            else:
                try:
                    git_panel.stage(repo_root, paths=rel_paths)
                    message = f"{policy.get('commit_message_prefix') or ''}{_summary_line(summary)}"
                    # Lote 92 (OBJ-6): hand the project id to git_panel.commit
                    # so a FAU-12-style id (and a magic close word) in the
                    # turn's own summary links/closes the board issue -- the
                    # "agent commits" half of the board's git hook.
                    board_project_id = ""
                    try:
                        from services.projects import project_for_session
                        board_project = project_for_session(session_id, owner)
                        board_project_id = str((board_project or {}).get("id") or "")
                    except Exception:
                        logger.debug("agent_git_policy: could not resolve project for board hook", exc_info=True)
                    result = git_panel.commit(repo_root, message, project_id=board_project_id or None)
                except git_panel.GitNothingToCommitError:
                    events.append({"action": "commit", "ok": False, "detail": "nothing_to_commit"})
                except git_panel.GitNoIdentityError:
                    events.append({"action": "commit", "ok": False, "detail": "git.no_identity"})
                except git_panel.GitCommandError as e:
                    events.append({"action": "commit", "ok": False,
                                    "detail": git_panel.stderr_snippet(e.stderr or e.stdout)})
                else:
                    committed_sha = result["sha"]
                    events.append({"action": "commit", "ok": True, "branch": branch, "sha": result["short"]})

    if policy.get("push") and committed_sha:
        upstream = git_panel.upstream_ref(repo_root)
        try:
            git_panel.push(repo_root, set_upstream=(not upstream) and bool(policy.get("push_set_upstream")))
        except git_panel.GitRejectedError as e:
            events.append({"action": "push", "ok": False, "detail": git_panel.stderr_snippet(e.stderr)})
        except git_panel.GitCommandError as e:
            events.append({"action": "push", "ok": False,
                            "detail": git_panel.stderr_snippet(e.stderr or e.stdout)})
        else:
            events.append({"action": "push", "ok": True, "branch": branch})

    return events
