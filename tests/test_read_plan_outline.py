"""read_file answers a big file with an index instead of a blind slice.

`read_file` used to hand back the first MAX_READ_CHARS characters of any file
and the note `... [truncated at 20000 chars]`. On this repo's own
src/agent_loop.py that is 4.75% of the file, cut mid-line, with no line count,
no symbol index and no hint that a range could be asked for.

src/read_plan.py replaces that reply for an *un-ranged* read of an oversized
file: facts, a symbol index with line numbers (reusing src/repo_map's
extractors), the first ~80 lines, and the literal call that fetches the rest.

Everything else has to stay exactly as it was, and most of what is below is
about proving that: a file that fits comes back byte-for-byte, an explicit
offset/limit read is untouched, and an unreadable path still produces today's
error. The last test checks the wiring — a module ReadFileTool does not call
would be nothing delivered.
"""
import json
import os

import pytest

from src import read_plan, repo_map
from src.agent_tools import filesystem_tools as fs_tools
from src.agent_tools.filesystem_tools import ReadFileTool
from src.constants import MAX_READ_CHARS
from src.tool_execution import _active_workspace


@pytest.fixture
def ws(tmp_path):
    """Bind tmp_path as the agent workspace so read_file can reach it."""
    token = _active_workspace.set(str(tmp_path))
    try:
        yield tmp_path
    finally:
        _active_workspace.reset(token)


async def read(path, **args):
    """Call the tool the way the agent does."""
    payload = {"path": str(path), **args} if args else str(path)
    content = json.dumps(payload) if args else payload
    return await ReadFileTool().execute(content, {})


def legacy_read(abs_path, offset=0, limit=0):
    """read_file's behaviour before src/read_plan.py existed, verbatim.

    Copied from the tool as it stood at b6a3634 so the regression tests compare
    against the real old output rather than against a description of it.
    """
    if offset > 0 or limit > 0:
        start = max(offset, 1)
        out, n, budget = [], 0, MAX_READ_CHARS
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i < start:
                    continue
                if limit > 0 and n >= limit:
                    break
                out.append(line)
                n += 1
                budget -= len(line)
                if budget <= 0:
                    out.append(f"\n... [truncated at {MAX_READ_CHARS} chars]")
                    break
        return "".join(out)
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read(MAX_READ_CHARS + 1)
    if len(data) > MAX_READ_CHARS:
        data = data[:MAX_READ_CHARS] + f"\n... [truncated at {MAX_READ_CHARS} chars]"
    return data


def write_py(path, funcs=200, body=14):
    """A Python file with `funcs` top-level functions, comfortably over the cap."""
    lines = ["import os", "", "MODULE_CONST = 1", ""]
    for i in range(funcs):
        lines.append(f"def handler_{i:03d}(payload):")
        lines.append(f'    """Handler number {i}."""')
        for j in range(body):
            lines.append(f"    step_{j} = payload + {i} * {j}  # padding to make the file big")
        lines.append(f"    return step_{body - 1}")
        lines.append("")
    lines.append("class Registry:")
    lines.append("    def register(self, fn):")
    lines.append("        self.fns.append(fn)")
    lines.append("")
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return text.count("\n") + (0 if text.endswith("\n") else 1)


# ── the point of the change ────────────────────────────────────────────

async def test_big_file_returns_index_line_count_and_range_instruction(ws):
    src = ws / "big.py"
    total = write_py(src, funcs=175)
    assert total > 3000 and src.stat().st_size > MAX_READ_CHARS

    out = (await read(src))["output"]

    assert f"{total} lines" in out                    # the total, not just what is shown
    assert "=== SYMBOLS" in out
    assert "def handler_000" in out and "def handler_174" in out
    assert "class Registry" in out                    # the far end of the file is indexed
    assert "Registry.register" in out                 # methods too, not only top level
    assert '"offset"' in out and '"limit"' in out     # the literal call to ask for more
    assert "read_file {" in out

    # Every index row is `line: name`, and every line number is real.
    body = out.split("=== SYMBOLS", 1)[1].split("===", 2)[2]
    file_lines = src.read_text(encoding="utf-8").split("\n")
    rows = [r for r in out.split("=== SYMBOLS", 1)[1].split("\n") if ": " in r and r.strip()[0].isdigit()]
    assert len(rows) > 100
    for row in rows[:40]:
        num, name = row.split(": ", 1)
        assert name.split()[-1].split("(")[0].split(".")[-1] in file_lines[int(num) - 1]
    assert body  # the file's own first lines follow the index


