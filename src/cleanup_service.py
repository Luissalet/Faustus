# src/cleanup_service.py
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Any, List, Optional

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC for this module's DB-bound timestamps.

    Mirrors the naive DateTime columns these values are compared against,
    without the deprecated stdlib UTC-now call (removed in Python 3.14).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CleanupConfig:
    """Configuration constants for cleanup operations."""
    ARCHIVE_AFTER_DAYS = 7
    DELETE_AFTER_DAYS = 14
    MIN_MESSAGES_TO_KEEP = 20
    PRESERVE_RECENT_COUNT = 10
    PROTECTED_KEYWORDS = ['important', 'remember', 'save this', 'keep', 'bookmark']
    ESTIMATED_MESSAGE_SIZE_BYTES = 512


def _apply_owner_filter(query, DbSession, owner: Optional[str]):
    """Apply owner filtering to a session query.

    SECURITY: strict — the previous OR predicate let one user's cleanup
    archive/delete every null-owner session, including ones that hadn't
    been migrated. Now: only rows owned by this user.
    """
    if owner is None:
        return query
    return query.filter(DbSession.owner == owner)


async def archive_inactive_sessions(session_manager, owner: Optional[str] = None) -> int:
    """
    Archive sessions that haven't been accessed in the configured number of days.

    Args:
        session_manager: The session manager instance
        owner: If set, only archive this user's sessions

    Returns:
        Number of sessions archived
    """
    cutoff_date = _utcnow() - timedelta(days=CleanupConfig.ARCHIVE_AFTER_DAYS)
    archived_count = 0

    from src.database import SessionLocal, Session as DbSession
    db = SessionLocal()
    try:
        q = db.query(DbSession).filter(
            DbSession.last_accessed < cutoff_date,
            DbSession.archived == False
        )
        q = _apply_owner_filter(q, DbSession, owner)
        sessions_to_archive = q.all()

        for session in sessions_to_archive:
            session.archived = True
            session.updated_at = _utcnow()
            archived_count += 1

        if archived_count > 0:
            db.commit()
            logger.info(f"Archived {archived_count} inactive sessions")

    except Exception as e:
        logger.error(f"Error archiving sessions: {e}")
        db.rollback()
    finally:
        db.close()

    return archived_count

async def cleanup_old_sessions(session_manager, owner: Optional[str] = None) -> Tuple[int, float]:
    """
    Delete old sessions based on specific criteria.

    Args:
        session_manager: The session manager instance
        owner: If set, only clean up this user's sessions

    Returns:
        Tuple of (number of sessions deleted, space freed in MB)
    """
    cutoff_date = _utcnow() - timedelta(days=CleanupConfig.DELETE_AFTER_DAYS)
    deleted_count = 0
    space_freed = 0

    from src.database import SessionLocal, Session as DbSession, ChatMessage as DbChatMessage
    db = SessionLocal()
    try:
        recent_q = db.query(DbSession).order_by(DbSession.created_at.desc())
        recent_q = _apply_owner_filter(recent_q, DbSession, owner)
        all_sessions = recent_q.all()
        recent_session_ids = {session.id for session in all_sessions[:CleanupConfig.PRESERVE_RECENT_COUNT]}

        base_query = db.query(DbSession).filter(
            DbSession.archived == True,
            DbSession.last_accessed < cutoff_date,
            DbSession.is_important == False,
            DbSession.message_count < CleanupConfig.MIN_MESSAGES_TO_KEEP
        )
        base_query = _apply_owner_filter(base_query, DbSession, owner)

        candidate_sessions = base_query.all()
        sessions_to_delete = []
        preserved_count = 0

        for session in candidate_sessions:
            if session.id in recent_session_ids:
                preserved_count += 1
                continue

            if session.message_count >= CleanupConfig.MIN_MESSAGES_TO_KEEP:
                preserved_count += 1
                continue

            session_name_lower = session.name.lower() if session.name else ""
            if any(keyword in session_name_lower for keyword in CleanupConfig.PROTECTED_KEYWORDS):
                preserved_count += 1
                continue

            sessions_to_delete.append(session)

        for session in sessions_to_delete:
            message_count = db.query(DbChatMessage).filter(
                DbChatMessage.session_id == session.id
            ).count()
            space_freed += message_count * CleanupConfig.ESTIMATED_MESSAGE_SIZE_BYTES

        session_ids = [session.id for session in sessions_to_delete]
        if session_ids:
            db.query(DbSession).filter(DbSession.id.in_(session_ids)).delete(synchronize_session=False)
            deleted_count = len(session_ids)
            db.commit()

            for session_id in session_ids:
                if session_id in session_manager.sessions:
                    del session_manager.sessions[session_id]

        if deleted_count > 0:
            space_freed_mb = space_freed / (1024 * 1024)
            logger.info(f"Deleted {deleted_count} old sessions, freeing approximately {space_freed_mb:.2f} MB")
            return deleted_count, space_freed_mb

    except Exception as e:
        logger.error(f"Error cleaning up old sessions: {e}")
        db.rollback()
    finally:
        db.close()

    return deleted_count, 0.0

