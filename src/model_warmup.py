"""src/model_warmup.py — load the default chat model at startup and keep it
resident: a residency KEEPER, not a one-shot warmup.

A cold local model means the first answer of the day waits a minute for the
weights (seen live: 'Ask Faustus' sat on 'Thinking…' while a 27B loaded), and
the phone (routes/mobile_routes.py) can send a question at any moment and
must get an answer without paying that cold load. When the default chat
endpoint is a local Ollama, this module asks Ollama to load that model right
after the server is up — `POST /api/generate` with no prompt loads without
generating — with `keep_alive: -1`, i.e. "do not unload", and then polls
`GET /api/ps` every few seconds for the life of the process:

  * gone from `/api/ps` (another Faustus restart lost the pin, another local
    app on the same Ollama unloaded it, `_evict`/`restore_keep_alive` did
    something it should not have) → reload immediately, unless a *different*
    model's load is in flight and needs the VRAM this instant (checked
    against `src.vram_admission`'s reservations and open tickets) — reloading
    into that race would just contend for the same bytes;
  * resident but `expires_at` is not effectively "never" (another client's
    own default `keep_alive`, commonly 5 m, overrode our `-1`) → re-pin with
    `-1` right away;
  * resident and pinned forever → nothing to do.

Remote endpoints have nothing to warm. Everything here is best effort and
never blocks startup or a chat turn.

Settings: `warm_default_model` (True), `warm_default_model_keep_alive`
("-1" = forever; any Ollama duration works), `warm_default_model_every_s`
(20; the old 120 is still accepted) — checked this often because another
app's default keep_alive (5 m) can unpin the model well before a slower
cadence would notice.

X-D correction: the default stays resident whenever Faustus is open and
nothing else was explicitly asked for — but when the owner picks another
local model that does not fit next to it, `src/vram_admission.py::admit()`
lets the default yield on its own (no card). This keeper must not fight that
by reloading the default the instant it notices it gone: `warm_default_model_
yield_minutes` (10) is how long a resident, non-default, non-embedding local
model that has actually been used counts as "the owner is using it" — the
keeper waits out that window (checked via `vram_admission.last_active_
seconds`, the same activity tracking `assess()` already updates on every
chat turn) before it reloads the default back in. It also never reloads into
a shortfall: if `vram_admission.assess(default)["fits"] is False` next to
what is resident right now, it waits and retries next cycle instead of
starting a load that would only spill to CPU/PCIe or contend for room the
other model is using.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_last: Dict[str, Any] = {"model": None, "url": None, "at": None, "ok": None, "detail": ""}
# The residency keeper's own state, separate from `_last` (which is about the
# most recent /api/generate warm ping): what the last /api/ps poll saw, and
# how many times this process has had to act on it.
_keeper: Dict[str, Any] = {
    "last_check": None, "resident": None, "expires_at": "", "reloads": 0, "repins": 0,
    # X-D: the model the default is currently yielding to (None when the
    # default is resident/being kept), and whether the last cycle skipped a
    # reload because the default would not fit next to what is resident.
    "yielding_to": None, "waiting_for_room": False,
}
# Logged once per episode (not every ~20 s cycle) — reset the moment the
# condition that caused it clears.
_yield_logged = False
_fit_wait_logged = False

# X-D: name hints for a local model that is an embedding model, not a chat
# model — the keeper must never treat one of these as "the owner picked
# another model to use", since it never carries a conversation on its own.
# Best-effort: Ollama's /api/ps has no capability flag, and asking
# /api/show for every resident every ~20 s would defeat the point of this
# being a cheap poll (see module docstring).
_EMBEDDING_NAME_HINTS = ("embed", "bge-", "bge:", "gte-", "gte:", "e5-", "e5:",
                         "nomic-embed", "mxbai-embed", "minilm")


def _looks_like_embedding_model(name: str) -> bool:
    n = str(name or "").strip().lower()
    return any(hint in n for hint in _EMBEDDING_NAME_HINTS)


def _settings() -> Dict[str, Any]:
    try:
        from src.settings import get_setting
        keep = get_setting("warm_default_model_keep_alive", "-1")
        return {
            "enabled": bool(get_setting("warm_default_model", True)),
            "keep_alive": keep if keep not in (None, "") else "-1",
            "every_s": float(get_setting("warm_default_model_every_s", 20) or 20),
            "yield_minutes": float(get_setting("warm_default_model_yield_minutes", 10) or 10),
        }
    except Exception:  # noqa: BLE001
        return {"enabled": True, "keep_alive": "-1", "every_s": 20.0, "yield_minutes": 10.0}


def _keep_alive_value(raw: Any) -> Any:
    """Ollama wants a number (seconds; -1 = forever) or a duration string."""
    s = str(raw).strip()
    try:
        return int(s)
    except ValueError:
        return s or -1


def _ollama_root(url: str) -> Optional[str]:
    """`http://host:port` when `url` is a local Ollama chat endpoint (native
    `/api/chat` or the OpenAI-compatible `/v1`), else None."""
    try:
        p = urlparse(url or "")
    except Exception:  # noqa: BLE001
        return None
    host = p.hostname or ""
    local = host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or p.port == 11434
    path = (p.path or "").rstrip("/")
    if not local or not (path in ("", "/v1", "/api") or path.startswith("/v1/") or path.startswith("/api/")):
        return None
    return f"{p.scheme or 'http'}://{host}:{p.port or 11434}"


def resolve_default() -> Optional[Dict[str, str]]:
    """The default chat model when it lives on a local Ollama: {url, model, root}."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, _headers = resolve_endpoint("default")
    except Exception as exc:  # noqa: BLE001
        logger.debug("model warmup: default endpoint unresolved: %s", exc)
        return None
    if not url or not model:
        return None
    root = _ollama_root(url)
    if not root:
        return None
    return {"url": url, "model": model, "root": root}


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def _canon_root(root: str) -> str:
    """Same collapsing `src.vram_admission._canonical_root` does (loopback
    aliases are the same Ollama), kept as a tiny local copy so this module
    stays import-order-independent from `vram_admission` — this is the one
    place both `vram_admission.is_default_model` and
    `run_model_pin._is_default_model` delegate to for "is this the default".
    """
    try:
        p = urlparse(str(root or ""))
    except Exception:  # noqa: BLE001
        return str(root or "")
    host = (p.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        host = "127.0.0.1"
    scheme = p.scheme or "http"
    port = p.port or 11434
    return f"{scheme}://{host}:{port}"


def is_default(root_or_endpoint: str, model: str) -> bool:
    """True when `(root_or_endpoint, model)` names the owner's default chat
    model. Accepts either a bare Ollama root (`http://host:port`) or a full
    endpoint URL with a path (`.../v1/chat/completions`) — only host and
    port are compared, so both callers can pass whatever they already have
    in hand. The one place "is this the default" is decided; `src.vram_
    admission.is_default_model` and `src.run_model_pin._is_default_model`
    both call this instead of re-deriving it."""
    target = resolve_default()
    if not target:
        return False
    return (_canon_root(root_or_endpoint) == _canon_root(target["root"])
            and _norm_model(model) == _norm_model(target["model"]))


def _load_options(url: str, model: str) -> Dict[str, Any]:
    """The per-model load options Settings → Local models saved, flattened
    like `routes/local_models_routes._set_keep_alive` does (named knobs win
    over `extra`); keep_alive is carried by the request itself."""
    try:
        from src.llm_core import _model_load_defaults
        saved = dict(_model_load_defaults(url, model) or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("model warmup: load options unavailable: %s", exc)
        return {}
    opts = {k: v for k, v in saved.items() if k not in ("keep_alive", "extra") and v not in (None, "")}
    extra = saved.get("extra")
    if isinstance(extra, dict):
        opts = {**extra, **opts}
    return opts


def _norm_model(name: str) -> str:
    m = str(name or "").strip().lower()
    return m[:-7] if m.endswith(":latest") else m


def _pin(root: str, model: str) -> None:
    """Protect the default model from `assess()`'s eviction suggestions and
    from a not-explicitly-named `_evict`. Idempotent, in-memory, cheap enough
    to call every cycle rather than tracking whether it changed."""
    try:
        from src import vram_admission
        vram_admission.pin_model(root, model)
    except Exception as exc:  # noqa: BLE001
        logger.debug("model warmup: pin failed: %s", exc)


def _load_in_flight(exclude_model: str) -> bool:
    """True when some OTHER model's load is reserving VRAM right now, or a
    person has an open admission ticket to answer. Forcing our reload into
    that window would just contend for the same bytes with a job that is
    already mid-decision — better to wait one cycle."""
    try:
        from src import vram_admission
        want = _norm_model(exclude_model)
        for r in vram_admission.reservations_snapshot():
            if _norm_model(r.get("model") or "") != want:
                return True
        if vram_admission.pending():
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("model warmup: in-flight check failed: %s", exc)
    return False


def _is_forever(expires_at: str) -> bool:
    """Mirrors the Studio's own `untilText` heuristic (10 years out counts as
    "kept loaded"): Ollama's `-1` convention lands either far in the future or
    at the zero time, never a normal few-minutes-out timestamp."""
    s = str(expires_at or "").strip()
    if not s:
        return False
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = abs((dt - now).total_seconds())
        return delta > 10 * 365 * 86400
    except Exception:  # noqa: BLE001
        return False


def _active_non_default_resident(ps: Dict[str, Any], target: Dict[str, str],
                                 yield_minutes: float) -> "tuple[Optional[str], Optional[float]]":
    """From an `/api/ps` reading already in hand (no extra call): the name
    and age (seconds since last chat turn) of a resident local model that is
    NOT the default and NOT an embedding model, and has been used within
    `yield_minutes` — the model the owner explicitly picked, per X-D. `None,
    None` when nothing qualifies."""
    try:
        from src import vram_admission
    except Exception:  # noqa: BLE001
        return None, None
    want_default = _norm_model(target["model"])
    cutoff = max(0.0, yield_minutes) * 60.0
    for m in ps.get("models") or []:
        name = str(m.get("name") or m.get("model") or "")
        if not name or _norm_model(name) == want_default or _looks_like_embedding_model(name):
            continue
        age = vram_admission.last_active_seconds(target["root"], name)
        if age is not None and age < cutoff:
            return name, age
    return None, None


async def _api_ps(root: str) -> Optional[Dict[str, Any]]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(root + "/api/ps")
        if resp.status_code != 200:
            return None
        return resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("model warmup: /api/ps failed: %s", exc)
        return None


async def warm_once() -> Dict[str, Any]:
    """Ask Ollama to load the default model (no generation) and pin it."""
    cfg = _settings()
    target = resolve_default()
    if not target:
        _last.update({"ok": None, "detail": "no local default model"})
        return dict(_last)
    import httpx
    body = {"model": target["model"], "keep_alive": _keep_alive_value(cfg["keep_alive"])}
    # Build the runner the way the chats will ask for it: the saved load
    # options (num_ctx, num_gpu, main_gpu, extra). Without them the model
    # came up with Ollama's default context and the first real prompt made
    # Ollama tear it down and load it again — the wait this module exists
    # to remove.
    load_opts = _load_options(target["url"], target["model"])
    if load_opts:
        body["options"] = load_opts
    try:
        async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
            resp = await client.post(target["root"] + "/api/generate", json=body)
        ok = resp.status_code == 200
        detail = "" if ok else resp.text[:200]
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{exc.__class__.__name__}: {exc}"
    _last.update({"model": target["model"], "url": target["root"], "at": time.time(), "ok": ok, "detail": detail})
    (logger.info if ok else logger.warning)("model warmup: %s %s%s", target["model"], "loaded" if ok else "failed", f" ({detail})" if detail else "")
    return dict(_last)


async def check_once() -> Dict[str, Any]:
    """One residency-keeper cycle: poll `/api/ps`, act only when the default
    model is missing or its keep_alive shrank, log only on a change."""
    global _yield_logged, _fit_wait_logged
    target = resolve_default()
    _keeper["last_check"] = time.time()
    if not target:
        _keeper.update({"resident": None, "expires_at": ""})
        return dict(_keeper)
    _pin(target["root"], target["model"])
    ps = await _api_ps(target["root"])
    if ps is None:
        logger.debug("model warmup: /api/ps unreachable this cycle — leaving reload to the next one")
        return dict(_keeper)
    want = _norm_model(target["model"])
    row = None
    for m in ps.get("models") or []:
        names = {_norm_model(m.get("name") or ""), _norm_model(m.get("model") or "")}
        if want in names:
            row = m
            break
    if row is None:
        _keeper["resident"] = False
        _keeper["expires_at"] = ""
        if _load_in_flight(target["model"]):
            logger.debug("model warmup: default model absent but another load is in flight — waiting")
            return dict(_keeper)
        # X-D: do not fight `vram_admission.admit()`'s own yield. A resident,
        # active, non-default, non-embedding local model is the owner's
        # explicit pick — reload only once it is gone or has gone idle past
        # `yield_minutes`.
        cfg = _settings()
        active_name, active_age = _active_non_default_resident(ps, target, cfg["yield_minutes"])
        if active_name is not None:
            _keeper["yielding_to"] = active_name
            msg = "model warmup: default model yields to %s (active %.0fs ago)"
            if not _yield_logged:
                logger.info(msg, active_name, active_age or 0.0)
                _yield_logged = True
            else:
                logger.debug(msg, active_name, active_age or 0.0)
            return dict(_keeper)
        _yield_logged = False
        _keeper["yielding_to"] = None
        # Never reload into a shortfall: if the default would not fit next
        # to what is resident right now, loading it would just spill to
        # CPU/PCIe (or contend with whatever else is mid-load) — wait and
        # retry next cycle instead.
        try:
            from src import vram_admission
            fit = await asyncio.to_thread(vram_admission.assess, target["root"], target["model"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("model warmup: fit check failed: %s", exc)
            fit = {}
        if fit.get("fits") is False:
            _keeper["waiting_for_room"] = True
            if not _fit_wait_logged:
                logger.info("model warmup: default model would not fit next to what is resident — waiting")
                _fit_wait_logged = True
            else:
                logger.debug("model warmup: default model would not fit next to what is resident — waiting")
            return dict(_keeper)
        _fit_wait_logged = False
        _keeper["waiting_for_room"] = False
        logger.info("model warmup: default model was unloaded by someone else — reloading")
        await warm_once()
        _keeper["reloads"] += 1
        _keeper["resident"] = True
        _keeper["expires_at"] = ""
        return dict(_keeper)
    expires = str(row.get("expires_at") or "")
    _keeper["resident"] = True
    _keeper["expires_at"] = expires
    _keeper["yielding_to"] = None
    _keeper["waiting_for_room"] = False
    _yield_logged = False
    _fit_wait_logged = False
    if _is_forever(expires):
        logger.debug("model warmup: default model resident, kept loaded forever")
        return dict(_keeper)
    logger.info("model warmup: keep_alive shortened by another client — re-pinned")
    await warm_once()
    _keeper["repins"] += 1
    _keeper["expires_at"] = ""
    return dict(_keeper)


async def _loop() -> None:
    # Give the rest of startup (MCP, indexes) a head start; the model load
    # is the heavy one and nothing else needs the GPU yet.
    await asyncio.sleep(3)
    while True:
        cfg = _settings()
        if cfg["enabled"]:
            try:
                await check_once()
            except Exception as exc:  # noqa: BLE001
                logger.debug("model warmup check failed: %s", exc)
        await asyncio.sleep(max(5.0, cfg["every_s"]))


def start() -> None:
    """Spawn the warmup loop (idempotent)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_loop(), name="model-warmup")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None


def status() -> Dict[str, Any]:
    return {**_settings(), **_last, **_keeper}
