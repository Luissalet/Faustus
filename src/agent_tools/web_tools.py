import asyncio
import inspect
import json
import re
from typing import Dict, Any

from src.constants import MAX_OUTPUT_CHARS

class WebSearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.search import comprehensive_web_search
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = content.strip()
        query = raw
        time_filter = None
        max_pages = 5
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "query" in parsed:
                    query = str(parsed.get("query", "")).strip()
                    tf = parsed.get("time_filter") or parsed.get("freshness")
                    if isinstance(tf, str) and tf.lower() in ("day", "week", "month", "year"):
                        time_filter = tf.lower()
                    mp = parsed.get("max_pages")
                    if isinstance(mp, int) and 1 <= mp <= 10:
                        max_pages = mp
            except json.JSONDecodeError:
                pass
        if not query:
            query = raw.split("\n")[0].strip()
        if time_filter is None:
            q_lc = query.lower()
            if any(kw in q_lc for kw in ("today", "latest", "breaking", "this morning", "right now", "currently")):
                time_filter = "day"
            elif any(kw in q_lc for kw in ("this week", "past week", "recent news", "last few days")):
                time_filter = "week"
            elif any(kw in q_lc for kw in ("this month", "past month")):
                time_filter = "month"
            elif " news" in q_lc or q_lc.startswith("news ") or q_lc.endswith(" news"):
                time_filter = "week"
        loop = asyncio.get_running_loop()
        if progress_cb:
            await progress_cb({
                "elapsed_s": 0,
                "tail": f"Searching web for: {query[:160]}",
            })
        try:
            text, sources = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: comprehensive_web_search(
                        query,
                        max_pages=max_pages,
                        time_filter=time_filter,
                        return_sources=True,
                    ),
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return {
                "error": f"web_search timed out after 30s: {query[:200]}",
                "exit_code": 1,
            }
        except Exception as e:
            return {
                "error": f"web_search failed: {type(e).__name__}: {str(e) or 'no details'}",
                "exit_code": 1,
                "untrusted_content": True,
            }
        if progress_cb:
            await progress_cb({
                "elapsed_s": 30,
                "tail": "Search completed; preparing sources.",
            })
        output = text[:MAX_OUTPUT_CHARS] if len(text) > MAX_OUTPUT_CHARS else text
        if sources:
            output += "\n\n<!-- SOURCES:" + json.dumps(sources) + " -->"
        return {"output": output, "exit_code": 0}

def _evidence_for_fetch(url: str, text: str, *, owner_id: str, project_id, truncated: bool,
                         fetched_bytes, total_bytes):
    """EvidenceRef for a web_fetch result (WEB-02): URL, content hash, and

    capture time, so a caller can later tell whether the page it is citing
    still matches what was actually read -- the same idea
    `context_ledger.evidence_for_read` already gives file reads, applied to
    the web instead of duplicating a second evidence shape for it (rule 4).
    `locator` is `byte_range` when the download budget cut the body short
    (the hash only covers what was kept, never the whole page) and `whole`
    otherwise, so a stale/partial fetch is distinguishable from a complete one.
    """
    import hashlib

    from src.contracts import EvidenceLocator, EvidenceRef
    from src.contracts.base import now_iso

    digest = hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()
    evidence_id = "evi_web_" + hashlib.sha256(
        f"{url}:{digest}".encode("utf-8", "replace")
    ).hexdigest()[:24]
    if truncated:
        locator = EvidenceLocator(kind="byte_range", value=f"0-{int(fetched_bytes or 0)}")
    else:
        locator = EvidenceLocator(kind="whole", value=str(url)[:512])
    return EvidenceRef(
        evidence_id=evidence_id,
        owner_id=owner_id or "system",
        project_id=project_id,
        source_type="web",
        source_ref=str(url)[:512],
        source_revision=digest[:16],
        content_sha256=digest,
        captured_at=now_iso(),
        locator=locator,
        derived_from=(),
        retention="task",
    )


_BROWSER = "mcp__builtin_browser__"
_CHALLENGE = re.compile(
    r"just a moment|checking your browser|verify you are human|attention required|"
    r"enable javascript and cookies|cf-browser-verification|un momento",
    re.IGNORECASE)


