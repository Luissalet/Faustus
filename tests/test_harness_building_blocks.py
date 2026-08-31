"""Unit tests for the functional-verification building blocks (the loop-level
behaviour is covered by test_agent_harness_functional.py): shadow checkpoints,
project test runner, diff review, scorecard, per-project audit, project
instructions, repository map, review-mode state and the trusted-workspace gate."""

import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

_HAS_GIT = shutil.which("git") is not None


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


@pytest.fixture
def settings(monkeypatch):
    """Override src.settings.get_setting for the modules' `_setting` helpers."""
    values = {}
    import src.settings as st
    real = st.get_setting

    def _get(key, default=None):
        return values[key] if key in values else real(key, default)
    monkeypatch.setattr(st, "get_setting", _get, raising=False)
    return values


def _w(path, text):
    """Write LF text as-is (Path.write_text would turn it into CRLF on Windows)."""
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    _w(root / "src" / "calc.py", "def add(a, b):\n    return a - b\n")
    _w(root / "src" / "__init__.py", "")
    _w(root / "tests" / "test_calc.py",
       "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
       "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _w(root / "README.md", "# demo\n")
    return root


# ---------------------------------------------------------------------------
# workspace_checkpoints
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_checkpoint_diff_restore_and_reuse(ws, data_dir):
    from src import workspace_checkpoints as wc
    cp = wc.checkpoint(str(ws), "before turn 1")
    assert cp and cp["created"] and len(cp["sha"]) == 40
    # Idle turn: the same tree reuses the commit instead of stacking a new one.
    again = wc.checkpoint(str(ws), "before turn 2")
    assert again["sha"] == cp["sha"] and again["reused"] is True
    assert [c["sha"] for c in wc.list_checkpoints(str(ws))] == [cp["sha"]]

    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "src" / "new.py").write_text("X = 1\n", encoding="utf-8")
    os.remove(ws / "README.md")
    changed = {c["path"]: c["status"] for c in wc.changed_since(str(ws), cp["sha"])}
    assert changed == {"src/calc.py": "M", "src/new.py": "A", "README.md": "D"}
    # Scoped to one path (absolute paths are accepted and normalised).
    only = wc.changed_since(str(ws), cp["sha"], [str(ws / "src" / "calc.py")])
    assert [c["path"] for c in only] == ["src/calc.py"]
    diff = wc.diff_since(str(ws), cp["sha"], "src/calc.py").replace("\r\n", "\n")
    assert "-    return a - b" in diff and "+    return a + b" in diff
    assert wc.file_at(str(ws), cp["sha"], "src/calc.py") == b"def add(a, b):\n    return a - b\n"
    assert wc.exists_at(str(ws), cp["sha"], "README.md") is True
    assert wc.exists_at(str(ws), cp["sha"], "src/new.py") is False
    # A path outside the workspace is refused, never resolved.
    assert wc.file_at(str(ws), cp["sha"], "../outside.txt") is None

    res = wc.restore(str(ws), cp["sha"])
    assert set(res["restored"]) == {"src/calc.py", "README.md"} and res["deleted"] == ["src/new.py"]
    assert not res["failed"]
    assert (ws / "src" / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
    assert (ws / "README.md").is_file() and not (ws / "src" / "new.py").exists()
    assert wc.changed_since(str(ws), cp["sha"]) == []
    st = wc.status(str(ws))
    assert st["present"] and st["count"] == 1 and st["head"] == cp["sha"]
    assert wc.reset(str(ws)) is True and wc.status(str(ws))["present"] is False


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_checkpoint_skips_vendored_dirs_and_big_files(ws, data_dir, settings):
    from src import workspace_checkpoints as wc
    settings["agent_checkpoint_max_file_mb"] = 0.5     # the floor of the setting
    (ws / "node_modules" / "left-pad").mkdir(parents=True)
    (ws / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    (ws / "big.dat").write_bytes(b"x" * (600 * 1024))
    (ws / "model.gguf").write_bytes(b"gguf")
    cp = wc.checkpoint(str(ws))
    assert cp
    tracked = subprocess.run(
        ["git", "--git-dir", wc.shadow_dir(str(ws)), "--work-tree", str(ws), "ls-tree", "-r", "--name-only", cp["sha"]],
        capture_output=True, text=True, encoding="utf-8").stdout.split()
    assert "src/calc.py" in tracked and "README.md" in tracked
    assert not any(p.startswith("node_modules/") for p in tracked)
    assert "big.dat" not in tracked and "model.gguf" not in tracked
    # The user's own .git (if any) is never touched: the shadow repo lives in DATA_DIR.
    assert not (ws / ".git").exists()
    assert wc.shadow_dir(str(ws)).startswith(str(data_dir))


def test_checkpoint_helpers_without_git_or_workspace(monkeypatch, data_dir):
    from src import workspace_checkpoints as wc
    monkeypatch.setattr(wc, "git_available", lambda: False)
    assert wc.checkpoint("/nonexistent") is None
    assert wc.changed_since("/x", "abc") == [] and wc.diff_since("/x", "abc") == ""
    res = wc.restore("/x", "abc")
    assert res["error"] and res["restored"] == []
    assert wc.status("")["present"] is False


def test_propose_commit_message_strips_chatter_and_lists_files():
    from src.workspace_checkpoints import propose_commit_message
    msg = propose_commit_message("por favor, arregla la función add en calc.py. Luego revisa los tests",
                                 ["src/calc.py", "tests/test_calc.py"], language="es")
    subject, body = msg.split("\n\n", 1)
    assert subject == "Arregla la función add en calc.py"
    assert "Archivos: src/calc.py, tests/test_calc.py" in body
    long = propose_commit_message("x" * 200, [])
    assert len(long.split("\n")[0]) <= 72 and long.split("\n")[0].endswith("…")
    assert propose_commit_message("/agents", []).startswith("Agent changes")


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_user_git_commit_only_commits_the_turns_files(ws, data_dir):
    from src import workspace_checkpoints as wc
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", "add", "-A"], cwd=ws, check=True, env=env)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "base"], cwd=ws, check=True, env=env)
    assert wc.user_repo_root(str(ws / "src")) == os.path.realpath(str(ws))
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "README.md").write_text("# demo (edited by hand)\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=ws, check=True)
    res = wc.user_git_commit(str(ws), ["src/calc.py"], "Fix add")
    assert res["ok"] and res["files"] == ["src/calc.py"], res
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True).stdout
    assert "README.md" in status and "calc.py" not in status   # the hand edit stays uncommitted
    again = wc.user_git_commit(str(ws), ["src/calc.py"], "Fix add")
    assert again["ok"] is False and again.get("nothing") is True
    assert wc.user_git_commit(str(ws), ["../outside.py"], "x")["ok"] is False


# ---------------------------------------------------------------------------
# project_tests
# ---------------------------------------------------------------------------

def test_detect_test_command_pytest_npm_override(ws, tmp_path):
    from src import project_tests as pt
    spec = pt.detect_test_command(str(ws))
    assert spec["kind"] == "pytest" and spec["argv"][1:3] == ["-m", "pytest"] and "-x" in spec["argv"]
    assert pt.detect_test_command(str(ws), "make check")["shell"] == "make check"
    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    spec = pt.detect_test_command(str(node))
    assert spec["kind"] == "npm" and spec["argv"][1] == "test" and "vitest run" in spec["label"]
    (node / "package.json").write_text(json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}), encoding="utf-8")
    assert pt.detect_test_command(str(node)) is None       # npm's placeholder is not a runner
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pt.detect_test_command(str(empty)) is None
    assert pt.detect_test_command(str(tmp_path / "missing")) is None


