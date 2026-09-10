"""MEDIA-05 — subtitle export (SRT/VTT).

`src.media_subtitles` turns already-timestamped segments (the shape
`services/local_video.py`'s `transcribe()` already produces) into the two
subtitle formats nothing in the repo could write before this. Pure text
formatting — no video or audio file is ever opened — so "changing
subtitles" can never touch the source media, which is MEDIA-05's actual
acceptance criterion for this half of the requirement.

Revert-and-fail check (COMUN.md rule 5): before this module existed there
was no way to get an SRT/VTT string out of a transcript at all — grep for
"\\.srt\\|\\.vtt\\|WEBVTT" across `src/`, `services/`, `routes/` (excluding
this batch's new files) turns up nothing. `tests/test_p1_media_subtitles.py`
therefore fails outright on the pre-lot tree with `ModuleNotFoundError:
No module named 'src.media_subtitles'` — verified by running it against a
copy of the tree with `src/media_subtitles.py` removed.
"""
from __future__ import annotations

from src import media_subtitles as subs


SEGMENTS = [
    {"start": 0.0, "end": 1.5, "text": "Hola,   mundo"},
    {"start": 1.5, "end": 3.25, "text": "segunda línea"},
]


def test_to_srt_numbers_cues_and_formats_timestamps():
    out = subs.to_srt(SEGMENTS)
    assert out == (
        "1\n00:00:00,000 --> 00:00:01,500\nHola, mundo\n\n"
        "2\n00:00:01,500 --> 00:00:03,250\nsegunda línea\n"
    )


def test_to_vtt_has_the_header_and_dot_decimal_timestamps():
    out = subs.to_vtt(SEGMENTS)
    assert out.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.500" in out
    assert "," not in out.split("WEBVTT\n\n", 1)[1].split("\n")[0]


def test_blank_text_segments_are_skipped_not_emitted_as_empty_cues():
    segments = [{"start": 0, "end": 1, "text": "   "}, {"start": 1, "end": 2, "text": "real"}]
    srt = subs.to_srt(segments)
    assert srt.count("-->") == 1
    assert "real" in srt


def test_an_hour_plus_timestamp_rolls_into_the_hours_field():
    segments = [{"start": 3661.2, "end": 3662.0, "text": "late"}]
    assert "01:01:01,200 -->" in subs.to_srt(segments)


def test_export_dispatches_by_format_and_refuses_unknown_ones():
    srt = subs.export(SEGMENTS, "srt")
    assert srt["filename"] == "subtitles.srt" and srt["media_type"] == "application/x-subrip"
    vtt = subs.export(SEGMENTS, "vtt")
    assert vtt["filename"] == "subtitles.vtt" and vtt["media_type"] == "text/vtt"
    try:
        subs.export(SEGMENTS, "ass")
        assert False, "an unsupported format must be refused"
    except ValueError as e:
        assert "ass" in str(e)


def test_empty_segments_produce_empty_but_well_formed_output():
    assert subs.to_srt([]) == ""
    assert subs.to_vtt([]) == "WEBVTT\n\n"
