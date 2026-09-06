"""The Context Engine's HTTP surface (routes/context_engine_routes.py).

These pin the rules that make an administrative door into the compiler safe to
leave open, each of which is a specific failure:

* a body that names another owner compiles as the SESSION's owner — without
  that, `POST /compile` is a way to read somebody else's memory by asking;
* a block carrying a credential is refused by name and is not stored, because
  a block store's whole purpose is to be pasted into prompts;
* a revision conflict answers with the revision that is actually stored, so the
  loser reloads instead of overwriting the write that beat it;
* `maintenance/run` refuses the agent's loopback token: it prunes the ledger
  and vacuums the store, and the model must not tidy away the audit trail of
  its own turns;
* the deletes are admin, and every listing is filtered by owner.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.context_engine_routes import setup_context_engine_routes
from src.context_engine import blocks, cache, capsules, store

TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A router over its own database, with auth disabled and one signed-in
    user stamped on every request the way the auth middleware would."""
    store.use_path(str(tmp_path / "ce.db"))
    cache.reset_working_set()
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    app = FastAPI()

    @app.middleware("http")
    async def _as_luis(request, call_next):
        request.state.current_user = "luis"
        return await call_next(request)

    app.include_router(setup_context_engine_routes())
    try:
        yield TestClient(app)
    finally:
        store.use_path(None)
        cache.reset_working_set()


def _request_body(**over):
    body = {
        "request": {
            "actor": {"agent_id": "tester", "model": "test-model"},
            "execution": {"owner": "luis", "session_id": "s1", "project_id": "p1"},
            "task": {"intent": "chat", "phase": "act", "query": "buenos dias"},
        },
    }
    body["request"].update(over)
    return body


# ── the rule the whole route exists to keep ────────────────────────────────

def test_the_body_cannot_choose_the_owner(client):
    """§19's hard rule. A compile that honoured `execution.owner` would be a
    privilege escalation wearing a diagnostic's clothes."""
    out = client.post("/api/context/compile",
                      json=_request_body(execution={"owner": "otro",
                                                    "session_id": "s1"}))
    assert out.status_code == 200, out.text
    assert out.json()["owner"] == "luis"

    # And the ledger row it left behind is the session owner's, not "otro"'s.
    listed = client.get("/api/context/packets").json()
    assert listed["packets"], listed
    assert {row["owner"] for row in listed["packets"]} == {"luis"}


def test_a_packet_row_carries_no_bodies_and_the_manifest_carries_no_text(client):
    compiled = client.post("/api/context/compile", json=_request_body()).json()
    packet_id = compiled["packet_id"]

    row = client.get(f"/api/context/packets/{packet_id}").json()["packet"]
    assert "sections" not in row and "items" in row
    assert set(row) >= {"tokens", "input_budget", "degraded", "section_tokens"}

    seen = client.get(f"/api/context/packets/{packet_id}/manifest").json()
    assert seen["ok"] is True
    for entry in seen["manifest"] or []:
        assert "body" not in entry and "source_ref" in entry


def test_an_unknown_packet_is_a_404(client):
    assert client.get("/api/context/packets/ctxpkt_nope").status_code == 404
    assert client.get("/api/context/packets/ctxpkt_nope/manifest").status_code == 404


# ── refusals are answers, not failures ─────────────────────────────────────

def test_a_block_with_a_secret_is_refused_by_name_and_not_stored(client):
    """A refusal is a 200 with `ok: false`: the caller asked whether this
    content may be stored and got an answer. The 4xx stays for a malformed
    body, which is a different thing to retry."""
    out = client.post("/api/context/blocks", json={
        "type": "project_rules", "title": "Deploy",
        "content": 'api_key: "sk-live-9f2c8ab41d77"',
    })
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["ok"] is False
    assert body["error"]["path"] == "block.content"
    # Named, not just refused: "contains a secret" is an error nobody can act on.
    assert "api_key" in body["error"]["message"]

    assert client.get("/api/context/blocks").json()["blocks"] == []


def test_a_revision_conflict_answers_with_the_revision_that_is_stored(client):
    created = client.post("/api/context/blocks", json={
        "type": "project_rules", "title": "Rules",
        "content": "Never touch migrations.",
    }).json()["block"]

    client.patch(f"/api/context/blocks/{created['id']}",
                 json={"content": "Now with a reason."})

    stale = client.patch(f"/api/context/blocks/{created['id']}",
                         json={"content": "Third writer.",
                               "expected_revision": created["revision"]}).json()
    assert stale["ok"] is False
    # The whole point of optimistic concurrency: the loser can re-read.
    assert stale["revision"] == created["revision"] + 1
    assert stale["error"]["revision"] == created["revision"] + 1


