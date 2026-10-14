"""Streaming voice sessions (services/voice_stream.py, routes/voice_stream_routes.py)."""

import asyncio
import json
import struct

import pytest

from services import voice_stream as vs


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeSTT:
    def __init__(self, text="hola, ¿qué tal estás hoy?", language="es"):
        self.text = text
        self.language = language
        self.calls = []
        self.available = True

    def _load_settings(self):
        return {"stt_enabled": True, "stt_provider": "local", "stt_model": "small",
                "voice_stop_phrases": ["basta ya"]}

    def transcribe(self, audio, language_override="", metadata=None):
        self.calls.append((len(audio), language_override))
        if metadata is not None:
            metadata["language"] = self.language
        return self.text(audio) if callable(self.text) else self.text


class FakeTTS:
    available = True

    def __init__(self):
        self.calls = []

    def _load_settings(self):
        return {"tts_enabled": True, "tts_provider": "system"}

    def synthesize(self, text, use_cache=True, language=""):
        self.calls.append((text, language))
        return b"RIFF\x00\x00\x00\x00WAVEfmt " + text.encode("utf-8")


class FakeRun:
    def __init__(self, run_id, frames, delay=0.0):
        self.run_id = run_id
        self.status = "running"
        self.frames = frames
        self.delay = delay


class FakeRuns:
    def __init__(self):
        self.active = {}
        self.stopped = []

    def get_active_run(self, sid):
        run = self.active.get(sid)
        return run if run is not None and run.status == "running" else None

    def get_run_id(self, sid):
        run = self.active.get(sid)
        return run.run_id if run else None

    async def subscribe(self, sid, expected_run=None):
        run = expected_run or self.active.get(sid)
        if run is None:
            return
        for frame in run.frames:
            if run.delay:
                await asyncio.sleep(run.delay)
            yield frame
        run.status = "done"

    def stop(self, sid, expected_run_id=None):
        self.stopped.append(sid)
        return True


def delta(text, **extra):
    return "data: " + json.dumps({"delta": text, **extra}) + "\n\n"


def replace(text):
    return "data: " + json.dumps({"type": "response_replace", "text": text}) + "\n\n"


DONE = "data: [DONE]\n\n"


def make_manager(stt=None, tts=None, runs=None, frames=None, delay=0.0):
    runs = runs or FakeRuns()
    sent = []

    async def start_turn(owner, sid, text, client_message_id):
        sent.append((owner, sid, text, client_message_id))
        run = FakeRun(f"run-{len(sent)}", frames or [delta("Hola. "), delta("Todo bien por aquí. "), delta("Adiós."), DONE],
                      delay)
        runs.active[sid] = run
        return run.run_id

    manager = vs.VoiceSessionManager(stt=stt or FakeSTT(), tts=tts or FakeTTS(), runs=runs, start_turn=start_turn)
    return manager, runs, sent


async def collect_until(session, kind, after=0, timeout=3.0):
    events = []
    deadline = asyncio.get_event_loop().time() + timeout
    cursor = after
    while asyncio.get_event_loop().time() < deadline:
        fresh = await session.events_after(cursor, 0.2)
        for event in fresh:
            cursor = event.seq
            events.append(event)
            if event.type == kind:
                return events
    raise AssertionError(f"no {kind!r} event; got {[e.type for e in events]}")


def pcm(seconds=1.0, rate=16000):
    return struct.pack("<h", 1000) * int(seconds * rate)


# ── text policies ─────────────────────────────────────────────────────────────

def test_spoken_text_skips_code_links_tables_and_markup():
    text = "**Hola** [mira esto](https://x.y). ```py\nprint(1)\n``` Fin https://a.b\n| a | b |\n![img](u)"
    assert vs.spoken_text(text) == "Hola mira esto. Fin"
    assert vs.spoken_text("Antes ```bash\nrm -rf") == "Antes"


def test_sentence_buffer_holds_the_unfinished_sentence_until_final():
    buf = vs.SentenceBuffer()
    assert buf.take("Primera frase. Segunda") == ["Primera frase."]
    assert buf.take("Primera frase. Segunda frase.") == []
    assert buf.take("Primera frase. Segunda frase.", final=True) == ["Segunda frase."]
    buf = vs.SentenceBuffer()
    assert buf.take("El Dr. Pérez llegó. Bien.", final=True) == ["El Dr. Pérez llegó.", "Bien."]


def test_sentence_buffer_budget_truncates():
    buf = vs.SentenceBuffer(budget=20)
    assert buf.take("Una frase corta. Otra frase bastante más larga que el resto.", final=True) == ["Una frase corta."]
    assert buf.truncated


