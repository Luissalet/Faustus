# services/tts/marks.py
"""Word timings and visemes for synthesized speech (Agora #251).

Voice mode drives its orb and its subtitle from this. Every speech provider
returns plain audio, so two sources feed the result:

* what the engine itself says (a command template may write a marks file, the
  local neural engine reports phonemes per chunk): passed through, after being
  validated against the text and the audio;
* otherwise an estimate made only from the audio and the text: the audio is
  cut into spoken stretches by its energy, and the words of the text are laid
  over those stretches in proportion to their syllables. No model, no GPU, no
  third-party package: it is deterministic and costs a few milliseconds.

The result (`build_marks`) is a JSON-ready dict::

    {"version": 1, "language": "es", "duration_ms": 2410, "timing": "audio",
     "words_source": "estimated" | "engine",
     "visemes_source": "estimated" | "engine",
     "viseme_set": ["rest", "AA", ...],
     "words": [{"i": 0, "text": "Hola", "start_char": 0, "end_char": 4,
                "start_ms": 40, "end_ms": 380}, ...],
     "visemes": [{"v": "AA", "start_ms": 40, "end_ms": 120}, ...]}

`start_char`/`end_char` are UTF-16 offsets into the exact text that was
synthesized, so a browser can slice the same string to highlight a word.
"""
from __future__ import annotations

import bisect
import difflib
import functools
import re
import struct
import sys
import unicodedata
from array import array
from typing import Any, Dict, List, Optional, Sequence, Tuple

MARKS_VERSION = 1
# The mouth shapes the front end knows how to draw. `rest` is a closed, neutral mouth.
VISEMES: Tuple[str, ...] = ("rest", "AA", "E", "I", "O", "U", "MBP", "FV", "L", "OTHER")
FRAME_MS = 10            # resolution of the energy profile
MIN_REST_MS = 40         # a gap shorter than this does not become a `rest`
PAUSE_FILL_FRAMES = 25   # silence shorter than 250 ms inside speech is a breath or a word gap, not a pause
MIN_BLIP_FRAMES = 3      # a burst shorter than 30 ms is noise
MIN_VALLEY_DEPTH = 0.12  # a dip of the envelope shallower than this (of the loudness) is not a syllable break
MIN_WORD_MS = 50
DEPTH_REWARD = 1.5
DIP_COUNT_COST = 0.5

_VOWEL_CLASS = {"a": "AA", "e": "E", "i": "I", "o": "O", "u": "U"}
_PLAIN = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "à": "a", "è": "e", "ì": "i", "ò": "o", "ù": "u",
          "â": "a", "ê": "e", "î": "i", "ô": "o", "û": "u", "ä": "a", "ë": "e", "ï": "i", "ö": "o"}
_ACCENTED_WEAK = set("íú")

_ES_WORDS = {"de", "la", "el", "que", "y", "en", "un", "los", "es", "se", "no", "por", "con", "una", "para", "las", "del",
             "al", "lo", "como", "más", "pero", "su", "le", "ya", "este", "esta", "hola", "muy", "sí", "qué", "está", "son"}
_EN_WORDS = {"the", "and", "is", "to", "of", "in", "that", "it", "you", "for", "with", "this", "are", "was", "on", "be",
             "have", "hello", "not", "can", "will", "what", "your", "from"}


# ── Language ──

def normalize_language(language: str, text: str = "") -> str:
    """'es' or 'en' (the two languages with letter rules). An empty or 'auto'
    language is guessed from the text; a language without rules gets the
    English ones, which only costs mouth-shape accuracy, never timing."""
    lang = (language or "").strip().lower().split("-")[0].split("_")[0]
    if lang in ("es", "en"):
        return lang
    if lang and lang != "auto":
        return "en"
    low = text.lower()
    words = re.findall(r"[a-záéíóúüñ]+", low)
    es = sum(1 for w in words if w in _ES_WORDS) + 2 * sum(1 for c in low if c in "ñ¿¡áéíóú")
    en = sum(1 for w in words if w in _EN_WORDS)
    return "es" if es > en else "en"


# ── Words and syllables ──

def _is_vowel(ch: str) -> bool:
    return _PLAIN.get(ch, ch) in _VOWEL_CLASS


def count_syllables(word: str, lang: str = "en") -> int:
    """Syllable nuclei of one word. Spanish: strong vowels (a, e, o, or an
    accented i/u) each carry a nucleus, an unaccented i/u next to another
    vowel is a diphthong. English: vowel groups, minus a silent final e."""
    w = word.lower()
    if not any(c.isalpha() for c in w):
        digits = sum(1 for c in w if c.isdigit())
        return max(1, int(round(digits * 1.5)))
    w = "".join(c for c in w if c.isalpha())
    if not w:
        return 1
    if lang == "es":
        w = re.sub(r"(?<=[qg])u(?=[eiéí])", "", w)   # que, gue: the u is silent
        count, run = 0, ""
        for i, ch in enumerate(w + " "):
            vowel = _is_vowel(ch) or (ch == "y" and i == len(w) - 1 and i > 0)
            if vowel:
                run += ch
                continue
            if run:
                strong = [c for c in run if _PLAIN.get(c, c) in "aeo" or c in _ACCENTED_WEAK]
                count += len(strong) if strong else 1
                run = ""
        return max(1, count)
    groups = re.findall(r"[aeiouy]+", w)
    count = len(groups)
    if count > 1 and w.endswith("e") and not w.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


_BREAK_CHARS = set(".,;:!?…—–。，；：！？")


