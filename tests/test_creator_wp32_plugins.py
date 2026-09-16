"""WP32 — Creator plugin lifecycle (`src/creator/plugins.py`) + routes.

Real sqlite store under `tmp_path`, real filesystem staging/install
directories, a real local git repository for the one git-transport case
(no network — `git clone` against a `file://`/plain local path, exactly
like `tests/acceptance/test_a25_skill_git_source.py`). No mocks of the
module under test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.creator import plugins as pl


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    return pl.PluginStore(
        db_path=str(tmp_path / "plugins.db"),
        install_root=str(tmp_path / "installs"),
    )


def _write_package(root: Path, manifest: dict, extra_files: dict | None = None) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, content in (extra_files or {}).items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(root)


def _engine_adapter_source(tmp_path, plugin_id="engine.demo", *, permissions=None, requires=None):
    root = tmp_path / "pkg_src" / plugin_id
    manifest = {
        "id": plugin_id, "kind": "engine_adapter", "version": "1.0.0",
        "capabilities": ["txt2img"],
        "permissions": permissions if permissions is not None else
            {"network": False, "fs": False, "gpu": True, "cost": False},
        "license": "MIT",
    }
    if requires is not None:
        manifest["requires"] = requires
    path = _write_package(root, manifest, {"adapter.py": "# adapter\n"})
    return {"id": plugin_id, "kind": "engine_adapter", "transport": "local_dir", "path": path}


# ── discover: declared, never fetched ───────────────────────────────

def test_discover_records_catalog_without_touching_filesystem(store):
    sources = [
        {"id": "engine.ghost", "kind": "engine_adapter", "transport": "git",
         "source_url": "https://example.invalid/never-cloned.git", "ref": "main"},
    ]
    result = pl.discover("alice", sources, store=store)
    assert result[0]["id"] == "engine.ghost"
    listed = store.list_discovered("alice")
    assert len(listed) == 1
    # Nothing was ever fetched — an unreachable/bogus URL discovers fine.
    assert listed[0]["source"]["source_url"].endswith("never-cloned.git")


# ── install: happy path ─────────────────────────────────────────────

def test_install_engine_adapter_happy_path(tmp_path, store):
    source = _engine_adapter_source(tmp_path)
    record = pl.install("alice", source, store=store)
    assert record["kind"] == "engine_adapter"
    assert record["status"] == "installed"
    assert record["enabled"] is False
    assert record["license_review_status"] == "unreviewed"  # never trusts package's own claim
    assert record["permissions"] == {"network": False, "fs": False, "gpu": True, "cost": False}
    assert os.path.isfile(os.path.join(record["install_dir"], "adapter.py"))
    hist = pl.history("alice", "engine.demo", store=store)
    assert hist[-1]["event_type"] == "installed"


def test_install_forces_unreviewed_even_if_package_claims_otherwise(tmp_path, store):
    root = tmp_path / "pkg_src" / "sneaky"
    manifest = {
        "id": "sneaky", "kind": "engine_adapter", "version": "1.0.0",
        "permissions": {"network": False, "fs": False, "gpu": False, "cost": False},
        "license_review_status": "reviewed_ok",  # a package cannot self-approve
    }
    path = _write_package(root, manifest)
    record = pl.install("alice", {"id": "sneaky", "kind": "engine_adapter",
                                  "transport": "local_dir", "path": path}, store=store)
    assert record["license_review_status"] == "unreviewed"


# ── verify: undeclared permission rejected ──────────────────────────

def test_verify_rejects_package_requesting_undeclared_permission(tmp_path, store):
    source = _engine_adapter_source(
        tmp_path, "engine.sneaky-network",
        permissions={"network": False, "fs": False, "gpu": False, "cost": False},
        requires=["outbound_network_call"],
    )
    with pytest.raises(pl.VerificationFailed) as exc:
        pl.install("alice", source, store=store)
    assert "network" in exc.value.undeclared
    assert store.get("alice", "engine.sneaky-network") is None  # nothing persisted


def test_verify_rejects_skill_requesting_network_without_declaring_it(tmp_path, store):
    root = tmp_path / "pkg_src" / "chatty-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: chatty-skill\n"
        "description: Talks to a remote API without saying so up front.\n"
        "permissions_backends: [network]\n"
        "---\n\n## Body\n", encoding="utf-8",
    )
    source = {"id": "chatty-skill", "kind": "skill", "transport": "local_dir", "path": str(root)}
    with pytest.raises(pl.VerificationFailed) as exc:
        pl.install("alice", source, store=store)
    assert "network" in exc.value.undeclared


def test_verify_rejects_privileged_frontmatter_request(tmp_path, store):
    root = tmp_path / "pkg_src" / "power-grab"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: power-grab\n"
        "description: Asks to turn off approvals for everything else.\n"
        "disabled_tools: [bash]\n"
        "---\n\n## Body\n", encoding="utf-8",
    )
    source = {"id": "power-grab", "kind": "skill", "transport": "local_dir", "path": str(root)}
    with pytest.raises(pl.VerificationFailed, match="privileged"):
        pl.install("alice", source, store=store)


def test_pasting_unknown_github_url_never_executes_anything(tmp_path, store):
    """EXT01 acceptance bar, literally: discovering an unknown GitHub URL
    must not run an installer. `discover()` never fetches; a subsequent
    `install()` against an unreachable URL fails as a transport error, never
    as a partial/garbage install."""
    pl.discover("alice", [{"id": "mystery", "kind": "engine_adapter", "transport": "git",
                           "source_url": "https://github.com/nonexistent/definitely-not-real.git",
                           "ref": "main"}], store=store)
    assert store.get("alice", "mystery") is None
    with pytest.raises(pl.TransportError):
        pl.install("alice", {"id": "mystery", "kind": "engine_adapter", "transport": "git",
                             "source_url": "/nonexistent/path/on/disk.git", "ref": "main"},
                  store=store)
    assert store.get("alice", "mystery") is None


# ── digest tampering after install ──────────────────────────────────

def test_verify_installed_detects_tampering_and_disables(tmp_path, store):
    source = _engine_adapter_source(tmp_path, "engine.tamper")
    record = pl.install("alice", source, store=store)
    pl.enable("alice", "engine.tamper", store=store)
    assert store.get("alice", "engine.tamper")["enabled"] is True

    # Mutate a byte on disk, outside this module's own write path.
    adapter_py = Path(record["install_dir"]) / "adapter.py"
    adapter_py.write_text("# tampered adapter\n", encoding="utf-8")

    result = pl.verify_installed("alice", "engine.tamper", store=store)
    assert result["ok"] is False
    assert result["trace"]
    updated = store.get("alice", "engine.tamper")
    assert updated["enabled"] is False
    assert updated["status"] == "disabled"
    hist = pl.history("alice", "engine.tamper", store=store)
    assert hist[-1]["event_type"] == "digest_mismatch"
    assert hist[-1]["trace"]


def test_verify_installed_ok_when_untouched(tmp_path, store):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.clean"), store=store)
    result = pl.verify_installed("alice", "engine.clean", store=store)
    assert result["ok"] is True


# ── enable / disable / license review ───────────────────────────────

def test_enable_never_calls_approval_store(tmp_path, store, monkeypatch):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.noapprove"), store=store)

    calls = []
    import src.approval_store as approval_store_mod

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("enable() must never open an approval")

    monkeypatch.setattr(approval_store_mod, "request", _spy)
    record = pl.enable("alice", "engine.noapprove", store=store)
    assert record["enabled"] is True
    assert calls == []


def test_reviewed_blocked_prevents_enable(tmp_path, store):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.blocked"), store=store)
    pl.set_license_review("alice", "engine.blocked", "reviewed_blocked",
                          reviewer="admin", note="GPL-3 incompatible", store=store)
    with pytest.raises(pl.PluginBlocked):
        pl.enable("alice", "engine.blocked", store=store)
    assert store.get("alice", "engine.blocked")["enabled"] is False


def test_reviewed_ok_allows_enable(tmp_path, store):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.ok"), store=store)
    pl.set_license_review("alice", "engine.ok", "reviewed_ok", reviewer="admin", store=store)
    record = pl.enable("alice", "engine.ok", store=store)
    assert record["enabled"] is True


def test_check_use_revalidates_every_time(tmp_path, store):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.gate"), store=store)
    with pytest.raises(pl.PluginDisabled):
        pl.check_use("alice", "engine.gate", store=store)
    pl.enable("alice", "engine.gate", store=store)
    pl.check_use("alice", "engine.gate", store=store)  # no raise
    pl.disable("alice", "engine.gate", store=store)
    with pytest.raises(pl.PluginDisabled):
        pl.check_use("alice", "engine.gate", store=store)


def test_permission_policy_is_ceiling_not_grant(tmp_path, store):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.ceiling"), store=store)
    with pytest.raises(pl.PluginDisabled):
        pl.permission_policy("alice", "engine.ceiling", store=store)
    pl.enable("alice", "engine.ceiling", store=store)
    perms = pl.permission_policy("alice", "engine.ceiling", store=store)
    assert perms == {"network": False, "fs": False, "gpu": True, "cost": False}


# ── update / rollback: byte-exact restore ───────────────────────────

def test_update_and_rollback_restore_byte_exact(tmp_path, store):
    v1 = _engine_adapter_source(tmp_path, "engine.versioned")
    pl.install("alice", v1, store=store)
    pl.enable("alice", "engine.versioned", store=store)
    original_bytes = (Path(v1["path"]) / "adapter.py").read_bytes()

    root_v2 = tmp_path / "pkg_src_v2" / "engine.versioned"
    manifest_v2 = {"id": "engine.versioned", "kind": "engine_adapter", "version": "2.0.0",
                  "capabilities": ["txt2img", "img2img"],
                  "permissions": {"network": False, "fs": False, "gpu": True, "cost": False}}
    path_v2 = _write_package(root_v2, manifest_v2, {"adapter.py": "# adapter v2\n"})
    v2_source = {"id": "engine.versioned", "kind": "engine_adapter",
                "transport": "local_dir", "path": path_v2}

    updated = pl.update("alice", "engine.versioned", v2_source, store=store)
    assert updated["version"] == "2.0.0"
    assert updated["enabled"] is False  # new content requires a fresh enable
    assert updated["license_review_status"] == "unreviewed"
    install_dir = Path(updated["install_dir"])
    assert (install_dir / "adapter.py").read_bytes() == b"# adapter v2\n"

    rolled_back = pl.rollback("alice", "engine.versioned", store=store)
    assert rolled_back["version"] == "1.0.0"
    assert (install_dir / "adapter.py").read_bytes() == original_bytes
    hist = pl.history("alice", "engine.versioned", store=store)
    assert [h["event_type"] for h in hist[-2:]] == ["updated", "rolled_back"]


def _build_readonly_target(tmp_path, name: str) -> Path:
    target = tmp_path / name
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "old.txt").write_bytes(b"stale content that must go")
    (target / "readonly.txt").write_bytes(b"stale readonly file")
    os.chmod(target / "readonly.txt", 0o444)
    os.chmod(target / "sub", 0o555)  # read-only directory: entries can't be unlinked as-is
    return target


def _naive_replace(tgt: Path, src: Path) -> None:
    for entry in list(tgt.iterdir()):
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in src.iterdir():
        dest = tgt / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest)
        else:
            shutil.copy2(entry, dest)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses POSIX permission checks — "
                                              "the naive-failure demonstration needs an "
                                              "unprivileged user")
def test_naive_replace_reproduces_the_reported_bug(tmp_path):
    """Proves the fixture actually reproduces the Windows-class failure: a
    plain, non-hardened replace (what the coordinator's report describes as
    `shutil.rmtree(..., ignore_errors=True)` swallowing the problem, or a
    bare `unlink()` on a read-only file on Windows) really does fail against
    a tree with a read-only file and a read-only directory."""
    target = _build_readonly_target(tmp_path, "naive_target")
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.txt").write_bytes(b"fresh content")
    with pytest.raises(PermissionError):
        _naive_replace(target, source)
    os.chmod(target / "sub", 0o755)  # let tmp_path teardown clean up


def test_replace_dir_contents_survives_read_only_entries(tmp_path):
    """POSIX reproduction of the Windows failure this fix closes: a target
    tree with a read-only FILE and a read-only DIRECTORY must still be
    fully replaced, byte for byte, by `_replace_dir_contents` — not
    partially, and not by silently keeping stale bytes around. Compared by
    content digest, not mtimes, so a restore that only touched metadata
    without truly rewriting bytes would still be caught."""
    target = _build_readonly_target(tmp_path, "target")
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.txt").write_bytes(b"fresh content")

    pl._replace_dir_contents(target, source)
    assert sorted(p.name for p in target.iterdir()) == ["new.txt"]
    assert (target / "new.txt").read_bytes() == b"fresh content"
    assert pl._digest_of_dir(str(target)) == pl._digest_of_dir(str(source))


def test_update_and_rollback_survive_read_only_source_fixture(tmp_path, store):
    """The end-to-end path the coordinator's report targets: v1 is
    installed from a fixture with a read-only file and a read-only
    directory (as a checkout or a prior backup might produce), v2 replaces
    it, and rollback restores v1 — compared by CONTENT DIGEST, not mtimes,
    so a restore that merely touched metadata without truly rewriting bytes
    would still be caught.
    """
    root_v1 = tmp_path / "ro_pkg_v1" / "engine.readonly"
    (root_v1 / "sub").mkdir(parents=True)
    (root_v1 / "sub" / "asset.bin").write_bytes(b"v1 nested asset")
    (root_v1 / "adapter.py").write_bytes(b"# v1 adapter\n")
    (root_v1 / "plugin.json").write_text(json.dumps({
        "id": "engine.readonly", "kind": "engine_adapter", "version": "1.0.0",
        "permissions": {"network": False, "fs": False, "gpu": False, "cost": False},
    }), encoding="utf-8")
    os.chmod(root_v1 / "adapter.py", 0o444)
    os.chmod(root_v1 / "sub", 0o555)
    try:
        source_v1 = {"id": "engine.readonly", "kind": "engine_adapter",
                    "transport": "local_dir", "path": str(root_v1)}
        pl.install("alice", source_v1, store=store)
        install_dir = Path(store.get("alice", "engine.readonly")["install_dir"])
        v1_digest = pl._digest_of_dir(str(install_dir))

        root_v2 = tmp_path / "ro_pkg_v2" / "engine.readonly"
        _write_package(root_v2, {
            "id": "engine.readonly", "kind": "engine_adapter", "version": "2.0.0",
            "permissions": {"network": False, "fs": False, "gpu": False, "cost": False},
        }, {"adapter.py": "# v2 adapter\n"})
        updated = pl.update("alice", "engine.readonly",
                            {"id": "engine.readonly", "kind": "engine_adapter",
                             "transport": "local_dir", "path": str(root_v2)}, store=store)
        assert updated["version"] == "2.0.0"
        assert not (install_dir / "sub").exists()  # the read-only v1 dir is truly gone
        assert (install_dir / "adapter.py").read_bytes() == b"# v2 adapter\n"

        rolled_back = pl.rollback("alice", "engine.readonly", store=store)
        assert rolled_back["version"] == "1.0.0"
        assert pl._digest_of_dir(str(install_dir)) == v1_digest
        assert (install_dir / "sub" / "asset.bin").read_bytes() == b"v1 nested asset"
    finally:
        # tmp_path teardown needs these writable again.
        for p in (root_v1 / "sub", root_v1 / "adapter.py"):
            if p.exists():
                os.chmod(p, 0o755 if p.is_dir() else 0o644)


def test_update_transport_failure_never_mutates(tmp_path, store):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.stable"), store=store)
    before = store.get("alice", "engine.stable")
    bad_source = {"id": "engine.stable", "kind": "engine_adapter",
                 "transport": "local_dir", "path": str(tmp_path / "does-not-exist")}
    with pytest.raises(pl.TransportError):
        pl.update("alice", "engine.stable", bad_source, store=store)
    after = store.get("alice", "engine.stable")
    assert after == before  # no retry, no partial mutation


def test_rollback_refuses_when_backup_digest_was_tampered(tmp_path, store):
    v1 = _engine_adapter_source(tmp_path, "engine.tamperback")
    pl.install("alice", v1, store=store)
    root_v2 = tmp_path / "pkg_src_v2b" / "engine.tamperback"
    manifest_v2 = {"id": "engine.tamperback", "kind": "engine_adapter", "version": "2.0.0",
                  "permissions": {"network": False, "fs": False, "gpu": True, "cost": False}}
    path_v2 = _write_package(root_v2, manifest_v2, {"adapter.py": "# v2\n"})
    updated = pl.update("alice", "engine.tamperback",
                        {"id": "engine.tamperback", "kind": "engine_adapter",
                         "transport": "local_dir", "path": path_v2}, store=store)
    backup_dir = Path(store.get("alice", "engine.tamperback")["previous_backup_dir"])
    (backup_dir / "adapter.py").write_text("# tampered backup\n", encoding="utf-8")
    with pytest.raises(pl.PluginError, match="digest"):
        pl.rollback("alice", "engine.tamperback", store=store)


# ── owner isolation ──────────────────────────────────────────────────

def test_owner_isolation(tmp_path, store):
    source_a = _engine_adapter_source(tmp_path, "engine.shared")
    root_b = tmp_path / "pkg_src_b" / "engine.shared"
    manifest_b = {"id": "engine.shared", "kind": "engine_adapter", "version": "9.9.9",
                 "permissions": {"network": False, "fs": False, "gpu": False, "cost": False}}
    path_b = _write_package(root_b, manifest_b, {"adapter.py": "# owner b\n"})
    source_b = {"id": "engine.shared", "kind": "engine_adapter", "transport": "local_dir", "path": path_b}

    rec_a = pl.install("alice", source_a, store=store)
    rec_b = pl.install("bob", source_b, store=store)
    assert rec_a["install_dir"] != rec_b["install_dir"]
    assert rec_a["version"] != rec_b["version"]

    pl.set_env("alice", "engine.shared", {"API_KEY": "alice-secret"}, store=store)
    pl.set_env("bob", "engine.shared", {"API_KEY": "bob-secret"}, store=store)
    assert pl.get_env("alice", "engine.shared", store=store)["API_KEY"] == "alice-secret"
    assert pl.get_env("bob", "engine.shared", store=store)["API_KEY"] == "bob-secret"

    pl.enable("alice", "engine.shared", store=store)
    assert store.get("bob", "engine.shared")["enabled"] is False  # unaffected by alice's enable


def test_env_never_leaks_into_history(tmp_path, store):
    pl.install("alice", _engine_adapter_source(tmp_path, "engine.secrety"), store=store)
    pl.set_env("alice", "engine.secrety", {"TOKEN": "super-secret-value"}, store=store)
    for event in pl.history("alice", "engine.secrety", store=store):
        assert "super-secret-value" not in json.dumps(event)


# ── uninstall retention ──────────────────────────────────────────────

def test_uninstall_retains_files_for_retention_window(tmp_path, store):
    record = pl.install("alice", _engine_adapter_source(tmp_path, "engine.gone"), store=store)
    pl.enable("alice", "engine.gone", store=store)
    pl.uninstall("alice", "engine.gone", retention_days=30, store=store)
    updated = store.get("alice", "engine.gone")
    assert updated["status"] == "uninstalled"
    assert updated["enabled"] is False
    assert os.path.isdir(record["install_dir"])  # not deleted yet
    assert pl.purge_expired(store=store, now=None) == []  # not due yet

    far_future = updated["purge_after"] + 1
    purged = pl.purge_expired(store=store, now=far_future)
    assert purged == ["engine.gone"]
    assert not os.path.isdir(record["install_dir"])
    assert store.get("alice", "engine.gone") is None


def test_uninstall_unknown_plugin_raises_not_found(store):
    with pytest.raises(pl.PluginNotFound):
        pl.uninstall("alice", "no-such-plugin", store=store)


# ── kind==skill over git, reusing skill_sources (no network — local repo) ─

def _git(args, cwd):
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


SKILL_MD_V1 = (
    "---\nname: repo-skill\ndescription: A skill fetched from a local git repo.\n"
    "version: 1.0.0\n---\n\n## Body v1\n"
)
SKILL_MD_V2 = (
    "---\nname: repo-skill\ndescription: A skill fetched from a local git repo (v2).\n"
    "version: 2.0.0\n---\n\n## Body v2\n"
)


def _init_repo(tmp_path) -> Path:
    repo = tmp_path / "skill-repo"
    repo.mkdir()
    _git(["init", "--quiet"], cwd=str(repo))
    (repo / "SKILL.md").write_text(SKILL_MD_V1, encoding="utf-8")
    (repo / "plugin.json").write_text(json.dumps({
        "id": "repo-skill", "kind": "skill", "version": "1.0.0",
        "permissions": {"network": False, "fs": False, "gpu": False, "cost": False},
    }), encoding="utf-8")
    _git(["add", "-A"], cwd=str(repo))
    _git(["commit", "--quiet", "-m", "v1"], cwd=str(repo))
    return repo


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available")
def test_skill_git_install_update_rollback_reuses_skill_sources(tmp_path, store, monkeypatch):
    # skill_sources' own store/backups are isolated the same way
    # tests/acceptance/test_a25_skill_git_source.py does it — never the
    # real DATA_DIR, so this test never depends on (or mutates) shared
    # repo-level state.
    import src.skill_sources as skill_sources_mod
    monkeypatch.setattr(skill_sources_mod, "SKILL_SOURCES_DB", str(tmp_path / "skill_sources.db"))
    monkeypatch.setattr(skill_sources_mod, "BACKUPS_ROOT", str(tmp_path / "skill_sources_backups"))

    repo = _init_repo(tmp_path)
    source = {"id": "repo-skill", "kind": "skill", "transport": "git",
             "source_url": str(repo), "ref": "HEAD"}
    record = pl.install("alice", source, store=store)
    assert record["kind"] == "skill"
    assert record["version"]
    install_dir = Path(record["install_dir"])
    assert (install_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V1

    from src.skill_sources import get_source as skill_get_source
    tracked = skill_get_source(pl._skill_key("alice", "repo-skill"))
    assert tracked is not None  # skill_sources itself tracked the install

    # Advance the branch.
    (repo / "SKILL.md").write_text(SKILL_MD_V2, encoding="utf-8")
    (repo / "plugin.json").write_text(json.dumps({
        "id": "repo-skill", "kind": "skill", "version": "2.0.0",
        "permissions": {"network": False, "fs": False, "gpu": False, "cost": False},
    }), encoding="utf-8")
    _git(["add", "-A"], cwd=str(repo))
    _git(["commit", "--quiet", "-m", "v2"], cwd=str(repo))

    updated = pl.update("alice", "repo-skill", source, store=store)
    assert (install_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V2

    rolled_back = pl.rollback("alice", "repo-skill", store=store)
    assert (install_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V1


# ── HTTP routes ───────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    test_store = pl.PluginStore(db_path=str(tmp_path / "route_plugins.db"),
                                install_root=str(tmp_path / "route_installs"))
    monkeypatch.setattr(pl, "get_store", lambda: test_store)

    from routes.creator_plugin_routes import setup_creator_plugin_routes
    app = FastAPI()
    app.include_router(setup_creator_plugin_routes())
    return TestClient(app), test_store


def test_routes_404_when_creator_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_plugin_routes import setup_creator_plugin_routes
    app = FastAPI()
    app.include_router(setup_creator_plugin_routes())
    client = TestClient(app)
    resp = client.get("/api/creator/plugins")
    assert resp.status_code == 404


def test_route_install_enable_manifest_roundtrip(client, tmp_path):
    api, test_store = client
    source = _engine_adapter_source(tmp_path, "engine.http")
    resp = api.post("/api/creator/plugins/install", json={"source": source})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "installed"

    enabled = api.post("/api/creator/plugins/engine.http/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    manifest = api.get("/api/creator/plugins/engine.http/manifest")
    assert manifest.status_code == 200
    assert manifest.json()["id"] == "engine.http"
    assert "history" in manifest.json()

    listed = api.get("/api/creator/plugins")
    assert listed.status_code == 200
    assert any(p["id"] == "engine.http" for p in listed.json()["plugins"])

    disabled = api.post("/api/creator/plugins/engine.http/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False


def test_route_manifest_404_for_unknown_plugin(client):
    api, _ = client
    resp = api.get("/api/creator/plugins/does-not-exist/manifest")
    assert resp.status_code == 404


def test_route_install_rejects_undeclared_permission(client, tmp_path):
    api, _ = client
    source = _engine_adapter_source(
        tmp_path, "engine.http-sneaky",
        permissions={"network": False, "fs": False, "gpu": False, "cost": False},
        requires=["network_call"],
    )
    resp = api.post("/api/creator/plugins/install", json={"source": source})
    assert resp.status_code == 422
    assert "network" in json.dumps(resp.json())
