"""Unified degradation diagnostics (Agora #202).

One registry of dependency checks (vector store, web search, notifications,
email, model endpoints, Sparks, MCP servers). Each check reports the same
shape::

    {id, label, state, cause, since, last_checked, latency_ms,
     fix_hint: {es, en, command}, affects: [..]}

``state`` is ``ok | degraded | down | unconfigured``. Informational only:
nothing here blocks a request or a feature.

Design:

* **Reuses the existing probes** in ``src.service_health`` (ChromaDB, SearXNG,
  ntfy, IMAP, providers) and ``src.sparks`` / ``mcp_manager``; it only adds the
  common shape, ``since`` tracking, a cache and the fix hints.
* **Time-boxed.** Every check runs in a worker thread with its own deadline
  (``CHECK_TIMEOUT_S``, a little longer for the fan-out probes). A check that
  overruns becomes ``degraded`` with a ``TimeoutError`` cause instead of
  blocking, and a check that raises becomes ``down`` with the real error class.
* **Never blocks a request.** ``get_report`` serves the cached report while it
  is fresh; once stale it still answers from the cache and refreshes in a
  background thread (single flight). Only the very first call waits.
* **No secrets.** Causes are scrubbed (URL credentials/query, bearer tokens,
  key=value secrets) and truncated.
* **``since``** is the first time a check was seen in its current state, and is
  persisted with the last report to ``DATA_DIR/degradation.json`` so it survives
  restarts and so the separate MCP process can read the running app's view.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
UNCONFIGURED = "unconfigured"
STATES = (OK, DEGRADED, DOWN, UNCONFIGURED)

CACHE_TTL_S = 20.0
CHECK_TIMEOUT_S = 3.0
PROBE_TIMEOUT_S = 4.5
FANOUT_TIMEOUT_S = 6.0
FIRST_CALL_WAIT_S = 9.0
SNAPSHOT_MAX_AGE_S = 120.0

_SEVERITY = {OK: 0, UNCONFIGURED: 0, DEGRADED: 1, DOWN: 2}

STATE_FILE: Optional[str] = None  # tests override; None -> DATA_DIR/degradation.json


def _path() -> str:
    if STATE_FILE:
        return STATE_FILE
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "degradation.json")


# --------------------------------------------------------------------- scrub

_URL_CRED = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@")
_URL_QUERY = re.compile(r"(\bhttps?://[^\s?#]+)[?#]\S*")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_KV_SECRET = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b\s*[=:]\s*\S+")
_LONG_TOKEN = re.compile(r"\b(?:sk|pk|ghp|xox[a-z])[-_][A-Za-z0-9_-]{12,}")


def scrub(text: Any, limit: int = 180) -> str:
    s = " ".join(str(text or "").split())
    s = _URL_CRED.sub(r"\1", s)
    s = _URL_QUERY.sub(r"\1", s)
    s = _BEARER.sub(r"\1 <redacted>", s)
    s = _KV_SECRET.sub(lambda m: m.group(1) + "=<redacted>", s)
    s = _LONG_TOKEN.sub("<redacted>", s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _cause(exc: BaseException) -> str:
    return scrub(f"{type(exc).__name__}: {exc}")


_CATEGORY_CLASS = {
    "timeout": "TimeoutError",
    "connection_refused": "ConnectionRefusedError",
    "dns_error": "gaierror",
    "tls_error": "SSLError",
    "network_error": "OSError",
    "http_error": "HTTPError",
    "auth_or_protocol_error": "AuthenticationError",
    "no_models": "EmptyCatalogError",
    "no_host": "ConfigurationError",
    "not_initialized": "StoreNotInitialized",
    "partial": "StorePartiallyUnavailable",
}


# --------------------------------------------------------------------- hints

_HINTS: Dict[str, Dict[str, str]] = {
    "chromadb": {
        "en": "Start the vector store (ChromaDB), then press Reconnect in Services. Until then document RAG and vector memory fall back to keyword matching.",
        "es": "Arranca el almacen vectorial (ChromaDB) y pulsa Reconectar en Servicios. Mientras tanto el RAG y la memoria vectorial caen a busqueda por palabras.",
        "command": "docker start odysseus-chromadb",
    },
    "searxng": {
        "en": "Start SearXNG or pick another search provider in Settings. Web search and Deep Research return nothing meanwhile.",
        "es": "Arranca SearXNG o elige otro buscador en Ajustes. Mientras tanto la busqueda web y Deep Research no devuelven nada.",
        "command": "docker compose up -d searxng",
    },
    "ntfy": {
        "en": "Check the ntfy server URL and topic in Integrations. Push notifications will not arrive until it answers.",
        "es": "Revisa la URL y el topic de ntfy en Integraciones. Las notificaciones push no llegaran hasta que responda.",
        "command": "",
    },
    "email": {
        "en": "Check the IMAP host, port and credentials of the failing account (Settings > Email). Mail reading, triage and sending for it are affected.",
        "es": "Revisa host, puerto y credenciales IMAP de la cuenta que falla (Ajustes > Correo). Se ven afectadas la lectura, la clasificacion y el envio.",
        "command": "",
    },
    "providers": {
        "en": "A model endpoint is not listing models. If it is local Ollama, start it; otherwise check the base URL (it ends in /v1) and the API key.",
        "es": "Un endpoint de modelos no lista modelos. Si es Ollama local, arrancalo; si no, revisa la URL base (acaba en /v1) y la clave.",
        "command": "ollama serve",
    },
    "sparks": {
        "en": "Prometheus (Sparks) does not answer. Faustus keeps the current connection and default model; start Prometheus or disable Sparks in Settings.",
        "es": "Prometheus (Sparks) no responde. Faustus conserva la conexion y el modelo por defecto actuales; arranca Prometheus o desactiva Sparks en Ajustes.",
        "command": "",
    },
    "mcp_servers": {
        "en": "An MCP server is down or failing. Open Settings > MCP, read its last output and reconnect it.",
        "es": "Un servidor MCP esta caido o falla. Abre Ajustes > MCP, lee su ultima salida y reconectalo.",
        "command": "",
    },
}

_AFFECTS: Dict[str, List[str]] = {
    "chromadb": ["document RAG", "vector memory", "semantic search"],
    "searxng": ["web search", "deep research"],
    "ntfy": ["push notifications", "reminders"],
    "email": ["mail reading", "mail triage", "mail sending"],
    "providers": ["chat with those models", "agent runs", "model catalogue"],
    "sparks": ["Sparks cluster models", "default backend sync"],
    "mcp_servers": ["tools from the failing MCP servers"],
}

_LABELS: Dict[str, str] = {
    "chromadb": "Vector store",
    "searxng": "Web search",
    "ntfy": "Notifications",
    "email": "Email",
    "providers": "Model endpoints",
    "sparks": "Sparks",
    "mcp_servers": "MCP servers",
}


# ------------------------------------------------------------------ registry

class Check:
    __slots__ = ("id", "label", "fn", "affects", "hint", "timeout", "needs_app")

    def __init__(self, id: str, label: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]], *,
                 affects: List[str], hint: Dict[str, str], timeout: float = CHECK_TIMEOUT_S,
                 needs_app: bool = False):
        self.id, self.label, self.fn = id, label, fn
        self.affects, self.hint, self.timeout, self.needs_app = affects, hint, timeout, needs_app


_REGISTRY: Dict[str, Check] = {}
_registry_lock = threading.Lock()


def register(check: Check) -> Check:
    """Add (or replace) a check. Optional subsystems can register their own."""
    with _registry_lock:
        _REGISTRY[check.id] = check
    return check


def unregister(check_id: str) -> None:
    with _registry_lock:
        _REGISTRY.pop(check_id, None)


def checks() -> List[Check]:
    with _registry_lock:
        return list(_REGISTRY.values())


def _result(state: str, cause: str = "") -> Dict[str, Any]:
    return {"state": state, "cause": scrub(cause)}


def _from_service(svc: Dict[str, Any]) -> Dict[str, Any]:
    """Map a ``service_health`` probe dict onto the common result."""
    status = svc.get("status")
    meta = svc.get("meta") or {}
    detail = str(svc.get("detail") or "")
    if status == "disabled":
        return _result(UNCONFIGURED, detail)
    if status == "ok":
        return _result(OK)
    cls = _CATEGORY_CLASS.get(str(meta.get("error") or ""), "")
    cause = f"{cls}: {detail}" if cls else detail
    return _result(DEGRADED if status == "degraded" else DOWN, cause)


def _chromadb(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src.service_health import chromadb_health
    return _from_service(chromadb_health(ctx.get("rag_manager"), ctx.get("memory_vector")))


def _searxng(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src.service_health import searxng_health
    return _from_service(searxng_health(ctx["inputs"]["settings"]))


def _ntfy(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src.service_health import ntfy_health
    return _from_service(ntfy_health(ctx["inputs"]["integrations"], ctx["inputs"]["settings"]))


def _email(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src.service_health import email_health
    return _from_service(email_health(ctx["inputs"]["accounts"]))


def _providers(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src.service_health import providers_health
    return _from_service(providers_health(ctx["inputs"]["endpoints"]))


def _sparks(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from src import sparks
    if not sparks.config().get("enabled"):
        return _result(UNCONFIGURED, "Sparks is disabled")
    st = sparks.status(with_recipes=False)
    if st.get("ok"):
        return _result(OK)
    return _result(DOWN, f"ConnectionError: {st.get('error') or 'Prometheus does not answer'}")


def _mcp(ctx: Dict[str, Any]) -> Dict[str, Any]:
    mgr = ctx.get("mcp_manager")
    if mgr is None:
        return _result(UNCONFIGURED, "MCP manager not available")
    statuses = mgr.get_all_statuses() or {}
    if not statuses:
        return _result(UNCONFIGURED, "no MCP servers configured")
    bad_err, bad_deg = [], []
    for sid, st in statuses.items():
        s = (st or {}).get("status")
        if s == "error":
            bad_err.append((sid, (st or {}).get("error")))
        elif s == "degraded":
            bad_deg.append((sid, (st or {}).get("degraded_reason")))
    if not bad_err and not bad_deg:
        return _result(OK)
    first_sid, first_msg = (bad_err or bad_deg)[0]
    names = ", ".join(sorted(s for s, _ in bad_err + bad_deg)[:5])
    first_line = str(first_msg or "").strip().splitlines()[0] if first_msg else ""
    cls = "ConnectionError" if bad_err else "DegradedServerWarning"
    cause = f"{cls}: {len(bad_err) + len(bad_deg)} of {len(statuses)} servers failing ({names})"
    if first_line:
        cause += f"; {first_sid}: {first_line}"
    all_down = len(bad_err) == len(statuses)
    return _result(DOWN if all_down else DEGRADED, cause)


def _install_builtin() -> None:
    def add(cid: str, fn: Callable, timeout: float = CHECK_TIMEOUT_S, needs_app: bool = False) -> None:
        register(Check(cid, _LABELS[cid], fn, affects=_AFFECTS[cid], hint=_HINTS[cid],
                       timeout=timeout, needs_app=needs_app))
    add("chromadb", _chromadb, needs_app=True)
    add("searxng", _searxng, timeout=PROBE_TIMEOUT_S)  # the probe itself waits up to 4 s
    add("ntfy", _ntfy, timeout=PROBE_TIMEOUT_S)
    add("email", _email, timeout=FANOUT_TIMEOUT_S)
    add("providers", _providers, timeout=FANOUT_TIMEOUT_S)
    add("sparks", _sparks)
    add("mcp_servers", _mcp, needs_app=True)


_install_builtin()


# ------------------------------------------------------------------- running

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gather_inputs() -> Dict[str, Any]:
    from src.service_health import _gather_inputs as gather
    return gather()


def _run_one(chk: Check, ctx: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        out = chk.fn(ctx)
        if out.get("state") not in STATES:
            out = _result(DOWN, f"InvalidResult: unknown state {out.get('state')!r}")
    except Exception as exc:  # noqa: BLE001 - a check must never take the report down
        out = _result(DOWN, _cause(exc))
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out


def run_checks(ctx: Optional[Dict[str, Any]] = None, *, include_app: bool = True) -> List[Dict[str, Any]]:
    """Run every registered check concurrently, each under its own deadline.

    Returns raw results ``{id, state, cause, latency_ms}`` in registry order.
    ``include_app=False`` skips checks that need live app objects (used by the
    separate MCP process).
    """
    ctx = dict(ctx or {})
    selected = [c for c in checks() if include_app or not c.needs_app]
    if not selected:
        return []
    if "inputs" not in ctx:
        try:
            ctx["inputs"] = _gather_inputs()
        except Exception:  # noqa: BLE001
            ctx["inputs"] = {"settings": {}, "integrations": [], "accounts": [], "endpoints": []}
    results: Dict[str, Dict[str, Any]] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="degradation")
    started = time.monotonic()
    try:
        futures = {c.id: pool.submit(_run_one, c, ctx) for c in selected}
        for chk in selected:
            remaining = max(0.05, chk.timeout - (time.monotonic() - started))
            try:
                results[chk.id] = futures[chk.id].result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                r = _result(DEGRADED, f"TimeoutError: no answer within {chk.timeout:g}s")
                r["latency_ms"] = int(chk.timeout * 1000)
                results[chk.id] = r
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [{"id": c.id, **results[c.id]} for c in selected]


# ------------------------------------------------------------ report + state

_lock = threading.Lock()
_cache: Dict[str, Any] = {"report": None, "at": 0.0}
_refreshing = threading.Event()
_since: Dict[str, Dict[str, str]] = {}
_since_loaded = False


def _load_since() -> None:
    global _since_loaded
    if _since_loaded:
        return
    _since_loaded = True
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data.get("items", []):
            if item.get("id") and item.get("state") in STATES and item.get("since"):
                _since.setdefault(item["id"], {"state": item["state"], "since": item["since"]})
    except Exception:  # noqa: BLE001 - first run / unreadable file
        pass


def _fix_hint(chk: Optional[Check]) -> Dict[str, str]:
    h = (chk.hint if chk else {}) or {}
    return {"es": h.get("es", ""), "en": h.get("en", ""), "command": h.get("command", "")}


def build_report(raw: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn raw results into the public report, updating ``since`` tracking."""
    now = _now_iso()
    items: List[Dict[str, Any]] = []
    with _lock:
        _load_since()
        for r in raw:
            chk = _REGISTRY.get(r["id"])
            prev = _since.get(r["id"])
            if not prev or prev["state"] != r["state"]:
                prev = {"state": r["state"], "since": now}
                _since[r["id"]] = prev
            bad = r["state"] in (DEGRADED, DOWN)
            items.append({
                "id": r["id"],
                "label": chk.label if chk else r["id"],
                "state": r["state"],
                "cause": r.get("cause", "") if r["state"] != OK else "",
                "since": prev["since"],
                "last_checked": now,
                "latency_ms": r.get("latency_ms"),
                "fix_hint": _fix_hint(chk) if bad else {"es": "", "en": "", "command": ""},
                "affects": list(chk.affects) if chk else [],
            })
    worst = max((_SEVERITY[i["state"]] for i in items), default=0)
    return {
        "generated_at": now,
        "overall": {0: OK, 1: DEGRADED, 2: DOWN}[worst],
        "counts": {s: sum(1 for i in items if i["state"] == s) for s in STATES},
        "items": items,
        "problems": [i for i in items if i["state"] in (DEGRADED, DOWN)],
    }


