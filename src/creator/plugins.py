"""src/creator/plugins.py — WP32: one lifecycle for Creator "plugins".

A "plugin" here is any of the four kinds of thing the Creator surface can
add on top of the base install: an engine adapter, an MCP media server, a
production skill, or a ComfyUI recipe. Today those four things are
installed, tracked and rolled back by different, unrelated mechanisms (raw
MCP server config, ``skill_sources``' git pinning, hand-copied recipe
folders). This module does not replace any of those authorities — it is the
ONE gate every plugin, regardless of kind, walks through before it can run:

    discover -> inspect -> verify -> install -> enable/disable -> update ->
    rollback -> uninstall

**Never a parallel authority.** A plugin of kind ``skill`` fetched over git
is placed and pinned by :mod:`src.skill_sources` (ADP-25's own digest,
frontmatter validation and byte-exact rollback) — this module wraps that
call, it does not reimplement it. A plugin of kind ``mcp_media_server`` is
metadata ABOUT an MCP server; this module never starts one — that stays
:mod:`src.mcp_manager`'s job, deliberately out of scope here (see the WP32
report for the exact follow-up hook). Engine adapters and ComfyUI recipes
have no existing authority, so this module is genuinely new ground for
those two kinds only.

**Store.** sqlite under ``DATA_DIR/creator/plugins.db``, the same
open-per-call / WAL / ``BEGIN IMMEDIATE`` discipline as
``src/harness_evolution/store.py`` and ``src/creator/store.py``. Three
tables: ``creator_plugins`` (one row per ``(owner, plugin_id)``, current
projection), ``creator_plugin_sources`` (the discovered catalog — declared,
never fetched), ``creator_plugin_events`` (append-only history, every
mutation, with a ``trace`` string for whatever caused it — a rejected
manifest, a digest mismatch, a revoked license).

**Permissions are a ceiling, never a grant.** ``permission_policy()`` and
``check_use()`` tell a caller (``tool_capabilities``/``preflight``, WP07/
WP09) what an enabled plugin is SCOPED to and whether it may be used at
all right now — they never open, consume or bypass an approval. Every
actual effect still goes through ``approval_store``/``tool_approvals`` per
task, exactly as ADR-14 requires ("context and memory are not
permissions"): a plugin being enabled only narrows what it is ALLOWED to
ask for, it is not itself the yes.

**Owner isolation.** Every row, every install directory and every stored
secret is keyed by owner (CONTRATO.md rule 3) — never resolved from a
request body by this module's callers, and two owners installing the exact
same ``plugin_id`` get entirely separate directories, digests and env.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "PLUGIN_KINDS", "PLUGIN_STATUSES", "LICENSE_REVIEW_STATES", "PERMISSION_KEYS",
    "PluginError", "ManifestInvalid", "VerificationFailed", "TransportError",
    "PluginNotFound", "PluginBlocked", "PluginDisabled",
    "PluginManifest", "VerifyResult", "PluginStore", "get_store",
    "discover", "inspect", "verify", "install", "enable", "disable", "update",
    "rollback", "uninstall", "verify_installed", "get_manifest",
    "permission_policy", "check_use", "set_license_review", "set_env", "get_env",
    "list_plugins", "history", "purge_expired",
]

#: The four kinds of thing this lifecycle covers (WP32 ficha). Closed
#: vocabulary — additive only, never renamed, so a stored ``kind`` value
#: never needs reinterpreting.
PLUGIN_KINDS = ("engine_adapter", "mcp_media_server", "skill", "comfyui_recipe")

#: The lifecycle phase a plugin row is in. ``enabled``/``disabled`` are the
#: two states a genuinely installed plugin toggles between; the boolean
#: ``enabled`` column is the one thing every caller actually needs to check
#: on each use, this column is for humans reading the record.
PLUGIN_STATUSES = ("discovered", "installed", "enabled", "disabled", "uninstalled")

#: A plugin's license is a REVIEW STATE, not a fact the package gets to
#: assert about itself (docs/09_SEGURIDAD_LICENCIAS_Y_MIGRACION.md). Every
#: install/update forces this back to ``unreviewed`` regardless of what the
#: package's own manifest claims — only :func:`set_license_review` (an
#: explicit, separate call) may move it.
LICENSE_REVIEW_STATES = ("unreviewed", "reviewed_ok", "reviewed_blocked")

#: The four permission buckets a plugin manifest declares against (WP32
#: ficha: "permisos requeridos (red/fs/gpu/coste)").
PERMISSION_KEYS = ("network", "fs", "gpu", "cost")

_BUSY_TIMEOUT_S = 30

#: Frontmatter keys that ask to weaken the approval system itself — the same
#: closed set ``src/skill_import_review.py`` refuses at any digest. Read-only
#: import of that module's vocabulary (never edited here) so a plugin of
#: kind ``skill`` is held to the same bar whether it arrives through ADP-25's
#: existing review path or through this one.
def _privilege_request_keys() -> frozenset:
    from src.skill_import_review import PRIVILEGE_REQUEST_KEYS
    return PRIVILEGE_REQUEST_KEYS

#: Free-text "requires" tags (declared by a package's own plugin.json, or
#: inferred from a skill's SKILL.md ``permissions.backends``) mapped to the
#: permission bucket they imply. Closed, additive vocabulary — a tag not
#: recognized here implies nothing (fails open toward "no undeclared
#: permission", not toward "everything requires review"); the honest floor
#: is what's actually named.
_BACKEND_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "network": ("network", "http", "web", "fetch", "url", "download", "api", "webhook"),
    "fs": ("fs", "file", "filesystem", "disk", "local_files"),
    "gpu": ("gpu", "cuda", "vram"),
    "cost": ("paid", "billed", "cost", "metered", "quota"),
}


def _implied_buckets(tags: Sequence[str]) -> Set[str]:
    buckets: Set[str] = set()
    for tag in tags:
        low = str(tag or "").strip().lower()
        if not low:
            continue
        for bucket, keywords in _BACKEND_KEYWORDS.items():
            if any(kw in low for kw in keywords):
                buckets.add(bucket)
    return buckets


# ── errors ───────────────────────────────────────────────────────────────

class PluginError(Exception):
    """Base for every error this module raises on purpose (a caller
    mistake, a failed verification, a blocked license) — never used for an
    unexpected crash, which propagates as whatever it actually is."""


class ManifestInvalid(PluginError):
    """The package's own ``plugin.json`` (or, for a script-less skill, its
    ``SKILL.md`` frontmatter) does not parse into a usable manifest."""


class VerificationFailed(PluginError):
    """``verify()`` refused the package. ``undeclared`` names the permission
    buckets the package appears to need but did not declare — the exact
    signal EXT01's acceptance bar asks for ("pegar una URL desconocida no
    ejecuta su instalador": this is what makes that refusal legible)."""

    def __init__(self, reason: str, undeclared: Optional[Sequence[str]] = None):
        super().__init__(reason)
        self.reason = reason
        self.undeclared = list(undeclared or [])


