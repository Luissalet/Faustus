"""Exercise the real global router inside app.py's actual timeout middleware.

The middleware is extracted with AST to avoid booting the full application,
database, pollers or models. Provider I/O is inert; its delay models cold prefill.
"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from routes import global_model_routes as route


def timeout_middleware():
    path = Path(__file__).resolve().parents[1] / 'app.py'
    tree = ast.parse(path.read_text(encoding='utf8'))
    names = {'_TIMEOUT_EXEMPT_PREFIXES', '_TIMEOUT_EXEMPT_SUFFIXES'}
    nodes = [n for n in tree.body if
             isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)
             or isinstance(n, ast.ClassDef) and n.name == '_RequestTimeoutMiddleware']
    assert len(nodes) == 3
    scope = dict(_asyncio=asyncio, _BaseHTTPMiddleware=BaseHTTPMiddleware,
                 _JSONResponse=JSONResponse, REQUEST_HARD_TIMEOUT=0.02)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
    return scope['_RequestTimeoutMiddleware']


class DelayedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.08)
        yield b'{"value":"native bytes"}'


async def exercise(stream, delayed_headers):
    requests = []
    row = SimpleNamespace(**dict.fromkeys(route._CONNECTION_FIELDS))
    row.id = 'global'; row.connection_revision = 'revision'
    async def provider(request):
        requests.append(request)
        if delayed_headers:
            await asyncio.sleep(0.08)
            return httpx.Response(200, content=b'data: {"ok":true}\n\ndata: [DONE]\n\n',
                                  headers={'content-type':'text/event-stream'})
        return httpx.Response(200, stream=DelayedBody(), headers={'content-type':'application/json'})
    app = FastAPI()
    app.add_middleware(timeout_middleware())
    app.include_router(route.setup_global_model_routes(), prefix='/api')
    @app.get('/ordinary-slow')
    async def ordinary():
        await asyncio.sleep(0.08)
        return {'ok':True}
    body = dict(expected_endpoint_id='global', expected_model='model',
                expected_connection_revision='revision', model='model',
                messages=[{'role':'user','content':'unchanged ñ'}], stream=stream)
    with patch.object(route, '_caller', return_value=('alice',True)), \
         patch.object(route, '_read_target', return_value=(row,'model')), \
         patch.object(route, '_runtime', return_value=('http://provider.invalid/v1/chat/completions',{})), \
         patch.object(route, '_client', side_effect=lambda:httpx.AsyncClient(transport=httpx.MockTransport(provider),trust_env=False)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://fixture.invalid') as client:
            response = await client.post('/api/global-model/v1/chat/completions',json=body)
            ordinary = await client.get('/ordinary-slow')
    assert len(requests) == 1  # no retry after either delayed header or body
    assert ordinary.status_code == 504  # the general deadline remains effective
    return response


def test_global_streaming_prefill_may_exceed_general_deadline():
    response = asyncio.run(exercise(stream=True, delayed_headers=True))
    assert response.status_code == 200
    assert response.content == b'data: {"ok":true}\n\ndata: [DONE]\n\n'


def test_global_nonstream_body_may_exceed_general_deadline():
    response = asyncio.run(exercise(stream=False, delayed_headers=False))
    assert response.status_code == 200
    assert response.content == b'{"value":"native bytes"}'
