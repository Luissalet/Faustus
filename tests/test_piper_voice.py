"""Piper TTS provider: voice path derivation, listing, fake-runtime synthesis
and capabilities shape. No network here — every fetch is monkeypatched."""
import json
import wave
import io

import pytest

from services.tts import piper_voice
from services.tts.tts_service import TTSService


@pytest.fixture(autouse=True)
def _isolated_piper_dir(tmp_path, monkeypatch):
    """Point every module-level dir at a scratch tree so tests never touch
    the real data/tts/piper directory."""
    voices = tmp_path / "voices"
    binp = tmp_path / "bin"
    monkeypatch.setattr(piper_voice, "PIPER_DIR", tmp_path)
    monkeypatch.setattr(piper_voice, "VOICES_DIR", voices)
    monkeypatch.setattr(piper_voice, "BIN_DIR", binp)
    return tmp_path


def _install_fake_voice(name: str, language: str = "es_ES", quality: str = "medium"):
    piper_voice._ensure_dirs()
    onnx_path, json_path = piper_voice.voice_paths(name)
    onnx_path.write_bytes(b"FAKE-ONNX")
    json_path.write_text(json.dumps({"language": {"code": language}, "audio": {"quality": quality}}))
    return onnx_path, json_path


# ── path derivation ──────────────────────────────────────────────────────

def test_voice_urls_derive_repo_path():
    onnx_url, json_url = piper_voice.voice_urls("es_ES-davefx-medium")
    assert onnx_url == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
        "es/es_ES/davefx/medium/es_ES-davefx-medium.onnx"
    )
    assert json_url == onnx_url + ".json"


def test_voice_urls_handle_dashed_voice_id():
    onnx_url, _ = piper_voice.voice_urls("es_ES-mls_10246-low")
    assert onnx_url.endswith("es/es_ES/mls_10246/low/es_ES-mls_10246-low.onnx")


def test_voice_urls_rejects_malformed_name():
    with pytest.raises(ValueError):
        piper_voice.voice_urls("invalidname")


def test_voice_paths_rejects_path_traversal():
    with pytest.raises(ValueError):
        piper_voice.voice_paths("../../etc/passwd")


# ── listing ──────────────────────────────────────────────────────────────

def test_list_voices_empty_when_no_dir():
    assert piper_voice.list_voices() == []


def test_list_voices_scans_installed_pairs():
    _install_fake_voice("es_ES-davefx-medium", language="es_ES", quality="medium")
    _install_fake_voice("en_US-amy-medium", language="en_US", quality="medium")
    voices = {v["name"]: v for v in piper_voice.list_voices()}
    assert set(voices) == {"es_ES-davefx-medium", "en_US-amy-medium"}
    assert voices["es_ES-davefx-medium"]["language"] == "es_ES"
    assert voices["es_ES-davefx-medium"]["quality"] == "medium"


def test_catalogue_voices_flags_installed():
    _install_fake_voice("es_ES-davefx-medium")
    catalogue = {v["name"]: v for v in piper_voice.catalogue_voices()}
    assert catalogue["es_ES-davefx-medium"]["installed"] is True
    assert catalogue["en_US-lessac-medium"]["installed"] is False
    # Every catalogue voice must be a real, download-able name.
    for name in piper_voice.CATALOGUE:
        piper_voice.voice_urls(name)  # must not raise


def test_select_voice_language_aware():
    _install_fake_voice("es_ES-davefx-medium", language="es_ES")
    _install_fake_voice("en_US-amy-medium", language="en_US")
    # Configured voice matches the requested language: keep it.
    assert piper_voice.select_voice("es_ES-davefx-medium", "es") == "es_ES-davefx-medium"
    # Configured voice does NOT match the requested language: switch to the
    # first installed voice for that language.
    assert piper_voice.select_voice("es_ES-davefx-medium", "en") == "en_US-amy-medium"
    # No language hint: keep the configured voice.
    assert piper_voice.select_voice("es_ES-davefx-medium", "") == "es_ES-davefx-medium"
    # Unknown configured voice, no language: falls back to first installed.
    assert piper_voice.select_voice("nonexistent", "") in {"es_ES-davefx-medium", "en_US-amy-medium"}


def test_select_voice_no_voices_installed():
    assert piper_voice.select_voice("anything", "es") is None


# ── downloads (fake fetch, no network) ──────────────────────────────────

def test_download_voice_writes_both_files():
    calls = []

    def fetch(url):
        calls.append(url)
        return b'{"language": {"code": "es_ES"}}' if url.endswith(".json") else b"FAKE-ONNX-BYTES"

    piper_voice.download_voice("es_ES-davefx-medium", fetch=fetch)
    onnx_path, json_path = piper_voice.voice_paths("es_ES-davefx-medium")
    assert onnx_path.read_bytes() == b"FAKE-ONNX-BYTES"
    assert json.loads(json_path.read_text())["language"]["code"] == "es_ES"
    assert len(calls) == 2


