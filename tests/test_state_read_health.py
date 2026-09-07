from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from src.state_mirror.read_health import capture_failures, note_failure


def test_read_failure_capture_is_nested_and_bounded():
    with capture_failures() as outer:
        note_failure(test_read_failure_capture_is_nested_and_bounded, OSError())
        with capture_failures() as inner:
            assert not inner
            for index in range(100):
                fn = lambda: None
                fn.__name__ = f'read{index}'
                note_failure(fn, ValueError())
            assert len(inner) == 16
        assert len(outer) == 1
    with capture_failures() as fresh:
        assert not fresh


def test_parallel_refreshes_do_not_share_read_failures():
    barrier = Barrier(2)
    def refresh(fail):
        with capture_failures() as failures:
            if fail:
                note_failure(refresh, PermissionError())
            barrier.wait(timeout=5)
            return set(failures)
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed, healthy = pool.submit(refresh, True), pool.submit(refresh, False)
        assert failed.result() == {'refresh: PermissionError'}
        assert not healthy.result()
