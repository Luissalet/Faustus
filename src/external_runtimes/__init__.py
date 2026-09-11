"""src.external_runtimes — read-only adapters to externally configured
agent/desktop runtimes Faustus does not run itself.

CMP-06 (INFORME_COMPARATIVO_V2.md §3.5) adds the first and, for now, only
member: `herdr.py`, a Herdr client. Every module in this package is
READ-ONLY by contract — see `herdr.py`'s module docstring for why that is
load-bearing, not incidental. **Sin investigación externa**: nothing here
was checked against a live instance of the runtime it talks to; the wire
contract is inferred from what the informe describes and is documented, in
`docs/api/external_runtimes.md`, as pending validation.
"""
from __future__ import annotations

from .herdr import (
    HerdrClient,
    HerdrConfig,
    HerdrError,
    NotConfiguredError,
    Presence,
    TransportError,
    UnsupportedVersionError,
    load_config,
    save_config,
)

__all__ = [
    "HerdrClient",
    "HerdrConfig",
    "HerdrError",
    "NotConfiguredError",
    "Presence",
    "TransportError",
    "UnsupportedVersionError",
    "load_config",
    "save_config",
]
