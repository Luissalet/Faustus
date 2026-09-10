"""VER-06 — a human accepting a change is not the same fact as a test suite
passing: services/review_state.py records `human_approved` separately from
any automatic verification, and a later edit can invalidate the recorded
approval signature."""
import inspect

import pytest

from services import review_state as rs


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


def test_accept_sets_human_approved_never_a_key_called_verified(data_dir):
    rs.init("m1", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")

    entry = rs.decide("m1", "a.py", "accept", content="print('hello')")

    assert rs.is_human_approved(entry, "a.py") is True
    assert "verified" not in entry
    assert "verified" not in entry["human_approved"]["a.py"]


def test_reject_does_not_mark_human_approved(data_dir):
    rs.init("m2", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")

    entry = rs.decide("m2", "a.py", "reject")

    assert rs.is_human_approved(entry, "a.py") is False


def test_content_drift_after_approval_invalidates_it(data_dir):
    rs.init("m3", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")
    entry = rs.decide("m3", "a.py", "accept", content="version one")

    assert rs.approval_still_valid(entry, "a.py", "version one") is True
    assert rs.approval_still_valid(entry, "a.py", "version TWO, edited after approval") is False


def test_approval_without_a_captured_signature_cannot_be_contradicted(data_dir):
    # An older caller that does not pass `content=` at accept time: nothing
    # was captured, so drift can never be detected — and this function must
    # not manufacture a False out of nothing to compare.
    rs.init("m4", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")
    entry = rs.decide("m4", "a.py", "accept")

    assert rs.is_human_approved(entry, "a.py") is True
    assert rs.approval_still_valid(entry, "a.py", "anything at all") is True


def test_decide_keeps_its_existing_three_positional_arg_call_shape():
    # routes/workspace_routes.py calls rs.decide(message_id, path, decision)
    # positionally — the new `content` keyword must not break that call.
    sig = inspect.signature(rs.decide)
    params = list(sig.parameters)
    assert params[:3] == ["message_id", "path", "decision"]
    assert sig.parameters["content"].kind == inspect.Parameter.KEYWORD_ONLY
