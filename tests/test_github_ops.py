"""`/github` in the composer (src/github_ops.py) and its twins: the agent tools
(src/agent_tools/github_ops_tools.py), the HTTP routes (routes/github_routes.py)
and the MCP server (mcp_servers/github_server.py).

Real `git` in throw-away repositories (skipped cleanly without it); `gh` is
never run: ``github_ops.run_gh`` is replaced by a canned double that records
every argv, so the tests can also prove what was NOT called (no remote write
before the policy and the approval).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_git_policy, constants as constants_mod, git_panel, github_ops  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from src import tool_execution as te  # noqa: E402
from src.git_github import GhNotAvailableError  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"
GITHUB_URL = "git@github.com:acme/widgets.git"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _git(args, cwd, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc


def _commit(path, name, content, message):
    fp = path / name
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["-c", "user.name=Dev One", "-c", "user.email=dev@example.com", "commit", "-m", message], path)


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("git_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-such-system-gitconfig"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(data_dir / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setattr(te, "owner_is_admin_or_single_user", lambda owner: True)
    github_ops._PENDING.clear()
    yield
    github_ops._PENDING.clear()


class FakeGh:
    """A canned `gh`. Records every argv in ``calls``; ``writes`` lists the ones
    that would have changed GitHub."""

    def __init__(self):
        self.calls = []
        self.labels = [
            {"name": "bug", "color": "d73a4a", "description": "Something is broken"},
            {"name": "stale", "color": "cccccc", "description": ""},
        ]
        self.prs = []
        self.runs = []
        self.issues = {}
        self.prs_open = {}
        self.fail = None  # stderr text -> every call fails with it
        self.missing = False

    @property
    def writes(self):
        return [c for c in self.calls if c[:2] in (["label", "create"], ["label", "edit"])]

    def __call__(self, args, cwd=None, *, timeout=25.0):
        if self.missing:
            raise GhNotAvailableError()
        a = list(args)
        self.calls.append(a)
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.fail)
        ok = lambda payload: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")  # noqa: E731
        if a[:2] == ["label", "list"]:
            return ok(self.labels)
        if a[:2] in (["label", "create"], ["label", "edit"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if a[:2] == ["pr", "list"] and "merged" in a:
            return ok(self.prs)
        if a[:2] == ["run", "list"]:
            return ok(self.runs)
        if a[:2] == ["issue", "list"]:
            return ok(self.issues.get(a[a.index("--label") + 1], []))
        if a[:2] == ["pr", "list"]:
            return ok(self.prs_open.get(a[a.index("--label") + 1], []))
        return SimpleNamespace(returncode=1, stdout="", stderr=f"fake gh: unexpected {a}")


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(github_ops, "run_gh", fake)
    return fake


@pytest.fixture
def repo(tmp_path):
    """master: Initial (v1.0.0) -> squash PR 12 -> squash PR 13 -> merge commit
    PR 14 (+ the branch commit it merges) -> chore -> untitled tweak."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["symbolic-ref", "HEAD", "refs/heads/master"], r)
    _commit(r, "package.json", json.dumps({"name": "widgets", "version": "1.0.0"}), "Initial commit")
    _git(["tag", "v1.0.0"], r)
    _commit(r, "a.txt", "1", "feat: add export (#12)")
    _commit(r, "b.txt", "2", "fix(api): handle empty body (#13)")
    _git(["checkout", "-q", "-b", "docs-pass"], r)
    _commit(r, "docs.md", "d", "docs: rewrite the guide")
    _git(["checkout", "-q", "master"], r)
    _git(["-c", "user.name=Dev One", "-c", "user.email=dev@example.com", "merge", "--no-ff", "docs-pass",
          "-m", "Merge pull request #14 from acme/docs-pass\n\nDocs pass"], r)
    _commit(r, "c.txt", "3", "chore: bump deps")
    _commit(r, "d.txt", "4", "Tweak readme")
    _git(["remote", "add", "origin", GITHUB_URL], r)
    return r


@pytest.fixture
def ws(repo):
    ws_token = te._active_workspace.set(str(repo))
    roots_token = te._active_workspace_roots.set((str(repo),))
    try:
        yield repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


PRS = [
    {"number": 12, "title": "Add export", "labels": [{"name": "enhancement"}], "author": {"login": "luis"}, "url": "https://github.com/acme/widgets/pull/12"},
    {"number": 13, "title": "Handle empty body", "labels": [{"name": "bug"}], "author": {"login": "marta"}, "url": "https://github.com/acme/widgets/pull/13"},
    {"number": 14, "title": "Docs pass", "labels": [{"name": "documentation"}], "author": {"login": "luis"}, "url": "https://github.com/acme/widgets/pull/14"},
    {"number": 99, "title": "Not in range", "labels": [], "author": {"login": "x"}, "url": ""},
]

LABEL_FILE = """\
labels:
  - name: bug
    color: "#D73A4A"
    description: Something is broken
  - name: blocker
    color: b60205
    description: Stops a release
  - name: Stale
    color: eeeeee
"""


def _write_label_file(repo, text=LABEL_FILE, name=".github/labels.yml"):
    _commit(repo, name, text, "add label file")


