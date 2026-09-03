"""The impact score finally orders something.

`services/objectives.py` has computed a structural impact score per objective
since the objectives dashboard shipped (PageRank .30 + betweenness .30 +
blocker_ratio .20 + staleness .10 + priority .10) and the hub has shown a
"priority hint" when that order disagreed with the human one — but nothing
consumed it, so the planner still worked through the tasks in the order they
were typed.

What is pinned here is the smallest honest version of consuming it, and the
limits of it just as hard as the behaviour:

* a SEQUENTIAL job whose tasks name `OBJ-n` runs them highest-impact first;
* a PARALLEL job is untouched — it has no order to fix;
* `agent_objective_ordering` off reproduces the written order exactly;
* a task that names no objective, or one this workspace's file does not have,
  keeps its own position; only the scored tasks are permuted among the slots
  they already occupied;
* the reordering is recorded on the job (`task_order`) and said in the verdict,
  so it is never a silent rearrangement of somebody's plan.

There is no scheduler here: ordering ONE job's task list is the whole scope.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from src import dispatch

# OBJ-2 and OBJ-3 depend on OBJ-1; OBJ-4 depends on OBJ-2 — the same shape
# tests/test_objectives.py scores. The resulting order is OBJ-2 (0.6288),
# OBJ-1 (0.4583), then OBJ-3 and OBJ-4 tied at 0.1877.
_NOW = "2099-01-01T00:00:00Z"          # future → staleness 0, deterministic
_OBJECTIVES = [
    {"t": "obj", "id": f"OBJ-{i}", "title": f"Objective {i}", "status": "open",
     "priority": 4 if i == 1 else 1, "owner": "user", "notes": "",
     "created_at": _NOW, "updated_at": _NOW, "last_actor": "user"}
    for i in range(1, 5)
] + [
    {"t": "dep", "from": "OBJ-2", "to": "OBJ-1"},
    {"t": "dep", "from": "OBJ-3", "to": "OBJ-1"},
    {"t": "dep", "from": "OBJ-4", "to": "OBJ-2"},
]


@pytest.fixture()
def workspace(tmp_path):
    """A folder with an objectives file, the way a project's workspace has one."""
    ws = tmp_path / "covernet"
    (ws / ".odysseus").mkdir(parents=True)
    with open(ws / ".odysseus" / "objectives.jsonl", "w", encoding="utf-8") as fh:
        for record in _OBJECTIVES:
            fh.write(json.dumps(record) + "\n")
    return str(ws)


def _tasks(*instructions):
    return [{"name": f"w{i + 1}", "instruction": text, "files": [], "model": ""}
            for i, text in enumerate(instructions)]


def _names(tasks):
    return [t["name"] for t in tasks]


# ── the pure ordering ───────────────────────────────────────────────────────

def test_the_tasks_run_in_the_order_the_graph_ranks_their_objectives(workspace):
    tasks = _tasks("finish OBJ-3", "finish OBJ-1", "tidy the changelog", "finish OBJ-2")
    ordered, record = dispatch.order_tasks_by_impact(tasks, workspace)
    # OBJ-2 > OBJ-1 > OBJ-3, and the task with no objective never moves.
    assert _names(ordered) == ["w4", "w2", "w3", "w1"]
    assert ordered[2]["instruction"] == "tidy the changelog"
    assert record == {"by": "impact", "from": ["w1", "w2", "w3", "w4"],
                      "to": ["w4", "w2", "w3", "w1"]}
    # Same input, same answer — the score is deterministic and so is this.
    assert dispatch.order_tasks_by_impact(tasks, workspace)[0] == ordered
    # and the caller's list was not mutated under it
    assert _names(tasks) == ["w1", "w2", "w3", "w4"]


def test_a_task_naming_an_objective_this_workspace_does_not_have_keeps_its_place(workspace):
    tasks = _tasks("close OBJ-99", "finish OBJ-1", "finish OBJ-2")
    ordered, record = dispatch.order_tasks_by_impact(tasks, workspace)
    # OBJ-99 is in no objectives file, so it carries no score and is not a
    # participant in the ordering: it stays exactly where it was written.
    assert _names(ordered) == ["w1", "w3", "w2"]
    assert record["to"] == ["w1", "w3", "w2"]