def tokenize(text: str) -> List[Dict[str, Any]]:
    """Words of `text`: whitespace-separated tokens that hold a letter or a
    digit, without the punctuation at their edges. Offsets are UTF-16 units
    (what JavaScript's `slice` counts) next to the Python offsets `_s`/`_e`."""
    astral = any(ord(c) > 0xFFFF for c in text)
    out: List[Dict[str, Any]] = []
    for m in re.finditer(r"\S+", text):
        tok = m.group(0)
        first = next((k for k, c in enumerate(tok) if c.isalnum()), None)
        if first is None:
            if out and any(c in _BREAK_CHARS for c in tok):
                out[-1]["br"] = True
            continue
        last = max(k for k, c in enumerate(tok) if c.isalnum())
        s, e = m.start() + first, m.start() + last + 1
        if astral:
            us, ue = len(text[:s].encode("utf-16-le")) // 2, len(text[:e].encode("utf-16-le")) // 2
        else:
            us, ue = s, e
        out.append({"text": text[s:e], "_s": s, "_e": e, "start_char": us, "end_char": ue,
                    "br": any(c in _BREAK_CHARS for c in tok[last + 1:])})
    # A line break is a pause too.
    for a, b in zip(out, out[1:]):
        if "\n" in text[a["_e"]:b["_s"]]:
            a["br"] = True
    return out


# ── Letters and phonemes to visemes ──

def letter_units(word: str, lang: str = "en") -> List[str]:
    """Approximate mouth shapes of a written word, one per sound. Not a
    phonemizer: it handles what changes the lips (vowels, b/m/p, f/v, l) and
    the silent letters of Spanish and English that would otherwise move them."""
    w = unicodedata.normalize("NFC", word.lower())
    n = len(w)
    out: List[str] = []
    i = 0
    while i < n:
        c = w[i]
        base = _PLAIN.get(c, c)
        nxt = w[i + 1] if i + 1 < n else ""
        two = w[i:i + 2]
        if c.isdigit():
            out.append("OTHER")
        elif base in _VOWEL_CLASS:
            if lang == "en":
                if two in ("ee", "ea", "ie"):
                    out.append("I"); i += 2; continue
                if two == "oo":
                    out.append("U"); i += 2; continue
                if two in ("ai", "ay", "ei", "ey"):
                    out.append("E"); i += 2; continue
                if c == "e" and i == n - 1 and n > 3 and not _is_vowel(w[i - 1]) and w[i - 1] != "l":
                    i += 1; continue   # silent final e
            out.append(_VOWEL_CLASS[base])
        elif c == "y":
            if lang == "es":
                out.append("I" if i == n - 1 else "OTHER")
            else:
                out.append("I" if i > 0 and not _is_vowel(w[i - 1]) else "OTHER")
        elif c == "h":
            # Silent in Spanish; in English a lone h is breath, and in ch/sh/th/ph/gh/wh it was counted with its partner.
            if lang == "en" and not (i > 0 and w[i - 1] in "cstpgw"):
                out.append("OTHER")
        elif c == "q":
            out.append("OTHER")
            if nxt == "u":
                if lang == "en":
                    out.append("U")
                i += 2; continue
        elif c == "g" and lang == "es" and nxt == "u" and i + 2 < n and w[i + 2] in "eiéí":
            out.append("OTHER"); i += 2; continue
        elif c == "w":
            out.append("U")
        elif c in "bmp" or (c == "v" and lang == "es"):
            if two == "ph" and lang == "en":
                out.append("FV"); i += 2; continue
            out.append("MBP")
            if nxt == c:
                i += 1
        elif c == "f" or (c == "v" and lang == "en"):
            out.append("FV")
            if nxt == c:
                i += 1
        elif c == "l":
            if lang == "es" and nxt == "l":
                out.append("OTHER"); i += 2; continue   # ll is a palatal sound, not an l
            out.append("L")
            if nxt == "l":
                i += 1
        elif c.isalpha():
            out.append("OTHER")
            if nxt == c or two in ("ch", "sh", "th") or two == "ck":
                i += 1
        i += 1
    return out or ["OTHER"]


_ARPA = {"AA": "AA", "AE": "AA", "AH": "AA", "AX": "E", "AO": "O", "AW": "AA", "AY": "AA", "EH": "E", "ER": "E",
         "EY": "E", "IH": "I", "IY": "I", "OW": "O", "OY": "O", "UH": "U", "UW": "U", "M": "MBP", "B": "MBP",
         "P": "MBP", "F": "FV", "V": "FV", "L": "L", "W": "U", "SIL": "rest", "SP": "rest", "PAU": "rest"}
_ARPA_SINGLE = {"M", "B", "P", "F", "V", "L", "W"}
_IPA: Dict[str, str] = {}
for _chars, _cls in (("aɑɒæɐʌɶ", "AA"), ("eɛəɘɜɝɚɞɤε", "E"), ("iɪɨyʏ", "I"), ("oɔøœɵ", "O"),
                     ("uʊʉɯw", "U"), ("mbpɓʙ", "MBP"), ("fvʋβɸ", "FV"), ("lɫɭʎɬɮ", "L")):
    for _ch in _chars:
        _IPA[_ch] = _cls
# The neural engine writes its diphthongs as one capital letter.
_IPA_CAPITAL = {"A": "E", "I": "AA", "O": "O", "W": "AA", "Y": "O"}
_IPA_IGNORED = set("ˈˌːˑʼ‿.|ˀ ")


