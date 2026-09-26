"""A risk that could not be computed never earns a doubt review, whatever
the tier floor (seen in a probe: a folder outside the allowed roots came
back as an error and, with a low floor, triggered a review)."""

from src import doubt_review as dr

DIFF = "--- a/x\n+++ b/x\n" + "".join(f"+value_{i} = compute({i})\n" for i in range(12))


def test_an_error_summary_is_not_a_reason_to_review():
    ok, summary = dr.should_review("src/app.py", "/ws", DIFF, min_tier="low", max_per_turn=3,
                                   risk_fn=lambda *a, **k: {"error": "outside the allowed roots", "exit_code": 1})
    assert ok is False and summary.get("error")


def test_a_real_low_tier_still_reviews_when_the_floor_is_low():
    ok, _ = dr.should_review("src/app.py", "/ws", DIFF, min_tier="low", max_per_turn=3,
                             risk_fn=lambda *a, **k: {"level": "low", "score": 10})
    assert ok is True
