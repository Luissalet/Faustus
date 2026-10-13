"""No-login uploads use the same existing storage identity as canonical chat."""
import asyncio
import io
import os
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers, UploadFile
from src.owner_identity import DEFAULT_LOCAL_OWNER
from src.upload_handler import UploadHandler
from routes.upload_routes import setup_upload_routes


@pytest.mark.parametrize('auth,user,token_owner,expected', [
    ('false', None, None, DEFAULT_LOCAL_OWNER),
    ('true', 'alice', None, 'alice'),
    ('true', 'api', 'alice', 'alice'),
])
def test_uploaded_file_is_resolvable_by_the_canonical_storage_owner(tmp_path, monkeypatch, auth, user, token_owner, expected):
    monkeypatch.setenv('AUTH_ENABLED', auth)
    handler = UploadHandler(str(tmp_path), str(tmp_path / 'uploads'))
    router, _ = setup_upload_routes(handler)
    endpoints = {route.endpoint.__name__: route.endpoint for route in router.routes}
    request = SimpleNamespace(
        state=SimpleNamespace(current_user=user, api_token=bool(token_owner), api_token_owner=token_owner),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=SimpleNamespace(is_configured=True, is_admin=lambda _owner: False))),
        client=SimpleNamespace(host='127.0.0.1'),
    )
    content = b'period,units\n2026,7\n'
    upload = UploadFile(io.BytesIO(content), filename='report.csv', headers=Headers({'content-type': 'text/csv'}))
    result = asyncio.run(endpoints['api_upload'](request, [upload], None))
    file_id = result['files'][0]['id']
    resolved = handler.resolve_upload(file_id, owner=expected)
    assert resolved is not None
    assert resolved['owner'] == expected
    with open(resolved['path'], 'rb') as saved:
        assert saved.read() == content
    response = asyncio.run(endpoints['download_file'](request, file_id, 0))
    assert os.path.normcase(str(response.path)) == os.path.normcase(resolved['path'])


def test_local_chat_reads_legacy_ownerless_upload_without_reassigning_it(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'false')
    handler = UploadHandler(str(tmp_path), str(tmp_path / 'uploads'))
    content = b'original legacy file'
    upload = UploadFile(io.BytesIO(content), filename='legacy.txt')
    legacy = handler.save_upload(upload, '127.0.0.1', owner=None)
    resolved = handler.resolve_upload(legacy['id'], owner=DEFAULT_LOCAL_OWNER)
    assert resolved is not None and resolved['owner'] is None
    assert resolved['id'] == legacy['id']
    assert all(row.get('owner') is None for row in handler._load_upload_index().values())
    with open(resolved['path'], 'rb') as saved:
        assert saved.read() == content
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    assert handler.resolve_upload(legacy['id'], owner=DEFAULT_LOCAL_OWNER) is None
