"""Snapshots of `data/` that run themselves and prove they would restore.

Roadmap, backend: *"Backup/restore guide and helper flow for `data/`."*

`scripts/odysseus-backup` already does manual snapshots well. The gap is that
nobody runs a CLI by hand: the machine that has never been backed up is always
the one with the chats, memories, projects, skills and gallery on it. So this
adds the three things a personal install actually needs.

1. **Automatic.** A background loop takes a snapshot every N hours and keeps the
   last N, so the safety net exists without anyone remembering it.
2. **Verified.** Writing a tarball proves nothing. Every snapshot is re-opened,
   its member paths validated, and every SQLite database inside it extracted to
   a temp dir and run through `PRAGMA integrity_check`. A backup that would not
   restore is reported as broken *at backup time*, not on the day you need it.
3. **Same format as the CLI.** Entries are `data/...`, exactly what
   `odysseus-backup restore` expects, so either tool can read the other's
   output. Only the DB check is extracted during verification — never the whole
   archive, which can be gigabytes of gallery images.

Restore deliberately has no endpoint. Overwriting `data/` under a running app
with open SQLite handles is how you turn one problem into two; the API hands
back the exact command to run with Faustus stopped.
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import tarfile
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from core.platform_compat import restrict_to_owner
from src.backup_crypto import (
    ENCRYPTED_SUFFIX,
    BackupCryptoError,
    DecryptingReader,
    decrypt_file,
    encrypt_stream,
    is_encrypted,
    passphrase_from_env,
)

logger = logging.getLogger(__name__)

PREFIX = "faustus-backup-"
_LEGACY_PREFIXES = ("odysseus-backup-",)
SUFFIX = ".tar.gz"
# Directory names inside data/ that are caches or bulk, skipped by default.
SKIP_DEFAULT = ("deep_research", "mail-attachments")

# ── profiles (SEC-1 / B-010) ──────────────────────────────────────────────
# The old snapshot was one thing: an unencrypted tar of nearly all of `data/`,
# which means `.app_key` AND everything that key protects, `auth.json` with the
# TOTP material, `sessions.json` with live sessions, and the MCP OAuth files —
# in one file that then travels to a NAS or a cloud folder. Reading it is
# enough to log in as the owner.
#
#: What you can hand to a sync folder: the chats, projects, gallery, memories
#: and settings, with the credentials left out. Restores to a working install
#: that asks you to log in again and to re-enter integration secrets.
PROFILE_CONTENT = "content"
#: Everything, including the key material — and therefore encrypted, always,
#: with a passphrase that is never stored in the archive or in `data/`.
PROFILE_FULL = "full"
PROFILES = (PROFILE_CONTENT, PROFILE_FULL)

#: Files directly under data/ that are credentials, not content.
SECRET_FILES = (".app_key", "auth.json", "sessions.json", "vault.json",
                "integrations.json", "workflow_credentials.json")
#: Directories under data/ that contain nothing but credentials.
SECRET_DIRS = ("mcp_oauth",)


def _is_secret(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in SECRET_DIRS:
        return True
    return len(parts) == 1 and parts[0] in SECRET_FILES


def data_dir() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR)


def backup_dir() -> Path:
    override = os.getenv("ODYSSEUS_BACKUP_DIR") or os.getenv("FAUSTUS_BACKUP_DIR")
    if override:
        return Path(override)
    from src.runtime_paths import get_app_root
    return Path(get_app_root()) / "backups"



# ── one backup at a time ──────────────────────────────────────────────────

LOCK_NAME = ".backup.lock"
LOCK_STALE_SECONDS = 3600


class BackupBusy(Exception):
    """Another snapshot holds the lease."""


class _Lease:
    """A lock file both the manual and the scheduled path take.

    They used to be able to run at the same time, writing, verifying and
    pruning the same directory: whoever finished second could prune the file
    the first one was still verifying. `O_EXCL` is the whole mechanism — it is
    atomic on every filesystem this runs on, and a lease older than an hour is
    treated as a crash, not as a live backup.
    """

    def __init__(self, root: Path):
        self.path = root / LOCK_NAME
        self.taken = False

    def __enter__(self) -> "_Lease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if attempt == 1 and self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise BackupBusy(f"another backup is running (lease at {self.path})")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "started": time.time()}, fh)
            self.taken = True
            return self
        raise BackupBusy(f"another backup is running (lease at {self.path})")

    def _stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if age < LOCK_STALE_SECONDS:
            return False
        logger.warning("[backup] clearing a stale lease (%.0f s old)", age)
        return True

    def __exit__(self, *exc: Any) -> None:
        if not self.taken:
            return
        try:
            self.path.unlink()
        except OSError as e:
            logger.warning("[backup] could not release the lease: %s", e)


# ── writing ───────────────────────────────────────────────────────────────

def _sqlite_safe_copy(src: Path, dst: Path) -> None:
    """Copy a live SQLite file with its own backup API, not a byte copy.

    A plain copy of a database being written to yields a tarball that only
    fails on the day you restore it.
    """
    src_conn = dst_conn = None
    try:
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dst))
        with dst_conn:
            src_conn.backup(dst_conn)
    except Exception:
        # Not a SQLite file (or unreadable) — copy the bytes and move on.
        dst.write_bytes(src.read_bytes())
    finally:
        # Windows will not delete a file that still has an open handle, and the
        # staging directory is a TemporaryDirectory: leaking either connection
        # turns a junk .db into a failed backup.
        for conn in (src_conn, dst_conn):
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


def _should_skip(rel: Path, include_research: bool, include_attachments: bool) -> bool:
    parts = rel.parts
    if not include_research and "deep_research" in parts:
        return True
    if not include_attachments and "mail-attachments" in parts:
        return True
    return False


def _tar_data_dir(src_root: Path, out: Path, *, include_research: bool,
                  include_attachments: bool, profile: str) -> Dict[str, Any]:
    """Write the tar.gz. Returns what went in and what was deliberately left out."""
    files = 0
    raw_bytes = 0
    excluded: List[str] = []
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        staged: Dict[Path, Path] = {}
        for db in src_root.rglob("*.db"):
            if not db.is_file() or db.is_symlink():
                continue
            rel = db.relative_to(src_root)
            if profile == PROFILE_CONTENT and _is_secret(rel):
                continue
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _sqlite_safe_copy(db, target)
            staged[db] = target

        with tarfile.open(out, "w:gz") as tar:
            for path in sorted(src_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                rel = path.relative_to(src_root)
                if _should_skip(rel, include_research, include_attachments):
                    continue
                if profile == PROFILE_CONTENT and _is_secret(rel):
                    excluded.append(rel.as_posix())
                    continue
                source = staged.get(path, path)
                # Always "data/..." so `odysseus-backup restore` accepts it,
                # even when the live directory is named something else.
                tar.add(source, arcname=str(PurePosixPath("data", *rel.parts)))
                files += 1
                try:
                    raw_bytes += source.stat().st_size
                except OSError:
                    pass
    return {"files": files, "uncompressed_bytes": raw_bytes, "excluded": sorted(excluded)}


def _fsync_file(path: Path) -> None:
    with open(path, "rb+") as fh:
        os.fsync(fh.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_key() -> Optional[bytes]:
    """The app key, used ONLY to authenticate the manifest — never copied into
    it. A manifest anyone can rewrite proves nothing about the archive."""
    from src.constants import APP_KEY_FILE
    try:
        return Path(APP_KEY_FILE).read_bytes()
    except OSError:
        return None


def write_manifest(archive: Path, body: Dict[str, Any]) -> Dict[str, Any]:
    """Write `<archive>.manifest.json` next to the archive and return it."""
    manifest = dict(body)
    manifest["schema"] = 1
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = _manifest_key()
    manifest["hmac"] = hmac.new(key, canonical, hashlib.sha256).hexdigest() if key else ""
    path = archive.with_name(archive.name + ".manifest.json")
    tmp = path.with_name(path.name + f".part-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    restrict_to_owner(tmp)
    os.replace(tmp, path)
    return manifest


def read_manifest(archive: Path) -> Dict[str, Any]:
    """Read and CHECK the manifest beside an archive.

    `authenticated` is False when there is no key, when the HMAC does not match
    and when the manifest is missing — three different sentences in `problem`,
    because they mean three different things to whoever is holding the file.
    """
    path = archive.with_name(archive.name + ".manifest.json")
    out: Dict[str, Any] = {"present": False, "authenticated": False, "problem": ""}
    if not path.is_file():
        out["problem"] = "no manifest beside this archive"
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        out["problem"] = f"unreadable manifest: {type(e).__name__}"
        return out
    out["present"] = True
    out["manifest"] = data
    stored = str(data.pop("hmac", "") or "")
    key = _manifest_key()
    if not stored:
        out["problem"] = "manifest carries no HMAC"
    elif key is None:
        out["problem"] = "no app key on this machine to check the manifest against"
    else:
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        out["authenticated"] = hmac.compare_digest(expected, stored)
        if not out["authenticated"]:
            out["problem"] = "manifest HMAC does not match: it was written by another install, or edited"
    return out


def snapshot(*, include_research: bool = False, include_attachments: bool = False,
             out_path: Optional[str] = None, keep: Optional[int] = None,
             verify: bool = True, profile: str = PROFILE_CONTENT,
             passphrase: Optional[str] = None) -> Dict[str, Any]:
    """Write one snapshot of the data directory and (by default) verify it.

    SEC-1 (B-010). Two profiles, and the safe one is the default:

    * ``content`` — no `.app_key`, no `auth.json`, no `sessions.json`, no
      vault, no integrations, no MCP OAuth. Plain tar.gz, because there is
      nothing in it that a passphrase would be protecting.
    * ``full`` — all of it, and therefore encrypted with a passphrase that is
      never written into the archive or into `data/`. Without a passphrase the
      snapshot is REFUSED rather than written in the clear.

    The file is written to a `.part` name in the same directory, fsynced,
    locked down to the owner and only then renamed into place, so a crash
    leaves no half-file that looks like a backup, and its name carries a random
    suffix so two snapshots in the same second cannot collide.
    """
    started = time.time()
    src_root = data_dir()
    if not src_root.is_dir():
        return {"ok": False, "error": f"no data directory at {src_root}"}
    if profile not in PROFILES:
        return {"ok": False, "error": f"unknown backup profile {profile!r}; use one of {PROFILES}"}

    if profile == PROFILE_FULL:
        passphrase = passphrase or passphrase_from_env()
        if not passphrase:
            return {"ok": False, "error": (
                "a full backup contains data/.app_key and the credentials it protects, so it is "
                "only written encrypted. Set FAUSTUS_BACKUP_PASSPHRASE (or pass one) — or take a "
                "`content` snapshot, which leaves the credentials out.")}

    encrypted = profile == PROFILE_FULL
    if out_path:
        out = Path(out_path)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{PREFIX}{stamp}-{uuid.uuid4().hex[:8]}{SUFFIX}"
        out = backup_dir() / (name + ENCRYPTED_SUFFIX if encrypted else name)
    try:
        out.resolve().relative_to(src_root.resolve())
        return {"ok": False, "error": "backup output must live outside data/"}
    except ValueError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        lease = _Lease(out.parent)
        lease.__enter__()
    except BackupBusy as e:
        return {"ok": False, "error": str(e), "busy": True}

    part = out.with_name(out.name + f".part-{uuid.uuid4().hex[:8]}")
    plain_part = out.with_name(out.name + f".plain-{uuid.uuid4().hex[:8]}")
    plaintext_sha = ""
    try:
        target = plain_part if encrypted else part
        counted = _tar_data_dir(src_root, target, include_research=include_research,
                                include_attachments=include_attachments, profile=profile)
        restrict_to_owner(target)
        _fsync_file(target)
        if encrypted:
            with open(plain_part, "rb") as src, open(part, "wb") as dst:
                info = encrypt_stream(src, dst, passphrase)
                dst.flush()
                os.fsync(dst.fileno())
            plaintext_sha = info["sha256"]
            restrict_to_owner(part)
        os.replace(part, out)
    except (OSError, BackupCryptoError) as e:
        for leftover in (part, plain_part):
            try:
                leftover.unlink()
            except OSError:
                pass
        lease.__exit__(None, None, None)
        return {"ok": False, "error": f"snapshot failed: {type(e).__name__}: {e}"}
    finally:
        try:
            plain_part.unlink()
        except OSError:
            pass

    result: Dict[str, Any] = {
        "ok": True,
        "path": str(out),
        "name": out.name,
        "profile": profile,
        "encrypted": encrypted,
        "files": counted["files"],
        "excluded": counted["excluded"],
        "uncompressed_bytes": counted["uncompressed_bytes"],
        "bytes": out.stat().st_size,
        "seconds": round(time.time() - started, 2),
        "included_research": include_research,
        "included_attachments": include_attachments,
    }
    try:
        result["manifest"] = write_manifest(out, {
            "name": out.name,
            "profile": profile,
            "encrypted": encrypted,
            "created": datetime.now().isoformat(timespec="seconds"),
            "files": counted["files"],
            "excluded": counted["excluded"],
            "bytes": result["bytes"],
            "sha256": _sha256_file(out),
            "plaintext_sha256": plaintext_sha,
        })
        if verify:
            result["verified"] = verify_archive(out, passphrase=passphrase)
            result["ok"] = bool(result["verified"].get("ok"))
        if keep:
            result["pruned"] = prune(keep)
    finally:
        lease.__exit__(None, None, None)
    logger.info("[backup] %s profile=%s encrypted=%s files=%s bytes=%s ok=%s", out.name,
                profile, encrypted, counted["files"], result["bytes"], result["ok"])
    return result


# ── reading / checking ────────────────────────────────────────────────────

def _member_problem(member: tarfile.TarInfo) -> Optional[str]:
    rel = PurePosixPath(member.name)
    if rel.is_absolute() or ".." in rel.parts:
        return f"path escapes the archive: {member.name}"
    if not rel.parts or rel.parts[0] != "data":
        return f"entry outside data/: {member.name}"
    if member.issym() or member.islnk():
        return f"link entry: {member.name}"
    if not (member.isdir() or member.isfile()):
        return f"special file entry: {member.name}"
    return None


def _open_archive(path: Path, passphrase: Optional[str]):
    """(tar, closer) for a plain or an encrypted snapshot, read as a STREAM.

    Streaming is not an optimisation here, it is the requirement: an encrypted
    snapshot has no plaintext on disk to seek around in, and a full one can be
    gigabytes. `r|gz` reads forward once, which is exactly what the decrypting
    reader can give it.
    """
    fh = open(path, "rb")
    try:
        if is_encrypted(path):
            pw = passphrase or passphrase_from_env()
            if not pw:
                raise BackupCryptoError(
                    "this snapshot is encrypted and no passphrase was given: "
                    "set FAUSTUS_BACKUP_PASSPHRASE, or pass one, to read it")
            reader = DecryptingReader(fh, pw)
            return tarfile.open(fileobj=reader, mode="r|gz"), reader
        return tarfile.open(fileobj=fh, mode="r|gz"), fh
    except Exception:
        fh.close()
        raise


def verify_archive(path: Any, *, check_databases: bool = True,
                   passphrase: Optional[str] = None,
                   check_hash: bool = True) -> Dict[str, Any]:
    """Open the archive, validate every member, integrity-check the databases.

    Only `*.db` members are extracted (to a temp dir); the rest is walked, so
    verifying a multi-gigabyte gallery costs almost nothing. An encrypted
    snapshot is decrypted on the way through — which also proves the
    passphrase and every authentication tag in the file.
    """
    p = Path(path)
    out: Dict[str, Any] = {"ok": False, "path": str(p), "members": 0,
                           "databases": [], "problems": [], "encrypted": False,
                           "manifest": {}}
    if not p.is_file():
        out["problems"].append("no such backup file")
        return out
    out["encrypted"] = is_encrypted(p)
    out["manifest"] = read_manifest(p)
    if check_hash:
        recorded = str((out["manifest"].get("manifest") or {}).get("sha256") or "")
        if recorded:
            actual = _sha256_file(p)
            out["sha256"] = actual
            if actual != recorded:
                out["problems"].append("the archive does not match the hash in its manifest")

    try:
        tar, closer = _open_archive(p, passphrase)
        try:
            with tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                for member in tar:
                    out["members"] += 1
                    problem = _member_problem(member)
                    if problem:
                        out["problems"].append(problem)
                        continue
                    if not (check_databases and member.isfile() and member.name.endswith(".db")):
                        continue
                    src = tar.extractfile(member)
                    if src is None:
                        out["problems"].append(f"unreadable member: {member.name}")
                        continue
                    target = tmp / PurePosixPath(member.name).name
                    with open(target, "wb") as fh:
                        while True:
                            block = src.read(1024 * 1024)
                            if not block:
                                break
                            fh.write(block)
                    out["databases"].append(_integrity_check(target, member.name))
                    target.unlink(missing_ok=True)
        finally:
            tar.close()
            closer.close()
    except BackupCryptoError as e:
        out["problems"].append(str(e))
        return out
    except Exception as e:
        # Deliberately broad: gzip truncation surfaces as EOFError/zlib.error,
        # which are neither TarError nor OSError. Anything that stops us
        # reading the archive means the same thing to the caller — this backup
        # cannot be trusted.
        out["problems"].append(f"archive is unreadable: {type(e).__name__}")
        return out

    broken = [db for db in out["databases"] if not db["ok"]]
    out["ok"] = not out["problems"] and not broken and out["members"] > 0
    if not out["members"]:
        out["problems"].append("archive is empty")
    return out


def _integrity_check(path: Path, name: str) -> Dict[str, Any]:
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        verdict = (row or ["no result"])[0]
        return {"name": name, "ok": verdict == "ok", "detail": verdict}
    except Exception as e:
        return {"name": name, "ok": False, "detail": type(e).__name__}


def _is_backup(p: Path) -> bool:
    name = p.name
    if name.endswith(ENCRYPTED_SUFFIX):
        name = name[: -len(ENCRYPTED_SUFFIX)]
    return p.is_file() and name.endswith(SUFFIX) and (
        p.name.startswith(PREFIX) or p.name.startswith(_LEGACY_PREFIXES))


def list_backups() -> List[Dict[str, Any]]:
    """Newest first. Tolerates a missing backups directory."""
    root = backup_dir()
    if not root.is_dir():
        return []
    entries = []
    for p in sorted(root.iterdir()):
        try:
            if not _is_backup(p):
                continue
            st = p.stat()
        except OSError:
            continue
        entries.append({"name": p.name, "path": str(p), "bytes": st.st_size,
                        "encrypted": p.name.endswith(ENCRYPTED_SUFFIX),
                        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                        "age_hours": round((time.time() - st.st_mtime) / 3600, 2)})
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries


def prune(keep: int) -> List[str]:
    """Delete all but the newest `keep` snapshots. Never touches other files."""
    if keep is None or keep <= 0:
        return []
    removed = []
    for entry in list_backups()[keep:]:
        try:
            path = Path(entry["path"])
            path.unlink()
            # The manifest is part of the snapshot; leaving it orphaned would
            # make the next listing look like a backup that lost its archive.
            path.with_name(path.name + ".manifest.json").unlink(missing_ok=True)
            removed.append(entry["name"])
        except OSError as e:
            logger.warning("[backup] could not prune %s: %s", entry["name"], e)
    return removed


def resolve_in_backup_dir(name: str) -> Optional[Path]:
    """Map a client-supplied name to a file inside the backup dir, or None.

    Name only — no traversal, no absolute paths, no reading arbitrary files
    off disk through an admin endpoint.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    candidate = backup_dir() / name
    try:
        candidate.resolve().relative_to(backup_dir().resolve())
    except (ValueError, OSError):
        return None
    return candidate if _is_backup(candidate) else None


