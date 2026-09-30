"""Live file reuse validates only delivered inline references through FileSource."""
import asyncio
from dataclasses import replace
import json
import os

import pytest

from src.context_engine import wiring
from src.context_engine.adapters.files import FileSource
from src.context_engine.candidates import RetrievalRequest
from src.context_engine.contracts import ContextBudget, ContextExecution, ContextItem, ContextPacket, ContextRequest, ContextSection, ContextTask


@pytest.fixture
def live(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("old\nsecond\nthird\n", encoding="utf-8")
    request = ContextRequest(request_id="synthetic", execution=ContextExecution(owner="fixture", workspace=str(tmp_path), turn_id="one"),
                             task=ContextTask(query="Review file"), explicit_refs=("file:synthetic.txt", "file:not_delivered.txt"))
    calls, fetched = [], []
    original_fetch = FileSource.fetch

    async def fetch(self, ref, req):
        fetched.append(ref)
        return await original_fetch(self, ref, req)

    async def compile_live(req, **kwargs):
        # This synthetic compiler deliberately does not query objectives.
        from src.context_engine.objective_reuse import record_query
        record_query(None, (), False)
        from src.context_engine.personal_memory_reuse import record_query as record_personal_query
        record_personal_query(None, (), False)
        from src.context_engine.document_reuse import record_query as record_document_query
        record_document_query(None, (), False)
        from src.context_engine.memory_engine_reuse import record_query as record_standing_query
        record_standing_query(None, (), False)
        calls.append(req)
        candidate = await FileSource().fetch(req.explicit_refs[0], RetrievalRequest(request=req))
        item = ContextItem(item_id="file", source_type="file", source_ref=req.explicit_refs[0],
                           source_revision=candidate.source_revision if candidate else "",
                           body=candidate.body if candidate else "", tokens=20)
        return ContextPacket(packet_id=f"ctxpkt_{len(calls)}", request_id=req.request_id, owner="fixture",
                             window=ContextBudget(max_tokens=4096, input_budget=900),
                             sections=(ContextSection(kind="code_map", items=(item,)),)), []

    monkeypatch.setattr(FileSource, "fetch", fetch)
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **k: 6000)
    monkeypatch.setattr(wiring, "_remember_omitted", lambda *a: [])
    monkeypatch.setattr(wiring, "_compile_live", compile_live)
    return path, request, calls, fetched


async def deliver(request, previous=None):
    return await wiring.deliver_round(request=request, messages=[], previous=previous)


async def test_changed_equal_stat_file_recompiles(live):
    path, request, calls, fetched = live
    first = await deliver(request)
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"old", b"NEW"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = await deliver(request, first)
    assert len(calls) == 2 and not second["report"].get("reused")
    assert "NEW" in str(second["message"]) and second["message"] is not first["message"]
    assert fetched == ["file:synthetic.txt"] * 3


async def test_unchanged_file_reuses_and_does_not_read_undelivered_ref(live):
    _, request, calls, fetched = live
    first = await deliver(request)
    second = await deliver(request, first)
    assert len(calls) == 1 and second["report"]["reused"] is True
    assert second["message"] is first["message"]
    assert fetched == ["file:synthetic.txt"] * 2
    assert "_file_reuse_receipts" not in json.dumps(first["report"])
    assert "_file_reuse_receipts" not in json.dumps(first["message"])


@pytest.mark.parametrize("change", ["missing", "binary", "denied"])
async def test_unavailable_file_invalidates_old_inline_content(live, monkeypatch, change):
    path, request, calls, _ = live
    first = await deliver(request)
    if change == "missing":
        path.unlink()
    elif change == "binary":
        path.write_bytes(b"a\x00b")
    else:
        original = open

        def denied(p, *a, **kw):
            if str(p) == str(path):
                raise PermissionError("synthetic unreadable")
            return original(p, *a, **kw)

        monkeypatch.setattr("builtins.open", denied)
    second = await deliver(request, first)
    assert len(calls) == 2
    assert second is None or not second["report"].get("reused")
    assert second is None or "old" not in str(second["message"])


async def test_outside_window_projection_is_stable_but_inside_changes(live):
    path, request, calls, _ = live
    request = replace(request, explicit_refs=("file:synthetic.txt#L2-L3",))
    first = await deliver(request)
    path.write_text("NEW\nsecond\nthird\n", encoding="utf-8")
    second = await deliver(request, first)
    assert second["report"]["reused"] is True and len(calls) == 1
    path.write_text("NEW\nSECOND\nthird\n", encoding="utf-8")
    third = await deliver(request, second)
    assert len(calls) == 2 and not third["report"].get("reused")


