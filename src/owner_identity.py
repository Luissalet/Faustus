"""Shared owner identity constants and helpers."""

from __future__ import annotations

import os
from typing import Optional


DEFAULT_LOCAL_OWNER = "__odysseus_local__"
DEFAULT_LOCAL_OWNER_LABEL = "Local"
INTERNAL_TOOL_USER = "internal-tool"

REQUEST_SENTINEL_OWNERS = frozenset({INTERNAL_TOOL_USER, "api", "demo", "system"})
RESERVED_AUTH_USERNAMES = REQUEST_SENTINEL_OWNERS | {DEFAULT_LOCAL_OWNER}


def auth_disabled() -> bool:
    """Return True only when auth is explicitly disabled by configuration."""
    return os.getenv("AUTH_ENABLED", "true").strip().lower() == "false"


def normalize_owner(owner: str | None) -> Optional[str]:
    """Normalize an owner-like value without inventing a fallback identity."""
    value = str(owner or "").strip()
    return value or None


def owner_key(owner: str | None) -> Optional[str]:
    normalized = normalize_owner(owner)
    return normalized.lower() if normalized else None


def is_request_sentinel_owner(owner: str | None) -> bool:
    return owner_key(owner) in REQUEST_SENTINEL_OWNERS


def effective_storage_owner(owner: str | None, *, auth_is_disabled: bool | None = None) -> Optional[str]:
    """Resolve the owner used for storage writes that need a real bucket.

    ``None`` still means no authenticated owner when auth is enabled. In the
    explicit no-login mode, it resolves to the reserved local owner instead of
    conflating local-operator writes with legacy NULL/ownerless rows.
    """
    normalized = normalize_owner(owner)
    if normalized:
        if is_request_sentinel_owner(normalized):
            return None
        return normalized
    disabled = auth_disabled() if auth_is_disabled is None else auth_is_disabled
    if disabled:
        return DEFAULT_LOCAL_OWNER
    return None


def single_user_mode() -> bool:
    """True when the install runs without login (AUTH_ENABLED=false).

    One person sits behind every owner bucket there: rows written as the
    first admin, as the reserved local owner or as legacy NULL all belong to
    the operator, so owner checks between them must not reject anything.
    """
    return auth_disabled()


def owners_match(stored: str | None, caller: str | None) -> bool:
    """Whether ``caller`` may use a row stored for ``stored``.

    With login on this is a plain identity check; in single-user mode every
    bucket is the same person (see :func:`single_user_mode`).
    """
    if single_user_mode():
        return True
    caller_norm = normalize_owner(caller)
    return bool(caller_norm) and caller_norm == normalize_owner(stored)


def is_default_local_owner(owner: str | None) -> bool:
    return owner_key(owner) == DEFAULT_LOCAL_OWNER
