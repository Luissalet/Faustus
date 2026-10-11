"""The write journal of the agent contract, and the engine that undoes one agent session.

Standard library only. ``agentkit.make_agent_router(data_dir=...)`` appends one line per agent write to
``<data>/agent_journal.jsonl`` (who, which session, why, which objects, what came back), serves it at
``GET /api/agent/journal`` and replays the apps' undo handlers at ``POST /api/agent/undo``. This module holds the parts that
have no web framework in them:

* :func:`mask_secrets`, :func:`summarize_args`, :func:`digest_args`, :func:`result_ids`: what a journal line says about a
  call without keeping its secrets or its bulk;
* :class:`Journal`: append-only JSON lines, rotated at 5 MB (three older files are kept), read back as one chain;
* :func:`undo_session`: reverse-order replay of the undo handlers of a session's writes, refusing (as a *conflict*) what
  somebody else wrote after it on the same object.

A journal line (``kind`` ``write``)::

    {"v": 1, "id": ULID, "kind": "write", "ts": 1790000000.1, "app": "cicero", "tool": "slide_update", "agent": "cursor",
     "session": "run-42", "reason": "Fix the typo in the title", "args_digest": "sha256:0f3a...", "args_summary": "{...}",
     "ids": ["slide_id=01J..."], "objects": ["deck:01J.../slide:01J..."], "etag": "r3", "ok": true, "error": "",
     "ms": 12, "profile": "all", "undoable": true, "before": {...}}

``objects`` are slash-separated paths (``deck:X/slide:Y``); two writes touch the same object when one path is the other or
a parent of it. ``before`` is what the tool's ``capture`` hook saw before the write (kept in the file, not served by default).
An undo is a line of kind ``undo`` with ``undoes`` naming the write.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Union

from .ids import new_ulid

__all__ = ["JOURNAL_FILE", "MAX_JOURNAL_BYTES", "Journal", "mask_secrets", "summarize_args", "digest_args", "result_ids",
           "default_objects", "objects_overlap", "undo_session", "clean_identity"]

JOURNAL_FILE = "agent_journal.jsonl"
MAX_JOURNAL_BYTES = 5 * 1024 * 1024
KEEP_ROTATED = 3
MAX_BEFORE_BYTES = 256 * 1024
SUMMARY_CHARS = 300

PathLike = Union[str, "os.PathLike[str]"]


# ------------------------------------------------------------------------------------------------ identity

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_identity(value: Any, limit: int = 80) -> str:
    """An agent or session id from a header or a body field: printable, trimmed, at most ``limit`` characters."""
    if value is None:
        return ""
    return _CONTROL.sub("", str(value)).strip()[:limit]


# ------------------------------------------------------------------------------------------------ masking

_SECRET_KEY_PARTS = ("password", "passwd", "passphrase", "secret", "token", "apikey", "authorization", "cookie", "credential",
                     "privatekey", "bearer", "accesskey", "signature", "sessionkey")
_STRING_PATTERNS: Sequence[tuple[re.Pattern[str], str]] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "***"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "***"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"\bhat_[A-Za-z0-9_-]{16,}"), "***"),
    (re.compile(r"(?<=://)[^\s/@:]+:[^\s/@]+@"), "***@"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)(?!\*\*\*)[^\s,;&\"']{3,}"), r"\1\2***"),
    (re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{40,}\b"), "***"),
)


def _secret_key(key: Any) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return flat == "auth" or any(part in flat for part in _SECRET_KEY_PARTS)


def mask_text(text: str) -> str:
    """``text`` with bearer tokens, API keys, credentials in URLs, ``password=...`` pairs and long opaque strings replaced by ``***``."""
    for pattern, replacement in _STRING_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_secrets(value: Any, *, depth: int = 0) -> Any:
    """A copy of ``value`` (JSON-like) where the value of a key that names a secret (``password``, ``token``, ``api_key``,
    ``authorization``...) is ``"***"`` and strings are passed through :func:`mask_text`."""
    if depth > 8:
        return "…"
    if isinstance(value, Mapping):
        return {str(k): ("***" if _secret_key(k) and v not in (None, "", False) else mask_secrets(v, depth=depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [mask_secrets(v, depth=depth + 1) for v in value]
    if isinstance(value, str):
        return mask_text(value)
    return value


def _shorten(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 60 else value[:57] + "…"
    if isinstance(value, Mapping):
        return {k: _shorten(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        out = [_shorten(v, depth + 1) for v in value[:6]]
        return out + [f"…(+{len(value) - 6})"] if len(value) > 6 else out
    return value


def summarize_args(args: Any, limit: int = SUMMARY_CHARS) -> str:
    """A short, secret-free rendering of the arguments of a call: at most ``limit`` characters."""
    try:
        text = json.dumps(_shorten(mask_secrets(args)), ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = mask_text(str(args))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def digest_args(args: Any) -> str:
    """``sha256:`` plus 16 hex characters of the canonical arguments (secrets masked first, so a digest never helps to guess one)."""
    import hashlib

    try:
        text = json.dumps(mask_secrets(args), ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(args)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def result_ids(result: Any, limit: int = 20) -> list[str]:
    """The identifiers a result carries at its top level: ``id``, ``*_id`` and ``*_ids`` as ``["id=...", "slide_id=..."]``."""
    out: list[str] = []
    if not isinstance(result, Mapping):
        return out
    for key, value in result.items():
        name = str(key)
        if not (name == "id" or name.endswith("_id") or name.endswith("_ids")):
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, (str, int)) and not isinstance(item, bool) and str(item):
                out.append(f"{name}={str(item)[:80]}")
    return out[:limit]


def default_objects(args: Any, result: Any) -> list[str]:
    """The objects a write touched when its tool does not say: ``deck:ID`` for every ``deck_id`` argument and ``id`` of
    the result (flat, so two writes with the same id overlap)."""
    out: list[str] = []
    if isinstance(args, Mapping):
        for key, value in args.items():
            if str(key).endswith("_id") and isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                out.append(f"{str(key)[:-3]}:{value}")
    if isinstance(result, Mapping):
        for item in result_ids(result):
            key, _, value = item.partition("=")
            if key.endswith("_id"):
                out.append(f"{key[:-3]}:{value}")
    return list(dict.fromkeys(out))[:20]


def objects_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    """Whether two lists of object paths touch the same object: equal paths, or one is an ancestor of the other
    (``deck:A`` contains ``deck:A/slide:B``)."""
    for a in left:
        for b in right:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


# ------------------------------------------------------------------------------------------------ the file

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = _locks[path] = threading.RLock()
        return lock


class Journal:
    """The append-only journal of one app, ``<data_dir>/agent_journal.jsonl``.

    ``append`` adds a line (and rotates first when the file would pass ``max_bytes``: ``.jsonl`` becomes ``.jsonl.1``, the
    old ``.1`` becomes ``.2``, and so on up to ``.3``; older lines are dropped). ``entries`` reads the whole chain, oldest
    first. A line that cannot be parsed is skipped."""

    def __init__(self, data_dir: PathLike, *, max_bytes: int = MAX_JOURNAL_BYTES, app: str = "", keep: int = KEEP_ROTATED) -> None:
        self.dir = Path(data_dir)
        self.path = self.dir / JOURNAL_FILE
        self.max_bytes = int(max_bytes)
        self.app = app
        self.keep = max(0, int(keep))
        self._lock = _lock_for(str(self.path))

    # ----- writing
    def _rotated(self, n: int) -> Path:
        return self.path.with_name(f"{JOURNAL_FILE}.{n}")

    def _rotate(self) -> None:
        if self.keep <= 0:
            self.path.unlink(missing_ok=True)
            return
        self._rotated(self.keep).unlink(missing_ok=True)
        for n in range(self.keep - 1, 0, -1):
            if self._rotated(n).exists():
                os.replace(self._rotated(n), self._rotated(n + 1))
        os.replace(self.path, self._rotated(1))

    def append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Add ``entry`` (``id``, ``ts``, ``app`` and ``v`` are filled in when missing); returns the stored dict."""
        row = dict(entry)
        row.setdefault("v", 1)
        row.setdefault("id", new_ulid())
        row.setdefault("ts", round(time.time(), 3))
        if self.app:
            row.setdefault("app", self.app)
        if isinstance(row.get("before"), (dict, list)):
            size = len(json.dumps(row["before"], ensure_ascii=False, default=str))
            if size > MAX_BEFORE_BYTES:
                row["before"] = None
                row["undoable"] = False
                row["not_undoable_reason"] = "snapshot_too_large"
        line = (json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            if size and size + len(line) > self.max_bytes:
                self._rotate()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return row

    # ----- reading
    def files(self) -> list[Path]:
        """The journal files, oldest first."""
        chain = [self._rotated(n) for n in range(self.keep, 0, -1)] + [self.path]
        return [p for p in chain if p.is_file()]

    def entries(self) -> Iterator[dict[str, Any]]:
        """Every parsable line of the chain, oldest first."""
        with self._lock:
            files = self.files()
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(row, dict):
                            yield row
            except OSError:
                continue

    def undone_ids(self, rows: Optional[Sequence[Mapping[str, Any]]] = None) -> set[str]:
        """Ids of the writes that a successful undo line has reversed."""
        return {str(r.get("undoes")) for r in (rows if rows is not None else self.entries())
                if r.get("kind") == "undo" and r.get("ok") and r.get("undoes")}

    def query(self, *, session: str = "", agent: str = "", limit: int = 100, kind: str = "", full: bool = False,
              since: float = 0.0, undoable: Optional[Callable[[str], bool]] = None) -> list[dict[str, Any]]:
        """The last ``limit`` matching lines, oldest first. Each write gets ``undone`` (a later undo reversed it); unless
        ``full``, ``before`` is replaced by ``has_snapshot``."""
        rows = list(self.entries())
        undone = self.undone_ids(rows)
        picked = []
        for row in rows:
            if session and row.get("session") != session:
                continue
            if agent and row.get("agent") != agent:
                continue
            if kind and row.get("kind") != kind:
                continue
            if since and float(row.get("ts") or 0) < since:
                continue
            picked.append(row)
        out = []
        for row in picked[-max(1, int(limit)):]:
            row = dict(row)
            if row.get("kind", "write") == "write":
                row["undone"] = row.get("id") in undone
                if undoable is not None and row.get("undoable") and not undoable(str(row.get("tool"))):
                    row["undoable"] = False
            if not full and "before" in row:
                row["has_snapshot"] = row.get("before") is not None
                row.pop("before")
            out.append(row)
        return out


# ------------------------------------------------------------------------------------------------ undo

def _call_undo(handler: Callable[..., Any], ctx: Any, record: dict[str, Any], dry_run: bool) -> Any:
    try:
        takes_dry_run = "dry_run" in inspect.signature(handler).parameters
    except (TypeError, ValueError):
        takes_dry_run = False
    if dry_run:
        return handler(ctx, record, dry_run=True) if takes_dry_run else None
    return handler(ctx, record, dry_run=False) if takes_dry_run else handler(ctx, record)


def _brief(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": row.get("id"), "tool": row.get("tool"), "agent": row.get("agent") or "", "session": row.get("session") or "",
            "ts": row.get("ts"), "objects": list(row.get("objects") or []), "summary": row.get("args_summary") or ""}


def undo_session(journal: Journal, handlers: Mapping[str, Callable[..., Any]], ctx: Any, *, session: str, agent: str = "",
                 dry_run: bool = False, reason: str = "", actor: str = "") -> dict[str, Any]:
    """Reverse the writes of one session, newest first.

    ``handlers[tool](ctx, record)`` (or ``(ctx, record, dry_run=True)`` when it declares ``dry_run``) undoes one write; it
    gets the journal line (``before``, ``ids``, ``objects``, ``etag``...), returns a dict saying what it did, and raises an
    :class:`~hoard_link.agentkit.AppError` with code ``conflict`` when the object changed since (the app's own version check).

    Only the writes of that ``session`` (and ``agent``, when given) that succeeded and have not been undone are looked at. A
    write is reported as a **conflict** and left alone when a later, not undone write of someone else touched an overlapping
    object; as **not undoable** when its tool has no handler, it failed or no snapshot was kept. With ``dry_run`` nothing
    changes and nothing is recorded: the answer says what would happen. Otherwise every attempt appends an ``undo`` line.

    Returns ``{"undone" | "would_undo", "conflicts", "not_undoable", "already_undone", "complete", ...}``."""
    rows = list(journal.entries())
    undone_before = journal.undone_ids(rows)
    mine: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        if row.get("kind", "write") != "write" or row.get("session") != session:
            continue
        if agent and row.get("agent") != agent:
            continue
        mine.append((index, row))
    result: dict[str, Any] = {"session": session, "agent": agent or None, "dry_run": bool(dry_run), "undone": [], "would_undo": [],
                              "conflicts": [], "not_undoable": [], "already_undone": [], "ignored_failed": []}
    mine_ids = {str(row.get("id")) for _, row in mine}
    key = "would_undo" if dry_run else "undone"
    if not mine:
        result["complete"] = True
        result["counts"] = {"writes": 0}
        return result
    undone_now: set[str] = set()
    would_objects: list[Any] = []                              # objects of the writes a dry run has already planned to take back
    for index, row in reversed(mine):
        wid = str(row.get("id"))
        if not row.get("ok", True):
            result["ignored_failed"].append(_brief(row))      # the tool failed: nothing was promised to be undone
            continue
        if wid in undone_before:
            result["already_undone"].append(wid)
            continue
        tool = str(row.get("tool"))
        handler = handlers.get(tool)
        if handler is None or not row.get("undoable"):
            why = row.get("not_undoable_reason") or ("no_handler" if handler is None else "not_undoable")
            result["not_undoable"].append({**_brief(row), "reason": why})
            continue
        rival = None
        for later in rows[index + 1:]:
            if later.get("kind", "write") != "write" or not later.get("ok", True):
                continue
            if str(later.get("id")) in mine_ids:
                continue                                      # a later write of this very session: it is handled before this one
            if str(later.get("id")) in undone_before or str(later.get("id")) in undone_now:
                continue
            if objects_overlap(row.get("objects") or [], later.get("objects") or []):
                rival = later
                break
        if rival is not None:
            result["conflicts"].append({**_brief(row), "reason": "later_write_by_other_session",
                                        "message": "Another session wrote to the same object afterwards.",
                                        "with": {"id": rival.get("id"), "tool": rival.get("tool"), "agent": rival.get("agent") or "",
                                                 "session": rival.get("session") or "", "ts": rival.get("ts")}})
            continue
        if dry_run and any(objects_overlap(row.get("objects") or [], objs) for objs in would_objects):
            # a dry run changes nothing, so the handler would see the state *before* this session's newer write on the same
            # object is taken back and could not match; a real run takes that one back first, so it is reported as planned
            item = {**_brief(row), "detail": {"after_newer_writes_of_this_session": True}}
            result[key].append(item)
            would_objects.append(row.get("objects") or [])
            undone_now.add(wid)
            continue
        try:
            detail = _call_undo(handler, ctx, dict(row), dry_run)
        except Exception as error:  # noqa: BLE001 - one failing handler must not stop the rest of the report
            code = str(getattr(error, "code", "") or "")
            message = str(getattr(error, "message", "") or error)
            if code == "conflict":
                result["conflicts"].append({**_brief(row), "reason": "changed_since", "message": message})
            else:
                result["not_undoable"].append({**_brief(row), "reason": "undo_failed", "message": f"{type(error).__name__}: {message}"[:300]})
            if not dry_run:
                journal.append({"kind": "undo", "undoes": wid, "tool": tool, "agent": row.get("agent") or "", "session": session,
                                "reason": reason, "actor": actor, "ok": False, "error": f"{code or type(error).__name__}: {message}"[:300],
                                "objects": row.get("objects") or []})
            continue
        item = {**_brief(row), "detail": detail if isinstance(detail, Mapping) else ({"result": detail} if detail is not None else {})}
        result[key].append(item)
        undone_now.add(wid)
        would_objects.append(row.get("objects") or [])
        if not dry_run:
            journal.append({"kind": "undo", "undoes": wid, "tool": tool, "agent": row.get("agent") or "", "session": session,
                            "reason": reason, "actor": actor, "ok": True, "objects": row.get("objects") or [],
                            "args_summary": row.get("args_summary") or ""})
    result["complete"] = not result["conflicts"] and not result["not_undoable"]
    result["counts"] = {"writes": len(mine), key: len(result[key]), "conflicts": len(result["conflicts"]),
                        "not_undoable": len(result["not_undoable"]), "already_undone": len(result["already_undone"])}
    return result
