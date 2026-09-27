import asyncio
import json

import pytest

from services.local_video_translation import translate_segments


ROWS = [
    {"start": 0, "end": 2, "text": "Good morning."},
    {"start": 2.5, "end": 4, "text": "The total is 17."},
]


def test_translation_preserves_timing_and_original_rows():
    seen = []

    async def model(url, name, messages, **kwargs):
        seen.append((url, name, messages, kwargs))
        return json.dumps({"texts": ["Buenos días.", "El total es 17."]})

    original = [dict(row) for row in ROWS]
    result = asyncio.run(translate_segments(ROWS, "en", "es", route=("http://model/v1/chat/completions", "3.8", {}), call=model))
    assert ROWS == original
    assert [(r["start"], r["end"]) for r in result["segments"]] == [(0.0, 2.0), (2.5, 4.0)]
    assert [r["text"] for r in result["segments"]] == ["Buenos días.", "El total es 17."]
    assert result["model"] == "3.8" and seen[0][3]["response_schema"]["properties"]["texts"]["minItems"] == 2


def test_translation_rejects_wrong_count_without_changing_source():
    async def model(*args, **kwargs):
        return '{"texts":["solo uno"]}'

    original = [dict(row) for row in ROWS]
    with pytest.raises(ValueError, match="wrong number"):
        asyncio.run(translate_segments(ROWS, "en", "es", route=("url", "model", {}), call=model))
    assert ROWS == original


def test_translation_marks_text_too_long_for_timing():
    async def model(*args, **kwargs):
        return json.dumps({"texts": ["palabra " * 12]})

    result = asyncio.run(translate_segments([{"start": 0, "end": 1, "text": "Hello"}], "en", "es",
                                            route=("url", "model", {}), call=model))
    assert result["segments"][0]["long_for_timing"] is True


def test_translation_batches_long_clip_without_reordering():
    calls = []

    async def model(url, name, messages, **kwargs):
        indexes = [row["index"] for row in json.loads(messages[-1]["content"])["segments"]]
        calls.append(indexes)
        return json.dumps({"texts": [f"traducción {index}" for index in indexes]})

    rows = [{"start": i, "end": i + .5, "text": f"original {i}"} for i in range(13)]
    result = asyncio.run(translate_segments(rows, "en", "es", route=("url", "model", {}), call=model))
    assert calls == [list(range(1, 13)), [13]]
    assert result["segments"][12]["text"] == "traducción 13"
    assert result["segments"][12]["start"] == 12
