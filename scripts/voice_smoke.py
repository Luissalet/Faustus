"""Explicit offline voice smoke test (first run downloads Whisper base).

No microphone, private recordings, settings changes or cloud inference.
Run with the Faustus venv after installing requirements-voice.txt on Windows.
"""
import io
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.tts.system_voice import synthesize_system
from faster_whisper import WhisperModel


def main():
    model = WhisperModel("base", device="cpu", compute_type="int8")
    for language, sentence, expected in [
        ("es-ES", "Hola Faustus. Crea una lista con tres ideas para un proyecto.", "tres ideas"),
        ("en-US", "Hello Faustus. Create a list with three ideas for a project.", "three ideas"),
    ]:
        start = time.monotonic()
        audio = synthesize_system(sentence, language=language)
        print(f"System TTS {language}: {len(audio)} bytes, {time.monotonic() - start:.2f}s")
        start = time.monotonic()
        segments, info = model.transcribe(io.BytesIO(audio))
        text = " ".join(segment.text.strip() for segment in segments)
        print(f"Whisper CPU: {time.monotonic() - start:.2f}s; detected={info.language}")
        print(text)
        assert expected in text.lower() and info.language == language[:2], "Check installed voices and Whisper model"
    print("PASS: English and Spanish automatic detection (audio never saved)")


if __name__ == "__main__":
    main()
