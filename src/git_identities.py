"""git_identities.py -- OBJ-4 (Lote 82): which SSH account a repo talks to.

On this app's real target setup, "account" is not a login -- it's an alias
in ``~/.ssh/config`` paired with a private key (``Host Luissalet`` ->
``id_ed25519_bookhoard``, alongside ``github-lsaletec`` and
``github-mlgpigeon``, all pointing at ``github.com``). Switching a repo's
"account" means rewriting its remote's host to a different alias -- git and
ssh do the rest, because ssh itself picks the key from the ``Host`` block
that matches. There is no separate "login" concept to model.

Three sources feed the identity list (never network calls at list time --
see the module docstring on ``probe_identity`` for why):

* ``~/.ssh/config`` ``Host`` blocks with a single, non-wildcard alias
  (``Host *`` and any multi-token/wildcard ``Host`` line is skipped -- there
  is no one concrete alias to rewrite a remote to);
* loose ``~/.ssh/*.pub`` files whose private key isn't already some block's
  ``IdentityFile`` -- listed with no alias (``ssh_host: null``), label is
  the key's filename;
* identities a human typed in through ``POST /api/git/identities``, kept in
  ``DATA_DIR/git_identities.json`` (owner-scoped) -- never the private key
  itself, only its path on disk.

``github_login`` is discovered, not configured: ``probe_identity`` runs
``ssh -T`` against the alias (or, for an alias-less loose key, ``-i
<file> git@<hostname>``) and parses GitHub's own
"Hi <login>! You've successfully authenticated" off stderr (GitHub answers
`-T` with exit 1 by design -- no shell access is exactly the point of the
probe). Cached 10 minutes per identity id so listing identities is always a
pure local read; a caller that wants a fresh answer calls the dedicated
``POST .../probe`` route.

Every subprocess (`ssh -T`, and the git calls done through `src.git_panel`)
runs via a real argv, never a shell.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from core.atomic_io import atomic_write_json
from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

_PROBE_TIMEOUT = 8.0
_PROBE_TTL = 600.0  # 10 minutes, per the contract

# id -> (monotonic timestamp, github_login, ok, detail)
_PROBE_CACHE: Dict[str, tuple] = {}

_DEFAULT_HOSTNAME = "github.com"


class GitIdentityError(Exception):
    """A request this module refuses: bad input, or an operation that needs
    something the identity doesn't have (e.g. rewriting a remote without an
    ssh alias). ``code`` is the short tag routes turn into ``git.<code>``."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Paths (resolved per call -- HOME/DATA_DIR can move under a test)
# ---------------------------------------------------------------------------
def _ssh_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh")


def _identities_file() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "git_identities.json")


def _owner_key(owner: Optional[str]) -> str:
    return str(owner or "").strip() or "_"


# ---------------------------------------------------------------------------
# ~/.ssh/config parsing
# ---------------------------------------------------------------------------
_HOST_LINE_RE = re.compile(r"^\s*Host\s+(.+?)\s*$", re.IGNORECASE)
_KV_RE = re.compile(r"^\s*(\S+)\s+(.*?)\s*$")


