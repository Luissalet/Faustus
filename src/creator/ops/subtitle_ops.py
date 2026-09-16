"""Subtitle document operations — WP16, over WP02/WP12's typed-op registry.

Every op here works on a ``subtitles`` document's ``content.cues``
(``src/creator/documents.py::_validate_subtitles``). Unlike
``transcript_ops.py``, cues are deliberately NOT required to be
non-overlapping within a speaker: SUB05's closure criterion #2 is
"solapes legítimos tienen representación propia" — two subtitle cues (a
caption and a translation line shown together, or a brief double-up while
splitting) can legitimately overlap, so no op here rejects that.

An op that edits a cue's text/timing sets ``edited: true`` on it — the
one flag :func:`src.creator.subtitles.regenerate_from_transcript` reads to
decide which cues a later "regenerate from transcript" run must leave
alone.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from ..errors import InvalidOperation
from .model import find_by_id, index_by_id, require_kind, ticks_str


def _cues(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    cues = content.get("cues")
    if not isinstance(cues, list):
        raise InvalidOperation("subtitles.content.cues must be an array")
    return cues


def _require_lines(value: Any, *, max_lines: int) -> List[str]:
    if not isinstance(value, list) or not value or not all(isinstance(l, str) and l for l in value):
        raise InvalidOperation("'lines' must be a non-empty array of non-empty strings")
    if len(value) > max_lines:
        raise InvalidOperation(f"'lines' has {len(value)} lines but the profile allows at most {max_lines}")
    return list(value)


def edit_cue(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Edits a cue's text/timing/speaker/style directly (by ``object_id``).
    Any field present in ``op`` overwrites the cue's own; timing is never
    inferred from a neighbour. Marks ``edited: true`` (SUB05)."""
    require_kind(doc, "subtitles")
    content = copy.deepcopy(doc["content"])
    cue = find_by_id(_cues(content), op.get("object_id"), what="cue")

    if "lines" in op:
        cue["lines"] = _require_lines(op["lines"], max_lines=content["profile"]["max_lines"])
    if "start_ticks" in op or "duration_ticks" in op:
        start = op.get("start_ticks", cue["start_ticks"])
        dur = op.get("duration_ticks", cue["duration_ticks"])
        if isinstance(start, int):
            start = ticks_str(start)
        if isinstance(dur, int):
            dur = ticks_str(dur)
        if not isinstance(start, str) or not start.isdigit():
            raise InvalidOperation("'start_ticks' must be a non-negative integer string")
        if not isinstance(dur, str) or not dur.isdigit() or int(dur) < 1:
            raise InvalidOperation("'duration_ticks' must be a positive integer string")
        cue["start_ticks"], cue["duration_ticks"] = start, dur
    if "speaker_id" in op:
        speaker_id = op["speaker_id"]
        if speaker_id is not None and not (isinstance(speaker_id, str) and speaker_id):
            raise InvalidOperation("'speaker_id' must be a non-empty string or null")
        cue["speaker_id"] = speaker_id
    if "style_id" in op:
        style_id = op["style_id"]
        if style_id is not None and not (isinstance(style_id, str) and style_id):
            raise InvalidOperation("'style_id' must be a non-empty string or null")
        cue["style_id"] = style_id
    if "editorial_status" in op:
        status = op["editorial_status"]
        if status not in ("proposed", "accepted", "needs_review"):
            raise InvalidOperation("'editorial_status' is invalid")
        cue["editorial_status"] = status

    cue["edited"] = True
    return content


def split_cue(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Splits a cue in two at an absolute ``at_ticks`` strictly inside its
    span, each half's ``lines`` given explicitly (no guessed word boundary,
    same discipline as ``transcript_ops.split_cue``). Both halves are
    marked ``edited: true``."""
    require_kind(doc, "subtitles")
    content = copy.deepcopy(doc["content"])
    cues = _cues(content)
    idx = index_by_id(cues, op.get("object_id"), what="cue")
    cue = cues[idx]
    start = int(cue["start_ticks"])
    end = start + int(cue["duration_ticks"])

    at_ticks = op.get("at_ticks")
    if isinstance(at_ticks, bool) or not isinstance(at_ticks, (int, str)):
        raise InvalidOperation("split_cue requires an integer/string 'at_ticks'")
    at_ticks = int(at_ticks)
    if not (start < at_ticks < end):
        raise InvalidOperation("'at_ticks' must be strictly inside the cue")

    max_lines = content["profile"]["max_lines"]
    first_lines = _require_lines(op.get("first_lines"), max_lines=max_lines)
    second_lines = _require_lines(op.get("second_lines"), max_lines=max_lines)
    new_cue_id = op.get("new_cue_id")
    if not isinstance(new_cue_id, str) or not new_cue_id:
        raise InvalidOperation("split_cue requires a non-empty string 'new_cue_id'")
    if any(c["id"] == new_cue_id for c in cues):
        raise InvalidOperation(f"cue id already exists: {new_cue_id!r}")

    first = copy.deepcopy(cue)
    first["lines"] = first_lines
    first["duration_ticks"] = ticks_str(at_ticks - start)
    first["edited"] = True

    second = copy.deepcopy(cue)
    second["id"] = new_cue_id
    second["lines"] = second_lines
    second["start_ticks"] = ticks_str(at_ticks)
    second["duration_ticks"] = ticks_str(end - at_ticks)
    second["edited"] = True

    cues[idx] = first
    cues.insert(idx + 1, second)
    return content


def merge_cues(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Merges two cues into one spanning both, ``lines`` given explicitly
    (never auto-concatenated). Marks the result ``edited: true``."""
    require_kind(doc, "subtitles")
    content = copy.deepcopy(doc["content"])
    cues = _cues(content)
    first_idx = index_by_id(cues, op.get("first_object_id"), what="cue")
    second_idx = index_by_id(cues, op.get("second_object_id"), what="cue")
    if first_idx == second_idx:
        raise InvalidOperation("merge_cues requires two distinct cues")
    first, second = cues[first_idx], cues[second_idx]

    lines = _require_lines(op.get("lines"), max_lines=content["profile"]["max_lines"])
    s1, e1 = int(first["start_ticks"]), int(first["start_ticks"]) + int(first["duration_ticks"])
    s2, e2 = int(second["start_ticks"]), int(second["start_ticks"]) + int(second["duration_ticks"])
    merged_start, merged_end = min(s1, s2), max(e1, e2)

    merged = copy.deepcopy(first)
    merged["lines"] = lines
    merged["start_ticks"] = ticks_str(merged_start)
    merged["duration_ticks"] = ticks_str(max(merged_end - merged_start, 1))
    merged["source_cue_ids"] = sorted(set(first.get("source_cue_ids", [])) | set(second.get("source_cue_ids", [])))
    merged["edited"] = True

    keep_idx, drop_idx = min(first_idx, second_idx), max(first_idx, second_idx)
    cues[keep_idx] = merged
    del cues[drop_idx]
    return content


