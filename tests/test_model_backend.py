"""tests/test_model_backend.py — src/model_backend.py: who serves a model
endpoint (Ollama / llama.cpp / a remote API), the "who serves it" switch
task's shared detection helper."""
import pytest

from src import model_backend as mb


@pytest.fixture(autouse=True)
def _clear_cache():
    mb.clear_cache()
    yield
    mb.clear_cache()


def test_is_ollama_url():
    assert mb.is_ollama_url("http://127.0.0.1:11434/v1") is True
    assert mb.is_ollama_url("http://localhost:11434/api/chat") is True
    assert mb.is_ollama_url("http://my-ollama-box:8080/v1") is True
    assert mb.is_ollama_url("http://127.0.0.1:8081/v1") is False
    assert mb.is_ollama_url("https://api.openai.com/v1") is False


def test_serving_backend_ollama_by_url(monkeypatch):
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: None)
    out = mb.serving_backend("http://127.0.0.1:11434/v1", probe=False)
    assert out == {"backend": "ollama", "label": "Ollama"}


def test_serving_backend_managed_llamacpp_engine(monkeypatch):
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: {"id": "eng-1"})
    out = mb.serving_backend("http://127.0.0.1:8081/v1", probe=False)
    assert out == {"backend": "llamacpp", "label": "llama.cpp (llama-server)"}


def test_serving_backend_explicit_remote_kind(monkeypatch):
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: None)
    out = mb.serving_backend("https://my-proxy.internal/v1", endpoint_kind="proxy", probe=False)
    assert out == {"backend": "remote", "label": "Remote API"}


def test_serving_backend_public_host_is_remote(monkeypatch):
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: None)
    out = mb.serving_backend("https://api.openai.com/v1", probe=False)
    assert out == {"backend": "remote", "label": "Remote API"}


def test_serving_backend_local_without_probe_is_unknown(monkeypatch):
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: None)
    out = mb.serving_backend("http://127.0.0.1:8090/v1", probe=False)
    assert out == {"backend": "unknown", "label": "Unknown"}


def test_serving_backend_probes_unmanaged_llamacpp(monkeypatch):
    """An unlabelled local endpoint that is not a managed engine (started
    outside Faustus, or discovered) is disambiguated by probing: Ollama's
    `/api/version` first, then llama-server's `/props`."""
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: None)

    calls = []

    class Resp:
        def __init__(self, status_code):
            self.status_code = status_code

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            calls.append(url)
            if url.endswith("/api/version"):
                return Resp(404)
            if url.endswith("/props"):
                return Resp(200)
            return Resp(404)

    import httpx
    monkeypatch.setattr(httpx, "Client", Client)

    out = mb.serving_backend("http://127.0.0.1:8090/v1", probe=True)
    assert out == {"backend": "llamacpp", "label": "llama.cpp (llama-server)"}
    assert calls[0].endswith("/api/version")
    assert calls[1].endswith("/props")

    # Cached: a second call within the TTL does not probe again.
    calls.clear()
    out2 = mb.serving_backend("http://127.0.0.1:8090/v1", probe=True)
    assert out2 == out
    assert calls == []


def test_serving_backend_probes_unlabelled_ollama(monkeypatch):
    """An unlabelled local endpoint whose port is not 11434 but that answers
    the Ollama version probe is still detected as Ollama."""
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: None)

    class Resp:
        def __init__(self, status_code):
            self.status_code = status_code

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return Resp(200) if url.endswith("/api/version") else Resp(404)

    import httpx
    monkeypatch.setattr(httpx, "Client", Client)

    out = mb.serving_backend("http://127.0.0.1:8090/v1", probe=True)
    assert out == {"backend": "ollama", "label": "Ollama"}


def test_serving_backend_down_local_endpoint_is_unknown(monkeypatch):
    monkeypatch.setattr("src.engine_swap.engine_for_url", lambda url: None)

    class Resp:
        status_code = 404

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", Client)

    out = mb.serving_backend("http://127.0.0.1:8090/v1", probe=True)
    assert out == {"backend": "unknown", "label": "Unknown"}
