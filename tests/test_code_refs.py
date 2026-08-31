"""Pasted `path:line` references: parsing, resolution, windows and injection.

The corpus below is not invented: every traceback, pytest failure and Node
stack in this file was produced by actually running the failing code (python3
run.py, python3 -m pytest, node src/a.js) and pasted here verbatim, with only
the checkout root rewritten to /home/dev/proj — which is itself the normal case,
since a traceback is usually pasted from a different machine or a CI log than
the workspace the agent is looking at.

What the tests care about is the same thing the feature exists for: the model
must be handed the exact failing lines (so it does not spend two grep rounds
rediscovering them), and it must never be handed anything else — a dependency's
source, a file outside the workspace, or the target of a symlink that escapes.
"""
import ast
import os
import re
import textwrap

import pytest

from src import code_refs as cr


# ── the corpus (real output, captured by running the code) ────────────────

PY_TRACEBACK = '''Traceback (most recent call last):
  File "/home/dev/proj/run.py", line 2, in <module>
    handler({"count": "x"})
  File "/home/dev/proj/src/app.py", line 12, in handler
    return int(payload["count"]) + 1
           ^^^^^^^^^^^^^^^^^^^^^
ValueError: invalid literal for int() with base 10: 'x'
'''

PY_TRACEBACK_WITH_DEPS = '''Traceback (most recent call last):
  File "/home/dev/proj/src/app.py", line 12, in handler
    return int(payload["count"]) + 1
  File "/home/dev/proj/.venv/lib/python3.11/site-packages/requests/api.py", line 73, in get
    return request("get", url, **kwargs)
  File "/usr/lib/python3.11/json/decoder.py", line 355, in raw_decode
    obj, end = self.scan_once(s, idx)
  File "<frozen importlib._bootstrap>", line 1204, in _gcd_import
ValueError: boom
'''

PYTEST_OUTPUT = '''=================================== FAILURES ===================================
___________________________________ test_add ___________________________________

    def test_add():
>       assert add(2, 2) == 5
E       assert 4 == 5
E        +  where 4 = add(2, 2)

tests/test_demo.py:6: AssertionError
=========================== short test summary info ============================
FAILED tests/test_demo.py::test_add - assert 4 == 5
1 failed in 0.02s
'''

NODE_STACK = '''/home/dev/proj/src/a.js:1
function boom() { throw new Error('kaboom'); }
                  ^

Error: kaboom
    at boom (/home/dev/proj/src/a.js:1:25)
    at outer (/home/dev/proj/src/a.js:2:20)
    at Object.<anonymous> (/home/dev/proj/src/a.js:3:1)
    at Module._compile (node:internal/modules/cjs/loader:1705:14)
    at node_modules/express/lib/router/index.js:47:12
'''


@pytest.fixture(autouse=True)
def _fresh_index():
    """The workspace index is cached per root; every test builds its own tree."""
    import src.agent_harness as ah
    ah._index_cache.clear()
    yield
    ah._index_cache.clear()


def _numbered(prefix, n):
    return "".join(f"{prefix}_line_{i}\n" for i in range(1, n + 1))


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text(_numbered("app", 200), encoding="utf-8")
    (tmp_path / "src" / "a.js").write_text(_numbered("// js", 60), encoding="utf-8")
    (tmp_path / "run.py").write_text(_numbered("run", 10), encoding="utf-8")
    (tmp_path / "tests" / "test_demo.py").write_text(textwrap.dedent("""\
        def add(a, b):
            return a + b


        def test_add():
            assert add(2, 2) == 5
        """), encoding="utf-8")
    return str(tmp_path)


# ── extract: the shapes that actually get pasted ──────────────────────────

def test_a_real_python_traceback_yields_every_frame_with_its_line():
    refs = cr.extract(PY_TRACEBACK)
    assert [(r.path, r.line, r.source) for r in refs] == [
        ("/home/dev/proj/run.py", 2, "traceback"),
        ("/home/dev/proj/src/app.py", 12, "traceback"),
    ]


def test_a_windows_traceback_frame_keeps_the_drive_letter_in_the_path():
    refs = cr.extract('  File "C:\\proj\\src\\app.py", line 42, in handler\n')
    assert refs == [cr.CodeRef("C:\\proj\\src\\app.py", 42, None, "traceback", "")]


def test_a_real_pytest_failure_gives_both_the_line_and_the_node_id():
    refs = cr.extract(PYTEST_OUTPUT)
    assert cr.CodeRef("tests/test_demo.py", 6, None, "pytest", "") in refs
    # The node id has no line: the test NAME is what identifies the frame.
    assert cr.CodeRef("tests/test_demo.py", None, None, "pytest", "test_add") in refs


def test_a_real_node_stack_gives_file_line_and_column():
    refs = cr.extract(NODE_STACK)
    ours = [r for r in refs if r.path.endswith("src/a.js")]
    assert (ours[0].line, ours[0].col, ours[0].source) == (1, None, "node")   # the header frame
    assert (25, 20, 1) == (ours[1].col, ours[2].col, ours[1].line)
    # `node:internal/...:1705:14` has no file extension and is not a path.
    assert not any("internal/modules" in r.path for r in refs)