# ---------------------------------------------------------------------------
# command parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line,expected", [
    ("", {"action": "help"}),
    ("help", {"action": "help"}),
    ("labels", {"action": "labels"}),
    ("label list", {"action": "labels"}),
    ("etiquetas", {"action": "labels"}),
    ("labels sync .github/labels.yml", {"action": "labels_sync", "file": ".github/labels.yml"}),
    ('labels sync "my labels.json"', {"action": "labels_sync", "file": "my labels.json"}),
    ("notes", {"action": "notes", "from": None, "to": None}),
    ("notes v1.0.0..v1.1.0", {"action": "notes", "from": "v1.0.0", "to": "v1.1.0"}),
    ("notes v1.0.0...HEAD", {"action": "notes", "from": "v1.0.0", "to": "HEAD"}),
    ("notes v1.0.0..", {"action": "notes", "from": "v1.0.0", "to": None}),
    ("notes ..release/1.2", {"action": "notes", "from": None, "to": "release/1.2"}),
    ("notes v2.0.0", {"action": "notes", "from": "v2.0.0", "to": None}),
    ("changelog v1..v2", {"action": "notes", "from": "v1", "to": "v2"}),
    ("release-check", {"action": "release_check", "tag": None}),
    ("release-check v1.2.0", {"action": "release_check", "tag": "v1.2.0"}),
    ("check v1.2.0", {"action": "release_check", "tag": "v1.2.0"}),
])
def test_parse_command_accepts(line, expected):
    got = github_ops.parse_command(line)
    assert got["ok"] is True
    for key, value in expected.items():
        assert got[key] == value, (line, got)


@pytest.mark.parametrize("line", [
    "frobnicate", "labels sync", "labels sync a b", "labels purge",
    "notes -rf", "notes a b", "notes --output=x", "release-check --force", "release-check a b",
    "notes v1;rm", "notes $(whoami)", "release-check ../../etc",
])
def test_parse_command_rejects_and_never_lets_a_ref_look_like_an_option(line):
    got = github_ops.parse_command(line)
    assert got["ok"] is False and got["error"] and got["usage"]


def test_run_command_bad_input_answers_with_usage_markdown(repo, gh):
    res = github_ops.run_command(OWNER, str(repo), "labels nuke")
    assert res["ok"] is False and "/github labels" in res["markdown"]
    assert gh.calls == []


def test_help_in_both_languages(repo):
    assert "Lista las etiquetas" in github_ops.run_command(OWNER, str(repo), "", "es")["markdown"]
    assert "Lists the repository" in github_ops.run_command(OWNER, str(repo), "help", "en")["markdown"]


# ---------------------------------------------------------------------------
# label file + sync plan
# ---------------------------------------------------------------------------
def test_label_file_shapes():
    yml = github_ops.parse_label_text(LABEL_FILE, "l.yml")
    assert [l["name"] for l in yml["labels"]] == ["bug", "blocker", "Stale"]
    assert yml["labels"][0]["color"] == "d73a4a"  # '#' stripped, lower-cased

    as_json = github_ops.parse_label_text(json.dumps([{"name": "x", "color": "ABCDEF"}, "plain"]), "l.json")
    assert [(l["name"], l["color"]) for l in as_json["labels"]] == [("x", "abcdef"), ("plain", "")]

    mapping = github_ops.parse_label_text("bug: {color: ff0000, description: d}\nwip: aaaaaa\n", "l.yaml")
    assert {l["name"]: l["color"] for l in mapping["labels"]} == {"bug": "ff0000", "wip": "aaaaaa"}


@pytest.mark.parametrize("text,name,cls", [
    ("", "l.yml", "labels.empty_file"),
    ("labels: []\n", "l.yml", "labels.empty_file"),
    ("- name: a\n  color: nothex\n", "l.yml", "labels.bad_file"),
    ("- name: a\n- name: A\n", "l.yml", "labels.bad_file"),
    ("- color: ffffff\n", "l.yml", "labels.bad_file"),
    ("- name: " + "x" * 60 + "\n", "l.yml", "labels.bad_file"),
    ("{not json", "l.json", "labels.bad_file"),
    ("just text", "l.yml", "labels.bad_file"),
])
def test_label_file_errors(text, name, cls):
    got = github_ops.parse_label_text(text, name)
    assert got["ok"] is False and got["error_class"] == cls


def test_label_file_must_live_inside_the_repo(repo):
    assert github_ops.load_label_file(str(repo), "../outside.yml")["error_class"] == "labels.outside_repo"
    assert github_ops.load_label_file(str(repo), "nope.yml")["error_class"] == "labels.no_file"


def test_plan_label_sync_is_case_insensitive_and_never_deletes():
    existing = [{"name": "bug", "color": "d73a4a", "description": "Something is broken"},
                {"name": "stale", "color": "cccccc", "description": ""},
                {"name": "legacy", "color": "000000", "description": ""}]
    desired = github_ops.parse_label_text(LABEL_FILE, "l.yml")["labels"]
    plan = github_ops.plan_label_sync(existing, desired)
    assert [c["name"] for c in plan["create"]] == ["blocker"]
    assert [u["name"] for u in plan["update"]] == ["stale"]
    assert plan["update"][0]["changes"]["color"] == {"from": "cccccc", "to": "eeeeee"}
    assert plan["update"][0]["new_name"] == "Stale"  # only the case differs: a rename
    assert plan["unchanged"] == ["bug"]
    assert plan["only_remote"] == ["legacy"]  # reported, never in create/update
    assert plan["changes"] == 2


def test_plan_label_sync_up_to_date():
    existing = [{"name": "bug", "color": "d73a4a", "description": "Something is broken"}]
    desired = [{"name": "BUG", "color": "D73A4A", "description": ""}]
    plan = github_ops.plan_label_sync(existing, desired)
    assert plan["changes"] == 1 and plan["update"][0]["new_name"] == "BUG"
    plan = github_ops.plan_label_sync(existing, [{"name": "bug", "color": "d73a4a", "description": ""}])
    assert plan["changes"] == 0


