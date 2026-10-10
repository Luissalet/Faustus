"""Daily paid spend cap: per-attempt gate, cache, receipts, streams, classification,
cross-process safety and the human-only settings (src/spend_cap.py)."""
import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core import middleware
from src import spend_cap

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cap(monkeypatch, tmp_path):
    monkeypatch.setattr(spend_cap, "STATE_FILE", str(tmp_path / "spend_cap.json"))
    values = {"paid_spend_cap_enabled": True, "paid_spend_cap_daily_usd": 1.0,
              "paid_spend_cap_daily_tokens": 1000}
    monkeypatch.setattr(spend_cap, "_setting", lambda key, default: values.get(key, default))
    from src import notifications
    monkeypatch.setattr(notifications, "emit", lambda *a, **k: None)

    async def instant(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)
    return values


@pytest.fixture()
def api(cap, monkeypatch):
    """A real local HTTP provider, classified as a configured paid API endpoint."""
    state = SimpleNamespace(scenario="normal", calls=[], url="", other=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append(state.scenario)
            sc = state.scenario
            if sc == "failed_nousage":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"busy")
                return
            if sc == "failed_usage":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": "billed failure"},
                                             "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                                                       "cost": 0.2}}).encode())
                return
            if sc == "truncated_after_usage" and len(state.calls) == 1:
                # a 200 stream that delivers a cost receipt and no text, then the
                # transport dies before the terminal chunk
                raw = ("data: " + json.dumps({"choices": [], "usage": {
                    "prompt_tokens": 1, "completion_tokens": 0, "cost": 1.0}}) + "\n\n").encode()
                self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                                 b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                                 + hex(len(raw))[2:].encode() + b"\r\n" + raw + b"\r\n")
                self.wfile.flush()
                self.close_connection = True
                return
            if sc == "retry" and len(state.calls) == 1:
                # another completed paid request reaches the cap while this one is rejected
                spend_cap.record(state.url, {"input_tokens": 1, "cost_usd": 1.0})
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(b"busy")
                return
            usage = {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.2}
            if sc == "free":
                usage = {"prompt_tokens": 600, "completion_tokens": 0, "cost": 0.0}
            elif sc == "nocost":
                usage = {"prompt_tokens": 600, "completion_tokens": 0}
            self.send_response(200)
            if data.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                frames = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
                          {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}]
                if sc == "duplicate":
                    frames.append(frames[-1])
                for frame in frames:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"model": "qa", "usage": usage,
                                             "choices": [{"message": {"role": "assistant",
                                                                      "content": "ok"}}]}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    prefix = f"http://127.0.0.1:{server.server_port}"
    from src import model_context
    monkeypatch.setattr(model_context, "_configured_endpoint_kind",
                        lambda url: "api" if str(url).startswith(prefix) else None)
    yield state
    server.shutdown()


def _msgs():
    return [{"role": "user", "content": str(uuid.uuid4())}]


def _acall(url, messages=None, **kw):
    from src import llm_core
    kw.setdefault("max_retries", 3)
    return asyncio.run(llm_core.llm_call_async(url, "qa", messages or _msgs(), timeout=10, **kw))


def _stream(url, **kw):
    from src import llm_core

    async def run():
        return [c async for c in llm_core.stream_llm(url, "qa", _msgs(), timeout=10, **kw)]
    return asyncio.run(run())


# --- 2. the gate sits before every attempt, after the cache -------------------

def test_async_retry_is_gated_before_the_second_send(api):
    api.scenario = "retry"
    with pytest.raises(spend_cap.SpendCapReached):
        _acall(api.url)
    assert len(api.calls) == 1  # the retry was never sent


def test_stream_retry_is_gated_before_the_second_send(api):
    api.scenario = "retry"
    chunks = _stream(api.url, max_retries=3)
    assert len(api.calls) == 1
    errors = [c for c in chunks if c.startswith("event: error")]
    assert errors and json.loads(errors[-1].split("data: ", 1)[1])["error_class"] == "spend_cap.reached"


