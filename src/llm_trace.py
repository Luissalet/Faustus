"""src/llm_trace.py — debugging aid: record every model call of a session.

Append-only JSONL per session under ``DATA_DIR/llm_traces/<session_id>.jsonl``
(one line per call: request + assembled response, secrets redacted). This is
NOT a metrics/analytics store — it exists so a human debugging a bad agent
turn can open the exact request that produced it, and "fork" it (re-send the
same request to another model) to compare answers. See ``routes/llm_trace_routes.py``
for the read/fork API and ``docs/api/llm_traces.md`` (if present) for the wire
shape.

Design constraints (per the feature spec):
* Tracing must NEVER break or slow down a real LLM call. Every public
  function here is wrapped so an internal failure only logs a warning.
* No session known -> no record (there is nothing useful to key it by, and
  writing into a shared "no session" bucket would mix unrelated turns).
* Secrets (Authorization headers, api_key/token fields, bearer strings found
  anywhere in the payload, including nested dicts/lists) are stripped before
  anything touches disk.
* A request whose messages are enormous is not stored verbatim — only a
  sha256 fingerprint + a size note, so one huge turn cannot blow up the trace
  file or the debugging UI that lists it.
* Old trace files are deleted opportunistically (no separate cron) once they
  are older than the retention window.

Hook points: ``src/llm_core.py``'s two public entry points,
``llm_call_async`` (non-streaming) and ``stream_llm`` (streaming, which
accumulates chunks as they pass through and records once at the end/on
error/on cancel). Both already carry ``session_id`` as an explicit parameter
— no new ContextVar was needed.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# Single background worker: trace writes are small and sequential per
# process, so one thread is plenty and keeps them strictly ordered without
# extra locking machinery beyond the per-session seq lock below.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-trace")

_SEQ_LOCK = threading.Lock()
_SEQ_CACHE: Dict[str, int] = {}

_RETENTION_LOCK = threading.Lock()
_last_retention_sweep = 0.0
# Do not stat every trace file on every single call; a sweep every 10
# minutes of wall clock is more than enough for an "opportunistic" cleanup.
_RETENTION_SWEEP_MIN_INTERVAL_S = 600.0


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def tracing_enabled() -> bool:
    try:
        return bool(_setting("llm_trace_enabled", True))
    except Exception:
        return True


def _max_request_chars() -> int:
    try:
        return int(_setting("llm_trace_max_request_chars", 2_000_000))
    except Exception:
        return 2_000_000


def _retention_days() -> int:
    try:
        return int(_setting("llm_trace_retention_days", 7))
    except Exception:
        return 7


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def _traces_dir() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    d = os.path.join(DATA_DIR, "llm_traces")
    os.makedirs(d, exist_ok=True)
    return d


def _log_path(session_id: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", str(session_id))[:120]
    return os.path.join(_traces_dir(), safe + ".jsonl")


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(authorization|api[_-]?key|apikey|token|secret|password|"
    r"bearer|x-api-key|cookie)",
    re.IGNORECASE,
)
_BEARER_STR_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_REDACTED = "[REDACTED]"


def _redact_string(value: str) -> str:
    if _BEARER_STR_RE.search(value):
        return _BEARER_STR_RE.sub(f"Bearer {_REDACTED}", value)
    return value


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively strip anything that looks like a credential.

    Any dict key matching ``_SECRET_KEY_RE`` has its value replaced
    wholesale; every remaining string is scanned for an inline
    ``Bearer <token>`` and has just the token redacted. Depth-capped so a
    pathological/cyclic structure cannot recurse forever.
    """
    if _depth > 20:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = redact(v, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, tuple):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


# ---------------------------------------------------------------------------
# size guard
# ---------------------------------------------------------------------------

