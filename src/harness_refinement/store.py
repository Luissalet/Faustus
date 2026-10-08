"""Harness proposals: the store, the status machine and the trigger -> result log.

A harness proposal is ONE small edit to the state that surrounds the immutable
base prompt: the project's standing instructions (``prompt_layer``), a skill
(``skill``), a memory entry (``memory``) or a user sub-agent specification
(``subagent_spec``). Everything here is generic over the axis: the axis-specific
reads and writes live in :mod:`src.harness_refinement.targets`, the model call
in :mod:`src.harness_refinement.proposer`.

The two rules this module exists to enforce:

* **A proposal is inert until a person approves it.** :func:`create_proposal`
  only ever writes a ``pending`` record; nothing in this module's creation path
  touches a target. :func:`approve` is the single place a target changes, and
  its callers are the REST routes behind ``require_human``.
* **Every applied edit can be undone by its id, exactly.** :func:`approve`
  records the content the target had BEFORE and the content it has AFTER the
  write (read back, not assumed: a store may normalise what it is given).
  :func:`undo` restores ``before`` byte for byte, and refuses with a stable
  error class when the target no longer holds the content this edit left
  there, because writing ``before`` back would silently destroy somebody
  else's later change.

Record
------
::

    {id, axis, target, op, before, after, diff, rationale, evidence, trigger,
     created_at, status, applied_at, undo_of, ...}

``status`` is ``pending | applied | rejected | undone``. An undo is itself a
record (``undo_of`` = the id it reverses, ``status`` ``applied``) so the history
reads as one list; the record it reverses becomes ``undone``.

Storage is one JSON file plus an append-only JSONL log under
``DATA_DIR/harness_proposals/``; both are written atomically and every
read-modify-write holds a cross-process file lock.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from difflib import unified_diff
from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.atomic_io import atomic_write_json
from core.file_lock import FileLock, LockTimeout
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

AXES = ("prompt_layer", "skill", "memory", "subagent_spec")
OPS = ("create", "update", "delete")
STATUSES = ("pending", "applied", "rejected", "undone")

#: Cap on one side of a record (a SKILL.md can be tens of KB; nothing else is).
MAX_CONTENT_CHARS = 64_000
MAX_LOG_LINES = 5_000
_ROTATE_KEEP = 2_500

_LOCK = threading.RLock()


class HarnessError(Exception):
    """A refusal with a stable ``error_class`` the API and the UI can branch on."""

    #: HTTP status each class maps to (routes read this; unknown -> 400).
    STATUS = {
        "harness.not_found": 404,
        "harness.not_pending": 409,
        "harness.target_changed": 409,
        "harness.target_changed_since_apply": 409,
        "harness.not_applied": 409,
        "harness.undo_of_undo": 409,
        "harness.busy": 409,
        "harness.target_missing": 404,
    }

    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class
        self.message = message

    @property
    def status_code(self) -> int:
        return self.STATUS.get(self.error_class, 400)

    def to_dict(self) -> Dict[str, str]:
        return {"error_class": self.error_class, "message": self.message}


# -- paths -----------------------------------------------------------------

def root() -> str:
    return os.path.join(DATA_DIR, "harness_proposals")


def proposals_path() -> str:
    return os.path.join(root(), "proposals.json")


def log_path() -> str:
    return os.path.join(root(), "log.jsonl")


def state_path() -> str:
    return os.path.join(root(), "state.json")


def _lock_path() -> str:
    return os.path.join(root(), "store.lock")


def _ensure_root() -> None:
    os.makedirs(root(), exist_ok=True)


class _Locked:
    """Process lock + cross-process file lock around a read-modify-write."""

    def __enter__(self):
        _LOCK.acquire()
        try:
            _ensure_root()
            self._file = FileLock(_lock_path(), timeout=15.0, stale_after=60.0)
            self._file.__enter__()
        except LockTimeout as exc:
            _LOCK.release()
            raise HarnessError("harness.busy", "another process is writing the harness proposals") from exc
        except BaseException:
            _LOCK.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._file.__exit__(*exc)
        finally:
            _LOCK.release()
        return False


def locked() -> _Locked:
    return _Locked()


# -- raw load / save -------------------------------------------------------

def _load_all() -> Dict[str, Dict[str, Any]]:
    try:
        with open(proposals_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("harness proposals file unreadable: %s", proposals_path(), exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(records: Mapping[str, Mapping[str, Any]]) -> None:
    _ensure_root()
    atomic_write_json(proposals_path(), dict(records), indent=2)


def load_state() -> Dict[str, Any]:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Mapping[str, Any]) -> None:
    _ensure_root()
    atomic_write_json(state_path(), dict(state), indent=2)


def mark_processed(session_id: str, ts: float) -> None:
    """Remember how far into a session the proposer has already looked, so the
    same correction never produces a second proposal on the next turn."""
    if not session_id:
        return
    with locked():
        state = load_state()
        seen = state.setdefault("processed", {})
        seen[session_id] = max(float(seen.get(session_id) or 0.0), float(ts or 0.0))
        if len(seen) > 2000:
            for key in sorted(seen, key=seen.get)[:500]:
                seen.pop(key, None)
        save_state(state)


def processed_until(session_id: str) -> float:
    try:
        return float((load_state().get("processed") or {}).get(session_id) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# -- the log: trigger -> result -------------------------------------------

def log_event(event: str, *, trigger: str = "", result: str = "", proposal_id: str = "",
              session_id: str = "", owner: str = "", detail: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Append one ``trigger -> result`` line. Never raises: a log that cannot
    be written must not be the reason a proposal or an undo fails."""
    entry = {
        "ts": time.time(), "event": event, "trigger": trigger, "result": result,
        "proposal_id": proposal_id, "session_id": session_id, "owner": owner or "",
        "detail": dict(detail or {}),
    }
    try:
        _ensure_root()
        with _LOCK:
            with open(log_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _rotate_log()
    except OSError:
        logger.debug("harness log write failed", exc_info=True)
    return entry


def _rotate_log() -> None:
    try:
        if os.path.getsize(log_path()) < 2_000_000:
            return
        with open(log_path(), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= MAX_LOG_LINES:
            return
        tmp = log_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-_ROTATE_KEEP:])
        os.replace(tmp, log_path())
    except OSError:
        pass


