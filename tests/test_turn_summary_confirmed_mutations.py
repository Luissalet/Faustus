"""The turn summary's "mutations" (the "Changes" list in the UI) must only
name files that really changed, were created, or were deleted this turn --
never a path a tool call merely claimed to touch. Seen live: the list
included a CSV whose content never changed (a no-op edit reported success)
and a JSON file in the workspace root that was never created at all.
"""
import json
import shutil

import pytest

_HAS_GIT = shutil.which("git") is not None


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "pokemon_species.csv").write_text("id,name\n1,bulbasaur\n", encoding="utf-8")
    return ws


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_only_really_changed_paths_are_reported(workspace, data_dir):
    from src import workspace_checkpoints as wc
    from src.agent_harness import TurnLedger

    cp = wc.checkpoint(str(workspace), "before")
    assert cp and cp.get("sha")

    # Real change: this one should survive.
    (workspace / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    ledger = TurnLedger(workspace=str(workspace), user_text="arregla add y guarda el csv")
    ledger.checkpoint = cp

    # A real edit.
    ledger.record("edit_file", json.dumps({"path": "src/calc.py", "old_string": "a - b", "new_string": "a + b"}),
                   {"output": "Edited src/calc.py (1 replacement)", "exit_code": 0})
    # A "successful" edit that changed nothing on disk (old_string == new_string,
    # or the model re-wrote the same content) -- the pokemon_species.csv from
    # the bug report.
    ledger.record("write_file", json.dumps({"path": "pokemon_species.csv", "content": "id,name\n1,bulbasaur\n"}),
                   {"output": "Wrote pokemon_species.csv", "exit_code": 0})
    # A claimed write to a path that was never actually created -- the
    # cults3d.json from the bug report.
    ledger.record("write_file", json.dumps({"path": "cults3d.json", "content": "{}"}),
                   {"output": "Wrote cults3d.json", "exit_code": 0})

    assert set(ledger.mutated_paths()) == {"src/calc.py", "pokemon_species.csv", "cults3d.json"}
    assert ledger.confirmed_mutated_paths() == ["src/calc.py"]
    assert ledger.summary()["mutations"] == ["src/calc.py"]


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_a_real_deletion_is_still_reported(workspace, data_dir):
    from src import workspace_checkpoints as wc
    from src.agent_harness import TurnLedger

    cp = wc.checkpoint(str(workspace), "before")
    (workspace / "pokemon_species.csv").unlink()

    ledger = TurnLedger(workspace=str(workspace), user_text="borra el csv que ya no hace falta")
    ledger.checkpoint = cp
    ledger.record("bash", "rm pokemon_species.csv", {"output": "", "exit_code": 0})

    assert ledger.confirmed_mutated_paths() == ["pokemon_species.csv"]


def test_without_a_checkpoint_a_nonexistent_path_is_dropped(workspace):
    """No checkpoint available at all (checkpoints off / no git): the
    weakest thing still worth checking -- a claimed path that plainly is not
    on disk -- is still enforced."""
    from src.agent_harness import TurnLedger

    ledger = TurnLedger(workspace=str(workspace), user_text="algo")
    ledger.checkpoint = {"skipped": True}
    ledger.record("write_file", json.dumps({"path": "src/calc.py", "content": "x"}),
                   {"output": "ok", "exit_code": 0})
    ledger.record("write_file", json.dumps({"path": "cults3d.json", "content": "{}"}),
                   {"output": "ok", "exit_code": 0})

    assert ledger.confirmed_mutated_paths() == ["src/calc.py"]
