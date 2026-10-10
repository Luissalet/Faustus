"""src/cookbook_job_logs.py - copyable, durable logs for Cookbook jobs.

A Cookbook "job" is one session the Cookbook launched (a model download, a
serve, a dependency install...). Until now the only trace of it was a 12 KB
tail the status poller kept, so a failed job gave nothing to paste into a bug
report: not the command that ran, not stderr, not the exit code.

This module keeps one record per job (`session_id`):

  * the command as executed: the user-level command, the launcher argv, the
    cwd and the environment *keys* handed to the child (secret values are
    redacted, never stored);
  * start / end times, status and exit code;
  * where the output lives: up to three files (`combined`, `stdout`, `stderr`).
    A LOCAL detached launch writes all three itself; for tmux / SSH jobs the
    poller only sees a pane capture, so the `combined` file is a capped copy of
    the latest snapshot and `stdout` / `stderr` are reported unavailable
    instead of being guessed.

Reads are tail reads from those files (a ring buffer of at most `MAX_TAIL`
lines) so memory stays flat however long the job ran; a file past
`MAX_FILE_BYTES` is trimmed from the head once the job ends and a read never
looks at more than that many bytes. Everything handed out goes through
`redact_secrets` again, so a token echoed by a tool does not leave the box.

Storage: `DATA_DIR/cookbook_job_logs/<session_id>.json` (metadata), plus
`<session_id>.log` for snapshot-backed jobs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_text
from core.log_safety import redact_secrets
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(DATA_DIR, "cookbook_job_logs")
MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_TAIL = 200
MAX_TAIL = 2000
STREAMS = ("combined", "stdout", "stderr")
TERMINAL = {"completed", "done", "error", "failed", "crashed", "stopped", "ended", "killed"}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SECRET_NAME_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|CREDENTIAL|AUTH|PRIVATE|COOKIE)", re.I)
_SECRET_FLAG_RE = re.compile(
    r"(--?(?:hf[-_]?token|token|api[-_]?key|password|passwd|secret|auth[-_]?token)(?:=|\s+))(\S+)", re.I
)
_lock = threading.Lock()


class JobLogError(ValueError):
    """Bad session id or stream; the routes map it to a 400/404."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _ID_RE.match(session_id):
        raise JobLogError("invalid job id")
    return session_id


def redact_text(text: Any) -> str:
    """Secrets out of a command line or a log line: `redact_secrets` for
    key=value / bearer shapes plus `--token VALUE` style flags."""
    s = "" if text is None else str(text)
    s = _SECRET_FLAG_RE.sub(lambda m: m.group(1) + "***", s)
    try:
        return redact_secrets(s)
    except Exception:  # noqa: BLE001 - redaction must never break a read
        return s


