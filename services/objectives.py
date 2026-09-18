"""Project objectives — a per-project dashboard the agent reads at session
start (and after every compaction) and updates at turn end via TYPED DELTAS,
never by rewriting the list.

Why it is built this way
------------------------
* **Storage is JSONL inside the workspace** (``<workspace>/.odysseus/``), the
  same place project memory lives: greppable, hand-editable, survives a
  database wipe, and travels with the folder. ``objectives.jsonl`` is the
  current state (one record per objective, plus separate dependency-edge
  records, beads-style); ``objectives_log.jsonl`` is an append-only audit log
  of applied deltas, recorded conflicts and evidence.
  Projects without a workspace use an owner-scoped managed store. Rebinding
  a folder preserves existing goals there before the new binding is saved.

* **Updates are a deterministic delta compiler**, not an LLM rewrite: the
  model (or the dashboard) proposes ADD/EDIT/KILL deltas, and this module
  validates and applies them one by one, recording conflicts instead of
  failing the batch (brenner_bot-style).

* **Everything degrades gracefully.** A corrupt objectives file is renamed to
  ``.corrupt`` and treated as empty; the helpers the chat/dispatch hot paths
  call never raise (same posture as the bottom of services/projects.py).

Pure stdlib — the graph math (PageRank power iteration, Brandes betweenness)
is small enough not to warrant a dependency.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.kernel_file_lock import KernelFileLock
from services import objective_locations as _locations

logger = logging.getLogger(__name__)

# Files live beside the project memory in <workspace>/.odysseus/.
OBJECTIVES_DIRNAME = ".odysseus"
OBJECTIVES_FILENAME = "objectives.jsonl"
OBJECTIVES_LOG_FILENAME = "objectives_log.jsonl"

VALID_STATUSES = ("open", "in_progress", "blocked", "done", "dropped")
MAX_TITLE_CHARS = 200
MAX_LOG_BYTES = 1_000_000        # rotate the audit log above this
MAX_SECTION_CHARS = 2500         # "## Project objectives" system-prompt cap
MAX_REMINDER_CHARS = 1200        # post-compaction reminder cap

_ID_RE = re.compile(r"^OBJ-(\d+)$")
#: An OBJ id as it appears INSIDE free text (a task, a commit message).
_MENTION_RE = re.compile(r"\bOBJ-\d+\b")


class ObjectiveError(ValueError):
    """Invalid objectives input or an unusable store — routes map to a 400."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_iso(text: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def _id_num(obj_id: str) -> int:
    m = _ID_RE.match(str(obj_id) or "")
    return int(m.group(1)) if m else 0


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------


def objectives_dir(project: Dict[str, Any]) -> str:
    return _locations.directory(project)


def objectives_path(project: Dict[str, Any]) -> str:
    base = objectives_dir(project)
    return os.path.join(base, OBJECTIVES_FILENAME) if base else ""


def log_path(project: Dict[str, Any]) -> str:
    base = objectives_dir(project)
    return os.path.join(base, OBJECTIVES_LOG_FILENAME) if base else ""


_held_stores = threading.local()


def _check_store_paths(base: str) -> None:
    """Objective grants do not authorize following metadata links elsewhere."""
    for path in (base, *(os.path.join(base, name) for name in
                        (OBJECTIVES_FILENAME, OBJECTIVES_LOG_FILENAME, "objectives.lock"))):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
            raise PermissionError("Objectives storage must not use linked metadata paths")


@contextmanager
def _store_guard(project: Dict[str, Any]):
    with _locations.routing_guard(project):
        if (not (project or {}).get('workspace') and _locations.managed_dir(project)
                and not _locations.active(project)):
            _locations.activate(project, _atomic_write)
        with _location_guard(project):
            yield


