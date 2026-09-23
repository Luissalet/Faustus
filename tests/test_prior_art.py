"""tests/test_prior_art.py — src/prior_art.py: rubric, verify, search,
license matrix, caching, report persistence.

No real network: `httpx.Client` is replaced with a fake that answers from a
canned `{owner/name: FakeResponse}` map, the same discipline
`services/search/providers.py`'s own tests use for its HTTP calls.
"""
from __future__ import annotations

import json as jsonlib
import time

import httpx
import pytest

from src import prior_art


# ── fakes ────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise jsonlib.JSONDecodeError("no data", "", 0)
        return self._json


class FakeClient:
    """Stand-in for `httpx.Client`: routes `/repos/{owner}/{name}` and
    `/search/repositories` GETs to a canned map keyed by `owner/name`
    (case-insensitive) or `"__search__"`. A value that is an Exception
    instance is raised instead of returned, to simulate a network error."""

    def __init__(self, repos=None, search=None):
        self.repos = {k.lower(): v for k, v in (repos or {}).items()}
        self.search = search

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        if "/search/repositories" in url:
            value = self.search
        else:
            key = url.rsplit("/repos/", 1)[-1].lower()
            value = self.repos.get(key, FakeResponse(404, {"message": "Not Found"}))
        if isinstance(value, Exception):
            raise value
        return value


def _repo_payload(full_name, *, archived=False, fork=False, pushed_at="2026-08-01T00:00:00Z",
                  spdx_id="MIT", stars=100, open_issues=3, description="A thing.",
                  default_branch="main", homepage=""):
    return {
        "full_name": full_name, "archived": archived, "fork": fork,
        "pushed_at": pushed_at, "stargazers_count": stars, "open_issues_count": open_issues,
        "description": description, "default_branch": default_branch, "homepage": homepage,
        "html_url": f"https://github.com/{full_name}",
        "license": {"spdx_id": spdx_id} if spdx_id else None,
    }


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """A fresh sqlite store per test, and `prior_art_enabled` defaulted on."""
    import src.constants as constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    prior_art._INITIALIZED_PATHS.clear()

    def _get_setting(key, default=None):
        return {"prior_art_enabled": True, "prior_art_github_token": ""}.get(key, default)

    monkeypatch.setattr("src.settings.get_setting", _get_setting)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    yield


def _stub_client(monkeypatch, repos=None, search=None):
    monkeypatch.setattr(prior_art.httpx, "Client",
                        lambda *a, **kw: FakeClient(repos=repos, search=search))


# ── rubric: shape, no network ───────────────────────────────────────────

