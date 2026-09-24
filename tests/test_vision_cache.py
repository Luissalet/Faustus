"""Vision answers are cached by image bytes, prompt and model.

Live: a CPU-only vision model described the same scanned page again and
again, minutes each time.
"""
import src.document_processor as dp
import src.vision_cache as vc


def _setup(monkeypatch, tmp_path, calls, answer="a circle at the top left"):
    monkeypatch.setattr(vc, "_cache_dir", lambda: str(tmp_path / "vc"))
    monkeypatch.setattr(dp, "_resolve_vl_model", lambda m, owner=None: ("http://x/v1", "vl-model", {}))
    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": "vl-model"})
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_vision_fallback_candidates", lambda owner=None: [])
    import src.privacy_policy as pp
    monkeypatch.setattr(pp, "assert_outbound", lambda *a, **k: None)

    def fake_llm_call(url, model, messages, headers=None, timeout=None, **kw):
        calls.append(model)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(dp, "llm_call", fake_llm_call)


def test_the_second_identical_question_is_served_from_the_cache(monkeypatch, tmp_path):
    calls = []
    _setup(monkeypatch, tmp_path, calls)
    img = [(b"\x89PNG-bytes-1", "image/png")]
    a = dp.analyze_image_with_vl_prompt(img, "what is circled?")
    b = dp.analyze_image_with_vl_prompt(img, "what is circled?")
    assert a["text"] == b["text"] == "a circle at the top left"
    assert calls == ["vl-model"]
    # another question or another image is a new call
    dp.analyze_image_with_vl_prompt(img, "and the bottom?")
    dp.analyze_image_with_vl_prompt([(b"\x89PNG-bytes-2", "image/png")], "what is circled?")
    assert len(calls) == 3


def test_failures_are_not_cached(monkeypatch, tmp_path):
    calls = []
    _setup(monkeypatch, tmp_path, calls, answer=TimeoutError("slow"))
    img = [(b"img", "image/png")]
    dp.analyze_image_with_vl_prompt(img, "q")
    dp.analyze_image_with_vl_prompt(img, "q")
    assert len(calls) == 2


def test_the_cache_can_be_turned_off(monkeypatch, tmp_path):
    calls = []
    _setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(vc, "enabled", lambda: False)
    img = [(b"img", "image/png")]
    dp.analyze_image_with_vl_prompt(img, "q")
    dp.analyze_image_with_vl_prompt(img, "q")
    assert len(calls) == 2
