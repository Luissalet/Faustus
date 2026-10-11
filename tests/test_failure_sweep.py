"""Every-turn failure signature sweep (#276 / radar #498)."""
from __future__ import annotations

import json

import pytest

from src import failure_sweep as fs


def test_signatures_loop_breaker_and_empty_final():
    hits = fs.signatures_for_turn(
        content="   ",
        metadata={"stop_reason": "non_progressing_loop", "run_id": "r1"},
        session_id="s1",
        message_id=9,
    )
    kinds = {h["signature"] for h in hits}
    assert "loop_breaker" in kinds
    assert "empty_final" in kinds
    assert all(h["session_id"] == "s1" and h["message_id"] == "9" for h in hits)


def test_repeated_empty_tools_and_identical_errors():
    events = [
        {"tool": "bash", "output": ""},
        {"tool": "bash", "output": "   "},
        {"tool": "bash", "result": ""},
        {"tool": "read_file", "exit_code": 1, "error": "Permission denied: /x"},
        {"tool": "read_file", "exit_code": 1, "error": "Permission denied: /x"},
        {"tool": "read_file", "exit_code": 1, "error": "Permission denied: /x"},
    ]
    hits = fs.signatures_for_turn(
        content="done",
        metadata={"tool_events": events},
        session_id="s2",
        message_id="m2",
    )
    kinds = {(h["signature"], h["tool"]) for h in hits}
    assert ("repeated_empty_tool", "bash") in kinds
    assert ("identical_tool_error", "read_file") in kinds
    # Error text must not appear in the report — only a fingerprint.
    detail = next(h["detail"] for h in hits if h["signature"] == "identical_tool_error")
    assert "Permission denied" not in detail
    assert "err=" in detail


def test_result_status_failed_counts_without_exit_code():
    """Native agent_loop persists result_status via tool_result_fields."""
    from src.tool_presentation import tool_result_fields

    raw = {"status": "failed", "output": "QA semantic failure", "ok": False}
    fields = tool_result_fields(raw)
    assert fields.get("result_status") == "failed"
    events = [
        {"tool": "qa_native_tool", "output": "QA semantic failure",
         "exit_code": None, **fields},
        {"tool": "qa_native_tool", "output": "QA semantic failure",
         "exit_code": None, **fields},
        {"tool": "qa_native_tool", "output": "QA semantic failure",
         "exit_code": None, **fields},
    ]
    hits = fs.signatures_for_turn(
        content="done",
        metadata={"tool_events": events},
        session_id="s-rs",
        message_id="m-rs",
    )
    assert ("identical_tool_error", "qa_native_tool") in {
        (h["signature"], h["tool"]) for h in hits
    }


def test_result_status_partial_not_failed_via_stdout():
    events = [
        {"tool": "bash", "result_status": "partial",
         "output": "error: looks scary but status is partial", "exit_code": None},
        {"tool": "bash", "result_status": "cancelled",
         "output": "error: cancelled", "exit_code": None},
        {"tool": "bash", "result_status": "outcome_unknown",
         "output": "error: unknown", "exit_code": None},
    ]
    hits = fs.signatures_for_turn(
        content="ok", metadata={"tool_events": events}, session_id="s", message_id=1,
    )
    assert not any(h["signature"] == "identical_tool_error" for h in hits)


def test_mcp_projection_is_complete_valid_json():
    big = {
        "days": 1, "since": "2026-10-11T00:00:00", "owner": "alice",
        "turns_scanned": 2, "hit_count": 46, "signature_count": 46,
        "rows_read": 2, "coverage": "complete", "truncated": False,
        "by_signature": [
            {"signature": f"sig_{i}", "tool": f"t{i}", "count": 3,
             "examples": [{"session_id": "s", "message_id": str(i), "run_id": ""}]}
            for i in range(46)
        ],
        "generated_at": "2026-10-11T12:00:00",
    }
    projected = fs.project_for_mcp(big, group_offset=0, group_limit=20)
    blob = json.dumps(projected, ensure_ascii=False)
    assert json.loads(blob) == projected  # round-trip, never sliced
    assert projected["signature_count"] == 46
    assert projected["groups_returned"] == 20
    assert projected["next_group_offset"] == 20
    more = fs.project_for_mcp(big, group_offset=20, group_limit=80)
    assert more["groups_returned"] == 26
    assert more["next_group_offset"] is None


