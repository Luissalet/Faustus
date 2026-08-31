"""Hardening of /api/workspace: cross-origin writes, editor launch, git env,
and the missing bounds.

The threat here is not a rogue user — the app is single-user on 127.0.0.1 with
AUTH_ENABLED=false. It is (a) the model reaching these routes through the
loopback `app_api` tool, and (b) any web page the user happens to have open
firing a no-preflight POST at localhost. Neither can be stopped by the auth
gate, because there effectively isn't one.
"""

import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.workspace_routes as wr


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    return TestClient(app)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    try:
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
                       cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "init"], cwd=ws, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    (ws / "src" / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    return ws


# ── (2) cross-origin writes ────────────────────────────────────────────────
#
# Every mutating POST of this router. Sent with a body/params that would be
# rejected later anyway — the point is that the 403 comes FIRST, before the
# handler does anything.
MUTATING_POSTS = [
    ("/api/workspace/reveal", {"workspace": "", "path": "x"}, None),
    ("/api/workspace/open_editor", {"workspace": "", "path": "x"}, None),
    ("/api/workspace/revert", {"workspace": "", "path": "x"}, None),
    ("/api/workspace/instructions/remember", {}, {"workspace": "", "text": "r"}),
    ("/api/workspace/instructions/draft", {}, {"workspace": ""}),
    ("/api/workspace/checkpoint/restore", {"workspace": "", "sha": "abcdef1"}, {"paths": ["a"]}),
    ("/api/workspace/checkpoint/reset", {"workspace": ""}, None),
    ("/api/workspace/commit", {"workspace": ""}, {"paths": ["a"], "message": "m"}),
    ("/api/workspace/review/m1/decide", {}, {"path": "a", "decision": "reject"}),
]


@pytest.mark.parametrize("url,params,body", MUTATING_POSTS)
def test_cross_site_post_is_refused(client, url, params, body):
    """The attack: a page on evil.example fires
    fetch(url, {method:'POST', mode:'no-cors', headers:{'Content-Type':'text/plain'}}).
    The browser stamps Sec-Fetch-Site: cross-site and cannot be made to lie."""
    r = client.post(url, params=params, json=body,
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403, r.text
    assert "cross-origin" in r.json()["detail"].lower()


@pytest.mark.parametrize("url,params,body", MUTATING_POSTS)
def test_same_site_post_is_refused(client, url, params, body):
    """`same-site` is a *different* origin (other port / subdomain). For an app
    that lives at one origin on localhost, that is still Not Us."""
    r = client.post(url, params=params, json=body,
                    headers={"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("url,params,body", MUTATING_POSTS)
def test_same_origin_post_passes_the_guard(client, url, params, body):
    """The app's own page must sail through: whatever the answer is, it must
    not be the origin 403."""
    r = client.post(url, params=params, json=body,
                    headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code != 403, r.text


@pytest.mark.parametrize("url,params,body", MUTATING_POSTS)
def test_headerless_post_passes_the_guard(client, url, params, body):
    """curl and the backend's own loopback send neither header. A web page
    cannot make the browser omit Sec-Fetch-Site, so absence is not a bypass —
    and breaking CLI use would be a real cost for no security gain."""
    r = client.post(url, params=params, json=body)
    assert r.status_code != 403, r.text


@pytest.mark.parametrize("url,params,body", MUTATING_POSTS)
def test_foreign_origin_header_is_refused(client, url, params, body):
    r = client.post(url, params=params, json=body,
                    headers={"Origin": "http://evil.example", "Host": "127.0.0.1:7000"})
    assert r.status_code == 403, r.text
    assert "origin" in r.json()["detail"].lower()


def test_matching_origin_header_passes(client, repo):
    r = client.post("/api/workspace/revert",
                    params={"workspace": str(repo), "path": "src/a.py"},
                    headers={"Origin": "http://127.0.0.1:7000", "Host": "127.0.0.1:7000",
                             "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_a_cross_site_revert_does_not_touch_the_file(client, repo):
    """The guard must run before the work, not alongside it."""
    r = client.post("/api/workspace/revert",
                    params={"workspace": str(repo), "path": "src/a.py"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"


def test_null_origin_is_refused(client):
    """A sandboxed iframe / file:// page sends `Origin: null`."""
    r = client.post("/api/workspace/checkpoint/reset", params={"workspace": ""},
                    headers={"Origin": "null", "Host": "127.0.0.1:7000"})
    assert r.status_code == 403


def test_the_read_routes_stay_open_to_cross_site(client, repo):
    """Only writes are gated. A GET cannot change anything, and gating reads
    would break scripts for nothing."""
    r = client.get("/api/workspace/file", params={"workspace": str(repo), "path": "src/a.py"},
                   headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


# ── (3) command injection in /open_editor ──────────────────────────────────

def _cmd_unquoted_runs(command_line: str):
    """The parts of a `cmd.exe … /s /c "…"` line that cmd parses as SYNTAX.

    Models what cmd actually does: `/s` strips exactly the first and last quote
    after `/c` and takes the rest verbatim; in that remainder, text inside a
    double-quoted run is literal and everything else is cmd syntax. Anything a
    filename contributes must land inside a quoted run.
    """
    assert " /c " in command_line, command_line
    rest = command_line.split(" /c ", 1)[1]
    assert rest.startswith('"') and rest.endswith('"'), rest
    return rest[1:-1].split('"')[::2]


def test_editor_argv_never_lets_an_ampersand_separate_commands():
    """A file called `x&calc.exe` is a legal NTFS name the agent itself can
    create. `shell=True` + list2cmdline (which only quotes on whitespace) made
    it `code -g C:\\ws\\x&calc.exe:1` → cmd.exe ran calc.exe."""
    target = r"C:\ws\x&calc.exe"

    # Real executable: an argv, so nothing parses the filename at all.
    argv = wr._editor_launch_args(r"C:\vsc\Code.exe", target, 3, windows=True)
    assert isinstance(argv, list)
    assert argv == [r"C:\vsc\Code.exe", "-g", r"C:\ws\x&calc.exe:3"]
    # And the argv survives Windows' own quoting rules unchanged.
    assert subprocess.list2cmdline(argv[1:]).count("&") == 1

    # .cmd wrapper: a pre-quoted command line, and the `&` sits INSIDE quotes,
    # where cmd.exe reads it as text.
    line = wr._editor_launch_args(r"C:\vsc\bin\code.cmd", target, 3, windows=True)
    assert isinstance(line, str)
    assert r'"C:\ws\x&calc.exe:3"' in line, line
    assert " /c " in line and " /s " in line
    for outside in _cmd_unquoted_runs(line):
        assert not any(ch in outside for ch in "&|^<>"), (outside, line)


def test_list2cmdline_is_why_shell_true_was_the_bug():
    """Documents the mechanism, so nobody reintroduces it thinking Popen quotes
    for them: with shell=True on Windows, Popen joins the list with
    list2cmdline and hands the STRING to cmd.exe — and list2cmdline quotes an
    argument only when it contains whitespace."""
    old_argv = [r"C:\vsc\bin\code.cmd", "-g", r"C:\ws\x&calc.exe:1"]
    joined = subprocess.list2cmdline(old_argv)
    assert "&" in joined and '"' not in joined      # nothing was quoted at all
    assert joined.split("&", 1)[1].startswith("calc.exe")   # cmd would run this

    # The replacement puts exactly that text inside a quoted run.
    line = wr._editor_launch_args(old_argv[0], r"C:\ws\x&calc.exe", 1, windows=True)
    assert all("&" not in run for run in _cmd_unquoted_runs(line)), line


def test_open_editor_on_a_windows_host_emits_a_quoted_cmd_line(client, repo, monkeypatch):
    """The Windows path of the route, exercised on any host: a .cmd launcher
    plus a filename with `&` must produce a pre-quoted command line and still
    no shell."""
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr("shutil.which",
                        lambda name: r"C:\vsc\bin\code.cmd" if name == "code.cmd" else None)
    monkeypatch.setattr(os, "name", "nt")
    (repo / "src" / "x&touch.py").write_text("pwn\n", encoding="utf-8")

    r = client.post("/api/workspace/open_editor",
                    params={"workspace": str(repo), "path": "src/x&touch.py", "line": 2})
    assert r.status_code == 200, r.text
    args, kwargs = calls[0]
    assert not kwargs.get("shell")
    assert isinstance(args, str), args
    assert all("&" not in run for run in _cmd_unquoted_runs(args)), args


def test_editor_cmd_wrapper_is_a_single_outer_quoted_command():
    """`cmd /s /c "…"` strips exactly the first and last quote and runs the
    rest verbatim; each token keeps its own quotes."""
    line = wr._editor_launch_args(r"C:\p\code.cmd", r"C:\ws\a b&c.py", 7, windows=True)
    after_c = line.split(" /c ", 1)[1]
    assert after_c.startswith('"') and after_c.endswith('"')
    inner = after_c[1:-1]
    assert inner == r'"C:\p\code.cmd" "-g" "C:\ws\a b&c.py:7"'


def test_editor_refuses_to_build_a_cmd_line_it_cannot_quote():
    """A quote in the path would break out of the quoting. It is illegal in a
    Windows filename, so it can only mean something is wrong: refuse, and let
    the caller fall back to the OS default handler."""
    assert wr._editor_launch_args(r"C:\p\code.cmd", 'C:\\ws\\a"b.py', 1, windows=True) is None


def test_editor_on_posix_is_a_plain_argv():
    argv = wr._editor_launch_args("/usr/bin/code", "/home/u/x&y.py", 0, windows=False)
    assert argv == ["/usr/bin/code", "-g", "/home/u/x&y.py:1"]   # line 0 → 1


def test_open_editor_never_passes_shell_true(client, repo, monkeypatch):
    """The regression guard: whatever the launcher ends up being, Popen must be
    called with shell falsy."""
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/code" if name == "code" else None)
    evil = repo / "src" / "x&touch.py"
    evil.write_text("pwn\n", encoding="utf-8")

    r = client.post("/api/workspace/open_editor",
                    params={"workspace": str(repo), "path": "src/x&touch.py", "line": 4})
    assert r.status_code == 200, r.text
    args, kwargs = calls[0]
    assert not kwargs.get("shell")
    assert args == ["/usr/bin/code", "-g", f"{evil}:4"]


def test_reveal_does_not_use_a_shell(client, repo, monkeypatch):
    """/reveal was never `shell=`-ed: explorer/open/xdg-open are real binaries,
    so the filename is an argv element and never syntax. Pinned so nobody
    'fixes' it the way open_editor was."""
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    evil = repo / "src" / "x&touch.py"
    evil.write_text("pwn\n", encoding="utf-8")

    r = client.post("/api/workspace/reveal",
                    params={"workspace": str(repo), "path": "src/x&touch.py"})
    assert r.status_code == 200, r.text
    args, kwargs = calls[0]
    assert not kwargs.get("shell")
    assert str(evil) in args or str(evil.parent) in args


# ── (5) git in a folder the client chose ───────────────────────────────────

def test_file_diff_does_not_run_the_repos_external_diff(client, tmp_path):
    """`[diff] external = <cmd>` in .git/config makes `git diff` run that
    command — silently, replacing the real output. The workspace is a folder
    the client names, so its config is attacker data. --no-ext-diff kills it."""
    ws = tmp_path / "evil"
    ws.mkdir()
    marker = tmp_path / "pwned.txt"
    try:
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    (ws / "f.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
                   cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i"],
                   cwd=ws, check=True, capture_output=True)
    (ws / "f.txt").write_text("a\nb\n", encoding="utf-8")
    subprocess.run(["git", "config", "diff.external",
                    f"sh -c 'echo PWNED > {marker}'"], cwd=ws, check=True, capture_output=True)

    r = client.get("/api/workspace/file_diff", params={"workspace": str(ws), "path": "f.txt"})
    assert r.status_code == 200, r.text
    assert not marker.exists(), "the repository's external diff command was executed"
    # And the real diff is still produced, rather than the empty output the
    # external driver would have left behind.
    assert "+b" in r.json()["diff"]


def test_file_diff_runs_git_with_the_hardened_argv_and_env(client, repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        seen.append((list(args), kwargs.get("env")))
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    r = client.get("/api/workspace/file_diff", params={"workspace": str(repo), "path": "src/a.py"})
    assert r.status_code == 200, r.text

    assert seen, "no git command ran"
    for argv, env in seen:
        assert argv[0] == "git"
        assert "-c" in argv and "core.fsmonitor=" in argv, argv
        assert env is not None, f"{argv} inherited the raw environment"
        # An inherited GIT_DIR must not be able to redirect the command.
        assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env and "GIT_INDEX_FILE" not in env
        if "diff" in argv:
            assert "--no-ext-diff" in argv and "--no-color" in argv, argv


def test_git_env_helper_strips_the_redirect_variables(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/else")
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/idx")
    env = wr._git_env()
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env and "GIT_INDEX_FILE" not in env


# ── (8a) the browse cap has to bite during the scan ────────────────────────

def test_browse_stops_scanning_at_the_cap(client, tmp_path, monkeypatch):
    """The cap used to be a slice applied AFTER stat()-ing every entry, so a
    huge folder cost a stat() per file before anything was returned."""
    monkeypatch.setattr(wr, "_MAX_BROWSE_DIRS", 5)
    big = tmp_path / "big"
    big.mkdir()
    for i in range(40):
        (big / f"d{i:02d}").mkdir()
        (big / f"f{i:02d}.txt").write_text("x", encoding="utf-8")

    stats = []
    real_scandir = os.scandir

    class CountingEntry:
        """DirEntry proxy that records every stat() the route asks for."""

        def __init__(self, entry):
            self._e = entry
            self.name = entry.name

        def is_dir(self, **kw):
            return self._e.is_dir(**kw)

        def is_file(self, **kw):
            return self._e.is_file(**kw)

        def stat(self, **kw):
            stats.append(self._e.name)
            return self._e.stat(**kw)

    class CountingScandir:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()
            return False

        def __iter__(self):
            for entry in self._it:
                yield CountingEntry(entry)

    monkeypatch.setattr(os, "scandir", CountingScandir)

    r = client.get("/api/workspace/browse", params={"path": str(big), "include_files": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["dirs"]) == 5 and len(d["files"]) == 5
    assert d["truncated"] is True
    # The whole point: we did not stat() all 40 files to return 5 of them.
    assert len(stats) <= 10, f"stat() called {len(stats)} times for a cap of 5"


def test_browse_marks_truncated_for_files_too(client, tmp_path, monkeypatch):
    """`truncated` used to be computed from the dir list alone, so a folder
    with 3 subdirs and 100 000 files reported a complete listing."""
    monkeypatch.setattr(wr, "_MAX_BROWSE_DIRS", 3)
    d = tmp_path / "filesonly"
    d.mkdir()
    for i in range(12):
        (d / f"f{i:02d}.txt").write_text("x", encoding="utf-8")

    r = client.get("/api/workspace/browse", params={"path": str(d), "include_files": True})
    body = r.json()
    assert len(body["files"]) == 3 and body["dirs"] == []
    assert body["truncated"] is True


def test_browse_of_a_small_folder_is_not_truncated(client, tmp_path):
    d = tmp_path / "small"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("x", encoding="utf-8")
    body = client.get("/api/workspace/browse",
                      params={"path": str(d), "include_files": True}).json()
    assert body["truncated"] is False
    assert [x["name"] for x in body["dirs"]] == ["sub"]
    assert [x["name"] for x in body["files"]] == ["a.txt"]


# ── (8b) the `paths` body list ─────────────────────────────────────────────

def test_checkpoint_restore_rejects_an_oversized_paths_list(client, repo):
    paths = [f"src/f{i}.py" for i in range(wr._MAX_BODY_PATHS + 1)]
    r = client.post("/api/workspace/checkpoint/restore",
                    params={"workspace": str(repo), "sha": "abcdef1"}, json={"paths": paths})
    assert r.status_code == 400
    assert "too many paths" in r.json()["detail"]


def test_commit_rejects_an_oversized_paths_list(client, repo):
    paths = [f"src/f{i}.py" for i in range(wr._MAX_BODY_PATHS + 1)]
    r = client.post("/api/workspace/commit", params={"workspace": str(repo)},
                    json={"paths": paths, "message": "m"})
    assert r.status_code == 400
    assert "too many paths" in r.json()["detail"]


def test_restore_receives_the_confined_paths_not_the_raw_ones(client, repo, monkeypatch):
    """The old code validated one list and passed another: every path went
    through _confine() and then the RAW strings were handed to wc.restore(),
    so the confinement decided nothing."""
    from src import workspace_checkpoints as wc
    got = {}

    def fake_restore(root, sha, paths=None):
        got["paths"] = list(paths or [])
        return {"restored": [], "deleted": [], "failed": [], "unchanged": 0}

    monkeypatch.setattr(wc, "restore", fake_restore)
    r = client.post("/api/workspace/checkpoint/restore",
                    params={"workspace": str(repo), "sha": "abcdef1"},
                    json={"paths": ["src/a.py"]})
    assert r.status_code == 200, r.text
    assert got["paths"] == [os.path.realpath(str(repo / "src" / "a.py"))]


def test_commit_receives_the_confined_paths_not_the_raw_ones(client, repo, monkeypatch):
    from src import workspace_checkpoints as wc
    got = {}

    def fake_commit(root, paths, message):
        got["paths"] = list(paths)
        return {"ok": True}

    monkeypatch.setattr(wc, "user_git_commit", fake_commit)
    r = client.post("/api/workspace/commit", params={"workspace": str(repo)},
                    json={"paths": ["src/a.py"], "message": "m"})
    assert r.status_code == 200, r.text
    assert got["paths"] == [os.path.realpath(str(repo / "src" / "a.py"))]


def test_a_path_escaping_the_workspace_is_still_refused(client, repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    r = client.post("/api/workspace/commit", params={"workspace": str(repo)},
                    json={"paths": ["src/a.py", "../outside.txt"], "message": "m"})
    assert r.status_code == 400
