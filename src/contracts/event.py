"""
contracts/event.py — the sealed envelope everything else reports through.

One vocabulary for progress, workflows, hooks, channels and observability, so
that the SSE stream a page listens to, the row an audit writes and the payload
a hook receives are the same object seen three times.  The names come straight
from the OpenHands reference:

    run.created → approval.requested → backend.started → tool.progress*
    → artifact.created* → run.completed | run.failed | run.cancelled

Redaction is part of the envelope, not a courtesy applied by whoever renders
it.  `redact()` removes secret values wherever they appear, and it *says how
many* it removed: a log line that quietly lost a field is indistinguishable
from one that never had it, and only one of those is safe to reason from.

The lesson from the two SSE dialects is baked in too: `sse()` emits an unnamed
frame, because a named frame never reaches `onmessage` and a page written
against the other endpoint goes deaf without erroring.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, now_iso, reject_unknown,
    text, timestamp, whole,
)


#: Closed on purpose.  A name that nothing routes on is a string in a log; a
#: name in this list is something a hook, a page and an audit all understand.
EVENT_NAMES = (
    "run.created", "run.completed", "run.failed", "run.cancelled", "run.interrupted",
    "approval.requested", "approval.granted", "approval.denied", "approval.expired",
    "backend.started", "backend.finished",
    "tool.progress", "tool.blocked",
    "artifact.created", "artifact.discarded",
    "memory.proposed",
    "skill.installed", "skill.removed",
    "workflow.started", "workflow.node", "workflow.paused", "workflow.finished",
)

_REDACTED = "<redacted>"

#: Key names that carry a credential often enough that the value is dropped on
#: sight.  This is the cheap half; the exact-value pass below is the real one.
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|api[_-]?key|apikey|authorization|"
    r"auth|credential|cookie|session[_-]?key|private[_-]?key|refresh[_-]?token)"
    r"(?:$|_)", re.IGNORECASE,
)

#: Below this length a "secret" is more likely to be a substring of ordinary
#: prose than a credential, and blanking every "1" in a payload helps nobody.
_MIN_SECRET_LEN = 8


def _scrub(value: Any, secrets: Tuple[str, ...], hits: list, depth: int = 0) -> Any:
    """Walk a payload replacing secret values and secret-looking keys.  Every
    replacement appends to `hits`, so the caller can report the count instead
    of hoping the redaction happened."""
    if depth > 12:
        hits.append("depth")
        return _REDACTED
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                out[name] = _REDACTED
                hits.append(name)
            else:
                out[name] = _scrub(item, secrets, hits, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, secrets, hits, depth + 1) for v in value]
    if isinstance(value, str):
        scrubbed = value
        for secret in secrets:
            if secret and len(secret) >= _MIN_SECRET_LEN and secret in scrubbed:
                scrubbed = scrubbed.replace(secret, _REDACTED)
                hits.append("value")
        return scrubbed
    return value


@dataclass(frozen=True)
class Event:
    """Immutable, ordered within its run, and redacted before it leaves."""

    name: str
    run_id: str = ""
    seq: int = 0
    at: str = ""
    owner: str = ""
    project_id: str = ""
    session_id: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    redactions: int = 0
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("name", "run_id", "seq", "at", "owner", "project_id", "session_id",
             "data", "redactions", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "event") -> "Event":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        name = text(data, "name", path, max_len=64)
        if name not in EVENT_NAMES:
            raise ContractError(
                path + ".name",
                f"is not a known event; add it to EVENT_NAMES if it is real, because "
                f"an unrouted name reaches no hook and no page. Known: {list(EVENT_NAMES)}",
                got=name,
            )
        payload = data.get("data")
        if payload is not None and not isinstance(payload, Mapping):
            raise ContractError(f"{path}.data", "expected an object", got=payload)
        return cls(
            name=name,
            run_id=text(data, "run_id", path, required=False, max_len=64),
            seq=whole(data, "seq", path, default=0, minimum=0),
            at=timestamp(data, "at", path, default=now_iso()),
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            session_id=text(data, "session_id", path, required=False, max_len=128),
            data=dict(payload or {}),
            redactions=whole(data, "redactions", path, default=0, minimum=0),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    def redact(self, secrets: Tuple[str, ...] = ()) -> "Event":
        """Return the version safe to log, stream, export and hand to a hook.
        `redactions` counts what went: a consumer can tell "nothing sensitive
        was here" from "something was, and it is gone"."""
        hits: list = []
        scrubbed = _scrub(dict(self.data), tuple(secrets), hits)
        return Event(
            name=self.name, run_id=self.run_id, seq=self.seq, at=self.at,
            owner=self.owner, project_id=self.project_id, session_id=self.session_id,
            data=scrubbed, redactions=self.redactions + len(hits),
            schema_version=self.schema_version,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name, "run_id": self.run_id, "seq": self.seq, "at": self.at,
            "owner": self.owner, "project_id": self.project_id,
            "session_id": self.session_id, "data": dict(self.data),
            "redactions": self.redactions,
        }

    def sse(self) -> str:
        """An **unnamed** SSE frame.  Named frames never reach `onmessage`, and
        a page written against the unnamed dispatch stream goes silently deaf
        on a named one — that cost us a debugging session once already."""
        return "data: " + json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True) + "\n\n"


def emit(name: str, *, run_id: str = "", seq: int = 0, secrets: Tuple[str, ...] = (),
         **payload: Any) -> Event:
    """Build a validated, already-redacted event in one call."""
    known = {"owner", "project_id", "session_id"}
    envelope = {k: payload.pop(k) for k in list(payload) if k in known}
    return Event.parse({
        "name": name, "run_id": run_id, "seq": seq, "data": payload, **envelope,
    }).redact(secrets)
