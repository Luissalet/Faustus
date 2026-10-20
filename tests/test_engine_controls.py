from src import engine_controls as controls
from src.reasoning_levels import levels_for
import pytest


@pytest.fixture(autouse=True)
def isolated_control_caches():
    reset_native_proofs()
    yield
    reset_native_proofs()


def test_controls_require_model_and_engine(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: False)
    assert controls.controls_for("http://configured/v1", "glm-5.3-flash-nvfp4") is None
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    assert controls.controls_for("http://configured/v1", "glm-4") is None
    assert controls.controls_for("http://configured/v1", "qwen3.8") is None
    result = levels_for("http://configured/v1", "nvidia/GLM-5.3-Flash-NVFP4")
    assert result["levels"] == ["low", "high", "max"]
    assert result["thinking_toggle"] is True


def test_fast_mode_uses_supported_low_effort_and_keeps_explicit_budget(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"chat_template_kwargs": {"enable_thinking": False},
               "reasoning_effort": "high", "reasoning_budget": 4096}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"] == 4096
    assert payload["reasoning_effort"] == "low"
    assert "reasoning_effort" not in payload["chat_template_kwargs"]
    assert payload["chat_template_kwargs"]["enable_thinking"] is True
    assert "reasoning_budget" not in payload
    payload["chat_template_kwargs"]["enable_thinking"] = True
    payload["reasoning_budget"] = 128
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"] == 128