def test_a_task_naming_two_objectives_takes_the_higher_one(workspace):
    # w1 names OBJ-3 (0.1877) and OBJ-2 (0.6288): it is worth the most
    # important thing it unblocks, so it goes first.
    tasks = _tasks("OBJ-3 needs OBJ-2 doing first", "finish OBJ-1")
    ordered, _ = dispatch.order_tasks_by_impact(tasks, workspace)
    assert _names(ordered) == ["w1", "w2"]
    tasks = _tasks("finish OBJ-1", "OBJ-3 needs OBJ-2 doing first")
    ordered, record = dispatch.order_tasks_by_impact(tasks, workspace)
    assert _names(ordered) == ["w2", "w1"] and record is not None


def test_nothing_to_reorder_is_no_record_at_all(workspace, tmp_path):
    # already in impact order
    assert dispatch.order_tasks_by_impact(_tasks("OBJ-2", "OBJ-1"), workspace)[1] is None
    # fewer than two scored tasks
    assert dispatch.order_tasks_by_impact(_tasks("OBJ-1", "tidy up"), workspace)[1] is None
    assert dispatch.order_tasks_by_impact(_tasks("a", "b"), workspace)[1] is None
    # one task
    assert dispatch.order_tasks_by_impact(_tasks("OBJ-1"), workspace)[1] is None
    # a workspace with no objectives file at all
    plain = tmp_path / "plain"
    plain.mkdir()
    assert dispatch.order_tasks_by_impact(_tasks("OBJ-2", "OBJ-1"), str(plain)) == (
        _tasks("OBJ-2", "OBJ-1"), None)


@pytest.mark.parametrize("tasks,ws", [
    (None, None), ("not a list", ""), ([None, None], "/nope"),
    ([{"instruction": "OBJ-1"}, "a string"], "/nope"),
    ([{"instruction": "OBJ-1"}, {"instruction": "OBJ-2"}], 7),
])
def test_ordering_never_raises_into_a_job(tasks, ws):
    """A job may lose its ordering; it may never fail over it."""
    out, record = dispatch.order_tasks_by_impact(tasks, ws)
    assert out == list(tasks or []) and record is None


def test_a_corrupt_objectives_file_costs_the_ordering_not_the_job(tmp_path):
    ws = tmp_path / "broken"
    (ws / ".odysseus").mkdir(parents=True)
    with open(ws / ".odysseus" / "objectives.jsonl", "w", encoding="utf-8") as fh:
        fh.write('{"t":"obj","id":"OBJ-1"\nNOT JSON AT ALL\n')
    tasks = _tasks("finish OBJ-2", "finish OBJ-1")
    assert dispatch.order_tasks_by_impact(tasks, str(ws)) == (tasks, None)


# ── the job: parallel, the setting, the record and the verdict ──────────────

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


@pytest.fixture()
def box(tmp_path, monkeypatch, workspace):
    """One dispatched job with a fake delegation tool that records the task
    list it was handed."""
    import src.ai_interaction as ai
    from src.agent_tools import subagent_tools as st
    sm = _SM()
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path / "dispatch"))
    monkeypatch.setattr(dispatch, "resolve_route",
                        lambda owner, model=None: ("http://127.0.0.1:11434/v1", model or "qwen3.5:9b", None))
    state = {"sent": None, "ws": workspace, "sm": sm}

    class FakeTool:
        async def execute(self, content, ctx):
            state["sent"] = json.loads(content)
            return {"exit_code": 0, "subagents": [
                {"name": t["name"], "status": "done", "mutations": [], "final_text": "",
                 "rounds": 1, "tool_calls": 1, "failed_calls": 0, "input_tokens": 1,
                 "output_tokens": 1, "role": "worker"}
                for t in state["sent"]["tasks"]]}

    monkeypatch.setattr(st, "DelegateAgentsTool", FakeTool)
    dispatch.reset_for_tests()
    yield state
    dispatch.reset_for_tests()


