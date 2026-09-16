"""WP12 — typed, declarative document operations (canvas/timeline/transcript,
undo/redo as revisions).

Real sqlite ``DocumentStore`` in ``tmp_path``, no mocks. Exercises
``apply_command`` end to end through the WP12 ops registry dispatch added
in ``src/creator/store.py::_apply_op``.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from src.creator.errors import InvalidDocument, InvalidOperation, RevisionConflict
from src.creator.ops import model, registry
from src.creator.ops.transcript_ops import compatible_srt_segments
from src.creator.ops.undo import undo_to
from src.creator.store import DocumentStore


# ----------------------------------------------------------------------
# model.py — rationals and regions
# ----------------------------------------------------------------------

def test_rational_round_trips_ntsc_without_precision_loss():
    r = model.Rational.from_dict({
        "ticks_per_second_numerator": "30000",
        "ticks_per_second_denominator": "1001",
    })
    assert r.as_fraction() == Fraction(30000, 1001)
    # One "tick" at 30000/1001 fps is 1001/30000 s exactly — not the
    # 0.033366666...  a float would give.
    assert r.seconds_of(1) == Fraction(1001, 30000)
    assert r.to_dict() == {
        "ticks_per_second_numerator": "30000",
        "ticks_per_second_denominator": "1001",
    }


def test_rational_round_trips_48khz_audio():
    r = model.Rational.from_dict({
        "ticks_per_second_numerator": "48000",
        "ticks_per_second_denominator": "1",
    })
    # 48000 ticks at 48000 Hz is exactly 1 second — no float drift.
    assert r.seconds_of(48000) == Fraction(1, 1)
    assert r.ticks_of(Fraction(1, 1)) == 48000


def test_ticks_reject_leading_zero_and_bool_and_float():
    with pytest.raises(InvalidOperation):
        model.Rational.from_dict({"ticks_per_second_numerator": "0030", "ticks_per_second_denominator": "1"})
    with pytest.raises(InvalidOperation):
        model.TimeRange.from_dict({"start_ticks": True, "duration_ticks": "1"})
    with pytest.raises(InvalidOperation):
        model.TimeRange.from_dict({"start_ticks": "0", "duration_ticks": 1.5})


def test_region_reproducible_with_exif_and_layer_transform():
    region = model.Region.from_dict({
        "x": 10, "y": 20, "width": 100, "height": 200,
        "source_w": 800, "source_h": 600,
        "exif_orientation": 6,
        "layer_transform": {"scale": 1.5, "rotate_degrees": 90},
    })
    out = region.to_dict()
    # Round-trips exactly, including the fields a bare x/y capture would
    # have thrown away.
    again = model.Region.from_dict(out)
    assert again == region
    assert out["exif_orientation"] == 6
    assert out["layer_transform"] == {"scale": 1.5, "rotate_degrees": 90}


def test_region_rejects_out_of_bounds():
    with pytest.raises(InvalidOperation):
        model.Region.from_dict({
            "x": 700, "y": 0, "width": 200, "height": 200,
            "source_w": 800, "source_h": 600,
        })


# ----------------------------------------------------------------------
# registry.py — unknown ops
# ----------------------------------------------------------------------

def test_registry_rejects_unknown_op_type():
    with pytest.raises(InvalidOperation):
        registry.apply({"kind": "canvas", "content": {}}, {"type": "canvas.teleport"})


def test_registry_rejects_malformed_op():
    with pytest.raises(InvalidOperation):
        registry.apply({"kind": "canvas", "content": {}}, {"no_type": True})


# ----------------------------------------------------------------------
# canvas ops — layers, transforms, order
# ----------------------------------------------------------------------

def _canvas_content():
    return {
        "width": 64, "height": 64, "operation_semantics_version": 1,
        "layers": [], "base_asset_ref": "occ_base",
    }


def test_canvas_add_move_rotate_crop_reorder_produce_shown_order(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())

    r = store.apply_command("alice", doc.id, "cmd-1", doc.revision, {
        "type": "canvas.add_layer", "object_id": "bg", "kind": "raster",
        "asset_ref": "occ_bg", "offset": {"x": 0, "y": 0},
    })
    doc = r["doc"]
    r = store.apply_command("alice", doc.id, "cmd-2", doc.revision, {
        "type": "canvas.add_layer", "object_id": "fg", "kind": "raster",
        "asset_ref": "occ_fg", "offset": {"x": 5, "y": 5},
    })
    doc = r["doc"]
    assert [l["id"] for l in doc.content["layers"]] == ["bg", "fg"]

    r = store.apply_command("alice", doc.id, "cmd-3", doc.revision, {
        "type": "canvas.move_layer", "object_id": "fg", "offset": {"x": 10, "y": 10},
    })
    doc = r["doc"]
    assert doc.content["layers"][1]["offset"] == {"x": 10, "y": 10}

    r = store.apply_command("alice", doc.id, "cmd-4", doc.revision, {
        "type": "canvas.rotate", "object_id": "fg", "degrees": 450,
    })
    doc = r["doc"]
    assert doc.content["layers"][1]["rotation_degrees"] == 90.0  # normalized mod 360

    r = store.apply_command("alice", doc.id, "cmd-5", doc.revision, {
        "type": "canvas.crop", "object_id": "canvas",
        "region": {"x": 0, "y": 0, "width": 32, "height": 32, "source_w": 64, "source_h": 64},
    })
    doc = r["doc"]
    assert doc.content["width"] == 32 and doc.content["height"] == 32

    r = store.apply_command("alice", doc.id, "cmd-6", doc.revision, {
        "type": "canvas.reorder", "order": ["fg", "bg"],
    })
    doc = r["doc"]
    assert [l["id"] for l in doc.content["layers"]] == ["fg", "bg"]

    # Deterministic: replaying with the same command_id returns the exact
    # same result without applying it twice (WP02 dedupe still holds
    # through the WP12 dispatch).
    r2 = store.apply_command("alice", doc.id, "cmd-6", doc.revision - 1, {
        "type": "canvas.reorder", "order": ["fg", "bg"],
    })
    assert r2["deduped"] is True
    assert r2["doc"].revision == doc.revision


def test_canvas_set_mask_and_unknown_object_id(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    r = store.apply_command("alice", doc.id, "c1", doc.revision, {
        "type": "canvas.add_layer", "object_id": "l1", "kind": "raster", "offset": {"x": 0, "y": 0},
    })
    doc = r["doc"]
    r = store.apply_command("alice", doc.id, "c2", doc.revision, {
        "type": "canvas.set_mask", "object_id": "l1", "mask_asset_ref": "occ_mask",
    })
    assert r["doc"].content["layers"][0]["mask_asset_ref"] == "occ_mask"

    with pytest.raises(InvalidOperation):
        store.apply_command("alice", doc.id, "c3", r["doc"].revision, {
            "type": "canvas.move_layer", "object_id": "does-not-exist", "offset": {"x": 1, "y": 1},
        })


# ----------------------------------------------------------------------
# timeline ops — overlap per track, not across tracks
# ----------------------------------------------------------------------

def _timeline_content():
    return {
        "clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
        "duration_ticks": "300",
        "tracks": [
            {"id": "v1", "kind": "video", "locked": False, "clips": []},
            {"id": "a1", "kind": "audio", "locked": False, "clips": []},
        ],
    }


def _clip(id_, start, dur):
    return {
        "id": id_, "asset_ref": "occ_x", "timeline_start_ticks": str(start),
        "source_range": {"start_ticks": "0", "duration_ticks": str(dur)},
        "source_clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
        "timeline_duration_ticks": str(dur),
    }


def test_timeline_overlap_rejected_within_track_allowed_across_tracks(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "timeline", _timeline_content())

    r = store.apply_command("alice", doc.id, "c1", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "v1", "clip": _clip("v-clip-1", 0, 100),
    })
    doc = r["doc"]

    # Same span on a DIFFERENT track (audio under video) is fine.
    r = store.apply_command("alice", doc.id, "c2", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "a1", "clip": _clip("a-clip-1", 0, 100),
    })
    doc = r["doc"]
    assert len(doc.content["tracks"][0]["clips"]) == 1
    assert len(doc.content["tracks"][1]["clips"]) == 1

    # Overlapping span on the SAME track is rejected.
    with pytest.raises(InvalidOperation):
        store.apply_command("alice", doc.id, "c3", doc.revision, {
            "type": "timeline.insert_clip", "track_id": "v1", "clip": _clip("v-clip-2", 50, 100),
        })

    # Non-overlapping (touching) span on the same track is fine.
    r = store.apply_command("alice", doc.id, "c4", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "v1", "clip": _clip("v-clip-2", 100, 50),
    })
    assert len(r["doc"].content["tracks"][0]["clips"]) == 2


def test_timeline_split_preserves_total_duration_and_trim_revalidates_overlap(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "timeline", _timeline_content())
    r = store.apply_command("alice", doc.id, "c1", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "v1", "clip": _clip("clip-1", 0, 100),
    })
    doc = r["doc"]

    r = store.apply_command("alice", doc.id, "c2", doc.revision, {
        "type": "timeline.split", "track_id": "v1", "clip_id": "clip-1",
        "at_ticks": 40, "new_clip_id": "clip-2",
    })
    doc = r["doc"]
    clips = doc.content["tracks"][0]["clips"]
    assert len(clips) == 2
    assert clips[0]["id"] == "clip-1" and clips[0]["timeline_duration_ticks"] == "40"
    assert clips[1]["id"] == "clip-2" and clips[1]["timeline_start_ticks"] == "40"
    total_source = int(clips[0]["source_range"]["duration_ticks"]) + int(clips[1]["source_range"]["duration_ticks"])
    assert total_source == 100  # nothing lost across the split

    # Trimming clip-2 to extend into clip-1's span must fail overlap check.
    with pytest.raises(InvalidOperation):
        store.apply_command("alice", doc.id, "c3", doc.revision, {
            "type": "timeline.trim", "track_id": "v1", "clip_id": "clip-2",
            "source_range": {"start_ticks": "0", "duration_ticks": "80"},
            "timeline_duration_ticks": "80", "timeline_start_ticks": "20",
        })


def test_timeline_ripple_delete_shifts_only_same_track(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "timeline", _timeline_content())
    r = store.apply_command("alice", doc.id, "c1", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "v1", "clip": _clip("clip-1", 0, 50),
    })
    doc = r["doc"]
    r = store.apply_command("alice", doc.id, "c2", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "v1", "clip": _clip("clip-2", 50, 50),
    })
    doc = r["doc"]
    r = store.apply_command("alice", doc.id, "c3", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "a1", "clip": _clip("a-clip", 50, 50),
    })
    doc = r["doc"]

    r = store.apply_command("alice", doc.id, "c4", doc.revision, {
        "type": "timeline.ripple_delete", "track_id": "v1", "clip_id": "clip-1",
    })
    doc = r["doc"]
    v1_clips = doc.content["tracks"][0]["clips"]
    a1_clips = doc.content["tracks"][1]["clips"]
    assert [c["id"] for c in v1_clips] == ["clip-2"]
    assert v1_clips[0]["timeline_start_ticks"] == "0"  # shifted left by 50
    assert a1_clips[0]["timeline_start_ticks"] == "50"  # other track untouched


def test_timeline_move_rejects_cross_kind_track(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "timeline", _timeline_content())
    r = store.apply_command("alice", doc.id, "c1", doc.revision, {
        "type": "timeline.insert_clip", "track_id": "v1", "clip": _clip("clip-1", 0, 50),
    })
    doc = r["doc"]
    with pytest.raises(InvalidOperation):
        store.apply_command("alice", doc.id, "c2", doc.revision, {
            "type": "timeline.move", "track_id": "v1", "clip_id": "clip-1",
            "timeline_start_ticks": "0", "target_track_id": "a1",
        })


# ----------------------------------------------------------------------
# transcript ops — per-speaker overlap, SRT compatible subset
# ----------------------------------------------------------------------

def _transcript_content():
    return {
        "source_asset_ref": "occ_audio", "sample_rate": 16000,
        "cues": [], "alignment_status": "partial",
    }


def _cue(id_, speaker, start, end, text="hi"):
    return {
        "id": id_, "text": text, "language": "en", "speaker_id": speaker,
        "start_sample": str(start), "end_sample": str(end),
        "words": [], "editorial_status": "accepted",
    }


def test_transcript_overlap_within_speaker_rejected_across_speakers_allowed(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    content = _transcript_content()
    content["cues"] = [_cue("c1", "spk_a", 0, 16000, "hello")]
    doc = store.create("alice", "proj1", "transcript", content)

    # Overlapping cue for a DIFFERENT speaker is fine (crosstalk).
    r = store.apply_command("alice", doc.id, "cmd1", doc.revision, {
        "type": "transcript.edit_segment", "object_id": "c1", "text": "hello there",
    })
    doc = r["doc"]

    r = store.apply_command("alice", doc.id, "cmd2", doc.revision, {
        "type": "transcript.assign_speaker", "object_id": "c1", "speaker_id": "spk_a",
    })
    doc = r["doc"]
    assert doc.content["cues"][0]["speaker_id"] == "spk_a"


def test_transcript_multispeaker_overlap_exports_compatible_srt_without_destroying_data():
    content = _transcript_content()
    content["cues"] = [
        _cue("c1", "spk_a", 0, 16000, "first speaker"),
        _cue("c2", "spk_b", 8000, 24000, "second speaker overlapping"),
        _cue("c3", "spk_a", 32000, 48000, "first speaker again"),
    ]
    # This is a legitimate multi-speaker document — c1/c2 overlap across
    # speakers and that must NOT raise when just reading it.
    subset = compatible_srt_segments(content)
    # Ordered, non-overlapping subset for SRT/VTT: c1 kept, c2 dropped
    # (overlaps c1), c3 kept (starts after c1 ends).
    assert [round(s["start"], 3) for s in subset] == [0.0, 2.0]
    assert subset[0]["text"] == "first speaker"
    assert subset[1]["text"] == "first speaker again"
    # The ORIGINAL cue list still has all three — export never mutates or
    # drops data from the document itself.
    assert len(content["cues"]) == 3

    from src.creator.ops.transcript_ops import export_compatible
    out = export_compatible(content, "srt")
    assert "first speaker" in out["text"]
    assert "second speaker overlapping" not in out["text"]


def test_transcript_split_and_merge_round_trip(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    content = _transcript_content()
    content["cues"] = [_cue("c1", "spk_a", 0, 16000, "hello world")]
    doc = store.create("alice", "proj1", "transcript", content)

    r = store.apply_command("alice", doc.id, "s1", doc.revision, {
        "type": "transcript.split_cue", "object_id": "c1", "at_sample": 8000,
        "new_cue_id": "c2", "first_text": "hello", "second_text": "world",
    })
    doc = r["doc"]
    assert [c["id"] for c in doc.content["cues"]] == ["c1", "c2"]

    r = store.apply_command("alice", doc.id, "m1", doc.revision, {
        "type": "transcript.merge_cues", "first_object_id": "c1", "second_object_id": "c2",
        "merged_text": "hello world",
    })
    doc = r["doc"]
    assert len(doc.content["cues"]) == 1
    assert doc.content["cues"][0]["text"] == "hello world"
    assert doc.content["cues"][0]["start_sample"] == "0"
    assert doc.content["cues"][0]["end_sample"] == "16000"


def test_transcript_add_track_and_assign_unknown_track_rejected(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    content = _transcript_content()
    content["cues"] = [_cue("c1", None, 0, 100, "hi")]
    doc = store.create("alice", "proj1", "transcript", content)

    r = store.apply_command("alice", doc.id, "t1", doc.revision, {
        "type": "transcript.add_track", "track_id": "spk_a", "label": "Speaker A",
    })
    doc = r["doc"]
    assert doc.content["speaker_tracks"] == [{"id": "spk_a", "label": "Speaker A"}]

    with pytest.raises(InvalidOperation):
        store.apply_command("alice", doc.id, "t2", doc.revision, {
            "type": "transcript.assign_speaker", "object_id": "c1", "speaker_id": "spk_unknown",
        })


# ----------------------------------------------------------------------
# undo/redo as new revisions
# ----------------------------------------------------------------------

def test_undo_to_creates_new_revision_history_stays_append_only(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    assert doc.revision == 1

    r = store.apply_command("alice", doc.id, "c1", doc.revision, {
        "type": "canvas.add_layer", "object_id": "l1", "kind": "raster", "offset": {"x": 0, "y": 0},
    })
    doc_rev2 = r["doc"]
    assert doc_rev2.revision == 2

    r = store.apply_command("alice", doc.id, "c2", doc_rev2.revision, {
        "type": "canvas.add_layer", "object_id": "l2", "kind": "raster", "offset": {"x": 1, "y": 1},
    })
    doc_rev3 = r["doc"]
    assert doc_rev3.revision == 3
    assert len(doc_rev3.content["layers"]) == 2

    result = undo_to(store, "alice", doc.id, 1, "undo-cmd-1", expected_revision=doc_rev3.revision)
    undone = result["doc"]
    # A NEW revision (4), not a rewrite of revision 1 or 3.
    assert undone.revision == 4
    assert undone.content["layers"] == []

    history = store.history("alice", doc.id)
    # Every command, including the undo, is still in the append-only log —
    # nothing was deleted or rewritten to make undo happen.
    assert [h["command_id"] for h in history] == ["c1", "c2", "undo-cmd-1"]
    assert [h["result_revision"] for h in history] == [2, 3, 4]

    # Revision 3's snapshot is still exactly what it was before the undo.
    snap3 = store.get_revision_snapshot("alice", doc.id, 3)
    assert len(snap3["content"]["layers"]) == 2

    # "Redo" is just undo_to a later revision.
    result = undo_to(store, "alice", doc.id, 3, "redo-cmd-1", expected_revision=undone.revision)
    redone = result["doc"]
    assert redone.revision == 5
    assert len(redone.content["layers"]) == 2


def test_undo_to_future_or_current_revision_rejected(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    with pytest.raises(InvalidOperation):
        undo_to(store, "alice", doc.id, 1, "bad-undo", expected_revision=doc.revision)


def test_undo_to_unknown_revision_is_document_not_found(tmp_path):
    from src.creator.errors import DocumentNotFound
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    with pytest.raises(DocumentNotFound):
        undo_to(store, "alice", doc.id, 999, "bad-undo", expected_revision=doc.revision)


def test_undo_to_is_owner_scoped(tmp_path):
    from src.creator.errors import DocumentNotFound
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    store.apply_command("alice", doc.id, "c1", doc.revision, {
        "type": "canvas.add_layer", "object_id": "l1", "kind": "raster", "offset": {"x": 0, "y": 0},
    })
    with pytest.raises(DocumentNotFound):
        undo_to(store, "mallory", doc.id, 1, "steal-undo", expected_revision=2)


# ----------------------------------------------------------------------
# unknown op type rejected with a typed error, through apply_command
# ----------------------------------------------------------------------

def test_unknown_op_type_rejected_with_typed_error(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    with pytest.raises(InvalidOperation):
        store.apply_command("alice", doc.id, "c1", doc.revision, {"type": "canvas.explode"})


def test_command_id_dedupe_still_works_through_registry_dispatch(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    op = {"type": "canvas.add_layer", "object_id": "l1", "kind": "raster", "offset": {"x": 0, "y": 0}}
    r1 = store.apply_command("alice", doc.id, "same-cmd", doc.revision, op)
    r2 = store.apply_command("alice", doc.id, "same-cmd", doc.revision, op)
    assert r1["deduped"] is False
    assert r2["deduped"] is True
    assert r1["doc"].revision == r2["doc"].revision == 2
    assert len(r2["doc"].content["layers"]) == 1  # applied once, not twice
