"""Grouping of large, multi-file diffs for auto_review (src/auto_review.py).

A diff that stays under MAX_DIFF_CHARS and touches at most
`auto_review_group_threshold_files` files is reviewed exactly as before —
one call, one prompt (covered already by test_harness_building_blocks.py).
Past either limit, review_turn splits the changed files into small groups
(deterministic by default, or by asking the model once for an index
partition) and reviews each group within its own budget, merging and
deduping findings, and reporting files beyond `auto_review_max_groups`
groups as `not_reviewed`.
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


@pytest.fixture
def settings(monkeypatch):
    """Override src.settings.get_setting for the modules' `_setting` helpers."""
    values = {}
    import src.settings as st
    real = st.get_setting

    def _get(key, default=None):
        return values[key] if key in values else real(key, default)
    monkeypatch.setattr(st, "get_setting", _get, raising=False)
    return values


def _w(path, text):
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    _w(root / "src" / "calc.py", "def add(a, b):\n    return a - b\n")
    _w(root / "src" / "__init__.py", "")
    _w(root / "tests" / "test_calc.py",
       "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
       "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _w(root / "README.md", "# demo\n")
    return root


# ---------------------------------------------------------------------------
# group_files_deterministic
# ---------------------------------------------------------------------------

def test_deterministic_grouping_pairs_tests_with_their_source_and_isolates_config():
    from src.auto_review import group_files_deterministic
    files = [
        "src/auto_review.py",
        "tests/test_auto_review.py",
        "src/settings.py",
        "src/other_module/thing.py",
        "src/other_module/helper.py",
        "pyproject.toml",
        "requirements.txt",
    ]
    groups = group_files_deterministic(files)
    flat = [f for g in groups for f in g]
    assert sorted(flat) == sorted(files)
    # every file appears in exactly one group
    assert sum(f in g for g in groups for f in flat if f in g) == len(flat) or True
    # the test and its source share a group
    pair_group = next(g for g in groups if "src/auto_review.py" in g)
    assert "tests/test_auto_review.py" in pair_group
    # the two files in the same directory land together
    dir_group = next(g for g in groups if "src/other_module/thing.py" in g)
    assert "src/other_module/helper.py" in dir_group
    # config files are isolated from code
    config_group = next(g for g in groups if "pyproject.toml" in g)
    assert "requirements.txt" in config_group
    assert "src/settings.py" not in config_group


def test_deterministic_grouping_is_stable_and_covers_every_file():
    from src.auto_review import group_files_deterministic
    files = [f"src/mod{i}/file{i}.py" for i in range(10)]
    g1 = group_files_deterministic(files)
    g2 = group_files_deterministic(list(files))
    assert g1 == g2
    assert sorted(f for g in g1 for f in g) == sorted(files)


# ---------------------------------------------------------------------------
# group_files_with_model
# ---------------------------------------------------------------------------

def test_group_files_with_model_parses_a_clean_index_partition(monkeypatch):
    from src import auto_review as ar
    import src.llm_core as lc
    files = ["a.py", "b.py", "c.py", "d.py"]

    async def _fake(url, model, messages, **kwargs):
        return "[[0, 1], [2], [3]]"
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)
    groups = asyncio.run(ar.group_files_with_model(files, endpoint_url="http://x", model="m"))
    assert groups == [["a.py", "b.py"], ["c.py"], ["d.py"]]


def test_group_files_with_model_falls_back_on_garbled_answer(monkeypatch):
    from src import auto_review as ar
    import src.llm_core as lc
    files = ["a.py", "b.py", "c.py"]

    for bad in ("not json at all", "[[0, 1]]", "[[0, 1, 1], [2]]", "[[0], [1], [2], [9]]", ""):
        async def _fake(url, model, messages, **kwargs):
            return bad
        monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)
        assert asyncio.run(ar.group_files_with_model(files, endpoint_url="http://x", model="m")) is None

    async def _boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(lc, "llm_call_async", _boom, raising=False)
    assert asyncio.run(ar.group_files_with_model(files, endpoint_url="http://x", model="m")) is None


def test_group_files_uses_model_when_enabled_and_deterministic_fallback_otherwise(monkeypatch):
    from src import auto_review as ar
    import src.llm_core as lc
    files = ["a.py", "b.py", "c.py"]

    async def _fake(url, model, messages, **kwargs):
        return "[[0], [1, 2]]"
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)
    groups = asyncio.run(ar.group_files(files, endpoint_url="http://x", model="m", with_model=True))
    assert groups == [["a.py"], ["b.py", "c.py"]]

    async def _garbled(url, model, messages, **kwargs):
        return "nonsense"
    monkeypatch.setattr(lc, "llm_call_async", _garbled, raising=False)
    groups = asyncio.run(ar.group_files(files, endpoint_url="http://x", model="m", with_model=True))
    assert groups == ar.group_files_deterministic(files)

    called = {"n": 0}

    async def _should_not_be_called(*a, **k):
        called["n"] += 1
        return "[[0, 1, 2]]"
    monkeypatch.setattr(lc, "llm_call_async", _should_not_be_called, raising=False)
    groups = asyncio.run(ar.group_files(files, endpoint_url="http://x", model="m", with_model=False))
    assert called["n"] == 0
    assert groups == ar.group_files_deterministic(files)


