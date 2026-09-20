"""Unit tests for src/stt_cleanup.py — the deterministic Whisper-hallucination
post-processor (FEATURE A)."""

from src.stt_cleanup import (
    clean_segments,
    clean_text,
    collapse_ngram_loops,
    format_timestamp,
    is_hallucination,
    normalize,
)


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_normalize_strips_punctuation_and_case():
    assert normalize("¡Gracias!") == "gracias"
    assert normalize("  Thanks   for watching.  ") == "thanks for watching"


def test_hallucination_table_matches_whole_segment_only_en():
    assert is_hallucination("Thanks for watching!")
    assert is_hallucination("Subtitles by Someone")
    # Substring inside real content must NOT trigger.
    assert not is_hallucination("Let's give thanks for watching the numbers rise this quarter")


def test_hallucination_table_matches_whole_segment_only_es():
    assert is_hallucination("Gracias por ver el vídeo")
    assert is_hallucination("¡Suscríbete!")
    assert is_hallucination("¡Gracias!")
    assert not is_hallucination("Gracias por la propuesta, la revisamos mañana")


def test_music_and_silence_markers():
    assert is_hallucination("[Music]")
    assert is_hallucination("(música)")
    assert is_hallucination("♪♪♪")
    assert not is_hallucination("La música del proyecto está lista")


def test_collapse_ngram_loop_single_word():
    text, n = collapse_ngram_loops("the the the the the plan is ready")
    assert text == "the plan is ready"
    assert n == 4


def test_collapse_ngram_loop_short_phrase():
    text, n = collapse_ngram_loops("vamos a vamos a vamos a vamos a hacerlo")
    assert text == "vamos a hacerlo"
    assert n == 3


def test_collapse_ngram_loop_leaves_short_emphasis_alone():
    # Below MIN_REPEATS (4): plausible spoken emphasis, not a hallucination loop.
    text, n = collapse_ngram_loops("no no no vamos a esperar")
    assert text == "no no no vamos a esperar"
    assert n == 0


def test_clean_segments_collapses_repeated_segments_and_keeps_timestamps_coherent():
    segments = [
        _seg(0.0, 2.0, "Vamos a revisar el presupuesto."),
        _seg(2.0, 4.0, "Vamos a revisar el presupuesto."),
        _seg(4.0, 6.1, "vamos a revisar el presupuesto"),
        _seg(6.1, 9.0, "El siguiente punto es el calendario."),
    ]
    cleaned, stats = clean_segments(segments)
    assert [s["text"] for s in cleaned] == [
        "Vamos a revisar el presupuesto.",
        "El siguiente punto es el calendario.",
    ]
    # The collapsed run keeps the first segment's start and the run's last end.
    assert cleaned[0]["start"] == 0.0
    assert cleaned[0]["end"] == 6.1
    assert cleaned[1]["start"] == 6.1
    assert stats["removed_duplicate"] == 2
    assert stats["segments_in"] == 4
    assert stats["segments_out"] == 2


def test_clean_segments_drops_hallucination_only_segments():
    segments = [
        _seg(0.0, 1.0, "[Music]"),
        _seg(1.0, 3.5, "Buenos días a todos, empecemos."),
        _seg(3.5, 4.0, "Gracias por ver el vídeo"),
    ]
    cleaned, stats = clean_segments(segments)
    assert [s["text"] for s in cleaned] == ["Buenos días a todos, empecemos."]
    assert stats["removed_hallucination"] == 2


def test_clean_segments_silence_only_transcript_yields_empty_output():
    segments = [_seg(0.0, 1.0, "[Music]"), _seg(1.0, 2.0, "you")]
    cleaned, stats = clean_segments(segments)
    assert cleaned == []
    assert stats["segments_out"] == 0
    assert stats["removed_hallucination"] == 2


def test_clean_segments_strips_trailing_hotkey():
    segments = [
        _seg(0.0, 3.0, "Y con eso cerramos la reunión."),
        _seg(3.0, 4.0, "faustus detente"),
    ]
    cleaned, stats = clean_segments(segments, hotkey_phrases=["faustus detente"])
    assert [s["text"] for s in cleaned] == ["Y con eso cerramos la reunión."]
    assert stats["hotkey_stripped"] == 1


def test_clean_segments_strips_trailing_hotkey_partial_segment():
    segments = [_seg(0.0, 3.0, "Cerramos la reunión aquí faustus detente")]
    cleaned, stats = clean_segments(segments, hotkey_phrases=["faustus detente"])
    assert cleaned[0]["text"] == "Cerramos la reunión aquí"
    assert stats["hotkey_stripped"] == 1


def test_clean_text_flat_string_provider():
    text, stats = clean_text("Thanks for watching")
    assert text == ""
    assert stats["removed_hallucination"] == 1

    text2, stats2 = clean_text("the the the the the report is done")
    assert text2 == "the report is done"
    assert stats2["ngram_loops_collapsed"] == 4


def test_format_timestamp():
    assert format_timestamp(None) == "--:--"
    assert format_timestamp(65) == "01:05"
    assert format_timestamp(3725) == "1:02:05"
