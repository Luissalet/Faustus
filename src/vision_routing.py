"""Vision routing: which vision-language model (VLM) reads images for a chat
model that cannot see them.

One resolver for every caller — chat attachments, tool-result images,
`inspect_image`, the gallery and the PDF helpers all reach it through
`src.document_processor._resolve_vl_model` — so "which VLM answers" has one
answer, shown to the user by `GET /api/vision/status`.

Order:

1. **Configured.** `vision_model` (per-user preference first, then the global
   setting) and `vision_endpoint_id`. With an endpoint set, the model is
   resolved on that endpoint (an empty model picks the endpoint's
   vision-capable one); without it, `vision_model` keeps its old meaning
   ("model" or "model@Endpoint name", searched across endpoints).
2. **Auto.** Nothing configured:
   a. a model the user's LOCAL endpoints report as vision-capable (Ollama
      `/api/show` capabilities, llama.cpp `/props` modalities, LM Studio
      `capabilities.vision` — the same probes `model_supports_vision` uses);
   b. a known VLM name (`AUTO_CANDIDATES`) among the endpoints' cached model
      lists, local endpoints first;
   c. the old live lookup of the same names (`ai_interaction._resolve_model`),
      only when some endpoint has no cached list to match against.
   An endpoint the privacy gate would refuse for `ocr_vision` is never picked
   by auto-detection (under `local_only` a cloud VLM is not silently used).
   The auto result is cached per owner for `AUTO_TTL` seconds (a miss for
   `AUTO_MISS_TTL`), so the probes do not run per image.

`known_vision_models` answers "which of these names can see" from caches and
the name heuristic only — never a network call — for `GET /api/models`.

`strip_images_for_text_only_route` is the proactive history filter: before a
request goes to a route that cannot see, image blocks are replaced by the
cached description of the image or a short placeholder.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

AUTO_TTL = 60.0
AUTO_MISS_TTL = 20.0
# Phase (a) probes: at most this many models per endpoint and this much
# wall time in total, so a slow local server cannot hold a turn.
_MAX_PROBED_MODELS = 24
_PROBE_BUDGET_S = 6.0

#: Local VLM families, tried before hosted names when matching cached lists.
LOCAL_VLM_CANDIDATES = (
    "qwen3-vl", "qwen2.5vl", "qwen2.5-vl", "gemma3", "llama3.2-vision",
    "minicpm-v", "moondream", "llava", "pixtral", "qwen2-vl",
)
#: The hosted names the old auto-detection tried, in their old order.
HOSTED_VLM_CANDIDATES = (
    "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
    "claude-sonnet-4-5-20250929", "claude-opus-4-20250514",
    "gemini-2.0-flash", "gemini-2.5-pro",
)
#: Every name auto-detection looks for. The first eleven keep the order the
#: resolver always had; the modern local families follow.
AUTO_CANDIDATES = HOSTED_VLM_CANDIDATES + ("llava", "pixtral", "qwen2-vl") + tuple(
    c for c in LOCAL_VLM_CANDIDATES if c not in ("llava", "pixtral", "qwen2-vl")
)

_cache_lock = threading.Lock()
_auto_cache: Dict[str, tuple] = {}


def clear_cache() -> None:
    """Forget every cached auto-detection and live inventory (tests, endpoint changes)."""
    with _cache_lock:
        _auto_cache.clear()
        _live_cache.clear()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _user_pref(key: str, owner: Optional[str]) -> Any:
    """The owner's own preference for `key`, or None when they set none."""
    if not owner:
        return None
    try:
        from routes.prefs_routes import _load_for_user
        prefs = _load_for_user(owner) or {}
    except Exception:  # noqa: BLE001 - prefs unavailable: use the global
        return None
    value = prefs.get(key)
    return None if value in (None, "") else value


def _global_setting(key: str, default: Any = "") -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def configured_vision_endpoint_id(owner: Optional[str]) -> str:
    value = _user_pref("vision_endpoint_id", owner)
    if value is None:
        value = _global_setting("vision_endpoint_id", "")
    return str(value or "").strip()


