"""WEB-04 — browser actions check a precondition and always carry a readback.

MAPA_REUTILIZACION.md marks WEB-04 "ausente": nothing in Faustus verifies a
click/type/navigate's target before it runs, or reports what actually
happened afterward — that is left entirely to `@playwright/mcp`. This test
drives `src.browser_actions.run_with_precondition` against a fake page
abstraction (injected snapshot/act callables), per the lote's own
instruction: no real browser, no network.
"""
import asyncio

import pytest

from src.browser_actions import ActionPrecondition, check_precondition, run_with_precondition


def _snapshot(url="https://example.com/checkout", title="Checkout", text="Delete button present"):
    async def _fn():
        return {"url": url, "title": title, "text": text}
    return _fn


def test_check_precondition_passes_when_url_and_element_match():
    assert check_precondition(
        ActionPrecondition(expected_url="/checkout", require_element="Delete button"),
        current_url="https://example.com/checkout?ref=1",
        snapshot_text="... Delete button present ...",
    ) is None


def test_check_precondition_fails_on_url_mismatch():
    reason = check_precondition(
        ActionPrecondition(expected_url="/checkout"),
        current_url="https://example.com/home",
        snapshot_text="",
    )
    assert reason is not None
    assert "/checkout" in reason


def test_check_precondition_fails_when_element_is_missing_after_layout_shift():
    # WEB-04 acceptance: a shifted layout must not produce a blind click on Delete.
    reason = check_precondition(
        ActionPrecondition(require_element="Delete button"),
        current_url="https://example.com/checkout",
        snapshot_text="... the page now shows a Cancel button instead ...",
    )
    assert reason is not None
    assert "Delete button" in reason


def test_run_with_precondition_refuses_the_action_when_it_fails():
    calls = {"acted": False}

    async def act():
        calls["acted"] = True
        return {"output": "clicked", "exit_code": 0}

    out = asyncio.run(run_with_precondition(
        "browser_click",
        precondition=ActionPrecondition(require_element="Delete button"),
        snapshot_fn=_snapshot(text="only a Cancel button here"),
        act_fn=act,
    ))
    assert out["blocked"] is True
    assert "Delete button" in out["error"]
    assert calls["acted"] is False  # the action never ran
    assert out["readback"]["url"] == "https://example.com/checkout"


def test_run_with_precondition_runs_and_attaches_fresh_readback():
    snapshots = iter([
        {"url": "https://example.com/checkout", "title": "Checkout", "text": "Delete button present"},
        {"url": "https://example.com/checkout/confirm", "title": "Confirmed", "text": "Order confirmed"},
    ])

    async def snapshot_fn():
        return next(snapshots)

    async def act():
        return {"output": "clicked delete", "exit_code": 0}

    out = asyncio.run(run_with_precondition(
        "browser_click",
        precondition=ActionPrecondition(require_element="Delete button"),
        snapshot_fn=snapshot_fn,
        act_fn=act,
    ))
    assert "blocked" not in out
    assert out["output"] == "clicked delete"
    # Readback reflects the page AFTER the action, not the precondition check.
    assert out["readback"]["url"] == "https://example.com/checkout/confirm"
    assert out["readback"]["title"] == "Confirmed"


def test_readback_dom_hash_changes_when_the_page_changes():
    calls = {"n": 0}

    async def snapshot_fn():
        calls["n"] += 1
        text = "before navigation" if calls["n"] == 1 else "after navigation, different DOM"
        return {"url": "https://example.com", "title": "T", "text": text}

    async def act():
        return {"output": "navigated", "exit_code": 0}

    out = asyncio.run(run_with_precondition(
        "browser_navigate",
        precondition=ActionPrecondition(),
        snapshot_fn=snapshot_fn,
        act_fn=act,
    ))
    # A stale hash from the pre-action snapshot would equal a hash of "before
    # navigation" -- the readback must be the POST-action hash instead.
    import hashlib
    stale_hash = hashlib.sha256(b"before navigation").hexdigest()
    assert out["readback"]["dom_hash"] != stale_hash
    assert out["readback"]["dom_hash"] == hashlib.sha256(b"after navigation, different DOM").hexdigest()


def test_no_precondition_means_action_always_runs():
    async def act():
        return {"output": "ok", "exit_code": 0}

    out = asyncio.run(run_with_precondition(
        "browser_navigate",
        precondition=ActionPrecondition(),
        snapshot_fn=_snapshot(),
        act_fn=act,
    ))
    assert out["output"] == "ok"
    assert "readback" in out
