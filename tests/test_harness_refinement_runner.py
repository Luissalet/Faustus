"""When the harness proposer runs: off by default, queued in the background
after a turn, pre-filtered without a model, and held back by the same guards
the unattended skill jobs obey (a fake guard stands in for the model runner)."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.database import ChatMessage as DbChatMessage
from core.database import Session as DbSession
from services.projects import ProjectStore
from src import agent_defs
from src.harness_refinement import proposer, runner, store, targets
from src.memory import MemoryManager

NOW = datetime(2026, 10, 1, 12, 0, 0)
CORRECTION = [("user", "write the release notes for v2", None),
              ("assistant", "Here are the release notes: long long long...", None),
              ("user", "that's wrong, I asked for three bullet points", None)]
CLEAN = [("user", "what is the capital of France?", None), ("assistant", "Paris.", None),
         ("user", "perfecto, gracias", None)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for mod in (store, targets, agent_defs):
        monkeypatch.setattr(mod, "DATA_DIR", str(data))
    projects = ProjectStore(str(data))
    monkeypatch.setattr(targets, "_project_store", lambda: projects)
    monkeypatch.setattr(targets, "_memory_manager", lambda: MemoryManager(str(data)))
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.database.SessionLocal", factory, raising=False)
    project = projects.create("Demo", instructions="Answer briefly.", scaffold_memory=False)
    monkeypatch.setattr("services.projects.project_for_session", lambda sid, owner=None: project)
    proposer._INFLIGHT.clear()
    runner.reset_for_tests()
    yield type("Env", (), {"db": factory, "project": project, "projects": projects})
    runner.reset_for_tests()


def _seed(env, sid, turns):
    db = env.db()
    db.add(DbSession(id=sid, name=sid, endpoint_url="http://x", model="m", owner="", message_count=0))
    for i, (role, text, events) in enumerate(turns):
        db.add(DbChatMessage(id=f"{sid}-m{i}", session_id=sid, role=role, content=text,
                             timestamp=NOW + timedelta(seconds=i * 10), meta_data=None))
    db.commit()
    db.close()


class FakeLlm:
    def __init__(self, project_id):
        self.calls = 0
        self.answer = {"propose": True, "reason": "bullets", "axis": "prompt_layer",
                       "target": f"project:{project_id}", "op": "update",
                       "after": "Answer briefly. Use bullet points when asked."}

    async def __call__(self, url, model, prompt, user_initiated):
        self.calls += 1
        return json.dumps(self.answer)


async def _no_sleep(_seconds):
    return None


# -- the setting -----------------------------------------------------------

def test_the_setting_is_off_by_default(monkeypatch):
    from src import settings
    assert settings.DEFAULT_SETTINGS["harness_refinement_enabled"] is False
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: default)
    assert runner.enabled() is False
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: True if key == runner.SETTING else default)
    assert runner.enabled() is True


def test_a_broken_settings_read_means_off(monkeypatch):
    from src import settings

    def boom(*a, **k):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(settings, "get_setting", boom)
    assert runner.enabled() is False


# -- queueing --------------------------------------------------------------

def test_disabled_queues_nothing_and_starts_no_worker(env, monkeypatch):
    started = []
    monkeypatch.setattr(runner, "_ensure_worker", lambda: started.append(1))
    assert runner.maybe_queue_after_turn("s1", "", enabled_fn=lambda: False) is False
    assert runner.status()["queued"] == 0 and started == []


def test_enabled_queues_one_job_per_session_and_never_runs_inline(env, monkeypatch):
    monkeypatch.setattr(proposer, "propose_for_session",
                        lambda *a, **k: pytest.fail("the foreground call must not run the proposer"))
    assert runner.maybe_queue_after_turn("s1", "me", enabled_fn=lambda: True, start_worker=False) is True
    assert runner.maybe_queue_after_turn("s1", "me", enabled_fn=lambda: True, start_worker=False) is False
    assert runner.maybe_queue_after_turn("s2", "me", enabled_fn=lambda: True, start_worker=False) is True
    assert runner.status()["queued"] == 2 and runner.status()["stats"]["queued"] == 2


def test_the_queue_is_bounded(env):
    for i in range(runner.QUEUE_MAX):
        assert runner.maybe_queue_after_turn(f"s{i}", "", enabled_fn=lambda: True, start_worker=False)
    assert runner.maybe_queue_after_turn("overflow", "", enabled_fn=lambda: True, start_worker=False) is False
    assert runner.status()["stats"]["dropped_full"] == 1


def test_queueing_never_raises(env, monkeypatch):
    def boom():
        raise RuntimeError("x")
    assert runner.maybe_queue_after_turn("s1", "", enabled_fn=boom) is False


def test_the_job_runs_on_its_own_thread(env, monkeypatch):
    seen = {}
    done = threading.Event()

    async def fake_run_job(job, **kw):
        seen["thread"] = threading.current_thread().name
        seen["job"] = job
        done.set()
        return {}

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    caller = threading.current_thread().name
    assert runner.maybe_queue_after_turn("s1", "me", enabled_fn=lambda: True) is True
    assert done.wait(5)
    assert seen["thread"] == "harness-refinement" != caller
    assert seen["job"]["session_id"] == "s1" and seen["job"]["owner"] == "me"


def test_the_chat_pipeline_calls_the_hook_after_the_turn_is_saved():
    root = Path(__file__).resolve().parent.parent
    source = (root / "routes" / "chat_helpers.py").read_text(encoding="utf-8")
    assert "maybe_queue_after_turn(session_id, _post_turn_owner)" in source
    hook = source.index("maybe_queue_after_turn")
    assert source.index("sess.add_message(ChatMessage(\"assistant\"") < hook < source.index("fix_memory.record_from_turn")


# -- the job ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_trivial_task_ends_at_the_prefilter_without_touching_the_guard(env):
    _seed(env, "s1", CLEAN)
    llm = FakeLlm(env.project["id"])

    def admission(url, model):
        pytest.fail("a trivial task must not even ask the guard")

    result = await runner.run_job({"session_id": "s1", "owner": ""}, sleep=_no_sleep, admission=admission, llm=llm)
    assert result == {"status": "skipped", "reason": "no_signal"}
    assert llm.calls == 0 and runner.status()["stats"]["prefiltered"] == 1


@pytest.mark.asyncio
async def test_a_job_with_a_signal_waits_for_the_guard_then_proposes(env):
    _seed(env, "s1", CORRECTION)
    llm = FakeLlm(env.project["id"])
    answers = iter(["foreground_activity", "model_busy", None, None])
    asked = []

    def admission(url, model):
        asked.append((url, model))
        return next(answers)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result = await runner.run_job({"session_id": "s1", "owner": ""}, sleep=fake_sleep, admission=admission,
                                  llm=llm, settle_s=1.0)
    assert result["status"] == "proposed" and result["proposal"]["status"] == "pending"
    assert llm.calls == 1
    assert sleeps == [1.0, runner.ADMISSION_POLL_S, runner.ADMISSION_POLL_S]      # settle, then two "not now"
    assert asked[0] == (None, None) and asked[-1][1] is not None                # re-checked with the real model right before the call
    assert env.projects.get(env.project["id"])["instructions"] == "Answer briefly."


@pytest.mark.asyncio
async def test_a_guard_that_never_clears_drops_the_job_without_calling_the_model(env):
    _seed(env, "s1", CORRECTION)
    llm = FakeLlm(env.project["id"])
    result = await runner.run_job({"session_id": "s1", "owner": ""}, sleep=_no_sleep,
                                  admission=lambda url, model: "would_load_model", llm=llm,
                                  wait_s=15.0, poll_s=5.0)
    assert result == {"status": "skipped", "reason": "would_load_model"}
    assert llm.calls == 0 and store.list_proposals() == []
    entry = store.read_log()[0]
    assert entry["event"] == "skipped" and entry["result"] == "would_load_model"
    assert entry["trigger"] == "turn_end:correction"
    assert runner.status()["stats"]["deferred_dropped"] == 1


@pytest.mark.asyncio
async def test_traffic_arriving_between_the_two_guard_checks_is_waited_out_not_dropped(env):
    """Seen live: the first check passed in a gap, the re-check with the real
    model landed on a request. That must wait, and must log nothing until it
    gives up."""
    _seed(env, "s1", CORRECTION)
    llm = FakeLlm(env.project["id"])
    answers = iter([None, "foreground_activity", "foreground_activity", None, None])

    def admission(url, model):
        return next(answers)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result = await runner.run_job({"session_id": "s1", "owner": ""}, sleep=fake_sleep, admission=admission,
                                  llm=llm, settle_s=0.0)
    assert result["status"] == "proposed" and llm.calls == 1
    assert sleeps == [runner.ADMISSION_POLL_S, runner.ADMISSION_POLL_S]
    assert [e["event"] for e in store.read_log()] == ["proposed"]            # no "skipped" noise on the way
    assert runner.status()["stats"]["ran"] == 1 and runner.status()["stats"]["deferred_dropped"] == 0


@pytest.mark.asyncio
async def test_a_recheck_that_never_clears_drops_the_job_once(env):
    _seed(env, "s1", CORRECTION)
    llm = FakeLlm(env.project["id"])
    calls = []

    def admission(url, model):
        calls.append(url)
        return None if url is None else "model_busy"

    result = await runner.run_job({"session_id": "s1", "owner": ""}, sleep=_no_sleep, admission=admission,
                                  llm=llm, settle_s=0.0, wait_s=10.0, poll_s=5.0)
    assert result == {"status": "skipped", "reason": "model_busy"}
    assert llm.calls == 0
    log = store.read_log()
    assert len(log) == 1 and log[0]["event"] == "skipped" and log[0]["result"] == "model_busy"
    assert runner.status()["stats"]["deferred_dropped"] == 1


# -- the admission probes, with a fake model guard ---------------------------

class FakeGuard:
    def __init__(self, may_run=True, busy=False, no_slot=False):
        self.may_run, self.busy, self.no_slot = may_run, busy, no_slot
        self.asked = []

    def should_run_with_model(self, job, url, model, user_initiated=False):
        self.asked.append((job, url, model, user_initiated))
        return self.may_run

    def model_busy(self, url):
        return self.busy

    def no_free_slot(self, url):
        return self.no_slot


@pytest.fixture
def quiet_machine(monkeypatch):
    from src import interactive_gate, model_lease, unattended_breaker
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda *a, **k: False)
    monkeypatch.setattr(unattended_breaker, "is_open", lambda *a, **k: False)
    monkeypatch.setattr(model_lease, "enabled", lambda: False)


def test_admission_passes_when_everything_is_quiet(quiet_machine):
    guard = FakeGuard()
    assert runner.admission_blocker("http://x/v1", "m", guard=guard) is None
    assert guard.asked == [("harness_refinement", "http://x/v1", "m", False)]      # an unattended job, never user-initiated


@pytest.mark.parametrize("guard,reason", [
    (FakeGuard(may_run=False), "would_load_model"),
    (FakeGuard(busy=True), "model_busy"),
    (FakeGuard(no_slot=True), "model_busy"),
])
def test_admission_respects_the_model_guard(quiet_machine, guard, reason):
    assert runner.admission_blocker("http://x/v1", "m", guard=guard) == reason


def test_admission_waits_while_the_person_is_working(monkeypatch, quiet_machine):
    from src import interactive_gate
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda *a, **k: True)
    assert runner.admission_blocker("http://x/v1", "m", guard=FakeGuard()) == "foreground_activity"


def test_admission_respects_the_failure_breaker(monkeypatch, quiet_machine):
    from src import unattended_breaker
    monkeypatch.setattr(unattended_breaker, "is_open", lambda *a, **k: True)
    assert runner.admission_blocker("http://x/v1", "m", guard=FakeGuard()) == "unattended_breaker_open"


def test_admission_respects_a_sibling_instance_on_the_runner(monkeypatch, quiet_machine):
    from src import model_lease, vram_admission
    monkeypatch.setattr(model_lease, "enabled", lambda: True)
    monkeypatch.setattr(vram_admission, "ollama_root", lambda url: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_lease, "sibling_reserved_bytes", lambda root: 1)
    assert runner.admission_blocker("http://x/v1", "m", guard=FakeGuard()) == "lease_reserved"
    monkeypatch.setattr(model_lease, "sibling_reserved_bytes", lambda root: 0)
    monkeypatch.setattr(model_lease, "sibling_active", lambda root: {"m": time.time()})
    assert runner.admission_blocker("http://x/v1", "m", guard=FakeGuard()) == "lease_sibling_active"


def test_admission_without_an_endpoint_or_with_a_broken_guard(monkeypatch, quiet_machine):
    monkeypatch.setattr(proposer, "resolve_endpoint", lambda owner="": (None, None))
    assert runner.admission_blocker(None, None, guard=FakeGuard()) == "no_endpoint"

    class Broken(FakeGuard):
        def should_run_with_model(self, *a, **k):
            raise RuntimeError("probe failed")

    assert runner.admission_blocker("http://x/v1", "m", guard=Broken()) == "model_guard_error"
