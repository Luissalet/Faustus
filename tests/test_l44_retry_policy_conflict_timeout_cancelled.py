"""Lote 44 (L42 point 5): `src/retry_policy.py::error_class_for` names
409/408/499 itself instead of falling through to the generic
`schema.contract_violation` bucket that `core/exceptions.py::http_error_class`
used to refine locally with a second, duplicate table — removed in this same
lote (COMUN.md rule 4: one authority for the status -> OBS-03-code mapping).

Revert proof (COMUN.md rule 5): with the three `if status == ...` branches
this lote added to `error_class_for` removed (`cp`-backed, never git),
`test_409_maps_to_conflict`/`test_408_maps_to_timeout_request`/
`test_499_maps_to_cancelled_client` all fail (each falls back to
`schema.contract_violation` instead), and
`test_http_error_class_delegates_with_nothing_left_to_add` fails too, since
`core/exceptions.py` no longer has its own fallback to catch what
`error_class_for` stopped naming.
"""
from __future__ import annotations

from core.exceptions import http_error_class
from src.retry_policy import error_class_for


def test_409_maps_to_conflict():
    assert error_class_for(status=409) == "conflict.concurrent_write"


def test_408_maps_to_timeout_request():
    assert error_class_for(status=408) == "timeout.request"


def test_499_maps_to_cancelled_client():
    assert error_class_for(status=499) == "cancelled.client"


def test_http_error_class_delegates_with_nothing_left_to_add():
    """core/exceptions.py's own status-specific refinement is gone (Lote 44)
    — it is a pure pass-through onto `error_class_for` now."""
    assert http_error_class(409) == "conflict.concurrent_write"
    assert http_error_class(408) == "timeout.request"
    assert http_error_class(499) == "cancelled.client"


def test_other_statuses_are_unaffected():
    """The three new branches must not shadow anything already correct."""
    assert error_class_for(status=404) == "resource.not_found"
    assert error_class_for(status=429) == "resource.rate_limited"
    assert error_class_for(status=500) == "transport.llm_service_error"
    assert error_class_for(status=418) == "schema.contract_violation"
