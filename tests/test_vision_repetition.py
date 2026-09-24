"""A vision answer that falls into a loop is cut (live, 24-09-2026: 3,600+
tokens of repeats at ~6 tokens/s from a CPU vision model) and every vision
call carries a token cap."""
from src import document_processor as dp


def test_repeated_lines_keep_the_first_and_say_they_were_cut():
    text = "Line one\n\"Like as the waves\"\n[___] by the shore\n[___] by the shore\n[___] by the shore\n[___] by the shore"
    out = dp.collapse_vision_repetition(text)
    assert out.count("[___] by the shore") == 1
    assert "repetition was cut" in out
    assert out.startswith("Line one\n\"Like as the waves\"")


def test_a_chunk_repeated_inside_a_line_is_cut():
    text = "Transcription: You go to seek the " + "[___] " * 200
    out = dp.collapse_vision_repetition(text)
    assert len(out) < 200
    assert "repetition was cut" in out
    assert out.startswith("Transcription: You go to seek the")


def test_normal_answers_are_untouched():
    text = ("The circled objects are: a fort by the sea, a lighthouse, a ship.\n"
            "Row 1: 3 arrows left\nRow 2: 2 arrows up\n\n\nRow 3: 1 arrow right")
    assert dp.collapse_vision_repetition(text) == text
    assert dp.collapse_vision_repetition("") == ""
    # two identical lines are allowed (a real page can repeat a line once)
    assert dp.collapse_vision_repetition("a b c\na b c") == "a b c\na b c"


def test_vision_calls_carry_the_token_cap(monkeypatch):
    seen = {}

    def fake_llm_call(url, model, messages, headers=None, timeout=None, max_tokens=None, **kw):
        seen["max_tokens"] = max_tokens
        return "answer"

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": "vl"})
    monkeypatch.setattr(dp, "_resolve_vl_model", lambda configured, owner=None: ("http://vl.test/v1", "vl", {}))
    monkeypatch.setattr(dp, "llm_call", fake_llm_call)
    monkeypatch.setattr(dp, "_vision_max_tokens", lambda: 777)
    from src import endpoint_resolver
    monkeypatch.setattr(endpoint_resolver, "resolve_vision_fallback_candidates", lambda owner=None: [])
    from src import vision_cache
    monkeypatch.setattr(vision_cache, "get", lambda key: None)
    monkeypatch.setattr(vision_cache, "put", lambda *a, **k: None)
    out = dp.analyze_image_with_vl_prompt([(b"png-bytes", "image/png")], "what is circled?")
    assert out["text"] == "answer"
    assert seen["max_tokens"] == 777


def test_a_configured_but_unreachable_vision_model_is_not_reported_as_missing(monkeypatch, tmp_path):
    from src import document_processor as dp
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": "vl-local"})

    def boom(configured, owner=None):
        raise ValueError("model 'vl-local' not found on any reachable endpoint")
    monkeypatch.setattr(dp, "_resolve_vl_model", boom)
    out = dp.analyze_image_with_vl_result(str(img))
    assert out["model"] == "" and out.get("unavailable")
    assert "configured but not reachable" in out["text"] and "vl-local" in out["text"]
    assert "No vision model configured" not in out["text"]

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": ""})
    assert "No vision model configured" in dp.analyze_image_with_vl_result(str(img))["text"]
