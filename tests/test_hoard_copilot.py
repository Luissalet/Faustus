"""Real references, data-space separation and context replay contracts."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.hoard_copilot import CopilotError, HoardCopilotStore, context_message
from services.projects import ProjectStore


def context(space="live", app="writer", project="novel"):
    return {"origin": {"app_id": app, "data_space_id": space, "surface": "editor"},
            "project": {"external_project_id": project, "title": "Same title"},
            "active_objects": [{"kind": "chapter", "object_id": "ch3", "revision": "17"}],
            "selection": {"text": "before", "revision": "17"}}


@pytest.fixture
def store(tmp_path):
    return HoardCopilotStore(str(tmp_path / "copilot.db"), ProjectStore(str(tmp_path / "projects.json")))


def test_repeated_context_reuses_project_and_no_workspace(store):
    first = store.resolve_project(context(), "owner")
    second = store.resolve_project(context(), "owner")
    assert first["project_id"] == second["project_id"]
    assert first["project_link_id"] == second["project_link_id"]
    assert store.project_store().get(first["project_id"], "owner")["workspace"] == ""


def test_same_names_in_apps_and_spaces_do_not_merge(store):
    resolved = [store.resolve_project(value, "owner") for value in (
        context(), context(space="qa"), context(app="cicero"), context(project="other"))]
    assert len({row["project_id"] for row in resolved}) == 4
    assert len(store.project_store().list("owner")) == 4


def test_app_context_does_not_require_a_project(store):
    value = context()
    value.pop("project")
    assert store.resolve_project(value, "owner")["project_id"] is None
    assert store.project_store().list("owner") == []


def test_concurrent_resolvers_make_one_project(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: store.resolve_project(context(), "owner"), range(8)))
    assert len({row["project_id"] for row in rows}) == 1
    assert len(store.project_store().list("owner")) == 1


def test_reopen_preserves_identity_and_context(tmp_path, store):
    resolved = store.resolve_project(context(), "owner")
    store.attach("sid", context(), "owner", resolved["project_link_id"])
    reopened = HoardCopilotStore(store.path, store.projects)
    assert reopened.get("sid", "owner")["context"] == {**context(), "contract_version": "1.0"}
    assert reopened.resolve_project(context(), "owner")["project_id"] == resolved["project_id"]
    assert reopened.get("sid", "other-owner") is None


def test_replay_freezes_selection_and_conflicting_send_is_detected(store):
    store.attach("sid", context(), "owner")
    frozen = store.prepare_turn("sid", "owner", context(), "send1", "rewrite")
    assert frozen["selection"]["text"] == "before"
    assert store.prepare_turn("sid", "owner", context(), "send1", "rewrite") == frozen
    changed = context()
    changed["selection"]["text"] = "different"
    with pytest.raises(CopilotError, match="different message or context"):
        store.prepare_turn("sid", "owner", changed, "send1", "rewrite")
    with pytest.raises(CopilotError):
        store.prepare_turn("sid", "owner", context(), "send1", "other message")


def test_studio_continuation_preserves_object_without_old_selection(store):
    store.attach("sid", context(), "owner")
    result = store.prepare_turn("sid", "owner", None, "send2", "continue")
    assert result["active_objects"][0]["object_id"] == "ch3"
    assert "selection" not in result
    assert "before" not in context_message(result)["content"]


def test_new_turn_does_not_mutate_previous_origin(store):
    store.attach("sid", context(), "owner")
    store.prepare_turn("sid", "owner", context(app="cicero"), "send2", "use deck")
    assert store.get("sid", "owner")["origin"]["app_id"] == "writer"
    assert store.get("sid", "owner")["context"]["origin"]["app_id"] == "cicero"


def test_deleted_project_is_not_replaced_by_same_name(store):
    resolved = store.resolve_project(context(), "owner")
    store.project_store().delete(resolved["project_id"], "owner")
    with pytest.raises(CopilotError) as error:
        store.resolve_project(context(), "owner")
    assert error.value.code == "reference_unavailable"


def test_switching_application_repeatedly_keeps_canonical_project(store):
    original = context()
    linked = store.resolve_project(original, "owner")
    store.attach("sid", original, "owner", linked["project_link_id"])
    for index in range(3):
        switched = store.prepare_turn("sid", "owner", context(app="cicero", project="deck"), f"send-{index}", "use deck")
        assert switched["conversation_project"]["external_project_id"] == "novel"
        assert switched["conversation_project"]["faustus_project_id"] == linked["project_id"]


def test_another_app_taking_part_is_linked_to_the_conversation(store, monkeypatch):
    from types import SimpleNamespace
    from src import hoard_copilot_turn as turn
    import services.hoard_copilot as copilot
    monkeypatch.setattr(copilot, "_store", store)
    store.attach("sid", context(app="tantalus", space="t-space"), "owner")
    store.attach("other", context(app="writer"), "owner")
    manager = SimpleNamespace(_connections={"ac3e1027": {"name": "Cicero's Hoard"}, "t1": {"name": "Tantalus's Hoard"}})
    created = {"exit_code": 0, "output": json.dumps({"result": {"id": "deck-9", "title": "Resumen de la vigilancia", "slides": []}})}
    assert turn.note_participation(manager, "sid", "mcp__ac3e1027__deck_create", '{"title": "Resumen de la vigilancia"}', created)
    assert turn.note_participation(manager, "sid", "mcp__ac3e1027__deck_get", '{"deck_id": "deck-9"}', {"exit_code": 0, "output": "{}"})
    # The origin app itself, a failed call and a chat that is not a Hoard conversation add nothing.
    assert not turn.note_participation(manager, "sid", "mcp__t1__watcher_get", '{"watcher_id": "w1"}', {"exit_code": 0})
    assert not turn.note_participation(manager, "sid", "mcp__ac3e1027__deck_update", '{"deck_id": "deck-9"}', {"exit_code": 1})
    assert not turn.note_participation(manager, "studio-chat", "mcp__ac3e1027__deck_get", '{"deck_id": "deck-9"}', {"exit_code": 0})
    rows = store.participants("sid")
    assert [(r["app_id"], r["object_id"], r["kind"], r["title"], r["effect"]) for r in rows] == [
        ("cicero", "deck-9", "deck", "Resumen de la vigilancia", "write")]
    assert store.get("sid", "owner")["origin"]["app_id"] == "tantalus"
    assert [item["session_id"] for item in store.list("owner", participant_app_id="cicero")] == ["sid"]
    assert store.list("owner", participant_app_id="writer") == []


def test_a_cut_result_still_names_the_object_it_created(store, monkeypatch):
    from types import SimpleNamespace
    from src import hoard_copilot_turn as turn
    import services.hoard_copilot as copilot
    monkeypatch.setattr(copilot, "_store", store)
    store.attach("sid", context(app="tantalus", space="t-space"), "owner")
    manager = SimpleNamespace(_connections={"ac3e1027": {"name": "Cicero's Hoard"}})
    cut = '{"id": "01M4Y050", "ref": "hoard://cicero/deck/01M4Y050", "title": "Vigilancia X100VI", "brief": "' + "x" * 5000
    assert turn.note_participation(manager, "sid", "mcp__ac3e1027__deck_create", '{"title": "Vigilancia X100VI"}',
                                   {"exit_code": 0, "output": cut})
    assert [(r["object_id"], r["kind"], r["title"]) for r in store.participants("sid")] == [("01M4Y050", "deck", "Vigilancia X100VI")]
