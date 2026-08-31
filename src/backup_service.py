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

import logging
import os
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PREFIX = "faustus-backup-"
_LEGACY_PREFIXES = ("odysseus-backup-",)
SUFFIX = ".tar.gz"
# Directory names inside data/ that are caches or bulk, skipped by default.
SKIP_DEFAULT = ("deep_research", "mail-attachments")


def data_dir() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR)


def backup_dir() -> Path:
    override = os.getenv("ODYSSEUS_BACKUP_DIR") or os.getenv("FAUSTUS_BACKUP_DIR")
    if override:
        return Path(override)
    from src.runtime_paths import get_app_root
    return Path(get_app_root()) / "backups"


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


def snapshot(*, include_research: bool = False, include_attachments: bool = False,
             out_path: Optional[str] = None, keep: Optional[int] = None,
             verify: bool = True) -> Dict[str, Any]:
    """Write one tar.gz of the data directory and (by default) verify it."""
    started = time.time()
    src_root = data_dir()
    if not src_root.is_dir():
        return {"ok": False, "error": f"no data directory at {src_root}"}

    out = Path(out_path) if out_path else (
        backup_dir() / f"{PREFIX}{datetime.now().strftime('%Y%m%d-%H%M%S')}{SUFFIX}")
    try:
        out.resolve().relative_to(src_root.resolve())
        return {"ok": False, "error": "backup output must live outside data/"}
    except ValueError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)

    files = 0
    raw_bytes = 0
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        staged: Dict[Path, Path] = {}
        for db in src_root.rglob("*.db"):
            if not db.is_file() or db.is_symlink():
                continue
            rel = db.relative_to(src_root)
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
                source = staged.get(path, path)
                # Always "data/..." so `odysseus-backup restore` accepts it,
                # even when the live directory is named something else.
                tar.add(source, arcname=str(PurePosixPath("data", *rel.parts)))
                files += 1
                try:
                    raw_bytes += source.stat().st_size
                except OSError:
                    pass

    result: Dict[str, Any] = {
        "ok": True,
        "path": str(out),
        "name": out.name,
        "files": files,
        "uncompressed_bytes": raw_bytes,
        "bytes": out.stat().st_size,
        "seconds": round(time.time() - started, 2),
        "included_research": include_research,
        "included_attachments": include_attachments,
    }
    if verify:
        result["verified"] = verify_archive(out)
        result["ok"] = bool(result["verified"].get("ok"))
    if keep:
        result["pruned"] = prune(keep)
    logger.info("[backup] %s files=%s bytes=%s ok=%s", out.name, files,
                result["bytes"], result["ok"])
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


def verify_archive(path: Any, *, check_databases: bool = True) -> Dict[str, Any]:
    """Open the archive, validate every member, integrity-check the databases.

    Only `*.db` members are extracted (to a temp dir); the rest is walked, so
    verifying a multi-gigabyte gallery costs almost nothing.
    """
    p = Path(path)
    out: Dict[str, Any] = {"ok": False, "path": str(p), "members": 0,
                           "databases": [], "problems": []}
    if not p.is_file():
        out["problems"].append("no such backup file")
        return out
    try:
        with tarfile.open(p, "r:gz") as tar:
            members = tar.getmembers()
            out["members"] = len(members)
            for member in members:
                problem = _member_problem(member)
                if problem:
                    out["problems"].append(problem)
            if out["problems"]:
                return out
            if check_databases:
                dbs = [m for m in members if m.isfile() and m.name.endswith(".db")]
                with tempfile.TemporaryDirectory() as tmp_str:
                    tmp = Path(tmp_str)
                    for member in dbs:
                        target = tmp / PurePosixPath(member.name).name
                        src = tar.extractfile(member)
                        if src is None:
                            out["problems"].append(f"unreadable member: {member.name}")
                            continue
                        with open(target, "wb") as fh:
                            fh.write(src.read())
                        out["databases"].append(_integrity_check(target, member.name))
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
    return p.is_file() and p.name.endswith(SUFFIX) and (
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
            Path(entry["path"]).unlink()
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
    """The exact manual step. Restore is destructive and needs the app stopped."""
    return (f"Stop Faustus, then run:  python scripts/odysseus-backup restore "
            f"\"{backup_dir() / name}\" --yes")


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
    return snapshot(
        include_research=bool(get_setting_fn("backup_include_research", False)),
        keep=keep,
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
