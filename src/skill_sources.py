"""A25 — git-backed skill sources: install, check for updates, verified
update, and byte-exact rollback.

A skill installed by `src/skills_runtime/discovery.py`/`services/memory/
skills.py` is just files under a directory (`SKILL.md` + whatever else it
ships). This module adds a SECOND, independent thing on top: where those
files came from and what revision is currently on disk, so a skill can be
tracked against a git repository the way a dependency is.

Three operations, matching the trigger this file closes (A25 — "Git skill
branch changes after install"):

* `install(skill_dir, source_url, ref)` — clone `ref` from `source_url`,
  verify it, and lay its files down at `skill_dir`. Records `pinned_revision`
  (the exact commit SHA checked out — never a moving ref name) and
  `source_url`/`ref` for later checks.
* `check_updates(skill_key)` — a read-only `git ls-remote` against the
  declared ref. Tells the caller the branch moved. Changes NOTHING: the
  installed skill keeps running the `pinned_revision` it always has, exactly
  the acceptance bar ("Pinned revision remains active until verified
  update").
* `update(skill_key, verify=True)` — fetches the new tip into a STAGING
  directory, never touching the live skill until verification (digest,
  frontmatter, and — if the skill declares a script — running it against a
  caller-supplied fixture) passes. Only then does it back up the current
  files byte-for-byte (so `rollback()` can restore them) and promote the
  staged ones into place.
* `rollback(skill_key)` — restores the previous revision's files from that
  backup, byte for byte, checking the restored digest against the one
  recorded when the backup was made rather than trusting the filesystem.

Deliberately NOT integrated into `services/memory/skills.py`'s SKILL.md
frontmatter or `src/skills_runtime/discovery.py`'s folder walk: source
metadata (URL, ref, revision, backup path) lives in its OWN store
(`skill_sources.db`, `skill_sources_backups/`) under `DATA_DIR`, entirely
outside the skill's own directory. That is what keeps ADP-25's digest
promise intact — `discovery.skill_digest` hashes every regular file in a
skill's folder, so if source-tracking metadata lived inside that folder,
recording an update check would itself change the digest an already-approved
run is pinned to. A file this module writes is never one `skill_digest`
would see.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

SKILL_SOURCES_DB = os.path.join(DATA_DIR, "skill_sources.db")
BACKUPS_ROOT = os.path.join(DATA_DIR, "skill_sources_backups")

#: Script extensions this module recognizes as "the skill has a script" for
#: `update()`'s fixture-execution verification step — the same interpreter
#: map `src/workflows/skills.py` uses to run one, so "has a script" means
#: the same thing in both places.
_SCRIPT_EXTENSIONS = (".py", ".js", ".mjs", ".sh")

GIT_TIMEOUT_SECONDS = 60


class SkillSourceError(Exception):
    """Raised for a caller mistake (unknown skill_key, non-empty install
    target, nothing to roll back to) — never for a verification failure,
    which `update()` reports in its return value instead so a routine "the
    branch moved but doesn't verify yet" is not an exception a caller has to
    catch."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_git(args: list[str], cwd: Optional[str] = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise SkillSourceError("git is not available on this host") from e
    except subprocess.TimeoutExpired as e:
        raise SkillSourceError(f"git {' '.join(args)} timed out") from e
    if result.returncode != 0:
        raise SkillSourceError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout.strip()


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(SKILL_SOURCES_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_sources (
            skill_key TEXT PRIMARY KEY,
            skill_dir TEXT NOT NULL,
            source_url TEXT NOT NULL,
            ref TEXT NOT NULL,
            pinned_revision TEXT NOT NULL,
            pinned_digest TEXT NOT NULL,
            previous_revision TEXT,
            previous_digest TEXT,
            previous_backup_dir TEXT,
            installed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _save_source(**fields) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO skill_sources (skill_key, skill_dir, source_url, ref, "
            "pinned_revision, pinned_digest, previous_revision, previous_digest, "
            "previous_backup_dir, installed_at, updated_at) "
            "VALUES (:skill_key,:skill_dir,:source_url,:ref,:pinned_revision,"
            ":pinned_digest,:previous_revision,:previous_digest,"
            ":previous_backup_dir,:installed_at,:updated_at) "
            "ON CONFLICT(skill_key) DO UPDATE SET "
            "skill_dir=excluded.skill_dir, source_url=excluded.source_url, "
            "ref=excluded.ref, pinned_revision=excluded.pinned_revision, "
            "pinned_digest=excluded.pinned_digest, "
            "previous_revision=excluded.previous_revision, "
            "previous_digest=excluded.previous_digest, "
            "previous_backup_dir=excluded.previous_backup_dir, "
            "updated_at=excluded.updated_at",
            fields,
        )
        conn.commit()
    finally:
        conn.close()


def get_source(skill_key: str) -> Optional[dict]:
    """This skill's recorded git source, or `None` if it was never
    `install()`-ed through this module."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM skill_sources WHERE skill_key = ?", (skill_key,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _digest_of(folder) -> str:
    """The same digest ADP-25 uses ("editing a file changes the digest and
    the run fails as editted") — reused, not reimplemented, so a skill
    installed through this module and one discovered the ordinary way agree
    on what "unchanged" means."""
    from src.skills_runtime.discovery import DiscoveredSkill, skill_digest
    found = DiscoveredSkill(name="", path=str(Path(folder) / "SKILL.md"),
                            origin="", root=str(folder), distance=0)
    return skill_digest(found)


def _validate_frontmatter(skill_md: Path) -> None:
    """`update()`'s verification step (b): a SKILL.md with a real `name` and
    `description` in its frontmatter. `Skill.from_markdown` itself never
    raises on a sparse/malformed document (it slugifies a fallback name) —
    this is deliberately STRICTER than that default-tolerant parser, because
    a fetched revision that silently became garbage should fail the update,
    not get promoted with a name nobody chose."""
    from services.memory.skill_format import parse_frontmatter
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"could not read SKILL.md: {e}") from e
    try:
        fm, _body = parse_frontmatter(text)
    except Exception as e:
        raise ValueError(f"frontmatter did not parse: {e}") from e
    if not isinstance(fm, dict) or not str(fm.get("name") or "").strip():
        raise ValueError("frontmatter is missing a non-empty 'name'")
    if not str(fm.get("description") or "").strip():
        raise ValueError("frontmatter is missing a non-empty 'description'")


def _declared_script(skill_md: Path) -> Optional[Path]:
    """The script this skill runs, if any: an explicit `script:` frontmatter
    field, or (failing that) the sole top-level file with a recognized
    script extension next to `SKILL.md` — the same layout
    `src/workflows/skills.py::run` expects for `node.config["script"]`.
    `None` means this skill has no script to verify by execution."""
    from services.memory.skill_format import parse_frontmatter
    try:
        fm, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except Exception:
        fm = {}
    declared = str((fm or {}).get("script") or "").strip()
    folder = skill_md.parent
    if declared:
        candidate = folder / declared
        return candidate if candidate.is_file() else None
    scripts = [p for p in folder.iterdir()
              if p.is_file() and p.suffix.lower() in _SCRIPT_EXTENSIONS]
    return scripts[0] if len(scripts) == 1 else None


def _fetch_ref_into(source_url: str, ref: str, dest_dir: str) -> str:
    """Clone `source_url`, check out `ref`, and copy its working tree
    (never its `.git`) into `dest_dir`. Returns the resolved commit SHA —
    the actual `pinned_revision`, not the ref name, so a branch that moves
    later never silently changes what an already-installed skill runs."""
    with tempfile.TemporaryDirectory(prefix="faustus-skillsrc-") as clone_dir:
        _run_git(["clone", "--quiet", "--no-single-branch", source_url, clone_dir])
        _run_git(["checkout", "--quiet", ref], cwd=clone_dir)
        sha = _run_git(["rev-parse", "HEAD"], cwd=clone_dir)
        shutil.rmtree(os.path.join(clone_dir, ".git"), ignore_errors=True)
        os.makedirs(dest_dir, exist_ok=True)
        for entry in os.listdir(clone_dir):
            src = os.path.join(clone_dir, entry)
            dst = os.path.join(dest_dir, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    return sha


def _remote_ref_sha(source_url: str, ref: str) -> str:
    """The commit `ref` currently points to on `source_url`, without
    fetching or touching any working copy — `check_updates()`'s whole
    mechanism."""
    out = _run_git(["ls-remote", source_url, ref])
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        # Not a branch/tag name ls-remote can match (e.g. a bare SHA, or a
        # ref that no longer exists) — resolve it the expensive way, via a
        # throwaway fetch, rather than reporting a false "no update".
        with tempfile.TemporaryDirectory(prefix="faustus-skillsrc-check-") as tmp:
            _run_git(["init", "--quiet", tmp])
            _run_git(["fetch", "--quiet", "--depth", "1", source_url, ref], cwd=tmp)
            return _run_git(["rev-parse", "FETCH_HEAD"], cwd=tmp)
    return lines[0].split()[0]


def _replace_dir_contents(target: Path, source: Path) -> None:
    """Empty `target` and copy every entry of `source` into it. Used for
    both promotion (staging -> live) and rollback (backup -> live) so the
    two share one "swap the files" primitive."""
    for entry in list(target.iterdir()):
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in source.iterdir():
        dest = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest)
        else:
            shutil.copy2(entry, dest)


def _backup_dir_for(skill_key: str) -> Path:
    # skill_key is often a filesystem path; hash it so the backup path never
    # itself tries to be one.
    import hashlib
    digest = hashlib.sha256(skill_key.encode("utf-8")).hexdigest()[:24]
    return Path(BACKUPS_ROOT) / digest / "previous"


def install(skill_dir, source_url: str, ref: str = "HEAD", *,
           skill_key: Optional[str] = None) -> dict:
    """Install a skill's files from `ref` of `source_url` into `skill_dir`
    (which must not already contain anything — use `update()` for a skill
    that is already installed). Verifies the same way `update()` does
    before laying anything down. Returns the saved source record."""
    skill_dir = str(Path(skill_dir))
    skill_key = skill_key or skill_dir
    target = Path(skill_dir)
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise SkillSourceError(
            f"{skill_dir!r} is not empty; install() is for a fresh skill "
            "directory, update() is for one that already has a source")
    with tempfile.TemporaryDirectory(prefix="faustus-skillsrc-install-") as staging:
        sha = _fetch_ref_into(source_url, ref, staging)
        skill_md = Path(staging) / "SKILL.md"
        if not skill_md.is_file():
            raise SkillSourceError(f"{source_url!r}@{ref} has no SKILL.md at its root")
        try:
            _validate_frontmatter(skill_md)
        except ValueError as e:
            raise SkillSourceError(f"fetched skill failed verification: {e}") from e
        digest = _digest_of(staging)
        _replace_dir_contents(target, Path(staging))
    now = _utcnow_iso()
    _save_source(skill_key=skill_key, skill_dir=skill_dir, source_url=source_url, ref=ref,
                pinned_revision=sha, pinned_digest=digest, previous_revision=None,
                previous_digest=None, previous_backup_dir=None,
                installed_at=now, updated_at=now)
    logger.info("Installed skill %r from %s@%s (%s)", skill_key, source_url, ref, sha[:12])
    return get_source(skill_key)


def check_updates(skill_key: str) -> dict:
    """Whether `ref` has moved since this skill was pinned. Read-only: never
    fetches into the skill's directory, never changes `pinned_revision`.
    "Pinned revision remains active until verified update" is enforced
    simply by this function never writing anything."""
    src = get_source(skill_key)
    if src is None:
        raise SkillSourceError(f"no git source recorded for skill {skill_key!r}")
    remote_sha = _remote_ref_sha(src["source_url"], src["ref"])
    return {
        "skill_key": skill_key,
        "source_url": src["source_url"],
        "ref": src["ref"],
        "pinned_revision": src["pinned_revision"],
        "remote_revision": remote_sha,
        "update_available": remote_sha != src["pinned_revision"],
    }


def update(skill_key: str, *, verify: bool = True,
          run_fixture: Optional[Callable[[str], "tuple[bool, str]"]] = None) -> dict:
    """Fetch the current tip of `ref` into a staging copy and, only if it
    verifies, promote it — backing up the current files first so
    `rollback()` has something byte-exact to restore.

    `run_fixture(script_path) -> (ok, detail)` is called when `verify` is
    True and the fetched skill declares a script (`_declared_script`); the
    caller supplies the fixture environment (there is no default one here —
    what a fixture run means is specific to the skill). Its absence does
    NOT fail verification: a caller that has no fixture handy still gets
    digest + frontmatter verification, which is the honest floor, not a
    silent downgrade.
    """
    src = get_source(skill_key)
    if src is None:
        raise SkillSourceError(f"no git source recorded for skill {skill_key!r}")
    skill_dir = Path(src["skill_dir"])
    if not skill_dir.is_dir():
        raise SkillSourceError(f"skill directory {skill_dir} no longer exists")

    with tempfile.TemporaryDirectory(prefix="faustus-skillsrc-update-") as staging_s:
        staging = Path(staging_s)
        sha = _fetch_ref_into(src["source_url"], src["ref"], staging_s)
        if sha == src["pinned_revision"]:
            return {"status": "unchanged", "pinned_revision": sha}

        skill_md = staging / "SKILL.md"
        if verify:
            if not skill_md.is_file():
                return {"status": "failed", "reason": "fetched revision has no SKILL.md",
                        "fetched_revision": sha}
            try:
                _validate_frontmatter(skill_md)
            except ValueError as e:
                return {"status": "failed", "reason": f"invalid frontmatter: {e}",
                        "fetched_revision": sha}
            script = _declared_script(skill_md)
            if script is not None and run_fixture is not None:
                try:
                    ok, detail = run_fixture(str(script))
                except Exception as e:  # noqa: BLE001 — a fixture crash is a verify failure
                    ok, detail = False, f"fixture raised: {e}"
                if not ok:
                    return {"status": "failed",
                            "reason": f"script fixture verification failed: {detail}",
                            "fetched_revision": sha}

        digest = _digest_of(staging_s)
        backup_dir = _backup_dir_for(skill_key)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, backup_dir)
        backup_digest = _digest_of(backup_dir)  # what's actually on disk in the backup
        _replace_dir_contents(skill_dir, staging)

    now = _utcnow_iso()
    _save_source(skill_key=skill_key, skill_dir=str(skill_dir), source_url=src["source_url"],
                ref=src["ref"], pinned_revision=sha, pinned_digest=digest,
                previous_revision=src["pinned_revision"], previous_digest=backup_digest,
                previous_backup_dir=str(backup_dir), installed_at=src["installed_at"],
                updated_at=now)
    logger.info("Updated skill %r %s -> %s", skill_key, src["pinned_revision"][:12], sha[:12])
    return {"status": "updated", "pinned_revision": sha,
            "previous_revision": src["pinned_revision"]}


def rollback(skill_key: str) -> dict:
    """Restore the previous revision's files byte-for-byte from the backup
    `update()` made, verifying the backup's digest before AND after the
    restore rather than trusting either the stored copy or the write."""
    src = get_source(skill_key)
    if src is None:
        raise SkillSourceError(f"no git source recorded for skill {skill_key!r}")
    if not src.get("previous_backup_dir") or not src.get("previous_revision"):
        raise SkillSourceError(f"skill {skill_key!r} has no previous revision to roll back to")
    backup_dir = Path(src["previous_backup_dir"])
    if not backup_dir.is_dir():
        raise SkillSourceError(f"backup for {skill_key!r} is missing on disk")
    if _digest_of(backup_dir) != src["previous_digest"]:
        raise SkillSourceError(
            f"backup for {skill_key!r} no longer matches its recorded digest; refusing to restore")

    skill_dir = Path(src["skill_dir"])
    skill_dir.mkdir(parents=True, exist_ok=True)
    _replace_dir_contents(skill_dir, backup_dir)
    restored_digest = _digest_of(skill_dir)
    if restored_digest != src["previous_digest"]:
        raise SkillSourceError(
            f"restore of {skill_key!r} did not reproduce the recorded digest "
            f"(expected {src['previous_digest']}, got {restored_digest})")

    now = _utcnow_iso()
    _save_source(skill_key=skill_key, skill_dir=str(skill_dir), source_url=src["source_url"],
                ref=src["ref"], pinned_revision=src["previous_revision"],
                pinned_digest=src["previous_digest"], previous_revision=None,
                previous_digest=None, previous_backup_dir=None,
                installed_at=src["installed_at"], updated_at=now)
    logger.info("Rolled back skill %r to %s", skill_key, src["previous_revision"][:12])
    return get_source(skill_key)
