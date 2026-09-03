"""A dispatched job whose worker is an agent Faustus did not write
(src/dispatch.py + src/agent_runners.py + src/external_worker.py).

The feature only earns its place by extending the harness of FAUSTUS.md §22 to
somebody else's binary, so this file pins both halves:

  * the harness still applies — the checkpoint before, the diff after, the
    verification, the honest status, the proof;
  * and the one thing it cannot apply is SAID. Faustus's command guard cannot
    see inside another agent's own shell, so the job's proof carries
    `external_agent_unguarded` and its verdict says it in words.

Plus the three refusals: the setting is off (it ships off), the runner is
unknown, the runner is not installed — each of them before any other worker of
the job starts, because failing halfway through costs the job's other workers
their time for a result that was never going to be complete.

And the invariant that protects everything that came before: **a job with no
`runner` is what it always was.**
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from src import agent_runners as reg
from src import dispatch

AGENT = """
import pathlib, sys
task = sys.argv[1]
print("agent working on:", task)
pathlib.Path("cart.py").write_text("def apply_tax(t, r):\\n    return t * (1 + r)\\n", encoding="utf-8")
print("wrote cart.py")
"""

RATE_LIMITED_AGENT = """
import pathlib, sys
print("HTTP 429 Too Many Requests")
print("rate limit reached, waiting")
pathlib.Path("cart.py").write_text("x = 1\\n", encoding="utf-8")
print("done anyway")
"""

FAILING_AGENT = """
import sys
print("I cannot do that")
sys.exit(2)
"""


class _SM:
    def __init__(self):
        self.sessions = {}
        self.messages = []

    def create_session(self, session_id, name, endpoint_url, model, rag=False, owner=None):
        s = SimpleNamespace(id=session_id, name=name, endpoint_url=endpoint_url, model=model,
                            owner=owner, headers=None)
        self.sessions[session_id] = s
        return s

    def get_session(self, sid):
        return self.sessions.get(sid)

    def add_message(self, sid, msg):
        self.messages.append((sid, msg))

    def save_sessions(self):
        self.saved = getattr(self, "saved", 0) + 1


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A dispatch job box with a fake external agent registered, and the
    built-in delegate tool replaced by one that records what it was given."""
    import src.ai_interaction as ai
    from src.agent_tools import subagent_tools as st

    sm = _SM()
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path / "dispatch"))
    monkeypatch.setattr(dispatch, "resolve_route",
                        lambda owner, model=None: ("http://127.0.0.1:11434/v1", model or "qwen3.5:9b", None))
    state = {"delegated": [], "result": {"subagents": [], "exit_code": 0, "output": ""}, "ws": str(tmp_path / "ws")}

    class FakeTool:
        async def execute(self, content, ctx):
            state["delegated"].append(json.loads(content))
            return state["result"]

    monkeypatch.setattr(st, "DelegateAgentsTool", FakeTool)

    # The fake agent, as a REGISTRY ROW: adding an agent is a table entry.
    def register(name: str, body: str) -> None:
        script = tmp_path / f"{name}.py"
        script.write_text(body, encoding="utf-8")
        state["runners"][name] = reg.Runner(
            key=name, label=f"Fake {name}", kind="cli", licence="open",
            install=f"ollama launch {name}", argv=(sys.executable, str(script), "{task}"),
            detect=(sys.executable,), notes="a test double")

    state["runners"] = {}
    state["register"] = register
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 30)
    monkeypatch.setattr(reg, "get", lambda key, **kw: state["runners"].get(str(key or "")))
    monkeypatch.setattr(reg, "runners", lambda **kw: list(state["runners"].values()))

    dispatch.reset_for_tests()
    (tmp_path / "ws").mkdir()
    state["sm"] = sm
    yield state
    dispatch.reset_for_tests()


# ── the invariant: nothing changes for a job without a runner ──────────────

