"""Word timings and visemes of synthesized speech, and the TTS API that returns them (Agora #251).

Audio comes from `tests/fake_tts_marks.py`, a fake engine whose true word times
are known, so the estimate can be measured instead of only checked for shape.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import struct
import sys
import time
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.tts_routes import setup_tts_routes
from services.tts import marks as M
from services.tts.command_voice import synthesize_command_detailed
from services.tts.tts_service import TTSService

FAKE = Path(__file__).with_name("fake_tts_marks.py")
_spec = importlib.util.spec_from_file_location("fake_tts_marks", FAKE)
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)

ES = "Hola, ¿qué tal estás? Me llamo Faustus y hablo contigo."
EN = "Hello there, I am Faustus. I can read your notes aloud while you work on something else."
SAMPLES = [
    ("es", ES),
    ("es", "Esta mañana he revisado el informe de ventas y todo parece estar en orden, pero falta un dato."),
    ("es", "Voy a abrir la carpeta del proyecto. Después ejecutaré las pruebas y te cuento el resultado."),
    ("en", EN),
    ("en", "The build finished without errors, although two tests were skipped because the network was down."),
    ("es", "Sí."),
    ("es", "Un resumen de tres puntos: primero, el presupuesto; segundo, los plazos; tercero, los riesgos."),
]


def wav(samples, rate=fake.RATE, channels=1, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 32767)) * channels for v in samples))
        else:
            w.writeframes(b"".join(struct.pack("B", int(128 + 127 * max(-1, min(1, v)))) * channels for v in samples))
    return buf.getvalue()


def spoken(text, lang="es"):
    samples, truth = fake.render(text, lang)
    return wav(samples), truth


def check_invariants(m, text):
    """The promises of the marks, whatever the audio."""
    words = M.tokenize(text)
    assert [w["text"] for w in m["words"]] == [w["text"] for w in words]
    prev_end = 0
    for i, w in enumerate(m["words"]):
        assert w["i"] == i
        assert 0 <= w["start_ms"] <= w["end_ms"] <= m["duration_ms"]
        assert w["start_ms"] >= prev_end, "words overlap or go back in time"
        prev_end = w["end_ms"]
        assert text.encode("utf-16-le")[2 * w["start_char"]:2 * w["end_char"]].decode("utf-16-le") == w["text"]
    prev_end = 0
    for v in m["visemes"]:
        assert v["v"] in M.VISEMES
        assert 0 <= v["start_ms"] < v["end_ms"] <= m["duration_ms"]
        assert v["start_ms"] >= prev_end
        prev_end = v["end_ms"]
    json.dumps(m)


# ── Mouth shapes ──

@pytest.mark.parametrize("word,expected", [
    ("hola", ["O", "L", "AA"]),                      # silent h
    ("queso", ["OTHER", "E", "OTHER", "O"]),         # qu is one sound
    ("mamá", ["MBP", "AA", "MBP", "AA"]),
    ("vino", ["MBP", "I", "OTHER", "O"]),            # Spanish v closes the lips like b
    ("fuego", ["FV", "U", "E", "OTHER", "O"]),
    ("guerra", ["OTHER", "E", "OTHER", "AA"]),       # silent u, rr is one sound
    ("llamo", ["OTHER", "AA", "MBP", "O"]),          # ll is not an l
    ("hay", ["AA", "I"]),
    ("ñandú", ["OTHER", "AA", "OTHER", "OTHER", "U"]),
])
def test_letter_visemes_spanish(word, expected):
    assert M.letter_units(word, "es") == expected


@pytest.mark.parametrize("word,expected", [
    ("hello", ["OTHER", "E", "L", "O"]),
    ("phone", ["FV", "O", "OTHER"]),                 # ph is an f, the final e is silent
    ("book", ["MBP", "U", "OTHER"]),
    ("very", ["FV", "E", "OTHER", "I"]),             # English v touches the lip, final y is an i
    ("bee", ["MBP", "I"]),
    ("queen", ["OTHER", "U", "I", "OTHER"]),
])
def test_letter_visemes_english(word, expected):
    assert M.letter_units(word, "en") == expected


def test_phoneme_visemes_cover_ipa_arpabet_and_the_engines_shorthand():
    assert [M.phoneme_viseme(p) for p in ("a", "ɛ", "i", "o", "u", "m", "b", "f", "l", "s")] == ["AA", "E", "I", "O", "U", "MBP", "MBP", "FV", "L", "OTHER"]
    assert [M.phoneme_viseme(p) for p in ("AA1", "EH0", "IY", "OW2", "UW", "M", "V", "L", "S", "SIL")] == ["AA", "E", "I", "O", "U", "MBP", "FV", "L", "OTHER", "rest"]
    # a run of IPA symbols from the neural engine: stress marks vanish, a capital is a diphthong
    assert M.phoneme_units("həlˈO") == ["OTHER", "E", "L", "O"]
    assert M.phoneme_units("HH AH0 L OW1") == ["OTHER", "AA", "L", "O"]
    assert M.phoneme_units(["m", "a", "p", "a"]) == ["MBP", "AA", "MBP", "AA"]
    assert M.phoneme_units(None) == [] and M.phoneme_units("") == []


@pytest.mark.parametrize("lang,word,count", [
    ("es", "hola", 2), ("es", "país", 2), ("es", "ciudad", 2), ("es", "Faustus", 2), ("es", "queso", 2), ("es", "y", 1),
    ("es", "tecnología", 5), ("es", "2026", 6),
    ("en", "hello", 2), ("en", "there", 1), ("en", "table", 2), ("en", "computer", 3), ("en", "banana", 3), ("en", "I", 1),
])
def test_syllable_counts(lang, word, count):
    assert M.count_syllables(word, lang) == count


def test_language_guess():
    assert M.normalize_language("es-ES") == "es" and M.normalize_language("en_US") == "en"
    assert M.normalize_language("", "Hola, ¿cómo estás?") == "es"
    assert M.normalize_language("auto", "Hello, how are you doing today?") == "en"
    assert M.normalize_language("fr") == "en"   # no rules for it: English ones, timings still right


def test_tokenize_offsets_count_utf16_units_like_javascript():
    text = "😀 Hola, «mundo» ¿sí?"
    words = M.tokenize(text)
    assert [w["text"] for w in words] == ["Hola", "mundo", "sí"]
    u16 = text.encode("utf-16-le")
    for w in words:
        assert u16[2 * w["start_char"]:2 * w["end_char"]].decode("utf-16-le") == w["text"]
    assert words[0]["br"] and not words[1]["br"]   # the comma


# ── Timings from the audio ──

@pytest.mark.parametrize("lang,text", SAMPLES)
def test_timings_are_monotonic_in_bounds_and_cover_every_word(lang, text):
    audio, _ = spoken(text, lang)
    m = M.build_marks(text, audio, lang)
    check_invariants(m, text)
    assert m["timing"] == "audio" and m["words_source"] == "estimated" and m["visemes_source"] == "estimated"
    assert m["language"] == lang and len(m["words"]) == len(M.tokenize(text))


def test_estimate_follows_the_true_word_times_of_a_known_audio():
    errors, inside, total = [], 0, 0
    for lang, text in SAMPLES:
        audio, truth = spoken(text, lang)
        m = M.build_marks(text, audio, lang)
        for w, t in zip(m["words"], truth):
            errors += [abs(w["start_ms"] - t["start_ms"]), abs(w["end_ms"] - t["end_ms"])]
            total += 1
            inside += t["start_ms"] <= (w["start_ms"] + w["end_ms"]) / 2 <= t["end_ms"]
    errors.sort()
    assert sum(errors) / len(errors) < 60
    assert errors[int(0.9 * len(errors))] < 120
    assert inside == total      # the middle of every estimated word falls inside the word that was spoken


def test_a_pause_at_a_comma_falls_between_words_not_inside_one():
    audio, truth = spoken(ES, "es")
    m = M.build_marks(ES, audio, "es")
    qué, tal = m["words"][1], m["words"][2]
    assert qué["end_ms"] - qué["start_ms"] < 400 and tal["end_ms"] - tal["start_ms"] < 400
    estás, me = m["words"][3], m["words"][4]
    assert me["start_ms"] - estás["end_ms"] > 400   # the question mark's pause (650 ms) is a gap, not stretched into a word


def test_audio_without_pauses_or_with_nothing_still_gives_valid_marks():
    tone = [0.5 * __import__("math").sin(i / 7) for i in range(fake.RATE * 3)]
    m = M.build_marks(ES, wav(tone), "es")
    check_invariants(m, ES)
    assert m["words"][0]["start_ms"] == 0 and m["words"][-1]["end_ms"] == m["duration_ms"]   # spread over the whole audio
    silent = M.build_marks(ES, wav([0.0] * fake.RATE * 2), "es")
    check_invariants(silent, ES)
    assert silent["duration_ms"] == 2000


def test_other_wav_layouts_are_decoded():
    samples, _ = fake.render("Hola mundo.", "es")
    stereo = M.build_marks("Hola mundo.", wav(samples, channels=2), "es")
    eight_bit = M.build_marks("Hola mundo.", wav(samples, width=1), "es")
    check_invariants(stereo, "Hola mundo.")
    check_invariants(eight_bit, "Hola mundo.")
    assert stereo["words"][0]["start_ms"] > 100 and eight_bit["words"][0]["start_ms"] > 100


def test_float_wav_and_streamed_header():
    samples, _ = fake.render("Hola mundo.", "es")
    pcm = struct.pack(f"<{len(samples)}f", *samples)
    fmt = struct.pack("<HHIIHH", 3, 1, fake.RATE, fake.RATE * 4, 4, 32)
    data = b"RIFF" + struct.pack("<I", 4 + 8 + 16 + 8 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    m = M.build_marks("Hola mundo.", data, "es")
    check_invariants(m, "Hola mundo.")
    streamed = data[:40] + struct.pack("<I", 0xFFFFFFFF) + data[44:]      # a streamed WAV does not know its length
    assert M.build_marks("Hola mundo.", streamed, "es")["duration_ms"] == m["duration_ms"]


def test_undecodable_audio_falls_back_to_the_text():
    m = M.build_marks(ES, b"\x00\x01garbage", "es")
    assert m["timing"] == "text_only"
    check_invariants(m, ES)
    none = M.build_marks(ES, None, "es")
    assert none["timing"] == "text_only" and none["duration_ms"] > 1000
    check_invariants(none, ES)


def test_mpeg_audio_gives_a_duration_from_its_header():
    # MPEG-1 layer III, 128 kbit/s, 44.1 kHz: 16000 bytes are exactly one second
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413
    audio = frame * 40
    m = M.build_marks("Hola mundo", b"ID3\x04\x00\x00\x00\x00\x00\x00" + audio, "es")
    assert m["timing"] == "audio"
    assert abs(m["duration_ms"] - len(audio) * 8 // 128) <= 1
    check_invariants(m, "Hola mundo")


def test_no_words_no_marks_but_still_valid():
    for text in ("...", "—", "¿?"):
        m = M.build_marks(text, wav([0.0] * 2205), "es")
        assert m["words"] == [] and all(v["v"] == "rest" for v in m["visemes"])


def test_deterministic_and_fast_on_long_text():
    text = " ".join(t for _, t in SAMPLES) * 3
    audio, _ = spoken(text, "es")
    a, started = M.build_marks(text, audio, "es"), time.perf_counter()
    elapsed = time.perf_counter() - started
    assert a == M.build_marks(text, audio, "es")
    assert elapsed < 3.0, f"{len(a['words'])} words took {elapsed:.2f}s"
    check_invariants(a, text)


def test_visemes_follow_the_letters_inside_each_word():
    audio, _ = spoken("Mamá, hola.", "es")
    m = M.build_marks("Mamá, hola.", audio, "es")
    first = m["words"][0]
    inside = [v["v"] for v in m["visemes"] if first["start_ms"] <= v["start_ms"] < first["end_ms"]]
    assert inside == ["MBP", "AA", "MBP", "AA"]
    assert m["visemes"][0]["v"] == "rest" and m["visemes"][0]["start_ms"] == 0   # the silence before the first word


# ── What the engine says ──

def test_engine_word_times_and_phonemes_are_passed_through():
    text = "Hola mundo"
    audio = wav([0.3] * (fake.RATE * 2))
    words = [{"text": "Hola", "start_ms": 100, "end_ms": 700, "phonemes": ["o", "l", "a"]},
             {"text": "mundo", "start_ms": 800, "end_ms": 1500, "phonemes": "m u n d o".split()}]
    m = M.build_marks(text, audio, "es", {"words": words})
    assert (m["words_source"], m["visemes_source"]) == ("engine", "engine")
    assert [(w["start_ms"], w["end_ms"]) for w in m["words"]] == [(100, 700), (800, 1500)]
    assert [v["v"] for v in m["visemes"] if v["start_ms"] < 700 and v["v"] != "rest"] == ["O", "L", "AA"]


def test_engine_phoneme_track_replaces_the_letter_guess():
    phonemes = [{"p": "m", "start_ms": 0, "end_ms": 100}, {"p": "a", "start_ms": 100, "end_ms": 300},
                {"p": "f", "start_ms": 300, "end_ms": 400}, {"p": "zz", "start_ms": 400, "end_ms": 99999}]
    m = M.build_marks("Mama", wav([0.3] * fake.RATE), "es", {"phonemes": phonemes})
    assert m["visemes_source"] == "engine" and m["words_source"] == "estimated"
    assert [(v["v"], v["start_ms"], v["end_ms"]) for v in m["visemes"][:4]] == [("MBP", 0, 100), ("AA", 100, 300), ("FV", 300, 400), ("OTHER", 400, 1000)]
    check_invariants(m, "Mama")


def test_engine_chunks_anchor_the_estimate_and_bring_phonemes():
    """The neural engine reports each chunk's text and phonemes (one group per word) and the chunk's place in the audio."""
    text = "Hello world. Good day."
    samples, truth = fake.render(text, "en")
    audio = wav(samples)
    first_end = truth[1]["end_ms"] + 300
    segments = [{"text": "Hello world.", "phonemes": "həlˈO wˈɜɹld", "start_ms": 0, "end_ms": first_end},
                {"text": "Good day.", "phonemes": "ɡˈʊd dˈA", "start_ms": first_end, "end_ms": int(len(samples) * 1000 / fake.RATE)}]
    m = M.build_marks(text, audio, "en", {"segments": segments})
    assert m["words_source"] == "estimated" and m["visemes_source"] == "engine"   # timings are still estimated, mouth shapes are real
    check_invariants(m, text)
    assert m["words"][2]["start_ms"] >= first_end
    assert [v["v"] for v in m["visemes"] if m["words"][0]["start_ms"] <= v["start_ms"] < m["words"][0]["end_ms"]][:2] == ["OTHER", "E"]


@pytest.mark.parametrize("engine", [
    {"words": [{"text": "x", "start_ms": 0, "end_ms": 10}]},                      # wrong number of words
    {"words": "nope"}, {"words": [{"text": "a"}, {"text": "b"}]},                  # no times
    {"words": [{"start_ms": "a", "end_ms": 1}, {"start_ms": 1, "end_ms": 2}]},
    {"segments": [{"text": "not in the text", "start_ms": 0, "end_ms": 100}]},
    {"segments": [{"text": "Hola", "start_ms": 500, "end_ms": 100}]},
    {"phonemes": [{"p": 1}]}, {"phonemes": "x"}, "garbage",
])
def test_inconsistent_engine_data_is_ignored(engine):
    audio, _ = spoken("Hola mundo", "es")
    m = M.build_marks("Hola mundo", audio, "es", engine)
    check_invariants(m, "Hola mundo")
    assert m["words_source"] == "estimated" and m["visemes_source"] == "estimated"


# ── The command contract and the API ──

def _fake_command(*extra):
    return f'"{sys.executable}" "{FAKE}" {{input_path}} {{output_path}} {" ".join(extra)}'.strip()


def test_command_marks_file_is_read_and_missing_or_broken_ones_are_not_fatal(tmp_path):
    audio, hints = synthesize_command_detailed(ES, _fake_command("{marks_path}"), language="es")
    assert audio[:4] == b"RIFF" and len(hints["words"]) == len(M.tokenize(ES))
    audio, hints = synthesize_command_detailed(ES, _fake_command(), language="es")      # the template does not ask for marks
    assert audio[:4] == b"RIFF" and hints is None
    broken = f'"{sys.executable}" -c "import sys,wave;w=wave.open(sys.argv[1],\'wb\');w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b\'\\0\\0\'*80);w.close();open(sys.argv[2],\'w\').write(\'{{nope\')" {{output_path}} {{marks_path}}'
    audio, hints = synthesize_command_detailed("hola", broken)
    assert audio[:4] == b"RIFF" and hints is None


def _client(tmp_path, template=None, provider="command"):
    svc = TTSService(cache_dir=str(tmp_path / "cache"))
    settings = {"tts_enabled": True, "tts_provider": provider, "tts_model": "m", "tts_voice": "v", "tts_speed": "1",
                "tts_command_template": template if template is not None else _fake_command("{marks_path}")}
    svc._load_settings = lambda: settings
    app = FastAPI()
    app.include_router(setup_tts_routes(svc))
    return TestClient(app)


def test_api_returns_audio_and_marks_in_one_json_body(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/tts/synthesize", json={"text": ES, "format": "base64", "marks": True, "language": "es"})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        assert set(body) == {"audio", "mime", "marks", "marks_ms", "synth_ms"}
        assert body["mime"] == "audio/wav" and base64.b64decode(body["audio"])[:4] == b"RIFF"
        marks = body["marks"]
        assert set(marks) == {"version", "language", "duration_ms", "timing", "words_source", "visemes_source", "viseme_set", "words", "visemes"}
        assert marks["version"] == 1 and marks["viseme_set"] == list(M.VISEMES) and marks["language"] == "es"
        assert marks["words_source"] == "engine"      # the fake command wrote a marks file
        assert set(marks["words"][0]) == {"i", "text", "start_char", "end_char", "start_ms", "end_ms"}
        assert set(marks["visemes"][0]) == {"v", "start_ms", "end_ms"}
        check_invariants(marks, ES)
        assert marks["duration_ms"] == int(len(wave.open(io.BytesIO(base64.b64decode(body["audio"]))).readframes(10 ** 9)) / 2 * 1000 / fake.RATE)


def test_api_estimates_when_the_engine_reports_nothing(tmp_path):
    with _client(tmp_path, template=_fake_command()) as c:
        marks = c.post("/api/tts/synthesize", json={"text": ES, "format": "base64", "marks": True, "language": "es"}).json()["marks"]
    assert marks["words_source"] == "estimated" and marks["visemes_source"] == "estimated"
    check_invariants(marks, ES)


def test_api_without_marks_is_unchanged(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/tts/synthesize", json={"text": "Hola", "language": "es"})
        assert r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
        r = c.post("/api/tts/synthesize", json={"text": "Hola", "format": "base64"})
        assert set(r.json()) == {"audio"}


def test_api_marks_need_the_json_format_and_never_cost_the_audio(tmp_path, monkeypatch):
    with _client(tmp_path) as c:
        assert c.post("/api/tts/synthesize", json={"text": "Hola", "marks": True}).status_code == 422
        import services.tts.marks as marks_module
        monkeypatch.setattr(marks_module, "build_marks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        r = c.post("/api/tts/synthesize", json={"text": "Hola", "format": "base64", "marks": True})
        assert r.status_code == 200 and r.json()["marks"] is None and base64.b64decode(r.json()["audio"])[:4] == b"RIFF"


def test_api_works_with_a_service_that_only_has_the_plain_interface():
    from types import SimpleNamespace
    svc = SimpleNamespace(available=True, _load_settings=lambda: {"tts_provider": "local"}, synthesize=lambda text, **k: wav([0.2] * 4000))
    app = FastAPI()
    app.include_router(setup_tts_routes(svc))
    with TestClient(app) as c:
        marks = c.post("/api/tts/synthesize", json={"text": "Hola", "format": "base64", "marks": True}).json()["marks"]
    assert marks["words_source"] == "estimated"
