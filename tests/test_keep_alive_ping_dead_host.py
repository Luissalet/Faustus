# -*- coding: utf-8 -*-
"""A keep-alive ping to a server that is not there must cost nothing twice.

`restore_keep_alive` runs at the end of every turn. With Ollama closed it was
making two blocking HTTP calls of 3 s each, every time, for nothing. In the
suite, where one test drives about thirty turns, that is the three minutes
that left runs sitting near the end with no failure and no output -- the
"stall at 97%" nobody could name.

`llm_core` already keeps a dead-host cooldown. This asserts the ping honours
it, and feeds it, so the second ping is a dictionary lookup.
"""

import src.llm_core as llm_core
import src.run_model_pin as rmp

ENDPOINT = "http://127.0.0.1:11434"
MODEL = "a-model"


def test_a_dead_host_is_not_pinged_at_all(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: True)
    monkeypatch.setattr(rmp, "_looks_like_ollama", lambda _e: True)
    monkeypatch.setattr(rmp, "_is_default_model", lambda _e, _m: False)

    import httpx

    def _boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a dead host must not be contacted")

    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(httpx, "post", _boom)

    assert rmp.restore_keep_alive(ENDPOINT, MODEL, -1) is False
    assert calls == []


def test_a_refused_probe_feeds_the_cooldown(monkeypatch):
    marked = []
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda url: marked.append(url) or True)
    monkeypatch.setattr(rmp, "_looks_like_ollama", lambda _e: True)
    monkeypatch.setattr(rmp, "_is_default_model", lambda _e, _m: False)

    import httpx

    def _refused(*args, **kwargs):
        raise ConnectionError("connection refused")

    posted = []
    monkeypatch.setattr(httpx, "get", _refused)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posted.append(a))

    rmp.restore_keep_alive(ENDPOINT, MODEL, -1)
    assert marked, "the cooldown was never told the host is down"
    assert ENDPOINT in marked[0]


def test_a_live_host_still_gets_its_ping(monkeypatch):
    """The cooldown must not become a reason to stop pinging a live server."""
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(rmp, "_looks_like_ollama", lambda _e: True)
    monkeypatch.setattr(rmp, "_is_default_model", lambda _e, _m: False)
    monkeypatch.setattr(rmp, "_norm_model", lambda m: m)
    monkeypatch.setattr(rmp, "_resident_load_options", lambda *a, **k: None)

    import httpx

    class _Resp:
        @staticmethod
        def json():
            return {"models": [{"name": MODEL}]}

    posted = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posted.append(a))

    assert rmp.restore_keep_alive(ENDPOINT, MODEL, -1) is True
    assert posted, "a live, resident model must still have its keep_alive restored"
