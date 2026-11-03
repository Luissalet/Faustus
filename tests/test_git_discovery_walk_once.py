"""Projects that link the same folder share one filesystem walk.

Seen live 04-11: three projects linked one 40-repo tree and discovery walked
it three times (~14 s each on Windows), so the repo list took ~50 s and the
Source control panel answered 504.
"""
import os

from src import git_panel


def test_same_root_in_several_projects_is_walked_once(monkeypatch, tmp_path):
    root = str(tmp_path)
    repo = os.path.join(root, "app")
    calls = []

    def walk(path, **kw):
        calls.append(path)
        return [repo]

    monkeypatch.setattr(git_panel, "_walk_repos", walk)
    monkeypatch.setattr(git_panel, "_project_root_folders", lambda project: [root])
    projects = [{"id": f"p{i}", "name": f"P{i}"} for i in range(3)]

    records = git_panel._walk_projects(projects)

    assert calls == [root]
    assert [r["project_id"] for r in records] == ["p0", "p1", "p2"]
    deduped = git_panel._dedupe_repos(records)
    assert len(deduped) == 1 and [p["id"] for p in deduped[0]["projects"]] == ["p0", "p1", "p2"]