def restore_command(name: str) -> str:
    """The exact manual step. Restore is destructive and needs the app stopped.

    A restore puts back whatever `sessions.json` the snapshot carried, which
    would resurrect sessions that were revoked in the meantime — so the CLI
    drops that file after extracting, and every user logs in again. Said here
    because the person reading this is about to do it.
    """
    path = backup_dir() / name
    if str(name).endswith(ENCRYPTED_SUFFIX):
        return (f"Stop Faustus, then run:  python scripts/odysseus-backup restore "
                f"\"{path}\" --yes --passphrase-env FAUSTUS_BACKUP_PASSPHRASE"
                f"   (active sessions are invalidated by the restore)")
    return (f"Stop Faustus, then run:  python scripts/odysseus-backup restore "
            f"\"{path}\" --yes   (active sessions are invalidated by the restore)")


def status() -> Dict[str, Any]:
    backups = list_backups()
    newest = backups[0] if backups else None
    return {
        "backup_dir": str(backup_dir()),
        "data_dir": str(data_dir()),
        "count": len(backups),
        "newest": newest,
        "total_bytes": sum(b["bytes"] for b in backups),
    }


# ── the part that makes it exist: doing it without being asked ────────────

DEFAULT_INTERVAL_HOURS = 24
DEFAULT_KEEP = 7


