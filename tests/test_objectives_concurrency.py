"""Regression coverage for independent agents updating one project."""

from concurrent.futures import ThreadPoolExecutor
import builtins
from pathlib import Path
import subprocess
import sys
import time

import pytest

from services import objectives as obj


@pytest.fixture
def project(tmp_path):
    return {"workspace": str(tmp_path)}


def test_concurrent_agents_keep_every_objective(project, monkeypatch):
    original = obj.load_state

    def slow_read(project, **kwargs):
        state = original(project, **kwargs)
        time.sleep(0.01)  # widen the read/modify/write race, without a barrier
        return state

    monkeypatch.setattr(obj, "load_state", slow_read)

    def add(index):
        try:
            return obj.apply_deltas(project, [{"op": "ADD", "title": f"Work {index}"}], "agent")
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(add, range(32)))
    assert not [r for r in results if isinstance(r, Exception)]
    state = original(project)
    assert len(state["objectives"]) == 32
    assert len({r["applied"][0]["id"] for r in results}) == 32
    assert len(obj.read_log(project, 100)) == 32


@pytest.mark.parametrize("op", ["EDIT", "KILL"])
def test_same_clock_tick_human_change_rejects_stale_agent(project, monkeypatch, op):
    monkeypatch.setattr(obj, "_now_iso", lambda: "2026-09-07T12:00:00Z")
    first = obj.apply_deltas(project, [{"op": "ADD", "title": "Keep this"}], "agent")
    base = first["state"]["objectives"][0]["updated_at"]
    obj.apply_deltas(project, [{"op": "EDIT", "id": "OBJ-1", "notes": "Human decision"}], "user")
    result = obj.apply_deltas(project, [{
        "op": op, "id": "OBJ-1", "notes": "Stale decision",
        "base_updated_at": base, "rationale": "An older plan",
    }], "agent")
    assert not result["applied"]
    assert "human edit wins" in result["conflicts"][0]["reason"]
    stored = obj.load_state(project)["objectives"]["OBJ-1"]
    assert stored["notes"] == "Human decision"
    assert stored["status"] == "open"


def test_second_precision_snapshot_detects_fractional_human_edit(project, monkeypatch):
    monkeypatch.setattr(obj, "_now_iso", lambda: "2026-09-07T12:00:00Z")
    obj.apply_deltas(project, [{"op": "ADD", "title": "Legacy timestamp"}], "agent")
    monkeypatch.setattr(obj, "_now_iso", lambda: "2026-09-07T12:00:00.123456Z")
    obj.apply_deltas(project, [{"op": "EDIT", "id": "OBJ-1", "priority": 1}], "user")
    result = obj.apply_deltas(project, [{
        "op": "EDIT", "id": "OBJ-1", "priority": 4,
        "base_updated_at": "2026-09-07T12:00:00Z",
    }], "agent")
    assert not result["applied"]
    assert obj.load_state(project)["objectives"]["OBJ-1"]["priority"] == 1


