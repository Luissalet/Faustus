"""Hedged first token for streaming chat requests.

A streamed request to the primary model that has produced no first token
(content, reasoning or a tool-call delta) after ``agent_hedge_delay_s``
seconds is sent again to a hedge target, and whichever stream produces its
first token first answers. The other stream is cancelled: its producer task
closes the generator, which closes the HTTP response, so a vLLM server sees
the client go away and aborts the request.

Hedging only happens before anything has been streamed to the caller, so a
tool call or a token is never produced twice. Once either stream has
released a first token the race is over and nothing else is started.

Hedge target (``agent_hedge_target``), always on the global LLM service (an
endpoint published by a Sparks recipe, or another endpoint pointing at one of
those servers); a local text model is never used as the alternative:

- ``auto``: the same model on another enabled endpoint when one lists it,
  otherwise the first configured fallback of the chain, otherwise a re-sent
  request to the same server (``reissue``).
- ``same_model``: only the same model on another endpoint.
- ``fallback``: only the first configured fallback of the chain.
- ``reissue``: the identical request sent again to the SAME endpoint and
  model (a new HTTP request, so the server gives it a new request id). It
  targets a single request the server accepted and then holds without
  emitting while the server itself is healthy. Guarded: at most
  ``agent_hedge_reissue_max`` re-sends per agent run (per-model overrides in
  ``agent_hedge_reissue_overrides``), and none when the server reports
  requests waiting in its queue (vLLM ``num_requests_waiting``, SGLang
  ``num_queue_reqs``, or Prometheus's ``/api/serving``), so a re-sent request
  never queues behind its own stuck sibling. A skipped re-send is recorded
  with its reason.
- ``<endpoint_id>``: the same model on that endpoint.
- ``<endpoint_id>::<model>``: a named model on that endpoint.
- ``off``: never hedge (same as ``agent_hedge_enabled`` = false).

The hedge request is prepared like any candidate of the chain (the caller's
``candidate_request_factory`` builds its messages, tools and limits for that
route, even when the route is not part of the chain), and a hedge that
answers is announced as a fallback so the caller adopts the route and model
that really answered. A re-send (``reissue``) instead reuses the request the
primary was sent (same messages, tools and limits) and needs no adoption: the
route and model are the primary's.

Every race that started a hedge is recorded (in memory, and appended to
``DATA_DIR/model_hedges.jsonl``) with the run id of the agent run it
belongs to, so the turn's metrics and the Settings card can say
"answered by hedge (endpoint X) after 23 s".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SETTING_ENABLED = "agent_hedge_enabled"
SETTING_DELAY = "agent_hedge_delay_s"
SETTING_TARGET = "agent_hedge_target"
SETTING_OVERRIDES = "agent_hedge_delay_overrides"
SETTING_REISSUE_MAX = "agent_hedge_reissue_max"
SETTING_REISSUE_OVERRIDES = "agent_hedge_reissue_overrides"
SETTING_KEYS = (SETTING_ENABLED, SETTING_DELAY, SETTING_TARGET, SETTING_OVERRIDES,
                SETTING_REISSUE_MAX, SETTING_REISSUE_OVERRIDES)

DEFAULT_DELAY_S = 20.0
MIN_DELAY_S = 1.0
MAX_DELAY_S = 600.0
DEFAULT_REISSUE_MAX = 1
MAX_REISSUE_MAX = 5
CAPACITY_TIMEOUT_S = 2.5
TARGET_MODES = ("auto", "same_model", "fallback", "reissue", "off")
_LOG_NAME = "model_hedges.jsonl"
_LOG_MAX_LINES = 2000
_RECENT: "deque[Dict[str, Any]]" = deque(maxlen=200)
_LOCK = threading.Lock()


def _get_setting(key: str, default: Any = None) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - an unreadable store keeps the default
        return default


def _clamp_delay(value: Any, default: float = DEFAULT_DELAY_S) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(MIN_DELAY_S, min(MAX_DELAY_S, number))


def parse_overrides(raw: Any) -> Dict[str, float]:
    """``{model: seconds}`` from the JSON text setting; bad entries are dropped."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for model, seconds in raw.items():
        name = str(model or "").strip()
        if not name or isinstance(seconds, bool):
            continue
        try:
            out[name] = _clamp_delay(float(seconds))
        except (TypeError, ValueError):
            continue
    return out


def _clamp_reissues(value: Any, default: int = DEFAULT_REISSUE_MAX) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_REISSUE_MAX, number))


