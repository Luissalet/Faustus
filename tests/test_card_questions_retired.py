"""A permission card is registered as an open question for the activity
tray; deciding the card never went through question_store, so those rows
stayed open for good and the notification tray filled with identical
"Allow this task to continue?" entries from finished chats."""
import routes.chat_routes as chat_routes
from src import question_store


def _open(tmp_path, monkeypatch):
    monkeypatch.setattr(question_store, "default_path", lambda: tmp_path / "q.sqlite3")
    card = question_store.open_question("Allow this task to continue?", session_id="s-card", owner="example-user")
    stale = question_store.open_question("Allow this task to continue?", session_id="s-old", owner="example-user")
    real = question_store.open_question("¿Qué carpeta uso?", session_id="s-other", owner="example-user")
    return card, stale, real


def _open_ids():
    return {q["question_id"] for q in question_store.list_open(owner="example-user")}


def test_deciding_a_card_closes_its_question(tmp_path, monkeypatch):
    card, stale, real = _open(tmp_path, monkeypatch)
    assert chat_routes.retire_card_questions("example-user", session_id="s-card") == 1
    assert _open_ids() == {stale["question_id"], real["question_id"]}


def test_cards_nobody_is_waiting_on_are_closed_on_listing(tmp_path, monkeypatch):
    card, stale, real = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(chat_routes.tool_approval_store, "pending_session_ids", lambda owner: ["s-card"])
    assert chat_routes.retire_card_questions("example-user") == 1
    assert _open_ids() == {card["question_id"], real["question_id"]}


def test_their_notifications_are_marked_read(tmp_path, monkeypatch):
    from routes import notifications_routes as nr
    monkeypatch.setattr(nr, "NOTIFICATIONS_FILE", str(tmp_path / "n.json"))
    card, stale, real = _open(tmp_path, monkeypatch)
    nr._save({"prefs": {}, "events": {"example-user": [
        {"id": "a", "dedupe_key": f"question:{card['question_id']}", "read": False},
        {"id": "b", "dedupe_key": f"question:{real['question_id']}", "read": False}]}})
    chat_routes.retire_card_questions("example-user", session_id="s-card")
    rows = {r["id"]: r["read"] for r in nr._load()["events"]["example-user"]}
    assert rows == {"a": True, "b": False}


def test_notifications_of_questions_that_no_longer_wait_are_settled(tmp_path, monkeypatch):
    """The tray showed 16 unread rows for questions decided a week before."""
    from routes import notifications_routes as nr
    monkeypatch.setattr(nr, "NOTIFICATIONS_FILE", str(tmp_path / "n.json"))
    card, stale, real = _open(tmp_path, monkeypatch)
    question_store.cancel_question(card["question_id"], reason="decided", owner=None)
    nr._save({"prefs": {}, "events": {"example-user": [
        {"id": "a", "dedupe_key": f"question:{card['question_id']}", "read": False},
        {"id": "b", "dedupe_key": "question:qst_gone", "read": False},
        {"id": "c", "dedupe_key": f"question:{real['question_id']}", "read": False},
        {"id": "d", "dedupe_key": "task:x", "read": False}]}})
    assert nr.settle_closed_questions("example-user") == 2
    unread = {r["id"] for r in nr._load()["events"]["example-user"] if not r.get("read")}
    assert unread == {"c", "d"}