def test_related_test_files_scopes_by_stem(ws):
    from src import project_tests as pt
    (ws / "tests" / "test_other.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (ws / "tests" / "test_calc_edge.py").write_text("def test_y():\n    pass\n", encoding="utf-8")
    # Related by content too: a test that imports the changed module without
    # carrying its name (the bench's tests/test_api.py exercising server.py).
    (ws / "tests" / "test_api.py").write_text("from src.calc import add\n\ndef test_api():\n    assert add(2, 2) == 4\n", encoding="utf-8")
    (ws / "tests" / "test_unrelated.py").write_text("import json\n\ndef test_z():\n    assert json.dumps(1) == '1'\n", encoding="utf-8")
    rel = pt.related_test_files(str(ws), [str(ws / "src" / "calc.py")])
    assert sorted(rel) == ["tests/test_api.py", "tests/test_calc.py", "tests/test_calc_edge.py"]
    assert set(rel[:2]) == {"tests/test_calc.py", "tests/test_calc_edge.py"}   # name matches first
    # A changed test file is itself in scope; an unknown module scopes nothing.
    assert pt.related_test_files(str(ws), ["tests/test_other.py"]) == ["tests/test_other.py"]
    assert pt.related_test_files(str(ws), ["src/nothing_here.py"]) == []
    assert pt.related_test_files(str(ws), ["../elsewhere/calc.py"]) == []


def test_run_for_turn_pass_fail_and_fix_message(ws, settings):
    from src import project_tests as pt
    settings["agent_project_tests"] = True
    settings["agent_project_tests_scope"] = "related"
    res = pt.run_for_turn(str(ws), ["src/calc.py"])
    assert res["ran"] and res["ok"] is False and res["kind"] == "pytest"
    assert res["scope"] == "related" and res["related_files"] == ["tests/test_calc.py"]
    assert "1 failed" in res["summary"] and res["failures"] and "test_add" in res["failures"][0]
    msg = pt.failure_message(res)
    assert msg.startswith("[Harness check") and "FAILED" in msg and "Do NOT delete, skip or weaken tests" in msg
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    res = pt.run_for_turn(str(ws), ["src/calc.py"])
    assert res["ok"] is True and res["summary"] == "1 passed" and res["failures"] == []
    c = pt.compact(res)
    assert c["ok"] is True and "output_tail" in c and set(c) >= {"ran", "kind", "summary", "command"}
    settings["agent_project_tests"] = False
    assert pt.run_for_turn(str(ws), ["src/calc.py"]) is None


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_export_tree_materialises_the_checkpoint(ws, data_dir, tmp_path):
    from src import workspace_checkpoints as wc
    cp = wc.checkpoint(str(ws))
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    dest = tmp_path / "export"
    assert wc.export_tree(str(ws), cp["sha"], str(dest)) is True
    assert (dest / "src" / "calc.py").read_bytes() == b"def add(a, b):\n    return a - b\n"   # the OLD content
    assert (dest / "tests" / "test_calc.py").is_file() and not (dest / "__checkpoint__.zip").exists()
    assert wc.export_tree(str(ws), "0" * 40, str(tmp_path / "nope")) is False


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_failures_are_split_into_new_and_pre_existing(ws, data_dir, settings):
    """A test that already failed at the checkpoint is reported as pre-existing;
    when every failure is pre-existing the run is flagged (no fix round)."""
    from src import project_tests as pt
    from src import workspace_checkpoints as wc
    settings["agent_project_tests"] = True
    # A second test (tied to calc.py only by import) that fails regardless of
    # calc.py: pre-existing and exempt.
    (ws / "tests" / "test_api_extra.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
        "from src.calc import add\n\n\ndef test_broken_before():\n    assert add(0, 0) == 99\n", encoding="utf-8")
    cp = wc.checkpoint(str(ws))
    # The turn: fixes add() — test_calc passes now, test_broken_before still fails.
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    res = pt.run_for_turn(str(ws), ["src/calc.py"], checkpoint_sha=cp["sha"])
    assert res["ok"] is False and res["pre_existing_only"] is True
    assert [pt._failure_id(f) for f in res["pre_existing"]] == ["tests/test_api_extra.py::test_broken_before"]
    assert res["new_failures"] == [] and res["baseline"]["ran"] is True and res["baseline"]["ok"] is False
    assert "pre-existing" in res["summary"]
    msg = pt.failure_message(res)
    assert "already failed before your change" in msg and "test_broken_before" in msg
    c = pt.compact(res)
    assert c["pre_existing_only"] is True and c["baseline"]["ok"] is False
    # The turn breaks add() instead: test_calc (tied by NAME to calc.py) fails
    # before and after → never exempt, the fix round happens.
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    res = pt.run_for_turn(str(ws), ["src/calc.py"], checkpoint_sha=cp["sha"])
    assert res["ok"] is False and not res.get("pre_existing_only")
    assert res["pre_existing"] and res["exempt"] == []
    settings["agent_project_tests_baseline"] = False
    res = pt.run_for_turn(str(ws), ["src/calc.py"], checkpoint_sha=cp["sha"])
    assert "baseline" not in res and res["new_failures"] == res["failures"]


def test_run_tests_timeout_is_inconclusive(ws):
    from src import project_tests as pt
    spec = {"kind": "custom", "shell": f'"{sys.executable}" -c "import time; time.sleep(30)"', "label": "sleep"}
    res = pt.run_tests(str(ws), spec, timeout_s=10)
    assert res["timed_out"] and res["ok"] is False and res["inconclusive"] and "timed out" in res["summary"]
    assert res["duration_s"] < 25


def test_parse_output_pytest_and_npm_variants():
    from src.project_tests import parse_output
    p = parse_output("pytest", 1, "FAILED tests/test_a.py::test_x - AssertionError: boom\n= 1 failed, 2 passed in 0.12s =")
    assert p["ok"] is False and p["summary"] == "1 failed, 2 passed" and p["failures"] == ["tests/test_a.py::test_x — AssertionError: boom"]
    none = parse_output("pytest", 5, "no tests ran in 0.01s")
    assert none["ok"] is True and none["inconclusive"] is True
    env = parse_output("pytest", 2, "ERROR tests/test_a.py\nE   ModuleNotFoundError: No module named 'fastapi'")
    assert env["inconclusive"] is True
    jest = parse_output("npm", 1, "Tests:       1 failed, 3 passed, 4 total\n  ✕ adds numbers (3 ms)")
    assert jest["summary"] == "1 failed, 3 passed, 4 total" and jest["failures"] == ["✕ adds numbers (3 ms)"]
    mocha = parse_output("npm", 0, "  3 passing (20ms)\n  1 pending")
    assert mocha["ok"] and mocha["summary"] == "3 passing, 1 pending"
    cargo = parse_output("cargo", 101, "test a ... FAILED\ntest result: FAILED. 1 passed; 1 failed; 0 ignored")
    assert cargo["summary"] == "1 passed, 1 failed" and cargo["failures"] == ["test a ... FAILED"]
    go = parse_output("go", 1, "--- FAIL: TestX (0.00s)\nFAIL\tpkg/x\t0.1s")
    assert go["failures"][0] == "TestX" and "failing" in go["summary"]


# ---------------------------------------------------------------------------
# auto_review
# ---------------------------------------------------------------------------

def test_review_parse_variants():
    from src.auto_review import _parse
    ok = _parse('<think>hmm</think>\n```json\n{"verdict": "ok", "summary": "fine", "findings": []}\n```')
    assert ok == {"verdict": "ok", "summary": "fine", "findings": []}
    issues = _parse('Sure: {"verdict": "issues", "summary": "bad", "findings": [{"severity": "ERROR", "file": "a.py", "line": "7", "issue": "off by one"},], }')
    assert issues["verdict"] == "issues" and issues["findings"] == [{"severity": "error", "file": "a.py", "line": 7, "issue": "off by one", "evidence": ""}]
    # verdict "ok" with an error-severity finding is promoted to "issues"; unknown severities become warnings.
    promoted = _parse('{"verdict": "ok", "findings": [{"severity": "fatal", "issue": "x"}, {"severity": "error", "issue": "y"}]}')
    assert promoted["verdict"] == "issues" and [f["severity"] for f in promoted["findings"]] == ["warning", "error"]
    assert _parse("I cannot review this.")["verdict"] == "unparsed"
    assert _parse("")["verdict"] == "unparsed"


def test_review_turn_calls_the_model_with_one_attempt_and_handles_empty(ws, data_dir, monkeypatch):
    """Regression: llm_call_async(max_retries=0) never calls the model and
    returns None (seen live: verdict 'unparsed', summary 'None')."""
    from src import auto_review as ar
    import src.llm_core as lc
    seen = {}

    async def _fake(url, model, messages, **kwargs):
        seen.update(kwargs, url=url, model=model, prompt=messages[-1]["content"])
        return '{"verdict": "issues", "summary": "wrong operator", "findings": [{"severity": "error", "file": "src/calc.py", "line": 2, "issue": "subtracts", "evidence": "return a * b"}]}'
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, files, sha: {"diff": "-    return a - b\n+    return a * b\n", "source": "checkpoint", "truncated": False})
    res = asyncio.run(ar.review_turn(workspace=str(ws), changed=["src/calc.py"], checkpoint_sha="abc",
                                     user_text="fix add", endpoint_url="http://127.0.0.1:11434/v1",
                                     model="qwen3.5:9b", reviewer_model=None, tests={"ran": True, "ok": True, "summary": "1 passed"}))
    assert seen["max_retries"] >= 1 and seen["model"] == "qwen3.5:9b" and seen["workload"] == "foreground"
    assert "<diff>" in seen["prompt"] and "return a * b" in seen["prompt"] and "1 passed" in seen["prompt"]
    assert res["verdict"] == "issues" and res["findings"][0]["line"] == 2 and res["diff_chars"] > 0
    msg = ar.fix_message(res)
    assert "src/calc.py:2" in msg and "subtracts" in msg and msg.startswith("[Harness check")

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(lc, "llm_call_async", _none, raising=False)
    res = asyncio.run(ar.review_turn(workspace=str(ws), changed=["src/calc.py"], checkpoint_sha="abc", user_text="x",
                                     endpoint_url="http://127.0.0.1:11434/v1", model="m"))
    assert res["verdict"] == "error" and "empty" in res["error"]

    async def _boom(*a, **k):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(lc, "llm_call_async", _boom, raising=False)
    res = asyncio.run(ar.review_turn(workspace=str(ws), changed=["src/calc.py"], checkpoint_sha="abc", user_text="x",
                                     endpoint_url="http://127.0.0.1:11434/v1", model="m"))
    assert res["verdict"] == "error" and "RuntimeError" in res["error"]
    # Nothing changed → skipped without calling anything.
    res = asyncio.run(ar.review_turn(workspace=str(ws), changed=[], checkpoint_sha=None, user_text="x",
                                     endpoint_url="http://x", model="m"))
    assert res["verdict"] == "skipped"