def test_a_malformed_body_is_a_400_and_not_a_refusal(client):
    assert client.post("/api/context/compile", content=b"nope").status_code == 400
    assert client.post("/api/context/blocks", json=[1, 2]).status_code == 400


def test_a_capsule_conflict_carries_its_revision_too(client):
    scope = "run-17"
    client.post(f"/api/context/capsules/{scope}/deltas",
                json={"ensure": True, "deltas": [
                    {"op": "add_decision", "value": "use sqlite"}]})
    current = client.get(f"/api/context/capsules/{scope}").json()["capsule"]

    stale = client.post(f"/api/context/capsules/{scope}/deltas", json={
        "deltas": [{"op": "add_decision", "value": "use postgres"}],
        "expected_revision": current["revision"] - 1,
    }).json()
    assert stale["ok"] is False
    assert stale["revision"] == current["revision"]


# ── the gates ──────────────────────────────────────────────────────────────

def test_maintenance_run_refuses_the_agents_loopback_token(client):
    """`require_human`, and this is why: the pass prunes the packet ledger and
    vacuums the store. A model that can run it can erase the record of what it
    was told."""
    refused = client.post("/api/context/maintenance/run", json={},
                          headers=TOOL_HEADERS)
    assert refused.status_code == 403
    assert "by a person" in refused.json()["detail"]

    # A person (auth disabled, no internal token) still gets through.
    allowed = client.post("/api/context/maintenance/run", json={"names": ["vacuum"]})
    assert allowed.status_code == 200 and allowed.json()["ok"] is True


def test_importing_project_memory_for_real_needs_a_person(client, tmp_path):
    """A dry run is a proposal and costs nothing, so the tool layer may ask.
    Writing the blocks makes standing context out of every note, so it may not."""
    project = {"id": "p1", "workspace": str(tmp_path)}
    proposed = client.post("/api/context/blocks/import-project-memory",
                           json={"project": project, "dry_run": True},
                           headers=TOOL_HEADERS)
    assert proposed.status_code == 200 and proposed.json()["ok"] is True

    refused = client.post("/api/context/blocks/import-project-memory",
                          json={"project": project, "dry_run": False},
                          headers=TOOL_HEADERS)
    assert refused.status_code == 403


def test_the_deletes_are_admin(client, monkeypatch):
    created = client.post("/api/context/blocks", json={
        "type": "project_rules", "title": "Rules", "content": "Keep it simple.",
    }).json()["block"]

    # With auth on and nobody signed in as an admin, admin routes close and the
    # reads stay open — the asymmetry the route file is built around.
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    assert client.delete(f"/api/context/blocks/{created['id']}").status_code == 403
    assert client.delete("/api/context/capsules/run-17").status_code == 403
    assert client.delete("/api/context/experiences/exp_1").status_code == 403
    assert client.get("/api/context/blocks").status_code == 200

    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    gone = client.delete(f"/api/context/blocks/{created['id']}")
    assert gone.status_code == 200 and gone.json()["deleted"] is True


# ── owner scoping ──────────────────────────────────────────────────────────

def test_the_listing_only_shows_this_owners_rows(client):
    client.post("/api/context/blocks", json={
        "type": "project_rules", "title": "Mine", "content": "Mine to read."})
    blocks.create_block(type="project_rules", owner="otro", project_id="p1",
                        title="Theirs", content="Not mine to read.")

    listed = client.get("/api/context/blocks").json()
    assert [b["title"] for b in listed["blocks"]] == ["Mine"]
    assert listed["owner"] == "luis"


def test_another_owners_row_is_a_404_not_a_403(client):
    """A 403 on a lookup by id confirms the id exists, which is the half of an
    answer a scan is looking for."""
    theirs = blocks.create_block(type="project_rules", owner="otro",
                                 title="Theirs", content="Not mine.")
    assert client.patch(f"/api/context/blocks/{theirs.id}",
                        json={"title": "Mine now"}).status_code == 404
    assert client.delete(f"/api/context/blocks/{theirs.id}").status_code == 404

    capsules.ensure("their-run", owner="otro", objective="theirs")
    assert client.get("/api/context/capsules/their-run").status_code == 404


def test_diagnostics_answers_without_a_packet_in_the_store(client):
    """The question this has to answer at three in the morning is "is the
    compiler degrading?", and it has to answer it on an empty install too."""
    out = client.get("/api/context/diagnostics").json()
    assert out["ok"] is True
    assert out["compiler"]["packets"] == 0
    assert "hit_rate" in out["cache"]
    assert "unavailable" in out["sources"]
