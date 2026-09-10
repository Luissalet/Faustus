"""A server restart does not make a running research vanish.

10-09-2026: runs lived only in `_active_tasks`. After a restart the screen's
card said "The research failed." under the last round message, and a
reloaded screen showed nothing at all. A marker JSON now records a run
from its first second; the next start-up turns every marker still
"running" into "interrupted", /api/research/active reports those, and
the screen shows a failed card with Retry, then dismisses the marker.
"""
import asyncio
import json
import time

import pytest

from src import research_handler
from src.research_handler import ResearchHandler


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "deep_research"
    d.mkdir()
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", d)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    return d


def _handler():
    h = ResearchHandler.__new__(ResearchHandler)
    h._active_tasks = {}
    return h


def _entry(**over):
    e = {"query": "whiplash rehab", "status": "running", "progress": {"model": "m"},
         "error": "", "started_at": 1000.0, "category": None, "owner": "luis"}
    e.update(over)
    return e


def test_a_start_leaves_a_marker_on_disk(data_dir, monkeypatch):
    async def _never(self, *a, **k):
        await asyncio.sleep(3600)
    monkeypatch.setattr(ResearchHandler, "call_research_service", _never)
    h = _handler()

    async def go():
        h.start_research("rp-marker", "whiplash rehab", "http://127.0.0.1:11434/v1", "m", owner="luis")
        await asyncio.sleep(0)
        h.cancel_research("rp-marker")
        await asyncio.sleep(0)
    asyncio.run(go())

    data = json.loads((data_dir / "rp-marker.json").read_text(encoding="utf-8"))
    assert data["marker"] is True
    assert data["query"] == "whiplash rehab"
    assert data["owner"] == "luis"
    # Cancelled by hand: the marker says so, and a restart will not call it interrupted.
    assert data["status"] == "cancelled"


def test_a_marker_never_tramples_a_saved_report(data_dir):
    h = _handler()
    path = data_dir / "rp-done.json"
    path.write_text(json.dumps({"query": "q", "status": "done", "result": "# Report", "owner": "luis"}), encoding="utf-8")

    h._write_marker("rp-done", _entry())

    assert json.loads(path.read_text(encoding="utf-8"))["result"] == "# Report"


def test_restart_turns_running_markers_into_interrupted(data_dir):
    h = _handler()
    h._write_marker("rp-lost", _entry())
    h._write_marker("rp-failed", _entry(status="error", error="boom"))
    (data_dir / "rp-report.json").write_text(json.dumps({"query": "q", "status": "done", "result": "# R", "owner": "luis"}), encoding="utf-8")

    assert h.recover_interrupted() == 1

    lost = json.loads((data_dir / "rp-lost.json").read_text(encoding="utf-8"))
    assert lost["status"] == "interrupted"
    assert "restarted" in lost["error"]
    assert lost["interrupted_at"] >= 1000.0
    assert json.loads((data_dir / "rp-failed.json").read_text(encoding="utf-8"))["status"] == "error"
    assert json.loads((data_dir / "rp-report.json").read_text(encoding="utf-8"))["status"] == "done"


def test_interrupted_runs_are_listed_for_their_owner_only(data_dir):
    h = _handler()
    h._write_marker("rp-mine", _entry())
    h._write_marker("rp-theirs", _entry(owner="ana"))
    h.recover_interrupted()

    mine = h.list_interrupted("luis")

    assert [x["session_id"] for x in mine] == ["rp-mine"]
    assert mine[0]["status"] == "interrupted"
    assert mine[0]["progress"]["phase"] == "error"
    assert "restarted" in mine[0]["progress"]["message"]
    assert h.list_interrupted("ana") and h.list_interrupted("bob") == []


def test_status_from_disk_carries_the_reason(data_dir):
    h = _handler()
    h._write_marker("rp-lost", _entry())
    h.recover_interrupted()

    status = h.get_status("rp-lost")

    assert status["status"] == "interrupted"
    assert status["progress"]["phase"] == "error"
    assert "restarted" in status["error"]


def test_dismiss_forgets_a_marker_but_never_a_report(data_dir):
    h = _handler()
    h._write_marker("rp-lost", _entry())
    h.recover_interrupted()
    (data_dir / "rp-report.json").write_text(json.dumps({"query": "q", "status": "done", "result": "# R", "owner": "luis", "marker": True}), encoding="utf-8")

    assert h.dismiss_interrupted("rp-lost", "ana") is False
    assert h.dismiss_interrupted("rp-lost", "luis") is True
    assert h.list_interrupted("luis") == []
    assert h.dismiss_interrupted("rp-report", "luis") is False
    assert (data_dir / "rp-report.json").exists()


def test_a_fresh_handler_recovers_on_construction(data_dir, monkeypatch):
    _handler()._write_marker("rp-lost", _entry())
    monkeypatch.setattr(ResearchHandler, "_initialize_legacy_engine", lambda self: None)

    h = ResearchHandler()

    assert h.list_interrupted("luis")[0]["session_id"] == "rp-lost"
    assert time.time() - json.loads((data_dir / "rp-lost.json").read_text(encoding="utf-8"))["interrupted_at"] < 60
