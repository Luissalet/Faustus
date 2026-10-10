"""The orb's mouth and the karaoke subtitle, pure TypeScript part (Agora #251).

`tests/voice_visemes.check.mjs` bundles `studio/src/voice/visemes.ts` with the
repo's esbuild. Here it also reads the marks the Python service produced for a
real sentence, so the two sides of the contract are checked against each other.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from services.tts.marks import build_marks

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "tests" / "voice_visemes.check.mjs"
pytestmark = pytest.mark.skipif(
    not (shutil.which("node") and (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()),
    reason="node + node_modules/esbuild needed",
)


def _run(env=None):
    import os
    return subprocess.run(["node", str(_CHECK)], capture_output=True, text=True, encoding="utf-8",
                          cwd=str(_REPO), timeout=120, env={**os.environ, **(env or {})})


def test_viseme_helpers_check_passes():
    proc = _run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout and "FAIL" not in proc.stdout


def test_the_browser_accepts_the_marks_the_server_makes(tmp_path):
    spec = importlib.util.spec_from_file_location("fake_tts_marks", _REPO / "tests" / "fake_tts_marks.py")
    fake = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake)
    import io
    import struct
    import wave
    text = "Hola, ¿qué tal? Mamá me ha dado un poco de pan."
    samples, _ = fake.render(text, "es")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fake.RATE)
        w.writeframes(b"".join(struct.pack("<h", int(v * 32767)) for v in samples))
    (tmp_path / "marks.json").write_text(json.dumps(build_marks(text, buf.getvalue(), "es")), encoding="utf-8")
    (tmp_path / "text.txt").write_text(text, encoding="utf-8")
    proc = _run({"MARKS_JSON": str(tmp_path / "marks.json"), "MARKS_TEXT": str(tmp_path / "text.txt")})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "marks produced by the Python service are accepted unchanged" in proc.stdout


def test_voice_panel_is_wired_to_the_marks():
    src = _REPO / "studio" / "src" / "voice"
    panel = (src / "VoicePanel.tsx").read_text(encoding="utf-8")
    orb = (src / "VoiceOrb.tsx").read_text(encoding="utf-8")
    audio = (src / "audio.ts").read_text(encoding="utf-8")
    assert "<VoiceSubtitle playback={playback} />" in panel and "playback={playback}" in panel
    assert "visemeAt" in orb and "orbShape(mouth, reduced.matches)" in orb   # reduced motion goes through the same door
    assert "marks: true" in audio and "parseMarks(body.marks, clean)" in audio