def _guard_messages(request: Dict[str, Any]) -> Dict[str, Any]:
    """Replace ``request['messages']`` with a fingerprint when it is too
    large to store verbatim, so one oversized turn cannot balloon the trace
    file or whatever renders it."""
    messages = request.get("messages")
    if messages is None:
        return request
    try:
        raw = json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:
        raw = str(messages)
    limit = _max_request_chars()
    if len(raw) <= limit:
        return request
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    out = dict(request)
    out["messages"] = None
    out["messages_omitted"] = {
        "reason": "exceeds llm_trace_max_request_chars",
        "sha256": digest,
        "chars": len(raw),
        "limit": limit,
    }
    return out


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def _sweep_retention() -> None:
    global _last_retention_sweep
    now = time.time()
    with _RETENTION_LOCK:
        if now - _last_retention_sweep < _RETENTION_SWEEP_MIN_INTERVAL_S:
            return
        _last_retention_sweep = now
    try:
        days = _retention_days()
        if days <= 0:
            return
        cutoff = now - days * 86400
        for path in glob.glob(os.path.join(_traces_dir(), "*.jsonl")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except Exception:
        logger.debug("[llm_trace] retention sweep failed", exc_info=True)


# ---------------------------------------------------------------------------
# seq allocation
# ---------------------------------------------------------------------------

def _next_seq(session_id: str) -> int:
    with _SEQ_LOCK:
        seq = _SEQ_CACHE.get(session_id)
        if seq is None:
            seq = 0
            path = _log_path(session_id)
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.strip():
                                seq += 1
                except OSError:
                    pass
        seq += 1
        _SEQ_CACHE[session_id] = seq
        return seq


# ---------------------------------------------------------------------------
# public: record a call
# ---------------------------------------------------------------------------

def _endpoint_host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(str(url))
        host = p.hostname or ""
        if p.path:
            # keep the path, drop query string and any embedded credentials
            return f"{p.scheme}://{host}{':' + str(p.port) if p.port else ''}{p.path}"
        return f"{p.scheme}://{host}{':' + str(p.port) if p.port else ''}"
    except Exception:
        return None


def _provider_kind(url: Optional[str]) -> Optional[str]:
    try:
        from src.llm_core import _detect_provider
        return _detect_provider(url or "")
    except Exception:
        return None


def _write_line(path: str, record: Dict[str, Any]) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.warning("[llm_trace] could not write trace record", exc_info=True)
    try:
        _sweep_retention()
    except Exception:
        pass


def record_call(
    *,
    session_id: Optional[str],
    run_id: Optional[str] = None,
    endpoint_url: Optional[str] = None,
    model: Optional[str] = None,
    request: Optional[Dict[str, Any]] = None,
    response_text: str = "",
    thinking_text: str = "",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """Best-effort, fire-and-forget trace of one model call.

    No-op (silently) when ``session_id`` is falsy or tracing is disabled.
    The actual disk write happens on a background thread so a slow disk
    never adds latency to the call this is tracing.
    """
    if not session_id:
        return
    try:
        if not tracing_enabled():
            return
        req = redact(dict(request or {}))
        req = _guard_messages(req)
        record = {
            "seq": None,  # filled in on the writer thread, ordered there
            "ts": time.time(),
            "session_id": str(session_id),
            "run_id": run_id,
            "endpoint": _endpoint_host(endpoint_url),
            "provider": _provider_kind(endpoint_url),
            "model": model,
            "request": req,
            "response_text": response_text or "",
            "thinking_text": thinking_text or "",
            "tool_calls": tool_calls or [],
            "finish_reason": finish_reason,
            "usage": usage or {},
            "duration_ms": duration_ms,
            "error": error,
        }
        _EXECUTOR.submit(_record_on_thread, str(session_id), record)
    except Exception:
        logger.warning("[llm_trace] record_call failed", exc_info=True)


def _record_on_thread(session_id: str, record: Dict[str, Any]) -> None:
    try:
        record["seq"] = _next_seq(session_id)
        _write_line(_log_path(session_id), record)
    except Exception:
        logger.warning("[llm_trace] background write failed", exc_info=True)


def flush_for_tests(timeout: float = 5.0) -> None:
    """Block until every ``record_call`` submitted so far has been written.

    The background executor is a single FIFO worker, so waiting on a no-op
    submitted after real work guarantees that work already completed. Test
    helper only — production code never needs synchronous tracing."""
    _EXECUTOR.submit(lambda: None).result(timeout=timeout)


# ---------------------------------------------------------------------------
# stream accumulation
# ---------------------------------------------------------------------------

class StreamAccumulator:
    """Consumes the same SSE-ish chunks ``stream_llm`` yields and rebuilds a
    response record out of them, without needing to understand every
    provider's wire format (the chunks are already normalized by llm_core
    into a small closed vocabulary of ``data: {...}`` events)."""

    __slots__ = ("text_parts", "thinking_parts", "tool_calls", "finish_reason", "usage", "error")

    def __init__(self) -> None:
        self.text_parts: List[str] = []
        self.thinking_parts: List[str] = []
        self.tool_calls: List[Dict[str, Any]] = []
        self.finish_reason: Optional[str] = None
        self.usage: Dict[str, Any] = {}
        self.error: Optional[str] = None

    def feed(self, chunk: str) -> None:
        try:
            is_error_event = False
            for line in str(chunk).splitlines():
                if line.startswith("event:"):
                    is_error_event = line[6:].strip() == "error"
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if is_error_event or data.get("error"):
                    self.error = str(data.get("error") or data.get("text") or "stream error")
                    continue
                kind = data.get("type")
                if kind == "tool_calls":
                    calls = data.get("calls")
                    if isinstance(calls, list):
                        self.tool_calls = calls
                elif kind == "tool_call_delta":
                    # Deltas are informative during the stream; the final
                    # `tool_calls` event above carries the assembled result,
                    # so deltas are not accumulated separately here.
                    pass
                elif kind == "usage":
                    d = data.get("data")
                    if isinstance(d, dict):
                        self.usage = d
                elif kind == "finish":
                    self.finish_reason = data.get("finish_reason")
                elif kind == "model_actual":
                    pass
                else:
                    delta = data.get("delta")
                    if isinstance(delta, str):
                        if data.get("thinking"):
                            self.thinking_parts.append(delta)
                        else:
                            self.text_parts.append(delta)
        except Exception:
            logger.debug("[llm_trace] StreamAccumulator.feed failed", exc_info=True)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def thinking(self) -> str:
        return "".join(self.thinking_parts)


# ---------------------------------------------------------------------------
# public: read back for the API layer
# ---------------------------------------------------------------------------

def _iter_records(session_id: str):
    path = _log_path(session_id)
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return


def list_calls(session_id: str) -> List[Dict[str, Any]]:
    """Summary rows for GET /api/llm-traces/{session_id}."""
    out: List[Dict[str, Any]] = []
    for rec in _iter_records(session_id):
        text = rec.get("response_text") or ""
        out.append({
            "seq": rec.get("seq"),
            "ts": rec.get("ts"),
            "model": rec.get("model"),
            "endpoint": rec.get("endpoint"),
            "provider": rec.get("provider"),
            "duration_ms": rec.get("duration_ms"),
            "response_chars": len(text),
            "response_preview": text[:200],
            "finish_reason": rec.get("finish_reason"),
            "error": rec.get("error"),
            "has_tool_calls": bool(rec.get("tool_calls")),
        })
    return out


def get_call(session_id: str, seq: int) -> Optional[Dict[str, Any]]:
    """Full record for GET /api/llm-traces/{session_id}/{seq}."""
    for rec in _iter_records(session_id):
        if rec.get("seq") == seq:
            return rec
    return None
