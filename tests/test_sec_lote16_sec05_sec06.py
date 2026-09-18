"""Lote 16 (Seguridad) - SEC-05 / SEC-06.

SEC-05: secrets never leak into logs/events/API responses. `src/mcp_manager.py`
(owned) is the piece the lot brief names by name - `build_server_env`, the
function every stdio MCP server's environment is built from. This file
proves Faustus's own internal token survives neither the "inherit everything"
path nor a server config that explicitly (and unfilterably) tries to name it.

SEC-06: owner isolation. `src/question_store.py` (NOT owned by this lote) is
one of the "new routes from lotes 2-10" the brief explicitly asks to be
walked - and it has no owner check at all on read/resolve/cancel, unlike its
sibling `src/tool_approvals.py`. That gap is demonstrated here (xfail,
strict) rather than patched, per the hard rule against touching files this
lote does not own; see the final report for the precise fix. `src/chat_outbox.py`
(also a "new route", also not owned) is checked too, as the positive control
showing the pattern that got this right.
"""
from __future__ import annotations

import pytest

from src import mcp_manager
from src.native_env import FAUSTUS_PRIVATE_NAMES


# ── SEC-05 ────────────────────────────────────────────────────────────────

def test_build_server_env_inherit_mode_never_leaks_the_internal_token(monkeypatch):
    """inherit_env=True is `{**os.environ, **declared}` before scrubbing - the
    real internal token IS in os.environ (every running Faustus process has
    it), so this is the path that actually matters, not a synthetic case."""
    monkeypatch.setenv("FAUSTUS_INTERNAL_TOKEN", "the-real-secret-token")
    monkeypatch.setenv("ODYSSEUS_INTERNAL_TOKEN", "legacy-secret-alias")
    env = mcp_manager.build_server_env({"MY_SERVER_VAR": "hello"}, inherit_env=True)
    assert env is not None
    assert "FAUSTUS_INTERNAL_TOKEN" not in env
    assert "ODYSSEUS_INTERNAL_TOKEN" not in env
    assert "the-real-secret-token" not in env.values()
    assert env["MY_SERVER_VAR"] == "hello"  # the server's own declared var still arrives


def test_build_server_env_strips_the_token_even_when_case_folded(monkeypatch):
    """Windows environment variable names are case-insensitive; a private
    name check keyed on exact-case would miss `faustus_internal_token`."""
    monkeypatch.setenv("FAUSTUS_INTERNAL_TOKEN", "the-real-secret-token")
    env = mcp_manager.build_server_env({"benign": "1"}, inherit_env=True)
    assert env is not None
    lowered_keys = {k.lower() for k in env}
    assert "faustus_internal_token" not in lowered_keys


@pytest.mark.parametrize("private_name", FAUSTUS_PRIVATE_NAMES)
def test_every_declared_private_name_is_scrubbed_from_the_merged_env(monkeypatch, private_name):
    monkeypatch.setenv(private_name, "leak-me-if-you-can")
    # A non-empty `declared` is what exercises the scrub path: inherit_env=True
    # with an empty declared dict returns None (the SDK's own curated safe
    # default; see mcp.client.stdio.get_default_environment), which never
    # touches os.environ at all and would make this test vacuous.
    env = mcp_manager.build_server_env({"x": "1"}, inherit_env=True)
    assert env is not None
    assert private_name not in env


# ── SEC-06 ────────────────────────────────────────────────────────────────

def test_chat_outbox_is_correctly_owner_scoped_positive_control(tmp_path, monkeypatch):
    """The pattern question_store should have followed: every read is
    filtered by `owner` in the SQL itself, so a second owner asking about the
    same (session_id, client_message_id) gets nothing."""
    from src import chat_outbox

    monkeypatch.setattr(chat_outbox, "default_path", lambda: tmp_path / "outbox.sqlite3")
    chat_outbox.record_intent(owner="alice", session_id="s1", client_message_id="m1")

    assert chat_outbox.get(owner="alice", session_id="s1", client_message_id="m1") is not None
    assert chat_outbox.get(owner="bob", session_id="s1", client_message_id="m1") is None


def test_owner_b_cannot_read_or_resolve_owner_as_open_question(tmp_path):
    """SEC-06, closed by lote 20 (integration): Store.get/resolve/cancel now
    take an optional `owner` and reject on mismatch the same way
    `src.tool_approvals.PendingApprovalStore.consume` does (`_normalized_owner`
    equality, checked before anything else). `routes/chat_routes.py`'s
    `question_store.resolve_question(question_id, answer, owner=owner)` call
    site (~1930) now passes it too. This file used to carry a strict xfail
    proving the gap (see docs/spec/v2/SEC_ESTADO.md); it is a green
    regression test now that both fixes landed."""
    from src.question_store import Store

    store = Store(tmp_path / "questions.db")
    opened = store.open("What's the deploy target?", session_id="sess-a", owner="alice")
    qid = opened["question_id"]

    # B has no legitimate route to this id, but if one leaks, the store itself
    # is A's last line of defence.
    seen_by_bob = store.get(qid, owner="bob")
    assert seen_by_bob is None, "question_store.get() leaks another owner's open question"

    resolved_by_bob = store.resolve(qid, {"text": "prod"}, owner="bob")
    assert resolved_by_bob.get("ok") is not True, (
        "question_store.resolve() let an unrelated caller answer another owner's question"
    )
    assert resolved_by_bob.get("reason") == "not_found"

    cancelled_by_bob = store.cancel(qid, owner="bob")
    assert cancelled_by_bob.get("ok") is not True, (
        "question_store.cancel() let an unrelated caller cancel another owner's question"
    )
    assert cancelled_by_bob.get("reason") == "not_found"

    # A's own access is completely unaffected.
    seen_by_alice = store.get(qid, owner="alice")
    assert seen_by_alice is not None
    resolved_by_alice = store.resolve(qid, {"text": "prod"}, owner="alice")
    assert resolved_by_alice.get("ok") is True

    # A caller that never passes `owner` at all (back-compat: single-user /
    # no-auth mode, or code that predates SEC-06) is unaffected either.
    store2 = Store(tmp_path / "questions2.db")
    legacy = store2.open("no owner tracked", session_id="sess-b")
    assert store2.get(legacy["question_id"]) is not None
    assert store2.resolve(legacy["question_id"], {"text": "ok"}).get("ok") is True
