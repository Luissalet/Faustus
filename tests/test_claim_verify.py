"""Layered claim verification: one test per rung with a claim only that rung
can settle, the fabricated figure layer 4 exists to catch, and the model
judgement that is labelled and never merged into the deterministic score
(src/claim_verify.py)."""

import pytest

from src import claim_verify as cv


SOURCE = (
    "Acme Corp's board approved a dividend for shareholders in 2025. "
    "Revenue was 1,240 million euros, up from 980 million the year before. "
    "The migration ran on Tuesday and the index rebuild finished at noon."
)


# ---------------------------------------------------------------------------
# One rung at a time
# ---------------------------------------------------------------------------


def test_layer_1_settles_a_claim_quoted_verbatim():
    result = cv.verify("Revenue was 1,240 million euros", SOURCE)
    assert result["layer"] == 1 and result["supported"] is True
    assert result["confidence"] == 1.0
    assert result["unsupported_terms"] == []
    assert result["label"] == cv.LABEL_DETERMINISTIC
    assert result["model_judgement"] is False and result["judgement"] is None


def test_layer_2_settles_what_only_case_accents_and_punctuation_separated():
    source = "El café estaba frío por la mañana."
    result = cv.verify("el cafe, estaba frio", source)
    assert result["layer"] == 2 and result["supported"] is True
    assert result["confidence"] == 0.9
    # Layer 1 could not: the bytes differ.
    assert "el cafe, estaba frio" not in source


def test_layer_3_settles_a_reordered_sentence_no_substring_can_reach():
    claim = "The index rebuild finished after the migration"
    result = cv.verify(claim, SOURCE)
    assert result["layer"] == 3 and result["supported"] is True
    assert 0.45 < result["confidence"] < 0.9
    assert "content words" in result["why"]
    assert cv.normalise(claim) not in cv.normalise(SOURCE)


def test_layer_4_catches_a_fabricated_figure_and_names_it():
    """The layer this ladder exists for."""
    result = cv.verify("Revenue was 3,500 million euros", SOURCE)

    assert result["layer"] == 4 and result["supported"] is False
    assert result["unsupported_terms"] == ["3,500"]
    assert "3,500" in result["why"]
    assert result["confidence"] == 0.8
    assert result["label"] == cv.LABEL_DETERMINISTIC


def test_layer_4_is_not_overruled_by_a_high_word_overlap():
    """A single invented number among a dozen borrowed words is exactly how a
    fabricated figure gets past a bag-of-words check. It must not get past
    this one."""
    claim = "Acme Corp's board approved a dividend and revenue was 7,777 million euros"
    result = cv.verify(claim, SOURCE)

    assert result["layer"] == 4 and result["supported"] is False
    assert result["unsupported_terms"] == ["7,777"]
    # ... and the same sentence without the invented figure sails through.
    honest = claim.replace(" and revenue was 7,777 million euros", "")
    assert cv.verify(honest, SOURCE)["supported"] is True


def test_layer_4_catches_a_fabricated_name():
    result = cv.verify("Globex approved the dividend", SOURCE)
    assert result["layer"] == 4 and result["supported"] is False
    assert result["unsupported_terms"] == ["globex"]
    assert "name" in result["why"]


def test_layer_4_reads_the_same_number_written_differently():
    """Layer 4 only ever refutes, so a spelling it failed to recognise would be
    a false accusation — the expensive error here."""
    for spelling in ("1240", "1,240", "1.240", "1240.0"):
        result = cv.verify(f"Revenue reached {spelling} million euros", SOURCE)
        assert result["layer"] != 4, spelling


def test_layer_4_passing_is_not_by_itself_support():
    """Everything it checked being present is not evidence the sentence is
    true — that is what layer 5 is for."""
    result = cv.verify("shareholders will be paid out of future profits", SOURCE)
    assert result["supported"] is False
    assert result["layer"] is None
    assert "no judge was supplied" in result["why"]


# ---------------------------------------------------------------------------
# Layer 5 — the only rung that talks to a model
# ---------------------------------------------------------------------------


PARAPHRASE = "Shareholders will be paid out of profits"


def _judge(supported=True, why="the dividend is a payment to shareholders",
           confidence=0.82):
    def judge(claim, source):
        judge.calls += 1
        return {"supported": supported, "why": why, "confidence": confidence}
    judge.calls = 0
    return judge


def test_layer_5_settles_a_paraphrase_no_deterministic_layer_could():
    judge = _judge()
    result = cv.verify(PARAPHRASE, SOURCE, judge=judge)

    assert judge.calls == 1
    assert result["layer"] == 5 and result["supported"] is True
    assert result["judgement"]["source"] == "model"
    assert result["judgement"]["why"].startswith("the dividend")
    assert result["judgement"]["question"] == cv.JUDGE_QUESTION


