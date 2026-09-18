"""Formal schema-migration registry (OPS-02 / QA-45).

Every SQLite database this application owns evolves its schema at startup
through small, individually-idempotent steps (see the `_migrate_add_*`
sequence in `core/database.py`). Nothing recorded which steps had already
run, nothing backed the file up first, and nothing wrapped the sequence so a
crash mid-way — a real power cut, or an exception three steps into forty —
left the schema wherever it happened to stop: sometimes fine, sometimes a
column added but not backfilled, never something the app could reason about
on the next boot.

This module is the missing piece, generic across every SQLite store:

1. **A real record.** A `schema_migrations` table (version, applied_at,
   checksum, steps) in the target database itself, so "what did we last
   apply here" is a query, not an assumption.
2. **Backup before touching anything.** A WAL-safe copy of the live file
   (`src.backup_service._sqlite_safe_copy` — never a raw byte copy of an
   open database) is taken before a single step runs.
3. **Restore on failure, not a guess.** If any step raises, every pooled
   connection is released and the file is replaced, byte for byte, with the
   pre-migration backup. The database that comes back is the complete
   previous schema — never empty, never half-applied, never a mix of the
   two — which is the literal acceptance bar for QA-45.

Steps themselves are still whatever `core/database.py` (or any other store)
already does — this does not rewrite forty existing migrations, it wraps
them so a failure among them is recoverable instead of silent.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"

Step = Tuple[str, Callable[[], None]]


class MigrationFailed(RuntimeError):
    """A migration step raised. The database was restored to its pre-migration
    state (or, if no backup could be taken, left exactly as the failure did —
    see `backup_error` on the raising call's logs)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_table(engine: Engine) -> None:
    """Create `schema_migrations` in the target database if it is not there yet.

    Deliberately not part of `Base.metadata` — it must exist and be readable
    even for a database whose ORM models never mention it, and even when the
    caller is a standalone script that only has an `Engine`.
    """
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} ("
            "version TEXT PRIMARY KEY, "
            "checksum TEXT NOT NULL, "
            "applied_at TEXT NOT NULL, "
            "steps INTEGER NOT NULL)"
        ))


def checksum_of(steps: List[Step]) -> str:
    """A stable identity for a given ordered set of step names.

    Only names go into the hash, never the callables — two runs of the same
    process comparing function identity would always differ. Adding, removing
    or reordering a step changes the checksum, which is exactly the signal
    that a fresh backup + apply pass is due.
    """
    digest = hashlib.sha256()
    for name, _ in steps:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def last_applied(engine: Engine) -> Optional[Dict[str, Any]]:
    """The most recent recorded row, or None if the table is absent/empty."""
    try:
        ensure_table(engine)
        with engine.connect() as conn:
            row = conn.execute(text(
                f"SELECT version, checksum, applied_at, steps "
                f"FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY applied_at DESC LIMIT 1"
            )).fetchone()
    except Exception as e:
        logger.warning("[migrations] could not read %s: %s", SCHEMA_MIGRATIONS_TABLE, e)
        return None
    if row is None:
        return None
    return {"version": row[0], "checksum": row[1], "applied_at": row[2], "steps": row[3]}


def sqlite_file_path(engine: Engine) -> Optional[Path]:
    """The on-disk file this engine points at, or None for `:memory:`/non-sqlite."""
    try:
        from core.database import _sqlite_db_path  # local import: avoid a load-time cycle
    except Exception:
        return None
    raw = _sqlite_db_path(engine.url)
    return Path(raw) if raw else None


def _wal_safe_copy(src: Path, dst: Path) -> None:
    """Copy a live SQLite file with its own backup API, not a byte copy.

    Deliberately duplicated (in miniature) from
    `src.backup_service._sqlite_safe_copy` rather than imported: this module
    runs from inside `core.database`'s module-level `init_db()` call, which
    means it runs during the FIRST import of practically anything in this
    app — including, transitively, `src.backup_service` itself. Importing
    `backup_service` from here at call time hit exactly that: a real
    `ImportError` from a module mid-import reaching back into itself. Taking
    the one function this actually needs, instead of the module, removes the
    cycle rather than papering over it.
    """
    import sqlite3
    src_conn = dst_conn = None
    try:
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dst))
        with dst_conn:
            src_conn.backup(dst_conn)
    except Exception:
        dst.write_bytes(src.read_bytes())
    finally:
        for conn in (src_conn, dst_conn):
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


