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
ALLOWED_KEYS = ("num_ctx", "num_gpu", "keep_alive", "main_gpu", "extra")

# `extra`: further Ollama runtime `options` a person may pin per model, by
# name, from the Options form (09-09-2026, Luis: "more customisation per
# model"). Only names Ollama's /api/chat `options` object actually accepts —
# these are llama.cpp parameters Ollama forwards per request. What is NOT here
# on purpose: llama-server command-line flags (`-jinja`, `--spec-type`,
# `--draft-*`, `--cache-type-k/v`, `-np`). Ollama does not take those per
# request; some exist as server environment (OLLAMA_KV_CACHE_TYPE,
# OLLAMA_FLASH_ATTENTION, OLLAMA_NUM_PARALLEL) and the rest need a llama.cpp
# endpoint. Accepting them here and silently dropping them would be the
# worst of both: a box that looks like it works.
EXTRA_OPTION_KEYS = frozenset({
    # generation
    "num_predict", "num_keep", "temperature", "top_k", "top_p", "min_p", "typical_p",
    "tfs_z", "repeat_last_n", "repeat_penalty", "presence_penalty", "frequency_penalty",
    "mirostat", "mirostat_tau", "mirostat_eta", "penalize_newline", "seed", "stop",
    # load / runtime
    "num_batch", "num_thread", "numa", "use_mmap", "use_mlock", "low_vram",
})
_EXTRA_BOOL_KEYS = frozenset({"numa", "use_mmap", "use_mlock", "low_vram", "penalize_newline"})
_EXTRA_INT_KEYS = frozenset({"num_predict", "num_keep", "top_k", "repeat_last_n", "mirostat",
                             "seed", "num_batch", "num_thread"})
EXTRA_MAX_ITEMS = 24


def sanitize_extra(raw: Any) -> Dict[str, Any]:
    """Validate the `extra` block: known Ollama option names only, typed."""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, dict):
        raise ValueError("extra must be an object of Ollama options")
    if len(raw) > EXTRA_MAX_ITEMS:
        raise ValueError(f"extra may hold at most {EXTRA_MAX_ITEMS} options")
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        k = str(key).strip()
        if k not in EXTRA_OPTION_KEYS:
            raise ValueError(
                f"'{k}' is not an Ollama request option. Ollama takes: "
                + ", ".join(sorted(EXTRA_OPTION_KEYS))
                + ". llama-server flags (-jinja, --spec-*, --cache-type-*) are not per-request in Ollama."
            )
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if k == "stop":
            items = value if isinstance(value, list) else [value]
            stops = [str(s) for s in items if str(s).strip()][:8]
            if stops:
                out[k] = stops
        elif k in _EXTRA_BOOL_KEYS:
            if isinstance(value, bool):
                out[k] = value
            else:
                text = str(value).strip().lower()
                if text not in ("true", "false", "1", "0", "on", "off", "yes", "no"):
                    raise ValueError(f"{k} must be true or false")
                out[k] = text in ("true", "1", "on", "yes")
        elif k in _EXTRA_INT_KEYS:
            if isinstance(value, bool):
                raise ValueError(f"{k} must be an integer")
            try:
                out[k] = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be an integer")
        else:
            if isinstance(value, bool):
                raise ValueError(f"{k} must be a number")
            try:
                out[k] = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
    return out

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
        if key == "extra":
            # The form sends the block as JSON text; the API may send a dict.
            if isinstance(value, str):
                import json as _json
                try:
                    value = _json.loads(value)
                except ValueError:
                    raise ValueError("extra must be valid JSON, e.g. {\"num_batch\": 512, \"min_p\": 0.05}")
            cleaned = sanitize_extra(value)
            if cleaned:
                out[key] = cleaned
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


def _global_options_for_netloc(url: str, model: str) -> Dict[str, Any]:
    """The pre-MOD-04 lookup, kept exactly as it was: the first saved GLOBAL
    entry (this module's original, unscoped table) whose model matches and
    whose endpoint resolves to ``url``'s host:port. Used both directly by
    :func:`resolve_for_request` (backward compat) and as the weakest rung of
    the MOD-04 scope ladder in :func:`resolve_with_origin`."""
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