def test_rubric_shape_and_no_network(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("rubric must not touch the network")
    monkeypatch.setattr(prior_art.httpx, "Client", _boom)

    out = prior_art.rubric("a markdown to pdf converter", stack="python",
                           license="MIT", constraints="offline only")
    assert out["idea"] == "a markdown to pdf converter"
    assert out["verdicts"] == ["reuse", "adapt", "write"]
    assert "components" in out["slate_shape"]
    assert "MIT" in out["instructions"]
    assert "offline only" in out["instructions"]
    assert "answer the user in the user's own language" in out["instructions"].lower()


def test_rubric_with_no_idea_still_returns_a_shape():
    out = prior_art.rubric("")
    assert out["idea"] == ""
    assert "slate_shape" in out


# ── license matrix ───────────────────────────────────────────────────────

@pytest.mark.parametrize("candidate,target,flag", [
    ("permissive", "permissive", False),
    ("permissive", "strong_copyleft", False),
    ("none", "permissive", True),
    ("weak_copyleft", "strong_copyleft", True),
    ("weak_copyleft", "permissive", False),
    ("strong_copyleft", "permissive", True),
    ("strong_copyleft", "weak_copyleft", True),
    ("strong_copyleft", "strong_copyleft", False),
])
def test_license_matrix_reuse_cases(candidate, target, flag):
    result = prior_art.license_compatibility(target, candidate, "reuse")
    assert result["flag"] is flag
    assert result["compatible"] is (not flag)


def test_license_matrix_adapt_is_always_compatible_and_notes_no_copy():
    for candidate in ("permissive", "weak_copyleft", "strong_copyleft", "none"):
        result = prior_art.license_compatibility("permissive", candidate, "adapt")
        assert result["compatible"] is True
        assert result["flag"] is False
        assert "study the approach" in result["note"]
    none_result = prior_art.license_compatibility("permissive", "none", "adapt")
    assert "never copy" in none_result["note"]


def test_license_matrix_write_is_always_compatible():
    result = prior_art.license_compatibility("permissive", "strong_copyleft", "write")
    assert result["compatible"] is True
    assert result["flag"] is False


def test_license_category_normalizes_suffixes_and_case():
    assert prior_art._license_category("mit") == "permissive"
    assert prior_art._license_category("GPL-3.0-or-later") == "strong_copyleft"
    assert prior_art._license_category("LGPL-2.1-only") == "weak_copyleft"
    assert prior_art._license_category("NOASSERTION") == "unrecognized"
    assert prior_art._license_category("OTHER") == "unrecognized"
    assert prior_art._license_category("SOME-RARE-1.0") == "unrecognized"
    assert prior_art._license_category(None) == "none"
    assert prior_art._license_category("") == "none"


# ── health ───────────────────────────────────────────────────────────────

def test_health_buckets():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert prior_art._health("2026-08-01T00:00:00Z", now=now) == "active"
    assert prior_art._health("2026-01-01T00:00:00Z", now=now) == "slowing"
    assert prior_art._health("2020-01-01T00:00:00Z", now=now) == "stale"
    assert prior_art._health(None, now=now) == "unknown"
    assert prior_art._health("not-a-date", now=now) == "unknown"


# ── verify: found / missing / archived / renamed / stale / fork ─────────

def test_verify_found_repo_confirms_reuse(monkeypatch):
    _stub_client(monkeypatch, repos={
        "psf/requests": FakeResponse(200, _repo_payload("psf/requests", spdx_id="Apache-2.0")),
    })
    slate = {"idea": "http client", "components": [
        {"name": "http client", "verdict": "reuse", "repos": ["psf/requests"], "rationale": "widely used"},
    ]}
    out = prior_art.verify(slate, target_license="MIT")
    assert out["exit_code"] == 0
    assert out["verified"] is True
    comp = out["components"][0]
    assert comp["outcome"] == "confirmed"
    assert comp["results"][0]["status"] == "found"
    assert comp["results"][0]["license"]["compatible"] is True
    assert "id" in out and out["id"].startswith("PA-")
    assert "psf/requests" in out["table"]


def test_verify_missing_repo_is_replace(monkeypatch):
    _stub_client(monkeypatch, repos={})  # every lookup 404s
    slate = {"components": [
        {"name": "x", "verdict": "reuse", "repos": ["nobody-xyz/does-not-exist-123"], "rationale": "?"},
    ]}
    out = prior_art.verify(slate)
    comp = out["components"][0]
    assert comp["results"][0]["status"] == "missing"
    assert comp["outcome"] == "replace"
    assert any("search" in a for a in comp["next_actions"])
    assert out["verified"] is True  # a real 404 still proves the API answered


def test_verify_archived_repo_downgrades_reuse(monkeypatch):
    _stub_client(monkeypatch, repos={
        "old/lib": FakeResponse(200, _repo_payload("old/lib", archived=True)),
    })
    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["old/lib"], "rationale": "?"}]}
    out = prior_art.verify(slate, target_license="MIT")
    comp = out["components"][0]
    assert comp["results"][0]["status"] == "archived"
    assert comp["outcome"] == "downgrade"


