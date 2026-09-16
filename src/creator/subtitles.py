"""subtitles.py — WP16: transcript -> subtitle tracks, editorial QA,
SRT/VTT/ASS export and timeline retiming.

A ``subtitles`` :class:`~src.creator.documents.CreatorDocument` is one
caption TRACK for one language (SUB09: "pistas separadas para original,
traducción y captions descriptivos" — separate tracks are separate
documents, so switching which one a caller exports from never touches, let
alone deletes, the others; nothing in this module or ``documents.py``
allows a subtitles document to reference another one's cues).

Pipeline, each step a pure function over plain dicts (no I/O, no ffmpeg,
mirroring ``src/media_subtitles.py``'s own "pure functions over
already-validated segments" discipline):

* :func:`from_transcript` — segments a WP12 ``transcript`` document's cues
  (word-level timing when available, char-proportional fallback otherwise)
  into subtitle cues respecting a :class:`SubtitleProfile`'s CPL/lines/CPS/
  duration limits, never splitting a word and treating a speaker change or
  a long pause as a forced cue boundary.
* :func:`regenerate_from_transcript` — re-runs segmentation but keeps every
  cue a human has edited (``edited: true``) verbatim, dropping only the
  freshly-generated cues that overlap them. SUB05's acceptance criterion,
  word for word: editing a cue keeps it edited even when the transcript it
  came from is re-transcribed later.
* :func:`qa_report` — every cue against the document's own profile, with
  the ``{actual, target}`` pair the ficha asks for, never a bare pass/fail.
* :func:`apply_retiming` — maps cue ticks through a
  ``src.creator.timeline.retiming.RetimingMap`` (the SAME map
  ``timeline.retime``/``timeline.snap`` already produce); a cue landing in
  an unmappable zone is marked ``unmapped: true`` and KEPT, never dropped
  (SUB10's "nunca queda apuntando al audio eliminado" applied to captions).
* :func:`to_srt`/:func:`to_vtt`/:func:`to_ass`/:func:`export` — this
  module's OWN exporters, not ``src/media_subtitles.py``'s: a subtitle cue
  is a LIST of lines (up to ``max_lines``) and ``media_subtitles``'s
  ``_clean_text`` deliberately collapses embedded newlines into one line,
  which would silently destroy a two-line cue. The cue-numbering/timestamp
  SHAPE is intentionally the same convention, just reimplemented for the
  list-of-lines case; ``services.local_video.validate_segments`` stays the
  authority for the single-line transcribe/dub path this module does not
  touch.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import InvalidOperation
from .ops.model import Rational, RetimingMap, TimeRange, ticks_str

# ── profiles ────────────────────────────────────────────────────────────

#: Per-language defaults (SUB04: "no se fuerza una supuesta norma universal
#: de línea única" — every profile here allows two lines by default; a
#: caller wanting one line passes ``max_lines: 1`` explicitly).
DEFAULT_PROFILES: Dict[str, Dict[str, Any]] = {
    "default": {
        "max_chars_per_line": 42, "max_lines": 2, "cps_max": 17.0,
        "min_duration_seconds": 1.0, "max_duration_seconds": 7.0,
        "min_gap_seconds": 0.08, "pause_break_seconds": 0.6,
    },
    "en": {
        "max_chars_per_line": 42, "max_lines": 2, "cps_max": 17.0,
        "min_duration_seconds": 1.0, "max_duration_seconds": 7.0,
        "min_gap_seconds": 0.08, "pause_break_seconds": 0.6,
    },
    "es": {
        "max_chars_per_line": 42, "max_lines": 2, "cps_max": 16.0,
        "min_duration_seconds": 1.0, "max_duration_seconds": 7.0,
        "min_gap_seconds": 0.08, "pause_break_seconds": 0.6,
    },
    "ja": {
        # CJK: character-per-line and CPS targets are conventionally lower
        # (each glyph carries more visual weight) — a genuinely different
        # profile, not the "default" one silently reused for a language it
        # was never tuned for.
        "max_chars_per_line": 16, "max_lines": 2, "cps_max": 8.0,
        "min_duration_seconds": 1.0, "max_duration_seconds": 7.0,
        "min_gap_seconds": 0.08, "pause_break_seconds": 0.5,
    },
    "zh": {
        "max_chars_per_line": 16, "max_lines": 2, "cps_max": 8.0,
        "min_duration_seconds": 1.0, "max_duration_seconds": 7.0,
        "min_gap_seconds": 0.08, "pause_break_seconds": 0.5,
    },
}

_PROFILE_KEYS = (
    "max_chars_per_line", "max_lines", "cps_max",
    "min_duration_seconds", "max_duration_seconds",
    "min_gap_seconds", "pause_break_seconds",
)


def resolve_profile(language: Optional[str], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = dict(DEFAULT_PROFILES.get((language or "default").lower(), DEFAULT_PROFILES["default"]))
    if overrides:
        for key in _PROFILE_KEYS:
            if key in overrides and overrides[key] is not None:
                base[key] = overrides[key]
    _validate_profile(base)
    return base


def _validate_profile(profile: Dict[str, Any]) -> None:
    for key in _PROFILE_KEYS:
        if key not in profile:
            raise InvalidOperation(f"subtitle profile is missing {key!r}")
    if not (isinstance(profile["max_chars_per_line"], int) and profile["max_chars_per_line"] >= 4):
        raise InvalidOperation("profile.max_chars_per_line must be an integer >= 4")
    if not (isinstance(profile["max_lines"], int) and 1 <= profile["max_lines"] <= 4):
        raise InvalidOperation("profile.max_lines must be an integer in 1..4 (never forced to 1)")
    for key in ("cps_max", "min_duration_seconds", "max_duration_seconds",
                "min_gap_seconds", "pause_break_seconds"):
        v = profile[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            raise InvalidOperation(f"profile.{key} must be a positive number")
    if profile["min_duration_seconds"] >= profile["max_duration_seconds"]:
        raise InvalidOperation("profile.min_duration_seconds must be < max_duration_seconds")


# ── styles ──────────────────────────────────────────────────────────────

DEFAULT_STYLE: Dict[str, Any] = {
    "font_family": "Arial",
    "font_size": 42,
    "primary_color": "#FFFFFF",
    "outline_color": "#000000",
    "back_color": "#000000",
    "bold": False,
    "italic": False,
    "shadow": 1,
    # ASS numpad alignment (2 = bottom-center); "position" is the
    # human-readable mirror kept in sync by `_normalize_style`.
    "alignment": 2,
    "position": "bottom_center",
    "margin_l": 20,
    "margin_r": 20,
    "margin_v": 24,
}

_POSITION_TO_ALIGNMENT = {
    "bottom_left": 1, "bottom_center": 2, "bottom_right": 3,
    "middle_left": 9, "middle_center": 10, "middle_right": 11,
    "top_left": 5, "top_center": 6, "top_right": 7,
}
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FONT_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,64}$")


def _normalize_style(style: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merges ``style`` over :data:`DEFAULT_STYLE`, validating every field —
    a caller-supplied ``font_family`` that is not a bare font NAME (no path
    separators, no drive letters, no ``..``) is rejected rather than passed
    through to an eventual renderer/font lookup (SUB08: a style is data, it
    is never a path or a command)."""
    merged = dict(DEFAULT_STYLE)
    if style:
        merged.update({k: v for k, v in style.items() if k in DEFAULT_STYLE and v is not None})
    if not isinstance(merged["font_family"], str) or not _FONT_RE.match(merged["font_family"]):
        raise InvalidOperation("style.font_family must be a plain font name (letters/digits/space/-/_, <=64 chars)")
    for key in ("primary_color", "outline_color", "back_color"):
        if not isinstance(merged[key], str) or not _HEX_RE.match(merged[key]):
            raise InvalidOperation(f"style.{key} must be a '#RRGGBB' hex color")
    if not isinstance(merged["font_size"], int) or not (4 <= merged["font_size"] <= 400):
        raise InvalidOperation("style.font_size must be an integer in 4..400")
    if merged["position"] not in _POSITION_TO_ALIGNMENT:
        raise InvalidOperation(f"style.position must be one of {sorted(_POSITION_TO_ALIGNMENT)}")
    merged["alignment"] = _POSITION_TO_ALIGNMENT[merged["position"]]
    for key in ("bold", "italic"):
        merged[key] = bool(merged[key])
    if not isinstance(merged["shadow"], int) or not (0 <= merged["shadow"] <= 8):
        raise InvalidOperation("style.shadow must be an integer in 0..8 (pixel depth)")
    for key in ("margin_l", "margin_r", "margin_v"):
        if not isinstance(merged[key], int) or merged[key] < 0:
            raise InvalidOperation(f"style.{key} must be a non-negative integer")
    return merged


