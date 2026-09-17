"""Command-template TTS/STT providers: placeholder substitution, timeouts,
non-zero exit and stdout-vs-{output_path} transcript capture — all against a
fake command (`python3 -c ...`), never a real third-party executable."""
import sys
import wave
import io

from services.tts.command_voice import synthesize_command
from services.stt.command_stt import transcribe_command
from services.tts.tts_service import TTSService
from services.stt.stt_service import STTService


def _wav_writer_command(output_placeholder="{output_path}"):
    """A command that writes a tiny valid WAV to {output_path}, echoing the
    voice/speed/language it was given into the file name isn't needed — the
    test asserts on the WAV bytes and on argv it received via a marker file."""
    script = (
        "import sys,wave;"
        "p=[a for a in sys.argv if a.startswith('--out=')][0].split('=',1)[1];"
        "w=wave.open(p,'wb');w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);"
        "w.writeframes(b'\\x00\\x00'*10);w.close()"
    )
    return f'{sys.executable} -c "{script}" --out={output_placeholder}'


# ── TTS ──────────────────────────────────────────────────────────────────

def test_synthesize_command_writes_and_reads_wav(tmp_path):
    template = _wav_writer_command()
    audio = synthesize_command("hola", template, voice="v1", speed=1.0, language="es")
    assert audio is not None and audio[:4] == b"RIFF"


def test_synthesize_command_substitutes_placeholders(tmp_path):
    marker = tmp_path / "marker.txt"
    script = (
        "import sys,wave;"
        "args=dict(a.split('=',1) for a in sys.argv[1:] if '=' in a);"
        f"open(r'{marker}', 'w').write(args.get('--voice','')+'|'+args.get('--speed','')+'|'+args.get('--lang',''));"
        "w=wave.open(args['--out'],'wb');w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\\x00\\x00');w.close()"
    )
    template = f'{sys.executable} -c "{script}" --voice={{voice}} --speed={{speed}} --lang={{language}} --out={{output_path}}'
    audio = synthesize_command("hola", template, voice="es_ES-davefx-medium", speed=1.25, language="es")
    assert audio is not None
    assert marker.read_text() == "es_ES-davefx-medium|1.25|es"


def test_synthesize_command_empty_template_returns_none():
    assert synthesize_command("hola", "", voice="v", speed=1.0) is None


def test_synthesize_command_missing_output_placeholder_returns_none():
    template = f"{sys.executable} -c \"pass\""
    assert synthesize_command("hola", template, voice="v", speed=1.0) is None


def test_synthesize_command_nonzero_exit_returns_none():
    template = f'{sys.executable} -c "import sys;sys.exit(1)" --out={{output_path}}'
    assert synthesize_command("hola", template, voice="v", speed=1.0) is None


def test_synthesize_command_timeout_returns_none(monkeypatch):
    import services.tts.command_voice as cv
    monkeypatch.setattr(cv, "COMMAND_TIMEOUT_S", 1)
    template = f'{sys.executable} -c "import time;time.sleep(5)" --out={{output_path}}'
    assert synthesize_command("hola", template, voice="v", speed=1.0) is None


def test_synthesize_command_cleans_up_temp_files(tmp_path, monkeypatch):
    import services.tts.command_voice as cv
    seen = {}
    orig_mkstemp = cv.tempfile.mkstemp

    def spy_mkstemp(*a, **kw):
        fd, name = orig_mkstemp(*a, **kw)
        seen.setdefault("paths", []).append(name)
        return fd, name

    monkeypatch.setattr(cv.tempfile, "mkstemp", spy_mkstemp)
    synthesize_command("hola", _wav_writer_command(), voice="v", speed=1.0)
    for p in seen["paths"]:
        assert not cv.Path(p).exists()


# ── TTSService dispatch ─────────────────────────────────────────────────

def test_tts_service_dispatches_to_command(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    template = _wav_writer_command()
    service._load_settings = lambda: {
        "tts_enabled": True, "tts_provider": "command", "tts_model": "", "tts_voice": "v",
        "tts_speed": "1", "tts_command_template": template,
    }
    audio = service.synthesize("hola", use_cache=False)
    assert audio is not None and audio[:4] == b"RIFF"


def test_tts_service_available_false_without_template(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: {
        "tts_enabled": True, "tts_provider": "command", "tts_model": "", "tts_voice": "v",
        "tts_speed": "1", "tts_command_template": "",
    }
    assert service.available is False


# ── STT ──────────────────────────────────────────────────────────────────

def test_transcribe_command_reads_stdout():
    template = f'{sys.executable} -c "print(\'hola mundo\')" {{input_path}}'
    text = transcribe_command(b"fake-audio-bytes", template, language="es")
    assert text == "hola mundo"


def test_transcribe_command_reads_output_file():
    script = "import sys;open(sys.argv[-1],'w').write('transcribed text')"
    template = f'{sys.executable} -c "{script}" {{output_path}}'
    text = transcribe_command(b"fake-audio-bytes", template)
    assert text == "transcribed text"


def test_transcribe_command_passes_input_and_language(tmp_path):
    marker = tmp_path / "seen.txt"
    script = (
        "import sys;"
        f"open(r'{marker}','w').write(open(sys.argv[1],'rb').read().decode()+'|'+sys.argv[2])"
    )
    template = f'{sys.executable} -c "{script}" {{input_path}} {{language}}'
    transcribe_command(b"AUDIO", template, language="en")
    assert marker.read_text() == "AUDIO|en"


def test_transcribe_command_empty_template_returns_none():
    assert transcribe_command(b"x", "") is None


def test_transcribe_command_nonzero_exit_returns_none():
    template = f'{sys.executable} -c "import sys;sys.exit(2)"'
    assert transcribe_command(b"x", template) is None


def test_transcribe_command_timeout_returns_none(monkeypatch):
    import services.stt.command_stt as cs
    monkeypatch.setattr(cs, "COMMAND_TIMEOUT_S", 1)
    template = f'{sys.executable} -c "import time;time.sleep(5)"'
    assert transcribe_command(b"x", template) is None


# ── STTService dispatch ─────────────────────────────────────────────────

def test_stt_service_dispatches_to_command():
    service = STTService()
    template = f'{sys.executable} -c "print(\'hola\')"'
    service._load_settings = lambda: {
        "stt_enabled": True, "stt_provider": "command", "stt_model": "", "stt_language": "",
        "stt_device": "auto", "stt_command_template": template,
    }
    text = service.transcribe(b"fake-audio")
    assert text == "hola"


def test_stt_service_available_false_without_template():
    service = STTService()
    service._load_settings = lambda: {
        "stt_enabled": True, "stt_provider": "command", "stt_model": "", "stt_language": "",
        "stt_device": "auto", "stt_command_template": "",
    }
    assert service.available is False