async def get_cleanup_preview(owner: Optional[str] = None) -> Dict[str, Any]:
    """
    Get a preview of what would be cleaned up without making changes.

    Args:
        owner: If set, only preview this user's sessions

    Returns:
        Dictionary containing preview information
    """
    cutoff_archive = _utcnow() - timedelta(days=CleanupConfig.ARCHIVE_AFTER_DAYS)
    cutoff_delete = _utcnow() - timedelta(days=CleanupConfig.DELETE_AFTER_DAYS)

    sessions_to_archive = []
    sessions_to_delete = []
    estimated_space_freed = 0
    preserved_sessions = []

    from src.database import SessionLocal, Session as DbSession
    db = SessionLocal()
    try:
        archive_q = db.query(DbSession).filter(
            DbSession.last_accessed < cutoff_archive,
            DbSession.archived == False
        )
        archive_q = _apply_owner_filter(archive_q, DbSession, owner)
        archive_candidates = archive_q.all()

        for session in archive_candidates:
            sessions_to_archive.append({
                "id": session.id,
                "name": session.name,
                "last_accessed": session.last_accessed.isoformat() if session.last_accessed else "Unknown",
                "message_count": session.message_count
            })

        recent_q = db.query(DbSession).order_by(DbSession.created_at.desc())
        recent_q = _apply_owner_filter(recent_q, DbSession, owner)
        all_sessions = recent_q.all()
        recent_session_ids = {session.id for session in all_sessions[:CleanupConfig.PRESERVE_RECENT_COUNT]}

        base_query = db.query(DbSession).filter(
            DbSession.archived == True,
            DbSession.last_accessed < cutoff_delete,
            DbSession.is_important == False,
            DbSession.message_count < CleanupConfig.MIN_MESSAGES_TO_KEEP
        )
        base_query = _apply_owner_filter(base_query, DbSession, owner)

        candidate_sessions = base_query.all()

        for session in candidate_sessions:
            if session.id in recent_session_ids:
                preserved_sessions.append({
                    "id": session.id,
                    "name": session.name,
                    "reason": f"part of last {CleanupConfig.PRESERVE_RECENT_COUNT} sessions",
                    "last_accessed": session.last_accessed.isoformat() if session.last_accessed else "Unknown",
                    "message_count": session.message_count
                })
                continue

            if session.message_count >= CleanupConfig.MIN_MESSAGES_TO_KEEP:
                preserved_sessions.append({
                    "id": session.id,
                    "name": session.name,
                    "reason": f"has {CleanupConfig.MIN_MESSAGES_TO_KEEP}+ messages",
                    "last_accessed": session.last_accessed.isoformat() if session.last_accessed else "Unknown",
                    "message_count": session.message_count
                })
                continue

            session_name_lower = session.name.lower() if session.name else ""
            matching_keywords = [keyword for keyword in CleanupConfig.PROTECTED_KEYWORDS if keyword in session_name_lower]
            if matching_keywords:
                preserved_sessions.append({
                    "id": session.id,
                    "name": session.name,
                    "reason": f"contains keyword: {matching_keywords[0]}",
                    "last_accessed": session.last_accessed.isoformat() if session.last_accessed else "Unknown",
                    "message_count": session.message_count
                })
                continue

            session_space = session.message_count * CleanupConfig.ESTIMATED_MESSAGE_SIZE_BYTES
            estimated_space_freed += session_space

            sessions_to_delete.append({
                "id": session.id,
                "name": session.name,
                "last_accessed": session.last_accessed.isoformat() if session.last_accessed else "Unknown",
                "message_count": session.message_count,
                "estimated_size_kb": round(session_space / 1024, 2)
            })

    except Exception as e:
        logger.error(f"Error generating cleanup preview: {e}")
    finally:
        db.close()

    return {
        "sessions_to_archive": sessions_to_archive,
        "sessions_to_delete": sessions_to_delete,
        "preserved_sessions": preserved_sessions,
        "estimated_space_freed_mb": round(estimated_space_freed / (1024 * 1024), 2)
    }