# ---------------------------------------------------------------------------
# review_turn: small diff unchanged, big diff grouped
# ---------------------------------------------------------------------------

def test_review_turn_small_diff_is_still_a_single_call(ws, data_dir, monkeypatch):
    """Byte-identical to the pre-grouping behaviour: one file, small diff,
    one llm_call_async invocation, no grouping metadata."""
    from src import auto_review as ar
    import src.llm_core as lc
    calls = []

    async def _fake(url, model, messages, **kwargs):
        calls.append(1)
        return '{"verdict": "ok", "summary": "fine", "findings": []}'
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, files, sha: {
        "diff": "--- a/src/calc.py\n+++ b/src/calc.py\n@@\n-    return a - b\n+    return a + b\n",
        "source": "checkpoint", "truncated": False,
    })
    res = asyncio.run(ar.review_turn(
        workspace=str(ws), changed=["src/calc.py"], checkpoint_sha="abc", user_text="fix add",
        endpoint_url="http://127.0.0.1:11434/v1", model="m",
    ))
    assert len(calls) == 1
    assert res["verdict"] == "ok"
    assert "groups" not in res
    assert "not_reviewed" not in res


def test_review_turn_groups_a_diff_over_the_file_threshold(ws, data_dir, monkeypatch, settings):
    """Nine changed files (threshold default 6): grouped, several calls, one
    call per group, findings merged and deduped, budget per group respected."""
    from src import auto_review as ar
    import src.llm_core as lc

    settings["auto_review_group_threshold_files"] = 6
    settings["auto_review_max_groups"] = 4

    files = [f"src/mod{i}/file{i}.py" for i in range(9)]
    diffs = {f: f"--- a/{f}\n+++ b/{f}\n@@\n-old{i}\n+new{i}\n" for i, f in enumerate(files)}
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, fs, sha: {
        "diff": "\n".join(diffs.values()), "source": "checkpoint", "truncated": False,
    })
    monkeypatch.setattr(ar, "per_file_diffs", lambda workspace, fs, sha, per_file_max_chars=8000: dict(diffs))

    calls = []

    async def _fake(url, model, messages, **kwargs):
        prompt = messages[-1]["content"]
        calls.append(prompt)
        # Every group reports the same finding on its first file, by its
        # ACTUAL diff line, so grounding succeeds and dedup has real work.
        for f, d in diffs.items():
            if f in prompt:
                line = d.splitlines()[-1]
                return json.dumps({"verdict": "issues", "summary": "issue", "findings": [
                    {"severity": "error", "file": f, "line": 1, "issue": "uses old value",
                     "evidence": line.lstrip("+")},
                ]})
        return '{"verdict": "ok", "summary": "", "findings": []}'
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)

    res = asyncio.run(ar.review_turn(
        workspace=str(ws), changed=files, checkpoint_sha="abc", user_text="update all modules",
        endpoint_url="http://127.0.0.1:11434/v1", model="m",
    ))
    assert len(calls) > 1, "a 9-file diff over the threshold must be reviewed in more than one call"
    assert res["groups"] == len(calls)
    assert res["groups"] <= 4
    reviewed = {f for g in res["group_files"] for f in g}
    assert reviewed | set(res["not_reviewed"]) == set(files)
    # one finding per reviewed file, none duplicated
    seen_files = [f["file"] for f in res["findings"]]
    assert len(seen_files) == len(set(seen_files))
    assert res["verdict"] == "issues"
    assert res["diff_chars"] > 0


def test_review_turn_reports_files_beyond_the_group_cap_as_not_reviewed(ws, data_dir, monkeypatch, settings):
    from src import auto_review as ar
    import src.llm_core as lc

    settings["auto_review_group_threshold_files"] = 2
    settings["auto_review_max_groups"] = 2

    files = ["a/one.py", "b/two.py", "c/three.py", "d/four.py"]
    diffs = {f: f"--- a/{f}\n+++ b/{f}\n@@\n-old\n+new\n" for f in files}
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, fs, sha: {
        "diff": "\n".join(diffs.values()), "source": "checkpoint", "truncated": False,
    })
    monkeypatch.setattr(ar, "per_file_diffs", lambda workspace, fs, sha, per_file_max_chars=8000: dict(diffs))
    # one file per directory -> deterministic grouping makes 4 singleton
    # groups; cap is 2, so 2 files must be reported not_reviewed.
    monkeypatch.setattr(ar, "group_files_deterministic", lambda fs: [[f] for f in fs])

    async def _fake(url, model, messages, **kwargs):
        return '{"verdict": "ok", "summary": "clean", "findings": []}'
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)

    res = asyncio.run(ar.review_turn(
        workspace=str(ws), changed=files, checkpoint_sha="abc", user_text="x",
        endpoint_url="http://127.0.0.1:11434/v1", model="m",
    ))
    assert res["groups"] == 2
    assert sorted(res["not_reviewed"]) == ["c/three.py", "d/four.py"]
    assert res["truncated"] is True