def resolve_for_request(url: str, model: str, *, session_id: Optional[str] = None,
                         project_id: Optional[str] = None) -> Dict[str, Any]:
    """The saved defaults that apply to a request for ``model`` at ``url``.

    Empty when nothing is saved for this model, when the saved entry belongs
    to an endpoint on another host, or when anything at all goes wrong — a
    missing default is never worth failing a chat over.

    ``session_id``/``project_id`` are optional (MOD-04): when given, a
    per-field session or project override (see :func:`set_scoped_options`)
    wins over the global entry this function has always returned — the same
    session > project > global ladder :func:`resolve_with_origin` documents,
    collapsed here into a flat dict for ``src/llm_core.py``'s existing
    two-argument call, which keeps working unchanged.
    """
    try:
        if not session_id and not project_id:
            return _global_options_for_netloc(url, model)
        resolved = resolve_with_origin(
            url, model, session_id=session_id, project_id=project_id,
        )
        return dict(resolved.get("options") or {})
    except Exception as e:  # noqa: BLE001
        logger.debug("model_load_options: resolve failed: %s", e)
        return {}


# ── MOD-04: scope (global / project / session) and precedence ──────────────
#
# The table above (`SETTING_KEY`, flat "endpoint|model" -> options) stays
# untouched — it IS the global scope, byte for byte, so every existing
# reader (`all_options`, `options_for_endpoint`, `declared_ollama_netlocs`,
# and `src/effective_config.py::_model_layer_offers`, a foreign file this
# lote does not touch) keeps seeing exactly what it always has. Project and
# session overrides live in a second settings key under the SAME authority
# (`src/settings.py` — rule 4 asks to reuse authorities, not to avoid a
# second *key* in the one store every other per-model/per-turn knob already
# uses); nothing here removes or reinterprets a global entry.
SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"
SCOPE_SESSION = "session"
SCOPES: Tuple[str, ...] = (SCOPE_GLOBAL, SCOPE_PROJECT, SCOPE_SESSION)

#: Strongest first — session overrides project overrides the global default,
#: mirroring `src/effective_config.py`'s own "turn > ... > global" ladder
#: (that module's model layer is one rung of a bigger ladder; this is the
#: same shape one level down, for the field this module itself owns).
SCOPE_PRECEDENCE: Tuple[str, ...] = (SCOPE_SESSION, SCOPE_PROJECT, SCOPE_GLOBAL)

SCOPED_SETTING_KEY = "model_load_options_scoped"


def _scoped_storage_key(endpoint_id: str, model: str, scope: str, scope_id: str) -> str:
    if scope not in (SCOPE_PROJECT, SCOPE_SESSION):
        raise ValueError(f"scope must be one of {SCOPE_PROJECT!r}/{SCOPE_SESSION!r}")
    scope_id = str(scope_id or "").strip()
    if not scope_id:
        raise ValueError(f"scope_id is required for scope={scope!r}")
    return f"{option_key(endpoint_id, model)}::{scope}:{scope_id}"


def all_scoped_options() -> Dict[str, Dict[str, Any]]:
    """Every saved project/session override, keyed by its storage key. Same
    "never raise, drop what does not sanitize" contract as :func:`all_options`."""
    try:
        from src.settings import get_setting
        raw = get_setting(SCOPED_SETTING_KEY, {})
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict) or "::" not in str(key):
            continue
        try:
            clean = sanitize_options(value)
        except ValueError:
            continue
        if clean:
            out[str(key)] = clean
    return out


def get_scoped_options(endpoint_id: str, model: str, scope: str, scope_id: str) -> Dict[str, Any]:
    key = _scoped_storage_key(endpoint_id, model, scope, scope_id)
    return dict(all_scoped_options().get(key, {}))


def set_scoped_options(endpoint_id: str, model: str, scope: str, scope_id: str,
                        options: Any) -> Dict[str, Any]:
    """Persist (or clear, for an empty object) one project/session override."""
    from src.settings import load_settings, save_settings
    key = _scoped_storage_key(endpoint_id, model, scope, scope_id)
    clean = sanitize_options(options)
    settings = dict(load_settings())
    table = settings.get(SCOPED_SETTING_KEY)
    table = dict(table) if isinstance(table, dict) else {}
    if clean:
        table[key] = clean
    else:
        table.pop(key, None)
    settings[SCOPED_SETTING_KEY] = table
    save_settings(settings)
    return clean