async def cleanup_sessions(session_manager, owner: Optional[str] = None) -> Tuple[int, int, float]:
    """
    Perform complete cleanup operations with error recovery.

    Args:
        session_manager: The session manager instance
        owner: If set, only clean up this user's sessions

    Returns:
        Tuple of (archived_count, deleted_count, space_freed_mb)
    """
    archived_count = 0
    deleted_count = 0
    space_freed_mb = 0.0

    try:
        archived_count = await archive_inactive_sessions(session_manager, owner=owner)
    except Exception as e:
        logger.error(f"Archive operation failed: {e}")

    try:
        deleted_count, space_freed_mb = await cleanup_old_sessions(session_manager, owner=owner)
    except Exception as e:
        logger.error(f"Delete operation failed: {e}")

    return archived_count, deleted_count, space_freed_mb


# ---------------------------------------------------------------------------
# OPS-07: temp/cache/blob hygiene, a trash mechanism (not hard-delete),
# quotas, orphan detection, and an honest space/remote-cost report.
#
# Nothing below builds a second store for something that already has one:
# chunked-upload orphan sweeping reuses `UploadHandler.
# cleanup_expired_chunked_sessions` (src/upload_handler.py, PERF-05) rather
# than re-walking `.chunked/` a second way; artifact usage is read straight
# off `src.constants.ARTIFACT_STORE_DIR`; session archive/delete counts
# reuse `get_cleanup_preview` above, not a second DB scan; the scorecard
# log path is `src.scorecard._path()`, not a re-guessed location.
# ---------------------------------------------------------------------------

TRASH_DIRNAME = ".trash"
DEFAULT_TRASH_RETENTION_DAYS = 30.0


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return DATA_DIR
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data")