def _ipa_char(ch: str, capitals: bool) -> str:
    if ch in _IPA_IGNORED or unicodedata.combining(ch):
        return "rest"
    if capitals and ch in _IPA_CAPITAL:
        return _IPA_CAPITAL[ch]
    return _IPA.get(ch.lower(), "OTHER")


def phoneme_viseme(symbol: str, capitals: bool = False) -> str:
    """Mouth shape of one phoneme: an ARPAbet code (stress digits ignored) or an IPA symbol.
    `capitals`: a single capital letter is the neural engine's diphthong shorthand (A = eɪ, I = aɪ, O = oʊ, W = aʊ, Y = ɔɪ)."""
    s = (symbol or "").strip()
    if not s:
        return "rest"
    key = s.rstrip("012").upper()
    if key in _ARPA and (len(key) > 1 or key in _ARPA_SINGLE) and not (capitals and s in _IPA_CAPITAL):
        return _ARPA[key]
    for ch in s:
        shape = _ipa_char(ch, capitals)
        if shape != "rest":
            return shape
    return "rest"


def phoneme_units(phonemes: Any) -> List[str]:
    """Visemes of a word's phonemes: a list of symbols, or one string (ARPAbet
    codes separated by spaces, or IPA symbols run together)."""
    if phonemes is None:
        return []
    if isinstance(phonemes, str):
        text = phonemes.strip()
        units = [phoneme_viseme(p) for p in text.split()] if " " in text else [_ipa_char(ch, True) for ch in text]
    else:
        units = [phoneme_viseme(str(p)) for p in phonemes]
    return [u for u in units if u != "rest"]


_UNIT_WEIGHT = {"AA": 1.6, "E": 1.5, "I": 1.4, "O": 1.5, "U": 1.4, "MBP": 0.8, "FV": 0.9, "L": 0.8, "OTHER": 0.7}


# ── Audio ──