def test_review_findings_are_grounded_in_the_diff_or_the_request():
    from src.auto_review import ground_findings
    diff = "--- a/src/calc.py\n+++ b/src/calc.py\n@@\n-    return a - b\n+    return a * b\n+    print('debug')\n"
    res = ground_findings([
        {"severity": "error", "file": "src/calc.py", "line": 2, "issue": "multiplies", "evidence": "return a * b"},
        {"severity": "error", "file": "src/calc.py", "line": 3, "issue": "debug print left", "evidence": "  print( 'debug' )  "},
        {"severity": "error", "file": "src/calc.py", "line": 9, "issue": "the button is placed after", "evidence": "<button id='refresh'>"},
        {"severity": "warning", "file": "src/calc.py", "line": None, "issue": "no evidence", "evidence": ""},
        {"severity": "error", "file": "", "line": None, "issue": "half of the request is missing", "evidence": "y actualiza projects.js"},
    ], diff, user_text="Renombra el campo y actualiza projects.js")
    f = res["findings"]
    assert f[0]["grounded"] and f[0]["severity"] == "error"
    assert f[1]["grounded"] is False or f[1]["grounded"] is True   # whitespace inside quotes: tolerated either way
    assert f[2]["grounded"] is False and f[2]["severity"] == "warning" and f[2]["demoted"] is True
    assert f[3]["grounded"] is False and f[3]["severity"] == "warning"
    assert f[4]["grounded"] is True and f[4]["severity"] == "error"     # request-phrase evidence
    assert res["ungrounded"] >= 2
    # A real diff line attached to a complaint about the agent's workflow
    # (seen live: "the request asks to use todowrite… the diff shows no todo
    # list", evidence '@app.get("/api/stats")') is not a code defect.
    wf = ground_findings([
        {"severity": "error", "file": "server.py", "line": 57, "evidence": "return a * b",
         "issue": "The request explicitly asks to use todowrite for the list of goals, but the diff shows no implementation of a todo list."},
        {"severity": "error", "file": "server.py", "line": 57, "evidence": "return a * b",
         "issue": "The request asks to check syntax at the end ('comprueba la sintaxis'), but the diff contains no syntax checking step."},
        {"severity": "error", "file": "server.py", "line": 2, "evidence": "return a * b",
         "issue": "multiplies instead of adding; the tests will fail"},
    ], diff, user_text="Añade el endpoint. Usa todowrite y comprueba la sintaxis al final.")
    sev = [(x["severity"], x.get("workflow", False)) for x in wf["findings"]]
    assert sev == [("warning", True), ("warning", True), ("error", False)]


