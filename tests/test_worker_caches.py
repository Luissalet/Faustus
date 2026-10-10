"""tests/test_worker_caches.py — #405: isolated workers get their own caches.

A fan-out candidate or an alternative's tests must not resolve packages
through the same pip/npm/uv cache as its siblings, and a call that names a
sibling's folder is written down.

Run: python -m pytest tests/test_worker_caches.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import worker_caches  # noqa: E402


def _ws(tmp_path, name="cand_a"):
    path = tmp_path / "exp" / name
    path.mkdir(parents=True)
    return str(path)


def test_isolated_env_points_every_cache_outside_the_workspace(tmp_path):
    ws = _ws(tmp_path)
    env = worker_caches.isolated_env(ws, {"PATH": "x", "PIP_CACHE_DIR": "/shared/pip", "KEEP": "1"})
    root = worker_caches.cache_root_for(ws)
    assert root == os.path.join(str(tmp_path / "exp"), "_caches", "cand_a")
    for name, sub in worker_caches.CACHE_VARS.items():
        assert env[name] == os.path.join(root, sub)
        assert os.path.isdir(env[name])
        assert not env[name].startswith(ws + os.sep)  # never in the diff
    assert env["KEEP"] == "1" and env["PATH"] == "x"


def test_two_workers_never_share_a_cache(tmp_path):
    a = worker_caches.isolated_env(_ws(tmp_path, "a"), {})
    b = worker_caches.isolated_env(_ws(tmp_path, "b"), {})
    for name in ("PIP_CACHE_DIR", "npm_config_cache", "UV_CACHE_DIR", "TMP"):
        assert a[name] != b[name]


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive names are a Windows thing")
def test_windows_case_variants_are_replaced(tmp_path):
    env = worker_caches.isolated_env(_ws(tmp_path), {"Tmp": "C:\\host", "NPM_CONFIG_CACHE": "C:\\npm"})
    assert "Tmp" not in env and "NPM_CONFIG_CACHE" not in env
    assert env["TMP"].endswith("tmp") and env["npm_config_cache"].endswith("npm")


def test_apply_active_is_a_no_op_outside_and_follows_tasks_inside(tmp_path):
    ws = _ws(tmp_path)
    base = {"PIP_CACHE_DIR": "/shared/pip"}
    assert worker_caches.apply_active(base) == base
    assert worker_caches.active_workspace() is None

    async def child():
        await asyncio.sleep(0)
        return worker_caches.apply_active(base)["PIP_CACHE_DIR"]

    async def main():
        with worker_caches.isolated(ws):
            inside = await asyncio.create_task(child())
            threaded = await asyncio.to_thread(lambda: worker_caches.apply_active(base)["PIP_CACHE_DIR"])
        return inside, threaded

    inside, threaded = asyncio.run(main())
    expected = os.path.join(worker_caches.cache_root_for(ws), "pip")
    assert inside == expected and threaded == expected
    assert worker_caches.apply_active(base) == base  # reset on exit


def test_foreign_paths_flags_siblings_and_shared_caches_only(tmp_path):
    ws = _ws(tmp_path, "a")
    sibling = _ws(tmp_path, "b")
    base = str(tmp_path / "exp" / "_base")
    shared = str(tmp_path / "home" / ".npm")
    text = (
        f"cat {ws}{os.sep}x.py && ls {sibling}{os.sep}src && cp {shared}{os.sep}pkg.tgz . "
        f"&& head {base}{os.sep}y.py && type {worker_caches.cache_root_for(ws)}{os.sep}pip "
        f"&& cat ../b/notes.txt && curl https://example.com/a/b"
    )
    hits = worker_caches.foreign_paths(text, ws, allow=[base], shared=[shared])
    kinds = {(os.path.basename(h["path"]), h["kind"]) for h in hits}
    assert ("src", "sibling") in kinds
    assert ("notes.txt", "sibling") in kinds
    assert ("pkg.tgz", "shared_cache") in kinds
    assert len(hits) == 3  # own folder, own cache, base and the URL are fine


def test_note_cross_reads_writes_the_audit_file(tmp_path):
    ws = _ws(tmp_path, "a")
    sibling = _ws(tmp_path, "b")
    assert worker_caches.note_cross_reads(f"ls {sibling}", "bash") == []  # not isolated
    with worker_caches.isolated(ws):
        hits = worker_caches.note_cross_reads(f"ls {sibling}", "bash")
    assert [h["kind"] for h in hits] == ["sibling"]
    recorded = worker_caches.read_cross_reads(ws)
    assert recorded and recorded[0]["tool"] == "bash" and recorded[0]["kind"] == "sibling"
    with open(os.path.join(worker_caches.cache_root_for(ws), "cross_reads.jsonl"), encoding="utf-8") as fh:
        assert json.loads(fh.readline())["path"] == os.path.normcase(os.path.normpath(sibling))


def test_a_drive_root_workspace_has_no_siblings():
    root = os.path.abspath(os.sep)
    ws = os.path.join(root, "proj")
    assert worker_caches.foreign_paths(os.path.join(root, "other", "x"), ws, shared=[]) == []
