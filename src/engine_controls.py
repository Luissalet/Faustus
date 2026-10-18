"""Generation controls verified against both the engine and model template."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from typing import Any

_cache: dict[str, tuple[float, bool]] = {}
_schema_cache: dict[tuple[str, str], dict[str, Any]] = {}
_schema_refresh_lock = threading.Lock()
_SCHEMA_TTL = 60.0
_SCHEMA_MAX_ENDPOINTS = 128
_SCHEMA_ANNOTATIONS = {'title', 'description', 'nullable', 'default', 'example', 'examples', 'deprecated'}


def _schema_base(value: str) -> str:
    """Canonical configured API base, retaining its path and endpoint identity."""
    if not isinstance(value, str):
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return ''
        path = parsed.path.rstrip('/')
        if path.endswith('/chat/completions'):
            path = path[:-len('/chat/completions')]
        if not path.endswith('/v1'):
            path += '/v1'
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, '', ''))
    except ValueError:
        return ''


def _schema_variants(node, document, seen=()):
    """Resolve the local refs/unions needed by the chat response-format field."""
    if not isinstance(node, dict) or len(seen) > 32:
        return []
    if '$ref' in node:
        if set(node) - {'$ref'} - _SCHEMA_ANNOTATIONS:
            # Do not ignore conjunctive sibling constraints in OpenAPI 3.1.
            return []
        ref = node['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen:
            return []
        target = document
        for token in ref[2:].split('/'):
            if not isinstance(target, dict):
                return []
            target = target.get(token.replace('~1', '/').replace('~0', '~'))
        return _schema_variants(target, document, (*seen, ref))
    for union in ('anyOf', 'oneOf'):
        if union in node:
            if set(node) - {union} - _SCHEMA_ANNOTATIONS:
                return []
            values = node[union]
            if not isinstance(values, list):
                return []
            return [branch for value in values for branch in _schema_variants(value, document, (*seen, union))]
    return [node]


def _schema_type(node, kind, document):
    return any(branch.get('type') == kind and
               not set(branch) - {'type', 'additionalProperties'} - _SCHEMA_ANNOTATIONS and
               (kind != 'object' or branch.get('additionalProperties', True) is True or
                branch.get('additionalProperties') == {})
               for branch in _schema_variants(node, document))


def _schema_properties(node, allowed_required=None):
    properties, required = node.get('properties'), node.get('required', [])
    if node.get('type') != 'object' or not isinstance(properties, dict) or not isinstance(required, list) or not all(type(key) is str for key in required):
        return None
    if allowed_required is not None and not set(required) <= allowed_required:
        return None
    if set(node) - {'type', 'properties', 'required', 'additionalProperties'} - _SCHEMA_ANNOTATIONS:
        return None
    if not isinstance(node.get('additionalProperties', True), (bool, dict)):
        return None
    return properties


def _openapi_json_schema(document, api_base: str) -> bool:
    """Verify the actual chat route's typed JSON-schema request envelope.

    This checks protocol declaration, not whether a model obeys it or computes
    correct values. Unknown schema constructions remain unverified.
    """
    if not isinstance(document, dict) or not isinstance(document.get('paths'), dict):
        return False
    api_path = urlsplit(api_base).path.rstrip('/') + '/chat/completions'
    for path in dict.fromkeys((api_path, '/v1/chat/completions')):
        route = document['paths'].get(path)
        post = route.get('post') if isinstance(route, dict) else None
        if not isinstance(post, dict):
            continue
        body = post.get('requestBody')
        for request_body in _schema_variants(body, document):
            content = request_body.get('content')
            media = content.get('application/json') if isinstance(content, dict) else None
            if not isinstance(media, dict):
                continue
            for request in _schema_variants(media.get('schema'), document):
                properties = _schema_properties(request)
                if properties is None:
                    continue
                for fmt in _schema_variants(properties.get('response_format'), document):
                    props = _schema_properties(fmt, {'type', 'json_schema'})
                    if props is None:
                        continue
                    selector = props.get('type')
                    selectors = _schema_variants(selector, document)
                    valid_selector = any(s.get('type') == 'string' and
                        not set(s) - {'type', 'enum', 'const'} - _SCHEMA_ANNOTATIONS and
                        ('const' not in s or s['const'] == 'json_schema') and
                        ('enum' not in s or isinstance(s['enum'], list) and
                         all(type(x) is str for x in s['enum']) and 'json_schema' in s['enum']) and
                        ('const' in s or 'enum' in s) for s in selectors)
                    if not valid_selector:
                        continue
                    for envelope in _schema_variants(props.get('json_schema'), document):
                        fields = _schema_properties(envelope, {'name', 'schema', 'strict'})
                        if fields is not None and all(
                            _schema_type(fields.get(name), kind, document)
                            for name, kind in (('name', 'string'), ('schema', 'object'), ('strict', 'boolean'))
                        ):
                            return True
    return False


def json_schema_capability(base_url: str, endpoint_id: str) -> dict[str, Any]:
    """Hot-path cache read only. Metadata refresh is owned by Sparks.sync()."""
    key = (endpoint_id, _schema_base(base_url))
    cached = _schema_cache.get(key) if isinstance(endpoint_id, str) else None
    if cached and time.monotonic() - cached['at'] < _SCHEMA_TTL:
        return {k: v for k, v in cached.items() if k != 'at'}
    return {'json_schema': False, 'status': 'unverified', 'source': 'served_openapi'}


def refresh_json_schema(endpoints: list[dict[str, Any]], *, fetch_openapi=None) -> dict[str, dict[str, Any]]:
    """Refresh identities supplied by the native managed-Sparks metadata sync.

    Never called by generation. Positive and negative results share a bounded
    TTL; changed URLs/models and retired identities invalidate old entries.
    """
    active = {}
    for row in endpoints:
        if not isinstance(row, dict):
            continue
        identity, base, models = row.get('endpoint_id'), _schema_base(row.get('base_url')), row.get('models')
        if not isinstance(identity, str) or not identity or not base or not isinstance(models, list) or not models or not all(type(model) is str and model for model in models):
            continue
        active[(identity, base)] = tuple(sorted(set(models)))
    with _schema_refresh_lock:
        for key in list(_schema_cache):
            if key not in active:
                _schema_cache.pop(key, None)
        for key, models in active.items():
            previous = _schema_cache.get(key)
            if previous and previous['models'] == models and time.monotonic() - previous['at'] < _SCHEMA_TTL:
                continue
            _schema_cache.pop(key, None)  # no stale proof while a changed identity is checked
            base = key[1]
            status, supported = 'unavailable', False
            try:
                if fetch_openapi is not None:
                    document = fetch_openapi(base)
                else:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(base[:-len('/v1')] + '/openapi.json', timeout=1) as response:
                        document = json.load(response)
                supported = _openapi_json_schema(document, base)
                status = 'verified' if supported else 'unsupported'
            except Exception:
                pass
            _schema_cache[key] = {'at': time.monotonic(), 'models': models, 'json_schema': supported,
                                  'status': status, 'source': 'served_openapi'}
            while len(_schema_cache) > _SCHEMA_MAX_ENDPOINTS:
                _schema_cache.pop(min(_schema_cache, key=lambda item: _schema_cache[item]['at']))
    return {identity: json_schema_capability(base, identity) for identity, base in active}


def vllm_thinking_budget(base_url: str) -> bool:
    """Probe a configured server's schema; never infer its engine from its IP."""
    base = re.sub(r"/v1(?:/.*)?$", "", base_url.rstrip("/"))
    cached = _cache.get(base)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    supported = False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + "/openapi.json", timeout=1) as response:
            schema = json.load(response)
        fields = schema.get("components", {}).get("schemas", {}).get("ChatCompletionRequest", {}).get("properties", {})
        supported = "thinking_token_budget" in fields and "chat_template_kwargs" in fields
    except Exception:
        pass
    _cache[base] = (time.monotonic(), supported)
    return supported