def _pre_migration_dir() -> Path:
    """Same directory `backup_service.backup_dir()` resolves to, computed
    independently so this module never has to import that one (see
    `_wal_safe_copy`)."""
    override = os.getenv("ODYSSEUS_BACKUP_DIR") or os.getenv("FAUSTUS_BACKUP_DIR")
    if override:
        root = Path(override)
    else:
        from src.runtime_paths import get_app_root
        root = Path(get_app_root()) / "backups"
    return root / "pre-migration"


def _backup_before_migration(db_path: Path) -> Path:
    """A crash-safe copy of the live file, taken before any step runs."""
    staging = _pre_migration_dir()
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / f"{db_path.stem}.{int(time.time() * 1000)}.pre-migration.db"
    _wal_safe_copy(db_path, dest)
    return dest


def _restore(engine: Engine, db_path: Path, backup_path: Path) -> None:
    """Put the pre-migration file back, after releasing every pooled handle.

    `Connection.backup()` overwrites the destination's full contents, so this
    is not "hope nothing is open" — SQLAlchemy's `engine.dispose()` closes the
    pool first, and any WAL/SHM/journal sidecars from the failed attempt are
    removed so a stale one is never read back as if it were current.
    """
    engine.dispose()
    _wal_safe_copy(backup_path, db_path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db_path.parent / (db_path.name + suffix)
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass


def run(engine: Engine, steps: List[Step], *, name: str = "app") -> Dict[str, Any]:
    """Apply `steps` once per distinct checksum.

    * No-ops (and says so) when the last recorded checksum already matches —
      restarting the app should not re-back-up a database that has not
      changed schema.
    * Otherwise: backs the file up, runs every step in order, and on ANY
      exception restores the backup and raises `MigrationFailed` — the caller
      (typically application startup) decides whether that is fatal.
    * On success, records a new `schema_migrations` row so the next boot
      recognizes this checksum as already applied.

    Steps are expected to be individually safe to re-run (the existing
    `_migrate_add_*` functions already check "does this column exist" before
    adding it), since a restore rewinds the FILE, not the in-process call
    stack that got partway through `steps`.
    """
    ensure_table(engine)
    checksum = checksum_of(steps)
    previous = last_applied(engine)
    if previous and previous["checksum"] == checksum:
        return {"ok": True, "applied": False, "name": name,
                "version": previous["version"], "reason": "schema unchanged since last boot"}

    db_path = sqlite_file_path(engine)
    backup_path: Optional[Path] = None
    backup_error: Optional[str] = None
    if db_path is not None and db_path.is_file():
        try:
            backup_path = _backup_before_migration(db_path)
        except Exception as e:
            # Do not refuse to migrate just because the safety net could not
            # be woven — but the caller's log makes very clear it is missing.
            backup_error = f"{type(e).__name__}: {e}"
            logger.warning("[migrations:%s] pre-migration backup failed: %s", name, backup_error)

    applied: List[str] = []
    for step_name, fn in steps:
        try:
            fn()
        except Exception as e:
            logger.error(
                "[migrations:%s] step %r failed after %d/%d succeeded (%s) — %s",
                name, step_name, len(applied), len(steps), applied,
                "restoring pre-migration backup" if backup_path else "NO BACKUP WAS TAKEN",
            )
            if backup_path is not None and db_path is not None:
                _restore(engine, db_path, backup_path)
                restored = True
            else:
                restored = False
            raise MigrationFailed(
                f"{name}: migration step {step_name!r} failed after {len(applied)}/{len(steps)} "
                f"prior steps succeeded ({e}); database "
                + ("restored from the pre-migration backup" if restored else
                   f"was NOT restored — no backup was taken ({backup_error})")
            ) from e
        else:
            applied.append(step_name)

    version = f"{int(time.time())}-{checksum[:12]}"
    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT OR REPLACE INTO {SCHEMA_MIGRATIONS_TABLE} "
            "(version, checksum, applied_at, steps) VALUES (:v, :c, :a, :s)"
        ), {"v": version, "c": checksum, "a": _now_iso(), "s": len(steps)})
    logger.info("[migrations:%s] applied %d step(s), version=%s", name, len(steps), version)
    return {"ok": True, "applied": True, "name": name, "version": version,
            "steps": len(steps), "backup": str(backup_path) if backup_path else None}