def test_independent_processes_keep_every_objective(project):
    processes = []
    try:
        for index in range(4):
            processes.append(subprocess.Popen([
                sys.executable, "-c",
                "import sys; from services.objectives import apply_deltas; "
                "p={'workspace':sys.argv[1]}; "
                "[apply_deltas(p,[{'op':'ADD','title':sys.argv[2]+'-'+str(i)}],'agent') "
                "for i in range(8)]",
                project["workspace"], str(index),
            ], cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for child in processes:
            _, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr.decode(errors="replace")
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
    assert len(obj.load_state(project)["objectives"]) == 32
    assert len(obj.read_log(project, 100)) == 32


def test_read_failure_never_replaces_existing_objectives(project, monkeypatch):
    obj.apply_deltas(project, [{"op": "ADD", "title": "Must survive"}], "user")
    path = Path(obj.objectives_path(project))
    before = path.read_bytes()
    original = builtins.open

    def unreadable(file, mode="r", *args, **kwargs):
        if Path(file) == path and mode == "rb":
            raise PermissionError("Synthetic read failure")
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", unreadable)
    assert obj.load_state(project) == {"objectives": {}, "edges": []}
    with pytest.raises(obj.ObjectiveError, match="Synthetic read failure"):
        obj.apply_deltas(project, [{"op": "ADD", "title": "Must not replace"}], "agent")
    assert path.read_bytes() == before


def test_failed_atomic_replace_cleans_temp_and_preserves_original(project, monkeypatch):
    obj.apply_deltas(project, [{"op": "ADD", "title": "Original"}], "user")
    path = Path(obj.objectives_path(project))
    before = path.read_bytes()

    def failure(*args):
        raise PermissionError("Synthetic replace failure")

    monkeypatch.setattr(obj.os, "replace", failure)
    with pytest.raises(obj.ObjectiveError, match="Synthetic replace failure"):
        obj.apply_deltas(project, [{"op": "ADD", "title": "Cannot save"}], "agent")
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("deps", [42, True, "OBJ-1", {"OBJ-1": True}, [None], [{}]])
@pytest.mark.parametrize("op", ["ADD", "EDIT"])
def test_malformed_dependencies_conflict_without_losing_valid_deltas(project, deps, op):
    obj.apply_deltas(project, [{"op": "ADD", "title": "Foundation"}], "user")
    result = obj.apply_deltas(project, [
        {"op": op, "id": "OBJ-1", "title": "Bad dependencies", "deps": deps},
        {"op": "ADD", "title": "Valid work"},
    ], "agent")
    assert len(result["conflicts"]) == 1
    assert [a["fields"]["title"] for a in result["applied"]] == ["Valid work"]


def test_duplicate_dependencies_are_one_edge(project):
    obj.apply_deltas(project, [{"op": "ADD", "title": "Foundation"}], "user")
    result = obj.apply_deltas(project, [{"op": "ADD", "title": "Dependent", "deps": ["OBJ-1", "OBJ-1"]}], "agent")
    assert result["state"]["edges"] == [{"from": "OBJ-2", "to": "OBJ-1"}]


def test_invalid_utf8_is_recovered_without_crashing_chat(project):
    path = Path(obj.objectives_path(project))
    path.parent.mkdir()
    path.write_bytes(b"\xff\xfe broken")
    assert obj.load_state(project) == {"objectives": {}, "edges": []}
    assert Path(str(path) + ".corrupt").read_bytes() == b"\xff\xfe broken"


def test_failed_corrupt_backup_prevents_overwrite(project, monkeypatch):
    path = Path(obj.objectives_path(project))
    path.parent.mkdir()
    path.write_text("not json", encoding="utf-8")
    original = obj.os.replace

    def cannot_backup(source, target):
        if str(target).endswith(".corrupt"):
            raise PermissionError("Cannot back up damaged objectives")
        return original(source, target)

    monkeypatch.setattr(obj.os, "replace", cannot_backup)
    # Hot-path recovery cannot quarantine it; strict writers must still refuse
    # the damaged original rather than treating that fallback as an empty file.
    assert obj.load_state(project) == {"objectives": {}, "edges": []}
    with pytest.raises(obj.ObjectiveError, match="corrupt.*preserved"):
        obj.apply_deltas(project, [{"op": "ADD", "title": "Do not erase evidence"}], "agent")
    assert path.read_text(encoding="utf-8") == "not json"


def test_repeated_corruption_keeps_both_damaged_copies(project):
    path = Path(obj.objectives_path(project))
    path.parent.mkdir()
    for text in ("first damaged copy", "second damaged copy"):
        path.write_text(text, encoding="utf-8")
        obj.load_state(project)
    assert {p.read_text(encoding="utf-8") for p in path.parent.glob("*.corrupt")} == {
        "first damaged copy", "second damaged copy",
    }


def test_linked_metadata_directory_cannot_escape_project(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project["workspace"] = str(workspace)
    link = workspace / obj.OBJECTIVES_DIRNAME
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    with pytest.raises(obj.ObjectiveError, match="linked"):
        obj.apply_deltas(project, [{"op": "ADD", "title": "Outside"}], "agent")
    assert obj.load_state(project) == {"objectives": {}, "edges": []}
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("filename", [obj.OBJECTIVES_FILENAME, obj.OBJECTIVES_LOG_FILENAME, "objectives.lock"])
def test_linked_store_files_are_not_followed(project, tmp_path, filename):
    base = Path(obj.objectives_dir(project))
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve me", encoding="utf-8")
    try:
        (base / filename).symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    with pytest.raises(obj.ObjectiveError, match="linked"):
        obj.apply_deltas(project, [{"op": "ADD", "title": "Not admitted"}], "agent")
    assert outside.read_text(encoding="utf-8") == "preserve me"
