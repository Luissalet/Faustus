"""Observations (src/observations, routes/observations_routes.py, mcp_servers/observations_server.py).

The small model writes one structured note per useful tool call; the notes are found
again in three steps (search -> timeline -> details). Covered here:

* the reply parser: valid, skip, garbage (nothing half-valid is stored);
* the deterministic pre-filter and the condensed job;
* storage, FTS5 search and its LIKE fallback, timeline neighbours, the details cap,
  the file lookup, pruning, owner scoping;
* the session recap and how it is trimmed to a measured budget;
* the worker: a bounded queue that never blocks the caller, a model that fails or
  answers garbage, "not now" admission, the off switch;
* the lifecycle-hook listeners (post_tool, turn_start, turn_end, session_start);
* the HTTP routes and the MCP server (in process and over a real stdio pipe);
* the settings group and its schema entries.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from src import lifecycle_hooks as lh
from src import settings as settings_mod
from src.observations import capture, listeners, retrieval, schema, store, worker

REAL_ENSURE_WORKER = worker.ensure_worker
T0 = 1_760_000_000.0   # a fixed instant; tests that group by day pin TZ=UTC


# -- fixtures and helpers ---------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """A private database, empty queue and turn state, no thread started behind a test's back."""
    from src import constants
    monkeypatch.setattr(constants, "OBSERVATIONS_DB", str(tmp_path / "observations.db"))
    store.reset_for_tests()
    worker.reset_for_tests()
    listeners.reset_for_tests()
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    yield
    worker.shutdown()
    lh._LISTENERS.clear()
    worker.reset_for_tests()


@pytest.fixture
def cfg(monkeypatch):
    """Settings overrides on top of DEFAULT_SETTINGS; mutate the returned dict."""
    overrides: dict = {}
    base = dict(settings_mod.DEFAULT_SETTINGS)
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {**base, **overrides})
    return overrides


def reply(**fields) -> str:
    body = {"type": "change", "title": "Changed the thing", "subtitle": "It works now",
            "facts": ["Fixed config.py"], "narrative": "Edited config.py.", "concepts": ["gotcha"],
            "files_read": [], "files_modified": []}
    body.update(fields)
    return json.dumps(body)


def make(title="Title", ts=T0, *, project="proj", owner="", session="s1", turn="t1", type="change",
         facts=None, narrative="", subtitle="", read=(), modified=(), workspace=""):
    obs = schema.Observation(type=type, title=title, subtitle=subtitle, facts=list(facts or [f"fact of {title}"]),
                             narrative=narrative, files_read=list(read), files_modified=list(modified))
    return store.add_observation(obs, owner=owner, session_id=session, turn_id=turn, project=project,
                                 workspace=workspace, created=ts, tool="bash")


def run(coro):
    return asyncio.run(coro)


def fake_model(text):
    async def call(messages, *, owner="", max_tokens=0):
        return text(messages) if callable(text) else text
    return call


def drain():
    """Take every queued job out of the queue (the worker thread is off in tests)."""
    jobs = []
    with worker._COND:
        while worker._QUEUE:
            jobs.append(worker._QUEUE.popleft())
    return jobs


def post_tool_ctx(tool="bash", content='{"command": "pytest -q"}', result=None, **extra):
    ctx = {"tool": tool, "content": content, "paths": [], "workspace": "/ws", "session_id": "s1",
           "owner": "ada", "project_id": "", "incognito": False,
           "result": result if result is not None else {"stdout": "3 passed in 0.2s", "exit_code": 0}}
    ctx.update(extra)
    return ctx


# -- schema: the reply parser -----------------------------------------------------

def test_a_valid_reply_becomes_an_observation():
    parsed = schema.parse_observation_reply(reply(files_read=["a.py"], files_modified=["b\\c.py"]))
    assert isinstance(parsed, schema.Observation)
    assert parsed.type == "change" and parsed.title == "Changed the thing"
    assert parsed.facts == ["Fixed config.py"] and parsed.concepts == ["gotcha"]
    assert parsed.files_read == ["a.py"] and parsed.files_modified == ["b/c.py"]


def test_a_reply_inside_a_fence_or_after_reasoning_is_found():
    fenced = "Here you go:\n```json\n" + reply() + "\n```\nHope it helps"
    assert isinstance(schema.parse_observation_reply(fenced), schema.Observation)
    thought = "<think>maybe {skip} ... {oops</think>\n" + reply(title="After the thought")
    assert schema.parse_observation_reply(thought).title == "After the thought"
    assert isinstance(schema.parse_observation_reply(("pre " + reply(), "model")), schema.Observation)


def test_a_skip_reply_is_a_skip_with_its_reason():
    parsed = schema.parse_observation_reply('{"skip": true, "reason": "noise"}')
    assert isinstance(parsed, schema.Skip) and parsed.reason == "noise"
    assert isinstance(schema.parse_observation_reply('{"skip": true}'), schema.Skip)


@pytest.mark.parametrize("raw", [
    "", None, "no json here", "[1, 2, 3]", "{broken", '{"type": "change"}',
    reply(type="bug"), reply(type=""), reply(title=""), reply(title="   "),
    reply(facts=[], narrative=""), reply(facts="", narrative=" "),
    json.dumps({"title": "x", "facts": ["y"]}),
])
def test_garbage_is_dropped_not_half_stored(raw):
    assert schema.parse_observation_reply(raw) is None


def test_sizes_are_clipped_and_unknown_concepts_dropped():
    parsed = schema.parse_observation_reply(reply(
        title="T" * 400, facts=[f"fact number {i}" for i in range(30)] + ["fact number 3"],
        concepts=["gotcha", "nonsense", "GOTCHA", "pattern"], narrative="n" * 5000))
    assert len(parsed.title) <= schema.MAX_TITLE
    assert len(parsed.facts) == schema.MAX_FACTS
    assert parsed.concepts == ["gotcha", "pattern"]
    assert len(parsed.narrative) <= schema.MAX_NARRATIVE


def test_the_type_is_case_insensitive_but_must_be_known():
    assert schema.parse_observation_reply(reply(type="BugFix")).type == "bugfix"
    assert set(schema.TYPES) == {"bugfix", "feature", "refactor", "change", "discovery", "decision", "other"}


def test_summary_replies():
    good = json.dumps({"request": "fix it", "investigated": "logs", "learned": "a race", "completed": "patched",
                       "next_steps": "add a test"})
    parsed = schema.parse_summary_reply(good)
    assert isinstance(parsed, schema.TurnSummary) and parsed.completed == "patched"
    assert isinstance(schema.parse_summary_reply('{"skip": true, "reason": "empty"}'), schema.Skip)
    for bad in ("", "nope", '{"request": "only the request"}', '{"learned": "no request"}', "[]"):
        assert schema.parse_summary_reply(bad) is None


# -- capture: the pre-filter and the condensed job ---------------------------------

@pytest.mark.parametrize("tool,content,result,reason", [
    ("ls", '{"path": "."}', {"output": "a\nb"}, "listing_tool"),
    ("glob", '{"pattern": "*.py"}', {"output": "a.py"}, "listing_tool"),
    ("todowrite", "{}", {"todos": []}, "listing_tool"),
    ("mcp__observations__obs_search", "{}", {"output": "x" * 80}, "observation_tool"),
    ("obs_details", "{}", {"output": "x" * 80}, "observation_tool"),
    ("bash", '{"command": "ls -la"}', {"stdout": "total 0", "exit_code": 0}, "looking_around"),
    ("bash", '{"command": "cd src && pwd"}', {"stdout": "/x", "exit_code": 0}, "looking_around"),
    ("bash", "git status", {"stdout": "clean", "exit_code": 0}, "looking_around"),
    ("bash", '{"command": "pytest"}', {"stdout": ""}, "empty_result"),
    ("read_file", '{"path": "a.py"}', {"content": "x = 1"}, "tiny_read"),
    ("read_file", '{"path": "a.py"}', {"error": "not found"}, "failed_read"),
    ("grep", '{"pattern": "x"}', {"error": "boom", "exit_code": 2}, "failed_read"),
    ("bash", '{"command": "rm -rf x"}', {"approval_required": True}, "refused"),
    ("write_file", "{}", {"status": "blocked", "output": "no"}, "refused"),
])
def test_the_prefilter_drops_what_cannot_teach_anything(tool, content, result, reason):
    assert capture.noise_reason(tool, content, result, session_id="x", tracker=capture.RepeatTracker()) == reason


