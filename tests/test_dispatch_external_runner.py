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
import copy
import inspect
import json
import sys
from types import SimpleNamespace

import pytest

from src import agent_runners as reg
from src import dispatch
from src import external_worker as external_worker_module

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
    weights = [prove.PENALTY.get(k, prove.EXTERNAL_UNGUARDED_PENALTY) for k in kinds]
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
    from src import prove

    assert prove.note_external_gate(None, ["x"]) is None
    packet = {"confidence": 0.9, "uncertainty": [{"kind": "mtime_only", "detail": "d"}]}
    assert prove.note_external_gate(dict(packet), []) == packet
    once = prove.note_external_gate(dict(packet), ["fake"])
    assert once["confidence"] == 0.8 and len(once["uncertainty"]) == 2
    twice = prove.note_external_gate(once, ["fake"])
    assert twice["confidence"] == 0.8, "the entry is added once, not once per call"
    junk = prove.note_external_gate({"confidence": "nonsense", "uncertainty": None}, ["fake"])
    assert junk["confidence"] == 0.0 and junk["uncertainty"][0]["kind"] == dispatch.EXTERNAL_UNGUARDED


# --------------------------------------------------------------------------
# The safety net for that swap: with nothing gated, the new annotator must
# produce the packet the old one produced, entry for entry and value for
# value. `_reference_note_unguarded` below is a frozen copy of the
# `dispatch._note_unguarded` that stood here before `prove.note_external_gate`
# replaced it — kept as an ORACLE rather than as code that runs in production,
# because "byte-identical to what shipped" is a claim, and a claim in a
# comment is worth nothing.
# --------------------------------------------------------------------------

def _reference_note_unguarded(packet, runners):
    """The pre-change `dispatch._note_unguarded`, verbatim. Do not fix it."""
    from src import prove

    if not packet or not runners:
        return packet
    try:
        entry = {"kind": dispatch.EXTERNAL_UNGUARDED,
                 "detail": prove.EXTERNAL_UNGUARDED_DETAIL + " (" + ", ".join(runners[:4]) + ")"}
        unc = list(packet.get("uncertainty") or [])
        if any(u.get("kind") == dispatch.EXTERNAL_UNGUARDED for u in unc):
            return packet
        unc.append(entry)
        unc.sort(key=lambda u: (-prove.PENALTY.get(str(u.get("kind")), prove.EXTERNAL_UNGUARDED_PENALTY),
                                str(u.get("kind"))))
        try:
            confidence = float(packet.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        packet["uncertainty"] = unc
        packet["confidence"] = round(max(0.0, confidence - prove.EXTERNAL_UNGUARDED_PENALTY), 3)
        packet["unguarded_runners"] = list(runners)
    except Exception:  # noqa: BLE001
        pass
    return packet


def _real_packets():
    """Proof packets as `_build_proof` really makes them — `prove.prove` output,
    not hand-built dicts, and with a fixed `now` so the identity is stable."""
    from src import prove

    changes = {"count": 1, "added": ["cart.py"], "modified": [], "deleted": [], "truncated": False}
    cases = [
        (changes, {"ran": True, "ok": True, "command": "pytest"}, {"paths": [], "workers": []}),
        (changes, {"ran": True, "ok": False, "command": "pytest", "failures": ["test_tax"]},
         {"paths": ["cart.py"], "workers": [{"name": "w1", "status": "done"}]}),
        (changes, {"ran": False, "ok": None}, {"paths": ["gone.py"], "workers": []}),
        (None, None, {"paths": [], "workers": [{"name": "w1", "status": "stopped",
                                                "outcome": "cancelled"}]}),
    ]
    return [prove.prove(ev, ver, cl, now=1_700_000_000.0) for ev, ver, cl in cases]


@pytest.mark.parametrize("runners", [["fake"], ["fake", "qwen"], ["a", "b", "c", "d", "e"]])
def test_note_external_gate_with_no_gates_is_the_old_annotation_exactly(runners):
    """This is what makes the swap in `_build_proof` safe: a job whose runners
    are all ungated — which is every job today whose runner row says
    `gate: "none"` — gets the packet it has always got."""
    from src import prove

    for packet in _real_packets():
        old = _reference_note_unguarded(copy.deepcopy(packet), list(runners))
        for gates in ({}, None, [], {"nobody": {"gated": False}}):
            new = prove.note_external_gate(copy.deepcopy(packet), list(runners), gates=gates)
            assert new == old, f"gates={gates!r} runners={runners!r}"


def test_a_gated_run_is_the_one_thing_that_differs():
    """The equivalence above is a safety net, not the point: a ledger saying
    the gate really ran must change the answer, or none of this was worth
    doing."""
    from src import prove

    packet = _real_packets()[0]
    led = {"gated": True, "calls": 7, "denied": 2, "unjudged": 0, "unseen": 0}
    gated = prove.note_external_gate(copy.deepcopy(packet), ["fake"], gates={"fake": led})
    assert gated != _reference_note_unguarded(copy.deepcopy(packet), ["fake"])
    assert dispatch.EXTERNAL_UNGUARDED not in [u["kind"] for u in gated["uncertainty"]]
    assert "unguarded_runners" not in gated
    assert gated["external_gate"]["judged"] == 7 and gated["external_gate"]["denied"] == 2
    # and it does not pay for a hole that is not there
    assert gated["confidence"] == packet["confidence"]


# --------------------------------------------------------------------------
# The gate, on the path that matters
#
# `src/agent_gate.py`, its route and the runner rows' `gate` descriptors were
# all built and tested — and `dispatch._run_external` passed `run_task` none of
# the arguments that arm any of it, so a dispatched Claude Code run was
# ungated while the docs, the settings copy and the proof all said the gate
# existed for it. These pin the wiring itself, not the gate's own behaviour
# (tests/test_agent_gate.py owns that).
# --------------------------------------------------------------------------

GATED_LEDGER = {"gated": True, "calls": 9, "denied": 1, "unjudged": 0, "unseen": 0,
                "stream_tool_calls": 9, "subagent_tool_calls": 0}


def _gated_result(**over):
    out = {"ok": True, "exit_code": 0, "outcome": "success", "output_tail": "wrote cart.py",
           "output_chars": 13, "state": "", "states": [], "why": "", "matched": "",
           "seconds": 0.4, "argv_shown": "claude -p …", "runner": "claudeish",
           "label": "Fake Claude", "status": "done", "error": "", "timed_out": False,
           "cancelled": False, "killed": False, "unguarded": False,
           "guard_note": "gated", "gate": dict(GATED_LEDGER)}
    out.update(over)
    return out


#: The real signature, captured at import before any fixture replaces
#: `run_task`. The spy takes `**kwargs`, so a renamed argument would land in it
#: silently and the gate would go back to never arming with every test still
#: green — and `dispatch._external_resume_supported` asks the signature, so a
#: double that did not carry it would answer the probe for the real module.
_REAL_RUN_TASK_SIG = inspect.signature(external_worker_module.run_task)


@pytest.fixture
def spy(box, monkeypatch):
    """Replace the worker itself and record exactly what dispatch handed it."""
    from src import external_worker

    calls = []

    def fake_run_task(runner_key, task, **kwargs):
        calls.append({"runner": runner_key, "task": task, **kwargs})
        return _gated_result(runner=str(runner_key))

    fake_run_task.__signature__ = _REAL_RUN_TASK_SIG
    monkeypatch.setattr(external_worker, "run_task", fake_run_task)
    box["register"]("claudeish", AGENT)
    box["calls"] = calls
    return box


def _run_one(box, **body):
    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "add apply_tax",
                                                       "runner": "claudeish"}],
                                            "workspace": box["ws"], "verify": "none", **body})
        assert await dispatch.wait(job, 30)
        return job

    return asyncio.run(run())