async def test_no_section_is_cut_in_half(ws):
    src = ws / "big.py"
    write_py(src, funcs=175)
    out = (await read(src))["output"]

    # The shown body is whole lines of the real file, in order, starting at 1.
    shown = out.split("=== LINES 1-", 1)[1].split("\n", 1)[1]
    shown = shown.split("\n\n... ")[0]
    original = src.read_text(encoding="utf-8")
    assert original.startswith(shown)
    assert shown.split("\n")[-1] in original.split("\n")

    # And the index ends on a complete entry, never a half-written name.
    for row in out.split("=== SYMBOLS", 1)[1].split("=== LINES", 1)[0].strip().split("\n"):
        assert row.startswith("  ") or ": " in row


async def test_symbol_index_reuses_repo_map_extraction(ws):
    """The outline is repo_map's extractor with line numbers, not a second one."""
    src = ws / "big.py"
    write_py(src, funcs=20)
    found = dict((n, ln) for n, ln in read_plan.outline(str(src)))
    assert found["def handler_000"] == 5
    # Every symbol the repo map reports for this file is in the outline, at a line.
    text = src.read_text(encoding="utf-8")
    repo_names = {s.split("(")[0].replace("class ", "").replace("def ", "")
                  for s in repo_map._py_symbols(text)}
    outline_names = {n.split("(")[0].replace("class ", "").replace("def ", "").replace("async ", "")
                     for n in found}
    assert repo_names <= outline_names
    assert "Registry.register" in found          # and the outline adds what navigation needs


# ── nothing else may change ────────────────────────────────────────────

@pytest.mark.parametrize("name,text", [
    ("small.py", "def a():\n    return 1\n"),
    ("empty.py", ""),
    ("no_newline.py", "x = 1"),
    ("plain.txt", "hello\nworld\n"),
    ("under_cap.py", "# pad\n" * 2000),
])
async def test_small_file_output_is_byte_identical(ws, name, text):
    src = ws / name
    src.write_text(text, encoding="utf-8", newline="")
    assert src.stat().st_size <= MAX_READ_CHARS
    assert (await read(src))["output"] == legacy_read(str(src))


