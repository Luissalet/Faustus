"""Process-local exclusion for participating filesystem mutation workers.

This is neither ownership nor an OS lock. Other processes, hardlink aliases and
changes to path resolution are outside its guarantees.
"""
from contextlib import contextmanager
import os
import threading
import weakref


_GUARD = threading.Lock()
_LOCKS = weakref.WeakValueDictionary()


def canonical_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


@contextmanager
def mutation_lock(path):
    """Keep a strong reference through acquisition, waiting and release.

    The guarded weak registry retains no paths once their users leave. Lookup
    and creation share a guard so concurrent waiters cannot get different locks.
    Call only in synchronous workers; never hold this lock across an await.
    """
    key = canonical_path(path)
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
    with lock:
        yield
