"""Lote 82 -- SSH identities (OBJ-4): src/git_identities.py.

Real `git` for the repo-facing pieces (skipped cleanly when it isn't on
PATH). HOME points at an isolated tmp dir carrying a fixture `~/.ssh/config`
with two `Host` aliases (plus one loose, unreferenced key and one `Host *`
block that must never surface as an identity); DATA_DIR is repointed so the
manual-identities store never touches the real one. The `ssh -T` probe is
always mocked -- this module must never hit the real network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import constants as constants_mod  # noqa: E402
from src import git_identities  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"


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


@pytest.fixture()
def ssh_home(tmp_path, monkeypatch):
    home = tmp_path / "sshhome"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)

    for name in ("id_acct1", "id_acct2", "id_loose"):
        (ssh_dir / name).write_text("PRIVATE\n", encoding="utf-8")
        (ssh_dir / f"{name}.pub").write_text("ssh-ed25519 AAAA...\n", encoding="utf-8")

    (ssh_dir / "config").write_text(
        "# fixture ssh config\n"
        "Host acct1\n"
        "    HostName github.com\n"
        f"    IdentityFile {ssh_dir / 'id_acct1'}\n"
        "\n"
        "Host acct2\n"
        "    HostName github.com\n"
        f"    IdentityFile {ssh_dir / 'id_acct2'}\n"
        "\n"
        "Host *\n"
        "    AddKeysToAgent yes\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows: expanduser ignores HOME
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    git_identities._PROBE_CACHE.clear()
    # A machine with a real `gh` logged in (Luis's Windows) would otherwise
    # merge its accounts -- with their logins -- into every listing here.
    from src import git_github
    monkeypatch.setattr(git_github, "gh_accounts",
                        lambda *a, **k: {"available": False, "version": None, "accounts": []})
    yield ssh_dir
    git_identities._PROBE_CACHE.clear()


# ---------------------------------------------------------------------------
# ~/.ssh/config parsing
# ---------------------------------------------------------------------------
def test_parse_ssh_config_finds_two_aliases_and_skips_wildcard(ssh_home):
    blocks = git_identities.parse_ssh_config(str(ssh_home / "config"))
    by_alias = {b["alias"]: b for b in blocks}
    assert set(by_alias) == {"acct1", "acct2"}  # "Host *" never surfaces
    for alias, b in by_alias.items():
        assert b["hostname"] == "github.com"
        assert b["identity_file"] == os.path.realpath(str(ssh_home / f"id_{alias}"))


def test_discover_loose_key_identities_excludes_files_already_referenced(ssh_home):
    cfg_identities = git_identities.discover_ssh_config_identities(str(ssh_home / "config"))
    used = {i["identity_file"] for i in cfg_identities}
    loose = git_identities.discover_loose_key_identities(str(ssh_home), used)
    labels = {l["label"] for l in loose}
    assert labels == {"id_loose"}
    assert loose[0]["ssh_host"] is None
    assert loose[0]["source"] == "ssh_config"


def test_list_identities_combines_sources_and_never_probes(ssh_home, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("list_identities must never call the ssh probe")

    monkeypatch.setattr(git_identities, "_probe_subprocess", _boom)
    result = git_identities.list_identities(OWNER)
    assert result["ssh_dir"] == str(ssh_home)
    labels = {i["label"] for i in result["identities"]}
    assert {"acct1", "acct2", "id_loose"} <= labels
    # Nothing probed yet -- every login is null.
    assert all(i["github_login"] is None for i in result["identities"])


# ---------------------------------------------------------------------------
# Manual identities (create / delete)
# ---------------------------------------------------------------------------
def test_create_manual_identity_rejects_missing_key_file(ssh_home, tmp_path):
    with pytest.raises(git_identities.GitIdentityError) as exc:
        git_identities.create_manual_identity(
            OWNER, label="Nope", identity_file=str(tmp_path / "does-not-exist"),
        )
    assert exc.value.code == "identity_file_missing"


def test_create_manual_identity_persists_and_lists(ssh_home):
    record = git_identities.create_manual_identity(
        OWNER, label="Work account", ssh_host="acct1", identity_file=str(ssh_home / "id_acct1"),
        git_user_name="Work Name", git_user_email="work@example.com",
    )
    assert record["source"] == "manual"
    assert record["id"].startswith("id_")

    listing = git_identities.list_identities(OWNER)["identities"]
    assert any(i["id"] == record["id"] and i["label"] == "Work account" for i in listing)

    # Owner-scoped: another owner sees nothing manual.
    other = git_identities.list_identities("mallory")["identities"]
    assert not any(i["id"] == record["id"] for i in other)


def test_create_manual_identity_write_ssh_config_appends_without_touching_existing_blocks(ssh_home):
    cfg_path = ssh_home / "config"
    before = cfg_path.read_text(encoding="utf-8")

    git_identities.create_manual_identity(
        OWNER, label="New alias", ssh_host="newalias", identity_file=str(ssh_home / "id_loose"),
        write_ssh_config=True,
    )

    after = cfg_path.read_text(encoding="utf-8")
    assert before in after  # the original two blocks are untouched, byte for byte
    assert "Host newalias" in after
    assert f"IdentityFile {ssh_home / 'id_loose'}" in after
    assert (ssh_home / "config.bak").read_text(encoding="utf-8") == before

    blocks = {b["alias"] for b in git_identities.parse_ssh_config(str(cfg_path))}
    assert blocks == {"acct1", "acct2", "newalias"}


def test_delete_manual_identity_only_affects_manual(ssh_home):
    record = git_identities.create_manual_identity(
        OWNER, label="Temp", identity_file=str(ssh_home / "id_loose"),
    )
    cfg_id = next(i["id"] for i in git_identities.list_identities(OWNER)["identities"]
                  if i["source"] == "ssh_config" and i["label"] == "acct1")

    assert git_identities.delete_manual_identity(OWNER, "no-such-id") is False
    assert git_identities.delete_manual_identity(OWNER, record["id"]) is True
    assert not any(i["id"] == record["id"] for i in git_identities.list_identities(OWNER)["identities"])
    # An ssh_config-sourced id is simply absent from the manual store -- the
    # route layer is what refuses to call this for a non-manual id at all.
    assert git_identities.delete_manual_identity(OWNER, cfg_id) is False


# ---------------------------------------------------------------------------
# Probe (mocked)
# ---------------------------------------------------------------------------
def _fake_github_probe(login: str):
    def _probe(argv, env, timeout):
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout="",
            stderr=f"Hi {login}! You've successfully authenticated, but GitHub does not provide shell access.\n",
        )
    return _probe


def _fake_probe_failure():
    def _probe(argv, env, timeout):
        return subprocess.CompletedProcess(
            argv, returncode=255, stdout="",
            stderr="Permission denied (publickey).\n",
        )
    return _probe


def test_probe_identity_parses_login_and_caches(ssh_home, monkeypatch):
    monkeypatch.setattr(git_identities, "_probe_subprocess", _fake_github_probe("Luissalet"))
    ident = git_identities.find_identity(OWNER, git_identities.list_identities(OWNER)["identities"][0]["id"])
    result = git_identities.probe_identity(ident)
    assert result == {"github_login": "Luissalet", "ok": True,
                       "detail": "Hi Luissalet! You've successfully authenticated, but GitHub does not provide shell access."}

    # Cached: a probe that would now raise is never called.
    monkeypatch.setattr(git_identities, "_probe_subprocess",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must be cached")))
    again = git_identities.probe_identity(ident)
    assert again["github_login"] == "Luissalet"


def test_probe_identity_force_bypasses_cache(ssh_home, monkeypatch):
    ident = next(i for i in git_identities.list_identities(OWNER)["identities"] if i["label"] == "acct1")
    monkeypatch.setattr(git_identities, "_probe_subprocess", _fake_github_probe("First"))
    git_identities.probe_identity(ident)
    monkeypatch.setattr(git_identities, "_probe_subprocess", _fake_github_probe("Second"))
    assert git_identities.probe_identity(ident)["github_login"] == "First"  # still cached
    assert git_identities.probe_identity(ident, force=True)["github_login"] == "Second"


def test_probe_identity_failure_is_ok_false_not_a_raise(ssh_home, monkeypatch):
    monkeypatch.setattr(git_identities, "_probe_subprocess", _fake_probe_failure())
    ident = next(i for i in git_identities.list_identities(OWNER)["identities"] if i["label"] == "acct2")
    result = git_identities.probe_identity(ident)
    assert result["ok"] is False
    assert result["github_login"] is None
    assert "Permission denied" in result["detail"]


# ---------------------------------------------------------------------------
# Remote URL rewriting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("git@Luissalet:Luissalet/Faustus.git", "git@acct2:Luissalet/Faustus.git"),
    ("git@github.com:owner/repo", "git@acct2:owner/repo.git"),
    ("https://github.com/owner/repo.git", "git@acct2:owner/repo.git"),
    ("https://github.com/owner/repo", "git@acct2:owner/repo.git"),
    ("ssh://git@github.com/owner/repo.git", "git@acct2:owner/repo.git"),
])
def test_rewrite_remote_alias_handles_every_url_shape(url, expected):
    assert git_identities.rewrite_remote_alias(url, "acct2") == expected


def test_rewrite_remote_alias_unrecognized_shape_returns_none():
    assert git_identities.rewrite_remote_alias("not a url at all", "acct2") is None


# ---------------------------------------------------------------------------
# Repo <-> identity: active match, set_repo_identity, repo_identity_info
# ---------------------------------------------------------------------------
def test_active_identity_for_repo_matches_alias_and_none_for_https(ssh_home, tmp_path):
    ssh_repo = _init_repo(tmp_path / "ssh_repo")
    _run(["remote", "add", "origin", "git@acct1:someone/repo.git"], cwd=ssh_repo)
    active = git_identities.active_identity_for_repo(str(ssh_repo), OWNER)
    assert active is not None and active["label"] == "acct1"

    https_repo = _init_repo(tmp_path / "https_repo")
    _run(["remote", "add", "origin", "https://github.com/someone/repo.git"], cwd=https_repo)
    assert git_identities.active_identity_for_repo(str(https_repo), OWNER) is None

    no_remote_repo = _init_repo(tmp_path / "no_remote_repo")
    assert git_identities.active_identity_for_repo(str(no_remote_repo), OWNER) is None


def test_set_repo_identity_rewrites_remote_and_sets_local_user(ssh_home, tmp_path):
    repo = _init_repo(tmp_path / "work")
    _run(["remote", "add", "origin", "https://github.com/someone/repo.git"], cwd=repo)

    identity = git_identities.create_manual_identity(
        OWNER, label="Work", ssh_host="acct2", identity_file=str(ssh_home / "id_acct2"),
        git_user_name="Work Bot", git_user_email="workbot@example.com",
    )
    result = git_identities.set_repo_identity(str(repo), identity)
    assert result["remote_url_before"] == "https://github.com/someone/repo.git"
    assert result["remote_url_after"] == "git@acct2:someone/repo.git"
    assert result["git_user"] == {"name": "Work Bot", "email": "workbot@example.com", "scope": "local"}

    new_url = _run(["remote", "get-url", "origin"], cwd=repo).stdout.strip()
    assert new_url == "git@acct2:someone/repo.git"
    local_name = _run(["config", "--local", "user.name"], cwd=repo).stdout.strip()
    assert local_name == "Work Bot"


def test_set_repo_identity_without_alias_raises(ssh_home, tmp_path):
    repo = _init_repo(tmp_path / "work2")
    _run(["remote", "add", "origin", "https://github.com/someone/repo.git"], cwd=repo)
    loose = next(i for i in git_identities.list_identities(OWNER)["identities"] if i["label"] == "id_loose")
    with pytest.raises(git_identities.GitIdentityError) as exc:
        git_identities.set_repo_identity(str(repo), loose)
    assert exc.value.code == "no_alias"


def test_set_repo_identity_no_remote_raises(ssh_home, tmp_path):
    repo = _init_repo(tmp_path / "work3")
    identity = next(i for i in git_identities.list_identities(OWNER)["identities"] if i["label"] == "acct1")
    with pytest.raises(git_identities.GitIdentityError) as exc:
        git_identities.set_repo_identity(str(repo), identity)
    assert exc.value.code == "no_remote"


def test_repo_identity_info_reports_scope(ssh_home, tmp_path):
    repo = _init_repo(tmp_path / "scoped")
    info = git_identities.repo_identity_info(str(repo), OWNER)
    assert info["git_user"]["scope"] == "local"  # _init_repo sets local config
    assert info["active"] is None  # no remote yet
