"""BASE-01 (lote 6): `/api/version` must also report the running checkout's
build (short git sha + commit date, read directly from `.git` -- NEVER a
`git` subprocess, per COMUN.md's hard rule) and the mtime + content hash of
the index.html actually served for `/studio`.

Reverting app.py's `build`/`served_studio` fields off `get_version()` makes
every test below fail on a missing key, proving the endpoint used to answer
with just `{"version": ...}`.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import _git_build_info, _served_studio_info, app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_version_endpoint_reports_build_and_served_studio(client: TestClient) -> None:
    response = client.get("/api/version")
    assert response.status_code == 200
    body = response.json()

    assert "version" in body
    assert isinstance(body["version"], str) and body["version"]

    assert "build" in body
    assert set(body["build"].keys()) == {"sha", "date"}
    # This checkout ships a real .git — lote 2-5's commits are already on
    # disk — so both fields must be populated, not just present.
    assert body["build"]["sha"] is not None
    assert len(body["build"]["sha"]) == 7
    assert all(c in "0123456789abcdef" for c in body["build"]["sha"])
    assert body["build"]["date"] is not None
    assert body["build"]["date"].endswith("Z")

    assert "served_studio" in body
    assert set(body["served_studio"].keys()) == {"path", "mtime", "sha", "bundle"}
    # The bundle the shell loads is what a rebuild changes; the shell is not.
    assert body["served_studio"]["bundle"]["path"] == "static/studio/studio.js"
    assert set(body["served_studio"]["bundle"].keys()) == {"path", "mtime", "sha"}
    assert body["served_studio"]["path"] == "static/index.html"
    assert body["served_studio"]["mtime"] is not None
    assert body["served_studio"]["sha"] is not None


def test_git_build_info_reads_the_reflog_without_a_git_subprocess(monkeypatch) -> None:
    """No `git` subprocess — literally: make `subprocess.run`/`Popen` raise
    and prove _git_build_info still answers correctly from plain file reads."""
    import subprocess

    def _forbidden(*args, **kwargs):
        raise AssertionError("_git_build_info must never shell out to git")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "check_output", _forbidden)

    info = _git_build_info(app_module.BASE_DIR)
    assert info["sha"] is not None
    assert len(info["sha"]) == 7
    assert info["date"] is not None


def test_git_build_info_matches_the_real_reflog_tip() -> None:
    log_path = os.path.join(app_module.BASE_DIR, ".git", "logs", "HEAD")
    with open(log_path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    expected_sha = lines[-1].split("\t", 1)[0].split(" ")[1][:7]

    info = _git_build_info(app_module.BASE_DIR)
    assert info["sha"] == expected_sha


def test_git_build_info_is_best_effort_with_no_git_dir(tmp_path) -> None:
    info = _git_build_info(str(tmp_path))
    assert info == {"sha": None, "date": None}


def test_served_studio_info_hashes_the_actually_served_index_html() -> None:
    from src.app_helpers import asset_version, abs_join

    info = _served_studio_info()
    assert info["path"] == "static/index.html"
    expected_path = abs_join(app_module.BASE_DIR, "static/index.html")
    assert info["sha"] == asset_version(expected_path)