def _persist(report: Dict[str, Any]) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - persistence is best effort
        logger.debug("degradation: could not persist snapshot", exc_info=True)


def refresh(ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run all checks now (blocking, bounded) and replace the cache."""
    report = build_report(run_checks(ctx))
    with _lock:
        _cache["report"] = report
        _cache["at"] = time.monotonic()
    _persist(report)
    return report


def _refresh_in_background(ctx: Optional[Dict[str, Any]]) -> None:
    if _refreshing.is_set():
        return
    _refreshing.set()

    def work() -> None:
        try:
            refresh(ctx)
        except Exception:  # noqa: BLE001
            logger.debug("degradation: background refresh failed", exc_info=True)
        finally:
            _refreshing.clear()

    threading.Thread(target=work, name="degradation-refresh", daemon=True).start()


def get_report(ctx: Optional[Dict[str, Any]] = None, *, force: bool = False) -> Dict[str, Any]:
    """Cached report. Fresh -> as is; stale -> still answered, refreshed behind.

    ``force`` re-runs the checks now (still bounded by the per-check deadlines).
    The first ever call has no cache and waits for a refresh, at most
    ``FIRST_CALL_WAIT_S``.
    """
    if force:
        return {**refresh(ctx), "cached": False, "age_s": 0.0}
    with _lock:
        report, at = _cache["report"], _cache["at"]
    if report is not None:
        age = time.monotonic() - at
        if age > CACHE_TTL_S:
            _refresh_in_background(ctx)
        return {**report, "cached": True, "age_s": round(age, 1)}
    _refresh_in_background(ctx)
    deadline = time.monotonic() + FIRST_CALL_WAIT_S
    while time.monotonic() < deadline:
        with _lock:
            report, at = _cache["report"], _cache["at"]
        if report is not None:
            return {**report, "cached": False, "age_s": round(time.monotonic() - at, 1)}
        time.sleep(0.05)
    return {"generated_at": _now_iso(), "overall": OK, "counts": {s: 0 for s in STATES},
            "items": [], "problems": [], "cached": False, "age_s": 0.0, "pending": True}


def snapshot_or_check(*, force: bool = False) -> Dict[str, Any]:
    """For the separate MCP process: the running app's last report when recent,
    otherwise a fresh run of the checks that need no live app objects."""
    if not force:
        try:
            with open(_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["generated_at"])).total_seconds()
            if 0 <= age <= SNAPSHOT_MAX_AGE_S:
                return {**data, "source": "app_snapshot", "age_s": round(age, 1)}
        except Exception:  # noqa: BLE001
            pass
    report = build_report(run_checks(None, include_app=False))
    return {**report, "source": "standalone_probe", "age_s": 0.0,
            "note": "Checks that need the running app (vector store, MCP servers) are not included."}


def reset_for_tests() -> None:
    global _since_loaded
    with _lock:
        _cache.update(report=None, at=0.0)
        _since.clear()
        _since_loaded = False
    _refreshing.clear()