def parse_reissue_overrides(raw: Any) -> Dict[str, int]:
    """``{model: re-sends per run}`` from the JSON text setting; 0 turns them off."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for model, count in raw.items():
        name = str(model or "").strip()
        if not name or isinstance(count, bool):
            continue
        try:
            out[name] = _clamp_reissues(float(count))
        except (TypeError, ValueError):
            continue
    return out


def config() -> Dict[str, Any]:
    target = str(_get_setting(SETTING_TARGET, "auto") or "auto").strip() or "auto"
    return {
        "enabled": bool(_get_setting(SETTING_ENABLED, True)),
        "delay_s": _clamp_delay(_get_setting(SETTING_DELAY, DEFAULT_DELAY_S)),
        "target": target,
        "delay_overrides": parse_overrides(_get_setting(SETTING_OVERRIDES, "{}")),
        "reissue_max": _clamp_reissues(_get_setting(SETTING_REISSUE_MAX, DEFAULT_REISSUE_MAX)),
        "reissue_overrides": parse_reissue_overrides(_get_setting(SETTING_REISSUE_OVERRIDES, "{}")),
    }


def reissues_for(model: str, cfg: Optional[Dict[str, Any]] = None) -> int:
    """Re-sent requests allowed per agent run for this model."""
    cfg = cfg or config()
    wanted = _model_key(model)
    for name, count in (cfg.get("reissue_overrides") or {}).items():
        if _model_key(name) == wanted:
            return int(count)
    value = cfg.get("reissue_max")
    return DEFAULT_REISSUE_MAX if value is None else int(value)


def _model_key(model: str) -> str:
    return str(model or "").strip().lower()


def delay_for(model: str, cfg: Optional[Dict[str, Any]] = None) -> float:
    """Seconds without a first token before the hedge starts, for this model."""
    cfg = cfg or config()
    overrides = cfg.get("delay_overrides") or {}
    wanted = _model_key(model)
    for name, seconds in overrides.items():
        if _model_key(name) == wanted:
            return seconds
    return float(cfg.get("delay_s") or DEFAULT_DELAY_S)


def _base(url: str) -> str:
    try:
        from src.llm_core import _model_list_base
        return _model_list_base(url)
    except Exception:  # noqa: BLE001
        return str(url or "").rstrip("/")


def _host_dead(url: str) -> bool:
    """A server in the dead-host cooldown or whose probes keep being refused
    (`src.probe_backoff`) is no hedge target."""
    try:
        from src import probe_backoff
        return bool(probe_backoff.should_skip(url))
    except Exception:  # noqa: BLE001
        return False


def _target(url: str, model: str, headers: Optional[Dict], descriptor: Optional[Dict],
            kind: str, candidate_index: Optional[int] = None) -> Dict[str, Any]:
    descriptor = descriptor or {}
    return {
        "kind": kind,
        "url": url,
        "model": model,
        "headers": dict(headers or {}),
        "endpoint_id": descriptor.get("endpoint_id"),
        "endpoint_label": descriptor.get("endpoint_label") or _base(url),
        "endpoint_cost_tracked": descriptor.get("endpoint_cost_tracked"),
        "connection_revision": descriptor.get("connection_revision") or "",
        "candidate_index": candidate_index,
    }


# ---------------------------------------------------------------------------
# the global LLM service: the only place a hedge may go
# ---------------------------------------------------------------------------
# Text generation runs on the global service (the Sparks); a local text model
# is never used as the alternative when it is slow. So a hedge target must be
# an endpoint published by a Sparks recipe (settings ``sparks_endpoints``) or
# another endpoint pointing at one of those servers (same scheme, host and
# port, e.g. a hand-made endpoint for a Spark). Nothing here is a fixed IP or
# model: the set follows whatever the Sparks sync published.

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _server(url: str) -> str:
    """``scheme://host:port`` of a URL, lower case; "" when it has no host."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return ""
    if not host:
        return ""
    return f"{(parts.scheme or 'http').lower()}://{host}:{port}"


def _load_settings() -> Dict[str, Any]:
    try:
        from src.settings import load_settings
        return load_settings() or {}
    except Exception:  # noqa: BLE001
        return {}


def _endpoint_rows(enabled_only: bool = True) -> List[Dict[str, Any]]:
    """Endpoints of the table (not official clients) with the models they list."""
    try:
        from src.database import ModelEndpoint, SessionLocal
        from src.endpoint_resolver import _endpoint_enabled_models
    except Exception:  # noqa: BLE001
        return []
    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint)
        if enabled_only:
            query = query.filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        out = []
        for ep in query.all():
            base = str(getattr(ep, "base_url", "") or "")
            if not base or base.startswith("faustus-cli://"):
                continue
            out.append({
                "endpoint_id": str(ep.id),
                "label": getattr(ep, "name", None) or str(ep.id),
                "base_url": base,
                "enabled": bool(getattr(ep, "is_enabled", True)),
                "models": list(_endpoint_enabled_models(ep) or []),
            })
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("[hedge] endpoint listing failed: %s", exc)
        return []
    finally:
        db.close()