def configured_vision_model(owner: Optional[str]) -> str:
    value = _user_pref("vision_model", owner)
    if value is None:
        value = _global_setting("vision_model", "")
    return str(value or "").strip()


# ---------------------------------------------------------------------------
# privacy + endpoints
# ---------------------------------------------------------------------------

def _gate_allows(url: str, owner: Optional[str]) -> bool:
    """Whether the privacy profile lets `ocr_vision` reach `url`. Auto-detection
    treats any doubt as "no": it must never pick a destination the gate would
    refuse at call time."""
    try:
        from src.privacy_policy import assert_outbound
        assert_outbound("ocr_vision", url, owner=owner)
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_local(url: str) -> bool:
    try:
        from src.privacy_policy import is_local_destination
        return bool(is_local_destination(url))
    except Exception:  # noqa: BLE001
        try:
            from src.chat_helpers import _is_local_host
            return _is_local_host(urlparse(url or "").hostname)
        except Exception:  # noqa: BLE001
            return False


def list_vision_endpoints(owner: Optional[str]) -> List[Dict[str, Any]]:
    """The owner's enabled chat endpoints as plain dicts:
    `{id, name, url, headers, models, local}` (`url` is the chat URL)."""
    out: List[Dict[str, Any]] = []
    try:
        from src.database import SessionLocal, ModelEndpoint
        from src.endpoint_resolver import (
            _endpoint_enabled_models, build_chat_url, build_headers, resolve_endpoint_runtime,
        )
    except Exception:  # noqa: BLE001
        return out
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        rows = q.all()
        for ep in rows:
            if (getattr(ep, "model_type", None) or "llm") not in ("llm", "chat", "vision"):
                continue
            try:
                base, api_key = resolve_endpoint_runtime(ep, owner=owner)
            except Exception:  # noqa: BLE001
                continue
            url = build_chat_url(base)
            if not str(url or "").lower().startswith(("http://", "https://")):
                continue
            models = list(_endpoint_enabled_models(ep))
            local = _is_local(url)
            if local:
                try:
                    from src.endpoint_resolver import _endpoint_hidden_models
                    hidden = _endpoint_hidden_models(ep)
                except Exception:  # noqa: BLE001
                    hidden = set()
                for name in live_local_inventory(url).get("models") or {}:
                    if name not in hidden and name not in models:
                        models.append(name)
            out.append({
                "id": ep.id,
                "name": getattr(ep, "name", None) or ep.id,
                "url": url,
                "headers": build_headers(api_key, base),
                "models": models,
                "local": local,
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("vision routing: endpoint listing failed: %s", exc)
    finally:
        db.close()
    return out


# ---------------------------------------------------------------------------
# capability
# ---------------------------------------------------------------------------

_LIVE_TTL_S = 60.0
_live_cache: Dict[str, tuple] = {}


def _ollama_base(url: str) -> str:
    """`http://host:port` of a local Ollama chat URL, or "" for anything else."""
    try:
        from src.chat_helpers import _is_local_ollama_url
        if not _is_local_ollama_url(url):
            return ""
    except Exception:  # noqa: BLE001
        return ""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def live_local_inventory(url: str) -> Dict[str, Any]:
    """What a LOCAL Ollama has installed right now and what is loaded:
    `{"models": {name: size_bytes}, "loaded": set(names)}`. Two cheap loopback
    calls (/api/tags, /api/ps) cached for a minute; empty for other servers.

    The endpoint's cached model list can be days old (a VLM pulled this
    morning is not in it), and the helper should prefer a model that is
    already in memory or small over loading a second big general model."""
    base = _ollama_base(url)
    if not base:
        return {"models": {}, "loaded": set()}
    now = time.monotonic()
    with _cache_lock:
        hit = _live_cache.get(base)
        if hit and now - hit[0] < _LIVE_TTL_S:
            return hit[1]
    inv: Dict[str, Any] = {"models": {}, "loaded": set()}
    try:
        import httpx
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            tags = client.get(base + "/api/tags")
            if tags.status_code == 200:
                for m in (tags.json() or {}).get("models") or []:
                    name = m.get("name") or m.get("model")
                    if name:
                        inv["models"][name] = int(m.get("size") or 0)
            ps = client.get(base + "/api/ps")
            if ps.status_code == 200:
                for m in (ps.json() or {}).get("models") or []:
                    name = m.get("name") or m.get("model")
                    if name:
                        inv["loaded"].add(name)
    except Exception as exc:  # noqa: BLE001 - a probe never breaks a turn
        logger.debug("vision routing: live inventory of %s failed: %s", base, exc)
    with _cache_lock:
        _live_cache[base] = (now, inv)
    return inv


def _dedicated_vlm(name: str) -> bool:
    n = (name or "").lower()
    return any(c in n for c in LOCAL_VLM_CANDIDATES) or bool(
        re.search(r"(?<![a-z])vl(?![a-z])|vlm|vision|llava|moondream|minicpm-v", n))


def rank_capable(url: str, capable: Sequence[str]) -> List[str]:
    """Order vision-capable models for the helper role: already loaded
    first (no load, no eviction), then dedicated vision models, then the
    smallest. A big general model that also sees is the last resort."""
    inv = live_local_inventory(url)
    sizes = inv.get("models") or {}
    loaded = inv.get("loaded") or set()

    def key(m: str):
        return (0 if m in loaded else 1, 0 if _dedicated_vlm(m) else 1,
                sizes.get(m) or 10**13, capable.index(m))
    return sorted(capable, key=key)


def _probe_endpoint(url: str, models: Sequence[str], deadline: float) -> tuple:
    """(vision-capable models in probe order, models reported text-only) for
    one LOCAL endpoint, using the server's own capability reports."""
    from src import chat_helpers as ch

    capable: List[str] = []
    negative: set = set()
    if not models:
        return capable, negative
    # Likely-vision names first so a hit comes before the budget runs out.
    ordered = sorted(models, key=lambda m: 0 if ch.is_vision_model(m) else 1)[:_MAX_PROBED_MODELS]
    try:
        lm = ch.lmstudio_supports_vision(url, ordered[0])
    except Exception:  # noqa: BLE001
        lm = None
    if lm is not None:
        for m in ordered:
            try:
                ans = ch.lmstudio_supports_vision(url, m)
            except Exception:  # noqa: BLE001
                ans = None
            if ans is True:
                capable.append(m)
            elif ans is False:
                negative.add(m)
        return capable, negative
    try:
        props = ch.llamacpp_supports_vision(url)
    except Exception:  # noqa: BLE001
        props = None
    if props is not None:
        # A llama-server serves one model: its projector decides for all.
        if props:
            capable.extend(ordered)
        else:
            negative.update(models)
        return capable, negative
    for m in ordered:
        if time.monotonic() > deadline:
            break
        try:
            ans = ch.ollama_supports_vision(url, m)
        except Exception:  # noqa: BLE001
            ans = None
        if ans is True:
            capable.append(m)
        elif ans is False:
            negative.add(m)
    return capable, negative


def _match_candidate(candidate: str, models: Sequence[str], negative: set) -> Optional[str]:
    c = candidate.lower()
    for m in models:
        if m.lower() == c and m not in negative:
            return m
    for m in models:
        if m in negative:
            continue
        ml = m.lower()
        if c in ml or ml in c:
            return m
    return None


def _route(ep: Dict[str, Any], model: str, source: str) -> Dict[str, Any]:
    return {
        "url": ep["url"], "model": model, "headers": dict(ep.get("headers") or {}),
        "endpoint_id": ep.get("id"), "endpoint_name": ep.get("name") or "",
        "local": bool(ep.get("local")), "source": source, "error": "",
    }


def _none(error: str) -> Dict[str, Any]:
    return {"url": "", "model": "", "headers": {}, "endpoint_id": None, "endpoint_name": "",
            "local": False, "source": "none", "error": error}


def _pick_on_endpoints(endpoints: List[Dict[str, Any]], owner: Optional[str],
                       source: str) -> Optional[Dict[str, Any]]:
    """Phases (a) and (b) over `endpoints` (already privacy-filtered)."""
    deadline = time.monotonic() + _PROBE_BUDGET_S
    negatives: Dict[str, set] = {}
    local = [ep for ep in endpoints if ep.get("local")]
    remote = [ep for ep in endpoints if not ep.get("local")]
    for ep in local:
        capable, negative = _probe_endpoint(ep["url"], ep.get("models") or [], deadline)
        negatives[ep["url"]] = negative
        if capable:
            return _route(ep, rank_capable(ep["url"], capable)[0], source)
    for group, names in ((local, AUTO_CANDIDATES), (remote, AUTO_CANDIDATES)):
        for ep in group:
            # A hosted model name on a local server is an alias of something
            # else (an Ollama tag called like a cloud model) -- only the
            # local VLM families count there.
            ranked = names if group is remote else LOCAL_VLM_CANDIDATES
            for cand in ranked:
                hit = _match_candidate(cand, ep.get("models") or [], negatives.get(ep["url"], set()))
                if hit:
                    return _route(ep, hit, source)
    return None


def _auto_detect(owner: Optional[str]) -> Dict[str, Any]:
    endpoints = list_vision_endpoints(owner)
    allowed = [ep for ep in endpoints if _gate_allows(ep["url"], owner)]
    found = _pick_on_endpoints(allowed, owner, "auto")
    if found:
        return found
    # (c) the old live lookup — only when some endpoint had nothing cached to
    # match against (or the listing itself was unavailable).
    if not endpoints or any(not ep.get("models") for ep in endpoints):
        from src import ai_interaction
        for cand in AUTO_CANDIDATES:
            try:
                url, model_id, headers = ai_interaction._resolve_model(cand, owner=owner)
            except Exception:  # noqa: BLE001
                continue
            if not url or not model_id or not _gate_allows(url, owner):
                continue
            if _is_local(url):
                from src import chat_helpers as _ch
                try:
                    confirmed = _ch.ollama_supports_vision(url, model_id)
                except Exception:  # noqa: BLE001
                    confirmed = None
                if confirmed is not True and not _dedicated_vlm(model_id):
                    continue
            ep = next((e for e in endpoints if e["url"].rstrip("/") == str(url).rstrip("/")), None)
            return {
                "url": url, "model": model_id, "headers": dict(headers or {}),
                "endpoint_id": ep["id"] if ep else None,
                "endpoint_name": ep["name"] if ep else "",
                "local": _is_local(url), "source": "auto", "error": "",
            }
    return _none("No vision model available")


def _resolve_on_endpoint(ep_id: str, model: str, owner: Optional[str]) -> Optional[Dict[str, Any]]:
    endpoints = [ep for ep in list_vision_endpoints(owner) if ep["id"] == ep_id]
    if not endpoints:
        # Not listed (e.g. model_type) — let the resolver decide.
        try:
            from src.endpoint_resolver import resolve_endpoint_by_id
            resolved = resolve_endpoint_by_id(ep_id, model or None, owner=owner)
        except Exception:  # noqa: BLE001
            resolved = None
        if not resolved or not model:
            return None
        url, mid, headers = resolved
        return {"url": url, "model": mid, "headers": dict(headers or {}), "endpoint_id": ep_id,
                "endpoint_name": "", "local": _is_local(url), "source": "configured", "error": ""}
    ep = endpoints[0]
    if model:
        models = ep.get("models") or []
        hit = model if (not models or model in models) else _match_candidate(model, models, set())
        return _route(ep, hit or model, "configured")
    picked = _pick_on_endpoints([ep], owner, "configured")
    if picked:
        return picked
    from src import chat_helpers as ch
    for m in ep.get("models") or []:
        if ch.is_vision_model(m):
            return _route(ep, m, "configured")
    return None


def resolve_vision_route(owner: Optional[str] = None, configured: Optional[str] = None,
                         *, use_cache: bool = True) -> Dict[str, Any]:
    """The VLM that answers for `owner`, as
    `{url, model, headers, endpoint_id, endpoint_name, local, source, error}`
    with `source` one of `configured` / `auto` / `none`.

    `configured` is the model the caller already read (the global
    `vision_model` or an explicit override); an empty/None value falls back
    to the owner's preference. A configured model that cannot be resolved is
    reported as `source: none` with the reason — it is never silently
    replaced by an auto-detected one."""
    model = str(configured or "").strip()
    user_model = str(_user_pref("vision_model", owner) or "").strip()
    if user_model and (not model or model == str(_global_setting("vision_model", "") or "").strip()):
        # The caller passed the global default; the owner's own choice wins.
        model = user_model
    elif not model:
        model = configured_vision_model(owner)
    ep_id = configured_vision_endpoint_id(owner)
    if ep_id:
        route = _resolve_on_endpoint(ep_id, model, owner)
        if route:
            return route
        if not model:
            return _none("The vision endpoint has no vision-capable model (or is not available)")
    if model:
        from src import ai_interaction
        try:
            url, model_id, headers = ai_interaction._resolve_model(model, owner=owner)
        except Exception as exc:  # noqa: BLE001
            out = _none(str(exc) or "not found")
            out["configured"] = model
            return out
        return {"url": url, "model": model_id, "headers": dict(headers or {}),
                "endpoint_id": None, "endpoint_name": "", "local": _is_local(url),
                "source": "configured", "error": ""}

    key = owner or ""
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            hit = _auto_cache.get(key)
        if hit and hit[0] > now:
            return dict(hit[1])
    route = _auto_detect(owner)
    ttl = AUTO_TTL if route.get("model") else AUTO_MISS_TTL
    with _cache_lock:
        _auto_cache[key] = (now + ttl, dict(route))
    return route


def vision_status(owner: Optional[str]) -> Dict[str, Any]:
    """What `GET /api/vision/status` returns: the VLM actually in use."""
    enabled = _user_pref("vision_enabled", owner)
    if enabled is None:
        enabled = _global_setting("vision_enabled", True)
    route = resolve_vision_route(owner)
    return {
        "enabled": bool(enabled),
        "source": route.get("source") or "none",
        "model": route.get("model") or "",
        "endpoint_id": route.get("endpoint_id"),
        "endpoint_name": route.get("endpoint_name") or "",
        "local": bool(route.get("local")),
        "configured_model": configured_vision_model(owner),
        "configured_endpoint_id": configured_vision_endpoint_id(owner),
        "error": route.get("error") or "",
    }


# ---------------------------------------------------------------------------
# /api/models: capability from caches only
# ---------------------------------------------------------------------------

def known_vision_models(chat_url: str, model_ids: Sequence[str], backend: str = "") -> List[str]:
    """Which of `model_ids` on `chat_url` accept images, from what is already
    known: the Ollama capability cache, the LM Studio and llama.cpp probe
    caches, then the name heuristic. Never opens a connection.

    `backend="llamacpp"`: a llama-server sees images only when it was started
    with a projector (`--mmproj`), whatever the model's name says, so with
    no cached `/props` answer nothing is claimed."""
    from src import chat_helpers as ch

    out: List[str] = []
    try:
        parsed = urlparse(chat_url or "")
        host, port = parsed.hostname or "", parsed.port
        root = f"{parsed.scheme or 'http'}://{parsed.netloc}"
    except ValueError:
        host, port, root = "", None, ""
    lm_entry = ch._lmstudio_models_cache.get((host, port)) if host else None
    lm_models = lm_entry[0] if lm_entry else None
    llama_entry = ch._llamacpp_props_cache.get((host, port)) if host else None
    llama = llama_entry[0] if llama_entry else None
    try:
        from src.llm_core import _ollama_caps_cache
    except Exception:  # noqa: BLE001
        _ollama_caps_cache = {}
    for mid in model_ids or []:
        if not isinstance(mid, str) or not mid:
            continue
        answer: Optional[bool] = None
        if lm_models:
            want = mid.strip().lower()
            for m in lm_models:
                if not isinstance(m, dict):
                    continue
                names = {str(m.get("key", "")).lower(), str(m.get("display_name", "")).lower()}
                caps = m.get("capabilities")
                if want in names and isinstance(caps, dict) and "vision" in caps:
                    answer = bool(caps.get("vision"))
                    break
        if answer is None and llama is not None:
            answer = bool(llama)
        if answer is None and root:
            hit = _ollama_caps_cache.get((root, mid))
            caps = hit[1] if hit else None
            if caps:
                answer = "vision" in caps
        if answer is None and backend == "llamacpp":
            continue
        if answer is None:
            answer = ch.is_vision_model(mid)
        if answer:
            out.append(mid)
    return out


# ---------------------------------------------------------------------------
# proactive history filter
# ---------------------------------------------------------------------------

_IMAGE_BLOCK_TYPES = ("image_url", "image", "input_image")


def _is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES


def messages_have_images(messages: Sequence[Any]) -> bool:
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list) and any(_is_image_block(b) for b in content):
            return True
        if msg.get("images"):
            return True
    return False


def _cached_description(att: Dict[str, Any]) -> str:
    text = str(att.get("vision") or "").strip()
    if text and not text.startswith("["):
        return text
    att_id = str(att.get("id") or "")
    if not att_id or "/" in att_id or "\\" in att_id or att_id.startswith("."):
        return ""
    try:
        from src.constants import UPLOAD_DIR
        path = os.path.join(UPLOAD_DIR, ".vision", att_id + ".txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            if text and not text.startswith("["):
                return text
    except Exception:  # noqa: BLE001
        pass
    return ""


def placeholder(name: str) -> str:
    return f"[image: {name or 'image'} — not shown, model has no vision]"


def _replacement_texts(msg: Dict[str, Any], count: int) -> List[str]:
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    source = str(meta.get("source") or "")
    if source.startswith("tool result: "):
        name = f"from {source[len('tool result: '):]}"
        return [placeholder(name)] * count
    atts = [a for a in (meta.get("attachments") or [])
            if isinstance(a, dict) and str(a.get("mime") or "").startswith("image/")]
    out = []
    for i in range(count):
        att = atts[i] if i < len(atts) else {}
        name = str(att.get("name") or "image")
        desc = _cached_description(att) if att else ""
        out.append(f"[Image: {name} — described for a model without vision]\n{desc}" if desc
                   else placeholder(name))
    return out


def strip_images(messages: List[Dict[str, Any]]) -> tuple:
    """A copy of `messages` with every image block replaced by text (the cached
    description when one exists, else a placeholder), and how many were
    replaced. The input list and its dicts are never modified."""
    out: List[Dict[str, Any]] = []
    replaced = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        has_blocks = isinstance(content, list) and any(_is_image_block(b) for b in content)
        has_native = bool(msg.get("images"))
        if not has_blocks and not has_native:
            out.append(msg)
            continue
        new = dict(msg)
        if has_native:
            n_native = len(msg.get("images") or [])
            new.pop("images", None)
            texts = _replacement_texts(msg, n_native)
            base = new.get("content") if isinstance(new.get("content"), str) else ""
            new["content"] = "\n".join([t for t in [base, *texts] if t])
            replaced += n_native
        if has_blocks:
            n = sum(1 for b in content if _is_image_block(b))
            texts = iter(_replacement_texts(msg, n))
            blocks = []
            for b in content:
                if _is_image_block(b):
                    blocks.append({"type": "text", "text": next(texts)})
                else:
                    blocks.append(b)
            new["content"] = blocks
            replaced += n
        out.append(new)
    return out, replaced


def history_filter_enabled() -> bool:
    return bool(_global_setting("vision_history_filter", True))


def route_sees_images(url: str, model: str) -> bool:
    try:
        from src.chat_helpers import model_supports_vision
        return bool(model_supports_vision(model or "", url or ""))
    except Exception:  # noqa: BLE001
        from src.chat_helpers import is_vision_model
        return is_vision_model(model or "")


async def strip_images_for_text_only_route(url: str, model: str, messages: List[Dict[str, Any]]):
    """`messages` for a request to (`url`, `model`): unchanged when there is no
    image or the route can see; otherwise a copy without image blocks. The
    capability probe (cached, may touch a local server once) runs off the
    event loop, and only when an image is actually present."""
    if not messages or not history_filter_enabled() or not messages_have_images(messages):
        return messages
    import asyncio
    try:
        sees = await asyncio.to_thread(route_sees_images, url, model)
    except Exception:  # noqa: BLE001
        sees = True
    if sees:
        return messages
    stripped, n = strip_images(messages)
    if n:
        logger.info("[vision] %d image(s) replaced by text for text-only route %s", n, model)
    return stripped
