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


def watched_dirs() -> Sequence[Path]:
    """Directories the eval suite must never write to: the repository's own
    ``data`` directory (the default data dir of a developer checkout)."""
    return [d for d in (REPO / "data",) if d.exists()]


def guard(extra_dirs: Sequence[Path] = ()) -> Iterator[None]:
    before = hp.capture_process_state([*watched_dirs(), *extra_dirs])
    yield
    after = hp.capture_process_state([*watched_dirs(), *extra_dirs])
    leaks = hp.state_leaks(before, after)
    if leaks:
        pytest.fail("test leaked global state:\n  " + "\n  ".join(f"{v.kind}: {v.detail}" for v in leaks),
                    pytrace=False)
