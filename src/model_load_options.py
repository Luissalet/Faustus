"""Per-model load defaults for Ollama models (Settings → Local models → Options).

Stored in the ``model_load_options`` setting as::

    {"<endpoint_id>|<model>": {"num_ctx": 32768, "num_gpu": 40, "keep_alive": "30m"}}

Only three knobs, all Ollama runtime options that decide how a model is
loaded rather than what it says: the context window (``num_ctx``), the
number of layers kept on the GPU (``num_gpu``) and how long the runner keeps
it resident (``keep_alive``). They sit UNDER explicit per-request overrides:
``/ctx 8192`` in a chat still wins over a saved 32768.

:func:`resolve_for_request` is what src/llm_core.py calls on every Ollama
request. It matches the request URL against the endpoint the options were
saved for by host:port (loopback aliases collapse to one), so a saved
``local-ollama|qwen3.5:9b`` applies whether the request goes through
``/v1`` or the native ``/api/chat`` of the same server. The endpoint table
is only consulted when the model name actually has saved options, so the
common case costs a dict lookup and no database round trip.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SETTING_KEY = "model_load_options"
ALLOWED_KEYS = ("num_ctx", "num_gpu", "keep_alive", "main_gpu")

NUM_CTX_MIN, NUM_CTX_MAX = 512, 1_048_576
NUM_GPU_MIN, NUM_GPU_MAX = 0, 1024
# Which card a model is pinned to (Ollama `main_gpu`, verified honoured by
# 0.33: "selecting requested single GPU … requested_main_gpu=0"). Auto when
# unset: Ollama takes the card with the most free memory and splits a model
# that fits no single card.
MAIN_GPU_MIN, MAIN_GPU_MAX = 0, 15
_KEEP_ALIVE_RE = re.compile(r"^-?\d+(ms|s|m|h)?$")

# Synthetic endpoint id for "the Ollama this machine runs" when no configured
# endpoint points at it (routes/local_models_routes.py).
DEFAULT_ENDPOINT_ID = "ollama-local"

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}


def option_key(endpoint_id: str, model: str) -> str:
    return f"{str(endpoint_id or '').strip()}|{str(model or '').strip()}"


def split_key(key: str) -> Tuple[str, str]:
    ep, _, model = str(key or "").partition("|")
    return ep, model


def sanitize_options(raw: Any) -> Dict[str, Any]:
    """Validate a client-supplied options object. Unknown keys are dropped,
    empty values unset the knob, bad values raise ValueError."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("options must be an object")
    out: Dict[str, Any] = {}
    for key in ALLOWED_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key == "num_ctx":
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise ValueError("num_ctx must be an integer")
            if not NUM_CTX_MIN <= n <= NUM_CTX_MAX:
                raise ValueError(f"num_ctx must be between {NUM_CTX_MIN} and {NUM_CTX_MAX}")
            out[key] = n
        elif key == "num_gpu":
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise ValueError("num_gpu must be an integer")
            if not NUM_GPU_MIN <= n <= NUM_GPU_MAX:
                raise ValueError(f"num_gpu must be between {NUM_GPU_MIN} and {NUM_GPU_MAX}")
            out[key] = n
        elif key == "main_gpu":
            if isinstance(value, bool):
                raise ValueError("main_gpu must be a GPU index")
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise ValueError("main_gpu must be a GPU index")
            if not MAIN_GPU_MIN <= n <= MAIN_GPU_MAX:
                raise ValueError(f"main_gpu must be between {MAIN_GPU_MIN} and {MAIN_GPU_MAX}")
            out[key] = n
        elif key == "keep_alive":
            if isinstance(value, bool):
                raise ValueError("keep_alive must be a duration like 10m, -1 or a number of seconds")
            if isinstance(value, (int, float)):
                out[key] = int(value)
                continue
            text = str(value).strip()
            if not _KEEP_ALIVE_RE.match(text):
                raise ValueError("keep_alive must be a duration like 10m, 1h, -1 or a number of seconds")
            out[key] = text
    return out


def all_options() -> Dict[str, Dict[str, Any]]:
    try:
        from src.settings import get_setting
        raw = get_setting(SETTING_KEY, {})
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and "|" in str(key):
            try:
                clean = sanitize_options(value)
            except ValueError:
                continue
            if clean:
                out[str(key)] = clean
    return out


def get_options(endpoint_id: str, model: str) -> Dict[str, Any]:
    return dict(all_options().get(option_key(endpoint_id, model), {}))


def set_options(endpoint_id: str, model: str, options: Any) -> Dict[str, Any]:
    """Persist (or clear, for an empty object) the options of one model."""
    from src.settings import load_settings, save_settings
    clean = sanitize_options(options)
    settings = dict(load_settings())
    table = settings.get(SETTING_KEY)
    table = dict(table) if isinstance(table, dict) else {}
    key = option_key(endpoint_id, model)
    if clean:
        table[key] = clean
    else:
        table.pop(key, None)
    settings[SETTING_KEY] = table
    save_settings(settings)
    return clean


