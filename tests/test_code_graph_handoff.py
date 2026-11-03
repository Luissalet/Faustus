"""Tests for `code_graph.handoff`: a tiny Python repo, real execution in a throwaway
copy (no network, no npm), HANDOFF.md with only verified commands as instructions."""
from __future__ import annotations

import json
import os
import shutil

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store

APP_PY = '''import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["APP_SECRET"]
PORT = int(os.environ.get("PORT", "8765"))
GREETING = os.environ.get("APP_GREETING", "hello")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = GREETING.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def connect():
    return sqlite3.connect("app.db")


if __name__ == "__main__":
    print("serving on", PORT, flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''
TEST_PY = '''import unittest


class SmokeTest(unittest.TestCase):
    def test_truth(self):
        self.assertEqual(1 + 1, 2)
'''


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@pytest.fixture()
def repo(tmp_path):
    ce_store.use_path(str(tmp_path / "ce.db"))
    root = tmp_path / "repo"
    root.mkdir()
    r = str(root)
    _write(r, "requirements.txt", "")
    _write(r, "app.py", APP_PY)
    _write(r, "tests/test_app.py", TEST_PY)
    _write(r, "scripts/backup.sh", "#!/bin/bash\necho 'backup disk is full' >&2\nexit 3\n")
    _write(r, "README.md", "# Tiny\n\nA tiny app. Faustus is not part of it.\n")
    tok1 = te._active_workspace.set(r)
    tok2 = te._active_workspace_roots.set((r,))
    try:
        yield r
    finally:
        te._active_workspace.reset(tok1)
        te._active_workspace_roots.reset(tok2)
        ce_store.use_path(None)


def _by_phase(res):
    out = {}
    for s in res["steps"]:
        out.setdefault(s["phase"], []).append(s)
    return out


def test_handoff_verifies_steps_and_writes_markdown(repo, tmp_path):
    out_dir = str(tmp_path / "out")
    res = code_graph.handoff(repo, out_dir, run=True, timeout=60, allow_network=False)
    assert res["exit_code"] == 0, res
    phases = _by_phase(res)

    venv, pip = phases["install"]
    assert venv["command"] == "python -m venv .venv" and venv["verdict"] == "verified" and venv["exit_code"] == 0
    assert pip["command"].startswith("pip install -r requirements.txt") and pip["verdict"] == "verified"
    assert isinstance(pip["duration_s"], float)

    run = phases["run"][0]
    assert run["command"] == "python app.py" and run["verdict"] == "verified"
    assert run["url"].startswith("http://127.0.0.1:") and "HTTP 200" in run["reason"]

    test = phases["test"][0]
    assert test["command"] == "python -m unittest discover -s tests" and test["verdict"] == "verified"
    assert any("OK" in ln for ln in test["output_tail"])

    cfg = phases["configure"][0]
    env = {e["name"]: e for e in res["env"]}
    assert env["APP_SECRET"]["required"] is True and env["APP_SECRET"]["default"] is None
    assert env["APP_GREETING"]["required"] is False and env["APP_GREETING"]["default"] == "hello"
    assert cfg["verdict"] == "verified"

    backup = phases["recover"][0]
    assert backup["command"] == "bash scripts/backup.sh"
    assert backup["verdict"] == "failed" and backup["exit_code"] == 3
    assert any("disk is full" in ln for ln in backup["output_tail"])

    demo = phases["demo"][0]
    assert demo["verdict"] == "verified" and demo["url"] == run["url"]

    assert res["counts"] == {"verified": 6, "failed": 1, "not run": 0}
    # nothing was left in the repository besides the output folder, and the copy is gone
    assert not os.path.exists(os.path.join(repo, ".venv"))

    md = open(res["path"], encoding="utf-8").read()
    assert res["path"] == os.path.join(out_dir, "HANDOFF.md") and md == res["markdown"]
    verified_part, _, not_verified = md.partition("## Not verified")
    assert "python app.py" in verified_part and "python -m unittest discover -s tests" in verified_part
    assert "| `APP_SECRET` | yes | - |" in verified_part and "| `APP_GREETING` | no | `hello` |" in verified_part
    assert "bash scripts/backup.sh" not in verified_part
    assert "bash scripts/backup.sh" in not_verified and "Exit code: 3" in not_verified and "disk is full" in not_verified
    assert "faustus" not in md.lower() and "code_graph" not in md.lower()
    report = json.load(open(res["report_path"], encoding="utf-8"))
    assert report["counts"]["failed"] == 1 and report["steps"][0]["phase"] == "install"


def test_handoff_without_running_marks_everything_not_run(repo, tmp_path):
    res = code_graph.handoff(repo, str(tmp_path / "out2"), run=False)
    assert res["exit_code"] == 0
    assert res["counts"]["verified"] == 0 and res["counts"]["not run"] == len(res["steps"])
    assert all(s["reason"].startswith("execution disabled") for s in res["steps"])
    md = res["markdown"]
    assert "## Not verified" in md and "Nothing in it is verified" in md
    assert "```bash" not in md.split("## Not verified")[0]


def test_handoff_missing_runtime_dependency_is_failed_not_verified(repo, tmp_path):
    _write(repo, "requirements.txt", "definitely-not-a-real-package-xyz==9.9.9\n")
    res = code_graph.handoff(repo, str(tmp_path / "out3"), run=True, timeout=40, allow_network=False)
    phases = _by_phase(res)
    pip = phases["install"][1]
    assert pip["verdict"] == "failed" and pip["exit_code"] not in (None, 0)
    assert phases["run"][0]["verdict"] == "not run" and "install" in phases["run"][0]["reason"]
    assert "definitely-not-a-real-package" in res["markdown"].split("## Not verified")[1] or \
        "pip install -r requirements.txt" in res["markdown"].split("## Not verified")[1]