class TransportError(PluginError):
    """Fetching the package failed (git/network/local path). Raised BEFORE
    anything is persisted — the ficha's "un fallo de transporte no reintenta
    mutación" is enforced simply by every mutating call fetching first and
    writing nothing until the fetch and verify both succeeded."""


class PluginNotFound(PluginError):
    """Unknown ``(owner, plugin_id)`` — indistinguishable from "exists but
    is not yours" per CONTRATO.md rule 3."""


class PluginBlocked(PluginError):
    """``enable()`` refused: the plugin's license review state is
    ``reviewed_blocked``."""


class PluginDisabled(PluginError):
    """``check_use()`` refused: the plugin is not currently enabled (never
    enabled, explicitly disabled, or auto-disabled by a digest mismatch)."""


# ── manifest shape ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class PluginManifest:
    id: str
    version: str
    digest: str
    kind: str
    capabilities: Tuple[str, ...] = ()
    permissions: Mapping[str, bool] = field(default_factory=dict)
    license_name: str = ""
    #: Always ``"unreviewed"`` coming out of :func:`inspect` — a package
    #: never gets to assert its own review outcome (see module docstring).
    license_review_status: str = "unreviewed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "version": self.version, "digest": self.digest,
            "kind": self.kind, "capabilities": list(self.capabilities),
            "permissions": {k: bool(self.permissions.get(k, False)) for k in PERMISSION_KEYS},
            "license_name": self.license_name,
            "license_review_status": self.license_review_status,
        }


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str = ""
    undeclared: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "undeclared": list(self.undeclared)}


# ── filesystem helpers ──────────────────────────────────────────────────

def _make_writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _rmtree_force(path: Any) -> None:
    """Resilient recursive delete (mirrors ``skill_sources._rmtree_force``):
    clears read-only bits before removing, no-ops on a missing path, works
    the same on Windows and POSIX. Deliberately duplicated rather than
    imported — a private helper of another module is not an interface this
    lot owns."""
    path = str(path)
    if not os.path.lexists(path):
        return
    for root, dirs, files in os.walk(path):
        for name in dirs:
            _make_writable(os.path.join(root, name))
        for name in files:
            _make_writable(os.path.join(root, name))
    _make_writable(path)
    shutil.rmtree(path)