def _run(box, **body):
    async def go():
        job = await dispatch.start("luis", {"workspace": box["ws"], "verify": False, **body})
        assert await dispatch.wait(job, 10)
        return job
    return asyncio.run(go())


def test_a_sequential_job_is_reordered_recorded_and_says_so(box):
    job = _run(box, parallel=False,
               tasks=["finish OBJ-3", "finish OBJ-1", "tidy the changelog", "finish OBJ-2"])
    sent = [t["instruction"] for t in box["sent"]["tasks"]]
    assert sent == ["finish OBJ-2", "finish OBJ-1", "tidy the changelog", "finish OBJ-3"]
    # recorded on the job, so the rearrangement is auditable rather than silent
    assert job.task_order["by"] == "impact"
    assert job.task_order["from"] == ["finish OBJ-3", "finish OBJ-1", "tidy the changelog", "finish OBJ-2"]
    assert job.task_order["to"] == ["finish OBJ-2", "finish OBJ-1", "tidy the changelog", "finish OBJ-3"]
    assert dispatch.compact(job)["task_order"] == job.task_order
    # …and said out loud
    assert "tasks ordered by objective impact: finish OBJ-2 → finish OBJ-1" in job.verdict
    # the mirror carries it, so a job read after a restart still explains itself
    dispatch.reset_for_tests()
    again = dispatch.get(job.id)
    assert again is not None and again.task_order == job.task_order


def test_a_parallel_job_is_left_exactly_as_it_was_sent(box):
    job = _run(box, parallel=True, tasks=["finish OBJ-3", "finish OBJ-1", "finish OBJ-2"])
    assert [t["instruction"] for t in box["sent"]["tasks"]] == [
        "finish OBJ-3", "finish OBJ-1", "finish OBJ-2"]
    assert job.task_order is None
    assert "task_order" not in dispatch.compact(job)
    assert "ordered by objective impact" not in (job.verdict or "")


def test_the_setting_off_reproduces_the_written_order(box, monkeypatch):
    monkeypatch.setattr(dispatch, "_setting",
                        lambda key, default: False if key == "agent_objective_ordering" else default)
    assert dispatch.objective_ordering_on() is False
    job = _run(box, parallel=False, tasks=["finish OBJ-3", "finish OBJ-1", "finish OBJ-2"])
    assert [t["instruction"] for t in box["sent"]["tasks"]] == [
        "finish OBJ-3", "finish OBJ-1", "finish OBJ-2"]
    assert job.task_order is None
    assert "task_order" not in dispatch.compact(job)
    assert "ordered by objective impact" not in (job.verdict or "")


def test_a_sequential_job_that_names_no_objective_is_untouched(box):
    job = _run(box, parallel=False, tasks=["tidy the changelog", "write the README"])
    assert [t["instruction"] for t in box["sent"]["tasks"]] == [
        "tidy the changelog", "write the README"]
    assert job.task_order is None


# ── the shared helper in services/objectives.py ─────────────────────────────

def test_mentioned_ids_reads_objective_ids_out_of_free_text():
    from services import objectives as obj
    assert obj.mentioned_ids("close OBJ-3, then OBJ-1, and OBJ-3 again") == ["OBJ-3", "OBJ-1"]
    assert obj.mentioned_ids("OBJ-12 not OBJ-1x, not SUBOBJ-2, not obj-4") == ["OBJ-12"]
    assert obj.mentioned_ids("") == [] and obj.mentioned_ids(None) == []
    assert obj.mentioned_ids(7) == [] and obj.mentioned_ids(["OBJ-1"]) == ["OBJ-1"]


def test_the_objectives_file_is_read_from_the_workspace_not_a_project_row(workspace):
    """The ordering asks the FOLDER, the same way the store defines itself —
    a dispatched job need not belong to a registered project to be ordered."""
    from services import objectives as obj
    state = obj.load_state({"workspace": workspace})
    assert sorted(state["objectives"]) == ["OBJ-1", "OBJ-2", "OBJ-3", "OBJ-4"]
    assert os.path.isfile(obj.objectives_path({"workspace": workspace}))