def test_labels_listing(repo, gh):
    res = github_ops.run_command(OWNER, str(repo), "labels", "en")
    assert res["ok"] and [l["name"] for l in res["labels"]] == ["bug", "stale"]
    assert "`bug`" in res["markdown"] and "acme/widgets" in res["markdown"]
    assert gh.calls[0][:2] == ["label", "list"] and gh.calls[0][-2:] == ["--repo", "acme/widgets"]
    assert gh.writes == []


# ---------------------------------------------------------------------------
# gh unavailable: read-only paths say so clearly
# ---------------------------------------------------------------------------
def test_labels_without_gh(repo, gh):
    gh.missing = True
    res = github_ops.run_command(OWNER, str(repo), "labels")
    assert res["ok"] is False and res["error_class"] == "gh.missing" and "not installed" in res["markdown"]


def test_labels_gh_not_signed_in(repo, gh):
    gh.fail = "To get started with GitHub CLI, please run:  gh auth login"
    res = github_ops.run_command(OWNER, str(repo), "labels")
    assert res["ok"] is False and res["error_class"] == "gh.unauthenticated" and "gh auth login" in res["markdown"]


def test_labels_without_github_remote(tmp_path, gh):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "--bare"], bare)
    r = tmp_path / "clone"
    r.mkdir()
    _git(["init"], r)
    _git(["remote", "add", "origin", str(bare)], r)
    res = github_ops.run_command(OWNER, str(r), "labels")
    assert res["ok"] is False and res["error_class"] == "gh.no_remote" and "local path" in res["error"]
    assert gh.calls == []  # gh is not even asked

    r2 = tmp_path / "noremote"
    r2.mkdir()
    _git(["init"], r2)
    res = github_ops.run_command(OWNER, str(r2), "labels")
    assert res["error_class"] == "gh.no_remote" and "no 'origin'" in res["error"]


# ---------------------------------------------------------------------------
# release notes
# ---------------------------------------------------------------------------
def test_notes_grouped_by_label_from_canned_gh_json(repo, gh):
    gh.prs = PRS
    res = github_ops.run_command(OWNER, str(repo), "notes", "en")
    assert res["ok"] and res["from"] == "v1.0.0" and res["to"] == "HEAD"
    assert res["commits"] == 5  # first-parent: the branch commit inside PR 14 is not listed twice
    titles = {g: [e["title"] for e in items] for g, items in res["groups"].items()}
    assert titles["features"] == ["Add export"]
    assert titles["fixes"] == ["Handle empty body"]
    assert titles["docs"] == ["Docs pass"]
    assert titles["maintenance"] == ["Bump deps"]
    assert titles["other"] == ["Tweak readme"]
    assert res["gh"]["used"] is True and res["pull_requests"] == 3
    md = res["markdown"]
    assert "### Features" in md and "- Add export (#12, @luis)" in md and "- Handle empty body (#13, @marta)" in md
    assert "- Tweak readme (" in md and "#99" not in md
    assert "compare/v1.0.0...HEAD" in md and "Not in range" not in md
    # the group order is stable: features before fixes before docs
    assert md.index("### Features") < md.index("### Fixes") < md.index("### Documentation") < md.index("### Maintenance") < md.index("### Other")
    assert gh.writes == []


def test_notes_labels_beat_the_commit_prefix(repo, gh):
    gh.prs = [dict(PRS[0], labels=[{"name": "bug"}]), PRS[1], PRS[2]]  # "feat:" commit, but labelled bug
    res = github_ops.release_notes(str(repo))
    assert sorted(e["title"] for e in res["groups"]["fixes"]) == ["Add export", "Handle empty body"]


def test_notes_in_spanish_headings(repo, gh):
    gh.prs = PRS
    md = github_ops.release_notes(str(repo), lang="es")["markdown"]
    assert "## Notas de la versión" in md and "### Novedades" in md and "### Correcciones" in md
    assert "Colaboradores" in md


def test_notes_work_from_local_git_when_gh_is_missing(repo, gh):
    gh.missing = True
    res = github_ops.release_notes(str(repo))
    assert res["ok"] and res["gh"] == {"used": False, "reason": "gh.missing", "detail": res["gh"]["detail"]}
    titles = {g: [e["title"] for e in items] for g, items in res["groups"].items()}
    # no labels, so the conventional-commit prefix decides
    assert titles["features"] == ["Add export"] and titles["fixes"] == ["Handle empty body"]
    assert "Docs pass" in titles["other"]  # the merge commit's title comes from its body
    assert "not installed" in res["markdown"]


def test_notes_work_when_gh_is_not_signed_in(repo, gh):
    gh.fail = "gh auth login required"
    res = github_ops.release_notes(str(repo))
    assert res["ok"] and res["gh"]["used"] is False and res["gh"]["reason"] == "gh.unauthenticated"
    assert "gh auth login" in res["markdown"]


def test_notes_without_a_github_remote_never_calls_gh(repo, gh):
    _git(["remote", "remove", "origin"], repo)
    res = github_ops.release_notes(str(repo))
    assert res["ok"] and res["gh"]["reason"] == "gh.no_remote" and gh.calls == []
    assert res["commits"] == 5