def test_a_job_with_no_runner_is_byte_identical_to_today(box):
    box["result"] = {"subagents": [{"name": "w1", "status": "done", "mutations": [],
                                    "final_text": "did it", "rounds": 2, "tool_calls": 3}],
                     "exit_code": 0, "output": "report"}

    async def run():
        job = await dispatch.start("luis", {"tasks": ["add apply_tax"], "workspace": box["ws"],
                                            "verify": "none"})
        assert await dispatch.wait(job, 10)
        return job

    job = asyncio.run(run())
    assert job.status == "done" and job.runners_used == []
    # the delegation got the job's own args, once, exactly as before
    assert len(box["delegated"]) == 1
    assert [t["instruction"] for t in box["delegated"][0]["tasks"]] == ["add apply_tax"]
    assert "runner" not in box["delegated"][0]["tasks"][0]
    payload = dispatch.compact(job)
    # not one new key anywhere in the answer
    text = json.dumps(payload)
    for word in ("runners", "unguarded", "external_agent_unguarded", "argv_shown"):
        assert word not in text, word
    assert "external" not in (job.verdict or "")
    assert payload["result"]["proof"]["verdict"] in dispatch_verdicts()
    assert all(u["kind"] != dispatch.EXTERNAL_UNGUARDED
               for u in payload["result"]["proof"]["uncertainty"])


def dispatch_verdicts():
    from src import prove
    return prove.VERDICTS


# ── an external agent as a worker, inside the same harness ─────────────────

def test_an_external_agent_runs_inside_the_harness_and_the_proof_says_it_was_unguarded(box):
    box["register"]("fake", AGENT)

    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "add apply_tax", "runner": "fake"}],
                                            "workspace": box["ws"], "verify": "none"})
        assert await dispatch.wait(job, 30)
        return job

    job = asyncio.run(run())
    # the built-in workers were never asked to do anything
    assert box["delegated"] == []
    assert job.status == "done" and job.runners_used == ["fake"]

    # ── the harness still applies: Faustus SAW the change on disk ──────────
    c = dispatch.compact(job)["result"]
    assert c["changes"]["count"] == 1 and "cart.py" in c["changes"]["added"]
    assert c["files_changed"] == ["cart.py"]
    # an external agent files no claim, so there is nothing to contradict
    assert c["claimed_only"] == []
    w = c["workers"][0]
    assert w["role"] == "external" and w["status"] == "done" and w["runner"] == "fake"
    assert w["files_changed"] == [], "an external agent's mutations are not its own word"
    assert "wrote cart.py" in w["summary"]
    assert w["unguarded"] is True and w["argv_shown"]

    # ── and the thing that cannot be checked is named, not hidden ─────────
    proof = c["proof"]
    kinds = [u["kind"] for u in proof["uncertainty"]]
    assert dispatch.EXTERNAL_UNGUARDED in kinds
    entry = [u for u in proof["uncertainty"] if u["kind"] == dispatch.EXTERNAL_UNGUARDED][0]
    assert "command guard did not see its commands" in entry["detail"] and "fake" in entry["detail"]
    assert proof["unguarded_runners"] == ["fake"]
    # it COSTS the proof confidence; a confidence below 1 always has its reasons
    assert proof["confidence"] < 0.7 and proof["uncertainty"]
    # the list is one list, sorted the way prove sorts it (heaviest first)
    from src import prove
    weights = [prove.PENALTY.get(k, dispatch.EXTERNAL_UNGUARDED_PENALTY) for k in kinds]
    assert weights == sorted(weights, reverse=True)
    # and the human-readable line says it too
    assert "external agent(s) ran unguarded: fake" in job.verdict
    assert dispatch.compact(job)["runners"] == ["fake"] and dispatch.compact(job)["unguarded"] is True

    # and it survives a restart: the mirror still says what ran unguarded
    dispatch.reset_for_tests()
    again = dispatch.get(job.id)
    assert again is not None and again.runners_used == ["fake"]
    assert dispatch.compact(again)["runners"] == ["fake"]


def test_the_external_agents_own_output_is_read_for_a_state_and_it_is_not_killed(box):
    box["register"]("limited", RATE_LIMITED_AGENT)

    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "work", "runner": "limited"}],
                                            "workspace": box["ws"], "verify": "none"})
        assert await dispatch.wait(job, 30)
        return job

    job = asyncio.run(run())
    # it ran to its own end: the rate limit was a report, never a kill
    assert job.status == "done"
    assert job.changes["count"] == 1
    states = dispatch.worker_states(job)
    assert states, states
    name = next(iter(states))
    assert states[name]["state"] == "rate_limited" and states[name]["why"]
    assert "reported (not killed)" in job.verdict


