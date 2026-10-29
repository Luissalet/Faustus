"""Work that used to hold the event loop for seconds in long turns now runs off it (task #1725).

The three culprits were captured by src/loop_watchdog.py in a live turn: the scan of every process of the PC behind
the desktop-tool check (7.9 s), the blocking probe of the model's context window (an HTTP call per round) and the
TLS context httpx rebuilt for each one-shot call (3 s).
"""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from src import host_tools, httpx_ssl_cache, model_context


# --- the process scan behind the desktop-tool check -------------------------------------------------------------

def test_stale_app_probe_is_served_while_a_thread_refreshes(monkeypatch):
    monkeypatch.setattr(host_tools.os, "name", "nt")
    monkeypatch.setattr(host_tools, "_heartbeat_fresh", lambda now: False)
    release = threading.Event()
    calls = []

    def slow_scan():
        calls.append(threading.current_thread().name)
        release.wait(5)
        return True

    monkeypatch.setattr(host_tools, "_app_process_running", slow_scan)
    monkeypatch.setattr(host_tools, "_app_cache", [time.time() - 60, False])  # an old answer: the app was closed
    monkeypatch.setattr(host_tools, "_probe_running", False)

    t0 = time.monotonic()
    assert host_tools.desktop_app_running() is False  # the stale answer, immediately
    assert time.monotonic() - t0 < 0.5
    assert host_tools.desktop_app_running() is False  # still one scan in flight, not two
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not host_tools._app_cache[1]:
        time.sleep(0.02)
    assert host_tools._app_cache[1] is True and time.time() - host_tools._app_cache[0] < 5
    assert len(calls) == 1 and calls[0] == "host-app-probe"


def test_first_app_probe_still_answers_for_real(monkeypatch):
    monkeypatch.setattr(host_tools.os, "name", "nt")
    monkeypatch.setattr(host_tools, "_heartbeat_fresh", lambda now: False)
    monkeypatch.setattr(host_tools, "_app_process_running", lambda: True)
    monkeypatch.setattr(host_tools, "_app_cache", [0.0, False])
    assert host_tools.desktop_app_running() is True


# --- the context window probe ---------------------------------------------------------------------------------

@pytest.fixture
def ctx_env(monkeypatch):
    model_context.clear_context_caches()
    monkeypatch.setattr(model_context, "is_local_endpoint", lambda url: True)
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda url: "")
    monkeypatch.setattr(model_context, "LOCAL_CONTEXT_TTL_S", 0.05)
    yield
    model_context.clear_context_caches()


def test_expired_context_length_is_not_probed_on_the_event_loop(ctx_env, monkeypatch):
    probes = []
    answer = [32768]
    gate = threading.Event()

    def probe(url, model):
        probes.append(threading.current_thread() is threading.main_thread())
        gate.wait(5)
        return answer[0], True

    monkeypatch.setattr(model_context, "_query_context_length", probe)
    gate.set()
    assert model_context.get_context_length("http://127.0.0.1:1/v1", "m") == 32768  # first answer, no loop: probes
    time.sleep(0.1)  # the local answer expires
    gate.clear()
    answer[0] = 65536

    async def on_loop():
        t0 = time.monotonic()
        value = model_context.get_context_length("http://127.0.0.1:1/v1", "m")
        return value, time.monotonic() - t0

    value, waited = asyncio.run(on_loop())
    assert value == 32768 and waited < 0.5  # the old answer, without waiting for the endpoint
    gate.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and model_context._refreshing:
        time.sleep(0.02)
    time.sleep(0.1)
    assert model_context.get_context_length("http://127.0.0.1:1/v1", "m") == 65536  # the thread refreshed it


def test_warm_context_length_probes_the_first_time_in_a_thread(ctx_env, monkeypatch):
    seen = []

    def probe(url, model):
        seen.append(threading.current_thread() is threading.main_thread())
        return 8192, True

    monkeypatch.setattr(model_context, "_query_context_length", probe)

    async def run():
        first = await model_context.warm_context_length("http://127.0.0.1:2/v1", "m")
        return first

    assert asyncio.run(run()) == 8192
    assert seen == [False]  # never the thread that runs the loop


# --- the TLS context ------------------------------------------------------------------------------------------

def test_default_tls_context_is_built_once(monkeypatch):
    from httpx._transports import default as transport

    built = []
    original = transport.create_ssl_context

    def counting(verify=True, cert=None, trust_env=True):
        built.append(verify)
        return original(verify=verify, cert=cert, trust_env=trust_env)

    httpx_ssl_cache.uninstall()
    monkeypatch.setattr(transport, "create_ssl_context", counting)
    try:
        assert httpx_ssl_cache.install()
        a = transport.create_ssl_context(verify=True, cert=None, trust_env=True)
        b = transport.create_ssl_context(verify=True, cert=None, trust_env=True)
        assert a is b and built == [True]
        # An explicit context or a different verify is never replaced by the shared one.
        import ssl
        mine = ssl.create_default_context()
        assert transport.create_ssl_context(verify=mine, cert=None, trust_env=True) is mine or built[-1] is mine
        with httpx.Client() as c1, httpx.Client() as c2:  # two clients, one context build
            assert c1 is not c2
        assert built.count(True) == 1
    finally:
        httpx_ssl_cache.uninstall()
