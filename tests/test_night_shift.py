"""src/night_shift.py -- an unattended dispatch queue under a budget."""
from __future__ import annotations

import asyncio
import types

import pytest

from src import night_shift as ns


def _patch_dispatch_module(monkeypatch, fake) -> None:
    """Stand `fake` in for `src.dispatch` everywhere `night_shift.py`'s
    `from src import dispatch` could find it. Patching `sys.modules` alone
    is not enough once the REAL `src.dispatch` has already been imported in
    this process (e.g. by another test file run earlier in the same
    session): Python's `from src import dispatch` then resolves through the
    `src` package's own cached `dispatch` attribute, not the module
    registry, so that attribute needs patching too."""
    import sys
    import src as _src_pkg
    monkeypatch.setitem(sys.modules, "src.dispatch", fake)
    monkeypatch.setattr(_src_pkg, "dispatch", fake, raising=False)


# ── validate_spec() ──────────────────────────────────────────────────────

def test_validate_spec_minimal():
    spec = ns.validate_spec({"tasks": ["do x"], "workspace": "/tmp/proj"})
    assert spec["tasks"] == ["do x"]
    assert spec["workspace"] == "/tmp/proj"
    assert spec["budget"]["max_minutes"] == 120
    assert spec["budget"]["max_tasks"] == 8
    assert spec["budget"]["max_tokens"] is None
    assert spec["verify"] is True


def test_validate_spec_accepts_newline_separated_tasks_string():
    spec = ns.validate_spec({"tasks": "a\nb\n\nc", "workspace": "/tmp"})
    assert spec["tasks"] == ["a", "b", "c"]


def test_validate_spec_rejects_no_tasks():
    with pytest.raises(ValueError):
        ns.validate_spec({"tasks": [], "workspace": "/tmp"})


def test_validate_spec_rejects_too_many_tasks():
    with pytest.raises(ValueError):
        ns.validate_spec({"tasks": [f"t{i}" for i in range(13)], "workspace": "/tmp"})


def test_validate_spec_rejects_missing_workspace():
    with pytest.raises(ValueError):
        ns.validate_spec({"tasks": ["a"]})


def test_validate_spec_rejects_bad_budget():
    with pytest.raises(ValueError):
        ns.validate_spec({"tasks": ["a"], "workspace": "/tmp", "budget": {"max_minutes": 0}})
    with pytest.raises(ValueError):
        ns.validate_spec({"tasks": ["a"], "workspace": "/tmp", "budget": {"max_tasks": -1}})
    with pytest.raises(ValueError):
        ns.validate_spec({"tasks": ["a"], "workspace": "/tmp", "budget": {"max_tokens": 0}})


def test_validate_spec_reads_settings_defaults(monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: {"night_shift_default_max_minutes": 60,
                                   "night_shift_default_max_tasks": 3}.get(key, default))
    spec = ns.validate_spec({"tasks": ["a"], "workspace": "/tmp"})
    assert spec["budget"]["max_minutes"] == 60
    assert spec["budget"]["max_tasks"] == 3


# ── persistence ──────────────────────────────────────────────────────────

@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    return tmp_path


def test_save_get_and_list(data_dir):
    shift = {"id": "abc123", "owner": "u1", "started": 100, "tasks": ["a"], "results": []}
    ns._save(shift)
    assert ns.get("u1", "abc123") == shift
    assert ns.get("u1", "nope") is None
    rows = ns.list_for("u1")
    assert len(rows) == 1 and rows[0]["id"] == "abc123"


def test_latest_for_picks_the_most_recently_started(data_dir):
    ns._save({"id": "old", "owner": "u1", "started": 100, "tasks": [], "results": []})
    ns._save({"id": "new", "owner": "u1", "started": 200, "tasks": [], "results": []})
    assert ns.latest_for("u1")["id"] == "new"


def test_scoped_per_owner(data_dir):
    ns._save({"id": "x", "owner": "u1", "started": 1, "tasks": [], "results": []})
    assert ns.get("u2", "x") is None
    assert ns.list_for("u2") == []


# ── sequential execution against a fake dispatch ─────────────────────────

class _FakeJob:
    def __init__(self, job_id, status="done", verdict="ok", files=None,
                 input_tokens=0, output_tokens=0):
        self.id = job_id
        self.status = status
        self.verdict = verdict
        self._files = files or []
        self._tokens = (input_tokens, output_tokens)

    def compact_payload(self):
        return {
            "verdict": self.verdict, "status": self.status,
            "result": {
                "files_changed": self._files,
                "verification": {"mode": "auto"} if self.status == "done" else None,
                "totals": {"input_tokens": self._tokens[0], "output_tokens": self._tokens[1]},
            },
        }


def _fake_dispatch(jobs_by_task, waits=None):
    """A stand-in for `src.dispatch` good enough for night_shift: `start`
    returns the next fake job for that task text, `wait` is a no-op, `compact`
    reads back the job's own canned payload."""
    fake = types.SimpleNamespace()

    async def start(owner, body):
        text = body["tasks"][0]["instruction"]
        job = jobs_by_task[text]
        return job

    async def wait(job, timeout):
        if waits is not None:
            waits.append(timeout)
        return True

    def compact(job):
        return job.compact_payload()

    fake.start = start
    fake.wait = wait
    fake.compact = compact
    return fake