@pytest.mark.parametrize("tool,content,result", [
    ("bash", '{"command": "pytest -q"}', {"stdout": "3 passed", "exit_code": 0}),
    ("bash", '{"command": "cd src && pytest -q"}', {"stdout": "1 failed", "exit_code": 1}),
    ("bash", '{"command": "make build"}', {"stdout": "built", "exit_code": 0}),
    ("read_file", '{"path": "a.py"}', {"content": "def f():\n    return 1\n" * 10}),
    ("write_file", '{"path": "a.py"}', {"success": True, "path": "a.py", "size": 12}),
    ("edit_file", '{"path": "a.py"}', {}),
    ("mcp__github__open_pr", "{}", {"results": "opened #12 with a body that says something"}),
])
def test_the_prefilter_keeps_what_may_teach_something(tool, content, result):
    assert capture.noise_reason(tool, content, result, session_id="x", tracker=capture.RepeatTracker()) is None


def test_the_same_call_with_the_same_outcome_is_observed_once_but_a_new_outcome_is_new():
    tracker = capture.RepeatTracker()
    call = ("bash", '{"command": "pytest"}')
    failing = {"stdout": "1 failed", "exit_code": 1}
    passing = {"stdout": "all passed", "exit_code": 0}
    assert capture.noise_reason(*call, failing, session_id="a", tracker=tracker) is None
    assert capture.noise_reason(*call, failing, session_id="a", tracker=tracker) == "repeat"
    assert capture.noise_reason(*call, passing, session_id="a", tracker=tracker) is None
    assert capture.noise_reason(*call, failing, session_id="b", tracker=tracker) is None   # another chat


def test_the_repeat_tracker_is_bounded():
    tracker = capture.RepeatTracker(window=3, max_sessions=2)
    for i in range(10):
        tracker.seen_before("a", f"fp{i}")
    assert tracker.seen_before("a", "fp0") is False     # fell out of the window
    for sid in ("a", "b", "c"):
        tracker.seen_before(sid, "x")
    assert len(tracker._seen) == 2


def test_the_job_is_condensed_and_redacted():
    ctx = post_tool_ctx(
        tool="bash", content=json.dumps({"command": "curl -H 'Authorization: Bearer abc123SECRET' x " + "y" * 900}),
        result={"stdout": "ok " * 1000 + " api_key=\"sk-ABCDEF123456\"", "exit_code": 0},
        paths=[])
    job = capture.build_job(ctx, turn_id="t9", ts=5.0)
    assert job["kind"] == "observation" and job["turn_id"] == "t9" and job["project"] == "/ws"
    assert len(job["args"]) <= capture.ARGS_CHARS and len(job["result"]) <= capture.RESULT_CHARS + 10
    assert "abc123SECRET" not in job["args"]
    assert "sk-ABCDEF123456" not in job["result"]


def test_files_come_from_the_tool_name():
    assert capture.files_from_call("read_file", ["a.py"]) == (["a.py"], [])
    assert capture.files_from_call("edit_file", ["a.py"]) == ([], ["a.py"])
    assert capture.files_from_call("apply_patch", ["a.py", "b.py"]) == ([], ["a.py", "b.py"])
    assert capture.files_from_call("bash", ["a.py"]) == ([], [])
    assert capture.files_from_call("read_file", None) == ([], [])


def test_result_excerpt_keeps_head_and_tail_of_a_long_result():
    text = "HEAD" + "m" * 5000 + "TAIL"
    excerpt = capture.result_excerpt({"stdout": text, "exit_code": 1}, limit=200)
    assert len(excerpt) <= 220 and "HEAD" in excerpt and "TAIL" in excerpt and "[exit 1]" in excerpt


# -- store: writes, search, timeline, details -------------------------------------

def test_an_observation_round_trips():
    obs_id = make("Fixed the cache key", facts=["cache.py builds keys from the tenant id"], narrative="Changed it.",
                  type="bugfix", read=["src/a.py"], modified=["src/b.py"])
    [record] = store.details([obs_id])
    assert record["title"] == "Fixed the cache key" and record["type"] == "bugfix"
    assert record["facts"] == ["cache.py builds keys from the tenant id"]
    assert record["files_read"] == ["src/a.py"] and record["files_modified"] == ["src/b.py"]
    assert record["created"].endswith("Z") and record["ref"] == str(obs_id) and record["tool"] == "bash"


def test_the_same_title_in_the_same_chat_is_stored_once():
    assert make("Same", session="s1") is not None
    assert make("Same", session="s1") is None
    assert make("Same", session="s2") is not None


def test_search_returns_the_index_shape_ranked_and_capped():
    for i in range(30):
        make(f"Retry policy tweak {i}", ts=T0 + i, facts=["the retry budget lives in client.py"])
    make("Unrelated", ts=T0 + 99, facts=["colors"])
    hits = store.search("retry budget", limit=100)
    assert len(hits) == store.SEARCH_MAX == 20
    assert set(hits[0]) == {"id", "created", "type", "title"}
    assert all("retry" in h["title"].lower() for h in hits)
    assert store.search("retry budget", limit=3)[:3] == hits[:3]


def test_search_finds_accents_prefixes_and_falls_back_to_any_word():
    make("Cambio de configuración", facts=["La configuración vive en settings.py"])
    make("Other", facts=["alpha beta"])
    assert [h["title"] for h in store.search("configuracion")] == ["Cambio de configuración"]
    assert [h["title"] for h in store.search("configur")] == ["Cambio de configuración"]
    assert [h["title"] for h in store.search("alpha zzzunknown")] == ["Other"]
    assert store.search("zzzunknown") == []


def test_search_filters_by_type_project_owner_and_since():
    make("a", ts=T0, type="bugfix", project="p1", owner="ada", facts=["shared word"])
    make("b", ts=T0 + 10, type="feature", project="p2", owner="bob", facts=["shared word"])
    assert [h["title"] for h in store.search("shared", kind="bugfix")] == ["a"]
    assert [h["title"] for h in store.search("shared", project="p2")] == ["b"]
    assert [h["title"] for h in store.search("shared", owner="ada")] == ["a"]
    assert [h["title"] for h in store.search("shared", since=T0 + 5)] == ["b"]
    assert len(store.search("shared")) == 2


@pytest.mark.parametrize("query", ['"unterminated', "AND OR NOT", "foo*bar", "col:umn", "()", "-x", "a" * 500, "%_\\"])
def test_search_text_is_never_interpreted_as_fts_syntax(query):
    make("anything", facts=["words"])
    assert isinstance(store.search(query), list)


def test_search_without_fts5_uses_like_with_the_same_shape(monkeypatch):
    monkeypatch.setattr(store, "_FTS_DDL", "CREATE VIRTUAL TABLE nope USING not_a_module(x)")
    store.reset_for_tests()
    assert store.fts_available() is False
    make("Retry policy", facts=["backoff is exponential"])
    make("Other", facts=["100% done_now"])
    assert [h["title"] for h in store.search("backoff exponential")] == ["Retry policy"]
    assert [h["title"] for h in store.search("100% done_now")] == ["Other"]     # % and _ are literal
    assert store.search("nothing") == []


