"""Lote 64 — IDX-02: incremental reindexing under concurrent writes and a
repo-size limit.

`tests/test_code_index.py::test_incremental_refresh_skips_unchanged_files`
already proves the single-threaded incremental contract (only a touched file
is reindexed). huecos.md's own gap is narrower and unverified: what happens
when `refresh()` runs WHILE a file underneath it keeps being written, and
what happens on a workspace bigger than one `refresh()` call is willing to
walk (`budget_files`, the "límite de tamaño de repo"). Both existed in
`src/code_index.py` before this closure — `store.db()`'s module lock and
`refresh()`'s own `budget_files`/`truncated` contract — this file is what was
missing: proof they hold.
"""
import os
import threading
import time

import pytest

from src import code_index as ci
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _module(index: int) -> str:
    return (
        f'"""Module {index}."""\n\n\n'
        f"def handler_{index}(x):\n"
        f"    return x + {index}\n"
    )


# ── concurrent writes during reindexing ─────────────────────────────────────

@pytest.fixture()
def busy_workspace(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root, exist_ok=True)
    for i in range(12):
        write(root, f"pkg/mod_{i}.py", _module(i))
    return root


def test_refresh_survives_a_file_being_rewritten_while_it_scans(ce_store, busy_workspace):
    """A writer thread keeps mutating one file's content throughout the scan
    (the real-world case: an editor autosave, a generator regenerating a
    file) while `refresh()` runs concurrently. `_read_hashed` reads the whole
    file in one `open().read()` — this proves that race never raises, never
    corrupts the store, and the index converges to a state some ONE
    generation of the hot file actually had (never a torn mix)."""
    hot_path = os.path.join(busy_workspace, "pkg", "mod_0.py")
    stop = threading.Event()
    errors = []

    def _writer():
        gen = 0
        while not stop.is_set():
            gen += 1
            try:
                with open(hot_path, "w", encoding="utf-8") as handle:
                    handle.write(
                        f'"""Module 0, generation {gen}."""\n\n\n'
                        f"def handler_0_gen_{gen}(x):\n"
                        f"    return x + {gen}\n"
                    )
            except OSError as exc:  # pragma: no cover - would fail the test below anyway
                errors.append(exc)
            time.sleep(0.001)

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    try:
        for _ in range(5):
            out = ci.refresh(busy_workspace, pause_on_pressure=False)
            assert isinstance(out, dict) and "reindexed" in out
    finally:
        stop.set()
        writer.join(timeout=5)

    assert not errors

    # Referential integrity: every indexed symbol still belongs to a file
    # `refresh()` currently knows about — a race must never leave the two
    # tables disagreeing about what exists.
    with store.db() as conn:
        orphans = conn.execute(
            "SELECT COUNT(*) AS n FROM ci_symbols s "
            "WHERE s.workspace = ? AND s.project_id = '' AND NOT EXISTS ("
            "  SELECT 1 FROM ci_files f WHERE f.workspace = s.workspace "
            "  AND f.project_id = s.project_id AND f.path = s.path)",
            (ci._norm_workspace(busy_workspace),)).fetchone()["n"]
    assert orphans == 0

    # The hot file settled on SOME single generation's definition, not a
    # blend of two reads: after the writer stops and one more refresh runs,
    # the file's LAST generation is indexed and findable by name.
    with open(hot_path, encoding="utf-8") as handle:
        final_text = handle.read()
    stop_writing_gen = int(final_text.split("handler_0_gen_")[1].split("(")[0])
    ci.refresh(busy_workspace, pause_on_pressure=False)
    final_hit = ci.find_definition(f"handler_0_gen_{stop_writing_gen}", workspace=busy_workspace)
    assert len(final_hit) == 1
    assert final_hit[0]["path"] == "pkg/mod_0.py"


def test_two_refresh_calls_from_different_threads_do_not_corrupt_the_store(ce_store, busy_workspace):
    """`store.db()` serialises on a module-level lock (see
    `src/context_engine/store.py::db`'s docstring: "a short-lived connection
    under the module lock"), so two `refresh()` calls racing from different
    threads must queue rather than interleave their writes. This is the
    property that makes it SAFE for two agents/routes to trigger a reindex
    of the same workspace at once, which a background scheduler and an
    on-demand `POST .../reindex` route both can do."""
    errors = []
    results = []
    lock = threading.Lock()

    def _run():
        try:
            out = ci.refresh(busy_workspace, pause_on_pressure=False)
            with lock:
                results.append(out)
        except Exception as exc:  # noqa: BLE001 - the test itself asserts on this
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(results) == 4
    status = ci.status(busy_workspace)
    assert status["files"] == 12
    assert status["symbols"] >= 12  # at least one handler per module


# ── repo size limit ──────────────────────────────────────────────────────────

@pytest.fixture()
def large_workspace(tmp_path):
    root = str(tmp_path / "big_repo")
    os.makedirs(root, exist_ok=True)
    for i in range(25):
        write(root, f"pkg/mod_{i:02d}.py", _module(i))
    return root


def test_a_small_budget_truncates_and_says_so(ce_store, large_workspace):
    out = ci.refresh(large_workspace, budget_files=5, pause_on_pressure=False)
    assert out["scanned"] == 5
    assert out["truncated"] is True
    status = ci.status(large_workspace)
    assert status["files"] <= 5


def test_truncated_refresh_never_deletes_files_it_did_not_look_at(ce_store, large_workspace):
    """A truncated walk must not conclude a file it never reached is gone —
    `refresh()`'s own `if not truncated:` guard around the removal pass. A
    small budget followed by a full one must still end up with all 25 files
    indexed, not the 5 from the first call plus deletions of the rest."""
    ci.refresh(large_workspace, budget_files=5, pause_on_pressure=False)
    assert ci.status(large_workspace)["files"] <= 5

    full = ci.refresh(large_workspace, budget_files=100, pause_on_pressure=False)
    assert full["truncated"] is False
    assert full["removed"] == 0
    status = ci.status(large_workspace)
    assert status["files"] == 25


def test_default_budget_comfortably_covers_an_ordinary_repo_without_truncating(ce_store, large_workspace):
    out = ci.refresh(large_workspace, pause_on_pressure=False)  # DEFAULT_BUDGET_FILES = 20,000
    assert out["truncated"] is False
    assert out["scanned"] == 25
