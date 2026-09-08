import json
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from core.middleware import require_admin
from routes.local_video_routes import setup_local_video_routes


def client(admin=True):
    app = FastAPI()
    app.include_router(setup_local_video_routes())
    def access():
        if not admin:
            raise HTTPException(403, 'Admin required')
    app.dependency_overrides[require_admin] = access
    return TestClient(app)


def test_local_video_admin_gate():
    assert client(False).get('/api/media/local-video/capabilities').status_code == 403


def test_local_video_bad_inputs_never_process(monkeypatch):
    def unexpected(*args):
        raise AssertionError('Invalid input reached worker')
    monkeypatch.setattr('routes.local_video_routes._process', unexpected)
    with client() as api:
        for filename, language, segments in [('x.html', 'en', '[]'), ('x.mp4', 'auto', '[]'), ('x.mp4', 'en', '[{}]')]:
            response = api.post('/api/media/local-video/dub', files={'file': (filename, b'video')}, data={'language': language, 'segments': segments})
            assert response.status_code == 400


def test_local_video_dub_download(monkeypatch):
    calls = []
    def render(data, suffix, payload):
        calls.append((data, suffix, payload))
        return b'local-video-result'
    monkeypatch.setattr('routes.local_video_routes._process', render)
    with client() as api:
        result = api.post('/api/media/local-video/dub', files={'file': ('x.mp4', b'video')}, data={'language': 'es', 'segments': json.dumps([{'start': 0, 'end': 2, 'text': 'Hola'}])})
    assert result.status_code == 200
    assert result.content == b'local-video-result'
    assert result.headers['cache-control'] == 'no-store'
    assert calls[0][2]['language'] == 'es'
