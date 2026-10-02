"""Subtitle cues: writers (SRT, WebVTT, LRC, plain text, ASS helpers) and tolerant parsers (SRT, WebVTT, LRC, YouTube json3).

Five apps carried their own SRT formatter and only one of them rounded correctly. Here the time is rounded to the millisecond
**before** it is split into fields, so ``59.9996`` s is ``00:01:00,000`` and never the invalid ``00:00:59,1000``.

Standard library only. The Node twin is ``js/hoard-commons/media.js`` (``toSrt``, ``parseVtt`` …); both are checked against
``tests/vectors/media_subs.json``. Rounding is "half up" on both sides (Python's ``round`` is half-to-even, JavaScript's is not).

A :class:`Cue` is ``start_s``, ``end_s`` (seconds; ``None`` when the file carries no timing), ``text``, an optional ``speaker`` and the
word timings (``words``: ``{"start_s", "end_s", "word", "p"}`` dicts) a transcriber produced. Every function that takes cues also
accepts plain dicts with those keys.

* :func:`subtitle_cues` / :func:`subtitle_text` — what a downloaded caption file *says*, whatever its format: rolling auto-captions
  (each line shown twice) are collapsed and the lines are joined into continuous speech (a line break stays only where the speaker stopped).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "Cue", "as_cue", "cues_from_segments", "srt_time", "vtt_time", "to_srt", "to_vtt", "to_lrc", "to_txt", "parse_srt", "parse_vtt",
    "parse_lrc", "parse_json3", "dedupe_rolling", "cues_to_text", "subtitle_cues", "subtitle_text", "ass_escape", "ass_color",
    "ass_time", "CUE_GAP_SECONDS",
]

#: Longest silence inside a sentence; a longer one means the speaker stopped.
CUE_GAP_SECONDS = 1.2


@dataclass
class Cue:
    start_s: Optional[float]
    end_s: Optional[float]
    text: str
    speaker: str = ""
    words: tuple = ()


def as_cue(value: "Cue | Mapping[str, Any]") -> Cue:
    """A :class:`Cue` from a cue or a dict (``start_s``, ``end_s``, ``text`` and optionally ``speaker``, ``words``)."""
    if isinstance(value, Cue):
        return value
    return Cue(value.get("start_s"), value.get("end_s"), str(value.get("text") or ""), str(value.get("speaker") or ""),
               tuple(value.get("words") or ()))


def cues_from_segments(segments: Iterable[Mapping[str, Any]]) -> list[Cue]:
    """Cues from transcriber segments (``{"start_s", "end_s", "text", "speaker"?, "words"?}``)."""
    return [as_cue(s) for s in segments]


# ---------------------------------------------------------------- time --

def _half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _fields(t: Optional[float]) -> tuple[int, int, int, int]:
    """(hours, minutes, seconds, milliseconds), rounded to the millisecond first so a carry reaches the next field."""
    total_ms = _half_up(max(0.0, float(t or 0.0)) * 1000)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    total_m = total_s // 60
    return total_m // 60, total_m % 60, total_s % 60, ms


def srt_time(t: Optional[float]) -> str:
    """``HH:MM:SS,mmm``."""
    h, m, s, ms = _fields(t)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def vtt_time(t: Optional[float]) -> str:
    """``HH:MM:SS.mmm``."""
    h, m, s, ms = _fields(t)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _span(c: Cue, min_duration_s: float) -> tuple[float, float]:
    start = max(0.0, float(c.start_s or 0.0))
    end = float(c.end_s) if c.end_s is not None else start
    return start, max(end, start, start + min_duration_s)


def _block_text(text: str) -> str:
    """Cue text for SRT/VTT: no blank lines inside (a blank line ends a cue)."""
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n", t)


# ---------------------------------------------------------------- writers --

def to_srt(cues: Iterable["Cue | Mapping[str, Any]"], *, speakers: bool = False, min_duration_s: float = 0.0) -> str:
    """SubRip. Cues with no text are skipped; ``speakers=True`` prefixes ``Name: ``; ``min_duration_s`` stretches a too-short cue."""
    blocks: list[str] = []
    for raw in cues:
        c = as_cue(raw)
        text = _block_text(c.text)
        if not text:
            continue
        if speakers and c.speaker:
            text = f"{c.speaker}: {text}"
        start, end = _span(c, min_duration_s)
        blocks.append(f"{len(blocks) + 1}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


def _vtt_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_vtt(cues: Iterable["Cue | Mapping[str, Any]"], *, speakers: bool = False, min_duration_s: float = 0.0) -> str:
    """WebVTT; text is escaped (``& < >``), a speaker becomes a ``<v Name>`` voice tag."""
    blocks: list[str] = []
    for raw in cues:
        c = as_cue(raw)
        text = _block_text(c.text)
        if not text:
            continue
        text = _vtt_escape(text)
        if speakers and c.speaker:
            text = f"<v {_vtt_escape(c.speaker).replace(chr(10), ' ')}>{text}"
        start, end = _span(c, min_duration_s)
        blocks.append(f"{vtt_time(start)} --> {vtt_time(end)}\n{text}\n")
    return "WEBVTT\n\n" + "\n".join(blocks) if blocks else "WEBVTT\n"


def _lrc_time(t: Optional[float]) -> str:
    cs = _half_up(max(0.0, float(t or 0.0)) * 100)
    return f"[{cs // 6000:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}]"


def to_lrc(cues: Iterable["Cue | Mapping[str, Any]"], *, metadata: Optional[Mapping[str, str]] = None) -> str:
    """LRC lyrics: ``[mm:ss.xx]line``; ``metadata`` (``{"ti": "Title", "ar": "Artist"}``) becomes the ``[ti:Title]`` header lines."""
    lines = [f"[{k}:{v}]" for k, v in (metadata or {}).items()]
    for raw in cues:
        c = as_cue(raw)
        text = re.sub(r"\s+", " ", str(c.text or "")).strip()
        if text:
            lines.append(f"{_lrc_time(c.start_s)}{text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _clock_label(t: Optional[float]) -> str:
    total = max(0, int(math.floor(float(t or 0.0))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def to_txt(cues: Iterable["Cue | Mapping[str, Any]"], *, timestamps: bool = False, speakers: bool = True) -> str:
    """Plain text, one cue per line: ``[mm:ss] Name: text`` (the timestamp and the name are optional)."""
    lines: list[str] = []
    for raw in cues:
        c = as_cue(raw)
        text = re.sub(r"\s+", " ", str(c.text or "")).strip()
        if not text:
            continue
        head = f"[{_clock_label(c.start_s)}] " if timestamps else ""
        who = f"{c.speaker}: " if speakers and c.speaker else ""
        lines.append(f"{head}{who}{text}")
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------- ASS helpers --

def ass_escape(text: Any) -> str:
    """Text for an ASS ``Dialogue`` line: backslashes become ``/`` and braces ``()`` (so no override tag can be injected), newlines ``\\N``."""
    return str(text or "").replace("\\", "/").replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """``#RRGGBB`` (or ``RRGGBB`` / ``#RGB``) as ASS ``&HAABBGGRR`` (``alpha`` 0 = opaque … 255 = transparent)."""
    h = str(hex_color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"not a #RRGGBB colour: {hex_color!r}")
    a = max(0, min(255, int(alpha)))
    return f"&H{a:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_time(t: Optional[float]) -> str:
    """``H:MM:SS.cc`` (centiseconds), the ASS time format."""
    cs = _half_up(max(0.0, float(t or 0.0)) * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


# ---------------------------------------------------------------- parsers --

_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$")
_ENTITIES = {"&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&apos;": "'", "&nbsp;": " "}


def _clock(raw: str) -> Optional[float]:
    m = _CLOCK.match(raw.strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4).ljust(3, "0")) / 1000


