"""Song (music document) operations — WP24 / MUS02.

Typed, declarative edits over a `song` document's `content` — same
discipline as `canvas_ops.py`/`timeline_ops.py`/`transcript_ops.py`: every
op returns a brand new `content` dict, never mutates its input, and
`registry.py` dispatches `op["type"]` to whichever handler is registered
here (WP12's `OPS` convention, discovered by `pkgutil`).

`documents.py::_validate_song` already requires `language`, `sections`
(non-empty, each `{id, kind, lyrics}`), `takes` and `selected_take` — this
module is the only place those fields are ever WRITTEN through a typed op;
a caller never PATCHes `content` directly (CONTRATO.md's typed-ops
discipline applies to `song` the same as every other kind).

Section `kind` is free text (verse/chorus/bridge/intro/outro/pre_chorus/…)
— MUS02's own acceptance criterion is explicit that structure is a request
the engine honours PROBABILISTICALLY, so this module does not pretend a
closed enum of "guaranteed" section kinds; it only requires the value be a
non-empty string, and `src.creator.music.build_structured_prompt` is where
that string becomes a `[kind]` marker in the prompt sent to the engine,
never a claim the output actually has that structure.

`style_tags`, `bpm_target`, `key_target`, `seed` live on `content` itself
(additive beyond `documents.py`'s required-field set, same as
`ops/transcript_ops.py::add_track`'s `speaker_tracks` — a document created
before this module existed keeps working; these keys only start existing
once one of `set_style`/`set_seed` is first applied, and `takes`/
`selected_take` stay owned by `src.creator.music`'s job-completion write,
never edited by hand through these ops).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from ..errors import InvalidOperation
from .model import find_by_id, index_by_id, require_kind


def _sections(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections = content.get("sections")
    if not isinstance(sections, list):
        raise InvalidOperation("song.content.sections must be an array")
    return sections


def edit_lyrics(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Replaces one section's `lyrics` text in place (by `object_id`)."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    section = find_by_id(_sections(content), op.get("object_id"), what="section")
    lyrics = op.get("lyrics")
    if not isinstance(lyrics, str):
        raise InvalidOperation("song.edit_lyrics requires a string 'lyrics' (may be empty for instrumental)")
    section["lyrics"] = lyrics
    return content


def add_section(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Appends a new section (or inserts at `at_index` when given). `kind`
    is free text — see module docstring — `lyrics` defaults to empty
    (instrumental section)."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    sections = _sections(content)

    section_id = op.get("section_id")
    if not isinstance(section_id, str) or not section_id:
        raise InvalidOperation("song.add_section requires a non-empty string 'section_id'")
    if any(s.get("id") == section_id for s in sections):
        raise InvalidOperation(f"section id already exists: {section_id!r}")
    kind = op.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise InvalidOperation("song.add_section requires a non-empty string 'kind'")
    lyrics = op.get("lyrics", "")
    if not isinstance(lyrics, str):
        raise InvalidOperation("song.add_section 'lyrics' must be a string")

    new_section = {"id": section_id, "kind": kind, "lyrics": lyrics}
    at_index = op.get("at_index")
    if at_index is None:
        sections.append(new_section)
    else:
        if not isinstance(at_index, int) or isinstance(at_index, bool) or not (0 <= at_index <= len(sections)):
            raise InvalidOperation("song.add_section 'at_index' must be an integer in [0, len(sections)]")
        sections.insert(at_index, new_section)
    return content


def remove_section(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Removes one section by `object_id`. Refuses to empty the section
    list — `documents.py::_validate_song` requires at least one, and this
    op would otherwise build an invalid document only to have `apply_command`
    reject the write with a less specific error."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    sections = _sections(content)
    idx = index_by_id(sections, op.get("object_id"), what="section")
    if len(sections) <= 1:
        raise InvalidOperation("song.remove_section refused: a song must keep at least one section")
    del sections[idx]
    return content


def reorder_sections(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Replaces section order with `order` — a permutation of every
    existing section id, given explicitly (never inferred), so a caller
    cannot silently drop or duplicate a section through a reorder."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    sections = _sections(content)
    order = op.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise InvalidOperation("song.reorder_sections requires a list of string section ids in 'order'")
    current_ids = {s["id"] for s in sections}
    if set(order) != current_ids or len(order) != len(sections):
        raise InvalidOperation(
            "song.reorder_sections 'order' must be a permutation of the document's current section ids")
    by_id = {s["id"]: s for s in sections}
    content["sections"] = [by_id[sid] for sid in order]
    return content


def set_style(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets style tags and/or target BPM/key — the additive fields
    `src.creator.music.generate` reads to build the engine's structured
    prompt (module docstring). Any field omitted from `op` is left
    unchanged; passing `null` explicitly clears `bpm_target`/`key_target`."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    if "style_tags" in op:
        tags = op["style_tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            raise InvalidOperation("song.set_style 'style_tags' must be a list of non-empty strings")
        content["style_tags"] = list(tags)
    if "bpm_target" in op:
        bpm = op["bpm_target"]
        if bpm is not None and (not isinstance(bpm, (int, float)) or isinstance(bpm, bool) or bpm <= 0):
            raise InvalidOperation("song.set_style 'bpm_target' must be a positive number or null")
        content["bpm_target"] = bpm
    if "key_target" in op:
        key = op["key_target"]
        if key is not None and not (isinstance(key, str) and key.strip()):
            raise InvalidOperation("song.set_style 'key_target' must be a non-empty string or null")
        content["key_target"] = key
    return content


def set_seed(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets (or clears, with `seed: null`) the reproducibility seed the
    next `generate()` call reads by default — MOD-06's "intento
    reproducible bajo condiciones, no garantía bit a bit universal" applies
    exactly as it does to `params.py`'s own `seed` fields elsewhere."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    seed = op.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or seed < 0):
        raise InvalidOperation("song.set_seed 'seed' must be a non-negative integer or null")
    content["seed"] = seed
    return content


def _takes(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    takes = content.get("takes")
    if not isinstance(takes, list):
        raise InvalidOperation("song.content.takes must be an array")
    return takes


_TAKE_REQUIRED_KEYS = ("id", "occurrence_id", "engine", "seed", "bpm", "key",
                       "duration_s", "created_at", "lyrics_timing")


def record_take(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Appends one generated take. This is the ONLY writer of `content.takes`
    — `src.creator.music.generate`/`variants` call it (through the normal
    `apply_command` path, so it is dedupe-safe under the same `command_id`
    every other op uses) once a job's `collect()` output has already been
    turned into a real artifact occurrence; nothing here talks to an
    adapter or the artifact store itself."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    take = op.get("take")
    if not isinstance(take, dict):
        raise InvalidOperation("song.record_take requires an object 'take'")
    for key in _TAKE_REQUIRED_KEYS:
        if key not in take:
            raise InvalidOperation(f"song.record_take 'take' is missing {key!r}")
    take_id = take["id"]
    if not isinstance(take_id, str) or not take_id:
        raise InvalidOperation("song.record_take 'take.id' must be a non-empty string")
    takes = _takes(content)
    if any(t.get("id") == take_id for t in takes):
        raise InvalidOperation(f"take id already exists: {take_id!r}")
    take = dict(take)
    take.setdefault("favorite", False)
    takes.append(take)
    if op.get("select", False):
        content["selected_take"] = take_id
    return content


def select_take(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets (or clears, with `take_id: null`) `content.selected_take`.
    Never removes history — WP24's own closing criterion: "elegir una
    variante no elimina el historial ni dispara nuevas generaciones
    automáticamente"; this op does nothing but move the pointer."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    take_id = op.get("take_id")
    if take_id is not None:
        if not isinstance(take_id, str) or not take_id:
            raise InvalidOperation("song.select_take 'take_id' must be a non-empty string or null")
        if not any(t.get("id") == take_id for t in _takes(content)):
            raise InvalidOperation(f"unknown take id: {take_id!r}")
    content["selected_take"] = take_id
    return content


def mark_take_favorite(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets one take's `favorite` flag — a human label, never touched by
    generation itself and never a trigger for anything else."""
    require_kind(doc, "song")
    content = copy.deepcopy(doc["content"])
    take = find_by_id(_takes(content), op.get("object_id"), what="take")
    favorite = op.get("favorite")
    if not isinstance(favorite, bool):
        raise InvalidOperation("song.mark_take_favorite requires a boolean 'favorite'")
    take["favorite"] = favorite
    return content


OPS = {
    "song.edit_lyrics": edit_lyrics,
    "song.add_section": add_section,
    "song.remove_section": remove_section,
    "song.reorder_sections": reorder_sections,
    "song.set_style": set_style,
    "song.set_seed": set_seed,
    "song.record_take": record_take,
    "song.select_take": select_take,
    "song.mark_take_favorite": mark_take_favorite,
}