def options_for_endpoint(endpoint_id: str) -> Dict[str, Dict[str, Any]]:
    """{model: options} for every model saved under one endpoint."""
    prefix = f"{endpoint_id}|"
    return {
        key[len(prefix):]: value
        for key, value in all_options().items()
        if key.startswith(prefix)
    }


# ── request-time resolution ─────────────────────────────────────────────────

def _netloc_key(url: str) -> str:
    """host:port with loopback aliases collapsed, '' when unparsable."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    if host in _LOOPBACK:
        host = "127.0.0.1"
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{host}:{port}"


def _default_ollama_base() -> str:
    base = (os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or "").strip()
    if not base:
        host = (os.getenv("LLM_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        base = f"http://{host}:11434"
    if not base.startswith("http"):
        base = "http://" + base
    return base.rstrip("/")


_ENDPOINT_TTL = 30.0
_endpoint_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def _endpoint_bases() -> Dict[str, str]:
    """{endpoint_id: base_url} for every configured endpoint, cached 30 s."""
    now = time.time()
    if _endpoint_cache["data"] is not None and now - _endpoint_cache["ts"] < _ENDPOINT_TTL:
        return _endpoint_cache["data"]
    out: Dict[str, str] = {}
    try:
        from core.database import SessionLocal, ModelEndpoint
        db = SessionLocal()
        try:
            for row in db.query(ModelEndpoint).all():
                ep_id = str(getattr(row, "id", "") or "")
                base = str(getattr(row, "base_url", "") or "")
                if ep_id and base:
                    out[ep_id] = base
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 — a missing table must not break a chat
        logger.debug("model_load_options: endpoint lookup failed: %s", e)
    _endpoint_cache["ts"] = now
    _endpoint_cache["data"] = out
    return out


def reset_endpoint_cache() -> None:
    _endpoint_cache["ts"] = 0.0
    _endpoint_cache["data"] = None


def _model_matches(saved: str, requested: str) -> bool:
    if saved == requested:
        return True
    # `qwen3.5` and `qwen3.5:latest` are the same tag to Ollama.
    def _canon(name: str) -> str:
        return name if ":" in name.split("/")[-1] else f"{name}:latest"
    return _canon(saved) == _canon(requested)


def declared_ollama_netlocs() -> frozenset:
    """host:port of every endpoint the admin saved load options for.

    Settings → Local models only lists Ollama servers (port 11434 or an
    "ollama" host — routes/model_routes.py ``_is_ollama_base``), so a saved
    option is the admin's word that the thing at that host:port is Ollama:
    llm_core uses it to move a /v1 request to the native /api/chat (the only
    surface that carries num_ctx/num_gpu/keep_alive) off the default port.
    Empty when nothing is saved or anything goes wrong.
    """
    try:
        table = all_options()
        if not table:
            return frozenset()
        out = set()
        bases: Optional[Dict[str, str]] = None
        for ep_id in {split_key(key)[0] for key in table}:
            if ep_id == DEFAULT_ENDPOINT_ID:
                base = _default_ollama_base()
            else:
                if bases is None:
                    bases = _endpoint_bases()
                base = bases.get(ep_id, "")
            netloc = _netloc_key(base) if base else ""
            if netloc:
                out.add(netloc)
        return frozenset(out)
    except Exception as e:  # noqa: BLE001
        logger.debug("model_load_options: declared netlocs failed: %s", e)
        return frozenset()


def is_declared_ollama_host(url: str) -> bool:
    """True when ``url`` points at a host:port the admin saved load options
    for (see :func:`declared_ollama_netlocs`)."""
    try:
        want = _netloc_key(url)
        return bool(want) and want in declared_ollama_netlocs()
    except Exception:  # noqa: BLE001
        return False


def resolve_for_request(url: str, model: str) -> Dict[str, Any]:
    """The saved defaults that apply to a request for ``model`` at ``url``.

    Empty when nothing is saved for this model, when the saved entry belongs
    to an endpoint on another host, or when anything at all goes wrong — a
    missing default is never worth failing a chat over.
    """
    try:
        table = all_options()
        if not table:
            return {}
        model = str(model or "").strip()
        candidates = [
            (split_key(key)[0], opts)
            for key, opts in table.items()
            if _model_matches(split_key(key)[1], model)
        ]
        if not candidates:
            return {}
        want = _netloc_key(url)
        if not want:
            return {}
        bases: Optional[Dict[str, str]] = None
        for ep_id, opts in candidates:
            if ep_id == DEFAULT_ENDPOINT_ID:
                base = _default_ollama_base()
            else:
                if bases is None:
                    bases = _endpoint_bases()
                base = bases.get(ep_id, "")
            if base and _netloc_key(base) == want:
                return dict(opts)
        return {}
    except Exception as e:  # noqa: BLE001
        logger.debug("model_load_options: resolve failed: %s", e)
        return {}
