"""The workspace file index cache must be bounded.

`/api/workspace/files` is designed to fire on every keystroke of the `@` picker
and indexes whatever root it is handed — up to 60 000 relative paths per entry.
The cache was a plain process-wide dict with a TTL and no cap: an expired entry
is overwritten, never dropped, so RSS grew one root at a time forever.
"""

import time

import pytest

import src.agent_harness as ah


@pytest.fixture(autouse=True)
def _clean_cache():
    ah._index_cache.clear()
    yield
    ah._index_cache.clear()


def _root(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    (d / "a.py").write_text("x\n", encoding="utf-8")
    return str(d)


def test_the_cache_never_grows_past_the_cap(tmp_path):
    roots = [_root(tmp_path, f"r{i:03d}") for i in range(ah._INDEX_CACHE_MAX + 20)]
    for r in roots:
        ah.workspace_file_index(r)
    assert len(ah._index_cache) == ah._INDEX_CACHE_MAX


def test_the_seventeenth_root_evicts_the_least_recently_used(tmp_path):
    """With a cap of 16, indexing a 17th root drops the one used longest ago —
    not an arbitrary one, and not the most recent."""
    import os
    roots = [_root(tmp_path, f"r{i:03d}") for i in range(ah._INDEX_CACHE_MAX + 1)]
    first, rest, extra = roots[0], roots[1:ah._INDEX_CACHE_MAX], roots[ah._INDEX_CACHE_MAX]

    ah.workspace_file_index(first)
    for r in rest:
        ah.workspace_file_index(r)
    assert len(ah._index_cache) == ah._INDEX_CACHE_MAX
    assert os.path.realpath(first) in ah._index_cache

    ah.workspace_file_index(extra)                       # the 17th
    assert len(ah._index_cache) == ah._INDEX_CACHE_MAX
    assert os.path.realpath(first) not in ah._index_cache, "the LRU entry survived"
    assert os.path.realpath(extra) in ah._index_cache
    for r in rest:
        assert os.path.realpath(r) in ah._index_cache


def test_a_cache_hit_counts_as_a_use(tmp_path):
    """A root someone keeps typing in must not fall out just because it was
    inserted first — otherwise the LRU degenerates into FIFO."""
    import os
    roots = [_root(tmp_path, f"r{i:03d}") for i in range(ah._INDEX_CACHE_MAX + 1)]
    oldest = roots[0]
    for r in roots[:ah._INDEX_CACHE_MAX]:
        ah.workspace_file_index(r)

    ah.workspace_file_index(oldest)          # served from cache, but still a use
    ah.workspace_file_index(roots[ah._INDEX_CACHE_MAX])

    assert os.path.realpath(oldest) in ah._index_cache
    assert os.path.realpath(roots[1]) not in ah._index_cache, "roots[1] was the LRU now"


def test_the_ttl_still_rebuilds_a_stale_entry(tmp_path, monkeypatch):
    root = _root(tmp_path, "ttl")
    assert ah.workspace_file_index(root) == ["a.py"]
    (tmp_path / "ttl" / "b.py").write_text("y\n", encoding="utf-8")
    # Inside the TTL the cached answer stands.
    assert ah.workspace_file_index(root) == ["a.py"]
    # Past it, the walk runs again.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + ah._INDEX_TTL_S + 1)
    assert sorted(ah.workspace_file_index(root)) == ["a.py", "b.py"]


def test_invalidate_index_still_drops_one_root(tmp_path):
    import os
    a, b = _root(tmp_path, "a"), _root(tmp_path, "b")
    ah.workspace_file_index(a)
    ah.workspace_file_index(b)
    ah.invalidate_index(a)
    assert os.path.realpath(a) not in ah._index_cache
    assert os.path.realpath(b) in ah._index_cache
    # And the next call rebuilds it rather than returning nothing.
    (tmp_path / "a" / "c.py").write_text("z\n", encoding="utf-8")
    assert sorted(ah.workspace_file_index(a)) == ["a.py", "c.py"]


def test_the_store_helper_tolerates_a_plain_dict(monkeypatch, tmp_path):
    """Several existing tests swap the cache for a plain dict
    (`monkeypatch.setattr(ah, "_index_cache", {})`). The eviction must not blow
    up on one — a harness failure may never break a chat turn."""
    monkeypatch.setattr(ah, "_index_cache", {}, raising=False)
    for i in range(ah._INDEX_CACHE_MAX + 5):
        ah.workspace_file_index(_root(tmp_path, f"p{i:03d}"))
    assert len(ah._index_cache) <= ah._INDEX_CACHE_MAX