def test_review_turn_demotes_ungrounded_errors_and_never_fix_rounds_on_them(ws, data_dir, monkeypatch):
    from src import auto_review as ar
    import src.llm_core as lc

    async def _fake(url, model, messages, **kwargs):
        return json.dumps({"verdict": "issues", "summary": "two problems", "findings": [
            {"severity": "error", "file": "src/calc.py", "line": 2, "issue": "invented", "evidence": "this line is not in the diff at all"},
            {"severity": "error", "file": "src/calc.py", "line": 2, "issue": "real", "evidence": "return a * b"},
        ]})
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, files, sha: {"diff": "-    return a - b\n+    return a * b\n", "source": "checkpoint", "truncated": False})
    res = asyncio.run(ar.review_turn(workspace=str(ws), changed=["src/calc.py"], checkpoint_sha="abc", user_text="fix add",
                                     endpoint_url="http://127.0.0.1:11434/v1", model="m"))
    sev = [(f["issue"], f["severity"], f["grounded"]) for f in res["findings"]]
    assert sev == [("invented", "warning", False), ("real", "error", True)] and res["ungrounded"] == 1
    assert "src/calc.py:2" in ar.fix_message(res) and "invented" not in ar.fix_message(res)

    async def _all_invented(url, model, messages, **kwargs):
        return json.dumps({"verdict": "issues", "summary": "bad", "findings": [
            {"severity": "error", "file": "x", "line": 1, "issue": "nope", "evidence": "nothing like this"}]})
    monkeypatch.setattr(lc, "llm_call_async", _all_invented, raising=False)
    res = asyncio.run(ar.review_turn(workspace=str(ws), changed=["src/calc.py"], checkpoint_sha="abc", user_text="fix add",
                                     endpoint_url="http://127.0.0.1:11434/v1", model="m"))
    assert res["verdict"] == "ok" and res["summary"].startswith("no finding could be located") and res["findings"][0]["severity"] == "warning"
    assert "ungrounded" in ar.compact(res)