def test_an_empty_query_is_the_most_recent():
    make("old", ts=T0)
    make("new", ts=T0 + 100)
    assert [h["title"] for h in store.search("")] == ["new", "old"]
    assert [h["title"] for h in store.recent(limit=1)] == ["new"]


def seven(**kw):
    return [make(f"n{i}", ts=T0 + i * 60, **kw) for i in range(1, 8)]


def test_timeline_returns_the_neighbours_in_chronological_order():
    ids = seven()
    entries = store.timeline([ids[3]], before=2, after=2)
    assert [e["title"] for e in entries] == ["n2", "n3", "n4", "n5", "n6"]
    assert [e["anchor"] for e in entries] == [False, False, True, False, False]
    assert set(entries[0]) == {"ref", "kind", "id", "created", "type", "title", "anchor"}


def test_timeline_edges_and_sides():
    ids = seven()
    assert [e["title"] for e in store.timeline([ids[0]], before=2, after=1)] == ["n1", "n2"]
    assert [e["title"] for e in store.timeline([ids[6]], before=1, after=5)] == ["n6", "n7"]
    assert [e["title"] for e in store.timeline([ids[3]], before=0, after=0)] == ["n4"]
    assert len(store.timeline([ids[3]], before=99, after=99)) == 7


def test_timeline_stays_inside_the_project_and_owner():
    a = make("a1", ts=T0, project="p1", owner="ada")
    make("b1", ts=T0 + 10, project="p2", owner="ada")
    make("a2", ts=T0 + 20, project="p1", owner="ada")
    make("x", ts=T0 + 30, project="p1", owner="bob")
    assert [e["title"] for e in store.timeline([a], owner="ada")] == ["a1", "a2"]
    assert store.timeline([a], owner="bob") == []         # not bob's note


def test_timeline_merges_anchors_and_includes_summaries_in_the_window():
    ids = seven()
    summary_id = store.add_summary(schema.TurnSummary(request="the ask", completed="done"), project="proj",
                                   session_id="s1", turn_id="t1", created=T0 + 4 * 60 + 5)
    entries = store.timeline([ids[1], ids[5]], before=1, after=1)
    titles = [e["title"] for e in entries]
    assert titles == ["n1", "n2", "n3", "the ask", "n5", "n6", "n7"]      # n4 is nobody's neighbour
    summary = next(e for e in entries if e["kind"] == "summary")
    assert summary["ref"] == f"S{summary_id}" and summary["anchor"] is False
    assert [e["ref"] for e in store.timeline([f"S{summary_id}"], before=1, after=1)] == [
        str(ids[3]), f"S{summary_id}", str(ids[4])]


def test_timeline_ignores_unknown_anchors_and_caps_them_at_five():
    ids = seven()
    assert store.timeline(["nope", 9999, None]) == []
    many = store.timeline(ids[:7], before=0, after=0)
    assert len(many) == store.ANCHORS_MAX


def test_details_caps_at_five_skips_missing_and_opens_summaries():
    ids = [make(f"d{i}", ts=T0 + i) for i in range(8)]
    sid = store.add_summary(schema.TurnSummary(request="r", learned="l"), project="proj", created=T0)
    got = store.details(ids)
    assert [r["title"] for r in got] == ["d0", "d1", "d2", "d3", "d4"]
    mixed = store.details([ids[0], 4242, f"S{sid}", f"#{ids[1]}"])
    assert [r["ref"] for r in mixed] == [str(ids[0]), f"S{sid}", str(ids[1])]
    assert mixed[1]["kind"] == "summary" and mixed[1]["learned"] == "l"
    assert store.details([]) == [] and store.details(["x", True, None]) == []


def test_details_respects_the_owner():
    mine = make("mine", owner="ada")
    theirs = make("theirs", owner="bob")
    assert [r["title"] for r in store.details([mine, theirs], owner="ada")] == ["mine"]
    assert len(store.details([mine, theirs])) == 2        # single-user mode sees everything


def test_the_file_lookup_matches_relative_absolute_and_other_roots():
    a = make("read it", ts=T0, read=["/ws/src/app.py"], workspace="/ws")
    b = make("changed it", ts=T0 + 10, modified=["src/app.py"], workspace="/ws")
    make("other file", ts=T0 + 20, modified=["src/other.py"], workspace="/ws")
    c = make("elsewhere", ts=T0 + 30, modified=["/other/checkout/src/app.py"], workspace="/other/checkout")
    got = store.for_file("/ws/src/app.py", limit=5, workspace="/ws")
    assert [h["id"] for h in got] == [c, b, a]
    assert {h["id"]: h["role"] for h in got} == {a: "read", b: "modified", c: "modified"}
    assert [h["id"] for h in store.for_file("src\\app.py", limit=1)] == [c]
    assert store.for_file("src/missing.py") == [] and store.for_file("") == []


def test_a_file_read_and_modified_by_one_note_reports_modified():
    both = make("read then fixed", read=["stats.py"], modified=["stats.py"])
    assert [(h["id"], h["role"]) for h in store.for_file("stats.py")] == [(both, "modified")]


def test_the_file_lookup_respects_the_owner():
    make("mine", read=["a.py"], owner="ada")
    make("theirs", read=["a.py"], owner="bob")
    assert [h["title"] for h in store.for_file("a.py", owner="bob")] == ["theirs"]


def test_pruning_and_deleting_remove_the_index_and_file_rows_too():
    old = make("old note", ts=T0 - 100 * 86400, facts=["findable"], read=["a.py"])
    new = make("new note", ts=T0, facts=["findable"], read=["a.py"])
    store.add_summary(schema.TurnSummary(request="old", learned="x"), created=T0 - 100 * 86400)
    assert store.prune(0) == {"observations": 0, "summaries": 0}
    assert store.prune(30, now=T0) == {"observations": 1, "summaries": 1}
    assert [h["id"] for h in store.search("findable")] == [new]
    assert [h["id"] for h in store.for_file("a.py")] == [new]
    assert store.delete_observation(new, owner="bob") is False
    assert store.delete_observation(new) is True and store.search("findable") == []
    assert store.details([old]) == []


def test_turn_observations_and_the_stats():
    make("t1 a", ts=T0, turn="t1")
    make("t1 b", ts=T0 + 1, turn="t1")
    make("t2 a", ts=T0 + 2, turn="t2")
    assert [n["title"] for n in store.turn_observations("s1", "t1")] == ["t1 a", "t1 b"]
    last_of_t1 = store.search("t1 b")[0]["id"]
    store.add_summary(schema.TurnSummary(request="r", learned="l"), session_id="s1", turn_id="t1", to_obs_id=last_of_t1)
    assert store.turn_observations("s1", "t1") == []          # already summarised
    assert [n["title"] for n in store.turn_observations("s1")] == ["t2 a"]        # newer than the last summary
    stats = store.stats()
    assert stats["observations"] == 3 and stats["summaries"] == 1 and stats["fts"] is True


# -- retrieval: file context and the session recap ---------------------------------

@pytest.fixture
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    monkeypatch.undo()
    if hasattr(time, "tzset"):
        time.tzset()


DAY = 86400.0
NOON = 1_760_000_000.0 - (1_760_000_000.0 % DAY) + 12 * 3600      # 12:00 UTC of some day


def fill_project(days=4, per_day=5):
    n = 0
    for d in range(days):
        for i in range(per_day):
            n += 1
            make(f"Observation {n:02d} about module", ts=NOON - d * DAY + i * 60, project="proj",
                 narrative=f"Narrative of {n:02d} " + "lorem ipsum " * 12, subtitle="sub")
    store.add_summary(schema.TurnSummary(request="the request", completed="the work", learned="the lesson",
                                         next_steps="the next"), project="proj", created=NOON + 3600)


