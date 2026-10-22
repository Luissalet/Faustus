"""Precision of the harness's own linking on a long book (`CriteriaBook.auto_link`):
one call proves at most three criteria, only terms that tell a criterion from
the others count, shell loops that only search files are not runs, a run that
prints its own failure proves nothing, and a regression built from the real
outputs of a 133-criteria task (tests/fixtures/contract_auto_link_trace.json:
the criteria involved, their term frequencies in the whole book, and three
calls - a README check that ran, a README `for ... printf` grep loop, and a
wrapper that exited 0 around `rc=1 FAIL ... No module named`)."""
import json
import pathlib

import pytest

from src import contract_criteria as cc
from src import verification as ver

FIX = json.loads((pathlib.Path(__file__).parent / "fixtures" / "contract_auto_link_trace.json")
                 .read_text(encoding="utf-8"))


def _crit(cid, quote, **kw):
    base = {"id": cid, "quote": quote, "state": "pending", "kind": "behaviour", "subject": None,
            "keys": [], "words": [], "matchers": [], "limits": [], "exits": [], "statuses": []}
    base.update(kw)
    return base


def _call(output, command="python -m arc scan good.arc", exit_code=0, call_id="c1", tool="bash"):
    return {"call_id": call_id, "tool": tool, "seq": 1, "command": command, "output": output,
            "exit_code": exit_code, "mutation": False}


def _auto(crit, call, df=None, terms=None):
    return ver.verify_contract_link(crit, call, strict=True, auto=True, df=df, terms_out=terms).state


# -- 1. one call proves at most three criteria, ranked by distinct terms --------

def _cap_book():
    rows = [
        _crit("C01", "The scan reports alpha1 and beta1 for a good archive.", keys=["alpha1", "beta1"]),
        _crit("C02", "The scan reports alpha2 and beta2 for a good archive.", keys=["alpha2", "beta2"]),
        _crit("C03", "The scan reports alpha3, beta3 and gamma3 for a good archive.",
              keys=["alpha3", "beta3", "gamma3"]),
        _crit("C04", "The scan reports alpha4 and beta4 for a good archive.", keys=["alpha4", "beta4"]),
        _crit("C05", "The scan reports alpha5, beta5 and gamma5 for a good archive.",
              keys=["alpha5", "beta5", "gamma5"]),
    ]
    return cc.CriteriaBook(rows)


def _doc(*names):
    """A JSON line that shows the given keys (a key counts when it shows quoted)."""
    return "{" + ", ".join('"%s": 1' % n for n in names) + "}"


def test_one_call_proves_at_most_three_criteria_ranked_by_terms_then_id():
    book = _cap_book()
    book.record_call("c1", "bash", "python -m arc scan good.arc",
                     {"exit_code": 0, "output": _doc("alpha1", "beta1", "alpha2", "beta2", "alpha3", "beta3", "gamma3",
                                                    "alpha4", "beta4", "alpha5", "beta5", "gamma5")}, kind="shell")
    proven = book.auto_link()
    # C03 and C05 show three terms each, then the tie between C01/C02/C04 goes by id
    assert sorted(proven) == ["C01", "C03", "C05"]
    assert [c["id"] for c in book.criteria if c["state"] == "pending"] == ["C02", "C04"]


def test_the_cap_counts_what_the_call_already_proved_in_an_earlier_pass():
    book = _cap_book()
    book.record_call("c1", "bash", "python -m arc scan good.arc",
                     {"exit_code": 0, "output": _doc("alpha1", "beta1", "alpha2", "beta2", "alpha3", "beta3", "gamma3",
                                                    "alpha4", "beta4", "alpha5", "beta5", "gamma5")}, kind="shell")
    first = book.auto_link()
    assert len(first) == 3
    assert book.auto_link() == []
    # a different call has its own three
    book.record_call("c2", "bash", "python -m arc scan other.arc", {"exit_code": 0, "output": _doc("alpha2", "beta2", "alpha4", "beta4")},
                     kind="shell")
    assert book.auto_link() == ["C02", "C04"]


# -- 2. distinctive terms only ---------------------------------------------------

@pytest.mark.parametrize("key", ["1", "Z", "\\n", "0.5", "by", "report", "start"])
def test_a_generic_key_never_proves_alone(key):
    crit = _crit("C1", "Something is labelled in the report output.", keys=[key])
    assert _auto(crit, _call(f"x {key} report start 1 Z 0.5 by \\n y"), df={}) == "pending"


