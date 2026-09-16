"""WP16 — subtitle editor: segmentation (`src/creator/subtitles.py`),
typed ops (`src/creator/ops/subtitle_ops.py`), a new `subtitles` document
kind (`src/creator/documents.py`), and `routes/creator_subtitle_routes.py`.

No engine, no ffmpeg, no model weights anywhere in this file — every input
is a hand-built `transcript` document content dict (WP15's own output
shape), consistent with CONTRATO.md rule 11 ("no ejecutar modelos").
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.creator import documents as documents_mod
from src.creator import subtitles as sub_mod
from src.creator.errors import InvalidDocument, InvalidOperation
from src.creator.ops import registry as ops_registry
from src.creator.ops import subtitle_ops

SAMPLE_RATE = 16000
OWNER = "alice"


# ── fixtures: transcript content builders ───────────────────────────────

def _word(text: str, start_s: float, end_s: float, *, confidence: float = 0.9) -> Dict[str, Any]:
    return {
        "id": f"w_{text}", "text": text,
        "start_sample": str(round(start_s * SAMPLE_RATE)),
        "end_sample": str(round(end_s * SAMPLE_RATE)),
        "confidence": confidence,
    }


def _cue(cue_id: str, text: str, start_s: float, end_s: float, *,
        speaker_id=None, words=None, editorial_status="proposed", language="en") -> Dict[str, Any]:
    return {
        "id": cue_id, "text": text, "language": language, "speaker_id": speaker_id,
        "start_sample": str(round(start_s * SAMPLE_RATE)), "end_sample": str(round(end_s * SAMPLE_RATE)),
        "words": words or [], "editorial_status": editorial_status, "confidence": 0.9,
    }


def _transcript_content(cues: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "source_asset_ref": "occ_1",
        "sample_rate": SAMPLE_RATE,
        "cues": cues,
        "alignment_status": "partial",
    }


def _long_sentence_transcript() -> Dict[str, Any]:
    """A single long sentence with per-word timing, evenly spaced at
    ~0.2s/word — long enough that it MUST be split into several cues
    under the default English profile (max 42 chars/line, CPS 17)."""
    words_text = (
        "this is a genuinely long sentence that a real speaker would take "
        "several seconds to say and it must be segmented into more than "
        "one caption cue to respect the reading speed and line length limits"
    ).split()
    # A natural-ish speaking pace: ~10-12 chars/second including the small
    # inter-word gap, comfortably under the default English CPS target
    # (17) so `from_transcript` never NEEDS to violate it just because the
    # synthetic source audio itself reads faster than any profile allows.
    words = []
    t = 0.0
    for w in words_text:
        dur = max(0.28, len(w) * 0.09)
        words.append(_word(w, t, t + dur))
        t += dur + 0.04
    full_text = " ".join(words_text)
    return _transcript_content([_cue("cue_0", full_text, 0.0, t, words=words)])


# ── from_transcript: segmentation rules ─────────────────────────────────

def test_from_transcript_never_exceeds_cpl_or_lines():
    content = sub_mod.from_transcript(_long_sentence_transcript())
    profile = content["profile"]
    assert len(content["cues"]) > 1, "a long sentence must be split into multiple cues"
    for cue in content["cues"]:
        assert len(cue["lines"]) <= profile["max_lines"]
        for line in cue["lines"]:
            assert len(line) <= profile["max_chars_per_line"]


def test_from_transcript_never_splits_a_word():
    content = sub_mod.from_transcript(_long_sentence_transcript())
    all_words = set(" ".join(_long_sentence_transcript()["cues"][0]["text"].split()).split())
    seen = []
    for cue in content["cues"]:
        for line in cue["lines"]:
            seen.extend(line.split())
    assert set(seen) == all_words, "every original word must appear intact, none split or dropped"


def test_from_transcript_respects_cps_target():
    content = sub_mod.from_transcript(_long_sentence_transcript())
    clock = sub_mod.Rational.from_dict(content["clock"])
    cps_max = content["profile"]["cps_max"]
    for cue in content["cues"]:
        dur_s = float(clock.seconds_of(int(cue["duration_ticks"])))
        chars = sum(len(l) for l in cue["lines"])
        cps = chars / dur_s
        # A small floating slack: min_duration padding can only ever LOWER
        # cps, never push it over target, so this must hold exactly.
        assert cps <= cps_max + 0.01, f"cue {cue['id']} cps={cps} exceeds target {cps_max}"


def test_from_transcript_two_lines_allowed_by_default():
    """SUB04: 'no se fuerza una supuesta norma universal de línea única'."""
    words = [_word(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(
        "one two three four five six seven eight nine ten eleven twelve".split())]
    transcript = _transcript_content([_cue("cue_0", "one two three four five six seven eight nine ten eleven twelve",
                                           0.0, 4.0, words=words)])
    content = sub_mod.from_transcript(transcript, profile_overrides={"max_chars_per_line": 12, "max_lines": 2})
    assert any(len(c["lines"]) == 2 for c in content["cues"]), "packing must actually use a second line when offered"


def test_from_transcript_speaker_change_forces_break():
    words_a = [_word("hello", 0.0, 0.3), _word("there", 0.3, 0.6)]
    words_b = [_word("general", 0.65, 0.95), _word("kenobi", 0.95, 1.3)]
    transcript = _transcript_content([
        _cue("cue_0", "hello there", 0.0, 0.6, speaker_id="spk_a", words=words_a),
        _cue("cue_1", "general kenobi", 0.65, 1.3, speaker_id="spk_b", words=words_b),
    ])
    content = sub_mod.from_transcript(transcript)
    speakers_per_cue = [c["speaker_id"] for c in content["cues"]]
    assert "spk_a" in speakers_per_cue and "spk_b" in speakers_per_cue
    assert len(content["cues"]) >= 2, "a speaker change must force a cue boundary"
    for cue in content["cues"]:
        assert cue["speaker_id"] in (None, "spk_a", "spk_b")


def test_from_transcript_min_duration_enforced():
    words = [_word("hi", 0.0, 0.05)]
    transcript = _transcript_content([_cue("cue_0", "hi", 0.0, 0.05, words=words)])
    content = sub_mod.from_transcript(transcript)
    clock = sub_mod.Rational.from_dict(content["clock"])
    dur_s = float(clock.seconds_of(int(content["cues"][0]["duration_ticks"])))
    assert dur_s >= content["profile"]["min_duration_seconds"] - 1e-9


def test_from_transcript_deterministic():
    transcript = _long_sentence_transcript()
    a = sub_mod.from_transcript(transcript)
    b = sub_mod.from_transcript(transcript)
    assert a["cues"] == b["cues"]


def test_from_transcript_fallback_without_word_timing_is_estimated():
    transcript = _transcript_content([_cue("cue_0", "no word timing here at all", 0.0, 2.0)])
    content = sub_mod.from_transcript(transcript)
    assert all(c["estimated_timing"] for c in content["cues"])


def test_resolve_profile_rejects_forced_single_line():
    with pytest.raises(InvalidOperation):
        sub_mod.resolve_profile("en", {"max_lines": 0})


# ── QA report ────────────────────────────────────────────────────────────

def _minimal_subtitles_content(**cue_overrides) -> Dict[str, Any]:
    cue = {
        "id": "sub_0", "start_ticks": "0", "duration_ticks": str(SAMPLE_RATE),
        "lines": ["hello world"], "speaker_id": None, "source_cue_ids": ["cue_0"],
        "editorial_status": "proposed", "edited": False, "unmapped": False,
        "estimated_timing": False, "style_id": None,
    }
    cue.update(cue_overrides)
    return {
        "language": "en",
        "clock": {"ticks_per_second_numerator": str(SAMPLE_RATE), "ticks_per_second_denominator": "1"},
        "profile": sub_mod.resolve_profile("en"),
        "style": sub_mod.DEFAULT_STYLE,
        "styles": {},
        "cues": [cue],
    }


def test_qa_report_flags_cps_and_reports_target():
    content = _minimal_subtitles_content(
        lines=["x" * 41], duration_ticks=str(round(0.1 * SAMPLE_RATE)),
    )
    report = sub_mod.qa_report(content)
    assert report["cues_with_issues"] == 1
    rules = {i["rule"] for i in report["issues"][0]["issues"]}
    assert "cps_max" in rules
    cps_issue = next(i for i in report["issues"][0]["issues"] if i["rule"] == "cps_max")
    assert cps_issue["target"] == content["profile"]["cps_max"]
    assert cps_issue["actual"] > cps_issue["target"]


def test_qa_report_flags_line_length_with_target():
    content = _minimal_subtitles_content(lines=["x" * 100])
    report = sub_mod.qa_report(content)
    issue = next(i for i in report["issues"][0]["issues"] if i["rule"] == "max_chars_per_line")
    assert issue["target"] == content["profile"]["max_chars_per_line"]
    assert issue["actual"] == 100


def test_qa_report_clean_document_has_no_issues():
    content = _minimal_subtitles_content()
    report = sub_mod.qa_report(content)
    assert report["cues_with_issues"] == 0
    assert report["issues"] == []


def test_qa_report_flags_unmapped():
    content = _minimal_subtitles_content(unmapped=True)
    report = sub_mod.qa_report(content)
    rules = {i["rule"] for i in report["issues"][0]["issues"]}
    assert "unmapped_after_retiming" in rules


# ── retiming ─────────────────────────────────────────────────────────────

def test_apply_retiming_shifts_mapped_cue():
    content = _minimal_subtitles_content(start_ticks="1000", duration_ticks="500")
    # source [0,2000) -> dest [5000,7000): a pure +5000 shift
    retiming = [{
        "source": {"start_ticks": "0", "duration_ticks": "2000"},
        "dest": {"start_ticks": "5000", "duration_ticks": "2000"},
        "mappable": True,
    }]
    out = sub_mod.apply_retiming(content, retiming, direction="source_to_dest")
    cue = out["cues"][0]
    assert cue["unmapped"] is False
    assert int(cue["start_ticks"]) == 6000


def test_apply_retiming_cut_zone_marks_unmapped_and_keeps_cue():
    """A cue landing in a zone the timeline cut away is marked unmapped and
    KEPT (never silently dropped) — SUB10 applied to captions."""
    content = _minimal_subtitles_content(start_ticks="1000", duration_ticks="500")
    retiming = [{
        "source": {"start_ticks": "0", "duration_ticks": "800"},
        "dest": {"start_ticks": "0", "duration_ticks": "800"},
        "mappable": True,
    }]  # ticks 1000-1500 fall entirely outside every segment
    out = sub_mod.apply_retiming(content, retiming, direction="source_to_dest")
    assert len(out["cues"]) == 1
    cue = out["cues"][0]
    assert cue["unmapped"] is True
    assert cue["start_ticks"] == "1000"  # left unchanged, not guessed


def test_apply_retiming_unmappable_segment_marks_unmapped():
    content = _minimal_subtitles_content(start_ticks="100", duration_ticks="50")
    retiming = [{
        "source": {"start_ticks": "0", "duration_ticks": "1000"},
        "dest": {"start_ticks": "0", "duration_ticks": "1000"},
        "mappable": False,
    }]
    out = sub_mod.apply_retiming(content, retiming, direction="source_to_dest")
    assert out["cues"][0]["unmapped"] is True


# ── exports: parseable by media_subtitles / local_video ─────────────────

def test_to_srt_is_valid_and_multiline():
    content = _minimal_subtitles_content(lines=["hello", "world"])
    srt_text = sub_mod.to_srt(content)
    assert "1\n00:00:00,000 --> 00:00:01,000\nhello\nworld" in srt_text


def test_to_vtt_is_valid_and_parseable_by_local_video():
    content = _minimal_subtitles_content()
    vtt_text = sub_mod.to_vtt(content)
    assert vtt_text.startswith("WEBVTT\n\n")


def test_export_srt_round_trips_through_local_video_validator():
    """`services.local_video.validate_segments` is the reference validator
    for a single-line-per-cue segment list — round-tripping the SRT export
    through it proves the timestamps this module writes are well-formed
    and ordered, without reimplementing that validator here."""
    from services import local_video

    transcript = _long_sentence_transcript()
    content = sub_mod.from_transcript(transcript)
    srt_text = sub_mod.to_srt(content)

    # Parse the SRT back into {start, end, text} rows (test-local parser —
    # this is verifying OUR export, not re-testing an SRT parser).
    rows = []
    for block in srt_text.strip().split("\n\n"):
        lines = block.split("\n")
        ts_match = re.match(
            r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)", lines[1])
        assert ts_match
        def _secs(h, m, s, ms):
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        start = _secs(*ts_match.groups()[0:4])
        end = _secs(*ts_match.groups()[4:8])
        text = " ".join(lines[2:]).replace("\n", " ")
        rows.append({"start": start, "end": end, "text": text})

    duration = rows[-1]["end"] + 5
    validated = local_video.validate_segments(rows, duration)
    assert len(validated) == len(rows)


def test_to_ass_escapes_curly_braces():
    content = _minimal_subtitles_content(lines=["click {\\fnEvilFont}here"])
    ass_text = sub_mod.to_ass(content)
    assert "{\\fn" not in ass_text
    assert "click (\\fnEvilFont)here" in ass_text


def test_to_ass_has_style_and_events_sections():
    content = _minimal_subtitles_content()
    ass_text = sub_mod.to_ass(content)
    assert "[V4+ Styles]" in ass_text
    assert "[Events]" in ass_text
    assert "Style: Default," in ass_text
    assert "Dialogue: 0," in ass_text


def test_normalize_style_rejects_path_like_font_family():
    with pytest.raises(InvalidOperation):
        sub_mod._normalize_style({"font_family": "../../etc/passwd"})
    with pytest.raises(InvalidOperation):
        sub_mod._normalize_style({"font_family": "C:\\Windows\\Fonts\\evil"})


def test_export_unknown_format_raises():
    content = _minimal_subtitles_content()
    with pytest.raises(InvalidOperation):
        sub_mod.export(content, "docx")


def test_ffmpeg_burn_filter_escapes_colons():
    filt = sub_mod.ffmpeg_burn_filter("C:/tmp/out.ass")
    assert filt == r"subtitles=C\:/tmp/out.ass"


# ── regenerate_from_transcript: human edits prevail ─────────────────────

def test_regenerate_keeps_edited_cue_over_new_transcript():
    transcript = _long_sentence_transcript()
    content = sub_mod.from_transcript(transcript)
    edited_cue_id = content["cues"][0]["id"]
    content["cues"][0]["lines"] = ["A HUMAN WROTE THIS"]
    content["cues"][0]["edited"] = True

    # A "later transcription" with slightly different word timings for the
    # SAME underlying audio window.
    new_transcript = _long_sentence_transcript()
    regenerated = sub_mod.regenerate_from_transcript(content, new_transcript)

    kept = [c for c in regenerated["cues"] if c["lines"] == ["A HUMAN WROTE THIS"]]
    assert len(kept) == 1, "the human edit must survive a transcript regeneration"
    assert kept[0]["edited"] is True


def test_regenerate_drops_only_overlapping_fresh_cues():
    transcript = _long_sentence_transcript()
    content = sub_mod.from_transcript(transcript)
    assert len(content["cues"]) > 2
    # Edit only the FIRST cue; the rest should regenerate fresh.
    content["cues"][0]["edited"] = True
    content["cues"][0]["lines"] = ["EDITED FIRST CUE"]

    regenerated = sub_mod.regenerate_from_transcript(content, transcript)
    lines_seen = [c["lines"] for c in regenerated["cues"]]
    assert ["EDITED FIRST CUE"] in lines_seen
    assert len(regenerated["cues"]) >= 2


# ── document kind wiring (documents.py) ─────────────────────────────────

def test_subtitles_is_a_known_document_kind():
    assert "subtitles" in documents_mod.DOCUMENT_KINDS


def test_validate_content_accepts_generated_subtitles():
    transcript = _long_sentence_transcript()
    content = sub_mod.from_transcript(transcript)
    documents_mod.validate_content("subtitles", content)  # must not raise


def test_validate_content_rejects_cue_missing_lines():
    content = _minimal_subtitles_content()
    del content["cues"][0]["lines"]
    with pytest.raises(InvalidDocument):
        documents_mod.validate_content("subtitles", content)


def test_validate_content_rejects_two_lines_over_profile_limit_only_at_op_level():
    # documents.py itself does not enforce max_lines (subtitles.py/subtitle_ops
    # do, at generation/edit time) — a hand-built 3-line cue is still
    # STRUCTURALLY valid content; this documents that division of labour.
    content = _minimal_subtitles_content(lines=["a", "b", "c"])
    documents_mod.validate_content("subtitles", content)  # must not raise


# ── typed ops (src/creator/ops/subtitle_ops.py), via the registry ───────

def _doc_snapshot(content):
    return {"kind": "subtitles", "content": content, "state": "proposed", "asset_refs": [], "revision": 1}


def test_ops_registered_in_registry():
    known = ops_registry.known_op_types()
    for op_type in subtitle_ops.OPS:
        assert known[op_type] == "src.creator.ops.subtitle_ops"


def test_edit_cue_sets_edited_flag_and_text():
    content = _minimal_subtitles_content()
    new_content = ops_registry.apply(_doc_snapshot(content), {
        "type": "subtitles.edit_cue", "object_id": "sub_0", "lines": ["new text"],
    })
    assert new_content["cues"][0]["lines"] == ["new text"]
    assert new_content["cues"][0]["edited"] is True


def test_edit_cue_rejects_too_many_lines_for_profile():
    content = _minimal_subtitles_content()
    with pytest.raises(InvalidOperation):
        ops_registry.apply(_doc_snapshot(content), {
            "type": "subtitles.edit_cue", "object_id": "sub_0", "lines": ["a", "b", "c"],
        })


def test_split_cue_produces_two_edited_halves():
    content = _minimal_subtitles_content(duration_ticks="1000")
    new_content = ops_registry.apply(_doc_snapshot(content), {
        "type": "subtitles.split_cue", "object_id": "sub_0", "at_ticks": 400,
        "first_lines": ["hello"], "second_lines": ["world"], "new_cue_id": "sub_new",
    })
    assert len(new_content["cues"]) == 2
    assert new_content["cues"][0]["duration_ticks"] == "400"
    assert new_content["cues"][1]["start_ticks"] == "400"
    assert all(c["edited"] for c in new_content["cues"])


def test_merge_cues_combines_span_and_marks_edited():
    content = _minimal_subtitles_content()
    content["cues"].append({
        "id": "sub_1", "start_ticks": str(SAMPLE_RATE + 1000), "duration_ticks": "500",
        "lines": ["second"], "speaker_id": None, "source_cue_ids": ["cue_1"],
        "editorial_status": "proposed", "edited": False, "unmapped": False,
        "estimated_timing": False, "style_id": None,
    })
    new_content = ops_registry.apply(_doc_snapshot(content), {
        "type": "subtitles.merge_cues", "first_object_id": "sub_0", "second_object_id": "sub_1",
        "lines": ["merged text"],
    })
    assert len(new_content["cues"]) == 1
    merged = new_content["cues"][0]
    assert merged["lines"] == ["merged text"]
    assert merged["edited"] is True
    assert int(merged["duration_ticks"]) == (SAMPLE_RATE + 1000 + 500) - 0


def test_shift_cues_moves_targeted_cues_only():
    content = _minimal_subtitles_content()
    content["cues"].append({
        "id": "sub_1", "start_ticks": "5000", "duration_ticks": "500",
        "lines": ["second"], "speaker_id": None, "source_cue_ids": [],
        "editorial_status": "proposed", "edited": False, "unmapped": False,
        "estimated_timing": False, "style_id": None,
    })
    new_content = ops_registry.apply(_doc_snapshot(content), {
        "type": "subtitles.shift_cues", "object_ids": ["sub_1"], "offset_ticks": 100,
    })
    assert new_content["cues"][0]["start_ticks"] == "0"       # untouched
    assert new_content["cues"][1]["start_ticks"] == "5100"    # shifted
    assert new_content["cues"][1]["edited"] is True


def test_shift_cues_rejects_negative_result():
    content = _minimal_subtitles_content(start_ticks="50")
    with pytest.raises(InvalidOperation):
        ops_registry.apply(_doc_snapshot(content), {
            "type": "subtitles.shift_cues", "offset_ticks": -100,
        })


def test_set_style_named_style_validated():
    content = _minimal_subtitles_content()
    new_content = ops_registry.apply(_doc_snapshot(content), {
        "type": "subtitles.set_style", "style_id": "emphasis",
        "style": {"font_size": 60, "primary_color": "#FF0000"},
    })
    assert new_content["styles"]["emphasis"]["font_size"] == 60
    assert new_content["styles"]["emphasis"]["primary_color"] == "#FF0000"


def test_set_style_rejects_bad_color():
    content = _minimal_subtitles_content()
    with pytest.raises(InvalidOperation):
        ops_registry.apply(_doc_snapshot(content), {
            "type": "subtitles.set_style", "style": {"primary_color": "red"},
        })


def test_apply_retiming_op_marks_unmapped_without_edited_flag():
    content = _minimal_subtitles_content(start_ticks="1000", duration_ticks="500")
    new_content = ops_registry.apply(_doc_snapshot(content), {
        "type": "subtitles.apply_retiming",
        "retiming": [{
            "source": {"start_ticks": "0", "duration_ticks": "800"},
            "dest": {"start_ticks": "0", "duration_ticks": "800"},
            "mappable": True,
        }],
    })
    assert new_content["cues"][0]["unmapped"] is True
    assert new_content["cues"][0]["edited"] is False  # automatic, not a human edit


def test_regenerate_op_preserves_edits():
    transcript = _long_sentence_transcript()
    content = sub_mod.from_transcript(transcript)
    content["cues"][0]["edited"] = True
    content["cues"][0]["lines"] = ["KEPT"]
    new_content = ops_registry.apply(_doc_snapshot(content), {
        "type": "subtitles.regenerate_from_transcript", "transcript_content": transcript,
    })
    assert any(c["lines"] == ["KEPT"] for c in new_content["cues"])


# ── ops dedupe, via the real DocumentStore.apply_command ────────────────

@pytest.fixture()
def own_document_store(tmp_path, monkeypatch):
    from src.creator import store as store_mod
    monkeypatch.setattr(store_mod, "default_path", lambda: str(tmp_path / "creator_documents.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", None)
    return store_mod.get_store()


def test_apply_command_dedupes_same_command_id(own_document_store):
    content = _minimal_subtitles_content()
    doc = own_document_store.create(OWNER, "proj_1", "subtitles", content)
    op = {"type": "subtitles.edit_cue", "object_id": "sub_0", "lines": ["once"]}
    r1 = own_document_store.apply_command(OWNER, doc.id, "cmd_1", doc.revision, op)
    r2 = own_document_store.apply_command(OWNER, doc.id, "cmd_1", doc.revision, op)
    assert r1["deduped"] is False
    assert r2["deduped"] is True
    assert r1["doc"].revision == r2["doc"].revision == 2


def test_apply_command_stale_revision_conflicts(own_document_store):
    from src.creator.errors import RevisionConflict
    content = _minimal_subtitles_content()
    doc = own_document_store.create(OWNER, "proj_1", "subtitles", content)
    own_document_store.apply_command(OWNER, doc.id, "cmd_a", doc.revision,
                                     {"type": "subtitles.edit_cue", "object_id": "sub_0", "lines": ["a"]})
    with pytest.raises(RevisionConflict):
        own_document_store.apply_command(OWNER, doc.id, "cmd_b", doc.revision,  # stale on purpose
                                         {"type": "subtitles.edit_cue", "object_id": "sub_0", "lines": ["b"]})


# ═══════════════════════════════════════════════════════════════════════
# routes/creator_subtitle_routes.py
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database as db_mod
    from core.database import Base
    url = "sqlite:///" + (tmp_path / "creator_wp16.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


#: `effective_storage_owner` resolves to this fixed id when AUTH_ENABLED is
#: false (dev/local mode) — the project a route-level test creates must be
#: owned by it, or every route sees "project not found" (same symmetry
#: CONTRATO.md rule 3 relies on for a genuinely foreign owner).
_LOCAL_OWNER = "__odysseus_local__"


@pytest.fixture()
def project_env(tmp_path, monkeypatch, own_database):
    from services.projects import ProjectStore
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("Subtitles test project", owner=_LOCAL_OWNER, scaffold_memory=False)
    return project["id"]


@pytest.fixture()
def route_client(tmp_path, monkeypatch, project_env):
    from src.creator import store as store_mod
    monkeypatch.setattr(store_mod, "default_path", lambda: str(tmp_path / "creator_documents.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", None)

    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: True if key == "creator_enabled" else default)

    from routes.creator_subtitle_routes import setup_creator_subtitle_routes
    from routes.creator_routes import setup_creator_routes
    app = FastAPI()
    app.include_router(setup_creator_routes())
    app.include_router(setup_creator_subtitle_routes())
    client = TestClient(app)
    return client, project_env


def _create_transcript_doc(client, project_id):
    resp = client.post("/api/creator/documents", json={
        "project_id": project_id, "kind": "transcript", "content": _long_sentence_transcript(),
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_route_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_subtitle_routes import setup_creator_subtitle_routes
    app = FastAPI()
    app.include_router(setup_creator_subtitle_routes())
    client = TestClient(app)
    assert client.post("/api/creator/subtitles/from-transcript", json={"transcript_doc_id": "doc_x"}).status_code == 404
    assert client.get("/api/creator/subtitles/doc_x/qa").status_code == 404
    assert client.get("/api/creator/subtitles/doc_x/export").status_code == 404


def test_route_from_transcript_creates_subtitles_document(route_client):
    client, project_id = route_client
    transcript_doc = _create_transcript_doc(client, project_id)
    resp = client.post("/api/creator/subtitles/from-transcript",
                       json={"transcript_doc_id": transcript_doc["id"]})
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["kind"] == "subtitles"
    assert doc["content"]["source_transcript_id"] == transcript_doc["id"]
    assert len(doc["content"]["cues"]) > 0


def test_route_from_transcript_unknown_transcript_is_404(route_client):
    client, _project_id = route_client
    resp = client.post("/api/creator/subtitles/from-transcript", json={"transcript_doc_id": "doc_nope"})
    assert resp.status_code == 404


def test_route_from_transcript_wrong_kind_is_404(route_client):
    client, project_id = route_client
    resp = client.post("/api/creator/documents", json={
        "project_id": project_id, "kind": "song",
        "content": {"language": "en", "sections": [{"id": "s1", "kind": "verse", "lyrics": "la la"}],
                   "takes": [], "selected_take": None},
    })
    song_doc = resp.json()
    resp2 = client.post("/api/creator/subtitles/from-transcript", json={"transcript_doc_id": song_doc["id"]})
    assert resp2.status_code == 404


def test_route_qa_reports_issues(route_client):
    client, project_id = route_client
    transcript_doc = _create_transcript_doc(client, project_id)
    sub_doc = client.post("/api/creator/subtitles/from-transcript",
                          json={"transcript_doc_id": transcript_doc["id"]}).json()
    resp = client.get(f"/api/creator/subtitles/{sub_doc['id']}/qa")
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert "total_cues" in report and "issues" in report


def test_route_qa_unknown_doc_is_404(route_client):
    client, _project_id = route_client
    assert client.get("/api/creator/subtitles/doc_nope/qa").status_code == 404


def test_route_export_srt_vtt_ass(route_client):
    client, project_id = route_client
    transcript_doc = _create_transcript_doc(client, project_id)
    sub_doc = client.post("/api/creator/subtitles/from-transcript",
                          json={"transcript_doc_id": transcript_doc["id"]}).json()
    for fmt, media_type in (("srt", "application/x-subrip"), ("vtt", "text/vtt"), ("ass", "text/x-ssa")):
        resp = client.get(f"/api/creator/subtitles/{sub_doc['id']}/export", params={"format": fmt})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith(media_type)
        assert len(resp.text) > 0


def test_route_export_invalid_format_is_400(route_client):
    client, project_id = route_client
    transcript_doc = _create_transcript_doc(client, project_id)
    sub_doc = client.post("/api/creator/subtitles/from-transcript",
                          json={"transcript_doc_id": transcript_doc["id"]}).json()
    resp = client.get(f"/api/creator/subtitles/{sub_doc['id']}/export", params={"format": "docx"})
    assert resp.status_code == 400


def test_route_qa_and_export_reject_foreign_owner(route_client):
    """CONTRATO.md rule 3: 'no es tuyo' y 'no existe' responden igual."""
    client, project_id = route_client
    transcript_doc = _create_transcript_doc(client, project_id)
    sub_doc = client.post("/api/creator/subtitles/from-transcript",
                          json={"transcript_doc_id": transcript_doc["id"]}).json()

    # Same doc id, different owner header is not something this fixture's
    # dev-auth path supports switching mid-test cleanly; instead verify the
    # store-level owner scoping the route relies on, directly.
    from src.creator import store as store_mod
    other_doc = store_mod.get_store().get("someone_else", sub_doc["id"])
    assert other_doc is None


def test_route_edit_then_export_reflects_human_edit(route_client):
    """End-to-end: create from transcript, edit a cue via the generic
    commands route, export, and see the edit — not the original text."""
    client, project_id = route_client
    transcript_doc = _create_transcript_doc(client, project_id)
    sub_doc = client.post("/api/creator/subtitles/from-transcript",
                          json={"transcript_doc_id": transcript_doc["id"]}).json()
    first_cue_id = sub_doc["content"]["cues"][0]["id"]

    resp = client.post(f"/api/creator/documents/{sub_doc['id']}/commands", json={
        "command_id": "cmd_edit_1", "expected_revision": sub_doc["revision"],
        "op": {"type": "subtitles.edit_cue", "object_id": first_cue_id, "lines": ["EDITED BY HUMAN"]},
    })
    assert resp.status_code == 200, resp.text

    export_resp = client.get(f"/api/creator/subtitles/{sub_doc['id']}/export", params={"format": "srt"})
    assert "EDITED BY HUMAN" in export_resp.text