async def test_mode_off_performs_no_validation_or_compilation(live, monkeypatch):
    _, request, calls, fetched = live
    first = await deliver(request)
    monkeypatch.setattr(wiring, "enabled", lambda: False)
    assert await deliver(request, first) is None
    assert len(calls) == 1 and len(fetched) == 1


async def test_policy_gate_prevents_disk_io(live, monkeypatch):
    _, request, calls, _ = live
    first = await deliver(request)
    gated = replace(request, policy=replace(request.policy, allow_project_sources=False))
    # Use the matching identity so this test reaches the freshness guard itself.
    previous = {**first, "_reuse_scope": wiring._reuse_scope(gated)}
    import src.file_mentions as mentions
    monkeypatch.setattr(mentions, "_read_head", lambda *a: pytest.fail("policy-gated read"))
    monkeypatch.setattr(mentions, "_window", lambda *a: pytest.fail("policy-gated read"))
    second = await deliver(gated, previous)
    assert len(calls) == 2 and (second is None or not second["report"].get("reused"))


async def test_concurrent_consumers_keep_receipts_local(live):
    _, request, calls, _ = live
    first = await deliver(request)
    results = await asyncio.gather(deliver(request, first), deliver(request, first))
    assert all(r["report"]["reused"] for r in results)
    assert all(r["message"] is first["message"] for r in results)
    assert len(calls) == 1
    assert first["_file_reuse_receipts"] == results[0]["_file_reuse_receipts"]


async def test_missing_private_identity_recompiles(live):
    _, request, calls, _ = live
    first = await deliver(request)
    legacy = {k: v for k, v in first.items() if k != "_file_reuse_receipts"}
    await deliver(request, legacy)
    assert len(calls) == 2


async def test_receipt_overflow_recompiles_instead_of_validating_prefix(live, monkeypatch):
    _, request, calls, fetched = live
    monkeypatch.setattr(wiring, "MAX_FILE_REUSE_REFS", 0)
    first = await deliver(request)
    assert first["_file_reuse_receipts"] is None
    await deliver(request, first)
    assert len(calls) == 2 and len(fetched) == 2


async def test_uncaptured_truncated_tail_keeps_reuse(live, monkeypatch):
    from src.context_engine.adapters import files
    path, request, calls, _ = live
    monkeypatch.setattr(files, "MAX_FILE_CHARS", 60)
    content = "\n".join(f"line {i}" for i in range(1, 101))
    path.write_text(content, encoding="utf-8")
    request = replace(request, explicit_refs=("file:synthetic.txt#L1-L100",))
    first = await deliver(request)
    path.write_text(content.replace("line 100", "TAIL 100"), encoding="utf-8")
    second = await deliver(request, first)
    assert len(calls) == 1 and second["report"]["reused"] is True


async def test_validation_exception_recompiles_without_reusing_old_message(live, monkeypatch):
    _, request, calls, _ = live
    first = await deliver(request)
    original = FileSource.fetch
    attempts = []

    async def unavailable(self, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("synthetic validation failure")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(FileSource, "fetch", unavailable)
    second = await deliver(request, first)
    assert len(calls) == 2
    assert second["message"] is not first["message"] and not second["report"].get("reused")


async def test_empty_captured_file_is_validated_when_it_disappears(live):
    path, request, calls, _ = live
    path.write_bytes(b"")
    first = await deliver(request)
    assert len(first["_file_reuse_receipts"]) == 1
    path.unlink()
    second = await deliver(request, first)
    assert len(calls) == 2 and not second["report"].get("reused")
    assert second["_file_reuse_receipts"] is None


async def test_missing_delivered_reference_recompiles_when_available(live):
    path, request, calls, fetched = live
    path.unlink()
    first = await deliver(request)
    assert first["_file_reuse_receipts"] is None
    path.write_text("available now", encoding="utf-8")
    second = await deliver(request, first)
    assert len(calls) == 2 and "available now" in str(second["message"])
    assert fetched == ["file:synthetic.txt"] * 2


async def test_legacy_report_prefix_cannot_certify_absence_of_file_receipts(live):
    _, request, calls, fetched = live
    first = await deliver(request)
    legacy = {k: v for k, v in first.items() if k != "_file_reuse_receipts"}
    legacy["report"] = {**first["report"], "sources": [{"source_type": "memory"}] * wiring.MAX_REPORT_ROWS}
    await deliver(request, legacy)
    assert len(calls) == 2
    assert fetched == ["file:synthetic.txt"] * 2  # no reconstruction/validation from report prefix
