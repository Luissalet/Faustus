"""src/openrouter_options.py — per-endpoint OpenRouter request options
(OBJ-8, Lote A2).

OpenRouter's own `/chat/completions` accepts a family of knobs no other
provider in `src/llm_core.py` understands: a `provider` preference block
(routing sort order, an explicit provider allow/deny list, a max price
ceiling, zero-data-retention, opt-out of training/retention), an opt-in web
search plugin, and a "native" multi-model fallback list (`models`, tried in
order by OpenRouter itself rather than Faustus's own retry loop). None of
this existed before this lote — every OpenRouter call went out with whatever
a generic OpenAI-compatible payload happens to carry.

This module is the single place those options are validated, persisted (one
JSON document per `endpoint_id`, `DATA_DIR/openrouter_endpoints.json`), and
turned into the extra payload keys a call actually needs. It never makes a
network call itself and never imports `src.llm_core` — `llm_core` imports
`apply_openrouter_payload` at the three points a payload is otherwise
complete (`llm_call`, `llm_call_async`, `_stream_llm_inner`), not the other
way round.

Faustus principle ("nunca pasar a pago silenciosamente" / one authority over
privacy, `src/privacy_policy.py`): the web search plugin — which OpenRouter
bills separately per query — is only ever added when a caller explicitly
asks for it this turn (`web_search_requested=True`) or the endpoint owner
opted a saved endpoint into it ahead of time (`prefs.web_search.enabled`);
never by default. `data_collection` follows the same discipline in the other
direction — see `apply_openrouter_payload`'s docstring for the exact rule.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Dict, List, Mapping, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

STORE_PATH = os.path.join(DATA_DIR, "openrouter_endpoints.json")

#: "" means "let OpenRouter pick" (its own default routing).
SORT_VALUES = ("", "price", "throughput", "latency")
DATA_COLLECTION_VALUES = ("auto", "allow", "deny")
MAX_ORDER_PROVIDERS = 20
MIN_WEB_SEARCH_RESULTS = 1
MAX_WEB_SEARCH_RESULTS = 10
DEFAULT_WEB_SEARCH_RESULTS = 5

_lock = threading.Lock()


def _default_prefs() -> Dict[str, Any]:
    """A fresh copy of the schema's defaults — every field present, nothing
    borrowed from a previous caller's mutation."""
    return {
        "sort": "",
        "allow_fallbacks": True,
        "require_parameters": False,
        "max_price": None,
        "zdr": False,
        "order": [],
        "ignore": [],
        "data_collection": "auto",
        "web_search": {"enabled": False, "max_results": DEFAULT_WEB_SEARCH_RESULTS},
        "native_fallback": False,
    }


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
def _validate_provider_list(name: str, value: Any) -> List[str]:
    if not isinstance(value, list) or len(value) > MAX_ORDER_PROVIDERS:
        raise ValueError(f"{name} must be a list of at most {MAX_ORDER_PROVIDERS} provider ids")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        out.append(item.strip())
    return out


def _validate_max_price(value: Any) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("max_price must be an object with optional 'prompt'/'completion' numbers")
    out: Dict[str, float] = {}
    for key in ("prompt", "completion"):
        if key not in value or value[key] is None:
            continue
        try:
            num = float(value[key])
        except (TypeError, ValueError):
            raise ValueError(f"max_price.{key} must be a number") from None
        if num < 0:
            raise ValueError(f"max_price.{key} must be >= 0")
        out[key] = num
    return out or None


