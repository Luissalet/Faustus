"""Isolated TTS worker process (src/media_tts_worker.py) and the parent-side
runner (services/tts/piper_voice.py::_run_worker / _synthesize_python).

Real subprocesses, no network, no real TTS engine: the worker's "test_fake"
engine (only registered when FAUSTUS_TTS_TEST_ENGINE=1 is set in the child's
own environment) stands in for piper so these tests exercise the real
process-spawn / stdin-JSON / timeout-kill machinery end to end.
"""
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from services.tts import piper_voice

WORKER = str(Path(__file__).resolve().parents[1] / "src" / "media_tts_worker.py")


def _env_with_fake_engine():
    env = dict(os.environ)
    env["FAUSTUS_TTS_TEST_ENGINE"] = "1"
    return env


def _run_worker_raw(engine, payload, output_path, timeout=10, env=None):
    return subprocess.run(
        [sys.executable, WORKER, engine, str(output_path)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True, timeout=timeout, env=env,
    )


# ── worker protocol, via the fake engine ───────────────────────────────────

def test_worker_fake_engine_writes_wav(tmp_path):
    out = tmp_path / "out.wav"
    result = _run_worker_raw("test_fake", {"mode": "ok", "frames": 200}, out,
                              env=_env_with_fake_engine())
    assert result.returncode == 0
    reply = json.loads(result.stdout)
    assert reply["ok"] is True
    assert reply["bytes"] > 0
    with wave.open(str(out), "rb") as wf:
        assert wf.getnframes() == 200


def test_worker_fake_engine_not_registered_without_env(tmp_path):
    """The fake engine must never be reachable in a normal (production)
    child environment — only when the test opts in explicitly."""
    out = tmp_path / "out.wav"
    env = dict(os.environ)
    env.pop("FAUSTUS_TTS_TEST_ENGINE", None)
    result = _run_worker_raw("test_fake", {"mode": "ok"}, out, env=env)
    reply = json.loads(result.stdout)
    assert reply["ok"] is False
    assert reply["error_code"] == "unknown_engine"


def test_worker_fake_engine_failure_reports_synth_failed(tmp_path):
    out = tmp_path / "out.wav"
    result = _run_worker_raw("test_fake", {"mode": "fail", "message": "boom"}, out,
                              env=_env_with_fake_engine())
    assert result.returncode == 1
    reply = json.loads(result.stdout)
    assert reply["ok"] is False
    assert reply["error_code"] == "synth_failed"
    assert "boom" in reply["message"]


def test_worker_fake_engine_not_installed(tmp_path):
    out = tmp_path / "out.wav"
    result = _run_worker_raw("test_fake", {"mode": "not_installed"}, out,
                              env=_env_with_fake_engine())
    assert result.returncode == 1
    reply = json.loads(result.stdout)
    assert reply["ok"] is False
    assert reply["error_code"] == "engine_not_installed"


def test_worker_unknown_engine(tmp_path):
    out = tmp_path / "out.wav"
    result = _run_worker_raw("not-a-real-engine", {}, out)
    reply = json.loads(result.stdout)
    assert reply["ok"] is False
    assert reply["error_code"] == "unknown_engine"


def test_worker_bad_args_when_missing_output_path():
    result = subprocess.run([sys.executable, WORKER, "piper"], input=b"{}",
                             capture_output=True, timeout=10)
    assert result.returncode == 2
    reply = json.loads(result.stdout)
    assert reply["error_code"] == "bad_args"


def test_worker_bad_payload_is_reported_not_raised(tmp_path):
    out = tmp_path / "out.wav"
    result = subprocess.run([sys.executable, WORKER, "piper", str(out)], input=b"not json",
                             capture_output=True, timeout=10)
    assert result.returncode == 2
    reply = json.loads(result.stdout)
    assert reply["error_code"] == "bad_payload"


# ── piper engine: not installed in this environment (real ImportError) ────

def test_worker_piper_not_installed(tmp_path):
    """piper-tts is not installed in this test environment: the worker must
    report a clean engine_not_installed, not an unhandled traceback."""
    out = tmp_path / "out.wav"
    result = _run_worker_raw("piper", {"text": "hola", "voice_path": str(tmp_path / "missing.onnx")}, out)
    assert result.returncode == 1
    reply = json.loads(result.stdout)
    assert reply["ok"] is False
    assert reply["error_code"] == "engine_not_installed"


# ── parent side: services/tts/piper_voice._run_worker ──────────────────────

def test_run_worker_success(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_TTS_TEST_ENGINE", "1")
    audio = piper_voice._run_worker("test_fake", {"mode": "ok", "frames": 100}, timeout=10)
    assert audio[:4] == b"RIFF"


def test_run_worker_raises_on_engine_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_TTS_TEST_ENGINE", "1")
    with pytest.raises(RuntimeError, match="synth_failed"):
        piper_voice._run_worker("test_fake", {"mode": "fail", "message": "kaboom"}, timeout=10)


def test_run_worker_timeout_kills_child(monkeypatch):
    """The child sleeps far longer than the timeout; the parent must return
    (having killed it) close to the timeout, not wait out the sleep."""
    monkeypatch.setenv("FAUSTUS_TTS_TEST_ENGINE", "1")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        piper_voice._run_worker("test_fake", {"mode": "sleep", "seconds": 30}, timeout=1)
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"parent waited {elapsed:.1f}s past a 1s timeout — child was not killed promptly"


def test_synthesize_python_runtime_uses_isolated_worker(monkeypatch, tmp_path):
    """`_synthesize_python` (invoked by `synthesize()` for the pip-package
    runtime) must go through the isolated worker, not import `piper` here."""
    monkeypatch.setenv("FAUSTUS_TTS_TEST_ENGINE", "1")
    monkeypatch.setattr(piper_voice, "_run_worker",
                         lambda engine, payload, timeout=piper_voice.WORKER_TIMEOUT_S: (
                             b"RIFFfakewav" if engine == "piper" and payload.get("voice_path") else (_ for _ in ()).throw(AssertionError("bad call"))
                         ))
    audio = piper_voice._synthesize_python("hola", Path("/some/voice.onnx"), 1.0)
    assert audio == b"RIFFfakewav"


def test_synthesize_never_raises_when_worker_fails(monkeypatch, tmp_path):
    """End-to-end through the public `synthesize()`: a failing/timing-out
    worker must degrade to None, never propagate an exception up to the
    TTS route — 'parent never crashes the server'."""
    onnx_path = tmp_path / "voices" / "es_ES-davefx-medium.onnx"
    onnx_path.parent.mkdir(parents=True)
    onnx_path.write_bytes(b"FAKE")
    json_path = tmp_path / "voices" / "es_ES-davefx-medium.onnx.json"
    json_path.write_text(json.dumps({"language": {"code": "es_ES"}}))

    monkeypatch.setattr(piper_voice, "VOICES_DIR", tmp_path / "voices")
    monkeypatch.setattr(piper_voice, "active_runtime", lambda: "python")

    def _boom(engine, payload, timeout=piper_voice.WORKER_TIMEOUT_S):
        raise RuntimeError("TTS worker (piper) failed [engine_not_installed]: nope")

    monkeypatch.setattr(piper_voice, "_run_worker", _boom)
    result = piper_voice.synthesize("hola mundo", voice="es_ES-davefx-medium", language="es")
    assert result is None


# ── voice path resolution (services/tts/piper_voice, sanity re-check) ─────

def test_voice_paths_resolve_under_voices_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(piper_voice, "VOICES_DIR", tmp_path / "voices")
    onnx_path, json_path = piper_voice.voice_paths("es_ES-davefx-medium")
    assert onnx_path == tmp_path / "voices" / "es_ES-davefx-medium.onnx"
    assert json_path == tmp_path / "voices" / "es_ES-davefx-medium.onnx.json"


# ── download URL construction (no network) ─────────────────────────────────

def test_download_url_layout_for_spanish_voice():
    onnx_url, json_url = piper_voice.voice_urls("es_ES-davefx-medium")
    assert onnx_url == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
        "es/es_ES/davefx/medium/es_ES-davefx-medium.onnx"
    )
    assert json_url == onnx_url + ".json"


def test_download_url_layout_for_english_voice():
    onnx_url, _ = piper_voice.voice_urls("en_US-lessac-medium")
    assert onnx_url.endswith("en/en_US/lessac/medium/en_US-lessac-medium.onnx")


def test_worker_timeout_is_read_at_call_time(monkeypatch):
    from services.tts import piper_voice as pv
    seen = {}

    def fake_run(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise RuntimeError("stop")
    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    monkeypatch.setattr(pv, "WORKER_TIMEOUT_S", 0.5)
    try:
        pv._run_worker("piper", {})
    except Exception:
        pass
    assert seen["timeout"] == 0.5