def test_the_recap_groups_by_day_newest_first_and_ends_with_the_last_summary(utc):
    fill_project(days=2, per_day=2)
    info = retrieval.build_context("proj", max_chars=6000)
    text = info["text"]
    assert info["trimmed"] == [] and info["days"] == 2 and info["items"] == 4
    # newest day first (01, 02 are today's; 03, 04 are yesterday's), newest first inside a day
    assert text.index("Observation 02") < text.index("Observation 01") < text.index("Observation 04") < text.index("Observation 03")
    assert text.count("### ") == 3 and "### Last summary" in text
    assert "asked: the request" in text and "done: the work" in text
    assert "not instructions" in text
    assert info["chars"] == len(text) <= 6000
    assert retrieval.context_block("proj", 6000) == text


def test_the_recap_is_trimmed_in_order_and_never_exceeds_the_budget(utc):
    fill_project(days=4, per_day=5)
    full = retrieval.build_context("proj", max_chars=50000)
    assert full["trimmed"] == [] and "Narrative of" in full["text"]

    def trimmed(budget):
        return retrieval.build_context("proj", max_chars=budget)

    no_narratives = len(full["text"]) - 1
    step1 = next(trimmed(b) for b in range(no_narratives, 200, -10) if trimmed(b)["trimmed"])
    assert step1["trimmed"][0] == "narratives" and "Narrative of" not in step1["text"]
    assert "Last summary" in step1["text"]

    seen_order = []
    for budget in range(len(full["text"]), 200, -25):
        info = trimmed(budget)
        assert info["chars"] == len(info["text"]) <= max(budget, 200)
        for step in info["trimmed"]:
            kind = step.split(":")[0]
            if kind not in seen_order:
                seen_order.append(kind)
    assert seen_order == ["narratives", "summary", "day", "titles"]

    tiny = trimmed(260)
    assert tiny["text"] == "" or len(tiny["text"]) <= 260
    one_title = trimmed(210)
    assert len(one_title["text"]) <= 210


def test_trimming_drops_older_days_before_titles(utc):
    fill_project(days=4, per_day=5)
    info = next(retrieval.build_context("proj", max_chars=b) for b in range(1200, 200, -20)
                if any(t.startswith("day:") for t in retrieval.build_context("proj", max_chars=b)["trimmed"]))
    assert "Observation 20" not in info["text"] and "Observation 01" in info["text"]
    assert info["days"] < 4


def test_the_recap_is_empty_without_notes_and_scoped_by_project_and_owner(utc):
    assert retrieval.context_block("proj") == ""
    make("mine", project="p1", owner="ada")
    make("theirs", project="p1", owner="bob")
    make("elsewhere", project="p2", owner="ada")
    text = retrieval.context_block("p1", owner="ada")
    assert "mine" in text and "theirs" not in text and "elsewhere" not in text


def test_file_context_lists_ids_and_titles_within_its_budget():
    for i in range(8):
        make(f"Touched app.py again {i}" + " long" * 8, ts=T0 + i, modified=["src/app.py"])
    text = retrieval.file_context_text("src/app.py", limit=5, max_chars=300)
    assert text.startswith("Earlier notes about this file") and len(text) <= 300 and "obs_details" in text
    assert retrieval.file_context_text("src/none.py") == ""


# -- worker: the model call, outcomes, the queue -----------------------------------

def a_job(**kw):
    job = {"kind": "observation", "ts": time.time(), "owner": "ada", "session_id": "s1", "turn_id": "t1",
           "workspace": "/ws", "project": "/ws", "tool": "bash", "args": "command=pytest", "result": "3 passed",
           "failed": False, "files_read": ["src/a.py"], "files_modified": [], "request": "make tests pass"}
    job.update(kw)
    return job


def test_a_good_reply_is_stored_with_the_files_the_harness_knew():
    job = a_job(files_modified=["src/known.py"])
    outcome = run(worker.process_job(job, model_call=fake_model(reply(files_modified=["src/said.py"]))))
    assert outcome == "stored"
    [hit] = store.search("config")
    [record] = store.details([hit["id"]])
    assert record["files_modified"] == ["src/said.py", "src/known.py"] and record["files_read"] == ["src/a.py"]
    assert record["session_id"] == "s1" and record["turn_id"] == "t1" and record["project"] == "/ws"
    assert worker.status()["stats"]["stored"] == 1


def test_the_prompt_carries_the_call_and_asks_for_the_schema():
    seen = {}

    def capture_prompt(messages):
        seen["prompt"] = messages[0]["content"]
        return '{"skip": true, "reason": "noise"}'

    run(worker.process_job(a_job(failed=True), model_call=fake_model(capture_prompt)))
    prompt = seen["prompt"]
    assert "Tool: bash" in prompt and "command=pytest" in prompt and "3 passed" in prompt
    assert "make tests pass" in prompt and "(the call failed)" in prompt
    assert '"skip": true' in prompt and "bugfix|feature|refactor|change|discovery|decision|other" in prompt
    assert "stays true on its own" in prompt


@pytest.mark.parametrize("text,outcome", [
    ('{"skip": true, "reason": "noise"}', "skipped"),
    ("I am a chatty model with no JSON", "garbage"),
    (reply(type="nonsense"), "garbage"),
])
def test_skips_and_garbage_store_nothing(text, outcome):
    assert run(worker.process_job(a_job(), model_call=fake_model(text))) == outcome
    assert store.stats()["observations"] == 0


def test_a_duplicate_is_counted_not_stored_twice():
    call = fake_model(reply())
    assert run(worker.process_job(a_job(), model_call=call)) == "stored"
    assert run(worker.process_job(a_job(), model_call=call)) == "duplicate"
    assert worker.status()["stats"]["duplicates"] == 1


def test_the_same_title_with_a_changed_fact_is_new_information():
    """A value that changed again under the same title is a new note; only the
    same content is a duplicate (review of r1: 20 then 25 kept only 20)."""
    first = make("Updated stats.py default", turn="t1", facts=["stats.py default_limit is 20"], modified=["stats.py"])
    second = make("Updated stats.py default", turn="t2", facts=["stats.py default_limit is 25"], modified=["stats.py"])
    assert first and second and first != second
    assert [h["id"] for h in store.search("25")] == [second]
    # same content, other wording of case/spacing/order: still a duplicate
    assert make("updated  stats.py DEFAULT", turn="t3", facts=["Stats.py default_limit is 25 "], modified=["stats.py"]) is None
    # same facts in another chat: a different chat keeps its own note
    assert make("Updated stats.py default", session="s2", facts=["stats.py default_limit is 25"], modified=["stats.py"])


def test_the_worker_keeps_a_changed_fact_and_drops_a_true_repeat():
    def answer(limit):
        return reply(title="Updated stats.py default", facts=[f"stats.py default_limit is {limit}"])
    assert run(worker.process_job(a_job(), model_call=fake_model(answer(20)))) == "stored"
    assert run(worker.process_job(a_job(), model_call=fake_model(answer(25)))) == "stored"
    assert run(worker.process_job(a_job(), model_call=fake_model(answer(25)))) == "duplicate"
    records = store.details([h["id"] for h in store.search("default_limit")])
    assert {tuple(r["facts"]) for r in records} == {
        ("stats.py default_limit is 20",), ("stats.py default_limit is 25",)}


def test_process_job_never_raises_whatever_the_model_does():
    async def boom(messages, *, owner="", max_tokens=0):
        raise RuntimeError("model fell over")

    async def not_now(messages, *, owner="", max_tokens=0):
        raise worker.NotNow("interactive_turn")

    async def nothing(messages, *, owner="", max_tokens=0):
        raise worker.ModelUnavailable("no_endpoint")

    assert run(worker.process_job(a_job(), model_call=boom)) == "error"
    assert run(worker.process_job(a_job(), model_call=not_now)) == "deferred:interactive_turn"
    assert run(worker.process_job(a_job(), model_call=nothing)) == "no_model"
    assert run(worker.process_job(a_job(), model_call=fake_model(None))) == "garbage"
    assert worker.status()["stats"]["last_blocker"] == "interactive_turn"
    assert store.stats()["observations"] == 0


