"""OPS-05 (lote 37): `app.py`'s real lifespan (`_startup_event`) tells
`src/safe_mode.py` when a boot starts and when it actually finished, not
just `launcher.py` (which already did this, but only around importing
`app.py` — BEFORE uvicorn ever runs this lifespan, so it cannot see a crash
inside `_startup_event` itself). Most real launch paths (Docker's
`CMD ["uvicorn", "app:app", ...]`, `start-macos.sh`, `launch-windows.ps1`,
any bare `uvicorn app:app`) invoke this lifespan WITHOUT going through
`launcher.py` at all — for them this is the only boot-failure signal that
ever gets recorded.

`_startup_event` spawns a dozen real background tasks and touches the real
database (see tests/test_shutdown_supervisor.py's own docstring on why); no
test in this repo calls it directly end-to-end, and this one does not either
— it proves the two calls exist, are guarded (a `safe_mode` hiccup must
never block booting the app), and sit in the right place: `mark_boot_started`
before the first risky step, `mark_boot_completed` only after startup is
actually done. `src/safe_mode.py`'s own contract (two unfinished boots in a
row -> next boot is safe mode) is already covered by
`tests/test_ops_safe_mode.py` and `tests/qa/test_qa_46_plugin_problematico.py`
— this file is only the wiring between `app.py` and that contract.
"""
from __future__ import annotations

import ast
import inspect

import pytest


def _startup_function_source() -> str:
    import app as app_module

    return inspect.getsource(app_module._startup_event)


def _find_calls(tree: ast.AST, attr_name: str) -> list[ast.Call]:
    """Every `Call` node anywhere in `tree` whose callee is `<something>.<attr_name>(...)`."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr_name
        ):
            found.append(node)
    return found


@pytest.fixture(scope="module")
def startup_tree():
    return ast.parse(_startup_function_source())


def test_mark_boot_started_is_called_exactly_once():
    tree = ast.parse(_startup_function_source())
    calls = _find_calls(tree, "mark_boot_started")
    assert len(calls) == 1, "safe_mode.mark_boot_started() must be called exactly once in _startup_event"


def test_mark_boot_completed_is_called_exactly_once():
    tree = ast.parse(_startup_function_source())
    calls = _find_calls(tree, "mark_boot_completed")
    assert len(calls) == 1, "safe_mode.mark_boot_completed() must be called exactly once in _startup_event"


def test_mark_boot_started_happens_before_mark_boot_completed():
    tree = ast.parse(_startup_function_source())
    started = _find_calls(tree, "mark_boot_started")[0]
    completed = _find_calls(tree, "mark_boot_completed")[0]
    assert started.lineno < completed.lineno


def test_mark_boot_started_runs_before_the_first_named_startup_step():
    """"Early in startup, before anything risky runs" — `mark_boot_started`'s
    own docstring. `install_secret_redaction()` is the first real startup
    action in `_startup_event` today; the boot marker must precede it, not
    follow, or a crash in an earlier step would go unrecorded."""
    tree = ast.parse(_startup_function_source())
    started = _find_calls(tree, "mark_boot_started")[0]
    redaction_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "install_secret_redaction"
    ]
    assert redaction_calls, "install_secret_redaction() call not found — has _startup_event changed shape?"
    assert started.lineno < redaction_calls[0].lineno


def test_mark_boot_completed_runs_after_startup_tasks_are_recorded():
    """"Startup actually finished" — `mark_boot_completed`'s own docstring.
    `app.state._startup_tasks = _supervisor.live()` is the last thing
    `_startup_event` records today; completion must be marked at or after
    that point, never earlier (which would call a boot "finished" while
    later startup steps could still fail)."""
    tree = ast.parse(_startup_function_source())
    completed = _find_calls(tree, "mark_boot_completed")[0]
    startup_tasks_assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "_startup_tasks"
            for t in node.targets
        )
    ]
    assert startup_tasks_assignments, "app.state._startup_tasks assignment not found — has _startup_event changed shape?"
    assert completed.lineno >= startup_tasks_assignments[0].lineno


@pytest.mark.parametrize("attr_name", ["mark_boot_started", "mark_boot_completed"])
def test_each_call_is_wrapped_in_a_try_that_never_lets_safe_mode_block_boot(attr_name):
    """A settings-write hiccup inside `safe_mode` must never be the reason
    the app fails to start — both calls sit inside their own `try/except`,
    mirroring `launcher.py`'s own best-effort pattern for the same two
    calls."""
    tree = ast.parse(_startup_function_source())
    call = _find_calls(tree, attr_name)[0]

    def _contains(node: ast.AST, target: ast.AST) -> bool:
        return any(child is target for child in ast.walk(node))

    tries = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    assert any(_contains(t, call) for t in tries), (
        f"safe_mode.{attr_name}() must be called inside a try/except so it can never block startup"
    )
