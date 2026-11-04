"""Isolation guard for the eval suite (H24).

No bench or eval test may leave process-wide state behind: a changed working
directory, a changed environment variable the app reads, stand-in modules left
in ``sys.modules``, or files added/removed/changed in a directory that belongs
to the developer rather than to the test. ``guard`` is the body of the autouse
fixture in ``tests/eval/conftest.py``; it is a plain generator so a test can
drive it directly and prove it fails on a leak.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Sequence

import pytest

from src.bench import harness_pair as hp

REPO = Path(__file__).resolve().parents[2]


def _served_by_live_instance(data: Path) -> bool:
    """True while a Faustus server started from this checkout is running.

    That server keeps writing to the same ``data`` directory (its log, run
    records, databases), so a before/after stamp of the directory cannot tell
    its writes from a test's and the guard would fail at random. The record
    is the one ``server_runtime`` keeps; only a live, verified owner counts.
    """
    if data.resolve() != (REPO / "data").resolve():
        return False
    try:
        import server_runtime
        record = server_runtime.read_record()
        return bool(record) and server_runtime.owned_process(record) is not None
    except Exception:  # noqa: BLE001 - unknown means keep watching
        return False


def watched_dirs() -> Sequence[Path]:
    """Directories the eval suite must never write to: the repository's own
    ``data`` directory (the default data dir of a developer checkout), unless
    a live server from this checkout is writing to it at the same time."""
    return [d for d in (REPO / "data",) if d.exists() and not _served_by_live_instance(d)]


def guard(extra_dirs: Sequence[Path] = ()) -> Iterator[None]:
    dirs = [*watched_dirs(), *extra_dirs]  # decided once, so both stamps match
    before = hp.capture_process_state(dirs)
    yield
    after = hp.capture_process_state(dirs)
    leaks = hp.state_leaks(before, after)
    if leaks:
        pytest.fail("test leaked global state:\n  " + "\n  ".join(f"{v.kind}: {v.detail}" for v in leaks),
                    pytrace=False)