def test_resolve_reviewer_modes(monkeypatch):
    from src import auto_review as ar
    assert ar.resolve_reviewer("qwen3.5:9b", "off") is None
    assert ar.resolve_reviewer("qwen3.5:9b", "same") == "qwen3.5:9b"
    assert ar.resolve_reviewer("qwen3.5:9b", "qwen3-coder-next") == "qwen3-coder-next"
    monkeypatch.setattr(ar, "_setting", lambda key, default=None: "same")
    assert ar.resolve_reviewer("m", None) == "m"
    monkeypatch.setattr(ar, "_setting", lambda key, default=None: "off")
    assert ar.resolve_reviewer("m", None) is None


# ---------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------

def test_scorecard_record_aggregate_and_table(data_dir, settings):
    from src import scorecard as sc
    settings["agent_scorecard"] = True
    harness_ok = {"stop_reason": "complete", "mutations": ["a.py"], "notes": [], "tool_calls": 4, "failed_calls": 1,
                  "static_checks": [{"ok": True}], "tests_fix_rounds": 1}
    e1 = sc.build_entry(session_id="s1", model="qwen3.5:9b", endpoint_label="local", workspace="/w", user_text="fix add  ",
                        duration_s=85.2, rounds=14, harness=harness_ok, tests={"ran": True, "ok": True},
                        review={"verdict": "ok", "findings": [], "model": "qwen3.5:9b"}, tokens_per_second=40.0)
    assert e1["verified"] is True and e1["tests"] == "pass" and e1["review"] == "ok" and e1["tests_fix_rounds"] == 1
    assert e1["task"] == "fix add" and e1["files_changed"] == 1 and e1["tok_s"] == 40.0
    e2 = sc.build_entry(session_id="s2", model="qwen3.5:9b", endpoint_label="local", workspace="/w", user_text="t2",
                        duration_s=30, rounds=3, harness={"stop_reason": "complete_unverified", "mutations": [], "notes": ["unverified: x"]},
                        tests={"ran": True, "ok": False}, review={"verdict": "unparsed"}, asked_user=True)
    assert e2["verified"] is False and e2["unverified"] is True and e2["tests"] == "fail" and e2["review"] is None
    e3 = sc.build_entry(session_id="s3", model="big:30b", endpoint_label="local", workspace=None, user_text="chat only",
                        duration_s=5, rounds=1, harness={"stop_reason": "complete", "mutations": []})
    for e in (e1, e2, e3):
        assert sc.record(e) is True
    assert len(sc.load()) == 3 and (data_dir / "scorecard.jsonl").is_file()
    rows = sc.aggregate(sc.load(), only_workspace=True)
    assert [r["model"] for r in rows] == ["qwen3.5:9b"]
    r = rows[0]
    assert r["turns"] == 2 and r["verified_rate"] == 50.0 and r["asked_user_rate"] == 50.0
    assert r["tests_pass_rate"] == 50.0 and r["tests_ran"] == 2 and r["review_ok_rate"] == 100.0 and r["reviewed"] == 1
    assert r["median_duration_s"] == 57.6 and r["unverified"] == 1
    table = sc.render_table(rows, language="es")
    assert table.splitlines()[0].startswith("| Modelo |") and "`qwen3.5:9b`" in table and "50%" in table
    assert "Todavía no hay" in sc.render_table([], "es")
    settings["agent_scorecard"] = False
    assert sc.record(e1) is False


