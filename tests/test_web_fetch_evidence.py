"""WEB-02 — web_fetch declares truncation explicitly and attaches an
EvidenceRef (URL, content hash, capture time) instead of leaving provenance
to the free-text "Source: <url>" header a model could paraphrase away.
"""
import asyncio
import hashlib
import json

import pytest

from src.agent_tools.web_tools import WebFetchTool
from src.constants import WEB_FETCH_SOFT_MAX_BYTES
from src.contracts import EvidenceRef


def _run(payload, ctx=None):
    return asyncio.run(WebFetchTool().execute(json.dumps(payload), ctx=ctx if ctx is not None else {}))


def test_full_fetch_declares_not_truncated_and_no_kept_bytes(monkeypatch):
    def fake_fetch(url, timeout=10, max_bytes=None):
        return {"content": "hello world", "title": "Hi", "error": "", "truncated": False,
                "fetched_bytes": 11, "total_bytes": 11}

    import src.search.content as alias_mod
    monkeypatch.setattr(alias_mod, "fetch_webpage_content", fake_fetch)

    out = _run({"url": "https://example.com/a"})
    assert out["truncated"] is False
    assert "kept_bytes" not in out


def test_download_truncation_declares_truncated_and_kept_bytes(monkeypatch):
    def fake_fetch(url, timeout=10, max_bytes=None):
        return {"content": "partial body", "title": "T", "error": "", "truncated": True,
                "fetched_bytes": WEB_FETCH_SOFT_MAX_BYTES, "total_bytes": 9_000_000}

    import src.search.content as alias_mod
    monkeypatch.setattr(alias_mod, "fetch_webpage_content", fake_fetch)

    out = _run({"url": "https://example.com/big.txt"})
    assert out["truncated"] is True
    assert out["kept_bytes"] == WEB_FETCH_SOFT_MAX_BYTES


def test_output_char_cap_also_declares_truncated_and_kept_bytes(monkeypatch):
    from src.constants import MAX_OUTPUT_CHARS

    huge_text = "y" * (MAX_OUTPUT_CHARS + 10_000)

    def fake_fetch(url, timeout=10, max_bytes=None):
        return {"content": huge_text, "title": "Big", "error": "", "truncated": False,
                "fetched_bytes": len(huge_text), "total_bytes": len(huge_text)}

    import src.search.content as alias_mod
    monkeypatch.setattr(alias_mod, "fetch_webpage_content", fake_fetch)

    out = _run({"url": "https://example.com/huge"})
    # The download itself was not budget-truncated, but the output-char cap
    # still cut it -- this must not silently disappear as just a
    # "[...truncated]" string the model has to notice on its own.
    assert out["truncated"] is True
    assert out["kept_bytes"] == len(out["output"])


def test_evidence_ref_hashes_exactly_the_returned_text(monkeypatch):
    text = "hello evidence world"

    def fake_fetch(url, timeout=10, max_bytes=None):
        return {"content": text, "title": "Hi", "error": "", "truncated": False,
                "fetched_bytes": len(text), "total_bytes": len(text)}

    import src.search.content as alias_mod
    monkeypatch.setattr(alias_mod, "fetch_webpage_content", fake_fetch)

    out = _run({"url": "https://example.com/evidence"}, ctx={"owner": "alice", "project_id": "proj1"})
    assert "evidence_refs" in out
    [ref] = out["evidence_refs"]
    evidence = EvidenceRef.from_mapping(ref)
    assert evidence.source_type == "web"
    assert evidence.source_ref == "https://example.com/evidence"
    assert evidence.owner_id == "alice"
    assert evidence.project_id == "proj1"
    assert evidence.content_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert evidence.locator.kind == "whole"


def test_evidence_ref_uses_byte_range_locator_when_truncated(monkeypatch):
    def fake_fetch(url, timeout=10, max_bytes=None):
        return {"content": "partial", "title": "T", "error": "", "truncated": True,
                "fetched_bytes": 7, "total_bytes": 9_000_000}

    import src.search.content as alias_mod
    monkeypatch.setattr(alias_mod, "fetch_webpage_content", fake_fetch)

    out = _run({"url": "https://example.com/partial"})
    [ref] = out["evidence_refs"]
    evidence = EvidenceRef.from_mapping(ref)
    assert evidence.locator.kind == "byte_range"
    assert evidence.locator.value == "0-7"


def test_missing_owner_falls_back_to_system(monkeypatch):
    def fake_fetch(url, timeout=10, max_bytes=None):
        return {"content": "hi", "title": "", "error": "", "truncated": False,
                "fetched_bytes": 2, "total_bytes": 2}

    import src.search.content as alias_mod
    monkeypatch.setattr(alias_mod, "fetch_webpage_content", fake_fetch)

    out = _run({"url": "https://example.com/x"}, ctx={})
    [ref] = out["evidence_refs"]
    evidence = EvidenceRef.from_mapping(ref)
    assert evidence.owner_id == "system"
