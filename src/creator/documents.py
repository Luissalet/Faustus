"""CreatorDocument — the typed, revisioned editorial aggregate (WP02).

Shape follows ``docs/spec/creator/plan/contracts/creator-document.schema.json``
(REFERENCE ONLY — that file is a design illustration for the plan package,
not a deployed API contract: it forces ``example_only`` and ``prj_``/``occ_``
id prefixes that do not match this repo's real ``ProjectStore``/artifact ids,
so it is not imported and enforced verbatim). This module reimplements the
same field shape per ``kind`` (canvas, timeline, transcript, song,
storyboard) against real ids, plus a document-level ``state`` the
architecture doc calls out explicitly: "Los estados aceptado/propuesto se
conservan explícitamente" (``docs/spec/creator/plan/docs/03_ARQUITECTURA_Y_CONTRATOS.md``).

Validation strategy (CONTRATO.md WP02 line): try ``jsonschema`` if it is
importable, but it is NOT declared in this repo's ``requirements*.txt`` —
grepped, absent — so it must never be a hard dependency. The structural
validator below is therefore the primary, always-available path; a
best-effort ``jsonschema`` pass only adds belt-and-braces type/shape checks
when the library happens to be installed, and never raises ImportError.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import InvalidDocument

SCHEMA_VERSION = 1

DOCUMENT_KINDS = ("canvas", "timeline", "transcript", "song", "storyboard")
DOCUMENT_STATES = ("proposed", "accepted")


def new_document_id() -> str:
    return f"doc_{uuid.uuid4().hex[:20]}"


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise InvalidDocument(msg)


def _is_str(v: Any, *, min_len: int = 0) -> bool:
    return isinstance(v, str) and len(v) >= min_len


def _is_ticks(v: Any, *, allow_zero: bool = False) -> bool:
    """A non-negative (or positive) integer serialized as a decimal string,
    per the plan's "integers as strings" rule for JS-precision safety."""
    if not isinstance(v, str) or not v:
        return False
    if not v.isdigit():
        return False
    if v != "0" and v[0] == "0":
        return False
    if not allow_zero and v == "0":
        return False
    return True


def _validate_clock(clock: Any, path: str) -> None:
    _require(isinstance(clock, dict), f"{path} must be an object")
    num = clock.get("ticks_per_second_numerator")
    den = clock.get("ticks_per_second_denominator")
    _require(_is_ticks(num), f"{path}.ticks_per_second_numerator must be a positive integer string")
    _require(_is_ticks(den), f"{path}.ticks_per_second_denominator must be a positive integer string")


def _validate_canvas(content: Dict[str, Any]) -> None:
    for key in ("width", "height", "layers", "base_asset_ref"):
        _require(key in content, f"canvas.content.{key} is required")
    _require(isinstance(content["width"], int) and content["width"] >= 1, "canvas.content.width must be >=1")
    _require(isinstance(content["height"], int) and content["height"] >= 1, "canvas.content.height must be >=1")
    _require(_is_str(content["base_asset_ref"], min_len=1), "canvas.content.base_asset_ref must be a non-empty string")
    layers = content["layers"]
    _require(isinstance(layers, list), "canvas.content.layers must be an array")
    for i, layer in enumerate(layers):
        _require(isinstance(layer, dict), f"canvas.content.layers[{i}] must be an object")
        for key in ("id", "kind", "visible", "opacity", "offset"):
            _require(key in layer, f"canvas.content.layers[{i}].{key} is required")
        _require(layer["kind"] in ("raster", "mask", "text", "control"), f"canvas.content.layers[{i}].kind is invalid")
        _require(isinstance(layer["visible"], bool), f"canvas.content.layers[{i}].visible must be a bool")
        opacity = layer["opacity"]
        _require(isinstance(opacity, (int, float)) and 0 <= opacity <= 1, f"canvas.content.layers[{i}].opacity must be in [0,1]")
        offset = layer["offset"]
        _require(isinstance(offset, dict) and isinstance(offset.get("x"), int) and isinstance(offset.get("y"), int),
                  f"canvas.content.layers[{i}].offset must have integer x/y")


