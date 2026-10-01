"""Zero-token advisories appended to tool results (src/loop_breaker.py)."""
from __future__ import annotations

import json
from pathlib import Path

from src import loop_breaker as lb
from src.context_engine import prompt_audit


def _guard():
    return lb.AdvisoryGuard()


def _ok(text="a.py:1: hit"):
    return {"output": text, "exit_code": 0}


def _grep(pattern, path="."):
    return json.dumps({"pattern": pattern, "path": path})


def test_rescan_of_same_tree_gets_one_advisory():
    g = _guard()
    assert g.advise("grep", _grep("foo", "src"), _ok(), 1) == ""
    assert g.advise("grep", _grep("bar", "src"), _ok(), 2) == lb.ADVISORY_RESCAN
    # at most once per run
    assert g.advise("grep", _grep("baz", "src"), _ok(), 3) == ""


def test_rescan_covers_ancestor_but_not_a_narrower_folder():
    g = _guard()
    g.advise("grep", _grep("foo", "src/app"), _ok(), 1)
    assert g.advise("grep", _grep("foo", "src/app/models"), _ok(), 2) == ""     # narrowing is fine
    g2 = _guard()
    g2.advise("grep", _grep("foo", "src/app"), _ok(), 1)
    assert g2.advise("grep", _grep("foo", "."), _ok(), 2) == lb.ADVISORY_RESCAN


def test_rescan_needs_results_window_and_a_broad_search():
    g = _guard()
    g.advise("grep", _grep("foo", "src"), {"output": "No matches found", "exit_code": 0}, 1)
    assert g.advise("grep", _grep("bar", "src"), _ok(), 2) == ""                 # nothing to re-use
    g = _guard()
    g.advise("grep", _grep("foo", "src"), _ok(), 1)
    assert g.advise("grep", _grep("bar", "src"), _ok(), 5) == ""                 # too far apart
    g = _guard()
    g.advise("grep", _grep("foo", "src/main.py"), _ok(), 1)                      # one file: not broad
    assert g.advise("grep", _grep("bar", "src/main.py"), _ok(), 2) == ""
    g = _guard()
    g.advise("grep", _grep("foo", "src"), {"error": "boom"}, 1)
    assert g.advise("grep", _grep("bar", "src"), _ok(), 2) == ""


def test_rescan_through_shell_and_glob():
    g = _guard()
    g.advise("bash", json.dumps({"command": "grep -rn foo src"}), _ok(), 1)
    assert g.advise("glob", json.dumps({"pattern": "**/*.py", "path": "."}), _ok(), 2) == lb.ADVISORY_RESCAN
    g = _guard()
    g.advise("bash", "rg foo", _ok(), 1)
    assert g.advise("bash", "find . -name '*.py'", _ok(), 3) == lb.ADVISORY_RESCAN
    g = _guard()
    g.advise("glob", json.dumps({"pattern": "setup.py"}), _ok(), 1)             # a named file is no sweep
    assert g.advise("glob", json.dumps({"pattern": "pyproject.toml"}), _ok(), 2) == ""


def _edit(path, old="foo()", new="bar()"):
    return json.dumps({"path": path, "old_string": old, "new_string": new})


def test_three_same_shape_edits_on_different_files_get_one_advisory():
    g = _guard()
    assert g.advise("edit_file", _edit("a.py"), _ok("ok"), 1) == ""
    assert g.advise("edit_file", _edit("b.py"), _ok("ok"), 2) == ""
    assert g.advise("edit_file", _edit("c.py"), _ok("ok"), 3) == lb.ADVISORY_BATCH_EDITS
    assert g.advise("edit_file", _edit("d.py"), _ok("ok"), 4) == ""              # once per run


