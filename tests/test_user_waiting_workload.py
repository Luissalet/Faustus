"""A model call made for a user who is waiting must not queue as background
work behind that user's own request (live 01-10: a bug hunt from the API sat
for half an hour, its "background" call waiting for foreground activity --
the bug hunt request itself -- to stop)."""
import asyncio
import threading

from src import interactive_gate as gate


def test_inside_a_tracked_request_the_user_is_waiting(monkeypatch):
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "true")
    seen = {}

    async def main():
        assert gate.workload_for("background") == "background"
        async with gate.track_interactive_request("/api/bug-hunt", "POST"):
            seen["inside"] = gate.workload_for("background")
            # a thread started from the request (asyncio.to_thread copies the context)
            seen["thread"] = await asyncio.to_thread(gate.workload_for, "background")
            # a task created inside it
            seen["task"] = await asyncio.create_task(asyncio.sleep(0, result=gate.workload_for()))
        seen["after"] = gate.workload_for("background")

    asyncio.run(main())
    assert seen == {"inside": "foreground", "thread": "foreground", "task": "foreground", "after": "background"}


def test_work_started_outside_a_request_stays_background():
    out = []
    t = threading.Thread(target=lambda: out.append(gate.workload_for("background")))
    t.start()
    t.join()
    assert out == ["background"] and gate.user_waiting() is False


def test_bug_hunt_and_workflow_model_calls_do_not_hardcode_background():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert 'workload="background"' not in (root / "src/bug_hunt.py").read_text(encoding="utf-8")
    for rel in ("src/workflows/model_calls.py", "src/workflows/agent_turn.py",
                "src/ci_failures.py", "src/brain/wiki.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "workload_for" in text, rel