def test_a_bare_windows_path_is_not_read_as_a_line_number():
    assert cr.extract(r"mira C:\proj\a.py cuando puedas") == []
    assert cr.extract(r"C:\proj\a.py") == []


def test_a_url_with_a_port_is_never_a_code_reference():
    assert cr.extract("el server escucha en http://localhost:8080/status") == []
    assert cr.extract("https://raw.example.com/repo/src/app.py:42 lo explica") == []
    # …while the same file:line outside a URL still is one.
    assert cr.extract("src/app.py:42 lo explica")[0].line == 42


def test_references_are_deduplicated_and_capped():
    refs = cr.extract("src/a.py:10\n" * 5)
    assert refs == [cr.CodeRef("src/a.py", 10, None, "generic", "")]
    assert len(cr.extract("".join(f"src/f{i}.py:{i}\n" for i in range(200)))) <= cr._MAX_REFS


# ── resolve: what is ours and what is not ─────────────────────────────────

def test_frames_from_another_checkout_resolve_by_suffix_against_the_index(ws):
    inside, outside = cr.resolve(ws, cr.extract(PY_TRACEBACK))
    assert [r.path for r in inside] == ["run.py", "src/app.py"]
    assert outside == []


def test_dependency_stdlib_and_frozen_frames_stay_outside(ws):
    inside, outside = cr.resolve(ws, cr.extract(PY_TRACEBACK_WITH_DEPS))
    assert [r.path for r in inside] == ["src/app.py"]
    assert [r.path for r in outside] == [
        "/home/dev/proj/.venv/lib/python3.11/site-packages/requests/api.py",
        "/usr/lib/python3.11/json/decoder.py",
        "<frozen importlib._bootstrap>",
    ]


def test_node_modules_frames_stay_outside(ws):
    inside, outside = cr.resolve(ws, cr.extract(NODE_STACK))
    assert {r.path for r in inside} == {"src/a.js"}
    assert [r.path for r in outside] == ["node_modules/express/lib/router/index.js"]


def test_a_path_that_does_not_exist_is_not_invented(ws):
    inside, outside = cr.resolve(ws, cr.extract("src/nope.py:12: SyntaxError"))
    assert inside == [] and [r.path for r in outside] == ["src/nope.py"]


# ── window ────────────────────────────────────────────────────────────────

def test_the_window_is_numbered_aligned_and_marks_the_pointed_line(ws):
    out = cr.window(os.path.join(ws, "src", "app.py"), 12, radius=3)
    assert out.splitlines() == [
        "   9 | app_line_9",
        "  10 | app_line_10",
        "  11 | app_line_11",
        "> 12 | app_line_12",
        "  13 | app_line_13",
        "  14 | app_line_14",
        "  15 | app_line_15",
    ]


def test_two_close_frames_share_one_window_with_both_lines_marked(ws):
    out = cr.window(os.path.join(ws, "src", "app.py"), [40, 46], radius=5)
    assert out.count(">") == 2
    assert out.startswith("  35 |") and out.rstrip().endswith("51 | app_line_51")
    # One window, not two overlapping ones: line 41 appears exactly once.
    assert len(re.findall(r"^\s+41 \| ", out, re.M)) == 1


def test_a_window_respects_its_character_budget(ws):
    out = cr.window(os.path.join(ws, "src", "app.py"), 100, radius=25, budget_chars=200)
    assert 0 < len(out) <= 200
    assert "> 100 | app_line_100" in out


def test_a_line_past_the_end_of_the_file_yields_nothing(ws):
    assert cr.window(os.path.join(ws, "run.py"), 9999) == ""
    assert cr.window(os.path.join(ws, "nope.py"), 3) == ""


# ── turn_context: the block the loop injects ──────────────────────────────

def test_the_block_carries_the_failing_lines_of_our_files_only(ws):
    ctx = cr.turn_context(ws, "me peta esto:\n" + PY_TRACEBACK_WITH_DEPS)
    assert ctx and "src/app.py" in ctx["text"]
    assert "> 12 | app_line_12" in ctx["text"]
    # The dependency frames are named, with an explicit "not yours" sentence…
    assert "site-packages/requests/api.py" in ctx["text"]
    assert "NOT your code" in ctx["text"] and "do not edit them" in ctx["text"]
    # …but never inlined.
    assert "scan_once" not in ctx["text"]
    assert [r.path for r in ctx["refs"]] == ["src/app.py"]
    assert len(ctx["outside"]) == 3


def test_a_pytest_node_id_centres_the_window_on_the_named_test(ws):
    ctx = cr.turn_context(ws, "FAILED tests/test_demo.py::test_add - assert 4 == 5")
    assert ctx and "def test_add():" in ctx["text"]
    assert "> 5 | def test_add():" in ctx["text"]


def test_a_file_already_inlined_by_an_at_mention_is_not_repeated(ws):
    text = "@src/app.py " + PY_TRACEBACK
    full = cr.turn_context(ws, text)
    assert full and "src/app.py" in full["text"]
    trimmed = cr.turn_context(ws, text, exclude=["src/app.py"])
    assert trimmed and "app_line_12" not in trimmed["text"]
    assert "run.py" in trimmed["text"]          # the other frame still rides along