def shift_cues(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Bulk-shifts a set of cues (``object_ids``, or every cue when omitted)
    by a signed ``offset_ticks`` — the "collective desync fix" SUB05 asks
    for. Rejects a shift that would push any targeted cue's start below
    zero rather than clamping it silently. Marks every shifted cue
    ``edited: true``."""
    require_kind(doc, "subtitles")
    content = copy.deepcopy(doc["content"])
    cues = _cues(content)
    offset = op.get("offset_ticks")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise InvalidOperation("shift_cues requires an integer 'offset_ticks'")
    object_ids = op.get("object_ids")
    if object_ids is not None:
        if not isinstance(object_ids, list) or not all(isinstance(i, str) for i in object_ids):
            raise InvalidOperation("'object_ids' must be an array of strings when given")
        target_ids = set(object_ids)
        for oid in target_ids:
            find_by_id(cues, oid, what="cue")
    else:
        target_ids = None

    for cue in cues:
        if target_ids is not None and cue["id"] not in target_ids:
            continue
        new_start = int(cue["start_ticks"]) + offset
        if new_start < 0:
            raise InvalidOperation(f"shift_cues would move cue {cue['id']!r} to a negative start_ticks")
        cue["start_ticks"] = ticks_str(new_start)
        cue["edited"] = True
    return content


def set_style(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets the document's default ``style`` (``style`` in the op) and/or
    adds or replaces a named style under ``content.styles`` (``style_id`` +
    ``style``) — validated in full by
    ``src.creator.subtitles._normalize_style`` before it is accepted, so a
    malformed color/font never reaches a stored document."""
    require_kind(doc, "subtitles")
    from ..subtitles import _normalize_style  # validated here, not reimplemented

    content = copy.deepcopy(doc["content"])
    style_id = op.get("style_id")
    style = op.get("style")
    if not isinstance(style, dict):
        raise InvalidOperation("set_style requires an object 'style'")
    normalized = _normalize_style(style)
    if style_id is None:
        content["style"] = normalized
    else:
        if not isinstance(style_id, str) or not style_id or style_id == "Default":
            raise InvalidOperation("'style_id' must be a non-empty string other than 'Default'")
        content.setdefault("styles", {})[style_id] = normalized
    return content


def apply_retiming(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Maps every cue through a retiming map (the same shape
    ``timeline.retime``/``timeline.snap`` store) — see
    ``src.creator.subtitles.apply_retiming``'s docstring for the
    unmapped-zone contract. Does NOT set ``edited`` (this is an automatic
    consequence of a timeline cut, not a human text/timing edit)."""
    require_kind(doc, "subtitles")
    from ..subtitles import apply_retiming as _apply_retiming

    retiming = op.get("retiming")
    if not isinstance(retiming, list):
        raise InvalidOperation("apply_retiming requires an array 'retiming'")
    direction = op.get("direction", "source_to_dest")
    return _apply_retiming(doc["content"], retiming, direction=direction)


def regenerate_from_transcript(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Re-segments from a (caller-supplied) transcript ``content`` dict,
    keeping every human-edited cue over the ones it overlaps. The caller
    (``routes/creator_subtitle_routes.py``) is the one that fetches the
    live ``transcript`` document and passes its ``content`` here — this op
    itself never resolves a document id, keeping it a pure ``(doc, op) ->
    content`` function like every other op."""
    require_kind(doc, "subtitles")
    from ..subtitles import regenerate_from_transcript as _regenerate

    transcript_content = op.get("transcript_content")
    if not isinstance(transcript_content, dict):
        raise InvalidOperation("regenerate_from_transcript requires an object 'transcript_content'")
    return _regenerate(doc["content"], transcript_content, profile_overrides=op.get("profile_overrides"))


OPS = {
    "subtitles.edit_cue": edit_cue,
    "subtitles.split_cue": split_cue,
    "subtitles.merge_cues": merge_cues,
    "subtitles.shift_cues": shift_cues,
    "subtitles.set_style": set_style,
    "subtitles.apply_retiming": apply_retiming,
    "subtitles.regenerate_from_transcript": regenerate_from_transcript,
}