def test_notes_explicit_range_and_tagged_end(repo, gh):
    gh.missing = True
    _git(["tag", "v1.1.0"], repo)
    _commit(repo, "e.txt", "5", "feat!: drop the v0 API")
    res = github_ops.release_notes(str(repo), "v1.0.0", "v1.1.0")
    assert res["commits"] == 5 and "breaking" not in res["groups"]
    res = github_ops.release_notes(str(repo), "v1.1.0", "HEAD")
    assert res["commits"] == 1 and [e["title"] for e in res["groups"]["breaking"]] == ["Drop the v0 API"]
    # `to` is the last tag: the range starts at the tag before it
    res = github_ops.release_notes(str(repo), None, "v1.1.0")
    assert res["from"] == "v1.0.0" and res["commits"] == 5


def test_notes_no_tags_covers_the_whole_history(tmp_path, gh):
    gh.missing = True
    r = tmp_path / "fresh"
    r.mkdir()
    _git(["init"], r)
    _commit(r, "a", "1", "fix: first")
    res = github_ops.release_notes(str(r))
    assert res["ok"] and res["from"] is None and res["commits"] == 1
    assert "No tags yet" in res["markdown"]
    assert "Sin etiquetas" in github_ops.release_notes(str(r), lang="es")["markdown"]


def test_notes_unknown_ref(repo, gh):
    res = github_ops.release_notes(str(repo), "v9.9.9", None)
    assert res["ok"] is False and res["error_class"] == "git.unknown_ref"
    assert github_ops.release_notes(str(repo), "--all", None)["error_class"] == "args.bad_ref"


@pytest.mark.parametrize("title,labels,group,clean", [
    ("feat: shiny", [], "features", "Shiny"),
    ("fix(ui): crash", [], "fixes", "Crash"),
    ("feat!: new api", [], "breaking", "New api"),
    ("perf: faster", [], "perf", "Faster"),
    ("Update thing", [], "other", "Update thing"),
    ("Update thing", ["type: bug"], "fixes", "Update thing"),
    ("Update thing", ["Security"], "security", "Update thing"),
    ("feat: x", ["dependencies"], "maintenance", "X"),
    ("docs: y", ["breaking-change"], "breaking", "Y"),
])
def test_classify_entry(title, labels, group, clean):
    assert github_ops.classify_entry(title, labels) == (group, clean)


# ---------------------------------------------------------------------------
# release check
# ---------------------------------------------------------------------------
def _by_id(res):
    return {i["id"]: i for i in res["items"]}


def _track_upstream(repo):
    _git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo)
    _git(["config", "branch.master.remote", "origin"], repo)
    _git(["config", "branch.master.merge", "refs/heads/master"], repo)


def test_release_check_all_green(repo, gh):
    _track_upstream(repo)
    _commit(repo, "pyproject.toml", '[project]\nname = "widgets"\nversion = "1.0.0"\n', "add pyproject")
    _git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo)
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    gh.runs = [{"name": "CI", "workflowName": "CI", "status": "completed", "conclusion": "success", "url": "u"},
               {"name": "CI", "workflowName": "CI", "status": "completed", "conclusion": "failure", "url": "old"}]  # older re-run
    res = github_ops.release_check(str(repo), "v1.0.0-rc1")
    items = _by_id(res)
    assert items["clean"]["status"] == "ok"
    assert items["pushed"]["status"] == "ok"
    assert items["tag"]["status"] == "ok" and "free" in items["tag"]["detail"]
    assert items["ci"]["status"] == "ok"
    assert items["blockers"]["status"] == "ok"
    assert items["versions"]["status"] == "fail"  # tag 1.0.0-rc1 vs files 1.0.0
    assert res["ready"] is False and res["blocking"] == ["versions"]
    assert ["run", "list", "--commit", head] == gh.calls[0][:4]
    res = github_ops.release_check(str(repo), "v1.0.0")
    assert _by_id(res)["versions"]["status"] == "ok" and _by_id(res)["tag"]["status"] == "fail"  # v1.0.0 exists elsewhere
    assert "not at HEAD" in _by_id(res)["tag"]["detail"]
    _git(["tag", "-f", "v1.0.0"], repo)  # now it points at HEAD
    res = github_ops.release_check(str(repo), "v1.0.0")
    assert res["ready"] is True and res["blocking"] == [] and _by_id(res)["tag"]["detail"] == "already points at HEAD"
    assert "**Ready to release**" in res["markdown"] and "- [x] Working tree is clean" in res["markdown"]


