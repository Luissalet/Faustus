"""WEB-06 — complex navigation and extraction: pagination, downloads, forms.

``src/browser_extraction.py`` did not exist before this lote.
"""
import asyncio
import hashlib
import os

import pytest

from src.browser_extraction import (
    DownloadExpectation,
    FormFillLimits,
    PageResult,
    PaginationLimits,
    check_form_fill,
    detect_restricted_access,
    paginate,
    project_fields,
    select_download_target,
    verify_download,
)


def _pages(pages):
    async def _fetch(index):
        return pages[index]
    return _fetch


def test_paginate_walks_until_no_next_page():
    pages = [
        PageResult(0, ({"id": 1},), True),
        PageResult(1, ({"id": 2},), True),
        PageResult(2, ({"id": 3},), False),
    ]
    out = asyncio.run(paginate(_pages(pages)))
    assert out["pages_fetched"] == 3
    assert [i["id"] for i in out["items"]] == [1, 2, 3]
    assert out["truncated"] is False
    assert out["reason"] == "exhausted"


def test_paginate_stops_at_max_pages_and_says_truncated():
    pages = [PageResult(i, ({"id": i},), True) for i in range(10)]
    out = asyncio.run(paginate(_pages(pages), limits=PaginationLimits(max_pages=3, max_items=1000)))
    assert out["pages_fetched"] == 3
    assert out["truncated"] is True
    assert out["reason"] == "max_pages"


def test_paginate_stops_at_max_items():
    pages = [PageResult(i, ({"id": i}, {"id": i + 100}), True) for i in range(10)]
    out = asyncio.run(paginate(_pages(pages), limits=PaginationLimits(max_pages=100, max_items=3)))
    assert out["item_count"] == 3
    assert out["truncated"] is True
    assert out["reason"] == "max_items"


def test_project_fields_never_returns_the_whole_row():
    row = {"name": "Alice", "ssn": "secret", "email": "a@example.org"}
    out = project_fields(row, ["name", "email"])
    assert out == {"name": "Alice", "email": "a@example.org"}
    assert "ssn" not in out


def test_detect_restricted_access_flags_a_block_page():
    assert detect_restricted_access("Please sign in to continue reading this article") is not None
    assert detect_restricted_access("Inicia sesión para continuar") is not None
    assert detect_restricted_access("Here is the full article text, freely available.") is None


def test_form_fill_limits_reject_oversized_forms():
    assert check_form_fill({"a": "1"}) is None
    huge = {f"f{i}": "x" for i in range(60)}
    err = check_form_fill(huge, limits=FormFillLimits(max_fields=50))
    assert err is not None and "50" in err
    err2 = check_form_fill({"bio": "x" * 5000}, limits=FormFillLimits(max_value_chars=4000))
    assert err2 is not None and "bio" in err2


# ---------------------------------------------------------------------------
# Sandbox: a single filename must never expose the rest of the folder.
# ---------------------------------------------------------------------------

def test_select_download_target_stays_inside_sandbox(tmp_path):
    target = select_download_target(str(tmp_path), "report.pdf")
    assert target == os.path.join(os.path.realpath(str(tmp_path)), "report.pdf")


def test_select_download_target_strips_directory_components():
    """A filename claiming to be `../../etc/passwd` must resolve to a plain
    file INSIDE the sandbox, not escape it -- the regression this guards:
    without `os.path.basename`, `select_download_target` would happily
    return a path outside `sandbox_root`."""
    import tempfile
    with tempfile.TemporaryDirectory() as sandbox:
        target = select_download_target(sandbox, "../../etc/passwd")
        assert target == os.path.join(os.path.realpath(sandbox), "passwd")
        assert target.startswith(os.path.realpath(sandbox))


def test_select_download_target_rejects_absolute_escape(tmp_path):
    # Even a filename engineered as an absolute path is basename-only.
    target = select_download_target(str(tmp_path), "/etc/shadow")
    assert target == os.path.join(os.path.realpath(str(tmp_path)), "shadow")


# ---------------------------------------------------------------------------
# Downloads: a partial result must never be reported as complete.
# ---------------------------------------------------------------------------

def test_verify_download_complete_when_size_and_hash_match(tmp_path):
    path = tmp_path / "file.bin"
    data = b"hello world"
    path.write_bytes(data)
    expectation = DownloadExpectation(expected_sha256=hashlib.sha256(data).hexdigest(), expected_bytes=len(data))
    outcome = verify_download(str(path), expectation)
    assert outcome.complete is True
    assert outcome.reason == "ok"


def test_verify_download_partial_is_never_complete(tmp_path):
    """The regression case named directly in WEB-06's acceptance criterion:
    a transfer cut short must not be reported as a finished download."""
    path = tmp_path / "partial.bin"
    path.write_bytes(b"only-half")
    outcome = verify_download(str(path), expected_total_bytes=1000)
    assert outcome.complete is False
    assert "of 1000" in outcome.reason


def test_verify_download_hash_mismatch_is_not_complete(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"actual content")
    expectation = DownloadExpectation(expected_sha256="0" * 64)
    outcome = verify_download(str(path), expectation)
    assert outcome.complete is False
    assert "sha256 mismatch" in outcome.reason
