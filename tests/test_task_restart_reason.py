"""A task run cut short by a restart is not "Stopped by user": the scheduler
clears `_running` before anything is cancelled on shutdown, and the reason
follows that flag."""

from src import task_scheduler as ts


def _sched(running: bool):
    s = object.__new__(ts.TaskScheduler)
    s._running = running
    return s


def test_cancel_while_running_is_the_user():
    assert _sched(True)._cancel_reason() == "Stopped by user"


def test_cancel_while_stopping_is_a_restart():
    assert _sched(False)._cancel_reason() == "Interrupted: Faustus was restarting"