def test_release_check_dirty_tree_unpushed_and_version_mismatch(repo, gh):
    _track_upstream(repo)
    _git(["reset", "-q", "--hard", "HEAD~1"], repo)  # origin/master is now ahead of nothing... make local ahead instead
    _git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo)
    _commit(repo, "x.txt", "x", "local only")
    (repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    _commit(repo, "VERSION", "2.0.0\n", "add VERSION")
    (repo / "dirty.txt").write_text("still uncommitted", encoding="utf-8")
    gh.runs = [{"workflowName": "CI", "status": "completed", "conclusion": "failure"}]
    gh.issues = {"blocker": [{"number": 7, "title": "Data loss on export", "url": "u"}]}
    gh.prs_open = {"release-blocker": [{"number": 9, "title": "Revert the migration", "url": "u"}]}
    res = github_ops.release_check(str(repo))
    items = _by_id(res)
    assert items["clean"]["status"] == "fail" and "1 file" in items["clean"]["detail"]
    assert items["pushed"]["status"] == "fail" and "ahead" in items["pushed"]["detail"]
    assert items["ci"]["status"] == "fail" and "CI (failure)" in items["ci"]["detail"]
    assert items["blockers"]["status"] == "fail"
    assert "issue #7 Data loss on export" in items["blockers"]["detail"] and "PR #9 Revert the migration" in items["blockers"]["detail"]
    assert items["versions"]["status"] == "fail" and "package.json=1.0.0" in items["versions"]["detail"] and "VERSION=2.0.0" in items["versions"]["detail"]
    assert res["ready"] is False and set(res["blocking"]) == {"clean", "pushed", "ci", "blockers", "versions"}
    assert "- [ ] Working tree is clean **FAIL**" in res["markdown"] and "Not ready" in res["markdown"]


def test_release_check_ci_pending_and_no_runs_are_warnings(repo, gh):
    _track_upstream(repo)
    gh.runs = [{"workflowName": "CI", "status": "in_progress", "conclusion": ""}]
    assert _by_id(github_ops.release_check(str(repo)))["ci"]["status"] == "warn"
    gh.runs = []
    item = _by_id(github_ops.release_check(str(repo)))["ci"]
    assert item["status"] == "warn" and "pushed" in item["detail"]


def test_release_check_custom_blocker_labels(repo, gh):
    gh.issues = {"p0": [{"number": 1, "title": "x", "url": ""}]}
    res = github_ops.release_check(str(repo), blocker_labels=["p0", "-evil"])
    assert _by_id(res)["blockers"]["status"] == "fail"
    labels_asked = [c[c.index("--label") + 1] for c in gh.calls if "--label" in c]
    assert set(labels_asked) == {"p0"}  # an option-looking label is dropped, never passed to gh


def test_release_check_local_items_work_without_gh(repo, gh):
    gh.missing = True
    res = github_ops.release_check(str(repo), "v1.1.0")
    items = _by_id(res)
    assert items["ci"]["status"] == "skip" and "not installed" in items["ci"]["detail"]
    assert items["blockers"]["status"] == "skip"
    assert items["clean"]["status"] == "ok" and items["tag"]["status"] == "ok"
    assert res["counts"]["skip"] == 2
    assert res["ready"] is False  # versions: package.json says 1.0.0, the tag 1.1.0


def test_release_check_without_a_github_remote(repo, gh):
    _git(["remote", "remove", "origin"], repo)
    res = github_ops.release_check(str(repo))
    assert _by_id(res)["ci"]["status"] == "skip" and "no 'origin'" in _by_id(res)["ci"]["detail"]
    assert gh.calls == []


def test_release_check_bad_tag(repo, gh):
    assert github_ops.release_check(str(repo), "--delete")["error_class"] == "args.bad_ref"


def test_find_versions(tmp_path):
    (tmp_path / "package.json").write_text('{"version": "3.1.0"}')
    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "package.json").write_text('{"name": "s"}')
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "3.1.0"\n[dependencies]\nversion = "9"\n')
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nversion = "3.1.0"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text('__version__ = "3.1.0"\n')
    found = {v["file"]: v["version"] for v in github_ops.find_versions(str(tmp_path))}
    assert found == {"package.json": "3.1.0", "pyproject.toml": "3.1.0", "Cargo.toml": "3.1.0", "src/__init__.py": "3.1.0"}


# ---------------------------------------------------------------------------
# label sync: policy AND approval
# ---------------------------------------------------------------------------
def test_sync_plan_is_read_only_and_policy_denied_by_default(repo, gh):
    _write_label_file(repo)
    res = github_ops.prepare_label_sync(OWNER, str(repo), ".github/labels.yml")
    assert res["ok"] and res["policy_allowed"] is False and res["approval_id"] is None
    assert res["requires_approval"] is True and res["git_policy_field"] == "push"
    assert "Settings > Git policy" in res["markdown"]
    assert [c["name"] for c in res["plan"]["create"]] == ["blocker"]
    assert gh.writes == [] and github_ops._PENDING == {}


def test_sync_plan_with_policy_allowed_arms_one_single_use_approval(repo, gh):
    _write_label_file(repo)
    agent_git_policy.set_global_policy({"push": True})
    res = github_ops.prepare_label_sync(OWNER, str(repo), ".github/labels.yml")
    assert res["policy_allowed"] is True and res["approval_id"]
    assert gh.writes == []  # asking for a plan changes nothing on GitHub
    assert "explicit approval" in res["markdown"]

    wrong = github_ops.apply_label_sync("mallory", res["approval_id"])
    assert wrong["ok"] is False and wrong["error_class"] == "approval.invalid"
    assert gh.writes == []  # the wrong owner burned nothing and wrote nothing... and the id was consumed:
    again = github_ops.apply_label_sync(OWNER, res["approval_id"])
    assert again["error_class"] == "approval.invalid"


def test_sync_apply_runs_create_and_edit_only_after_approval(repo, gh):
    _write_label_file(repo)
    agent_git_policy.set_global_policy({"push": True})
    res = github_ops.prepare_label_sync(OWNER, str(repo), ".github/labels.yml")
    done = github_ops.apply_label_sync(OWNER, res["approval_id"], "es")
    assert done["ok"] and done["applied"] and [r["op"] for r in done["results"]] == ["create", "update"]
    create = next(c for c in gh.writes if c[:2] == ["label", "create"])
    assert create[:5] == ["label", "create", "blocker", "--color", "b60205"] and "--description" in create
    edit = next(c for c in gh.writes if c[:2] == ["label", "edit"])
    assert edit[:3] == ["label", "edit", "stale"] and "--name" in edit and edit[edit.index("--name") + 1] == "Stale"
    assert all(c[-2:] == ["--repo", "acme/widgets"] for c in gh.writes)
    assert not any(c[:2] == ["label", "delete"] for c in gh.calls)
    assert "Aplicado" in done["markdown"]
    # the approval is spent
    assert github_ops.apply_label_sync(OWNER, res["approval_id"])["error_class"] == "approval.invalid"


