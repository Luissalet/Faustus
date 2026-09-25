"""Research podcast (src/research_podcast.py + /api/research/{id}/podcast).

Covers: script parsing/repair and validation, the voice selection matrix,
pure-Python WAV concatenation, long-report condensation, the background job
with a fake model and a fake synthesizer, the routes' owner scoping, and that
writing the podcast block never clobbers the rest of the research JSON.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import struct
import wave
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import research_podcast as rp


# ── helpers ─────────────────────────────────────────────────────────────────

def _sine_wav(seconds: float = 0.2, rate: int = 22050, freq: float = 440.0, channels: int = 1) -> bytes:
    n = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) * channels
            for i in range(n))
        w.writeframes(frames)
    return buf.getvalue()


def _script_json(n: int = 6) -> str:
    return json.dumps({"lines": [
        {"speaker": "A" if i % 2 == 0 else "B", "text": f"Line number {i} about the findings."}
        for i in range(n)]})


def _write_research(tmp_path, sid="abc-123", owner="alice", **extra):
    d = tmp_path / "data" / "deep_research"
    d.mkdir(parents=True, exist_ok=True)
    data = {"query": "Is coffee healthy?", "status": "completed", "owner": owner,
            "result": "# Coffee\n\nModerate intake is linked to lower risk [1].",
            "raw_report": "# Coffee\n\nModerate intake is linked to lower risk [1].",
            "sources": [{"url": "https://example.org", "title": "Study"}],
            "report_language": "en", "stats": {"rounds": 3}, "category": "health",
            "hidden_images": ["x.png"], **extra}
    p = d / f"{sid}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


INSTALLED = [
    {"name": "es_ES-davefx-medium", "language": "es_ES", "quality": "medium"},
    {"name": "es_ES-sharvard-medium", "language": "es_ES", "quality": "medium"},
    {"name": "en_US-lessac-medium", "language": "en_US", "quality": "medium"},
    {"name": "en_US-ryan-high", "language": "en_US", "quality": "high"},
]
CATALOGUE = [
    {"name": "es_ES-davefx-medium", "language": "es_ES", "quality": "medium"},
    {"name": "es_ES-sharvard-medium", "language": "es_ES", "quality": "medium"},
    {"name": "en_US-lessac-medium", "language": "en_US", "quality": "medium"},
    {"name": "en_US-amy-medium", "language": "en_US", "quality": "medium"},
    {"name": "en_GB-alan-medium", "language": "en_GB", "quality": "medium"},
]


# ── script parsing ──────────────────────────────────────────────────────────

def test_parse_clean_json():
    lines = rp.parse_script(_script_json(6))
    assert len(lines) == 6
    assert [l["speaker"] for l in lines[:2]] == ["A", "B"]


def test_parse_fenced_json_with_think_and_trailing_commas():
    raw = ("<think>let me plan</think>\n```json\n"
           '{"lines": [{"speaker": "A", "text": "Hi."}, {"speaker": "B", "text": "Hello."},'
           '{"speaker": "A", "text": "Finding one."}, {"speaker": "B", "text": "Interesting."},]}\n```')
    lines = rp.parse_script(raw)
    assert [l["text"] for l in lines] == ["Hi.", "Hello.", "Finding one.", "Interesting."]


def test_parse_truncated_json_salvages_complete_items():
    full = _script_json(8)
    truncated = full[: full.rfind('{"speaker"') + 20]   # cut inside the last item
    lines = rp.parse_script(truncated)
    assert len(lines) == 7


def test_parse_accepts_bare_array_and_speaker_aliases():
    raw = json.dumps([
        {"speaker": "Host A", "text": "One."}, {"speaker": "speaker b", "text": "Two."},
        {"speaker": "1", "text": "Three."}, {"speaker": "2", "text": "Four."}])
    assert [l["speaker"] for l in rp.parse_script(raw)] == ["A", "B", "A", "B"]


def test_parse_named_speakers_map_by_first_appearance():
    raw = json.dumps({"dialogue": [
        {"speaker": "Ana", "text": "One."}, {"speaker": "Luis", "text": "Two."},
        {"speaker": "Ana", "text": "Three."}, {"speaker": "Luis", "text": "Four."},
        {"speaker": "Narrator", "text": "dropped"}]})
    lines = rp.parse_script(raw)
    assert [l["speaker"] for l in lines] == ["A", "B", "A", "B"]


def test_parse_plain_transcript():
    raw = "Title: nothing\n**A:** Welcome.\nB: Thanks.\nA: The report says X.\nB: Good to know."
    lines = rp.parse_script(raw)
    assert [l["speaker"] for l in lines] == ["A", "B", "A", "B"]
    assert lines[0]["text"] == "Welcome."


def test_clean_line_strips_markup_citations_and_urls():
    out = rp.clean_line("**Well** [laughs] the study [3] at https://x.org says *(sighs)* `yes`")
    assert out == "Well the study at says yes"


@pytest.mark.parametrize("raw", [
    "", "no json here", '{"lines": []}',
    json.dumps({"lines": [{"speaker": "A", "text": f"x{i}"} for i in range(6)]}),   # one speaker
    json.dumps({"lines": [{"speaker": "A", "text": "a"}, {"speaker": "B", "text": "b"}]}),  # too short
])
def test_parse_rejects_unusable(raw):
    with pytest.raises(rp.ScriptError):
        rp.parse_script(raw)


# ── build_script ────────────────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, messages, max_tokens, schema=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "schema": schema})
        return self.replies.pop(0) if self.replies else ""


def test_build_script_retries_once_on_malformed_reply():
    llm = FakeLLM(["sorry, here is the podcast!", _script_json(6)])
    lines = asyncio.run(rp.build_script("# R\n\nShort report.", "es", 3, llm=llm))
    assert len(lines) == 6
    assert len(llm.calls) == 2
    assert llm.calls[0]["schema"] == rp.SCRIPT_SCHEMA
    assert "Spanish" in llm.calls[0]["messages"][0]["content"]
    assert "could not be used" in llm.calls[1]["messages"][-1]["content"]


def test_build_script_fails_with_clear_message_after_retry():
    llm = FakeLLM(["nope", "still nope"])
    with pytest.raises(rp.PodcastError, match="usable podcast script"):
        asyncio.run(rp.build_script("# R\n\nShort report.", "en", 3, llm=llm))


def test_long_report_is_condensed_in_chunks_first():
    sections = [f"## Section {i}\n\n" + " ".join(f"word{i}_{j}" for j in range(900)) for i in range(8)]
    report = "# Title\n\n" + "\n\n".join(sections) + "\n\n## Sources\n\n1. https://a.org\n"
    assert rp.word_count(report) > 7000
    progress = []

    class ByKind(FakeLLM):
        async def __call__(self, messages, max_tokens, schema=None):
            await super().__call__(messages, max_tokens, schema)
            return _script_json(8) if schema else "Faithful notes of this part."

    llm = ByKind([])

    async def run():
        return await rp.build_script(report, "en", 6, llm=llm, on_progress=progress.append)

    lines = asyncio.run(run())
    assert len(lines) == 8
    condense_calls = [c for c in llm.calls if c["schema"] is None]
    assert 4 <= len(condense_calls) <= 9
    # No chunk sent to the model is longer than the chunk budget.
    for c in condense_calls:
        body = c["messages"][1]["content"]
        assert rp.word_count(body) <= rp.CHUNK_WORDS + 40
    # The source list and URLs never reach the model.
    assert all("https://a.org" not in c["messages"][1]["content"] for c in llm.calls)
    assert any(p.get("phase") == "condensing" for p in progress)
    assert "Condensed notes" in llm.calls[-1]["messages"][1]["content"]


def test_chunk_sections_splits_an_oversized_section():
    sec = "## Big\n\n" + " ".join(["w"] * 5000)
    chunks = rp.chunk_sections([sec], 1400)
    assert len(chunks) >= 4
    assert all(rp.word_count(c) <= 1400 for c in chunks)


# ── voices ──────────────────────────────────────────────────────────────────

def test_voices_two_distinct_for_language():
    v = rp.pick_voices("es", INSTALLED, "", "", CATALOGUE)
    assert {v["A"], v["B"]} == {"es_ES-davefx-medium", "es_ES-sharvard-medium"}
    assert v["A"] != v["B"] and v["warnings"] == []


def test_voices_prefer_higher_quality():
    v = rp.pick_voices("en", INSTALLED, "", "", CATALOGUE)
    assert v["A"] == "en_US-ryan-high" and v["B"] == "en_US-lessac-medium"


def test_voices_overrides_win_when_they_fit():
    v = rp.pick_voices("es", INSTALLED, "es_ES-sharvard-medium", "es_ES-davefx-medium", CATALOGUE)
    assert (v["A"], v["B"]) == ("es_ES-sharvard-medium", "es_ES-davefx-medium")


def test_voices_override_in_wrong_language_or_missing_is_ignored_with_warning():
    v = rp.pick_voices("es", INSTALLED, "en_US-lessac-medium", "es_ES-nope-medium", CATALOGUE)
    assert v["A"].startswith("es_ES") and v["B"].startswith("es_ES") and v["A"] != v["B"]
    assert len(v["warnings"]) == 2


def test_voices_same_override_for_both_still_gives_distinct():
    v = rp.pick_voices("es", INSTALLED, "es_ES-davefx-medium", "es_ES-davefx-medium", CATALOGUE)
    assert v["A"] == "es_ES-davefx-medium" and v["B"] == "es_ES-sharvard-medium"


def test_voices_single_installed_uses_it_twice_with_different_speed():
    only = [INSTALLED[0]]
    v = rp.pick_voices("es", only, "", "", CATALOGUE)
    assert v["A"] == v["B"] == "es_ES-davefx-medium"
    assert v["speed"]["A"] != v["speed"]["B"]
    assert v["warnings"] and "es_ES-sharvard-medium" in v["warnings"][0]


def test_voices_none_fails_naming_catalogue_voices():
    with pytest.raises(rp.PodcastError) as e:
        rp.pick_voices("es", [INSTALLED[2]], "", "", CATALOGUE)
    msg = str(e.value)
    assert "es_ES-davefx-medium" in msg and "Settings" in msg


def test_voices_none_for_uncatalogued_language():
    with pytest.raises(rp.PodcastError, match="German"):
        rp.pick_voices("de", INSTALLED, "", "", CATALOGUE)


# ── audio ───────────────────────────────────────────────────────────────────

def test_concat_wavs_adds_pauses_and_keeps_format():
    a, b = _sine_wav(0.2), _sine_wav(0.3, freq=660)
    out, duration = rp.concat_wavs([(a, 0.5), (b, 0.0)])
    with wave.open(io.BytesIO(out)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)
        assert w.getnframes() == int(0.2 * 22050) + int(0.5 * 22050) + int(0.3 * 22050)
    assert abs(duration - 1.0) < 0.01


def test_concat_wavs_mismatch_without_ffmpeg_is_an_error(monkeypatch):
    monkeypatch.setattr(rp, "_ffmpeg", lambda: None)
    with pytest.raises(rp.PodcastError, match="ffmpeg"):
        rp.concat_wavs([(_sine_wav(0.1, 22050), 0.1), (_sine_wav(0.1, 16000), 0)])


@pytest.mark.skipif(rp._ffmpeg() is None, reason="ffmpeg not installed")
def test_concat_wavs_mismatch_resamples_with_ffmpeg():
    out, duration = rp.concat_wavs([(_sine_wav(0.2, 22050), 0.0), (_sine_wav(0.2, 16000), 0.0)])
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == 22050
    assert abs(duration - 0.4) < 0.02


@pytest.mark.skipif(rp._ffmpeg() is None, reason="ffmpeg not installed")
def test_wav_to_mp3_with_ffmpeg():
    mp3 = rp.wav_to_mp3(_sine_wav(0.5))
    assert mp3 and (mp3[:3] == b"ID3" or mp3[0] == 0xFF)


def test_wav_to_mp3_without_ffmpeg_is_none(monkeypatch):
    monkeypatch.setattr(rp, "_ffmpeg", lambda: None)
    assert rp.wav_to_mp3(_sine_wav(0.1)) is None


def test_split_for_speech_bounds_pieces():
    text = ("This is a sentence. " * 40) + ("x" * 700)
    parts = rp.split_for_speech(text, 120)
    assert all(len(p) <= 120 for p in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


# ── research JSON ───────────────────────────────────────────────────────────

def test_write_podcast_block_keeps_every_other_key(tmp_path):
    p = _write_research(tmp_path, extra_key={"nested": [1, 2]})
    before = json.loads(p.read_text())
    rp.write_podcast_block(p, {"status": "done", "artifact_id": "occ_x"})
    after = json.loads(p.read_text())
    assert after.pop("podcast") == {"status": "done", "artifact_id": "occ_x"}
    assert after == before


def test_restart_marks_a_running_block_failed(tmp_path):
    p = _write_research(tmp_path, sid="gone-1", podcast={"status": "running"})
    rp._JOBS.pop("gone-1", None)
    out = rp.status_payload("gone-1", p)
    assert out["status"] == "failed" and "restarted" in out["error"]
    assert json.loads(p.read_text())["podcast"]["status"] == "failed"


def test_report_player_injected_only_when_done():
    html = "<html><body><p>r</p></body></html>"
    assert rp.inject_report_audio(html, "s1", {"podcast": {"status": "failed"}}) == html
    out = rp.inject_report_audio(html, "s1", {"podcast": {"status": "done", "artifact_id": "occ"}})
    assert "/api/research/s1/podcast/audio" in out and out.endswith("</body></html>")


# ── job lifecycle ───────────────────────────────────────────────────────────

@pytest.fixture
def fake_tts(monkeypatch):
    monkeypatch.setattr(rp, "_runtime", lambda: "python")
    monkeypatch.setattr(rp, "_installed_voices", lambda: INSTALLED)
    monkeypatch.setattr(rp, "_catalogue", lambda: CATALOGUE)
    monkeypatch.setattr(rp, "_setting", lambda key, default: {"research_podcast_format": "wav"}.get(key, default))
    calls = []

    def synth(text, voice, speed):
        calls.append((text, voice, speed))
        return _sine_wav(0.05, freq=300 if voice.startswith("en_US-ryan") else 500)

    monkeypatch.setattr(rp, "_synthesize", synth)
    saved = {}

    def save(files, *, owner, session_id):
        saved.update({name: content for name, content in files})
        saved["_owner"] = owner
        return {name: f"occ_{i}" for i, (name, _) in enumerate(files)}

    monkeypatch.setattr(rp, "_save_artifacts", save)
    return SimpleNamespace(calls=calls, saved=saved)


def test_job_runs_to_done_and_writes_block(tmp_path, fake_tts):
    p = _write_research(tmp_path, sid="job-ok")
    llm = FakeLLM([_script_json(6)])

    async def run():
        first = await rp.start_podcast("job-ok", p, "alice", llm=llm)
        assert first["status"] == "running"
        with pytest.raises(rp.PodcastBusy):
            await rp.start_podcast("job-ok", p, "alice", llm=llm)
        await rp._JOBS["job-ok"]["task"]
        return rp.status_payload("job-ok", p)

    out = asyncio.run(run())
    assert out["status"] == "done", out
    assert out["audio_url"] == "/api/research/job-ok/podcast/audio"
    assert out["transcript_url"] == "/api/artifacts/occ_1/download"
    assert len(out["script"]) == 6
    block = json.loads(p.read_text())["podcast"]
    assert block["artifact_id"] == "occ_0" and block["transcript_artifact_id"] == "occ_1"
    assert block["voices"] == {"A": "en_US-ryan-high", "B": "en_US-lessac-medium"}
    assert block["lines"] == 6 and block["duration_s"] > 0 and block["format"] == "wav"
    assert len(fake_tts.calls) == 6
    assert fake_tts.saved["_owner"] == "alice"
    transcript = next(v for k, v in fake_tts.saved.items() if k.endswith(".md")).decode()
    assert "**A:** Line number 0" in transcript
    # The rest of the report is untouched.
    data = json.loads(p.read_text())
    assert data["result"].startswith("# Coffee") and data["hidden_images"] == ["x.png"]


def test_job_failure_is_recorded(tmp_path, fake_tts, monkeypatch):
    p = _write_research(tmp_path, sid="job-bad")
    monkeypatch.setattr(rp, "_synthesize", lambda text, voice, speed: None)
    llm = FakeLLM([_script_json(4)])

    async def run():
        await rp.start_podcast("job-bad", p, "alice", llm=llm)
        await rp._JOBS["job-bad"]["task"]
        return rp.status_payload("job-bad", p)

    out = asyncio.run(run())
    assert out["status"] == "failed" and "Piper could not synthesize" in out["error"]


def test_start_without_voices_fails_before_any_model_call(tmp_path, fake_tts, monkeypatch):
    p = _write_research(tmp_path, sid="job-novoice", report_language="es")
    monkeypatch.setattr(rp, "_installed_voices", lambda: [INSTALLED[2]])
    llm = FakeLLM([_script_json(4)])
    with pytest.raises(rp.PodcastError, match="es_ES-davefx-medium"):
        asyncio.run(rp.start_podcast("job-novoice", p, "alice", llm=llm))
    assert llm.calls == []
    assert "podcast" not in json.loads(p.read_text())


def test_start_without_piper_runtime(tmp_path, fake_tts, monkeypatch):
    p = _write_research(tmp_path, sid="job-noruntime")
    monkeypatch.setattr(rp, "_runtime", lambda: None)
    with pytest.raises(rp.PodcastError, match="Piper is not installed"):
        asyncio.run(rp.start_podcast("job-noruntime", p, "alice", llm=FakeLLM([])))


# ── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def routes(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.research_routes.DEEP_RESEARCH_DIR", str(tmp_path / "data" / "deep_research"))
    from unittest.mock import MagicMock
    from routes.research_routes import setup_research_routes
    handler = MagicMock()
    handler._active_tasks = {}
    router = setup_research_routes(handler)

    def route(path, method):
        for r in router.routes:
            if getattr(r, "path", "") == path and method in getattr(r, "methods", set()):
                return r.endpoint
        raise AssertionError(path)
    return route


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(current_user=user), client=None)


def test_routes_are_owner_scoped(tmp_path, routes, fake_tts):
    _write_research(tmp_path, sid="bob-1", owner="bob")
    get = routes("/api/research/{session_id}/podcast", "GET")
    post = routes("/api/research/{session_id}/podcast", "POST")
    audio = routes("/api/research/{session_id}/podcast/audio", "GET")
    for call in (lambda: get("bob-1", _req("alice")), lambda: post("bob-1", _req("alice")),
                 lambda: audio("bob-1", _req("alice"), 0)):
        with pytest.raises(HTTPException) as e:
            asyncio.run(call())
        assert e.value.status_code == 404
    out = asyncio.run(get("bob-1", _req("bob")))
    assert out["status"] == "none"


def test_route_start_errors_map_to_status_codes(tmp_path, routes, fake_tts, monkeypatch):
    _write_research(tmp_path, sid="al-1", owner="alice")
    post = routes("/api/research/{session_id}/podcast", "POST")
    monkeypatch.setattr(rp, "_runtime", lambda: None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(post("al-1", _req("alice")))
    assert e.value.status_code == 400 and "Piper" in e.value.detail
    rp._JOBS["al-1"] = {"status": "running"}
    try:
        with pytest.raises(HTTPException) as e:
            asyncio.run(post("al-1", _req("alice")))
        assert e.value.status_code == 409
    finally:
        rp._JOBS.pop("al-1", None)


def test_audio_route_404_until_done(tmp_path, routes):
    _write_research(tmp_path, sid="al-2", owner="alice", podcast={"status": "failed"})
    audio = routes("/api/research/{session_id}/podcast/audio", "GET")
    with pytest.raises(HTTPException) as e:
        asyncio.run(audio("al-2", _req("alice"), 0))
    assert e.value.status_code == 404


def test_audio_route_serves_file_inline_or_as_download(tmp_path, routes, monkeypatch):
    f = tmp_path / "a.wav"
    f.write_bytes(_sine_wav(0.05))
    _write_research(tmp_path, sid="al-3", owner="alice", podcast={"status": "done", "artifact_id": "occ_9"})
    monkeypatch.setattr(rp, "audio_file", lambda block: (str(f), "audio/wav", "podcast.wav"))
    audio = routes("/api/research/{session_id}/podcast/audio", "GET")
    inline = asyncio.run(audio("al-3", _req("alice"), 0))
    assert inline.headers["content-disposition"].startswith("inline")
    dl = asyncio.run(audio("al-3", _req("alice"), 1))
    assert dl.headers["content-disposition"].startswith("attachment")


def test_save_artifacts_and_audio_file_against_a_real_store(tmp_path, monkeypatch):
    """The artifact-store seam for real: bytes land, ids come back, and
    audio_file() finds the stored file again by id."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database as db_mod
    from src import artifact_store

    engine = create_engine(f"sqlite:///{tmp_path / 'art.db'}", connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    store = tmp_path / "artifacts"
    store.mkdir()
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(store))

    wav = _sine_wav(0.05)
    ids = rp._save_artifacts([("podcast-x.wav", wav), ("podcast-x.md", b"# t\n")],
                             owner="alice", session_id="sid-1")
    assert set(ids) == {"podcast-x.wav", "podcast-x.md"}
    found = rp.audio_file({"artifact_id": ids["podcast-x.wav"]})
    assert found is not None
    path, media, label = found
    assert open(path, "rb").read() == wav
    assert media.startswith("audio/") and label == "podcast-x.wav"


