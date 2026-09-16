"""A25 — acceptance-parity case.

Trigger (contrato, literal): "Git skill branch changes after install".
Expected (contrato, literal): "Pinned revision remains active until
verified update; rollback restores prior artifact".

`src/skill_sources.py` (new, A25) is exercised here against a REAL local
git repository built in `tmp_path` (`git init`, real commits, the branch
advanced) — no network, no mock of git or of the module under test.

Sequence: `install()` from the repo's first commit pins a SHA; the branch
then advances with a second commit; `check_updates()` sees the branch moved
but the installed skill's `pinned_revision` and on-disk files are
untouched; `update(verify=True)` fetches, verifies (digest + frontmatter +
a script fixture, since the test skill ships one), and only then promotes,
recording the first revision as `previous/`; `rollback()` restores it byte
for byte and the digest check that guards it is shown to actually matter
(the "restore" path also refuses a backup whose digest was tampered with).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.acceptance.conftest import record_evidence

from src.skill_sources import (
    SkillSourceError,
    check_updates,
    get_source,
    install,
    rollback,
    update,
)
import src.skill_sources as skill_sources_mod
from src.skills_runtime.discovery import DiscoveredSkill, skill_digest


SKILL_MD_V1 = """---
name: greet-user
description: Greets the user by name using a small script.
version: 1.0.0
category: general
tags: [demo]
script: greet.py
---

## When to use
When you need to greet someone.

## Procedure
1. Run greet.py with the user's name.
"""

GREET_PY_V1 = "print('hello v1')\n"

SKILL_MD_V2 = """---
name: greet-user
description: Greets the user by name using a small script (v2, louder).
version: 2.0.0
category: general
tags: [demo]
script: greet.py
---

## When to use
When you need to greet someone, loudly.