def test_install_binary_extracts_executable(monkeypatch):
    import tarfile

    # Build a fake tar.gz with a "piper" executable inside, mimicking the
    # real release archive's layout.
    monkeypatch.setattr(piper_voice.os, "name", "posix", raising=False)
    monkeypatch.setitem(piper_voice.PIPER_RELEASE_ASSETS, "posix", "piper_linux_x86_64.tar.gz")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"#!/bin/sh\necho fake piper\n"
        info = tarfile.TarInfo(name="piper/piper")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        lib_data = b"fake-lib"
        lib_info = tarfile.TarInfo(name="piper/libpiper.so")
        lib_info.size = len(lib_data)
        tf.addfile(lib_info, io.BytesIO(lib_data))
    archive_bytes = buf.getvalue()

    def fetch(url):
        assert url.endswith("piper_linux_x86_64.tar.gz")
        return archive_bytes

    target = piper_voice.install_binary(fetch=fetch)
    assert target.exists()
    assert target.read_bytes() == b"#!/bin/sh\necho fake piper\n"
    assert (piper_voice.BIN_DIR / "libpiper.so").exists()


# ── runtime discovery ────────────────────────────────────────────────────

def test_active_runtime_none_when_nothing_present(monkeypatch):
    monkeypatch.setattr(piper_voice, "python_runtime_available", lambda: False)
    monkeypatch.setattr(piper_voice, "binary_runtime_available", lambda: False)
    assert piper_voice.active_runtime() is None


def test_active_runtime_prefers_python(monkeypatch):
    monkeypatch.setattr(piper_voice, "python_runtime_available", lambda: True)
    monkeypatch.setattr(piper_voice, "binary_runtime_available", lambda: True)
    assert piper_voice.active_runtime() == "python"


def test_active_runtime_falls_back_to_binary(monkeypatch):
    monkeypatch.setattr(piper_voice, "python_runtime_available", lambda: False)
    monkeypatch.setattr(piper_voice, "binary_runtime_available", lambda: True)
    assert piper_voice.active_runtime() == "binary"


# ── synthesis via a fake runtime ────────────────────────────────────────

def test_synthesize_returns_none_without_runtime(monkeypatch):
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: None)
    assert piper_voice.synthesize("hola", voice="es_ES-davefx-medium") is None


def test_synthesize_returns_none_without_voices(monkeypatch):
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: "python")
    assert piper_voice.synthesize("hola", voice="") is None


def test_synthesize_binary_runtime(monkeypatch, tmp_path):
    _install_fake_voice("es_ES-davefx-medium")
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: "binary")

    def fake_run_binary(text, onnx_path, length_scale):
        assert onnx_path.name == "es_ES-davefx-medium.onnx"
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 100)
        return buf.getvalue()

    monkeypatch.setattr(piper_voice, "_synthesize_binary", fake_run_binary)
    audio = piper_voice.synthesize("hola mundo", voice="es_ES-davefx-medium", speed=2.0, language="es")
    assert audio is not None and audio[:4] == b"RIFF"


def test_synthesize_python_runtime(monkeypatch):
    _install_fake_voice("en_US-amy-medium", language="en_US")
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: "python")

    def fake_run_python(text, onnx_path, length_scale):
        assert length_scale == pytest.approx(1 / 1.5)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 50)
        return buf.getvalue()

    monkeypatch.setattr(piper_voice, "_synthesize_python", fake_run_python)
    audio = piper_voice.synthesize("hello world", voice="en_US-amy-medium", speed=1.5)
    assert audio is not None and audio[:4] == b"RIFF"


# ── capabilities shape ──────────────────────────────────────────────────

def test_piper_status_shape(monkeypatch):
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: "python")
    _install_fake_voice("es_ES-davefx-medium")
    status = piper_voice.piper_status()
    assert status["dependency_installed"] is True
    assert status["runtime"] == "python"
    assert any(v["name"] == "es_ES-davefx-medium" for v in status["installed_voices"])
    assert any(v["name"] == "en_US-lessac-medium" for v in status["catalogue"])


# ── integration through TTSService ──────────────────────────────────────

def test_tts_service_dispatches_to_piper(monkeypatch, tmp_path):
    _install_fake_voice("es_ES-davefx-medium")
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: "python")
    monkeypatch.setattr(piper_voice, "_synthesize_python", lambda text, path, scale: b"RIFFfakewav")

    service = TTSService(cache_dir=str(tmp_path))
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "tts_enabled": True, "tts_provider": "piper", "tts_model": "", "tts_voice": "es_ES-davefx-medium", "tts_speed": "1",
    })
    audio = service.synthesize("hola", use_cache=False)
    assert audio == b"RIFFfakewav"


def test_tts_service_available_reflects_piper_state(monkeypatch, tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "tts_enabled": True, "tts_provider": "piper", "tts_model": "", "tts_voice": "", "tts_speed": "1",
    })
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: None)
    assert service.available is False