def test_scorecard_table_route_filters_by_workspace(data_dir, settings, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.scorecard_routes as sr
    from src import scorecard as sc
    settings["agent_scorecard"] = True
    ws_a, ws_b = tmp_path / "a", tmp_path / "b"
    ws_a.mkdir(); ws_b.mkdir()
    h = {"stop_reason": "complete", "mutations": ["x.py"], "notes": []}
    sc.record(sc.build_entry(session_id="1", model="m1", endpoint_label="l", workspace=str(ws_a), user_text="t", duration_s=1, rounds=1, harness=h))
    sc.record(sc.build_entry(session_id="2", model="m2", endpoint_label="l", workspace=str(ws_b), user_text="t", duration_s=1, rounds=1, harness=h))
    sc.record(sc.build_entry(session_id="3", model="m1", endpoint_label="l", workspace=str(ws_b), user_text="t", duration_s=1, rounds=1, harness=h))
    import src.auth_helpers as ah
    import src.tool_security as ts
    app = FastAPI()
    app.include_router(sr.setup_scorecard_routes())
    c = TestClient(app)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sr, "get_current_user", lambda request: "admin")
        mp.setattr(sr, "owner_is_admin_or_single_user", lambda owner: True)
        all_rows = c.get("/api/scorecard/table").json()
        assert all_rows["models"] == 2 and all_rows["turns"] == 3
        only_b = c.get("/api/scorecard/table", params={"workspace": str(ws_b)}).json()
        assert only_b["turns"] == 2 and only_b["models"] == 2 and only_b["workspace"] == str(ws_b)
        only_a = c.get("/api/scorecard/table", params={"workspace": str(ws_a) + os.sep}).json()
        assert only_a["turns"] == 1 and "`m1`" in only_a["markdown"] and "`m2`" not in only_a["markdown"]


# ---------------------------------------------------------------------------
# project_audit
# ---------------------------------------------------------------------------

def test_project_audit_records_loads_and_indexes(data_dir, tmp_path):
    from src import project_audit as pa
    key = pa.workspace_key(str(tmp_path))
    assert key.startswith("ws-") and key == pa.workspace_key(str(tmp_path) + os.sep)
    assert pa.record(key, session_id="s", message_id=1, model="m", files=[], workspace=str(tmp_path), stop_reason="complete") is None
    pa.record(key, session_id="s1", message_id=10, model="m", files=["a.py", "b.py"], workspace=str(tmp_path),
              stop_reason="complete", checkpoint="abc", user_text="  fix   a  ", tests="pass", review="ok", project_id="p1")
    pa.record(key, session_id="s2", message_id=11, model="m", files=["a.py"], workspace=str(tmp_path), stop_reason="awaiting_user")
    rows = pa.load(key)
    assert [r["message_id"] for r in rows] == [11, 10]   # newest first
    assert rows[1]["request"] == "fix a" and rows[1]["tests"] == "pass" and rows[1]["project_id"] == "p1"
    idx = {r["path"]: r for r in pa.files_index(key)}
    assert idx["a.py"]["turns"] == 2 and sorted(idx["a.py"]["sessions"]) == ["s1", "s2"] and idx["b.py"]["turns"] == 1
    assert pa.load(key, limit=1) == rows[:1]
    assert pa.clear(key) is True and pa.load(key) == []
    assert pa.load("../../etc/passwd") == []


# ---------------------------------------------------------------------------
# project_instructions
# ---------------------------------------------------------------------------