def test_an_external_agent_that_fails_makes_the_job_partial_not_a_lie(box):
    box["register"]("broken", FAILING_AGENT)

    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "x", "runner": "broken"}],
                                            "workspace": box["ws"], "verify": "none"})
        assert await dispatch.wait(job, 30)
        return job

    job = asyncio.run(run())
    assert job.status == "partial"
    c = dispatch.compact(job)["result"]
    assert c["workers"][0]["status"] == "error" and "exited with code 2" in c["workers"][0]["error"]
    assert c["exit_code"] == 1 and c["changes"]["count"] == 0
    assert "external agent(s) ran unguarded: broken" in job.verdict


def test_a_job_can_mix_a_local_worker_and_an_external_one(box):
    box["register"]("fake", AGENT)
    box["result"] = {"subagents": [{"name": "local", "status": "done", "mutations": ["notes.md"],
                                    "final_text": "local did its bit"}],
                     "exit_code": 0, "output": "report"}

    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "write notes"},
                                                      {"instruction": "add apply_tax", "runner": "fake"}],
                                            "workspace": box["ws"], "verify": "none", "parallel": False})
        assert await dispatch.wait(job, 30)
        return job

    job = asyncio.run(run())
    # the local worker went to delegate_agents, and ONLY it did
    assert len(box["delegated"]) == 1
    assert [t["instruction"] for t in box["delegated"][0]["tasks"]] == ["write notes"]
    names = [w["name"] for w in dispatch.compact(job)["result"]["workers"]]
    assert "local" in names and len(names) == 2
    assert job.runners_used == ["fake"]
    # the local worker's claim is still checked against the disk; the external
    # one still claims nothing
    assert dispatch.compact(job)["result"]["claimed_only"] == ["notes.md"]


def test_cancelling_a_job_kills_the_external_agent_and_still_says_it_ran(box):
    """Cancelling is not a failure — and it is not an excuse to forget that an
    unguarded agent already ran."""
    box["register"]("hang", "import time\ntime.sleep(120)\n")

    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "hang", "runner": "hang"}],
                                            "workspace": box["ws"], "verify": "none"})
        await asyncio.sleep(0.4)
        assert dispatch.cancel(job) is True
        assert await dispatch.wait(job, 10)
        return job

    job = asyncio.run(run())
    assert job.status == "cancelled"
    assert job.runners_used == ["hang"], "an agent that ran must be named even when cancelled"
    assert "external agent(s) ran unguarded: hang" in job.verdict
    kinds = [u["kind"] for u in (job.proof or {}).get("uncertainty") or []]
    assert dispatch.EXTERNAL_UNGUARDED in kinds
    # its process tree really goes (the kill is the worker thread's, on the
    # status the cancel set)
    import os
    import time as _t
    if os.name != "nt":
        for _ in range(60):
            if "hang.py" not in os.popen("ps -eo command").read():
                break
            _t.sleep(0.1)
        assert "hang.py" not in os.popen("ps -eo command").read()


# ── the refusals, all before anything starts ───────────────────────────────

def test_the_setting_off_refuses_with_the_reason(box, monkeypatch):
    box["register"]("fake", AGENT)
    monkeypatch.setattr(reg, "enabled", lambda: False)
    with pytest.raises(ValueError) as err:
        asyncio.run(dispatch.start("luis", {"tasks": [{"instruction": "x", "runner": "fake"}],
                                            "workspace": box["ws"]}))
    message = str(err.value)
    assert "agent_external_runners" in message and "off" in message
    assert "third-party binaries" in message and "command guard" in message
    assert box["delegated"] == [], "nothing may start"


def test_an_unknown_runner_fails_cleanly_and_starts_nothing(box):
    box["register"]("fake", AGENT)
    with pytest.raises(ValueError) as err:
        asyncio.run(dispatch.start("luis", {"tasks": [{"instruction": "x", "runner": "ghost"},
                                                      {"instruction": "the other worker"}],
                                            "workspace": box["ws"]}))
    assert "unknown agent runner: 'ghost'" in str(err.value)
    # the job's OTHER worker was not spent on a result that could not be complete
    assert box["delegated"] == []
    assert dispatch.list_jobs("luis") == []


