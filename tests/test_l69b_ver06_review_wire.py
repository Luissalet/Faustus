"""Lote 69b — VER-06: the wire shape `services/review_state.status_payload`
produces is exactly what `studio/src/adapters/review.ts::reviewStateFrom`
expects (message_id, pending/accepted/rejected, approvals[].diff_sha256/
.stale, tests_status kept separate from human approval). No HTTP layer
needed — this module IS the shape the route serializes verbatim.

Demonstrates the hueco first (a bare `init()` carries no `tests_status`
today — see "Cambios necesarios en ficheros ajenos" in the batch report:
`routes/chat_routes.py::_record_turn_side_effects` never passes
`tests_status=` to `review_state.init`), then the full contract once a
caller does supply one.
"""
import importlib

import services.review_state as rs


def _reset(tmp_path, monkeypatch):
    importlib.reload(rs)
    monkeypatch.setattr(rs, "_path", lambda: str(tmp_path / "review_state.json"))
    return rs


def test_a_turn_with_no_files_never_registers(tmp_path, monkeypatch):
    m = _reset(tmp_path, monkeypatch)
    assert m.get("no-such-message") is None


def test_pending_accept_reject_and_approval_signature(tmp_path, monkeypatch):
    m = _reset(tmp_path, monkeypatch)
    m.init("msg1", session_id="s1", workspace="/ws", files=["a.py", "b.py"], checkpoint="deadbeef")
    entry = m.get("msg1")
    assert entry["pending"] == ["a.py", "b.py"]

    m.decide("msg1", "a.py", "accept", content="print(1)\n")
    m.decide("msg1", "b.py", "reject")
    entry = m.get("msg1")
    assert entry["pending"] == []
    assert entry["accepted"] == ["a.py"]
    assert entry["rejected"] == ["b.py"]

    payload = m.status_payload("msg1", entry, current_content={"a.py": "print(1)\n"})
    # The exact keys studio/src/adapters/review.ts::stateFrom reads.
    assert payload["message_id"] == "msg1"
    assert payload["pending"] == []
    assert payload["accepted"] == ["a.py"]
    assert payload["rejected"] == ["b.py"]
    approvals = {a["path"]: a for a in payload["approvals"]}
    assert approvals["a.py"]["diff_sha256"]
    assert approvals["a.py"]["stale"] is False


def test_editing_the_file_after_accept_marks_the_approval_stale(tmp_path, monkeypatch):
    m = _reset(tmp_path, monkeypatch)
    m.init("msg2", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")
    m.decide("msg2", "a.py", "accept", content="version one\n")
    entry = m.get("msg2")
    # Someone edited the file after the human approved this exact content.
    payload = m.status_payload("msg2", entry, current_content={"a.py": "version two\n"})
    approval = payload["approvals"][0]
    assert approval["stale"] is True, "changing the diff after accept must invalidate the old approval"


def test_tests_status_is_echoed_separately_from_human_approval(tmp_path, monkeypatch):
    """VER-06: 'Aceptar manualmente no convierte tests fallidos en exitosos.'
    A human accept never writes into tests_status, and tests_status never
    feeds back into human_approved — they are read as two independent facts."""
    m = _reset(tmp_path, monkeypatch)
    m.init("msg3", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef",
           tests_status={"ran": True, "ok": False, "inconclusive": False})
    m.decide("msg3", "a.py", "accept", content="x\n")
    entry = m.get("msg3")
    payload = m.status_payload("msg3", entry, current_content={"a.py": "x\n"})
    assert payload["tests_status"] == {"ran": True, "ok": False, "inconclusive": False}
    assert "a.py" in payload["accepted"], "a human can still accept a file whose tests failed"


def test_chat_routes_now_passes_tests_status(monkeypatch):
    """Lote 70a, punto A.4 closed the gap this test used to document (see
    tests/test_l70_a04_review_tests_status_wired.py for the end-to-end
    proof): routes/chat_routes.py::_record_turn_side_effects now forwards
    hz["tests"] into review_state.init(tests_status=...), so the UI's
    "Automatic tests" badge reflects a turn's real test outcome instead of
    always reading "not run"."""
    import inspect

    import routes.chat_routes as chat_routes
    src = inspect.getsource(chat_routes._record_turn_side_effects)
    call = src.split("review_state.init(", 1)[1]
    call = call[: call.index("\n\n") if "\n\n" in call else len(call)]
    assert "tests_status=hz.get(\"tests\")" in call