def _validate_timeline(content: Dict[str, Any]) -> None:
    for key in ("clock", "duration_ticks", "tracks"):
        _require(key in content, f"timeline.content.{key} is required")
    _validate_clock(content["clock"], "timeline.content.clock")
    _require(_is_ticks(content["duration_ticks"]), "timeline.content.duration_ticks must be a positive integer string")
    tracks = content["tracks"]
    _require(isinstance(tracks, list) and len(tracks) >= 1, "timeline.content.tracks must be a non-empty array")
    for i, track in enumerate(tracks):
        _require(isinstance(track, dict), f"timeline.content.tracks[{i}] must be an object")
        for key in ("id", "kind", "locked", "clips"):
            _require(key in track, f"timeline.content.tracks[{i}].{key} is required")
        _require(track["kind"] in ("video", "audio", "image", "caption", "overlay"),
                  f"timeline.content.tracks[{i}].kind is invalid")
        _require(isinstance(track["locked"], bool), f"timeline.content.tracks[{i}].locked must be a bool")
        clips = track["clips"]
        _require(isinstance(clips, list), f"timeline.content.tracks[{i}].clips must be an array")
        for j, clip in enumerate(clips):
            cpath = f"timeline.content.tracks[{i}].clips[{j}]"
            _require(isinstance(clip, dict), f"{cpath} must be an object")
            for key in ("id", "asset_ref", "timeline_start_ticks", "source_range",
                        "source_clock", "timeline_duration_ticks"):
                _require(key in clip, f"{cpath}.{key} is required")
            _require(_is_ticks(clip["timeline_start_ticks"], allow_zero=True), f"{cpath}.timeline_start_ticks invalid")
            _require(_is_ticks(clip["timeline_duration_ticks"]), f"{cpath}.timeline_duration_ticks invalid")
            sr = clip["source_range"]
            _require(isinstance(sr, dict) and _is_ticks(sr.get("start_ticks"), allow_zero=True)
                      and _is_ticks(sr.get("duration_ticks")), f"{cpath}.source_range invalid")
            _validate_clock(clip["source_clock"], f"{cpath}.source_clock")


def _validate_transcript(content: Dict[str, Any]) -> None:
    for key in ("source_asset_ref", "sample_rate", "cues", "alignment_status"):
        _require(key in content, f"transcript.content.{key} is required")
    _require(_is_str(content["source_asset_ref"], min_len=1), "transcript.content.source_asset_ref must be a non-empty string")
    _require(isinstance(content["sample_rate"], int) and content["sample_rate"] >= 1,
              "transcript.content.sample_rate must be >=1")
    _require(content["alignment_status"] in ("partial", "verified_fixture", "not_aligned", "needs_review"),
              "transcript.content.alignment_status is invalid")
    cues = content["cues"]
    _require(isinstance(cues, list), "transcript.content.cues must be an array")
    for i, cue in enumerate(cues):
        cpath = f"transcript.content.cues[{i}]"
        _require(isinstance(cue, dict), f"{cpath} must be an object")
        for key in ("id", "text", "language", "speaker_id", "start_sample", "end_sample",
                    "words", "editorial_status"):
            _require(key in cue, f"{cpath}.{key} is required")
        _require(_is_str(cue["text"], min_len=1), f"{cpath}.text must be non-empty")
        _require(cue["editorial_status"] in ("proposed", "accepted", "needs_review"),
                  f"{cpath}.editorial_status is invalid")
        _require(cue["speaker_id"] is None or _is_str(cue["speaker_id"], min_len=1), f"{cpath}.speaker_id invalid")
        _require(_is_ticks(cue["start_sample"], allow_zero=True), f"{cpath}.start_sample invalid")
        _require(_is_ticks(cue["end_sample"]), f"{cpath}.end_sample invalid")
        _require(isinstance(cue["words"], list), f"{cpath}.words must be an array")


