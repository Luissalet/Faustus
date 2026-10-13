"""Completed uploads are not active work; preserve the actual per-minute limiter."""
import asyncio
import io
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile
from routes import upload_routes
from src.upload_handler import UploadHandler

def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'false')
    monkeypatch.setattr(upload_routes.time, 'time', lambda: 5000.0)
    handler = UploadHandler(str(tmp_path), str(tmp_path / 'uploads'))
    router, _ = upload_routes.setup_upload_routes(handler)
    endpoint = next(route.endpoint for route in router.routes if route.endpoint.__name__ == 'api_upload')
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),
        state=SimpleNamespace(current_user=None, api_token=False, api_token_owner=None))
    def send(content, index):
        file = UploadFile(io.BytesIO(content), filename=f'file{index}.txt', headers=Headers({'content-type': 'text/plain'}))
        return asyncio.run(endpoint(request, [file], None))
    return handler, send

@pytest.mark.parametrize('duplicate', [False, True])
def test_four_completed_uploads_in_the_same_second_are_not_concurrent(tmp_path, monkeypatch, duplicate):
    handler, send = setup(tmp_path, monkeypatch)
    results = [send(b'same private file' if duplicate else f'private file {index}'.encode(), index) for index in range(4)]
    assert all(result['files'][0]['status'] == 'ready' for result in results)
    assert len(handler.upload_rate_log['127.0.0.1']) == 4
    if duplicate:
        assert len({result['files'][0]['id'] for result in results}) == 1

def test_actual_per_minute_limit_still_applies(tmp_path, monkeypatch):
    handler, send = setup(tmp_path, monkeypatch)
    handler.upload_rate_limit = 2
    send(b'private one', 1)
    send(b'private two', 2)
    with pytest.raises(HTTPException) as error:
        send(b'private three', 3)
    assert error.value.status_code == 429
    assert 'Upload rate limit' in error.value.detail