def test_a_key_in_many_criteria_needs_a_second_term():
    crit = _crit("C1", "The frobnicate step logs a wibble line.", keys=["frobnicate", "wibble"])
    both, one = _doc("frobnicate", "wibble"), _doc("frobnicate")
    rare = {"frobnicate": 1, "wibble": 1}
    assert _auto(crit, _call(both), df=rare) == "verified"
    assert _auto(crit, _call(one), df=rare) == "pending"          # one of two keys is not the sentence
    assert _auto(crit, _call("frobnicate wibble done"), df=rare) == "pending"   # bare words are not output
    shared = {"frobnicate": 3, "wibble": 3}
    assert _auto(crit, _call(both), df=shared) == "verified"
    common = {"frobnicate": 9, "wibble": 9}
    assert _auto(crit, _call(both), df=common) == "pending"


def test_a_word_in_many_criteria_does_not_count():
    crit = _crit("C1", "Rounding happens on the exact group_sums.", words=["rounding", "exact", "group_sums"])
    out = "rounding exact group_sums"
    assert _auto(crit, _call(out), df={"rounding": 1, "exact": 1, "group_sums": 1}) == "verified"
    assert _auto(crit, _call(out), df={"rounding": 1, "exact": 6, "group_sums": 7}) == "pending"


def test_three_distinct_words_are_needed_not_two():
    crit = _crit("C1", "Rounding happens on the exact group_sums.", words=["rounding", "exact", "group_sums"])
    assert _auto(crit, _call("rounding exact"), df={}) == "pending"
    assert _auto(crit, _call("rounding exact group_sums"), df={}) == "verified"


@pytest.mark.parametrize("quote", ["meters", "`meters`", "Z", "'1'"])
def test_a_criterion_that_is_a_bare_token_is_never_proven(quote):
    crit = _crit("C1", quote, keys=[quote.strip("`'")])
    assert _auto(crit, _call(f"output with {quote.strip(chr(96) + chr(39))} in it"), df={}) == "pending"


def test_a_sentence_with_many_terms_needs_about_two_thirds_of_them_shown_together():
    keys = ["pk1", "pk2", "pk3", "pk4", "pk5"]
    crit = _crit("C1", "Five things: pk1, pk2, pk3, pk4 and pk5 are there.", keys=keys)
    assert _auto(crit, _call(_doc("pk1")), df={}) == "pending"
    assert _auto(crit, _call(_doc("pk1", "pk2", "pk3")), df={}) == "pending"
    assert _auto(crit, _call(_doc("pk1", "pk2", "pk3", "pk4")), df={}) == "verified"
    # the same keys scattered over unrelated lines of a long output are not one record
    scattered = "\n".join([_doc("pk1"), "x", "x", "x", "x", _doc("pk2"), "x", "x", "x", "x", _doc("pk3", "pk4")])
    assert _auto(crit, _call(scattered), df={}) == "pending"


def test_a_limit_needs_a_unit_that_touches_its_number_for_one_letter_units():
    limit = {"num": "300", "unit": "B"}
    assert ver._limit_shown("rc=1 b'{}'", limit) is True      # the model's own link keeps the loose match
    assert ver._limit_shown("rc=1 b'{}'", limit, tight=True) is False
    assert ver._limit_shown("peak 29B", limit, tight=True) is True
    assert ver._limit_shown("peak 29 MB of 300 MB", {"num": "300", "unit": "MB"}, tight=True) is True


def test_formats_and_limits_count_once_each_and_a_widespread_key_needs_one_of_them():
    # two date-format matchers are one kind of evidence: no proof by timestamps alone
    crit = _crit("C1", "The instant must be after 2000-01-01T00:00:00Z and before 2100-01-01T00:00:00Z.",
                 matchers=[{"on": "output", "src": "2000-01-01T00:00:00Z", "kind": "instant"},
                           {"on": "output", "src": "2100-01-01T00:00:00Z", "kind": "instant"}])
    assert _auto(crit, _call("seen at 2026-01-01T00:00:00Z"), df={}) == "pending"


# -- 3. shell compound keywords are transparent ------------------------------------

@pytest.mark.parametrize("command,expected", [
    ('for o in --by --tz; do printf "%s %s\\n" "$o" "$(grep -c -- "$o" README.md)"; done', False),
    ("for f in a.md b.md; do grep -n '^#' $f; done", False),
    ("if grep -q serve README.md; then echo yes; fi", False),
    ("while read l; do echo $l; done < list.txt", False),
    ("cd w && grep -n x f && for w in a b; do printf '%s\\n' $w; done", False),
    ("for x in 1 2; do python -m arc scan $x.arc; done", True),
    ("if python -m arc scan a.arc; then echo ok; fi", True),
    ("cd w && for x in 1 2; do ./run-tests.sh $x; done", True),
])
def test_shell_loops_are_judged_by_the_commands_inside(command, expected):
    assert cc.is_run_call("bash", command) is expected


# -- 4. a run that prints its own failure proves nothing ---------------------------