@pytest.fixture()
def fake_dispatch(monkeypatch):
    def _install(jobs_by_task, waits=None):
        fake = _fake_dispatch(jobs_by_task, waits=waits)
        _patch_dispatch_module(monkeypatch, fake)
        return fake
    return _install


async def _run_and_wait(shift_id, owner):
    task = ns._RUNNING.get(shift_id)
    if task is not None:
        await task


async def test_start_runs_every_task_sequentially(data_dir, fake_dispatch):
    jobs = {
        "t1": _FakeJob("j1", files=["a.py"]),
        "t2": _FakeJob("j2", files=["b.py"]),
    }
    fake_dispatch(jobs)
    shift = ns.start("u1", {"tasks": ["t1", "t2"], "workspace": "/tmp"})
    assert shift["state"] == "queued"
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "done"
    assert len(final["results"]) == 2
    assert final["results"][0]["files_changed"] == ["a.py"]
    assert final["results"][1]["job_id"] == "j2"


async def test_a_failed_dispatch_call_is_recorded_not_fatal(data_dir, monkeypatch):
    async def boom_start(owner, body):
        raise ValueError("no route")

    fake = types.SimpleNamespace(start=boom_start,
                                 wait=lambda job, t: None,
                                 compact=lambda job: {})
    _patch_dispatch_module(monkeypatch, fake)
    shift = ns.start("u1", {"tasks": ["t1"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "done"
    assert final["results"][0]["status"] == "error"
    assert final["results"][0]["needs_attention"] is True


async def test_budget_exhausted_by_max_tasks(data_dir, fake_dispatch):
    jobs = {f"t{i}": _FakeJob(f"j{i}") for i in range(1, 4)}
    fake_dispatch(jobs)
    shift = ns.start("u1", {"tasks": ["t1", "t2", "t3"], "workspace": "/tmp",
                            "budget": {"max_tasks": 2}})
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "budget_exhausted"
    assert len(final["results"]) == 2
    assert final["skipped"] == ["t3"]


async def test_budget_exhausted_by_minutes(data_dir, monkeypatch, fake_dispatch):
    jobs = {"t1": _FakeJob("j1"), "t2": _FakeJob("j2")}
    fake_dispatch(jobs)
    # First pre-check (task 1) reads as fresh; the second (task 2) reads as
    # already past the budget.
    elapsed = iter([0.0, 60.0])
    monkeypatch.setattr(ns, "_elapsed_minutes", lambda shift: next(elapsed))
    shift = ns.start("u1", {"tasks": ["t1", "t2"], "workspace": "/tmp",
                            "budget": {"max_minutes": 30, "max_tasks": 10}})
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "budget_exhausted"
    assert len(final["results"]) == 1


async def test_budget_exhausted_by_tokens(data_dir, fake_dispatch):
    jobs = {"t1": _FakeJob("j1", input_tokens=5000, output_tokens=5000), "t2": _FakeJob("j2")}
    fake_dispatch(jobs)
    shift = ns.start("u1", {"tasks": ["t1", "t2"], "workspace": "/tmp",
                            "budget": {"max_tokens": 5000}})
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "budget_exhausted"
    assert len(final["results"]) == 1


async def test_stop_between_tasks(data_dir, fake_dispatch, monkeypatch):
    jobs = {"t1": _FakeJob("j1"), "t2": _FakeJob("j2"), "t3": _FakeJob("j3")}
    calls = {"n": 0}

    async def start(owner, body):
        calls["n"] += 1
        if calls["n"] == 1:
            # Stop is requested while the first task is "in flight".
            ns.stop("u1", shift_id_holder["id"])
        text = body["tasks"][0]["instruction"]
        return jobs[text]

    fake = types.SimpleNamespace(start=start, wait=lambda job, t: None,
                                 compact=lambda job: job.compact_payload())
    _patch_dispatch_module(monkeypatch, fake)
    shift_id_holder = {}
    shift = ns.start("u1", {"tasks": ["t1", "t2", "t3"], "workspace": "/tmp"})
    shift_id_holder["id"] = shift["id"]
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "stopped"
    assert len(final["results"]) == 1
    assert final["skipped"] == ["t2", "t3"]


async def test_stop_returns_false_for_a_finished_or_missing_shift(data_dir, fake_dispatch):
    jobs = {"t1": _FakeJob("j1")}
    fake_dispatch(jobs)
    shift = ns.start("u1", {"tasks": ["t1"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    assert ns.stop("u1", shift["id"]) is False
    assert ns.stop("u1", "does-not-exist") is False


# ── report() ─────────────────────────────────────────────────────────────

def test_report_for_unknown_shift():
    text = ns.report("u1", "nope")
    assert "No shift found" in text


async def test_report_shape(data_dir, fake_dispatch):
    jobs = {"t1": _FakeJob("j1", files=["a.py"]), "t2": _FakeJob("j2", status="error")}
    fake_dispatch(jobs)
    shift = ns.start("u1", {"tasks": ["t1", "t2"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    text = ns.report("u1", shift["id"])
    assert "# Night shift" in text
    assert "a.py" in text
    assert "needs your attention" in text.lower()


def test_report_says_nothing_ran_when_empty(data_dir):
    ns._save({"id": "empty", "owner": "u1", "workspace": "/tmp", "state": "done",
              "started": 1, "finished": 2, "budget": {"max_minutes": 1, "max_tasks": 1},
              "tasks": [], "results": []})
    text = ns.report("u1", "empty")
    assert "Nothing ran" in text


# ── the agent tool ────────────────────────────────────────────────────────

async def test_tool_start_status_stop_report(data_dir, fake_dispatch):
    from src.agent_tools.night_shift_tools import NightShiftTool
    jobs = {"t1": _FakeJob("j1", files=["a.py"])}
    fake_dispatch(jobs)
    tool = NightShiftTool()

    started = await tool.execute(
        '{"action": "start", "tasks": ["t1"], "workspace": "/tmp"}', {"owner": "u1"})
    assert "shift" in started
    shift_id = started["shift"]["id"]
    await _run_and_wait(shift_id, "u1")
    status = await tool.execute(f'{{"action": "status", "id": "{shift_id}"}}', {"owner": "u1"})
    assert status["shift"]["state"] == "done"
    report = await tool.execute(f'{{"action": "report", "id": "{shift_id}"}}', {"owner": "u1"})
    assert "# Night shift" in report["output"]
    stop = await tool.execute(f'{{"action": "stop", "id": "{shift_id}"}}', {"owner": "u1"})
    assert stop["stopped"] is False  # already finished
    bad = await tool.execute('{"action": "bogus"}', {"owner": "u1"})
    assert bad.get("error")


async def test_tool_start_requires_tasks(data_dir):
    from src.agent_tools.night_shift_tools import NightShiftTool
    tool = NightShiftTool()

    result = await tool.execute('{"action": "start", "tasks": [], "workspace": "/tmp"}',
                                {"owner": "u1"})
    assert result.get("error")


# ── routes smoke ─────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch, data_dir):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import night_shift_routes

    app = FastAPI()
    app.include_router(night_shift_routes.setup_night_shift_routes())
    monkeypatch.setattr(night_shift_routes, "require_user", lambda request: "u1")
    monkeypatch.setattr(night_shift_routes, "_is_admin", lambda owner: True)
    return TestClient(app)


async def test_route_create_then_get(client, fake_dispatch):
    jobs = {"t1": _FakeJob("j1")}
    fake_dispatch(jobs)
    resp = client.post("/api/night-shift", json={"tasks": ["t1"], "workspace": "/tmp"})
    assert resp.status_code == 200
    shift_id = resp.json()["id"]
    await _run_and_wait(shift_id, "u1")
    got = client.get(f"/api/night-shift/{shift_id}")
    assert got.status_code == 200 and got.json()["state"] == "done"


def test_route_create_validates(client):
    resp = client.post("/api/night-shift", json={"tasks": [], "workspace": "/tmp"})
    assert resp.status_code == 400


def test_route_404_for_unknown_shift(client):
    assert client.get("/api/night-shift/nope").status_code == 404
    assert client.get("/api/night-shift/nope/report").status_code == 404
    resp = client.post("/api/night-shift/nope/stop")
    assert resp.status_code == 404


async def test_route_report_is_markdown(client, fake_dispatch):
    jobs = {"t1": _FakeJob("j1")}
    fake_dispatch(jobs)
    resp = client.post("/api/night-shift", json={"tasks": ["t1"], "workspace": "/tmp"})
    shift_id = resp.json()["id"]
    await _run_and_wait(shift_id, "u1")
    got = client.get(f"/api/night-shift/{shift_id}/report")
    assert got.status_code == 200
    assert "# Night shift" in got.text


# ── watch action ──────────────────────────────────────────────────────────

async def test_watch_action_reports_the_latest_finished_shift_once(data_dir, monkeypatch, tmp_path):
    from src import watchers
    monkeypatch.setattr(watchers, "_state_dir", lambda: str(tmp_path / "state"))
    ns._save({"id": "s1", "owner": "u1", "workspace": "/tmp", "state": "done",
              "started": 1, "finished": 2, "budget": {"max_minutes": 1, "max_tasks": 1},
              "tasks": ["t"], "results": []})

    text, changed = await watchers.action_night_shift_report("u1")
    assert changed is True
    assert "# Night shift" in text
    from src.builtin_actions import TaskNoop
    with pytest.raises(TaskNoop):
        await watchers.action_night_shift_report("u1")


async def test_watch_action_noop_with_no_shift(data_dir, monkeypatch, tmp_path):
    from src import watchers
    from src.builtin_actions import TaskNoop
    monkeypatch.setattr(watchers, "_state_dir", lambda: str(tmp_path / "state"))

    with pytest.raises(TaskNoop):
        await watchers.action_night_shift_report("u-with-no-shifts")