# ── tokenizing a transcript into words with timing ─────────────────────

@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int
    cue_id: str
    speaker_id: Optional[str]
    estimated: bool


def _transcript_tokens(transcript_content: Dict[str, Any]) -> List[_Token]:
    cues = transcript_content.get("cues") or []
    tokens: List[_Token] = []
    for cue in sorted(cues, key=lambda c: int(c["start_sample"])):
        if cue.get("editorial_status") == "needs_review":
            continue
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        cue_id = str(cue["id"])
        speaker_id = cue.get("speaker_id")
        start = int(cue["start_sample"])
        end = int(cue["end_sample"])
        words = [
            w for w in (cue.get("words") or [])
            if w.get("text") and w.get("start_sample") is not None and w.get("end_sample") is not None
        ]
        if words:
            for w in words:
                w_text = str(w["text"]).strip()
                if not w_text:
                    continue
                tokens.append(_Token(w_text, int(w["start_sample"]), int(w["end_sample"]),
                                     cue_id, speaker_id, estimated=False))
            continue
        # Fallback: split the cue's plain text into words and distribute the
        # cue's span proportionally by character length — never a fabricated
        # per-word timestamp claimed as measured (`estimated=True` says so).
        parts = text.split()
        if not parts:
            continue
        total_chars = sum(len(p) for p in parts) or 1
        span = max(end - start, 1)
        cursor = start
        acc_chars = 0
        for i, part in enumerate(parts):
            acc_chars += len(part)
            w_end = end if i == len(parts) - 1 else start + (span * acc_chars) // total_chars
            w_end = max(w_end, cursor + 1)
            tokens.append(_Token(part, cursor, w_end, cue_id, speaker_id, estimated=True))
            cursor = w_end
    return tokens


