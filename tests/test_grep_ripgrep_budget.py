"""grep with ripgrep: same size limit as the fallback, partial results on timeout, never a bare "No matches".

Seen on the PC once ripgrep was installed: rg over D:/LocalAI (not a Git repo, so no .gitignore) spent the
whole 20 s budget inside model weights and the tool answered only "grep: timed out". With --max-filesize the
same search answers in about 4 s.
"""

import asyncio
import json
import shutil
import subprocess

import pytest

import src.agent_tools.filesystem_tools as fs
import src.tool_execution as te


def _grep(**args):
    return asyncio.run(fs.GrepTool().execute(json.dumps(args), {}))


class FakeRg:
    """Streams the given lines, then (if hang) blocks like a ripgrep still walking a huge tree."""
    lines, hang, seen = [], False, {}

    def __init__(self, cmd, **kw):
        FakeRg.seen.update(cmd=cmd, kw=kw, killed=False)
        self.stdout = self._out()
        self._done = False

    def _out(self):
        for ln in FakeRg.lines:
            yield ln + "\n"
        if FakeRg.hang:
            import time
            time.sleep(3)
        self._done = True

    def poll(self):
        return 0 if self._done else None

    def kill(self):
        FakeRg.seen["killed"] = True


@pytest.fixture
def fake_rg(monkeypatch, tmp_path):
    monkeypatch.setattr(te, "_resolve_search_root", lambda raw: str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "C:/fake/rg.exe" if name == "rg" else None)
    monkeypatch.setattr(subprocess, "Popen", FakeRg)
    FakeRg.seen = {}
    return FakeRg


def test_a_timeout_keeps_what_ripgrep_found(fake_rg, monkeypatch):
    fake_rg.lines, fake_rg.hang = ["a.txt:1:needle one", "b.txt:7:needle two"], True
    monkeypatch.setattr(fs, "GREP_FALLBACK_BUDGET_S", 0.5)
    res = _grep(pattern="needle")
    assert res["exit_code"] == 0, res
    assert "a.txt:1:needle one" in res["output"] and "b.txt:7:needle two" in res["output"]
    assert "stopped early" in res["output"] and "partial" in res["output"]
    assert fake_rg.seen["killed"]
    cmd = fake_rg.seen["cmd"]
    assert "--max-filesize" in cmd and str(fs.GREP_FALLBACK_MAX_FILE_BYTES) in cmd
    assert fake_rg.seen["kw"].get("encoding") == "utf-8"


def test_ripgrep_is_stopped_once_there_are_enough_matches(fake_rg):
    fake_rg.lines, fake_rg.hang = [f"f{i}.txt:1:needle" for i in range(10)], True
    import time
    t = time.monotonic()
    res = _grep(pattern="needle", max_results=3)
    assert time.monotonic() - t < 2.5, "waited for the whole tree instead of stopping at max_results"
    assert res["output"].count(":1:needle") == 3 and "stopped early" not in res["output"]
    assert "capped at 3 matches" in res["output"] and fake_rg.seen["killed"]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_real_ripgrep_finds_text_and_reports_the_size_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(te, "_resolve_search_root", lambda raw: str(tmp_path))
    (tmp_path / "a.py").write_text("x = 1\nneedle here ñ\n", encoding="utf-8")
    (tmp_path / "big.log").write_text("needle\n" + "x" * (3 * 1024 * 1024), encoding="utf-8")
    found = _grep(pattern="needle here")
    assert found["exit_code"] == 0 and "a.py:2:needle here ñ" in found["output"], found
    only_big = _grep(pattern="^needle$")
    assert only_big["exit_code"] == 0
    assert "No matches" in only_big["output"] and "are not searched" in only_big["output"], only_big