def test_dispatch_hands_the_worker_everything_the_gate_needs(spy):
    job = _run_one(spy)
    assert len(spy["calls"]) == 1
    call = spy["calls"][0]

    # The id the gate's ledger and its receipts are filed under. One per TASK,
    # so two tasks in one job cannot share a run token.
    assert call["run_id"] == f"{job.id}-0"
    # Everything this agent may write.
    assert call["workspace_roots"] == [job.workspace]
    assert call["owner"] == "luis"
    # A dispatched job is a background asyncio task: nobody can answer a
    # CAUTION prompt, so the gate must refuse rather than ask.
    assert call["attended"] is False
    # And the honest absence: this path cannot reach the delegation's file
    # locks, so it claims none rather than naming a holder it cannot check.
    assert "locks" not in call and "worker_key" not in call


def test_two_tasks_on_one_runner_get_two_run_ids(spy):
    async def run():
        job = await dispatch.start("luis", {"tasks": [
            {"instruction": "one", "runner": "claudeish"},
            {"instruction": "two", "runner": "claudeish"},
        ], "workspace": spy["ws"], "verify": "none"})
        assert await dispatch.wait(job, 30)
        return job

    job = asyncio.run(run())
    assert [c["run_id"] for c in spy["calls"]] == [f"{job.id}-0", f"{job.id}-1"]


def test_a_gated_run_reports_unguarded_false_all_the_way_out(spy):
    job = _run_one(spy)

    assert job.runner_gates == {"claudeish": GATED_LEDGER}
    report = (job.result or {})["subagents"][0]
    assert report["unguarded"] is False

    c = dispatch.compact(job)["result"]
    assert c["workers"][0]["unguarded"] is False

    proof = c["proof"]
    kinds = [u["kind"] for u in proof["uncertainty"]]
    assert dispatch.EXTERNAL_UNGUARDED not in kinds
    assert "unguarded_runners" not in proof
    assert proof["external_gate"] == {"gated": ["claudeish"], "unguarded": [], "judged": 9,
                                      "denied": 1, "unjudged": 0, "unseen": 0}
    assert any(o["kind"] == "external_gate" for o in proof["observations"])


