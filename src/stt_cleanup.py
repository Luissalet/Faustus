# src/stt_cleanup.py
"""Deterministic post-processing for STT output.

Whisper-family models produce a small, well-known set of artifacts on noisy
or silent audio: it repeats the same line verbatim across consecutive
segments, it hallucinates a stock phrase ("Thanks for watching", "Gracias
por ver el vídeo") out of near-silence, and it occasionally loops a single
word or short phrase within one segment ("the the the the the"). None of
this is a language-understanding problem — it is pattern noise with a fixed
shape, so it is removed with fixed, table-driven rules rather than another
model call.

This module is the single place that cleanup runs. ``services/stt/stt_service.py``
calls it right before a transcript leaves the process, for every provider
(local Whisper, command, and OpenAI-compatible endpoint alike), so a caller
never sees the raw hallucinated text regardless of which STT backend is
configured.

Two entry points:
  * ``clean_segments`` — for a provider that has per-segment timestamps
    (local faster-whisper). Repeated-segment collapsing needs several
    segments to compare, so this is the more thorough pass.
  * ``clean_text`` — for a provider that only returns one flat string
    (command / OpenAI-compatible endpoint). Runs the same phrase and
    n-gram-loop rules against the whole string as a single segment.

Both return ``(cleaned, stats)`` where ``stats`` counts exactly what was
removed, for logging and for the meeting-notes pipeline's warning banner.
"""

from __future__ import annotations

import re
import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── Hallucination phrase table (en/es) ──────────────────────────────────
#
# Each entry is the phrase Whisper is known to hallucinate on silence or
# near-silence, normalized (lowercase, punctuation stripped) at match time.
# A segment is dropped only when its ENTIRE normalized text equals one of
# these entries — never a substring match, so "gracias por tu ayuda" is
# left alone while a bare "gracias" (a whole segment on its own, the shape
# Whisper actually produces on silence) is dropped.
HALLUCINATION_PHRASES: Tuple[Tuple[str, str], ...] = (
    # English
    ("thanks for watching", "en"),
    ("thank you for watching", "en"),
    ("thanks for watching!", "en"),
    ("please subscribe", "en"),
    ("subscribe", "en"),
    ("like and subscribe", "en"),
    ("dont forget to subscribe", "en"),
    ("see you in the next video", "en"),
    ("see you next time", "en"),
    ("thank you", "en"),
    ("thanks", "en"),
    ("you", "en"),
    # Spanish
    ("gracias por ver el video", "es"),
    ("gracias por ver el vídeo", "es"),
    ("suscribete", "es"),
    ("suscríbete", "es"),
    ("suscribete al canal", "es"),
    ("dale like y suscribete", "es"),
    ("nos vemos en el proximo video", "es"),
    ("nos vemos en el próximo vídeo", "es"),
    ("gracias", "es"),
)

_HALLUCINATION_SET = frozenset(p for p, _lang in HALLUCINATION_PHRASES)

# Phrases that are usually followed by a credited name ("Subtitles by John
# Doe") — matched as a PREFIX of the whole normalized segment rather than an
# exact match, since the trailing name is never the same twice.
HALLUCINATION_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("subtitles by", "en"),
    ("captions by", "en"),
    ("subtitulado por", "es"),
    ("subtitulos por", "es"),
)
_HALLUCINATION_PREFIX_SET = tuple(p for p, _lang in HALLUCINATION_PREFIXES)

