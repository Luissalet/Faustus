"""Studio lists canonical origins without exposing another owner's references."""
from types import SimpleNamespace

import pytest

from services.hoard_copilot import HoardCopilotStore
from services.projects import ProjectStore
from tests.test_session_export_routes import _add_session, harness  # noqa: F401


def context(app="writer", space="personal"):
    return {"origin": {"app_id": app, "app_name": app.title(),
                       "data_space_id": space, "surface": "editor"}}


@pytest.fixture
def origins(tmp_path, monkeypatch):
    store = HoardCopilotStore(str(tmp_path / "copilot.db"),
                             ProjectStore(str(tmp_path / "projects.json")))
    monkeypatch.setattr("services.hoard_copilot.get_store", lambda: store)
    monkeypatch.setattr("src.session_draft.DATA_DIR", tmp_path / "drafts")
    return store


def test_origins_are_filtered_by_both_live_session_and_owner(origins):
    origins.attach("own", context(), "alice")
    origins.attach("other", context("cicero"), "bob")
    origins.attach("gone", context("tantalus"), "alice")
    allowed = [SimpleNamespace(id="own", owner="alice"),
               SimpleNamespace(id="other", owner="alice"),
               SimpleNamespace(id="native", owner="alice")]
    assert origins.session_origins(iter(allowed)) == {"own": context()["origin"]}


def test_public_session_origin_stays_original_after_cross_app_turn(origins):
    origins.attach("shared", context(), "alice")
    origins.prepare_turn("shared", "alice", context("cicero", "slides"),
                         "turn-2", "Continue in the slides")
    allowed = [SimpleNamespace(id="shared", owner="alice")]
    assert origins.session_origins(allowed)["shared"] == context()["origin"]


def test_anonymous_origin_and_empty_inventory(origins):
    origins.attach("anonymous", context(), None)
    assert origins.session_origins([]) == {}
    assert origins.session_origins([SimpleNamespace(id="anonymous", owner=None)]) == {
        "anonymous": context()["origin"]}


def test_sessions_http_lists_origins_and_native_chats_without_cross_owner_leak(harness, origins):
    writer = _add_session(harness, name="Novel")
    cicero = _add_session(harness, name="Deck")
    native = _add_session(harness, name="Faustus chat")
    other = _add_session(harness, name="Other owner's chat", owner="bob")
    origins.attach(writer, context(), "alice")
    origins.attach(cicero, context("cicero", "slides"), "alice")
    origins.attach(other, context("tantalus"), "bob")
    # The reused export harness supplies real DB/routes; complete its in-memory
    # manager surface specifically for the ordinary session listing endpoint.
    for session in harness.sm.sessions.values():
        session.archived = False
        session.endpoint_url = "http://localhost"
        session.rag = False
    harness.sm.get_sessions_for_user.side_effect = lambda owner: {
        sid: session for sid, session in harness.sm.sessions.items()
        if session.owner == owner}
    response = harness.client.get("/api/sessions")
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()}
    assert set(rows) == {writer, cicero, native}
    assert rows[writer]["hoard_origin"] == context()["origin"]
    assert rows[cicero]["hoard_origin"] == context("cicero", "slides")["origin"]
    assert rows[native]["hoard_origin"] is None
    assert rows[writer]["name"] == "Novel"
    assert rows[writer]["message_count"] == 3


def test_text_patch_preserves_existing_studio_attachments(harness, origins):
    sid = _add_session(harness, name="Shared draft")
    saved = harness.client.put(f"/api/sessions/{sid}/draft", json={
        "text": "Studio text", "attachment_ids": ["studio-file", "studio-image"]})
    assert saved.status_code == 200
    revised = harness.client.patch(f"/api/sessions/{sid}/draft", json={"text": "Writer text"})
    assert revised.status_code == 200, revised.text
    assert revised.json()["attachment_ids"] == ["studio-file", "studio-image"]
    assert revised.json()["text"] == "Writer text"
    # Clearing only text still leaves the shared Studio attachment selection.
    cleared = harness.client.patch(f"/api/sessions/{sid}/draft", json={"text": ""})
    assert cleared.json()["attachment_ids"] == ["studio-file", "studio-image"]


def test_explicit_patch_attachment_clear_and_old_put_replacement_remain_available(harness, origins):
    sid = _add_session(harness, name="Shared draft")
    harness.client.put(f"/api/sessions/{sid}/draft", json={"text": "words", "attachment_ids": ["file"]})
    patched = harness.client.patch(f"/api/sessions/{sid}/draft", json={"text": "words", "attachment_ids": []})
    assert patched.status_code == 200
    assert patched.json()["attachment_ids"] == []
    harness.client.put(f"/api/sessions/{sid}/draft", json={"text": "words", "attachment_ids": ["file"]})
    replaced = harness.client.put(f"/api/sessions/{sid}/draft", json={"text": ""})
    assert replaced.json() == {"text": "", "attachment_ids": [], "updated_at": 0}


def test_draft_patch_cannot_access_other_owner_or_unknown_session(harness, origins):
    sid = _add_session(harness, name="Private", owner="bob")
    assert harness.client.patch(f"/api/sessions/{sid}/draft", json={"text": "intrusion"}).status_code == 404
    assert harness.client.patch("/api/sessions/unknown/draft", json={"text": "intrusion"}).status_code == 404
