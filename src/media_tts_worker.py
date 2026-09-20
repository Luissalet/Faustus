"""Text-to-speech synthesis, run in a disposable child process.

Mirrors `media_probe_worker.py` / `media_transform_worker.py`'s discipline:
no engine library is imported by the parent server process, only by this
child — a hang, a segfault, or an unexpected `sys.exit()` inside a TTS
engine kills the child, never the server. The parent (`services/tts/
piper_voice.py::_run_worker`) spawns this with a hard timeout and kills the
child on expiry or cancellation; nothing here is trusted to self-terminate.

Protocol: argv = [engine_name, output_wav_path]; a single JSON object is
read from stdin, e.g. {"text": "...", "voice_path": "...", "length_scale": 1.0}.
On success the engine writes a WAV file to output_wav_path and this prints
exactly one JSON line to stdout: {"ok": true, "bytes": N}. On failure it
prints {"ok": false, "error_code": "...", "message": "..."} and exits 1 (2
for a malformed invocation, before any engine ran).

Adding a new engine: write one `_synth_<name>(payload, output_path)`
function that imports its library lazily (inside the function body, never
at module scope) and register it in ENGINES. `payload` is whatever the
parent chose to send; this worker never touches settings, the database or
the network itself — the parent resolves voice paths and passes them in.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, Dict


class EngineNotInstalled(Exception):
    """The requested engine's library, binary or voice file is missing —
    reported back to the parent as `error_code: engine_not_installed`
    instead of a generic synthesis failure."""


# ── engines (each imports its own library lazily, inside the function) ────

def _synth_piper(payload: Dict[str, Any], output_path: str) -> None:
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise EngineNotInstalled(f"piper-tts package not installed: {exc}") from exc

    voice_path = payload.get("voice_path") or ""
    if not voice_path or not os.path.isfile(voice_path):
        raise EngineNotInstalled(f"Piper voice file not found: {voice_path!r}")

    try:
        pv = PiperVoice.load(voice_path)
    except Exception as exc:  # noqa: BLE001 — any load failure means "not usable"
        raise EngineNotInstalled(f"Piper voice failed to load: {exc}") from exc

    text = payload.get("text") or ""
    length_scale = float(payload.get("length_scale") or 1.0)

    import wave
    with wave.open(output_path, "wb") as wf:
        if hasattr(pv, "synthesize_wav"):
            # Current piper-tts (>=1.3): synthesize() only yields audio
            # chunks; synthesize_wav() writes the WAV file and takes a
            # SynthesisConfig rather than a length_scale kwarg.
            try:
                from piper.config import SynthesisConfig
                pv.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=length_scale))
            except ImportError:
                pv.synthesize_wav(text, wf)
        else:
            # Older piper-tts: synthesize() itself writes to the wav file.
            try:
                pv.synthesize(text, wf, length_scale=length_scale)
            except TypeError:
                pv.synthesize(text, wf)


ENGINES: Dict[str, Callable[[Dict[str, Any], str], None]] = {
    "piper": _synth_piper,
}

# Test-only fake engine, registered ONLY when FAUSTUS_TTS_TEST_ENGINE=1 is
# explicitly set in the child's environment — never set in a normal run, so
# production code paths never see "test_fake" as a valid engine name.
if os.environ.get("FAUSTUS_TTS_TEST_ENGINE") == "1":
    def _synth_test_fake(payload: Dict[str, Any], output_path: str) -> None:
        mode = payload.get("mode", "ok")
        if mode == "sleep":
            time.sleep(float(payload.get("seconds", 5)))
        if mode == "fail":
            raise RuntimeError(payload.get("message", "fake engine failure"))
        if mode == "not_installed":
            raise EngineNotInstalled("fake engine reports itself not installed")
        import wave
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * int(payload.get("frames", 50)))

    ENGINES["test_fake"] = _synth_test_fake


def _reply(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=True))


def main(argv) -> int:
    if len(argv) != 3:
        _reply({"ok": False, "error_code": "bad_args",
                "message": "usage: media_tts_worker.py <engine> <output_wav_path>"})
        return 2

    engine_name, output_path = argv[1], argv[2]
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except (ValueError, UnicodeError) as exc:
        _reply({"ok": False, "error_code": "bad_payload", "message": str(exc)})
        return 2

    engine_fn = ENGINES.get(engine_name)
    if engine_fn is None:
        _reply({"ok": False, "error_code": "unknown_engine",
                "message": f"no TTS engine registered: {engine_name!r}"})
        return 2

    try:
        engine_fn(payload, output_path)
    except EngineNotInstalled as exc:
        _reply({"ok": False, "error_code": "engine_not_installed", "message": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001 — never let a raw traceback leak to the parent
        _reply({"ok": False, "error_code": "synth_failed",
                "message": f"{type(exc).__name__}: {exc}"[:500]})
        return 1

    try:
        size = os.path.getsize(output_path)
    except OSError:
        size = 0
    _reply({"ok": True, "bytes": size})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