def _copy_tree_contents(src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        d = os.path.join(dst, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def _replace_dir_contents(target: Path, source: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in list(target.iterdir()):
        if entry.is_dir():
            _rmtree_force(entry)
        else:
            entry.unlink()
    for entry in source.iterdir():
        dest = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest)
        else:
            shutil.copy2(entry, dest)


def _digest_of_dir(path: str) -> str:
    """sha256 over every regular file under ``path``, keyed by relative
    path — the generic digest for engine_adapter/mcp_media_server/
    comfyui_recipe plugins (kind ``skill`` uses ADP-25's own
    ``skill_digest`` instead, via :func:`_digest_of_skill`, so a skill
    plugin's digest agrees with the rest of the skill system on what
    "unchanged" means)."""
    h = hashlib.sha256()
    root = Path(path)
    rels = sorted(
        str(p.relative_to(root)).replace(os.sep, "/")
        for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )
    for rel in rels:
        data = (root / rel).read_bytes()
        h.update(rel.encode("utf-8")); h.update(b"\0")
        h.update(str(len(data)).encode("ascii")); h.update(b"\0")
        h.update(data); h.update(b"\x1e")
    return h.hexdigest()


def _digest_of_skill(folder: str) -> str:
    from src.skills_runtime.discovery import DiscoveredSkill, skill_digest
    found = DiscoveredSkill(name="", path=str(Path(folder) / "SKILL.md"),
                            origin="", root=str(folder), distance=0)
    return skill_digest(found)


def _digest_for(kind: str, folder: str) -> str:
    if kind == "skill" and (Path(folder) / "SKILL.md").is_file():
        return _digest_of_skill(folder)
    return _digest_of_dir(folder)


# ── git transport (offline-safe: works against a local file:// or path
#    remote exactly as it does against a real GitHub URL — no code here
#    ever talks to a network host that a test did not itself point it at) ─

_GIT_TIMEOUT_S = 60


def _run_git(args: List[str], cwd: Optional[str] = None) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, timeout=_GIT_TIMEOUT_S)
    except FileNotFoundError as e:
        raise TransportError("git is not available on this host") from e
    except subprocess.TimeoutExpired as e:
        raise TransportError(f"git {' '.join(args)} timed out") from e
    if result.returncode != 0:
        raise TransportError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout.strip()


def _fetch_git_ref(source_url: str, ref: str, dest_dir: str) -> str:
    with tempfile.TemporaryDirectory(prefix="faustus-plugin-clone-",
                                     ignore_cleanup_errors=True) as clone_dir:
        _run_git(["clone", "--quiet", "--no-single-branch", source_url, clone_dir])
        _run_git(["checkout", "--quiet", ref], cwd=clone_dir)
        sha = _run_git(["rev-parse", "HEAD"], cwd=clone_dir)
        _rmtree_force(os.path.join(clone_dir, ".git"))
        _copy_tree_contents(clone_dir, dest_dir)
    return sha


# ── package fetch (discover never calls this — see discover() below) ────

@dataclass
class _Fetched:
    staging_dir: str
    revision: str

    def cleanup(self) -> None:
        _rmtree_force(self.staging_dir)


def _fetch_package(source: Mapping[str, Any]) -> _Fetched:
    """Materialize a declared source into a fresh staging directory. This is
    the ONLY place this module talks to git or the filesystem outside
    ``DATA_DIR`` — ``discover()`` never calls it (declared, never fetched),
    and every caller of this function fetches into staging BEFORE writing
    anything to the store, so a transport failure here leaves no partial
    state to clean up or accidentally retry as a mutation."""
    transport = str(source.get("transport") or "").strip()
    staging = tempfile.mkdtemp(prefix="faustus-plugin-stage-")
    try:
        if transport == "local_dir":
            path = source.get("path")
            if not path or not os.path.isdir(path):
                raise TransportError(f"local_dir source path does not exist: {path!r}")
            _copy_tree_contents(path, staging)
            return _Fetched(staging, "local")
        if transport == "git":
            source_url = str(source.get("source_url") or "")
            ref = str(source.get("ref") or "HEAD")
            if not source_url:
                raise TransportError("git source requires source_url")
            sha = _fetch_git_ref(source_url, ref, staging)
            return _Fetched(staging, sha)
        raise ManifestInvalid(f"unsupported source transport: {transport!r}")
    except Exception:
        _rmtree_force(staging)
        raise


# ── manifest / requires extraction ───────────────────────────────────────

def _read_plugin_json(staging_dir: str) -> Optional[Dict[str, Any]]:
    p = Path(staging_dir) / "plugin.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — reported as a manifest problem, not a crash
        raise ManifestInvalid(f"plugin.json is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ManifestInvalid("plugin.json must be a JSON object")
    return data


def _skill_frontmatter(staging_dir: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """Best-effort ``(frontmatter, error)`` for a kind=='skill' package
    without its own plugin.json — read-only, never raises (a skill with no
    SKILL.md or unparsable frontmatter just yields no declared backends,
    which ``verify`` then treats as "nothing implied" rather than a crash;
    ``install`` separately requires SKILL.md to exist for a real skill
    install via ``skill_sources``)."""
    skill_md = Path(staging_dir) / "SKILL.md"
    if not skill_md.is_file():
        return {}, "no SKILL.md"
    try:
        from services.memory.skill_format import parse_frontmatter
        fm, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        return (fm if isinstance(fm, dict) else {}), None
    except Exception as e:  # noqa: BLE001
        return {}, str(e)


def _build_manifest(plugin_id_hint: str, kind_hint: str, staging_dir: str,
                    revision: str) -> Tuple[PluginManifest, Set[str], List[str]]:
    """Returns ``(manifest, requires, privilege_hits)`` — ``requires`` is the
    set of permission buckets the package appears to need (from its own
    declared ``requires`` tags, or a skill's SKILL.md backends);
    ``privilege_hits`` are any ``PRIVILEGE_REQUEST_KEYS`` a skill's
    frontmatter asked for, which :func:`verify` refuses outright."""
    declared = _read_plugin_json(staging_dir)
    privilege_hits: List[str] = []
    requires_tags: List[str] = []

    if declared is not None:
        plugin_id = str(declared.get("id") or plugin_id_hint or "").strip()
        kind = str(declared.get("kind") or kind_hint or "").strip()
        version = str(declared.get("version") or revision[:12] or "0").strip()
        capabilities = tuple(str(c) for c in (declared.get("capabilities") or []))
        raw_perms = declared.get("permissions") or {}
        permissions = {k: bool(raw_perms.get(k)) for k in PERMISSION_KEYS}
        license_name = str(declared.get("license") or declared.get("license_name") or "")
        requires_tags = [str(t) for t in (declared.get("requires") or [])]
    else:
        plugin_id = plugin_id_hint or ""
        kind = kind_hint or "skill"
        version = revision[:12] if revision and revision != "local" else "0"
        capabilities = ()
        permissions = {k: False for k in PERMISSION_KEYS}
        license_name = ""

    if kind == "skill":
        fm, _err = _skill_frontmatter(staging_dir)
        if isinstance(fm, dict):
            # Reuse skills_runtime.bridge's own flat-key reader (rule 4: no
            # parallel reinterpretation of a SKILL.md's frontmatter shape —
            # this parser has no nested maps, see that module's docstring).
            from src.skills_runtime.bridge import permissions_from_frontmatter
            perms_block = permissions_from_frontmatter(fm)
        else:
            perms_block = {}
        backends = list(perms_block.get("backends") or [])
        requires_tags = requires_tags + [str(b) for b in backends]
        if str(perms_block.get("network")).strip().lower() in ("true", "1", "yes"):
            requires_tags.append("network")
        if not capabilities and isinstance(fm, dict) and fm.get("description"):
            capabilities = (str(fm.get("description"))[:200],)
        if isinstance(fm, dict):
            privilege_hits = sorted(k for k in fm if k in _privilege_request_keys())

    if not plugin_id:
        raise ManifestInvalid("plugin manifest has no 'id' (declare one in plugin.json)")
    if kind not in PLUGIN_KINDS:
        raise ManifestInvalid(f"unknown plugin kind: {kind!r}")

    digest = _digest_for(kind, staging_dir)
    manifest = PluginManifest(
        id=plugin_id, version=version, digest=digest, kind=kind,
        capabilities=capabilities, permissions=permissions,
        license_name=license_name, license_review_status="unreviewed",
    )
    requires = _implied_buckets(requires_tags)
    return manifest, requires, privilege_hits


def inspect(source: Mapping[str, Any]) -> PluginManifest:
    """Fetch ``source`` into a throwaway staging directory, build its
    manifest, and discard the staging copy — a pure read of what a package
    claims to be, never executed, never persisted."""
    fetched = _fetch_package(source)
    try:
        manifest, _requires, _hits = _build_manifest(
            str(source.get("id") or ""), str(source.get("kind") or ""),
            fetched.staging_dir, fetched.revision,
        )
        return manifest
    finally:
        fetched.cleanup()


def verify(manifest: PluginManifest, requires: Set[str],
          privilege_hits: Optional[Sequence[str]] = None) -> VerifyResult:
    """Pure check — no filesystem, no store. Raises :class:`VerificationFailed`
    on any of: an invalid manifest shape, a privileged frontmatter request
    (skills only), or a permission the package appears to need but did not
    declare in ``manifest.permissions``."""
    if not manifest.id or not manifest.version or not manifest.digest:
        raise VerificationFailed("manifest is missing id/version/digest")
    if manifest.kind not in PLUGIN_KINDS:
        raise VerificationFailed(f"unknown plugin kind: {manifest.kind!r}")
    bad_keys = set(manifest.permissions.keys()) - set(PERMISSION_KEYS)
    if bad_keys:
        raise VerificationFailed(f"manifest declares unknown permission keys: {sorted(bad_keys)}")

    if privilege_hits:
        raise VerificationFailed(
            f"skill frontmatter requests privileged keys: {sorted(privilege_hits)}")

    undeclared = sorted(b for b in requires if not manifest.permissions.get(b))
    if undeclared:
        raise VerificationFailed(
            f"package requires undeclared permission(s): {undeclared}", undeclared=undeclared)

    return VerifyResult(ok=True)


# ── store ─────────────────────────────────────────────────────────────

def _default_db_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "plugins.db")


def _owner_slug(owner: str) -> str:
    return hashlib.sha256((owner or "").encode("utf-8")).hexdigest()[:24]


def _plugin_slug(plugin_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in plugin_id)[:80]
    return f"{safe}.{hashlib.sha256(plugin_id.encode('utf-8')).hexdigest()[:12]}"


class PluginStore:
    def __init__(self, db_path: Optional[str] = None,
                install_root: Optional[str] = None) -> None:
        self.db_path = db_path or _default_db_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        if install_root is None:
            from src.constants import DATA_DIR
            install_root = os.path.join(DATA_DIR, "creator", "plugins")
        self.install_root = install_root
        self._init_lock = threading.Lock()
        self._ensure_schema()

    def install_dir_for(self, owner: str, plugin_id: str) -> str:
        return os.path.join(self.install_root, _owner_slug(owner), _plugin_slug(plugin_id), "current")

    def backup_dir_for(self, owner: str, plugin_id: str) -> str:
        return os.path.join(self.install_root, _owner_slug(owner), _plugin_slug(plugin_id), "previous")

    @contextmanager
    def _conn(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if immediate:
                conn.execute("COMMIT")
        except Exception:
            if immediate:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS creator_plugins (
                    owner TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    version TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    license_name TEXT NOT NULL,
                    license_review_status TEXT NOT NULL,
                    reviewer TEXT,
                    review_note TEXT,
                    status TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    install_dir TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    previous_version TEXT,
                    previous_digest TEXT,
                    previous_backup_dir TEXT,
                    previous_license_review_status TEXT,
                    env_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    uninstalled_at REAL,
                    purge_after REAL,
                    PRIMARY KEY (owner, plugin_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS creator_plugin_sources (
                    owner TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    discovered_at REAL NOT NULL,
                    PRIMARY KEY (owner, source_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS creator_plugin_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    trace TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_plugin_events_plugin "
                "ON creator_plugin_events(owner, plugin_id, created_at)")

    # ── discovered catalog ───────────────────────────────────────────

    def record_discovered(self, owner: str, source: Mapping[str, Any]) -> Dict[str, Any]:
        source_id = str(source.get("id") or "")
        kind = str(source.get("kind") or "")
        if not source_id:
            raise ManifestInvalid("discovered source needs an 'id'")
        if kind not in PLUGIN_KINDS:
            raise ManifestInvalid(f"unknown plugin kind: {kind!r}")
        now = time.time()
        payload = json.dumps(dict(source))
        with self._conn(immediate=True) as conn:
            conn.execute(
                "INSERT INTO creator_plugin_sources (owner, source_id, kind, payload_json, discovered_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner, source_id) DO UPDATE SET "
                "kind=excluded.kind, payload_json=excluded.payload_json, discovered_at=excluded.discovered_at",
                (owner or "", source_id, kind, payload, now),
            )
        return {"id": source_id, "kind": kind, "source": dict(source), "discovered_at": now}

    def list_discovered(self, owner: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM creator_plugin_sources WHERE owner = ? ORDER BY discovered_at ASC",
                (owner or "",)).fetchall()
        return [{"id": r["source_id"], "kind": r["kind"],
                "source": json.loads(r["payload_json"]), "discovered_at": r["discovered_at"]}
               for r in rows]

    # ── plugins projection ───────────────────────────────────────────

    def get(self, owner: str, plugin_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM creator_plugins WHERE owner = ? AND plugin_id = ?",
                (owner or "", plugin_id)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_plugins(self, owner: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM creator_plugins WHERE owner = ? ORDER BY updated_at DESC",
                (owner or "",)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def upsert(self, owner: str, plugin_id: str, *, kind: str, version: str, digest: str,
              capabilities: Sequence[str], permissions: Mapping[str, bool],
              license_name: str, install_dir: str, source: Mapping[str, Any],
              status: str, enabled: bool,
              previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        now = time.time()
        existing = self.get(owner, plugin_id)
        created_at = existing["created_at"] if existing else now
        prev = previous or {}
        with self._conn(immediate=True) as conn:
            conn.execute(
                "INSERT INTO creator_plugins (owner, plugin_id, kind, version, digest, "
                " capabilities_json, permissions_json, license_name, license_review_status, "
                " reviewer, review_note, status, enabled, install_dir, source_json, "
                " previous_version, previous_digest, previous_backup_dir, "
                " previous_license_review_status, env_json, created_at, updated_at, "
                " uninstalled_at, purge_after) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner, plugin_id) DO UPDATE SET "
                "kind=excluded.kind, version=excluded.version, digest=excluded.digest, "
                "capabilities_json=excluded.capabilities_json, "
                "permissions_json=excluded.permissions_json, "
                "license_name=excluded.license_name, "
                "license_review_status=excluded.license_review_status, "
                "status=excluded.status, enabled=excluded.enabled, "
                "install_dir=excluded.install_dir, source_json=excluded.source_json, "
                "previous_version=excluded.previous_version, "
                "previous_digest=excluded.previous_digest, "
                "previous_backup_dir=excluded.previous_backup_dir, "
                "previous_license_review_status=excluded.previous_license_review_status, "
                "updated_at=excluded.updated_at, "
                "uninstalled_at=excluded.uninstalled_at, purge_after=excluded.purge_after",
                (owner or "", plugin_id, kind, version, digest,
                 json.dumps(list(capabilities)), json.dumps({k: bool(permissions.get(k)) for k in PERMISSION_KEYS}),
                 license_name, "unreviewed", None, None, status, int(bool(enabled)),
                 install_dir, json.dumps(dict(source)),
                 prev.get("version"), prev.get("digest"), prev.get("backup_dir"),
                 prev.get("license_review_status"), "{}", created_at, now, None, None),
            )
        return self.get(owner, plugin_id)

    def set_enabled(self, owner: str, plugin_id: str, enabled: bool, status: str) -> Dict[str, Any]:
        now = time.time()
        with self._conn(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE creator_plugins SET enabled = ?, status = ?, updated_at = ? "
                "WHERE owner = ? AND plugin_id = ?",
                (int(bool(enabled)), status, now, owner or "", plugin_id))
            if cur.rowcount != 1:
                raise PluginNotFound(plugin_id)
        return self.get(owner, plugin_id)

    def set_license_review(self, owner: str, plugin_id: str, review_status: str,
                           reviewer: str, note: str) -> Dict[str, Any]:
        if review_status not in LICENSE_REVIEW_STATES:
            raise ManifestInvalid(f"unknown license review status: {review_status!r}")
        now = time.time()
        with self._conn(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE creator_plugins SET license_review_status = ?, reviewer = ?, "
                "review_note = ?, updated_at = ? WHERE owner = ? AND plugin_id = ?",
                (review_status, reviewer, note, now, owner or "", plugin_id))
            if cur.rowcount != 1:
                raise PluginNotFound(plugin_id)
        return self.get(owner, plugin_id)

    def set_env(self, owner: str, plugin_id: str, env: Mapping[str, str]) -> None:
        """Owner-scoped secret values for this plugin. Stored in its OWN
        column, never emitted into an event payload (rule 2: "payloads
        sensibles separados de ids/tipos") — :meth:`append_event` never
        receives this dict."""
        now = time.time()
        with self._conn(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE creator_plugins SET env_json = ?, updated_at = ? "
                "WHERE owner = ? AND plugin_id = ?",
                (json.dumps({str(k): str(v) for k, v in dict(env).items()}), now, owner or "", plugin_id))
            if cur.rowcount != 1:
                raise PluginNotFound(plugin_id)

    def get_env(self, owner: str, plugin_id: str) -> Dict[str, str]:
        row = self.get(owner, plugin_id)
        if row is None:
            raise PluginNotFound(plugin_id)
        return dict(row.get("env") or {})

    def mark_uninstalled(self, owner: str, plugin_id: str, purge_after: float) -> Dict[str, Any]:
        now = time.time()
        with self._conn(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE creator_plugins SET status = 'uninstalled', enabled = 0, "
                "uninstalled_at = ?, purge_after = ?, updated_at = ? "
                "WHERE owner = ? AND plugin_id = ?",
                (now, purge_after, now, owner or "", plugin_id))
            if cur.rowcount != 1:
                raise PluginNotFound(plugin_id)
        return self.get(owner, plugin_id)

    def delete(self, owner: str, plugin_id: str) -> None:
        with self._conn(immediate=True) as conn:
            conn.execute("DELETE FROM creator_plugins WHERE owner = ? AND plugin_id = ?",
                        (owner or "", plugin_id))

    def list_purge_candidates(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = time.time() if now is None else now
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM creator_plugins WHERE status = 'uninstalled' "
                "AND purge_after IS NOT NULL AND purge_after <= ?", (now,)).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── history (append-only) ────────────────────────────────────────

    def append_event(self, owner: str, plugin_id: str, event_type: str,
                     payload: Mapping[str, Any], trace: str = "") -> None:
        with self._conn(immediate=True) as conn:
            conn.execute(
                "INSERT INTO creator_plugin_events (owner, plugin_id, event_type, payload_json, "
                "trace, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (owner or "", plugin_id, event_type, json.dumps(dict(payload)), trace, time.time()))

    def history(self, owner: str, plugin_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_type, payload_json, trace, created_at FROM creator_plugin_events "
                "WHERE owner = ? AND plugin_id = ? ORDER BY event_id ASC",
                (owner or "", plugin_id)).fetchall()
        return [{"event_type": r["event_type"], "payload": json.loads(r["payload_json"]),
                "trace": r["trace"], "created_at": r["created_at"]} for r in rows]


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "owner": row["owner"], "id": row["plugin_id"], "kind": row["kind"],
        "version": row["version"], "digest": row["digest"],
        "capabilities": json.loads(row["capabilities_json"]),
        "permissions": json.loads(row["permissions_json"]),
        "license_name": row["license_name"],
        "license_review_status": row["license_review_status"],
        "reviewer": row["reviewer"], "review_note": row["review_note"],
        "status": row["status"], "enabled": bool(row["enabled"]),
        "install_dir": row["install_dir"], "source": json.loads(row["source_json"]),
        "previous_version": row["previous_version"], "previous_digest": row["previous_digest"],
        "previous_backup_dir": row["previous_backup_dir"],
        "previous_license_review_status": row["previous_license_review_status"],
        "env": json.loads(row["env_json"] or "{}"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "uninstalled_at": row["uninstalled_at"], "purge_after": row["purge_after"],
    }


_store: Optional[PluginStore] = None
_store_lock = threading.Lock()


def get_store() -> PluginStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PluginStore()
    return _store


# ── lifecycle: discover ──────────────────────────────────────────────

def discover(owner: str, sources: Sequence[Mapping[str, Any]], *,
            store: Optional[PluginStore] = None) -> List[Dict[str, Any]]:
    """Record declared sources into the catalog. NEVER fetches, NEVER
    executes — the acceptance bar this closes directly (EXT01: "pegar una
    URL GitHub desconocida no ejecuta automáticamente su script de
    instalación"). Each ``source`` needs at least ``id`` and ``kind``; the
    rest (``transport``, ``path``/``source_url``+``ref``) is opaque to this
    function and only interpreted later, by :func:`inspect`/:func:`install`."""
    store = store or get_store()
    return [store.record_discovered(owner, s) for s in sources]


# ── lifecycle: install / update (shared machinery) ──────────────────

def _stage_and_verify(plugin_id_hint: str, kind_hint: str,
                      source: Mapping[str, Any]) -> Tuple[PluginManifest, _Fetched]:
    fetched = _fetch_package(source)
    try:
        manifest, requires, hits = _build_manifest(
            plugin_id_hint, kind_hint, fetched.staging_dir, fetched.revision)
        verify(manifest, requires, hits)  # raises VerificationFailed
        return manifest, fetched
    except Exception:
        fetched.cleanup()
        raise


def install(owner: str, source: Mapping[str, Any], *,
           store: Optional[PluginStore] = None) -> Dict[str, Any]:
    """``discover``'s catalog entry (if any) is informational only — install
    always re-fetches and re-verifies from ``source`` itself, so an install
    call is never trusting a stale discovered record.

    Order, exactly as the ficha states it: staging -> verification ->
    registro. Nothing is written to the store until :func:`verify` has
    already passed against the STAGED content."""
    store = store or get_store()
    plugin_id_hint = str(source.get("id") or "")
    kind_hint = str(source.get("kind") or "")
    manifest, fetched = _stage_and_verify(plugin_id_hint, kind_hint, source)
    try:
        install_dir = store.install_dir_for(owner, manifest.id)
        if os.path.isdir(install_dir) and os.listdir(install_dir):
            raise PluginError(
                f"plugin {manifest.id!r} is already installed for this owner; use update()")

        if manifest.kind == "skill" and str(source.get("transport")) == "git":
            # Reuse skill_sources for the actual placement/pinning of a git
            # skill (rule: "no crear autoridades paralelas") — its own
            # ADP-25 digest becomes this plugin's digest of record, so the
            # two never disagree about what "unchanged" means.
            from src.skill_sources import install as skill_install, SkillSourceError
            os.makedirs(os.path.dirname(install_dir) or ".", exist_ok=True)
            try:
                rec = skill_install(install_dir, str(source.get("source_url")),
                                    str(source.get("ref") or "HEAD"),
                                    skill_key=_skill_key(owner, manifest.id))
            except SkillSourceError as e:
                raise TransportError(str(e)) from e
            digest = rec["pinned_digest"]
            version = manifest.version if manifest.version != "0" else rec["pinned_revision"][:12]
        else:
            Path(install_dir).mkdir(parents=True, exist_ok=True)
            _replace_dir_contents(Path(install_dir), Path(fetched.staging_dir))
            digest = manifest.digest
            version = manifest.version

        record = store.upsert(
            owner, manifest.id, kind=manifest.kind, version=version, digest=digest,
            capabilities=manifest.capabilities, permissions=manifest.permissions,
            license_name=manifest.license_name, install_dir=install_dir, source=source,
            status="installed", enabled=False,
        )
        store.append_event(owner, manifest.id, "installed",
                           {"version": version, "digest": digest, "kind": manifest.kind},
                           trace=f"installed from {source.get('transport')}")
        return record
    finally:
        fetched.cleanup()


def _skill_key(owner: str, plugin_id: str) -> str:
    return f"creator_plugin:{owner}:{plugin_id}"


def update(owner: str, plugin_id: str, source: Mapping[str, Any], *,
          store: Optional[PluginStore] = None) -> Dict[str, Any]:
    """Same gate as :func:`install` (stage -> verify) applied to an already
    installed plugin, with the previous version backed up first so
    :func:`rollback` has something byte-exact to restore. The license
    review state is ALWAYS reset to ``unreviewed`` on a successful update
    (new bytes, new review) and the plugin is left disabled — new content
    is never silently re-enabled."""
    store = store or get_store()
    existing = store.get(owner, plugin_id)
    if existing is None:
        raise PluginNotFound(plugin_id)

    manifest, fetched = _stage_and_verify(plugin_id, existing["kind"], source)
    try:
        if manifest.id != plugin_id:
            raise ManifestInvalid(
                f"fetched package declares id {manifest.id!r}, expected {plugin_id!r}")

        install_dir = existing["install_dir"]
        backup_dir = store.backup_dir_for(owner, plugin_id)
        if os.path.isdir(backup_dir):
            _rmtree_force(backup_dir)
        if os.path.isdir(install_dir):
            os.makedirs(os.path.dirname(backup_dir) or ".", exist_ok=True)
            shutil.copytree(install_dir, backup_dir)
        else:
            backup_dir = ""

        if manifest.kind == "skill" and str(source.get("transport")) == "git":
            from src.skill_sources import update as skill_update, get_source as skill_get_source
            if skill_get_source(_skill_key(owner, plugin_id)) is None:
                # This install predates skill_sources tracking (or was a
                # local_dir install) — fall back to the generic swap so
                # update() still works uniformly.
                _replace_dir_contents(Path(install_dir), Path(fetched.staging_dir))
                digest = manifest.digest
                version = manifest.version
            else:
                result = skill_update(_skill_key(owner, plugin_id), verify=True)
                if result.get("status") == "failed":
                    raise VerificationFailed(f"skill source update failed: {result.get('reason')}")
                if result.get("status") == "unchanged":
                    digest = existing["digest"]
                    version = existing["version"]
                else:
                    from src.skill_sources import get_source as skill_get_source2
                    rec = skill_get_source2(_skill_key(owner, plugin_id))
                    digest = rec["pinned_digest"]
                    version = manifest.version if manifest.version != "0" else rec["pinned_revision"][:12]
        else:
            _replace_dir_contents(Path(install_dir), Path(fetched.staging_dir))
            digest = manifest.digest
            version = manifest.version

        record = store.upsert(
            owner, plugin_id, kind=manifest.kind, version=version, digest=digest,
            capabilities=manifest.capabilities, permissions=manifest.permissions,
            license_name=manifest.license_name, install_dir=install_dir, source=source,
            status="disabled", enabled=False,
            previous={"version": existing["version"], "digest": existing["digest"],
                     "backup_dir": backup_dir or None,
                     "license_review_status": existing["license_review_status"]},
        )
        store.append_event(owner, plugin_id, "updated",
                           {"from_version": existing["version"], "to_version": version, "digest": digest},
                           trace="update staged, verified and promoted; license review reset")
        return record
    finally:
        fetched.cleanup()


def rollback(owner: str, plugin_id: str, *, store: Optional[PluginStore] = None) -> Dict[str, Any]:
    """Restore the previous version byte-for-byte, verifying the backup's
    digest before AND after the restore — never trusting the stored copy or
    the write itself (same discipline as ``skill_sources.rollback``)."""
    store = store or get_store()
    existing = store.get(owner, plugin_id)
    if existing is None:
        raise PluginNotFound(plugin_id)
    if not existing.get("previous_backup_dir") or not existing.get("previous_digest"):
        raise PluginError(f"plugin {plugin_id!r} has no previous version to roll back to")

    backup_dir = Path(existing["previous_backup_dir"])
    if not backup_dir.is_dir():
        raise PluginError(f"backup for {plugin_id!r} is missing on disk")

    before_digest = _digest_for(existing["kind"], str(backup_dir))
    if before_digest != existing["previous_digest"]:
        raise PluginError(
            f"backup for {plugin_id!r} no longer matches its recorded digest; refusing to restore")

    install_dir = Path(existing["install_dir"])
    _replace_dir_contents(install_dir, backup_dir)
    after_digest = _digest_for(existing["kind"], str(install_dir))
    if after_digest != existing["previous_digest"]:
        raise PluginError(
            f"restore of {plugin_id!r} did not reproduce the recorded digest "
            f"(expected {existing['previous_digest']}, got {after_digest})")

    record = store.upsert(
        owner, plugin_id, kind=existing["kind"], version=existing["previous_version"],
        digest=existing["previous_digest"], capabilities=existing["capabilities"],
        permissions=existing["permissions"], license_name=existing["license_name"],
        install_dir=existing["install_dir"], source=existing["source"],
        status="disabled", enabled=False, previous={},
    )
    store.set_license_review(
        owner, plugin_id, existing.get("previous_license_review_status") or "unreviewed",
        reviewer="", note="restored by rollback")
    store.append_event(owner, plugin_id, "rolled_back",
                       {"to_version": existing["previous_version"], "digest": existing["previous_digest"]},
                       trace="byte-exact restore verified before and after")
    return store.get(owner, plugin_id) or record


# ── lifecycle: enable / disable / license review ────────────────────

def enable(owner: str, plugin_id: str, *, store: Optional[PluginStore] = None) -> Dict[str, Any]:
    """Flip a plugin on. Deliberately does NOT touch ``approval_store`` or
    any other approval mechanism — enabling only widens what the plugin is
    SCOPED to ask for later (:func:`permission_policy`); every actual effect
    still needs its own per-task approval (ADR-14)."""
    store = store or get_store()
    record = store.get(owner, plugin_id)
    if record is None:
        raise PluginNotFound(plugin_id)
    if record["license_review_status"] == "reviewed_blocked":
        raise PluginBlocked(f"plugin {plugin_id!r} license is reviewed_blocked; cannot enable")
    updated = store.set_enabled(owner, plugin_id, True, "enabled")
    store.append_event(owner, plugin_id, "enabled", {}, trace="")
    return updated


def disable(owner: str, plugin_id: str, *, store: Optional[PluginStore] = None) -> Dict[str, Any]:
    store = store or get_store()
    updated = store.set_enabled(owner, plugin_id, False, "disabled")
    store.append_event(owner, plugin_id, "disabled", {}, trace="")
    return updated


def set_license_review(owner: str, plugin_id: str, status: str, *, reviewer: str = "",
                       note: str = "", store: Optional[PluginStore] = None) -> Dict[str, Any]:
    """The ONLY way a plugin's license review state changes — never derived
    from the package's own manifest (see module docstring). Setting
    ``reviewed_blocked`` on an enabled plugin does not itself disable it
    (that is an explicit :func:`disable` call, or the next :func:`enable`
    attempt being refused); it does immediately prevent any FUTURE enable."""
    store = store or get_store()
    updated = store.set_license_review(owner, plugin_id, status, reviewer, note)
    store.append_event(owner, plugin_id, "license_review",
                       {"status": status, "reviewer": reviewer}, trace=note)
    return updated


def set_env(owner: str, plugin_id: str, env: Mapping[str, str], *,
           store: Optional[PluginStore] = None) -> None:
    """Owner-scoped secret values for this plugin's own environment,
    isolated per owner (two owners of the same plugin id never share a
    value) and never written into the append-only history."""
    store = store or get_store()
    store.set_env(owner, plugin_id, env)


def get_env(owner: str, plugin_id: str, *, store: Optional[PluginStore] = None) -> Dict[str, str]:
    store = store or get_store()
    return store.get_env(owner, plugin_id)


def uninstall(owner: str, plugin_id: str, *, retention_days: int = 30,
             store: Optional[PluginStore] = None) -> Dict[str, Any]:
    """Marks the plugin uninstalled and disabled; keeps its install
    directory on disk for ``retention_days`` (rollback/audit window) rather
    than deleting it — :func:`purge_expired` is the separate, explicit
    cleanup step once that window has passed."""
    store = store or get_store()
    existing = store.get(owner, plugin_id)
    if existing is None:
        raise PluginNotFound(plugin_id)
    purge_after = time.time() + max(0, int(retention_days)) * 86400
    record = store.mark_uninstalled(owner, plugin_id, purge_after)
    store.append_event(owner, plugin_id, "uninstalled",
                       {"retention_days": retention_days}, trace="")
    return record


def purge_expired(*, store: Optional[PluginStore] = None,
                  now: Optional[float] = None) -> List[str]:
    """Permanently deletes install/backup directories for plugins past
    their retention window. Never called implicitly by any other function
    in this module — a caller (an admin route, a scheduled task) opts in
    explicitly."""
    store = store or get_store()
    purged = []
    for record in store.list_purge_candidates(now=now):
        _rmtree_force(record["install_dir"])
        if record.get("previous_backup_dir"):
            _rmtree_force(record["previous_backup_dir"])
        store.delete(record["owner"], record["id"])
        purged.append(record["id"])
    return purged


# ── consultable by tool_capabilities / preflight ─────────────────────

def get_manifest(owner: str, plugin_id: str, *, store: Optional[PluginStore] = None) -> Dict[str, Any]:
    store = store or get_store()
    record = store.get(owner, plugin_id)
    if record is None:
        raise PluginNotFound(plugin_id)
    return record


def list_plugins(owner: str, *, store: Optional[PluginStore] = None) -> List[Dict[str, Any]]:
    store = store or get_store()
    return store.list_plugins(owner)


def history(owner: str, plugin_id: str, *, store: Optional[PluginStore] = None) -> List[Dict[str, Any]]:
    store = store or get_store()
    return store.history(owner, plugin_id)


def permission_policy(owner: str, plugin_id: str, *,
                      store: Optional[PluginStore] = None) -> Dict[str, bool]:
    """The permission CEILING this plugin is scoped to — never a grant.
    Raises :class:`PluginDisabled` if the plugin is not currently enabled,
    so a caller cannot read a stale ceiling for a plugin nobody may use
    right now."""
    check_use(owner, plugin_id, store=store)
    record = get_manifest(owner, plugin_id, store=store)
    return dict(record["permissions"])


def check_use(owner: str, plugin_id: str, *, store: Optional[PluginStore] = None) -> None:
    """Revalidated on every use (EXT04: "revalidar enabled/scopes y owner
    aunque un cliente conserve schemas cacheados") — raises rather than
    returning a bool so a caller cannot forget to check the result.
    ``readonlyHint`` or any other client-declared hint is never consulted
    here; this is the one server-side source of truth."""
    store = store or get_store()
    record = store.get(owner, plugin_id)
    if record is None:
        raise PluginNotFound(plugin_id)
    if record["license_review_status"] == "reviewed_blocked":
        raise PluginBlocked(f"plugin {plugin_id!r} license is reviewed_blocked")
    if not record["enabled"] or record["status"] not in ("enabled",):
        raise PluginDisabled(f"plugin {plugin_id!r} is not enabled (status={record['status']!r})")


def verify_installed(owner: str, plugin_id: str, *,
                     store: Optional[PluginStore] = None) -> Dict[str, Any]:
    """Recomputes the on-disk digest of an installed plugin and compares it
    to the digest recorded at install/update time. A mismatch means the
    files changed outside this module's own write path — auto-disables the
    plugin and records the trace, rather than silently trusting stale
    metadata the next time something tries to use it."""
    store = store or get_store()
    record = store.get(owner, plugin_id)
    if record is None:
        raise PluginNotFound(plugin_id)
    install_dir = record["install_dir"]
    if not os.path.isdir(install_dir):
        current = None
    else:
        current = _digest_for(record["kind"], install_dir)
    if current == record["digest"]:
        return {"ok": True, "digest": current}

    store.set_enabled(owner, plugin_id, False, "disabled")
    trace = f"expected digest {record['digest']}, found {current!r}"
    store.append_event(owner, plugin_id, "digest_mismatch",
                       {"expected": record["digest"], "found": current}, trace=trace)
    return {"ok": False, "reason": "digest mismatch", "expected": record["digest"],
           "found": current, "trace": trace}