def _validate_web_search(value: Any, base: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("web_search must be an object with 'enabled'/'max_results'")
    merged = dict(base)
    if "enabled" in value:
        if not isinstance(value["enabled"], bool):
            raise ValueError("web_search.enabled must be a boolean")
        merged["enabled"] = value["enabled"]
    if "max_results" in value:
        try:
            n = int(value["max_results"])
        except (TypeError, ValueError):
            raise ValueError("web_search.max_results must be an integer") from None
        if not (MIN_WEB_SEARCH_RESULTS <= n <= MAX_WEB_SEARCH_RESULTS):
            raise ValueError(
                f"web_search.max_results must be between {MIN_WEB_SEARCH_RESULTS} and {MAX_WEB_SEARCH_RESULTS}"
            )
        merged["max_results"] = n
    return merged


def _validate_patch(patch: Mapping[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """Validate `patch` against the known schema and merge it onto `base` (an
    already-defaulted prefs dict). Raises `ValueError` with a message naming
    the offending field — routes turn that straight into a 400.
    """
    if not isinstance(patch, Mapping):
        raise ValueError("patch must be an object")
    unknown = set(patch.keys()) - set(base.keys())
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    merged = dict(base)
    if "sort" in patch:
        if patch["sort"] not in SORT_VALUES:
            raise ValueError(f"sort must be one of: {', '.join(repr(v) for v in SORT_VALUES)}")
        merged["sort"] = patch["sort"]
    if "allow_fallbacks" in patch:
        if not isinstance(patch["allow_fallbacks"], bool):
            raise ValueError("allow_fallbacks must be a boolean")
        merged["allow_fallbacks"] = patch["allow_fallbacks"]
    if "require_parameters" in patch:
        if not isinstance(patch["require_parameters"], bool):
            raise ValueError("require_parameters must be a boolean")
        merged["require_parameters"] = patch["require_parameters"]
    if "max_price" in patch:
        merged["max_price"] = _validate_max_price(patch["max_price"])
    if "zdr" in patch:
        if not isinstance(patch["zdr"], bool):
            raise ValueError("zdr must be a boolean")
        merged["zdr"] = patch["zdr"]
    if "order" in patch:
        merged["order"] = _validate_provider_list("order", patch["order"])
    if "ignore" in patch:
        merged["ignore"] = _validate_provider_list("ignore", patch["ignore"])
    if "data_collection" in patch:
        if patch["data_collection"] not in DATA_COLLECTION_VALUES:
            raise ValueError(f"data_collection must be one of: {', '.join(DATA_COLLECTION_VALUES)}")
        merged["data_collection"] = patch["data_collection"]
    if "web_search" in patch:
        merged["web_search"] = _validate_web_search(
            patch["web_search"], base.get("web_search") or _default_prefs()["web_search"]
        )
    if "native_fallback" in patch:
        if not isinstance(patch["native_fallback"], bool):
            raise ValueError("native_fallback must be a boolean")
        merged["native_fallback"] = patch["native_fallback"]
    return merged


# ---------------------------------------------------------------------------
# Persistence — one JSON document, `{endpoint_id: prefs}`, atomic write
# (tempfile + os.replace, same pattern as `src/command_guard.py`).
# ---------------------------------------------------------------------------
def _load_store_locked() -> Dict[str, Any]:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("wrong shape")
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        logger.warning("openrouter_options: store at %s corrupt (%s); starting empty", STORE_PATH, exc)
        try:
            os.replace(STORE_PATH, STORE_PATH + ".corrupt")
        except OSError:
            pass
        return {}


def _save_store_locked(data: Dict[str, Any]) -> None:
    directory = os.path.dirname(STORE_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".openrouter_endpoints.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, STORE_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_prefs(endpoint_id: Optional[str]) -> Dict[str, Any]:
    """Effective prefs for `endpoint_id`: schema defaults merged with
    whatever was persisted for it. `None`/empty is a valid input, not an
    error — it means "no saved endpoint", which is what every `llm_core.py`
    call site has today (they know a bare url/model, not a saved endpoint
    id): those callers get plain defaults back.
    """
    defaults = _default_prefs()
    key = (endpoint_id or "").strip()
    if not key:
        return defaults
    with _lock:
        store = _load_store_locked()
    saved = store.get(key)
    if not isinstance(saved, dict):
        return defaults
    try:
        return _validate_patch(saved, defaults)
    except ValueError as exc:
        logger.warning("openrouter_options: stored prefs for %r invalid (%s); using defaults", key, exc)
        return defaults


def set_prefs(endpoint_id: str, patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate `patch` and merge it onto `endpoint_id`'s current (or
    default) prefs, persist, and return the resulting full prefs document.
    """
    key = (endpoint_id or "").strip()
    if not key:
        raise ValueError("endpoint_id must be a non-empty string")
    with _lock:
        store = _load_store_locked()
        base = _default_prefs()
        current = store.get(key)
        if isinstance(current, dict):
            try:
                base = _validate_patch(current, base)
            except ValueError:
                base = _default_prefs()  # corrupt on-disk entry: patch replaces it wholesale
        merged = _validate_patch(patch, base)
        store[key] = merged
        _save_store_locked(store)
    return merged


def delete_prefs(endpoint_id: str) -> None:
    """Remove `endpoint_id`'s saved prefs, if any. Idempotent — deleting an
    id with nothing saved is not an error."""
    key = (endpoint_id or "").strip()
    if not key:
        return
    with _lock:
        store = _load_store_locked()
        if key in store:
            del store[key]
            _save_store_locked(store)


def all_prefs() -> Dict[str, Dict[str, Any]]:
    """Every endpoint with a saved (validated) document — `GET
    /api/openrouter/prefs`. A corrupt individual entry is skipped rather than
    failing the whole listing."""
    with _lock:
        store = _load_store_locked()
    out: Dict[str, Dict[str, Any]] = {}
    for key, saved in store.items():
        if not isinstance(saved, dict):
            continue
        try:
            out[key] = _validate_patch(saved, _default_prefs())
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Payload application — the one thing `src/llm_core.py` calls.
# ---------------------------------------------------------------------------
def apply_openrouter_payload(
    payload: Dict[str, Any],
    *,
    provider: str,
    endpoint_id: Optional[str],
    model: str,
    project: Optional[Dict[str, Any]] = None,
    fallback_models: Optional[List[str]] = None,
    web_search_requested: Optional[bool] = None,
) -> Dict[str, Any]:
    """Mutate (and return) an OpenAI-compatible `payload` with this
    endpoint's OpenRouter options. A no-op whenever `provider != "openrouter"`
    — every other provider either ignores these fields or 400s on them, so
    this must never touch a payload headed anywhere else.

    Pure aside from the `get_prefs`/`privacy_policy.get_privacy_profile`
    reads: no network call, nothing sent, so any caller that already has a
    payload dict and a provider string can call this right before sending.
    `endpoint_id`/`project`/`fallback_models` are whatever the caller already
    has at hand — `None` is a normal, common input for all three, not a
    degraded case.
    """
    if provider != "openrouter":
        return payload

    # Lote A1 (src/llm_core.py's usage-normalization sites,
    # src/autonomy_budget.py) reads `usage.cost` / `usage.cost_details` /
    # `*_tokens_details` back off the response only when OpenRouter is asked
    # to include them. This is the one line that always applies, prefs or
    # not — real cost accounting shouldn't depend on an endpoint having been
    # configured at all.
    payload["usage"] = {"include": True}

    prefs = get_prefs(endpoint_id)

    provider_opts: Dict[str, Any] = {}
    if prefs["sort"]:
        provider_opts["sort"] = prefs["sort"]
    if prefs["allow_fallbacks"] is not True:
        provider_opts["allow_fallbacks"] = prefs["allow_fallbacks"]
    if prefs["require_parameters"]:
        provider_opts["require_parameters"] = True
    if prefs["max_price"]:
        provider_opts["max_price"] = dict(prefs["max_price"])
    if prefs["zdr"]:
        provider_opts["zdr"] = True
    if prefs["order"]:
        provider_opts["order"] = list(prefs["order"])
    if prefs["ignore"]:
        provider_opts["ignore"] = list(prefs["ignore"])

    data_collection = prefs.get("data_collection", "auto")
    resolved_data_collection: Optional[str] = None
    if data_collection in ("allow", "deny"):
        resolved_data_collection = data_collection
    else:
        # "auto": Faustus principle — never send data out silently. Under the
        # active privacy profile's strictest setting (`local_only`,
        # src/privacy_policy.py — "nothing should leave this machine"), opt
        # this OpenRouter call OUT of provider-side training/retention
        # explicitly, rather than trusting OpenRouter's own account-level
        # default to already agree. `local_preferred` (the default) and
        # `cloud_allowed` leave the field unset — `privacy_policy`'s own
        # docstring is explicit that neither blocks an outbound call, and
        # OpenRouter's dashboard-level setting is the honest source of truth
        # in that case, not a guess made here.
        from src import privacy_policy

        if privacy_policy.get_privacy_profile(project) == privacy_policy.PROFILE_LOCAL_ONLY:
            resolved_data_collection = "deny"
    if resolved_data_collection:
        provider_opts["data_collection"] = resolved_data_collection

    if provider_opts:
        payload["provider"] = provider_opts

    # web_search: never on by default — either this turn asked for it
    # explicitly, or the endpoint owner opted in ahead of time.
    web_search = prefs.get("web_search") or {}
    if web_search_requested is True or web_search.get("enabled"):
        max_results = web_search.get("max_results", DEFAULT_WEB_SEARCH_RESULTS)
        payload["plugins"] = [{"id": "web", "max_results": max_results}]

    if prefs.get("native_fallback") and fallback_models:
        combined = [model, *fallback_models]
        deduped: List[str] = []
        for candidate in combined:
            if isinstance(candidate, str) and candidate and candidate not in deduped:
                deduped.append(candidate)
        payload["models"] = deduped[:8]

    return payload
