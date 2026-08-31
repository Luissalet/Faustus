"""The `@` mention picker end to end: the API contract and the composer popup.

Two halves, because a mismatch between them is the failure that matters — the
popup inserting a token the server would not resolve.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.workspace_routes as wr

_REPO = Path(__file__).resolve().parents[1]
_MODULE = (_REPO / "static" / "js" / "fileMentions.js").as_uri()


@pytest.fixture
def ws(tmp_path):
    for rel in ("src/agent_loop.py", "routes/workspace_routes.py", "static/js/chat.js"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n", encoding="utf-8")
    from src import agent_harness
    agent_harness._index_cache.clear()
    return tmp_path


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    return TestClient(app)


def test_files_endpoint_ranks_and_returns_relative_paths(client, ws):
    r = client.get("/api/workspace/files", params={"workspace": str(ws), "q": "chat.js"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["files"][0]["rel"] == "static/js/chat.js"
    assert d["files"][0]["name"] == "chat.js"
    assert d["files"][0]["dir"] == "static/js"


def test_files_endpoint_reports_a_bad_workspace_instead_of_500(client, tmp_path):
    r = client.get("/api/workspace/files", params={"workspace": str(tmp_path / "nope"), "q": "a"})
    assert r.status_code == 200
    assert r.json()["files"] == [] and r.json()["error"]


def test_files_endpoint_is_admin_only(monkeypatch, ws):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    r = TestClient(app).get("/api/workspace/files", params={"workspace": str(ws), "q": "a"})
    assert r.status_code == 403


def _node(script):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    res = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True,
                         text=True, encoding="utf-8", cwd=_REPO, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_composer_trigger_opens_on_at_and_never_on_an_email():
    out = _node(f"""
      import {{ activeQuery, applyPick }} from {json.dumps(_MODULE)};
      const q = (v) => activeQuery(v, v.length);
      console.log(JSON.stringify({{
        typed: q('fix @src/ag'),
        bare: q('look at @'),
        email: q('write to me@host.com'),
        pastSpace: q('fix @src/a.py and then'),
        newline: q('line one\\n@src/a'),
        afterParen: q('(@src/a'),
      }}));
    """)
    assert out["typed"] == {"query": "src/ag", "start": 4}
    assert out["bare"] == {"query": "", "start": 8}
    assert out["email"] is None
    assert out["pastSpace"] is None          # the menu closes once the token ends
    assert out["newline"] == {"query": "src/a", "start": 9}
    assert out["afterParen"] == {"query": "src/a", "start": 1}


def test_picking_a_file_replaces_only_the_typed_token():
    out = _node(f"""
      import {{ applyPick }} from {json.dumps(_MODULE)};
      console.log(JSON.stringify({{
        simple: applyPick('fix @src/ag', 11, 4, 'src/agent_loop.py'),
        midSentence: applyPick('fix @ag now', 7, 4, 'src/agent_loop.py'),
        spaced: applyPick('see @c', 6, 4, 'my dir/c.js'),
      }}));
    """)
    assert out["simple"]["value"] == "fix @src/agent_loop.py "
    assert out["midSentence"]["value"] == "fix @src/agent_loop.py now"
    # A path with a space must come back quoted or the server would read it as
    # two mentions and report both as missing.
    assert out["spaced"]["value"] == 'see @"my dir/c.js" '


def test_popup_paths_round_trip_through_the_server_resolver(client, ws):
    """What the popup inserts is what src/file_mentions.py resolves."""
    from src import file_mentions
    r = client.get("/api/workspace/files", params={"workspace": str(ws), "q": "agent"})
    rel = r.json()["files"][0]["rel"]
    out = _node(f"""
      import {{ applyPick }} from {json.dumps(_MODULE)};
      console.log(JSON.stringify(applyPick('fix @agent', 10, 4, {json.dumps(rel)})));
    """)
    res = file_mentions.resolve(str(ws), out["value"])
    assert res["resolved"] == [rel] and not res["missing"]