def test_a_storage_failure_is_an_error_outcome(monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "add_observation", broken)
    assert run(worker.process_job(a_job(), model_call=fake_model(reply()))) == "error"


def test_an_old_job_expires_without_a_model_call():
    called = []

    async def spy(messages, *, owner="", max_tokens=0):
        called.append(1)
        return reply()

    stale = a_job(ts=time.time() - worker.JOB_TTL_S - 5)
    assert run(worker.process_job(stale, model_call=spy)) == "expired"
    assert called == [] and worker.status()["stats"]["expired"] == 1


def test_the_off_switch_stops_queueing_and_processing(cfg):
    cfg["agent_observations_enabled"] = False
    assert worker.enqueue(a_job()) is False and worker.pending() == 0
    assert run(worker.process_job(a_job(), model_call=fake_model(reply()))) == "disabled"
    assert store.stats()["observations"] == 0
    cfg["agent_observations_enabled"] = True
    assert worker.enqueue(a_job()) is True and worker.pending() == 1


def test_a_full_queue_drops_the_oldest_and_never_blocks(cfg):
    cfg["agent_observations_queue_max"] = 10
    started = time.perf_counter()
    for i in range(25):
        assert worker.enqueue(a_job(args=f"n{i}"))
    assert time.perf_counter() - started < 0.5
    assert worker.pending() == 10
    assert [j["args"] for j in drain()] == [f"n{i}" for i in range(15, 25)]
    assert worker.status()["stats"]["dropped_full"] == 15


def test_enqueue_returns_at_once_even_when_the_model_is_slow(monkeypatch):
    """The real thread and a model that takes a while: the caller is not delayed and the work still lands."""
    monkeypatch.setattr(worker, "ensure_worker", REAL_ENSURE_WORKER)

    async def slow(messages, *, owner="", max_tokens=0):
        await asyncio.sleep(0.15)
        return reply(title="Slow " + messages[0]["content"].split("Arguments: ")[1].split("\n")[0])

    monkeypatch.setattr(worker, "default_model_call", slow)
    try:
        started = time.perf_counter()
        for i in range(3):
            worker.enqueue(a_job(args=f"call {i}"))
        assert time.perf_counter() - started < 0.1
        deadline = time.time() + 10
        while time.time() < deadline and store.stats()["observations"] < 3:
            time.sleep(0.05)
        assert sorted(h["title"] for h in store.recent()) == ["Slow call 0", "Slow call 1", "Slow call 2"]
    finally:
        worker.shutdown()
    assert worker.status()["worker_alive"] is False


def test_a_blocked_job_goes_back_to_the_head_of_the_queue():
    """What the worker loop does with `deferred:`: keep the job, ahead of newer ones."""
    first, second = a_job(args="first"), a_job(args="second")
    worker.enqueue(first)
    worker.enqueue(second)
    [taken] = [worker._QUEUE.popleft()]

    async def not_now(messages, *, owner="", max_tokens=0):
        raise worker.NotNow("model_busy")

    assert run(worker.process_job(taken, model_call=not_now)).startswith("deferred")
    worker._QUEUE.appendleft(taken)
    assert [j["args"] for j in drain()] == ["first", "second"]


def test_admission_says_why_not(monkeypatch):
    from src import background_job_guard as guard
    from src.context_engine import maintenance
    monkeypatch.setattr(maintenance, "should_yield", lambda: True)
    assert worker.admission_blocker("http://127.0.0.1:8082/v1", "m") == "interactive_turn"
    monkeypatch.setattr(maintenance, "should_yield", lambda: False)
    monkeypatch.setattr(guard, "should_run_with_model", lambda *a, **k: False)
    assert worker.admission_blocker("http://127.0.0.1:8082/v1", "m") == "would_load_model"
    monkeypatch.setattr(guard, "should_run_with_model", lambda *a, **k: True)
    monkeypatch.setattr(guard, "model_busy", lambda url: True)
    monkeypatch.setattr(guard, "no_free_slot", lambda url: False)
    assert worker.admission_blocker("http://127.0.0.1:8082/v1", "m") == "model_busy"
    monkeypatch.setattr(guard, "model_busy", lambda url: False)
    assert worker.admission_blocker("http://127.0.0.1:8082/v1", "m") is None


def test_the_default_model_call_uses_the_utility_endpoint_with_reasoning_off(monkeypatch, cfg):
    import src.endpoint_resolver as resolver
    import src.llm_core as llm_core
    seen = {}
    monkeypatch.setattr(resolver, "resolve_endpoint", lambda prefix, **kw: ("http://h/v1", "tiny", {"X": "1"}))
    monkeypatch.setattr(worker, "admission_blocker", lambda url, model: None)

    async def fake_llm(**kwargs):
        seen.update(kwargs)
        return ("{}", "tiny")

    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm)
    out = run(worker.default_model_call([{"role": "user", "content": "hi"}], owner="ada"))
    assert out == "{}"
    assert seen["url"] == "http://h/v1" and seen["model"] == "tiny"
    assert seen["gen_overrides"] == {"think": False} and seen["workload"] == "background"
    assert seen["max_tokens"] <= 500 and seen["temperature"] <= 0.2

    cfg["agent_observations_model"] = "other-tiny"
    run(worker.default_model_call([{"role": "user", "content": "hi"}]))
    assert seen["model"] == "other-tiny"

    monkeypatch.setattr(worker, "admission_blocker", lambda url, model: "model_busy")
    with pytest.raises(worker.NotNow):
        run(worker.default_model_call([{"role": "user", "content": "hi"}]))
    monkeypatch.setattr(resolver, "resolve_endpoint", lambda prefix, **kw: (None, None, None))
    with pytest.raises(worker.ModelUnavailable):
        run(worker.default_model_call([{"role": "user", "content": "hi"}]))


# -- worker: the turn summary --------------------------------------------------------

SUMMARY = json.dumps({"request": "make tests pass", "investigated": "the failing test", "learned": "an off-by-one",
                      "completed": "fixed it", "next_steps": "none"})


def test_a_turn_summary_is_written_from_the_turns_notes():
    first = make("Found the off-by-one", ts=T0, turn="tt", session="s9", project="/ws", owner="ada")
    last = make("Fixed the loop bound", ts=T0 + 5, turn="tt", session="s9", project="/ws", owner="ada")
    make("Another turn", ts=T0 + 9, turn="other", session="s9", project="/ws", owner="ada")
    seen = {}

    def spy(messages):
        seen["prompt"] = messages[0]["content"]
        return SUMMARY

    job = {"kind": "summary", "ts": time.time(), "session_id": "s9", "turn_id": "tt", "request": "make tests pass",
           "owner": "ada"}
    assert run(worker.process_job(job, model_call=fake_model(spy))) == "stored"
    assert "Found the off-by-one" in seen["prompt"] and "Fixed the loop bound" in seen["prompt"]
    assert "Another turn" not in seen["prompt"]
    summary = store.last_summary(project="/ws", owner="ada")
    assert summary["request"] == "make tests pass" and summary["completed"] == "fixed it"
    assert summary["observations"] == [first, last] and summary["obs_count"] == 2
    assert run(worker.process_job(job, model_call=fake_model(SUMMARY))) == "skipped"     # once per turn


