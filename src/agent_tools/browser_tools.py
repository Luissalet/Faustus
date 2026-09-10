"""agent_tools/browser_tools.py — WEB-05/WEB-06 tool wrappers (Lote 54).

Both `src/browser_evidence.py` (WEB-05) and `src/browser_extraction.py`
(WEB-06) were built as plain-data libraries with real tests (Lote 48) but no
tool the model could call — this module is that wiring, in the same thin
"parse args, call the library, format the result" shape `code_tools.py`
already uses. No new evidence/extraction logic lives here.

    capture_evidence   WEB-05: build / compare / check staleness of a screenshot
    browser_extract    WEB-06: paginate / form limits / download sandbox+verify

`capture_evidence` takes image data the model already has (from
`desktop_screenshot`, a browser MCP screenshot, or any prior capture) — it
does not grab a screenshot itself, so it works with any capture source
without this module knowing which one produced it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from src.browser_evidence import (
    AnnotationPoint,
    ElementRegion,
    ScreenshotCapture,
    build_capture,
    compare_captures,
    evidence_for_capture,
    is_stale_for_edit,
)
from src.browser_extraction import (
    DownloadExpectation,
    FormFillLimits,
    PageResult,
    PaginationLimits,
    check_form_fill,
    check_login_reuse,
    detect_restricted_access,
    paginate,
    select_download_target,
    verify_download,
)

logger = logging.getLogger(__name__)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _capture_from_mapping(d: Dict[str, Any]) -> ScreenshotCapture:
    """Reconstruct a `ScreenshotCapture` from the mapping `to_mapping()`
    produces (and this tool returns) — so a caller can hold onto a prior
    `capture` result and hand it back for `compare`/`check_stale` without
    re-uploading the image bytes."""
    res = d.get("resolution") or {}
    vp = d.get("viewport") or {}
    region_d = d.get("region")
    annotation_d = d.get("annotation")
    return ScreenshotCapture(
        url=str(d.get("url") or ""),
        title=str(d.get("title") or ""),
        width=int(res.get("width") or 0),
        height=int(res.get("height") or 0),
        scale=float(d.get("scale") or 1.0),
        viewport_width=int(vp.get("width") or res.get("width") or 0),
        viewport_height=int(vp.get("height") or res.get("height") or 0),
        captured_at=str(d.get("captured_at") or ""),
        image_sha256=str(d.get("image_sha256") or ""),
        dom_hash=str(d.get("dom_hash") or ""),
        region=ElementRegion(**region_d) if isinstance(region_d, dict) and region_d else None,
        annotation=AnnotationPoint(**annotation_d) if isinstance(annotation_d, dict) and annotation_d else None,
    )


class CaptureEvidenceTool:
    """`capture_evidence` (WEB-05)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or "build").strip().lower()
        try:
            if action == "build":
                region_arg = args.get("region")
                region = ElementRegion(**region_arg) if isinstance(region_arg, dict) and region_arg else None
                annotation_arg = args.get("annotation")
                annotation = (
                    AnnotationPoint(**annotation_arg) if isinstance(annotation_arg, dict) and annotation_arg else None
                )
                capture = build_capture(
                    url=str(args.get("url") or ""),
                    title=str(args.get("title") or ""),
                    width=int(args.get("width") or 0),
                    height=int(args.get("height") or 0),
                    scale=float(args.get("scale") or 1.0),
                    viewport_width=args.get("viewport_width"),
                    viewport_height=args.get("viewport_height"),
                    image_b64=args.get("image_b64"),
                    dom_hash=str(args.get("dom_hash") or ""),
                    region=region,
                    annotation=annotation,
                )
                evidence = evidence_for_capture(
                    capture,
                    owner_id=str((ctx or {}).get("owner") or "system"),
                    project_id=(ctx or {}).get("project_id"),
                )
                return {
                    "output": f"Captured evidence for {capture.url or '(no url)'} "
                              f"({capture.width}x{capture.height}, sha256={capture.image_sha256[:16]}...).",
                    "exit_code": 0,
                    "capture": capture.to_mapping(),
                    "evidence": evidence.to_mapping(),
                }
            if action == "compare":
                before_d, after_d = args.get("before"), args.get("after")
                if not isinstance(before_d, dict) or not isinstance(after_d, dict):
                    return {"error": "capture_evidence: `before` and `after` must each be a prior "
                                     "`capture` result", "exit_code": 1}
                diff = compare_captures(_capture_from_mapping(before_d), _capture_from_mapping(after_d))
                return {
                    "output": (
                        f"same_page={diff['same_page']} image_changed={diff['image_changed']} "
                        f"dom_changed={diff['dom_changed']}"
                    ),
                    "exit_code": 0, **diff,
                }
            if action == "check_stale":
                capture_d = args.get("capture")
                if not isinstance(capture_d, dict):
                    return {"error": "capture_evidence: `capture` (a prior `capture` result) is required",
                            "exit_code": 1}
                reason = is_stale_for_edit(
                    _capture_from_mapping(capture_d),
                    current_url=str(args.get("current_url") or ""),
                    current_dom_hash=str(args.get("current_dom_hash") or ""),
                )
                return {
                    "output": reason or "the capture still matches the current page; safe to use as an edit point.",
                    "exit_code": 0, "stale": reason is not None, "reason": reason,
                }
            return {"error": f"capture_evidence: unknown action {action!r} "
                             f"(use \"build\", \"compare\" or \"check_stale\")", "exit_code": 1}
        except (ValueError, TypeError) as exc:
            return {"error": f"capture_evidence: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - tools never raise
            logger.warning("capture_evidence failed: %s", exc, exc_info=True)
            return {"error": f"capture_evidence: {type(exc).__name__}: {exc}", "exit_code": 1}


class BrowserExtractTool:
    """`browser_extract` (WEB-06)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_search_root

        args = _args(content)
        action = str(args.get("action") or "").strip().lower()
        try:
            if action == "paginate":
                pages = args.get("pages")
                if not isinstance(pages, list) or not pages:
                    return {"error": "browser_extract: `pages` (a list of {items, has_next}) is required",
                            "exit_code": 1}
                parsed = [
                    PageResult(page_index=i, items=tuple(p.get("items") or ()), has_next=bool(p.get("has_next")))
                    for i, p in enumerate(pages) if isinstance(p, dict)
                ]

                async def _fetch_page(i: int) -> PageResult:
                    return parsed[i]

                limits = PaginationLimits(
                    max_pages=int(args.get("max_pages") or 20),
                    max_items=int(args.get("max_items") or 2000),
                )
                result = await paginate(_fetch_page, limits=limits)
                return {
                    "output": f"{result['item_count']} item(s) over {result['pages_fetched']} page(s)"
                              + (f" — truncated ({result['reason']})" if result["truncated"] else ""),
                    "exit_code": 0, **result,
                }
            if action == "detect_restricted_access":
                marker = detect_restricted_access(str(args.get("page_text") or ""))
                return {"output": marker or "no restricted-access marker found", "exit_code": 0,
                        "restricted": marker is not None, "marker": marker}
            if action == "check_form_fill":
                fields = args.get("fields")
                if not isinstance(fields, dict):
                    return {"error": "browser_extract: `fields` must be an object", "exit_code": 1}
                limits = FormFillLimits(
                    max_fields=int(args.get("max_fields") or 50),
                    max_value_chars=int(args.get("max_value_chars") or 4000),
                )
                reason = check_form_fill(fields, limits=limits)
                return {"output": reason or "form fill within limits", "exit_code": 0,
                        "allowed": reason is None, "reason": reason}
            if action == "select_download_target":
                sandbox_root = _resolve_search_root(str(args.get("sandbox_root") or ""))
                filename = str(args.get("filename") or "")
                if not filename:
                    return {"error": "browser_extract: `filename` is required", "exit_code": 1}
                target = select_download_target(sandbox_root, filename)
                return {"output": f"Download target: {target}", "exit_code": 0, "path": target}
            if action == "verify_download":
                path = str(args.get("path") or "")
                if not path:
                    return {"error": "browser_extract: `path` is required", "exit_code": 1}
                expectation = DownloadExpectation(
                    expected_sha256=args.get("expected_sha256") or None,
                    expected_bytes=args.get("expected_bytes"),
                )
                outcome = verify_download(
                    path, expectation, expected_total_bytes=args.get("expected_total_bytes"),
                )
                return {
                    "output": f"{'complete' if outcome.complete else 'INCOMPLETE'}: {outcome.reason}",
                    "exit_code": 0 if outcome.complete else 1, **outcome.to_mapping(),
                }
            if action == "check_login_reuse":
                from src.browser_sessions import get_session

                task_id = str(args.get("task_id") or "")
                owner = str((ctx or {}).get("owner") or "")
                session = get_session(owner, task_id) if task_id else None
                if session is None:
                    return {"error": f"browser_extract: no open browser session for task {task_id!r}",
                            "exit_code": 1}
                allowed, reason = check_login_reuse(session)
                return {"output": reason or "reusing the saved login is allowed for this session",
                        "exit_code": 0, "allowed": allowed, "reason": reason}
            return {"error": f"browser_extract: unknown action {action!r}", "exit_code": 1}
        except (ValueError, TypeError, OSError) as exc:
            return {"error": f"browser_extract: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - tools never raise
            logger.warning("browser_extract failed: %s", exc, exc_info=True)
            return {"error": f"browser_extract: {type(exc).__name__}: {exc}", "exit_code": 1}
