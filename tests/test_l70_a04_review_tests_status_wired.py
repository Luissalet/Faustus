"""Lote 70a, punto A.4 — `routes/chat_routes.py::_record_turn_side_effects`
must pass this turn's real test outcome (`hz["tests"]`) into
`services/review_state.py::init(tests_status=...)`, which has accepted that
kwarg since VER-06 (lote 68/69b) but was never actually given one — the
review card's "Automatic tests" badge read "not run" for every turn, even
one whose tests really did run and fail.

End-to-end through the real function (no reimplementation of its logic):
call `_record_turn_side_effects` with a `metrics["harness"]` shaped exactly
as the agent loop produces it (review_mode on, a `tests` sub-dict, at least
one mutated file) and read back the registered `services/review_state`
entry.
"""
from __future__ import annotations

import importlib

import routes.chat_routes as chat_routes
import services.review_state as review_state


def _reset(tmp_path, monkeypatch):
    importlib.reload(review_state)
    monkeypatch.setattr(review_state, "_path", lambda: str(tmp_path / "review_state.json"))
    # chat_routes imports the module lazily (`from services import review_state`)
    # inside the function body, so patching the module object itself (not an
    # attribute on chat_routes) is enough to be seen there too.
    return review_state


def test_a_turn_with_failed_tests_records_tests_status_on_the_review_entry(tmp_path, monkeypatch):
    m = _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(chat_routes, "logger", chat_routes.logger)  # no-op, keeps intent explicit

    metrics = {
        "model": "gpt-4o",
        "harness": {
            "mutations": ["a.py"],
            "workspace": "/ws",
            "review_mode": True,
            "checkpoint": "deadbeef",
            "tests": {"ran": True, "ok": False, "inconclusive": False},
        },
    }
    chat_routes._record_turn_side_effects(
        "session-1", "msg-a04", metrics, "fix the bug", {"project_id": None},
    )

    entry = m.get("msg-a04")
    assert entry is not None, "review_state.init must have been called (review_mode + mutations)"
    assert entry["tests_status"] == {"ran": True, "ok": False, "inconclusive": False}


def test_a_turn_with_no_tests_leaves_tests_status_absent(tmp_path, monkeypatch):
    m = _reset(tmp_path, monkeypatch)

    metrics = {
        "model": "gpt-4o",
        "harness": {
            "mutations": ["b.py"],
            "workspace": "/ws",
            "review_mode": True,
            "checkpoint": "deadbeef",
            # No "tests" key at all — e.g. run_tests was off this turn.
        },
    }
    chat_routes._record_turn_side_effects(
        "session-1", "msg-a04-b", metrics, "tweak formatting", {"project_id": None},
    )

    entry = m.get("msg-a04-b")
    assert entry is not None
    assert entry["tests_status"] is None
