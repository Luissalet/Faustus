"""VER-05 — verify_citation: supported | weak | unsupported, deterministic
(no model) by default."""
from src import verification as ver


def test_supported_when_the_sentence_matches_the_source():
    source = "Revenue grew 8% year over year, driven mainly by the European market."
    claim = "Revenue grew 8% year over year."
    assert ver.verify_citation(claim, source) == "supported"


def test_unsupported_when_the_source_does_not_contain_the_cited_figure():
    # Same fixture as tests/qa/test_qa_21_citas_vacias.py: an accessible
    # source that simply does not back the claim.
    source = ("The 2025 annual report shows revenue grew 8% year over year, "
              "driven mainly by the European market.")
    claim = "Revenue grew 47% year over year according to the 2025 annual report."
    assert ver.verify_citation(claim, source) == "unsupported"


def test_weak_when_nothing_deterministic_settles_it_either_way():
    source = "The team shipped a redesign of the onboarding flow this quarter."
    claim = "The onboarding flow redesign improved signup conversion."
    assert ver.verify_citation(claim, source) == "weak"


def test_empty_source_is_never_supported():
    assert ver.verify_citation("Revenue grew 8%.", "") in ("weak", "unsupported")
    assert ver.verify_citation("Revenue grew 8%.", "") != "supported"
