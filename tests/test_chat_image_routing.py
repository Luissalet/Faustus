import sys
for mod_name in ["src.endpoint_resolver", "src.database", "core.database"]:
    _mod = sys.modules.get(mod_name)
    if _mod is not None and not getattr(_mod, "__file__", None):
        sys.modules.pop(mod_name, None)

import json
from types import SimpleNamespace

from tests.helpers.import_state import clear_fake_endpoint_resolver_modules

clear_fake_endpoint_resolver_modules("routes.chat_routes")

from routes import chat_routes


def test_pending_image_event_preserves_recovery_identity_and_uncertainty():
    from src.prospero_images import _unknown
    result = _unknown('existing-request', 'still pending', job_id='job-1', state='pending')
    event = chat_routes._image_result_event(result, 'edit_image', 'put on a hat')
    # The same event is streamed and stored in tool_events for later recovery.
    restored = json.loads(json.dumps(event))
    assert restored['request_id'] == 'existing-request'
    assert restored['prospero_job_id'] == 'job-1'
    assert restored['result_status'] == restored['status'] == 'outcome_unknown'
    assert restored['uncertainty']['reconcile_action']
    assert restored['exit_code'] == 1 and 'image_url' not in restored


def test_successful_image_event_preserves_image_and_remote_receipt():
    result = {'image_url': '/api/generated-image/own.png', 'image_id': 'own',
              'request_id': 'req', 'prospero_asset_id': 'asset', 'results': 'done',
              'private_internal_field': 'not a public event'}
    event = chat_routes._image_result_event(result, 'generate_image', 'draw')
    assert event['exit_code'] == 0 and event['image_url'] == result['image_url']
    assert event['request_id'] == 'req' and event['prospero_asset_id'] == 'asset'
    assert 'private_internal_field' not in event


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *conditions):
        return self

    def all(self):
        return list(self.rows)


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def query(self, model):
        return _FakeQuery(self.rows)

    def close(self):
        self.closed = True


def _session(model="qwen3.5:latest", endpoint_url="http://localhost:11434/v1/chat/completions"):
    return SimpleNamespace(model=model, endpoint_url=endpoint_url)


def _endpoint(base_url, model_type="image", models=None):
    cached_models = None if models is None else json.dumps(models)
    return SimpleNamespace(
        base_url=base_url,
        model_type=model_type,
        is_enabled=True,
        cached_models=cached_models,
    )


def test_image_model_prefix_routes_to_image_generation_without_endpoint_lookup(monkeypatch):
    def fail_if_called():
        raise AssertionError("prefixed image models should not need a DB lookup")

    monkeypatch.setattr(chat_routes, "SessionLocal", fail_if_called)

    assert chat_routes._is_image_generation_session(_session(model="dall-e-3"))


def test_namespaced_gpt_image_model_routes_to_image_generation_without_endpoint_lookup(monkeypatch):
    def fail_if_called():
        raise AssertionError("provider-prefixed image models should not need a DB lookup")

    monkeypatch.setattr(chat_routes, "SessionLocal", fail_if_called)

    assert chat_routes._is_image_generation_session(_session(model="openai/gpt-5-image"))


def test_image_endpoint_does_not_catch_text_model_on_different_path(monkeypatch):
    db = _FakeDb([
        _endpoint("http://localhost:11434/v1/images", models=["sdxl-local"]),
    ])
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: db)

    assert not chat_routes._is_image_generation_session(_session())
    assert db.closed


def test_image_endpoint_cache_must_contain_selected_model(monkeypatch):
    db = _FakeDb([
        _endpoint("http://localhost:11434/v1", models=["sdxl-local"]),
    ])
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: db)

    assert not chat_routes._is_image_generation_session(_session(model="qwen3.5:latest"))


def test_matching_image_endpoint_routes_selected_image_model(monkeypatch):
    db = _FakeDb([
        _endpoint("http://localhost:11434/v1", models=["sdxl-local"]),
    ])
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: db)

    assert chat_routes._is_image_generation_session(_session(model="sdxl-local"))
