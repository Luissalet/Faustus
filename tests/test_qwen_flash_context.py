from types import SimpleNamespace

from src import model_context


def test_flash_native_window_is_distinct_from_27b_family():
    assert model_context._lookup_known('qwen3.8-flash-next') == 262144
    assert model_context._lookup_known('RadixArk/Qwen3.8-Flash-Next-NVFP4') == 262144
    assert model_context._lookup_known('qwen3.8-27b') == 131072


def test_smaller_local_serving_window_overrides_flash_model_maximum(monkeypatch):
    monkeypatch.setattr(model_context, '_configured_endpoint_kind', lambda url: 'local')
    monkeypatch.setattr('src.llm_core._is_host_dead', lambda url: False)
    def get(url, **kwargs):
        return SimpleNamespace(is_success=url.endswith('/models'),
            json=lambda: {'data':[{'id':'qwen3.8-flash-next','max_model_len':32768}]})
    monkeypatch.setattr(model_context.httpx, 'get', get)
    assert model_context._query_context_length('http://spark4:30001/v1/chat/completions', 'qwen3.8-flash-next') == (32768, True)