def test_filters_match_the_studio_panel():
    assert vs.is_stop_phrase("¡Para!")
    assert vs.is_stop_phrase("basta ya", ["basta ya"])
    assert not vs.is_stop_phrase("para el reloj")
    assert vs.is_hallucination("Subtítulos realizados por la comunidad de Amara.org")
    assert vs.is_hallucination("   ")
    assert not vs.is_hallucination("sí")
    assert vs.strip_wake_word("Oye Faustus, abre el correo") == (True, "Oye abre el correo")
    assert vs.strip_wake_word("abre el correo") == (False, "abre el correo")
    assert vs.speech_language("Hola, gracias") == "es"
    assert vs.speech_language("Thanks for this, here you have the answer", "es") == "en"


def test_pcm_becomes_a_wav_any_backend_reads():
    wav = vs.pcm16_to_wav(pcm(0.5))
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert vs.sniff_container(wav) == "wav"
    assert vs.sniff_container(b"\x1a\x45\xdf\xa3....") == "webm"
    assert vs.sniff_container(pcm(0.1)) == ""


def test_reply_text_ignores_reasoning_and_follows_replacements():
    reply = vs.ReplyText()
    for frame in [delta("pienso", thinking=True), delta("Borrador."), replace(""), delta("Final.")]:
        for payload in vs.sse_payloads(frame):
            reply.feed(payload)
    assert reply.text == "Final."
    assert reply.replaced


# ── sessions ──────────────────────────────────────────────────────────────────

def test_server_mode_sends_once_and_speaks_the_reply_in_order():
    async def scenario():
        manager, runs, sent = make_manager(delay=0.01)
        session = await manager.create("luis", "sid-1")
        result = await session.submit_clip(vs.pcm16_to_wav(pcm(1.2)), speech_end_at=None)
        assert result["verdict"] == "ok" and result["text"].startswith("hola")
        events = await collect_until(session, "speech_end")
        await asyncio.sleep(0.3)  # the follow loop must not pick the same run up again
        audio = [e for e in events if e.type == "audio"]
        assert [e.data["text"] for e in audio] == ["Hola.", "Todo bien por aquí.", "Adiós."]
        assert all(e.audio and e.data["mime"] == "audio/wav" for e in audio)
        assert len(sent) == 1 and sent[0][2] == result["text"] and sent[0][3].startswith("voice-")
        assert runs.stopped == []
        metrics = session.history[-1]
        assert metrics["run_id"] == "run-1"
        assert metrics["speech_end_to_first_audio_ready_ms"] >= 0
        assert "speech_end_to_transcript_ms" in metrics
        await manager.close_all()
    asyncio.run(scenario())


def test_interrupt_stops_speech_but_not_the_turn():
    async def scenario():
        frames = [delta(f"Frase número {i}. ") for i in range(12)] + [DONE]
        manager, runs, sent = make_manager(frames=frames, delay=0.05)
        session = await manager.create("luis", "sid-1")
        await session.send_text("cuéntame algo")
        await collect_until(session, "audio")
        await session.interrupt("barge_in")
        before = len([e for e in session.events if e.type == "audio"])
        await collect_until(session, "turn", after=session.events[-1].seq)  # the run keeps going to the end
        await asyncio.sleep(0.2)
        after = len([e for e in session.events if e.type == "audio"])
        assert after == before
        assert runs.stopped == []
        assert any(e.type == "interrupted" for e in session.events)
        await session.cancel_turn()
        assert runs.stopped == ["sid-1"]
        await manager.close_all()
    asyncio.run(scenario())


def test_client_mode_only_transcribes_and_follows_the_run_the_client_starts():
    async def scenario():
        manager, runs, sent = make_manager()
        session = await manager.create("luis", "sid-1", send_mode="client")
        result = await session.submit_clip(vs.pcm16_to_wav(pcm(1.0)))
        assert result["verdict"] == "ok" and "turn" not in result
        assert sent == []
        runs.active["sid-1"] = FakeRun("studio-run", [delta("Respuesta desde Studio."), DONE])
        events = await collect_until(session, "speech_end")
        assert [e.data["text"] for e in events if e.type == "audio"] == ["Respuesta desde Studio."]
        assert session.history[-1]["run_id"] == "studio-run"
        assert "speech_end_to_transcript_ms" in session.history[-1]
        await manager.close_all()
    asyncio.run(scenario())