# ── packing tokens into subtitle cues ───────────────────────────────────

def _line_len(line: str) -> int:
    return len(line)


def _pack_tokens(tokens: Sequence[_Token], profile: Dict[str, Any], sample_rate: int) -> List[Dict[str, Any]]:
    max_cpl = profile["max_chars_per_line"]
    max_lines = profile["max_lines"]
    cps_max = profile["cps_max"]
    max_dur_ticks = round(profile["max_duration_seconds"] * sample_rate)
    pause_break_ticks = round(profile["pause_break_seconds"] * sample_rate)
    min_dur_ticks = round(profile["min_duration_seconds"] * sample_rate)

    cues: List[Dict[str, Any]] = []
    cur_lines: List[str] = [""]
    cur_tokens: List[_Token] = []

    def close() -> None:
        nonlocal cur_lines, cur_tokens
        lines = [l for l in cur_lines if l]
        if not lines or not cur_tokens:
            cur_lines, cur_tokens = [""], []
            return
        start = cur_tokens[0].start
        end = cur_tokens[-1].end
        if end - start < min_dur_ticks:
            end = start + min_dur_ticks
        speaker_ids = {t.speaker_id for t in cur_tokens}
        cues.append({
            "lines": lines,
            "start": start, "end": end,
            "speaker_id": next(iter(speaker_ids)) if len(speaker_ids) == 1 else None,
            "source_cue_ids": sorted({t.cue_id for t in cur_tokens}),
            "estimated_timing": any(t.estimated for t in cur_tokens),
        })
        cur_lines, cur_tokens = [""], []

    prev: Optional[_Token] = None
    for tok in tokens:
        if prev is not None and cur_tokens:
            forced = (tok.speaker_id != prev.speaker_id) or (tok.start - prev.end >= pause_break_ticks)
            if forced:
                close()

        candidate = (cur_lines[-1] + (" " if cur_lines[-1] else "") + tok.text)
        if _line_len(candidate) <= max_cpl:
            trial = cur_lines[:-1] + [candidate]
        elif len(cur_lines) < max_lines:
            trial = cur_lines + [tok.text]
        else:
            if cur_tokens:
                close()
            trial = [tok.text]

        if cur_tokens:
            prospective_start = cur_tokens[0].start
            prospective_end = tok.end
            dur_ticks = max(prospective_end - prospective_start, 1)
            chars = sum(_line_len(l) for l in trial)
            cps = chars / (dur_ticks / sample_rate)
            if dur_ticks > max_dur_ticks or cps > cps_max:
                close()
                trial = [tok.text]

        cur_lines = trial
        cur_tokens.append(tok)
        prev = tok

    close()
    return cues