def test_stream_receipt_is_booked_before_the_retry_is_gated(api):
    api.scenario = "truncated_after_usage"
    chunks = _stream(api.url, max_retries=2)
    assert len(api.calls) == 1  # the second POST was never sent
    status = spend_cap.status()
    assert status["paid_calls"] == 1 and status["spent_usd"] == pytest.approx(1.0)
    errors = [c for c in chunks if c.startswith("event: error")]
    assert errors and json.loads(errors[-1].split("data: ", 1)[1])["error_class"] == "spend_cap.reached"


def test_stream_http_error_with_a_usage_receipt_is_booked(api):
    api.scenario = "failed_usage"
    chunks = _stream(api.url, max_retries=1)
    assert any(c.startswith("event: error") for c in chunks)
    status = spend_cap.status()
    assert status["paid_calls"] >= 1
    assert status["spent_usd"] == pytest.approx(0.2 * status["paid_calls"])
    assert status["tokens_today"] == 10 * status["paid_calls"]


def test_stream_http_error_without_a_receipt_books_nothing(api):
    api.scenario = "failed_nousage"
    _stream(api.url, max_retries=1)
    assert spend_cap.status()["paid_calls"] == 0


def test_cache_hits_are_neither_refused_nor_counted(api):
    messages = _msgs()
    assert _acall(api.url, messages) == "ok"
    assert _acall(api.url, messages) == "ok"
    assert len(api.calls) == 1
    assert spend_cap.status()["paid_calls"] == 1
    spend_cap.record(api.url, {"cost_usd": 5.0})  # the cap is now reached
    assert _acall(api.url, messages) == "ok"  # still served from the cache
    assert len(api.calls) == 1
    with pytest.raises(spend_cap.SpendCapReached):
        _acall(api.url)  # a genuinely new request is refused


def test_sync_call_gates_after_the_cache(api):
    from src import llm_core
    messages = _msgs()
    assert llm_core.llm_call(api.url, "qa", messages, timeout=10) == "ok"
    spend_cap.record(api.url, {"cost_usd": 5.0})
    assert llm_core.llm_call(api.url, "qa", messages, timeout=10) == "ok"
    assert len(api.calls) == 1
    with pytest.raises(spend_cap.SpendCapReached):
        llm_core.llm_call(api.url, "qa", _msgs(), timeout=10)
    assert len(api.calls) == 1


def test_cli_and_local_are_never_paid_calls(cap):
    from src import model_context
    assert not spend_cap.is_paid_endpoint("faustus-cli://claude")
    assert model_context.is_local_endpoint("http://192.168.0.236:8002/v1")
    assert not spend_cap.is_paid_endpoint("http://192.168.0.236:8002/v1/chat/completions", "glm")


# --- 4. receipts and stream frames -------------------------------------------

def test_failed_response_with_a_usage_receipt_is_booked(api):
    api.scenario = "failed_usage"
    with pytest.raises(HTTPException):
        _acall(api.url, max_retries=1)
    st = spend_cap.status()
    assert st["paid_calls"] == 1 and st["spent_usd"] == pytest.approx(0.2) and st["tokens_today"] == 10


def test_failed_response_without_a_receipt_books_nothing(api):
    from src import llm_core

    class R:
        status_code = 503
        text = "busy"

        def json(self):
            raise ValueError("no json")
    llm_core._cap_book_failed_response(api.url, "qa", "openai", R())
    assert spend_cap.status()["paid_calls"] == 0


def test_duplicate_final_usage_frame_is_not_summed(api):
    api.scenario = "duplicate"
    _stream(api.url)
    st = spend_cap.status()
    assert st["paid_calls"] == 1 and st["spent_usd"] == pytest.approx(0.2) and st["tokens_today"] == 10


