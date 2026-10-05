"""Local research runs: bounded same-origin crawl with durable checkpoints.

Each completed page is saved before advancing the frontier. A process restart
can resume an interrupted run; no scheduler or external account is required.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.constants import DATA_DIR
from src.contracts.base import now_iso
from src.reach.extract import ExtractField, ExtractRequest, _safe_ref, extract_html, fetch_page

_LOCK = threading.RLock()
_RUNNING = set()


class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["start", "resume", "status", "cancel", "export"] = "start"
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    urls: list[str] = Field(default_factory=list, max_length=30)
    max_pages: int = Field(default=20, ge=1, le=100)
    max_depth: int = Field(default=2, ge=0, le=5)
    delay_s: float = Field(default=1.0, ge=0.5, le=10)
    transport: Literal["fetch", "browser"] = "fetch"
    profile: str = Field(default="research", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    path_prefix: str = Field(default="/", max_length=1000)
    row_selectors: list[str] = Field(default_factory=list, max_length=5)
    fields: dict[str, ExtractField] = Field(default_factory=dict, max_length=30)
    max_items_per_page: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100)
    limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_job(self):
        if self.action == "start":
            if not self.urls or any(len(u) > 2048 or urlsplit(u).scheme not in ("http", "https") for u in self.urls):
                raise ValueError("start needs explicit HTTP(S) seed URLs")
            if self.run_id:
                raise ValueError("start creates its own run_id")
            if self.fields:
                ExtractRequest(html="", fields=self.fields, row_selectors=self.row_selectors)
        elif not self.run_id:
            raise ValueError("run_id is required")
        if not self.path_prefix.startswith("/"):
            raise ValueError("path_prefix must start with /")
        return self


def root(owner):
    scope = hashlib.sha256(str(owner or "").encode()).hexdigest()
    return Path(DATA_DIR) / "reach-runs" / scope


def _path(owner, run_id):
    return root(owner) / (run_id + ".json")


def _save(owner, state, *, preserve_cancel=False):
    path = _path(owner, state["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with _LOCK:
        if preserve_cancel and path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            if current["status"] == "cancelled":
                state["status"] = "cancelled"
        temp.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temp.replace(path)


def _load(owner, run_id):
    with _LOCK:
        try:
            return json.loads(_path(owner, run_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("research run not found") from None


def _canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def _origin(url):
    p = urlsplit(url)
    return (p.scheme.lower(), p.netloc.lower())


def _robot(url, cache):
    origin = _origin(url)
    if origin not in cache:
        robot_url = urlunsplit((*origin, "/robots.txt", "", ""))
        try:
            response = fetch_page(robot_url)
            parser = RobotFileParser(robot_url)
            if response.status_code == 404:
                parser.parse([])
            elif response.status_code != 200:
                parser.disallow_all = True
            else:
                parser.parse(response.text.splitlines())
            cache[origin] = parser
        except Exception:
            parser = RobotFileParser(robot_url)
            parser.disallow_all = True
            cache[origin] = parser
    parser = cache[origin]
    interval = parser.crawl_delay("FaustusReach") or parser.crawl_delay("*") or 0
    return parser.can_fetch("FaustusReach", url), interval


def _summary(state, offset=0, limit=5):
    return {"run_id": state["id"], "status": state["status"], "pages": len(state["results"]),
        "pending": len(state["frontier"]), "errors": state["errors"],
        "bytes": state["bytes"], "reason": state.get("reason", ""),
        "results": state["results"][offset:offset + limit], "offset": offset,
        "has_more": offset + limit < len(state["results"]), "untrusted_content": True}


def work(owner, run_id):
    state = _load(owner, run_id)
    cfg = CrawlRequest.model_validate(state["request"])
    robots, last_request = {}, 0.0
    origins = {_origin(u) for u in cfg.urls}
    try:
        state["status"] = "running"
        _save(owner, state, preserve_cancel=True)
        while state["frontier"] and len(state["visited"]) < cfg.max_pages:
            # Reload the cancellation signal, without overwriting our checkpoint.
            if _load(owner, run_id)["status"] == "cancelled":
                state["status"] = "cancelled"
                break
            url, depth = state["frontier"][0]
            if url in state["visited"]:
                state["frontier"].pop(0)
                continue
            allowed, crawl_delay = _robot(url, robots)
            if not allowed:
                state["errors"].append({"url": _safe_ref(url), "error": "robots_or_transport_refused"})
            else:
                delay = max(cfg.delay_s, crawl_delay)
                if delay > 60:
                    state["status"], state["reason"] = "paused", "robots_delay_exceeds_run_budget"
                    break
                remaining = delay - (time.monotonic() - last_request)
                if remaining > 0:
                    time.sleep(remaining)
                last_request = time.monotonic()
                try:
                    if cfg.transport == "browser":
                        from src.reach.browser import read_response
                        response = read_response(url, owner=owner, profile=cfg.profile)
                    else:
                        response = fetch_page(url)
                    response.raise_for_status()
                    actual = str(response.url)
                    if _origin(actual) not in origins:
                        raise ValueError("redirect left the seed origins")
                    if "html" not in response.headers.get("content-type", "text/html"):
                        raise ValueError("crawl pages must be HTML")
                    if state["bytes"] + len(response.content) > 20_000_000:
                        state["status"], state["reason"] = "paused", "total_byte_limit"
                        break
                    from src.browser_extraction import detect_restricted_access
                    soup = BeautifulSoup(response.text, "html.parser")
                    if detect_restricted_access(soup.get_text(" ", strip=True)):
                        raise ValueError("restricted page")
                    links = [_canonical(urljoin(actual, str(a["href"]))) for a in soup.select("a[href]")]
                    for node in soup.select("script, style, noscript"):
                        node.decompose()
                    row = {"url": _safe_ref(actual), "depth": depth, "fetched_at": now_iso(),
                        "sha256": hashlib.sha256(response.content).hexdigest(),
                        "title": soup.title.get_text()[:500] if soup.title else "",
                        "text": soup.get_text("\n", strip=True)[:8000],
                        "source_truncated": bool(getattr(response, "truncated", False))}
                    if cfg.fields:
                        extracted, _ = extract_html(response.text, ExtractRequest(html="", fields=cfg.fields,
                            row_selectors=cfg.row_selectors, max_items=cfg.max_items_per_page), base_url=actual)
                        row.update(extracted)
                        row["missing_count"] = len(row.get("missing", []))
                        row["missing"] = row.get("missing", [])[:100]
                        row["metadata_truncated"] = any(isinstance(values, dict) and len(values) > 3 for values in row.get("selectors_used", {}).values()) or row["missing_count"] > 100
                        row["selectors_used"] = {name: dict(list(values.items())[:3]) if isinstance(values, dict) else values
                                                 for name, values in row.get("selectors_used", {}).items()}
                        row["extracted_count"] = len(row["items"])
                        while row["items"] and len(json.dumps(row, ensure_ascii=False)) > 256_000:
                            row["items"].pop()
                            row["truncated"] = True
                    state["results"].append(row)
                    state["bytes"] += len(response.content)
                    if depth < cfg.max_depth:
                        queued = {u for u, _ in state["frontier"]} | set(state["visited"])
                        for link in links:
                            if (_origin(link) in origins and urlsplit(link).path.startswith(cfg.path_prefix)
                                    and link not in queued and len(state["frontier"]) < 2000):
                                state["frontier"].append([link, depth + 1])
                                queued.add(link)
                except Exception as exc:
                    state["errors"].append({"url": _safe_ref(url), "error": type(exc).__name__})
            state["visited"].append(url)
            state["frontier"].pop(0)
            _save(owner, state, preserve_cancel=True)
        if state["status"] == "running":
            state["status"] = "completed"
            state["reason"] = "max_pages" if state["frontier"] else "exhausted"
        _save(owner, state, preserve_cancel=True)
    except Exception as exc:
        state["status"], state["reason"] = "paused", type(exc).__name__
        _save(owner, state, preserve_cancel=True)
    finally:
        with _LOCK:
            _RUNNING.discard((str(owner or ""), run_id))


def run_crawl(request: CrawlRequest, *, owner=None):
    if request.action == "start":
        run_id = uuid.uuid4().hex
        seeds = list(dict.fromkeys(_canonical(u) for u in request.urls))
        state = {"id": run_id, "request": request.model_dump(mode="json"), "status": "queued",
            "frontier": [[u, 0] for u in seeds], "visited": [], "results": [], "errors": [], "bytes": 0}
        _save(owner, state)
    else:
        run_id = request.run_id
        state = _load(owner, run_id)
    key = (str(owner or ""), run_id)
    with _LOCK:
        if request.action == "export":
            path = _path(owner, run_id).with_suffix(".results.json")
            body = json.dumps({"run_id": run_id, "results": state["results"],
                "errors": state["errors"], "untrusted_content": True}, ensure_ascii=False).encode("utf-8")
            path.write_bytes(body)
            return {"run_id": run_id, "path": str(path), "pages": len(state["results"]),
                "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "untrusted_content": True}
        if request.action == "status":
            if state["status"] in ("running", "queued") and key not in _RUNNING:
                state["status"], state["reason"] = "paused", "process_interrupted"
            return _summary(state, request.offset, request.limit)
        if request.action == "cancel":
            state["status"] = "cancelled"
            _save(owner, state)
            return _summary(state)
        if key not in _RUNNING:
            if request.action == "resume" and (state["status"] == "completed" or not state["frontier"]):
                return _summary(state)
            if sum(k[0] == key[0] for k in _RUNNING) >= 2 or len(_RUNNING) >= 8:
                state['status'], state['reason'] = 'paused', 'active_run_limit'
                _save(owner, state)
                return _summary(state)
            if request.action == 'resume':
                state['status'], state['reason'] = 'queued', ''
                _save(owner, state)
            _RUNNING.add(key)
            threading.Thread(target=work, args=(owner, run_id), daemon=True, name="reach-research").start()
    return _summary(state)