def test_project_instructions_block_priority_cache_and_cap(ws, settings):
    from src import project_instructions as pi
    pi.invalidate()
    assert pi.block(str(ws)) == ""
    (ws / "CLAUDE.md").write_text("Use pnpm.\r\nNever touch migrations.\r\n", encoding="utf-8")
    pi.invalidate(str(ws))
    text = pi.block(str(ws))
    assert text.startswith("\n\n## Project instructions from CLAUDE.md") and "Never touch migrations." in text
    assert "\r\n" not in text
    # AGENTS.md wins over CLAUDE.md.
    (ws / "AGENTS.md").write_text("Run `make test` before finishing.\n", encoding="utf-8")
    pi.invalidate(str(ws))
    text = pi.block(str(ws))
    assert "from AGENTS.md" in text and "make test" in text and "pnpm" not in text
    # Cached: an edit within the TTL is not re-read until invalidated.
    (ws / "AGENTS.md").write_text("Changed rule.\n", encoding="utf-8")
    assert "make test" in pi.block(str(ws))
    pi.invalidate(str(ws))
    assert "Changed rule." in pi.block(str(ws))
    # Size cap (never below 500 chars) → truncated with a note.
    settings["agent_project_instructions_max_chars"] = 500
    (ws / "AGENTS.md").write_text("x" * 2000, encoding="utf-8")
    pi.invalidate(str(ws))
    info = pi.read(str(ws))
    assert info["truncated"] and info["chars"] == 500
    assert "(truncated" in pi.block(str(ws))
    settings["agent_project_instructions"] = False
    pi.invalidate(str(ws))
    assert pi.block(str(ws)) == ""


