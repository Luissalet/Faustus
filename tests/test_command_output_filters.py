"""Tests for src/command_output_filters.py — deterministic compression of
the in-prompt copy of shell tool output, and its wiring point in
src/tool_execution.py::format_tool_result (called AFTER
src/tool_result_offload.py has already persisted the full result).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_store, command_output_filters as cof, tool_result_offload as offload
from src.tool_execution import format_tool_result


# ---------------------------------------------------------------------------
# classify() — command recognition, including the forms the task called out
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest -q", "pytest"),
        ("python -m pytest tests/", "pytest"),
        (r".\venv\Scripts\python.exe -m pytest tests", "pytest"),
        ("cd /repo && python -m pytest -q", "pytest"),
        ("FOO=bar BAZ=1 python -m pytest -q", "pytest"),
        ("python -m unittest discover", "unittest"),
        ("cd C:\\project && python -m unittest discover", "unittest"),
        ("node --test test/", "node_test"),
        ("npx vitest run", "node_test"),
        ("npx jest --ci", "node_test"),
        ("npm test", "node_test"),
        ("pnpm test", "node_test"),
        ("git diff --stat", "git_diff"),
        ("git show HEAD~1", "git_diff"),
        ("git status", "git_status"),
        ("git status --short", "git_status"),
        ("grep -rn TODO src/", "grep"),
        ("rg --json TODO", "grep"),
        ("findstr /s TODO *.py", "grep"),
        ("Get-ChildItem | Select-String -Pattern 'TODO'", "grep"),
        ("pip install requests", "install"),
        ("FOO=1 pip install requests", "install"),
        ("npm install left-pad", "install"),
        ("uv pip install requests", "install"),
        ("echo hello world", "generic"),
        ("curl -O http://example.invalid/file.tar.gz", "generic"),
    ],
)
def test_classify_recognizes_command_forms(command, expected):
    assert cof.classify(command) == expected


# ---------------------------------------------------------------------------
# pytest — a large passing run collapses; a failing run keeps tracebacks
# ---------------------------------------------------------------------------

def _pytest_pass_fixture(n: int = 200) -> str:
    lines = [
        "============================= test session starts ==============================",
        "platform linux -- Python 3.11.4, pytest-8.0.0, pluggy-1.4.0",
        "rootdir: /home/user/project",
        f"collected {n} items",
        "",
    ]
    for i in range(n):
        lines.append(f"tests/test_module.py::test_case_{i:03d} PASSED               [{i * 100 // n:3d}%]")
    lines += ["", f"============================== {n} passed in 4.21s ==============================", ""]
    return "\n".join(lines)


def test_pytest_passing_run_of_200_collapses_to_one_summary_line():
    output = _pytest_pass_fixture(200)
    text, meta = cof.compress("pytest -q", output, 0)
    assert meta["filter"] == "pytest"
    assert "200 passed" in text
    assert "test_case_150" not in text
    assert meta["kept_lines"] < meta["original_lines"]
    assert len(text) < len(output) * 0.2


def _pytest_fail_fixture() -> str:
    lines = [
        "============================= test session starts ==============================",
        "platform linux -- Python 3.11.4, pytest-8.0.0, pluggy-1.4.0",
        "rootdir: /home/user/project",
        "collected 200 items",
        "",
    ]
    for i in range(198):
        lines.append(f"tests/test_module.py::test_case_{i:03d} PASSED               [{i:3d}%]")
    lines += [
        "",
        "=================================== FAILURES ===================================",
        "________________________________ test_add_numbers ________________________________",
        "",
        "    def test_add_numbers():",
        "        result = add(2, 2)",
        ">       assert result == 5",
        "E       assert 4 == 5",
        "E        +  where 4 = add(2, 2)",
        "",
        "tests/test_module.py:42: AssertionError",
        "________________________________ test_divide_by_zero ________________________________",
        "",
        "    def test_divide_by_zero():",
        "        with pytest.raises(ZeroDivisionError):",
        ">           divide(1, 0)",
        "E           Failed: DID NOT RAISE <class 'ZeroDivisionError'>",
        "",
        "tests/test_module.py:58: Failed",
        "=============================== short test summary info ================================",
        "FAILED tests/test_module.py::test_add_numbers - assert 4 == 5",
        "FAILED tests/test_module.py::test_divide_by_zero - Failed: DID NOT RAISE <class 'ZeroDivisionError'>",
        "=========================== 2 failed, 198 passed in 3.87s ===============================",
    ]
    return "\n".join(lines)


def test_pytest_failing_run_keeps_failure_tracebacks_and_summary():
    output = _pytest_fail_fixture()
    text, meta = cof.compress("pytest -q", output, 1)
    assert meta["filter"] == "pytest"
    # Failure detail survives.
    assert "test_add_numbers" in text
    assert "AssertionError" in text
    assert "assert 4 == 5" in text
    assert "test_divide_by_zero" in text
    assert "DID NOT RAISE" in text
    # Short summary + final summary survive.
    assert "FAILED tests/test_module.py::test_add_numbers" in text
    assert "2 failed, 198 passed in 3.87s" in text
    # The 198 individual PASSED lines are gone, collapsed into a count.
    assert "test_case_050 PASSED" not in text
    assert "198 passed" in text
    assert meta["kept_lines"] < meta["original_lines"]
    assert len(text) < len(output) * 0.5


def test_pytest_traceback_capped_per_test():
    # A single failure with a huge traceback: capped, but head+tail kept.
    body = ["________________________________ test_huge ________________________________"]
    body += [f"    frame_{i}()" for i in range(150)]
    body += ["E       AssertionError: boom"]
    output = (
        "=================================== FAILURES ===================================\n"
        + "\n".join(body)
        + "\n=========================== 1 failed in 0.10s ===============================\n"
    )
    text, meta = cof.compress("pytest -q", output, 1)
    assert "AssertionError: boom" in text
    assert "frame_0()" in text  # head kept
    assert "lines omitted" in text
    assert meta["kept_lines"] < 160


# ---------------------------------------------------------------------------
# unittest / node --test / vitest / jest / npm test — keyword-based filter
# ---------------------------------------------------------------------------

def test_unittest_failure_kept_dots_collapsed():
    lines = ["." * 50]
    lines += [
        "======================================================================",
        "FAIL: test_something (tests.test_mod.TestCase)",
        "----------------------------------------------------------------------",
        "Traceback (most recent call last):",
        '  File "tests/test_mod.py", line 12, in test_something',
        "    self.assertEqual(1, 2)",
        "AssertionError: 1 != 2",
        "",
        "----------------------------------------------------------------------",
        "Ran 51 tests in 0.02s",
        "",
        "FAILED (failures=1)",
    ]
    output = "\n".join(lines)
    text, meta = cof.compress("python -m unittest discover", output, 1)
    assert meta["filter"] == "node_test"
    assert "AssertionError: 1 != 2" in text
    assert "FAIL: test_something" in text
    assert "50 passed" in text


def test_npm_test_jest_style_failure_kept():
    lines = ["PASS src/foo.test.js"] * 40
    lines += [
        "FAIL src/bar.test.js",
        "  ✕ adds numbers correctly",
        "    expect(received).toBe(expected)",
        "    Expected: 5",
        "    Received: 4",
        "",
        "Tests: 1 failed, 40 passed, 41 total",
    ]
    output = "\n".join(lines)
    text, meta = cof.compress("npm test", output, 1)
    assert "FAIL src/bar.test.js" in text
    assert "Expected: 5" in text
    assert "1 failed, 40 passed, 41 total" in text
    assert meta["kept_lines"] < meta["original_lines"]


# ---------------------------------------------------------------------------
# git diff — lockfile collapsed, real code change kept with capped context
# ---------------------------------------------------------------------------

def _git_diff_fixture() -> str:
    code_diff = [
        "diff --git a/src/foo.py b/src/foo.py",
        "index abc1234..def5678 100644",
        "--- a/src/foo.py",
        "+++ b/src/foo.py",
        "@@ -1,10 +1,10 @@",
        " def foo():",
        "     x = 1",
        "     y = 2",
        "-    return x + y",
        "+    return x + y + 1",
        " ",
        " def bar():",
        "     pass",
    ]
    lock_lines = [
        "diff --git a/package-lock.json b/package-lock.json",
        "index 1111111..2222222 100644",
        "--- a/package-lock.json",
        "+++ b/package-lock.json",
        "@@ -1,300 +1,320 @@",
        " {",
        '   "name": "app",',
    ]
    for i in range(150):
        lock_lines.append(f'-    "dep{i}": "1.0.{i}",')
    for i in range(160):
        lock_lines.append(f'+    "dep{i}": "1.0.{i + 1}",')
    return "\n".join(code_diff + lock_lines)


def test_git_diff_keeps_code_change_collapses_lockfile():
    output = _git_diff_fixture()
    text, meta = cof.compress("git diff", output, 0)
    assert meta["filter"] == "git_diff"
    # Real code change is kept, with headers and the actual +/- lines.
    assert "diff --git a/src/foo.py b/src/foo.py" in text
    assert "@@ -1,10 +1,10 @@" in text
    assert "-    return x + y" in text
    assert "+    return x + y + 1" in text
    # Unchanged context beyond 1 line is dropped.
    assert "def foo():" not in text
    assert "unchanged line(s) omitted" in text
    # The lockfile diff is collapsed to one summary line, not full content.
    assert "package-lock.json: lockfile diff collapsed" in text
    assert '"dep75"' not in text
    assert len(text) < len(output) * 0.3


# ---------------------------------------------------------------------------
# git status — long and short forms, grouped with counts and a path cap
# ---------------------------------------------------------------------------

def test_git_status_long_form_grouped_and_capped():
    untracked = [f"file_{i:03d}.txt" for i in range(150)]
    lines = [
        "On branch main",
        "Your branch is up to date with 'origin/main'.",
        "",
        "Changes to be committed:",
        '  (use "git restore --staged <file>..." to unstage)',
        "\tmodified:   a.py",
        "\tnew file:   b.py",
        "",
        "Changes not staged for commit:",
        '  (use "git add <file>..." to update what will be committed)',
        "\tmodified:   c.py",
        "",
        "Untracked files:",
        '  (use "git add <file>..." to include in what will be committed)',
    ]
    lines += [f"\t{f}" for f in untracked]
    output = "\n".join(lines)
    text, meta = cof.compress("git status", output, 0)
    assert meta["filter"] == "git_status"
    assert "modified:   a.py" in text
    assert "file_099.txt" in text  # within the first 100
    assert "file_149.txt" not in text  # beyond the cap
    assert "+50 more" in text
    assert len(text) < len(output)


def test_git_status_short_form_grouped_by_state():
    lines = [" M a.py", "A  b.py"]
    lines += [f"?? new_file_{i:03d}.txt" for i in range(200)]
    output = "\n".join(lines)
    text, meta = cof.compress("git status --short", output, 0)
    assert meta["filter"] == "git_status"
    assert "?? (200):" in text
    assert " M a.py" in text
    assert "A  b.py" in text
    assert "new_file_099.txt" in text
    assert "new_file_150.txt" not in text
    assert "+100 more" in text
    assert len(text) < len(output)


# ---------------------------------------------------------------------------
# grep / rg — grouped by file, matches and files capped
# ---------------------------------------------------------------------------

def test_rg_output_grouped_and_capped_across_many_files():
    lines = []
    for f in range(80):
        for m in range(100):
            lines.append(f"src/module_{f:03d}.py:{m + 1}:    TODO fix this thing #{m}")
    output = "\n".join(lines)
    text, meta = cof.compress("rg TODO src/", output, 0)
    assert meta["filter"] == "grep"
    assert "src/module_000.py (100 match(es)):" in text
    assert "+80 more in this file" in text
    assert "+30 more file(s)" in text
    assert len(text) < len(output) * 0.3


# ---------------------------------------------------------------------------
# pip / npm install — noise dropped, errors and final line kept
# ---------------------------------------------------------------------------

def test_pip_install_noise_dropped_errors_and_final_line_kept():
    lines = ["Collecting requests"]
    for i in range(80):
        lines.append(f"  Downloading requests-2.{i}.tar.gz (12 kB)")
        lines.append("Requirement already satisfied: certifi in /usr/lib (2024.2.2)")
    lines.append("ERROR: pip's dependency resolver does not currently support this combination")
    lines.append("Successfully installed requests-2.31.0 certifi-2024.2.2")
    output = "\n".join(lines)
    text, meta = cof.compress("pip install requests", output, 0)
    assert meta["filter"] == "install"
    assert "Successfully installed requests-2.31.0 certifi-2024.2.2" in text
    assert "ERROR: pip's dependency resolver" in text
    assert "Downloading requests-2." not in text
    assert len(text) < len(output) * 0.3


# ---------------------------------------------------------------------------
# Generic fallback — ANSI/progress stripped, repeated lines collapsed
# ---------------------------------------------------------------------------

def test_generic_strips_ansi_and_cr_progress_bars():
    output = (
        "\x1b[32mBuilding...\x1b[0m\n"
        "Progress: 10%\rProgress: 50%\rProgress: 100%\n"
        "\x1b[1mDone\x1b[0m\n"
    )
    text, meta = cof.compress("some-build-tool run", output, 0)
    assert "\x1b[" not in text
    assert "\r" not in text
    assert "Progress: 100%" in text
    assert "Progress: 10%" not in text


def test_generic_keeps_every_line_of_windows_output():
    # A Windows program ends lines with \r\n. Seen live: a monthly report
    # reached the model as two blank lines and the last one.
    report = [
        "2025-12    1  base      90.00  iva      3.60  total      93.60",
        "2026-01    2  base     147.20  iva     28.67  total     175.87",
        "2026-02    2  base     374.97  iva     45.74  total     420.71",
    ]
    text, meta = cof.compress("python -m billing.cli report data.json", "\r\n".join(report) + "\r\n", 0)
    for line in report:
        assert line in text
    # a progress redraw inside CRLF output still shows only its final state
    text, _ = cof.compress("some-build-tool run", "Progress: 10%\rProgress: 100%\r\nDone\r\n" * 3, 0)
    assert "Progress: 100%" in text and "Done" in text and "Progress: 10%" not in text


def test_output_that_only_lost_colour_codes_does_not_ask_for_a_rerun():
    output = "".join(f"\x1b[32mline {i} ok\x1b[0m\n" for i in range(20))
    text, meta = cof.compress("some-build-tool run", output, 0)
    assert all(f"line {i} ok" in text for i in range(20))
    assert "rerun" not in text


def test_generic_collapses_repeated_lines():
    lines = ["Connecting..."] * 200 + ["Connected."]
    output = "\n".join(lines)
    text, meta = cof.compress("some-tool run", output, 0)
    assert meta["filter"] == "generic"
    assert "(\u00d7200)" in text
    assert text.count("Connecting...") == 1
    assert "Connected." in text
    assert len(text) < len(output) * 0.1


# ---------------------------------------------------------------------------
# Hard rules: never longer, fail open
# ---------------------------------------------------------------------------

def test_never_makes_output_longer_returns_original():
    output = "hello\n"
    text, meta = cof.compress("echo hello", output, 0)
    assert text == output
    assert meta["filter"] == "none"


def test_fails_open_on_filter_exception(monkeypatch):
    def _boom(output, exit_code):
        raise RuntimeError("filter bug")

    monkeypatch.setitem(cof._FILTERS, "pytest", _boom)
    output = _pytest_pass_fixture(50)
    text, meta = cof.compress("pytest -q", output, 0)
    assert text == output
    assert meta["filter"] == "none"


def test_never_drops_a_line_naming_error_outside_a_kept_block():
    # A command that classifies as 'generic' (not test/diff/status/grep/
    # install) must still never silently drop a line mentioning an error.
    lines = ["some normal line " + str(i) for i in range(5)]
    lines.insert(2, "WARNING: disk usage is high")
    output = "\n".join(lines)
    text, meta = cof.compress("do-something --verbose", output, 0)
    assert "WARNING: disk usage is high" in text


def test_empty_output_untouched():
    text, meta = cof.compress("pytest -q", "", 0)
    assert text == ""
    assert meta == {
        "filter": "none", "original_lines": 0, "kept_lines": 0,
        "original_chars": 0, "kept_chars": 0,
    }


# ---------------------------------------------------------------------------
# Windows command forms
# ---------------------------------------------------------------------------

def test_windows_powershell_forms_classified():
    assert cof.classify("Get-ChildItem -Recurse | Select-String -Pattern 'TODO'") == "grep"
    assert cof.classify(r".\venv\Scripts\python.exe -m pytest tests\unit") == "pytest"
    assert cof.classify("findstr /s /i TODO *.py") == "grep"


# ---------------------------------------------------------------------------
# Wiring: format_tool_result compresses the in-prompt copy
# ---------------------------------------------------------------------------

def test_format_tool_result_compresses_bash_output_only():
    result = {"output": _pytest_pass_fixture(200), "exit_code": 0}
    formatted = format_tool_result("bash: pytest -q", result, tool="bash", command="pytest -q")
    assert "200 passed" in formatted
    assert "test_case_150" not in formatted
    assert "output compressed by pytest" in formatted


def test_format_tool_result_skips_compression_for_non_shell_tools():
    result = {"output": _pytest_pass_fixture(200), "exit_code": 0}
    formatted = format_tool_result("python: run script", result, tool="python", command="pytest -q")
    assert "test_case_150" in formatted  # untouched — python isn't a shell tool


def test_format_tool_result_respects_raw_requested_opt_out():
    result = {"output": _pytest_pass_fixture(200), "exit_code": 0, "_raw_requested": True}
    formatted = format_tool_result("bash: pytest -q", result, tool="bash", command="pytest -q")
    assert "test_case_150" in formatted


def test_format_tool_result_respects_setting_off(monkeypatch):
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: False)
    result = {"output": _pytest_pass_fixture(200), "exit_code": 0}
    formatted = format_tool_result("bash: pytest -q", result, tool="bash", command="pytest -q")
    assert "test_case_150" in formatted


# ---------------------------------------------------------------------------
# Integration: the model sees compressed text; the persisted/offloaded
# artifact still holds the raw, uncompressed output.
# ---------------------------------------------------------------------------

@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "offload.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    yield engine
    engine.dispose()


def test_offloaded_artifact_keeps_raw_while_prompt_copy_is_compressed(own_database):
    # Small enough that the offload preview (head 1500 + tail 500 chars)
    # is NOT itself truncated, so any shrinkage seen in the formatted text
    # is compression's doing, not the offload's own truncation.
    raw_output = _pytest_pass_fixture(20)
    assert len(raw_output) <= 2000

    offloaded = offload.offload_if_oversized(
        {"output": raw_output, "exit_code": 0},
        owner="alice", session_id="s1", run_id="r1", call_id="c1", tool="bash",
        threshold_chars=200,
    )
    assert offloaded.get("artifact_id")
    # The offload preview itself was not truncated (raw fits in the window).
    assert "test_case_010" in offloaded["output"]

    formatted = format_tool_result(
        "bash: pytest -q", offloaded, tool="bash", command="pytest -q",
    )
    assert "20 passed" in formatted
    assert "test_case_010" not in formatted  # compressed away from the prompt

    read_back = offload.read_artifact_range(offloaded["artifact_id"], owner="alice")
    assert "test_case_010 PASSED" in read_back["text"]
    assert "20 passed in 4.21s" in read_back["text"]
