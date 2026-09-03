"""Rule packs over a worker's own output (src/output_rules.py).

The claim under test: you can name what a worker is doing — rate-limited,
sitting at a prompt, repeating itself, out of disk — from the TAIL of its
output alone, with substrings and a confirming regex, deterministically, and
without ever raising into the job that called you.

What is pinned here:
  * every state's pack fires on the wording it exists for;
  * the tail-only rule — a match far up the scrollback is history, not state;
  * the repeated-line heuristic, and the caller's own "no new bytes" signal;
  * the matched literal comes back, so the UI shows WHY, not just WHAT;
  * ordinary build output matches nothing;
  * determinism, and totality on junk.
"""
from __future__ import annotations

import pytest

from src import output_rules
from src.output_rules import classify_output, tail, tail_delta

# What a worker's log really looks like when nothing is wrong.
BUILD_OUTPUT = """\
$ npm run build
> app@1.4.2 build
> tsc -p tsconfig.json && vite build

vite v5.0.11 building for production...
transforming (243) node_modules/lodash-es/lodash.js
✓ 1204 modules transformed.
dist/index.html                   0.46 kB │ gzip:  0.30 kB
dist/assets/index-4f29a1.js     142.19 kB │ gzip: 46.02 kB
✓ built in 11429 ms
9 passed, 0 failed
"""


def _states(text, **kw):
    return classify_output(text, **kw)["states"]


# ── every state's pack ──────────────────────────────────────────────────────

@pytest.mark.parametrize("state,text", [
    ("rate_limited", "POST /v1/messages\nHTTP 429 Too Many Requests"),
    ("rate_limited", "error: rate limit exceeded, retry in 60s"),
    ("rate_limited", "Error: quota exceeded for this billing period"),
    ("rate_limited", "usage limit reached — try again after 5pm"),
    ("waiting_for_input", "rm -rf build/\nOverwrite existing file? [y/N] "),
    ("waiting_for_input", "connecting to db1\nPassword:"),
    ("waiting_for_input", "Delete 3 branches (yes/no)?"),
    ("waiting_for_input", "installer ready\nPress any key to continue . . ."),
    ("waiting_for_input", "$ ls\ntotal 0\n$"),
    ("waiting_for_input", "python3\n>>>"),
    ("auth_error", "GET /api/v2/repos → 401"),
    ("auth_error", "fatal: could not read Username: Permission denied"),
    ("auth_error", "you are not authorized to push to this remote"),
    ("disk_full", "cp: error writing 'big.bin': No space left on device"),
    ("disk_full", "OSError: [Errno 28] ENOSPC"),
    ("oom", "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB"),
    ("oom", "make -j16\nKilled"),
    ("oom", "the kernel killed process 4412 (node)"),
    ("finished_ok", "9 passed in 1.4s\nProcess finished with exit code 0"),
    ("failed", "pytest -q\n1 failed\nexit code: 1"),
    ("failed", "the container exited with status 137"),
])
def test_every_pack_fires_on_the_wording_it_exists_for(state, text):
    verdict = classify_output(text)
    assert state in verdict["states"], verdict
    assert verdict["confidence"] > 0.5
    assert any(m["state"] == state for m in verdict["matches"])


def test_the_states_are_the_documented_set_and_are_reported_in_order():
    assert output_rules.STATES == ("rate_limited", "waiting_for_input", "stuck", "auth_error",
                                   "disk_full", "oom", "finished_ok", "failed")
    # a log that trips two packs reports them in STATES order, not in the
    # order they happen to appear in the text
    both = classify_output("no space left on device\nHTTP 429 Too Many Requests")
    assert both["states"] == ["rate_limited", "disk_full"]
    # the states a caller must never kill for
    assert output_rules.BLOCKED_STATES == ("rate_limited", "waiting_for_input", "stuck")
    assert output_rules.blocked(classify_output("429 too many requests")) == ["rate_limited"]
    assert output_rules.blocked(classify_output("exit code 0")) == []
    assert output_rules.blocked("not a verdict") == []


