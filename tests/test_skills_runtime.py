"""The skills bridge (src/skills_runtime) — a SKILL.md read as a capability.

Two rules carry the package, and both are tested by trying to break them:

* **deny by default** — a SKILL.md that says nothing about permissions gets
  none, which means no backend, which means nothing can run it. That reads
  like a bug the first time and it is the point;
* **provenance never elevates** — the same file in `.claude/skills` and in
  `.odysseus/skills` produces the same manifest, byte for byte, because where
  a file sits is not a statement about what it may do.

The third thing checked here is smaller and cost a rewrite: the frontmatter
parser has no nested maps, so a `permissions:` block cannot be written in a
SKILL.md at all. The bridge reads flat `permissions_*` keys instead, and a
declaration that goes through `Skill` rather than the file text loses them —
which is why `manifest_from_markdown` exists.
"""
from __future__ import annotations

import os

import pytest

from src.contracts import ContractError
from src.skills_runtime import bridge, discovery


def write_skill(folder, name, *, extra_lines=(), version="1.0.0", category="general"):
    path = os.path.join(str(folder), name)
    os.makedirs(path, exist_ok=True)
    lines = ["---", f"name: {name}", "description: does a thing",
             f"version: {version}", f"category: {category}"]
    lines += list(extra_lines)
    lines += ["---", "", "## When to Use", "- when testing", "",
              "## Procedure", "- step one", ""]
    file_path = os.path.join(path, "SKILL.md")
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return file_path


def manifest_of(path):
    with open(path, encoding="utf-8") as fh:
        return bridge.manifest_from_markdown(fh.read(), source=path)


# ── deny by default ────────────────────────────────────────────────────────

def test_a_skill_that_asks_for_nothing_gets_nothing_and_cannot_run(tmp_path):
    from src import capability_registry as registry

    manifest = manifest_of(write_skill(tmp_path, "quiet-skill"))
    assert manifest.permissions.backends == ()
    assert manifest.permissions.network is False
    assert manifest.permissions.secrets == ()
    assert manifest.permissions.host_access is False

    rows = registry.candidates(manifest)
    assert not any(r["ok"] for r in rows)
    assert {r["reason"] for r in rows} == {"no_backend_declared"}
    why = registry.why_no_backend(manifest)
    assert "declares no backend" in why
    assert "an empty list means none, never any" in why


def test_an_empty_backend_list_is_none_and_not_any(tmp_path):
    """The bug the real skills folder found: an empty list used to mean "no
    filter", so a manifest that never asked for anywhere to run was eligible
    everywhere."""
    from src import capability_registry as registry
    manifest = manifest_of(write_skill(tmp_path, "empty-list",
                                       extra_lines=["permissions_backends: []"]))
    assert manifest.permissions.backends == ()
    assert not any(r["ok"] for r in registry.candidates(manifest))


def test_declared_permissions_come_through_from_flat_keys(tmp_path):
    manifest = manifest_of(write_skill(tmp_path, "loud-skill", extra_lines=[
        "permissions_backends: [docker_workspace]",
        "permissions_network: true",
        "permissions_secrets: [github]",
        "permissions_max_seconds: 300",
        "outputs: [report=artifact:document]",
        "memory_read_scopes: [project]",
        "approval_required_when: [publish]",
    ]))
    assert manifest.permissions.backends == ("docker_workspace",)
    assert manifest.permissions.network is True
    assert manifest.permissions.secrets == ("github",)
    assert manifest.permissions.max_seconds == 300
    assert manifest.outputs == (("report", manifest.outputs[0][1]),)
    assert str(manifest.outputs[0][1]) == "artifact:document"
    assert manifest.memory.read_scopes == ("project",)
    # And asking for the network earns the card even though only `publish` was
    # declared — the same rule that applies to any manifest.
    assert set(manifest.effective_approvals()) == {"publish", "network", "secrets"}


# ── provenance never elevates ──────────────────────────────────────────────

def test_the_same_skill_in_three_folders_gets_the_same_manifest(tmp_path):
    bodies = []
    for origin in discovery.SKILL_DIR_NAMES:
        folder = tmp_path / origin
        folder.mkdir(parents=True, exist_ok=True)
        bodies.append(manifest_of(write_skill(folder, "same-skill")))
    fingerprints = {m.fingerprint() for m in bodies}
    assert len(fingerprints) == 1, "where a skill lives changed what it may do"
    assert all(m.permissions.backends == () for m in bodies)


def test_being_found_next_to_the_work_does_not_grant_anything(tmp_path):
    (tmp_path / ".git").mkdir()                     # the walk stops here
    near = tmp_path / "project" / ".odysseus" / "skills"
    far = tmp_path / ".claude" / "skills"
    near.mkdir(parents=True)
    far.mkdir(parents=True)
    write_skill(near, "twin")
    write_skill(far, "twin")

    found = discovery.discover(str(tmp_path / "project"))
    assert [f.distance for f in found] == [0, 1]        # nearest first
    assert found[0].origin.endswith(os.path.join(".odysseus", "skills"))
    assert found[1].origin.endswith(os.path.join(".claude", "skills"))

    a, b = manifest_of(found[0].path), manifest_of(found[1].path)
    assert a.fingerprint() == b.fingerprint()


# ── refusals that name the field ───────────────────────────────────────────