@pytest.mark.parametrize("output", [
    "ingest 2M   rc=1 FAIL  C:\\Python313\\python.EXE: No module named meter",
    "meters   -> FAIL (0.0s, 11 MB)",
    "ALL LIMITS: FAIL",
    "STDERR: C:\\Python313\\python.EXE: No module named meter",
    "ModuleNotFoundError: x",
    "bash: arc: command not found",
    "the checker finished with exit status 2",
    "tests done, exit code: 1",
])
def test_a_wrapper_that_exited_zero_but_printed_a_failure_proves_nothing(output):
    crit = _crit("C1", "The scan reports alpha1 and beta1 for a good archive.", keys=["alpha1", "beta1"])
    assert _auto(crit, _call(_doc("alpha1", "beta1") + " " + output, exit_code=0), df={}) == "pending"
    assert _auto(crit, _call(_doc("alpha1", "beta1")), df={}) == "verified"


@pytest.mark.parametrize("output", ["FAIL: 0", "failures: 0, rc=0", "0 FAIL", "exit status 0", "no failures"])
def test_a_clean_report_of_zero_failures_is_not_a_failure(output):
    crit = _crit("C1", "The scan reports alpha1 and beta1 for a good archive.", keys=["alpha1", "beta1"])
    assert _auto(crit, _call(_doc("alpha1", "beta1") + " " + output), df={}) == "verified"


def test_the_failure_rule_has_no_exemption_for_a_criterion_about_tests():
    crit = _crit("C1", "The tests report alpha1 and beta1 for archives.", keys=["alpha1", "beta1"])
    assert _auto(crit, _call(_doc("alpha1", "beta1") + " rc=1 FAIL No module named x"), df={}) == "pending"
    for summary in ("3 failed, 20 passed in 1.2s", "1 error in 0.3s", "ERROR tests/test_cli.py - ImportError: x",
                    "FAILED tests/test_cli.py::test_a - assert 1 == 2"):
        assert _auto(crit, _call(_doc("alpha1", "beta1") + "\n" + summary), df={}) == "pending", summary
    assert _auto(crit, _call(_doc("alpha1", "beta1") + "\n20 passed in 1.2s"), df={}) == "verified"


# -- 5. regression: the real outputs of a 133-criteria task ------------------------

def _trace_book():
    rows = [dict(c, state="pending", auto=False, evidence_refs=[]) for c in FIX["criteria"]]
    book = cc.CriteriaBook(rows)
    book._df_cache = (len(book.criteria), dict(FIX["df"]))   # the term frequencies of the whole 133-book
    return book


def _record(book, name):
    c = FIX["calls"][name]
    return book.record_call(c["call_id"], c["tool"], c["command"],
                            {"exit_code": c["exit_code"], "output": c["output"]},
                            kind="shell" if c["tool"] == "bash" else "shell")


def test_trace_the_failed_wrapper_proves_nothing_whatever_its_exit_code():
    book = _trace_book()
    call = _record(book, "failed_wrapper")
    assert "No module named meter" in call["output"] and "rc=1" in call["output"]
    assert book.auto_link() == []
    for crit in book.criteria:
        assert _auto(crit, call, df=FIX["df"]) == "pending", crit["id"]


def test_trace_the_readme_grep_loop_is_not_a_run():
    book = _trace_book()
    call = _record(book, "readme_grep_loop")
    assert "for o in" in call["command"] and "printf" in call["command"]
    assert cc.is_run_call("bash", call["command"]) is False
    assert book.auto_link() == []
    assert all(c["state"] == "pending" for c in book.criteria)


def test_trace_the_readme_check_proves_at_most_what_it_really_shows():
    book = _trace_book()
    _record(book, "readme_grep_loop")
    _record(book, "failed_wrapper")
    call = _record(book, "readme_check")
    assert call["exit_code"] == 0 and "ALL README CHECKS PASSED" in call["output"]
    proven = book.auto_link()
    # before: 42 criteria; now at most three, and only ones the output shows
    assert len(proven) <= cc.AUTO_CALL_CAP
    assert set(proven) <= {"C048", "C124", "C125"}
    # the criteria about time zones, buckets and limits are not shown by a README check
    for cid in ("C067", "C070", "C087", "C132", "C028", "C077", "C078", "C104"):
        assert book.get(cid)["state"] == "pending", cid


def test_trace_the_three_calls_together_never_prove_the_dst_criteria():
    book = _trace_book()
    for name in ("readme_grep_loop", "failed_wrapper", "readme_check"):
        _record(book, name)
        book.auto_link()
    assert sum(1 for c in book.criteria if c["state"] == "proven") <= cc.AUTO_CALL_CAP
    for cid in ("C067", "C070", "C087"):
        assert book.get(cid)["state"] == "pending"
