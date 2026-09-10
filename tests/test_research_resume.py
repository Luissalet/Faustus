"""Lote 8 — resume an interrupted research from its last confirmed checkpoint.

TASK-02 / QA-10: `deep_research.py` now checkpoints after every confirmed
round and every written final-report part; the checkpoint survives a
restart (recover_interrupted flips the marker to "interrupted" without
losing it, and `_write_marker`'s own coarser writes must not erase it
either); `POST /api/research/{id}/resume` starts a NEW run seeded from that
checkpoint — prior findings/citations, `max_rounds` reduced by the rounds
already done, already-issued queries excluded, already-written report parts
reused — and stamps `resumed_from`/`resumed_kept` on the saved result.
Compatibility: a marker with no checkpoint (the pre-existing "interrupted +
Retry from scratch" behaviour of restart_survival) still 409s on /resume,
unchanged.
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src import research_handler
from src.research_handler import ResearchHandler
from src.deep_research import DeepResearcher
from src.research_citations import SourceRegistry
from routes.research_routes import setup_research_routes

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# DeepResearcher-level: the checkpoint itself
# ---------------------------------------------------------------------

def _finding(tag: str) -> dict:
    return {"url": f"https://example.test/{tag}", "title": tag, "summary": "s", "evidence": "e"}


def test_checkpoint_fires_once_per_confirmed_round_with_trimmed_findings():
    """Every round that reaches SYNTHESIZE checkpoints exactly once, with
    rounds_done/queries_done growing and no raw HTML — just url/title/extract
    (COMUN rule: findings can be large, keep only what resume needs)."""
    checkpoints = []
    r = DeepResearcher("http://unused.invalid", "m", max_rounds=2, min_rounds=1,
                       checkpoint_callback=checkpoints.append)
    r._create_plan = AsyncMock(return_value="plan")
    r._classify_category = AsyncMock(return_value="")
    r._should_stop = AsyncMock(return_value=False)
    r._final_report = AsyncMock(return_value="FINAL")
    rounds = {"queries": [["q1", "q2"], ["q3"]], "n": 0}

    async def gen_queries(question, report, round_num):
        out = rounds["queries"][rounds["n"]]
        rounds["n"] += 1
        r.queries_used.update(out)  # the real _generate_queries does this too
        return out
    r._generate_queries = gen_queries

    async def search_and_extract(queries, question):
        return [_finding(q) for q in queries]
    r._search_and_extract = search_and_extract

    async def synth(question, findings, current_report):
        return f"report with {len(findings)} findings"
    r._synthesize = synth

    asyncio.run(r.research("What is the effect of X on Y?"))

    assert len(checkpoints) == 2
    assert [c["rounds_done"] for c in checkpoints] == [1, 2]
    assert [f["url"] for f in checkpoints[0]["findings"]] == ["https://example.test/q1", "https://example.test/q2"]
    assert checkpoints[0]["findings"][0].keys() == {"url", "title", "summary", "evidence"}
    assert checkpoints[1]["queries_done"] == sorted(["q1", "q2", "q3"])
    assert checkpoints[0]["report_parts"] == []
    assert checkpoints[1]["sources"]["version"] == 1
    assert len(checkpoints[1]["sources"]["sources"]) == 3
    assert checkpoints[1]["evolving_report"] == "report with 3 findings"


def test_report_parts_checkpoint_and_a_resumed_run_skips_written_parts():
    """`_final_report_in_parts` checkpoints after every part, and a resume
    that already has part 1 does not spend an LLM call rewriting it."""
    r = DeepResearcher("http://unused.invalid", "m")
    r.subquestions = [f"Section {i}" for i in range(1, 9)]  # 6 + 2 -> two parts
    r.report_language = "en"
    checkpoints = []
    r._checkpoint_cb = checkpoints.append
    calls = []

    async def _llm(messages, **k):
        calls.append(messages[-1]["content"])
        return f"## part {len(calls)} [1]"
    r._llm = _llm
    r._emit = lambda **kw: None

    out = asyncio.run(r._final_report("q", "evolving"))
    assert len(calls) == 2
    assert [c["report_parts"] for c in checkpoints] == [["## part 1 [1]"], ["## part 1 [1]", "## part 2 [1]"]]

    # Resume: part 1 is already on disk — only part 2 is regenerated.
    r2 = DeepResearcher("http://unused.invalid", "m")
    r2.subquestions = r.subquestions
    r2.report_language = "en"
    calls2 = []

    async def _llm2(messages, **k):
        calls2.append(messages[-1]["content"])
        return "## part 2 redo [1]"
    r2._llm = _llm2
    r2._emit = lambda **kw: None

    out2 = asyncio.run(r2._final_report("q", "evolving", prior_parts=["## part 1 [1]"]))
    assert len(calls2) == 1
    assert "part 1 [1]" in out2 and "part 2 redo [1]" in out2
    assert out.count("## part") == out2.count("## part") == 2


def test_final_report_without_prior_parts_keeps_the_old_two_arg_call():
    """A caller/test that stubs `_final_report` with a plain 2-argument
    `lambda q, rep: ...` (e.g. test_deep_research_same_pages_again.py) must
    keep working: `research()` only adds the `prior_parts` kwarg when there
    is something to resume. FAILS without that guard — `research()` used to
    always pass `prior_parts=...`, which such a lambda cannot accept."""
    r = DeepResearcher("http://unused.invalid", "m", max_rounds=1, min_rounds=1)
    r._create_plan = AsyncMock(return_value="plan")
    r._classify_category = AsyncMock(return_value="")
    r._generate_queries = AsyncMock(return_value=["q1"])
    r._search_and_extract = AsyncMock(return_value=[_finding("q1")])
    r._synthesize = AsyncMock(return_value="report body")
    r._should_stop = AsyncMock(return_value=False)
    async def _ready(value):
        return value
    # The old two-arg contract: a plain lambda whose call returns an
    # awaitable, accepting no `prior_parts` kwarg — same shape as
    # test_deep_research_same_pages_again.py's `_final_report` stub.
    r._final_report = lambda q, rep: _ready("FINAL")

    result = asyncio.run(r.research("q?"))
    assert result.strip() == "FINAL"


def test_round_one_of_a_continuation_gets_the_follow_up_instruction():
    """`round_num` restarts at 1 for a continuation (chat follow-up, or a
    resumed run) that already carries a non-empty `report`. Before the fix,
    round 1 always got the "first round, broad queries" instruction, so a
    resumed run's round 1 asked for the same broad queries `queries_used`
    then filtered to nothing. FAILS without the `and not report` guard."""
    r = DeepResearcher("http://unused.invalid", "m")
    r.research_plan = "plan"
    seen = {}

    async def _llm(messages, **k):
        seen["prompt"] = messages[-1]["content"]
        return "[]"
    r._llm = _llm

    asyncio.run(r._generate_queries("q?", "", 1))
    assert "first round" in seen["prompt"].lower()

    seen.clear()
    asyncio.run(r._generate_queries("q?", "Findings gathered before the restart.", 1))
    assert "already have partial findings" in seen["prompt"].lower()
    assert "first round" not in seen["prompt"].lower()


# ---------------------------------------------------------------------
# ResearchHandler-level: the checkpoint on disk survives everything else
# that touches the marker
# ---------------------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "deep_research"
    d.mkdir()
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", d)
    monkeypatch.setattr("routes.research_routes.DEEP_RESEARCH_DIR", str(d))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    return d


def _handler():
    h = ResearchHandler.__new__(ResearchHandler)
    h._active_tasks = {}
    return h


def test_write_marker_preserves_an_existing_checkpoint(data_dir):
    """A checkpoint written mid-run must survive every later `_write_marker`
    call for the same session (cancel, timeout, error) — those write a fresh
    status dict from the in-memory entry, which knows nothing of the
    checkpoint. FAILS without the `current.get("checkpoint")` carry-over:
    the marker's status/error update would silently drop the checkpoint,
    and a run that failed after 3 good rounds would offer no Resume at all."""
    h = _handler()
    entry = {"query": "q", "status": "running", "started_at": 1.0, "owner": "luis", "max_rounds": 6}
    h._write_marker("rp-cp", entry)
    checkpoint = {"rounds_done": 3, "findings": [_finding("a")], "queries_done": ["a"],
                  "report_parts": [], "evolving_report": "draft", "sources": {"version": 1, "sources": []},
                  "updated_at": 10.0}
    h._write_checkpoint("rp-cp", checkpoint)

    # Simulates the hard-timeout / error branch of `_run`, which re-writes
    # the marker with status="error" from the in-memory entry alone.
    h._write_marker("rp-cp", {**entry, "status": "error", "error": "boom"})

    saved = research_handler._research_json_path("rp-cp")
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert data["checkpoint"] == checkpoint


def test_write_checkpoint_never_tramples_a_saved_report(data_dir):
    h = _handler()
    path = data_dir / "rp-done.json"
    path.write_text('{"query": "q", "status": "done", "result": "# Report", "owner": "luis"}', encoding="utf-8")

    h._write_checkpoint("rp-done", {"rounds_done": 1, "findings": [], "queries_done": [],
                                     "report_parts": [], "evolving_report": "", "sources": {}, "updated_at": 0})

    assert json.loads(path.read_text(encoding="utf-8")).get("checkpoint") is None


def test_list_interrupted_reports_a_checkpoint_summary_for_resume(data_dir):
    h = _handler()
    entry = {"query": "q", "status": "running", "started_at": 1.0, "owner": "luis", "max_rounds": 6}
    h._write_marker("rp-with-cp", entry)
    h._write_checkpoint("rp-with-cp", {
        "rounds_done": 3, "findings": [_finding("a"), _finding("b")], "queries_done": ["a", "b"],
        "report_parts": ["## part 1"], "evolving_report": "draft", "sources": {"version": 1, "sources": []},
        "updated_at": 0,
    })
    h._write_marker("rp-without-cp", {**entry, "status": "running"})
    h.recover_interrupted()

    items = {i["session_id"]: i for i in h.list_interrupted("luis")}
    assert items["rp-with-cp"]["checkpoint"] == {"rounds_done": 3, "sources": 2, "report_parts": 1}
    assert "checkpoint" not in items["rp-without-cp"]


# ---------------------------------------------------------------------
# QA-10: the full resume flow, no real model or search involved
# ---------------------------------------------------------------------

class _CrashingResearcher:
    """Three rounds worth of checkpoints, then never returns — a process
    killed mid-round, not a caught exception (a caught one would fall
    through to `_fallback_research`, which is not what a restart looks
    like)."""

    def __init__(self, **kwargs):
        self._checkpoint_cb = kwargs.get("checkpoint_callback")
        self.findings = []
        self.evolving_report = ""
        self.citations = SourceRegistry()

    async def research(self, query, **kw):
        for i, q in enumerate(["q1", "q2", "q3"], start=1):
            f = _finding(q)
            self.findings.append(f)
            self.citations.add(f)
            self.evolving_report = f"draft after round {i}"
            self._checkpoint_cb({
                "rounds_done": i, "findings": list(self.findings),
                "queries_done": ["q1", "q2", "q3"][:i], "report_parts": [],
                "evolving_report": self.evolving_report,
                "sources": self.citations.snapshot(), "updated_at": 0,
            })
            await asyncio.sleep(0)
        await asyncio.sleep(3600)  # the restart happens here — never reached again

    def get_stats(self):
        return {}


def test_qa10_resume_excludes_completed_rounds_and_stamps_resumed_from(data_dir, monkeypatch):
    monkeypatch.setattr("src.deep_research.DeepResearcher", _CrashingResearcher)
    h = _handler()
    monkeypatch.setattr(h, "_probe_endpoint", AsyncMock())
    monkeypatch.setattr(h, "_ensure_search_backend", AsyncMock())

    async def drive_original():
        h.start_research("rp-orig", "widget market", "http://unused.invalid", "m",
                         owner="alice", category="howto", max_rounds=6)
        for _ in range(10):
            await asyncio.sleep(0)
    asyncio.run(drive_original())

    # --- "the server restarts": a fresh handler instance recovers markers ---
    h2 = _handler()
    assert h2.recover_interrupted() == 1
    interrupted = h2.list_interrupted("alice")
    assert interrupted[0]["session_id"] == "rp-orig"
    assert interrupted[0]["checkpoint"] == {"rounds_done": 3, "sources": 3, "report_parts": 0}

    # --- resume: a real run whose DeepResearcher.research() we can inspect ---
    seen_kwargs = {}

    class _ResumedResearcher:
        def __init__(self, **kwargs):
            self.findings = []

        async def research(self, query, **kw):
            seen_kwargs.update(kw)
            return "Final report [1]."

        def get_stats(self):
            return {}
    monkeypatch.setattr("src.deep_research.DeepResearcher", _ResumedResearcher)
    monkeypatch.setattr("routes.research_routes.resolve_endpoint",
                        lambda *a, **k: ("http://resumed.invalid", "m2", {}))
    monkeypatch.setattr(h2, "_probe_endpoint", AsyncMock())
    monkeypatch.setattr(h2, "_ensure_search_backend", AsyncMock())

    router = setup_research_routes(h2)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/{session_id}/resume")
    from types import SimpleNamespace
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice"))

    # A Task created by start_research() is bound to the loop it was created
    # on; the resume call and waiting for that task out to completion must
    # therefore share one asyncio.run(), not two.
    async def resume_and_finish():
        out = await target(session_id="rp-orig", request=request)
        task = h2._active_tasks[out["session_id"]]["task"]
        await task
        return out
    out = asyncio.run(resume_and_finish())

    assert out["resumed_from"] == "rp-orig"
    assert out["resumed_kept"] == {"rounds_done": 3, "sources": 3, "report_parts": 0}
    new_sid = out["session_id"]
    assert new_sid != "rp-orig"

    # The old interrupted card is gone — its checkpoint was handed off.
    assert h2.list_interrupted("alice") == []

    # The completed rounds are NOT repeated: the resumed run was told to
    # exclude exactly the queries and URLs the checkpoint already covered.
    assert seen_kwargs["prior_queries"] == {"q1", "q2", "q3"}
    assert seen_kwargs["prior_urls"] == {f"https://example.test/{q}" for q in ("q1", "q2", "q3")}
    assert [f["url"] for f in seen_kwargs["prior_findings"]] == [f"https://example.test/{q}" for q in ("q1", "q2", "q3")]
    assert seen_kwargs["prior_report"] == "draft after round 3"

    # max_rounds(6) - rounds_done(3) = 3 for the resumed run.
    assert h2._active_tasks[new_sid]["max_rounds"] == 3

    saved = research_handler._research_json_path(new_sid)
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["status"] == "done"
    assert data["resumed_from"] == "rp-orig"
    assert data["resumed_kept"] == {"rounds_done": 3, "sources": 3, "report_parts": 0}


def test_resume_returns_409_when_the_marker_has_no_checkpoint(data_dir):
    """Compatibility: the pre-existing restart_survival behaviour (an
    interrupted marker with nothing confirmed yet) keeps offering only
    Retry-from-scratch — /resume 409s instead of starting an empty run."""
    h = _handler()
    h._write_marker("rp-empty", {"query": "q", "status": "running", "started_at": 1.0, "owner": "alice"})
    h.recover_interrupted()

    router = setup_research_routes(h)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/{session_id}/resume")
    from types import SimpleNamespace
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice"))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="rp-empty", request=request))
    assert exc.value.status_code == 409


def test_resume_rejects_a_different_owner(data_dir):
    h = _handler()
    h._write_marker("rp-bob", {"query": "q", "status": "running", "started_at": 1.0, "owner": "bob"})
    h._write_checkpoint("rp-bob", {"rounds_done": 1, "findings": [_finding("a")], "queries_done": ["a"],
                                    "report_parts": [], "evolving_report": "d",
                                    "sources": {"version": 1, "sources": []}, "updated_at": 0})
    h.recover_interrupted()

    router = setup_research_routes(h)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/{session_id}/resume")
    from types import SimpleNamespace
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice"))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="rp-bob", request=request))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------
# Studio: the interrupted card's three actions
# ---------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_the_resume_adapter_maps_checkpoints_and_the_409():
    result = subprocess.run(["node", "studio/checks/research-resume.check.mjs"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok research-resume" in result.stdout


def test_the_screen_offers_three_actions_on_a_checkpointed_interrupted_card():
    """Source-level contract for the three-button card (mirrors how
    test_studio_research_restart_js.py pins the two-button one): Resume only
    when there is a checkpoint, Retry from scratch reuses the existing
    `launch` action untouched, Create alternative queues a new job without
    touching the interrupted one, and the checkpoint summary line is shown.
    Also pins the restart_survival compatibility contract this builds on:
    dismissResearch keeps its two no-checkpoint call sites and gains the two
    that give a checkpoint up (Retry from scratch, Dismiss)."""
    screen = (ROOT / "studio/src/screens/research/Research.tsx").read_text(encoding="utf-8")
    assert screen.count("void dismissResearch(") == 4
    assert "job.status === 'interrupted'" in screen
    assert "void resume(job)" in screen
    assert "void launch({ ...job, sessionId: null })" in screen  # Retry from scratch, reused as-is
    assert "createAlternative(job)" in screen
    assert "checkpointLine(job.checkpoint)" in screen
    adapter = (ROOT / "studio/src/adapters/research.ts").read_text(encoding="utf-8")
    assert "export async function resumeResearch" in adapter
    assert "checkpoint?: ResearchCheckpoint" in adapter
    route = (ROOT / "routes/research/research_routes.py").read_text(encoding="utf-8")
    assert '@router.post("/api/research/{session_id}/resume")' in route