def test_sync_apply_refused_when_the_policy_was_withdrawn(repo, gh):
    _write_label_file(repo)
    agent_git_policy.set_global_policy({"push": True})
    res = github_ops.prepare_label_sync(OWNER, str(repo), ".github/labels.yml")
    agent_git_policy.set_global_policy({"push": False})
    done = github_ops.apply_label_sync(OWNER, res["approval_id"])
    assert done["ok"] is False and done["error_class"] == "git.policy_denied" and done["git_policy_field"] == "push"
    assert gh.writes == []


def test_sync_apply_refused_when_the_plan_went_stale(repo, gh):
    _write_label_file(repo)
    agent_git_policy.set_global_policy({"push": True})
    res = github_ops.prepare_label_sync(OWNER, str(repo), ".github/labels.yml")
    gh.labels.append({"name": "blocker", "color": "ffffff", "description": ""})  # someone else created it meanwhile
    done = github_ops.apply_label_sync(OWNER, res["approval_id"])
    assert done["ok"] is False and done["error_class"] == "approval.stale"
    assert gh.writes == []


def test_sync_approval_expires(repo, gh, monkeypatch):
    _write_label_file(repo)
    agent_git_policy.set_global_policy({"push": True})
    res = github_ops.prepare_label_sync(OWNER, str(repo), ".github/labels.yml")
    github_ops._PENDING[res["approval_id"]]["expires"] = 0
    assert github_ops.apply_label_sync(OWNER, res["approval_id"])["error_class"] == "approval.invalid"
    assert gh.writes == []


def test_sync_nothing_to_do_needs_no_approval(repo, gh):
    agent_git_policy.set_global_policy({"push": True})
    _write_label_file(repo, "- name: bug\n  color: d73a4a\n")
    res = github_ops.prepare_label_sync(OWNER, str(repo), ".github/labels.yml")
    assert res["plan"]["changes"] == 0 and res["approval_id"] is None and res["requires_approval"] is False
    assert "Nothing to do" in res["markdown"]


def test_sync_via_the_command_line(repo, gh):
    _write_label_file(repo)
    res = github_ops.run_command(OWNER, str(repo), "labels sync .github/labels.yml", "es")
    assert res["ok"] and res["kind"] == "labels_sync" and res["approval_id"] is None
    assert "Plan de sincronización" in res["markdown"] and "no permite escrituras remotas" in res["markdown"]
    missing = github_ops.run_command(OWNER, str(repo), "labels sync nope.yml")
    assert missing["ok"] is False and missing["error_class"] == "labels.no_file"


# ---------------------------------------------------------------------------
# agent tools
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


def _tool(cls):
    return cls().execute


def test_agent_tools_are_wired_everywhere():
    from src import tool_capabilities, tool_index, tool_index_examples, tool_schemas, tool_security
    from src.agent_tools import TOOL_HANDLERS
    names = {"github_labels", "github_labels_sync", "github_notes", "github_release_check"}
    assert names <= set(TOOL_HANDLERS)
    assert names <= te._GIT_TOOL_NAMES
    assert names <= {s["function"]["name"] for s in tool_schemas.TOOL_SCHEMAS} if hasattr(tool_schemas, "TOOL_SCHEMAS") else True
    assert names <= set(tool_security.NON_ADMIN_BLOCKED_TOOLS)
    assert {"github_labels", "github_notes", "github_release_check"} <= set(tool_security.READ_ONLY_TOOLS) if hasattr(tool_security, "READ_ONLY_TOOLS") else True
    assert names <= set(tool_index.TOOL_DESCRIPTIONS) if hasattr(tool_index, "TOOL_DESCRIPTIONS") else True
    assert names <= set(tool_index_examples.TOOL_EXAMPLES) if hasattr(tool_index_examples, "TOOL_EXAMPLES") else True


def test_agent_labels_lists_and_plans_without_ever_arming_an_approval(ws, gh):
    from src.agent_tools.github_ops_tools import GithubLabelsTool
    _write_label_file(ws)
    agent_git_policy.set_global_policy({"push": True})
    listed = _run(_tool(GithubLabelsTool)("{}", {"owner": OWNER}))
    assert listed["exit_code"] == 0 and [l["name"] for l in listed["labels"]] == ["bug", "stale"]
    plan = _run(_tool(GithubLabelsTool)(json.dumps({"file": ".github/labels.yml"}), {"owner": OWNER}))
    assert plan["exit_code"] == 0 and plan["plan"]["changes"] == 2 and "approval_id" not in plan
    assert github_ops._PENDING == {} and gh.writes == []


def test_agent_labels_sync_refused_without_policy_then_without_approval_then_runs(ws, gh):
    from src.agent_tools.github_ops_tools import GithubLabelsSyncTool
    _write_label_file(ws)
    call = json.dumps({"file": ".github/labels.yml"})
    # 1. policy off: refused, even when a human approved this exact call
    denied = _run(_tool(GithubLabelsSyncTool)(json.dumps({"file": ".github/labels.yml", "user_confirmed": True}), {"owner": OWNER}))
    assert denied["exit_code"] == 1 and denied["error_class"] == "git.policy_denied" and denied["git_policy_field"] == "push"
    assert gh.writes == []
    # 2. policy on, no approval: refused with the plan to show
    agent_git_policy.set_global_policy({"push": True})
    ask = _run(_tool(GithubLabelsSyncTool)(call, {"owner": OWNER}))
    assert ask["exit_code"] == 1 and ask["error_class"] == "approval.required" and "blocker" in ask["error"]
    assert gh.writes == []
    # 3. policy on + the human's yes (a sealed card, or user_confirmed): runs
    ok = _run(_tool(GithubLabelsSyncTool)(call, {"owner": OWNER, "human_approved": True}))
    assert ok["exit_code"] == 0 and ok["applied"] is True and len(gh.writes) == 2
    gh.calls.clear()
    gh.labels = gh.labels + [{"name": "blocker", "color": "b60205", "description": "Stops a release"}]
    gh.labels[1] = {"name": "Stale", "color": "eeeeee", "description": ""}
    again = _run(_tool(GithubLabelsSyncTool)(json.dumps({"file": ".github/labels.yml", "user_confirmed": True}), {"owner": OWNER}))
    assert again["exit_code"] == 0 and again["applied"] is False and gh.writes == []  # already in sync