def test_approval_expired_and_model_refusal():
    hits = fs.signatures_for_turn(
        content="I can't help with that request.",
        metadata={"approval": {"state": "expired"}},
        session_id="s3",
        message_id=1,
    )
    assert {h["signature"] for h in hits} >= {"approval_expired", "model_refusal"}


def test_scan_rows_groups_and_never_includes_prompt_text():
    rows = [
        {
            "role": "assistant",
            "session_id": "a",
            "id": 1,
            "content": "SECRET PROMPT LEAK?",
            "metadata": json.dumps({
                "stop_reason": "rounds_exhausted",
                "tool_events": [
                    {"tool": "bash", "exit_code": 2, "error": "boom SECRET"},
                    {"tool": "bash", "exit_code": 2, "error": "boom SECRET"},
                    {"tool": "bash", "exit_code": 2, "error": "boom SECRET"},
                ],
            }),
        },
        {
            "role": "assistant",
            "session_id": "b",
            "id": 2,
            "content": "ok",
            "metadata": {"stop_reason": "rounds_exhausted"},
        },
        {"role": "user", "content": "hi", "session_id": "a"},
    ]
    out = fs.scan_rows(rows)
    assert out["turns_scanned"] == 2
    assert out["hit_count"] >= 3
    blob = json.dumps(out)
    assert "SECRET" not in blob
    assert "PROMPT" not in blob
    by = {(g["signature"], g["tool"]): g["count"] for g in out["by_signature"]}
    assert by[("rounds_exhausted", "")] == 2
    assert by[("identical_tool_error", "bash")] == 1


def test_hub_payload_is_compact_and_keyed():
    body = fs.scan_rows([{
        "role": "assistant", "session_id": "s", "id": 1, "content": "",
        "metadata": {"stop_reason": "budget_exhausted"},
    }])
    body.update({"owner": "luis", "days": 1, "since": "2026-10-11T00:00:00"})
    payload = fs.build_hub_payload(body)
    assert payload["signature_count"] >= 1
    assert set(payload) <= set(fs._HUB_KEYS)
    assert "content" not in json.dumps(payload)


def test_report_reads_owner_sessions():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Base, ChatMessage, Session

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    db = sessionmaker(bind=engine)()
    try:
        db.add(Session(id="own1", name="own1", endpoint_url="http://x", model="m", owner="alice"))
        db.add(Session(id="other", name="other", endpoint_url="http://x", model="m", owner="bob"))
        db.add(ChatMessage(
            id="own1-1", session_id="own1", role="assistant", content="",
            meta_data=json.dumps({"stop_reason": "loop_breaker"}),
        ))
        db.add(ChatMessage(
            id="other-1", session_id="other", role="assistant", content="",
            meta_data=json.dumps({"stop_reason": "loop_breaker"}),
        ))
        db.commit()
        out = fs.report("alice", days=1, db=db)
    finally:
        db.close()
    assert out["turns_scanned"] == 1
    assert {g["signature"] for g in out["by_signature"]} >= {"loop_breaker", "empty_final"}
    assert out["owner"] == "alice"
    assert out["coverage"] == "complete"
    assert out["next_cursor"] is None