def _trash_root() -> str:
    path = os.path.join(_data_dir(), TRASH_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def move_to_trash(path: str, *, category: str = "misc") -> Optional[str]:
    """Move a file or directory into ``DATA_DIR/.trash/<category>/`` instead
    of deleting it outright — every OPS-07 removal in this module goes
    through here, so "freed" means "recoverable for
    DEFAULT_TRASH_RETENTION_DAYS", never gone the instant a preview looked
    right. Returns the trash destination, or None if `path` did not exist.
    """
    if not path or not os.path.exists(path):
        return None
    bucket = os.path.join(_trash_root(), category)
    os.makedirs(bucket, exist_ok=True)
    stamp = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    dest = os.path.join(bucket, f"{stamp}__{os.path.basename(os.path.normpath(path))}")
    shutil.move(path, dest)
    return dest


def purge_trash(*, older_than_days: float = DEFAULT_TRASH_RETENTION_DAYS) -> Dict[str, Any]:
    """Permanently remove trash entries past retention. This IS a real
    delete — but only of things this module itself already moved into
    `.trash/`, and only once they have sat there past the retention window,
    never at `move_to_trash()` time."""
    root = _trash_root()
    cutoff = time.time() - (older_than_days * 86400)
    removed: List[str] = []
    freed_bytes = 0
    try:
        categories = sorted(os.listdir(root))
    except OSError:
        categories = []
    for category in categories:
        cat_dir = os.path.join(root, category)
        if not os.path.isdir(cat_dir):
            continue
        for name in sorted(os.listdir(cat_dir)):
            entry = os.path.join(cat_dir, name)
            try:
                mtime = os.path.getmtime(entry)
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            freed_bytes += _dir_size(entry) if os.path.isdir(entry) else os.path.getsize(entry)
            try:
                if os.path.isdir(entry):
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    os.remove(entry)
            except OSError:
                continue
            removed.append(f"{category}/{name}")
    return {"removed": removed, "freed_bytes": freed_bytes}


def sweep_upload_orphans(upload_handler, *, max_age_seconds: int = 24 * 3600) -> int:
    """Names the PERF-05 chunked-session sweep as part of OPS-07's hygiene
    surface, reusing `UploadHandler.cleanup_expired_chunked_sessions` rather
    than re-implementing the walk."""
    return upload_handler.cleanup_expired_chunked_sessions(max_age_seconds=max_age_seconds)


def enforce_blob_quota(evictable: List[Tuple[str, str, float]], *, quota_bytes: int,
                        current_total_bytes: int, dry_run: bool = True) -> Dict[str, Any]:
    """Bring `current_total_bytes` under `quota_bytes` by trashing entries
    from `evictable` — oldest `mtime` first — and ONLY from `evictable`.

    `evictable` is a list of `(path, category, mtime)` the CALLER has
    already determined are safe to remove (e.g. blobs with no surviving
    reference in the manifest/DB). This function never discovers
    "unreferenced" on its own and never touches a path outside that list —
    the guarantee behind OPS-07's acceptance criterion ("no borrar lo
    referenciado") is structural here, not a heuristic this function could
    get wrong. Nothing evicted is deleted outright; `move_to_trash` is used,
    so an eviction is recoverable until `purge_trash` age it out.
    """
    if current_total_bytes <= quota_bytes:
        return {"evicted": [], "freed_bytes": 0, "remaining_bytes": current_total_bytes}
    ordered = sorted(evictable, key=lambda e: e[2])
    evicted: List[Dict[str, Any]] = []
    freed = 0
    remaining = current_total_bytes
    for path, category, _mtime in ordered:
        if remaining <= quota_bytes:
            break
        try:
            size = _dir_size(path) if os.path.isdir(path) else os.path.getsize(path)
        except OSError:
            continue
        if not dry_run:
            move_to_trash(path, category=category)
        evicted.append({"path": path, "bytes": size})
        freed += size
        remaining -= size
    return {"evicted": evicted, "freed_bytes": freed, "remaining_bytes": remaining}


def runs_log_retention(*, older_than_days: float = 60.0, dry_run: bool = True) -> Dict[str, Any]:
    """Trash (never hard-delete) `DATA_DIR/runs/*.jsonl` replay logs older
    than `older_than_days`. Reuses the same `runs/` directory OBS-04's
    support bundle already reads (`src.support_bundle.recent_events`) —
    not a second log root."""
    runs_dir = os.path.join(_data_dir(), "runs")
    cutoff = time.time() - (older_than_days * 86400)
    try:
        names = sorted(os.listdir(runs_dir))
    except OSError:
        names = []
    trashed: List[str] = []
    freed_bytes = 0
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(runs_dir, name)
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if not dry_run:
            move_to_trash(path, category="runs")
        trashed.append(name)
        freed_bytes += size
    return {"trashed": trashed, "freed_bytes": freed_bytes, "dry_run": dry_run}


def artifact_store_usage(*, store_dir: Optional[str] = None) -> Dict[str, Any]:
    """Real, measured size of the content-addressed artifact store
    (`src.artifact_store`) — never estimated."""
    from src.constants import ARTIFACT_STORE_DIR
    store = store_dir or ARTIFACT_STORE_DIR
    if not store or not os.path.isdir(store):
        return {"path": store, "exists": False, "bytes": 0, "file_count": 0}
    total = 0
    count = 0
    for root, _dirs, files in os.walk(store, followlinks=False):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                continue
    return {"path": store, "exists": True, "bytes": total, "file_count": count}


def scorecard_log_usage() -> Dict[str, Any]:
    """Measured size of OBS-05's scorecard log (`src.scorecard._path()`) —
    reused path, not a re-guessed one."""
    from src.scorecard import _path as scorecard_path
    path = scorecard_path()
    if not os.path.isfile(path):
        return {"path": path, "bytes": 0}
    return {"path": path, "bytes": os.path.getsize(path)}


def trash_usage() -> Dict[str, Any]:
    root = _trash_root()
    return {"path": root, "bytes": _dir_size(root)}


async def storage_report(owner: Optional[str] = None) -> Dict[str, Any]:
    """One dict a future storage panel can render as-is: real, measured
    usage by category — never a placeholder number. Reuses
    `get_cleanup_preview` for the session half rather than re-scanning."""
    preview = await get_cleanup_preview(owner=owner)
    return {
        "sessions": {
            "archivable": len(preview["sessions_to_archive"]),
            "deletable": len(preview["sessions_to_delete"]),
            "estimated_deletable_mb": preview["estimated_space_freed_mb"],
        },
        "artifact_store": artifact_store_usage(),
        "scorecard_log": scorecard_log_usage(),
        "trash": trash_usage(),
    }


# --- Remote cost: measured only, never estimated as zero when unknown ----

_COST_EVENT_TYPES = ("run_cost", "external_worker_result")
_COST_FIELD_NAMES = ("total_cost_usd", "cost_usd")


def remote_cost_report(*, since: Optional[float] = None, max_files: int = 60) -> Dict[str, Any]:
    """Sum whatever measured remote-run cost events exist under
    `DATA_DIR/runs/*.jsonl` (the same replay log OBS-04 reads) in the
    period.

    KNOWN GAP, stated plainly rather than glossed over: as of this lot, no
    writer actually appends a cost event to that log —
    `src.external_worker.run_task()` computes `total_cost_usd` from the
    CLI's own final event and hands it back to its caller (see that
    module's `_reconcile`/result construction), but nothing persists it
    anywhere this function — or a person — can read afterwards. So today
    this always returns `known_total_usd=None, unknown_period=True`, which
    is the correct, honest answer to "what did remote runs cost this
    period" — never a silent 0 standing in for missing data (this is
    OPS-07's acceptance criterion). The moment a caller starts writing a
    `{"event": "run_cost", "total_cost_usd": ...}` line into that log, this
    function picks it up with no further change needed here. See this lot's
    report for the exact file/lines a future change would add that write.
    """
    runs_dir = os.path.join(_data_dir(), "runs")
    try:
        names = sorted(os.listdir(runs_dir))
    except OSError:
        names = []
    names = [n for n in names if n.endswith(".jsonl")][-max_files:]

    total = 0.0
    found_any = False
    unknown_cost_events = 0
    for name in names:
        path = os.path.join(runs_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("event") not in _COST_EVENT_TYPES:
                continue
            ts = event.get("ts") or event.get("timestamp")
            if since is not None and isinstance(ts, (int, float)) and ts < since:
                continue
            cost = next((event[f] for f in _COST_FIELD_NAMES if event.get(f) is not None), None)
            if isinstance(cost, (int, float)):
                total += float(cost)
                found_any = True
            else:
                unknown_cost_events += 1

    if unknown_cost_events > 0 or not found_any:
        return {
            "known_total_usd": round(total, 6) if found_any else None,
            "unknown_period": True,
            "unknown_cost_events": unknown_cost_events,
            "note": ("remote run cost is not yet logged by any writer in this build; "
                     "see this lot's report for the exact gap"),
        }
    return {"known_total_usd": round(total, 6), "unknown_period": False, "unknown_cost_events": 0}