## Procedure
1. Run greet.py with the user's name.
"""

GREET_PY_V2 = "print('HELLO V2')\n"


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
    repo = tmp_path / "source_repo"
    repo.mkdir()
    _git(["init", "--quiet", "-b", "main"], repo)
    (repo / "SKILL.md").write_text(SKILL_MD_V1, encoding="utf-8")
    (repo / "greet.py").write_text(GREET_PY_V1, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "--quiet", "-m", "v1"], repo)
    return repo


def _advance_branch(repo: Path) -> None:
    (repo / "SKILL.md").write_text(SKILL_MD_V2, encoding="utf-8")
    (repo / "greet.py").write_text(GREET_PY_V2, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "--quiet", "-m", "v2"], repo)


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """A25's own sqlite store and backup root, isolated per test — never the
    real DATA_DIR."""
    monkeypatch.setattr(skill_sources_mod, "SKILL_SOURCES_DB",
                        str(tmp_path / "skill_sources.db"))
    monkeypatch.setattr(skill_sources_mod, "BACKUPS_ROOT",
                        str(tmp_path / "skill_sources_backups"))
    return tmp_path


@pytest.mark.acceptance("A25")
def test_install_pins_the_exact_commit_not_the_branch_name(isolated_store, tmp_path, request):
    repo = _make_source_repo(tmp_path)
    v1_sha = _git(["rev-parse", "HEAD"], repo)
    skill_dir = tmp_path / "installed" / "greet-user"

    src = install(str(skill_dir), f"file://{repo}", "main")

    assert src["pinned_revision"] == v1_sha
    assert src["source_url"] == f"file://{repo}"
    assert src["ref"] == "main"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V1
    assert (skill_dir / "greet.py").read_text(encoding="utf-8") == GREET_PY_V1
    # No stray .git directory leaked into the installed skill.
    assert not (skill_dir / ".git").exists()
    record_evidence(request, mechanism="install", pinned_revision=v1_sha, skill_dir=str(skill_dir))


@pytest.mark.acceptance("A25")
def test_check_updates_sees_the_branch_move_but_does_not_touch_the_active_skill(
    isolated_store, tmp_path, request,
):
    repo = _make_source_repo(tmp_path)
    skill_dir = tmp_path / "installed" / "greet-user"
    src = install(str(skill_dir), f"file://{repo}", "main")
    v1_sha = src["pinned_revision"]

    before_bytes = (skill_dir / "SKILL.md").read_bytes()
    _advance_branch(repo)
    v2_sha = _git(["rev-parse", "HEAD"], repo)
    assert v2_sha != v1_sha

    status = check_updates(str(skill_dir))
    assert status["pinned_revision"] == v1_sha
    assert status["remote_revision"] == v2_sha
    assert status["update_available"] is True

    # The literal acceptance bar: pinned revision remains ACTIVE.
    after = get_source(str(skill_dir))
    assert after["pinned_revision"] == v1_sha
    assert (skill_dir / "SKILL.md").read_bytes() == before_bytes
    record_evidence(request, mechanism="check_updates", pinned_revision=v1_sha,
                    remote_revision=v2_sha, update_available=True)


@pytest.mark.acceptance("A25")
def test_update_verifies_in_staging_before_promoting_and_keeps_a_rollback_backup(
    isolated_store, tmp_path, request,
):
    repo = _make_source_repo(tmp_path)
    skill_dir = tmp_path / "installed" / "greet-user"
    src = install(str(skill_dir), f"file://{repo}", "main")
    v1_sha = src["pinned_revision"]
    v1_digest = src["pinned_digest"]
    _advance_branch(repo)
    v2_sha = _git(["rev-parse", "HEAD"], repo)

    fixture_calls = []

    def run_fixture(script_path):
        fixture_calls.append(script_path)
        out = subprocess.run(["python3", script_path], capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and "HELLO V2" in out.stdout, out.stdout

    result = update(str(skill_dir), verify=True, run_fixture=run_fixture)

    assert result["status"] == "updated"
    assert result["pinned_revision"] == v2_sha
    assert result["previous_revision"] == v1_sha
    assert fixture_calls, "the declared script must be verified against the fixture"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V2
    assert (skill_dir / "greet.py").read_text(encoding="utf-8") == GREET_PY_V2

    src_after = get_source(str(skill_dir))
    assert src_after["pinned_revision"] == v2_sha
    assert src_after["previous_revision"] == v1_sha
    assert src_after["previous_digest"] == v1_digest
    backup_dir = Path(src_after["previous_backup_dir"])
    assert backup_dir.is_dir()
    assert (backup_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V1
    record_evidence(request, mechanism="update", from_revision=v1_sha, to_revision=v2_sha,
                    fixture_ran=True)


@pytest.mark.acceptance("A25")
def test_update_rejected_by_a_failing_fixture_leaves_the_pinned_revision_untouched(
    isolated_store, tmp_path, request,
):
    repo = _make_source_repo(tmp_path)
    skill_dir = tmp_path / "installed" / "greet-user"
    src = install(str(skill_dir), f"file://{repo}", "main")
    v1_sha = src["pinned_revision"]
    _advance_branch(repo)
    before_bytes = (skill_dir / "greet.py").read_bytes()

    def failing_fixture(script_path):
        return False, "fixture says no"

    result = update(str(skill_dir), verify=True, run_fixture=failing_fixture)

    assert result["status"] == "failed"
    assert "fixture" in result["reason"]
    still = get_source(str(skill_dir))
    assert still["pinned_revision"] == v1_sha
    assert (skill_dir / "greet.py").read_bytes() == before_bytes
    record_evidence(request, mechanism="update (rejected)", pinned_revision=v1_sha)


@pytest.mark.acceptance("A25")
def test_rollback_restores_the_prior_artifact_byte_for_byte(isolated_store, tmp_path, request):
    repo = _make_source_repo(tmp_path)
    skill_dir = tmp_path / "installed" / "greet-user"
    src = install(str(skill_dir), f"file://{repo}", "main")
    v1_sha, v1_digest = src["pinned_revision"], src["pinned_digest"]
    _advance_branch(repo)
    v2_sha = _git(["rev-parse", "HEAD"], repo)

    def ok_fixture(script_path):
        return True, "ok"

    updated = update(str(skill_dir), verify=True, run_fixture=ok_fixture)
    assert updated["status"] == "updated"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V2

    restored = rollback(str(skill_dir))

    assert restored["pinned_revision"] == v1_sha
    assert restored["pinned_digest"] == v1_digest
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD_V1
    assert (skill_dir / "greet.py").read_text(encoding="utf-8") == GREET_PY_V1
    found = DiscoveredSkill(name="", path=str(skill_dir / "SKILL.md"), origin="",
                            root=str(skill_dir), distance=0)
    assert skill_digest(found) == v1_digest
    # A rolled-back skill has no further "previous" to roll back to again.
    assert restored["previous_backup_dir"] is None
    with pytest.raises(SkillSourceError):
        rollback(str(skill_dir))
    record_evidence(request, mechanism="rollback", restored_revision=v1_sha,
                    from_revision=v2_sha)


@pytest.mark.acceptance("A25")
def test_rollback_refuses_a_backup_whose_digest_no_longer_matches(
    isolated_store, tmp_path, request,
):
    """The digest check is load-bearing, not decorative: corrupt the backup
    on disk after a successful `update()` and `rollback()` must refuse
    rather than silently restore tampered bytes."""
    repo = _make_source_repo(tmp_path)
    skill_dir = tmp_path / "installed" / "greet-user"
    install(str(skill_dir), f"file://{repo}", "main")
    _advance_branch(repo)

    def ok_fixture(script_path):
        return True, "ok"

    update(str(skill_dir), verify=True, run_fixture=ok_fixture)
    src_after = get_source(str(skill_dir))
    backup_dir = Path(src_after["previous_backup_dir"])
    (backup_dir / "SKILL.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(SkillSourceError, match="digest"):
        rollback(str(skill_dir))
    # Still on v2 — a refused rollback must not have partially applied.
    assert get_source(str(skill_dir))["pinned_revision"] != src_after["previous_revision"]
    record_evidence(request, mechanism="rollback (refused, tampered backup)")
