"""IDX-01 — identidad de carpetas y fuentes (src/project_identity.py).

`services/projects.py::ProjectStore` already keeps `project_id` stable
across a `workspace` change (see
`tests/test_project_identity.py::test_project_id_survives_renaming_the_folder`
and `tests/qa/test_qa_48_migracion_de_carpeta.py`). This module adds the two
things that were still missing per docs/spec/v2/MAPA_REUTILIZACION.md (IDX-01
row): a `.faustus/project.json` marker the FOLDER carries (id + a structure
fingerprint), and `relocate(project_id, new_path)` — an entry point that
refuses an absent path explicitly instead of pointing a project at nothing.

Before `src/project_identity.py` existed none of this was possible: these
tests fail with `ModuleNotFoundError: No module named 'src.project_identity'`
if the module is removed.
"""

import os

import pytest

from services.projects import ProjectStore
from src import project_identity as pid


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(str(tmp_path / "data"))


def _folder(tmp_path, name):
    path = str(tmp_path / name)
    os.makedirs(path, exist_ok=True)
    return path


def test_write_and_read_marker_round_trips(tmp_path):
    workspace = _folder(tmp_path, "proj")
    with open(os.path.join(workspace, "a.txt"), "w") as fh:
        fh.write("hello")

    marker = pid.write_marker(workspace, "proj-123")
    assert marker["project_id"] == "proj-123"
    assert marker["structure_hash"]

    reread = pid.read_marker(workspace)
    assert reread["project_id"] == "proj-123"
    assert reread["structure_hash"] == marker["structure_hash"]
    assert os.path.isfile(pid.marker_path(workspace))


def test_read_marker_is_none_when_absent_or_unreadable(tmp_path):
    workspace = _folder(tmp_path, "bare")
    assert pid.read_marker(workspace) is None

    marker_dir = os.path.join(workspace, pid.MARKER_DIRNAME)
    os.makedirs(marker_dir, exist_ok=True)
    with open(pid.marker_path(workspace), "w") as fh:
        fh.write("{not json")
    assert pid.read_marker(workspace) is None       # corrupt, not a crash


def test_write_marker_refuses_to_overwrite_a_different_projects_marker(tmp_path):
    workspace = _folder(tmp_path, "shared")
    pid.write_marker(workspace, "proj-A")
    with pytest.raises(pid.ProjectIdentityError):
        pid.write_marker(workspace, "proj-B")
    # Re-writing the SAME id is fine (a refresh, not a conflict).
    refreshed = pid.write_marker(workspace, "proj-A")
    assert refreshed["project_id"] == "proj-A"


def test_structure_hash_changes_when_files_change_and_ignores_noise_dirs(tmp_path):
    workspace = _folder(tmp_path, "hashed")
    with open(os.path.join(workspace, "a.txt"), "w") as fh:
        fh.write("v1")
    before = pid.compute_structure_hash(workspace)

    noisy = os.path.join(workspace, "node_modules", "pkg")
    os.makedirs(noisy, exist_ok=True)
    with open(os.path.join(noisy, "index.js"), "w") as fh:
        fh.write("noise")
    assert pid.compute_structure_hash(workspace) == before  # ignored dir, no change

    with open(os.path.join(workspace, "b.txt"), "w") as fh:
        fh.write("v2")
    after = pid.compute_structure_hash(workspace)
    assert after != before


def test_structure_hash_of_a_missing_path_is_empty(tmp_path):
    assert pid.compute_structure_hash(str(tmp_path / "nope")) == ""


def test_verify_marker_reports_id_and_structure_agreement(tmp_path):
    workspace = _folder(tmp_path, "verify")
    with open(os.path.join(workspace, "a.txt"), "w") as fh:
        fh.write("v1")
    pid.write_marker(workspace, "proj-1")

    same = pid.verify_marker(workspace, "proj-1")
    assert same == {"present": True, "project_id_matches": True, "structure_hash_matches": True}

    wrong_id = pid.verify_marker(workspace, "proj-2")
    assert wrong_id["project_id_matches"] is False

    with open(os.path.join(workspace, "b.txt"), "w") as fh:
        fh.write("v2")
    changed = pid.verify_marker(workspace, "proj-1")
    assert changed["structure_hash_matches"] is False

    never_marked = pid.verify_marker(_folder(tmp_path, "unmarked"), "proj-1")
    assert never_marked == {"present": False, "project_id_matches": None, "structure_hash_matches": None}