def test_verify_renamed_repo_reports_canonical_name(monkeypatch):
    _stub_client(monkeypatch, repos={
        "old-owner/old-name": FakeResponse(200, _repo_payload("new-owner/new-name")),
    })
    slate = {"components": [{"name": "x", "verdict": "adapt", "repos": ["old-owner/old-name"], "rationale": "?"}]}
    out = prior_art.verify(slate)
    entry = out["components"][0]["results"][0]
    assert entry["full_name"] == "new-owner/new-name"
    assert entry["renamed"] is True


def test_verify_stale_repo_downgrades_reuse(monkeypatch):
    _stub_client(monkeypatch, repos={
        "old/stale": FakeResponse(200, _repo_payload("old/stale", pushed_at="2019-01-01T00:00:00Z")),
    })
    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["old/stale"], "rationale": "?"}]}
    out = prior_art.verify(slate, target_license="MIT")
    comp = out["components"][0]
    assert comp["results"][0]["health"] == "stale"
    assert comp["outcome"] == "downgrade"


def test_verify_fork_is_viable_but_flagged_in_status(monkeypatch):
    _stub_client(monkeypatch, repos={
        "someone/fork": FakeResponse(200, _repo_payload("someone/fork", fork=True)),
    })
    slate = {"components": [{"name": "x", "verdict": "adapt", "repos": ["someone/fork"], "rationale": "?"}]}
    out = prior_art.verify(slate)
    assert out["components"][0]["results"][0]["status"] == "fork"
    assert out["components"][0]["outcome"] == "confirmed"


def test_verify_gpl_reuse_into_mit_is_flagged():
    result = prior_art.license_compatibility("permissive", "strong_copyleft", "reuse")
    assert result["flag"] is True


def test_verify_mit_reuse_is_ok():
    result = prior_art.license_compatibility("permissive", "permissive", "reuse")
    assert result["flag"] is False


def test_verify_no_license_adapt_note(monkeypatch):
    _stub_client(monkeypatch, repos={
        "someone/nolicense": FakeResponse(200, _repo_payload("someone/nolicense", spdx_id=None)),
    })
    slate = {"components": [{"name": "x", "verdict": "adapt", "repos": ["someone/nolicense"], "rationale": "?"}]}
    out = prior_art.verify(slate)
    comp = out["components"][0]
    assert comp["results"][0]["license"]["candidate_category"] == "none"
    assert any("no license" in a.lower() or "read it" in a.lower() for a in comp["next_actions"])


def test_verify_unrecognized_license_reuse_is_reviewed_not_downgraded(monkeypatch):
    # A license file GitHub could not identify (dual/custom) is not "no
    # license": keep the reuse verdict and ask a human to read it.
    _stub_client(monkeypatch, repos={
        "someone/dual": FakeResponse(200, _repo_payload("someone/dual", spdx_id="NOASSERTION")),
    })
    slate = {"components": [{"name": "parser", "verdict": "reuse", "repos": ["someone/dual"], "rationale": "hard"}]}
    out = prior_art.verify(slate, target_license="MIT")
    comp = out["components"][0]
    lic = comp["results"][0]["license"]
    assert lic["candidate_category"] == "unrecognized"
    assert lic["flag"] is False and lic.get("review") is True
    assert comp["outcome"] == "confirmed"
    assert any("read the license" in a for a in comp["next_actions"])


# ── downgrade / replace / confirmed outcomes together ────────────────────

def test_verify_write_component_needs_no_repos():
    slate = {"components": [{"name": "glue code", "verdict": "write", "repos": [], "rationale": "trivial"}]}
    out = prior_art.verify(slate)
    assert out["verified"] is True
    assert out["components"][0]["outcome"] == "confirmed"


# ── rate limiting ─────────────────────────────────────────────────────────

def test_verify_rate_limited_response_is_reported_cleanly(monkeypatch):
    _stub_client(monkeypatch, repos={
        "x/y": FakeResponse(403, {"message": "rate limit"}, headers={"X-RateLimit-Remaining": "0",
                                                                      "X-RateLimit-Reset": "12345"}),
    })
    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["x/y"], "rationale": "?"}]}
    out = prior_art.verify(slate)
    assert out["rate_limited"] is True
    entry = out["components"][0]["results"][0]
    assert entry["status"] == "rate_limited"
    assert entry["rate_limit_reset"] == "12345"


