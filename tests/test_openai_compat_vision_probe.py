"""Vision capability of LAN OpenAI-compatible servers is asked, not guessed.

#1101: on servers with /tokenize (vLLM, the Sparks) the question costs no generation:
the same message is tokenized with and without the probe image. Servers without it keep
the 1-token completion probe.
"""
import io
import json
import urllib.error

from src import chat_helpers as ch


class _Resp:
    status = 200

    def __init__(self, body=b"{}"):
        self._body = body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reset():
    ch._openai_vision_cache.clear()


def _has_image(req):
    return b"image_url" in (req.data or b"")


def _http_error(req, code, body):
    return urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body))


def test_tokenize_answers_vision_without_any_completion(monkeypatch):
    _reset()
    seen = []

    def vllm(req, timeout=0):
        seen.append(req.full_url)
        if req.full_url.endswith("/tokenize"):
            return _Resp(json.dumps({"count": 31 if _has_image(req) else 13}).encode())
        raise AssertionError("no completion may be sent when /tokenize answers")

    monkeypatch.setattr("urllib.request.urlopen", vllm)
    assert ch.openai_compat_supports_vision("http://192.168.0.236:8002/v1", "glm-5.3-flash-nvfp4") is True
    assert seen and all(u == "http://192.168.0.236:8002/tokenize" for u in seen)
    assert ch.openai_compat_supports_vision("http://192.168.0.236:8002", "glm-5.3-flash-nvfp4") is True
    assert len(seen) == 2  # cached


def test_tokenize_rejecting_the_image_means_text_only(monkeypatch):
    _reset()

    def text_only(req, timeout=0):
        if req.full_url.endswith("/tokenize") and _has_image(req):
            raise _http_error(req, 500, b'{"error": "Resolving modality {\'image\'} requires a multimodal processor but none is available."}')
        if req.full_url.endswith("/tokenize"):
            return _Resp(b'{"count": 13}')
        raise AssertionError("no completion may be sent when /tokenize answers")

    monkeypatch.setattr("urllib.request.urlopen", text_only)
    assert ch.openai_compat_supports_vision("http://10.0.0.5:8000/v1", "text-model") is False


def test_without_tokenize_the_completion_probe_still_decides(monkeypatch):
    _reset()
    seen = []

    def other_server(req, timeout=0):
        seen.append(req.full_url)
        if req.full_url.endswith("/tokenize"):
            raise _http_error(req, 404, b'{"detail": "Not Found"}')
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", other_server)
    assert ch.openai_compat_supports_vision("http://192.168.0.7:30001/v1", "qwen") is True
    assert seen[-1] == "http://192.168.0.7:30001/v1/chat/completions"


def test_image_ignored_by_tokenize_falls_back_to_completion(monkeypatch):
    _reset()
    seen = []

    def ignores_images(req, timeout=0):
        seen.append(req.full_url)
        if req.full_url.endswith("/tokenize"):
            return _Resp(b'{"count": 13}')
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", ignores_images)
    assert ch.openai_compat_supports_vision("http://10.0.0.6:8000/v1", "m") is True
    assert seen[-1] == "http://10.0.0.6:8000/v1/chat/completions"


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
