"""Grounding ledger: labels, exclusions, the retry-then-mark decision and the setting."""
import pytest

from src import answer_checks, grounding_ledger as gl


def labels(answer, question="", outputs=("x",)):
    led = gl.build_ledger(answer, question, list(outputs))
    return {e["text"]: e["label"] for e in led["entries"]}


# ── extraction ─────────────────────────────────────────────────────────────

def test_extracts_money_percent_units_and_decimals():
    figs = gl.extract_figures("Total 1.234,56 € and 12,5 % growth, 40 kg, 3.75 average.")
    assert [f["text"] for f in figs] == ["1.234,56 €", "12,5 %", "40 kg", "3.75"]
    assert [f["kind"] for f in figs] == ["money", "percent", "unit", "number"]


def test_small_bare_integers_and_years_are_not_figures():
    assert gl.extract_figures("We did 3 steps in 2026 and 12 items.") == []


def test_dates_times_versions_urls_code_and_list_markers_are_skipped():
    text = ("1. First\n2) Second\nOn 25 de diciembre de 2026 at 17:30, see https://x.test/a/12345 "
            "version 2.10.4 and `price = 4999` ```\n88888\n``` or 2026-09-25 and 25/12/2026.")
    assert gl.extract_figures(text) == []


def test_thousands_with_dots_are_not_taken_for_a_version():
    figs = gl.extract_figures("Sold 1.234.567 units.")
    assert len(figs) == 1 and figs[0]["readings"][0][0] == 1234567


def test_identifiers_are_not_quantities():
    assert gl.extract_figures("Order 1234567890123 shipped.") == []


def test_range_second_number_is_kept():
    figs = gl.extract_figures("Between 20-50 % of cases.")
    assert [f["text"] for f in figs] == ["50 %"] or [f["text"] for f in figs][-1] == "50 %"


def test_scale_suffixes():
    fig = gl.extract_figures("Revenue of 1.2M and 3 millones")[0]
    assert fig["readings"][0][0] == 1200000
    led = labels("Revenue of 1.2M", outputs=["revenue 1234567"])
    assert led["1.2M"] == "observed"


def test_struck_figures_are_left_alone():
    assert gl.extract_figures("was ~~9999 €~~ now") == []


# ── labels ─────────────────────────────────────────────────────────────────

def test_observed_with_locale_formats_and_rounding():
    lab = labels("Total 1.234,56 € ; also $1,235 and 12,5 %.", outputs=["total: 1234.56 eur\nrate 12.5"])
    assert lab["1.234,56 €"] == "observed"
    assert lab["$1,235"] == "observed"
    assert lab["12,5 %"] == "observed"


def test_rounding_tolerance_is_half_a_unit_of_the_last_digit():
    assert labels("It is 15 kg", outputs=["weight 14.6"])["15 kg"] == "observed"
    assert labels("It is 15 kg", outputs=["weight 14.2"])["15 kg"] == "unsupported"
    assert labels("It is 5.5 %", outputs=["value 5.52"])["5.5 %"] == "observed"


def test_ambiguous_separator_matches_either_reading():
    assert labels("Stock 1.234 units", outputs=["stock 1234"])["1.234"] == "observed"
    assert labels("Weight 1.234 kg", outputs=["weight 1.234"])["1.234 kg"] == "observed"


def test_cited_when_only_in_the_question():
    lab = labels("You budgeted 500 €.", question="My budget is 500 euros", outputs=["other 7"])
    assert lab["500 €"] == "cited"


def test_derived_sum_difference_ratio_and_percentage_change():
    out = ["jan: 100\nfeb: 150\nmar: 50"]
    lab = labels("Total 300 €, feb minus jan 50 kg, feb/jan 1.5, growth 50 %.", outputs=out)
    assert lab["300 €"] == "derived"
    led = gl.build_ledger("Growth from jan to feb was 50 %.", "", out)
    entry = led["entries"][0]
    assert entry["label"] in ("derived", "observed")


def test_derived_carries_its_formula():
    led = gl.build_ledger("Combined 257 EUR.", "", ["a 120\nb 137"])
    e = led["entries"][0]
    assert e["label"] == "derived" and "+" in e["via"]


def test_percent_from_a_fraction():
    led = gl.build_ledger("Rate is 12 %.", "", ["rate 0.12"])
    assert led["entries"][0]["label"] == "derived"


def test_count_of_listed_items():
    answer = "Found 15 files:\n" + "\n".join(f"- f{i}" for i in range(15))
    assert labels(answer, outputs=["irrelevant 7"])["15"] == "count"


def test_count_of_tool_lines():
    lines = "\n".join(f"row {i}" for i in range(20))
    assert labels("There are 20 rows in total.", outputs=[lines])["20"] == "count"


def test_unsupported_figure():
    lab = labels("The price is 4.999 € and 77 %.", outputs=["price 10"])
    assert lab["4.999 €"] == "unsupported" and lab["77 %"] == "unsupported"