def test_a_model_verdict_is_labelled_and_kept_out_of_the_deterministic_score():
    result = cv.verify(PARAPHRASE, SOURCE, judge=_judge(confidence=0.99))

    assert result["label"] == cv.LABEL_MODEL
    assert result["model_judgement"] is True
    # The deterministic confidence stays 0: nothing deterministic settled it.
    assert result["confidence"] == 0.0
    assert result["deterministic"] == {"supported": False, "layer": None,
                                       "confidence": 0.0}
    # The model's own number lives in its own record and nowhere else.
    assert result["judgement"]["confidence"] == 0.99
    assert "model judgement" in result["why"]


def test_without_a_judge_no_result_ever_claims_layer_5():
    for claim in (PARAPHRASE, "Revenue was 3,500 million euros",
                  "Revenue was 1,240 million euros", "", "nonsense words entirely"):
        assert cv.verify(claim, SOURCE)["layer"] != 5
        assert cv.verify(claim, SOURCE)["judgement"] is None
        assert cv.verify(claim, SOURCE)["label"] == cv.LABEL_DETERMINISTIC


def test_a_deterministic_answer_never_pays_for_a_model_call():
    judge = _judge()
    assert cv.verify("Revenue was 1,240 million euros", SOURCE, judge=judge)["layer"] == 1
    assert cv.verify("Revenue was 3,500 million euros", SOURCE, judge=judge)["layer"] == 4
    assert judge.calls == 0


def test_a_judge_that_refuses_the_claim_is_reported_as_such():
    result = cv.verify(PARAPHRASE, SOURCE,
                       judge=_judge(supported=False, why="the source says no such thing"))
    assert result["layer"] == 5 and result["supported"] is False
    assert result["label"] == cv.LABEL_MODEL


@pytest.mark.parametrize("raw,expected", [
    (True, True),
    (False, False),
    ("yes, it follows from the dividend", True),
    ("No — the source does not say that", False),
    ({"verdict": "yes"}, True),
    ({"supported": "no", "why": "nope"}, False),
])
def test_the_judge_may_answer_in_the_shapes_a_model_actually_uses(raw, expected):
    result = cv.verify(PARAPHRASE, SOURCE, judge=lambda c, s: raw)
    assert result["layer"] == 5 and result["supported"] is expected


@pytest.mark.parametrize("raw", [None, "", {}, 12, [1, 2]])
def test_a_judge_that_says_nothing_usable_leaves_the_claim_unsettled(raw):
    result = cv.verify(PARAPHRASE, SOURCE, judge=lambda c, s: raw)
    assert result["layer"] is None and result["supported"] is False
    assert result["judgement"] is None


def test_a_judge_that_raises_is_reported_not_propagated():
    def judge(claim, source):
        raise RuntimeError("the model is down")

    result = cv.verify(PARAPHRASE, SOURCE, judge=judge)
    assert result["layer"] is None and result["supported"] is False
    assert "RuntimeError" in result["judge_error"]


def test_a_keyword_only_judge_is_called_correctly_too():
    def judge(*, claim, source):
        return {"supported": True}

    assert cv.verify(PARAPHRASE, SOURCE, judge=judge)["layer"] == 5


def test_the_judge_prompt_is_the_one_question_every_caller_asks():
    prompt = cv.judge_prompt("a claim", SOURCE, source_chars=20)
    assert prompt.startswith(cv.JUDGE_QUESTION)
    assert "CLAIM:\na claim" in prompt
    assert SOURCE[:20] in prompt and SOURCE[:21] not in prompt


# ---------------------------------------------------------------------------
# Nothing here may raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claim,source", [
    (None, None),
    ("", ""),
    ("a claim", ""),
    ("", SOURCE),
    (b"bytes claim", SOURCE),
    (12345, SOURCE),
    (object(), object()),
    ("\x00\x01\x02", SOURCE),
    ("x" * 50_000, "y" * 50_000),
    (["a", "list"], {"a": "dict"}),
])
def test_verify_never_raises_on_junk(claim, source):
    result = cv.verify(claim, source)
    assert set(result) >= {"supported", "layer", "confidence", "why",
                           "unsupported_terms", "label", "deterministic"}
    assert isinstance(result["supported"], bool)
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["layer"] in (1, 2, 3, 4, 5, None)


def test_an_empty_source_supports_nothing_and_says_why():
    result = cv.verify("Revenue was 1,240 million euros", "")
    assert result["supported"] is False and result["layer"] is None
    assert "source is empty" in result["why"]
    assert result["unsupported_terms"]


def test_a_claim_with_no_content_words_is_not_matched_by_accident():
    # A one-character "claim" is a substring of almost anything; it is not a
    # claim, and layer 1 must not be allowed to bless it.
    assert cv.verify("a", SOURCE)["supported"] is False
    assert cv.verify("...", SOURCE)["layer"] is None


def test_verify_claims_keeps_each_claim_beside_its_verdict():
    rows = cv.verify_claims(
        ["Revenue was 1,240 million euros", "Revenue was 3,500 million euros"],
        SOURCE)
    assert [r["layer"] for r in rows] == [1, 4]
    assert rows[1]["claim"].endswith("3,500 million euros")
    assert cv.verify_claims("a single string", SOURCE)[0]["claim"] == "a single string"