def test_a_turn_without_notes_makes_no_model_call():
    called = []

    async def spy(messages, *, owner="", max_tokens=0):
        called.append(1)
        return SUMMARY

    job = {"kind": "summary", "ts": time.time(), "session_id": "none", "turn_id": "x", "request": "r"}
    assert run(worker.process_job(job, model_call=spy)) == "skipped" and called == []


def test_summary_skip_garbage_and_switch(cfg):
    make("note", turn="tt", session="s9")
    job = {"kind": "summary", "ts": time.time(), "session_id": "s9", "turn_id": "tt", "request": "r"}
    assert run(worker.process_job(job, model_call=fake_model('{"skip": true, "reason": "empty"}'))) == "skipped"
    assert run(worker.process_job(job, model_call=fake_model("garbage"))) == "garbage"
    cfg["agent_observations_summaries"] = False
    assert run(worker.process_job(job, model_call=fake_model(SUMMARY))) == "disabled"
    assert store.last_summary() is None


# -- listeners and the lifecycle hooks -----------------------------------------------

def test_lifecycle_listeners_run_after_user_hooks_and_never_break_a_turn():
    calls = []
    lh.register_listener("post_tool", lambda ctx: calls.append(ctx["tool"]) or "note for the model", listener_id="t1")
    lh.register_listener("post_tool", lambda ctx: 1 / 0, listener_id="bad")
    lh.register_listener("post_tool", lambda ctx: None, listener_id="quiet")
    results = run(lh.run_async("post_tool", {"tool": "read_file", "result": {"content": "x"}}))
    assert calls == ["read_file"]
    assert [(r.hook_id, r.action, r.output) for r in results] == [("t1", "inject", "note for the model")]
    d = {"result": {"a": 1}}
    lh.register_listener("turn_end", lambda ctx: ctx.update(mutated=True), listener_id="mutator")
    run(lh.run_async("turn_end", d))
    assert "mutated" not in d                                    # listeners get a copy
    assert lh.unregister_listener("t1") == 1
    assert run(lh.run_async("post_tool", {"tool": "x"})) == []
    with pytest.raises(lh.HookError):
        lh.register_listener("not_an_event", lambda ctx: None, listener_id="x")


def test_listeners_run_even_when_user_hooks_are_switched_off(monkeypatch):
    monkeypatch.setattr(lh, "_get_setting", lambda key, default=None: False if key == "lifecycle_hooks_enabled" else default)
    lh.register_listener("turn_start", lambda ctx: "still here", listener_id="t1")
    assert [r.output for r in run(lh.run_async("turn_start", {}))] == ["still here"]


def test_install_registers_the_four_events_idempotently():
    listeners.install()
    listeners.install()
    for event in ("turn_start", "post_tool", "turn_end", "session_start"):
        assert [e["id"] for e in lh._LISTENERS[event]] == ["observations"]
    listeners.uninstall()
    assert all(not entries for entries in lh._LISTENERS.values())


def test_a_post_tool_event_queues_one_job_with_the_turn_and_request():
    listeners.install()
    run(lh.run_async("turn_start", {"session_id": "s1", "user_message": "make   the tests\npass"}))
    results = run(lh.run_async("post_tool", post_tool_ctx()))
    assert results == []                                        # nothing to tell the model
    [job] = drain()
    assert job["kind"] == "observation" and job["session_id"] == "s1" and job["owner"] == "ada"
    assert job["request"] == "make the tests pass" and job["turn_id"] and job["tool"] == "bash"
    assert job["args"] == "command=pytest -q" and "3 passed" in job["result"]


def test_noise_incognito_and_the_per_turn_cap_queue_nothing(cfg):
    listeners.install()
    run(lh.run_async("post_tool", post_tool_ctx(tool="ls", result={"output": "a"})))
    run(lh.run_async("post_tool", post_tool_ctx(incognito=True)))
    assert worker.pending() == 0
    stats = worker.status()["stats"]["prefiltered"]
    assert stats == {"listing_tool": 1}                         # incognito is not even counted
    cfg["agent_observations_max_per_turn"] = 3
    run(lh.run_async("turn_start", {"session_id": "s1", "user_message": "go"}))
    for i in range(6):
        run(lh.run_async("post_tool", post_tool_ctx(content=json.dumps({"command": f"make step{i}"}))))
    assert worker.pending() == 3
    assert worker.status()["stats"]["prefiltered"]["turn_cap"] == 3


def test_a_listener_error_does_not_reach_the_loop(monkeypatch):
    listeners.install()

    def broken(job, **kw):
        raise RuntimeError("queue exploded")
    monkeypatch.setattr(worker, "enqueue", broken)
    assert run(lh.run_async("post_tool", post_tool_ctx())) == []


def test_turn_end_queues_one_summary_only_when_the_turn_left_notes(cfg):
    listeners.install()
    run(lh.run_async("turn_start", {"session_id": "s1", "user_message": "empty turn"}))
    run(lh.run_async("post_tool", post_tool_ctx(tool="ls", result={"output": "a"})))
    run(lh.run_async("turn_end", {"session_id": "s1", "user_message": "empty turn"}))
    assert worker.pending() == 0

    run(lh.run_async("turn_start", {"session_id": "s1", "user_message": "real work"}))
    run(lh.run_async("post_tool", post_tool_ctx()))
    run(lh.run_async("turn_end", {"session_id": "s1", "user_message": "real work"}))
    jobs = drain()
    assert [j["kind"] for j in jobs] == ["observation", "summary"]
    assert jobs[1]["turn_id"] == jobs[0]["turn_id"] and jobs[1]["request"] == "real work" and jobs[1]["owner"] == "ada"

    cfg["agent_observations_summaries"] = False
    run(lh.run_async("turn_start", {"session_id": "s1", "user_message": "again"}))
    run(lh.run_async("post_tool", post_tool_ctx(content='{"command": "make again"}')))
    run(lh.run_async("turn_end", {"session_id": "s1", "user_message": "again"}))
    assert [j["kind"] for j in drain()] == ["observation"]


def test_sub_agents_are_observed_under_their_own_session():
    listeners.install()
    run(lh.run_async("turn_start", {"session_id": "main", "user_message": "delegate"}))
    run(lh.run_async("turn_start", {"session_id": "child", "user_message": "sub task"}))
    run(lh.run_async("post_tool", post_tool_ctx(session_id="child")))
    run(lh.run_async("post_tool", post_tool_ctx(session_id="main", content='{"command": "make other"}')))
    sessions = {j["session_id"]: j["request"] for j in drain()}
    assert sessions == {"child": "sub task", "main": "delegate"}


def test_file_context_rides_on_a_read_only_when_asked(cfg):
    make("Fixed the parser", read=["src/parser.py"], modified=["src/parser.py"], project="/ws", workspace="/ws", owner="ada")
    listeners.install()
    read = post_tool_ctx(tool="read_file", content='{"path": "src/parser.py"}', paths=["src/parser.py"],
                         result={"content": "def parse():\n    return 1\n" * 5})
    assert run(lh.run_async("post_tool", read)) == []                                  # off by default
    cfg["agent_observations_file_context"] = True
    [hit] = run(lh.run_async("post_tool", read))
    assert hit.hook_id == "observations" and "Fixed the parser" in hit.output and "obs_details" in hit.output
    other = post_tool_ctx(tool="bash", paths=["src/parser.py"])
    assert run(lh.run_async("post_tool", other)) == []                                 # only a read_file
    failed = dict(read, result={"error": "nope"})
    assert run(lh.run_async("post_tool", failed)) == []
    assert run(lh.run_async("post_tool", dict(read, incognito=True))) == []
    other_owner = dict(read, owner="bob")
    assert run(lh.run_async("post_tool", other_owner)) == []                          # bob has no notes on it
    # attach_to_result puts it where the model sees it, next to the file's content
    result = {"content": "x"}
    lh.attach_to_result(result, [hit])
    assert "Fixed the parser" in result["hook_notes"][0]["output"]