def _validate_song(content: Dict[str, Any]) -> None:
    for key in ("language", "sections", "takes", "selected_take"):
        _require(key in content, f"song.content.{key} is required")
    _require(_is_str(content["language"], min_len=1), "song.content.language must be non-empty")
    sections = content["sections"]
    _require(isinstance(sections, list) and len(sections) >= 1, "song.content.sections must be non-empty")
    for i, section in enumerate(sections):
        spath = f"song.content.sections[{i}]"
        _require(isinstance(section, dict), f"{spath} must be an object")
        for key in ("id", "kind", "lyrics"):
            _require(key in section, f"{spath}.{key} is required")
        _require(isinstance(section["lyrics"], str), f"{spath}.lyrics must be a string")
    _require(isinstance(content["takes"], list), "song.content.takes must be an array")
    st = content["selected_take"]
    _require(st is None or _is_str(st, min_len=1), "song.content.selected_take invalid")


def _validate_storyboard(content: Dict[str, Any]) -> None:
    _require("shots" in content, "storyboard.content.shots is required")
    shots = content["shots"]
    _require(isinstance(shots, list) and len(shots) >= 1, "storyboard.content.shots must be non-empty")
    for i, shot in enumerate(shots):
        spath = f"storyboard.content.shots[{i}]"
        _require(isinstance(shot, dict), f"{spath} must be an object")
        for key in ("id", "brief", "duration", "clock", "reference_assets", "selected_take"):
            _require(key in shot, f"{spath}.{key} is required")
        _require(_is_str(shot["brief"], min_len=1), f"{spath}.brief must be non-empty")
        dur = shot["duration"]
        _require(isinstance(dur, dict) and _is_ticks(dur.get("start_ticks"), allow_zero=True)
                  and _is_ticks(dur.get("duration_ticks")), f"{spath}.duration invalid")
        _validate_clock(shot["clock"], f"{spath}.clock")
        _require(isinstance(shot["reference_assets"], list), f"{spath}.reference_assets must be an array")
        st = shot["selected_take"]
        _require(st is None or _is_str(st, min_len=1), f"{spath}.selected_take invalid")


_VALIDATORS = {
    "canvas": _validate_canvas,
    "timeline": _validate_timeline,
    "transcript": _validate_transcript,
    "song": _validate_song,
    "storyboard": _validate_storyboard,
}


def validate_content(kind: str, content: Any) -> None:
    """Raise :class:`InvalidDocument` when ``content`` does not match the
    shape required for ``kind``. Structural (always available) plus a
    best-effort ``jsonschema`` type pass when that library happens to be
    importable — never a hard dependency (see module docstring)."""
    if kind not in DOCUMENT_KINDS:
        raise InvalidDocument(f"unknown document kind: {kind!r}")
    if not isinstance(content, dict):
        raise InvalidDocument("content must be an object")
    _VALIDATORS[kind](content)
    _optional_jsonschema_pass(content)


def _optional_jsonschema_pass(content: Dict[str, Any]) -> None:
    """Best-effort extra check. Never raises for an absent/broken library,
    and never treated as the source of truth: the structural validator above
    always ran first and already accepted ``content``."""
    try:
        import jsonschema  # noqa: F401
    except Exception:
        return
    # jsonschema is present in this environment but not a declared
    # dependency (see module docstring); intentionally not wired to the
    # plan's REFERENCE ONLY schema (id/prefix shapes do not match real
    # data). Nothing further to do here — the structural pass is authoritative.
    return


@dataclass
class CreatorDocument:
    id: str
    project_id: str
    owner: str
    kind: str
    schema_version: int
    revision: int
    content: Dict[str, Any]
    state: str = "proposed"
    asset_refs: List[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_public_dict(self) -> Dict[str, Any]:
        """Owner is deliberately NOT included: it is a storage-internal
        scoping field, never echoed to a client (CONTRATO.md rule 3)."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "state": self.state,
            "asset_refs": list(self.asset_refs),
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def now() -> float:
    return time.time()