def test_report_partial_coverage_and_continue():
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Base, ChatMessage, Session

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    db = sessionmaker(bind=engine)()
    try:
        db.add(Session(id="p1", name="p1", endpoint_url="http://x", model="m", owner="alice"))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for i in range(5):
            db.add(ChatMessage(
                id=f"p1-{i}", session_id="p1", role="assistant", content="ok",
                meta_data=json.dumps({"stop_reason": "rounds_exhausted"}),
                timestamp=now - timedelta(seconds=i),
            ))
        db.commit()
        page = fs.report("alice", days=1, db=db, page_limit=2)
        assert page["coverage"] == "partial"
        assert page["rows_read"] == 2
        assert page["next_cursor"]
        rest = fs.report("alice", days=1, db=db, page_limit=10, cursor=page["next_cursor"])
        merged = fs.merge_reports(page, rest)
        assert merged["turns_scanned"] == 5
        assert merged["coverage"] == "complete"
    finally:
        db.close()


def test_cursor_preserves_microseconds_roundtrip():
    from datetime import datetime

    ts = datetime(2026, 10, 11, 12, 18, 27, 300000)
    raw = fs._encode_cursor(ts, "qa-msg-3")
    assert ".300000" in raw
    back, mid = fs._decode_cursor(raw)
    assert mid == "qa-msg-3"
    assert back == ts


def test_same_second_microsecond_pages_exhaust_full_report():
    """CHANGES r2: five rows in one second must survive page_limit + continue."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Base, ChatMessage, Session

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    db = sessionmaker(bind=engine)()
    try:
        db.add(Session(
            id="qa-precision", name="Own QA", endpoint_url="http://x",
            model="m", owner="qa-owner",
        ))
        stamp = (
            datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
            - timedelta(minutes=5)
        )
        for i in range(1, 6):
            db.add(ChatMessage(
                id=f"qa-msg-{i}", session_id="qa-precision", role="assistant",
                content="own QA response",
                meta_data=json.dumps({"stop_reason": "rounds_exhausted"}),
                timestamp=stamp + timedelta(microseconds=i * 100000),
            ))
        db.commit()
        full = fs.report("qa-owner", days=1, db=db, page_limit=100)
        first = fs.report("qa-owner", days=1, db=db, page_limit=2)
        assert full["turns_scanned"] == 5
        assert first["coverage"] == "partial"
        assert first["turns_scanned"] == 2
        assert first["next_cursor"]
        # Page-1 last row is newest-but-one → stamp+400000µs (ids desc: 5 then 4).
        assert ".400000|" in first["next_cursor"]
        decoded_ts, decoded_id = fs._decode_cursor(first["next_cursor"])
        assert decoded_id == "qa-msg-4"
        assert decoded_ts.microsecond == 400000
        second = fs.report(
            "qa-owner", days=1, db=db, page_limit=2, cursor=first["next_cursor"],
        )
        assert second["turns_scanned"] >= 1
        merged = fs.merge_reports(first, second)
        # Keep paging until complete and match the unpaginated report.
        cursor = second.get("next_cursor")
        pages = merged
        while pages.get("coverage") == "partial" and cursor:
            nxt = fs.report("qa-owner", days=1, db=db, page_limit=2, cursor=cursor)
            pages = fs.merge_reports(pages, nxt)
            cursor = nxt.get("next_cursor")
        assert pages["turns_scanned"] == full["turns_scanned"] == 5
        assert pages["coverage"] == "complete"
        assert pages["hit_count"] == full["hit_count"]
    finally:
        db.close()


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(
        fs, "report",
        lambda owner, days=1, db=None, publish=False: {
            "owner": owner or "", "days": days, "turns_scanned": 0,
            "hit_count": 0, "signature_count": 0, "by_signature": [],
            "publish": bool(publish),
        },
    )
    from routes.diagnostics_routes import setup_diagnostics_routes

    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None))
    with TestClient(app) as c:
        yield c
    settings_mod._invalidate_caches()


def test_api_failure_sweep(api_client):
    resp = api_client.get("/api/diagnostics/failure-sweep?days=7&publish=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 7
    assert body["publish"] is True