def test_agent_labels_sync_arguments_and_gh_errors(ws, gh):
    from src.agent_tools.github_ops_tools import GithubLabelsSyncTool
    assert _run(_tool(GithubLabelsSyncTool)("{}", {"owner": OWNER}))["error_class"] == "args.missing"
    _write_label_file(ws)
    gh.fail = "gh auth login"
    res = _run(_tool(GithubLabelsSyncTool)(json.dumps({"file": ".github/labels.yml", "user_confirmed": True}), {"owner": OWNER}))
    assert res["exit_code"] == 1 and res["error_class"] == "gh.unauthenticated"


def test_agent_notes_and_release_check(ws, gh):
    from src.agent_tools.github_ops_tools import GithubNotesTool, GithubReleaseCheckTool
    gh.prs = PRS
    notes = _run(_tool(GithubNotesTool)("{}", {"owner": OWNER}))
    assert notes["exit_code"] == 0 and notes["pull_requests"] == 3 and "### Features" in notes["output"]
    gh.missing = True
    notes = _run(_tool(GithubNotesTool)(json.dumps({"from": "v1.0.0", "to": "HEAD"}), {"owner": OWNER}))
    assert notes["gh"]["used"] is False
    check = _run(_tool(GithubReleaseCheckTool)(json.dumps({"tag": "v1.0.0", "blocker_labels": "p0, p1"}), {"owner": OWNER}))
    assert check["exit_code"] == 0 and {i["id"] for i in check["items"]} == {"clean", "pushed", "tag", "ci", "blockers", "versions"}
    bad = _run(_tool(GithubNotesTool)(json.dumps({"from": "--oops"}), {"owner": OWNER}))
    assert bad["exit_code"] == 1 and bad["error_class"] == "args.bad_ref"


def _dispatch(name, content, ws, **kw):
    from collections import namedtuple
    ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])
    return _run(te.execute_tool_block(ToolBlock(name, content), session_id="sess-gh", owner=OWNER,
                                      workspace=str(ws), **kw))


def test_agent_notes_through_the_dispatcher(ws, gh):
    """The new names ride the git branch of execute_tool_block (owner +
    human_approved), not the generic fallback."""
    from src.tool_capabilities import ToolRunSecurityContext
    gh.prs = PRS
    desc, result = _dispatch("github_notes", "{}", ws, security_context=ToolRunSecurityContext())
    assert result["exit_code"] == 0 and result["pull_requests"] == 3 and "### Features" in result["output"]


def test_agent_label_sync_rides_a_sealed_approval_card_through_the_dispatcher(ws, gh):
    """Policy allows push; then the ONLY thing that unlocks the write is the
    sealed, human-answered card for this exact call -- the same mechanism
    git_commit/git_push use."""
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
    _write_label_file(ws)
    agent_git_policy.set_global_policy({"push": True})
    content = json.dumps({"file": ".github/labels.yml"})

    # no card -> refused, with the plan, and nothing written
    desc, result = _dispatch("github_labels_sync", content, ws, security_context=ToolRunSecurityContext())
    assert result["exit_code"] == 1 and result["error_class"] == "approval.required" and gh.writes == []

    store = ToolApprovalStore()
    pending = store.create(owner=OWNER, session_id="sess-gh", origin_run_id="run-1", tool_name="github_labels_sync",
                           content=content, workspace=str(ws), external_untrusted_context_seen=True,
                           capabilities=capabilities_for_action("github_labels_sync", content))
    grant = store.consume(pending.approval_id, decision="approve_task", owner=OWNER, session_id="sess-gh")
    assert grant is not None
    desc, result = _dispatch("github_labels_sync", content, ws,
                             security_context=ToolRunSecurityContext(external_untrusted_context_seen=True),
                             exact_approval=grant)
    assert result["exit_code"] == 0 and result["applied"] is True and len(gh.writes) == 2


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import github_routes
    from src.auth_helpers import effective_user, require_user

    app = FastAPI()
    app.include_router(github_routes.setup_github_routes())
    app.dependency_overrides[require_user] = lambda: OWNER
    app.dependency_overrides[require_admin] = lambda: None
    monkeypatch.setattr(github_routes, "effective_user", lambda request: OWNER)
    return TestClient(app)


