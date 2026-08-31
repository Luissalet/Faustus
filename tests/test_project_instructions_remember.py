"""`#` / `/remember`: one standing rule appended to the project's own
instructions file, so it reaches every later turn's system prompt."""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.workspace_routes as wr
from src import project_instructions as pi


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    return TestClient(app)


def test_creates_agents_md_when_the_project_has_none(tmp_path):
    res = pi.remember(str(tmp_path), "run the tests with pytest -q")
    assert res["created"] and res["rel"] == "AGENTS.md"
    body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Notes added from chat" in body
    assert "- run the tests with pytest -q" in body


def test_appends_to_the_file_the_project_already_uses(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Rules\n\n- never touch vendor/\n", encoding="utf-8")
    res = pi.remember(str(tmp_path), "prefer pnpm")
    assert res["rel"] == "CLAUDE.md" and not res["created"]
    body = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "- never touch vendor/" in body and "- prefer pnpm" in body
    assert not (tmp_path / "AGENTS.md").exists()


def test_later_rules_join_the_same_section(tmp_path):
    pi.remember(str(tmp_path), "first")
    pi.remember(str(tmp_path), "second")
    body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert body.count("## Notes added from chat") == 1
    assert body.index("- first") < body.index("- second")


def test_a_rule_is_inserted_above_a_following_section(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# P\n\n## Notes added from chat\n\n- one\n\n## Layout\n\n- src/ holds the app\n",
        encoding="utf-8")
    pi.remember(str(tmp_path), "two")
    body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert body.index("- two") < body.index("## Layout")
    assert "- src/ holds the app" in body


def test_the_same_rule_twice_is_reported_not_duplicated(tmp_path):
    pi.remember(str(tmp_path), "prefer pnpm")
    res = pi.remember(str(tmp_path), "  prefer pnpm  ")
    assert res["duplicate"] is True
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").count("- prefer pnpm") == 1


def test_composer_sigil_list_markers_and_newlines_are_stripped():
    assert pi.normalise_rule("# prefer pnpm") == "prefer pnpm"
    assert pi.normalise_rule("- prefer\n  pnpm") == "prefer pnpm"
    assert pi.normalise_rule("   ") == ""
    assert len(pi.normalise_rule("x" * 900)) <= 500


def test_crlf_files_stay_crlf(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"# P\r\n\r\n- one\r\n")
    pi.remember(str(tmp_path), "two")
    raw = (tmp_path / "AGENTS.md").read_bytes()
    assert b"- two" in raw
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")   # no lone LF introduced


def test_the_new_rule_reaches_the_system_prompt_block(tmp_path):
    pi.invalidate()
    pi.remember(str(tmp_path), "deploy only on Fridays")
    assert "deploy only on Fridays" in pi.block(str(tmp_path))


def test_route_appends_and_reports_the_path(client, tmp_path):
    r = client.post("/api/workspace/instructions/remember",
                    json={"workspace": str(tmp_path), "text": "# use tabs"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rule"] == "use tabs" and d["created"] and d["rel"] == "AGENTS.md"


def test_route_rejects_an_empty_rule(client, tmp_path):
    r = client.post("/api/workspace/instructions/remember",
                    json={"workspace": str(tmp_path), "text": "   "})
    assert r.status_code == 400


def test_route_is_admin_only(monkeypatch, tmp_path):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    r = TestClient(app).post("/api/workspace/instructions/remember",
                             json={"workspace": str(tmp_path), "text": "x"})
    assert r.status_code == 403