# ── the tail is the whole point ─────────────────────────────────────────────

def test_a_match_above_the_tail_window_is_history_not_state():
    """The report's point: you must not re-scan the scrollback. A rate limit
    400 KB ago is not what the worker is doing now."""
    old = "HTTP 429 Too Many Requests\n" + "".join(f"compiling module {i}\n" for i in range(2_000))
    assert len(old) > output_rules.TAIL_BYTES * 4
    assert _states(old) == []
    # the same words INSIDE the window are reported
    assert _states("x" * 100 + "\nHTTP 429 Too Many Requests") == ["rate_limited"]
    # and a caller may widen the window on purpose
    assert _states(old, tail_bytes=len(old) + 1) == ["rate_limited"]
    assert tail(old) == old[-output_rules.TAIL_BYTES:]
    assert tail("short") == "short"


def test_a_prompt_only_counts_on_the_last_line():
    """`[y/N]` quoted in the middle of a log is documentation; on the last
    line it is a worker that will never finish on its own."""
    quoted = "the flag prints Continue? [y/N] and waits\nnow running the suite\n9 passed\n"
    assert "waiting_for_input" not in _states(quoted)
    assert _states(quoted + "Continue? [y/N] ") == ["waiting_for_input"]


def test_tail_delta_hands_back_only_the_new_bytes():
    assert tail_delta(0, "abcdef") == "abcdef"
    assert tail_delta(3, "abcdef") == "def"
    assert tail_delta(6, "abcdef") == ""
    # a stream that shrank (a restarted command) is new from the start:
    # classify too much once rather than miss a state
    assert tail_delta(99, "abc") == "abc"
    assert tail_delta("junk", "abc") == "abc"
    assert tail_delta(2, None) == ""
    assert tail_delta(-5, "ab") == "ab"


# ── stuck ───────────────────────────────────────────────────────────────────

def test_the_same_line_repeated_at_the_tail_reads_as_stuck():
    text = "starting\nwaiting for lock\nwaiting for lock\nwaiting for lock"
    verdict = classify_output(text)
    assert verdict["states"] == ["stuck"]
    assert verdict["matches"][0]["literal"] == "waiting for lock"
    assert "3 times" in verdict["matches"][0]["line"]
    # twice is not a loop
    assert "stuck" not in _states("starting\nwaiting for lock\nwaiting for lock")
    # …unless the caller says so
    assert "stuck" in _states("starting\nwaiting for lock\nwaiting for lock", repeats=2)
    # the repetition must be AT the tail: an old loop that moved on is not it
    assert "stuck" not in _states("retry\nretry\nretry\nconnected\ndone")


def test_no_new_bytes_is_the_callers_own_signal_and_needs_no_text():
    """Whether a stream has produced anything since the last check is the one
    thing text cannot say about itself, so the caller supplies it."""
    verdict = classify_output("still working…", no_new_bytes=True)
    assert verdict["states"] == ["stuck"]
    assert verdict["matches"][0]["line"] == "no new output since the last check"
    assert verdict["confidence"] == 0.9
    assert classify_output("", no_new_bytes=True)["states"] == ["stuck"]
    assert classify_output("still working…")["states"] == []


# ── the matched literal is the evidence ─────────────────────────────────────

def test_the_matched_literal_and_its_line_come_back():
    verdict = classify_output("GET /v1/models\nresponse: 429 slow down there\n")
    match = verdict["matches"][0]
    assert match["state"] == "rate_limited"
    assert match["literal"] == "429"
    assert match["line"] == "response: 429 slow down there"
    assert 0.0 < match["confidence"] <= 1.0
    assert output_rules.why(verdict) == "response: 429 slow down there"
    assert output_rules.why(verdict, "oom") == ""
    assert output_rules.why(None) == "" and output_rules.why({"matches": "junk"}) == ""
    # the literal is bounded even when the line is not
    long_line = "x" * 5000 + " no space left on device"
    assert len(classify_output(long_line)["matches"][0]["line"]) <= output_rules.LINE_CHARS


