"""Shared image entrypoints select the user's executor without running a model."""
import sys
from types import SimpleNamespace

import pytest

from src import ai_interaction, settings


@pytest.fixture
def delegated(monkeypatch):
    calls = []
    async def run_image(*args, **kwargs):
        calls.append((args, kwargs))
        return {'image_url': '/api/generated-image/confirmed.png', 'image_id': 'confirmed', 'results': 'done'}
    monkeypatch.setitem(sys.modules, 'src.prospero_images', SimpleNamespace(run_image=run_image))
    def setting(key, owner='', default=None):
        assert owner == 'alice'
        return 'prospero' if key == 'image_execution_backend' else default
    monkeypatch.setattr(settings, 'get_user_setting', setting)
    monkeypatch.setattr(ai_interaction, '_resolve_model', lambda *a, **k: pytest.fail('No parallel image model execution'))
    return calls


@pytest.mark.asyncio
async def test_generation_delegates_with_session_owner_and_request_id(delegated):
    result = await ai_interaction.do_generate_image('a landscape\nignored-model', 'session', 'alice', request_id='turn-1')
    assert result['image_id'] == 'confirmed'
    assert delegated == [(('a landscape', 'session', 'alice'), {'request_id': 'turn-1'})]


@pytest.mark.asyncio
async def test_uploaded_edit_delegates_original_file_and_instruction(delegated, tmp_path):
    path = tmp_path / 'source.png'
    path.write_bytes(b'file-validation-belongs-to-adapter')
    result = await ai_interaction.do_edit_image('add a hat', str(path), session_id='session', owner='alice', request_id='turn-2')
    assert result['image_id'] == 'confirmed'
    assert delegated == [(('add a hat', 'session', 'alice'), {'image_path': str(path), 'request_id': 'turn-2'})]


@pytest.mark.asyncio
async def test_missing_prompt_or_input_never_delegates(delegated, tmp_path):
    assert 'error' in await ai_interaction.do_generate_image('', 'session', 'alice')
    assert 'error' in await ai_interaction.do_edit_image('add a hat', str(tmp_path / 'missing'), session_id='session', owner='alice')
    assert delegated == []
