"""Stable references for content handed from one Hoard app to another.

References carry identity and provenance, not file paths or content. Apps keep
their own storage and authorize reads through their existing agent contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit


@dataclass(frozen=True)
class ArtifactRef:
    app: str
    kind: str
    id: str

    @property
    def uri(self) -> str:
        return f"hoard://{quote(self.app, safe='')}/{quote(self.kind, safe='')}/{quote(self.id, safe='')}"


def parse_ref(uri: str) -> ArtifactRef:
    parsed = urlsplit(uri)
    if parsed.scheme != "hoard" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("invalid Hoard artifact reference")
    parts = parsed.path.lstrip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("Hoard artifact reference needs kind and id")
    app, kind, artifact_id = unquote(parsed.netloc), unquote(parts[0]), unquote(parts[1])
    if not all((app, kind, artifact_id)) or any(c in app for c in "/\\"):
        raise ValueError("invalid Hoard artifact reference")
    return ArtifactRef(app, kind, artifact_id)


def revision_for(data: bytes | str) -> str:
    """Content revision for idempotent imports; UTF-8 for text."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(payload).hexdigest()
