"""The Python grep fallback (no ripgrep) is bounded and says when it stopped.

Seen in an agent turn: grep with no path over a workspace that holds model
weights ran for more than ten minutes. The fallback built the whole file list
first and then read every file as text, with no time, size or count limit.
"""

import asyncio
import json
import shutil

import pytest

import src.agent_tools.filesystem_tools as fs
import src.tool_execution as te


@pytest.fixture
def fallback(monkeypatch, tmp_path):
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "rg" else real_which(name, *a, **k))
    monkeypatch.setattr(te, "_resolve_search_root", lambda raw: str(tmp_path))
    return tmp_path


def _grep(**args):
    return asyncio.run(fs.GrepTool().execute(json.dumps(args), {}))


def test_finds_text_and_skips_binary_and_oversized_files(fallback, monkeypatch):
    (fallback / "a.py").write_text("x = 1\nneedle here\n", encoding="utf-8")
    (fallback / "weights.bin").write_bytes(b"\0\0needle\0" * 10)
    (fallback / "big.txt").write_text("needle\n" * 100, encoding="utf-8")
    monkeypatch.setattr(fs, "GREP_FALLBACK_MAX_FILE_BYTES", 200)

    res = _grep(pattern="needle")

    assert res["exit_code"] == 0, res
    assert "a.py:2:needle here" in res["output"]
    assert "weights.bin" not in res["output"]
    assert "big.txt:" not in res["output"]  # not searched...
    assert "1 file(s) over" in res["output"] and "big.txt" in res["output"] and "not searched" in res["output"]
    assert "stopped early" not in res["output"]


def test_a_skipped_large_file_is_never_a_silent_no_match(fallback, monkeypatch):
    """Review of #156: a needle inside a file over the size limit used to come back as "No matches"."""
    (fallback / "big.txt").write_text("needle\n" + "x" * 300 + "\n", encoding="utf-8")
    monkeypatch.setattr(fs, "GREP_FALLBACK_MAX_FILE_BYTES", 100)

    res = _grep(pattern="needle")

    assert "No matches" not in res["output"]
    assert "not searched" in res["output"] and "big.txt" in res["output"] and "partial" in res["output"]
    assert "capped at" not in res["output"]


def test_the_budget_also_covers_the_lines_of_one_file(fallback, monkeypatch):
    """Review of #156: the clock was only read between files, so one slow file could outlive the budget."""
    (fallback / "huge.log").write_text("x\n" * 1_500_000, encoding="utf-8")
    monkeypatch.setattr(fs, "GREP_FALLBACK_MAX_FILE_BYTES", 50 * 1024 * 1024)
    monkeypatch.setattr(fs, "GREP_FALLBACK_BUDGET_S", 0.02)

    res = _grep(pattern="needle")

    assert "stopped early" in res["output"] and "inside" in res["output"] and "huge.log" in res["output"], res


def test_time_budget_stops_and_says_the_result_is_partial(fallback, monkeypatch):
    for i in range(5):
        (fallback / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(fs, "GREP_FALLBACK_BUDGET_S", -1.0)

    res = _grep(pattern="needle")

    assert res["exit_code"] == 0, res
    assert "stopped early" in res["output"] and "partial" in res["output"]
    assert "path=" in res["output"]


def test_a_glob_that_matches_nothing_cannot_walk_unbounded(fallback, monkeypatch):
    for i in range(3):
        sub = fallback / f"d{i}"
        sub.mkdir()
        (sub / "x.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(fs, "GREP_FALLBACK_BUDGET_S", -1.0)

    res = _grep(pattern="needle", glob="*.nothing")

    assert "stopped early" in res["output"], res


def test_file_count_cap(fallback, monkeypatch):
    for i in range(4):
        (fallback / f"f{i}.txt").write_text("hay\n", encoding="utf-8")
    monkeypatch.setattr(fs, "GREP_FALLBACK_MAX_FILES", 2)

    res = _grep(pattern="needle")

    assert "stopped early after 2 files" in res["output"], res