def parse_ssh_config(path: str) -> List[Dict[str, Any]]:
    """Every concrete (single, non-wildcard token) ``Host`` block in the ssh
    config at `path`, as ``{"alias", "hostname", "identity_file"}``.
    ``Host *`` and any pattern-y alias (containing ``*``/``?``) is dropped --
    a wildcard block describes defaults, not an account this app could ever
    rewrite a remote to."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    blocks: List[Dict[str, Any]] = []
    aliases: Optional[List[str]] = None
    current: Dict[str, str] = {}

    def _flush() -> None:
        if not aliases:
            return
        for alias in aliases:
            if "*" in alias or "?" in alias:
                continue
            blocks.append({
                "alias": alias,
                "hostname": current.get("hostname") or _DEFAULT_HOSTNAME,
                "identity_file": current.get("identityfile"),
            })

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _HOST_LINE_RE.match(line)
        if m:
            _flush()
            aliases = m.group(1).split()
            current = {}
            continue
        if aliases is None:
            continue  # a directive before any Host block: global default, not ours
        mk = _KV_RE.match(line)
        if not mk:
            continue
        key = mk.group(1).lower()
        value = mk.group(2).strip().strip('"')
        if key == "hostname":
            current["hostname"] = value
        elif key == "identityfile":
            current["identityfile"] = os.path.realpath(os.path.expanduser(value))
    _flush()
    return blocks


def discover_ssh_config_identities(cfg_path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for blk in parse_ssh_config(cfg_path):
        iid = "sshcfg:" + hashlib.sha1(blk["alias"].encode("utf-8", "replace")).hexdigest()[:12]
        out.append({
            "id": iid, "label": blk["alias"], "ssh_host": blk["alias"],
            "hostname": blk["hostname"], "identity_file": blk["identity_file"],
            "git_user_name": None, "git_user_email": None, "source": "ssh_config",
        })
    return out


def discover_loose_key_identities(ssh_dir: str, used_identity_files: Set[str]) -> List[Dict[str, Any]]:
    """``~/.ssh/*.pub`` files whose private key is not already some Host
    block's ``IdentityFile``. No alias -- listed so a human can still see and
    probe the key, but applying it to a repo needs a new ``Host`` block
    first (``POST /api/git/identities`` with ``write_ssh_config``)."""
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(ssh_dir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".pub"):
            continue
        priv_path = os.path.join(ssh_dir, name[:-4])
        if not os.path.isfile(priv_path):
            continue
        real = os.path.realpath(priv_path)
        if real in used_identity_files:
            continue
        iid = "loose:" + hashlib.sha1(real.encode("utf-8", "replace")).hexdigest()[:12]
        out.append({
            "id": iid, "label": name[:-4], "ssh_host": None, "hostname": _DEFAULT_HOSTNAME,
            "identity_file": real, "git_user_name": None, "git_user_email": None,
            "source": "ssh_config",
        })
    return out


def _append_ssh_config_block(cfg_path: str, alias: str, hostname: str, identity_file: str) -> None:
    """Append a new ``Host`` block. Never touches an existing block -- this
    is additive only. Backs up the file first (best effort) so a mistaken
    write is always recoverable by hand."""
    os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
    if os.path.isfile(cfg_path):
        try:
            shutil.copy2(cfg_path, cfg_path + ".bak")
        except OSError:
            logger.warning("git_identities: could not back up %s before appending", cfg_path)
    needs_leading_blank = os.path.isfile(cfg_path) and os.path.getsize(cfg_path) > 0
    block = ("\n" if needs_leading_blank else "") + (
        f"Host {alias}\n"
        f"    HostName {hostname}\n"
        f"    IdentityFile {identity_file}\n"
        f"    User git\n"
    )
    with open(cfg_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(block)
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Manual identities (DATA_DIR/git_identities.json, owner-scoped)
# ---------------------------------------------------------------------------
def _load_manual(owner: Optional[str], path: Optional[str] = None) -> List[Dict[str, Any]]:
    target = path or _identities_file()
    if not os.path.isfile(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        logger.error("git_identities: store %s unreadable: %s", target, e)
        return []
    if not isinstance(raw, dict):
        return []
    bucket = raw.get(_owner_key(owner))
    return list(bucket) if isinstance(bucket, list) else []


def _save_manual(owner: Optional[str], identities: List[Dict[str, Any]], path: Optional[str] = None) -> None:
    target = path or _identities_file()
    with _LOCK:
        all_data: Dict[str, Any] = {}
        if os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    all_data = loaded
            except (OSError, ValueError):
                all_data = {}
        all_data[_owner_key(owner)] = identities
        atomic_write_json(target, all_data, indent=2)


def create_manual_identity(owner: Optional[str], *, label: str, ssh_host: Optional[str] = None,
                            hostname: str = _DEFAULT_HOSTNAME, identity_file: str,
                            git_user_name: Optional[str] = None, git_user_email: Optional[str] = None,
                            write_ssh_config: bool = False, path: Optional[str] = None,
                            ssh_config_path: Optional[str] = None) -> Dict[str, Any]:
    label = (label or "").strip()
    if not label:
        raise GitIdentityError("invalid_label", "label is required")
    identity_file = os.path.realpath(os.path.expanduser((identity_file or "").strip()))
    if not identity_file or not os.path.isfile(identity_file):
        raise GitIdentityError("identity_file_missing", f"identity_file not found: {identity_file!r}")
    ssh_host = (ssh_host or "").strip() or None
    hostname = (hostname or _DEFAULT_HOSTNAME).strip() or _DEFAULT_HOSTNAME
    cfg_path = ssh_config_path or os.path.join(_ssh_dir(), "config")

    if ssh_host and write_ssh_config:
        existing = {b["alias"] for b in parse_ssh_config(cfg_path)}
        if ssh_host not in existing:
            _append_ssh_config_block(cfg_path, ssh_host, hostname, identity_file)

    with _LOCK:
        identities = _load_manual(owner, path)
        record = {
            "id": "id_" + uuid.uuid4().hex[:16], "label": label, "ssh_host": ssh_host,
            "hostname": hostname, "identity_file": identity_file,
            "git_user_name": (git_user_name or "").strip() or None,
            "git_user_email": (git_user_email or "").strip() or None,
            "source": "manual",
        }
        identities.append(record)
        _save_manual(owner, identities, path)
    return record


def delete_manual_identity(owner: Optional[str], identity_id: str, path: Optional[str] = None) -> bool:
    with _LOCK:
        identities = _load_manual(owner, path)
        remaining = [i for i in identities if i.get("id") != identity_id]
        if len(remaining) == len(identities):
            return False
        _save_manual(owner, remaining, path)
    _PROBE_CACHE.pop(identity_id, None)
    return True


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _cached_probe(identity_id: str) -> Optional[Dict[str, Any]]:
    hit = _PROBE_CACHE.get(identity_id)
    if not hit:
        return None
    ts, login, ok, detail = hit
    if time.monotonic() - ts > _PROBE_TTL:
        return None
    return {"github_login": login, "ok": ok, "detail": detail}


def _finalize(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(entry)
    cached = _cached_probe(entry["id"]) if entry.get("id") else None
    out["github_login"] = cached["github_login"] if cached else None
    return out


def _gh_source_identities() -> List[Dict[str, Any]]:
    """The `gh` CLI's own logged-in accounts (Lote 84), as identities:
    ``source: "gh"``, no ``ssh_host`` (there is no ssh alias to rewrite a
    remote to -- ``set_repo_identity`` special-cases this source instead of
    requiring one) and ``github_login`` filled in directly rather than
    through the probe cache, since `gh` already tells us the login. Never
    raises -- `gh` missing or erroring just means no `gh` identities, not a
    broken identities list."""
    from src import git_github  # lazy: avoid an import-time cycle

    try:
        info = git_github.gh_accounts()
    except Exception:  # noqa: BLE001 - a `gh` hiccup must not break /identities
        logger.debug("git_identities: gh_accounts() failed", exc_info=True)
        return []
    if not info.get("available"):
        return []
    out: List[Dict[str, Any]] = []
    for acct in info.get("accounts", []):
        login = acct.get("login")
        if not login:
            continue
        out.append({
            "id": f"gh:{login}", "label": login, "ssh_host": None,
            "hostname": _DEFAULT_HOSTNAME, "identity_file": None,
            "git_user_name": None, "git_user_email": None,
            "github_login": login, "protocol": acct.get("protocol"),
            "active": bool(acct.get("active")), "source": "gh",
        })
    return out


def list_identities(owner: Optional[str], *, manual_path: Optional[str] = None,
                    ssh_config_path: Optional[str] = None) -> Dict[str, Any]:
    """``GET /api/git/identities`` payload. Never probes ssh -- `github_login`
    on a ``ssh_config``/``manual`` entry is whatever is currently cached,
    `null` otherwise; a ``gh`` entry's `github_login` is always known (it IS
    the `gh` account's login), no probe involved."""
    ssh_dir = _ssh_dir()
    cfg_path = ssh_config_path or os.path.join(ssh_dir, "config")
    cfg_identities = discover_ssh_config_identities(cfg_path)
    used_files = {i["identity_file"] for i in cfg_identities if i.get("identity_file")}
    loose = discover_loose_key_identities(ssh_dir, used_files)
    manual = _load_manual(owner, manual_path)
    identities = [_finalize(e) for e in (*cfg_identities, *loose, *manual)] + _gh_source_identities()
    return {"identities": identities, "ssh_config_path": cfg_path, "ssh_dir": ssh_dir}


def find_identity(owner: Optional[str], identity_id: str, **path_overrides: Any) -> Optional[Dict[str, Any]]:
    for ident in list_identities(owner, **path_overrides)["identities"]:
        if ident.get("id") == identity_id:
            return ident
    return None


# ---------------------------------------------------------------------------
# Probe (`ssh -T`) -- the only network call in this module
# ---------------------------------------------------------------------------
_LOGIN_RE = re.compile(r"Hi\s+([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)!\s+You've successfully authenticated")


def _probe_subprocess(argv: List[str], env: Dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    """The one seam that actually shells out to `ssh`. Its own function so
    tests can monkeypatch it instead of mocking `subprocess.run` globally."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 2, env=env)


def probe_identity(identity: Dict[str, Any], *, force: bool = False,
                   timeout: float = _PROBE_TIMEOUT) -> Dict[str, Any]:
    """``{"github_login": str|None, "ok": bool, "detail": str}``. Cached 10
    minutes per identity id unless `force`. Never raises -- a probe that
    can't run (ssh missing, timeout, connection refused) is just `ok: False`
    with the reason in `detail`."""
    iid = identity.get("id")
    if not force and iid:
        cached = _cached_probe(iid)
        if cached is not None:
            return cached

    alias = identity.get("ssh_host")
    target = f"git@{alias}" if alias else f"git@{identity.get('hostname') or _DEFAULT_HOSTNAME}"
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={int(timeout)}"]
    if not alias and identity.get("identity_file"):
        argv += ["-i", identity["identity_file"]]
    argv.append(target)

    try:
        proc = _probe_subprocess(argv, native_host_environment(), timeout)
    except Exception as e:  # noqa: BLE001 - missing ssh binary, timeout, ...
        result = {"github_login": None, "ok": False, "detail": str(e)[:500]}
    else:
        stderr = proc.stderr or ""
        m = _LOGIN_RE.search(stderr)
        if m:
            result = {"github_login": m.group(1), "ok": True, "detail": stderr.strip()[:500]}
        else:
            detail = stderr.strip() or (proc.stdout or "").strip()
            result = {"github_login": None, "ok": False, "detail": detail[:500]}

    if iid:
        _PROBE_CACHE[iid] = (time.monotonic(), result["github_login"], result["ok"], result["detail"])
    return result


# ---------------------------------------------------------------------------
# Remote-URL rewriting and the repo <-> identity relationship
# ---------------------------------------------------------------------------
_HTTPS_URL_RE = re.compile(r"^https?://[^/]+/(?P<path>.+?)(?:\.git)?/?$")
_SSH_SCHEME_URL_RE = re.compile(r"^ssh://(?:[^@/]+@)?[^/]+/(?P<path>.+?)(?:\.git)?/?$")
_SSH_SCP_URL_RE = re.compile(r"^(?:[^@/]+@)?[^:/]+:(?P<path>.+?)(?:\.git)?/?$")
_ALIAS_FROM_SSH_RE = re.compile(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)[:/]")


def rewrite_remote_alias(url: str, alias: str) -> Optional[str]:
    """`url` (ssh scp-like, ``ssh://``, or ``https://``) rewritten to
    ``git@<alias>:<owner>/<repo>.git``, or None if its shape isn't
    recognised (left untouched by the caller in that case)."""
    url = (url or "").strip()
    for rx in (_HTTPS_URL_RE, _SSH_SCHEME_URL_RE, _SSH_SCP_URL_RE):
        m = rx.match(url)
        if m:
            path = m.group("path").strip("/")
            if path:
                return f"git@{alias}:{path}.git"
    return None


def rewrite_remote_https(url: str) -> Optional[str]:
    """`url` rewritten to ``https://github.com/<owner>/<repo>.git`` -- the
    https-protocol counterpart of `rewrite_remote_alias`, used when a `gh`
    identity's account has ``protocol: "https"`` (Lote 84)."""
    url = (url or "").strip()
    for rx in (_HTTPS_URL_RE, _SSH_SCHEME_URL_RE, _SSH_SCP_URL_RE):
        m = rx.match(url)
        if m:
            path = m.group("path").strip("/")
            if path:
                return f"https://github.com/{path}.git"
    return None


def _alias_from_url(url: str) -> Optional[str]:
    url = (url or "").strip()
    if not url or url.startswith(("http://", "https://")):
        return None
    m = _ALIAS_FROM_SSH_RE.match(url)
    return m.group(1) if m else None


def active_identity_for_repo(repo_path: str, owner: Optional[str], *, remote: str = "origin",
                             remotes: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """The identity whose `ssh_host` matches `remote`'s host alias, or None
    (an https remote, or an alias with no matching identity). Never probes
    -- only ever an alias match plus whatever login is already cached.

    `remotes`, when given, is used INSTEAD of a fresh `git remote -v` --
    `routes/git_routes.py` passes the ones `repo_summary` already fetched so
    listing repos never runs `remote -v` twice per repo."""
    from src import git_panel

    if remotes is None:
        remotes = git_panel.repo_remotes(repo_path)
    entry = next((r for r in remotes if r.get("name") == remote), None)
    if not entry:
        return None
    alias = _alias_from_url(entry.get("fetch_url") or entry.get("push_url") or "")
    if not alias:
        return None
    for ident in list_identities(owner)["identities"]:
        if ident.get("ssh_host") == alias:
            return ident
    return None


def _local_git_user(repo_path: str) -> Dict[str, str]:
    from src import git_panel

    name_p = git_panel.run_git(repo_path, "config", "--local", "user.name")
    email_p = git_panel.run_git(repo_path, "config", "--local", "user.email")
    return {
        "name": name_p.stdout.strip() if name_p.returncode == 0 else "",
        "email": email_p.stdout.strip() if email_p.returncode == 0 else "",
    }


def _git_user_with_scope(repo_path: str) -> Dict[str, str]:
    from src import git_panel

    local = _local_git_user(repo_path)
    if local["name"] or local["email"]:
        return {**local, "scope": "local"}
    effective = git_panel.repo_user(repo_path)
    if effective["name"] or effective["email"]:
        return {**effective, "scope": "global"}
    return {"name": "", "email": "", "scope": "none"}


def repo_identity_info(repo_path: str, owner: Optional[str], *, remote: str = "origin") -> Dict[str, Any]:
    """``GET /api/git/repos/{id}/identity`` payload.

    Auto-probes the active identity's `github_login` when it isn't already
    cached (Lote 84), bounded to `_PROBE_TIMEOUT` (8s) so the chip can show
    "(login: X)" the first time this route is hit, without a human clicking
    the separate probe button -- and without ever blocking longer than a
    manual probe already would. A `source: "gh"` identity is skipped: its
    login is already known directly from `gh`, no ssh probe needed."""
    from src import git_panel

    entry = next((r for r in git_panel.repo_remotes(repo_path) if r.get("name") == remote), None)
    remote_url = (entry.get("fetch_url") or entry.get("push_url") or "") if entry else ""
    active = active_identity_for_repo(repo_path, owner, remote=remote)
    if active and active.get("source") != "gh" and active.get("github_login") is None and active.get("id"):
        probed = probe_identity(active)
        active = {**active, "github_login": probed.get("github_login")}
    return {
        "active": active,
        "remote": remote, "remote_url": remote_url,
        "git_user": _git_user_with_scope(repo_path),
    }


def set_repo_identity(repo_path: str, identity: Dict[str, Any], *, remote: str = "origin",
                      set_git_user: bool = True) -> Dict[str, Any]:
    """Rewrite `remote`'s URL to `identity`'s alias and, when `set_git_user`
    and the identity carries a name/email, set them as this repo's LOCAL
    `user.name`/`user.email` (never global). Returns the
    ``PUT /api/git/repos/{id}/identity`` payload (minus ``repo``, which the
    route layer adds -- it needs owner-scoped discovery this module doesn't
    have).

    A ``source: "gh"`` identity (Lote 84) has no ssh alias -- it rewrites the
    remote straight to plain ``github.com`` (ssh) or ``https://github.com``
    (when the `gh` account's own protocol is https), and sets `user.name` to
    the account's login ONLY when the repo doesn't already have one (never
    overwrites an existing name, and never touches `user.email` -- a `gh`
    account carries no email to set)."""
    from src import git_panel

    is_gh = identity.get("source") == "gh"
    alias = identity.get("ssh_host")
    if not alias and not is_gh:
        raise GitIdentityError("no_alias", "This identity has no ssh config alias to rewrite the remote to")
    entry = next((r for r in git_panel.repo_remotes(repo_path) if r.get("name") == remote), None)
    if not entry:
        raise GitIdentityError("no_remote", f"Repo has no remote named {remote!r}")
    before = entry.get("fetch_url") or entry.get("push_url") or ""

    if is_gh and identity.get("protocol") == "https":
        after = rewrite_remote_https(before)
    else:
        after = rewrite_remote_alias(before, alias or _DEFAULT_HOSTNAME)
    if not after:
        raise GitIdentityError("unrecognized_url", f"Could not parse remote url: {before!r}")

    proc = git_panel.run_git(repo_path, "remote", "set-url", remote, after)
    if proc.returncode != 0:
        raise git_panel.GitCommandError(["remote", "set-url", remote, after], proc.returncode, proc.stdout, proc.stderr)

    if set_git_user:
        if identity.get("git_user_name"):
            git_panel.run_git(repo_path, "config", "user.name", identity["git_user_name"])
        elif is_gh and identity.get("github_login") and not git_panel.repo_user(repo_path)["name"]:
            git_panel.run_git(repo_path, "config", "user.name", identity["github_login"])
        if identity.get("git_user_email"):
            git_panel.run_git(repo_path, "config", "user.email", identity["git_user_email"])

    return {
        "remote": remote, "remote_url_before": before, "remote_url_after": after,
        "git_user": _git_user_with_scope(repo_path),
    }
