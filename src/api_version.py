"""
api_version.py — negotiated wire version for the chat SSE protocol (ARCH-01).

Every additive field the observability lots put on chat events (`trace_id`,
`step_id`, `call_id`, `sequence`, `stream_id`, the OBS-02 phase vocabulary...)
is safe for an old client to ignore — a new key in a JSON object nobody reads
yet breaks nothing. What is NOT automatically safe is the day a future change
needs a client to understand something it cannot safely ignore (a renamed
field, a changed meaning, a required event a client must not silently skip).
That day needs a single place the server can ask "does this client understand
what I am about to send", and a single place a client can ask "does this
server speak what I expect", instead of every route guessing from a missing
field or a 500 downstream.

Negotiation, not a version-bump war: a client that has never heard of this
scheme — no `X-Faustus-Client-Version` header, which is every request today
and every existing test — is treated as compatible, exactly the behaviour
before this module existed (COMUN rule 3: nothing that worked yesterday stops
working today). Rejection (426) is reserved for a client that IDENTIFIES
itself as older than `MIN_CLIENT_VERSION` — the one case this module exists
to give a comprehensible error for, instead of a stream of events it cannot
parse.
"""
from __future__ import annotations

from typing import Optional, Tuple

#: Bump when the wire shape changes in a way an old client cannot safely
#: ignore (a renamed/removed field, a changed meaning of an existing one).
#: A purely additive field does NOT require a bump — that is the whole point
#: of "additive". This lot (OBS-01/OBS-02/QA-09) only adds fields, so it ships
#: alongside API_VERSION="2.0" without moving MIN_CLIENT_VERSION.
API_VERSION = "2.0"

#: The oldest client version this server still streams real events to. A
#: client that IDENTIFIES as older gets 426 instead of events it cannot
#: parse. Raise this only when a future lot ships a genuinely breaking change
#: — and say so explicitly in that lot's report.
MIN_CLIENT_VERSION = "2.0"

API_VERSION_HEADER = "X-Faustus-Api-Version"
CLIENT_VERSION_HEADER = "X-Faustus-Client-Version"


def _parse(version: str) -> Optional[Tuple[int, ...]]:
    """A dotted numeric version ("2.0", "1.10.3") to a comparable tuple, or
    None when it is not that shape. Garbage input is a client bug, not a
    compatibility question this function should guess at."""
    raw = str(version or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_supported(client_version: Optional[str]) -> bool:
    """Whether a client identifying as `client_version` may receive the live
    stream.

    Missing/blank means the client predates this scheme entirely — every
    request before this lot, and every existing test — and is treated as
    compatible. A client that DOES send a version is measured against
    `MIN_CLIENT_VERSION`; one that sends something unparseable is treated as
    unsupported rather than silently let through, since guessing would defeat
    the point of asking in the first place.
    """
    raw = (client_version or "").strip()
    if not raw:
        return True
    parsed = _parse(raw)
    minimum = _parse(MIN_CLIENT_VERSION)
    if parsed is None or minimum is None:
        return False
    return parsed >= minimum


def upgrade_required_detail(client_version: Optional[str]) -> str:
    """The message a 426 carries — plain enough to act on, per ARCH-01's
    acceptance criterion ("a client too old fails comprehensibly")."""
    shown = (client_version or "").strip() or "unknown"
    return (
        f"This client (API version {shown}) is older than the minimum this "
        f"server supports ({MIN_CLIENT_VERSION}). Reload the app to pick up "
        f"a compatible build."
    )