def test_relocate_moves_workspace_and_keeps_project_id(tmp_path, store):
    old_ws = _folder(tmp_path, "D_old/Faustus")
    project = store.create("Faustus", folder="Faustus", workspace=old_ws)

    new_ws = _folder(tmp_path, "E_new_disk/Faustus")
    result = pid.relocate(project["id"], new_ws, store=store)

    assert result["project"]["id"] == project["id"]           # identity unchanged
    assert result["project"]["workspace"] == new_ws
    assert result["old_workspace"] == old_ws
    assert result["old_path_missing"] is False                # old_ws still exists here
    assert result["marker"]["project_id"] == project["id"]
    assert pid.read_marker(new_ws)["project_id"] == project["id"]

    # Fetching the project again shows the relocation persisted.
    refetched = store.get(project["id"])
    assert refetched["workspace"] == new_ws


def test_relocate_flags_when_the_old_path_is_actually_gone(tmp_path, store):
    """The real disk-migration case: the old folder was already moved/deleted
    by the time `relocate` runs."""
    old_ws = str(tmp_path / "D_old" / "Faustus")   # never created
    project = store.create("Faustus", folder="Faustus", workspace="")
    store.update(project["id"], {"workspace": ""})
    # Force a stale, now-missing workspace onto the row directly (simulates a
    # disk that was already unplugged/renamed before Faustus noticed).
    rows = store._load()
    for row in rows:
        if row["id"] == project["id"]:
            row["workspace"] = old_ws
    store._save(rows)

    new_ws = _folder(tmp_path, "E_new_disk/Faustus")
    result = pid.relocate(project["id"], new_ws, store=store)
    assert result["old_path_missing"] is True
    assert result["project"]["workspace"] == new_ws


def test_relocate_refuses_a_path_that_does_not_exist(tmp_path, store):
    old_ws = _folder(tmp_path, "D_old/Faustus")
    project = store.create("Faustus", folder="Faustus", workspace=old_ws)

    missing = str(tmp_path / "does" / "not" / "exist")
    with pytest.raises(pid.ProjectIdentityError):
        pid.relocate(project["id"], missing, store=store)

    # Nothing changed: the explicit-resolution requirement is "refuse and
    # leave things as they were", never "point at nothing".
    untouched = store.get(project["id"])
    assert untouched["workspace"] == old_ws


def test_relocate_of_an_unknown_project_id_raises(tmp_path, store):
    with pytest.raises(pid.ProjectIdentityError):
        pid.relocate("does-not-exist", str(tmp_path), store=store)


def test_relocate_flags_a_marker_left_by_a_different_project(tmp_path, store):
    old_ws = _folder(tmp_path, "D_old/Faustus")
    project = store.create("Faustus", folder="Faustus", workspace=old_ws)

    new_ws = _folder(tmp_path, "reused_folder")
    pid.write_marker(new_ws, "someone-elses-project-id")

    result = pid.relocate(project["id"], new_ws, store=store)
    assert result["marker_conflict"] is True
    # The relocate still goes through (it is an explicit user action); the
    # marker at the destination is updated to the NEW owner.
    assert pid.read_marker(new_ws)["project_id"] == project["id"]


def test_ensure_marker_backfills_quietly_for_a_pre_existing_project(tmp_path, store):
    workspace = _folder(tmp_path, "backfilled")
    project = store.create("Faustus", folder="Faustus", workspace=workspace)
    assert pid.read_marker(workspace) is None          # created before this module existed

    marker = pid.ensure_marker(project)
    assert marker["project_id"] == project["id"]
    assert pid.read_marker(workspace)["project_id"] == project["id"]

    # Idempotent: calling again does not raise or duplicate work.
    assert pid.ensure_marker(project) is None


def test_ensure_marker_skips_a_project_with_no_workspace_on_disk(tmp_path, store):
    project = store.create("NoFolder", folder="NoFolder", workspace="")
    assert pid.ensure_marker(project) is None
