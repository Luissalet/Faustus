"""Heartbeat disk writes yield to the loop and cannot outlive lease removal."""
import asyncio
import json
import threading
import time
import types

import pytest

from src import model_lease as ml


class _NoSocketLoop(asyncio.SelectorEventLoop):
    """Real asyncio scheduling with no socket/self-pipe in these CPU tests."""
    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        pass

    def __init__(self):
        super().__init__()
        self._selector.select = lambda timeout=None: (
            time.sleep(min(timeout or 0, .002)), [])[1]


def _run(coro):
    with asyncio.Runner(loop_factory=_NoSocketLoop) as runner:
        return runner.run(coro)


@pytest.fixture()
def lease(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_SHARED_DIR", str(tmp_path))
    monkeypatch.setenv("APP_PORT", "64918")
    monkeypatch.setattr(ml, "_get_setting", lambda key, default: default)
    ml.reset_for_tests()
    yield tmp_path
    ml.reset_for_tests()


async def _wait(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(.002)


def _ticks(monkeypatch):
    first = asyncio.Event()
    later = asyncio.Event()
    intervals = []

    async def sleep(interval):
        intervals.append(interval)
        await (first if len(intervals) == 1 else later).wait()

    proxy = types.SimpleNamespace(**{n: getattr(asyncio, n) for n in dir(asyncio)})
    proxy.sleep = sleep
    monkeypatch.setattr(ml, "asyncio", proxy)
    return first, intervals


def test_held_heartbeat_write_allows_loop_progress(lease, monkeypatch):
    async def scenario():
        tick, intervals = _ticks(monkeypatch)
        ml.start()
        real_write = ml._atomic_write
        entered, progress, release = (threading.Event() for _ in range(3))
        observations = []

        def held(path, record):
            entered.set()
            assert release.wait(5), "owned write was not released"
            real_write(path, record)

        def controller():
            assert entered.wait(5)
            observations.append(progress.wait(1))
            release.set()

        monkeypatch.setattr(ml, "_atomic_write", held)
        watcher = threading.Thread(target=controller)
        watcher.start()
        try:
            tick.set()
            await _wait(entered)
            progress.set()
            await asyncio.to_thread(watcher.join, 5)
            assert observations == [True], "disk write prevented the loop from making progress"
            async with asyncio.timeout(5):
                while len(intervals) < 2:
                    await asyncio.sleep(.002)
            assert intervals == [10.0, 10.0]
            path = ml._lease_path(ml.self_record()["instance_id"])
            with open(path, encoding="utf-8") as fh:
                assert json.load(fh)["heartbeat"] == ml.self_record()["heartbeat"]
        finally:
            release.set()
            watcher.join(5)
            await ml.stop()

    _run(scenario())


def test_concurrent_repeated_stop_joins_heartbeat_before_removing_lease(lease, monkeypatch):
    async def scenario():
        tick, _ = _ticks(monkeypatch)
        ml.start()
        path = ml._lease_path(ml.self_record()["instance_id"])
        real_write, real_remove = ml._atomic_write, ml.os.remove
        entered, release, finished = (threading.Event() for _ in range(3))
        order = []

        def held(path, record):
            entered.set()
            assert release.wait(5)
            real_write(path, record)
            order.append("write")
            finished.set()

        def remove(target):
            if target == path:
                assert finished.is_set()
                order.append("remove")
            real_remove(target)

        monkeypatch.setattr(ml, "_atomic_write", held)
        monkeypatch.setattr(ml.os, "remove", remove)
        tick.set()
        await _wait(entered)
        first = asyncio.create_task(ml.stop())
        second = asyncio.create_task(ml.stop())
        try:
            for _ in range(3):
                await asyncio.sleep(.005)
                first.cancel()
                second.cancel()
            assert not first.done() and not second.done()
            assert ml.os.path.exists(path)
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert order == ["write", "remove"]
        assert not ml.os.path.exists(path)
        assert ml.self_record() == {} and ml._HEARTBEAT_TASK is None
        await ml.stop()
        ml.note_pin("http://127.0.0.1:11434", "fixture")
        assert not ml.os.path.exists(path)

    _run(scenario())


def test_failed_heartbeat_write_is_best_effort_and_next_tick_is_scheduled(lease, monkeypatch):
    async def scenario():
        tick, intervals = _ticks(monkeypatch)
        ml.start()
        path = ml._lease_path(ml.self_record()["instance_id"])
        failures = []

        def denied(path, record):
            failures.append(record)
            raise OSError("owned fixture write failed")

        monkeypatch.setattr(ml, "_atomic_write", denied)
        try:
            tick.set()
            async with asyncio.timeout(5):
                while len(intervals) < 2:
                    await asyncio.sleep(.002)
            assert len(failures) == 1 and intervals == [10.0, 10.0]
            assert not ml._HEARTBEAT_TASK.done()
        finally:
            await ml.stop()
        assert not ml.os.path.exists(path)

    _run(scenario())


def test_flush_serializes_snapshots_so_a_slow_write_cannot_restore_old_state(lease, monkeypatch):
    ml.start()
    path = ml._lease_path(ml.self_record()["instance_id"])
    real_write = ml._atomic_write
    entered, release, second_done = (threading.Event() for _ in range(3))
    failures = []

    def held(path, record):
        if record["default"] == {}:
            entered.set()
            assert release.wait(5)
        real_write(path, record)

    def flush(done=None):
        try:
            ml._flush(force=True)
        except BaseException as exc:
            failures.append(exc)
        finally:
            if done is not None:
                done.set()

    monkeypatch.setattr(ml, "_atomic_write", held)
    first = threading.Thread(target=flush)
    second = threading.Thread(target=flush, args=(second_done,))
    try:
        first.start()
        assert entered.wait(5)
        with ml._LOCK:
            ml._state["record"]["default"] = {"model": "new state"}
        second.start()
        # With serial writes the second waits; without them it completes
        # first and the held stale snapshot overwrites it on release.
        second_done.wait(.1)
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert not first.is_alive() and not second.is_alive() and failures == []
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["default"] == {"model": "new state"}
    _run(ml.stop())