def read_log(*, limit: int = 100, proposal_id: str = "", owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first."""
    out: List[Dict[str, Any]] = []
    try:
        with open(log_path(), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if proposal_id and entry.get("proposal_id") != proposal_id:
            continue
        if owner and entry.get("owner") not in ("", owner):
            continue
        out.append(entry)
        if len(out) >= max(1, int(limit)):
            break
    return out


# -- records ---------------------------------------------------------------

def make_diff(before: Optional[str], after: Optional[str], name: str = "") -> str:
    label = name or "target"
    # One line per entry even when the text has no final newline: a diff whose
    # last "-" line runs into the first "+" line cannot be read or rendered.
    lines = unified_diff(
        (before or "").splitlines(), (after or "").splitlines(),
        fromfile=f"{label}@current", tofile=f"{label}@proposed", lineterm="",
    )
    text = "\n".join(lines)
    return text + "\n" if text else ""


def changed_lines(before: Optional[str], after: Optional[str]) -> int:
    """Added + removed lines of the edit: the size measure for "smallest edit"."""
    n = 0
    for line in unified_diff((before or "").splitlines(), (after or "").splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith(("+", "-")):
            n += 1
    return n


def new_id() -> str:
    return uuid.uuid4().hex


def create_proposal(*, axis: str, target: str, op: str, before: Optional[str], after: Optional[str],
                    rationale: str, evidence: Iterable[Mapping[str, Any]], trigger: str,
                    owner: str = "", session_id: str = "", model: str = "",
                    meta: Optional[Mapping[str, Any]] = None, proposal_id: Optional[str] = None,
                    risk_flags: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Store a PENDING proposal. This never reads or writes the target."""
    if axis not in AXES:
        raise HarnessError("harness.bad_axis", f"unknown axis {axis!r}")
    if op not in OPS:
        raise HarnessError("harness.bad_op", f"unknown op {op!r}")
    for side in (before, after):
        if side is not None and len(side) > MAX_CONTENT_CHARS:
            raise HarnessError("harness.too_large", f"content exceeds {MAX_CONTENT_CHARS} characters")
    record = {
        "id": proposal_id or new_id(),
        "axis": axis, "target": target, "op": op,
        "before": before, "after": after,
        "diff": make_diff(before, after, target),
        "rationale": str(rationale or "")[:2000],
        "evidence": [dict(e) for e in evidence][:12],
        "trigger": trigger, "created_at": time.time(),
        "status": "pending", "applied_at": None, "undo_of": None,
        "owner": owner or "", "session_id": session_id or "", "model": model or "",
        "risk_flags": list(risk_flags or []),
        "meta": dict(meta or {}),
    }
    with locked():
        records = _load_all()
        records[record["id"]] = record
        _save_all(records)
    log_event("proposed", trigger=trigger, result=f"{axis}:{op} {target}", proposal_id=record["id"],
              session_id=session_id, owner=owner, detail={"model": model})
    return record


def get(proposal_id: str) -> Optional[Dict[str, Any]]:
    return _load_all().get(str(proposal_id or ""))


def list_proposals(*, status: Optional[str] = None, axis: Optional[str] = None,
                   session_id: Optional[str] = None, owner: Optional[str] = None,
                   limit: int = 200) -> List[Dict[str, Any]]:
    out = []
    for rec in _load_all().values():
        if status and rec.get("status") != status:
            continue
        if axis and rec.get("axis") != axis:
            continue
        if session_id and rec.get("session_id") != session_id:
            continue
        if owner and rec.get("owner") not in ("", owner):
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out[:max(1, int(limit))]


def pending_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    for rec in list_proposals(status="pending", session_id=session_id, limit=5):
        return rec
    return None


def _targets_mod():
    from src.harness_refinement import targets as _targets
    return _targets


def _update(record: Dict[str, Any]) -> None:
    records = _load_all()
    records[record["id"]] = record
    _save_all(records)


# -- decisions -------------------------------------------------------------

def approve(proposal_id: str, *, by: str = "", targets_api: Any = None) -> Dict[str, Any]:
    """Apply a pending proposal to its target. The ONLY path that does.

    Idempotent: approving an already-applied proposal returns it unchanged and
    writes nothing. Refuses (``harness.target_changed``) when the target no
    longer holds the content the proposal was made against.
    """
    api = targets_api or _targets_mod()
    with locked():
        record = _load_all().get(str(proposal_id or ""))
        if record is None:
            raise HarnessError("harness.not_found", f"no harness proposal {proposal_id!r}")
        if record.get("status") == "applied" and not record.get("undo_of"):
            return dict(record, already_applied=True)
        if record.get("status") != "pending":
            raise HarnessError("harness.not_pending",
                               f"proposal is {record.get('status')!r}, only a pending one can be applied")
        owner = record.get("owner") or ""
        current = api.read_current(record["axis"], record["target"], owner)
        if not api.same_content(current, record.get("before")):
            log_event("apply_refused", trigger=record.get("trigger", ""), result="target changed since the proposal",
                      proposal_id=record["id"], session_id=record.get("session_id", ""), owner=owner)
            raise HarnessError(
                "harness.target_changed",
                "The target changed after this proposal was made, so applying it now would overwrite that "
                "change. Reject this proposal and run a new one.")
        try:
            api.apply_write(record, owner)
        except HarnessError as exc:
            log_event("apply_refused", trigger=record.get("trigger", ""), result=exc.error_class,
                      proposal_id=record["id"], session_id=record.get("session_id", ""), owner=owner,
                      detail={"message": exc.message})
            raise
        record["applied_content"] = api.read_current(record["axis"], record["target"], owner)
        record["status"] = "applied"
        record["applied_at"] = time.time()
        record["applied_by"] = by or "unknown"
        _update(record)
    log_event("applied", trigger=record.get("trigger", ""), result=f"{record['axis']}:{record['op']} {record['target']}",
              proposal_id=record["id"], session_id=record.get("session_id", ""), owner=record.get("owner", ""),
              detail={"by": by})
    return dict(record)


def reject(proposal_id: str, *, by: str = "", reason: str = "") -> Dict[str, Any]:
    with locked():
        record = _load_all().get(str(proposal_id or ""))
        if record is None:
            raise HarnessError("harness.not_found", f"no harness proposal {proposal_id!r}")
        if record.get("status") == "rejected":
            return dict(record, already_rejected=True)
        if record.get("status") != "pending":
            raise HarnessError("harness.not_pending",
                               f"proposal is {record.get('status')!r}, only a pending one can be rejected")
        record["status"] = "rejected"
        record["rejected_at"] = time.time()
        record["rejected_by"] = by or "unknown"
        record["reject_reason"] = str(reason or "")[:500]
        _update(record)
    if (record.get("meta") or {}).get("delegate"):
        try:
            _targets_mod().reject_delegate(record, by=by or "unknown", reason=reason)
        except Exception:  # noqa: BLE001 - the harness decision stands either way
            logger.debug("delegate reject failed", exc_info=True)
    log_event("rejected", trigger=record.get("trigger", ""), result=str(reason or "")[:200],
              proposal_id=record["id"], session_id=record.get("session_id", ""), owner=record.get("owner", ""),
              detail={"by": by})
    return dict(record)


def undo(proposal_id: str, *, by: str = "", targets_api: Any = None) -> Dict[str, Any]:
    """Restore the target to exactly what it held before ``proposal_id`` was
    applied. Returns the NEW undo record (``undo_of`` = ``proposal_id``).

    Refuses with ``harness.target_changed_since_apply`` when the target no
    longer holds what this edit left there (someone edited it afterwards), with
    ``harness.not_applied`` for a proposal that never applied, and with
    ``harness.undo_of_undo`` for an undo record (re-propose instead).
    """
    api = targets_api or _targets_mod()
    with locked():
        records = _load_all()
        record = records.get(str(proposal_id or ""))
        if record is None:
            raise HarnessError("harness.not_found", f"no harness proposal {proposal_id!r}")
        if record.get("undo_of"):
            raise HarnessError("harness.undo_of_undo",
                               "this record is itself an undo; propose the edit again instead of undoing an undo")
        if record.get("status") == "undone":
            raise HarnessError("harness.not_applied", "this proposal was already undone")
        if record.get("status") != "applied":
            raise HarnessError("harness.not_applied",
                               f"proposal is {record.get('status')!r}; only an applied proposal can be undone")
        owner = record.get("owner") or ""
        current = api.read_current(record["axis"], record["target"], owner)
        if not api.same_content(current, record.get("applied_content")):
            log_event("undo_refused", trigger=record.get("trigger", ""), result="target changed since apply",
                      proposal_id=record["id"], session_id=record.get("session_id", ""), owner=owner)
            raise HarnessError(
                "harness.target_changed_since_apply",
                "The target was edited after this proposal was applied. Undoing now would discard that later "
                "change, so nothing was touched. Edit it by hand if you still want the old text back.")
        inverse = {"create": "delete", "delete": "create", "update": "update"}[record["op"]]
        undo_record = {
            "id": new_id(), "axis": record["axis"], "target": record["target"], "op": inverse,
            "before": current, "after": record.get("before"),
            "diff": make_diff(current, record.get("before"), record["target"]),
            "rationale": f"Undo of {record['id']}", "evidence": [], "trigger": "undo",
            "created_at": time.time(), "status": "applied", "applied_at": time.time(),
            "applied_by": by or "unknown", "undo_of": record["id"], "owner": owner,
            "session_id": record.get("session_id", ""), "model": "", "risk_flags": [],
            "meta": {},
        }
        api.apply_restore(record, owner)
        restored = api.read_current(record["axis"], record["target"], owner)
        exact = api.same_content(restored, record.get("before"))
        undo_record["restore_exact"] = bool(exact)
        undo_record["applied_content"] = restored
        record["status"] = "undone"
        record["undone_at"] = time.time()
        record["undone_by"] = by or "unknown"
        record["undo_id"] = undo_record["id"]
        records[record["id"]] = record
        records[undo_record["id"]] = undo_record
        _save_all(records)
    log_event("undone", trigger=record.get("trigger", ""), result=f"{record['axis']}:{inverse} {record['target']}",
              proposal_id=record["id"], session_id=record.get("session_id", ""), owner=owner,
              detail={"by": by, "undo_id": undo_record["id"], "exact": bool(exact)})
    return dict(undo_record)


def sync_delegates() -> int:
    """Skill proposals are also visible (and decidable) in Skills > Proposals,
    because they live in the skill proposal store too. When a person decides one
    THERE, mirror the decision onto the harness record so both lists agree.
    Returns how many records changed."""
    changed = 0
    api = _targets_mod()
    with locked():
        records = _load_all()
        for rec in list(records.values()):
            if rec.get("status") != "pending" or not (rec.get("meta") or {}).get("delegate"):
                continue
            verdict = api.delegate_status(rec)
            if verdict == "approved":
                rec["status"] = "applied"
                rec["applied_at"] = time.time()
                rec["applied_by"] = "skills-proposals"
                rec["applied_content"] = api.read_current(rec["axis"], rec["target"], rec.get("owner") or "")
            elif verdict == "rejected":
                rec["status"] = "rejected"
                rec["rejected_at"] = time.time()
                rec["rejected_by"] = "skills-proposals"
            else:
                continue
            records[rec["id"]] = rec
            changed += 1
            log_event("applied" if verdict == "approved" else "rejected", trigger=rec.get("trigger", ""),
                      result="decided in Skills > Proposals", proposal_id=rec["id"],
                      session_id=rec.get("session_id", ""), owner=rec.get("owner", ""))
        if changed:
            _save_all(records)
    return changed


def counts(owner: Optional[str] = None) -> Dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for rec in list_proposals(owner=owner, limit=100000):
        out[rec.get("status", "pending")] = out.get(rec.get("status", "pending"), 0) + 1
    return out
