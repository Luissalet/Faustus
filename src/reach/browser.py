"""Owner-scoped local browser reads and bounded JSON response capture.

Uses the family's installed browser transport. Its request guard applies to
every subrequest; profiles never reuse the person's normal Chrome profile.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.browser_extraction import detect_restricted_access
from src.constants import DATA_DIR
from src.contracts.base import now_iso
from src.hoard_link.web.browser import BrowserRung
from src.reach.extract import MAX_BODY_BYTES, _safe_ref

_BROWSERS = {}
_LOCK = threading.Lock()
_PROFILE_LOCKS = {}


class BrowserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["read", "capture", "status", "close"] = "read"
    url: str | None = Field(default=None, max_length=2048)
    profile: str = Field(default="research", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    endpoint_contains: str | None = Field(default=None, min_length=1, max_length=2048)
    timeout_s: int = Field(default=25, ge=1, le=60)
    scrolls: int = Field(default=0, ge=0, le=20)
    max_responses: int = Field(default=10, ge=1, le=30)

    @model_validator(mode="after")
    def source(self):
        if self.action in ("read", "capture") and (not self.url or urlsplit(self.url).scheme not in ("http", "https")):
            raise ValueError("an HTTP(S) URL is required")
        if self.action == "capture" and not self.endpoint_contains:
            raise ValueError("capture needs endpoint_contains")
        return self


def profile_path(owner, profile="research"):
    key = hashlib.sha256(str(owner or "").encode()).hexdigest()
    return Path(DATA_DIR) / "reach-browser" / key / profile


def browser_for(owner=None, profile="research"):
    path = profile_path(owner, profile)
    with _LOCK:
        if str(path) not in _BROWSERS:
            _BROWSERS[str(path)] = BrowserRung(path, headless=True, idle_s=60)
        return _BROWSERS[str(path)]


@contextmanager
def _profile_use(owner, profile, timeout_s, cancel=None):
    """Queued callers cannot visit a page after their async request was cancelled."""
    key = str(profile_path(owner, profile))
    with _LOCK:
        gate = _PROFILE_LOCKS.setdefault(key, threading.Lock())
    deadline = time.monotonic() + timeout_s
    acquired = False
    try:
        while not acquired:
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                raise ValueError('browser profile wait cancelled or timed out')
            acquired = gate.acquire(blocking=False)
            if not acquired:
                if cancel is None: time.sleep(.05)
                else: cancel.wait(.05)
        yield max(1, deadline - time.monotonic())
    finally:
        if acquired: gate.release()


def read_response(url, *, owner=None, profile="research", timeout_s=25, _cancel=None):
    with _profile_use(owner, profile, timeout_s, _cancel) as remaining:
        return _read_response(url, owner=owner, profile=profile, timeout_s=remaining)


def _read_response(url, *, owner=None, profile="research", timeout_s=25):
    fr = browser_for(owner, profile).fetch(url, timeout_s=timeout_s)
    if fr.error or fr.status >= 400:
        raise ValueError(fr.error or f"browser HTTP {fr.status}")
    body = fr.text.encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("browser document exceeds byte budget")
    soup = BeautifulSoup(fr.text, "html.parser")
    restricted = detect_restricted_access(soup.get_text(" ", strip=True))
    if restricted:
        raise ValueError("restricted page: " + restricted)
    return httpx.Response(fr.status or 200, content=body,
        headers={"content-type": fr.content_type or "text/html"},
        request=httpx.Request("GET", fr.final_url or url))


def capture(request: BrowserRequest, *, owner=None, _cancel=None):
    with _profile_use(owner, request.profile, request.timeout_s, _cancel) as remaining:
        return _capture(request, owner=owner, remaining=remaining, cancel=_cancel)


def _capture(request: BrowserRequest, *, owner=None, remaining=25, cancel=None):
    browser = browser_for(owner, request.profile)
    # browser_session() installs the family guard before yielding the context.
    # Never allow a refused top-level URL to be opened by another code path.
    from src.hoard_link.web import safety
    problem = safety.check_url(request.url, safety.PUBLIC)
    if problem:
        raise ValueError(problem)
    results, seen, total = [], set(), 0
    truncated = False
    errors = 0
    started = time.monotonic()
    with browser.browser_session(headless=True) as context:
        page = context.new_page()
        def response_received(response):
            nonlocal total, truncated, errors
            ref = _safe_ref(response.url)
            if request.endpoint_contains not in ref or not 200 <= response.status < 300:
                return
            mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if mime != "application/json" and not mime.endswith("+json"):
                return
            if len(results) >= request.max_responses:
                truncated = True
                return
            try:
                declared = int(response.headers.get("content-length", "0"))
                if declared > MAX_BODY_BYTES:
                    truncated = True
                    return
                body = response.body()
                if len(body) > MAX_BODY_BYTES or total + len(body) > MAX_BODY_BYTES:
                    truncated = True
                    return
                digest = hashlib.sha256(body).hexdigest()
                identity = (ref, digest)
                if identity in seen:
                    return
                data = json.loads(body)
                seen.add(identity)
                total += len(body)
                results.append({"url": ref, "status": response.status, "sha256": digest,
                    "bytes": len(body), "fetched_at": now_iso(), "data": data})
            except Exception:
                errors += 1
        page.on("response", response_received)
        try:
            page.goto(request.url, wait_until="domcontentloaded", timeout=max(1, round((remaining - (time.monotonic() - started)) * 1000)))
            # Fixed bounded scrolls trigger a site's own pagination, not code
            # supplied by a page or model. A deadline covers settle and scrolling.
            for index in range(request.scrolls + 1):
                if (cancel is not None and cancel.is_set()) or time.monotonic() - started >= remaining:
                    truncated = True
                    break
                if index:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
            settle = min(2000, max(0, round((remaining - (time.monotonic() - started)) * 1000)))
            if settle and not (cancel is not None and cancel.is_set()):
                page.wait_for_timeout(settle)
            restricted = detect_restricted_access(page.inner_text("body")[:10000])
            if not results:
                raise ValueError("restricted page: " + restricted if restricted else "no matching JSON response captured")
        finally:
            page.close()
    return {"results": results, "captured_count": len(results), "truncated": truncated,
        "unreadable_responses": errors, "untrusted_content": True, "profile": request.profile}


async def run_browser(request: BrowserRequest, *, owner=None):
    if request.action == "capture":
        return await _cancellable_thread(capture, request, owner=owner)
    browser = browser_for(owner, request.profile)
    if request.action == "status":
        return {"available": browser.available(), "reason": browser.unavailable_reason(), "profile": request.profile}
    if request.action == "close":
        await asyncio.to_thread(browser.close)
        with _LOCK:
            _BROWSERS.pop(str(profile_path(owner, request.profile)), None)
        return {"closed": True, "profile": request.profile}
    response = await _cancellable_thread(read_response, request.url, owner=owner,
        profile=request.profile, timeout_s=request.timeout_s)
    soup = BeautifulSoup(response.text, "html.parser")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    text = soup.get_text("\n", strip=True)
    return {"url": _safe_ref(str(response.url)), "title": soup.title.get_text() if soup.title else "",
        "text": text[:24000], "truncated": len(text) > 24000, "untrusted_content": True}


async def _cancellable_thread(fn, *args, **kwargs):
    signal = threading.Event()
    try:
        return await asyncio.to_thread(fn, *args, **kwargs, _cancel=signal)
    except asyncio.CancelledError:
        signal.set()
        raise
