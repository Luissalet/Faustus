"""Exceptions raised by :mod:`hoard_link`."""

from __future__ import annotations

from typing import Optional


class HoardLinkError(Exception):
    """Base class for every error this package raises on purpose."""


class Unavailable(HoardLinkError):
    """No server could be resolved for a capability.

    ``reasons`` is the list of short strings collected while walking the
    resolution order (one per source that was tried and did not pan out),
    so the caller (or its Settings screen) can show *why*.
    """

    def __init__(self, capability: str, reasons: list[str]):
        self.capability = capability
        self.reasons = list(reasons)
        joined = "; ".join(self.reasons) if self.reasons else "no reason recorded"
        super().__init__(f"{capability} unavailable: {joined}")


class BackendError(HoardLinkError):
    """A resolved server failed a real call.

    ``status`` is the HTTP status, or ``0`` when there was no HTTP response
    at all (connection refused, timeout, a local command that could not
    start) — e.g. a stale address in explicit configuration.
    """

    def __init__(self, provider: Optional[str], status: int, body_excerpt: str):
        self.provider = provider
        self.status = status
        self.body_excerpt = body_excerpt
        who = provider or "backend"
        if status == 0:
            message = f"{who} unreachable: {body_excerpt}"
        else:
            message = f"{who} returned HTTP {status}: {body_excerpt}"
        super().__init__(message)


def missing_dependency(package: str, feature: str = "", *, pip_name: Optional[str] = None) -> Unavailable:
    """The :class:`Unavailable` the commons raise when an optional package is not installed:
    ``raise missing_dependency("bs4", "html extraction", pip_name="beautifulsoup4")``."""
    what = feature or package
    return Unavailable(what, [f"the optional package {package!r} is not installed (pip install {pip_name or package})"])