def test_edit_streak_needs_same_shape_distinct_files_and_no_gap():
    g = _guard()
    g.advise("edit_file", _edit("a.py"), _ok("ok"), 1)
    g.advise("edit_file", _edit("b.py", "x = 1", "y = 2"), _ok("ok"), 2)         # different shape
    assert g.advise("edit_file", _edit("c.py"), _ok("ok"), 3) == ""
    g = _guard()
    for i in range(3):
        assert g.advise("edit_file", _edit("same.py"), _ok("ok"), i) == ""       # same file
    g = _guard()
    g.advise("edit_file", _edit("a.py"), _ok("ok"), 1)
    g.advise("edit_file", _edit("b.py"), _ok("ok"), 2)
    g.advise("read_file", json.dumps({"path": "x.py"}), _ok("text"), 3)          # a gap resets it
    assert g.advise("edit_file", _edit("c.py"), _ok("ok"), 4) == ""
    g = _guard()
    for i, name in enumerate(("a.py", "b.py", "c.py")):
        out = g.advise("write_file", json.dumps({"path": name, "content": "# generated header\n" * 5}),
                       _ok("ok"), i)
    assert out == lb.ADVISORY_BATCH_EDITS


def test_shell_pipelines_over_the_tree_get_one_advisory():
    g = _guard()
    assert g.advise("bash", json.dumps({"command": "find . -name '*.py' | xargs wc -l"}), _ok(), 1) \
        == lb.ADVISORY_SHELL_PIPELINE
    # the pipeline rule fired once; the second find is the (separate) re-scan hint\n    assert g.advise("bash", "find . -type f | sort | uniq -c", _ok(), 2) != lb.ADVISORY_SHELL_PIPELINE
    g = _guard()
    assert g.advise("powershell", "Get-ChildItem -Recurse src | Measure-Object", _ok(), 1) \
        == lb.ADVISORY_SHELL_PIPELINE
    assert _guard().advise("bash", "ls -la | wc -l", _ok(), 1) == ""
    assert _guard().advise("bash", "git log --oneline | wc -l", _ok(), 1) == ""


def test_setting_off_means_no_guard(monkeypatch):
    assert lb.advisory_guard_from_settings(lambda k, d=None: False) is None
    assert isinstance(lb.advisory_guard_from_settings(lambda k, d=None: d), lb.AdvisoryGuard)
    assert lb.advise(None, "grep", _grep("x"), _ok(), 1) == ""


def test_advise_never_raises():
    g = _guard()
    assert g.advise("grep", object(), object(), "nope") == ""


def test_advisory_lands_in_tool_result_and_prompt_prefix_is_unchanged():
    system = {"role": "system", "content": "You are Faustus. Stable system prompt."}
    user = {"role": "user", "content": "find where foo is used"}
    call1 = {"role": "assistant", "content": "```grep\n{}\n```"}
    res1 = {"role": "user", "content": "[tool result] a.py:1: hit"}
    call2 = {"role": "assistant", "content": "```grep\n{}\n```"}
    before = [system, user, call1, res1, call2]

    g = _guard()
    g.advise("grep", _grep("foo", "src"), _ok(), 1)
    advisory = g.advise("grep", _grep("bar", "src"), _ok(), 2)
    assert advisory
    plain_result = "[tool result] a.py:2: hit"
    res2 = {"role": "user", "content": lb.with_advisory(plain_result, advisory)}
    assert res2["content"].startswith(plain_result) and res2["content"].endswith(advisory)

    fp_before = prompt_audit.fingerprint_prompt(before)
    fp_after = prompt_audit.fingerprint_prompt(before + [res2])
    # every block that was already in the prompt is byte-identical ...
    assert prompt_audit.stable_prefix_blocks(fp_before, fp_after) == len(before)
    # ... and a run with the guard off sends exactly the same prefix
    fp_off = prompt_audit.fingerprint_prompt(before + [{"role": "user", "content": plain_result}])
    assert prompt_audit.stable_prefix_blocks(fp_off, fp_after) == len(before)
    assert fp_after["blocks"][-1]["sha256"] != fp_off["blocks"][-1]["sha256"]


def test_agent_loop_appends_only_to_the_formatted_tool_result():
    src = Path(__file__).resolve().parents[1].joinpath("src", "agent_loop.py").read_text(encoding="utf-8")
    assert "_advisory_guard.advise(" in src
    at = src.index("_advisory_guard.advise(")
    window = src[at:at + 400]
    assert 'formatted = f"{formatted}' in window
    assert "system_prompt" not in window and "messages.append" not in window