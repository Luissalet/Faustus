"""Lote 44 (L41): `src/rerank.py` consults `src.privacy_policy.assert_outbound`
before opening its HTTP call, closing the exact gap
`src/privacy_policy.py`'s own module docstring names ("the cross-encoder
reranker in src/rerank.py" as a call site still missing this lote).

Mirrors `tests/test_privacy_policy_auxiliaries.py`'s own pattern (monkeypatch
`privacy_policy.assert_outbound` itself rather than the settings underneath
it, proving the policy is actually consulted, not that the call fails for
some unrelated reason) and `tests/test_rerank.py`'s own doubles
(`install_endpoint`/`forbid_transport`) for discovery and the "never touches
the network" guarantee.

Revert proof (COMUN.md rule 5): with the `assert_outbound` call this lote
added to `rerank()` removed (`cp`-backed, never git),
`test_a_blocked_reranker_never_opens_a_socket` fails on `forbid_transport`'s
own assertion (the network IS reached), and
`test_a_blocked_reranker_reports_the_named_reason` fails because `reason` is
`None`/`reranked` instead of `privacy_blocked`.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import privacy_policy as pp
from src import rerank as rr

from tests.test_rerank import Endpoint, PASSAGES, forbid_transport, install_endpoint, install_transport


def _blocked(monkeypatch, calls=None):
    def fake_assert_outbound(component, destination, **kwargs):
        if calls is not None:
            calls.append((component, destination))
        raise pp.PrivacyPolicyError(
            component, destination, pp.PROFILE_LOCAL_ONLY,
            pp.ErrorInfo(code="privacy.blocked_outbound", message="blocked"),
        )
    monkeypatch.setattr(pp, "assert_outbound", fake_assert_outbound)


def test_a_blocked_reranker_never_opens_a_socket(monkeypatch):
    install_endpoint(monkeypatch)
    calls = []
    _blocked(monkeypatch, calls)
    forbid_transport(monkeypatch)

    result = rr.rerank("run a command", PASSAGES)

    assert calls == [("reranker", "http://localhost:8080/v1/rerank")]
    assert result.degraded
    assert result.reason == rr.REASON_PRIVACY_BLOCKED
    # Untouched, in the input order — same contract every other degradation keeps.
    assert result.passages == PASSAGES
    assert result.order == [0, 1, 2]


def test_the_policy_is_asked_before_any_endpoint_specific_work(monkeypatch):
    """`owner` and the resolved endpoint URL travel to the gate exactly as
    resolved — a wrong owner or a stale URL would silently defeat the whole
    point of asking first."""
    install_endpoint(monkeypatch, ep_id="ep-priv", base_url="https://remote.example.com")
    seen = {}

    def fake_assert_outbound(component, destination, *, owner=None, **kwargs):
        seen["component"] = component
        seen["destination"] = destination
        seen["owner"] = owner
        raise pp.PrivacyPolicyError(
            component, destination, pp.PROFILE_LOCAL_ONLY,
            pp.ErrorInfo(code="privacy.blocked_outbound", message="blocked"),
        )
    monkeypatch.setattr(pp, "assert_outbound", fake_assert_outbound)
    forbid_transport(monkeypatch)

    rr.rerank("q", PASSAGES, owner="alice")

    assert seen == {
        "component": "reranker",
        "destination": "https://remote.example.com/v1/rerank",
        "owner": "alice",
    }


def test_an_unblocked_profile_still_reaches_the_network_as_before(monkeypatch):
    """`local_preferred`/`cloud_allowed` (the module's real `assert_outbound`,
    not faked here) must not regress the already-covered happy path — the
    gate is additive, not a new blanket block."""
    install_endpoint(monkeypatch)

    def handler(url, payload, headers):
        from tests.test_rerank import Response, scores_payload
        return Response(scores_payload([(0, 0.9), (1, 0.1), (2, 0.5)]))

    install_transport(monkeypatch, handler)

    result = rr.rerank("run a command", PASSAGES)

    assert result.reranked is True
    assert result.reason is None