async def _read_with_browser(url: str, ctx: dict, *, why: str = "") -> "dict | None":
    """A page a plain fetch was refused on (HTTP 403, a Cloudflare or bot
    challenge, a page that needs JavaScript) read through the integrated
    browser instead, as the person would. None when the browser is off,
    not offered to this turn or cannot read it either: the caller keeps the
    original error.

    Seen live (22-10): "¿Ha sido Minecraft Dungeons 2 un fracaso?" -- SteamDB
    and Wikipedia answered 403 / a Cloudflare challenge to web_fetch, the
    error said "open the site in a browser", and the model, with the
    browser offered, never did; the answer had to say it could not get the
    first game's numbers.
    """
    disabled = ctx.get("disabled_tools") if isinstance(ctx, dict) else None
    disabled = {str(x) for x in (disabled or ())}
    if "builtin_browser" in disabled or any(
            f"{_BROWSER}{name}" in disabled for name in ("browser_navigate", "browser_snapshot")):
        return None
    try:
        from src.tool_utils import get_mcp_manager
        mgr = get_mcp_manager()
    except Exception:  # noqa: BLE001
        mgr = None
    if mgr is None or not hasattr(mgr, "call_tool"):
        return None

    async def call(name: str, args: dict, timeout: float) -> "dict | None":
        try:
            res = await asyncio.wait_for(mgr.call_tool(f"{_BROWSER}{name}", args), timeout=timeout)
        except Exception:  # noqa: BLE001 - the original error stands
            return None
        if not isinstance(res, dict) or res.get("exit_code") not in (0, None) or res.get("error"):
            return None
        return res

    nav = await call("browser_navigate", {"url": url}, 60)
    if nav is None:
        return None
    text = str(nav.get("stdout") or "")
    for _ in range(3):
        if text and not _CHALLENGE.search(text[:4000]):
            break
        await call("browser_wait_for", {"time": 4}, 15)
        snap = await call("browser_snapshot", {}, 45)
        text = str((snap or {}).get("stdout") or text)
    if not text.strip() or _CHALLENGE.search(text[:4000]):
        return None
    note = (f"[read with the integrated browser: a plain fetch was refused ({why[:160]}). "
            "This is the page's accessibility snapshot; refs like [ref=e12] can be used with the "
            "browser tools to click or read further.]\n\n")
    body = text[:MAX_OUTPUT_CHARS - len(note)]
    output = note + body
    evidence = _evidence_for_fetch(
        url, body, owner_id=str(ctx.get("owner") or "system") if isinstance(ctx, dict) else "system",
        project_id=(ctx.get("project_id") or None) if isinstance(ctx, dict) else None,
        truncated=len(text) > len(body), fetched_bytes=len(text.encode("utf-8")), total_bytes=None,
    ).to_mapping()
    return {"output": output, "exit_code": 0, "read_with": "integrated_browser", "read_status": "ok",
            "untrusted_content": True, "evidence_refs": [evidence]}


class WebFetchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.search.content import fetch_webpage_content
        from src.constants import WEB_FETCH_HARD_MAX_BYTES
        raw = content.strip()
        url = ""
        max_bytes = None
        inspect_http = False
        look_flag = None          # None: automatic (setting); True: always look; False: never
        look_question = ""
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    url = str(parsed.get("url") or "").strip()
                    inspect_http = parsed.get("inspect") is True
                    if isinstance(parsed.get("look"), bool):
                        look_flag = parsed["look"]
                    look_question = str(parsed.get("question") or "").strip()[:1200]
                    # Download-budget override (#3812): "full": true raises the
                    # budget to the hard cap; an explicit max_bytes is clamped
                    # to the hard cap downstream. Default stays the soft cap.
                    if parsed.get("full") is True:
                        max_bytes = WEB_FETCH_HARD_MAX_BYTES
                    mb = parsed.get("max_bytes")
                    if isinstance(mb, int) and mb > 0:
                        max_bytes = mb
            except json.JSONDecodeError:
                url = ""
        if not url:
            url = raw.split("\n")[0].strip()
        if not url or url.startswith("{") or any(c in url for c in (" ", "\t", "\n")):
            return {"error": "web_fetch: provide a single URL or domain, e.g. example.com", "exit_code": 1}
        low = url.lower()
        if "://" in low and not low.startswith(("http://", "https://")):
            return {"error": f"web_fetch: unsupported URL scheme (only http/https): {url[:80]}", "exit_code": 1}
        if not low.startswith(("http://", "https://")):
            url = "https://" + url
        loop = asyncio.get_running_loop()
        try:
            def _fetch():
                kwargs = {"timeout": 10}
                try:
                    sig = inspect.signature(fetch_webpage_content)
                    if "max_bytes" in sig.parameters:
                        kwargs["max_bytes"] = max_bytes
                    if "inspect" in sig.parameters and inspect_http:
                        kwargs["inspect"] = True
                    if "keep_html" in sig.parameters and look_flag is not False and not inspect_http and _look_setting():
                        kwargs["keep_html"] = True        # to tell a page of tables/charts from a short page
                except (TypeError, ValueError):
                    # Some deployed/test shims may not expose a signature.
                    # Prefer compatibility over failing the whole fetch.
                    pass
                return fetch_webpage_content(url, **kwargs)

            result = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return {"error": f"web_fetch: timed out fetching {url}", "exit_code": 1}
        except Exception as e:
            return {"error": f"web_fetch: {url}: {e}", "exit_code": 1}
        err = result.get("error")
        if not inspect_http and result.get("read_status") == "blocked":
            via_browser = await _read_with_browser(url, ctx, why=str(err or ""))
            if via_browser is not None:
                return via_browser
            return {"error": f"web_fetch: {url}: {err}", "exit_code": 1,
                    "read_status": "blocked", "blocked_reason": result.get("blocked_reason"),
                    "http_status": result.get("http_status"), "final_url": result.get("final_url"),
                    "resolution_hint": result.get("resolution_hint"),
                    "untrusted_content": True, "evidence_refs": []}
        if inspect_http:
            if err:
                return {"error": f"web_fetch: {url}: {err}", "exit_code": 1,
                        "untrusted_content": True}
            from src.web_profile import passive_http_profile
            profile = passive_http_profile(url, result)
            output = json.dumps(profile, ensure_ascii=False, indent=2)
            # JSON escaping can amplify even a short hostile header. Keep a
            # complete parseable profile while trimming observation values.
            for cap in (128, 64, 32, 0):
                if len(output) <= MAX_OUTPUT_CHARS:
                    break
                profile["profile_truncated"] = True
                for header in profile["headers"].values():
                    if len(header["value"]) > cap:
                        header["value"] = header["value"][:cap]
                        header["value_truncated"] = True
                output = json.dumps(profile, ensure_ascii=False, indent=2)
            response = {"output": output, "profile": profile, "exit_code": 0,
                        "untrusted_content": True, "evidence_refs": []}
            # An old cache entry with unknown capture time is still useful,
            # but cannot mint a dated observation receipt.
            if profile["captured_at"]:
                evidence = _evidence_for_fetch(
                    url, output, owner_id=str(ctx.get("owner") or "system"),
                    project_id=ctx.get("project_id") or None, truncated=False,
                    fetched_bytes=None, total_bytes=None,
                ).to_mapping()
                evidence["captured_at"] = profile["captured_at"]
                evidence["locator"] = {"kind": "whole", "value":
                    "passive_http_profile: selected HTTP metadata; no page body"}
                response["evidence_refs"] = [evidence]
            return response
        text = (result.get("content") or "").strip()
        title = result.get("title") or ""

        # "Look at the page": when the text is little or the page is mostly tables/charts/canvases, read a screenshot of it
        # with the resident vision model (src/visual_index/lookpage.py). Never fatal.
        look = None
        look_note = ""
        if look_flag is not False and not err:
            look, look_note = await _look_at(url, look_question, text, result, ctx, forced=look_flag is True)
        capture_block = ""
        if look is not None and look.has_text:
            capture_block = f"\n\n---\n{look.marker()}\n{look.text}"

        if not text and not capture_block:
            # Nothing readable came back: a refusal reported as a network
            # error ("NetworkError: blocked: HTTP 403 forbidden"), a page
            # that needs JavaScript, an empty shell. The browser reads what a
            # person would see.
            if not inspect_http:
                via_browser = await _read_with_browser(
                    url, ctx, why=str(err or "no readable text content (the page may need JavaScript)"))
                if via_browser is not None:
                    return via_browser
            if look_note:
                return {"error": f"web_fetch: {url}: no readable text content. {look_note}", "exit_code": 1,
                        "look": look.as_dict() if look is not None else None, "untrusted_content": True}
            if err:
                return {
                    "error": f"web_fetch: {url}: {err}",
                    "exit_code": 1,
                    "untrusted_content": True,
                }
            return {"error": f"web_fetch: {url}: no readable text content (not HTML, or the page needs JS/login)", "exit_code": 1}

        # Tell the model when the download budget cut the body short and how
        # to get the rest, instead of silently presenting a partial page as
        # the whole thing.
        size_note = ""
        if result.get("truncated"):
            fetched = result.get("fetched_bytes") or 0
            total = result.get("total_bytes")
            total_txt = f" of {total:,} bytes" if total else ""
            size_note = (
                f"[partial content: download stopped at {fetched:,} bytes{total_txt}. "
                f'Re-call with {{"url": "{url}", "full": true}} to fetch up to '
                f"{WEB_FETCH_HARD_MAX_BYTES:,} bytes.]\n\n"
            )

        # Source classification + extraction-quality (#source-types): surfaced
        # both as text the model reads (quality_note, when the extracted text
        # is likely incomplete/wrong) and as structured fields for any caller
        # that reads the dict directly rather than the rendered output.
        source_type = result.get("source_type", "unknown")
        is_official = bool(result.get("is_official"))
        extraction_quality = result.get("extraction_quality") or {}
        quality_note = result.get("quality_note") or ""
        if quality_note:
            size_note += f"[note: {quality_note}]\n\n"

        # The notice must lead the output so the MAX_OUTPUT_CHARS trim below can
        # never drop it. The title is untrusted, uncapped page content, so a
        # giant title ahead of the notice could push it out of range; keep the
        # notice first and cap the title as a second guard.
        if len(title) > 300:
            title = title[:300] + "..."
        header = (f"# {title}\n" if title else "") + f"Source: {url}\n\n"
        if look_note:
            size_note += f"[note: {look_note}]\n\n"
        if capture_block:
            room = max(2000, MAX_OUTPUT_CHARS - len(size_note) - len(header) - len(capture_block) - 40)
            output = size_note + header + (text[:room] if len(text) > room else text) + capture_block
        else:
            output = size_note + header + text
        download_truncated = bool(result.get("truncated"))
        fetched_bytes = result.get("fetched_bytes")
        total_bytes = result.get("total_bytes")

        output_truncated = len(output) > MAX_OUTPUT_CHARS
        if output_truncated:
            output = output[:MAX_OUTPUT_CHARS] + "\n\n[...truncated]"

        # WEB-02: declared, never silent -- two independent truncation points
        # (the download budget in fetch_webpage_content, and the output-char
        # cap here) collapse into one explicit `truncated`/`kept_bytes` pair
        # instead of the caller having to notice a "[...truncated]" string.
        response: Dict[str, Any] = {"output": output, "exit_code": 0}
        response["web_sources"] = [{"url": url, "title": title or url}]
        response["source_type"] = source_type
        response["is_official"] = is_official
        response["extraction_quality"] = extraction_quality
        if quality_note:
            response["quality_note"] = quality_note
        # Structured page shape (Markdown by default): a caller doing
        # citations/evidence matching can use these instead of re-parsing
        # `output`'s Markdown syntax itself.
        response["format"] = result.get("format") or "text"
        links = result.get("links")
        if links:
            response["links"] = links
        headings = result.get("headings")
        if headings:
            response["headings"] = headings
        if look is not None:
            response["look"] = look.as_dict()
            if look.has_text:
                response["from_capture"] = True
        response["truncated"] = download_truncated or output_truncated
        if download_truncated:
            response["kept_bytes"] = fetched_bytes
        elif output_truncated:
            response["kept_bytes"] = len(output)

        # EvidenceRef (WEB-02): URL, content hash, capture time -- best-effort,
        # must never turn a successful fetch into a failure (mirrors
        # ReadFileTool's own evidence bookkeeping in filesystem_tools.py).
        try:
            owner_id = str(ctx.get("owner") or "") if isinstance(ctx, dict) else ""
            project_id = (str(ctx.get("project_id") or "") or None) if isinstance(ctx, dict) else None
            evidence = _evidence_for_fetch(
                url, text,
                owner_id=owner_id or "system",
                project_id=project_id,
                truncated=download_truncated,
                fetched_bytes=fetched_bytes,
                total_bytes=total_bytes,
            )
            response["evidence_refs"] = [evidence.to_mapping()] if text else []
            if capture_block:
                shot = _evidence_for_fetch(
                    url, look.text, owner_id=owner_id or "system", project_id=project_id,
                    truncated=False, fetched_bytes=None, total_bytes=None).to_mapping()
                shot["locator"] = {"kind": "whole", "value": "screenshot of the page, read by the vision model"}
                response["evidence_refs"].append(shot)
        except Exception:
            pass

        return response


def _look_setting() -> bool:
    try:
        from src.settings import get_setting

        return bool(get_setting("web_fetch_look_at_page", True))
    except Exception:  # noqa: BLE001
        return True


async def _look_at(url: str, question: str, text: str, result: dict, ctx: dict, *, forced: bool):
    """``(LookResult | None, note)``: read a screenshot when the page needs it. ``note`` is one honest sentence for the model
    when the page seems to keep its data visually and nobody could read it; "" otherwise. Never raises."""
    if not forced and not _look_setting():
        return None, ""
    try:
        from src.visual_index import lookpage

        assessment = lookpage.assess_page(text, str(result.get("raw_html") or ""), js_rendered=bool(result.get("js_rendered")))
        if not forced and not assessment.wants_look:
            return None, ""
        owner = (ctx.get("owner") if isinstance(ctx, dict) else None) or None
        look = await lookpage.look_at_page(url, question, owner=owner, reason=assessment.reason or "requested by the caller")
        if look.has_text:
            return look, ""
        if forced or assessment.visual_signals:
            return look, lookpage.gap_sentence(look, url) or ""
        return look, ""
    except Exception:  # noqa: BLE001
        return None, ""