def _layer_options(endpoint_id: str, model: str, scope: str,
                    scope_id: str) -> Tuple[Dict[str, Any], str]:
    """One scope's options plus the storage path they came from ("origin")."""
    if scope == SCOPE_GLOBAL:
        return dict(get_options(endpoint_id, model)), f"model_load_options:{option_key(endpoint_id, model)}"
    if not scope_id:
        return {}, ""
    try:
        key = _scoped_storage_key(endpoint_id, model, scope, scope_id)
    except ValueError:
        return {}, ""
    return dict(get_scoped_options(endpoint_id, model, scope, scope_id)), f"{SCOPED_SETTING_KEY}:{key}"


def _endpoint_id_for_netloc(url: str) -> str:
    """Which configured endpoint id ``url`` belongs to — the synthetic
    :data:`DEFAULT_ENDPOINT_ID` for "the Ollama this machine runs", the id of
    a configured :class:`ModelEndpoint` row, or ``""`` when neither matches.
    Used to key a project/session override even when no GLOBAL entry has
    ever been saved for this model (a scoped-only override still resolves)."""
    want = _netloc_key(url)
    if not want:
        return ""
    # A configured endpoint that happens to sit at the same host:port as
    # "the Ollama this machine runs" wins the id — it is the one an admin
    # actually named in Settings › Local models, so it is the one under
    # which a scoped override would have been saved.
    for ep_id, base in (_endpoint_bases() or {}).items():
        if _netloc_key(base) == want:
            return ep_id
    if want == _netloc_key(_default_ollama_base()):
        return DEFAULT_ENDPOINT_ID
    return ""


def resolve_with_origin(url: str, model: str, *, session_id: Optional[str] = None,
                         project_id: Optional[str] = None,
                         endpoint_id: Optional[str] = None) -> Dict[str, Any]:
    """The MOD-04 answer: effective per-model load options resolved
    session -> project -> global, field by field, with provenance —
    "quien manda" for the Local models UI. Shaped like a small, self-contained
    echo of `src.effective_config`'s `EffectiveValue`/`overridden` (that
    module is not imported: it has no endpoint URL to resolve the global
    layer against, and reusing its dataclasses here would need touching a
    file this lote does not own — see the report's "Cambios necesarios en
    ficheros ajenos" for the follow-up that would let it read this ladder
    too).

    Returns ``{"endpoint_id", "model", "options": {field: value, ...},
    "origin": {field: {"scope", "path"}}, "overridden": {field: [{"scope",
    "value", "path"}, ...]}}``. Never raises: any failure degrades to "no
    scoped opinion", the same promise every other function in this module
    makes.
    """
    # A caller that already resolved which endpoint it means (every route in
    # this lote does — it just picked the endpoint to talk to) should say so
    # directly rather than have this module re-derive it from `url` through
    # a second, possibly differently-sourced endpoint table: the two agree
    # in production (both read `core.database.ModelEndpoint`), but nothing
    # here should DEPEND on them staying in lockstep when the caller already
    # knows the answer.
    if endpoint_id:
        endpoint_id = str(endpoint_id)
    else:
        try:
            endpoint_id = _endpoint_id_for_netloc(url)
        except Exception as e:  # noqa: BLE001
            logger.debug("model_load_options: resolve_with_origin endpoint lookup failed: %s", e)
            endpoint_id = ""

    layers: Dict[str, Tuple[Dict[str, Any], str]] = {}
    try:
        layers[SCOPE_GLOBAL] = _layer_options(endpoint_id, model, SCOPE_GLOBAL, "")
        if project_id:
            layers[SCOPE_PROJECT] = _layer_options(endpoint_id, model, SCOPE_PROJECT, str(project_id))
        if session_id:
            layers[SCOPE_SESSION] = _layer_options(endpoint_id, model, SCOPE_SESSION, str(session_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("model_load_options: resolve_with_origin layer read failed: %s", e)

    fields = sorted({f for opts, _ in layers.values() for f in opts})
    options: Dict[str, Any] = {}
    origin: Dict[str, Dict[str, str]] = {}
    overridden: Dict[str, List[Dict[str, Any]]] = {}
    for field_name in fields:
        winner_scope = ""
        for scope in SCOPE_PRECEDENCE:
            opts, path = layers.get(scope, ({}, ""))
            if field_name not in opts:
                continue
            if not winner_scope:
                winner_scope = scope
                options[field_name] = opts[field_name]
                origin[field_name] = {"scope": scope, "path": path}
                continue
            overridden.setdefault(field_name, []).append(
                {"scope": scope, "value": opts[field_name], "path": path}
            )
    return {
        "endpoint_id": endpoint_id,
        "model": str(model or "").strip(),
        "options": options,
        "origin": origin,
        "overridden": overridden,
    }
