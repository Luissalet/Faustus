"""Auto calibration report (OBJ-58, #228): pairs each recorded reasoning mode with
what the person wrote next and counts corrections per mode, source and reason."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatMessage, Session
from src import think_calibration as tc
from src.think_mode import is_correction


def _a(mode, reasons=(), source="rule", content="answer"):
    return {"role": "assistant", "content": content,
            "metadata": json.dumps({"think_mode": {"mode": mode, "source": source, "reasons": list(reasons)}})}


def _u(text):
    return {"role": "user", "content": text}


def _rows(session, *msgs):
    return [{"session_id": session, **m} for m in msgs]


def test_is_correction_spots_both_languages_and_ignores_thanks():
    assert is_correction("no, eso esta mal")
    assert is_correction("that's wrong, try again")
    assert not is_correction("gracias, perfecto")


def test_each_turn_is_paired_with_the_next_user_message_in_its_session():
    rows = (_rows("s1", _u("hola"), _a("fast", ["small_talk"]), _u("no, eso está mal"),
                  _a("think", ["prior_correction"]), _u("vale, gracias"))
            + _rows("s2", _u("x"), _a("deep", ["asked_depth"])))
    recs = tc.outcomes(rows)
    assert [(r["mode"], r["corrected"]) for r in recs] == [("fast", True), ("think", False), ("deep", None)]


def test_a_low_effort_think_counts_as_light():
    row = {"session_id": "s", "role": "assistant", "content": "x",
           "metadata": {"think_mode": {"mode": "think", "effort": "low", "source": "rule", "reasons": ["light"]}}}
    assert tc.outcomes([row])[0]["mode"] == "light"


def test_messages_without_think_mode_and_malformed_metadata_are_skipped():
    rows = _rows("s", _u("a"), {"role": "assistant", "content": "x", "metadata": "{not json"},
                 {"role": "assistant", "content": "y", "metadata": None}, _u("no, mal")) + ["junk", None]
    assert tc.outcomes(rows) == []


def test_summary_rates_and_flags_a_reason_corrected_far_above_the_average():
    recs = ([{"mode": "fast", "source": "rule", "reasons": ["small_talk"], "empty": False, "corrected": True}] * 6
            + [{"mode": "fast", "source": "rule", "reasons": ["small_talk"], "empty": False, "corrected": False}] * 4
            + [{"mode": "think", "source": "rule", "reasons": ["coding"], "empty": False, "corrected": False}] * 30
            + [{"mode": "think", "source": "explicit", "reasons": [], "empty": True, "corrected": True}] * 3)
    out = tc.summarize(recs)
    assert out["turns"]["turns"] == 43
    assert out["auto"]["followed"] == 40 and out["auto"]["corrected"] == 6
    assert out["by_reason"]["small_talk"]["correction_rate"] == 0.6
    assert out["by_source"]["explicit"]["empty_rate"] == 1.0
    assert [f["key"] for f in out["flags"]][:1] == ["small_talk"]
    assert all(f["key"] != "coding" for f in out["flags"])
    # explicit choices are not Auto's: they never enter the reason buckets
    assert "(none)" not in out["by_reason"]


def test_a_bucket_with_few_followed_turns_is_never_flagged():
    recs = ([{"mode": "fast", "source": "rule", "reasons": ["light"], "empty": False, "corrected": True}] * 3
            + [{"mode": "think", "source": "rule", "reasons": ["coding"], "empty": False, "corrected": False}] * 20)
    assert tc.summarize(recs)["flags"] == []


def test_empty_history_gives_an_empty_report():
    out = tc.summarize([])
    assert out["turns"]["turns"] == 0 and out["auto"]["correction_rate"] is None and out["flags"] == []


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _store(db, owner, sid, msgs, start):
    db.add(Session(id=sid, name=sid, endpoint_url="http://x", model="m", owner=owner))
    for i, m in enumerate(msgs):
        db.add(ChatMessage(id=f"{sid}-{i}", session_id=sid, role=m["role"], content=m["content"],
                           meta_data=m.get("metadata"), timestamp=start + timedelta(seconds=i)))
    db.commit()


def test_report_reads_only_the_owner_sessions_inside_the_window(db, tmp_path):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _store(db, "admin", "a1", [_u("hola"), _a("fast", ["small_talk"]), _u("no, eso está mal")], now)
    _store(db, "other", "o1", [_u("hola"), _a("deep", ["asked_depth"]), _u("try again")], now)
    _store(db, "admin", "old", [_u("hola"), _a("think", ["coding"]), _u("gracias")], now - timedelta(days=60))
    out = tc.report("admin", 30, db=db, battery_dir=str(tmp_path))
    assert out["turns"]["turns"] == 1 and out["by_mode"]["fast"]["corrected"] == 1
    assert "deep" not in out["by_mode"] and "think" not in out["by_mode"]
    assert out["days"] == 30 and out["rows_read"] == 3 and out["truncated"] is False
    assert tc.report("admin", 365, db=db, battery_dir=str(tmp_path))["turns"]["turns"] == 2
    assert tc.report("admin", "nonsense", db=db, battery_dir=str(tmp_path))["days"] == tc.DEFAULT_DAYS
    assert tc.session_owners(db=db) == ["admin", "other"]


def test_harness_mcp_tool_refuses_to_guess_between_several_users(monkeypatch):
    pytest.importorskip("mcp")
    import mcp_servers.harness_server as srv
    for key in srv._OWNER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tc, "session_owners", lambda: ["admin", "other"])
    out = json.loads(asyncio.run(srv.call_tool("think_mode_calibration", {}))[0].text)
    assert "ODYSSEUS_MCP_HARNESS_OWNER" in out["error"]

    seen = {}
    monkeypatch.setenv("ODYSSEUS_MCP_HARNESS_OWNER", "admin")
    monkeypatch.setattr(tc, "report", lambda owner, days: seen.update(owner=owner, days=days) or {"ok": True})
    out = json.loads(asyncio.run(srv.call_tool("think_mode_calibration", {"days": 7}))[0].text)
    assert out == {"ok": True} and seen == {"owner": "admin", "days": 7}


def test_route_reports_for_the_signed_in_user(monkeypatch):
    pytest.importorskip("starlette.testclient")
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import src.auth_helpers as auth
    diag = pytest.importorskip("routes.diagnostics_routes")

    seen = {}
    monkeypatch.setattr(auth, "effective_user", lambda request: "admin")
    monkeypatch.setattr(tc, "report", lambda owner, days: seen.update(owner=owner, days=days) or {"flags": []})
    app = FastAPI()
    app.include_router(diag.setup_diagnostics_routes(rag_manager=None, rag_available=False,
                                                     research_handler=None, memory_vector=None))
    res = TestClient(app).get("/api/diagnostics/think-mode?days=14")
    assert res.status_code == 200 and res.json() == {"flags": []}
    assert seen == {"owner": "admin", "days": 14}

def test_battery_sessions_carry_the_battery_verdict(db, tmp_path):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _store(db, "admin", "ev1", [_u("cuenta los enlaces"), _a("think", ["lookup"])], now)
    _store(db, "admin", "ev2", [_u("suma 2 y 2"), _a("fast", ["small_talk"])], now)
    _store(db, "admin", "chat", [_u("hola"), _a("fast", ["small_talk"])], now)
    (tmp_path / "20261010-120000.json").write_text(json.dumps({"tasks": [
        {"id": "links", "ok": True, "session": "ev1"}, {"id": "sum", "ok": False, "session": "ev2"}]}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{nope", encoding="utf-8")
    assert tc.battery_verdicts(30, str(tmp_path)) == {"ev1": True, "ev2": False}
    out = tc.report("admin", 30, db=db, battery_dir=str(tmp_path))["battery"]
    assert out["turns"] == {"turns": 2, "passed": 1, "pass_rate": 0.5}
    assert out["by_mode"]["fast"]["pass_rate"] == 0.0 and out["by_mode"]["think"]["pass_rate"] == 1.0
    assert out["by_reason"]["lookup"]["passed"] == 1
    assert tc.battery_verdicts(30, str(tmp_path / "missing")) == {}


def test_explicit_choices_never_flag_an_auto_mode():
    """Review #228: 20 corrected turns where the person picked fast by hand
    must not make Auto's fast look wrong."""
    recs = ([{"mode": "fast", "source": "rule", "reasons": ["small_talk"], "empty": False, "corrected": False}] * 10
            + [{"mode": "think", "source": "rule", "reasons": ["coding"], "empty": False, "corrected": False}] * 30
            + [{"mode": "fast", "source": "explicit", "reasons": [], "empty": False, "corrected": True}] * 20)
    out = tc.summarize(recs)
    assert out["by_mode"]["fast"]["correction_rate"] == 0.667      # all activity still reported
    assert out["auto_by_mode"]["fast"]["correction_rate"] == 0.0
    assert out["flags"] == []


def test_auto_mode_really_corrected_is_flagged():
    recs = ([{"mode": "fast", "source": "rule", "reasons": ["short"], "empty": False, "corrected": True}] * 6
            + [{"mode": "fast", "source": "rule", "reasons": ["short"], "empty": False, "corrected": False}] * 4
            + [{"mode": "think", "source": "rule", "reasons": ["coding"], "empty": False, "corrected": False}] * 40)
    keys = {(f["kind"], f["key"]) for f in tc.summarize(recs)["flags"]}
    assert ("mode", "fast") in keys and ("reason", "short") in keys
