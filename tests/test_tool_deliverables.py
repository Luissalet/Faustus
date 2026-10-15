"""src/tool_deliverables.py: files a tool made for the user become owner-scoped downloads.
Real artifact store and database, as in tests/test_tool_result_offload.py."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_catalog, artifact_store, tool_deliverables as td


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "deliver.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    yield engine
    engine.dispose()


def _mcp_result(payload):
    return {"stdout": json.dumps(payload), "stderr": "", "exit_code": 0}


def test_a_downloaded_video_becomes_a_download_link_for_its_owner(own_database, tmp_path):
    video = tmp_path / "clip [123].mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 4096)
    result = _mcp_result({"ok": True, "status": "done", "files": [{"path": str(video)}],
                          "deliverables": [{"path": str(video), "name": video.name}]})
    out = td.deliver(result, owner="luis", session_id="s1", call_id="c1", tool="mcp__links__media_download")
    assert out is not result and out[td.MARKER] is True
    [dl] = out["downloads"]
    assert dl["url"] == f"/api/artifacts/{dl['id']}/download" and dl["name"] == video.name
    assert dl["media_type"] == "video/mp4" and dl["size"] == video.stat().st_size
    assert f"[clip (123).mp4]({dl['url']})" in out["stdout"], "brackets in the name would break the Markdown link"
    assert json.loads(result["stdout"])["ok"] is True  # the original result is left alone
    with db_mod.SessionLocal() as db:
        row = artifact_catalog.get(db, dl["id"])
        assert row is not None and row.owner == "luis" and row.session_id == "s1"
        assert artifact_catalog.path(row).endswith(".mp4")
        assert open(artifact_catalog.path(row), "rb").read() == video.read_bytes()


def test_the_same_call_twice_reuses_one_download(own_database, tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3" + b"a" * 100)
    result = _mcp_result({"deliverables": [{"path": str(f)}]})
    one = td.deliver(dict(result), owner="luis", session_id="s", call_id="c", tool="t")
    two = td.deliver(dict(result), owner="luis", session_id="s", call_id="c", tool="t")
    assert one["downloads"][0]["id"] == two["downloads"][0]["id"]
    assert td.deliver(one, owner="luis") is one


def test_results_without_deliverables_errors_and_bad_paths(own_database, tmp_path):
    plain = _mcp_result({"ok": True})
    assert td.deliver(plain, owner="luis") is plain
    failed = {"stdout": "", "stderr": json.dumps({"deliverables": ["/x"]}), "exit_code": 1}
    assert td.deliver(failed, owner="luis") is failed
    assert td.deliver("text", owner="luis") == "text"
    out = td.deliver(_mcp_result({"deliverables": ["relative.mp4", str(tmp_path / "missing.mp4")]}), owner="luis")
    assert out["downloads"] == []
    assert "not an absolute path" in out["stdout"] and "missing.mp4: the file is not there" in out["stdout"]


def test_no_owner_or_too_big_is_said_plainly(own_database, tmp_path, monkeypatch):
    f = tmp_path / "big.mp4"
    f.write_bytes(b"b" * 2048)
    out = td.deliver(_mcp_result({"deliverables": [str(f)]}), owner="")
    assert out["downloads"] == [] and "no signed-in owner" in out["stdout"]
    monkeypatch.setattr(td, "max_bytes", lambda: 1024)
    out = td.deliver(_mcp_result({"deliverables": [str(f)]}), owner="luis")
    assert out["downloads"] == [] and "over the" in out["stdout"]


def test_structured_content_is_read_too(own_database, tmp_path):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"\xff\xd8\xff" + b"j" * 50)
    result = {"stdout": "Done.", "exit_code": 0, "structured_content": {"deliverables": [{"path": str(f)}]}}
    out = td.deliver(result, owner="luis", tool="t")
    assert out["downloads"][0]["media_type"] == "image/jpeg"
    assert out["stdout"].startswith("Done.")