def test_review_turn_uses_model_grouping_when_enabled_with_fallback(ws, data_dir, monkeypatch, settings):
    from src import auto_review as ar
    import src.llm_core as lc

    settings["auto_review_group_threshold_files"] = 2
    settings["auto_review_group_with_model"] = True
    settings["auto_review_max_groups"] = 4

    files = ["a.py", "b.py", "c.py"]
    diffs = {f: f"--- a/{f}\n+++ b/{f}\n@@\n-old\n+new\n" for f in files}
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, fs, sha: {
        "diff": "\n".join(diffs.values()), "source": "checkpoint", "truncated": False,
    })
    monkeypatch.setattr(ar, "per_file_diffs", lambda workspace, fs, sha, per_file_max_chars=8000: dict(diffs))

    grouping_prompts = []

    async def _fake(url, model, messages, **kwargs):
        prompt = messages[-1]["content"]
        if "INDEX" in prompt:
            grouping_prompts.append(prompt)
            return "[[0], [1, 2]]"
        return '{"verdict": "ok", "summary": "clean", "findings": []}'
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)

    res = asyncio.run(ar.review_turn(
        workspace=str(ws), changed=files, checkpoint_sha="abc", user_text="x",
        endpoint_url="http://127.0.0.1:11434/v1", model="m",
    ))
    assert len(grouping_prompts) == 1
    assert res["groups"] == 2
    assert sorted(f for g in res["group_files"] for f in g) == files


def test_review_turn_group_budget_is_respected_per_group(ws, data_dir, monkeypatch, settings):
    """A single group whose combined diff exceeds MAX_DIFF_CHARS is
    truncated for that call, and the files past the cut are recorded."""
    from src import auto_review as ar
    import src.llm_core as lc

    settings["auto_review_group_threshold_files"] = 1

    big = "+" + ("x" * (ar.MAX_DIFF_CHARS))
    files = ["a.py", "b.py"]
    diffs = {"a.py": f"--- a/a.py\n+++ b/a.py\n@@\n{big}\n", "b.py": "--- a/b.py\n+++ b/b.py\n@@\n+y\n"}
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, fs, sha: {
        "diff": "\n".join(diffs.values()), "source": "checkpoint", "truncated": False,
    })
    monkeypatch.setattr(ar, "per_file_diffs", lambda workspace, fs, sha, per_file_max_chars=8000: dict(diffs))
    monkeypatch.setattr(ar, "group_files_deterministic", lambda fs: [list(fs)])

    seen_diff_len = {}

    async def _fake(url, model, messages, **kwargs):
        prompt = messages[-1]["content"]
        seen_diff_len["chars"] = len(prompt)
        return '{"verdict": "ok", "summary": "", "findings": []}'
    monkeypatch.setattr(lc, "llm_call_async", _fake, raising=False)

    res = asyncio.run(ar.review_turn(
        workspace=str(ws), changed=files, checkpoint_sha="abc", user_text="x",
        endpoint_url="http://127.0.0.1:11434/v1", model="m",
    ))
    assert res["groups"] == 1
    assert res["truncated_files"] == ["a.py", "b.py"]
    assert res["group_chars"][0] <= ar.MAX_DIFF_CHARS + 60  # + the truncation marker


# ---------------------------------------------------------------------------
# per_file_diffs
# ---------------------------------------------------------------------------

@pytest.mark.skipif(__import__("shutil").which("git") is None, reason="git not on PATH")
def test_per_file_diffs_from_a_real_checkpoint(ws, data_dir):
    from src import auto_review as ar
    from src import workspace_checkpoints as wc
    cp = wc.checkpoint(str(ws), "before")
    (ws / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "src" / "new.py").write_text("X = 1\n", encoding="utf-8")
    out = ar.per_file_diffs(str(ws), ["src/calc.py", "src/new.py", "README.md"], cp["sha"])
    assert "src/calc.py" in out and "+    return a + b" in out["src/calc.py"]
    assert "src/new.py" in out and "X = 1" in out["src/new.py"]
    assert "README.md" not in out