def test_the_session_recap_is_injected_only_when_asked(cfg, utc):
    fill_project(days=2, per_day=2)
    listeners.install()
    ctx = {"session_id": "s1", "workspace": "proj", "owner": "", "incognito": False, "user_message": "hi"}
    assert run(lh.run_async("session_start", ctx)) == []
    cfg["agent_observations_inject_session_start"] = True
    [hit] = run(lh.run_async("session_start", ctx))
    assert hit.name == "Recent observations" and "Observation 04" in hit.output
    assert "### " in lh.render_context([hit]) and "Recent observations (inject)" in lh.render_context([hit])
    cfg["agent_observations_inject_max_chars"] = 600
    [small] = run(lh.run_async("session_start", ctx))
    assert len(small.output) <= 600
    assert run(lh.run_async("session_start", dict(ctx, incognito=True))) == []
    assert run(lh.run_async("session_start", dict(ctx, workspace=""))) == []
    assert run(lh.run_async("session_start", dict(ctx, workspace="no-notes-here"))) == []


# -- settings ---------------------------------------------------------------------------

def test_the_settings_exist_with_safe_defaults_and_a_schema_group():
    from src import agent_settings_schema as schema_mod
    defaults = settings_mod.DEFAULT_SETTINGS
    keys = [k for k in defaults if k.startswith("agent_observations_")]
    assert set(keys) == {
        "agent_observations_enabled", "agent_observations_summaries", "agent_observations_inject_session_start",
        "agent_observations_inject_max_chars", "agent_observations_file_context", "agent_observations_queue_max",
        "agent_observations_max_per_turn", "agent_observations_model", "agent_observations_retention_days"}
    assert defaults["agent_observations_enabled"] is True
    assert defaults["agent_observations_inject_session_start"] is False        # prompt-changing parts are opt-in
    assert defaults["agent_observations_file_context"] is False
    group = next(g for g in schema_mod.GROUPS if g["id"] == "observations")
    assert {f["key"] for f in group["fields"]} == set(keys)
    assert schema_mod.schema_problems() == []
    assert schema_mod.coerce_setting_value("agent_observations_queue_max", 99999) == 1000
    assert schema_mod.coerce_setting_value("agent_observations_enabled", "off") is False


def test_a_missing_setting_store_means_the_defaults_not_a_crash(monkeypatch):
    def broken():
        raise RuntimeError("settings unreadable")
    monkeypatch.setattr(settings_mod, "load_settings", broken)
    assert worker.enabled() is True and worker.queue_limit() == 100


# -- HTTP routes --------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.observations_routes as routes
    from src.auth_helpers import require_user
    who = {"user": "ada"}
    monkeypatch.setattr(routes, "effective_user", lambda request: who["user"])
    app = FastAPI()
    app.include_router(routes.setup_observations_routes())
    app.dependency_overrides[require_user] = lambda: who["user"]
    client = TestClient(app)
    client.who = who
    return client


def test_the_three_steps_over_http(api):
    ids = [make(f"Http note {i}", ts=T0 + i * 60, owner="ada", facts=["the gateway retries twice"]) for i in range(5)]
    make("Bob's note", owner="bob", facts=["the gateway retries twice"])
    found = api.get("/api/observations/search", params={"q": "gateway retries", "limit": 3}).json()["results"]
    assert len(found) == 3 and set(found[0]) == {"id", "created", "type", "title"}
    assert all(h["title"].startswith("Http note") for h in found)
    assert api.get("/api/observations/search", params={"q": "gateway", "limit": 99}).status_code == 422
    entries = api.get("/api/observations/timeline", params={"anchor": str(ids[2]), "before": 1, "after": 1}).json()["entries"]
    assert [e["title"] for e in entries] == ["Http note 1", "Http note 2", "Http note 3"]
    records = api.get("/api/observations/details", params={"ids": f"{ids[0]},{ids[1]}"}).json()["records"]
    assert [r["title"] for r in records] == ["Http note 0", "Http note 1"] and "facts" in records[0]
    recent = api.get("/api/observations/recent", params={"limit": 2}).json()["results"]
    assert [r["title"] for r in recent] == ["Http note 4", "Http note 3"]


def test_http_validation_and_ownership(api):
    mine = make("mine", owner="ada", facts=["secret plan"])
    theirs = make("theirs", owner="bob", facts=["secret plan"])
    assert api.get("/api/observations/timeline").status_code == 400
    assert api.get("/api/observations/details").status_code == 400
    assert api.get("/api/observations/details", params={"ids": "1,2,3,4,5,6"}).status_code == 400
    assert api.get("/api/observations/search", params={"since": "garbage"}).status_code == 400
    assert api.get("/api/observations/search", params={"limit": 21}).status_code == 422
    assert api.get("/api/observations/for-file").status_code == 400
    assert api.get("/api/observations/details", params={"ids": str(theirs)}).json()["records"] == []
    assert api.get("/api/observations/timeline", params={"anchor": str(theirs)}).json()["entries"] == []
    assert [h["title"] for h in api.get("/api/observations/search", params={"q": "secret"}).json()["results"]] == ["mine"]
    assert api.delete(f"/api/observations/{theirs}").status_code == 404
    assert api.delete(f"/api/observations/{mine}").json() == {"deleted": mine}
    api.who["user"] = "bob"
    assert [h["title"] for h in api.get("/api/observations/search", params={"q": "secret"}).json()["results"]] == ["theirs"]


def test_http_since_accepts_epoch_and_iso(api):
    make("early", ts=T0, owner="ada")
    make("late", ts=T0 + 7200, owner="ada")
    assert [r["title"] for r in api.get("/api/observations/recent").json()["results"]] == ["late", "early"]
    iso_mid = store.iso(T0 + 3600)
    for since in (str(T0 + 3600), iso_mid, iso_mid.replace("Z", "")):
        found = api.get("/api/observations/search", params={"q": "", "since": since}).json()["results"]
        assert [r["title"] for r in found] == ["late"], since


def test_http_for_file_context_and_status(api, utc):
    make("Edited router", modified=["src/router.py"], owner="ada", project="/ws", workspace="/ws")
    hits = api.get("/api/observations/for-file", params={"path": "/ws/src/router.py", "workspace": "/ws"}).json()["results"]
    assert [h["title"] for h in hits] == ["Edited router"] and hits[0]["role"] == "modified"
    context = api.get("/api/observations/context", params={"project": "/ws", "max_chars": 400}).json()
    assert "Edited router" in context["text"] and context["chars"] <= 400 and context["budget"] == 400
    status = api.get("/api/observations/status").json()
    assert status["store"]["observations"] == 1 and "stats" in status["worker"] and status["store"]["fts"] is True


# -- the MCP server ----------------------------------------------------------------------

mcp = pytest.importorskip("mcp")
import mcp_servers.observations_server as srv  # noqa: E402

TOOL_NAMES = {"obs_search", "obs_timeline", "obs_details", "obs_recent"}


def call(name, arguments):
    return run(srv.call_tool(name, arguments))[0].text


@pytest.fixture(autouse=False)
def fresh_server(monkeypatch):
    monkeypatch.setattr(srv, "_initialized", False)
    monkeypatch.setattr(srv, "_engine", {})
    monkeypatch.delenv("ODYSSEUS_MCP_OBSERVATIONS_OWNER", raising=False)


def test_importing_the_server_starts_nothing():
    assert srv.server.name == "observations"


