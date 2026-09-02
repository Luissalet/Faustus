"""The static-analysis gate (src/static_checks.py) and its wiring in the turn.

The harness only proved the changed files *parse* (py_compile / node --check).
Parsing accepts `Depends(get_db)` without the import; the mistake then costs a
40 s pytest run, or shows up when the app boots. These tests pin the four
things that make the gate trustworthy:

  * a real undefined name introduced by the turn is reported, on its line;
  * the same finding OUTSIDE the turn's diff is ignored (it cannot spend a
    fix round — the rule project_tests.compare_with_baseline already applies);
  * with no tool installed the result is `unavailable`: no round, no failure;
  * a hanging or gibberish-emitting tool never breaks the turn.

Plus the wiring test: a module nobody calls is nothing delivered.
"""

import ast
import os
import shutil
import sys
import time

import pytest

_HAS_GIT = shutil.which("git") is not None
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


@pytest.fixture
def settings(monkeypatch):
    """Override src.settings.get_setting for the module's `_setting` helper."""
    values = {}
    import src.settings as st
    real = st.get_setting

    def _get(key, default=None):
        return values[key] if key in values else real(key, default)
    monkeypatch.setattr(st, "get_setting", _get, raising=False)
    return values


def _w(path, text):
    """Write LF text as-is (Path.write_text would turn it into CRLF on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    _w(root / "src" / "api.py", "import os\n\n\ndef ping():\n    return os.sep\n")
    _w(root / "src" / "__init__.py", "")
    _w(root / "README.md", "# demo\n")
    return root


def _has_python_checker(workspace) -> bool:
    from src import static_checks as sc
    return any(".py" in (c.get("exts") or ()) for c in sc.detect_checkers(str(workspace)))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_detect_finds_a_python_checker_and_selects_correctness_rules_only(ws):
    """ruff or pyflakes must be found, and ruff must run with --select F,E9:
    an unconfigured project has hundreds of style warnings and they would
    drown F821 ('undefined name'), the finding this whole gate exists for."""
    from src import static_checks as sc
    checkers = sc.detect_checkers(str(ws))
    py = [c for c in checkers if ".py" in (c.get("exts") or ())]
    assert py, "neither ruff nor pyflakes was found (pyflakes is in requirements.txt)"
    assert py[0]["tool"] in ("ruff", "pyflakes")
    for c in checkers:
        if c["tool"] == "ruff":
            argv = c["argv"]
            assert "--select" in argv and argv[argv.index("--select") + 1] == "F,E9"
            assert not any(a.startswith("--select=E,W") for a in argv)


def test_detect_is_empty_when_the_feature_is_off(ws, settings):
    from src import static_checks as sc
    settings["agent_static_analysis"] = "off"
    assert sc.detect_checkers(str(ws), mode=sc.resolve_mode(None)) == []
    assert sc.run_for_turn(str(ws), ["src/api.py"]) is None


def test_an_override_command_wins_over_detection(ws, settings):
    from src import static_checks as sc
    settings["agent_static_analysis_command"] = "my-linter --strict"
    checkers = sc.detect_checkers(str(ws))
    assert len(checkers) == 1 and checkers[0]["tool"] == "my-linter"
    assert checkers[0]["argv"] == ["my-linter", "--strict"]


def test_types_mode_adds_the_type_checkers_names_mode_does_not(ws):
    """`names` is the default because it is the cheap, high-signal tier;
    `types` opts into the whole-project type checkers (tsc, mypy, cargo)."""
    from src import static_checks as sc
    _w(ws / "tsconfig.json", '{"compilerOptions": {"noEmit": true}}\n')
    names = {c["tool"] for c in sc.detect_checkers(str(ws), mode="names")}
    types = {c["tool"] for c in sc.detect_checkers(str(ws), mode="types")}
    assert "tsc" not in names
    assert types >= names
    if shutil.which("tsc") or os.path.isfile(os.path.join(str(ws), "node_modules", ".bin", "tsc")):
        assert "tsc" in types


# ---------------------------------------------------------------------------
# Parsing — never trust the tool's output shape
# ---------------------------------------------------------------------------

def test_parse_findings_reads_ruff_pyflakes_cargo_and_tsc():
    from src import static_checks as sc
    ruff = sc.parse_findings("src/api.py:8:16: F821 Undefined name `Depends`\n"
                             "src/api.py:2:8: F401 [*] `json` imported but unused\n"
                             "Found 2 errors.\n[*] 1 fixable with the `--fix` option.\n")
    assert [(f["line"], f["code"]) for f in ruff] == [(8, "F821"), (2, "F401")]
    assert ruff[0]["msg"] == "Undefined name `Depends`"
    assert ruff[1]["msg"] == "`json` imported but unused"      # the [*] marker is dropped

    pf = sc.parse_findings("src/api.py:8:16: undefined name 'Depends'\n"
                           "src/api.py:2:1: 'json' imported but unused\n")
    assert [(f["line"], f["code"]) for f in pf] == [(8, "F821"), (2, "F401")]

    go = sc.parse_findings("# example.com/probe\nvet: ./main.go:7:2: undefined: undefinedCall\n")
    assert len(go) == 1 and go[0]["path"] == "main.go" and go[0]["line"] == 7

    rs = sc.parse_findings("src/main.rs:2:13: error[E0425]: cannot find function `missing_fn`\n"
                           "error: could not compile `probe` (bin \"probe\") due to 1 previous error\n")
    assert len(rs) == 1 and rs[0]["code"] == "E0425" and rs[0]["line"] == 2

    ts = sc.parse_findings("a.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n",
                           fmt="tsc")
    assert len(ts) == 1 and ts[0]["code"] == "TS2322" and ts[0]["line"] == 1 and ts[0]["path"] == "a.ts"


def test_malformed_tool_output_never_raises_and_never_invents_findings():
    from src import static_checks as sc
    for junk in ("", "\x00\x00\x00", "Segmentation fault\n", "not:a:number: boom\n",
                 "a.py:not_a_line:1: F821 x\n", ":::\n", "a.py:0:0:\n",
                 "a.py:99999999999999999999:1: F821 huge\n", "Found 3 errors.\n",
                 "Traceback (most recent call last):\n  File \"x\", line 1\n"):
        assert sc.parse_findings(junk) == [], junk
    # …and a tool that exits non-zero with unparseable noise is INCONCLUSIVE,
    # never "clean" (a silent false negative is the one outcome we refuse).
    entry = sc._entry_from_run("a.py", "linty", rc=1, out="Segmentation fault\n", err="")
    assert entry["ok"] is None and entry["errors"] == [] and "linty" in (entry["error"] or "")


def test_coloured_tool_output_is_still_parsed():
    """Measured, not assumed: project_tests._clean_env sets FORCE_COLOR=0, and
    ruff's colour library reads FORCE_COLOR as *present = force colour ON*
    whatever the value. Every finding then arrived wrapped in escape codes and
    matched nothing — the gate reported "clean" on a file full of F821."""
    from src import static_checks as sc
    coloured = ("\x1b[1msrc/api.py\x1b[0m\x1b[36m:\x1b[0m2\x1b[36m:\x1b[0m8\x1b[36m:\x1b[0m "
                "\x1b[1m\x1b[31mF821\x1b[0m Undefined name `Depends`\n")
    out = sc.parse_findings(coloured)
    assert len(out) == 1 and out[0]["code"] == "F821" and out[0]["line"] == 2
    assert out[0]["path"] == "src/api.py" and "\x1b" not in out[0]["msg"]
    env = sc._tool_env()
    assert "FORCE_COLOR" not in env and env["NO_COLOR"] == "1"


def test_parse_hunks_marks_only_the_lines_the_turn_added():
    from src import static_checks as sc
    diff = (
        "diff --git a/src/api.py b/src/api.py\n"
        "index 111..222 100644\n"
        "--- a/src/api.py\n"
        "+++ b/src/api.py\n"
        "@@ -1,4 +1,5 @@\n"
        " import os\n"
        "-old = 1\n"
        "+new_a = 1\n"
        "+new_b = 2\n"
        " tail = 3\n"
        "@@ -20,2 +21,3 @@ def f():\n"
        " keep\n"
        "+added_later = 4\n"
        " keep2\n"
    )
    assert sc.parse_hunks(diff) == {2, 3, 22}
    assert sc.parse_hunks("") == set()
    assert sc.parse_hunks("@@ garbage @@\n+x\n") == set()
    # A brand-new file: git emits every line as added, so all of it is "changed".
    assert sc.parse_hunks("@@ -0,0 +1,3 @@\n+a\n+b\n+c\n") == {1, 2, 3}


# ---------------------------------------------------------------------------
# The adjudication rule — the heart of it
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_an_undefined_name_the_turn_introduced_is_reported_on_its_line(ws, data_dir, settings):
    """The exact scenario: 'add the /api/foo endpoint', the model writes the
    route using a symbol it never imported. py_compile says OK; this says F821."""
    from src import static_checks as sc
    from src import workspace_checkpoints as wc
    if not _has_python_checker(ws):
        pytest.skip("no python checker available")
    cp = wc.checkpoint(str(ws))
    assert cp and cp.get("sha")
    _w(ws / "src" / "api.py",
       "import os\n\n\ndef ping():\n    return os.sep\n\n\n"
       "def get_item(db=Depends(get_db)):\n    return db\n")
    res = sc.run_for_turn(str(ws), ["src/api.py"], checkpoint_sha=cp["sha"])
    assert res and res["ran"] and res["available"] and not res["inconclusive"]
    assert res["ok"] is False
    codes = {f["code"] for f in res["findings"]}
    names = " ".join(f["msg"] for f in res["findings"])
    assert "F821" in codes, res["findings"]
    assert "Depends" in names and "get_db" in names
    assert all(f["line"] == 8 for f in res["findings"]), res["findings"]
    assert all(f["path"] == "src/api.py" for f in res["findings"])

    # It reaches the model as a bounded fix instruction…
    msg = sc.fix_message(res)
    assert msg.startswith("[Harness check") and "F821" in msg and "src/api.py:8" in msg
    assert "edit_file" in msg
    # …and the TurnLedger.static_checks seam the UI and the scorecard read.
    entries = sc.ledger_entries(res)
    assert len(entries) == 1 and entries[0]["path"] == "src/api.py"
    assert entries[0]["ok"] is False and entries[0]["tool"] in ("ruff", "pyflakes")
    assert entries[0]["errors"] and entries[0]["errors"][0]["code"] == "F821"
    assert set(entries[0]["errors"][0]) == {"line", "code", "msg"}


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_the_same_finding_outside_the_diff_costs_no_fix_round(ws, data_dir, settings):
    """A warning that was already there is not this turn's fault. Same rule as
    project_tests.compare_with_baseline, and for the same reason: a fix round
    spent on someone else's mess is a round not spent on the user's request."""
    from src import static_checks as sc
    from src import workspace_checkpoints as wc
    if not _has_python_checker(ws):
        pytest.skip("no python checker available")
    _w(ws / "src" / "legacy.py",
       "def handler():\n    return already_undefined()\n\n\ndef helper():\n    return 1\n")
    cp = wc.checkpoint(str(ws))
    assert cp and cp.get("sha")
    # The turn touches helper() only — the F821 on line 2 is untouched.
    _w(ws / "src" / "legacy.py",
       "def handler():\n    return already_undefined()\n\n\ndef helper():\n    return 2\n")
    res = sc.run_for_turn(str(ws), ["src/legacy.py"], checkpoint_sha=cp["sha"])
    assert res and res["ran"] and res["available"] and not res["inconclusive"]
    assert res["findings"] == [], res["findings"]
    assert res["ignored"] >= 1 and res["ok"] is True
    assert sc.ledger_entries(res)[0]["ok"] is True

    # Now the turn edits the very line that was broken → it owns it.
    _w(ws / "src" / "legacy.py",
       "def handler():\n    return still_undefined()\n\n\ndef helper():\n    return 1\n")
    res2 = sc.run_for_turn(str(ws), ["src/legacy.py"], checkpoint_sha=cp["sha"])
    assert res2["ok"] is False and [f["line"] for f in res2["findings"]] == [2]


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_without_a_checkpoint_nothing_is_attributable_so_no_round_is_spent(ws, data_dir):
    """No checkpoint (no git, checkpoints off) → we cannot tell new from
    pre-existing. Inconclusive, not a verdict."""
    from src import static_checks as sc
    if not _has_python_checker(ws):
        pytest.skip("no python checker available")
    _w(ws / "src" / "api.py", "def f():\n    return nope()\n")
    res = sc.run_for_turn(str(ws), ["src/api.py"], checkpoint_sha=None)
    assert res["inconclusive"] is True and res["findings"] == []
    assert "checkpoint" in (res["summary"] or "")


# ---------------------------------------------------------------------------
# Honest degradation
# ---------------------------------------------------------------------------

def test_no_tool_at_all_is_unavailable_not_a_failure(ws, monkeypatch):
    """No ImportError, no silent false negative: 'unavailable', no fix round,
    and a message saying what to install."""
    from src import static_checks as sc
    monkeypatch.setattr(sc, "detect_checkers", lambda *a, **kw: [])
    res = sc.run_for_turn(str(ws), ["src/api.py"], checkpoint_sha="deadbeef")
    assert res["ran"] is False and res["available"] is False
    assert res["ok"] is None and res["findings"] == [] and res["inconclusive"] is True
    assert "pyflakes" in res["unavailable"] and "ruff" in res["unavailable"]
    assert sc.ledger_entries(res) == []          # nothing may look like a checked file
    assert sc.compact(res)["available"] is False


def test_pyflakes_is_the_out_of_the_box_fallback(ws, monkeypatch):
    """ruff is optional; pyflakes ships in requirements.txt (~100 KB, pure
    Python, no dependencies) so the gate works on a fresh install."""
    from src import static_checks as sc
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    monkeypatch.setattr(sc, "_find_tool", lambda ws_, name: None)
    checkers = sc.detect_checkers(str(ws))
    py = [c for c in checkers if ".py" in (c.get("exts") or ())]
    assert py and py[0]["tool"] == "pyflakes"
    req = open(os.path.join(_ROOT, "requirements.txt"), encoding="utf-8").read()
    assert "pyflakes" in req


def test_files_with_no_checker_are_skipped_not_reported(ws):
    from src import static_checks as sc
    out = sc.run_for_files(str(ws), ["README.md"])
    assert out == []


def test_a_turn_that_only_touched_a_readme_is_not_told_to_install_anything(ws):
    """'no tool installed' and 'nothing checkable was changed' are different
    answers — only the first deserves an install hint in the turn summary."""
    from src import static_checks as sc
    res = sc.run_for_turn(str(ws), ["README.md"], checkpoint_sha="deadbeef")
    assert res["ran"] is False and res["available"] is False and res["ok"] is None
    assert res["unavailable"] == "" and "no file with a static checker" in res["summary"]


def test_the_projects_own_style_rules_cannot_drown_the_signal(ws):
    """Run against a real ruff, not against an idea of it: a project whose
    config selects E/W/I/N/UP/D and line-length 60 must still yield ONLY the
    F rules. Style noise here would make every turn spend its fix round on
    import order."""
    from src import static_checks as sc
    if "ruff" not in {c["tool"] for c in sc.detect_checkers(str(ws))}:
        pytest.skip("ruff not available")
    _w(ws / "pyproject.toml",
       '[tool.ruff]\nline-length = 60\n\n[tool.ruff.lint]\nselect = ["E", "W", "I", "N", "UP", "D"]\n')
    _w(ws / "src" / "api.py",
       "import os,sys\nimport json\ndef  BadlyNamed( a ):\n    x=1\n    return undefined_thing(a)\n")
    out = sc.run_for_files(str(ws), ["src/api.py"])
    assert len(out) == 1 and out[0]["ok"] is False
    codes = {e["code"] for e in out[0]["errors"]}
    assert "F821" in codes
    assert all(c.startswith("F") or c.startswith("E9") for c in codes), codes

    # …and a project whose ruff config is broken is INCONCLUSIVE, never a
    # false accusation against the turn.
    _w(ws / "pyproject.toml", "[tool.ruff.lint]\nselect = [123]\n")
    broken = sc.run_for_files(str(ws), ["src/api.py"])
    assert broken[0]["ok"] is None and broken[0]["errors"] == []


def test_a_hanging_tool_is_killed_and_never_hangs_the_turn(ws):
    """The timeout must bound the whole thing (project_tests._kill_tree kills
    the tree, not just the direct child)."""
    from src import static_checks as sc
    sleeper = {
        "tool": "sleepy", "label": "sleepy", "exts": (".py",), "format": "generic",
        "argv": [sys.executable, "-c", "import time; time.sleep(120)"],
        "whole_project": False, "cwd": str(ws),
    }
    t0 = time.time()
    out = sc.run_for_files(str(ws), ["src/api.py"], 3, checkers=[sleeper])
    elapsed = time.time() - t0
    assert elapsed < 40, f"took {elapsed}s — the timeout did not bound it"
    assert len(out) == 1 and out[0]["ok"] is None and "timed out" in (out[0]["error"] or "")
    assert out[0]["errors"] == []


def test_a_tool_that_cannot_be_launched_is_inconclusive(ws):
    from src import static_checks as sc
    ghost = {
        "tool": "ghost", "label": "ghost", "exts": (".py",), "format": "generic",
        "argv": ["definitely-not-a-real-binary-xyz", "--check"],
        "whole_project": False, "cwd": str(ws),
    }
    out = sc.run_for_files(str(ws), ["src/api.py"], 10, checkers=[ghost])
    assert len(out) == 1 and out[0]["ok"] is None and out[0]["error"]


def test_run_for_files_returns_the_documented_shape(ws):
    from src import static_checks as sc
    if not _has_python_checker(ws):
        pytest.skip("no python checker available")
    _w(ws / "src" / "api.py", "def f():\n    return missing_symbol_here\n")
    out = sc.run_for_files(str(ws), ["src/api.py"])
    assert len(out) == 1
    e = out[0]
    assert set(e) >= {"path", "tool", "ok", "errors"}
    assert e["path"] == "src/api.py" and e["ok"] is False
    assert e["errors"] and set(e["errors"][0]) == {"line", "code", "msg"}
    assert e["errors"][0]["line"] == 2 and e["errors"][0]["code"] == "F821"


# ---------------------------------------------------------------------------
# The seam: TurnLedger.static_checks (UI + scorecard come free)
# ---------------------------------------------------------------------------

def test_analysis_entries_merge_into_the_syntax_check_list_without_duplicating():
    """static/js/agentHarnessUI.js lists one chip per entry and src/scorecard.py
    counts the not-ok ones: an analysis entry must UPDATE the syntax entry for
    the same file, not append a second one."""
    from src import static_checks as sc
    syntax = [{"path": "src/api.py", "ok": True, "error": None},
              {"path": "static/app.js", "ok": True, "error": None}]
    merged = sc.merge_static_checks(syntax, [
        {"path": "src/api.py", "tool": "ruff", "ok": False,
         "errors": [{"line": 8, "code": "F821", "msg": "Undefined name `Depends`"}],
         "error": "ruff: F821 Undefined name `Depends` (line 8)"},
        {"path": "main.go", "tool": "go vet", "ok": True, "errors": [], "error": None},
    ])
    assert [e["path"] for e in merged] == ["src/api.py", "static/app.js", "main.go"]
    api = merged[0]
    assert api["ok"] is False and api["tool"] == "ruff" and "F821" in api["error"]
    assert merged[1]["ok"] is True and "tool" not in merged[1]
    # The scorecard reads exactly this.
    from src import scorecard
    entry = scorecard.build_entry(
        session_id=None, model="m", endpoint_label=None, workspace="/w", user_text="add /api/foo",
        duration_s=1.0, rounds=1, harness={"stop_reason": "complete", "mutations": ["src/api.py"],
                                           "static_checks": merged, "notes": []})
    assert entry["syntax_errors"] == 1
    # An inconclusive run must never be counted as a broken file.
    assert scorecard.build_entry(
        session_id=None, model="m", endpoint_label=None, workspace="/w", user_text="x",
        duration_s=1.0, rounds=1,
        harness={"stop_reason": "complete", "mutations": ["a.py"], "notes": [],
                 "static_checks": sc.merge_static_checks([], [])})["syntax_errors"] == 0


def test_compact_keeps_the_card_small():
    from src import static_checks as sc
    res = {"ran": True, "available": True, "inconclusive": False, "ok": False, "mode": "names",
           "tools": ["ruff"], "summary": "2 problems", "duration_s": 0.2, "ignored": 3,
           "findings": [{"path": "a.py", "line": i, "code": "F821", "msg": "x" * 500, "tool": "ruff"}
                        for i in range(80)],
           "results": [{"path": "a.py", "tool": "ruff", "ok": False, "errors": []}]}
    c = sc.compact(res)
    assert c["ok"] is False and c["ignored"] == 3 and c["tools"] == ["ruff"]
    assert len(c["findings"]) <= 25 and len(c["findings"][0]["msg"]) <= 300
    assert "results" not in c
    assert sc.compact(None) is None


# ---------------------------------------------------------------------------
# Wiring — a module nobody calls is nothing delivered
# ---------------------------------------------------------------------------

def test_agent_loop_runs_the_gate_between_the_syntax_check_and_the_project_tests():
    """Order is the whole point: parse (ms) → names (0.2 s) → pytest (40 s).
    Failing early is the feature; running the gate after the suite would waste
    exactly the 40 s it exists to save."""
    with open(os.path.join(_ROOT, "src", "agent_loop.py"), encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)

    def _calls(attr, owner=None):
        out = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            for cand in [n.func, *n.args]:
                if (isinstance(cand, ast.Attribute) and cand.attr == attr
                        and (owner is None or (isinstance(cand.value, ast.Name) and cand.value.id == owner))):
                    out.append(n)
                    break
        return out

    syntax = _calls("static_check_files", "_harness")
    static = _calls("run_for_turn", "_static_checks")
    tests = _calls("run_for_turn", "_ptests")
    assert syntax, "agent_loop never calls agent_harness.static_check_files"
    assert static, "agent_loop never calls static_checks.run_for_turn — the gate is dead code"
    assert tests, "agent_loop never calls project_tests.run_for_turn"
    assert syntax[0].lineno < static[0].lineno < tests[0].lineno, (
        "order must be syntax check → static analysis → project tests")

    assert "from src import static_checks as _static_checks" in source
    # It must be called with the workspace, the turn's changed files and the
    # checkpoint the diff is adjudicated against…
    call = static[0]
    pos = call.args[1:] if (call.args and isinstance(call.args[0], ast.Attribute)) else call.args
    assert [getattr(a, "id", None) for a in pos[:1]] == ["workspace"]
    assert "checkpoint_sha" in [k.arg for k in call.keywords]
    # …and its findings must actually reach the model and the ledger.
    block = source[source.index("_static_checks.run_for_turn"):]
    block = block[:block.index("project_tests as _ptests")]
    assert "_static_checks.fix_message(" in block
    assert "_static_checks.ledger_entries(" in block
    assert "_ledger.static_checks" in block


# ---------------------------------------------------------------------------
# …and the same order proved by running the loop, not by reading it
# (same mocking pattern as tests/test_agent_harness_functional.py)
# ---------------------------------------------------------------------------

def _collect(gen):
    import asyncio

    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    import json
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch, settings=None, tool_exec=None):
    import src.agent_loop as al
    settings = dict(settings or {})
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: settings.get(key, default), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        if tool_exec is not None:
            r = tool_exec(block)
            if r is not None:
                return (block.tool_type, r)
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _scripted_stream(monkeypatch, rounds):
    import json
    import src.agent_loop as al
    calls = {"n": 0, "messages": []}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        calls["messages"].append([dict(m) for m in messages])
        text, finish = rounds[i]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return calls


def _edit_call(path, old, new):
    import json
    return "```edit_file\n" + json.dumps({"path": path, "old_string": old, "new_string": new}) + "\n```"


def _real_edit(workspace):
    import json

    def _exec(block):
        if block.tool_type != "edit_file":
            return None
        args = json.loads(block.content)
        p = os.path.join(workspace, args["path"])
        text = open(p, encoding="utf-8").read()
        if args["old_string"] not in text:
            return {"error": "old_string not found", "exit_code": 1}
        open(p, "w", encoding="utf-8").write(text.replace(args["old_string"], args["new_string"], 1))
        return {"output": f"Edited {args['path']} (1 replacement)", "exit_code": 0,
                "diff": {"added": 1, "removed": 1}}
    return _exec


def _loop(workspace, user, harness_options=None, max_rounds=8):
    import src.agent_loop as al
    opts = {"trusted_workspace": os.path.realpath(workspace)}
    opts.update(harness_options or {})
    return _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": user}], max_rounds=max_rounds,
        relevant_tools={"read_file", "edit_file", "glob"}, workspace=workspace,
        session_id="sess-static", harness_options=opts)))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A tiny green pytest project, so the tests stage is a clean signal."""
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "data"))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    root = tmp_path / "proj"
    _w(root / "src" / "calc.py", "def add(a, b):\n    return a + b\n")
    _w(root / "tests" / "test_calc.py",
       "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
       "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    return str(root)


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_the_loop_flags_the_undefined_name_BEFORE_it_spends_40s_on_pytest(project, monkeypatch):
    """The whole point, end to end: the model writes the endpoint using a name
    it never imported, and the runtime says so in 0.2 s instead of after the
    project's suite — then accepts the fix."""
    from src import static_checks as sc
    if not _has_python_checker(project):
        pytest.skip("no python checker available")
    _patch_common(monkeypatch, settings={"agent_project_tests": True}, tool_exec=_real_edit(project))
    calls = _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "    return a + b\n",
                    "    return a + b\n\n\ndef get_item(db=Depends(get_db)):\n    return db\n"), "tool_calls"),
        ("Listo, he añadido el endpoint /api/foo.", "stop"),
        (_edit_call("src/calc.py", "def get_item(db=Depends(get_db)):", "def get_item(db):"), "tool_calls"),
        ("Corregido: ya no uso Depends.", "stop"),
    ])
    events = _loop(project, "Añade el endpoint /api/foo")
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "static_analysis" in statuses, statuses
    assert "syntax_error" not in statuses          # it parsed perfectly — that is the point
    assert statuses.index("static_analysis") < statuses.index("tests_running"), statuses
    assert statuses[-1] == "verified", statuses

    flagged = next(e for e in events if e.get("type") == "harness_check" and e["status"] == "static_analysis")
    assert flagged["attempt"] == 1 and flagged["errors"]
    assert flagged["errors"][0]["path"] == "src/calc.py" and "F821" in flagged["errors"][0]["error"]

    # The model got it as a runtime message naming file, line and rule.
    fix_prompt = calls["messages"][2][-1]["content"]
    assert fix_prompt.startswith("[Harness check") and "F821" in fix_prompt
    assert "src/calc.py:5" in fix_prompt and "Depends" in fix_prompt

    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["static_fix_rounds"] == 1 and summary["tests"]["ok"] is True
    assert any(n.startswith("static_analysis:") for n in summary["notes"]), summary["notes"]
    assert summary["static_analysis"]["available"] is True
    metrics = next(e for e in events if e.get("type") == "metrics")["data"]
    assert metrics["harness"]["static_fix_rounds"] == 1
    assert metrics["harness"]["static_analysis"]["tools"] and "findings" in metrics["harness"]["static_analysis"]
    assert sc.compact(None) is None


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_the_loop_does_not_spend_a_round_on_a_warning_that_was_already_there(project, monkeypatch):
    if not _has_python_checker(project):
        pytest.skip("no python checker available")
    import pathlib
    _w(pathlib.Path(project) / "src" / "legacy.py",
       "def handler():\n    return already_undefined()\n\n\ndef helper():\n    return 1\n")
    _patch_common(monkeypatch, settings={"agent_project_tests": True}, tool_exec=_real_edit(project))
    _scripted_stream(monkeypatch, [
        (_edit_call("src/legacy.py", "return 1", "return 2"), "tool_calls"),
        ("He cambiado helper() en src/legacy.py.", "stop"),
    ])
    events = _loop(project, "Cambia helper() a 2 en src/legacy.py")
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "static_analysis" not in statuses, statuses
    assert statuses[-1] == "verified", statuses
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["static_fix_rounds"] == 0
    assert summary["static_analysis"]["ok"] is True and summary["static_analysis"]["ignored"] >= 1
    # The file is still chipped as checked in the Verified card.
    assert [c for c in summary["static_checks"] if c["path"] == "src/legacy.py"][0]["ok"] is True


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_the_gate_can_be_turned_off_per_project(project, monkeypatch):
    _patch_common(monkeypatch, settings={"agent_project_tests": False}, tool_exec=_real_edit(project))
    _scripted_stream(monkeypatch, [
        (_edit_call("src/calc.py", "    return a + b\n",
                    "    return a + b\n\n\ndef broken():\n    return Depends(x)\n"), "tool_calls"),
        ("Hecho.", "stop"),
    ])
    events = _loop(project, "Añade broken()", harness_options={"static_analysis": "off"})
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "static_analysis" not in statuses, statuses
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["static_analysis"] is None and summary["static_fix_rounds"] == 0