def test_search_rate_limited(monkeypatch):
    _stub_client(monkeypatch, search=FakeResponse(403, {"message": "rate limit"},
                                                  headers={"X-RateLimit-Remaining": "0"}))
    out = prior_art.search("requests")
    assert out["rate_limited"] is True
    assert out["results"] == []


# ── offline / disabled ───────────────────────────────────────────────────

def test_verify_offline_marks_every_repo_unverified(monkeypatch):
    _stub_client(monkeypatch, repos={"x/y": httpx.ConnectError("no route to host")})
    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["x/y"], "rationale": "?"}]}
    out = prior_art.verify(slate)
    assert out["exit_code"] == 0
    assert out["verified"] is False
    assert out["reason"]
    entry = out["components"][0]["results"][0]
    assert entry["status"] == "unverified"


def test_verify_disabled_by_setting_never_touches_the_network(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("verify must not touch the network when disabled")
    monkeypatch.setattr(prior_art.httpx, "Client", _boom)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None:
                        False if key == "prior_art_enabled" else default)

    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["psf/requests"], "rationale": "?"}]}
    out = prior_art.verify(slate)
    assert out["exit_code"] == 0
    assert out["verified"] is False
    assert "disabled" in out["reason"]
    assert out["components"][0]["results"][0]["status"] == "unverified"


def test_search_disabled_by_setting_never_touches_the_network(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("search must not touch the network when disabled")
    monkeypatch.setattr(prior_art.httpx, "Client", _boom)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None:
                        False if key == "prior_art_enabled" else default)
    out = prior_art.search("requests")
    assert out["verified"] is False
    assert "disabled" in out["reason"]


# ── cache ────────────────────────────────────────────────────────────────

def test_cache_hit_avoids_a_second_request(monkeypatch):
    calls = {"n": 0}

    class CountingClient(FakeClient):
        def get(self, url, params=None):
            calls["n"] += 1
            return super().get(url, params=params)

    monkeypatch.setattr(prior_art.httpx, "Client",
                        lambda *a, **kw: CountingClient(repos={
                            "psf/requests": FakeResponse(200, _repo_payload("psf/requests")),
                        }))
    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["psf/requests"], "rationale": "?"}]}
    prior_art.verify(slate)
    assert calls["n"] == 1
    prior_art.verify(slate)  # second call: cache hit, no new request
    assert calls["n"] == 1


def test_cache_respects_ttl(monkeypatch):
    monkeypatch.setattr(prior_art, "_CACHE_TTL", __import__("datetime").timedelta(seconds=0))
    calls = {"n": 0}

    class CountingClient(FakeClient):
        def get(self, url, params=None):
            calls["n"] += 1
            return super().get(url, params=params)

    monkeypatch.setattr(prior_art.httpx, "Client",
                        lambda *a, **kw: CountingClient(repos={
                            "psf/requests": FakeResponse(200, _repo_payload("psf/requests")),
                        }))
    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["psf/requests"], "rationale": "?"}]}
    prior_art.verify(slate)
    time.sleep(0.01)
    prior_art.verify(slate)
    assert calls["n"] == 2  # TTL of 0 means every call is a miss


# ── 40-repo cap ──────────────────────────────────────────────────────────

def test_more_than_40_repos_are_capped(monkeypatch):
    repos = {f"o{i}/r{i}": FakeResponse(200, _repo_payload(f"o{i}/r{i}")) for i in range(50)}
    _stub_client(monkeypatch, repos=repos)
    slate = {"components": [
        {"name": f"c{i}", "verdict": "reuse", "repos": [f"o{i}/r{i}"], "rationale": "?"}
        for i in range(50)
    ]}
    out = prior_art.verify(slate)
    fetched_statuses = [
        entry["status"]
        for comp in out["components"]
        for entry in comp["results"]
    ]
    assert fetched_statuses.count("found") <= 40