def _decode(text: str) -> str:
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)) if int(m.group(1), 16) <= 0x10FFFF else "", text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) <= 0x10FFFF else "", text)
    for k, v in _ENTITIES.items():
        text = text.replace(k, v)
    return text.replace("&amp;", "&")


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", _decode(re.sub(r"<[^>]+>", "", line))).strip()


_VOICE = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]+)>")
_HEADER = re.compile(r"^(?:WEBVTT|NOTE|STYLE|REGION|Kind:|Language:)")


def _parse_text(content: str, per_line: bool, untimed: bool) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n{2,}", content.replace("\r", "")):
        lines = [ln.strip() for ln in block.split("\n")]
        lines = [ln for ln in lines if ln]
        if not lines or re.match(r"^(?:NOTE|STYLE|REGION)\b", lines[0]):
            continue
        time_at = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        start = end = None
        speaker = ""
        texts: list[str] = []
        if time_at >= 0:
            a, _, b = lines[time_at].partition("-->")
            start = _clock(a)
            end = _clock((b.strip().split() or [""])[0])
            body = lines[time_at + 1:]  # lines before the time line are the cue identifier
        else:
            if not untimed:
                continue
            body = [ln for ln in lines if not _HEADER.match(ln) and not re.fullmatch(r"\d+", ln)]
        for ln in body:
            v = _VOICE.search(ln)
            if v and not speaker:
                speaker = _decode(v.group(1)).strip()
            clean = _clean_line(ln)
            if clean:
                texts.append(clean)
        if not texts:
            continue
        if per_line:
            cues.extend(Cue(start, end, t, speaker) for t in texts)
        else:
            cues.append(Cue(start, end, "\n".join(texts), speaker))
    return cues


def parse_srt(content: str, *, per_line: bool = False) -> list[Cue]:
    """SubRip (also accepts ``.`` as the decimal mark). One cue per block (multi-line text joined with ``\\n``); ``per_line=True``
    gives one cue per text line. Blocks without a time line are ignored."""
    return _parse_text(str(content or ""), per_line, False)


