"""Vision capability of LAN OpenAI-compatible servers is asked, not guessed."""
import io
import urllib.error

from src import chat_helpers as ch


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reset():
    ch._openai_vision_cache.clear()


def test_image_accepted_means_vision(monkeypatch):
    _reset()
    seen = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: seen.append(req.full_url) or _Resp())
    assert ch.openai_compat_supports_vision("http://192.168.0.236:8002", "glm-5.3-flash-nvfp4") is True
    assert seen == ["http://192.168.0.236:8002/v1/chat/completions"]
    assert ch.openai_compat_supports_vision("http://192.168.0.236:8002/v1", "glm-5.3-flash-nvfp4") is True
    assert len(seen) == 1  # cached


def test_multimodal_rejection_means_text_only(monkeypatch):
    _reset()
    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"error": "model is not a multimodal model"}'))
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert ch.openai_compat_supports_vision("http://10.0.0.5:8000/v1", "text-model") is False


def test_unreachable_or_public_is_unknown(monkeypatch):
    _reset()
    def down(req, timeout=0):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", down)
    assert ch.openai_compat_supports_vision("http://10.0.0.5:8000/v1", "m") is None
    assert ch.openai_compat_supports_vision("https://api.example.com/v1", "m") is None