# Music/silence markers a transcript sometimes carries verbatim: "[Music]",
# "(música)", bare note glyphs. Matched as the WHOLE normalized segment.
_MUSIC_RE = re.compile(
    r"^[\[\(\{]?\s*(m[uú]sica|music|silencio|silence|applause|aplausos)\s*[\]\)\}]?$"
)
_NOTE_GLYPH_RE = re.compile(r"^[♪♫♩♬\s]+$")  # ♪ ♫ ♩ ♬

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace — the shape used
    for every table lookup and every duplicate/similarity comparison below.
    Never used for what actually gets returned to the caller."""
    t = (text or "").strip().lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def is_hallucination(text: str) -> bool:
    """True when ``text``, taken as a whole, is one of the known
    silence/hallucination artifacts (table match or a bare music marker)."""
    raw = (text or "").strip()
    if not raw:
        return True
    if _NOTE_GLYPH_RE.match(raw):
        return True
    norm = normalize(raw)
    if not norm:
        return True
    if _MUSIC_RE.match(norm):
        return True
    if norm in _HALLUCINATION_SET:
        return True
    return any(norm.startswith(prefix) for prefix in _HALLUCINATION_PREFIX_SET)


# ── n-gram loop collapsing ("the the the the" → "the") ─────────────────

# Longest phrase (in words) checked for a repeating loop, and how many times
# in a row it must repeat before it counts as a loop rather than normal
# repetition ("no no no" said for emphasis is 3 words; anything shorter than
# MIN_REPEATS is left alone on purpose — deciding "is this emphasis or a
# hallucination" from word count alone is exactly the guess this module is
# supposed to avoid making).
_MAX_NGRAM = 4
MIN_REPEATS = 4


def collapse_ngram_loops(text: str) -> Tuple[str, int]:
    """Collapse an immediate word/short-phrase loop within one string down to
    a single occurrence. Returns ``(collapsed_text, loops_collapsed)``.

    Works from the longest n-gram down to unigrams so a repeating two-word
    phrase is collapsed as a unit rather than each word being flagged
    separately.
    """
    words = text.split()
    if len(words) < MIN_REPEATS:
        return text, 0

    collapsed = 0
    out: List[str] = []
    i = 0
    n_words = len(words)
    lowered = [w.lower().strip(".,!?;:") for w in words]

    while i < n_words:
        matched = False
        for n in range(_MAX_NGRAM, 0, -1):
            if i + n > n_words:
                continue
            phrase = lowered[i:i + n]
            if not any(phrase):
                continue
            repeats = 1
            j = i + n
            while j + n <= n_words and lowered[j:j + n] == phrase:
                repeats += 1
                j += n
            if repeats >= MIN_REPEATS:
                out.extend(words[i:i + n])
                collapsed += repeats - 1
                i = j
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1

    return " ".join(out), collapsed


# ── Segment-level pipeline ──────────────────────────────────────────────

@dataclass
class CleanupStats:
    segments_in: int = 0
    segments_out: int = 0
    removed_hallucination: int = 0
    removed_duplicate: int = 0
    ngram_loops_collapsed: int = 0
    hotkey_stripped: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "segments_in": self.segments_in,
            "segments_out": self.segments_out,
            "removed_hallucination": self.removed_hallucination,
            "removed_duplicate": self.removed_duplicate,
            "ngram_loops_collapsed": self.ngram_loops_collapsed,
            "hotkey_stripped": self.hotkey_stripped,
        }


DUPLICATE_SIMILARITY_THRESHOLD = 0.88


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return a == b
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= DUPLICATE_SIMILARITY_THRESHOLD


def _strip_hotkey(text: str, hotkey_norm: str) -> Tuple[str, bool]:
    """Remove a configured trailing hotkey/command phrase from the very end
    of ``text``, if present. Compares normalized forms but slices the
    original text so casing/punctuation of what remains is untouched."""
    if not hotkey_norm:
        return text, False
    norm = normalize(text)
    if not norm.endswith(hotkey_norm):
        return text, False
    if norm == hotkey_norm:
        return "", True
    # Find the split point in the ORIGINAL text by matching word count from
    # the end (normalize() only ever removes punctuation and collapses
    # whitespace, so word counts line up).
    hotkey_words = len(hotkey_norm.split())
    words = text.split()
    if len(words) <= hotkey_words:
        return "", True
    return " ".join(words[:-hotkey_words]).strip(), True


def clean_segments(
    segments: Sequence[Dict[str, Any]],
    *,
    hotkey_phrases: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Clean a list of ``{"start": float, "end": float, "text": str}``
    segments. Timestamps stay coherent: a collapsed run of duplicate
    segments keeps the first segment's ``start`` and the run's last
    ``end``, and every kept segment's own start/end pair is untouched.
    """
    stats = CleanupStats()
    stats.segments_in = len(segments)

    hotkeys_norm = sorted(
        (normalize(h) for h in (hotkey_phrases or []) if h and h.strip()),
        key=len,
        reverse=True,
    )

    # Pass 1: per-segment n-gram loop collapse + hallucination drop.
    pass1: List[Dict[str, Any]] = []
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        text, loops = collapse_ngram_loops(text)
        stats.ngram_loops_collapsed += loops
        text = text.strip()
        if not text or is_hallucination(text):
            stats.removed_hallucination += 1
            continue
        pass1.append({"start": seg.get("start"), "end": seg.get("end"), "text": text})

    # Pass 2: collapse a run of consecutive identical/near-identical segments.
    pass2: List[Dict[str, Any]] = []
    for seg in pass1:
        if pass2 and _similar(normalize(pass2[-1]["text"]), normalize(seg["text"])):
            # Extend the kept segment's end to cover the whole run; keep its
            # (first) text and start.
            if seg.get("end") is not None:
                pass2[-1]["end"] = seg["end"]
            stats.removed_duplicate += 1
            continue
        pass2.append(dict(seg))

    # Pass 3: strip a trailing configured hotkey/command phrase from the
    # very last segment only (it is a command artifact spoken at the end of
    # a take, not something that can appear mid-transcript).
    if pass2 and hotkeys_norm:
        last = pass2[-1]
        for hk in hotkeys_norm:
            new_text, stripped = _strip_hotkey(last["text"], hk)
            if stripped:
                stats.hotkey_stripped += 1
                last["text"] = new_text
                break
        if not pass2[-1]["text"]:
            pass2.pop()

    stats.segments_out = len(pass2)
    return pass2, stats.as_dict()


def clean_text(text: str, *, hotkey_phrases: Optional[Sequence[str]] = None) -> Tuple[str, Dict[str, int]]:
    """Clean a flat transcript string (no per-segment timestamps available —
    command/endpoint providers). Treated as one segment: the phrase table and
    n-gram loop collapse still apply; duplicate-segment collapsing is a
    no-op on a single segment."""
    segments = [{"start": None, "end": None, "text": text}]
    cleaned, stats = clean_segments(segments, hotkey_phrases=hotkey_phrases)
    joined = " ".join(s["text"] for s in cleaned).strip()
    return joined, stats


def segments_to_text(segments: Sequence[Dict[str, Any]]) -> str:
    """Join cleaned segments back into a plain transcript string."""
    return " ".join(str(s.get("text") or "").strip() for s in segments if s.get("text")).strip()


def format_timestamp(seconds: Optional[float]) -> str:
    """``mm:ss`` (or ``h:mm:ss`` past an hour), the shape used in the
    meeting-notes transcript. ``None`` (no timestamp available) renders as
    ``"--:--"`` rather than guessing zero."""
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
