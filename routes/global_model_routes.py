"""Stateless OpenAI transport for the exact configured global model.

Credentials stay in Faustus. No session, history, agent, model launch, fallback
or retry is involved. The caller pins the descriptor for its whole native job.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from src.auth_helpers import _auth_disabled, effective_user, require_user
from src.tls_overrides import llm_verify

_CONNECTION_FIELDS = (
    'id', 'connection_revision', 'base_url', 'api_key', 'provider_auth_id',
    'endpoint_kind', 'owner', 'is_enabled', 'hidden_models',
)
_EXPECTED = ('expected_endpoint_id', 'expected_model', 'expected_connection_revision')
_RESPONSE_HEADERS = ('content-type', 'retry-after', 'x-request-id', 'request-id')


def _error(status, code):
    # Never attach exception text, endpoint URLs or credential material.
    return HTTPException(status, detail={'error': code})


def _caller(request):
    if getattr(request.state, 'api_token', False):
        if 'chat' not in set(getattr(request.state, 'api_token_scopes', ()) or ()):
            raise _error(403, 'chat_scope_required')
        owner = getattr(request.state, 'api_token_owner', None)
        if not owner or owner in ('api', 'internal-tool', 'system', 'demo'):
            raise _error(403, 'token_owner_required')
    else:
        require_user(request)
        owner = effective_user(request) or ''
    auth = getattr(request.app.state, 'auth_manager', None)
    admin = _auth_disabled() or not owner or bool(auth and auth.is_admin(owner))
    return owner, admin


def _read_target(owner, admin):
    # Importing the router must not initialize a database or load model code.
    from core.database import ModelEndpoint, SessionLocal
    from src.settings import load_settings

    settings = load_settings()
    identity, model = settings.get('default_endpoint_id'), settings.get('default_model')
    if not isinstance(identity, str) or not identity or not isinstance(model, str) or not model:
        raise _error(503, 'global_model_unconfigured')
    with SessionLocal() as db:
        row = db.query(ModelEndpoint).filter(ModelEndpoint.id == identity).first()
        if row is None or (not admin and row.owner not in (None, owner)):
            raise _error(404, 'global_model_not_found')
        endpoint = SimpleNamespace(**{name: getattr(row, name, None) for name in _CONNECTION_FIELDS})
    if not endpoint.is_enabled:
        raise _error(503, 'global_model_disabled')
    import json
    try:
        hidden = json.loads(endpoint.hidden_models) if isinstance(endpoint.hidden_models, str) else endpoint.hidden_models
    except ValueError:
        raise _error(503, 'global_model_configuration_invalid') from None
    if model in (hidden or ()):
        raise _error(503, 'global_model_hidden')
    return endpoint, model


def _descriptor(endpoint, model):
    return {'endpoint_id': endpoint.id, 'model': model,
            'connection_revision': endpoint.connection_revision or '',
            'chat_path': '/api/global-model/v1/chat/completions', 'protocol': 'openai',
            'vision': _vision(endpoint, model)}


def _vision(endpoint, model):
    from src.endpoint_resolver import build_chat_url
    from src.vision_routing import known_vision_models
    # Existing cache/name metadata only; never probe or select another VLM.
    return model in known_vision_models(build_chat_url(endpoint.base_url), [model])


def _runtime(endpoint, owner):
    from src.endpoint_resolver import (
        EndpointConfigurationChanged, build_chat_url, build_headers, resolve_endpoint_runtime,
    )

    try:
        base, key = resolve_endpoint_runtime(endpoint, owner=owner or None)
    except EndpointConfigurationChanged:
        raise _error(409, 'global_model_changed') from None
    url = build_chat_url(base)
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.path.endswith('/chat/completions'):
        raise _error(503, 'global_model_protocol_unsupported')
    return url, build_headers(key, base)


def _resolve(owner, admin, expected):
    endpoint, model = _read_target(owner, admin)
    if (endpoint.id, model, endpoint.connection_revision or '') != expected:
        raise _error(409, 'global_model_changed')
    try:
        url, headers = _runtime(endpoint, owner)
        # Credential refresh can overlap a settings/connection writer. Verify
        # both the global pick and all private connection fields before send.
        current, current_model = _read_target(owner, admin)
    except HTTPException:
        raise
    except Exception:
        raise _error(503, 'global_model_credentials_unavailable') from None
    if current_model != model or any(getattr(current, field) != getattr(endpoint, field) for field in _CONNECTION_FIELDS):
        raise _error(409, 'global_model_changed')
    return url, headers


def _client():
    # Only stored LLM endpoints can reach this transport. Preserve the same
    # private-CA support as the other Faustus LLM provider entry points.
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15), verify=llm_verify(),
                             trust_env=False, follow_redirects=False)


async def _while_connected(request, operation):
    """A caller leaving before headers/body must also close the provider call."""
    async def disconnected():
        while (await request.receive())['type'] != 'http.disconnect':
            pass
    pending = asyncio.create_task(operation)
    departure = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((pending, departure), return_when=asyncio.FIRST_COMPLETED)
        if pending in done:
            return await pending
        raise _error(499, 'caller_disconnected')
    finally:
        pending.cancel()
        departure.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(pending, departure, return_exceptions=True)


def setup_global_model_routes():
    router = APIRouter()

    @router.get('/global-model')
    def descriptor(request: Request):
        owner, admin = _caller(request)
        return _descriptor(*_read_target(owner, admin))

    @router.post('/global-model/v1/chat/completions')
    async def completions(request: Request):
        owner, admin = _caller(request)
        try:
            payload = await request.json()
        except ValueError:
            raise _error(400, 'invalid_json') from None
        if not isinstance(payload, dict):
            raise _error(422, 'object_required')
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in _EXPECTED):
            raise _error(422, 'global_model_descriptor_required')
        expected = tuple(payload.pop(key) for key in _EXPECTED)
        if payload.get('model', expected[1]) != expected[1]:
            raise _error(409, 'global_model_changed')
        if any(key in payload for key in ('url', 'base_url', 'endpoint_url', 'headers', 'api_key', 'provider_auth_id')):
            raise _error(422, 'destination_override_not_supported')
        if not isinstance(payload.get('messages'), list) or not payload['messages']:
            raise _error(422, 'messages_required')
        if 'stream' in payload and not isinstance(payload['stream'], bool):
            raise _error(422, 'stream_must_be_boolean')
        payload['model'] = expected[1]
        url, auth = await asyncio.to_thread(_resolve, owner, admin, expected)
        client = _client()
        upstream = None
        closed = False

        async def close():
            nonlocal closed
            if closed:
                return
            closed = True
            # Starlette may be closing this iterator inside a cancelled scope.
            # Retain the upstream until its close and the client close finish.
            with anyio.CancelScope(shield=True):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    await client.aclose()

        try:
            upstream = await _while_connected(request, client.send(
                client.build_request('POST', url, json=payload, headers=auth), stream=True))
            headers = {key: upstream.headers[key] for key in _RESPONSE_HEADERS if key in upstream.headers}
            headers['Cache-Control'] = 'no-store'
            if payload.get('stream'):
                async def chunks():
                    try:
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                    finally:
                        await close()
                class RelayResponse(StreamingResponse):
                    async def __call__(self, scope, receive, send):
                        try:
                            await super().__call__(scope, receive, send)
                        finally:
                            await close()
                return RelayResponse(chunks(), status_code=upstream.status_code, headers=headers)
            content = await _while_connected(request, upstream.aread())
            return Response(content, status_code=upstream.status_code, headers=headers)
        except httpx.TimeoutException:
            raise _error(504, 'global_model_timeout') from None
        except httpx.HTTPError:
            raise _error(502, 'global_model_transport_failed') from None
        finally:
            if upstream is None or not payload.get('stream'):
                await close()

    return router
