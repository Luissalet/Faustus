"""A test run that exits 0 without doing the job must not pass.

`src/output_oracle.py` decides that; these tests are about `run_tests` actually
consulting it, and about the verdict it records staying self-consistent — a
forced exit code beside an `ok` that still says True would be worse than no
check at all.
"""
import pytest

from src import project_tests
from src.output_oracle import EXIT_OUTPUT_MISMATCH


class FakeProc:
    def __init__(self, stdout, returncode):
        self._stdout = stdout
        self.returncode = returncode
        self.pid = 4242

    def communicate(self, timeout=None):
        return self._stdout, ""


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Run `run_tests` against a suite whose output and exit code we choose."""

    def go(stdout, returncode, expected=None):
        monkeypatch.setattr(
            project_tests.subprocess, "Popen",
            lambda argv, **kw: FakeProc(stdout, returncode),
        )
        spec = {"kind": "pytest", "argv": ["python", "-m", "pytest"]}
        if expected is not None:
            spec["expected_output_contains"] = expected
        return project_tests.run_tests(str(tmp_path), spec)

    return go


def test_a_declared_string_that_is_present_leaves_the_run_alone(run):
    res = run("collected 47 items\n47 passed in 3.2s", 0, expected="47 passed")
    assert res["exit_code"] == 0
    assert res["output_matched"] is True
    assert res["ok"] is True


def test_exit_zero_without_the_declared_string_becomes_a_failure(run):
    # The classic silent pass: pytest collected nothing and said so calmly.
    res = run("no tests ran in 0.01s", 0, expected="47 passed")
    assert res["exit_code"] == EXIT_OUTPUT_MISMATCH
    assert res["output_matched"] is False
    # `ok` came from parse_output, which only saw exit 0. It must not now
    # disagree with the code the oracle forced.
    assert res["ok"] is False


def test_declaring_nothing_leaves_the_run_untouched_and_unchecked(run):
    res = run("collected 47 items\n47 passed in 3.2s", 0)
    assert res["exit_code"] == 0
    assert res["ok"] is True
    # None, never True: nothing was declared, so nothing was checked, and
    # collapsing that to True would read "we never looked" as "it passed".
    assert res["output_matched"] is None


def test_a_blank_declaration_checks_nothing(run):
    res = run("47 passed in 3.2s", 0, expected="   ")
    assert res["exit_code"] == 0
    assert res["output_matched"] is None


def test_a_genuine_failure_is_never_rewritten(run):
    """The oracle unmasks false successes; it does not relabel honest failures."""
    res = run("FAILED tests/test_a.py::test_one - AssertionError\n1 failed in 0.4s", 1,
              expected="47 passed")
    assert res["exit_code"] == 1, "a failing run must keep the code it earned"
    assert res["output_matched"] is False
    assert res["ok"] is False


def test_a_genuine_failure_keeps_its_code_even_when_it_did_print_the_string(run):
    res = run("47 passed in 3.2s\nFATAL: teardown crashed", 2, expected="47 passed")
    assert res["exit_code"] == 2
    assert res["output_matched"] is True


def test_a_timed_out_run_is_reported_as_unchecked_not_as_a_mismatch(run, tmp_path, monkeypatch):
    """A timeout already failed on its own terms, and its output is a fragment.

    Running the oracle over that fragment would rewrite a timeout as an output
    mismatch and hide why the suite was killed.
    """
    import subprocess as _subprocess

    class _Hanging(FakeProc):
        def __init__(self):
            super().__init__("", None)
            self._first = True

        def communicate(self, timeout=None):
            if self._first:
                self._first = False
                raise _subprocess.TimeoutExpired("pytest", timeout or 1)
            return "", ""

    monkeypatch.setattr(project_tests, "_kill_tree", lambda proc: None)
    monkeypatch.setattr(project_tests.subprocess, "Popen", lambda argv, **kw: _Hanging())
    res = project_tests.run_tests(
        str(tmp_path),
        {"kind": "pytest", "argv": ["python", "-m", "pytest"],
         "expected_output_contains": "47 passed"},
    )
    assert res["timed_out"] is True
    assert res["output_matched"] is None
    assert res["exit_code"] is None


def test_the_verdict_travels_to_the_ui_beside_the_exit_code(run):
    """A forced 65 with no `output_matched` next to it is unexplainable."""
    res = run("no tests ran in 0.01s", 0, expected="47 passed")
    compact = project_tests.compact(res)
    assert compact["exit_code"] == EXIT_OUTPUT_MISMATCH
    assert compact["output_matched"] is False
