"""Screenshots as actionable evidence (WEB-05).

``docs/spec/v2/backlog.json`` (WEB-05): a browser screenshot must record
resolution, scale, viewport, timestamp and page -- and the point an edit
refers to must be a VISUAL reference (a pixel region, an annotated point),
never a fabricated line-of-code the model never actually read. Today
``src/browser_view.py`` grabs a screenshot for the UI panel and throws away
everything except the raw bytes; nothing in the repo records what the
capture actually shows or lets a caller tell a stale capture from a fresh
one.

This module is the seam: :func:`build_capture` turns one screenshot into a
:class:`ScreenshotCapture` carrying everything WEB-05 requires, optionally
cropped to an element region or annotated at a point;
:func:`evidence_for_capture` wraps it as the same :class:`~src.contracts.EvidenceRef`
shape ``src/agent_tools/web_tools.py`` already gives `web_fetch` results
(rule 4 -- reuse the existing evidence authority, don't invent a second
one); :func:`compare_captures` is the explicit before/after diff WEB-05
asks for; and :func:`is_stale_for_edit` is the acceptance criterion itself,
made a function instead of a hope: "a previous capture is never used as the
exact map of a new page without re-inspecting it".

Every function here takes plain data (bytes/base64, hashes, dimensions) --
never a Playwright/MCP handle -- so it is testable with no browser or
network, the same shape ``src/browser_actions.py`` uses for its own
snapshot/act injection.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.contracts import EvidenceLocator, EvidenceRef
from src.contracts.base import now_iso


@dataclass(frozen=True)
class ElementRegion:
    """A crop expressed as pixels of the CAPTURED image -- never a code line
    or a CSS selector alone, so it survives a re-render that changed the
    DOM but not the visible layout."""

    x: int
    y: int
    width: int
    height: int
    selector: str = ""

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("ElementRegion width/height must be positive")
        if self.x < 0 or self.y < 0:
            raise ValueError("ElementRegion x/y must not be negative")

    def to_mapping(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"x": self.x, "y": self.y, "width": self.width, "height": self.height}
        if self.selector:
            out["selector"] = self.selector
        return out


@dataclass(frozen=True)
class AnnotationPoint:
    """One coordinate annotated on the capture (WEB-05: "anotación de
    coordenadas"), with an optional short human label."""

    x: int
    y: int
    label: str = ""

    def to_mapping(self) -> Dict[str, Any]:
        return {"x": self.x, "y": self.y, "label": self.label}


@dataclass(frozen=True)
class ScreenshotCapture:
    """Everything WEB-05 requires to be recorded about ONE screenshot."""

    url: str
    title: str
    width: int
    height: int
    scale: float
    viewport_width: int
    viewport_height: int
    captured_at: str
    image_sha256: str
    # Fingerprint of the page's DOM/accessibility snapshot AT capture time --
    # the staleness signal `is_stale_for_edit` checks against a fresh one.
    dom_hash: str = ""
    region: Optional[ElementRegion] = None
    annotation: Optional[AnnotationPoint] = None

    def to_mapping(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "url": self.url,
            "title": self.title,
            "resolution": {"width": self.width, "height": self.height},
            "scale": self.scale,
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "captured_at": self.captured_at,
            "image_sha256": self.image_sha256,
            "dom_hash": self.dom_hash,
        }
        if self.region is not None:
            out["region"] = self.region.to_mapping()
        if self.annotation is not None:
            out["annotation"] = self.annotation.to_mapping()
        return out


def _image_sha256(*, image_b64: Optional[str], image_bytes: Optional[bytes]) -> str:
    if image_bytes is not None:
        return hashlib.sha256(image_bytes).hexdigest()
    if image_b64:
        try:
            return hashlib.sha256(base64.b64decode(image_b64, validate=False)).hexdigest()
        except (binascii.Error, ValueError):
            # Not valid base64 -- still hash the raw string deterministically
            # rather than raising: a capture with an unreadable image is a
            # worse failure than one with an image hash that only means
            # "this string", so callers keep a usable evidence record.
            return hashlib.sha256(image_b64.encode("utf-8", "replace")).hexdigest()
    raise ValueError("build_capture requires image_b64 or image_bytes")


def build_capture(
    *,
    url: str,
    title: str = "",
    width: int,
    height: int,
    scale: float = 1.0,
    viewport_width: Optional[int] = None,
    viewport_height: Optional[int] = None,
    image_b64: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    dom_hash: str = "",
    region: Optional[ElementRegion] = None,
    annotation: Optional[AnnotationPoint] = None,
    captured_at: Optional[str] = None,
) -> ScreenshotCapture:
    """Build one :class:`ScreenshotCapture` from a raw screenshot result.

    ``viewport_width``/``viewport_height`` default to the image's own
    ``width``/``height`` -- a caller that only has the rendered image (no
    separate viewport report) still gets a valid capture instead of an
    error, matching how ``desktop_tools.DesktopTool._screenshot`` already
    reports "captured WxH, returned as W'xH'" with the viewport implicit.
    """
    return ScreenshotCapture(
        url=str(url or "")[:2048],
        title=str(title or "")[:300],
        width=int(width),
        height=int(height),
        scale=float(scale),
        viewport_width=int(viewport_width if viewport_width is not None else width),
        viewport_height=int(viewport_height if viewport_height is not None else height),
        captured_at=captured_at or now_iso(),
        image_sha256=_image_sha256(image_b64=image_b64, image_bytes=image_bytes),
        dom_hash=dom_hash or "",
        region=region,
        annotation=annotation,
    )


def evidence_for_capture(
    capture: ScreenshotCapture,
    *,
    owner_id: str,
    project_id: Optional[str] = None,
    retention: str = "task",
) -> EvidenceRef:
    """The same :class:`~src.contracts.EvidenceRef` shape
    ``web_tools._evidence_for_fetch`` gives a `web_fetch` result, applied to
    a screenshot instead of a page's text (rule 4: one evidence authority,
    not a second one for images)."""
    evidence_id = "evi_shot_" + hashlib.sha256(
        f"{capture.url}:{capture.image_sha256}:{capture.captured_at}".encode("utf-8", "replace")
    ).hexdigest()[:24]
    locator = EvidenceLocator(kind="page", value=(capture.url or "")[:512])
    return EvidenceRef(
        evidence_id=evidence_id,
        owner_id=owner_id or "system",
        project_id=project_id,
        source_type="media",
        source_ref=(capture.url or "")[:512],
        source_revision=capture.image_sha256[:16],
        content_sha256=capture.image_sha256,
        captured_at=capture.captured_at,
        locator=locator,
        derived_from=(),
        retention=retention,
    )


def compare_captures(before: ScreenshotCapture, after: ScreenshotCapture) -> Dict[str, Any]:
    """Explicit before/after comparison (WEB-05: "comparación antes/después").

    Never guesses: ``image_changed``/``dom_changed`` are plain hash
    inequality, and ``same_page`` is a URL comparison -- a caller asking
    "did my action actually do anything" gets a real answer instead of two
    screenshots it has to eyeball itself.
    """
    return {
        "same_page": bool(before.url) and before.url == after.url,
        "image_changed": before.image_sha256 != after.image_sha256,
        "dom_changed": bool(before.dom_hash) and bool(after.dom_hash) and before.dom_hash != after.dom_hash,
        "before": before.to_mapping(),
        "after": after.to_mapping(),
    }


def is_stale_for_edit(capture: ScreenshotCapture, *, current_url: str, current_dom_hash: str = "") -> Optional[str]:
    """``None`` when `capture` still matches the CURRENT page and may be used
    as an edit point; otherwise the reason it may not.

    This IS the WEB-05 acceptance criterion: "a previous capture is never
    used as the exact map of a new page without re-inspecting it." Mirrors
    ``browser_actions.check_precondition``'s shape (a reason string or
    ``None``) so a caller decides what to do next instead of an exception
    interrupting a flow that may want to just re-capture and continue.
    """
    if capture.url and current_url and capture.url != current_url:
        return (
            f"this capture was taken on {capture.url!r}; the current page is "
            f"{current_url!r} -- re-inspect the page before using this capture as an edit point"
        )
    if capture.dom_hash and current_dom_hash and capture.dom_hash != current_dom_hash:
        return (
            "the page has changed since this capture was taken -- re-inspect it before "
            "using this capture as an edit point"
        )
    return None
