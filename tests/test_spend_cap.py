"""The daily hard cap on paid-provider spend (src/spend_cap.py)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from src import spend_cap

PAID = "https://api.openai.com/v1/chat/completions"
PAID2 = "https://openrouter.ai/api/v1/chat/completions"
LOCAL = "http://127.0.0.1:8081/v1/chat/completions"
DAY1 = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
DAY2 = DAY1 + timedelta(days=1)


@pytest.fixture()
def cap(monkeypatch, tmp_path):
    monkeypatch.setattr(spend_cap, "STATE_FILE", str(tmp_path / "spend_cap.json"))
    values = {"paid_spend_cap_enabled": True, "paid_spend_cap_daily_usd": 1.0,
              "paid_spend_cap_daily_tokens": 1000}
    monkeypatch.setattr(spend_cap, "_setting", lambda key, default: values.get(key, default))
    events = []
    from src import notifications
    monkeypatch.setattr(notifications, "emit", lambda *a, **k: events.append((a, k)))
    spend_cap.values = values
    spend_cap.events = events
    return values


def test_defaults_are_on_with_sensible_limits():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["paid_spend_cap_enabled"] is True
    assert DEFAULT_SETTINGS["paid_spend_cap_daily_usd"] == 5.0
    assert DEFAULT_SETTINGS["paid_spend_cap_daily_tokens"] == 2_000_000


def test_paid_vs_local_classification():
    assert spend_cap.is_paid_endpoint(PAID)
    assert not spend_cap.is_paid_endpoint(LOCAL)
    assert not spend_cap.is_paid_endpoint("http://localhost:11434/v1")
    assert not spend_cap.is_paid_endpoint("faustus-cli://claude")
    assert not spend_cap.is_paid_endpoint("")
    assert not spend_cap.is_paid_endpoint(PAID2, "meta-llama/llama-3.3-70b-instruct:free")
    assert spend_cap.is_paid_endpoint(PAID2, "meta-llama/llama-3.3-70b-instruct")


def test_local_calls_are_never_counted_or_refused(cap):
    spend_cap.record(LOCAL, {"input_tokens": 10**9, "cost_usd": 999}, now=DAY1)
    assert spend_cap.status(now=DAY1)["paid_calls"] == 0
    spend_cap.gate(LOCAL, now=DAY1)


def test_cost_and_tokens_accumulate_per_provider(cap):
    spend_cap.record(PAID, {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.25}, now=DAY1)
    spend_cap.record(PAID2, {"total_tokens": 40, "cost_usd": 0.1}, now=DAY1)
    st = spend_cap.status(now=DAY1)
    assert st["spent_usd"] == pytest.approx(0.35)
    assert st["tokens_today"] == 190
    assert len(st["by_provider"]) == 2
    assert st["reached"] is False


def test_unknown_cost_counts_tokens_only_and_is_reported(cap):
    spend_cap.record(PAID, {"input_tokens": 10, "output_tokens": 5}, now=DAY1)
    spend_cap.record(PAID, {}, now=DAY1)
    st = spend_cap.status(now=DAY1)
    assert st["spent_usd"] == 0.0
    assert st["tokens_today"] == 15
    assert st["unknown_cost_calls"] == 2


def test_refuses_at_usd_cap_with_typed_error(cap):
    spend_cap.record(PAID, {"cost_usd": 1.0, "input_tokens": 1}, now=DAY1)
    with pytest.raises(spend_cap.SpendCapReached) as err:
        spend_cap.gate(PAID, now=DAY1)
    e = err.value
    assert e.status_code == 429 and e.error_class == "spend_cap.reached"
    assert e.limit_kind == "usd" and e.spent_usd == pytest.approx(1.0) and e.limit_usd == 1.0
    assert e.resets_at.startswith("2026-10-1")
    assert "local models are unaffected" in e.message
    spend_cap.gate(LOCAL, now=DAY1)  # local still fine


def test_refuses_at_token_cap_even_without_prices(cap):
    spend_cap.record(PAID, {"input_tokens": 600, "output_tokens": 400}, now=DAY1)
    with pytest.raises(spend_cap.SpendCapReached) as err:
        spend_cap.gate(PAID, now=DAY1)
    assert err.value.limit_kind == "tokens"


def test_zero_limit_turns_that_limit_off(cap):
    cap["paid_spend_cap_daily_tokens"] = 0
    cap["paid_spend_cap_daily_usd"] = 0
    spend_cap.record(PAID, {"input_tokens": 10**8, "cost_usd": 500}, now=DAY1)
    spend_cap.gate(PAID, now=DAY1)


def test_disabled_never_refuses_but_still_records(cap):
    cap["paid_spend_cap_enabled"] = False
    spend_cap.record(PAID, {"cost_usd": 50}, now=DAY1)
    spend_cap.gate(PAID, now=DAY1)
    assert spend_cap.status(now=DAY1)["spent_usd"] == 50


def test_day_rollover_resets_the_account(cap):
    spend_cap.record(PAID, {"cost_usd": 2.0}, now=DAY1)
    with pytest.raises(spend_cap.SpendCapReached):
        spend_cap.gate(PAID, now=DAY1)
    spend_cap.gate(PAID, now=DAY2)
    st = spend_cap.status(now=DAY2)
    assert st["spent_usd"] == 0 and st["paid_calls"] == 0 and st["day"] != spend_cap.status(now=DAY1)["day"]


def test_one_notification_per_day(cap):
    spend_cap.record(PAID, {"cost_usd": 2.0}, now=DAY1)
    for _ in range(3):
        with pytest.raises(spend_cap.SpendCapReached):
            spend_cap.gate(PAID, now=DAY1)
    assert len(spend_cap.events) == 1
    assert spend_cap.events[0][1]["data"]["kind"] == "spend_cap"
    spend_cap.record(PAID, {"cost_usd": 2.0}, now=DAY2)
    spend_cap.record(PAID, {"cost_usd": 2.0}, now=DAY2)
    with pytest.raises(spend_cap.SpendCapReached):
        spend_cap.gate(PAID, now=DAY2)
    assert len(spend_cap.events) == 2


def test_corrupt_state_file_starts_fresh(cap, tmp_path):
    (tmp_path / "spend_cap.json").write_text("{not json", encoding="utf-8")
    spend_cap.gate(PAID, now=DAY1)
    assert spend_cap.status(now=DAY1)["spent_usd"] == 0


# --- the choke point in src/llm_core.py ---------------------------------------

def _drain(agen):
    async def run():
        return [c async for c in agen]
    return asyncio.run(run())


# The async/stream choke-point tests (per-attempt gate, cache, receipts) run against
# a real HTTP fixture in tests/test_spend_cap_attempts.py.


def test_sync_call_is_gated(cap):
    from src import llm_core
    spend_cap.record(PAID, {"cost_usd": 2.0})
    with pytest.raises(spend_cap.SpendCapReached):
        llm_core.llm_call(PAID, "m", [{"role": "user", "content": "x"}])


# --- API ----------------------------------------------------------------------

TOOL = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


@pytest.fixture()
def client(cap, monkeypatch):
    from routes.spend_cap_routes import setup_spend_cap_routes
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_spend_cap_routes())
    return TestClient(app)


def test_api_status(client):
    spend_cap.record(PAID, {"cost_usd": 0.3, "input_tokens": 7})
    body = client.get("/api/spend-cap", headers=TOOL).json()
    assert body["enabled"] is True and body["limit_usd"] == 1.0 and body["limit_tokens"] == 1000
    assert body["spent_usd"] == pytest.approx(0.3) and body["tokens_today"] == 7
    assert body["by_provider"][0]["calls"] == 1 and body["resets_at"]


def test_api_patch_writes_the_settings(client, monkeypatch):
    import src.settings as settings
    seen = {}
    monkeypatch.setattr(settings, "update_settings", lambda patch, **k: seen.update(patch) or {})
    resp = client.patch("/api/spend-cap", json={"enabled": False, "daily_usd": 9.5, "daily_tokens": 0})
    assert resp.status_code == 200
    assert seen == {"paid_spend_cap_enabled": False, "paid_spend_cap_daily_usd": 9.5,
                    "paid_spend_cap_daily_tokens": 0}
    assert client.patch("/api/spend-cap", json={}).status_code == 400
    assert client.post("/api/spend-cap", json={"daily_usd": -1}).status_code == 422


def test_api_patch_is_human_only(cap, monkeypatch):
    from routes.spend_cap_routes import setup_spend_cap_routes
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_spend_cap_routes())
    c = TestClient(app)
    assert c.patch("/api/spend-cap", json={"enabled": False}, headers=TOOL).status_code in (401, 403)


# --- MCP ----------------------------------------------------------------------

def test_mcp_spend_cap_status_tool(cap):
    pytest.importorskip("mcp")
    import mcp_servers.harness_server as srv
    assert "spend_cap_status" in [t.name for t in asyncio.run(srv.list_tools())]
    spend_cap.record(PAID, {"cost_usd": 0.4})
    out = json.loads(asyncio.run(srv.call_tool("spend_cap_status", {}))[0].text)
    assert out["spent_usd"] == pytest.approx(0.4) and out["enabled"] is True


def test_studio_check():
    import shutil
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    node = shutil.which("node")
    if not node or not (root / "node_modules" / "esbuild").exists():
        pytest.skip("node or esbuild not available")
    done = subprocess.run([node, "studio/checks/spend-cap.check.mjs"], cwd=root,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
