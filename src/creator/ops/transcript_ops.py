"""Transcript (multi-speaker) operations — WP12.

The arch doc is explicit about the legacy contract this must NOT break
(``docs/spec/creator/plan/docs/03_ARQUITECTURA_Y_CONTRATOS.md`` § "Tiempo,
regiones y operaciones"): ``services.local_video.validate_segments``
presumes ordered, non-overlapping segments, and that stays true for the
short transcribe/dub path it already serves. This module adds a SEPARATE,
editorial notion of overlap scoped per speaker — two different speakers can
genuinely talk over each other; the same speaker cannot say two things at
once — and only narrows a multi-speaker cue list down to an
ordered/non-overlapping SUBSET when a caller actually wants an SRT/VTT
file, reusing ``src/media_subtitles.py`` (imported, not reimplemented) for
the format itself.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from ..errors import InvalidOperation
from .model import find_by_id, index_by_id, require_kind


def _cues(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    cues = content.get("cues")
    if not isinstance(cues, list):
        raise InvalidOperation("transcript.content.cues must be an array")
    return cues


def _range_from_start_end(cue: Dict[str, Any]) -> Tuple[int, int]:
    """``cues[].start_sample``/``end_sample`` are absolute sample offsets
    (not start+duration), unlike the timeline's clips — read them as such."""
    start = cue.get("start_sample")
    end = cue.get("end_sample")
    if not isinstance(start, str) or not start.isdigit():
        raise InvalidOperation(f"cue {cue.get('id')!r}.start_sample must be a non-negative integer string")
    if not isinstance(end, str) or not end.isdigit() or int(end) <= int(start):
        raise InvalidOperation(f"cue {cue.get('id')!r}.end_sample must be an integer string > start_sample")
    return int(start), int(end)


def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _check_no_overlap_within_speaker(cues: List[Dict[str, Any]]) -> None:
    by_speaker: Dict[Optional[str], List[Tuple[str, Tuple[int, int]]]] = {}
    for cue in cues:
        by_speaker.setdefault(cue.get("speaker_id"), []).append((cue["id"], _range_from_start_end(cue)))
    for speaker_id, entries in by_speaker.items():
        entries.sort(key=lambda pair: pair[1][0])
        for (id_a, range_a), (id_b, range_b) in zip(entries, entries[1:]):
            if _overlaps(range_a, range_b):
                raise InvalidOperation(
                    f"cues {id_a!r} and {id_b!r} overlap for the same speaker {speaker_id!r}"
                )