@contextmanager
def _location_guard(project: Dict[str, Any]):
    """Serialize the whole transaction, including recovery and log rotation.

    Re-entry on this thread lets apply_deltas use the same public storage
    helpers as other callers without dropping its process-owned lock.
    """
    base = objectives_dir(project)
    if not base:
        yield
        return
    _check_store_paths(base)
    # Resolve the directory, not the held file: on Windows realpath of a
    # byte-locked file can fall back to a differently prefixed spelling.
    path = os.path.join(os.path.realpath(base), "objectives.lock")
    key = os.path.normcase(path)
    if key.startswith("\\\\?\\unc\\"):
        key = "\\\\" + key[8:]
    elif key.startswith("\\\\?\\"):
        key = key[4:]
    held = getattr(_held_stores, "paths", None)
    if held is None:
        held = _held_stores.paths = set()
    if key in held:
        yield
        return
    with KernelFileLock(path):
        _check_store_paths(base)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


def load_state(project: Dict[str, Any], *, strict: bool = False) -> Dict[str, Any]:
    """Current objectives + dependency edges.

    Returns ``{"objectives": {id: record}, "edges": [{"from","to"}]}``.
    A corrupt file is renamed to ``.corrupt`` and treated as empty — this is
    read on the chat hot path and must never raise. Strict writers and mirror
    snapshots instead refuse corrupt data without moving the original.
    """
    if not strict and not os.path.lexists(objectives_path(project)):
        return {"objectives": {}, "edges": []}
    try:
        with _store_guard(project):
            return _load_state(project, strict=strict)
    except OSError as exc:
        if strict:
            raise
        logger.warning("Could not read objectives safely: %s", exc)
        return {"objectives": {}, "edges": []}


def _load_state(project: Dict[str, Any], *, strict: bool = False) -> Dict[str, Any]:
    state: Dict[str, Any] = {"objectives": {}, "edges": []}
    path = objectives_path(project)
    if not path:
        return state
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return state
    except OSError as e:
        if strict:
            raise
        logger.warning("objectives.jsonl unreadable (%s); treating as empty", e)
        return state
    try:
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise ValueError("record is not an object")
            if rec.get("t") == "obj" and rec.get("id"):
                state["objectives"][str(rec["id"])] = rec
            elif rec.get("t") == "dep" and rec.get("from") and rec.get("to"):
                state["edges"].append({"from": str(rec["from"]), "to": str(rec["to"])})
    except (ValueError, TypeError) as e:
        if strict:
            raise ObjectiveError('Objective data is corrupt; the original file was preserved') from e
        # Corrupt state must not take a chat down: keep the broken copy so
        # nothing is silently destroyed, then start empty.
        logger.warning("objectives.jsonl corrupt (%s); renaming to .corrupt", e)
        try:
            backup = path + ".corrupt"
            if os.path.lexists(backup):
                backup = path + "." + uuid.uuid4().hex + ".corrupt"
            os.replace(path, backup)
        except OSError:
            pass
        return {"objectives": {}, "edges": []}
    return state


def save_state(project: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Atomic rewrite of objectives.jsonl (tmp file + os.replace)."""
    try:
        with _store_guard(project):
            _save_state(project, state)
    except OSError as exc:
        raise ObjectiveError(f"Could not save objectives: {exc}") from exc


def _atomic_write(path: str, content: bytes) -> None:
    """Unique, exclusively created temp file; failures leave no partial file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def preserve_for_rebinding(project: Dict[str, Any]) -> None:
    """Keep goals and their audit trail before changing a workspace binding.

    Old portable files are retained. All writers with this project identity,
    including old snapshots, switch to the managed store once the last marker
    write succeeds. A failed copy leaves the original store authoritative.
    """
    with _locations.routing_guard(project):
        target = _locations.managed_dir(project)
        if not target or _locations.active(project) or not project.get('workspace'):
            return
        with _location_guard(project):
            source = objectives_dir(project)
            copies = []
            for name in (OBJECTIVES_FILENAME, OBJECTIVES_LOG_FILENAME):
                try:
                    with open(os.path.join(source, name), 'rb') as fh:
                        copies.append((name, fh.read()))
                except FileNotFoundError:
                    continue
            if not copies:
                return  # An empty project may adopt the new folder's goals.
            _check_store_paths(target)
            for name, content in copies:
                _atomic_write(os.path.join(target, name), content)
            _locations.activate(project, _atomic_write)


def _save_state(project: Dict[str, Any], state: Dict[str, Any]) -> None:
    path = objectives_path(project)
    if not path:
        raise ObjectiveError("Objectives require a project identity or workspace")
    lines: List[str] = []
    for oid in sorted(state.get("objectives") or {}, key=_id_num):
        lines.append(json.dumps(state["objectives"][oid], ensure_ascii=False))
    for edge in state.get("edges") or []:
        lines.append(json.dumps({"t": "dep", "from": edge["from"], "to": edge["to"]},
                                ensure_ascii=False))
    try:
        _atomic_write(path, ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))
    except OSError as e:
        raise ObjectiveError(f"Could not save objectives: {e}")


