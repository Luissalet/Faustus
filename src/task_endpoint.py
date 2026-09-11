"""Shared resolver for background-task AI endpoints."""

import logging

from src import privacy_policy
from src.endpoint_resolver import (
    resolve_endpoint,
    resolve_utility_fallback_candidates,
)
from src.llm_core import llm_call_async_with_fallback
from src.interactive_gate import wait_for_interactive_quiet

logger = logging.getLogger(__name__)


def resolve_task_endpoint(fallback_url=None, fallback_model=None, fallback_headers=None, owner=None):
    """Return (endpoint_url, model, headers) for background tasks.

    Reads task_endpoint_id / task_model from admin settings.
    Falls back to the provided values when the setting is empty or the
    endpoint cannot be resolved.
    """
    return resolve_endpoint("task", fallback_url, fallback_model, fallback_headers, owner=owner)


def resolve_task_candidates(
    fallback_url=None,
    fallback_model=None,
    fallback_headers=None,
    owner=None,
):
    """Return ordered background-task LLM candidates.

    Order:
    1. configured Background Tasks endpoint/model, or caller fallback
    2. Utility endpoint/model
    3. Default endpoint/model
    4. Utility fallback chain

    ADP-03/ADP-22 contract gap: everything past slot 1 is an ALTERNATE
    endpoint this function falls to when the configured Background Tasks
    endpoint isn't usable -- a `local_only` session must never reach a
    remote one that way (`tests/adaptations/test_contract_boundaries.py`,
    guarantee a). Slot 1 itself is left unchecked: it is the operator's own
    explicit Background Tasks endpoint choice, not a fallback, and
    `src/provider_policy.py::resolve_route` is the place that already
    classifies an explicitly-selected endpoint against the active profile.
    """
    candidates = []

    def _append(url, model, headers, *, alternate=True):
        if not url or not model:
            return
        key = (url, model)
        if any((u, m) == key for u, m, _ in candidates):
            return
        if alternate:
            try:
                privacy_policy.assert_outbound("task_endpoint_fallback", url, owner=owner)
            except privacy_policy.PrivacyPolicyError as exc:
                logger.info(
                    "task_endpoint: dropped fallback candidate under "
                    "local_only profile (%s)", exc.error_info.message,
                )
                return
        candidates.append((url, model, headers or {}))

    _append(*resolve_task_endpoint(fallback_url, fallback_model, fallback_headers, owner=owner),
           alternate=False)
    _append(*resolve_endpoint("utility", owner=owner))
    _append(*resolve_endpoint("default", owner=owner))
    for url, model, headers in resolve_utility_fallback_candidates(owner=owner):
        _append(url, model, headers)
    return candidates


async def task_llm_call_async(
    messages,
    *,
    fallback_url=None,
    fallback_model=None,
    fallback_headers=None,
    owner=None,
    **kwargs,
):
    """Call the shared background-task LLM candidate chain."""
    candidates = resolve_task_candidates(
        fallback_url=fallback_url,
        fallback_model=fallback_model,
        fallback_headers=fallback_headers,
        owner=owner,
    )
    if not candidates:
        raise RuntimeError("No LLM endpoint available for background task")
    await wait_for_interactive_quiet("background task LLM")
    kwargs.setdefault("workload", "background")
    return await llm_call_async_with_fallback(candidates, messages=messages, **kwargs)