def test_a_run_already_running_when_voice_opens_is_not_read_aloud():
    async def scenario():
        runs = FakeRuns()
        runs.active["sid-1"] = FakeRun("old-run", [delta("Respuesta vieja."), DONE], delay=0.05)
        manager, _, _ = make_manager(runs=runs)
        session = await manager.create("luis", "sid-1")
        await asyncio.sleep(0.4)
        assert not [e for e in session.events if e.type in ("audio", "turn")]
        follower = await manager.create("luis", "sid-1", follow_current=True)
        assert follower._baseline_run == ""
        await manager.close_all()
    asyncio.run(scenario())


def test_rewritten_answer_does_not_repeat_sentences():
    async def scenario():
        frames = [delta("Primera idea. Segunda idea. "), replace(""), delta("Primera idea. Tercera idea."), DONE]
        manager, runs, sent = make_manager(frames=frames, delay=0.02)
        session = await manager.create("luis", "sid-1")
        await session.send_text("hola")
        events = await collect_until(session, "speech_end")
        # "Segunda idea." was still held back (unfinished) when the answer was rewritten without it.
        assert [e.data["text"] for e in events if e.type == "audio"] == ["Primera idea.", "Tercera idea."]
        await manager.close_all()
    asyncio.run(scenario())


def test_stop_phrase_interrupts_and_sends_nothing():
    async def scenario():
        manager, runs, sent = make_manager(stt=FakeSTT(text="¡Para!"))
        session = await manager.create("luis", "sid-1")
        result = await session.submit_clip(vs.pcm16_to_wav(pcm(0.5)))
        assert result["verdict"] == "stop_phrase" and sent == []
        assert any(e.type == "interrupted" for e in session.events)
        manager.stt.text = "Subtítulos realizados por la comunidad de Amara.org"
        assert (await session.submit_clip(vs.pcm16_to_wav(pcm(0.5))))["verdict"] == "hallucination"
        await manager.close_all()
    asyncio.run(scenario())


def test_wake_word_mode_drops_utterances_without_it():
    async def scenario():
        manager, runs, sent = make_manager(stt=FakeSTT(text="abre el correo"))
        session = await manager.create("luis", "sid-1", wake_word=True)
        assert (await session.submit_clip(vs.pcm16_to_wav(pcm(0.6))))["verdict"] == "no_wake_word"
        manager.stt.text = "Faustus abre el correo"
        result = await session.submit_clip(vs.pcm16_to_wav(pcm(0.6)))
        assert result["text"] == "abre el correo" and sent[-1][2] == "abre el correo"
        await manager.close_all()
    asyncio.run(scenario())


def test_streamed_pcm_gives_partials_then_one_final_with_preroll():
    async def scenario():
        stt = FakeSTT(text=lambda audio: f"{len(audio)} bytes")
        manager, runs, sent = make_manager(stt=stt)
        session = await manager.create("luis", "sid-1", send_mode="client")
        assert not manager.partials_affordable()  # unknown recognizer speed: no guesses that could delay the final
        manager.note_stt(100, 64000)               # 2 s transcribed in 0.1 s
        assert manager.partials_affordable()
        session.begin_utterance(16000, preroll_ms=500)
        for _ in range(4):
            await session.add_audio(pcm(0.5))
            session._partial_at -= 2  # pretend a second passed between frames
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        result = await session.end_utterance()
        partials = [e for e in session.events if e.type == "transcript" and not e.data["final"]]
        assert partials, "no partial transcript while speaking"
        assert result["verdict"] == "ok" and result["text"] == f"{len(vs.pcm16_to_wav(pcm(2.0)))} bytes"
        assert session.metrics["preroll_ms"] == 500
        await manager.close_all()
    asyncio.run(scenario())


def test_sessions_are_bounded_per_owner_and_close_cleanly():
    async def scenario():
        manager, _, _ = make_manager()
        created = [await manager.create("luis", "sid-1") for _ in range(vs.MAX_SESSIONS_PER_OWNER + 1)]
        open_ids = {s["id"] for s in manager.list("luis")}
        assert len(open_ids) == vs.MAX_SESSIONS_PER_OWNER and created[0].id not in open_ids
        with pytest.raises(KeyError):
            manager.get(created[1].id, "otra")
        assert await manager.close(created[1].id)
        with pytest.raises(KeyError):
            manager.get(created[1].id)
        await manager.close_all()
    asyncio.run(scenario())