def test_no_tool_evidence_skips_the_check():
    led = gl.build_ledger("The price is 4.999 €.", "q", [])
    assert led["status"] == "skipped" and led["reason"] == "no_tool_evidence" and led["unsupported"] == []
    assert gl.build_ledger("x 4.999 €", "q", ["   ", ""])["status"] == "skipped"


def test_tool_with_no_numbers_leaves_figures_unsupported():
    led = gl.build_ledger("It costs 99 €.", "q", ["no figures here"])
    assert led["status"] == "checked" and len(led["unsupported"]) == 1


def test_derivation_is_bounded():
    out = ["\n".join(f"v{i} {1000 + i * 7}" for i in range(500))]
    led = gl.build_ledger("Result 5 kg and 123456 €.", "", out)
    assert led["status"] == "checked" and len(led["entries"]) == 2
    many = " ".join(f"{100 + i} €" for i in range(200))
    assert len(gl.build_ledger(many, "", ["x 1"])["entries"]) <= gl.MAX_FIGURES


# ── marking and the note ───────────────────────────────────────────────────

def test_mark_unsupported_strikes_and_notes_in_both_languages():
    text = "El total es 99 € y 7.777 kg."
    led = gl.build_ledger(text, "", ["total 10"])
    marked = gl.mark_unsupported(text, led["unsupported"], "es")
    assert "~~99 €~~" in marked and "~~7.777 kg~~" in marked and "Nota:" in marked
    en = gl.mark_unsupported("The total is 99 EUR.", gl.build_ledger("The total is 99 EUR.", "", ["t 10"])["unsupported"])
    assert "~~99 EUR~~" in en and "Note:" in en


def test_marking_twice_does_not_nest():
    text = "Total 99 €."
    led = gl.build_ledger(text, "", ["t 10"])
    once = gl.mark_unsupported(text, led["unsupported"], "en")
    again = gl.build_ledger(once, "", ["t 10"])
    assert again["unsupported"] == []


def test_mark_with_nothing_unsupported_is_identity():
    assert gl.mark_unsupported("plain text", []) == "plain text"


# ── decision: retry first, then mark ───────────────────────────────────────

def test_review_disabled_does_nothing():
    r = gl.grounding_review("Total 99 €", "", ["t 10"], enabled=False)
    assert r["action"] == "none" and r["ledger"] is None and r["answer"] == "Total 99 €"


def test_review_asks_for_a_correction_first_then_marks():
    first = gl.grounding_review("Total 99 €", "", ["t 10"], enabled=True, retry_used=False)
    assert first["action"] == "retry" and "99 €" in first["note"] and first["answer"] == "Total 99 €"
    second = gl.grounding_review("Total 99 €", "", ["t 10"], enabled=True, retry_used=True, lang="en")
    assert second["action"] == "mark" and "~~99 €~~" in second["answer"]


def test_review_clean_answer_and_skipped_answer():
    assert gl.grounding_review("Total 10 €", "", ["t 10"], enabled=True)["action"] == "none"
    skipped = gl.grounding_review("Total 99 €", "", [], enabled=True)
    assert skipped["action"] == "none" and skipped["ledger"]["status"] == "skipped"


def test_trace_entry_is_compact_and_serialisable():
    import json
    r = gl.grounding_review("Total 99 € and 10 €", "", ["t 10"], enabled=True)
    tr = r["trace"]
    assert tr["kind"] == "grounding_ledger" and tr["action"] == "retry"
    assert tr["counts"]["unsupported"] == 1 and {f["label"] for f in tr["figures"]} == {"observed", "unsupported"}
    json.dumps(tr)


def test_review_never_raises(monkeypatch):
    monkeypatch.setattr(gl, "build_ledger", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = gl.grounding_review("x 99 €", "", ["t 1"], enabled=True)
    assert r["action"] == "none" and r["trace"]["status"] == "error"


# ── setting and the answer_checks bridge ───────────────────────────────────

def test_setting_defaults_off_and_is_in_the_schema():
    from src import agent_settings_schema as schema
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS[gl.SETTING_KEY] is False
    keys = [f["key"] for g in schema.GROUPS for f in g["fields"]]
    assert gl.SETTING_KEY in keys


def test_is_enabled_follows_the_setting(monkeypatch):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: True if k == gl.SETTING_KEY else d)
    assert gl.is_enabled() is True
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: d)
    assert gl.is_enabled() is False


def test_answer_checks_bridge_and_rewrite_note():
    r = answer_checks.grounding_review("Total 99 €", "", ["t 10"], enabled=True)
    assert r["action"] == "retry"
    note = answer_checks.rewrite_note([], [], None, None, grounding=r["note"])
    assert "Harness check" in note and "99 €" in note
    assert "do not appear" not in answer_checks.rewrite_note([], [], None, None)
    assert answer_checks.grounding_review("Total 99 €", "", ["t 10"], enabled=False)["action"] == "none"