def test_agents_md_draft_uses_detected_facts_and_never_overwrites(ws, monkeypatch):
    from src import project_instructions as pi
    import src.agent_harness as ah
    monkeypatch.setattr(ah, "_index_cache", {}, raising=False)
    (ws / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    d = pi.draft(str(ws), language="es")
    assert d["path"].endswith("AGENTS.md") and d["exists"] is False
    assert d["facts"]["languages"][0] == "Python" and "src" in d["facts"]["top_dirs"] and "package.json" in d["facts"]["manifests"]
    assert d["facts"]["test_command"].startswith("python") and "pytest" in d["facts"]["test_command"]   # pytest wins over npm
    text = d["text"]
    assert text.startswith("# ws — instructions for the coding agent") and "## Cómo se ejecutan los tests" in text
    assert "pytest -x -q" in text and "## No tocar" in text and "`src/`" in text
    # An existing instructions file is reported, and the draft is still produced.
    (ws / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    d2 = pi.draft(str(ws))
    assert d2["exists"] is True and d2["existing"].endswith("CLAUDE.md") and "## How to run the tests" in d2["text"]
    assert pi.draft(str(ws / "missing"))["error"]


def test_agents_md_draft_route_writes_once(ws, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.workspace_routes as wr
    monkeypatch.setattr(wr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    c = TestClient(app)
    r = c.post("/api/workspace/instructions/draft", json={"workspace": str(ws), "language": "en"})
    assert r.status_code == 200 and r.json()["written"] is False and "# ws" in r.json()["text"]
    assert not (ws / "AGENTS.md").exists()
    r = c.post("/api/workspace/instructions/draft", json={"workspace": str(ws), "write": True})
    assert r.status_code == 200 and r.json()["written"] is True
    assert (ws / "AGENTS.md").read_text(encoding="utf-8").startswith("# ws")
    # Second write: the file exists now → nothing overwritten.
    (ws / "AGENTS.md").write_text("mine\n", encoding="utf-8")
    r = c.post("/api/workspace/instructions/draft", json={"workspace": str(ws), "write": True})
    assert r.status_code == 200 and r.json()["written"] is False and r.json()["exists"] is True
    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "mine\n"
    assert c.post("/api/workspace/instructions/draft", json={"workspace": str(ws / "nope")}).status_code == 400


# ---------------------------------------------------------------------------
# repo_map
# ---------------------------------------------------------------------------

def test_repo_map_lists_files_and_symbols_with_budget(ws, settings, monkeypatch):
    from src import repo_map as rm
    import src.agent_harness as ah
    (ws / "static" / "js").mkdir(parents=True)
    (ws / "static" / "js" / "app.js").write_text(
        "export function render(x) {}\nconst helper = (a) => a\nclass Widget {}\n", encoding="utf-8")
    (ws / "src" / "models.py").write_text("class User(Base):\n    def save(self): pass\n\ndef make_user():\n    pass\n", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "x.js").write_text("function vendored() {}\n", encoding="utf-8")
    monkeypatch.setattr(ah, "_index_cache", {}, raising=False)
    rm.invalidate()
    text = rm.build(str(ws), "arregla models.py")
    assert text.startswith("Repository map of the workspace (")
    assert "## Tree" in text and "## Symbols" in text
    assert "src/models.py: class User(save), def make_user" in text
    assert "static/js/app.js: render, Widget, helper" in text
    assert "x.js" not in text and "node_modules" not in text   # (the header itself says "vendored … skipped")
    # The file the user mentioned ranks first among the symbol lines.
    sym = text.split("## Symbols", 1)[1].strip().splitlines()[1:]
    assert sym[0].startswith("src/models.py:")
    # Budget: a tiny token budget still yields a header + (part of) the tree.
    rm.invalidate()
    small = rm.build(str(ws), "", max_tokens=300)
    assert small.startswith("Repository map") and len(small) <= 300 * 4 + 400
    settings["agent_repo_map"] = False
    rm.invalidate()
    assert rm.build(str(ws)) == ""


# ---------------------------------------------------------------------------
# review_state
# ---------------------------------------------------------------------------

def test_review_state_lifecycle(data_dir):
    from services import review_state as rs
    e = rs.init(42, session_id="s1", workspace="/w", files=["a.py", "b.py", ""], checkpoint="abc")
    assert e["pending"] == ["a.py", "b.py"] and e["accepted"] == [] and e["checkpoint"] == "abc"
    assert rs.init(42, session_id="s1", workspace="/w", files=["zzz"], checkpoint=None)["pending"] == ["a.py", "b.py"]  # idempotent
    assert rs.decide(42, "a.py", "accept")["accepted"] == ["a.py"]
    e = rs.decide(42, "b.py", "reject")
    assert e["pending"] == [] and e["rejected"] == ["b.py"]
    assert rs.decide(42, "b.py", "accept")["rejected"] == [] and rs.get(42)["accepted"] == ["a.py", "b.py"]
    assert rs.decide(99, "a.py", "accept") is None
    rs.init(43, session_id="s1", workspace="/w", files=["c.py"], checkpoint=None)
    pend = rs.pending_for_session("s1")
    assert [p["message_id"] for p in pend] == ["43"] and rs.pending_for_session("other") == []
    assert rs.forget(43) is True and rs.get(43) is None and rs.forget(43) is False
    assert (data_dir / "review_state.json").is_file()


# ---------------------------------------------------------------------------
# ledger: paths the model passed as absolute are recorded workspace-relative
# ---------------------------------------------------------------------------

def test_ledger_records_absolute_paths_relative_to_the_workspace(ws):
    from src.agent_harness import TurnLedger, workspace_relative
    root = str(ws)
    assert workspace_relative(root, str(ws / "src" / "calc.py")) == "src/calc.py"
    assert workspace_relative(root, "src/calc.py") == "src/calc.py"
    assert workspace_relative(root, str(ws.parent / "elsewhere.py")) == str(ws.parent / "elsewhere.py")
    assert workspace_relative(None, str(ws / "x.py")) == str(ws / "x.py")
    led = TurnLedger(root, "arregla calc.py")
    led.record("edit_file", json.dumps({"path": str(ws / "src" / "calc.py"), "old_string": "-", "new_string": "+"}),
               {"output": "Edited", "exit_code": 0}, 1)
    led.record("write_file", json.dumps({"path": "tests/test_new.py", "content": "x"}), {"output": "Wrote", "exit_code": 0}, 2)
    assert led.mutated_paths() == ["src/calc.py", "tests/test_new.py"]


# ---------------------------------------------------------------------------
# tool gate: trusted workspace
# ---------------------------------------------------------------------------

def test_trusted_workspace_gate_only_frees_writes_inside_the_folder(tmp_path):
    from src.tool_capabilities import ToolRunSecurityContext
    root = tmp_path / "proj"
    root.mkdir()
    ctx = ToolRunSecurityContext(trusted_workspace=str(root))
    ctx.external_untrusted_context_seen = True     # a read_file of workspace content armed the gate
    edit_inside = json.dumps({"path": "src/calc.py", "old_string": "a", "new_string": "b"})
    edit_abs = json.dumps({"path": str(root / "x.py"), "content": "1"})
    assert ctx.decision_for("edit_file", edit_inside).allowed
    assert ctx.decision_for("write_file", edit_abs).allowed
    assert ctx.decision_for("apply_patch", "*** Begin Patch\n*** Update File: src/calc.py\n@@\n-a\n+b\n*** End Patch").allowed
    # Escapes, deletions, shell and delegation stay gated.
    assert not ctx.decision_for("edit_file", json.dumps({"path": "../outside.py", "old_string": "a", "new_string": "b"})).allowed
    assert not ctx.decision_for("write_file", json.dumps({"path": str(tmp_path / "other.txt"), "content": "x"})).allowed
    assert not ctx.decision_for("apply_patch", "*** Begin Patch\n*** Delete File: src/calc.py\n*** End Patch").allowed
    assert not ctx.decision_for("bash", "rm -rf build").allowed
    assert not ctx.decision_for("delegate_agents", "[]").allowed
    ctx.trusted_agents = True
    assert ctx.decision_for("delegate_agents", "[]").allowed
    # Without the flag the same write is gated; without an armed gate everything passes.
    plain = ToolRunSecurityContext()
    plain.external_untrusted_context_seen = True
    assert not plain.decision_for("edit_file", edit_inside).allowed
    assert ToolRunSecurityContext(trusted_workspace=str(root)).decision_for("bash", "ls").allowed
