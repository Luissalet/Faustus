"""Keep graceful adapter fallbacks distinct from successful empty snapshots.

Context-local rather than adapter instance state: refreshes for two users can
run concurrently on a shared adapter without borrowing each other's failures.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_failures = ContextVar('state_mirror_read_failures', default=None)


@contextmanager
def capture_failures():
    failures = set()
    token = _failures.set(failures)
    try:
        yield failures
    finally:
        _failures.reset(token)


def note_failure(fn, exc):
    failures = _failures.get()
    if failures is not None and len(failures) < 16:
        # A diagnostic, not a copy of potentially private file paths or payloads.
        failures.add(f'{getattr(fn, "__name__", "read")}: {type(exc).__name__}')
