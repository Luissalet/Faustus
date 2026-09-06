"""The typed context-link endpoints under `/api/projects/{id}/context`.

Six properties are pinned here, and each of them is a specific thing that
would break, silently, if it were wrong:

* **the old shape still works.** `POST {"path": "..."}` is what the live
  Studio screen sends and has always sent. It must still create the same
  `work_root` item, with the same ten-hex id, and answer with the same
  `{"item": ...}` body — including the 400 on a refused path, because a
  compatibility promise that only covered the happy path would break the
  error toast rather than the feature;

* **the typed shape goes through the service**, which validates the source
  before it writes and deduplicates a repeated attach instead of storing it
  twice;

* **a rejection is an answer**: 200 with `{"ok": false, "error": {path,
  message}}`, the convention `routes/contracts_routes.py` set. A 4xx here
  would make "this document does not exist" indistinguishable from "the
  server is broken";

* **the listing carries no content.** A tripwire resolver that raises on
  every call proves it: the list is what the store holds — membership,
  policy, `index_status`, `content_revision` — and nothing read from a
  source;

* **detach never deletes the source.** The file is still on disk afterwards;

* **a link that is not yours is a 404, never a 403.** A 403 confirms that
  the id exists, which is the one fact the check was protecting.
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import middleware  # noqa: E402
from routes import project_routes  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from services.projects import ProjectStore  # noqa: E402
from src.project_context.resolvers import (  # noqa: E402
    install_default_resolvers, register_resolver,
)

OWNER = "luis"
OTHER = "mallory"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(project_routes, "get_store", lambda: st)
    return st


@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    monkeypatch.setattr(project_routes, "effective_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(project_routes.setup_project_routes())
    return TestClient(app)


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


@pytest.fixture()
def project(store, workspace):
    return store.create("Faustus", folder="Faustus", workspace=workspace,
                        owner=OWNER)


@pytest.fixture()
def note(tmp_path):
    """A text file worth linking: it has a line a query can find."""
    path = tmp_path / "requirements.md"
    path.write_text("# Product requirements\n\nThe exporter must emit TOON.\n",
                    encoding="utf-8")
    return str(path)


class _Tripwire:
    """A resolver that fails the test if anything asks it a question.

    Registered for `file` while the listing endpoint runs. Every method
    raises, not only the reading ones: the claim is not "the list reads no
    content", it is "the list consults no source at all".
    """

    kind = "file"

    def _boom(self, *_a, **_k):
        raise AssertionError("the listing consulted a resolver")

    validate = metadata = revision = read = search = extract = _boom
    policy_note = _boom


# ── the shape the live UI sends ────────────────────────────────────────────


def test_the_path_shape_answers_exactly_as_it_always_did(client, store, project,
                                                         tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    answer = client.post(f"/api/projects/{project['id']}/context",
                         json={"path": str(docs)})
    assert answer.status_code == 200, answer.text
    body = answer.json()

    # The whole response, unchanged: one `item`, four legacy keys, a ten-hex
    # id. Not a link envelope, not an `ok`.
    assert set(body) == {"item"}
    item = body["item"]
    assert set(item) == {"id", "path", "kind", "name"}
    assert len(item["id"]) == 10 and int(item["id"], 16) >= 0
    assert item["kind"] == "folder" and item["name"] == "docs"

    # And it is still an editable root, which is what makes it compatible:
    # normalising an old item to `read_only` would revoke write access the
    # user already had.
    link = store.get_link(project["id"], item["id"], owner=OWNER)
    assert link["access_mode"] == "work_root"
    assert link["retrieval_policy"] == "on_demand"


def test_a_refused_path_is_still_a_400(client, project, tmp_path):
    """The live UI reads `detail` off a 4xx to build its toast."""
    refused = client.post(f"/api/projects/{project['id']}/context",
                          json={"path": str(tmp_path / "nowhere")})
    assert refused.status_code == 400
    assert "existing file or folder" in refused.json()["detail"]


def test_a_body_with_neither_shape_is_answered_not_crashed(client, project):
    out = client.post(f"/api/projects/{project['id']}/context", json={})
    assert out.status_code == 200
    assert out.json() == {"ok": False,
                          "error": {"path": "source",
                                    "message": "give either a 'path' or a typed 'source'"}}


# ── the typed shape ────────────────────────────────────────────────────────


def test_a_typed_source_is_attached_and_a_repeat_is_deduplicated(client, project,
                                                                 note):
    payload = {
        "source": {"kind": "file", "path": note},
        "label": "Product requirements v4",
        "role": "requirements",
        "retrieval_policy": "auto",
        "tags": ["spec"],
    }
    first = client.post(f"/api/projects/{project['id']}/context", json=payload)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is True and body["action"] == "attached"
    assert body["deduplicated"] is False

    link = body["link"]
    assert link["kind"] == "file"
    assert link["role"] == "requirements"
    assert link["label"] == "Product requirements v4"
    assert link["tags"] == ["spec"]
    # Knowledge, not permission: a new link never widens the work roots.
    assert link["access_mode"] == "read_only"
    # Validated before it was written, so the revision is real.
    assert link["content_revision"].startswith("file:")
    assert link["index_status"] == "queued"
    assert link["id"].startswith("ctx_")

    again = client.post(f"/api/projects/{project['id']}/context", json=payload)
    assert again.status_code == 200
    repeated = again.json()
    assert repeated["ok"] is True and repeated["deduplicated"] is True
    assert repeated["link"]["id"] == link["id"]

    listed = client.get(f"/api/projects/{project['id']}/context").json()
    assert [ln["id"] for ln in listed["links"]] == [link["id"]]


def test_a_source_that_does_not_exist_is_a_200_that_says_so(client, project,
                                                            tmp_path):
    """The convention of `routes/contracts_routes.py`: the caller asked a
    question and got an answer. A 4xx would confuse "not linkable" with
    "the server fell over"."""
    out = client.post(f"/api/projects/{project['id']}/context",
                      json={"source": {"kind": "file",
                                       "path": str(tmp_path / "ghost.md")}})
    assert out.status_code == 200
    body = out.json()
    assert body["ok"] is False
    assert body["error"]["path"] == "source"
    assert body["error"]["message"]
    # Nothing about the source leaked into the refusal beyond its state.
    assert "ghost.md" not in body["error"]["message"]


def test_an_unknown_kind_names_the_field_it_is_about(client, project, note):
    out = client.post(f"/api/projects/{project['id']}/context",
                      json={"source": {"kind": "chat", "path": note}})
    assert out.status_code == 200
    body = out.json()
    assert body["ok"] is False
    assert body["error"]["path"].startswith("source")
    assert "kind" in body["error"]["message"]


# ── the listing ────────────────────────────────────────────────────────────


def test_the_listing_reads_no_source_and_still_says_where_the_index_is(
        client, project, note):
    attached = client.post(f"/api/projects/{project['id']}/context",
                           json={"source": {"kind": "file", "path": note},
                                 "retrieval_policy": "auto"}).json()["link"]

    register_resolver(_Tripwire())
    try:
        listed = client.get(f"/api/projects/{project['id']}/context")
    finally:
        install_default_resolvers()

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["ok"] is True
    assert body["context_revision"] >= 1

    row = next(ln for ln in body["links"] if ln["id"] == attached["id"])
    assert row["index_status"] == "queued"
    assert row["content_revision"] == attached["content_revision"]

    # No source content reached the response under any of the names a body
    # could arrive under.
    for key in ("text", "body", "content", "chunks", "snippet", "excerpt"):
        assert key not in row
    assert "The exporter must emit TOON." not in listed.text


# ── policy ─────────────────────────────────────────────────────────────────


def test_patch_changes_the_policy_and_refuses_the_identity(client, project, note):
    link = client.post(f"/api/projects/{project['id']}/context",
                       json={"source": {"kind": "file", "path": note}}).json()["link"]
    url = f"/api/projects/{project['id']}/context/{link['id']}"

    patched = client.patch(url, json={"retrieval_policy": "on_demand",
                                      "role": "archive", "enabled": False})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["ok"] is True
    assert body["link"]["retrieval_policy"] == "on_demand"
    assert body["link"]["role"] == "archive"
    assert body["link"]["enabled"] is False
    # Identity untouched.
    assert body["link"]["kind"] == "file" and body["link"]["path"] == note

    # `kind` is not a patchable field, so a body that carries one changes
    # nothing at all rather than repointing an approved link at another
    # source through the back door.
    refused = client.patch(url, json={"kind": "document"})
    assert refused.status_code == 200
    assert refused.json() == {"ok": False,
                              "error": {"path": "patch",
                                        "message": "nothing to change"}}
    still = client.get(url).json()
    assert still["link"]["kind"] == "file"


def test_a_policy_value_outside_the_vocabulary_is_answered_not_stored(
        client, project, note):
    link = client.post(f"/api/projects/{project['id']}/context",
                       json={"source": {"kind": "file", "path": note}}).json()["link"]
    out = client.patch(f"/api/projects/{project['id']}/context/{link['id']}",
                       json={"retrieval_policy": "always_full"})
    assert out.status_code == 200
    body = out.json()
    assert body["ok"] is False
    # The field, by name. `normalize_link` would have coerced it to the
    # default and answered 200, which is the right posture for reading a
    # hand-edited projects.json and the wrong one for a request.
    assert body["error"]["path"] == "retrieval_policy"
    assert "always_full" in body["error"]["message"]

    stored = client.get(f"/api/projects/{project['id']}/context/{link['id']}").json()
    assert stored["link"]["retrieval_policy"] == link["retrieval_policy"]

    # Same at the door: an attach cannot smuggle one in either.
    attach = client.post(f"/api/projects/{project['id']}/context",
                         json={"source": {"kind": "file", "path": note},
                               "role": "villain"})
    assert attach.status_code == 200
    assert attach.json()["error"]["path"] == "role"


# ── inspect and refresh ────────────────────────────────────────────────────


def test_inspect_and_refresh_report_the_revision_moving(client, project, note,
                                                        tmp_path):
    link = client.post(f"/api/projects/{project['id']}/context",
                       json={"source": {"kind": "file", "path": note},
                             "retrieval_policy": "auto"}).json()["link"]
    url = f"/api/projects/{project['id']}/context/{link['id']}"

    fresh = client.get(url).json()
    assert fresh["ok"] is True and fresh["state"] == "ok"
    assert fresh["stale"] is False
    assert fresh["revision"] == link["content_revision"]

    with open(note, "a", encoding="utf-8") as fh:
        fh.write("\nAnd it must round-trip.\n")

    moved = client.get(url).json()
    assert moved["stale"] is True
    assert moved["link"]["content_revision"] == link["content_revision"]

    refreshed = client.post(f"{url}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["ok"] is True and body["changed"] is True
    assert body["previous_revision"] == link["content_revision"]
    assert body["revision"] != body["previous_revision"]
    # Stale, not cleared: the old index keeps serving until a new one lands.
    assert body["index_status"] == "stale"

    settled = client.post(f"{url}/refresh").json()
    assert settled["ok"] is True and settled["changed"] is False


def test_a_link_whose_source_vanished_stays_visible_and_detachable(
        client, project, tmp_path):
    doomed = tmp_path / "doomed.md"
    doomed.write_text("here for now\n", encoding="utf-8")
    link = client.post(f"/api/projects/{project['id']}/context",
                       json={"source": {"kind": "file", "path": str(doomed)}}
                       ).json()["link"]
    os.remove(doomed)

    broken = client.get(f"/api/projects/{project['id']}/context/{link['id']}")
    assert broken.status_code == 200
    body = broken.json()
    assert body["ok"] is False and body["state"] == "missing"
    assert body["error"]["path"] == "source"
    assert body["link"]["id"] == link["id"]

    # It is still in the list, and it can still be removed by hand.
    listed = client.get(f"/api/projects/{project['id']}/context").json()
    assert link["id"] in [ln["id"] for ln in listed["links"]]
    assert client.delete(
        f"/api/projects/{project['id']}/context/{link['id']}").status_code == 200


# ── detach ─────────────────────────────────────────────────────────────────


def test_detaching_removes_the_link_and_never_the_source(client, project, note):
    link = client.post(f"/api/projects/{project['id']}/context",
                       json={"source": {"kind": "file", "path": note}}).json()["link"]

    gone = client.delete(f"/api/projects/{project['id']}/context/{link['id']}")
    assert gone.status_code == 200, gone.text
    body = gone.json()
    # The legacy key the UI checks, and the sentence the agent repeats.
    assert body["success"] is True
    assert "not deleted" in body["message"]

    assert client.get(f"/api/projects/{project['id']}/context").json()["links"] == []

    # The whole point: the file is still there, with its bytes.
    assert os.path.exists(note)
    with open(note, encoding="utf-8") as fh:
        assert "The exporter must emit TOON." in fh.read()

    assert client.delete(
        f"/api/projects/{project['id']}/context/{link['id']}").status_code == 404


def test_the_legacy_item_id_and_the_new_link_id_use_the_same_endpoint(
        client, project, note, tmp_path):
    docs = tmp_path / "shared"
    docs.mkdir()
    legacy = client.post(f"/api/projects/{project['id']}/context",
                         json={"path": str(docs)}).json()["item"]
    typed = client.post(f"/api/projects/{project['id']}/context",
                        json={"source": {"kind": "file", "path": note}}
                        ).json()["link"]

    assert not legacy["id"].startswith("ctx_") and typed["id"].startswith("ctx_")
    for identifier in (legacy["id"], typed["id"]):
        assert client.delete(
            f"/api/projects/{project['id']}/context/{identifier}").status_code == 200
    assert client.get(f"/api/projects/{project['id']}/context").json()["links"] == []
    assert os.path.isdir(docs) and os.path.exists(note)


# ── isolation ──────────────────────────────────────────────────────────────


def test_another_owners_link_is_404_and_never_403(client, store, monkeypatch,
                                                  tmp_path, note):
    """Two ways to reach a link that is not yours, one answer for both.

    A 403 would say "that id exists, you just cannot have it", which is
    exactly the fact the ownership check exists to withhold.
    """
    (tmp_path / "other-ws").mkdir()
    (tmp_path / "my-ws").mkdir()
    theirs = store.create("Theirs", folder="Theirs",
                          workspace=str(tmp_path / "other-ws"), owner=OTHER)
    mine = store.create("Mine", folder="Mine",
                        workspace=str(tmp_path / "my-ws"), owner=OWNER)

    # Attach as the other owner...
    monkeypatch.setattr(project_routes, "effective_user", lambda request: OTHER)
    foreign = client.post(f"/api/projects/{theirs['id']}/context",
                          json={"source": {"kind": "file", "path": note}}
                          ).json()["link"]

    # ...and come back as somebody else.
    monkeypatch.setattr(project_routes, "effective_user", lambda request: OWNER)

    # Their project, their link.
    for method, url in (
        ("get", f"/api/projects/{theirs['id']}/context"),
        ("get", f"/api/projects/{theirs['id']}/context/{foreign['id']}"),
        ("delete", f"/api/projects/{theirs['id']}/context/{foreign['id']}"),
    ):
        answer = getattr(client, method)(url)
        assert answer.status_code == 404, (method, url, answer.text)
    assert client.patch(f"/api/projects/{theirs['id']}/context/{foreign['id']}",
                        json={"role": "archive"}).status_code == 404
    assert client.post(
        f"/api/projects/{theirs['id']}/context/{foreign['id']}/refresh"
        ).status_code == 404

    # My project, their link id: the same 404, not "no such project".
    for url in (f"/api/projects/{mine['id']}/context/{foreign['id']}",):
        assert client.get(url).status_code == 404
        assert client.delete(url).status_code == 404
        assert client.patch(url, json={"role": "archive"}).status_code == 404
        assert client.post(f"{url}/refresh").status_code == 404

    # And nothing of theirs is visible from mine.
    assert client.get(f"/api/projects/{mine['id']}/context").json()["links"] == []


def test_every_context_route_is_admin_gated(store, project, monkeypatch, note):
    """`require_admin` guards all six. With auth on and no admin session the
    router answers 403 rather than reading somebody's project."""
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    monkeypatch.setattr(project_routes, "effective_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(project_routes.setup_project_routes())
    guarded = TestClient(app)

    pid = project["id"]
    assert guarded.get(f"/api/projects/{pid}/context").status_code == 403
    assert guarded.post(f"/api/projects/{pid}/context",
                        json={"path": note}).status_code == 403
    assert guarded.get(f"/api/projects/{pid}/context/ctx_1").status_code == 403
    assert guarded.patch(f"/api/projects/{pid}/context/ctx_1",
                         json={"role": "archive"}).status_code == 403
    assert guarded.post(f"/api/projects/{pid}/context/ctx_1/refresh").status_code == 403
    assert guarded.delete(f"/api/projects/{pid}/context/ctx_1").status_code == 403
