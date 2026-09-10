"""Lote 38 spec item 1: "5.000 ficheros sinteticos y medida de ficheros
abiertos" — the walk must never `open()` a file it excludes, at a scale
where "read everything and filter in memory" would be visibly different
from "prune before opening".

Kept in its own module (rather than tests/test_code_index.py) because it
writes several thousand files to `tmp_path` and is slower than the rest of
the suite.
"""

import builtins
import os

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


def _build_large_repo(root: str, *, vendor_files: int = 200, own_files: int = 4800) -> None:
    vendor_dir = os.path.join(root, "vendor")
    os.makedirs(vendor_dir, exist_ok=True)
    for i in range(vendor_files):
        with open(os.path.join(vendor_dir, f"lib_{i}.py"), "w", encoding="utf-8") as handle:
            handle.write(f"def vendored_{i}():\n    return {i}\n")
    with open(os.path.join(vendor_dir, "blob.bin"), "wb") as handle:
        handle.write(b"\x00\x01\x02binary" * 1000)

    own_dir = os.path.join(root, "src")
    os.makedirs(own_dir, exist_ok=True)
    for i in range(own_files):
        with open(os.path.join(own_dir, f"mod_{i}.py"), "w", encoding="utf-8") as handle:
            handle.write(f"def own_{i}():\n    return {i}\n")


@pytest.mark.slow
def test_5000_file_repo_never_opens_vendor_or_binary_files(ce_store, tmp_path):
    root = str(tmp_path / "big_repo")
    _build_large_repo(root)
    total_files = 200 + 1 + 4800  # vendor .py + blob.bin + own .py
    assert total_files == 5001

    opened: list[str] = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    builtins.open = spy_open
    try:
        result = ci.refresh(root)
    finally:
        builtins.open = real_open

    # Every candidate file that was actually indexed came from src/, never
    # from vendor/ — the walk pruned the directory before os.walk descended,
    # so vendor/'s 200 files and its binary were never candidates and their
    # open() calls (below) never happened.
    assert result["reindexed"] == 4800
    assert result["scanned"] == 4800

    vendor_opens = [path for path in opened if f"{os.sep}vendor{os.sep}" in path]
    binary_opens = [path for path in opened if path.endswith(".bin")]
    assert vendor_opens == []
    assert binary_opens == []
    # Sanity: the spy did observe opens for the repo's own files (plus a
    # .gitignore/.faustusignore probe), so an empty `opened` list would not
    # have proven anything.
    assert any(f"{os.sep}src{os.sep}" in path for path in opened)

    assert ci.find_definition("own_0", workspace=root)
    assert ci.find_definition("vendored_0", workspace=root) == []