def test_missing_budget_does_not_invent_a_cap_or_overwrite_native_budget(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload={}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert "thinking_token_budget" not in payload
    payload={"thinking_token_budget":128}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"]==128
    assert payload["chat_template_kwargs"]["clear_thinking"] is False


def test_unlimited_and_explicit_template_choice_survive(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload={"reasoning_budget":-1,"chat_template_kwargs":{"clear_thinking":False}}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"]==-1
    assert payload["chat_template_kwargs"]["clear_thinking"] is False


def test_fast_mode_without_explicit_budget_never_forces_empty_reasoning(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload={"reasoning_effort":"high","chat_template_kwargs":{"enable_thinking":False,"reasoning_effort":"high"}}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"]=="low"
    assert "reasoning_effort" not in payload["chat_template_kwargs"]
    assert "thinking_token_budget" not in payload


def test_unsupported_effort_never_silently_reaches_template(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"reasoning_effort": "xhigh"}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"] == "max"


def test_native_template_effort_is_fallback_but_top_level_wins(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"chat_template_kwargs": {"reasoning_effort": "low"}}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"] == "low"
    assert "reasoning_effort" not in payload["chat_template_kwargs"]
    payload = {"reasoning_effort": "high", "thinking_token_budget": 128,
               "reasoning_budget": 0, "chat_template_kwargs": {"reasoning_effort": "low"}}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"] == "high"
    assert payload["thinking_token_budget"] == 0
    assert "reasoning_effort" not in payload["chat_template_kwargs"]


def test_explicit_budget_argument_only_applies_to_verified_glm(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"chat_template_kwargs": {"enable_thinking": False}}
    controls.apply_controls(payload, "http://configured/v1", "qwen3.8", reasoning_budget=0)
    assert payload == {"chat_template_kwargs": {"enable_thinking": False}}
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: False)
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4", reasoning_budget=128)
    assert payload == {"chat_template_kwargs": {"enable_thinking": False}}


def native_document(path='/v1/chat/completions'):
    return {'paths': {path: {'post': {'requestBody': {'content': {'application/json': {
        'schema': {'$ref': '#/components/schemas/ChatCompletionRequest'}}}}}}},
        'components': {'schemas': {'ChatCompletionRequest': {'type': 'object', 'properties': {
            'thinking_token_budget': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
            'chat_template_kwargs': {'anyOf': [{'type': 'object', 'additionalProperties': True}, {'type': 'null'}]}}}}}}


def native_probe(monkeypatch, document):
    """Only transport is synthetic; discovery and typed parsing remain real."""
    import io, json
    calls = []
    class Opener:
        def open(self, url, timeout):
            calls.append(url)
            if isinstance(document[0], Exception):
                raise document[0]
            return io.BytesIO(json.dumps(document[0]).encode())
    monkeypatch.setattr(controls.urllib.request, 'build_opener', lambda *args: Opener())
    return calls


def reset_native_proofs():
    controls._cache.clear()
    controls._schema_cache.clear()
    getattr(controls, '_direct_history_cache', {}).clear()


def test_direct_native_probe_establishes_history_without_managed_sync(monkeypatch):
    reset_native_proofs()
    base, model = 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4'
    calls = native_probe(monkeypatch, [native_document()])
    assert not controls.interleaved_history_capability(base, model)['interleaved_history']
    payload = {}
    controls.apply_controls(payload, base, model)
    assert controls.interleaved_history_capability(base, model)['interleaved_history']
    assert payload['chat_template_kwargs']['clear_thinking'] is False
    assert len(calls) == 1
    controls.apply_controls({}, base+'/chat/completions', model)
    assert len(calls) == 1
    assert not controls.interleaved_history_capability(base, 'another-model')['interleaved_history']
    assert not controls.interleaved_history_capability('http://other.invalid/v1', model)['interleaved_history']
    assert not controls.json_schema_capability(base, 'not-managed')['json_schema']


def test_direct_explicit_clear_choices_survive_real_probe(monkeypatch):
    reset_native_proofs()
    native_probe(monkeypatch, [native_document()])
    for clear in (True, False):
        payload = {'chat_template_kwargs': {'clear_thinking': clear}}
        controls.apply_controls(payload, 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4')
        assert payload['chat_template_kwargs']['clear_thinking'] is clear


@pytest.mark.parametrize('bad', ['type', 'missing', 'different_route'])
def test_direct_history_needs_typed_actual_route(monkeypatch, bad):
    document = native_document('/elsewhere/chat/completions' if bad == 'different_route' else '/v1/chat/completions')
    fields = document['components']['schemas']['ChatCompletionRequest']['properties']
    if bad == 'type': fields['thinking_token_budget'] = {'type': 'string'}
    if bad == 'missing': fields.pop('chat_template_kwargs')
    native_probe(monkeypatch, [document])
    controls.apply_controls({}, 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4')
    assert not controls.interleaved_history_capability('http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4')['interleaved_history']


def test_custom_api_path_cannot_borrow_root_schema(monkeypatch):
    native_probe(monkeypatch, [native_document()])
    controls.apply_controls({}, 'http://fixture.invalid/custom/v1', 'glm-5.3-flash-nvfp4')
    assert not controls.interleaved_history_capability('http://fixture.invalid/custom/v1', 'glm-5.3-flash-nvfp4')['interleaved_history']


def test_direct_history_survives_expiry_and_observation_failure_then_withdraws(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(controls.time, 'monotonic', lambda: clock[0])
    document = [native_document()]
    calls = native_probe(monkeypatch, document)
    base, model = 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4'
    controls.apply_controls({}, base, model)
    clock[0] += 61
    assert controls.interleaved_history_capability(base, model)['interleaved_history']
    assert len(calls) == 1
    document[0] = OSError('unavailable')
    controls.apply_controls({}, base, model)
    assert controls.interleaved_history_capability(base, model)['interleaved_history']
    clock[0] += 61; document[0] = native_document()
    document[0]['components']['schemas']['ChatCompletionRequest']['properties'].pop('thinking_token_budget')
    controls.apply_controls({}, base, model)
    assert not controls.interleaved_history_capability(base, model)['interleaved_history']
    assert len(calls) == 3


@pytest.mark.parametrize('change', ['retire', 'identity', 'models', 'negative'])
def test_managed_known_changes_invalidate_direct_proof(monkeypatch, change):
    document = native_document(); native_probe(monkeypatch, [document])
    base, model = 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4'
    rows = [{'endpoint_id': 'managed-a', 'base_url': base, 'models': [model]}]
    controls.refresh_json_schema(rows, fetch_openapi=lambda _: document)
    controls.apply_controls({}, base, model)
    stamp = next(iter(controls._schema_cache.values()))['at']
    monkeypatch.setattr(controls.time, 'monotonic', lambda: stamp+61)
    if change == 'retire': rows = []
    if change == 'identity': rows[0]['endpoint_id'] = 'managed-b'
    if change == 'models': rows[0]['models'] = ['another-model']
    if change == 'negative': document['components']['schemas']['ChatCompletionRequest']['properties'].pop('thinking_token_budget')
    def observe(_):
        if change in ('identity', 'models'): raise OSError('new identity has no proof')
        return document
    controls.refresh_json_schema(rows, fetch_openapi=observe)
    assert not controls.interleaved_history_capability(base, model)['interleaved_history']


def test_managed_empty_refresh_preserves_never_managed_direct_route(monkeypatch):
    native_probe(monkeypatch, [native_document()])
    base, model = 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4'
    controls.apply_controls({}, base, model)
    controls.refresh_json_schema([], fetch_openapi=lambda _: pytest.fail('no active endpoint'))
    assert controls.interleaved_history_capability(base, model)['interleaved_history']


def test_direct_valid_negative_retires_older_managed_history(monkeypatch):
    document = [native_document()]; native_probe(monkeypatch, document)
    base, model = 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4'
    controls.refresh_json_schema([{'endpoint_id': 'managed-a', 'base_url': base, 'models': [model]}],
                                fetch_openapi=lambda _: document[0])
    controls.apply_controls({}, base, model)
    stamp = controls._cache[base[:-3]][0]
    monkeypatch.setattr(controls.time, 'monotonic', lambda: stamp+61)
    document[0]['components']['schemas']['ChatCompletionRequest']['properties'].pop('thinking_token_budget')
    controls.apply_controls({}, base, model)
    assert not controls.interleaved_history_capability(base, model)['interleaved_history']


def test_direct_metadata_is_bounded_and_does_not_discover_other_models(monkeypatch):
    calls = native_probe(monkeypatch, [native_document()])
    payload = {}; controls.apply_controls(payload, 'http://fixture.invalid/v1', 'qwen3.8')
    assert payload == {} and calls == []
    for n in range(controls._SCHEMA_MAX_ENDPOINTS+1):
        controls.apply_controls({}, f'http://fixture-{n}.invalid/v1', 'glm-5.3-flash-nvfp4')
    assert len(controls._cache) <= controls._SCHEMA_MAX_ENDPOINTS
    assert len(controls._direct_history_cache) <= controls._SCHEMA_MAX_ENDPOINTS
    assert not controls.interleaved_history_capability('http://fixture-0.invalid/v1', 'glm-5.3-flash-nvfp4')['interleaved_history']


def test_direct_malformed_document_withdraws_prior_proof(monkeypatch):
    document = [native_document()]; native_probe(monkeypatch, document)
    base, model = 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4'
    controls.apply_controls({}, base, model)
    stamp = controls._cache[base[:-3]][0]
    monkeypatch.setattr(controls.time, 'monotonic', lambda: stamp+61)
    document[0] = []
    controls.apply_controls({}, base, model)
    assert not controls.interleaved_history_capability(base, model)['interleaved_history']


def test_custom_actual_route_is_supported_but_not_shared_with_root(monkeypatch):
    native_probe(monkeypatch, [native_document('/custom/v1/chat/completions')])
    base, model = 'http://fixture.invalid/custom/v1', 'glm-5.3-flash-nvfp4'
    controls.apply_controls({}, base, model)
    assert controls.interleaved_history_capability(base, model)['interleaved_history']
    assert not controls.interleaved_history_capability('http://fixture.invalid/v1', model)['interleaved_history']


def test_history_never_probes_and_unavailable_cannot_create_proof(monkeypatch):
    calls = native_probe(monkeypatch, [OSError('unavailable')])
    base, model = 'http://fixture.invalid/v1', 'glm-5.3-flash-nvfp4'
    payload = {}; controls.apply_controls(payload, base, model)
    assert payload == {} and len(calls) == 1
    monkeypatch.setattr(controls.urllib.request, 'build_opener', lambda *args: pytest.fail('history must not probe'))
    assert not controls.interleaved_history_capability(base, model)['interleaved_history']
