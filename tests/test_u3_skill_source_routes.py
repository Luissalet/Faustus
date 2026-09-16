"""routes/skill_source_routes.py — the HTTP surface over `src/skill_sources.py`.

Real TestClient against the real router (only `require_admin` is patched,
same pattern `tests/test_adp32_resource_pools.py` uses for another
admin-only route module), a real local git repo in `tmp_path`, real
`src.skill_sources` calls underneath — nothing here is a mock of the module
under test.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.skill_source_routes as ssr
import src.skill_sources as skill_sources_mod


def _git(args, cwd):
    env = {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _make_source_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "--quiet", "-b", "main"], repo)
    (repo / "SKILL.md").write_text(
        "---\nname: http-skill\ndescription: A skill installed over HTTP.\n"
        "version: 1.0.0\n---\n\n## When to use\nAlways.\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "--quiet", "-m", "v1"], repo)
    return repo


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_sources_mod, "SKILL_SOURCES_DB",
                        str(tmp_path / "skill_sources.db"))
    monkeypatch.setattr(skill_sources_mod, "BACKUPS_ROOT",
                        str(tmp_path / "skill_sources_backups"))
    return tmp_path


def _app(monkeypatch, *, admin_raises=None):
    def _require_admin(request):
        if admin_raises is not None:
            raise admin_raises

    monkeypatch.setattr(ssr, "require_admin", _require_admin)
    app = FastAPI()
    app.include_router(ssr.setup_skill_source_routes())
    return app


def test_install_requires_admin(monkeypatch, isolated_store, tmp_path):
    client = TestClient(_app(monkeypatch, admin_raises=HTTPException(403, "Admin only")))
    repo = _make_source_repo(tmp_path)
    resp = client.post("/api/skill-sources", json={
        "skill_dir": str(tmp_path / "installed"), "source_url": f"file://{repo}", "ref": "main",
    })
    assert resp.status_code == 403


def test_install_check_update_and_rollback_over_http(monkeypatch, isolated_store, tmp_path):
    client = TestClient(_app(monkeypatch))
    repo = _make_source_repo(tmp_path)
    skill_dir = tmp_path / "installed" / "http-skill"

    resp = client.post("/api/skill-sources", json={
        "skill_dir": str(skill_dir), "source_url": f"file://{repo}", "ref": "main",
    })
    assert resp.status_code == 200, resp.text
    v1_sha = resp.json()["source"]["pinned_revision"]

    # Advance the branch.
    (repo / "SKILL.md").write_text(
        "---\nname: http-skill\ndescription: v2 over HTTP.\nversion: 2.0.0\n---\n\n"
        "## When to use\nAlways.\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "--quiet", "-m", "v2"], repo)
    v2_sha = _git(["rev-parse", "HEAD"], repo)

    skill_key = str(skill_dir)
    resp = client.get(f"/api/skill-sources/{skill_key}/check-updates")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pinned_revision"] == v1_sha
    assert body["remote_revision"] == v2_sha
    assert body["update_available"] is True

    resp = client.post(f"/api/skill-sources/{skill_key}/update", json={"verify": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "updated"
    assert resp.json()["pinned_revision"] == v2_sha

    resp = client.post(f"/api/skill-sources/{skill_key}/rollback")
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"]["pinned_revision"] == v1_sha


def test_get_unknown_skill_source_is_404(monkeypatch, isolated_store, tmp_path):
    client = TestClient(_app(monkeypatch))
    resp = client.get("/api/skill-sources/never/installed/here")
    assert resp.status_code == 404
