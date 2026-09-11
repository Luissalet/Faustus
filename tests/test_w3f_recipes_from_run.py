"""tests/test_w3f_recipes_from_run.py — W3-F (CONTRATO_W3.md).

`src/recipes.py::from_run`'s ``inputs`` used to be a hardcoded
``["task description"]`` placeholder (documented as a known limitation in
`docs/adaptations/decisions/CMP-12.md`) because the run log only carries the
assistant side of a turn. This lot wires the REAL first user message in:
`from_run` reads the ``session_id`` the log's own first line already names
(`src/agent_runs.py::_RunLog`), then the earliest ``role="user"``
`core.database.ChatMessage` of that session — with its own secret redaction,
a privacy check against the session's owner, and a fallback to the old
placeholder everywhere that lookup cannot honestly succeed.

Real SQLAlchemy session (sqlite), like `tests/test_adp06_document_links.py`
and `tests/test_approval_store.py` — `core.database.SessionLocal` is
monkeypatched so `_first_user_message`'s own local
``from core.database import SessionLocal`` picks it up.

`-p no:cacheprovider -W ignore` per COMUN.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as db_mod
from core.database import Base, ChatMessage as DbChatMessage, Session as DbSession
from src import constants as constants_mod
from src import recipes


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    yield


@pytest.fixture()
def sa_session(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "w3f.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _make_session(sa_session, session_id: str, owner: str | None) -> None:
    sa_session.add(DbSession(
        id=session_id, name="t", endpoint_url="http://x", model="m", owner=owner,
    ))
    sa_session.commit()


def _add_message(sa_session, session_id: str, role: str, content: str, *, offset_seconds: int) -> None:
    sa_session.add(DbChatMessage(
        id=f"{session_id}-{role}-{offset_seconds}",
        session_id=session_id,
        role=role,
        content=content,
        timestamp=datetime(2026, 1, 1) + timedelta(seconds=offset_seconds),
    ))
    sa_session.commit()


def _write_run_log(tmp_path, run_id, *, session_id=None, finished=True, label="Fix the off-by-one"):
    runs_dir = os.path.join(str(tmp_path), "runs")
    os.makedirs(runs_dir, exist_ok=True)
    first_line = {"status": "running", "run_id": run_id, "lane": "local", "label": label}
    if session_id is not None:
        first_line["session_id"] = session_id
    lines = [
        first_line,
        {"seq": 1, "ev": 'data: {"type": "tool_start", "tool_type": "read_file"}\n\n'},
        {"seq": 2, "ev": 'data: {"type": "tool_start", "tool_type": "edit_file"}\n\n'},
    ]
    lines.append({"status": "done" if finished else "stopped", "ts": 1.0})
    path = os.path.join(runs_dir, f"{run_id}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


# ---------------------------------------------------------------------------
# The real first user message becomes `inputs`
# ---------------------------------------------------------------------------

def test_uses_the_earliest_user_message_of_the_run_s_session(tmp_path, sa_session):
    _make_session(sa_session, "sess-1", owner="alice")
    _add_message(sa_session, "sess-1", "user", "Rewrite the onboarding email to be shorter.", offset_seconds=0)
    _add_message(sa_session, "sess-1", "assistant", "Sure, working on it.", offset_seconds=5)
    _add_message(sa_session, "sess-1", "user", "Also fix the subject line.", offset_seconds=10)
    _write_run_log(tmp_path, "run-1", session_id="sess-1")

    recipe = recipes.from_run("run-1", "alice")

    assert recipe.inputs == ["Rewrite the onboarding email to be shorter."]


def test_falls_back_to_the_placeholder_with_no_session_id_in_the_log(tmp_path, sa_session):
    # No `session_id` on the log's first line at all (an older/foreign log
    # shape, or the log writer never got to it) — nothing to look up.
    _write_run_log(tmp_path, "run-2", session_id=None)

    recipe = recipes.from_run("run-2", "alice")

    assert recipe.inputs == ["task description"]


def test_falls_back_to_the_placeholder_when_the_session_does_not_exist(tmp_path, sa_session):
    _write_run_log(tmp_path, "run-3", session_id="does-not-exist")

    recipe = recipes.from_run("run-3", "alice")

    assert recipe.inputs == ["task description"]


def test_falls_back_to_the_placeholder_when_the_session_has_no_user_message(tmp_path, sa_session):
    _make_session(sa_session, "sess-4", owner="alice")
    _add_message(sa_session, "sess-4", "assistant", "Nothing from the user was ever logged.", offset_seconds=0)
    _write_run_log(tmp_path, "run-4", session_id="sess-4")

    recipe = recipes.from_run("run-4", "alice")

    assert recipe.inputs == ["task description"]


# ---------------------------------------------------------------------------
# Privacy: never another owner's message
# ---------------------------------------------------------------------------

def test_never_leaks_another_owner_s_first_message(tmp_path, sa_session):
    _make_session(sa_session, "sess-5", owner="bob")
    _add_message(sa_session, "sess-5", "user", "Bob's private project brief, not Alice's business.", offset_seconds=0)
    _write_run_log(tmp_path, "run-5", session_id="sess-5")

    recipe = recipes.from_run("run-5", "alice")

    assert recipe.inputs == ["task description"]
    assert "Bob" not in json.dumps(recipe.to_dict())


def test_a_legacy_shared_session_with_no_owner_is_still_readable(tmp_path, sa_session):
    _make_session(sa_session, "sess-6", owner=None)
    _add_message(sa_session, "sess-6", "user", "Shared/legacy chat, no owner column set.", offset_seconds=0)
    _write_run_log(tmp_path, "run-6", session_id="sess-6")

    recipe = recipes.from_run("run-6", "alice")

    assert recipe.inputs == ["Shared/legacy chat, no owner column set."]


# ---------------------------------------------------------------------------
# Content shape and secrets
# ---------------------------------------------------------------------------

def test_extracts_only_the_text_blocks_of_a_multimodal_message(tmp_path, sa_session):
    _make_session(sa_session, "sess-7", owner="alice")
    content = json.dumps([
        {"type": "text", "text": "Summarize this screenshot."},
        {"type": "image", "image_url": "data:image/png;base64,AAAA"},
    ])
    _add_message(sa_session, "sess-7", "user", content, offset_seconds=0)
    _write_run_log(tmp_path, "run-7", session_id="sess-7")

    recipe = recipes.from_run("run-7", "alice")

    assert recipe.inputs == ["Summarize this screenshot."]


def test_redacts_a_secret_in_the_db_message_separately_from_the_log(tmp_path, sa_session):
    _make_session(sa_session, "sess-8", owner="alice")
    _add_message(sa_session, "sess-8", "user", 'Use api_key: "sk-super-secret-value" to call the API.', offset_seconds=0)
    _write_run_log(tmp_path, "run-8", session_id="sess-8")

    recipe = recipes.from_run("run-8", "alice")

    dumped = json.dumps(recipe.to_dict())
    assert "sk-super-secret-value" not in dumped


def test_truncates_a_long_first_message(tmp_path, sa_session):
    _make_session(sa_session, "sess-9", owner="alice")
    long_text = "x" * 900
    _add_message(sa_session, "sess-9", "user", long_text, offset_seconds=0)
    _write_run_log(tmp_path, "run-9", session_id="sess-9")

    recipe = recipes.from_run("run-9", "alice")

    assert len(recipe.inputs) == 1
    assert len(recipe.inputs[0]) < len(long_text)
    assert recipe.inputs[0].endswith("…")


# ---------------------------------------------------------------------------
# The rest of from_run's behaviour (steps/title/rejection) is untouched
# ---------------------------------------------------------------------------

def test_steps_and_title_are_unaffected_by_the_new_inputs_lookup(tmp_path, sa_session):
    _make_session(sa_session, "sess-10", owner="alice")
    _add_message(sa_session, "sess-10", "user", "Do the thing.", offset_seconds=0)
    _write_run_log(tmp_path, "run-10", session_id="sess-10", label="Fix the off-by-one")

    recipe = recipes.from_run("run-10", "alice")

    assert recipe.title == "Fix the off-by-one"
    assert recipe.tools == ["read_file", "edit_file"]
    assert recipe.steps == ["use `read_file`", "use `edit_file`"]
    assert recipe.status == "draft"


def test_an_unfinished_run_still_rejects_before_any_db_lookup(tmp_path, sa_session):
    _write_run_log(tmp_path, "run-11", session_id="sess-11", finished=False)

    with pytest.raises(recipes.RecipeFromRunError) as exc:
        recipes.from_run("run-11", "alice")
    assert exc.value.error_class == "recipes.run_not_finished"


def test_a_missing_run_still_404s(tmp_path, sa_session):
    with pytest.raises(recipes.RecipeFromRunError) as exc:
        recipes.from_run("does-not-exist", "alice")
    assert exc.value.error_class == "recipes.run_not_found"