def test_ledger_overwrites_frames_of_one_attempt_but_books_new_attempts(cap):
    url = "https://billing-fixture.example/v1"
    ledger = spend_cap.AttemptLedger(url, "m")
    ledger.begin_attempt()
    ledger.frame({"input_tokens": 5})
    ledger.frame({"input_tokens": 5, "output_tokens": 5, "cost_usd": 0.2})
    ledger.frame({"input_tokens": 5, "output_tokens": 5, "cost_usd": 0.2})  # cumulative repeat
    ledger.begin_attempt()  # a genuinely new attempt
    ledger.frame({"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.3})
    ledger.finish(errored=False)
    st = spend_cap.status()
    assert st["paid_calls"] == 2 and st["spent_usd"] == pytest.approx(0.5) and st["tokens_today"] == 12


def test_unknown_cost_stays_honest(api):
    api.scenario = "nocost"
    _acall(api.url)
    st = spend_cap.status()
    assert st["spent_usd"] == 0 and st["unknown_cost_calls"] == 1 and st["tokens_today"] == 600


# --- 5. classification --------------------------------------------------------

def test_provider_reporting_zero_cost_is_not_blocked_by_the_token_cap(api):
    api.scenario = "free"
    for _ in range(4):  # 4 x 600 tokens would pass 1000 if they counted
        assert _acall(api.url) == "ok"
    st = spend_cap.status()
    assert st["spent_usd"] == 0 and st["tokens_today"] == 2400
    assert st["tokens_counted_for_cap"] == 0 and st["unknown_cost_calls"] == 0 and not st["reached"]


def test_token_cap_applies_to_paid_calls_of_unknown_price(api):
    api.scenario = "nocost"
    _acall(api.url)
    _acall(api.url)
    with pytest.raises(spend_cap.SpendCapReached) as err:
        _acall(api.url)
    assert err.value.limit_kind == "tokens"


def test_token_cap_covers_all_paid_tokens_when_the_dollar_limit_is_off(cap):
    cap["paid_spend_cap_daily_usd"] = 0
    url = "https://billing-fixture.example/v1"
    spend_cap.record(url, {"input_tokens": 1200, "cost_usd": 0.0})
    with pytest.raises(spend_cap.SpendCapReached):
        spend_cap.gate(url)


def test_classification_rules(monkeypatch):
    url = "https://billing-fixture.example/v1/chat/completions"
    assert spend_cap.classify(url, "gpt-x") == "paid"
    assert spend_cap.classify(url, "vendor/model:free") == "free-model"
    assert spend_cap.classify("faustus-cli://codex") == "cli"
    assert spend_cap.classify("http://localhost:11434/v1") == "local"
    from src import model_context
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda u: "local")
    assert spend_cap.classify(url, "gpt-x") == "local"  # declared local beats a remote-looking URL
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda u: "api")
    assert spend_cap.classify("http://192.168.0.5:8000/v1", "gpt-x") == "paid"  # declared API on a private IP


def test_flat_rate_subscription_providers_are_not_paid_calls():
    from src.chatgpt_subscription import is_chatgpt_subscription_base
    from src.copilot import is_copilot_base
    for check, probe in ((is_chatgpt_subscription_base, "https://chatgpt.com/backend-api/codex"),
                         (is_copilot_base, "https://api.githubcopilot.com")):
        if check(probe):
            assert spend_cap.classify(probe, "m") == "subscription"


# --- 3. multi-process safety --------------------------------------------------

WORKER = '''
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src import spend_cap
mode, shared, name = sys.argv[2], Path(sys.argv[3]), sys.argv[4]
url = "https://billing-fixture.example/v1"
if mode == "barrier":
    original = spend_cap._load
    def synchronized(now):
        value = original(now)
        (shared / name).touch()
        for _ in range(60):
            if all((shared / n).exists() for n in ("a", "b")):
                break
            time.sleep(0.02)
        return value
    spend_cap._load = synchronized
    spend_cap.record(url, {"cost_usd": 0.5, "total_tokens": 10})
else:
    for _ in range(25):
        spend_cap.record(url, {"cost_usd": 0.01, "total_tokens": 1})
'''