def append_log(project: Dict[str, Any], record: Dict[str, Any]) -> bool:
    """Append one audit record; rotate above MAX_LOG_BYTES.

    Swallow-and-log: a failed audit write must never fail the apply that
    produced it.
    """
    try:
        with _store_guard(project):
            return _append_log(project, record)
    except OSError as exc:
        logger.warning("Could not lock objectives log: %s", exc)
        return False


def _append_log(project: Dict[str, Any], record: Dict[str, Any]) -> bool:
    path = log_path(project)
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if os.path.getsize(path) > MAX_LOG_BYTES:
            _rotate_log(path)
        return True
    except OSError as e:
        logger.warning("Could not append to objectives log: %s", e)
        return False


def _rotate_log(path: str) -> None:
    """Keep the newest ~half of the log, cut at a line boundary."""
    with open(path, "rb") as fh:
        data = fh.read()
    half = data[len(data) // 2:]
    nl = half.find(b"\n")
    kept = half[nl + 1:] if nl >= 0 else half
    _atomic_write(path, kept)


def read_log(project: Dict[str, Any], limit: int = 50) -> List[Dict[str, Any]]:
    """The last `limit` audit records, oldest first. Never raises."""
    if not os.path.lexists(log_path(project)):
        return []
    try:
        with _store_guard(project):
            return _read_log(project, limit)
    except OSError as exc:
        logger.warning("Could not read objectives log safely: %s", exc)
        return []


def _read_log(project: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    path = log_path(project)
    if not path or not os.path.isfile(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines[-max(1, int(limit)):]:
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except (ValueError, TypeError):
            continue
    return out


def next_id(state: Dict[str, Any]) -> str:
    """OBJ-<n>, monotonically increasing over every id ever stored —
    including dropped objectives, which are kept so history stays diffable."""
    top = max((_id_num(oid) for oid in state.get("objectives") or {}), default=0)
    return f"OBJ-{top + 1}"


def mentioned_ids(text: Any) -> List[str]:
    """Every ``OBJ-<n>`` a piece of free text names, in order of first mention
    and without repeats.

    One definition of "this text talks about that objective", shared by the
    callers that read a task, an instruction or a commit message and have to
    agree on the answer. Total: anything at all in, a list out — this is read
    on the dispatch settle path and must never raise.
    """
    try:
        found = _MENTION_RE.findall(str(text or ""))
    except Exception as e:  # noqa: BLE001 - hot path, never raise
        logger.debug("mentioned_ids failed: %s", e)
        return []
    out: List[str] = []
    for oid in found:
        if oid not in out:
            out.append(oid)
    return out


# ----------------------------------------------------------------------
# The delta compiler (deterministic — no LLM anywhere)
# ----------------------------------------------------------------------


def _outgoing(state: Dict[str, Any], oid: str) -> List[str]:
    return [e["to"] for e in state.get("edges") or [] if e.get("from") == oid]


def _would_cycle(state: Dict[str, Any], frm: str, to: str) -> bool:
    """Would the dep edge frm→to (frm depends on to) close a cycle?
    True when `frm` is already reachable from `to` along dep edges."""
    if frm == to:
        return True
    seen = {to}
    queue = deque([to])
    while queue:
        node = queue.popleft()
        for nxt in _outgoing(state, node):
            if nxt == frm:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def _valid_status(value: Any) -> Optional[str]:
    status = str(value or "").strip().lower()
    return status if status in VALID_STATUSES else None


def _valid_priority(value: Any) -> Optional[int]:
    # bool is an int in Python; int(1.9) silently truncates. Neither is a
    # priority supplied by the user. Keep integral legacy strings/floats.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        pri = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return pri if 1 <= pri <= 4 else None


def _valid_deps(value: Any) -> Optional[List[str]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(d, str) or not _ID_RE.fullmatch(d) for d in value):
        return None
    return list(dict.fromkeys(value))


def _replace_deps(state: Dict[str, Any], oid: str, deps: List[str],
                  conflicts: List[Dict[str, Any]], op: str) -> None:
    """Replace the objective's outgoing edges with `deps`, guarding cycles.
    An edge that would close a cycle is skipped and recorded as a conflict;
    the rest of the delta still lands."""
    state["edges"] = [e for e in state["edges"] if e.get("from") != oid]
    for dep in deps:
        if _would_cycle(state, oid, dep):
            conflicts.append({"op": op, "id": oid,
                              "reason": f"dep {oid} -> {dep} would create a cycle; edge not added"})
            continue
        state["edges"].append({"from": oid, "to": dep})


def apply_deltas(
    project: Dict[str, Any],
    deltas: List[Dict[str, Any]],
    actor: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and apply typed deltas; conflicts are recorded, never raised.

    Delta shape: ``{"op":"ADD|EDIT|KILL", "id"?, "title"?, "status"?,
    "priority"?, "notes"?, "deps"?:[ids], "rationale"?, "base_updated_at"?}``.
    Apply order: all ADDs (input order), then all EDITs, then all KILLs.
    Returns ``{"applied":[...], "conflicts":[...], "state":{...}}``.
    """
    try:
        with _store_guard(project):
            return _apply_deltas(project, deltas, actor, session_id)
    except OSError as exc:
        raise ObjectiveError(f"Could not update objectives safely: {exc}") from exc


def _next_timestamp(state: Dict[str, Any]) -> str:
    """Strictly advance the revision even when the clock stalls or moves back."""
    now = _parse_iso(_now_iso()) or datetime.now(timezone.utc)
    for record in state["objectives"].values():
        previous = _parse_iso(record.get("updated_at"))
        if previous is not None and previous >= now:
            now = previous + timedelta(microseconds=1)
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _human_conflict(actor: str, delta: Dict[str, Any], record: Dict[str, Any]) -> bool:
    base = delta.get("base_updated_at")
    if actor != "agent" or not base or record.get("last_actor") != "user":
        return False
    current, snapshot = _parse_iso(record.get("updated_at")), _parse_iso(base)
    # Compare instants, not strings: '.123Z' sorts BEFORE 'Z'. Invalid or
    # future snapshots cannot authorize overwriting a human change either.
    return current is None or snapshot is None or current != snapshot


def _apply_deltas(project, deltas, actor, session_id):
    actor = "agent" if actor == "agent" else "user"
    state = load_state(project, strict=True)
    applied: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    adds: List[Dict[str, Any]] = []
    edits: List[Dict[str, Any]] = []
    kills: List[Dict[str, Any]] = []
    for delta in deltas or []:
        if not isinstance(delta, dict):
            conflicts.append({"op": None, "id": None, "reason": "delta is not an object"})
            continue
        op = str(delta.get("op") or "").strip().upper()
        if op == "ADD":
            adds.append(delta)
        elif op == "EDIT":
            edits.append(delta)
        elif op == "KILL":
            kills.append(delta)
        else:
            conflicts.append({"op": op or None, "id": delta.get("id"),
                              "reason": f"unknown op '{delta.get('op')}' (use ADD, EDIT or KILL)"})

    now = _next_timestamp(state)

    for delta in adds:
        title = delta["title"].strip() if isinstance(delta.get("title"), str) else ""
        if not 1 <= len(title) <= MAX_TITLE_CHARS:
            conflicts.append({"op": "ADD", "id": None,
                              "reason": f"ADD requires a title of 1..{MAX_TITLE_CHARS} characters"})
            continue
        dup = next((o for o in state["objectives"].values()
                    if o.get("status") != "dropped"
                    and str(o.get("title") or "").strip().casefold() == title.casefold()), None)
        if dup:
            conflicts.append({"op": "ADD", "id": None,
                              "reason": f"duplicate title of {dup.get('id')}: '{title}'"})
            continue
        status = _valid_status(delta.get("status") or "open")
        if status is None:
            conflicts.append({"op": "ADD", "id": None,
                              "reason": f"invalid status '{delta.get('status')}'"})
            continue
        priority = _valid_priority(delta.get("priority") if delta.get("priority") is not None else 3)
        if priority is None:
            conflicts.append({"op": "ADD", "id": None,
                              "reason": f"invalid priority '{delta.get('priority')}' (1..4, 1 highest)"})
            continue
        deps = _valid_deps(delta.get("deps"))
        if deps is None:
            conflicts.append({"op": "ADD", "id": None,
                              "reason": "deps must be an array of OBJ identifiers"})
            continue
        unknown = [d for d in deps if d not in state["objectives"]]
        if unknown:
            conflicts.append({"op": "ADD", "id": None,
                              "reason": f"unknown dep id(s): {', '.join(unknown)}"})
            continue
        oid = next_id(state)
        state["objectives"][oid] = {
            "t": "obj", "id": oid, "title": title, "status": status,
            "priority": priority, "owner": actor,
            "notes": str(delta.get("notes") or ""),
            "created_at": now, "updated_at": now, "last_actor": actor,
        }
        if deps:
            _replace_deps(state, oid, deps, conflicts, "ADD")
        applied.append({"op": "ADD", "id": oid,
                        "fields": {"title": title, "status": status, "priority": priority,
                                   **({"deps": deps} if deps else {})},
                        "rationale": str(delta.get("rationale") or "")})

    for delta in edits:
        oid = str(delta.get("id") or "")
        obj = state["objectives"].get(oid)
        if not obj:
            conflicts.append({"op": "EDIT", "id": oid or None,
                              "reason": f"objective '{oid}' does not exist"})
            continue
        editable = {k: delta[k] for k in ("title", "status", "priority", "notes", "deps")
                    if k in delta}
        if not editable:
            continue        # empty edit → skip, not a conflict
        changes: Dict[str, Any] = {}
        bad = None
        if "title" in editable:
            title = editable["title"].strip() if isinstance(editable["title"], str) else ""
            if not 1 <= len(title) <= MAX_TITLE_CHARS:
                bad = f"invalid title (1..{MAX_TITLE_CHARS} characters)"
            else:
                changes["title"] = title
        if bad is None and "status" in editable:
            status = _valid_status(editable["status"])
            if status is None:
                bad = f"invalid status '{editable['status']}'"
            else:
                changes["status"] = status
        if bad is None and "priority" in editable:
            priority = _valid_priority(editable["priority"])
            if priority is None:
                bad = f"invalid priority '{editable['priority']}' (1..4)"
            else:
                changes["priority"] = priority
        if bad is None and ("title" in changes or "status" in changes):
            title = changes.get("title", obj.get("title", ""))
            status = changes.get("status", obj.get("status"))
            duplicate = next((other for other_id, other in state["objectives"].items()
                if other_id != oid and other.get("status") != "dropped"
                and str(other.get("title") or "").strip().casefold() == title.casefold()), None)
            if status != "dropped" and duplicate:
                bad = f"duplicate title of {duplicate.get('id')}: '{title}'"
        if bad is None and "notes" in editable:
            changes["notes"] = str(editable["notes"] or "")
        deps: Optional[List[str]] = None
        if bad is None and "deps" in editable:
            deps = _valid_deps(editable["deps"])
            if deps is None:
                bad = "deps must be an array of OBJ identifiers"
            else:
                unknown = [d for d in deps if d not in state["objectives"]]
                if unknown:
                    bad = f"unknown dep id(s): {', '.join(unknown)}"
        if bad:
            conflicts.append({"op": "EDIT", "id": oid, "reason": bad})
            continue
        # Human edit wins: an agent editing over a user's newer change is a
        # conflict, not a silent overwrite. The agent passes base_updated_at
        # (the updated_at it last saw); a user edit after that wins.
        if _human_conflict(actor, delta, obj):
            conflicts.append({"op": "EDIT", "id": oid,
                              "reason": "human edit wins: the objective was edited by a user "
                                        "after the state this delta was based on"})
            continue
        if deps is not None:
            _replace_deps(state, oid, deps, conflicts, "EDIT")
            changes["deps"] = _outgoing(state, oid)
        obj.update({k: v for k, v in changes.items() if k != "deps"})
        obj["updated_at"] = now
        obj["last_actor"] = actor
        applied.append({"op": "EDIT", "id": oid, "fields": changes,
                        "rationale": str(delta.get("rationale") or "")})

    for delta in kills:
        oid = str(delta.get("id") or "")
        obj = state["objectives"].get(oid)
        if not obj:
            conflicts.append({"op": "KILL", "id": oid or None,
                              "reason": f"objective '{oid}' does not exist"})
            continue
        rationale = str(delta.get("rationale") or "").strip()
        if actor == "agent" and not rationale:
            conflicts.append({"op": "KILL", "id": oid,
                              "reason": "KILL requires a rationale when the agent proposes it"})
            continue
        if _human_conflict(actor, delta, obj):
            conflicts.append({"op": "KILL", "id": oid,
                              "reason": "human edit wins: the objective changed since this snapshot"})
            continue
        # The record is kept with status "dropped" — history stays diffable.
        obj["status"] = "dropped"
        obj["updated_at"] = now
        obj["last_actor"] = actor
        applied.append({"op": "KILL", "id": oid, "fields": {"status": "dropped"},
                        "rationale": rationale})

    if applied:
        save_state(project, state)   # may raise ObjectiveError on a dead disk

    for entry in applied:
        append_log(project, {"ts": now, "kind": "delta", "actor": actor,
                             "op": entry["op"], "id": entry["id"], "fields": entry["fields"],
                             "rationale": entry.get("rationale") or "", "session": session_id})
    for entry in conflicts:
        append_log(project, {"ts": now, "kind": "conflict", "actor": actor,
                             "op": entry.get("op"), "id": entry.get("id"),
                             "fields": {}, "rationale": "", "session": session_id,
                             "reason": entry.get("reason")})

    return {"applied": applied, "conflicts": conflicts, "state": serialize_state(state)}


def serialize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """The state as the API/tool payload: sorted objectives (with their
    outgoing deps inlined) and the raw edge list."""
    objectives = []
    for oid in sorted(state.get("objectives") or {}, key=_id_num):
        obj = dict(state["objectives"][oid])
        obj["deps"] = _outgoing(state, oid)
        objectives.append(obj)
    return {"objectives": objectives, "edges": list(state.get("edges") or [])}


# ----------------------------------------------------------------------
# Graph impact score (pure stdlib)
# ----------------------------------------------------------------------


def impact_scores(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per-objective structural importance on the dependency DAG.

    score = PageRank*0.30 + Betweenness*0.30 + blocker_ratio*0.20
          + staleness*0.10 + priority_boost*0.10, each rounded to 4 decimals
    so the same state always yields the same numbers.
    """
    objectives = {oid: o for oid, o in (state.get("objectives") or {}).items()
                  if o.get("status") != "dropped"}
    ids = sorted(objectives, key=_id_num)
    if not ids:
        return {}
    edges = [(e["from"], e["to"]) for e in state.get("edges") or []
             if e.get("from") in objectives and e.get("to") in objectives]

    pagerank = _pagerank(ids, edges)
    betweenness = _betweenness(ids, edges)

    # dependents[x] = objectives that directly depend on x (edge dep → x)
    dependents: Dict[str, List[str]] = {oid: [] for oid in ids}
    for frm, to in edges:
        dependents[to].append(frm)

    not_done = [oid for oid in ids if objectives[oid].get("status") != "done"]
    now = datetime.now(timezone.utc)

    out: Dict[str, Dict[str, Any]] = {}
    for oid in ids:
        obj = objectives[oid]
        if obj.get("status") == "done":
            blocked = []
        else:
            blocked = [d for d in dependents[oid] if objectives[d].get("status") != "done"]
        denom = max(1, len([x for x in not_done if x != oid]))
        blocker_ratio = len(blocked) / denom
        updated = _parse_iso(obj.get("updated_at") or "")
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            days = max(0.0, (now - updated).total_seconds() / 86400.0)
        else:
            days = 0.0
        staleness = min(days / 30.0, 1.0)
        priority = _valid_priority(obj.get("priority")) or 3
        priority_boost = (5 - priority) / 4.0
        components = {
            "pagerank": round(pagerank.get(oid, 0.0), 4),
            "betweenness": round(betweenness.get(oid, 0.0), 4),
            "blocker_ratio": round(blocker_ratio, 4),
            "staleness": round(staleness, 4),
            "priority_boost": round(priority_boost, 4),
        }
        score = (components["pagerank"] * 0.30 + components["betweenness"] * 0.30
                 + components["blocker_ratio"] * 0.20 + components["staleness"] * 0.10
                 + components["priority_boost"] * 0.10)
        out[oid] = {"score": round(score, 4), "components": components, "hint": None}

    # Priority hint (beads_viewer): flag an objective whose structural rank is
    # well ahead of its human priority AND that is blocking open work.
    score_rank = {oid: i for i, oid in enumerate(
        sorted(ids, key=lambda o: (-out[o]["score"], _id_num(o))))}
    priority_rank = {oid: i for i, oid in enumerate(
        sorted(ids, key=lambda o: (_valid_priority(objectives[o].get("priority")) or 3, _id_num(o))))}
    for oid in ids:
        blocks_open = (objectives[oid].get("status") != "done"
                       and any(objectives[d].get("status") != "done" for d in dependents[oid]))
        if priority_rank[oid] - score_rank[oid] >= 2 and blocks_open:
            out[oid]["hint"] = "structurally blocking; consider raising priority"
    return out


def _pagerank(ids: List[str], edges: List[Tuple[str, str]],
              iterations: int = 20, damping: float = 0.85) -> Dict[str, float]:
    """Power iteration where each dependent links to its dependency — an
    objective many things depend on collects rank. Normalized to 0..1 by max."""
    n = len(ids)
    links: Dict[str, List[str]] = {oid: [] for oid in ids}    # frm → [to] (dep direction)
    for frm, to in edges:
        links[frm].append(to)
    rank = {oid: 1.0 / n for oid in ids}
    for _ in range(iterations):
        nxt = {oid: (1.0 - damping) / n for oid in ids}
        dangling = sum(rank[oid] for oid in ids if not links[oid])
        for oid in ids:
            nxt[oid] += damping * dangling / n
        for frm in ids:
            outs = links[frm]
            if not outs:
                continue
            share = damping * rank[frm] / len(outs)
            for to in outs:
                nxt[to] += share
        rank = nxt
    top = max(rank.values()) if rank else 0.0
    if top <= 0:
        return {oid: 0.0 for oid in ids}
    return {oid: rank[oid] / top for oid in ids}


def _betweenness(ids: List[str], edges: List[Tuple[str, str]]) -> Dict[str, float]:
    """Brandes' algorithm on the (small) directed dep graph; normalized by
    the max (0 everywhere when no paths pass through anything)."""
    adj: Dict[str, List[str]] = {oid: [] for oid in ids}
    for frm, to in edges:
        adj[frm].append(to)
    bc = {oid: 0.0 for oid in ids}
    for s in ids:
        stack: List[str] = []
        preds: Dict[str, List[str]] = {oid: [] for oid in ids}
        sigma = {oid: 0.0 for oid in ids}
        sigma[s] = 1.0
        dist = {oid: -1 for oid in ids}
        dist[s] = 0
        queue = deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = {oid: 0.0 for oid in ids}
        while stack:
            w = stack.pop()
            for v in preds[w]:
                if sigma[w] > 0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                bc[w] += delta[w]
    top = max(bc.values()) if bc else 0.0
    if top <= 0:
        return {oid: 0.0 for oid in ids}
    return {oid: bc[oid] / top for oid in ids}


# ----------------------------------------------------------------------
# Prompt rendering (system block + post-compaction reminder)
# ----------------------------------------------------------------------


def render_lines(state: Dict[str, Any]) -> List[str]:
    """One line per non-dropped objective, only unresolved deps listed:
    ``OBJ-3 [in_progress] (P2) Title — blocked by OBJ-1, OBJ-2``"""
    objectives = state.get("objectives") or {}
    lines = []
    for oid in sorted(objectives, key=_id_num):
        obj = objectives[oid]
        if obj.get("status") == "dropped":
            continue
        unresolved = [dep for dep in _outgoing(state, oid)
                      if (objectives.get(dep) or {}).get("status") not in ("done", "dropped")
                      and dep in objectives]
        line = f"{oid} [{obj.get('status')}] (P{obj.get('priority', 3)}) {obj.get('title')}"
        if unresolved:
            line += " — blocked by " + ", ".join(unresolved)
        lines.append(line)
    return lines


def _capped_lines_text(lines: List[str], cap: int) -> str:
    """Join lines under a character cap, truncating at a line boundary with
    an in-band note so the model knows the list is partial."""
    text = "\n".join(lines)
    if len(text) <= cap:
        return text
    kept: List[str] = []
    used = 0
    note_room = 80
    for line in lines:
        if used + len(line) + 1 > cap - note_room:
            break
        kept.append(line)
        used += len(line) + 1
    remaining = len(lines) - len(kept)
    kept.append(f"[objectives truncated — {remaining} more; call project_objectives for the rest]")
    return "\n".join(kept)


_STANDING_INSTRUCTION = (
    "Read these objectives before planning. When a turn of real work ends, "
    "update them with the `project_objectives` tool using typed deltas "
    "(ADD/EDIT/KILL, each with a rationale); never rewrite the whole list. "
    "Statuses must reflect actual work and evidence, not intentions."
)


def objectives_block(project: Dict[str, Any], cap: int = MAX_SECTION_CHARS,
                     include_instruction: bool = True) -> str:
    """The '## Project objectives' section, or '' when there is nothing to
    show. Only changes when the objectives change (KV-cache friendly)."""
    if not objectives_dir(project):
        return ""
    state = load_state(project)
    lines = render_lines(state)
    if not lines:
        return ""
    header = "## Project objectives\n"
    tail = ("\n\n" + _STANDING_INSTRUCTION) if include_instruction else ""
    budget = max(200, cap - len(header) - len(tail))
    return header + _capped_lines_text(lines, budget) + tail


# ----------------------------------------------------------------------
# Payloads for the tool / API
# ----------------------------------------------------------------------


def list_payload(project: Dict[str, Any]) -> Dict[str, Any]:
    """Compact 'list' answer for the agent tool: objectives + scores."""
    state = load_state(project)
    serialized = serialize_state(state)
    return {"objectives": serialized["objectives"], "scores": impact_scores(state)}


def dashboard_payload(project: Dict[str, Any], log_limit: int = 50, *, strict: bool = False) -> Dict[str, Any]:
    """The full dashboard answer for the HTTP API."""
    state = load_state(project, strict=strict)
    serialized = serialize_state(state)
    return {
        "objectives": serialized["objectives"],
        "edges": serialized["edges"],
        "scores": impact_scores(state),
        "log": read_log(project, log_limit),
    }


# ----------------------------------------------------------------------
# Evidence records (dispatch/chat hooks — must never raise)
# ----------------------------------------------------------------------


def add_evidence(project: Dict[str, Any], obj_id: str, source: str, ref: str,
                 confidence: float, note: str = "") -> bool:
    """Append one evidence record to the audit log. Swallow-and-log — this is
    called from the dispatch settle path and must never raise."""
    try:
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0
        return append_log(project, {
            "ts": _now_iso(), "kind": "evidence", "id": str(obj_id),
            "source": str(source), "ref": str(ref),
            "confidence": confidence, "note": str(note or "")[:400],
        })
    except Exception as e:  # noqa: BLE001 - hot path, never raise
        logger.debug("add_evidence(%s) failed: %s", obj_id, e)
        return False