def test_route_command_notes_and_errors(client, repo, gh):
    gh.prs = PRS
    r = client.post("/api/github/command", json={"workspace": str(repo), "args": "notes", "lang": "es"})
    assert r.status_code == 200 and "### Novedades" in r.json()["markdown"]
    sub = repo / "sub"
    sub.mkdir()
    assert client.post("/api/github/command", json={"workspace": str(sub), "args": "notes"}).status_code == 200  # inside the repo
    r = client.post("/api/github/command", json={"workspace": str(repo.parent), "args": "notes"})
    assert r.status_code == 400 and r.json()["error_class"] == "git.not_a_repo"
    r = client.post("/api/github/command", json={"workspace": "relative/path", "args": "notes"})
    assert r.status_code == 400
    r = client.post("/api/github/command", json={"workspace": str(repo), "args": "labels nuke"})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_route_sync_needs_policy_then_approval_then_applies(client, repo, gh):
    _write_label_file(repo)
    body = {"workspace": str(repo), "args": "labels sync .github/labels.yml"}
    plan = client.post("/api/github/command", json=body).json()
    assert plan["approval_id"] is None and plan["policy_allowed"] is False
    assert client.post("/api/github/labels/sync/apply", json={"approval_id": "made-up"}).json()["error_class"] == "approval.invalid"
    assert gh.writes == []

    agent_git_policy.set_global_policy({"push": True})
    plan = client.post("/api/github/command", json=body).json()
    assert plan["approval_id"] and gh.writes == []
    done = client.post("/api/github/labels/sync/apply", json={"approval_id": plan["approval_id"], "lang": "en"}).json()
    assert done["ok"] and done["applied"] and len(gh.writes) == 2
    assert "created" in done["markdown"]


def test_route_status(client, repo, monkeypatch):
    from src import git_github
    monkeypatch.setattr(git_github, "gh_accounts", lambda **kw: {"available": True, "version": "2.0.0", "accounts": [{"login": "luis"}]})
    r = client.get("/api/github/status", params={"workspace": str(repo)}).json()
    assert r["gh_available"] is True and r["accounts"] == ["luis"]
    assert r["repo"]["slug"] == "acme/widgets" and r["repo"]["github"] is True


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
def test_mcp_server_lists_three_read_only_tools_and_is_registered():
    pytest.importorskip("mcp")
    import mcp_servers.github_server as gs
    from src import builtin_mcp
    tools = asyncio.run(gs.list_tools())
    assert {t.name for t in tools} == {"github_labels", "github_notes", "github_release_check"}
    assert all(t.inputSchema["type"] == "object" for t in tools)
    assert not any("sync" in t.name for t in tools)  # no remote write over MCP
    assert builtin_mcp._BUILTIN_SERVERS["github"][0] == "mcp_servers/github_server.py"
    assert "github" in builtin_mcp.NATIVE_TWIN_SERVERS


def test_mcp_server_calls(ws, gh):
    pytest.importorskip("mcp")
    import mcp_servers.github_server as gs
    gh.prs = PRS

    def call(name, args):
        out = asyncio.run(gs.call_tool(name, args))
        return out[0].text

    notes = json.loads(call("github_notes", {"lang": "es"}))
    assert notes["pull_requests"] == 3 and "### Novedades" in notes["markdown"]
    check = json.loads(call("github_release_check", {"tag": "v9.9.9"}))
    assert check["ready"] is False and "versions" in check["blocking"]
    labels = json.loads(call("github_labels", {}))
    assert [l["name"] for l in labels["labels"]] == ["bug", "stale"]

    _write_label_file(ws)
    agent_git_policy.set_global_policy({"push": True})
    plan = json.loads(call("github_labels", {"file": ".github/labels.yml"}))
    assert plan["plan"]["changes"] == 2
    assert github_ops._PENDING == {} and gh.writes == []  # an MCP call never arms an approval nor writes

    assert call("nope", {}) == "Unknown tool: nope"
    assert "outside" in call("github_notes", {"root": os.path.dirname(str(ws))}).lower() or "no git repository" in call("github_notes", {"root": os.path.dirname(str(ws))})


# ---------------------------------------------------------------------------
# Spanish (the Studio sends lang=es)
# ---------------------------------------------------------------------------
def test_release_check_details_and_gh_errors_in_spanish(repo, gh):
    gh.missing = True
    res = github_ops.run_command(OWNER, str(repo), "release-check v0.9.0", "es")
    items = {i["id"]: i for i in res["items"]}
    assert "libre: la etiqueta aún no existe" in items["tag"]["detail"]
    assert "no tiene rama remota asociada" in items["pushed"]["detail"]
    assert "los archivos dicen 1.0.0 pero la etiqueta es v0.9.0" in items["versions"]["detail"]
    assert "Comprobación de versión" in res["markdown"] and "omitidos" in res["markdown"]
    labels = github_ops.run_command(OWNER, str(repo), "labels", "es")
    assert labels["ok"] is False and labels["error"].startswith("El CLI de GitHub (gh) no está instalado")
    assert labels["markdown"].startswith("**gh.missing**")
    # the English text is untouched when the UI is English
    assert github_ops.run_command(OWNER, str(repo), "labels", "en")["error"].startswith("The GitHub CLI (gh) is not installed")


def test_local_remote_and_bad_refs_in_spanish(tmp_path, repo, gh):
    bare = tmp_path / "b.git"
    bare.mkdir()
    _git(["init", "--bare"], bare)
    _git(["remote", "set-url", "origin", str(bare)], repo)
    res = github_ops.run_command(OWNER, str(repo), "labels", "es")
    assert res["error_class"] == "gh.no_remote" and "es una ruta local" in res["error"] and str(bare) in res["error"]
    notes = github_ops.release_notes(str(repo), "v9.9.9", None, "es")
    assert notes["error_class"] == "git.unknown_ref" and "no es un commit ni una etiqueta" in notes["error"]
    forged = github_ops.apply_label_sync(OWNER, "nope", "es")
    assert forged["error_class"] == "approval.invalid" and "no existe, ha caducado" in forged["error"]
