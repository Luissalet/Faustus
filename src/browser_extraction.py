"""Complex navigation and extraction (WEB-06).

``docs/spec/v2/backlog.json`` (WEB-06): pagination, downloads and forms need
declared limits; an attachment lands in the sandbox with its hash/size
checked, not trusted on the browser's say-so; a restricted page is reported
as restricted, never quietly scraped around. The acceptance criterion is
concrete: a half-downloaded file must never be reported as complete, and a
"pick one file" result must never hand back a path that reaches the rest of
the folder.

Every function here is plain data in, plain data out -- pagination takes an
INJECTED async page-fetcher (mirrors ``src/browser_actions.py``'s
``snapshot_fn``/``act_fn`` injection, so this is testable with a fake
paginator and no real browser), and the download/sandbox helpers work on
paths and byte counts a caller already has. Login-session reuse defers to
``src/browser_sessions.policy_allows`` (WEB-03's own authority) instead of a
second downloads/logins flag living here (rule 4).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Pagination / scroll / tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaginationLimits:
    """Declared caps -- WEB-06 requires "límites y readback", not an
    unbounded crawl that only stops when the site runs out of pages."""

    max_pages: int = 20
    max_items: int = 2000


@dataclass(frozen=True)
class PageResult:
    page_index: int
    items: Tuple[Mapping[str, Any], ...]
    has_next: bool


FetchPageFn = Callable[[int], Awaitable[PageResult]]
ProgressFn = Callable[[Dict[str, Any]], Awaitable[None]]


async def paginate(
    fetch_page: FetchPageFn,
    *,
    limits: PaginationLimits = PaginationLimits(),
    progress_cb: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """Walk pages (or scroll-infinite "pages") via `fetch_page` until it says
    there is no next page, or a limit is hit -- whichever comes first.

    Always returns a `truncated`/`reason` pair rather than silently stopping,
    so a caller can tell "the site ran out" from "the limit cut this off"
    (the same distinction ``web_tools.WebFetchTool`` already makes for a
    download-budget cutoff on a single page, extended here to a walk over
    many).
    """
    items: List[Mapping[str, Any]] = []
    page_index = 0
    truncated = False
    reason = "exhausted"
    while True:
        page = await fetch_page(page_index)
        items.extend(page.items)
        page_index += 1
        if progress_cb:
            await progress_cb({"page": page_index, "items_so_far": len(items)})
        if len(items) >= limits.max_items:
            truncated = True
            reason = "max_items"
            break
        if page_index >= limits.max_pages:
            truncated = bool(page.has_next)
            reason = "max_pages" if page.has_next else "exhausted"
            break
        if not page.has_next:
            break
    return {
        "items": list(items[: limits.max_items]),
        "pages_fetched": page_index,
        "item_count": min(len(items), limits.max_items),
        "truncated": truncated,
        "reason": reason,
    }


def project_fields(row: Mapping[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    """Only the requested columns of `row` -- never the whole row, which may
    carry fields the caller never asked to see (WEB-06 frontend: "posibilidad
    de seleccionar campos")."""
    return {f: row[f] for f in fields if f in row}


# ---------------------------------------------------------------------------
# Restricted access
# ---------------------------------------------------------------------------

_RESTRICTED_MARKERS: Tuple[str, ...] = (
    "access denied", "acceso denegado", "accès refusé", "zugriff verweigert",
    "please sign in to continue", "inicia sesión para continuar",
    "you do not have permission", "no tienes permiso",
    "403 forbidden", "401 unauthorized",
    "this content is not available in your country",
)


def detect_restricted_access(page_text: str) -> Optional[str]:
    """The marker phrase found in `page_text`, or ``None``.

    A hit means the page did not hand over what was asked for -- extraction
    must report that plainly (WEB-06: "no evadir accesos restringidos")
    instead of returning whatever fragment of the block page happened to
    parse as "content".
    """
    low = (page_text or "").lower()
    for marker in _RESTRICTED_MARKERS:
        if marker in low:
            return marker
    return None


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormFillLimits:
    max_fields: int = 50
    max_value_chars: int = 4000


def check_form_fill(fields: Mapping[str, Any], *, limits: FormFillLimits = FormFillLimits()) -> Optional[str]:
    """``None`` when `fields` is within `limits`; otherwise why it is refused."""
    if len(fields) > limits.max_fields:
        return f"form fill has {len(fields)} fields, limit is {limits.max_fields}"
    for key, value in fields.items():
        if len(str(value)) > limits.max_value_chars:
            return f"field {key!r} value exceeds {limits.max_value_chars} characters"
    return None


# ---------------------------------------------------------------------------
# Downloads: sandbox + completeness
# ---------------------------------------------------------------------------


def select_download_target(sandbox_root: str, filename: str) -> str:
    """One sandboxed file path for `filename` inside `sandbox_root`.

    Always a single FILE path, never a directory handle -- a caller that
    hands this back to the model cannot "expose the whole folder" (WEB-06's
    acceptance criterion) through it, because nothing else under
    `sandbox_root` is reachable from a single filename. `filename` is
    basename-only (any directory component from the site is discarded) and
    the resolved path is checked to still be inside `sandbox_root` before
    it is returned, closing the classic `../../etc/passwd` escape.
    """
    safe_name = os.path.basename(str(filename or "").strip()) or "download.bin"
    root = os.path.realpath(sandbox_root)
    target = os.path.realpath(os.path.join(root, safe_name))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"download target {filename!r} escapes the sandbox root")
    return target


@dataclass(frozen=True)
class DownloadExpectation:
    expected_sha256: Optional[str] = None
    expected_bytes: Optional[int] = None


@dataclass(frozen=True)
class DownloadOutcome:
    path: str
    bytes_written: int
    sha256: str
    complete: bool
    reason: str

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "bytes_written": self.bytes_written,
            "sha256": self.sha256,
            "complete": self.complete,
            "reason": self.reason,
        }


def verify_download(
    path: str,
    expectation: DownloadExpectation = DownloadExpectation(),
    *,
    expected_total_bytes: Optional[int] = None,
) -> DownloadOutcome:
    """Read `path` off disk and check it against what was expected.

    `complete` is the WEB-06 acceptance criterion made a field instead of an
    assumption: a download the transfer cut short (`expected_total_bytes`
    larger than what actually landed) or that does not match a declared
    hash/size comes back with `complete=False` and a reason, and a caller
    must never present that as a finished download.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    complete = True
    reason = "ok"
    if expectation.expected_bytes is not None and size != expectation.expected_bytes:
        complete = False
        reason = f"expected {expectation.expected_bytes} bytes, got {size}"
    elif expected_total_bytes is not None and size < expected_total_bytes:
        complete = False
        reason = f"download stopped at {size} of {expected_total_bytes} bytes"
    elif expectation.expected_sha256 and digest != expectation.expected_sha256:
        complete = False
        reason = f"sha256 mismatch (expected {expectation.expected_sha256[:12]}..., got {digest[:12]}...)"
    return DownloadOutcome(path=path, bytes_written=size, sha256=digest, complete=complete, reason=reason)


def check_login_reuse(session: Any) -> Tuple[bool, Optional[str]]:
    """(allowed, reason) for reusing the session's saved login on a form --
    delegates to WEB-03's own policy authority instead of a second one."""
    from src.browser_sessions import policy_allows

    return policy_allows(session, "reuse_login")