def redact_env(env: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Keys handed to the child. A secret-looking name keeps the key and
    loses the value; the rest are kept (truncated) because `CUDA_VISIBLE_DEVICES`
    is exactly what a bug report needs."""
    out: Dict[str, str] = {}
    for k, v in (env or {}).items():
        k = str(k)
        out[k] = "***" if _SECRET_NAME_RE.search(k) else redact_text(str(v))[:200]
    return out


def _meta_path(session_id: str) -> str:
    return os.path.join(LOG_DIR, f"{_check_id(session_id)}.json")


def _load(session_id: str) -> Optional[dict]:
    try:
        with open(_meta_path(session_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, JobLogError):
        return None


def _save(meta: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    atomic_write_text(_meta_path(meta["session_id"]), json.dumps(meta, ensure_ascii=False, indent=1))


def record_launch(
    session_id: str,
    *,
    kind: str = "",
    model: str = "",
    command: str = "",
    argv: Optional[List[str]] = None,
    cwd: str = "",
    env: Optional[Dict[str, Any]] = None,
    remote: str = "",
    launcher: str = "",
    script: str = "",
    files: Optional[Dict[str, str]] = None,
) -> Optional[dict]:
    """Record what was executed. Merges into an existing record (the launcher
    and the endpoint each know a different half) and never raises: a logging
    failure must not stop a model from launching."""
    try:
        with _lock:
            meta = _load(session_id) or {"session_id": _check_id(session_id), "started_at": now_iso(), "status": "running"}
            if kind:
                meta["kind"] = kind
            if model:
                meta["model"] = model
            if command:
                meta["command"] = redact_text(command)
            if argv is not None:
                meta["argv"] = [redact_text(a) for a in argv]
            if cwd:
                meta["cwd"] = str(cwd)
            if env is not None:
                meta["env"] = {**(meta.get("env") or {}), **redact_env(env)}
            if remote is not None:
                meta["remote"] = remote or meta.get("remote") or "local"
            if launcher:
                meta["launcher"] = redact_text(launcher)[:4000]
            if script:
                meta["script"] = redact_text(script)[:20000]
            if files:
                meta["files"] = {**(meta.get("files") or {}), **{k: str(v) for k, v in files.items() if k in STREAMS}}
            _save(meta)
            return meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("cookbook job log record failed for %r: %s", session_id, exc)
        return None


def observe(
    session_id: str,
    *,
    status: str,
    exit_code: Optional[int] = None,
    snapshot: Optional[str] = None,
    defaults: Optional[dict] = None,
) -> None:
    """Called by the status poller on every pass. Tracks status / exit code /
    end time, and for snapshot-backed jobs stores the latest capture. Never
    raises."""
    try:
        with _lock:
            meta = _load(session_id)
            if meta is None:
                meta = {"session_id": _check_id(session_id), "started_at": now_iso(), "status": status}
                for k, v in (defaults or {}).items():
                    if v:
                        meta[k] = redact_text(v) if k == "command" else v
            st = (status or "").lower()
            meta["status"] = st or meta.get("status", "")
            if exit_code is not None:
                meta["exit_code"] = int(exit_code)
            if st in TERMINAL:
                meta.setdefault("ended_at", now_iso())
                if not meta.get("ended_at"):
                    meta["ended_at"] = now_iso()
            elif meta.get("ended_at") and st:
                meta.pop("ended_at", None)  # restarted under the same id
            if snapshot and "combined" not in (meta.get("files") or {}):
                path = os.path.join(LOG_DIR, f"{session_id}.log")
                os.makedirs(LOG_DIR, exist_ok=True)
                data = snapshot.encode("utf-8", "replace")
                if len(data) > MAX_FILE_BYTES:
                    data = b"[... trimmed ...]\n" + data[-MAX_FILE_BYTES:]
                atomic_write_text(path, data.decode("utf-8", "replace"))
                meta["snapshot_file"] = path
            if st in TERMINAL:
                for p in _stream_paths(meta).values():
                    _trim_head(p)
            _save(meta)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cookbook job log observe failed for %r: %s", session_id, exc)


def _stream_paths(meta: dict) -> Dict[str, str]:
    paths = dict(meta.get("files") or {})
    if "combined" not in paths and meta.get("snapshot_file"):
        paths["combined"] = meta["snapshot_file"]
    return paths


def _trim_head(path: str) -> None:
    """Enforce the size cap on a finished job's file; best effort (a file a
    process still holds open on Windows may refuse)."""
    try:
        size = os.path.getsize(path)
        if size <= MAX_FILE_BYTES:
            return
        with open(path, "rb") as fh:
            fh.seek(size - MAX_FILE_BYTES)
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(b"[... older output trimmed to the size cap ...]\n" + data)
    except OSError:
        pass


def _tail_lines(path: str, n: int) -> tuple[List[str], int, bool]:
    """Last `n` lines of a file, reading at most MAX_FILE_BYTES from its end.
    Returns (lines, bytes_in_file, cut): `cut` is True when older output exists
    that is not in `lines`."""
    size = os.path.getsize(path)
    start = max(0, size - MAX_FILE_BYTES)
    with open(path, "rb") as fh:
        fh.seek(start)
        text = fh.read().decode("utf-8", "replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    cut = start > 0 or len(lines) > n
    return lines[-n:] if n else [], size, cut


def get_meta(session_id: str) -> Optional[dict]:
    return _load(session_id)


def list_jobs(limit: int = 50) -> List[dict]:
    rows: List[dict] = []
    try:
        names = [n for n in os.listdir(LOG_DIR) if n.endswith(".json")]
    except OSError:
        return rows
    for name in names:
        meta = _load(name[:-5])
        if meta:
            rows.append(_summary(meta))
    rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return rows[: max(1, min(int(limit or 50), 200))]


def _summary(meta: dict) -> dict:
    return {k: meta.get(k) for k in ("session_id", "kind", "model", "remote", "status", "exit_code", "started_at", "ended_at")}


def available_streams(meta: dict) -> List[str]:
    return [s for s, p in _stream_paths(meta).items() if p and os.path.exists(p)]


def read_log(session_id: str, stream: str = "combined", tail: int = DEFAULT_TAIL) -> dict:
    """The job record plus the last `tail` lines of one stream. In the
    `combined` stream a line that also appears in the stderr file is flagged
    `stderr: true` so the UI can highlight it."""
    stream = (stream or "combined").lower()
    if stream not in STREAMS:
        raise JobLogError(f"stream must be one of {list(STREAMS)}")
    _check_id(session_id)
    meta = _load(session_id)
    if meta is None:
        raise KeyError(session_id)
    try:
        tail = int(tail)
    except (TypeError, ValueError):
        tail = DEFAULT_TAIL
    tail = max(1, min(tail, MAX_TAIL))
    paths = _stream_paths(meta)
    path = paths.get(stream)
    avail = available_streams(meta)
    out = {
        "job": _public(meta),
        "stream": stream,
        "tail": tail,
        "available_streams": avail,
        "stream_available": stream in avail,
        "lines": [],
        "total_bytes": 0,
        "truncated": False,
    }
    if stream not in avail:
        return out
    lines, size, cut = _tail_lines(path, tail)
    err_set: set = set()
    if stream == "combined" and "stderr" in avail:
        try:
            err_lines, _, _ = _tail_lines(paths["stderr"], MAX_TAIL * 2)
            err_set = {l.strip() for l in err_lines if l.strip()}
        except OSError:
            err_set = set()
    out["lines"] = [
        {"text": redact_text(l), "stderr": (stream == "stderr") or (l.strip() in err_set)} for l in lines
    ]
    out["total_bytes"] = size
    out["truncated"] = cut
    return out


def _public(meta: dict) -> dict:
    keys = ("session_id", "kind", "model", "remote", "status", "exit_code", "started_at", "ended_at",
            "command", "argv", "cwd", "env", "launcher", "script")
    pub = {k: meta.get(k) for k in keys if meta.get(k) not in (None, "", [], {})}
    pub["duration_s"] = _duration(meta)
    return pub


def _duration(meta: dict) -> Optional[float]:
    try:
        a = datetime.fromisoformat(meta["started_at"])
        b = datetime.fromisoformat(meta["ended_at"]) if meta.get("ended_at") else datetime.now(timezone.utc)
        return round((b - a).total_seconds(), 1)
    except (KeyError, ValueError, TypeError):
        return None


def render_header(meta: dict) -> str:
    j = _public(meta)
    parts = [
        f"# Faustus Cookbook job {j['session_id']}",
        f"type: {j.get('kind', '?')}   model: {j.get('model', '?')}   host: {j.get('remote', 'local')}",
        f"status: {j.get('status', '?')}   exit code: {j['exit_code'] if 'exit_code' in j else 'n/a'}",
        f"started: {j.get('started_at', '?')}   ended: {j.get('ended_at', 'still running')}"
        + (f"   duration: {j['duration_s']}s" if j.get("duration_s") is not None else ""),
    ]
    if j.get("cwd"):
        parts.append(f"cwd: {j['cwd']}")
    if j.get("command"):
        parts.append(f"command: {j['command']}")
    if j.get("argv"):
        parts.append("launcher argv: " + " ".join(j["argv"]))
    if j.get("env"):
        parts.append("env: " + " ".join(f"{k}={v}" for k, v in j["env"].items()))
    return "\n".join(parts)


def render_text(session_id: str, stream: str = "combined", tail: int = DEFAULT_TAIL) -> str:
    data = read_log(session_id, stream, tail)
    meta = _load(session_id) or {}
    body = "\n".join(("[stderr] " if l["stderr"] and stream == "combined" else "") + l["text"] for l in data["lines"])
    note = ""
    if not data["stream_available"]:
        note = f"(stream {stream!r} is not available for this job; available: {data['available_streams'] or 'none'})"
    elif data["truncated"]:
        note = f"(showing the last {len(data['lines'])} lines; download the full log for the rest)"
    return "\n".join(x for x in (render_header(meta), f"stream: {stream}", "-" * 40, note, body) if x) + "\n"


def download_path(session_id: str, stream: str = "combined") -> Optional[Path]:
    stream = (stream or "combined").lower()
    if stream not in STREAMS:
        raise JobLogError(f"stream must be one of {list(STREAMS)}")
    _check_id(session_id)
    meta = _load(session_id)
    if meta is None:
        raise KeyError(session_id)
    p = _stream_paths(meta).get(stream)
    return Path(p) if p and os.path.exists(p) else None
