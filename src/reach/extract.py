"""Structured extraction over the family's guarded transport.

Patterns from Scrapling (selectors) and browser network research (JSON bodies).
No upstream code, browser installation, generated script or implicit fuzzy match.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.browser_extraction import detect_restricted_access
from src.contracts.base import now_iso

MAX_BODY_BYTES = 2_000_000
MAX_VALUE_CHARS = 4000
Selector = Annotated[str, Field(min_length=1, max_length=1000)]


class ExtractField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selectors: list[Selector] = Field(default_factory=list, max_length=5)
    attribute: str | None = Field(default=None, min_length=1, max_length=100)
    pointer: str | None = Field(default=None, max_length=2048)


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    html: str | None = Field(default=None, max_length=MAX_BODY_BYTES)
    json_data: Any = None
    har: dict[str, Any] | None = None
    endpoint_contains: str | None = Field(default=None, min_length=1, max_length=4096)
    row_selectors: list[Selector] = Field(default_factory=list, max_length=5)
    rows_pointer: str = Field(default="", max_length=2048)
    fields: dict[str, ExtractField] = Field(min_length=1, max_length=30)
    next_selector: Selector | None = None
    max_pages: int = Field(default=1, ge=1, le=10)
    max_items: int = Field(default=100, ge=1, le=2000)
    transport: Literal["fetch", "browser"] = "fetch"
    profile: str = Field(default="research", pattern=r"^[a-zA-Z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def check_source(self):
        sources = [self.url is not None, self.html is not None,
                   self.json_data is not None, self.har is not None]
        if sum(sources) != 1:
            raise ValueError("provide exactly one of url, html, json_data or har")
        if self.url is not None and (not self.url.strip() or urlsplit(self.url).scheme not in ("http", "https")):
            raise ValueError("url must be an HTTP or HTTPS URL")
        if self.har is not None and not self.endpoint_contains:
            raise ValueError("HAR extraction needs endpoint_contains")
        if self.next_selector and self.url is None:
            raise ValueError("pagination needs url")
        for name, spec in self.fields.items():
            if not name or len(name) > 100:
                raise ValueError("field names must have 1 to 100 characters")
            if bool(spec.selectors) == (spec.pointer is not None):
                raise ValueError("each field needs selectors OR a JSON pointer")
        return self


def json_pointer(value: Any, pointer: str) -> Any:
    """RFC 6901, including literal slash/tilde keys; no generated expressions."""
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with /")
    for part in pointer[1:].split("/"):
        # Reject malformed escapes rather than silently reading another key.
        if any(part[i + 1:i + 2] not in ("0", "1") for i, c in enumerate(part) if c == "~"):
            raise ValueError("invalid JSON pointer escape")
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isascii() or not key.isdigit() or (key != "0" and key.startswith("0")):
                raise KeyError(key)
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise KeyError(key)
    return value


def _safe_ref(url: str) -> str:
    p = urlsplit(url)
    # Captures can contain signed URLs and credentials. Only retain origin/path.
    host = p.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if p.port:
        host += ":" + str(p.port)
    return urlunsplit((p.scheme, host, p.path, "", ""))


def har_body(har: dict, endpoint_contains: str) -> tuple[Any, str, bytes]:
    candidates = []
    for entry in (har.get("log") or {}).get("entries", []):
        url = str((entry.get("request") or {}).get("url") or "")
        if endpoint_contains not in _safe_ref(url):
            continue
        response = entry.get("response") or {}
        content = response.get("content") or {}
        mime = str(content.get("mimeType") or "").split(";", 1)[0].strip().lower()
        if not 200 <= int(response.get("status") or 0) < 300 or not (mime == "application/json" or mime.endswith("+json")):
            continue
        text = content.get("text")
        if not isinstance(text, str):
            continue
        if len(text.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("captured response exceeds byte budget")
        body = base64.b64decode(text, validate=True) if content.get("encoding") == "base64" else text.encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("captured response exceeds byte budget")
        candidates.append((json.loads(body), _safe_ref(url), body))
    if len(candidates) != 1:
        raise ValueError(f"expected one matching JSON response, found {len(candidates)}; narrow the capture or endpoint")
    return candidates[0]


def _value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, (dict, list)):
        raise ValueError("selected fields must be scalar values")
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS], True
    return value, False


def extract_json(data: Any, request: ExtractRequest) -> dict:
    rows = json_pointer(data, request.rows_pointer)
    if not isinstance(rows, list):
        rows = [rows]
    items, missing, shortened = [], [], False
    for index, row in enumerate(rows[:request.max_items]):
        item = {}
        for name, spec in request.fields.items():
            if spec.pointer is None:
                raise ValueError("JSON extraction requires field pointers")
            try:
                value = json_pointer(row, spec.pointer)
            except (KeyError, IndexError):
                item[name] = None
                missing.append({"row": index, "field": name})
                continue
            item[name], cut = _value(value)
            shortened |= cut
        items.append(item)
    return {"items": items, "missing": missing, "selectors_used": {},
            "truncated": len(rows) > request.max_items or shortened}


def extract_html(html: str, request: ExtractRequest, *, base_url: str = "") -> tuple[dict, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    marker = detect_restricted_access(soup.get_text(" ", strip=True))
    if marker:
        raise ValueError("restricted page: " + marker)
    for node in soup.select("script, style, noscript"):
        node.decompose()
    rows, used = [], {}
    for selector in request.row_selectors:
        rows = soup.select(selector, limit=request.max_items + 1)
        if rows:
            used["rows"] = selector
            break
    if not request.row_selectors:
        rows = [soup]
    items, missing, shortened = [], [], False
    for index, row in enumerate(rows[:request.max_items]):
        item = {}
        for name, spec in request.fields.items():
            if not spec.selectors:
                raise ValueError("HTML extraction requires field selectors")
            found, value = None, None
            for selector in spec.selectors:
                node = row.select_one(selector)
                if node is None:
                    continue
                value = node.get(spec.attribute) if spec.attribute else node.get_text(" ", strip=True)
                if value is not None:
                    found = selector
                    break
            if found is None:
                missing.append({"row": index, "field": name})
            else:
                used.setdefault(name, {})[str(index)] = found
                if spec.attribute in ("href", "src") and base_url:
                    value = urljoin(base_url, str(value))
            item[name], cut = _value(value)
            shortened |= cut
        items.append(item)
    next_node = soup.select_one(request.next_selector) if request.next_selector else None
    next_url = urljoin(base_url, str(next_node.get("href"))) if next_node and next_node.get("href") else None
    return {"items": items, "missing": missing, "selectors_used": used,
            "truncated": len(rows) > request.max_items or shortened}, next_url


def fetch_page(url: str):
    # Same family web service and DNS-pinned fallback as web_fetch.
    from services.search.content import _get_public_url
    return _get_public_url(url, {"User-Agent": "FaustusReach/1.0"}, 10, max_bytes=MAX_BODY_BYTES)


async def extract(request: ExtractRequest, *, owner=None) -> dict:
    items, pages, missing, used, seen = [], [], [], [], set()
    truncated, reason, source_cut = False, "exhausted", False
    next_url = request.url
    for page_index in range(request.max_pages):
        ref, mime = "inline", "text/html"
        if request.url:
            if next_url in seen:
                truncated, reason = True, "pagination_loop"
                break
            seen.add(next_url)
            if request.transport == "browser":
                from src.reach.browser import read_response
                response = await asyncio.to_thread(read_response, next_url, owner=owner, profile=request.profile)
            else:
                response = await asyncio.to_thread(fetch_page, next_url)
            response.raise_for_status()
            ref = str(response.url)
            seen.add(ref)
            body = response.content
            source_cut = bool(getattr(response, "truncated", False))
            mime = response.headers.get("content-type", "text/html").lower()
            data = json.loads(body) if "json" in mime else None
            html = response.text
        elif request.har is not None:
            data, ref, body = har_body(request.har, request.endpoint_contains)
            mime, html = "application/json", ""
        elif request.json_data is not None:
            data = request.json_data
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            mime, html = "application/json", ""
        else:
            html = request.html
            body, data = html.encode("utf-8"), None
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("source exceeds byte budget")
        remaining = request.model_copy(update={"max_items": request.max_items - len(items)})
        if "json" in mime:
            result, next_url = extract_json(data, remaining), None
        elif "html" in mime:
            result, next_url = extract_html(html, remaining, base_url=ref if ref != "inline" else "")
        else:
            raise ValueError("source must be HTML or JSON")
        offset = len(items)
        items.extend(result["items"])
        missing.extend({**m, "row": offset + m["row"]} for m in result["missing"])
        used.append(result["selectors_used"])
        pages.append({"url": _safe_ref(ref) if ref != "inline" else ref,
                      "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                      "fetched_at": now_iso(), "truncated": source_cut})
        if source_cut:
            truncated, reason = True, "source_byte_limit"
            break
        if result["truncated"]:
            truncated, reason = True, "item_or_value_limit"
            break
        if not next_url:
            break
        if len(items) >= request.max_items:
            truncated, reason = True, "max_items"
            break
        if page_index + 1 >= request.max_pages:
            truncated, reason = True, "max_pages"
    return {"items": items, "item_count": len(items), "pages": pages, "missing": missing,
            "selectors_used": used, "truncated": truncated, "reason": reason,
            "untrusted_content": True}