def test_the_gate_is_a_documented_setting_with_the_house_pattern():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_static_analysis"] == "names"
    assert DEFAULT_SETTINGS["agent_static_analysis_command"] == ""
    with open(os.path.join(_ROOT, "src", "settings.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "static_checks.py" in src              # the comment names the owner module
    assert src.index("agent_project_tests_baseline") < src.index("agent_static_analysis")


def test_the_turn_ledger_carries_the_result(ws):
    from src.agent_harness import TurnLedger
    led = TurnLedger(str(ws), "add the /api/foo endpoint")
    assert led.static_checks == [] and led.static_analysis is None
    assert led.static_fix_rounds == 0
    led.static_analysis = {"ok": False}
    s = led.summary()
    assert s["static_analysis"] == {"ok": False} and s["static_fix_rounds"] == 0
    assert "static_checks" in s


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_end_to_end_on_a_toy_workspace_costs_well_under_a_test_run(ws, data_dir):
    """The promise: failing in 0.2 s instead of 40 s of pytest."""
    from src import static_checks as sc
    from src import workspace_checkpoints as wc
    if not _has_python_checker(ws):
        pytest.skip("no python checker available")
    cp = wc.checkpoint(str(ws))
    _w(ws / "src" / "api.py", "import os\n\n\ndef ping():\n    return os.sep\n\n\n"
                              "def foo():\n    return Depends(get_db)\n")
    t0 = time.time()
    res = sc.run_for_turn(str(ws), ["src/api.py"], checkpoint_sha=cp["sha"])
    assert res["ok"] is False and res["findings"]
    assert time.time() - t0 < 20
    assert res["duration_s"] >= 0


def test_a_utf8_bom_is_not_a_syntax_error_for_the_inprocess_pyflakes(ws):
    """Seen live (ronda 6): a test file written by PowerShell carries a UTF-8
    BOM. Python's compiler accepts the BOM in *bytes* but not as the U+FEFF
    character of a decoded str, so decoding with plain utf-8 turned every
    BOM'd file into 'invalid non-printable character U+FEFF' on line 1 — and
    when the turn had touched line 1 (the import line) the gate charged a
    fix round to the model, which then rewrote the whole file to 'remove
    the BOM'. Decode with utf-8-sig, like Python itself does."""
    from src import static_checks as sc
    p = ws / "bommed.py"
    p.write_bytes(b"\xef\xbb\xbfimport os\n\n\ndef f():\n    return os.sep\n")
    rc, out, err = sc._run_inprocess_pyflakes(str(ws), ["bommed.py"])
    assert rc == 0
    assert "FEFF" not in out and "FEFF" not in err
    assert out.strip() == "" and err.strip() == ""
    # A real problem on a BOM'd file is still reported, on the right line.
    p.write_bytes(b"\xef\xbb\xbfimport os\n\n\ndef f():\n    return missing_name\n")
    rc, out, err = sc._run_inprocess_pyflakes(str(ws), ["bommed.py"])
    assert "bommed.py:5" in out and "missing_name" in out
