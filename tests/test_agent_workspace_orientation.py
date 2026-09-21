"""The workspace block names what is in the folder.

20-09-2026: later turns in a long agent chat knew the workspace PATH but not
its contents any more (earlier tool rounds are trimmed between turns), and
spent ten to twenty rounds of `ls`/`cd` re-finding the folder and the helper
scripts the same chat had written an hour earlier.
"""
from src import agent_loop


def test_root_entries_are_named(tmp_path):
    (tmp_path / "cults3d_gen.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "1-3").mkdir()
    (tmp_path / ".faustus").mkdir()  # hidden: not worth prompt tokens
    line = agent_loop._workspace_orientation(str(tmp_path))
    assert "cults3d_gen.py" in line
    assert "1-3/" in line
    assert ".faustus" not in line
    assert line.endswith("\n")

    rules = agent_loop._workspace_coding_rules(str(tmp_path))
    assert str(tmp_path) in rules
    assert "cults3d_gen.py" in rules


def test_a_huge_folder_is_capped(tmp_path):
    for i in range(400):
        (tmp_path / f"folder-{i:03d}").mkdir()
    line = agent_loop._workspace_orientation(str(tmp_path))
    assert len(line) < 800, "the prompt line must stay small"
    assert "more)" in line


def test_no_workspace_and_unreadable_folder_are_silent(tmp_path):
    assert agent_loop._workspace_orientation("") == ""
    assert agent_loop._workspace_orientation(str(tmp_path / "does-not-exist")) == ""
    assert agent_loop._workspace_coding_rules(None) == ""


def test_empty_folder_says_so(tmp_path):
    assert "empty" in agent_loop._workspace_orientation(str(tmp_path))