def test_a_runner_that_is_not_installed_names_its_install_command(box):
    box["runners"]["absent"] = reg.Runner(
        key="absent", label="Absent Agent", kind="cli", licence="open",
        install="ollama launch absent", argv=("definitely-not-a-real-binary-xyz", "{task}"),
        detect=("absent",))
    with pytest.raises(ValueError) as err:
        asyncio.run(dispatch.start("luis", {"tasks": [{"instruction": "x", "runner": "absent"}],
                                            "workspace": box["ws"]}))
    assert "not installed" in str(err.value) and "ollama launch absent" in str(err.value)
    assert box["delegated"] == []


def test_an_agent_with_no_recorded_invocation_is_refused_not_guessed_at(box):
    box["runners"]["cline"] = reg.Runner(key="cline", label="Cline", kind="cli", licence="open",
                                         argv=(), detect=("cline",))
    with pytest.raises(ValueError) as err:
        asyncio.run(dispatch.start("luis", {"tasks": [{"instruction": "x", "runner": "cline"}],
                                            "workspace": box["ws"]}))
    assert reg.NOT_RUNNABLE_NOTE in str(err.value)


# ── the request plumbing ───────────────────────────────────────────────────

def test_a_job_wide_runner_applies_to_every_task_and_a_task_may_override_it():
    args = dispatch.build_args({"tasks": ["one", "two"], "runner": "claude"})
    assert [t["runner"] for t in args["tasks"]] == ["claude", "claude"]
    args = dispatch.build_args({"tasks": [{"instruction": "one", "runner": "qwen"},
                                          {"instruction": "two"}], "runner": "claude"})
    assert [t["runner"] for t in args["tasks"]] == ["qwen", "claude"]
    assert dispatch.runner_keys(args) == ["qwen", "claude"]


def test_no_runner_means_no_runner_key_at_all():
    args = dispatch.build_args({"tasks": ["one", "two"]})
    assert all("runner" not in t for t in args["tasks"])
    assert dispatch.runner_keys(args) == []
    assert dispatch.vet_runners(args) is None


def test_the_external_timeout_is_the_smaller_of_the_two_ceilings(box, monkeypatch):
    """The job's per-worker timeout and the operator's bound on third-party
    binaries both apply; neither may raise the other."""
    job = SimpleNamespace(id="j", args={"timeout_s": 600})
    monkeypatch.setattr(reg, "timeout_s", lambda: 900)
    assert dispatch._external_timeout(job) == 600.0
    monkeypatch.setattr(reg, "timeout_s", lambda: 120)
    assert dispatch._external_timeout(job) == 120.0
    job.args["timeout_s"] = "nonsense"
    assert dispatch._external_timeout(job) == 120.0

    def boom():
        raise RuntimeError("settings are gone")

    monkeypatch.setattr(reg, "timeout_s", boom)
    job.args["timeout_s"] = 300
    assert dispatch._external_timeout(job) == 300.0


def test_the_proof_annotation_is_total():
    """A proof that cannot be annotated comes back as it was; a job with no
    runner is never annotated at all."""
    assert dispatch._note_unguarded(None, ["x"]) is None
    packet = {"confidence": 0.9, "uncertainty": [{"kind": "mtime_only", "detail": "d"}]}
    assert dispatch._note_unguarded(dict(packet), []) == packet
    once = dispatch._note_unguarded(dict(packet), ["fake"])
    assert once["confidence"] == 0.8 and len(once["uncertainty"]) == 2
    twice = dispatch._note_unguarded(once, ["fake"])
    assert twice["confidence"] == 0.8, "the entry is added once, not once per call"
    junk = dispatch._note_unguarded({"confidence": "nonsense", "uncertainty": None}, ["fake"])
    assert junk["confidence"] == 0.0 and junk["uncertainty"][0]["kind"] == dispatch.EXTERNAL_UNGUARDED