# ── agent tool ──────────────────────────────────────────────────────────────

def test_tool_is_owner_scoped_and_returns_dicts(tmp_path, monkeypatch, fake_tts):
    from src import research_handler
    from src.agent_tools.research_podcast_tools import ResearchPodcastTool
    d = tmp_path / "data" / "deep_research"
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", d)
    _write_research(tmp_path, sid="tool-1", owner="alice")
    tool = ResearchPodcastTool()

    out = asyncio.run(tool.execute('{"research_id": "tool-1", "action": "status"}', {"owner": "bob"}))
    assert isinstance(out, dict) and out["exit_code"] == 1 and "not found" in out["error"]
    out = asyncio.run(tool.execute('{"research_id": "../etc"}', {"owner": "alice"}))
    assert out["exit_code"] == 1
    out = asyncio.run(tool.execute('{"research_id": "tool-1", "action": "status"}', {"owner": "alice"}))
    assert out["exit_code"] == 0 and out["status"] == "none"

    monkeypatch.setattr(rp, "make_llm_caller", lambda owner: FakeLLM([_script_json(4)]))

    async def run():
        started = await tool.execute('{"research_id": "tool-1"}', {"owner": "alice"})
        await rp._JOBS["tool-1"]["task"]
        done = await tool.execute('{"research_id": "tool-1", "action": "status"}', {"owner": "alice"})
        again = await tool.execute('{"research_id": "tool-1"}', {"owner": "alice"})
        return started, done, again

    started, done, again = asyncio.run(run())
    assert started["status"] == "running" and started["exit_code"] == 0
    assert done["status"] == "done" and "Podcast ready" in done["output"]
    assert again["status"] == "done"   # an existing podcast is not remade without regenerate


def test_request_voices_win_over_settings(monkeypatch):
    from src import research_podcast as rp
    installed = [{"name": "es_ES-davefx-medium", "language": "es_ES", "quality": "medium"},
                 {"name": "es_ES-sharvard-medium", "language": "es_ES", "quality": "medium"}]
    monkeypatch.setattr(rp, "_setting", lambda k, d=None: {"research_podcast_voice_a": "es_ES-davefx-medium"}.get(k, d))
    got = rp.pick_voices("es", installed=installed, voice_a="es_ES-sharvard-medium", voice_b="es_ES-davefx-medium",
                         catalogue=[])
    assert (got["A"], got["B"]) == ("es_ES-sharvard-medium", "es_ES-davefx-medium")