def test_a_runner_the_gate_could_not_reach_is_still_reported_unguarded(spy, monkeypatch):
    """The other half of the same wire: a result with no ledger — every runner
    whose row says `gate: "none"` — is exactly what it always was."""
    from src import external_worker

    monkeypatch.setattr(external_worker, "run_task",
                        lambda runner_key, task, **kw: _gated_result(
                            runner=str(runner_key), unguarded=True, gate={}))
    job = _run_one(spy)

    assert job.runner_gates == {}
    assert (job.result or {})["subagents"][0]["unguarded"] is True
    proof = dispatch.compact(job)["result"]["proof"]
    assert dispatch.EXTERNAL_UNGUARDED in [u["kind"] for u in proof["uncertainty"]]
    assert proof["unguarded_runners"] == ["claudeish"]
    assert "external_gate" not in proof


def test_the_arming_arguments_are_the_ones_run_task_declares(spy):
    _run_one(spy)
    call = dict(spy["calls"][0])
    runner, task = call.pop("runner"), call.pop("task")
    # TypeError here means dispatch is passing a name the worker does not have.
    _REAL_RUN_TASK_SIG.bind(runner, task, **call)


# --------------------------------------------------------------------------
# Resume: the handle, and the round trip that uses it
#
# `dispatch` carried the resume handle and probed for support long before
# anything could produce an id. Both halves are here now, so the probe answers
# True and a fix round can continue the run that made the change.
# --------------------------------------------------------------------------

def test_the_worker_can_be_resumed_in_this_build():
    assert dispatch._external_resume_supported() is True


def test_a_reported_session_id_becomes_the_workers_resume_handle():
    report = dispatch._external_report(
        {"name": "w1", "instruction": "add apply_tax"}, 0,
        {"status": "done", "ok": True, "runner": "claudeish", "session_id": "sess-9"})
    assert report["runner_session"] == "sess-9"
    # and the chat-session id stays None: an external agent has no Faustus
    # session, and confusing the two would resume the wrong thing.
    assert report["session_id"] is None


def test_a_runner_that_reported_nothing_leaves_the_fresh_fixer_path():
    report = dispatch._external_report({"name": "w1", "instruction": "x"}, 0,
                                       {"status": "done", "ok": True, "runner": "qwen"})
    assert report["runner_session"] == ""
    job = SimpleNamespace(result={"subagents": [report]})
    assert dispatch._resume_target(job) is None


def test_the_fix_round_prefers_the_run_that_touched_the_failing_file():
    rows = [
        dispatch._external_report({"name": "w1", "instruction": "x"}, 0,
                                  {"status": "done", "ok": True, "runner": "claudeish",
                                   "session_id": "sess-early"}),
        dispatch._external_report({"name": "w2", "instruction": "y"}, 1,
                                  {"status": "done", "ok": True, "runner": "claudeish",
                                   "session_id": "sess-late"}),
    ]
    job = SimpleNamespace(result={"subagents": rows})
    target = dispatch._resume_target(job, {"related_files": ["cart.py"]})
    # An external agent files no `mutations`, so nothing outranks recency and
    # the LAST run wins — the session holding the most recent state.
    assert target == {"kind": "runner", "id": "sess-late", "runner": "claudeish",
                      "name": "w2", "model": "", "agent": ""}


def test_a_task_carrying_a_handle_is_continued_not_restarted(spy):
    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "fix it",
                                                       "runner": "claudeish"}],
                                            "workspace": spy["ws"], "verify": "none"})
        assert await dispatch.wait(job, 30)
        return job

    # The shape `_run_external` reads: what the fix loop writes onto its fixer.
    original = dispatch._run_external

    async def with_handle(job, tasks, cb):
        for t in tasks:
            t["resume"] = {"kind": "runner", "id": "sess-9", "runner": "claudeish"}
        return await original(job, tasks, cb)

    import unittest.mock as mock
    with mock.patch.object(dispatch, "_run_external", with_handle):
        asyncio.run(run())

    assert spy["calls"][0]["resume"] == "sess-9"


def test_a_session_handle_is_not_offered_to_an_external_agent(spy):
    """`kind: "session"` is a Faustus chat session. Handing it to a foreign
    CLI as its own run id would resume nothing and look like it had."""
    original = dispatch._run_external

    async def with_handle(job, tasks, cb):
        for t in tasks:
            t["resume"] = {"kind": "session", "id": "abc123", "runner": ""}
        return await original(job, tasks, cb)

    import unittest.mock as mock
    with mock.patch.object(dispatch, "_run_external", with_handle):
        _run_one(spy)

    assert "resume" not in spy["calls"][0]
