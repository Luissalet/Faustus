from dataclasses import replace

import pytest

from src.skills_runtime import discovery
from tests.test_skills_runtime import write_skill


def test_worktree_git_file_stops_discovery_at_its_repository(tmp_path):
    repo = tmp_path / "worktree"
    child = repo / "src"
    child.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n")
    folder = repo / ".agents" / "skills"
    folder.mkdir(parents=True)
    write_skill(folder, "local")
    assert discovery.roots_for(str(child))[0] == [str(child), str(repo)]
    assert [item.name for item in discovery.discover(str(child))] == ["local"]


def test_loading_rechecks_size_after_discovery(tmp_path, monkeypatch):
    folder = tmp_path / ".agents" / "skills"
    folder.mkdir(parents=True)
    path = write_skill(folder, "changing")
    found, = discovery.discover(str(tmp_path))
    monkeypatch.setattr(discovery, "MAX_SKILL_BYTES", 10)
    with pytest.raises(ValueError, match="larger"):
        discovery.load(found)


def test_loading_does_not_ignore_a_discovery_error(tmp_path):
    folder = tmp_path / ".agents" / "skills"
    folder.mkdir(parents=True)
    write_skill(folder, "invalid")
    found, = discovery.discover(str(tmp_path))
    with pytest.raises(ValueError, match="unreadable"):
        discovery.load(replace(found, error="unreadable source"))


def test_loading_rejects_a_source_outside_the_recorded_root(tmp_path):
    inside = tmp_path / "project"
    outside = tmp_path / "private"
    inside.mkdir()
    outside.mkdir()
    path = write_skill(outside, "outside")
    found = discovery.DiscoveredSkill("outside", str(path), ".agents/skills", str(inside), 0)
    with pytest.raises(ValueError, match="outside"):
        discovery.load(found)


@pytest.mark.parametrize("link_kind", ["file", "folder"])
def test_discovery_does_not_follow_skill_links_outside_the_root(tmp_path, link_kind):
    root = tmp_path / "project"
    folder = root / ".agents" / "skills"
    folder.mkdir(parents=True)
    outside = tmp_path / "private"
    outside.mkdir()
    source = write_skill(outside, "secret")
    try:
        if link_kind == "file":
            target = folder / "secret"
            target.mkdir()
            (target / "SKILL.md").symlink_to(source)
        else:
            (folder / "secret").symlink_to(outside / "secret", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    assert not [item for item in discovery.discover(str(root)) if not item.error]