def test_a_version_that_cannot_be_compared_is_refused_not_coerced(tmp_path):
    path = write_skill(tmp_path, "bad-version", version="1.0")
    with pytest.raises(ContractError) as err:
        manifest_of(path)
    assert "version" in err.value.path
    assert "semantic version" in err.value.message


def test_a_typo_in_a_permission_key_is_named(tmp_path):
    path = write_skill(tmp_path, "typo-skill",
                       extra_lines=["permissions_host_acces: true"])
    with pytest.raises(ContractError) as err:
        manifest_of(path)
    assert "host_acces" in str(err.value)
    assert "did you mean 'host_access'" in str(err.value)


def test_a_truthy_string_is_not_a_permission_here_either(tmp_path):
    path = write_skill(tmp_path, "truthy-skill",
                       extra_lines=["permissions_network: 'yes'",
                                    "permissions_backends: [docker_workspace]"])
    with pytest.raises(ContractError) as err:
        manifest_of(path)
    assert "network" in err.value.path


def test_an_output_entry_has_to_say_name_equals_type(tmp_path):
    path = write_skill(tmp_path, "bad-outputs", extra_lines=["outputs: [report]"])
    with pytest.raises(ContractError) as err:
        manifest_of(path)
    assert "name=type" in err.value.message


# ── discovery ──────────────────────────────────────────────────────────────

def test_the_walk_stops_at_the_repository_and_never_reaches_your_home(tmp_path):
    """The first version climbed to the filesystem root, so a scratch folder
    picked up the user's personal `.claude/skills` from their home directory.
    A test in a temp path found a real skill of Luis's — which is how this was
    caught."""
    project = tmp_path / "outside" / "repo" / "pkg"
    project.mkdir(parents=True)
    (tmp_path / "outside" / "repo" / ".git").mkdir()
    home_ish = tmp_path / ".claude" / "skills"
    home_ish.mkdir(parents=True)
    write_skill(home_ish, "personal-skill")

    roots, reason = discovery.roots_for(str(project))
    assert reason == "repository root"
    assert roots[-1] == str(tmp_path / "outside" / "repo")
    assert str(tmp_path) not in roots
    assert [f.name for f in discovery.discover(str(project))] == []

    # And with no repository at all, it does not climb one directory.
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    roots, reason = discovery.roots_for(str(lonely))
    assert roots == [str(lonely)]
    assert "not a repository" in reason


def test_discovery_walks_up_and_records_where_each_one_came_from(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".odysseus" / "skills").mkdir(parents=True)
    (tmp_path / "a" / "b" / ".claude" / "skills").mkdir(parents=True)
    write_skill(tmp_path / ".odysseus" / "skills", "top")
    write_skill(tmp_path / "a" / "b" / ".claude" / "skills", "deep")

    found = {f.name: f for f in discovery.discover(str(tmp_path / "a" / "b"))}
    assert set(found) == {"top", "deep"}
    assert found["deep"].distance == 0
    assert found["top"].distance == 2
    assert found["top"].bytes > 0 and found["top"].error == ""


def test_a_name_in_two_places_is_reported_rather_than_silently_picked(tmp_path):
    (tmp_path / ".git").mkdir()
    for origin in (".odysseus/skills", ".claude/skills"):
        folder = tmp_path / origin
        folder.mkdir(parents=True)
        write_skill(folder, "duplicated")
    found = discovery.discover(str(tmp_path))
    dupes = discovery.shadowed(found)
    assert list(dupes) == ["duplicated"]
    assert len(dupes["duplicated"]) == 2
    assert {os.path.basename(os.path.dirname(os.path.dirname(d.path)))
            for d in dupes["duplicated"]} == {"skills"}


def test_a_document_too_big_to_be_one_is_listed_and_not_loaded(tmp_path, monkeypatch):
    folder = tmp_path / ".odysseus" / "skills"
    folder.mkdir(parents=True)
    path = write_skill(folder, "huge")
    monkeypatch.setattr(discovery, "MAX_SKILL_BYTES", 10)
    found = [f for f in discovery.discover(str(tmp_path)) if f.name == "huge"]
    assert found and "larger than 10 bytes" in found[0].error
    assert found[0].bytes > 10


def test_the_nested_category_layout_is_found_too(tmp_path):
    """`data/skills/<category>/<name>/SKILL.md` and `<folder>/<name>/SKILL.md`
    are both real layouts in this repo."""
    folder = tmp_path / ".odysseus" / "skills" / "writing"
    folder.mkdir(parents=True)
    write_skill(folder, "outline")
    found = discovery.discover(str(tmp_path))
    assert [f.name for f in found] == ["outline"]


# ── the survey an operator reads ───────────────────────────────────────────

def test_the_survey_separates_valid_from_runnable(tmp_path):
    class _Loose:
        def __init__(self, path, name):
            self.path, self.name = path, name

    quiet = write_skill(tmp_path, "quiet")
    loud = write_skill(tmp_path, "loud",
                       extra_lines=["permissions_backends: [docker_workspace]"])
    broken = write_skill(tmp_path, "broken", version="not-a-version")

    results = {r.name: r for r in bridge.survey(
        [_Loose(quiet, "quiet"), _Loose(loud, "loud"), _Loose(broken, "broken")])}

    assert results["quiet"].ok is True and results["quiet"].runnable is False
    assert "declares no backend" in results["quiet"].why_not
    assert results["loud"].ok is True                 # runnable depends on docker
    assert results["broken"].ok is False
    assert "version" in results["broken"].error_path
