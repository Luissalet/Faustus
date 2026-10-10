"""Voice turn latency timeline: recording, gaps, aggregation, REST and MCP.

All timestamps are synthetic; no microphone, no audio.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.voice_timeline_routes import setup_voice_timeline_routes
from src import voice_timeline as vt

T0 = 1_790_000_000_000  # 2026-09 in epoch ms
NOW = T0 + 60_000


def marks(speech_end=T0, text=400, request=450, token=1650, chunk=2100, audio=2250, **extra):
    out = {"speech_end": speech_end, "transcript_ready": speech_end + text, "request_sent": speech_end + request,
           "first_token": speech_end + token, "tts_chunk": speech_end + chunk, "first_audio": speech_end + audio}
    out.update(extra)
    return out


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "DATA_DIR", str(tmp_path))
    return tmp_path


def test_gaps_of_a_complete_turn():
    turn = vt.record_turn("s1", "t1", marks(stt_server_ms=310, tts_server_ms=120, server_ttft_ms=1100), now_ms=NOW)
    gaps = turn["gaps"]
    assert gaps["speech_to_text"] == 400
    assert gaps["text_to_first_token"] == 1250      # from transcript ready, request send included
    assert gaps["text_to_request"] == 50 and gaps["request_to_first_token"] == 1200
    assert gaps["first_token_to_audio"] == 600
    assert gaps["first_token_to_tts_chunk"] == 450 and gaps["tts_chunk_to_playback"] == 150
    assert gaps["total"] == 2250
    assert turn["complete"] is True
    assert turn["server"] == {"stt_server_ms": 310, "tts_server_ms": 120, "server_ttft_ms": 1100}


def test_missing_marks_give_null_gaps_and_a_later_call_completes_the_turn():
    first = vt.record_turn("s1", "t1", {k: v for k, v in marks().items() if k in ("speech_end", "transcript_ready")}, now_ms=NOW)
    assert first["gaps"]["speech_to_text"] == 400
    assert first["gaps"]["first_token_to_audio"] is None and first["complete"] is False
    done = vt.record_turn("s1", "t1", {"first_token": T0 + 1650, "first_audio": T0 + 2250}, now_ms=NOW)
    assert done["gaps"]["first_token_to_audio"] == 600 and done["complete"] is True
    assert len(vt.list_turns("s1")) == 1                # upserted, not duplicated


def test_impossible_or_junk_values_are_dropped_not_stored():
    turn = vt.record_turn("s1", "t1", {
        "speech_end": T0 + 5000, "transcript_ready": T0,          # ends before it starts
        "first_token": "abc", "first_audio": True, "tts_chunk": -3,
        "request_sent": 10, "stt_server_ms": -1, "tts_server_ms": 99, "bogus": 1}, now_ms=NOW)
    assert turn["gaps"]["speech_to_text"] is None               # negative order -> None
    assert set(turn["marks"]) == {"speech_end", "transcript_ready"}   # 10 is far from now: a bad clock
    assert turn["server"] == {"tts_server_ms": 99}


def test_ids_are_required_and_rows_are_capped(monkeypatch):
    with pytest.raises(ValueError):
        vt.record_turn("", "t1", marks())
    monkeypatch.setattr(vt, "MAX_ROWS", 5)
    for i in range(9):
        vt.record_turn("s1", f"t{i}", marks(), now_ms=NOW)
    ids = [t["turn_id"] for t in vt.list_turns("s1", 50)]
    assert ids[0] == "t8" and len(ids) == 5 and "t0" not in ids   # newest first, oldest pruned


def test_percentiles_and_aggregate():
    assert vt.percentile([], 50) is None
    assert vt.percentile([7], 90) == 7
    assert vt.percentile([100, 200, 300, 400, 500], 50) == 300
    assert vt.percentile([100, 200, 300, 400, 500], 90) == 460
    for i, wait in enumerate([1000, 1200, 1400, 1600, 2000]):
        vt.record_turn("s1", f"t{i}", marks(token=wait + 400), now_ms=NOW)
    vt.record_turn("s1", "partial", {"speech_end": T0, "transcript_ready": T0 + 500}, now_ms=NOW)
    agg = vt.aggregate(vt.list_turns("s1"))
    assert agg["turns"] == 6
    ttf = agg["gaps"]["text_to_first_token"]
    assert ttf["n"] == 5 and ttf["p50_ms"] == 1400 and ttf["p90_ms"] == 1840
    assert agg["gaps"]["speech_to_text"]["n"] == 6          # the partial turn still counts where it can


def test_sessions_and_owners_are_separate():
    vt.record_turn("a", "t1", marks(), owner="ana", now_ms=NOW)
    vt.record_turn("b", "t1", marks(), owner="bob", now_ms=NOW)
    assert [t["session_id"] for t in vt.list_turns("a")] == ["a"]
    assert [t["session_id"] for t in vt.list_turns(owner="bob")] == ["b"]
    assert len(vt.list_turns()) == 2


def test_nothing_on_disk_reads_as_empty(tmp_path):
    assert vt.list_turns() == []
    assert vt.session_report()["aggregate"]["turns"] == 0
    assert not (tmp_path / "voice_timeline.db").exists()   # reading never creates the file


@pytest.fixture
def client():
    app = FastAPI()

    @app.middleware("http")
    async def _user(request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(setup_voice_timeline_routes())
    return TestClient(app)


def _live_marks(**kw):
    import time
    base = int(time.time() * 1000) - 5000
    return marks(speech_end=base, **kw)


def test_api_record_list_and_aggregate(client):
    posted = client.post("/api/voice/timeline", json={"session_id": "s1", "turn_id": "t1", **_live_marks(), "junk": 1})
    assert posted.status_code == 200
    assert posted.json()["turn"]["gaps"]["first_token_to_audio"] == 600
    client.post("/api/voice/timeline", json={"session_id": "s1", "turn_id": "t2", **_live_marks(token=2000, chunk=2300, audio=2500)})
    client.post("/api/voice/timeline", json={"session_id": "other", "turn_id": "t1", **_live_marks()})
    body = client.get("/api/voice/timeline", params={"session_id": "s1"}).json()
    assert [t["turn_id"] for t in body["turns"]] == ["t2", "t1"]
    assert body["aggregate"]["turns"] == 2
    assert body["aggregate"]["gaps"]["first_token_to_audio"]["p50_ms"] == 550
    everywhere = client.get("/api/voice/timeline/aggregate").json()
    assert everywhere["turns"] == 3 and everywhere["session_id"] is None


def test_api_rejects_a_turn_without_ids(client):
    assert client.post("/api/voice/timeline", json={"turn_id": "t1"}).status_code == 422
    assert client.post("/api/voice/timeline", json={"session_id": "s", "turn_id": ""}).status_code == 422
    assert client.get("/api/voice/timeline", params={"limit": 0}).status_code == 422


def test_api_scopes_by_user(client):
    client.post("/api/voice/timeline", json={"session_id": "a", "turn_id": "1", **_live_marks()}, headers={"X-Test-User": "ana"})
    client.post("/api/voice/timeline", json={"session_id": "b", "turn_id": "1", **_live_marks()}, headers={"X-Test-User": "bob"})
    ana = client.get("/api/voice/timeline", headers={"X-Test-User": "ana"}).json()
    assert [t["session_id"] for t in ana["turns"]] == ["a"]


def test_mcp_tool_reads_the_same_rows():
    pytest.importorskip("mcp")
    import mcp_servers.harness_server as srv

    tools = asyncio.run(srv.list_tools())
    assert "voice_turn_timeline" in [t.name for t in tools]
    vt.record_turn("s1", "t1", marks(), owner="ana", now_ms=NOW)
    vt.record_turn("s2", "t1", marks(), now_ms=NOW)
    out = json.loads(asyncio.run(srv.call_tool("voice_turn_timeline", {"session_id": "s1"}))[0].text)
    assert [t["session_id"] for t in out["turns"]] == ["s1"]
    assert "owner" not in out["turns"][0]
    assert out["turns"][0]["gaps"]["total"] == 2250
    assert out["aggregate"]["gaps"]["speech_to_text"]["p50_ms"] == 400
    allturns = json.loads(asyncio.run(srv.call_tool("voice_turn_timeline", {"limit": "bad"}))[0].text)
    assert len(allturns["turns"]) == 2


def test_same_ids_from_two_users_never_share_or_change_a_row(client):
    """Review 182 r1 P1: Bob posting Alice's ids must not read, merge into or alter her turn."""
    ids = {"session_id": "shared", "turn_id": "t1"}
    alice = {"X-Test-User": "alice"}
    bob = {"X-Test-User": "bob"}
    base = _live_marks()
    client.post("/api/voice/timeline", json={**ids, "speech_end": base["speech_end"], "transcript_ready": base["transcript_ready"]}, headers=alice)
    answer = client.post("/api/voice/timeline", json={**ids, "transcript_ready": base["speech_end"] + 1800}, headers=bob).json()["turn"]
    assert answer["owner"] == "bob"
    assert "speech_end" not in answer["marks"]            # Alice's mark is not returned to Bob
    mine = client.get("/api/voice/timeline", headers=alice).json()["turns"]
    assert len(mine) == 1 and mine[0]["owner"] == "alice"
    assert mine[0]["gaps"]["speech_to_text"] == 400        # unchanged by Bob's post
    theirs = client.get("/api/voice/timeline", headers=bob).json()["turns"]
    assert len(theirs) == 1 and theirs[0]["owner"] == "bob"
    # The same user still completes their own turn.
    again = client.post("/api/voice/timeline", json={**ids, "first_token": base["speech_end"] + 1650}, headers=alice).json()["turn"]
    assert again["owner"] == "alice" and again["gaps"]["speech_to_text"] == 400 and "first_token" in again["marks"]


def test_store_keys_a_turn_by_owner():
    vt.record_turn("s", "t", {"speech_end": T0}, owner="ana", now_ms=NOW)
    vt.record_turn("s", "t", {"transcript_ready": T0 + 900}, owner="bob", now_ms=NOW)
    assert vt.list_turns(owner="ana")[0]["marks"] == {"speech_end": T0}
    assert vt.list_turns(owner="bob")[0]["marks"] == {"transcript_ready": T0 + 900}