def global_service() -> Dict[str, Any]:
    """``{"endpoint_ids", "servers"}`` of the global LLM service (the Sparks)."""
    settings = _load_settings()
    if not settings.get("sparks_enabled", True):
        return {"endpoint_ids": set(), "servers": set()}
    ids = {str(v) for v in (settings.get("sparks_endpoints") or {}).values() if v}
    servers = set()
    if ids:
        for row in _endpoint_rows(enabled_only=False):
            if row["endpoint_id"] in ids:
                server = _server(row["base_url"])
                host = server.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]")
                if server and host not in _LOOPBACK:
                    servers.add(server)
    return {"endpoint_ids": ids, "servers": servers}


def is_global(endpoint_id: Any, url: str, service: Optional[Dict[str, Any]] = None) -> bool:
    """True when this route belongs to the global LLM service."""
    service = service if service is not None else global_service()
    if endpoint_id and str(endpoint_id) in service.get("endpoint_ids", ()):
        return True
    server = _server(url)
    return bool(server) and server in service.get("servers", ())


def endpoint_models(service: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Enabled endpoints of the global LLM service with the models they list:
    the only endpoints a hedge may use."""
    service = service if service is not None else global_service()
    out = []
    for row in _endpoint_rows(enabled_only=True):
        if not is_global(row["endpoint_id"], row["base_url"], service):
            continue
        out.append({
            "endpoint_id": row["endpoint_id"],
            "label": row["label"],
            "base": _base(row["base_url"]),
            "models": row["models"],
        })
    return out


def _resolve(endpoint_id: str, model: str):
    try:
        from src.endpoint_resolver import _resolve_endpoint_by_id_with_descriptor
        return _resolve_endpoint_by_id_with_descriptor(endpoint_id, model, require_exact_model=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[hedge] could not resolve %s/%s: %s", endpoint_id, model, exc)
        return None


def same_model_elsewhere(url: str, model: str,
                         service: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The same model on another enabled endpoint of the global service."""
    service = service if service is not None else global_service()
    primary = _base(url)
    wanted = _model_key(model)
    for ep in endpoint_models(service):
        if ep["base"] == primary:
            continue
        match = next((m for m in ep["models"] if _model_key(m) == wanted), None)
        if not match:
            continue
        resolved = _resolve(ep["endpoint_id"], match)
        if not resolved:
            continue
        (h_url, h_model, h_headers), descriptor = resolved
        if _base(h_url) == primary or _host_dead(h_url):
            continue
        if not is_global(descriptor.get("endpoint_id") or ep["endpoint_id"], h_url, service):
            continue
        return _target(h_url, h_model, h_headers, descriptor, "same_model")
    return None


def _chain_fallback(url: str, candidates, descriptors,
                    service: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The first fallback of the chain that belongs to the global service."""
    service = service if service is not None else global_service()
    primary = _base(url)
    for index, cand in enumerate(list(candidates or [])[1:], start=1):
        try:
            c_url, c_model, c_headers = cand
        except (TypeError, ValueError):
            continue
        if not c_url or str(c_url).startswith("faustus-cli://") or _host_dead(c_url):
            continue
        descriptor = descriptors[index] if descriptors and index < len(descriptors) else {}
        if not is_global((descriptor or {}).get("endpoint_id"), c_url, service):
            continue  # a local text model is never the alternative
        kind = "same_model" if (_model_key(c_model) == _model_key(candidates[0][1])
                                and _base(c_url) != primary) else "fallback"
        return _target(c_url, c_model, c_headers, descriptor, kind, candidate_index=index)
    return None


def _named(spec: str, url: str, model: str, candidates,
           service: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    service = service if service is not None else global_service()
    endpoint_id, _, named_model = spec.partition("::")
    endpoint_id, named_model = endpoint_id.strip(), (named_model.strip() or model)
    resolved = _resolve(endpoint_id, named_model)
    if not resolved:
        return None
    (h_url, h_model, h_headers), descriptor = resolved
    if not is_global(descriptor.get("endpoint_id") or endpoint_id, h_url, service):
        logger.info("[hedge] target %s is not on the global LLM service; no hedge", spec)
        return None
    if _base(h_url) == _base(url) and _model_key(h_model) == _model_key(model):
        return None  # the primary itself is not a hedge
    kind = "same_model" if _model_key(h_model) == _model_key(model) else "named"
    index = None
    for i, cand in enumerate(list(candidates or [])):
        if i and _base(cand[0]) == _base(h_url) and _model_key(cand[1]) == _model_key(h_model):
            index = i
            break
    return _target(h_url, h_model, h_headers, descriptor, kind, candidate_index=index)


def _reissue(url: str, model: str, headers: Optional[Dict], descriptors,
             service: Optional[Dict[str, Any]] = None,
             cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The primary route itself, for a re-sent request; only on the global
    service and only when this model allows at least one re-send per run."""
    service = service if service is not None else global_service()
    descriptor = (descriptors[0] if descriptors else None) or {}
    if not is_global(descriptor.get("endpoint_id"), url, service):
        return None
    if reissues_for(model, cfg) <= 0:
        return None
    found = _target(url, model, headers, descriptor, "reissue", candidate_index=0)
    found["reissue_max"] = reissues_for(model, cfg)
    return found


# ---------------------------------------------------------------------------
# re-send guards: per-run budget and the server's own queue
# ---------------------------------------------------------------------------

_REISSUES: Dict[str, int] = {}
_REISSUES_MAX_KEYS = 500
_WAITING_METRICS = ("vllm:num_requests_waiting", "sglang:num_queue_reqs")
_RUNNING_METRICS = ("vllm:num_requests_running", "sglang:num_running_reqs")


def _metric_sum(text: str, names) -> Optional[float]:
    total, seen = 0.0, False
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name not in names:
            continue
        try:
            total += float(line.rsplit(" ", 1)[-1])
            seen = True
        except ValueError:
            continue
    return total if seen else None


def _get_text(url: str, timeout: float) -> Optional[str]:
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "text/plain"}),
                         timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read(4_000_000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - closed, refused, timeout
        logger.debug("[hedge] %s unreadable: %s", url, exc)
        return None


def _serving_capacity(url: str) -> Optional[Dict[str, Any]]:
    """Waiting/running from Prometheus's ``/api/serving`` for this server."""
    try:
        from src import sparks
        code, body = sparks._request("GET", "/api/serving", timeout=CAPACITY_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None
    if code != 200 or not isinstance(body, dict):
        return None
    server = _server(url)
    for ep in body.get("endpoints") or []:
        if not isinstance(ep, dict) or _server(ep.get("base_url") or "") != server:
            continue
        now = ep.get("now") or {}
        if now.get("waiting") is None:
            return None
        return {"waiting": float(now.get("waiting") or 0), "running": now.get("running"),
                "source": "prometheus /api/serving"}
    return None


def server_capacity(url: str) -> Dict[str, Any]:
    """What the server says about its queue: ``{known, waiting, running,
    saturated, source}``. Read from the server's ``/metrics`` (vLLM or
    SGLang), else from Prometheus's ``/api/serving``. Unknown is not saturated."""
    server = _server(url)
    out: Dict[str, Any] = {"known": False, "waiting": None, "running": None,
                           "saturated": False, "source": ""}
    text = _get_text(server + "/metrics", CAPACITY_TIMEOUT_S) if server else None
    waiting = _metric_sum(text, _WAITING_METRICS) if text else None
    if waiting is not None:
        out.update(known=True, waiting=waiting, running=_metric_sum(text, _RUNNING_METRICS),
                   source=f"{server}/metrics")
    else:
        found = _serving_capacity(url)
        if found:
            out.update(known=True, **found)
    out["saturated"] = bool(out["known"] and (out["waiting"] or 0) > 0)
    return out


def _run_key(session_id: Optional[str] = None) -> str:
    try:
        from src.llm_trace import current_run_id
        run_id = current_run_id() or ""
    except Exception:  # noqa: BLE001
        run_id = ""
    if run_id:
        return f"run:{run_id}"
    return f"session:{session_id}" if session_id else ""


def reissue_gate(url: str, model: str, *, session_id: Optional[str] = None,
                 cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide, when the delay has passed, whether the re-send may go out.

    ``{ok, reason, used, limit, capacity}``; ``reason`` is ``limit`` (this run
    already spent its re-sends) or ``saturated`` (the server has requests
    waiting, so the copy would queue behind its own stuck sibling). A re-send
    that is allowed is counted against the run at once."""
    limit = reissues_for(model, cfg)
    key = _run_key(session_id)
    with _LOCK:
        used = _REISSUES.get(key, 0) if key else 0
    if limit <= 0 or used >= limit:
        return {"ok": False, "reason": "limit", "used": used, "limit": limit, "capacity": None}
    capacity = server_capacity(url)
    if capacity.get("saturated"):
        return {"ok": False, "reason": "saturated", "used": used, "limit": limit, "capacity": capacity}
    if key:
        with _LOCK:
            _REISSUES[key] = _REISSUES.get(key, 0) + 1
            while len(_REISSUES) > _REISSUES_MAX_KEYS:
                _REISSUES.pop(next(iter(_REISSUES)))
    return {"ok": True, "reason": "", "used": used + 1, "limit": limit, "capacity": capacity}


def plan(url: str, model: str, headers: Optional[Dict] = None, candidates=None,
         descriptors=None, cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The hedge for a primary route, or None when hedging does not apply."""
    cfg = cfg or config()
    if not cfg.get("enabled") or not url or str(url).startswith("faustus-cli://"):
        return None
    spec = str(cfg.get("target") or "auto").strip()
    mode = spec.lower()
    candidates = list(candidates or [(url, model, headers or {})])
    if mode == "off":
        return None
    service = global_service()
    found = None
    if mode in ("auto", "same_model"):
        found = same_model_elsewhere(url, model, service)
        if found is None and mode == "auto":
            found = _chain_fallback(url, candidates, descriptors, service)
    elif mode == "fallback":
        found = _chain_fallback(url, candidates, descriptors, service)
    elif mode != "reissue":
        found = _named(spec, url, model, candidates, service)
    if found is None and mode in ("auto", "reissue"):
        # No other route of the global service: send the same request again
        # to the same server (a single held sequence, not a sick server).
        found = _reissue(url, model, headers, descriptors, service, cfg)
    if found is None:
        return None
    found["delay_s"] = delay_for(model, cfg)
    found["mode"] = mode if mode in TARGET_MODES else "named"
    return found


# ---------------------------------------------------------------------------
# the race
# ---------------------------------------------------------------------------

def _event(chunk: str) -> Dict[str, Any]:
    for line in str(chunk).splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
            except (ValueError, TypeError):
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def first_token(chunk: str) -> bool:
    """Content, reasoning or a tool call released by the model."""
    try:
        from src.first_token_deadline import meaningful
        if meaningful(chunk):
            return True
    except Exception:  # noqa: BLE001
        pass
    data = _event(chunk)
    return data.get("type") == "tool_calls" and bool(data.get("calls"))


def _is_error(chunk: str) -> bool:
    return str(chunk).startswith("event: error")


def _passes_through(chunk: str) -> bool:
    """Status frames a waiting user should see even before the race is decided."""
    data = _event(chunk)
    return data.get("type") == "harness_check" and data.get("reason") == "sparks_service_unavailable"


async def _produce(tag: str, source, queue: "asyncio.Queue") -> None:
    """One task owns each stream for its whole life, cleanup included."""
    try:
        async for chunk in source:
            await queue.put((tag, "chunk", chunk))
        await queue.put((tag, "end", None))
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        await queue.put((tag, "end", None))
    except Exception as exc:  # noqa: BLE001 - handed to the consumer
        await queue.put((tag, "error", exc))
    finally:
        close = getattr(source, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[hedge] closing the %s stream failed: %s", tag, exc)


async def _stop(task: Optional["asyncio.Task"]) -> bool:
    """Cancel a producer and wait until its stream is closed."""
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("[hedge] stopped stream raised %s", exc)
    return True


async def race_first_token(
    primary,
    start_hedge: Callable[[], Awaitable[Any]],
    delay_s: float,
    *,
    on_decided: Optional[Callable[[Dict[str, Any]], Any]] = None,
):
    """Yield the primary stream, or the hedge when it answers first.

    ``start_hedge`` is called once, only when ``delay_s`` passes without a
    first token from the primary, and returns the hedge stream (or None). It
    runs in its own task (preparing the hedge request may take a while), so
    the primary keeps being read meanwhile; a primary that answers during
    that preparation wins at once and the preparation is cancelled.
    Before a winner is known every chunk is held, so nothing reaches the
    caller twice. ``on_decided`` is called once when a hedge was started and
    may return a chunk (or a list of chunks) yielded before the winner's output.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    t0 = time.monotonic()
    tasks: Dict[str, Optional[asyncio.Task]] = {
        "primary": asyncio.create_task(_produce("primary", primary, queue)),
        "hedge": None,
    }
    held: Dict[str, List[str]] = {"primary": [], "hedge": []}
    errored = {"primary": False, "hedge": False}
    done: Dict[str, Optional[tuple]] = {"primary": None, "hedge": None}
    hedge_due = delay_s is not None and delay_s >= 0
    timing: Dict[str, Any] = {"started": None, "ready": None, "failed": False}
    winner: Optional[str] = None

    async def start_and_produce() -> None:
        """Prepare the hedge request, then own its stream like the primary's."""
        try:
            source = await start_hedge()
        except asyncio.CancelledError:
            raise  # the primary answered (or the caller left) while preparing
        except Exception as exc:  # noqa: BLE001 - the primary keeps going
            logger.warning("[hedge] could not start the hedge: %s", exc)
            source = None
        if source is None:
            timing["failed"] = True
            await queue.put(("hedge", "end", None))
            return
        timing["ready"] = time.monotonic() - t0
        await _produce("hedge", source, queue)

    def running(tag: str) -> bool:
        return tasks[tag] is not None and not errored[tag] and done[tag] is None

    try:
        while winner is None:
            timeout = None
            if hedge_due and tasks["hedge"] is None:
                timeout = max(0.0, t0 + float(delay_s) - time.monotonic())
            try:
                tag, kind, value = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                hedge_due = False
                timing["started"] = time.monotonic() - t0
                tasks["hedge"] = asyncio.create_task(start_and_produce())
                continue
            if kind == "chunk":
                if tag == "primary" and tasks["hedge"] is None and _passes_through(value):
                    yield value
                    continue
                held[tag].append(value)
                if _is_error(value):
                    errored[tag] = True
                elif not errored[tag] and first_token(value):
                    winner = tag
                    break
            else:
                done[tag] = (kind, value)
            if not running("primary") and not running("hedge"):
                # No stream can still answer: the primary's own outcome
                # (its error, or its empty end) is what the caller sees.
                winner = "primary"

        loser = "hedge" if winner == "primary" else "primary"
        cancelled = await _stop(tasks[loser])
        if tasks["hedge"] is not None and not timing["failed"] and on_decided is not None:
            answered = not errored[winner] and any(first_token(c) for c in held[winner])
            info = {
                "winner": winner,
                "outcome": "first_token" if answered else ("error" if errored[winner] or (
                    done[winner] and done[winner][0] == "error") else "empty"),
                "after_s": round(time.monotonic() - t0, 3),
                "delay_s": float(delay_s),
                "hedge_started_s": round(timing["started"] or 0.0, 3),
                # None: the primary answered while the hedge was still being
                # prepared, so the hedge request was never sent.
                "hedge_ready_s": None if timing["ready"] is None else round(timing["ready"], 3),
                "loser_cancelled": bool(cancelled),
            }
            try:
                notice = on_decided(info)
            except Exception as exc:  # noqa: BLE001 - telemetry never costs the answer
                logger.debug("[hedge] on_decided failed: %s", exc)
                notice = None
            for frame in ([notice] if isinstance(notice, str) else list(notice or [])):
                if frame:
                    yield frame
        for chunk in held[winner]:
            yield chunk
        held["primary"], held["hedge"] = [], []
        if done[winner] is not None:
            kind, value = done[winner]
            if kind == "error":
                raise value
            return
        while True:
            tag, kind, value = await queue.get()
            if tag != winner:
                continue
            if kind == "chunk":
                yield value
            elif kind == "error":
                raise value
            else:
                return
    finally:
        pending = [t for t in tasks.values() if t is not None and not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - closing only
                pass


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def _log_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, _LOG_NAME)


def _append_log(entry: Dict[str, Any]) -> None:
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if os.path.getsize(path) > 600_000:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()[-_LOG_MAX_LINES:]
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(lines)
    except Exception as exc:  # noqa: BLE001 - the log never costs a turn
        logger.debug("[hedge] log append failed: %s", exc)


def _load_log(limit: int) -> List[Dict[str, Any]]:
    try:
        with open(_log_path(), encoding="utf-8") as fh:
            lines = fh.readlines()[-max(1, limit):]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def record(info: Dict[str, Any], hedge: Dict[str, Any], *, primary_url: str, primary_model: str,
           primary_route: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Store one decided race and return its entry (also the SSE payload)."""
    try:
        from src.llm_trace import current_run_id
        run_id = current_run_id() or ""
    except Exception:  # noqa: BLE001
        run_id = ""
    route = primary_route or {}
    winner_is_hedge = info.get("winner") == "hedge"
    entry = {
        "ts": time.time(),
        "run_id": run_id,
        "session_id": session_id or "",
        "winner": info.get("winner"),
        "outcome": info.get("outcome"),
        "after_s": info.get("after_s"),
        "delay_s": info.get("delay_s"),
        "hedge_started_s": info.get("hedge_started_s"),
        "hedge_ready_s": info.get("hedge_ready_s"),
        "loser_cancelled": bool(info.get("loser_cancelled")),
        "primary_model": primary_model,
        "primary_endpoint_id": route.get("endpoint_id"),
        "primary_endpoint_label": route.get("endpoint_label") or _base(primary_url),
        "kind": hedge.get("kind"),
        "hedge_kind": hedge.get("kind"),
        "hedge_model": hedge.get("model"),
        "hedge_endpoint_id": hedge.get("endpoint_id"),
        "hedge_endpoint_label": hedge.get("endpoint_label"),
        "answered_by": hedge.get("model") if winner_is_hedge else primary_model,
        "answered_by_endpoint_label": (hedge.get("endpoint_label") if winner_is_hedge
                                       else route.get("endpoint_label") or _base(primary_url)),
    }
    with _LOCK:
        _RECENT.append(entry)
    _append_log(entry)
    logger.info("[hedge] %s %s after %.1fs (hedge %s @ %s started at %.1fs)",
                entry["winner"], entry["outcome"], entry["after_s"] or 0, entry["hedge_model"],
                entry["hedge_endpoint_label"], entry["hedge_started_s"] or 0)
    return entry


def record_skip(gate: Dict[str, Any], hedge: Dict[str, Any], *, primary_url: str,
                primary_model: str, after_s: float, primary_route: Optional[Dict[str, Any]] = None,
                session_id: Optional[str] = None) -> Dict[str, Any]:
    """Store a re-send that was due but not sent, with the reason."""
    try:
        from src.llm_trace import current_run_id
        run_id = current_run_id() or ""
    except Exception:  # noqa: BLE001
        run_id = ""
    route = primary_route or {}
    capacity = gate.get("capacity") or {}
    entry = {
        "ts": time.time(),
        "run_id": run_id,
        "session_id": session_id or "",
        "winner": None,
        "outcome": "skipped",
        "skip_reason": gate.get("reason") or "",
        "reissues_used": gate.get("used"),
        "reissues_limit": gate.get("limit"),
        "server_waiting": capacity.get("waiting"),
        "server_running": capacity.get("running"),
        "capacity_source": capacity.get("source") or "",
        "after_s": round(float(after_s or 0), 3),
        "delay_s": hedge.get("delay_s"),
        "hedge_started_s": round(float(after_s or 0), 3),
        "hedge_ready_s": None,
        "loser_cancelled": False,
        "primary_model": primary_model,
        "primary_endpoint_id": route.get("endpoint_id"),
        "primary_endpoint_label": route.get("endpoint_label") or _base(primary_url),
        "kind": hedge.get("kind"),
        "hedge_kind": hedge.get("kind"),
        "hedge_model": hedge.get("model"),
        "hedge_endpoint_id": hedge.get("endpoint_id"),
        "hedge_endpoint_label": hedge.get("endpoint_label"),
        "answered_by": primary_model,
        "answered_by_endpoint_label": route.get("endpoint_label") or _base(primary_url),
    }
    with _LOCK:
        _RECENT.append(entry)
    _append_log(entry)
    logger.info("[hedge] re-send of %s skipped (%s; waiting=%s, used %s of %s)", primary_model,
                entry["skip_reason"], entry["server_waiting"], entry["reissues_used"],
                entry["reissues_limit"])
    return entry


def notice_chunk(entry: Dict[str, Any]) -> str:
    return "data: " + json.dumps({"type": "hedge", **entry}, ensure_ascii=False) + "\n\n"


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 20), _LOG_MAX_LINES))
    with _LOCK:
        items = list(_RECENT)
    if len(items) < limit:
        seen = {(i.get("ts"), i.get("run_id")) for i in items}
        older = [i for i in _load_log(limit) if (i.get("ts"), i.get("run_id")) not in seen]
        items = older + items
    return items[-limit:][::-1]


def for_run(run_id: str) -> List[Dict[str, Any]]:
    """Hedges of one agent run, oldest first (for the turn's metrics)."""
    if not run_id:
        return []
    with _LOCK:
        return [dict(i) for i in _RECENT if i.get("run_id") == run_id]


# ---------------------------------------------------------------------------
# status and changes (REST, agent tool, Settings card)
# ---------------------------------------------------------------------------

def target_options() -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = [
        {"value": "auto", "label": "Automatic: same model elsewhere, else the first fallback, else re-send"},
        {"value": "same_model", "label": "Same model on another endpoint"},
        {"value": "fallback", "label": "First fallback model of the chain"},
        {"value": "reissue", "label": "Re-send the same request to the same server"},
        {"value": "off", "label": "No hedge"},
    ]
    for ep in endpoint_models():
        for model in ep["models"]:
            options.append({
                "value": f"{ep['endpoint_id']}::{model}",
                "label": f"{model} · {ep['label']}",
                "endpoint_id": ep["endpoint_id"],
                "model": model,
            })
    return options


def _default_route():
    try:
        from src.settings import load_settings
        settings = load_settings()
        endpoint_id = settings.get("default_endpoint_id") or ""
        model = settings.get("default_model") or ""
        resolved = _resolve(endpoint_id, model) if endpoint_id and model else None
        fallbacks = []
        try:
            from src.endpoint_resolver import resolve_fallback_entries_with_descriptors
            fallbacks = resolve_fallback_entries_with_descriptors(
                settings.get("default_model_fallbacks") or [], require_exact_model=True)
        except Exception:  # noqa: BLE001
            fallbacks = []
        return resolved, fallbacks
    except Exception:  # noqa: BLE001
        return None, []


def stats(items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    items = items if items is not None else recent(_LOG_MAX_LINES)
    skipped = [i for i in items if i.get("outcome") == "skipped"]
    items = [i for i in items if i.get("outcome") != "skipped"]
    hedge_wins = [i for i in items if i.get("winner") == "hedge" and i.get("outcome") == "first_token"]
    waits = [float(i.get("after_s") or 0) for i in hedge_wins]
    return {
        "races": len(items),
        "hedge_wins": len(hedge_wins),
        "primary_wins": sum(1 for i in items if i.get("winner") == "primary" and i.get("outcome") == "first_token"),
        "avg_hedge_answer_s": round(sum(waits) / len(waits), 1) if waits else None,
        "reissue_wins": sum(1 for i in hedge_wins if (i.get("hedge_kind") or i.get("kind")) == "reissue"),
        "reissue_skips": len(skipped),
        "reissue_skips_saturated": sum(1 for i in skipped if i.get("skip_reason") == "saturated"),
    }


def status(limit: int = 20) -> Dict[str, Any]:
    cfg = config()
    resolved, fallbacks = _default_route()
    current = None
    if resolved:
        (url, model, headers), descriptor = resolved
        candidates = [(url, model, headers)] + [c for c, _d in fallbacks]
        descriptors = [descriptor] + [d for _c, d in fallbacks]
        found = plan(url, model, headers, candidates, descriptors, cfg={**cfg, "enabled": True})
        current = {
            "model": model,
            "endpoint_id": descriptor.get("endpoint_id"),
            "endpoint_label": descriptor.get("endpoint_label"),
            "delay_s": delay_for(model, cfg),
            "reissue_max": reissues_for(model, cfg),
            "hedge": None if found is None else {
                k: found.get(k) for k in ("kind", "model", "endpoint_id", "endpoint_label", "delay_s")
            },
        }
    items = recent(limit)
    return {
        **cfg,
        "min_delay_s": MIN_DELAY_S,
        "max_delay_s": MAX_DELAY_S,
        "max_reissue_max": MAX_REISSUE_MAX,
        "current": current,
        "targets": target_options(),
        "recent": items,
        "stats": stats(),
    }


def validate_target(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "auto"
    if text.lower() in TARGET_MODES:
        return text.lower()
    endpoint_id, sep, model = text.partition("::")
    known = {ep["endpoint_id"]: ep for ep in endpoint_models()}
    if endpoint_id not in known:
        if any(row["endpoint_id"] == endpoint_id for row in _endpoint_rows(enabled_only=True)):
            raise ValueError(f"endpoint {endpoint_id} is not part of the global LLM service (Sparks); "
                             "a local text model is never used as the hedge")
        raise ValueError(f"unknown or disabled endpoint: {endpoint_id}")
    if sep and model.strip() and known[endpoint_id]["models"] and model.strip() not in known[endpoint_id]["models"]:
        raise ValueError(f"{model.strip()} is not listed by endpoint {endpoint_id}")
    return text


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Change {enabled, delay_s, target, delay_overrides, reissue_max,
    reissue_overrides}; any subset."""
    from src.settings import update_settings
    changes: Dict[str, Any] = {}
    if patch.get("enabled") is not None:
        changes[SETTING_ENABLED] = bool(patch["enabled"])
    if patch.get("delay_s") is not None:
        try:
            seconds = float(patch["delay_s"])
        except (TypeError, ValueError):
            raise ValueError("delay_s must be a number") from None
        if not MIN_DELAY_S <= seconds <= MAX_DELAY_S:
            raise ValueError(f"delay_s must be between {MIN_DELAY_S:g} and {MAX_DELAY_S:g} seconds")
        changes[SETTING_DELAY] = seconds
    if patch.get("target") is not None:
        changes[SETTING_TARGET] = validate_target(patch["target"])
    if patch.get("delay_overrides") is not None:
        raw = patch["delay_overrides"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except ValueError:
                raise ValueError("delay_overrides must be a JSON object {model: seconds}") from None
        if not isinstance(raw, dict):
            raise ValueError("delay_overrides must be an object {model: seconds}")
        changes[SETTING_OVERRIDES] = json.dumps(parse_overrides(raw), ensure_ascii=False)
    if patch.get("reissue_max") is not None:
        value = patch["reissue_max"]
        try:
            number = float(value)
            count = int(number) if not isinstance(value, bool) and number.is_integer() else None
        except (TypeError, ValueError):
            count = None
        if count is None or not 0 <= count <= MAX_REISSUE_MAX:
            raise ValueError(f"reissue_max must be a whole number between 0 and {MAX_REISSUE_MAX}")
        changes[SETTING_REISSUE_MAX] = count
    if patch.get("reissue_overrides") is not None:
        raw = patch["reissue_overrides"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except ValueError:
                raise ValueError("reissue_overrides must be a JSON object {model: re-sends}") from None
        if not isinstance(raw, dict):
            raise ValueError("reissue_overrides must be an object {model: re-sends}")
        for name, count in raw.items():
            ok = (not isinstance(count, bool) and str(name or "").strip() != ""
                  and isinstance(count, (int, float)) and float(count).is_integer()
                  and 0 <= count <= MAX_REISSUE_MAX)
            if not ok:
                raise ValueError(f"reissue_overrides[{name!r}] must be a whole number "
                                 f"between 0 and {MAX_REISSUE_MAX}, got {count!r}")
        changes[SETTING_REISSUE_OVERRIDES] = json.dumps(parse_reissue_overrides(raw), ensure_ascii=False)
    if not changes:
        raise ValueError("nothing to change")
    update_settings(changes)
    return status()
