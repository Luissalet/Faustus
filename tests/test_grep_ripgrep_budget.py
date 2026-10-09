"""grep with ripgrep: same size limit as the fallback, partial results on timeout, never a bare "No matches".

Seen on the PC once ripgrep was installed: rg over D:/LocalAI (not a Git repo, so no .gitignore) spent the
whole 20 s budget inside model weights and the tool answered only "grep: timed out". With --max-filesize the
same search answers in about 4 s.
"""

import asyncio
import io
import json
import shutil
import subprocess
import sys

import pytest

import src.agent_tools.filesystem_tools as fs
import src.tool_execution as te


def _grep(**args):
    return asyncio.run(fs.GrepTool().execute(json.dumps(args), {}))


class FakeRg:
    """Streams the given lines, then (if hang) blocks like a ripgrep still walking a huge tree."""
    lines, hang, seen, err_text, code = [], False, {}, "", 0

    def __init__(self, cmd, **kw):
        FakeRg.seen.update(cmd=cmd, kw=kw, killed=False)
        self.stdout = self._out()
        self.stderr = io.StringIO(FakeRg.err_text)
        self._done = False

    def _out(self):
        for ln in FakeRg.lines:
            yield ln + "\n"
        if FakeRg.hang:
            import time
            time.sleep(3)
        self._done = True

    def poll(self):
        return self.code if self._done else None

    def wait(self, timeout=None):
        import time
        deadline = time.monotonic() + (timeout or 0)
        while not self._done and time.monotonic() < deadline:
            time.sleep(0.005)
        if not self._done:
            raise subprocess.TimeoutExpired(cmd="rg", timeout=timeout)
        return self.returncode

    @property
    def returncode(self):
        return self.code

    def kill(self):
        FakeRg.seen["killed"] = True
        self._done = True  # a killed child is dead immediately, like a real one


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


def test_an_invalid_regex_is_an_error_not_no_matches(fake_rg):
    """rg exit 2 (bad pattern) used to surface as exit 0 / "No matches" with stderr discarded."""
    fake_rg.lines, fake_rg.hang, fake_rg.code = [], False, 2
    fake_rg.err_text = "regex parse error: unclosed group"
    res = _grep(pattern="(")
    assert res["exit_code"] == 1, res
    assert "unclosed group" in res["error"] and "No matches" not in res.get("output", "")


def test_hits_still_declare_the_size_limit(fake_rg):
    """The size limit is declared conservatively on every search with results."""
    fake_rg.lines, fake_rg.hang = ["a.py:1:needle"], False
    res = _grep(pattern="needle")
    assert res["exit_code"] == 0, res
    assert "a.py:1:needle" in res["output"]
    assert "were not searched" in res["output"] and "partial" in res["output"]


def test_an_error_with_hits_is_not_swallowed(fake_rg):
    """rg exit 2 after some matches must not lose the stderr detail."""
    fake_rg.lines, fake_rg.hang, fake_rg.code = ["a.py:1:needle"], False, 2
    fake_rg.err_text = "regex parse error: unclosed group\n"
    res = _grep(pattern="(")
    assert res["exit_code"] == 0, res
    assert "a.py:1:needle" in res["output"]
    assert "unclosed group" in res["output"] and "incomplete" in res["output"]


def test_a_huge_stderr_does_not_stall_the_search(fake_rg, monkeypatch):
    """262 KB of rg warnings used to risk filling the pipe: drain is bounded now."""
    fake_rg.lines, fake_rg.hang, fake_rg.code = [], False, 2
    fake_rg.err_text = "regex parse error: unclosed group\n" + ("warning: something\n" * 20000)
    monkeypatch.setattr(fs, "GREP_STDERR_KEEP_BYTES", 256)
    res = _grep(pattern="(")
    assert res["exit_code"] == 1, res
    assert "unclosed group" in res["error"], res
    assert len(res["error"]) < 500, res


def test_a_own_kill_with_a_negative_code_is_not_a_ripgrep_failure(fake_rg):
    """POSIX: our own kill reports -9; it must not invent "results may be incomplete"."""
    fake_rg.lines, fake_rg.hang, fake_rg.code = [f"f{i}.txt:1:needle" for i in range(10)], True, -9
    res = _grep(pattern="needle", max_results=3)
    assert res["exit_code"] == 0, res
    assert res["output"].count(":1:needle") == 3
    assert "incomplete" not in res["output"] and "capped at 3 matches" in res["output"], res