def test_the_tools_are_listed_read_only_with_bilingual_first_lines():
    tools = run(srv.list_tools())
    assert {t.name for t in tools} == TOOL_NAMES
    for tool in tools:
        first = tool.description.splitlines()[0]
        assert len(first) <= 110, (tool.name, len(first))
        assert first.rstrip().endswith("Read-only."), tool.name
        assert any(word in first.lower() for word in ("busca", "cronolog", "detalle", "notas")), tool.name
        assert tool.annotations is not None and tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        schema_ = tool.inputSchema
        assert schema_["type"] == "object"
        for field, spec in (schema_.get("properties") or {}).items():
            assert "type" in spec, f"{tool.name}.{field}"
        for required in schema_.get("required", []):
            assert required in schema_["properties"]


def test_the_three_steps_through_the_server(fresh_server):
    ids = [make(f"Mcp note {i}", ts=T0 + i * 60, facts=["token bucket refills every second"]) for i in range(4)]
    found = json.loads(call("obs_search", {"query": "token bucket"}))["results"]
    assert len(found) == 4 and set(found[0]) == {"id", "created", "type", "title"}
    entries = json.loads(call("obs_timeline", {"anchor_ids": [str(ids[1])], "before": 1, "after": 0}))["entries"]
    assert [e["title"] for e in entries] == ["Mcp note 0", "Mcp note 1"]
    records = json.loads(call("obs_details", {"ids": [ids[0], str(ids[1])]}))["records"]
    assert [r["title"] for r in records] == ["Mcp note 0", "Mcp note 1"] and records[0]["facts"]
    recent = json.loads(call("obs_recent", {"limit": 2}))["results"]
    assert [r["title"] for r in recent] == ["Mcp note 3", "Mcp note 2"]


def test_the_server_messages_instead_of_raising(fresh_server):
    assert "Unknown tool" in call("obs_nope", {})
    assert "anchor_ids" in call("obs_timeline", {})
    assert "ids needs" in call("obs_details", {})
    assert "at most 5" in call("obs_details", {"ids": list("123456")})
    assert run(srv.call_tool("obs_search", None))[0].text.startswith("{")
    assert json.loads(call("obs_search", {"query": "x", "limit": "many", "type": "bogus"}))["results"] == []


def test_the_server_reads_only_its_owners_notes(fresh_server, monkeypatch):
    make("ada's", owner="ada", facts=["shared phrase"])
    make("bob's", owner="bob", facts=["shared phrase"])
    assert len(json.loads(call("obs_search", {"query": "shared phrase"}))["results"]) == 2
    monkeypatch.setenv("ODYSSEUS_MCP_OBSERVATIONS_OWNER", "ada")
    assert [r["title"] for r in json.loads(call("obs_search", {"query": "shared phrase"}))["results"]] == ["ada's"]


def test_the_server_is_known_to_the_builtin_list_once_installed():
    from src import builtin_mcp
    assert listeners.register_mcp_server() is True
    script, name = builtin_mcp._BUILTIN_SERVERS["observations"]
    assert script == "mcp_servers/observations_server.py" and name.startswith("Built-in")
    assert os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(__file__)), script))
    builtin_mcp._BUILTIN_SERVERS.pop("observations", None)


def test_a_real_stdio_round_trip(tmp_path, monkeypatch):
    """List the tools and call two of them over the server's actual stdio pipe, in a child process."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from src import constants

    data_dir = tmp_path / "child-data"
    data_dir.mkdir()
    monkeypatch.setattr(constants, "OBSERVATIONS_DB", str(data_dir / "observations.db"))
    store.reset_for_tests()
    note = make("Stdio note", facts=["the pipe works end to end"])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params = StdioServerParameters(
        command=sys.executable, args=[os.path.join(root, "mcp_servers", "observations_server.py")],
        env={**os.environ, "ODYSSEUS_DATA_DIR": str(data_dir), "PYTHONPATH": root})

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                found = await session.call_tool("obs_search", {"query": "pipe works"})
                opened = await session.call_tool("obs_details", {"ids": [str(note)]})
                # the index hands out integer ids; a model that copies one as-is must not hit a schema error
                by_int = await session.call_tool("obs_timeline", {"anchor_ids": [note]})
                assert not by_int.isError, by_int.content[0].text
                return listed, found, opened

    listed, found, opened = asyncio.run(asyncio.wait_for(go(), timeout=60))
    assert {t.name for t in listed.tools} == TOOL_NAMES
    assert all(t.annotations and t.annotations.readOnlyHint for t in listed.tools)
    assert json.loads(found.content[0].text)["results"][0]["title"] == "Stdio note"
    assert json.loads(opened.content[0].text)["records"][0]["facts"] == ["the pipe works end to end"]


# -- through the real agent loop ------------------------------------------------------------

def test_the_agent_loop_feeds_the_recorder_and_the_note_lands(monkeypatch, tmp_path):
    """The real stream_agent_loop with a fake provider and a fake tool executor: the post_tool event
    carries the tool's result and owner, the recorder queues it, the (fake) small model answers on
    the worker thread, and the note is findable. Nothing in the loop waits for any of it."""
    import src.agent_loop as al

    settings = {}
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: settings.get(key, default), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "BUILD FAILED: missing semicolon in parser.c line 40", "exit_code": 2})

    monkeypatch.setattr(al, "execute_tool_block", fake_exec, raising=False)
    rounds = [("```bash\nmake build\n```", "tool_calls"), ("It failed on a semicolon.", "stop")]
    state = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        text, finish = rounds[min(state["n"], len(rounds) - 1)]
        state["n"] += 1
        yield "data: " + json.dumps({"delta": text}) + "\n\n"
        yield "data: " + json.dumps({"type": "finish", "finish_reason": finish}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    prompts = []

    async def small_model(messages, *, owner="", max_tokens=0):
        prompts.append(messages[0]["content"])
        return reply(type="discovery", title="The build fails in parser.c", facts=["parser.c line 40 lacks a semicolon"],
                     files_read=["parser.c"])

    monkeypatch.setattr(worker, "default_model_call", small_model)
    monkeypatch.setattr(worker, "ensure_worker", REAL_ENSURE_WORKER)
    listeners.install()

    async def drive():
        stream = al.stream_agent_loop(
            "http://127.0.0.1:11434/v1", "qwen3.5:9b",
            [{"role": "user", "content": "why does the build fail?"}],
            max_rounds=4, relevant_tools={"bash"}, security_gate_bypass=True, owner="ada", session_id="s-e2e",
            workspace=str(tmp_path), harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})
        return [chunk async for chunk in stream]

    try:
        started = time.perf_counter()
        chunks = asyncio.run(drive())
        loop_seconds = time.perf_counter() - started
        deadline = time.time() + 10
        while time.time() < deadline and store.stats()["observations"] < 1:
            time.sleep(0.05)
    finally:
        worker.shutdown()
    assert any("It failed on a semicolon" in c for c in chunks)
    [hit] = store.search("semicolon")
    [record] = store.details([hit["id"]])
    assert record["title"] == "The build fails in parser.c" and record["type"] == "discovery"
    assert record["tool"] == "bash" and record["session_id"] == "s-e2e" and record["project"] == str(tmp_path)
    assert record["files_read"] == ["parser.c"]
    [prompt] = prompts
    assert "make build" in prompt and "BUILD FAILED" in prompt and "[exit 2]" in prompt
    assert "why does the build fail?" in prompt           # the turn's request, from turn_start
    assert loop_seconds < 30


def test_the_wiring_is_in_place():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(rel):
        with open(os.path.join(root, rel), encoding="utf-8") as handle:
            return handle.read()

    app = read("app.py")
    assert "app.include_router(setup_observations_routes())" in app and "_observations.install()" in app
    loop = read("src/agent_loop.py")
    post_tool = loop[loop.index('run_async("post_tool"'):]
    post_tool = post_tool[:post_tool.index("_all_hook_results")]
    assert '"result": result' in post_tool and '"owner": owner' in post_tool and '"incognito"' in post_tool
    assert 'run_async("turn_end"' in read("routes/chat_helpers.py")
