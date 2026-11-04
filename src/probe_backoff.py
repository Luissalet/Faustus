"""Back-off for capability probes against servers that refuse connections.

Auto-detection (the vision helper, a model's context window or capabilities,
hedge targets) asks local servers what they can do: LM Studio's model list,
llama.cpp's `/props`, Ollama's `/api/show`, an OpenAI-compatible
`/tokenize`. A server that is gone -- seen live: two llama.cpp endpoints whose
model files had been deleted -- refuses every connection, and on Windows a
refused connection to a local port still takes ~2 s (the stack retries the
SYN), so each probe chain cost ~5.5 s per endpoint, every time.

After `FAIL_THRESHOLD` consecutive refusals of the same `scheme://host:port`,
probes to it are skipped for a window that starts at `BASE_S` and doubles on
each further refusal (one probe is let through when the window ends) up to
`MAX_S`. Any answer from the server, a real request that reached it
(`llm_core._clear_host_dead`) or an explicit retry clears it.

Only "connection refused" counts (and a connect timeout to loopback, which is
the same thing reported late). A timeout elsewhere is a busy server, not a
missing one, and stays with the existing per-call cooldown in `llm_core`.
"""
from __future__ import annotations

import errno
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

FAIL_THRESHOLD = 2
BASE_S = 60.0
MAX_S = 900.0

_lock = threading.Lock()
# server -> {"fails": int, "until": epoch, "last_error": str, "last_at": epoch}
_state: Dict[str, Dict[str, Any]] = {}

_REFUSED_ERRNOS = {errno.ECONNREFUSED, 10061}
_REFUSED_TEXT = ("connection refused", "actively refused", "errno 111", "winerror 10061")


def server_key(url: str) -> str:
    """`scheme://host:port` of `url` (lower case), "" when it has no host."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return ""
    if not host:
        return ""
    return f"{(parts.scheme or 'http').lower()}://{host}:{port}"


def is_refused(exc: BaseException) -> bool:
    """Whether `exc` (or what it wraps) is a refused TCP connection."""
    seen = 0
    cur: Optional[BaseException] = exc
    while cur is not None and seen < 6:
        if isinstance(cur, ConnectionRefusedError):
            return True
        if isinstance(cur, OSError) and getattr(cur, "errno", None) in _REFUSED_ERRNOS:
            return True
        if getattr(cur, "winerror", None) == 10061:
            return True
        reason = getattr(cur, "reason", None)  # urllib.error.URLError
        if isinstance(reason, BaseException) and reason is not cur and is_refused(reason):
            return True
        if any(t in str(cur).lower() for t in _REFUSED_TEXT):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


_LOOPBACK = ("127.", "localhost", "[::1]", "::1")


def _loopback_connect_timeout(key: str, exc: BaseException) -> bool:
    """A connect (not read) timeout to this machine's own loopback. A listening
    socket accepts at once even when its server is busy, so on loopback this is
    a refusal that outlasted a short probe timeout (Windows needs ~2 s to
    report one): LM Studio's 1 s probe of a dead llama.cpp port ends this way."""
    host = key.split("://", 1)[-1].rsplit(":", 1)[0]
    if not host.startswith(_LOOPBACK):
        return False
    names = {cls.__name__ for cls in type(exc).__mro__}
    return "ConnectTimeout" in names


def _window(fails: int) -> float:
    over = max(0, fails - FAIL_THRESHOLD)
    return min(MAX_S, BASE_S * (2 ** over))


def note_failure(url: str, exc: BaseException) -> bool:
    """Record a failed probe of `url`. Returns True when the server is now
    being skipped. Errors other than a refused connection reset nothing and
    count nothing."""
    key = server_key(url)
    if not key:
        return False
    if not (is_refused(exc) or _loopback_connect_timeout(key, exc)):
        return False
    now = time.time()
    with _lock:
        st = _state.setdefault(key, {"fails": 0, "until": 0.0, "last_error": "", "last_at": 0.0})
        st["fails"] = int(st.get("fails") or 0) + 1
        st["last_error"] = str(exc)[:200]
        st["last_at"] = now
        if st["fails"] >= FAIL_THRESHOLD:
            st["until"] = now + _window(st["fails"])
            return True
        return False


def note_success(url: str) -> None:
    """The server answered (any HTTP status): forget its refusals."""
    key = server_key(url)
    if not key:
        return
    with _lock:
        _state.pop(key, None)


def skipped(url: str) -> bool:
    """Whether probes to `url` are currently being skipped."""
    key = server_key(url)
    if not key:
        return False
    with _lock:
        st = _state.get(key)
        return bool(st and st.get("until", 0.0) > time.time())


def should_skip(url: str) -> bool:
    """`skipped(url)`, or the shared dead-host cooldown of `llm_core` covers it.
    The one check auto-detection paths use before probing a server."""
    if skipped(url):
        return True
    try:
        from src.llm_core import _is_host_dead
        return bool(_is_host_dead(url))
    except Exception:  # noqa: BLE001 - the cooldown is an optimisation
        return False


def clear(url: Optional[str] = None) -> int:
    """Forget the refusals of `url`'s server, or of every server. Returns how
    many entries were removed."""
    with _lock:
        if not url:
            n = len(_state)
            _state.clear()
            return n
        return 1 if _state.pop(server_key(url), None) is not None else 0


def status() -> List[Dict[str, Any]]:
    """Every server with recorded refusals, for the REST/MCP/Settings views:
    `{server, failures, skipped, retry_in_s, until, last_error, last_at}`."""
    now = time.time()
    out: List[Dict[str, Any]] = []
    with _lock:
        items = [(k, dict(v)) for k, v in _state.items()]
    for key, st in sorted(items):
        until = float(st.get("until") or 0.0)
        out.append({
            "server": key,
            "failures": int(st.get("fails") or 0),
            "skipped": until > now,
            "retry_in_s": round(max(0.0, until - now), 1),
            "until": until or None,
            "last_error": st.get("last_error") or "",
            "last_at": st.get("last_at") or None,
        })
    return out
