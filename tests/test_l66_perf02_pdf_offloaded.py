"""L66/PERF-02 — a large PDF attachment must not hold the event loop.

Before this fix, `ChatHandler.preprocess_message` called
`document_processor.build_user_content` (which, for a PDF attachment, calls
the synchronous pypdf-based `_process_pdf`) directly inside the request
coroutine. That is CPU/IO-bound synchronous work running straight on the
event loop: while it runs, nothing else scheduled on that loop — a
healthcheck poll, another session's cancel — gets to run either.

This test proves the opposite: with `_process_pdf` made artificially slow,
a concurrent "healthcheck" coroutine on the SAME event loop still completes
promptly instead of being stuck behind it, because `build_user_content` now
runs via `asyncio.to_thread`.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src import document_processor as dp
from src.chat_handler import ChatHandler
from src.upload_handler import UploadHandler

SLOW_SECONDS = 0.35


def _make_pdf_upload_store(tmp_path):
    upload_dir = tmp_path / "uploads"
    dated = upload_dir / "2026" / "06" / "01"
    dated.mkdir(parents=True)
    pdf_id = "f" * 32 + ".pdf"  # must be valid hex per UPLOAD_ID_RE
    pdf_path = dated / pdf_id
    pdf_path.write_bytes(b"%PDF-1.4 fake pdf bytes for a test\n%%EOF")
    index = {
        "carol:hpdf": {
            "id": pdf_id,
            "path": str(pdf_path),
            "mime": "application/pdf",
            "size": pdf_path.stat().st_size,
            "name": "big-scan.pdf",
            "original_name": "big-scan.pdf",
            "owner": "carol",
        }
    }
    import json
    (upload_dir / "uploads.json").write_text(json.dumps(index), encoding="utf-8")
    return upload_dir, pdf_id


@pytest.mark.asyncio
async def test_a_healthcheck_task_is_not_stuck_behind_pdf_extraction(tmp_path, monkeypatch):
    upload_dir, pdf_id = _make_pdf_upload_store(tmp_path)
    handler = UploadHandler(str(tmp_path), str(upload_dir))
    monkeypatch.setattr("src.chat_handler.UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "vision_enabled" else default,
    )

    def _slow_process_pdf(path, owner=None, evidence_out=None, allow_vision=True):
        # A real, synchronous, CPU-holding sleep — the same shape a big
        # pypdf parse or a page-by-page text extraction actually has.
        time.sleep(SLOW_SECONDS)
        return "\n\n[PDF content]: extracted text[PDF summary: pages=1, text_pages=1, needs_ocr=false]"

    monkeypatch.setattr(dp, "_process_pdf", _slow_process_pdf)

    chat_handler = ChatHandler(
        session_manager=None, memory_manager=None, chat_processor=None,
        research_handler=None, preset_manager=None, upload_handler=handler,
    )
    # id=None: skip the PDF-form auto-doc-creation branch (DB-backed, not the
    # concern of this test) and go straight to the plain _process_pdf call.
    sess = SimpleNamespace(id=None, owner="carol", model="text-model", endpoint_url="")

    healthcheck_finished_at: dict = {}
    started_at = time.monotonic()

    async def fake_healthcheck_poll():
        # A real healthcheck is near-instant; a few event-loop turns of
        # "asleep" is enough to prove it was actually scheduled promptly,
        # not queued behind a hundreds-of-ms synchronous call.
        for _ in range(3):
            await asyncio.sleep(0)
        healthcheck_finished_at["t"] = time.monotonic()
        return "ok"

    pdf_task = asyncio.create_task(
        chat_handler.preprocess_message("please read this pdf", [pdf_id], sess, auto_opened_docs=[])
    )
    health_task = asyncio.create_task(fake_healthcheck_poll())

    await health_task
    # The healthcheck must finish well before the slow PDF extraction does —
    # it was never blocked waiting for the event loop to come back from a
    # synchronous call.
    assert healthcheck_finished_at["t"] - started_at < SLOW_SECONDS / 2

    _enhanced, user_content, _text_ctx, _yt, attachment_meta = await pdf_task
    assert attachment_meta and attachment_meta[0]["id"] == pdf_id
    assert "extracted text" in user_content
