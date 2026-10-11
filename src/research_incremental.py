"""Incremental Deep Research refresh (radar #496 / #274).

Keeps a finished report as a section ledger tied to its sources (url, fetch
time, content hash). On refresh, only sections whose evidence aged past
``max_age_days`` or whose source content hash changed are redone; the rest
stay as-is and the report marks what was updated.

Pure planning/merge helpers — no network by default. Age uses
``citation_registry[].fetched_at`` (display ``sources`` often omit it) and
falls back to the report's ``completed_at``. Live re-hash of remote URLs is
optional via ``url_status`` (caller-supplied); the HTTP/MCP surfaces do not
fetch pages on plan — that limit is documented until a bounded checker lands.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_MAX_AGE_DAYS = 7
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)
_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# Auto-generated appendices (EN + ES) — not research sections to refresh.
_SKIP_SECTION_TITLES = frozenset({
    "research summary",
    "sources",
    "how to read this report",
    "fuentes",
    "cómo leer este informe",
    "como leer este informe",
    "resumen de la investigación",
    "resumen de la investigacion",
})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return ts
    except ValueError:
        return None


def _slug(title: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return (cleaned or "section")[:80]


def split_sections(markdown: str) -> List[Dict[str, Any]]:
    """Split a report body into ## / ### sections (order preserved)."""
    text = markdown if isinstance(markdown, str) else ""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        body = text.strip()
        if not body:
            return []
        return [{
            "id": "body",
            "title": "Report",
            "level": 2,
            "body": body,
            "start": 0,
            "end": len(text),
        }]
    sections: List[Dict[str, Any]] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = match.group(2).strip()
        level = len(match.group(1))
        # Skip the auto-generated summary/sources appendices (EN + ES).
        if title.lower() in _SKIP_SECTION_TITLES:
            continue
        body = text[match.end():end].strip()
        sections.append({
            "id": f"{_slug(title)}-{i}",
            "title": title,
            "level": level,
            "body": body,
            "start": start,
            "end": end,
            "heading": match.group(0).rstrip(),
        })
    return sections


def citation_numbers(text: str) -> List[int]:
    found: List[int] = []
    for match in _CITE_RE.finditer(text or ""):
        for part in match.group(1).split(","):
            part = part.strip()
            if part.isdigit():
                n = int(part)
                if n not in found:
                    found.append(n)
    return found