async def test_file_just_under_the_cap_is_untouched(ws):
    src = ws / "edge.py"
    src.write_text("a = 1  # pad\n" * (MAX_READ_CHARS // 13 - 1), encoding="utf-8")
    assert src.stat().st_size < MAX_READ_CHARS
    out = (await read(src))["output"]
    assert out == legacy_read(str(src))
    assert "=== SYMBOLS" not in out


@pytest.mark.parametrize("offset,limit", [(1, 5), (400, 120), (2000, 0), (0, 30)])
async def test_explicit_range_is_unchanged(ws, offset, limit):
    src = ws / "big.py"
    write_py(src, funcs=175)
    args = {}
    if offset:
        args["offset"] = offset
    if limit:
        args["limit"] = limit
    out = (await read(src, **args))["output"]
    assert out == legacy_read(str(src), offset=offset, limit=limit)
    assert "=== SYMBOLS" not in out


async def test_unreadable_and_missing_paths_keep_todays_errors(ws):
    missing = await read(ws / "nope.txt")
    assert missing["exit_code"] == 1 and missing.get("not_found") is True
    assert "does not exist" in missing["error"]

    (ws / "adir").mkdir()
    isdir = await read(ws / "adir")
    assert isdir["exit_code"] == 1 and "is a directory (use ls)" in isdir["error"]

    if os.name != "nt" and os.geteuid() != 0:
        locked = ws / "locked.py"
        locked.write_text("x = 1\n" * 20000, encoding="utf-8")
        locked.chmod(0o000)
        try:
            denied = await read(locked)
            assert denied["exit_code"] == 1 and "permission denied" in denied["error"]
        finally:
            locked.chmod(0o644)


# ── the window decides the cap ─────────────────────────────────────────

def test_small_window_lowers_the_cap_a_large_one_does_not():
    assert read_plan.budget_chars(8192, fraction=0.25) == 8192
    assert read_plan.budget_chars(128000, fraction=0.25) == MAX_READ_CHARS
    assert read_plan.budget_chars(8192, 0.25) < read_plan.budget_chars(128000, 0.25)
    # An unproven window changes nothing at all — the ceiling that always applied.
    assert read_plan.budget_chars(0) == MAX_READ_CHARS
    # Nonsense settings fall back rather than producing a nonsense cap.
    assert read_plan.budget_chars(8192, fraction="nope") == 8192
    assert read_plan.budget_chars(8192, fraction=-3) == 8192
    assert read_plan.budget_chars(1024, fraction=0.25) == read_plan.MIN_BUDGET_CHARS


async def test_8k_window_returns_less_than_a_128k_one(ws):
    src = ws / "big.py"
    write_py(src, funcs=175)
    small = read_plan.plan(str(src), 8192, fraction=0.25)
    large = read_plan.plan(str(src), 128000, fraction=0.25)
    assert small.budget_chars == 8192 and large.budget_chars == MAX_READ_CHARS
    assert len(small.output) <= small.budget_chars
    assert len(large.output) <= large.budget_chars
    assert small.total_lines == large.total_lines
    assert small.symbols and small.head_lines >= 1


async def test_outline_can_be_switched_off(ws):
    src = ws / "big.py"
    write_py(src, funcs=175)
    off = read_plan.plan(str(src), 0, enabled=False)
    assert off.output is None and off.oversized is False


# ── degrading gracefully ───────────────────────────────────────────────

async def test_file_without_symbols_still_gets_facts_and_the_range_call(ws):
    log = ws / "app.log"
    rows = ["2026-08-30 12:00:%02d INFO worker=%d handled request in %d ms"
            % (i % 60, i % 8, i) for i in range(4000)]
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    out = (await read(log))["output"]
    assert "=== SYMBOLS" not in out                   # nothing to index, so no index
    assert "4000 lines" in out and "KB" in out        # but the facts are there
    assert "read_file {" in out and '"offset"' in out # and how to ask for the rest
    assert "truncated at" in out
    # A file with no index keeps the whole budget for content: at least as much
    # of the file as the old blind cut showed, minus the framing.
    shown = out.split("=== LINES 1-", 1)[1].split("\n", 1)[1]
    assert len(shown) > MAX_READ_CHARS * 0.9
    assert log.read_text(encoding="utf-8").startswith(shown.split("\n\n... ")[0])


async def test_binary_file_does_not_raise(ws):
    blob = ws / "payload.bin"
    blob.write_bytes(bytes(range(256)) * 200)
    r = await read(blob)
    assert r["exit_code"] == 0 and r["output"]


# ── encodings ──────────────────────────────────────────────────────────

async def test_crlf_file_counts_lines_and_shows_no_carriage_returns(ws):
    src = ws / "windows.py"
    body = "\r\n".join(f"def fn_{i}():\r\n    return {i}" for i in range(900))
    src.write_bytes(body.encode("utf-8"))
    out = (await read(src))["output"]

    assert "1800 lines" in out                        # counted on the LF form, as read_file reads it
    assert "\r" not in out
    assert "def fn_000" not in out and "def fn_0" in out
    assert "=== SYMBOLS" in out


async def test_unicode_and_emoji_survive_whole(ws):
    src = ws / "unicode.py"
    lines = ['# -*- coding: utf-8 -*-', 'GREETING = "hola señor 🚀 añadido"', '']
    for i in range(1500):
        lines.append(f'def función_{i}():')
        lines.append(f'    return "🎉 línea {i} — ünïcödé ✨ 中文 اللغة"')
        lines.append("")
    src.write_text("\n".join(lines), encoding="utf-8")
    assert src.stat().st_size > MAX_READ_CHARS

    out = (await read(src))["output"]
    assert "�" not in out                        # no character was cut in half
    assert "🚀" in out and "hola señor 🚀 añadido" in out
    assert "🎉 línea 0 — ünïcödé ✨ 中文 اللغة" in out
    assert "def función_0" in out
    # Characters, not bytes: the cap is a character count and the file is
    # multi-byte, so the reply must be inside the cap measured in characters.
    assert len(out) <= MAX_READ_CHARS


async def test_multibyte_file_under_the_cap_in_characters_is_returned_whole(ws):
    """st_size is bytes; the cap is characters. An emoji file whose bytes exceed
    the cap but whose characters do not must still come back untouched."""
    src = ws / "emoji.txt"
    src.write_text("🚀" * 9000 + "\n", encoding="utf-8")
    assert src.stat().st_size > MAX_READ_CHARS
    assert (await read(src))["output"] == legacy_read(str(src))


# ── wiring ─────────────────────────────────────────────────────────────

async def test_read_file_actually_calls_read_plan(ws, monkeypatch):
    """A module nobody calls is nothing delivered."""
    src = ws / "big.py"
    write_py(src, funcs=175)
    calls = []
    real = read_plan.plan

    def spy(abs_path, window_tokens=0, **kw):
        calls.append((abs_path, window_tokens, kw))
        return real(abs_path, window_tokens, **kw)

    # Patched on the module object the tool actually holds — other tests in the
    # suite reload src.agent_tools, so a dotted-string target is not reliable.
    assert fs_tools.read_plan is read_plan
    monkeypatch.setattr(fs_tools.read_plan, "plan", spy)
    out = (await read(src))["output"]
    assert len(calls) == 1
    assert calls[0][0] == os.path.realpath(str(src))
    assert calls[0][2]["display_path"] == str(src)
    assert "=== SYMBOLS" in out

    # …and is not called for an explicit range.
    calls.clear()
    await read(src, offset=10, limit=5)
    assert calls == []


async def test_read_file_uses_the_resolved_window(ws, monkeypatch):
    src = ws / "big.py"
    write_py(src, funcs=175)
    monkeypatch.setattr(fs_tools.read_plan, "resolve_window_tokens", lambda ctx=None: 8192)
    out = (await read(src))["output"]
    assert "cap 8192 chars" in out
    assert len(out) <= 8192


def test_window_resolution_never_probes_the_endpoint(monkeypatch):
    """A 20 s /v1/models call has no business inside a file read."""
    import src.model_context as mc

    def explode(*a, **k):
        raise AssertionError("read_plan must not probe for the context window")

    monkeypatch.setattr(mc, "_query_context_length", explode)
    monkeypatch.setattr(mc, "_context_cache", {}, raising=False)
    assert read_plan.resolve_window_tokens({}) == 0
    assert read_plan.resolve_window_tokens({"context_length": 16384}) == 16384
    monkeypatch.setattr(mc, "_context_cache", {("http://x", "m"): (32768, True)}, raising=False)
    assert read_plan.resolve_window_tokens(None) == 32768
    # An unproven window (the bare DEFAULT_CONTEXT fallback) is not trusted.
    monkeypatch.setattr(mc, "_context_cache", {("http://x", "m"): (128000, False)}, raising=False)
    assert read_plan.resolve_window_tokens(None) == 0


def test_settings_defaults_are_declared():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_read_outline"] is True
    assert DEFAULT_SETTINGS["agent_read_window_fraction"] == read_plan.DEFAULT_FRACTION


# ── the repo map's own output must not have moved ──────────────────────

PY_SAMPLE = '''\
import os

MAX_THINGS = 12
_PRIVATE_CONST = 3


class Registry:
    def __init__(self):
        self.items = []

    def add(self, item):
        return self.items.append(item)

    def _hidden(self):
        pass


def public_entry(a):
    def inner_helper(b):
        return b
    return inner_helper(a)


async def _private_entry():
    return None
'''

JS_SAMPLE = '''\
export function topLevel(a) {
  function nestedHelper(b) { return b; }
  return nestedHelper(a);
}

const arrowThing = (x) => x + 1;

class Widget {
  render() { return 1; }
}

  function indentedInsideAWrapper() { return 2; }
'''


def test_repo_map_rendering_is_unchanged():
    """The one-line-per-file summary the repo map injects every turn.

    Locked down because read_plan reuses these extractors: a change made for the
    outline must not quietly rewrite what every turn's repository map says.
    """
    assert repo_map._py_symbols(PY_SAMPLE) == [
        "class Registry(add, _hidden)",
        "def public_entry",
        "MAX_THINGS",
    ]
    assert repo_map._regex_symbols(JS_SAMPLE, repo_map._LANG_RES["js"]) == [
        "topLevel", "Widget", "arrowThing",
    ]


def test_outline_adds_lines_privates_and_nested_without_a_second_extractor():
    py = dict(repo_map.symbol_lines(PY_SAMPLE, "py"))
    assert py["class Registry"] == 7
    assert py["Registry.add"] == 11                  # methods, with their own line
    assert py["Registry._hidden"] == 14              # private ones too
    assert py["def public_entry"] == 18
    assert py["public_entry.inner_helper"] == 19     # one level into a long function
    assert py["async def _private_entry"] == 24      # the repo map hides these; the outline needs them
    assert py["MAX_THINGS"] == 3

    js = dict(repo_map.symbol_lines(JS_SAMPLE, "js"))
    assert js["topLevel"] == 1 and js["Widget"] == 8 and js["arrowThing"] == 6
    assert js["nestedHelper"] == 2
    # The same anchored patterns, run over a dedented view, find what a wrapper hides.
    assert js["indentedInsideAWrapper"] == 12

    assert repo_map.symbol_lines("a,b,c\n1,2,3\n", "none") == []
    assert repo_map.lang_for_path("x.py") == "py" and repo_map.lang_for_path("x.log") == "none"


def test_unparseable_python_falls_back_instead_of_raising(tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text("def ok():\n    pass\n\ndef (((:\n" + "# pad\n" * 6000, encoding="utf-8")
    names = dict(read_plan.outline(str(broken)))
    assert names["ok"] == 1                          # regex fallback, still with a line


async def test_source_file_with_nothing_to_index_degrades_like_a_log(ws):
    """A .py that is pure data has no symbols either — same graceful degrade."""
    data = ws / "table.py"
    data.write_text("\n".join(f'    ("row {i}", {i}, "value number {i} padding"),' for i in range(2000)),
                    encoding="utf-8")
    out = (await read(data))["output"]
    assert "=== SYMBOLS" not in out
    assert "2000 lines" in out and "read_file {" in out
    assert "truncated at" in out


async def test_index_too_long_for_the_budget_is_trimmed_whole(ws):
    src = ws / "many.py"
    src.write_text("\n".join(f"def symbol_with_a_fairly_long_name_{i:04d}():\n    return {i}\n"
                             for i in range(1200)), encoding="utf-8")
    p = read_plan.plan(str(src), 8192, fraction=0.25)
    assert p.oversized and len(p.output) <= 8192
    assert "more symbols further down the file" in p.output
    # trimmed between entries, never mid-name
    rows = p.output.split("=== SYMBOLS", 1)[1].split("=== LINES", 1)[0].strip().split("\n")
    for row in rows[1:-1]:
        assert row.split(": ", 1)[1].startswith("def symbol_with_a_fairly_long_name_")
    assert p.head_lines >= 1                          # the body never disappears entirely
