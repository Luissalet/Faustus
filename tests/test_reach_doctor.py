"""`reach doctor` (R1) — no-network (live=False) and mocked live=True."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from src.reach.base import clear_availability_cache
from src.reach.doctor import doctor


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_availability_cache()
    yield
    clear_availability_cache()


def test_doctor_live_false_touches_no_network():
    report = asyncio.run(doctor(live=False))
    assert set(report["channels"]) == {
        "web", "youtube", "github", "reddit", "x", "hackernews", "rss", "arxiv", "wikipedia",
    }
    assert report["total_count"] == 9
    assert report["ready_count"] >= 1
    assert report["live"] is False
    for name, info in report["channels"].items():
        assert info["status"] in ("ready", "needs_config", "unavailable")
        for b in info["backends"]:
            assert b["checked_live"] is False


def test_doctor_summary_counts_ready_channels():
    report = asyncio.run(doctor(live=False))
    assert report["summary"] == f"{report['ready_count']}/{report['total_count']} channels ready"


def test_doctor_never_includes_a_token_value(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: (
        "super-secret-value" if key == "reach_github_token" else default
    ))
    report = asyncio.run(doctor(live=False))
    import json
    dumped = json.dumps(report)
    assert "super-secret-value" not in dumped


def test_doctor_live_true_makes_one_cached_request_per_backend(monkeypatch):
    import src.reach.github as gh_mod

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"rate": {"remaining": 42}})

    def make_client(**kwargs):
        kwargs.pop("timeout", None)
        headers = kwargs.pop("headers", None) or {}
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)

    monkeypatch.setattr(gh_mod, "make_client", make_client)

    report1 = asyncio.run(doctor(live=True))
    report2 = asyncio.run(doctor(live=True))
    github_api_entry = next(b for b in report1["channels"]["github"]["backends"] if b["name"] == "github_api")
    assert github_api_entry["status"] == "ready"
    assert github_api_entry["checked_live"] is True
    # second doctor() call must be served from the 10-minute availability cache
    assert calls["n"] == 1
