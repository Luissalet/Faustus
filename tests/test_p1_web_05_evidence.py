"""WEB-05 — screenshots as actionable evidence.

``src/browser_evidence.py`` did not exist before this lote: a screenshot's
resolution/scale/viewport/timestamp/page were never recorded together, an
edit point had no visual reference shape, and nothing checked a capture
against the CURRENT page before treating it as accurate.
"""
import base64

import pytest

from src.browser_evidence import (
    AnnotationPoint,
    ElementRegion,
    ScreenshotCapture,
    build_capture,
    compare_captures,
    evidence_for_capture,
    is_stale_for_edit,
)

_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-bytes").decode()


def test_build_capture_records_resolution_scale_viewport_timestamp_page():
    cap = build_capture(
        url="https://example.org/dash", title="Dashboard",
        width=1280, height=720, scale=0.667,
        viewport_width=1920, viewport_height=1080,
        image_b64=_PNG_B64, dom_hash="abc123",
    )
    m = cap.to_mapping()
    assert m["url"] == "https://example.org/dash"
    assert m["resolution"] == {"width": 1280, "height": 720}
    assert m["scale"] == pytest.approx(0.667)
    assert m["viewport"] == {"width": 1920, "height": 1080}
    assert m["captured_at"]  # timestamp present
    assert m["image_sha256"]
    assert m["dom_hash"] == "abc123"


def test_element_crop_and_annotation_are_pixels_not_a_code_line():
    region = ElementRegion(x=10, y=20, width=100, height=40, selector="button.save")
    point = AnnotationPoint(x=55, y=40, label="clicked here")
    cap = build_capture(
        url="https://example.org", width=100, height=100,
        image_b64=_PNG_B64, region=region, annotation=point,
    )
    m = cap.to_mapping()
    assert m["region"] == {"x": 10, "y": 20, "width": 100, "height": 40, "selector": "button.save"}
    assert m["annotation"] == {"x": 55, "y": 40, "label": "clicked here"}


def test_element_region_rejects_non_positive_size():
    with pytest.raises(ValueError):
        ElementRegion(x=0, y=0, width=0, height=10)


def test_evidence_for_capture_reuses_evidence_ref_shape():
    cap = build_capture(url="https://example.org/a", width=800, height=600, image_b64=_PNG_B64)
    evidence = evidence_for_capture(cap, owner_id="alice", project_id="proj1")
    m = evidence.to_mapping()
    assert m["source_type"] == "media"
    assert m["owner_id"] == "alice"
    assert m["project_id"] == "proj1"
    assert m["content_sha256"] == cap.image_sha256
    assert m["locator"]["kind"] == "page"
    assert m["locator"]["value"] == "https://example.org/a"


def test_compare_captures_never_assumes_a_change():
    before = build_capture(url="https://example.org", width=100, height=100, image_b64=_PNG_B64, dom_hash="h1")
    same = build_capture(url="https://example.org", width=100, height=100, image_b64=_PNG_B64, dom_hash="h1")
    diff = compare_captures(before, same)
    assert diff["image_changed"] is False
    assert diff["dom_changed"] is False
    assert diff["same_page"] is True

    after = build_capture(
        url="https://example.org", width=100, height=100,
        image_b64=base64.b64encode(b"different-bytes").decode(), dom_hash="h2",
    )
    diff2 = compare_captures(before, after)
    assert diff2["image_changed"] is True
    assert diff2["dom_changed"] is True


# ---------------------------------------------------------------------------
# The acceptance criterion itself: a previous capture is never used as the
# exact map of a NEW page without re-inspecting it.
# ---------------------------------------------------------------------------

def test_capture_from_a_different_url_is_stale_for_edit():
    cap = build_capture(url="https://example.org/old-page", width=100, height=100, image_b64=_PNG_B64)
    reason = is_stale_for_edit(cap, current_url="https://example.org/new-page")
    assert reason is not None
    assert "old-page" in reason and "new-page" in reason


def test_capture_whose_dom_changed_is_stale_for_edit():
    cap = build_capture(url="https://example.org", width=100, height=100, image_b64=_PNG_B64, dom_hash="v1")
    reason = is_stale_for_edit(cap, current_url="https://example.org", current_dom_hash="v2")
    assert reason is not None
    assert "changed" in reason


def test_matching_capture_is_not_stale():
    cap = build_capture(url="https://example.org", width=100, height=100, image_b64=_PNG_B64, dom_hash="v1")
    assert is_stale_for_edit(cap, current_url="https://example.org", current_dom_hash="v1") is None


# This IS the regression test for the acceptance criterion: without
# `is_stale_for_edit`'s URL check, nothing distinguishes a capture of the
# old page from one of the new page -- reverting the function to always
# `return None` makes this test fail.
def test_regression_stale_capture_must_not_pass_as_current():
    cap = build_capture(url="https://a.example/checkout", width=10, height=10, image_b64=_PNG_B64)
    assert is_stale_for_edit(cap, current_url="https://a.example/cart") is not None