def _mp3_duration_ms(data: bytes) -> Optional[int]:
    """Duration in ms of a constant-bitrate MPEG audio layer III file, from its first frame header."""
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        pos = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    limit = min(len(data) - 4, pos + 8192)
    while pos < limit:
        if data[pos] == 0xFF and (data[pos + 1] & 0xE0) == 0xE0:
            version, layer = (data[pos + 1] >> 3) & 3, (data[pos + 1] >> 1) & 3
            bitrate_index = (data[pos + 2] >> 4) & 15
            if version != 1 and layer == 1 and 0 < bitrate_index < 15:
                table = ((32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3
                         else (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
                return int((len(data) - pos) * 8 / table[bitrate_index - 1])
        pos += 1
    return None


def _decode_wav(data: bytes) -> Optional[Dict[str, Any]]:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt " and body + 16 <= len(data):
            tag, ch, rate, _byterate, _align, bits = struct.unpack("<HHIIHH", data[body:body + 16])
            if tag == 0xFFFE and size >= 26 and body + 26 <= len(data):
                tag = struct.unpack("<H", data[body + 24:body + 26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            pcm = data[body:end]
            break
        pos = body + size + (size & 1)
    if not fmt or pcm is None:
        return None
    tag, ch, rate, bits = fmt
    width = bits // 8
    if ch < 1 or rate < 1000 or width < 1 or tag not in (1, 3):
        return None
    frames = len(pcm) // (width * ch)
    duration = int(frames * 1000 / rate)
    if frames == 0:
        return {"duration_ms": 0, "rate": rate, "samples": None, "scale": 1.0}
    pcm = pcm[:frames * width * ch]
    if tag == 1 and bits == 16:
        samples, scale = array("h"), 32768.0
        samples.frombytes(pcm)
    elif tag == 1 and bits == 32:
        samples, scale = array("i"), 2147483648.0
        samples.frombytes(pcm)
    elif tag == 1 and bits == 8:
        samples, scale = array("h", (b - 128 for b in pcm)), 128.0
    elif tag == 1 and bits == 24:
        samples = array("i", (int.from_bytes(pcm[k:k + 3], "little", signed=True) for k in range(0, len(pcm), 3)))
        scale = 8388608.0
    elif tag == 3 and bits == 32:
        samples, scale = array("f"), 1.0
        samples.frombytes(pcm)
    elif tag == 3 and bits == 64:
        samples, scale = array("d"), 1.0
        samples.frombytes(pcm)
    else:
        return {"duration_ms": duration, "rate": rate, "samples": None, "scale": 1.0}
    if sys.byteorder == "big" and bits in (16, 32, 64):
        samples.byteswap()
    if ch > 1:
        samples = samples[::ch]
    return {"duration_ms": duration, "rate": rate, "samples": samples, "scale": scale}


def _energy(samples: Sequence[float], rate: int, scale: float) -> List[float]:
    """RMS per FRAME_MS frame, normalised to full scale."""
    spf = max(1, rate * FRAME_MS // 1000)
    step = max(1, spf // 40)
    out: List[float] = []
    for start in range(0, len(samples), spf):
        chunk = samples[start:start + spf:step]
        if not len(chunk):
            break
        out.append((sum(v * v for v in chunk) / len(chunk)) ** 0.5 / scale)
    return out


def analyze_audio(audio: Optional[bytes]) -> Dict[str, Any]:
    """{"duration_ms": int | None, "frames": [rms per 10 ms] | None}. WAV is
    decoded; MPEG audio only yields a duration estimate; anything else nothing."""
    if not audio:
        return {"duration_ms": None, "frames": None}
    try:
        wav = _decode_wav(audio)
    except Exception:
        wav = None
    if wav:
        frames = _energy(wav["samples"], wav["rate"], wav["scale"]) if wav["samples"] is not None else None
        return {"duration_ms": wav["duration_ms"], "frames": frames}
    try:
        return {"duration_ms": _mp3_duration_ms(audio), "frames": None}
    except Exception:
        return {"duration_ms": None, "frames": None}


def _active_segments(frames: Sequence[float], lo_ms: int, hi_ms: int) -> List[Tuple[int, int]]:
    """Spoken stretches (ms) inside [lo_ms, hi_ms): where the energy stands out of the noise floor."""
    lo_f, hi_f = lo_ms // FRAME_MS, min(len(frames), -(-hi_ms // FRAME_MS))
    seg = list(frames[lo_f:hi_f])
    if len(seg) < 2:
        return []
    ordered = sorted(seg)
    peak = ordered[int(0.95 * (len(ordered) - 1))]
    floor = ordered[int(0.10 * (len(ordered) - 1))]
    if peak < 1e-4:
        return []
    if floor > 0.4 * peak:
        return [(lo_ms, hi_ms)]   # no contrast between loud and quiet: a steady sound is all speech
    threshold = max(floor + 0.10 * (peak - floor), 0.02 * peak)
    active = [v > threshold for v in seg]
    runs: List[List[int]] = []
    i, n = 0, len(active)
    while i < n:
        j = i
        while j < n and active[j] == active[i]:
            j += 1
        runs.append([i, j, int(active[i])])
        i = j
    # Fill the short silences inside speech (stop closures), then drop the blips.
    for k in range(1, len(runs) - 1):
        if not runs[k][2] and runs[k][1] - runs[k][0] < PAUSE_FILL_FRAMES and runs[k - 1][2] and runs[k + 1][2]:
            runs[k][2] = 1
    merged: List[List[int]] = []
    for r in runs:
        if merged and merged[-1][2] == r[2]:
            merged[-1][1] = r[1]
        else:
            merged.append(r[:])
    out = [(a, b) for a, b, on in merged if on and b - a >= MIN_BLIP_FRAMES]
    return [(max(lo_ms, (lo_f + a) * FRAME_MS), min(hi_ms, (lo_f + b) * FRAME_MS)) for a, b in out]


# ── Word timings ──

def _word_weight(word: Dict[str, Any], lang: str) -> float:
    letters = sum(1 for c in word["text"] if c.isalnum())
    syl = count_syllables(word["text"], lang)
    return syl + 0.08 * max(0, letters - syl)


def _valleys(frames: Sequence[float], lo_ms: int, hi_ms: int) -> List[Tuple[int, float]]:
    """Dips of the energy envelope inside [lo_ms, hi_ms): (time_ms, depth 0..1 of the group's loudness). In speech these
    are where syllables, and often words, break; a quiet plateau counts once, at its middle."""
    lo_f, hi_f = lo_ms // FRAME_MS, min(len(frames), -(-hi_ms // FRAME_MS))
    seg = frames[lo_f:hi_f]
    n = len(seg)
    if n < 5:
        return []
    ordered = sorted(seg)
    peak = ordered[int(0.95 * (n - 1))]
    if peak < 1e-4:
        return []
    smooth = [(seg[max(0, i - 1)] + seg[i] + seg[min(n - 1, i + 1)]) / 3.0 for i in range(n)]
    found: List[Tuple[int, float]] = []
    run: List[int] = []
    for i in range(n):
        window = smooth[max(0, i - 3):i + 4]
        if smooth[i] <= min(window):
            run.append(i)
            continue
        if run:
            found.append(_valley_at(smooth, run, peak))
            run = []
    if run:
        found.append(_valley_at(smooth, run, peak))
    return [((lo_f + f) * FRAME_MS + FRAME_MS // 2, d) for f, d in found if d >= MIN_VALLEY_DEPTH]


def _valley_at(smooth: Sequence[float], run: List[int], peak: float) -> Tuple[int, float]:
    mid = run[len(run) // 2]
    left = max(smooth[max(0, run[0] - 10):run[0] + 1])
    right = max(smooth[run[-1]:run[-1] + 11])
    return mid, max(0.0, min(left, right) - smooth[mid]) / peak


def _lay_out(weights: Sequence[float], syllables: Sequence[int], segs: Sequence[Tuple[int, int]],
             valleys: Sequence[Tuple[int, float]]) -> Tuple[List[int], List[int]]:
    """Words laid end to end over the spoken stretches `segs` (the silences between them are skipped).

    Each word boundary starts at the position its syllable weight gives it. A dip of the envelope close to it is where
    the speaker really broke, so the boundaries are chosen together (dynamic programming over the nearby dips): near
    the proportional place, on a deep dip, and leaving each word as many dips inside as it has syllables minus one."""
    starts = [s for s, _ in segs]
    cum = [0]
    for s, e in segs:
        cum.append(cum[-1] + (e - s))
    total = cum[-1]
    n = len(weights)
    wsum = sum(weights) or 1.0

    def at(u: float, side: str) -> int:
        u = min(max(u, 0.0), float(total))
        if side == "start":
            k = min(bisect.bisect_right(cum, u) - 1, len(segs) - 1)
        else:
            k = bisect.bisect_left(cum, u) - 1
        k = max(0, k)
        return int(round(starts[k] + (u - cum[k])))

    # Dips in the coordinate of spoken time.
    dips: List[Tuple[float, float]] = []
    for t, depth in valleys:
        k = bisect.bisect_right(starts, t) - 1
        if k >= 0 and t <= segs[k][1]:
            dips.append((cum[k] + (t - segs[k][0]), depth))
    dips.sort()
    dip_u = [u for u, _ in dips]

    acc = 0.0
    prop: List[float] = []
    for w in weights[:-1]:
        acc += w
        prop.append(total * acc / wsum)
    bounds: List[float] = [0.0]
    if n > 1 and dips:
        layers: List[List[Tuple[float, float, int]]] = []   # per boundary: (u, cost so far, index of the best previous)
        previous: List[Tuple[float, float, int]] = [(0.0, 0.0, -1)]
        for i in range(1, n + 1):
            if i == n:
                options = [(float(total), 0.0)]
                scale = 1.0
            else:
                near = total * (weights[i - 1] + weights[i]) / (2.0 * wsum)
                reach = max(100.0, 0.75 * near)
                lo, hi = bisect.bisect_left(dip_u, prop[i - 1] - reach), bisect.bisect_right(dip_u, prop[i - 1] + reach)
                options = [(prop[i - 1], 0.0)] + [dips[k] for k in range(lo, hi) if abs(dip_u[k] - prop[i - 1]) > 1.0]
                scale = max(40.0, 0.4 * near)
            layer: List[Tuple[float, float, int]] = []
            for u2, depth in options:
                own = 0.0 if i == n else ((u2 - prop[i - 1]) / scale) ** 2 - DEPTH_REWARD * depth
                best, arg = 1e18, -1
                for j, (u1, cost1, _) in enumerate(previous):
                    if u2 < u1:
                        continue
                    pair = 0.0
                    if u2 - u1 < MIN_WORD_MS:
                        pair += 5.0 * (MIN_WORD_MS - (u2 - u1)) / MIN_WORD_MS
                    inside = max(0, bisect.bisect_left(dip_u, u2 - 20.0) - bisect.bisect_right(dip_u, u1 + 20.0))
                    pair += DIP_COUNT_COST * abs(inside - (syllables[i - 1] - 1))
                    if cost1 + pair < best:
                        best, arg = cost1 + pair, j
                if arg >= 0:
                    layer.append((u2, best + own, arg))
            if not layer:
                layer = [(options[0][0], 1e9, 0)]
            layers.append(layer)
            previous = layer
        # walk back from the last boundary
        k = min(range(len(layers[-1])), key=lambda x: layers[-1][x][1])
        chosen: List[float] = []
        for li in range(n - 1, 0, -1):
            k = layers[li][k][2]
            chosen.append(layers[li - 1][k][0])
        bounds += list(reversed(chosen))
    else:
        bounds += prop
    bounds.append(float(total))
    s_list = [at(bounds[i], "start") for i in range(n)]
    e_list = [at(bounds[i + 1], "end") for i in range(n)]
    return s_list, e_list


def _match_pauses(gaps: Sequence[float], cands: Sequence[float]) -> Dict[int, int]:
    """Which pauses in the audio are the punctuation of the text. `gaps`: where each pause falls, as a fraction of the
    spoken time; `cands`: where each punctuation mark falls, as a fraction of the text's syllable weight (ascending).
    Monotone assignment of pauses to marks that minimises the distance between them; a pause without a mark nearby
    stays unmatched (a hesitation), a mark without a pause is simply not a pause (most commas). {gap: candidate}."""
    g, c = len(gaps), len(cands)
    if not g or not c:
        return {}
    skip, reach, inf = 0.25, 0.25, 1e9
    dp = [[inf] * (c + 1) for _ in range(g + 1)]
    back: List[List[Optional[Tuple[int, Optional[int]]]]] = [[None] * (c + 1) for _ in range(g + 1)]
    dp[0] = [0.0] * (c + 1)
    for j in range(g):
        for nxt in range(c + 1):
            cur = dp[j][nxt]
            if cur >= inf:
                continue
            if cur + skip < dp[j + 1][nxt]:
                dp[j + 1][nxt], back[j + 1][nxt] = cur + skip, (nxt, None)
            lo = bisect.bisect_left(cands, gaps[j] - reach, nxt)
            hi = bisect.bisect_right(cands, gaps[j] + reach, nxt)
            for k in range(lo, hi):
                cost = cur + abs(gaps[j] - cands[k])
                if cost < dp[j + 1][k + 1]:
                    dp[j + 1][k + 1], back[j + 1][k + 1] = cost, (nxt, k)
    nxt = min(range(c + 1), key=lambda x: dp[g][x])
    out: Dict[int, int] = {}
    for j in range(g, 0, -1):
        prev, k = back[j][nxt]  # type: ignore[misc]
        if k is not None:
            out[j - 1] = k
        nxt = prev
    return out


def estimate_word_timings(words: Sequence[Dict[str, Any]], frames: Optional[Sequence[float]], lo_ms: int, hi_ms: int,
                          lang: str = "en") -> List[Tuple[int, int]]:
    """(start_ms, end_ms) per word, inside [lo_ms, hi_ms], non-decreasing.

    The words are laid out by syllable weight over the *spoken* time only (the
    energy profile says where speech is), so a long pause in the audio falls
    between words instead of stretching one. A pause that lines up with a
    punctuation mark of the text pins that boundary, and the words on each side
    are laid out in their own stretch, where the dips of the envelope decide
    the exact place of every word boundary (see `_lay_out`)."""
    n = len(words)
    if n == 0:
        return []
    hi_ms = max(hi_ms, lo_ms)
    segs = _active_segments(frames, lo_ms, hi_ms) if frames else []
    if not segs:
        segs = [(lo_ms, hi_ms)]
    weights = [_word_weight(w, lang) for w in words]
    syllables = [count_syllables(w["text"], lang) for w in words]
    wsum = sum(weights) or 1.0
    pinned: List[Tuple[int, int]] = []   # (segment index before the pause, word index before the pause)
    if len(segs) > 1:
        total = sum(e - s for s, e in segs) or 1
        gap_at, running = [], 0
        for s, e in segs[:-1]:
            running += e - s
            gap_at.append(running / total)
        cand_words = [i for i in range(n - 1) if words[i].get("br")]
        cum_w, acc = [], 0.0
        for w in weights:
            acc += w
            cum_w.append(acc / wsum)
        pinned = sorted((j, cand_words[k]) for j, k in _match_pauses(gap_at, [cum_w[i] for i in cand_words]).items())
    valleys = _valleys(frames, lo_ms, hi_ms) if frames else []
    s_list: List[int] = []
    e_list: List[int] = []
    w0, g0 = 0, 0
    for seg_i, word_i in pinned + [(len(segs) - 1, n - 1)]:
        a, b = _lay_out(weights[w0:word_i + 1], syllables[w0:word_i + 1], segs[g0:seg_i + 1], valleys)
        s_list += a
        e_list += b
        w0, g0 = word_i + 1, seg_i + 1
    out: List[Tuple[int, int]] = []
    prev = lo_ms
    for s, e in zip(s_list, e_list):
        s = min(max(s, prev, lo_ms), hi_ms)
        e = min(max(e, s), hi_ms)
        out.append((s, e))
        prev = e
    return out


def _estimate_duration_ms(words: Sequence[Dict[str, Any]], lang: str) -> int:
    """No decodable audio: about 190 ms per syllable (a conversational pace)."""
    return int(sum(count_syllables(w["text"], lang) for w in words) * 190 + 300)


# ── Visemes ──

def _word_visemes(text: str, phonemes: Any, lang: str, start: int, end: int) -> List[Dict[str, Any]]:
    units = phoneme_units(phonemes) if phonemes else []
    if not units:
        units = letter_units(text, lang)
    if end <= start:
        return []
    weights = [_UNIT_WEIGHT.get(u, 0.7) for u in units]
    total = sum(weights)
    out: List[Dict[str, Any]] = []
    acc = 0.0
    for u, w in zip(units, weights):
        a = start + int(round((end - start) * acc / total))
        acc += w
        b = start + int(round((end - start) * acc / total))
        if b <= a:
            continue
        if out and out[-1]["v"] == u and out[-1]["end_ms"] == a:
            out[-1]["end_ms"] = b
        else:
            out.append({"v": u, "start_ms": a, "end_ms": b})
    return out


def _with_rests(visemes: List[Dict[str, Any]], duration_ms: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor = 0
    for v in visemes:
        if v["start_ms"] - cursor >= MIN_REST_MS:
            out.append({"v": "rest", "start_ms": cursor, "end_ms": v["start_ms"]})
        out.append(v)
        cursor = max(cursor, v["end_ms"])
    if duration_ms - cursor >= MIN_REST_MS:
        out.append({"v": "rest", "start_ms": cursor, "end_ms": duration_ms})
    return out


def _engine_visemes(phonemes: Any, duration_ms: int) -> Optional[List[Dict[str, Any]]]:
    """Visemes from an engine's timed phonemes [{"p": "a", "start_ms": 0, "end_ms": 80}]; None if unusable."""
    if not isinstance(phonemes, list) or not phonemes:
        return None
    items: List[Dict[str, Any]] = []
    for ph in phonemes:
        if not isinstance(ph, dict):
            return None
        symbol = ph.get("p", ph.get("phoneme"))
        s, e = ph.get("start_ms"), ph.get("end_ms")
        if not isinstance(symbol, str) or isinstance(s, bool) or isinstance(e, bool) \
                or not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            return None
        s, e = int(min(max(s, 0), duration_ms)), int(min(max(e, 0), duration_ms))
        if e <= s:
            continue
        items.append({"v": phoneme_viseme(symbol), "start_ms": s, "end_ms": e})
    if not items:
        return None
    items.sort(key=lambda x: (x["start_ms"], x["end_ms"]))
    out: List[Dict[str, Any]] = []
    for it in items:
        if out:
            it["start_ms"] = max(it["start_ms"], out[-1]["end_ms"])
        if it["end_ms"] <= it["start_ms"]:
            continue
        if out and out[-1]["v"] == it["v"] and out[-1]["end_ms"] == it["start_ms"]:
            out[-1]["end_ms"] = it["end_ms"]
        else:
            out.append(it)
    return out or None


# ── Engine-provided data ──

ALIGN_MIN_SHARE = 0.8    # share of the text's words, and of the engine's, that must find their counterpart
ALIGN_FUSE = 4           # an engine may split a word of the text into up to this many words, or merge as many into one
ALIGN_NUMBER = 8         # words a spoken number may take ("mil novecientos noventa y nueve")
ALIGN_BAND = 40          # how far from the diagonal the alignment looks


def _norm_word(word: Any) -> str:
    """Casefolded, without accents or punctuation: what two spellings of the same word share."""
    folded = unicodedata.normalize("NFKD", str(word).casefold())
    return "".join(c for c in folded if c.isalnum())


@functools.lru_cache(maxsize=65536)
def _similar(a: str, b: str) -> float:
    if a == b:
        return 1.0 if a else 0.0
    if min(len(a), len(b)) < 4 or a[0] != b[0] or abs(len(a) - len(b)) > 3:
        return 0.0
    return 0.8 if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 else 0.0


def _join_phonemes(parts: List[Any]) -> Any:
    if parts and all(isinstance(p, str) and p for p in parts):
        return " ".join(parts)
    if parts and all(isinstance(p, list) for p in parts):
        return [x for p in parts for x in p]
    return None


def _align_words(text_words: List[str], engine_words: List[str]) -> List[Tuple[str, int, int, int]]:
    """Best alignment of the words of the text with the engine's, as steps (kind, i, j, k): `m` one to one, `s` the text
    word i is the engine words j..j+k-1 (split, or a number said in words), `e` the text words i..i+k-1 are the engine
    word j (merged), `t` text word i has no counterpart, `g` engine word j has none. Banded around the diagonal."""
    m, n = len(text_words), len(engine_words)
    has_digit = [any(c.isdigit() for c in w) for w in text_words]
    band = ALIGN_BAND + abs(m - n)
    best: Dict[Tuple[int, int], float] = {(0, 0): 0.0}
    back: Dict[Tuple[int, int], Tuple[Tuple[int, int], Tuple[str, int, int, int]]] = {}

    def relax(src: Tuple[int, int], dst: Tuple[int, int], gain: float, step: Tuple[str, int, int, int]) -> None:
        score = best[src] + gain
        if dst not in best or score > best[dst] + 1e-9:
            best[dst] = score
            back[dst] = (src, step)

    for i in range(m + 1):
        centre = int(i * n / m) if m else 0
        for j in range(max(0, centre - band), min(n, centre + band) + 1):
            if (i, j) not in best:
                continue
            if i < m:
                relax((i, j), (i + 1, j), 0.0, ("t", i, j, 1))
            if j < n:
                relax((i, j), (i, j + 1), 0.0, ("g", i, j, 1))
            if i < m and j < n:
                sim = _similar(text_words[i], engine_words[j])
                if sim:
                    relax((i, j), (i + 1, j + 1), sim, ("m", i, j, 1))
            if i < m:
                limit = ALIGN_NUMBER if has_digit[i] else ALIGN_FUSE
                for k in range(1 if has_digit[i] else 2, limit + 1):
                    if j + k > n:
                        break
                    if has_digit[i] or "".join(engine_words[j:j + k]) == text_words[i]:
                        relax((i, j), (i + 1, j + k), 1.0 + 0.001 * k, ("s", i, j, k))
            if j < n:
                for k in range(2, ALIGN_FUSE + 1):
                    if i + k > m:
                        break
                    if "".join(text_words[i:i + k]) == engine_words[j]:
                        relax((i, j), (i + k, j + 1), float(k), ("e", i, j, k))
    if (m, n) not in best:
        return []
    steps: List[Tuple[str, int, int, int]] = []
    cell = (m, n)
    while cell != (0, 0):
        cell, step = back[cell]
        steps.append(step)
    steps.reverse()
    return steps


def _engine_words(words: List[Dict[str, Any]], given: Any, duration_ms: int) -> Tuple[Optional[List[Tuple[int, int, Any]]], Optional[str]]:
    """(spans, why): timings from an engine's own word list, one per word of the text, when that list is the text
    spoken: the words are aligned (casefold, no accents or punctuation) and at least ALIGN_MIN_SHARE of the text's
    words and of the engine's must find their counterpart, tolerating a number said in words, a word the engine split
    in several and several it merged into one. `why` is "text_mismatch" when the list is well formed but belongs to
    another text; a malformed list gives (None, None)."""
    if not isinstance(given, list) or not given or not words:
        return None, None
    timed: List[Tuple[int, int, Any, str]] = []
    prev = 0
    for g in given:
        if not isinstance(g, dict) or not isinstance(g.get("text"), str):
            return None, None
        s, e = g.get("start_ms"), g.get("end_ms")
        if isinstance(s, bool) or isinstance(e, bool) or not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            return None, None
        s = int(min(max(s, prev, 0), duration_ms))
        e = int(min(max(e, s), duration_ms))
        timed.append((s, e, g.get("phonemes"), _norm_word(g["text"])))
        prev = e
    text_norm = [_norm_word(w["text"]) for w in words]
    engine_norm = [t[3] for t in timed]

    if len(timed) == len(words) and all(_similar(a, b) for a, b in zip(text_norm, engine_norm)):
        return [(s, e, ph) for s, e, ph, _ in timed], None   # the usual case, no alignment needed

    spans: List[Optional[Tuple[int, int, Any]]] = [None] * len(words)
    matched_text = matched_engine = 0
    for kind, i, j, k in _align_words(text_norm, engine_norm):
        if kind == "m":
            spans[i] = timed[j][:3]
            matched_text += 1
            matched_engine += 1
        elif kind == "s":
            spans[i] = (timed[j][0], timed[j + k - 1][1], _join_phonemes([t[2] for t in timed[j:j + k]]))
            matched_text += 1
            matched_engine += k
        elif kind == "e":
            s, e = timed[j][0], timed[j][1]
            lengths = [max(1, len(text_norm[x])) for x in range(i, i + k)]
            acc = 0
            for x, ln in zip(range(i, i + k), lengths):
                a = s + int(round((e - s) * acc / sum(lengths)))
                acc += ln
                spans[x] = (a, s + int(round((e - s) * acc / sum(lengths))), None)
            matched_text += k
            matched_engine += 1
    if matched_text < ALIGN_MIN_SHARE * len(words) or matched_engine < ALIGN_MIN_SHARE * len(timed):
        return None, "text_mismatch"

    # The few words the engine did not report sit in the gap between their neighbours.
    out: List[Tuple[int, int, Any]] = []
    i = 0
    while i < len(spans):
        if spans[i] is not None:
            out.append(spans[i])  # type: ignore[arg-type]
            i += 1
            continue
        j = i
        while j < len(spans) and spans[j] is None:
            j += 1
        left = out[-1][1] if out else 0
        right = spans[j][0] if j < len(spans) else duration_ms  # type: ignore[index]
        right = max(right, left)
        lengths = [max(1, len(text_norm[x])) for x in range(i, j)]
        acc = 0
        for x, ln in zip(range(i, j), lengths):
            a = left + int(round((right - left) * acc / sum(lengths)))
            acc += ln
            out.append((a, left + int(round((right - left) * acc / sum(lengths))), None))
        i = j
    fixed: List[Tuple[int, int, Any]] = []
    prev = 0
    for s, e, ph in out:
        s = int(min(max(s, prev, 0), duration_ms))
        e = int(min(max(e, s), duration_ms))
        fixed.append((s, e, ph))
        prev = e
    return fixed, None


def _segment_timings(text: str, words: List[Dict[str, Any]], segments: Any, frames: Optional[Sequence[float]],
                     duration_ms: int, lang: str) -> Optional[List[Tuple[int, int, Any]]]:
    """Per-chunk engine output [{"text", "phonemes", "start_ms", "end_ms"}]: each chunk's words are laid over its own
    stretch of the audio, which anchors the estimate every few seconds instead of once for the whole text."""
    if not isinstance(segments, list) or not segments or not words:
        return None
    result: List[Optional[Tuple[int, int, Any]]] = [None] * len(words)
    cursor = 0
    for seg in segments:
        if not isinstance(seg, dict) or not isinstance(seg.get("text"), str):
            return None
        s, e = seg.get("start_ms"), seg.get("end_ms")
        if isinstance(s, bool) or isinstance(e, bool) or not isinstance(s, (int, float)) or not isinstance(e, (int, float)) or e < s:
            return None
        s, e = int(min(max(s, 0), duration_ms)), int(min(max(e, 0), duration_ms))
        chunk = seg["text"].strip()
        pos = text.find(chunk, cursor) if chunk else -1
        if pos < 0:
            return None
        end = pos + len(chunk)
        idx = [k for k, w in enumerate(words) if w["_s"] >= pos and w["_e"] <= end]
        cursor = end
        if not idx:
            continue
        spans = estimate_word_timings([words[k] for k in idx], frames, s, e, lang)
        phs = seg.get("phonemes")
        per_word = phs.split() if isinstance(phs, str) and len(phs.split()) == len(idx) else [None] * len(idx)
        for k, (a, b), p in zip(idx, spans, per_word):
            result[k] = (a, b, p)
    out: List[Tuple[int, int, Any]] = []
    prev = 0
    for r in result:
        if r is None:
            return None
        a = max(r[0], prev)
        b = max(r[1], a)
        out.append((a, b, r[2]))
        prev = b
    return out


# ── Public ──

def build_marks(text: str, audio: Optional[bytes] = None, language: str = "", engine: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Word timings and visemes for `text` spoken as `audio` (see the module docstring for the shape).

    `engine` is what the speech engine itself reported, all optional: `words`
    ([{"text", "start_ms", "end_ms", "phonemes"?}], one per word of the text),
    `segments` ([{"text", "phonemes"?, "start_ms", "end_ms"}], consecutive chunks of
    the text) and `phonemes` ([{"p", "start_ms", "end_ms"}] for the whole audio).
    Anything that does not line up with the text or the audio is ignored."""
    lang = normalize_language(language, text)
    words = tokenize(text)
    info = analyze_audio(audio)
    frames = info["frames"]
    timing = "audio"
    duration = info["duration_ms"]
    if duration is None:
        duration, timing = _estimate_duration_ms(words, lang), "text_only"
    duration = int(duration)
    engine = engine if isinstance(engine, dict) else {}

    spans, why = _engine_words(words, engine.get("words"), duration)
    engine_ignored = None
    if why:
        # The engine's words are those of another text: nothing else in the same report can be trusted for this one.
        engine_ignored, engine = why, {}
    words_source = "engine"
    if spans is None:
        words_source = "estimated"   # chunk boundaries from the engine anchor the estimate, the words in them are still laid out
        spans = _segment_timings(text, words, engine.get("segments"), frames, duration, lang)
    if spans is None:
        spans = [(a, b, None) for a, b in estimate_word_timings(words, frames, 0, duration, lang)]

    out_words: List[Dict[str, Any]] = []
    visemes: List[Dict[str, Any]] = []
    for i, (w, (a, b, ph)) in enumerate(zip(words, spans)):
        out_words.append({"i": i, "text": w["text"], "start_char": w["start_char"], "end_char": w["end_char"],
                          "start_ms": a, "end_ms": b})
        visemes.extend(_word_visemes(w["text"], ph, lang, a, b))

    visemes_source = "estimated"
    engine_visemes = _engine_visemes(engine.get("phonemes"), duration)
    if engine_visemes is not None:
        visemes, visemes_source = engine_visemes, "engine"
    elif spans and all(p for _, _, p in spans):
        visemes_source = "engine"
    result = {
        "version": MARKS_VERSION, "language": lang, "duration_ms": duration, "timing": timing,
        "words_source": words_source, "visemes_source": visemes_source,
        "viseme_set": list(VISEMES), "words": out_words, "visemes": _with_rests(visemes, duration),
    }
    if engine_ignored:
        result["engine_ignored"] = engine_ignored
    return result