def test_capabilities_never_load_a_model():
    manager, _, _ = make_manager()
    caps = manager.capabilities()
    assert caps["models_loaded_by_discovery"] is False
    assert caps["streaming"]["interrupt_cancels_turn"] is False
    assert "pcm16" in caps["streaming"]["input_formats"]
    assert caps["streaming"]["send_modes"] == ["server", "client"]


def test_speculative_transcript_is_used_unless_withdrawn():
    async def scenario():
        stt = FakeSTT(text=lambda audio: f"{len(audio)} bytes")
        manager, runs, sent = make_manager(stt=stt)
        session = await manager.create("luis", "sid-1", send_mode="client", partials=False)
        session.begin_utterance(16000)
        await session.add_audio(pcm(1.0))
        assert session.speculate()
        await session.add_audio(pcm(0.5))  # the trailing silence the client waits for
        result = await session.end_utterance()
        assert result["text"] == f"{len(vs.pcm16_to_wav(pcm(1.0)))} bytes"
        assert session.metrics["speculative"] is True
        session.begin_utterance(16000)
        await session.add_audio(pcm(1.0))
        session.speculate()
        session.speculate_cancel()  # the person spoke again
        await session.add_audio(pcm(1.0))
        result = await session.end_utterance()
        assert result["text"] == f"{len(vs.pcm16_to_wav(pcm(2.0)))} bytes"
        assert session.metrics.get("speculations_withdrawn") == 1 and not session.metrics.get("speculative")
        await manager.close_all()
    asyncio.run(scenario())


def test_first_chunk_can_end_at_a_clause_and_pictographs_are_not_spoken():
    buf = vs.SentenceBuffer(first_clause=40)
    text = "La población de Tokio depende de cómo definas la ciudad, porque hay tres cifras distintas"
    assert buf.take(text) == ["La población de Tokio depende de cómo definas la ciudad,"]
    assert buf.take(text + ". Fin.", final=True) == ["porque hay tres cifras distintas.", "Fin."]
    assert vs.SentenceBuffer().take("¡De nada! 😊", final=True) == ["¡De nada!"]
    assert vs.SentenceBuffer(first_clause=40).take("Son 1,200 euros en total y algo más") == []


def test_slow_recognizer_turns_partials_off():
    manager, _, _ = make_manager()
    manager.note_stt(4000, 96000)  # 3 s of audio took 4 s
    assert not manager.partials_affordable()


def test_a_session_left_by_its_websocket_closes_after_the_grace_period(monkeypatch):
    async def scenario():
        manager, _, _ = make_manager()
        session = await manager.create("luis", "sid-1", send_mode="client")
        session.attach()
        session.detach()
        monkeypatch.setattr(vs, "DETACHED_TTL_S", 0.1)
        await asyncio.sleep(0.4)
        assert session.closed and manager.list("luis") == []
        rest = await manager.create("luis", "sid-1")  # REST/MCP sessions never had a socket: not closed by this
        await asyncio.sleep(0.3)
        assert not rest.closed
        await manager.close_all()
    asyncio.run(scenario())


def test_client_mode_timings_belong_to_the_run_the_transcript_started(monkeypatch):
    async def scenario():
        manager, runs, sent = make_manager()
        session = await manager.create("luis", "sid-1", send_mode="client")
        emit = session.emit

        async def eager_client(kind, data=None, **extra):
            event = await emit(kind, data, **extra)
            if kind == "transcript" and (data or {}).get("final"):
                # The client sends the moment it hears the transcript; its run starts before emit returns.
                runs.active["sid-1"] = FakeRun("studio-run", [delta("Respuesta."), DONE])
                await asyncio.sleep(0.2)
            return event

        monkeypatch.setattr(session, "emit", eager_client)
        await session.submit_clip(vs.pcm16_to_wav(pcm(1.0)))
        await collect_until(session, "speech_end")
        assert session.history[-1]["run_id"] == "studio-run"
        assert session.history[-1]["utterance"] == 1

        # A transcript the client discarded must not lend its timings to a much later turn.
        monkeypatch.setattr(session, "emit", emit)
        await session.submit_clip(vs.pcm16_to_wav(pcm(1.0)))
        session._pending_metrics_at -= vs.PENDING_METRICS_TTL_S + 1
        cursor = session.events[-1].seq
        runs.active["sid-1"] = FakeRun("later-run", [delta("Otra."), DONE])
        await collect_until(session, "speech_end", after=cursor)
        assert session.history[-1]["run_id"] == "later-run"
        assert session.history[-1].get("utterance") is None and session.history[-1]["source"] == "chat"
        await manager.close_all()
    asyncio.run(scenario())