def _source_by_number(sources: Sequence[Mapping[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Index sources by citation number.

    Prefers ``n`` (citation_registry) then ``number`` (display sources), else
    1-based position — matching how ``[k]`` is assigned at write time.
    """
    out: Dict[int, Dict[str, Any]] = {}
    for i, src in enumerate(sources or (), start=1):
        if not isinstance(src, Mapping):
            continue
        num = src.get("n", src.get("number"))
        try:
            key = int(num) if num is not None else i
        except (TypeError, ValueError):
            key = i
        out[key] = dict(src)
    return out


def enrich_sources(
    sources: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    citation_registry: Optional[Mapping[str, Any]] = None,
    completed_at: Any = None,
) -> List[Dict[str, Any]]:
    """Merge display ``sources`` with ``citation_registry`` timestamps/numbers.

    Saved research JSON often stores display sources without ``fetched_at``;
    the registry keeps ``n``, ``url`` and ``fetched_at``. When both lack a
    timestamp, fall back to the report's ``completed_at``.
    """
    reg_list: List[Mapping[str, Any]] = []
    if isinstance(citation_registry, Mapping):
        raw = citation_registry.get("sources")
        if isinstance(raw, list):
            reg_list = [s for s in raw if isinstance(s, Mapping)]
    by_url: Dict[str, Mapping[str, Any]] = {}
    by_n: Dict[int, Mapping[str, Any]] = {}
    for s in reg_list:
        url = str(s.get("url") or "").strip()
        if url:
            by_url[url] = s
        try:
            n = int(s["n"]) if s.get("n") is not None else int(s["number"]) if s.get("number") is not None else None
        except (TypeError, ValueError, KeyError):
            n = None
        if n is not None:
            by_n[n] = s

    completed_iso = ""
    completed_ts = _parse_ts(completed_at)
    if completed_ts is not None:
        completed_iso = completed_ts.isoformat(timespec="seconds")

    base: List[Mapping[str, Any]] = []
    if sources:
        base = [s for s in sources if isinstance(s, Mapping)]
    elif reg_list:
        base = list(reg_list)

    out: List[Dict[str, Any]] = []
    for i, src in enumerate(base, start=1):
        row = dict(src)
        url = str(row.get("url") or "").strip()
        try:
            n = int(row["n"]) if row.get("n") is not None else (
                int(row["number"]) if row.get("number") is not None else i
            )
        except (TypeError, ValueError):
            n = i
        reg = by_url.get(url) or by_n.get(n) or {}
        if not row.get("fetched_at") and reg.get("fetched_at"):
            row["fetched_at"] = reg["fetched_at"]
        if not row.get("fetched_at") and completed_iso:
            row["fetched_at"] = completed_iso
        if row.get("n") is None and row.get("number") is None:
            if reg.get("n") is not None:
                row["n"] = reg["n"]
            else:
                row["n"] = n
        if not row.get("number"):
            row["number"] = row.get("n") or n
        if not row.get("evidence") and reg.get("evidence"):
            row["evidence"] = reg["evidence"]
        if not row.get("summary") and reg.get("summary"):
            row["summary"] = reg["summary"]
        if url and not row.get("url"):
            row["url"] = url
        elif not row.get("url") and reg.get("url"):
            row["url"] = reg["url"]
        out.append(row)
    return out


def _finding_hash(finding: Mapping[str, Any]) -> str:
    body = " ".join(
        str(finding.get(k) or "").strip()
        for k in ("evidence", "summary", "content", "snippet", "title")
        if finding.get(k)
    )
    if not body:
        body = str(finding.get("url") or "")
    return hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()


def build_section_ledger(
    report: str,
    *,
    sources: Optional[Sequence[Mapping[str, Any]]] = None,
    findings: Optional[Sequence[Mapping[str, Any]]] = None,
    built_at: Optional[datetime] = None,
    citation_registry: Optional[Mapping[str, Any]] = None,
    completed_at: Any = None,
) -> Dict[str, Any]:
    """Map each section to the sources it cites (ids, urls, hashes, fetched_at)."""
    enriched = enrich_sources(
        sources, citation_registry=citation_registry, completed_at=completed_at or built_at,
    )
    sections = split_sections(report)
    by_num = _source_by_number(enriched)
    findings_by_url: Dict[str, Mapping[str, Any]] = {}
    for f in findings or []:
        if isinstance(f, Mapping) and f.get("url"):
            findings_by_url[str(f["url"]).strip()] = f
    stamp_dt = built_at or _parse_ts(completed_at) or _now()
    stamp = stamp_dt.isoformat(timespec="seconds")
    ledger_sections: List[Dict[str, Any]] = []
    for sec in sections:
        cites = citation_numbers(sec.get("body") or "")
        linked: List[Dict[str, Any]] = []
        for n in cites:
            src = by_num.get(n) or {}
            url = str(src.get("url") or "").strip()
            finding = findings_by_url.get(url) if url else None
            content_hash = ""
            if finding:
                content_hash = _finding_hash(finding)
            elif src.get("content_hash"):
                content_hash = str(src["content_hash"])
            elif src.get("evidence") or src.get("summary"):
                content_hash = _finding_hash(src)
            linked.append({
                "number": n,
                "url": url[:500],
                "title": str(src.get("title") or "")[:200],
                "fetched_at": str(src.get("fetched_at") or stamp)[:40],
                "content_hash": content_hash[:64],
            })
        ledger_sections.append({
            "id": sec["id"],
            "title": sec["title"],
            "level": sec["level"],
            "citation_numbers": cites,
            "sources": linked,
            "built_at": stamp,
            "body_sha256": hashlib.sha256(
                (sec.get("body") or "").encode("utf-8", errors="ignore")
            ).hexdigest()[:16],
        })
    return {
        "version": 1,
        "built_at": stamp,
        "section_count": len(ledger_sections),
        "sections": ledger_sections,
        "content_change_checks": "caller_url_status_only",
    }


def evaluate_section(
    section: Mapping[str, Any],
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
    url_status: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ledger_built_at: Any = None,
) -> Dict[str, Any]:
    """Return freshness for one ledger section.

    ``url_status`` maps url → ``{content_hash, fetched_at, changed?, unreachable?}``.
    Sections with no citations age against ``built_at`` / ledger timestamp.
    """
    now = now or _now()
    max_age = max(1, int(max_age_days or DEFAULT_MAX_AGE_DAYS))
    cutoff = now - timedelta(days=max_age)
    reasons: List[str] = []
    stale_sources = 0
    checked = 0
    for src in section.get("sources") or []:
        if not isinstance(src, Mapping):
            continue
        checked += 1
        url = str(src.get("url") or "")
        fetched = _parse_ts(src.get("fetched_at"))
        if fetched is not None and fetched < cutoff:
            reasons.append(f"age:{url[:60]}")
            stale_sources += 1
            continue
        status = (url_status or {}).get(url) if url else None
        if isinstance(status, Mapping):
            if status.get("unreachable"):
                reasons.append(f"unreachable:{url[:60]}")
                stale_sources += 1
            elif status.get("changed") or (
                status.get("content_hash")
                and src.get("content_hash")
                and status["content_hash"] != src["content_hash"]
            ):
                reasons.append(f"hash:{url[:60]}")
                stale_sources += 1
    if checked == 0:
        # No citations — still age with the section/ledger built_at.
        built = _parse_ts(section.get("built_at") or ledger_built_at)
        if built is not None and built < cutoff:
            reasons.append("age:section")
            fresh = False
        else:
            fresh = True
    else:
        fresh = stale_sources == 0
    return {
        "id": section.get("id"),
        "title": section.get("title"),
        "fresh": fresh,
        "stale": not fresh,
        "stale_sources": stale_sources,
        "source_count": checked,
        "reasons": reasons[:12],
    }


def plan_refresh(
    ledger: Mapping[str, Any],
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
    url_status: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Decide which sections to redo."""
    sections = [
        s for s in (ledger.get("sections") or []) if isinstance(s, Mapping)
    ]
    evaluations = [
        evaluate_section(
            s, max_age_days=max_age_days, now=now, url_status=url_status,
            ledger_built_at=ledger.get("built_at"),
        )
        for s in sections
    ]
    redo = [e for e in evaluations if e["stale"]]
    keep = [e for e in evaluations if e["fresh"]]
    return {
        "max_age_days": max(1, int(max_age_days or DEFAULT_MAX_AGE_DAYS)),
        "section_count": len(sections),
        "redo_count": len(redo),
        "keep_count": len(keep),
        "redo": redo,
        "keep": keep,
        "complete_refresh": len(redo) == len(sections) and len(sections) > 0,
        "noop": len(redo) == 0,
    }


def merge_updated_sections(
    report: str,
    updates: Mapping[str, str],
    *,
    updated_ids: Optional[Iterable[str]] = None,
    marker_prefix: str = "Updated",
) -> str:
    """Replace section bodies by id; stamp a one-line marker under the heading."""
    sections = split_sections(report)
    if not sections:
        return report
    updated = set(updated_ids or updates.keys())
    parts: List[str] = []
    # Preserve preamble before first heading.
    first_start = sections[0]["start"]
    if first_start > 0:
        parts.append(report[:first_start].rstrip() + "\n\n")
    for sec in sections:
        heading = sec.get("heading") or f"## {sec['title']}"
        body = updates.get(sec["id"], sec.get("body") or "")
        if sec["id"] in updated:
            stamp = _now().isoformat(timespec="seconds")
            mark = f"> *{marker_prefix} {stamp}*\n\n"
            if not body.lstrip().startswith(">"):
                body = mark + body.lstrip()
        parts.append(f"{heading}\n\n{body.rstrip()}\n\n")
    return "".join(parts).rstrip() + "\n"


def refresh_query(original_query: str, plan: Mapping[str, Any]) -> str:
    """Build a focused query for redoing only stale sections."""
    titles = [
        str(s.get("title") or "").strip()
        for s in (plan.get("redo") or [])
        if isinstance(s, Mapping) and s.get("title")
    ]
    if not titles:
        return original_query
    listed = "\n".join(f"- {t}" for t in titles)
    return (
        f"{original_query.strip()}\n\n"
        f"Incremental refresh: rewrite ONLY these sections whose evidence is "
        f"stale or changed. Keep the same ## headings. Do not rewrite other "
        f"sections.\n{listed}"
    )


def _ledger_has_timestamps(ledger: Mapping[str, Any]) -> bool:
    for sec in ledger.get("sections") or []:
        if not isinstance(sec, Mapping):
            continue
        for src in sec.get("sources") or []:
            if isinstance(src, Mapping) and src.get("fetched_at"):
                return True
        if sec.get("built_at") or ledger.get("built_at"):
            # Section-level age is enough when there are no citations.
            if not (sec.get("sources") or []):
                return True
    return False


def plan_from_saved(
    saved: Mapping[str, Any],
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    url_status: Optional[Mapping[str, Mapping[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build or reuse a ledger from a saved research JSON and plan a refresh."""
    report = str(saved.get("raw_report") or saved.get("result") or "")
    ledger = saved.get("section_ledger")
    registry = saved.get("citation_registry") if isinstance(saved.get("citation_registry"), Mapping) else None
    # Rebuild when missing, empty, or written without fetched_at (pre-fix ledgers
    # and display-only sources that omitted the registry timestamps).
    if (
        not isinstance(ledger, Mapping)
        or not ledger.get("sections")
        or not _ledger_has_timestamps(ledger)
    ):
        ledger = build_section_ledger(
            report,
            sources=saved.get("sources") if isinstance(saved.get("sources"), list) else [],
            findings=saved.get("raw_findings") if isinstance(saved.get("raw_findings"), list) else [],
            citation_registry=registry,
            completed_at=saved.get("completed_at"),
            built_at=_parse_ts(saved.get("completed_at")) or _now(),
        )
    plan = plan_refresh(
        ledger, max_age_days=max_age_days, now=now, url_status=url_status,
    )
    plan["ledger"] = ledger
    plan["query"] = str(saved.get("query") or "")
    plan["refresh_query"] = refresh_query(plan["query"], plan)
    plan["content_change_checks"] = "caller_url_status_only"
    return plan