def test_the_last_occurrence_is_the_news():
    """A log that hit a rate limit and then a disk error reports the LATEST
    line of each pack, not the first one it ever saw."""
    verdict = classify_output("429 first time\nstill going\n429 second time\n")
    assert verdict["matches"][0]["line"] == "429 second time"


# ── the false-positive floor ────────────────────────────────────────────────

def test_ordinary_build_output_matches_nothing():
    verdict = classify_output(BUILD_OUTPUT)
    assert verdict == {"states": [], "matches": [], "confidence": 0.0}


@pytest.mark.parametrize("text", [
    "built in 11429 ms",                       # 429 inside a number
    "wrote 401kB to dist/",                    # 401 inside a number
    "OK: 403214 rows exported",                # 403 inside a number
    "compiling module 429 of 900",             # a bounded number that is not a status
    "linking object 401.o",
    "flag --continue? see the docs",           # a prompt-ish word, not a prompt
    "little space left on the canvas",
    "exit code is documented in README",       # the words, no code
    "the killed_jobs table has 4 rows",        # `killed` as a word, not the OOM line
])
def test_the_gate_does_not_fire_on_numbers_and_prose(text):
    assert classify_output(text)["states"] == []


def test_packs_can_be_restricted_to_the_rules_a_caller_cares_about():
    text = "HTTP 429 Too Many Requests\nPassword:"
    assert _states(text) == ["rate_limited", "waiting_for_input"]
    assert _states(text, packs=["rate_limited"]) == ["rate_limited"]
    assert _states(text, packs=["oom", "disk_full"]) == []
    assert _states(text, packs=[]) == ["rate_limited", "waiting_for_input"]      # empty = all
    assert _states(text, packs=["not_a_state"]) == []
    assert _states(text, packs=42) == ["rate_limited", "waiting_for_input"]      # junk = all


def test_known_states_validates_a_wait_condition():
    assert output_rules.known_states(["stuck", "nope", "OOM"]) == ["stuck", "oom"]
    assert output_rules.known_states(None) == [] and output_rules.known_states(7) == []


# ── pure, deterministic, total ──────────────────────────────────────────────

def test_the_same_input_always_gives_the_same_verdict():
    text = BUILD_OUTPUT + "\nHTTP 429 Too Many Requests\nretry\nretry\nretry"
    first = classify_output(text)
    for _ in range(5):
        assert classify_output(text) == first
    assert first["states"] == ["rate_limited", "stuck"]


@pytest.mark.parametrize("junk", [
    None, 0, 12.5, b"", b"no space left on device", bytearray(b"429 too many requests"),
    {"a": 1}, [1, 2, 3], object(), "\x00\x00�", "\n" * 500, "é" * 9000,
])
def test_junk_never_raises_and_answers_the_empty_verdict_shape(junk):
    verdict = classify_output(junk)
    assert set(verdict) == {"states", "matches", "confidence"}
    assert isinstance(verdict["states"], list) and isinstance(verdict["matches"], list)
    assert isinstance(verdict["confidence"], float)


def test_a_broken_rule_cannot_escape_into_the_caller(monkeypatch):
    """Whatever goes wrong inside, a classifier answers a verdict — the job
    that called it must never fail over a hint."""
    def boom(*a, **kw):
        raise RuntimeError("rules exploded")
    monkeypatch.setattr(output_rules, "_lines_at_tail", boom)
    assert classify_output("anything at all") == {"states": [], "matches": [], "confidence": 0.0}


def test_nothing_here_touches_the_disk_or_the_network():
    """Pure by construction: the module imports `re` and typing and nothing
    else — no I/O to fail, no dependency to be wrong about."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(output_rules.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported == {"re", "typing", "__future__"}, imported
