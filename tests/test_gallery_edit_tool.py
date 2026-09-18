import base64
import io
import json

import httpx
import pytest
from PIL import Image

from src.tools import image as tool


@pytest.fixture
def png():
    output = io.BytesIO()
    Image.new('RGBA', (8, 8), 'red').save(output, format='PNG')
    return output.getvalue()


@pytest.fixture
def service(monkeypatch, png):
    calls = []
    monkeypatch.setattr(tool, '_owned_image', lambda image_id, owner: (png, (8, 8)))
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            data = {'id':'edited-id', 'filename':'edited.png'} if url.endswith('/upload') else {'image':base64.b64encode(png).decode()}
            return httpx.Response(200, json=data, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize('action,path', [('upscale','upscale-local'), ('rembg','remove-bg'), ('inpaint','inpaint'), ('harmonize','harmonize')])
async def test_routes_owner_and_confirmed_gallery_result(service, action, path):
    result = await tool.do_edit_image(json.dumps({'image_id':'source', 'action':action, 'mask_id':'mask', 'prompt':'red'}), owner='alice')
    assert result['exit_code'] == 0, result
    assert service[0][0].endswith('/api/image/' + path)
    assert service[1][0].endswith('/api/gallery/upload')
    assert all(call[1]['headers']['X-Odysseus-Owner'] == 'alice' for call in service)
    assert result['image_id'] == 'edited-id'
    assert result['source_image_id'] == 'source'
    assert result['image_url'] == '/api/generated-image/edited.png'


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [
    {'action':'../../evil'}, {'action':'upscale','scale':3}, {'action':'inpaint'},
    {'action':'harmonize','strength':float('nan')}, {'action':'harmonize','strength':True},
])
async def test_invalid_edits_never_dispatch(service, args):
    result = await tool.do_edit_image(json.dumps({'image_id':'source', **args}), owner='alice')
    assert result['exit_code'] == 1
    assert service == []


@pytest.mark.asyncio
async def test_mask_dimensions_must_match(service, monkeypatch, png):
    monkeypatch.setattr(tool, '_owned_image', lambda image_id, owner: (png, (4, 4) if image_id == 'mask' else (8, 8)))
    result = await tool.do_edit_image('{"image_id":"source","action":"inpaint","mask_id":"mask","prompt":"red"}', owner='alice')
    assert 'dimensions' in result['error']
    assert service == []


def test_owner_is_checked_before_reading_files(monkeypatch, tmp_path, png):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database
    import src.constants
    engine = create_engine('sqlite://')
    database.GalleryImage.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, 'SessionLocal', sessions)
    monkeypatch.setattr(src.constants, 'GENERATED_IMAGES_DIR', str(tmp_path))
    with sessions() as db:
        db.add(database.GalleryImage(id='private', filename='private.png', owner='bob', is_active=True))
        db.commit()
    with pytest.raises(ValueError, match='owner'):
        tool._owned_image('private', 'alice')
    (tmp_path / 'private.png').write_bytes(png)
    assert tool._owned_image('private', 'bob')[1] == (8, 8)
    with sessions() as db:
        db.query(database.GalleryImage).filter_by(id='private').update({'filename':'../outside.png'})
        db.commit()
    with pytest.raises(ValueError, match='unavailable'):
        tool._owned_image('private', 'bob')
    engine.dispose()


def test_invalid_image_is_not_saved():
    with pytest.raises(Exception):
        tool._png(b'<html>not an image</html>')


@pytest.mark.asyncio
async def test_service_failure_is_not_reported_as_an_edit(service, monkeypatch):
    async def fail(self, url, **kwargs):
        service.append(url)
        return httpx.Response(503, json={'detail':'unavailable'}, request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', fail)
    result = await tool.do_edit_image('{"image_id":"source","action":"rembg"}', owner='alice')
    assert result['exit_code'] == 1
    assert '503' in result['error']
    assert len(service) == 1


@pytest.mark.asyncio
async def test_missing_owner_never_dispatches(service, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    result = await tool.do_edit_image('{"image_id":"source","action":"rembg"}')
    assert result['exit_code'] == 1
    assert service == []