def _popen_running_a_script(monkeypatch, tmp_path, body):
    """A real child process with real pipes: only the rg command is replaced."""
    script = tmp_path / "_stderr_child.py"
    script.write_text(body, encoding="utf-8")
    real_popen = subprocess.Popen
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "C:/fake/rg.exe" if name == "rg" else None)
    monkeypatch.setattr(te, "_resolve_search_root", lambda raw: str(tmp_path))
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: real_popen([sys.executable, str(script)], **kw))


def test_many_stderr_lines_are_drained_so_a_real_error_is_not_lost(monkeypatch, tmp_path):
    """20k stderr lines: a reader that stops early leaves the child blocked on the
    full pipe, the budget expires and a real exit-2 error is reported as a timeout."""
    _popen_running_a_script(monkeypatch, tmp_path,
                            "import sys\n"
                            "for _ in range(20000):\n"
                            "    sys.stderr.write('read error: many warnings\\n')\n"
                            "sys.stderr.flush()\n"
                            "sys.exit(2)\n")
    import time
    t = time.monotonic()
    res = _grep(pattern="needle")
    assert time.monotonic() - t < 15, "the child was left blocked on the pipe until the budget expired"
    assert res["exit_code"] == 1, res
    assert "many warnings" in res["error"], res


def test_a_single_huge_stderr_line_is_bounded_and_keeps_the_cause(monkeypatch, tmp_path):
    """A 262 KB line used to be retained whole before it could be trimmed, and the
    cause on a later line was lost because only the first line was kept."""
    _popen_running_a_script(monkeypatch, tmp_path,
                            "import sys\n"
                            "sys.stderr.write('E' * 262144 + '\\n')\n"
                            "sys.stderr.write('error: the real cause\\n')\n"
                            "sys.stderr.flush()\n"
                            "sys.exit(2)\n")
    res = _grep(pattern="needle")
    assert res["exit_code"] == 1, res
    assert "the real cause" in res["error"], res
    assert len(res["error"]) < 500, res


def test_the_stderr_budget_counts_utf8_bytes_not_characters():
    """Review r3: 20k U+1F525 chars are 60k UTF-8 bytes; the retained text must
    respect the declared byte cap, not the character count."""
    blob = "\U0001F525" * 20000
    head, tail = fs._bounded_stderr_chunks([blob], fs.GREP_STDERR_KEEP_BYTES)
    assert len("".join(head).encode("utf-8")) <= fs.GREP_STDERR_KEEP_BYTES // 2, head
    assert len("".join(tail).encode("utf-8")) <= fs.GREP_STDERR_KEEP_BYTES // 2, tail
    assert "\U0001F525" in "".join(head) and "\U0001F525" in "".join(tail)


def test_a_unicode_stderr_is_bounded_in_bytes_and_keeps_the_cause(monkeypatch, tmp_path):
    """Real pipes: 20k U+1F525 (60k UTF-8 bytes) drained to EOF, cause kept,
    and nothing beyond the byte cap retained."""
    _popen_running_a_script(monkeypatch, tmp_path,
                            "import sys\n"
                            "sys.stderr.write('\\U0001F525' * 20000 + '\\n')\n"
                            "sys.stderr.write('error: the real cause\\n')\n"
                            "sys.stderr.flush()\n"
                            "sys.exit(2)\n")
    res = _grep(pattern="needle")
    assert res["exit_code"] == 1, res
    assert "the real cause" in res["error"], res
    assert len(res["error"]) < 500, res


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
    assert only_big["output"].count("are not searched") == 1, only_big


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_real_ripgrep_invalid_regex_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(te, "_resolve_search_root", lambda raw: str(tmp_path))
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    res = _grep(pattern="(unclosed")
    assert res["exit_code"] == 1, res
    assert "error" in res and "No matches" not in res.get("output", ""), res
    assert "unclosed group" in res["error"], res


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_real_ripgrep_nested_glob_with_a_skipped_big_file_still_declares_the_limit(monkeypatch, tmp_path):
    """The r1 review case: glob **/sub/*.txt, a small hit and a matching >2 MB file.

    The r1 stat-walk matched the glob against the basename only, so a glob with a
    directory component never matched and the omission stayed hidden.
    """
    monkeypatch.setattr(te, "_resolve_search_root", lambda raw: str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "small.txt").write_text("needle here\n", encoding="utf-8")
    (sub / "big.txt").write_text("needle\n" + "x" * (3 * 1024 * 1024), encoding="utf-8")
    res = _grep(pattern="needle", glob="**/sub/*.txt")
    assert res["exit_code"] == 0, res
    assert "small.txt:1:needle here" in res["output"], res
    assert "were not searched" in res["output"] and "partial" in res["output"], res