def test_eight_frames_stay_inside_the_budget_and_the_file_cap(tmp_path):
    for i in range(8):
        (tmp_path / f"mod{i}.py").write_text(_numbered(f"m{i}", 300), encoding="utf-8")
    paste = "".join(f'  File "/ci/build/mod{i}.py", line 150, in f{i}\n' for i in range(8))
    ctx = cr.turn_context(str(tmp_path), paste, budget_chars=1200)
    assert ctx
    bodies = re.findall(r"```\w*\n(.*?)\n```", ctx["text"], re.S)
    assert 0 < len(bodies) <= cr._MAX_FILES
    assert sum(len(b) for b in bodies) <= 1200


def test_nothing_to_say_returns_none(ws):
    assert cr.turn_context(ws, "arregla el login, por favor") is None
    assert cr.turn_context("", PY_TRACEBACK) is None


def test_the_feature_can_be_switched_off(ws, monkeypatch):
    monkeypatch.setattr(cr, "_setting",
                        lambda k, d: False if k == "agent_code_refs" else d)
    assert cr.turn_context(ws, PY_TRACEBACK) is None


def test_the_budget_setting_is_the_default(ws, monkeypatch):
    seen = {}

    def fake(key, default):
        seen[key] = default
        return 120 if key == "agent_code_ref_chars" else True

    monkeypatch.setattr(cr, "_setting", fake)
    ctx = cr.turn_context(ws, PY_TRACEBACK)
    assert seen["agent_code_ref_chars"] == 4000
    body = re.findall(r"```\w*\n(.*?)\n```", ctx["text"], re.S)
    assert sum(len(b) for b in body) <= 120


# ── the same containment invariant as file_mentions.context_text ──────────

@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_a_frame_naming_a_link_out_of_the_workspace_is_never_inlined(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "outside_secret.py"
    outside.write_text("SUPER_SECRET_TOKEN = 'abc123'\n", encoding="utf-8")
    try:
        os.symlink(outside, os.path.join(ws, "notas.py"))
    except (OSError, NotImplementedError):                 # pragma: no cover
        pytest.skip("cannot create symlinks here")
    ctx = cr.turn_context(ws, '  File "notas.py", line 1, in <module>\n')
    assert ctx is None or "SUPER_SECRET_TOKEN" not in ctx["text"]


def test_a_frame_naming_a_secret_file_is_not_inlined(ws):
    with open(os.path.join(ws, "credentials"), "w", encoding="utf-8") as fh:
        fh.write("aws_secret_access_key = TOPSECRET\n")
    ctx = cr.turn_context(ws, "credentials:1: bad token\n" + PY_TRACEBACK)
    assert ctx and "TOPSECRET" not in ctx["text"]


# ── the false positive this kills in the harness ──────────────────────────

def test_pasted_dependency_frames_are_not_user_named_missing_files(tmp_path):
    """They used to look like files the user named that do not exist, which is
    what fires check_target_substitution — on a turn where nothing was
    substituted, because nobody ever meant to edit requests/api.py."""
    import src.agent_harness as h
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    paste = ('me peta esto:\n'
             '  File "src/app.py", line 1, in handler\n'
             '  File "venv/lib/python3.11/site-packages/requests/api.py", line 73, in get\n'
             '    at Router.handle (node_modules/express/lib/router/index.js:47:12)\n')
    led = h.TurnLedger(str(tmp_path), paste)
    assert led.user_missing_paths() == []
    led.record("edit_file", '{"path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2"}',
               {"output": "Edited", "exit_code": 0}, 2)
    assert led.check_target_substitution("Arreglado el cast en src/app.py.") is None


# ── wiring: a module nobody calls is nothing delivered ────────────────────

def test_agent_loop_injects_the_code_ref_block_next_to_the_at_mentions():
    """The loop must actually call code_refs.turn_context, wrap the block in the
    untrusted-context envelope, and insert it before the user's turn — after the
    repo map and the @ mentions, closest to the request."""
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "agent_loop.py")
    with open(src_path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "to_thread"
             and any(isinstance(a, ast.Attribute) and a.attr == "turn_context"
                     and isinstance(a.value, ast.Name) and a.value.id == "_code_refs"
                     for a in n.args)]
    assert calls, "agent_loop never calls code_refs.turn_context"
    # The workspace and the user's message are what it is called with, and the
    # files already inlined by @ mentions are excluded so nothing is duplicated.
    call = calls[0]
    assert [getattr(a, "id", None) for a in call.args[1:]] == ["workspace", "_last_user"]
    assert [k.arg for k in call.keywords] == ["exclude"]

    block = source[source.index("from src import code_refs"):]
    block = block[:block.index("_ody_qwen_finetune_model =")]
    assert "untrusted_context_message(" in block
    assert "_insert_before_latest_user(" in block
    # After the mentions (which are after the repo map), before the user turn.
    assert source.index("from src import file_mentions") < source.index("from src import code_refs")
    assert source.index("from src import repo_map") < source.index("from src import code_refs")