def glm53_flash(model: str) -> bool:
    normalized = re.sub(r"[_. ]", "-", str(model).lower())
    return bool(re.search(r"(?:^|/)glm-5-3-flash(?:-|$)", normalized))


def controls_for(base_url: str, model: str) -> dict[str, Any] | None:
    if glm53_flash(model) and vllm_thinking_budget(base_url):
        # NVIDIA GLM-5.3-Flash's published template: low / high, else max.
        return {"levels": ["low", "high", "max"], "default": "max",
                "source": "verified_model_template", "engine": "vllm",
                "thinking_supported": True, "thinking_toggle": True,
                "thinking_off_behavior": "low_effort"}
    return None


def apply_controls(payload: dict[str, Any], base_url: str, model: str, *, reasoning_budget: Any = None) -> None:
    caps = controls_for(base_url, model)
    if not caps:
        return
    template = dict(payload.get("chat_template_kwargs") or {})
    thinking = template.get("enable_thinking", True)
    native_effort = template.pop("reasoning_effort", None)
    effort = str(payload.get("reasoning_effort") or native_effort or "max").lower()
    if effort not in caps["levels"]:
        # Fit generic Faustus effort profiles to the model's actual levels.
        effort = "low" if effort in ("none", "minimal", "medium") else "max"
    if not thinking:
        # GLM-5.3-Flash always reasons. Its supported fast path is Low; an
        # artificially empty think block is outside its training template.
        effort = "low"
    template["enable_thinking"] = True
    template.setdefault("clear_thinking", True)
    payload["chat_template_kwargs"] = template
    payload["reasoning_effort"] = effort
    budget = payload.pop("reasoning_budget", None)
    if reasoning_budget is not None:
        budget = reasoning_budget  # an explicit budget does not require a separate think toggle
    if budget is not None:
        # Honor the caller's existing budget, including -1 (unlimited), and
        # leave validation to the engine. Never invent a default cap or
        # replace an explicitly supplied native budget when no override exists.
        payload["thinking_token_budget"] = budget
    # Do not pass llama-server's spelling to vLLM.
    payload.pop("thinking_budget_tokens", None)
