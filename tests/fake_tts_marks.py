"""A fake speech engine for tests and QA (Agora #251): never a real voice.

    python fake_tts_marks.py INPUT_TXT OUTPUT_WAV [MARKS_JSON] [--lang en]

Each syllable of the text becomes a 150 ms tone, words are separated by a short
gap, commas by 380 ms of silence and full stops by 650 ms, so the audio has the
energy profile of real speech. The words' true times are known, and when
MARKS_JSON is given they are written there the way a real engine would report
them: {"words": [{"text", "start_ms", "end_ms", "phonemes"}]}. Pure standard library.
"""
import json
import math
import re
import struct
import sys
import wave

RATE = 22050
SYLLABLE_MS, IN_WORD_GAP_MS, WORD_GAP_MS = 150, 30, 70
LEAD_MS, TAIL_MS = 150, 200
SOFT_PAUSE_MS, HARD_PAUSE_MS = 380, 650
VOWELS = "aeiouáéíóúü"


def syllables(word, lang):
    groups = len(re.findall("[%s]+" % VOWELS, word.lower()))
    if lang == "en" and groups > 1 and word.lower().endswith("e") and not word.lower().endswith(("le", "ee")):
        groups -= 1   # a silent final e
    return max(1, groups)


def render(text, lang="es"):
    """(samples, truth): float samples in -1..1 and [{"text", "start_ms", "end_ms", "phonemes"}]."""
    samples = []
    truth = []

    def silence(ms):
        samples.extend([0.0] * int(RATE * ms / 1000))

    def tone(ms, freq):
        n = int(RATE * ms / 1000)
        samples.extend(0.55 * math.sin(math.pi * i / n) ** 2 * math.sin(2 * math.pi * freq * i / RATE) for i in range(n))

    silence(LEAD_MS)
    tokens = [m.group(0) for m in re.finditer(r"\S+", text) if any(c.isalnum() for c in m.group(0))]
    for index, token in enumerate(tokens):
        start = len(samples) * 1000 / RATE
        word = token.strip(".,;:!?¿¡…\"'()")
        count = syllables(word, lang)
        for s in range(count):
            tone(SYLLABLE_MS, 130 + 25 * ((index + s) % 5))
            if s < count - 1:
                silence(IN_WORD_GAP_MS)
        end = len(samples) * 1000 / RATE
        phonemes = ["a" if c in "aáà" else "e" if c in "eé" else "i" if c in "ií" else "o" if c in "oó" else "u" if c in "uúü" else "t"
                    for c in word.lower() if c.isalpha()]
        truth.append({"text": word, "start_ms": int(start), "end_ms": int(end), "phonemes": phonemes})
        tail = token[-1:]
        silence(HARD_PAUSE_MS if tail in ".!?…" else SOFT_PAUSE_MS if tail in ",;:" else WORD_GAP_MS)
    silence(TAIL_MS)
    return samples, truth


def write_wav(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767)) for v in samples))


if __name__ == "__main__":
    argv = sys.argv[1:]
    lang = "es"
    if "--lang" in argv:
        k = argv.index("--lang")
        lang = argv[k + 1]
        del argv[k:k + 2]
    text = open(argv[0], encoding="utf-8").read()
    samples, truth = render(text, lang)
    write_wav(argv[1], samples)
    if len(argv) > 2:
        with open(argv[2], "w", encoding="utf-8") as f:
            json.dump({"words": truth}, f)