def edit_segment(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Edits an existing cue's editable fields in place (by ``object_id``).
    ``speaker_id`` is deliberately NOT editable here — use
    ``assign_speaker`` so that op name always shows up in history for that
    kind of change."""
    require_kind(doc, "transcript")
    content = copy.deepcopy(doc["content"])
    cue = find_by_id(_cues(content), op.get("object_id"), what="cue")

    if "text" in op:
        text = op["text"]
        if not isinstance(text, str) or not text:
            raise InvalidOperation("transcript.edit_segment 'text' must be a non-empty string")
        cue["text"] = text
    if "language" in op:
        language = op["language"]
        if not isinstance(language, str) or not language:
            raise InvalidOperation("transcript.edit_segment 'language' must be a non-empty string")
        cue["language"] = language
    if "editorial_status" in op:
        status = op["editorial_status"]
        if status not in ("proposed", "accepted", "needs_review"):
            raise InvalidOperation("transcript.edit_segment 'editorial_status' is invalid")
        cue["editorial_status"] = status
    if "start_sample" in op or "end_sample" in op:
        start = str(op.get("start_sample", cue["start_sample"]))
        end = str(op.get("end_sample", cue["end_sample"]))
        cue["start_sample"], cue["end_sample"] = start, end
        _range_from_start_end(cue)

    _check_no_overlap_within_speaker(_cues(content))
    return content


def split_cue(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Splits one cue into two at an absolute ``at_sample``, strictly
    inside its span. Text for each half is given explicitly (``first_text``/
    ``second_text``) — this op does not guess a word boundary from a plain
    string; a caller with word-level timing should compute the split from
    ``words`` and pass the two texts it decided on."""
    require_kind(doc, "transcript")
    content = copy.deepcopy(doc["content"])
    cues = _cues(content)
    idx = index_by_id(cues, op.get("object_id"), what="cue")
    cue = cues[idx]
    start, end = _range_from_start_end(cue)

    at_sample = op.get("at_sample")
    if not isinstance(at_sample, (int, str)) or isinstance(at_sample, bool):
        raise InvalidOperation("transcript.split_cue requires an integer/string 'at_sample'")
    at_sample = int(at_sample)
    if not (start < at_sample < end):
        raise InvalidOperation("transcript.split_cue 'at_sample' must be strictly inside the cue")

    first_text = op.get("first_text")
    second_text = op.get("second_text")
    if not isinstance(first_text, str) or not first_text or not isinstance(second_text, str) or not second_text:
        raise InvalidOperation("transcript.split_cue requires non-empty 'first_text' and 'second_text'")

    new_cue_id = op.get("new_cue_id")
    if not isinstance(new_cue_id, str) or not new_cue_id:
        raise InvalidOperation("transcript.split_cue requires a non-empty string 'new_cue_id'")
    if any(c["id"] == new_cue_id for c in cues):
        raise InvalidOperation(f"cue id already exists: {new_cue_id!r}")

    first = copy.deepcopy(cue)
    first["text"] = first_text
    first["end_sample"] = str(at_sample)
    first["words"] = []

    second = copy.deepcopy(cue)
    second["id"] = new_cue_id
    second["text"] = second_text
    second["start_sample"] = str(at_sample)
    second["words"] = []

    cues[idx] = first
    cues.insert(idx + 1, second)
    _check_no_overlap_within_speaker(cues)
    return content


def merge_cues(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Merges two cues of the SAME speaker into one spanning both, in
    ``merged_text`` order given explicitly by the caller (never
    auto-concatenated — punctuation/spacing is an editorial choice)."""
    require_kind(doc, "transcript")
    content = copy.deepcopy(doc["content"])
    cues = _cues(content)
    first_idx = index_by_id(cues, op.get("first_object_id"), what="cue")
    second_idx = index_by_id(cues, op.get("second_object_id"), what="cue")
    if first_idx == second_idx:
        raise InvalidOperation("transcript.merge_cues requires two distinct cues")
    first, second = cues[first_idx], cues[second_idx]
    if first.get("speaker_id") != second.get("speaker_id"):
        raise InvalidOperation("transcript.merge_cues requires both cues to share the same speaker_id")

    merged_text = op.get("merged_text")
    if not isinstance(merged_text, str) or not merged_text:
        raise InvalidOperation("transcript.merge_cues requires a non-empty 'merged_text'")

    s1, e1 = _range_from_start_end(first)
    s2, e2 = _range_from_start_end(second)
    merged = copy.deepcopy(first)
    merged["text"] = merged_text
    merged["start_sample"] = str(min(s1, s2))
    merged["end_sample"] = str(max(e1, e2))
    merged["words"] = []

    keep_idx = min(first_idx, second_idx)
    drop_idx = max(first_idx, second_idx)
    cues[keep_idx] = merged
    del cues[drop_idx]
    _check_no_overlap_within_speaker(cues)
    return content


def assign_speaker(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets (or clears, with ``speaker_id: null``) a cue's speaker. If a
    non-null ``speaker_id`` is given and ``content['speaker_tracks']``
    exists (see :func:`add_track`), it must name a known track — keeps
    speaker ids from silently drifting from whatever the UI showed when a
    track was created."""
    require_kind(doc, "transcript")
    content = copy.deepcopy(doc["content"])
    cue = find_by_id(_cues(content), op.get("object_id"), what="cue")
    speaker_id = op.get("speaker_id")
    if speaker_id is not None and not (isinstance(speaker_id, str) and speaker_id):
        raise InvalidOperation("transcript.assign_speaker 'speaker_id' must be a non-empty string or null")
    tracks = content.get("speaker_tracks")
    if speaker_id is not None and isinstance(tracks, list) and tracks:
        if not any(t.get("id") == speaker_id for t in tracks):
            raise InvalidOperation(f"unknown speaker track: {speaker_id!r}")
    cue["speaker_id"] = speaker_id
    _check_no_overlap_within_speaker(_cues(content))
    return content


def add_track(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Registers a named speaker track (additive ``content['speaker_tracks']``
    list — not part of the reference schema's required transcript fields,
    so an older transcript document without it keeps working; it only
    starts existing once this op is first applied)."""
    require_kind(doc, "transcript")
    content = copy.deepcopy(doc["content"])
    track_id = op.get("track_id")
    if not isinstance(track_id, str) or not track_id:
        raise InvalidOperation("transcript.add_track requires a non-empty string 'track_id'")
    label = op.get("label", track_id)
    if not isinstance(label, str) or not label:
        raise InvalidOperation("transcript.add_track 'label' must be a non-empty string")
    tracks = content.setdefault("speaker_tracks", [])
    if any(t.get("id") == track_id for t in tracks):
        raise InvalidOperation(f"speaker track already exists: {track_id!r}")
    tracks.append({"id": track_id, "label": label})
    return content


def compatible_srt_segments(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reduces a (possibly multi-speaker, overlapping) cue list to the
    largest ordered/non-overlapping prefix-compatible subset, in seconds —
    exactly the shape ``src/media_subtitles.py`` expects. Greedy by start
    time: keeps a cue only if it starts at/after the previously kept cue's
    end, so the result is always ordered and non-overlapping without
    reinterpreting or truncating any KEPT cue's timing."""
    sample_rate = content.get("sample_rate")
    if not isinstance(sample_rate, int) or sample_rate < 1:
        raise InvalidOperation("transcript.content.sample_rate must be a positive integer")
    cues = sorted(
        (c for c in _cues(content) if c.get("editorial_status") != "needs_review"),
        key=lambda c: int(c["start_sample"]),
    )
    kept: List[Dict[str, Any]] = []
    last_end = -1
    for cue in cues:
        start, end = _range_from_start_end(cue)
        if start < last_end:
            continue  # drops this cue from the export-compatible subset only
        kept.append({
            "start": start / sample_rate,
            "end": end / sample_rate,
            "text": cue["text"],
        })
        last_end = end
    return kept


def export_compatible(content: Dict[str, Any], fmt: str) -> Dict[str, str]:
    """SRT/VTT text for the compatible subset, via ``media_subtitles``
    (never reimplemented here)."""
    from src import media_subtitles

    segments = compatible_srt_segments(content)
    try:
        return media_subtitles.export(segments, fmt)
    except ValueError as exc:
        raise InvalidOperation(str(exc)) from exc


OPS = {
    "transcript.edit_segment": edit_segment,
    "transcript.split_cue": split_cue,
    "transcript.merge_cues": merge_cues,
    "transcript.assign_speaker": assign_speaker,
    "transcript.add_track": add_track,
}
