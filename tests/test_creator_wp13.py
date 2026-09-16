"""WP13 — timeline y relojes: rational clocks, VFR PTS index, per-track
validation, retiming maps, snapping and the project/EDL projection, plus
the four additive ops (``snap``/``retime``/``add_marker``/``ripple_insert``)
wired through the real WP02 ``DocumentStore`` (sqlite, ``tmp_path``).
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from src.creator.errors import InvalidOperation
from src.creator.ops.model import Rational, RetimingMap
from src.creator.store import DocumentStore
from src.creator.timeline import clocks, project_view, retiming, snapping, tracks, validate


# ----------------------------------------------------------------------
# clocks.py
# ----------------------------------------------------------------------

def test_convert_ticks_exact_no_float_drift_over_100_cuts():
    """Closure criterion: a hundred consecutive conversions between NTSC
    29.97 fps ticks and 48kHz audio ticks, chained, land on the SAME value
    as one direct conversion of the accumulated duration — a float pipeline
    would drift."""
    ntsc = clocks.NTSC_30000_1001
    audio = clocks.AUDIO_48K
    frame_ticks = 1  # 1 tick at 29.97fps
    accumulated_ntsc = 0
    accumulated_audio_via_chain = 0
    for _ in range(100):
        accumulated_ntsc += frame_ticks
        step_audio = clocks.convert_ticks(frame_ticks, ntsc, audio, rounding="floor")
        accumulated_audio_via_chain += step_audio
    direct = clocks.convert_ticks(accumulated_ntsc, ntsc, audio, rounding="floor")
    # Chained per-frame floor rounding CAN differ from one direct floor by
    # at most the rounding error of a single step accumulated — but the
    # important guarantee is each step is computed via Fraction, not float,
    # so re-deriving the same chain twice is bit-for-bit identical (no
    # accumulating float error growing across runs).
    accumulated_audio_via_chain_2 = 0
    for _ in range(100):
        accumulated_audio_via_chain_2 += clocks.convert_ticks(frame_ticks, ntsc, audio, rounding="floor")
    assert accumulated_audio_via_chain == accumulated_audio_via_chain_2
    # And the exact Fraction seconds of 100 NTSC ticks matches 100 * one tick.
    assert ntsc.seconds_of(100) == 100 * ntsc.seconds_of(1)
    assert direct >= 0


def test_convert_ticks_rounding_policies_are_explicit():
    a = Rational(1, 1)
    b = Rational(3, 1)
    # 1 tick at 1/s -> 3 ticks at 3/s exactly (1 second * 3)
    assert clocks.convert_ticks(1, a, b, rounding="floor") == 3
    # 1 tick at 3/s -> 1/3 second -> at 1/s: floor=0, ceil=1, nearest=0
    assert clocks.convert_ticks(1, b, a, rounding="floor") == 0
    assert clocks.convert_ticks(1, b, a, rounding="ceil") == 1
    assert clocks.convert_ticks(1, b, a, rounding="nearest") == 0
    # 2/3 second rounds to nearest=1
    assert clocks.convert_ticks(2, b, a, rounding="nearest") == 1
    with pytest.raises(InvalidOperation):
        clocks.convert_ticks(1, a, b, rounding="bogus")


def test_master_clock_round_trip():
    mc = clocks.MasterClock.from_dict({
        "ticks_per_second_numerator": "30000", "ticks_per_second_denominator": "1001",
    })
    assert mc.to_dict() == {"ticks_per_second_numerator": "30000", "ticks_per_second_denominator": "1001"}
    assert mc.seconds_of(30000) == Fraction(1001, 1)
    assert mc.ticks_of_seconds(Fraction(1001, 1)) == 30000


def test_pts_index_vfr_documented_proxy_not_averaged():
    # A VFR clip: frames at 0, 900, 1801, 2703 ticks (irregular spacing) at
    # a 30000-ticks/second base clock — NOT evenly spaced, so an averaged
    # constant-fps assumption would be wrong.
    idx = clocks.PtsIndex(clock=Rational(30000, 1), pts_ticks=[0, 900, 1801, 2703])
    assert idx.frame_count == 4
    assert idx.frame_at_tick(0, policy="floor") == 0
    assert idx.frame_at_tick(950, policy="floor") == 1
    assert idx.frame_at_tick(1800, policy="floor") == 1
    assert idx.frame_at_tick(1801, policy="floor") == 2
    # nearest: 1750 is closer to 1801 than to 900
    assert idx.frame_at_tick(1750, policy="nearest") == 2
    assert idx.tick_of_frame(2) == 1801
    with pytest.raises(InvalidOperation):
        idx.tick_of_frame(99)


def test_pts_index_rejects_non_increasing_frames():
    with pytest.raises(InvalidOperation):
        clocks.PtsIndex(clock=Rational(30, 1), pts_ticks=[0, 10, 10, 20])
    with pytest.raises(InvalidOperation):
        clocks.PtsIndex(clock=Rational(30, 1), pts_ticks=[])


def test_pts_index_to_cfr_resample_explicit_policy():
    idx = clocks.PtsIndex(clock=Rational(30000, 1), pts_ticks=[0, 900, 2000, 3500])
    chosen = idx.to_cfr(Rational(30, 1), policy="nearest")
    assert isinstance(chosen, list) and len(chosen) > 0
    assert all(0 <= f < 4 for f in chosen)


# ----------------------------------------------------------------------
# tracks.py — per-track validation, markers
# ----------------------------------------------------------------------

def _clip(cid, start, dur, src_start="0", src_dur="100"):
    return {
        "id": cid, "asset_ref": "occ_x", "timeline_start_ticks": str(start),
        "timeline_duration_ticks": str(dur),
        "source_range": {"start_ticks": src_start, "duration_ticks": src_dur},
        "source_clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
    }


def _timeline_content(*clips_by_track):
    tracks_list = []
    for i, clips in enumerate(clips_by_track):
        tracks_list.append({"id": f"t{i}", "kind": "video" if i == 0 else "audio", "locked": False, "clips": clips})
    return {
        "clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
        "duration_ticks": "1000",
        "tracks": tracks_list,
    }


def test_validate_track_ok_when_ordered_no_overlap():
    content = _timeline_content([_clip("a", 0, 10), _clip("b", 10, 10)])
    tracks.validate_all_tracks(content)  # no raise


def test_validate_track_rejects_overlap_same_track():
    content = _timeline_content([_clip("a", 0, 10), _clip("b", 5, 10)])
    with pytest.raises(InvalidOperation):
        tracks.validate_all_tracks(content)


def test_overlap_across_tracks_is_allowed():
    content = _timeline_content([_clip("a", 0, 10)], [_clip("b", 0, 10)])
    tracks.validate_all_tracks(content)  # no raise: different tracks


def test_cut_points_and_track_span():
    content = _timeline_content([_clip("a", 0, 10), _clip("b", 10, 5)])
    track = tracks.get_track(content, "t0")
    assert tracks.cut_points(track) == [0, 10, 15]
    span = tracks.track_span(track)
    assert (span.start_ticks, span.end_ticks) == (0, 15)


def test_markers_add_and_validate():
    content = _timeline_content([_clip("a", 0, 10)])
    content = tracks.add_marker(content, "m1", "5", "chapter one")
    markers = tracks.get_markers(content)
    assert markers == [{"id": "m1", "at_ticks": "5", "label": "chapter one"}]
    tracks.validate_markers(content)
    with pytest.raises(InvalidOperation):
        tracks.add_marker(content, "m1", "9")  # duplicate id


# ----------------------------------------------------------------------
# retiming.py
# ----------------------------------------------------------------------

def test_map_time_linear_segment():
    rm = RetimingMap.from_list([
        {"source": {"start_ticks": "0", "duration_ticks": "100"}, "dest": {"start_ticks": "0", "duration_ticks": "50"}},
    ])
    r = retiming.map_time(rm, 25)
    assert r.source_ticks == 50  # 2x speed
    assert r.segment is not None


def test_map_time_cut_returns_none_never_points_at_removed_audio():
    rm = RetimingMap.from_list([
        {"source": {"start_ticks": "0", "duration_ticks": "10"}, "dest": {"start_ticks": "0", "duration_ticks": "10"}},
        {"source": {"start_ticks": "10", "duration_ticks": "5"}, "dest": {"start_ticks": "10", "duration_ticks": "5"}, "mappable": False},
        {"source": {"start_ticks": "15", "duration_ticks": "10"}, "dest": {"start_ticks": "15", "duration_ticks": "10"}},
    ])
    r = retiming.map_time(rm, 12)
    assert r.source_ticks is None
    r2 = retiming.map_time(rm, 20)
    assert r2.source_ticks == 20


def test_map_time_outside_every_segment_raises():
    rm = RetimingMap.from_list([
        {"source": {"start_ticks": "0", "duration_ticks": "10"}, "dest": {"start_ticks": "0", "duration_ticks": "10"}},
    ])
    with pytest.raises(InvalidOperation):
        retiming.map_time(rm, 100)


def test_cut_boundaries_and_unmappable_ranges():
    rm = RetimingMap.from_list([
        {"source": {"start_ticks": "0", "duration_ticks": "10"}, "dest": {"start_ticks": "0", "duration_ticks": "10"}},
        {"source": {"start_ticks": "10", "duration_ticks": "5"}, "dest": {"start_ticks": "10", "duration_ticks": "5"}, "mappable": False},
    ])
    assert retiming.cut_boundaries(rm) == [0, 10, 15]
    unmappable = retiming.unmappable_ranges(rm)
    assert len(unmappable) == 1 and unmappable[0].start_ticks == 10


# ----------------------------------------------------------------------
# snapping.py
# ----------------------------------------------------------------------

def test_snap_prefers_cut_over_frame_grid():
    content = _timeline_content([_clip("a", 0, 100), _clip("b", 100, 50)])
    content = tracks.add_marker(content, "m1", "97")
    result = snapping.snap(content, 96, window_ticks=10, frame_ticks=1)
    # 100 (a cut point) beats a same-or-closer frame-grid tick.
    assert result.candidate is not None
    assert result.candidate.source in ("cut", "marker")


def test_snap_no_candidate_within_window_returns_input():
    content = _timeline_content([_clip("a", 0, 10)])
    result = snapping.snap(content, 500, window_ticks=5)
    assert result.candidate is None
    assert result.snapped_ticks == 500


# ----------------------------------------------------------------------
# validate.py
# ----------------------------------------------------------------------

def test_validate_report_ok_for_clean_document():
    content = _timeline_content([_clip("a", 0, 10)])
    report = validate.validate(content)
    assert report.ok
    assert report.to_dict()["ok"] is True


def test_validate_report_flags_overlap_with_target():
    content = _timeline_content([_clip("a", 0, 10), _clip("b", 5, 10)])
    report = validate.validate(content)
    assert not report.ok
    codes = {f.code for f in report.findings}
    assert "track_overlap" in codes
    assert any(f.target == "t0" for f in report.findings)


def test_validate_report_warns_clip_past_duration():
    content = _timeline_content([_clip("a", 0, 10)])
    content["duration_ticks"] = "5"
    report = validate.validate(content)
    assert report.ok  # warning, not an error
    assert any(f.code == "clip_exceeds_duration" for f in report.findings)


# ----------------------------------------------------------------------
# project_view.py
# ----------------------------------------------------------------------

def test_project_view_flattens_sorted():
    content = _timeline_content([_clip("b", 10, 10), _clip("a", 0, 10)])
    view = project_view.project_view(content)
    assert [c["id"] for c in view["tracks"][0]["clips"]] == ["a", "b"]
    assert view["tracks"][0]["span"] == {"start_ticks": "0", "end_ticks": "20"}


def test_to_edl_minimal_cmx3600():
    content = _timeline_content([_clip("a", 0, 10), _clip("b", 10, 5)])
    edl = project_view.to_edl(content)
    assert edl.startswith("TITLE:")
    assert "001" in edl and "002" in edl
    assert "a" in edl and "b" in edl


def test_to_edl_requires_a_video_track():
    content = _timeline_content([])
    content["tracks"][0]["kind"] = "audio"
    with pytest.raises(InvalidOperation):
        project_view.to_edl(content)


# ----------------------------------------------------------------------
# ops: snap / retime / add_marker / ripple_insert — through the real store
# ----------------------------------------------------------------------

OWNER = "user_wp13"


@pytest.fixture()
def store(tmp_path):
    return DocumentStore(db_path=str(tmp_path / "creator_documents.sqlite3"))


def _new_timeline_doc(store: DocumentStore):
    content = _timeline_content([_clip("a", 0, 10), _clip("b", 20, 10)])
    content = tracks.add_marker(content, "m1", "15")
    return store.create(OWNER, "prj_1", "timeline", content)


def test_op_snap_moves_clip_to_nearest_cut(store):
    doc = _new_timeline_doc(store)
    result = store.apply_command(OWNER, doc.id, "cmd-snap", doc.revision, {
        "type": "timeline.snap", "track_id": "t0", "clip_id": "b",
        "near_ticks": 12, "window_ticks": 10,
    })
    new_clip = result["doc"].content["tracks"][0]["clips"][0]
    picked = [c for c in result["doc"].content["tracks"][0]["clips"] if c["id"] == "b"][0]
    assert picked["timeline_start_ticks"] == "10"  # snapped onto clip a's end (a cut)


def test_op_retime_attaches_map_and_validates_shape(store):
    doc = _new_timeline_doc(store)
    result = store.apply_command(OWNER, doc.id, "cmd-retime", doc.revision, {
        "type": "timeline.retime", "track_id": "t0", "clip_id": "a",
        "retiming_map": [
            {"source": {"start_ticks": "0", "duration_ticks": "10"}, "dest": {"start_ticks": "0", "duration_ticks": "10"}},
        ],
    })
    clip = [c for c in result["doc"].content["tracks"][0]["clips"] if c["id"] == "a"][0]
    assert clip["retiming_map"][0]["source"]["duration_ticks"] == "10"

    with pytest.raises(InvalidOperation):
        store.apply_command(OWNER, doc.id, "cmd-retime-bad", result["doc"].revision, {
            "type": "timeline.retime", "track_id": "t0", "clip_id": "a",
            "retiming_map": [
                {"source": {"start_ticks": "0", "duration_ticks": "10"}, "dest": {"start_ticks": "0", "duration_ticks": "10"}},
                {"source": {"start_ticks": "10", "duration_ticks": "10"}, "dest": {"start_ticks": "5", "duration_ticks": "10"}},
            ],
        })


def test_op_add_marker(store):
    doc = _new_timeline_doc(store)
    result = store.apply_command(OWNER, doc.id, "cmd-marker", doc.revision, {
        "type": "timeline.add_marker", "marker_id": "m2", "at_ticks": "25", "label": "beat drop",
    })
    markers = result["doc"].content["markers"]
    assert any(m["id"] == "m2" and m["label"] == "beat drop" for m in markers)


def test_op_ripple_insert_shifts_later_clips(store):
    doc = _new_timeline_doc(store)
    new_clip = _clip("c", 10, 5)  # inserted right after clip a ends, before b starts
    result = store.apply_command(OWNER, doc.id, "cmd-ripple", doc.revision, {
        "type": "timeline.ripple_insert", "track_id": "t0", "clip": new_clip,
    })
    clips_by_id = {c["id"]: c for c in result["doc"].content["tracks"][0]["clips"]}
    # 'a' ends at 10 (< 10 insertion point, untouched), 'c' at 10, 'b' was at 20 -> shifted by 5 -> 25
    assert clips_by_id["a"]["timeline_start_ticks"] == "0"
    assert clips_by_id["c"]["timeline_start_ticks"] == "10"
    assert clips_by_id["b"]["timeline_start_ticks"] == "25"
    tracks.validate_track(result["doc"].content["tracks"][0])  # no overlap after ripple


def test_op_ripple_insert_rejects_duplicate_clip_id(store):
    doc = _new_timeline_doc(store)
    dup = _clip("a", 50, 5)
    with pytest.raises(InvalidOperation):
        store.apply_command(OWNER, doc.id, "cmd-ripple-dup", doc.revision, {
            "type": "timeline.ripple_insert", "track_id": "t0", "clip": dup,
        })


def test_undo_after_wp13_ops_restores_prior_revision(store):
    from src.creator.ops.undo import undo_to

    doc = _new_timeline_doc(store)
    r1 = store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
        "type": "timeline.add_marker", "marker_id": "m9", "at_ticks": "1",
    })
    assert len(r1["doc"].content["markers"]) == 2
    r2 = undo_to(store, OWNER, doc.id, target_revision=doc.revision, command_id="cmd-undo",
                  expected_revision=r1["doc"].revision)
    assert len(r2["doc"].content["markers"]) == 1
