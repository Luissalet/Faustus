"""A command that exits 0 without doing the job is the failure to catch.

The oracle checks a step's output against a string declared BEFORE the step
ran. These pin the three things that make it evidence rather than decoration:
an expectation declared and missing turns a success into a failure, a failure
is never turned into anything else, and "nothing was declared" stays visibly
distinct from "it passed".
"""
import pytest

from src import output_oracle as oracle


def test_declared_string_present_leaves_the_exit_code_alone():
    code, matched = oracle.apply(0, "collected 47 items\n47 passed in 3.2s", "47 passed")
    assert (code, matched) == (0, True)


def test_exit_zero_without_the_declared_string_becomes_a_failure():
    """The silent failure: pytest collected nothing and still exited 0."""
    code, matched = oracle.apply(0, "no tests ran in 0.01s", "47 passed")
    assert code == oracle.EXIT_OUTPUT_MISMATCH
    assert matched is False


def test_a_real_failure_keeps_its_own_exit_code():
    """The oracle may only unmask a false success, never relabel a failure."""
    for failing in (1, 2, 124, 137, -9):
        code, matched = oracle.apply(failing, "boom", "47 passed")
        assert code == failing
        assert matched is False


def test_a_failing_step_is_not_rescued_by_a_matching_string():
    code, matched = oracle.apply(1, "47 passed, then the fixture teardown crashed", "47 passed")
    assert (code, matched) == (1, True)


def test_nothing_declared_is_unchecked_not_passed():
    for empty in (None, "", "   "):
        code, matched = oracle.apply(0, "whatever it printed", empty)
        assert code == 0
        assert matched is None, "no expectation must not read as a pass"

    result = oracle.check("whatever it printed", None)
    assert result.matched is None
    assert "not checked" in oracle.describe(result)


def test_match_is_substring_not_pattern():
    """`.*` as an expectation would pass everything."""
    assert oracle.check("no tests ran", ".*").matched is False
    assert oracle.check("a.*b", ".*").matched is True   # only a literal one matches


def test_a_near_miss_is_still_a_miss_but_says_which_kind():
    wrong_case = oracle.check("47 PASSED in 3.2s", "47 passed")
    assert wrong_case.matched is False
    assert "different case" in wrong_case.why

    wrong_space = oracle.check("47  passed  in 3.2s", "47 passed in")
    assert wrong_space.matched is False
    assert "whitespace" in wrong_space.why

    assert oracle.apply(0, "47 PASSED", "47 passed")[0] == oracle.EXIT_OUTPUT_MISMATCH


def test_the_verdict_carries_what_was_declared():
    """A weak declaration proves little; the record has to show which it was."""
    result = oracle.check("something", "e")
    assert result.matched is True
    assert result.expected == "e"
    assert "'e'" in oracle.describe(result)


def test_non_string_output_and_exit_code_do_not_raise():
    assert oracle.apply(0, None, "x")[0] == oracle.EXIT_OUTPUT_MISMATCH
    assert oracle.apply("not a number", "x", "x") == (1, True)
    assert oracle.check(12345, "234").matched is True


@pytest.mark.parametrize(
    "value,expected_reason",
    [
        (None, None),
        ("47 passed", None),
        (17, "must be a string"),
        ("   ", "must not be blank"),
        ("x" * (oracle.MAX_EXPECTED_CHARS + 1), "at most"),
    ],
)
def test_declarations_are_validated_where_they_are_declared(value, expected_reason):
    reason = oracle.validate_expectation(value)
    if expected_reason is None:
        assert reason is None
    else:
        assert reason and expected_reason in reason