def due(interval_hours: float) -> bool:
    """True when there is no snapshot, or the newest one is older than the interval."""
    backups = list_backups()
    if not backups:
        return True
    return backups[0]["age_hours"] >= max(float(interval_hours), 0.25)


def run_scheduled_snapshot(get_setting_fn=None) -> Optional[Dict[str, Any]]:
    """One tick of the schedule: snapshot if due, prune, return the result.

    Synchronous on purpose — the caller runs it in a thread so tarring a few
    hundred megabytes never blocks the event loop.
    """
    if get_setting_fn is None:
        from src.settings import get_setting as get_setting_fn  # noqa: PLC0415
    if not bool(get_setting_fn("backup_auto_enabled", True)):
        return None
    try:
        interval = float(get_setting_fn("backup_interval_hours", DEFAULT_INTERVAL_HOURS) or DEFAULT_INTERVAL_HOURS)
        keep = int(get_setting_fn("backup_keep", DEFAULT_KEEP) or DEFAULT_KEEP)
    except (TypeError, ValueError):
        interval, keep = DEFAULT_INTERVAL_HOURS, DEFAULT_KEEP
    if not due(interval):
        return None
    # SEC-1 (B-010): an unattended snapshot takes the profile it can actually
    # protect. With a passphrase in the environment it takes the full one,
    # encrypted; without one it takes `content`, which leaves the credentials
    # out — rather than writing them to disk in the clear every night.
    passphrase = passphrase_from_env()
    return snapshot(
        include_research=bool(get_setting_fn("backup_include_research", False)),
        keep=keep,
        profile=PROFILE_FULL if passphrase else PROFILE_CONTENT,
        passphrase=passphrase,
    )


async def run_auto_backups(*, first_delay: float = 180.0,
                           check_every: float = 1800.0) -> None:
    """Background loop started at app startup. Never raises into the app.

    Checks often, snapshots rarely: the interval decides, so a laptop that is
    off overnight still gets a snapshot when it comes back rather than skipping
    the slot entirely.
    """
    import asyncio
    await asyncio.sleep(first_delay)
    while True:
        try:
            result = await asyncio.to_thread(run_scheduled_snapshot)
            if result and not result.get("ok"):
                logger.warning("[backup] scheduled snapshot did not verify: %s",
                               (result.get("verified") or {}).get("problems"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[backup] scheduled snapshot failed: %s", type(e).__name__)
        await asyncio.sleep(check_every)
