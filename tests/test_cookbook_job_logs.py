"""Cookbook job logs (#205): command recording with redaction, tail / size cap,
the REST routes and the `cookbook_job_log` MCP tool."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.cookbook_logs_routes as logs_routes
from src import cookbook_job_logs as jl


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jl, "LOG_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(logs_routes, "require_admin", lambda request: None)
    return tmp_path


def _job(tmp_path, jid="serve-aa11bb22", out="", err="", combined=None):
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    files = {}
    (d / f"{jid}.log").write_text(combined if combined is not None else out + err, encoding="utf-8")
    files["combined"] = str(d / f"{jid}.log")
    if out or err:
        (d / f"{jid}.out.log").write_text(out, encoding="utf-8")
        (d / f"{jid}.err.log").write_text(err, encoding="utf-8")
        files["stdout"] = str(d / f"{jid}.out.log")
        files["stderr"] = str(d / f"{jid}.err.log")
    jl.record_launch(jid, kind="serve", model="org/model", command="llama-server -m m.gguf --port 8080",
                     argv=["bash", "x.sh"], cwd="C:/work", env={"HF_TOKEN": "hf_secretvalue123", "CUDA_VISIBLE_DEVICES": "0"},
                     remote="local", files=files)
    return jid


def test_record_redacts_command_and_env(tmp_path):
    jid = "dl-1"
    jl.record_launch(jid, kind="download", command="hf download x --token hf_abcdef123456 --include a",
                     argv=["bash", "--api-key=sk-12345678901234567890"],
                     env={"HF_TOKEN": "hf_abcdef123456", "MY_PASSWORD": "pw", "CUDA_VISIBLE_DEVICES": "0,1"})
    meta = jl.get_meta(jid)
    blob = json.dumps(meta)
    assert "hf_abcdef123456" not in blob and "sk-12345678901234567890" not in blob and '"pw"' not in blob
    assert meta["env"]["HF_TOKEN"] == "***" and meta["env"]["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert meta["command"].startswith("hf download x --token ***")


def test_observe_tracks_status_exit_and_end_time(tmp_path):
    jid = _job(tmp_path)
    jl.observe(jid, status="running")
    assert "ended_at" not in jl.get_meta(jid)
    jl.observe(jid, status="error", exit_code=3)
    meta = jl.get_meta(jid)
    assert meta["status"] == "error" and meta["exit_code"] == 3 and meta["ended_at"]
    jl.observe(jid, status="running")  # restarted under the same id
    assert "ended_at" not in jl.get_meta(jid)


def test_observe_creates_a_snapshot_backed_job(tmp_path):
    jl.observe("cookbook-zz", status="completed", exit_code=0, snapshot="line1\nline2\n",
               defaults={"kind": "download", "model": "m", "command": "hf download m --token hf_zzzzzz123456"})
    data = jl.read_log("cookbook-zz")
    assert [l["text"] for l in data["lines"]] == ["line1", "line2"]
    assert data["available_streams"] == ["combined"] and not data["stream_available"] is False
    assert "hf_zzzzzz123456" not in data["job"]["command"]
    assert jl.read_log("cookbook-zz", "stderr")["stream_available"] is False


def test_tail_limit_and_truncated_flag(tmp_path):
    jid = _job(tmp_path, combined="\n".join(f"l{i}" for i in range(500)))
    data = jl.read_log(jid, "combined", 10)
    assert [l["text"] for l in data["lines"]][-1] == "l499" and len(data["lines"]) == 10 and data["truncated"]
    assert len(jl.read_log(jid, "combined", 10**6)["lines"]) == 500  # clamped to MAX_TAIL
    assert jl.read_log(jid, "combined", 0)["tail"] == 1


def test_size_cap_on_read_and_on_finish(tmp_path, monkeypatch):
    monkeypatch.setattr(jl, "MAX_FILE_BYTES", 1000)
    jid = _job(tmp_path, combined="\n".join(f"line-{i:05d}" for i in range(2000)))
    data = jl.read_log(jid, "combined", 2000)
    assert data["truncated"] and data["lines"][-1]["text"] == "line-01999"
    assert sum(len(l["text"]) + 1 for l in data["lines"]) <= 1100
    jl.observe(jid, status="completed", exit_code=0)
    import os
    assert os.path.getsize(jl.get_meta(jid)["files"]["combined"]) <= 1100
    with open(jl.get_meta(jid)["files"]["combined"], encoding="utf-8") as fh:
        assert fh.readline().startswith("[... older output trimmed")


def test_stderr_lines_are_flagged_in_combined(tmp_path):
    jid = _job(tmp_path, out="loading\nready\n", err="CUDA error: out of memory\n",
               combined="loading\nCUDA error: out of memory\nready\n")
    lines = jl.read_log(jid)["lines"]
    assert [l["stderr"] for l in lines] == [False, True, False]
    assert [l["text"] for l in jl.read_log(jid, "stderr")["lines"]] == ["CUDA error: out of memory"]
    assert [l["text"] for l in jl.read_log(jid, "stdout")["lines"]] == ["loading", "ready"]


def test_secret_in_output_is_redacted_on_read(tmp_path):
    jid = _job(tmp_path, combined="Authorization: Bearer abcdefghijklmnop1234567890\nHF_TOKEN=hf_leak1234567890abc\n")
    text = json.dumps(jl.read_log(jid))
    assert "abcdefghijklmnop1234567890" not in text and "hf_leak1234567890abc" not in text


def test_bad_ids_and_streams(tmp_path):
    with pytest.raises(jl.JobLogError):
        jl.read_log("../etc/passwd")
    jid = _job(tmp_path)
    with pytest.raises(jl.JobLogError):
        jl.read_log(jid, "bogus")
    with pytest.raises(KeyError):
        jl.read_log("nope-1")


def test_header_has_command_exit_code_and_times(tmp_path):
    jid = _job(tmp_path)
    jl.observe(jid, status="error", exit_code=137)
    head = jl.render_header(jl.get_meta(jid))
    for needle in ("serve-aa11bb22", "exit code: 137", "command: llama-server -m m.gguf", "started:", "ended:",
                   "cwd: C:/work", "CUDA_VISIBLE_DEVICES=0", "HF_TOKEN=***"):
        assert needle in head
    assert "hf_secretvalue123" not in head


# ---- REST ------------------------------------------------------------------

def _client():
    app = FastAPI()
    app.include_router(logs_routes.setup_cookbook_logs_routes())
    return TestClient(app)


def test_api_log_text_download_and_errors(tmp_path):
    jid = _job(tmp_path, out="a\n", err="boom\n", combined="a\nboom\n")
    jl.observe(jid, status="error", exit_code=1)
    c = _client()
    r = c.get(f"/api/cookbook/jobs/{jid}/log?tail=5")
    assert r.status_code == 200 and r.json()["lines"][1] == {"text": "boom", "stderr": True}
    assert r.json()["job"]["exit_code"] == 1 and "command:" in r.json()["header"]
    assert "[stderr] boom" in c.get(f"/api/cookbook/jobs/{jid}/log?format=text").text
    d = c.get(f"/api/cookbook/jobs/{jid}/download?stream=stderr")
    assert d.status_code == 200 and "attachment" in d.headers["content-disposition"]
    assert d.text.replace("\r", "").endswith("boom\n") and "exit code: 1" in d.text
    assert c.get(f"/api/cookbook/jobs/{jid}/log?stream=zzz").status_code == 400
    assert c.get("/api/cookbook/jobs/missing-1/log").status_code == 404
    assert [j["session_id"] for j in c.get("/api/cookbook/jobs").json()["jobs"]] == [jid]
    jl.observe("snap-1", status="running", snapshot="x\n")
    assert c.get("/api/cookbook/jobs/snap-1/download?stream=stderr").status_code == 404


def test_api_requires_admin(tmp_path, monkeypatch):
    from fastapi import HTTPException

    def deny(request):
        raise HTTPException(403, "admin only")

    monkeypatch.setattr(logs_routes, "require_admin", deny)
    assert _client().get("/api/cookbook/jobs/x-1/log").status_code == 403


# ---- MCP -------------------------------------------------------------------

def test_mcp_cookbook_job_log_tool(tmp_path):
    pytest.importorskip("mcp")
    import mcp_servers.harness_server as srv

    jid = _job(tmp_path, out="a\n", err="boom\n", combined="a\nboom\n")
    jl.observe(jid, status="error", exit_code=2)

    def call(args):
        return json.loads(asyncio.run(srv.call_tool("cookbook_job_log", args))[0].text)

    out = call({"job_id": jid, "stream": "stderr", "tail": 5})
    assert out["lines"][0]["text"] == "boom" and out["job"]["exit_code"] == 2
    assert call({})["jobs"][0]["session_id"] == jid
    assert "error" in call({"job_id": "nope-1"})
    assert "error" in call({"job_id": jid, "stream": "bad"})
