"""Lote 84 -- GitHub via `gh` (src/git_github.py) and its routes.

`gh` itself is ALWAYS mocked, through the one seam `git_github._run_gh` --
never a real `gh` binary. `git` is real throughout (skipped cleanly when it
isn't on PATH); a fake `gh repo create`/`repo view` points `sshUrl` at a real
local bare repo so the route-level tests can push for real without any
network.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import git_routes  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from src import constants as constants_mod  # noqa: E402
from src import git_github, git_identities, git_panel  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"


# ---------------------------------------------------------------------------
# git fixture helpers -- real git.
# ---------------------------------------------------------------------------
def _run(args, cwd, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed (rc={proc.returncode}): {proc.stderr}")
    return proc


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init"], cwd=path)
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=path)
    _run(["config", "user.name", "Repo User"], cwd=path)
    _run(["config", "user.email", "repo-user@example.com"], cwd=path)
    return path


def _commit(path, filename, content, message):
    fp = path / filename
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    _run(["add", "-A"], cwd=path)
    _run(["commit", "-m", message], cwd=path)
    return _run(["rev-parse", "HEAD"], cwd=path).stdout.strip()


def _bare(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "--bare"], cwd=path)
    return path


@pytest.fixture(autouse=True)
def isolated_git_identity(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("git_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-such-system-gitconfig"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path_factory.mktemp("data")))
    # A global identity so `mode: "init"` + `initial_commit` (the default)
    # can make its first commit without every test setting one by hand.
    subprocess.run(["git", "config", "--global", "user.name", "Global User"], cwd=str(home), check=True)
    subprocess.run(["git", "config", "--global", "user.email", "global@example.com"], cwd=str(home), check=True)
    git_panel.invalidate_discovery_cache()
    git_github.invalidate_accounts_cache()
    yield
    git_panel.invalidate_discovery_cache()
    git_github.invalidate_accounts_cache()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


@pytest.fixture()
def client(store, monkeypatch):
    def _owner_from_header(request):
        return request.headers.get("x-test-owner") or None
    monkeypatch.setattr(git_routes, "effective_user", _owner_from_header)
    app = FastAPI()
    app.include_router(git_routes.setup_git_routes())
    return TestClient(app)


def _project(store, workspace, *, name="Proj", owner=OWNER):
    return store.create(name=name, folder=name, workspace=str(workspace), owner=owner, scaffold_memory=False)


def _hdr(owner=OWNER):
    return {"x-test-owner": owner}


# ---------------------------------------------------------------------------
# A fake `gh` -- the one seam (`_run_gh`) the contract asks tests to inject.
# ---------------------------------------------------------------------------
def _auth_status_text(accounts):
    """accounts: list of (login, active, protocol, scopes)."""
    lines = ["github.com"]
    for login, active, protocol, scopes in accounts:
        lines.append(f"  - Logged in to github.com account {login} (keyring)")
        lines.append(f"  - Active account: {'true' if active else 'false'}")
        lines.append(f"  - Git operations protocol: {protocol}")
        lines.append("  - Token: gho_************************************")
        lines.append("  - Token scopes: " + ", ".join(f"'{s}'" for s in scopes))
    return "\n".join(lines) + "\n"


def _make_fake_run_gh(*, accounts=(("Luissalet", True, "ssh", ("repo", "gist")),),
                      bare_by_name=None, exists_names=(), fail_names=()):
    """A drop-in for `git_github._run_gh`: dispatches on `gh`'s subcommand,
    never touches a real `gh` binary or the network. `bare_by_name` maps a
    `login/name` full-name to a REAL local bare repo path used as the fake
    `sshUrl`, so a route test's `git push` after creation is real git against
    a real (local) remote."""
    bare_by_name = bare_by_name or {}

    def fake(args, *, env=None, timeout=None):
        args = list(args)
        if args[:1] == ["--version"]:
            return subprocess.CompletedProcess(args, 0, "gh version 2.40.1 (2024-01-15)\n", "")
        if args[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, _auth_status_text(accounts), "")
        if args[:2] == ["auth", "token"]:
            login = args[3] if len(args) > 3 else "unknown"
            return subprocess.CompletedProcess(args, 0, f"gho_faketoken_{login}\n", "")
        if args[:2] == ["repo", "create"]:
            full_name = args[2]
            if full_name in fail_names:
                return subprocess.CompletedProcess(args, 1, "", "unexpected server error\n")
            if full_name in exists_names:
                return subprocess.CompletedProcess(
                    args, 1, "",
                    f"GraphQL: Name already exists on this account (createRepository)\n{full_name}\n",
                )
            return subprocess.CompletedProcess(args, 0, f"https://github.com/{full_name}\n", "")
        if args[:2] == ["repo", "view"]:
            full_name = args[2]
            ssh_url = bare_by_name.get(full_name) or f"git@github.com:{full_name}.git"
            data = {"nameWithOwner": full_name, "url": f"https://github.com/{full_name}", "sshUrl": ssh_url}
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        raise AssertionError(f"unexpected gh invocation: {args}")

    return fake


# ---------------------------------------------------------------------------
# gh_version / gh_available
# ---------------------------------------------------------------------------
def test_gh_available_reflects_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert git_github.gh_available() is False
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    assert git_github.gh_available() is True


def test_gh_version_parses_and_is_none_when_gh_missing(monkeypatch):
    monkeypatch.setattr(git_github, "_run_gh", _make_fake_run_gh())
    assert git_github.gh_version() == "2.40.1"

    def _missing(args, **kw):
        raise git_github.GhNotAvailableError()
    monkeypatch.setattr(git_github, "_run_gh", _missing)
    assert git_github.gh_version() is None


# ---------------------------------------------------------------------------
# gh_accounts
# ---------------------------------------------------------------------------
def test_gh_accounts_parses_login_active_protocol_scopes(monkeypatch):
    accounts = (
        ("Luissalet", True, "ssh", ("gist", "read:org", "repo", "workflow")),
        ("Mlgpigeon", False, "ssh", ("gist", "read:org", "repo")),
    )
    monkeypatch.setattr(git_github, "_run_gh", _make_fake_run_gh(accounts=accounts))
    result = git_github.gh_accounts(use_cache=False)
    assert result["available"] is True
    assert result["version"] == "2.40.1"
    by_login = {a["login"]: a for a in result["accounts"]}
    assert set(by_login) == {"Luissalet", "Mlgpigeon"}
    assert by_login["Luissalet"]["active"] is True
    assert by_login["Luissalet"]["protocol"] == "ssh"
    assert by_login["Luissalet"]["scopes"] == ["gist", "read:org", "repo", "workflow"]
    assert by_login["Mlgpigeon"]["active"] is False
    # Never the token.
    assert not any("gho_" in json.dumps(a) for a in result["accounts"])


def test_gh_accounts_available_false_when_gh_missing(monkeypatch):
    def _missing(args, **kw):
        raise git_github.GhNotAvailableError()
    monkeypatch.setattr(git_github, "_run_gh", _missing)
    result = git_github.gh_accounts(use_cache=False)
    assert result == {"available": False, "version": None, "accounts": []}


def test_gh_accounts_is_cached_then_bypassable(monkeypatch):
    calls = {"n": 0}
    inner = _make_fake_run_gh()

    def counting(args, **kw):
        calls["n"] += 1
        return inner(args, **kw)
    monkeypatch.setattr(git_github, "_run_gh", counting)

    git_github.gh_accounts()
    n_after_first = calls["n"]
    git_github.gh_accounts()
    assert calls["n"] == n_after_first  # served from the 15s cache

    git_github.gh_accounts(use_cache=False)
    assert calls["n"] > n_after_first  # explicitly bypassed


# ---------------------------------------------------------------------------
# gh_token
# ---------------------------------------------------------------------------
def test_gh_token_returns_stripped_token(monkeypatch):
    monkeypatch.setattr(git_github, "_run_gh", _make_fake_run_gh())
    assert git_github.gh_token("Luissalet") == "gho_faketoken_Luissalet"


def test_gh_token_none_on_failure_or_missing_gh(monkeypatch):
    def _fail(args, **kw):
        return subprocess.CompletedProcess(args, 1, "", "no such user\n")
    monkeypatch.setattr(git_github, "_run_gh", _fail)
    assert git_github.gh_token("nope") is None

    def _missing(args, **kw):
        raise git_github.GhNotAvailableError()
    monkeypatch.setattr(git_github, "_run_gh", _missing)
    assert git_github.gh_token("Luissalet") is None


# ---------------------------------------------------------------------------
# create_github_repo
# ---------------------------------------------------------------------------
def test_create_github_repo_returns_full_name_and_urls(monkeypatch):
    monkeypatch.setattr(git_github, "_run_gh", _make_fake_run_gh())
    result = git_github.create_github_repo("Luissalet", "faustus", private=True)
    assert result == {
        "full_name": "Luissalet/faustus",
        "html_url": "https://github.com/Luissalet/faustus",
        "ssh_url": "git@github.com:Luissalet/faustus.git",
        "https_url": "https://github.com/Luissalet/faustus.git",
    }


def test_create_github_repo_already_exists_raises(monkeypatch):
    monkeypatch.setattr(git_github, "_run_gh",
                        _make_fake_run_gh(exists_names={"Luissalet/faustus"}))
    with pytest.raises(git_github.GitHubRepoExistsError):
        git_github.create_github_repo("Luissalet", "faustus")


def test_create_github_repo_other_failure_raises_command_error(monkeypatch):
    monkeypatch.setattr(git_github, "_run_gh",
                        _make_fake_run_gh(fail_names={"Luissalet/faustus"}))
    with pytest.raises(git_github.GitHubCommandError) as exc:
        git_github.create_github_repo("Luissalet", "faustus")
    assert "unexpected server error" in exc.value.stderr


def test_create_github_repo_missing_gh_raises(monkeypatch):
    def _missing(args, **kw):
        raise git_github.GhNotAvailableError()
    monkeypatch.setattr(git_github, "_run_gh", _missing)
    with pytest.raises(git_github.GhNotAvailableError):
        git_github.create_github_repo("Luissalet", "faustus")


# ---------------------------------------------------------------------------
# git_identities merge (`source: "gh"`) and set_repo_identity for it
# ---------------------------------------------------------------------------
def test_list_identities_includes_gh_accounts_as_a_source(monkeypatch):
    accounts = (("Luissalet", True, "ssh", ("repo",)), ("Mlgpigeon", False, "https", ("repo",)))
    monkeypatch.setattr(git_github, "gh_accounts", lambda **kw: {
        "available": True, "version": "2.40.1",
        "accounts": [{"login": l, "active": a, "protocol": p, "scopes": list(s)} for l, a, p, s in accounts],
    })
    result = git_identities.list_identities(OWNER)
    gh_ids = [i for i in result["identities"] if i["source"] == "gh"]
    assert {i["label"] for i in gh_ids} == {"Luissalet", "Mlgpigeon"}
    luissalet = next(i for i in gh_ids if i["label"] == "Luissalet")
    assert luissalet["id"] == "gh:Luissalet"
    assert luissalet["ssh_host"] is None
    assert luissalet["github_login"] == "Luissalet"
    assert luissalet["protocol"] == "ssh"


def test_list_identities_no_gh_accounts_when_gh_unavailable(monkeypatch):
    monkeypatch.setattr(git_github, "gh_accounts", lambda **kw: {"available": False, "version": None, "accounts": []})
    result = git_identities.list_identities(OWNER)
    assert not any(i["source"] == "gh" for i in result["identities"])


def _init_repo_no_identity(path):
    """Like `_init_repo`, but WITHOUT a repo-local `user.name`/`email` --
    for the "sets user.name to the gh login when the repo has none" case."""
    path.mkdir(parents=True, exist_ok=True)
    _run(["init"], cwd=path)
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=path)
    return path


def test_set_repo_identity_gh_source_ssh_rewrites_to_plain_github(tmp_path, monkeypatch):
    # The file's autouse fixture sets a GLOBAL identity (so init-commit tests
    # don't each need one); this one test needs NO effective identity at all
    # to prove the gh-login fallback fires, so it points `GIT_CONFIG_GLOBAL`
    # at a file that doesn't exist.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-gitconfig"))
    repo = _init_repo_no_identity(tmp_path / "gh_ssh_repo")
    _run(["remote", "add", "origin", "git@SomeAlias:owner/repo.git"], cwd=repo)
    identity = {"id": "gh:Luissalet", "label": "Luissalet", "ssh_host": None, "source": "gh",
                "github_login": "Luissalet", "protocol": "ssh"}
    result = git_identities.set_repo_identity(str(repo), identity)
    assert result["remote_url_after"] == "git@github.com:owner/repo.git"
    assert result["git_user"]["name"] == "Luissalet"  # no local (or global) name was set before


def test_set_repo_identity_gh_source_https_rewrites_to_https(tmp_path):
    repo = _init_repo(tmp_path / "gh_https_repo")
    _run(["remote", "add", "origin", "git@SomeAlias:owner/repo.git"], cwd=repo)
    identity = {"id": "gh:Mlgpigeon", "label": "Mlgpigeon", "ssh_host": None, "source": "gh",
                "github_login": "Mlgpigeon", "protocol": "https"}
    result = git_identities.set_repo_identity(str(repo), identity)
    assert result["remote_url_after"] == "https://github.com/owner/repo.git"


def test_set_repo_identity_gh_source_never_overwrites_existing_name(tmp_path):
    repo = _init_repo(tmp_path / "gh_named_repo")
    _run(["config", "user.name", "Already Set"], cwd=repo)
    _run(["remote", "add", "origin", "git@SomeAlias:owner/repo.git"], cwd=repo)
    identity = {"id": "gh:Luissalet", "label": "Luissalet", "ssh_host": None, "source": "gh",
                "github_login": "Luissalet", "protocol": "ssh"}
    result = git_identities.set_repo_identity(str(repo), identity)
    assert result["git_user"]["name"] == "Already Set"


def test_repo_identity_info_auto_probes_uncached_login(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "auto_probe_repo")
    ssh_dir = tmp_path / "sshhome" / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "id_acct").write_text("PRIVATE\n", encoding="utf-8")
    (ssh_dir / "id_acct.pub").write_text("ssh-ed25519 AAAA\n", encoding="utf-8")
    (ssh_dir / "config").write_text(
        f"Host acct\n    HostName github.com\n    IdentityFile {ssh_dir / 'id_acct'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "sshhome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "sshhome"))
    _run(["remote", "add", "origin", "git@acct:owner/repo.git"], cwd=repo)

    def _fake_probe(argv, env, timeout):
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout="",
            stderr="Hi ProbedLogin! You've successfully authenticated, but GitHub does not provide shell access.\n",
        )
    monkeypatch.setattr(git_identities, "_probe_subprocess", _fake_probe)
    git_identities._PROBE_CACHE.clear()

    info = git_identities.repo_identity_info(str(repo), OWNER)
    assert info["active"]["github_login"] == "ProbedLogin"

    # Second call must NOT probe again -- now cached.
    monkeypatch.setattr(git_identities, "_probe_subprocess",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must be cached")))
    again = git_identities.repo_identity_info(str(repo), OWNER)
    assert again["active"]["github_login"] == "ProbedLogin"
    git_identities._PROBE_CACHE.clear()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def test_get_github_accounts_route(client, monkeypatch):
    monkeypatch.setattr(git_github, "_run_gh",
                        _make_fake_run_gh(accounts=(("Luissalet", True, "ssh", ("repo",)),)))
    resp = client.get("/api/git/github/accounts", headers=_hdr())
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["version"] == "2.40.1"
    assert body["accounts"][0]["login"] == "Luissalet"


def test_get_github_accounts_route_when_gh_missing(client, monkeypatch):
    def _missing(args, **kw):
        raise git_github.GhNotAvailableError()
    monkeypatch.setattr(git_github, "_run_gh", _missing)
    resp = client.get("/api/git/github/accounts", headers=_hdr())
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "version": None, "accounts": []}


def test_create_repo_with_github_create_pushes_for_real(client, store, tmp_path, monkeypatch):
    parent = tmp_path / "work"
    parent.mkdir()
    _project(store, parent)
    bare = _bare(tmp_path / "remote.git")
    monkeypatch.setattr(git_github, "_run_gh", _make_fake_run_gh(
        bare_by_name={"Luissalet/newproj": str(bare)},
    ))

    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(parent), "name": "newproj",
        "github": {"create": True, "login": "Luissalet", "private": True, "push": True},
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["github"]["full_name"] == "Luissalet/newproj"
    assert body["github"]["ssh_url"] == str(bare)
    assert body["push"]["ok"] is True
    assert body["repo"]["remotes"][0]["name"] == "origin"
    assert body["repo"]["remotes"][0]["fetch_url"] == str(bare)

    # The push was real: the bare remote now has the initial commit on
    # `main` (the default `default_branch`).
    remote_sha = _run(["rev-parse", "main"], cwd=bare).stdout.strip()
    local_sha = _run(["rev-parse", "HEAD"], cwd=parent / "newproj").stdout.strip()
    assert remote_sha == local_sha


def test_create_repo_with_github_create_requires_login(client, store, tmp_path, monkeypatch):
    parent = tmp_path / "work2"
    parent.mkdir()
    _project(store, parent)
    monkeypatch.setattr(git_github, "_run_gh", _make_fake_run_gh())

    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(parent), "name": "noproj",
        "github": {"create": True},
    }, headers=_hdr())
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "github.login_required"


def test_create_repo_with_github_create_name_taken_409_keeps_local_repo(client, store, tmp_path, monkeypatch):
    parent = tmp_path / "work3"
    parent.mkdir()
    _project(store, parent)
    monkeypatch.setattr(git_github, "_run_gh",
                        _make_fake_run_gh(exists_names={"Luissalet/dupe"}))

    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(parent), "name": "dupe",
        "github": {"create": True, "login": "Luissalet"},
    }, headers=_hdr())
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "github.exists"
    # The local repo was already created and is NOT rolled back.
    assert body["repo"]["path"] == str(parent / "dupe")
    assert (parent / "dupe" / ".git").exists()


def test_create_repo_with_github_create_other_failure_502(client, store, tmp_path, monkeypatch):
    parent = tmp_path / "work4"
    parent.mkdir()
    _project(store, parent)
    monkeypatch.setattr(git_github, "_run_gh",
                        _make_fake_run_gh(fail_names={"Luissalet/broken"}))

    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(parent), "name": "broken",
        "github": {"create": True, "login": "Luissalet"},
    }, headers=_hdr())
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_class"] == "github.failed"
    assert "unexpected server error" in body["stderr"]
    assert body["repo"]["path"] == str(parent / "broken")


def test_clone_mode_ignores_github_create(client, store, tmp_path, monkeypatch):
    parent = tmp_path / "work5"
    parent.mkdir()
    _project(store, parent)
    bare = _bare(tmp_path / "seed.git")
    seed = _init_repo(tmp_path / "seed_wt")
    _commit(seed, "a.txt", "1\n", "initial")
    _run(["remote", "add", "origin", str(bare)], cwd=seed)
    _run(["push", "-u", "origin", "master"], cwd=seed)

    def _boom(args, **kw):
        raise AssertionError(f"gh must never be called for a clone: {args}")
    monkeypatch.setattr(git_github, "_run_gh", _boom)

    resp = client.post("/api/git/repos", json={
        "mode": "clone", "parent_folder": str(parent), "name": "cloned", "url": str(bare),
        "github": {"create": True, "login": "Luissalet"},
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["github"] is None
    assert body["push"] is None


def test_github_publish_creates_remote_and_pushes(client, store, tmp_path, monkeypatch):
    parent = tmp_path / "work6"
    parent.mkdir()
    repo = _init_repo(parent / "publishme")
    _commit(repo, "a.txt", "1\n", "initial")
    _project(store, parent)
    bare = _bare(tmp_path / "published.git")
    monkeypatch.setattr(git_github, "_run_gh", _make_fake_run_gh(
        bare_by_name={"Mlgpigeon/publishme": str(bare)},
    ))

    rid = git_panel.compute_repo_id(str(repo))
    resp = client.post(f"/api/git/repos/{rid}/github/publish", json={
        "login": "Mlgpigeon", "private": False, "push": True,
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["github"]["full_name"] == "Mlgpigeon/publishme"
    assert body["push"]["ok"] is True
    assert body["repo"]["remotes"][0]["fetch_url"] == str(bare)


def test_github_publish_409_when_origin_already_exists(client, store, tmp_path, monkeypatch):
    parent = tmp_path / "work7"
    parent.mkdir()
    repo = _init_repo(parent / "already")
    _commit(repo, "a.txt", "1\n", "initial")
    _run(["remote", "add", "origin", "git@github.com:someone/already.git"], cwd=repo)
    _project(store, parent)

    def _boom(args, **kw):
        raise AssertionError(f"gh must never be called when origin already exists: {args}")
    monkeypatch.setattr(git_github, "_run_gh", _boom)

    rid = git_panel.compute_repo_id(str(repo))
    resp = client.post(f"/api/git/repos/{rid}/github/publish", json={"login": "Mlgpigeon"}, headers=_hdr())
    assert resp.status_code == 409
    assert resp.json()["error_class"] == "git.remote_exists"