def _enforce_min_gap(cues: List[Dict[str, Any]], min_gap_ticks: int) -> None:
    for i in range(len(cues) - 1):
        if cues[i]["end"] + min_gap_ticks > cues[i + 1]["start"]:
            cues[i]["end"] = max(cues[i]["start"] + 1, cues[i + 1]["start"] - min_gap_ticks)


def _finalize_cues(packed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, c in enumerate(packed):
        out.append({
            "id": f"sub_{i}",
            "start_ticks": ticks_str(c["start"]),
            "duration_ticks": ticks_str(max(c["end"] - c["start"], 1)),
            "lines": list(c["lines"]),
            "speaker_id": c["speaker_id"],
            "source_cue_ids": list(c["source_cue_ids"]),
            "editorial_status": "proposed",
            "edited": False,
            "unmapped": False,
            "estimated_timing": bool(c["estimated_timing"]),
            "style_id": None,
        })
    return out


def _majority_language(transcript_content: Dict[str, Any], override: Optional[str]) -> str:
    if override:
        return override
    counts: Dict[str, int] = {}
    for cue in transcript_content.get("cues") or []:
        lang = cue.get("language")
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return "und"
    return max(counts.items(), key=lambda kv: kv[1])[0]


def from_transcript(
    transcript_content: Dict[str, Any],
    *,
    language: Optional[str] = None,
    profile_overrides: Optional[Dict[str, Any]] = None,
    style: Optional[Dict[str, Any]] = None,
    source_transcript_id: Optional[str] = None,
) -> Dict[str, Any]:
    """A ``subtitles`` document ``content`` dict segmented from a WP12
    ``transcript`` document's ``content``."""
    sample_rate = transcript_content.get("sample_rate")
    if not isinstance(sample_rate, int) or sample_rate < 1:
        raise InvalidOperation("transcript.content.sample_rate must be a positive integer")
    lang = _majority_language(transcript_content, language)
    profile = resolve_profile(lang, profile_overrides)
    tokens = _transcript_tokens(transcript_content)
    packed = _pack_tokens(tokens, profile, sample_rate)
    _enforce_min_gap(packed, round(profile["min_gap_seconds"] * sample_rate))
    cues = _finalize_cues(packed)
    content: Dict[str, Any] = {
        "language": lang,
        "clock": {"ticks_per_second_numerator": ticks_str(sample_rate), "ticks_per_second_denominator": "1"},
        "profile": profile,
        "style": _normalize_style(style),
        "styles": {},
        "cues": cues,
    }
    if source_transcript_id:
        content["source_transcript_id"] = source_transcript_id
    return content


def _cue_overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    a_s, a_e = int(a["start_ticks"]), int(a["start_ticks"]) + int(a["duration_ticks"])
    b_s, b_e = int(b["start"]), int(b["end"])
    return a_s < b_e and b_s < a_e


def regenerate_from_transcript(
    old_content: Dict[str, Any],
    transcript_content: Dict[str, Any],
    *,
    profile_overrides: Optional[Dict[str, Any]] = None,
    style: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Re-segments ``transcript_content`` fresh, then restores every cue the
    old document had ``edited: true`` verbatim over the freshly generated
    ones it overlaps (dropping only the OVERLAPPED fresh cues — a human
    edit always prevails over a later transcript, per SUB05's acceptance
    criterion; an edited cue with no overlap in the new pass is still kept,
    never silently discarded)."""
    lang = old_content.get("language")
    fresh = from_transcript(
        transcript_content, language=lang,
        profile_overrides=profile_overrides or old_content.get("profile"),
        style=style or old_content.get("style"),
        source_transcript_id=old_content.get("source_transcript_id"),
    )
    fresh_cues = fresh["cues"]
    edited = [c for c in old_content.get("cues", []) if c.get("edited")]

    dropped_fresh_ids = set()
    for ec in edited:
        for fc in fresh_cues:
            if fc["id"] in dropped_fresh_ids:
                continue
            same_speaker = (ec.get("speaker_id") is None or fc.get("speaker_id") is None
                            or ec.get("speaker_id") == fc.get("speaker_id"))
            if same_speaker and _cue_overlaps(
                ec, {"start": int(fc["start_ticks"]), "end": int(fc["start_ticks"]) + int(fc["duration_ticks"])},
            ):
                dropped_fresh_ids.add(fc["id"])

    remaining_fresh = [fc for fc in fresh_cues if fc["id"] not in dropped_fresh_ids]
    merged = sorted(edited + remaining_fresh, key=lambda c: int(c["start_ticks"]))
    # Re-id sequentially so ids stay dense/predictable; edited cues keep
    # their own `id` value only if unique, otherwise get a fresh one — a
    # collision cannot happen here in practice (fresh ids are `sub_i`),
    # but this stays defensive rather than assuming it.
    seen_ids = set()
    for i, cue in enumerate(merged):
        if cue["id"] in seen_ids:
            cue["id"] = f"sub_r{i}"
        seen_ids.add(cue["id"])
    fresh["cues"] = merged
    return fresh


# ── QA ──────────────────────────────────────────────────────────────────

def qa_report(content: Dict[str, Any]) -> Dict[str, Any]:
    clock = Rational.from_dict(content["clock"])
    profile = content["profile"]
    issues: List[Dict[str, Any]] = []
    for cue in content.get("cues", []):
        start = int(cue["start_ticks"])
        dur = int(cue["duration_ticks"])
        duration_seconds = float(clock.seconds_of(dur))
        chars = sum(len(l) for l in cue["lines"])
        cps = (chars / duration_seconds) if duration_seconds > 0 else float("inf")
        cue_issues: List[Dict[str, Any]] = []
        if len(cue["lines"]) > profile["max_lines"]:
            cue_issues.append({"rule": "max_lines", "actual": len(cue["lines"]), "target": profile["max_lines"]})
        for line in cue["lines"]:
            if len(line) > profile["max_chars_per_line"]:
                cue_issues.append({
                    "rule": "max_chars_per_line", "actual": len(line),
                    "target": profile["max_chars_per_line"], "line": line,
                })
        if cps > profile["cps_max"]:
            cue_issues.append({"rule": "cps_max", "actual": round(cps, 2), "target": profile["cps_max"]})
        if duration_seconds < profile["min_duration_seconds"]:
            cue_issues.append({
                "rule": "min_duration_seconds", "actual": round(duration_seconds, 3),
                "target": profile["min_duration_seconds"],
            })
        if duration_seconds > profile["max_duration_seconds"]:
            cue_issues.append({
                "rule": "max_duration_seconds", "actual": round(duration_seconds, 3),
                "target": profile["max_duration_seconds"],
            })
        if cue.get("unmapped"):
            cue_issues.append({"rule": "unmapped_after_retiming", "actual": True, "target": False})
        if cue_issues:
            issues.append({"cue_id": cue["id"], "start_ticks": cue["start_ticks"], "issues": cue_issues})
    return {
        "total_cues": len(content.get("cues", [])),
        "cues_with_issues": len(issues),
        "issues": issues,
    }


# ── retiming ─────────────────────────────────────────────────────────────

def apply_retiming(
    content: Dict[str, Any],
    retiming_segments: List[Dict[str, Any]],
    *,
    direction: str = "source_to_dest",
) -> Dict[str, Any]:
    """Maps every cue's ticks through ``retiming_segments`` (the same shape
    ``timeline.retime``/``timeline.snap`` store on a clip). A cue whose
    start or end falls outside every segment, or inside a
    ``mappable: false`` one, is marked ``unmapped: true`` and its ticks are
    LEFT UNCHANGED — kept, never dropped, so an editor can find and fix it
    rather than have it vanish from the track."""
    from .timeline.retiming import map_time

    rmap = RetimingMap.from_list(retiming_segments)
    new_content = copy.deepcopy(content)
    for cue in new_content["cues"]:
        start = int(cue["start_ticks"])
        dur = int(cue["duration_ticks"])
        last_inclusive = max(start + dur - 1, start)
        try:
            r_start = map_time(rmap, start, direction=direction)
            r_end = map_time(rmap, last_inclusive, direction=direction)
        except InvalidOperation:
            cue["unmapped"] = True
            continue
        if r_start.source_ticks is None or r_end.source_ticks is None:
            cue["unmapped"] = True
            continue
        new_start = r_start.source_ticks
        new_end = r_end.source_ticks + 1
        if new_end <= new_start:
            new_end = new_start + 1
        cue["start_ticks"] = ticks_str(new_start)
        cue["duration_ticks"] = ticks_str(new_end - new_start)
        cue["unmapped"] = False
    return new_content


# ── export: SRT / VTT / ASS ───────────────────────────────────────────

def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _vtt_timestamp(seconds: float) -> str:
    return _srt_timestamp(seconds).replace(",", ".")


def _ass_timestamp(seconds: float) -> str:
    total_cs = max(0, round(seconds * 100))
    hours, rest = divmod(total_cs, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, cs = divmod(rest, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _clean_line(text: str) -> str:
    """One display line never itself contains a newline (that would silently
    add an extra line the profile's ``max_lines`` never approved)."""
    return " ".join(str(text or "").split())


def _cue_seconds(cue: Dict[str, Any], clock: Rational) -> Tuple[float, float]:
    start = int(cue["start_ticks"])
    dur = int(cue["duration_ticks"])
    return float(clock.seconds_of(start)), float(clock.seconds_of(start + dur))


def _ordered_cues(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted(content.get("cues", []), key=lambda c: int(c["start_ticks"]))


def to_srt(content: Dict[str, Any]) -> str:
    clock = Rational.from_dict(content["clock"])
    blocks = []
    for i, cue in enumerate(_ordered_cues(content), start=1):
        s, e = _cue_seconds(cue, clock)
        text = "\n".join(_clean_line(l) for l in cue["lines"] if _clean_line(l))
        if not text:
            continue
        blocks.append(f"{i}\n{_srt_timestamp(s)} --> {_srt_timestamp(e)}\n{text}")
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def to_vtt(content: Dict[str, Any]) -> str:
    clock = Rational.from_dict(content["clock"])
    blocks = []
    for cue in _ordered_cues(content):
        s, e = _cue_seconds(cue, clock)
        text = "\n".join(_clean_line(l) for l in cue["lines"] if _clean_line(l))
        if not text:
            continue
        blocks.append(f"{_vtt_timestamp(s)} --> {_vtt_timestamp(e)}\n{text}")
    body = "\n\n".join(blocks)
    return "WEBVTT\n\n" + body + ("\n" if body else "")


def _ass_escape(text: str) -> str:
    """Strips every literal ``{``/``}`` from user-authored text. An ASS
    override block (``\\fn`` a font NAME, ``\\p`` a vector drawing string,
    ``\\move``/``\\pos`` coordinates) is recognised ONLY between a literal
    ``{`` and ``}`` — with those two characters gone from the text, a bare
    backslash sequence is inert, just a literal string of characters shown
    on screen, never parsed as a command (SUB08: "una etiqueta maliciosa
    no se interpreta como ruta o comando")."""
    return text.replace("{", "(").replace("}", ")")


def _ass_color(hex_color: str) -> str:
    """``#RRGGBB`` -> ASS's ``&HAABBGGRR`` (BGR, alpha 00 = opaque)."""
    r, g, b = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f"&H00{b.upper()}{g.upper()}{r.upper()}"


def _resolved_styles(content: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    default_style = _normalize_style(content.get("style"))
    styles = {"Default": default_style}
    for style_id, override in (content.get("styles") or {}).items():
        merged = dict(default_style)
        merged.update({k: v for k, v in (override or {}).items() if k in DEFAULT_STYLE and v is not None})
        styles[str(style_id)] = _normalize_style(merged)
    return styles


def _ass_style_line(name: str, style: Dict[str, Any]) -> str:
    bold = "-1" if style["bold"] else "0"
    italic = "-1" if style["italic"] else "0"
    return (
        f"Style: {name},{style['font_family']},{style['font_size']},"
        f"{_ass_color(style['primary_color'])},{_ass_color(style['primary_color'])},"
        f"{_ass_color(style['outline_color'])},{_ass_color(style['back_color'])},"
        f"{bold},{italic},0,0,100,100,0,0,1,2,{style['shadow']},{style['alignment']},"
        f"{style['margin_l']},{style['margin_r']},{style['margin_v']},1"
    )


def to_ass(content: Dict[str, Any]) -> str:
    clock = Rational.from_dict(content["clock"])
    styles = _resolved_styles(content)
    lines = [
        "[Script Info]",
        "; Generated by src/creator/subtitles.py (WP16) — do not hand-edit paths/fonts here.",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for style_id, style in styles.items():
        lines.append(_ass_style_line(style_id, style))
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for cue in _ordered_cues(content):
        s, e = _cue_seconds(cue, clock)
        style_id = cue.get("style_id") or "Default"
        if style_id not in styles:
            style_id = "Default"
        text = "\\N".join(_ass_escape(_clean_line(l)) for l in cue["lines"] if _clean_line(l))
        if not text:
            continue
        lines.append(f"Dialogue: 0,{_ass_timestamp(s)},{_ass_timestamp(e)},{style_id},,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


_EXPORTERS = {
    "srt": ("subtitles.srt", "application/x-subrip", to_srt),
    "vtt": ("subtitles.vtt", "text/vtt", to_vtt),
    "ass": ("subtitles.ass", "text/x-ssa", to_ass),
}


def export(content: Dict[str, Any], fmt: str) -> Dict[str, str]:
    entry = _EXPORTERS.get(fmt)
    if entry is None:
        raise InvalidOperation(f"unsupported subtitle format {fmt!r}; use 'srt', 'vtt' or 'ass'")
    filename, media_type, fn = entry
    return {"text": fn(content), "filename": filename, "media_type": media_type}


# ── renderer hook (WP14 burns this in; no dependency on that module here) ─

_FFMPEG_SUBTITLES_ESCAPE = str.maketrans({":": r"\:", "'": r"\'", "\\": r"\\\\", "[": r"\[", "]": r"\]"})


def ffmpeg_burn_filter(ass_or_srt_path: str) -> str:
    """The ``-vf`` filter fragment to burn ``ass_or_srt_path`` into a video
    with ffmpeg's own ``subtitles`` filter — escaped per the filter's own
    argument syntax (colons and brackets are argument separators there), so
    a path is never interpolated unescaped into a shell/filter string."""
    escaped = ass_or_srt_path.translate(_FFMPEG_SUBTITLES_ESCAPE)
    return f"subtitles={escaped}"


__all__ = [
    "DEFAULT_PROFILES", "DEFAULT_STYLE", "resolve_profile",
    "from_transcript", "regenerate_from_transcript", "qa_report",
    "apply_retiming", "to_srt", "to_vtt", "to_ass", "export",
    "ffmpeg_burn_filter",
]