def _run_workers(tmp_path, mode, names):
    data = tmp_path / "shared-data"
    data.mkdir()
    shared = tmp_path / "barrier"
    shared.mkdir()
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    env = dict(os.environ, ODYSSEUS_DATA_DIR=str(data), HF_HUB_OFFLINE="1")
    procs = [subprocess.Popen([sys.executable, str(worker), str(ROOT), mode, str(shared), n], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in names]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode(errors="replace")
    state = json.loads((data / "spend_cap.json").read_text(encoding="utf-8"))
    return state


def test_two_processes_interleaving_load_and_save_lose_no_update(tmp_path):
    state = _run_workers(tmp_path, "barrier", ["a", "b"])
    assert state["calls"] == 2 and state["usd"] == pytest.approx(1.0) and state["tokens"] == 20


def test_many_processes_share_one_exact_total(tmp_path):
    state = _run_workers(tmp_path, "stress", ["p1", "p2", "p3", "p4"])
    assert state["calls"] == 100 and state["usd"] == pytest.approx(1.0) and state["tokens"] == 100


# --- 1. the settings are human-only -------------------------------------------

def test_manage_settings_cannot_touch_the_cap(monkeypatch):
    import src.settings as settings_mod
    from src.agent_tools.admin_tools import do_manage_settings
    store = {}
    monkeypatch.setattr(settings_mod, "load_settings", lambda: dict(store))
    monkeypatch.setattr(settings_mod, "save_settings", lambda s: store.update(s))
    for key in sorted(spend_cap.PROTECTED_SETTINGS):
        for action in ("set", "reset", "delete"):
            out = asyncio.run(do_manage_settings(json.dumps(
                {"action": action, "key": key, "value": False if "enabled" in key else 0})))
            assert out.get("exit_code") == 1 and "person" in out["error"], (key, action, out)
    assert store == {}
    ok = asyncio.run(do_manage_settings(json.dumps(
        {"action": "set", "key": "agent_input_token_budget", "value": 8000})))
    assert ok.get("exit_code") == 0 and store["agent_input_token_budget"] == 8000


@pytest.fixture()
def settings_post(monkeypatch):
    from unittest.mock import MagicMock
    import routes.auth_routes as auth_routes
    from src.settings import DEFAULT_SETTINGS
    stored = dict(DEFAULT_SETTINGS)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(stored))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda new: stored.clear() or stored.update(new))
    auth_manager = MagicMock()
    auth_manager.get_username_for_token.return_value = "luis"
    auth_manager.is_admin.return_value = True
    router = auth_routes.setup_auth_routes(auth_manager)
    post = next(r.endpoint for r in router.routes
                if getattr(r, "path", "") == "/api/auth/settings" and "POST" in getattr(r, "methods", ()))

    async def call(body, headers=None):
        async def js():
            return body
        request = SimpleNamespace(cookies={}, headers=headers or {}, state=SimpleNamespace(),
                                  client=SimpleNamespace(host="127.0.0.1"), json=js)
        return await post(request)
    return SimpleNamespace(stored=stored, call=lambda b, h=None: asyncio.run(call(b, h)))


def test_settings_api_refuses_cap_changes_from_the_internal_token(settings_post):
    tool = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}
    for body in ({"paid_spend_cap_enabled": False}, {"paid_spend_cap_daily_usd": 9999},
                 {"paid_spend_cap_daily_tokens": 0}):
        with pytest.raises(HTTPException) as err:
            settings_post.call(body, tool)
        assert err.value.status_code == 403
    assert settings_post.stored["paid_spend_cap_enabled"] is True
    assert settings_post.stored["paid_spend_cap_daily_usd"] == 5.0
    # the tool layer may still send an unchanged value (a whole-form echo) and other settings
    settings_post.call({"paid_spend_cap_daily_usd": 5.0, "agent_max_rounds": 7}, tool)
    assert settings_post.stored["agent_max_rounds"] == 7


def test_settings_api_lets_a_person_change_the_cap(settings_post):
    settings_post.call({"paid_spend_cap_daily_usd": 7.0})
    assert settings_post.stored["paid_spend_cap_daily_usd"] == 7.0