def parse_vtt(content: str, *, per_line: bool = False) -> list[Cue]:
    """WebVTT: header, ``NOTE`` / ``STYLE`` / ``REGION`` blocks, cue identifiers, cue settings and inline tags are dropped, HTML
    entities decoded, a ``<v Name>`` voice tag becomes ``speaker``."""
    return _parse_text(str(content or ""), per_line, False)


_LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_LRC_LINE = re.compile(r"^((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)\s*(.*)$")


def parse_lrc(content: str, *, last_s: float = 4.0) -> list[Cue]:
    """LRC lyrics. A line with several stamps yields several cues; ``[offset:+500]`` is applied; a cue ends where the next line
    starts (the last one lasts ``last_s``); enhanced ``<mm:ss.xx>`` word tags are dropped; metadata tags are ignored."""
    offset_ms = 0
    entries: list[tuple[float, str]] = []
    for raw in str(content or "").replace("\r", "").split("\n"):
        line = raw.strip()
        off = re.match(r"^\[offset:\s*([+-]?\d+)\s*\]$", line, re.I)
        if off:
            offset_ms = int(off.group(1))
            continue
        m = _LRC_LINE.match(line)
        if not m:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>", "", m.group(2))).strip()
        for s in _LRC_STAMP.finditer(m.group(1)):
            frac = s.group(3) or ""
            t = int(s.group(1)) * 60 + int(s.group(2)) + (int(frac) / 10 ** len(frac) if frac else 0)
            entries.append((t, text))
    entries = [(t - offset_ms / 1000, text) for t, text in entries]
    entries.sort(key=lambda e: e[0])
    cues: list[Cue] = []
    for i, (t, text) in enumerate(entries):
        if text:
            cues.append(Cue(t, entries[i + 1][0] if i + 1 < len(entries) else t + last_s, text))
    return cues


def parse_json3(content: str, *, dedupe: bool = False) -> list[Cue]:
    """YouTube's ``json3`` caption format (``events[].tStartMs / dDurationMs / segs[].utf8``). Invalid JSON gives ``[]``."""
    try:
        data = json.loads(str(content or "").strip())
    except ValueError:
        return []
    out: list[Cue] = []
    events = data.get("events") if isinstance(data, dict) else None
    for e in events or []:
        if not isinstance(e, dict):
            continue
        text = re.sub(r"\s+", " ", "".join(str(x.get("utf8") or "") for x in (e.get("segs") or []) if isinstance(x, dict))).strip()
        if not text:
            continue
        t0, dur = e.get("tStartMs"), e.get("dDurationMs")
        start = t0 / 1000 if isinstance(t0, (int, float)) and not isinstance(t0, bool) else None
        end = start + dur / 1000 if start is not None and isinstance(dur, (int, float)) and not isinstance(dur, bool) else start
        out.append(Cue(start, end, text))
    return dedupe_rolling(out) if dedupe else out


def dedupe_rolling(cues: Iterable["Cue | Mapping[str, Any]"]) -> list[Cue]:
    """Collapse the repeats of rolling auto-captions: a line identical to one of the last three is dropped (its end time extends
    that cue), and a line that continues the previous one (``"hello"`` then ``"hello world"``) replaces it."""
    out: list[Cue] = []
    for raw in cues:
        line = as_cue(raw)
        same = next((x for x in out[-3:] if x.text == line.text), None)
        if same is not None:
            if line.end_s is not None and (same.end_s is None or line.end_s > same.end_s):
                same.end_s = line.end_s
            continue
        prev = out[-1] if out else None
        if prev is not None and line.text.startswith(prev.text + " "):
            prev.text = line.text
            if line.end_s is not None:
                prev.end_s = line.end_s
            continue
        out.append(replace(line))
    return out


def cues_to_text(cues: Sequence["Cue | Mapping[str, Any]"], gap_s: float = CUE_GAP_SECONDS) -> str:
    """Continuous speech from cues: lines are joined with a space; a newline is kept where the speaker stopped (a silence longer
    than ``gap_s``) or the previous line ended a sentence."""
    items = [as_cue(c) for c in cues]
    out = ""
    for i, cue in enumerate(items):
        if i > 0:
            prev = items[i - 1]
            gap = cue.start_s - prev.end_s if prev.end_s is not None and cue.start_s is not None else 0
            out += "\n" if gap > gap_s or re.search(r"[.!?…]$", prev.text) else " "
        out += cue.text
    return out


def subtitle_cues(content: str) -> list[Cue]:
    """The lines of a caption file (WebVTT, SRT or json3, detected) with their timing, one cue per text line, rolling repeats removed."""
    trimmed = str(content or "").strip()
    raw = parse_json3(trimmed) if trimmed.startswith("{") else _parse_text(str(content or ""), True, True)
    return dedupe_rolling(raw)


def subtitle_text(content: str, gap_s: float = CUE_GAP_SECONDS) -> str:
    """Plain text of a caption file as continuous speech (see :func:`cues_to_text`)."""
    return cues_to_text(subtitle_cues(content), gap_s)