# ── token: settings secret, env fallback, never leaked ───────────────────

def test_token_from_settings_secret_is_sent_as_bearer_and_never_in_report(monkeypatch):
    seen_headers = {}

    class CapturingClient(FakeClient):
        def __init__(self, headers=None, **kw):
            super().__init__(repos={"psf/requests": FakeResponse(200, _repo_payload("psf/requests"))})
            seen_headers.update(headers or {})

    monkeypatch.setattr(prior_art.httpx, "Client", lambda *a, **kw: CapturingClient(**kw))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None:
                        "sekrit-token" if key == "prior_art_github_token" else
                        (True if key == "prior_art_enabled" else default))

    slate = {"components": [{"name": "x", "verdict": "reuse", "repos": ["psf/requests"], "rationale": "?"}]}
    out = prior_art.verify(slate)
    assert seen_headers.get("Authorization") == "Bearer sekrit-token"
    assert "sekrit-token" not in jsonlib.dumps(out)

    saved = prior_art.report(out["id"])
    assert "sekrit-token" not in jsonlib.dumps(saved)


def test_token_falls_back_to_env(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None:
                        True if key == "prior_art_enabled" else default)
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    assert prior_art._resolve_token() == "env-token"
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("GH_TOKEN", "gh-env-token")
    assert prior_art._resolve_token() == "gh-env-token"


def test_no_token_configured_means_no_authorization_header(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None:
                        True if key == "prior_art_enabled" else default)
    headers = prior_art._headers()
    assert "Authorization" not in headers


def test_token_is_sent_only_as_the_authorization_header(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None:
                        "sekrit" if key == "prior_art_github_token" else default)
    headers = prior_art._headers()
    assert headers["Authorization"] == "Bearer sekrit"
    assert all(k == "Authorization" or "sekrit" not in str(v) for k, v in headers.items())


# ── search ───────────────────────────────────────────────────────────────

def test_search_returns_verified_fields(monkeypatch):
    _stub_client(monkeypatch, search=FakeResponse(200, {
        "total_count": 1,
        "items": [_repo_payload("psf/requests", stars=52000)],
    }))
    out = prior_art.search("http client", language="python")
    assert out["verified"] is True
    assert out["results"][0]["full_name"] == "psf/requests"
    assert out["results"][0]["license_category"] == "permissive"
    assert "language:python" in out["query"]


def test_search_requires_query():
    out = prior_art.search("")
    assert out["exit_code"] == 1


# ── report persistence ────────────────────────────────────────────────────

def test_report_persistence_and_listing(monkeypatch):
    _stub_client(monkeypatch, repos={"psf/requests": FakeResponse(200, _repo_payload("psf/requests"))})
    slate = {"idea": "an http client", "components": [
        {"name": "x", "verdict": "reuse", "repos": ["psf/requests"], "rationale": "?"},
    ]}
    out = prior_art.verify(slate, owner="ada")
    report_id = out["id"]
    assert report_id.startswith("PA-")

    fetched = prior_art.report(report_id)
    assert fetched["idea"] == "an http client"
    assert fetched["owner"] == "ada"
    assert fetched["results"]["components"][0]["results"][0]["full_name"] == "psf/requests"

    rows = prior_art.reports(10)
    assert any(r["id"] == report_id for r in rows)

    scoped = prior_art.reports(10, owner="ada")
    assert all(r["owner"] == "ada" for r in scoped)

    assert prior_art.report("PA-999999") is None


# ── agent tool arg parsing (bare string = rubric idea) ───────────────────

def test_agent_tool_bare_string_is_rubric_shorthand():
    from src.agent_tools.prior_art_tools import _parse_args
    args = _parse_args("a markdown to pdf converter")
    assert args == {"action": "rubric", "idea": "a markdown to pdf converter"}


def test_agent_tool_json_object_passes_through():
    from src.agent_tools.prior_art_tools import _parse_args
    args = _parse_args('{"action": "search", "query": "http client"}')
    assert args == {"action": "search", "query": "http client"}
