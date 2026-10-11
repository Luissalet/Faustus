"""Sweep every turn for deterministic failure signatures (radar #498 / #276).

A sample of chats misses rare agent failures. This module walks **all**
assistant turns in a time window (owner-scoped), detects fixed signatures
from metadata and tool-event counters, and groups them for Diagnostics and
the Hub bus. It never returns prompts, replies, file contents or secrets —
only signature ids, counts, tool names and short linked examples
(session_id + message/run id).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 1
MAX_DAYS = 30
#: Soft page size for one HTTP/MCP call before asking the client to continue.
MAX_ROWS = 50000
#: Internal fetch size while walking history (keyset pages).
BATCH_ROWS = 2500
#: Absolute ceiling; beyond this the report is honest about partial coverage.
MAX_ROWS_HARD = 500000
MAX_EXAMPLES = 5
MAX_GROUP_EXAMPLES = 3
_ERROR_HEAD = 80
_REFUSAL_RE = re.compile(
    r"(?i)^\s*(i\s+can'?t\s+help\s+with\s+that|i\s+cannot\s+assist|"
    r"lo\s+siento,?\s+no\s+(puedo|debo)|as an ai (language )?model)"
)

#: Canonical ``tool_result_fields`` / ``ToolResult.status`` values that count
#: as a tool failure for identical-error signatures.
_FAILED_RESULT_STATUSES = frozenset({"failed", "denied", "conflict"})
#: Explicit non-success statuses that must NOT fall through to stdout heuristics.
_EXPLICIT_NON_FAIL_STATUSES = frozenset({
    "succeeded", "cancelled", "partial", "outcome_unknown",
})

#: Deterministic stop_reason values the agent loop stamps on a turn.
STOP_SIGNATURES = {
    "rounds_exhausted": "rounds_exhausted",
    "budget_exhausted": "budget_exhausted",
    "budget_exceeded": "budget_exhausted",
    "loop_breaker": "loop_breaker",
    "non_progressing_loop": "loop_breaker",
    "loop_breaker_triggered": "loop_breaker",
    "intent_nudge_exhausted": "intent_nudge_exhausted",
    "approval_expired": "approval_expired",
    "provider_tool_format_error": "provider_tool_format_error",
    "cancelled": "cancelled",
}

EVENT_TYPE = "faustus.diag.failure_sweep"
SEND_TIMEOUT_S = 2.0
_HUB_KEYS = (
    "ts", "owner", "days", "since", "turns_scanned", "signature_count",
    "hit_count", "by_signature", "coverage", "truncated", "rows_read",
)


def _metadata(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clamp_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = DEFAULT_DAYS
    return max(1, min(MAX_DAYS, value))


def _error_fingerprint(text: str) -> str:
    """Stable short id for an error string (no raw text in the report)."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())[:240]
    if not cleaned:
        return ""
    return hashlib.sha256(cleaned.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _tool_name(event: Mapping[str, Any]) -> str:
    for key in ("tool", "name", "tool_name"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return ""


def _tool_failed(event: Mapping[str, Any]) -> Tuple[bool, str]:
    """Return (failed, fingerprint). Fingerprint is empty when not failed.

    Prefer the canonical ``result_status`` that ``tool_result_fields`` /
    ``agent_loop`` persist on tool events. Only fall back to exit_code /
    error / legacy ``status`` / ``error:`` stdout when that field is absent.
    Explicit cancelled / partial / outcome_unknown / succeeded never become
    failures via a normal stdout match.
    """
    if not isinstance(event, Mapping):
        return False, ""
    err = event.get("error")
    out = event.get("output")
    err_text = err if isinstance(err, str) else ""
    out_text = out if isinstance(out, str) else ""

    result_status = str(event.get("result_status") or "").strip().lower()
    if result_status in _FAILED_RESULT_STATUSES:
        return True, _error_fingerprint(err_text or out_text or result_status)
    if result_status in _EXPLICIT_NON_FAIL_STATUSES:
        return False, ""

    code = event.get("exit_code")
    if code not in (None, 0, "0"):
        return True, _error_fingerprint(err_text or out_text or f"exit:{code}")
    if err_text.strip():
        return True, _error_fingerprint(err_text)
    status = str(event.get("status") or "").lower()
    if status in ("error", "failed", "failure"):
        return True, _error_fingerprint(err_text or status)
    if out_text.strip().lower().startswith("error:"):
        return True, _error_fingerprint(out_text)
    return False, ""


def _tool_empty(event: Mapping[str, Any]) -> bool:
    if not isinstance(event, Mapping):
        return False
    failed, _ = _tool_failed(event)
    if failed:
        return False
    out = event.get("output")
    if out is None:
        out = event.get("result")
    if out is None:
        return True
    if isinstance(out, str) and not out.strip():
        return True
    return False


def _stop_reason(md: Mapping[str, Any]) -> str:
    for key in ("stop_reason", "ledger_stop_reason"):
        value = md.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    harness = md.get("harness") if isinstance(md.get("harness"), Mapping) else {}
    value = harness.get("stop_reason")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def signatures_for_turn(
    *,
    content: str,
    metadata: Any,
    session_id: str = "",
    message_id: Any = None,
) -> List[Dict[str, Any]]:
    """Detect every failure signature on one assistant turn. Pure."""
    md = _metadata(metadata)
    hits: List[Dict[str, Any]] = []
    example = {
        "session_id": str(session_id or "")[:120],
        "message_id": str(message_id or md.get("run_id") or "")[:120],
        "run_id": str(md.get("run_id") or "")[:120],
    }

    reason = _stop_reason(md)
    mapped = STOP_SIGNATURES.get(reason)
    if mapped:
        hits.append({
            "signature": mapped,
            "tool": "",
            "detail": reason[:_ERROR_HEAD] if reason != mapped else "",
            **example,
        })

    # Explicit flags some paths stamp without stop_reason.
    if md.get("approval_expired") or (
            isinstance(md.get("approval"), Mapping)
            and str(md["approval"].get("state") or "") == "expired"):
        hits.append({"signature": "approval_expired", "tool": "", "detail": "", **example})

    text = content if isinstance(content, str) else ""
    if not text.strip():
        hits.append({"signature": "empty_final", "tool": "", "detail": "", **example})
    elif _REFUSAL_RE.search(text[:400]):
        hits.append({"signature": "model_refusal", "tool": "", "detail": "", **example})

    events = md.get("tool_events") if isinstance(md.get("tool_events"), list) else []
    empty_run = 0
    error_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    for event in events:
        if not isinstance(event, Mapping):
            continue
        tool = _tool_name(event)
        if _tool_empty(event):
            empty_run += 1
            if empty_run >= 3:
                hits.append({
                    "signature": "repeated_empty_tool",
                    "tool": tool,
                    "detail": f"streak>={empty_run}",
                    **example,
                })
                empty_run = 0  # one hit per streak segment
        else:
            empty_run = 0
        failed, fp = _tool_failed(event)
        if failed and fp:
            error_counts[(tool or "?", fp)] += 1
        # Approval expired surfaced as a tool error string — fingerprint only.
        err = event.get("error") if isinstance(event.get("error"), str) else ""
        if "approval" in err.lower() and "expir" in err.lower():
            hits.append({"signature": "approval_expired", "tool": tool, "detail": "", **example})

    for (tool, fp), count in error_counts.items():
        if count >= 3:
            hits.append({
                "signature": "identical_tool_error",
                "tool": tool,
                "detail": f"count={count};err={fp}",
                **example,
            })

    # De-dupe identical signature+tool+session+message
    seen = set()
    unique: List[Dict[str, Any]] = []
    for hit in hits:
        key = (hit["signature"], hit.get("tool") or "", hit.get("session_id"), hit.get("message_id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def _group(hits: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        sig = str(hit.get("signature") or "")
        tool = str(hit.get("tool") or "")
        key = (sig, tool)
        if key not in buckets:
            buckets[key] = {
                "signature": sig,
                "tool": tool,
                "count": 0,
                "examples": [],
            }
            order.append(key)
        bucket = buckets[key]
        bucket["count"] += 1
        if len(bucket["examples"]) < MAX_GROUP_EXAMPLES:
            bucket["examples"].append({
                "session_id": str(hit.get("session_id") or ""),
                "message_id": str(hit.get("message_id") or ""),
                "run_id": str(hit.get("run_id") or ""),
                "detail": str(hit.get("detail") or "")[:_ERROR_HEAD],
            })
    groups = [buckets[k] for k in order]
    groups.sort(key=lambda g: (-int(g["count"]), g["signature"], g["tool"]))
    return groups


def scan_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Scan already-loaded assistant rows. No DB access."""
    hits: List[Dict[str, Any]] = []
    turns = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("role") or "") != "assistant":
            continue
        turns += 1
        hits.extend(signatures_for_turn(
            content=row.get("content") if isinstance(row.get("content"), str) else "",
            metadata=row.get("metadata"),
            session_id=str(row.get("session_id") or ""),
            message_id=row.get("id") or row.get("message_id"),
        ))
    groups = _group(hits)
    return {
        "turns_scanned": turns,
        "hit_count": len(hits),
        "signature_count": len(groups),
        "by_signature": groups,
        # Flat sample for MCP consumers that want a short list.
        "examples": [
            {
                "signature": h["signature"],
                "tool": h.get("tool") or "",
                "session_id": h.get("session_id") or "",
                "message_id": h.get("message_id") or "",
            }
            for h in hits[:MAX_EXAMPLES]
        ],
    }


def _encode_cursor(ts: Any, message_id: Any) -> str:
    """Encode keyset cursor. Must keep full timestamp precision (incl. µs).

    Truncating to seconds makes rows that share a wall-clock second but differ
    in microseconds invisible to the next page: the filter compares exact
    datetimes, so a cursor of ``T12:18:27`` skips ``T12:18:27.300000``.
    """
    if isinstance(ts, datetime):
        # Always emit microseconds so encode→decode round-trips exactly.
        stamp = ts.replace(tzinfo=None).isoformat(timespec="microseconds")
    else:
        stamp = str(ts or "")
    return f"{stamp}|{message_id}"


def _decode_cursor(raw: Any) -> Optional[Tuple[datetime, str]]:
    if not isinstance(raw, str) or "|" not in raw:
        return None
    stamp, mid = raw.split("|", 1)
    if not stamp or not mid:
        return None
    try:
        ts = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return ts, mid


def merge_reports(
    base: Mapping[str, Any],
    nxt: Mapping[str, Any],
) -> Dict[str, Any]:
    """Accumulate a continuation page into a prior report (client or tests)."""
    hits_count = int(base.get("hit_count") or 0) + int(nxt.get("hit_count") or 0)
    turns = int(base.get("turns_scanned") or 0) + int(nxt.get("turns_scanned") or 0)
    rows = int(base.get("rows_read") or 0) + int(nxt.get("rows_read") or 0)
    # Rebuild groups from concatenated group lists via recount.
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    for group in list(base.get("by_signature") or []) + list(nxt.get("by_signature") or []):
        if not isinstance(group, Mapping):
            continue
        sig = str(group.get("signature") or "")
        tool = str(group.get("tool") or "")
        key = (sig, tool)
        if key not in buckets:
            buckets[key] = {
                "signature": sig,
                "tool": tool,
                "count": 0,
                "examples": [],
            }
            order.append(key)
        bucket = buckets[key]
        bucket["count"] += int(group.get("count") or 0)
        for ex in group.get("examples") or []:
            if not isinstance(ex, Mapping):
                continue
            if len(bucket["examples"]) >= MAX_GROUP_EXAMPLES:
                break
            bucket["examples"].append({
                "session_id": str(ex.get("session_id") or ""),
                "message_id": str(ex.get("message_id") or ""),
                "run_id": str(ex.get("run_id") or ""),
                "detail": str(ex.get("detail") or "")[:_ERROR_HEAD],
            })
    groups = [buckets[k] for k in order]
    groups.sort(key=lambda g: (-int(g["count"]), g["signature"], g["tool"]))
    coverage = str(nxt.get("coverage") or "complete")
    return {
        **dict(base),
        "turns_scanned": turns,
        "hit_count": hits_count,
        "rows_read": rows,
        "signature_count": len(groups),
        "by_signature": groups,
        "coverage": coverage,
        "truncated": coverage == "partial",
        "next_cursor": nxt.get("next_cursor"),
        "generated_at": nxt.get("generated_at") or base.get("generated_at"),
        "hub_publish": nxt.get("hub_publish") or base.get("hub_publish"),
    }


def report(
    owner: Optional[str],
    days: Any = DEFAULT_DAYS,
    *,
    db=None,
    publish: bool = False,
    cursor: Any = None,
    page_limit: Any = None,
) -> Dict[str, Any]:
    """Sweep assistant turns for ``owner`` over the last ``days``.

    Walks history in keyset batches so a single call can cover the whole
    window. When a page ceiling is hit, ``coverage`` is ``partial`` and
    ``next_cursor`` lets the client continue until complete. Never claims
    full coverage while a silent row limit is active.
    """
    from sqlalchemy import and_, func, or_

    from core.database import ChatMessage, Session, SessionLocal

    days = _clamp_days(days)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    try:
        limit = int(page_limit) if page_limit is not None else MAX_ROWS
    except (TypeError, ValueError):
        limit = MAX_ROWS
    limit = max(1, min(MAX_ROWS_HARD, limit))
    start = _decode_cursor(cursor)

    own = db is None
    db = db or SessionLocal()
    all_hits: List[Dict[str, Any]] = []
    turns = 0
    rows_read = 0
    last_ts: Any = None
    last_id: Any = None
    exhausted = False
    try:
        while rows_read < limit:
            batch_n = min(BATCH_ROWS, limit - rows_read)
            query = (
                db.query(
                    ChatMessage.id,
                    ChatMessage.session_id,
                    ChatMessage.role,
                    func.substr(ChatMessage.content, 1, 500),
                    ChatMessage.meta_data,
                    ChatMessage.timestamp,
                )
                .join(Session, Session.id == ChatMessage.session_id)
                .filter(ChatMessage.timestamp >= since)
                .filter(ChatMessage.role == "assistant")
            )
            if owner:
                query = query.filter(Session.owner == owner)
            if start is not None:
                cts, cid = start
                query = query.filter(or_(
                    ChatMessage.timestamp < cts,
                    and_(ChatMessage.timestamp == cts, ChatMessage.id < cid),
                ))
            rows = (
                query.order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc())
                .limit(batch_n)
                .all()
            )
            if not rows:
                exhausted = True
                break
            for r in rows:
                turns += 1
                all_hits.extend(signatures_for_turn(
                    content=r[3] or "",
                    metadata=r[4],
                    session_id=str(r[1] or ""),
                    message_id=r[0],
                ))
            rows_read += len(rows)
            last_ts, last_id = rows[-1][5], rows[-1][0]
            start = (last_ts, last_id)
            if len(rows) < batch_n:
                exhausted = True
                break
        else:
            # Hit page_limit with a full last batch — peek one more row.
            if last_ts is not None and last_id is not None:
                peek_q = (
                    db.query(ChatMessage.id)
                    .join(Session, Session.id == ChatMessage.session_id)
                    .filter(ChatMessage.timestamp >= since)
                    .filter(ChatMessage.role == "assistant")
                )
                if owner:
                    peek_q = peek_q.filter(Session.owner == owner)
                peek_q = peek_q.filter(or_(
                    ChatMessage.timestamp < last_ts,
                    and_(ChatMessage.timestamp == last_ts, ChatMessage.id < last_id),
                ))
                exhausted = peek_q.limit(1).first() is None
            else:
                exhausted = True
    finally:
        if own:
            db.close()

    groups = _group(all_hits)
    partial = not exhausted
    next_cursor = (
        _encode_cursor(last_ts, last_id) if partial and last_id is not None else None
    )
    out: Dict[str, Any] = {
        "days": days,
        "since": since.isoformat(timespec="seconds"),
        "owner": owner or "",
        "rows_read": rows_read,
        "truncated": partial,
        "coverage": "partial" if partial else "complete",
        "next_cursor": next_cursor,
        "page_limit": limit,
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        "turns_scanned": turns,
        "hit_count": len(all_hits),
        "signature_count": len(groups),
        "by_signature": groups,
        "examples": [
            {
                "signature": h["signature"],
                "tool": h.get("tool") or "",
                "session_id": h.get("session_id") or "",
                "message_id": h.get("message_id") or "",
            }
            for h in all_hits[:MAX_EXAMPLES]
        ],
    }
    if publish:
        out["hub_publish"] = emit_hub_report(out, owner=owner)
    return out


def project_for_mcp(
    report_body: Mapping[str, Any],
    *,
    group_offset: int = 0,
    group_limit: int = 80,
) -> Dict[str, Any]:
    """Structured MCP projection: always serialises to complete, valid JSON.

    Never character-slice the dumps. Large ``by_signature`` lists continue via
    ``next_group_offset``; counters always reflect the full report.
    """
    try:
        offset = max(0, int(group_offset))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, min(200, int(group_limit)))
    except (TypeError, ValueError):
        limit = 80
    groups = [
        g for g in (report_body.get("by_signature") or []) if isinstance(g, Mapping)
    ]
    slice_ = groups[offset:offset + limit]
    projected_groups = []
    for g in slice_:
        projected_groups.append({
            "signature": str(g.get("signature") or "")[:80],
            "tool": str(g.get("tool") or "")[:80],
            "count": int(g.get("count") or 0),
            "examples": [
                {
                    "session_id": str(ex.get("session_id") or "")[:120],
                    "message_id": str(ex.get("message_id") or "")[:120],
                    "run_id": str(ex.get("run_id") or "")[:120],
                }
                for ex in (g.get("examples") or [])[:MAX_GROUP_EXAMPLES]
                if isinstance(ex, Mapping)
            ],
        })
    next_off = offset + len(slice_)
    return {
        "days": int(report_body.get("days") or 0),
        "since": str(report_body.get("since") or "")[:40],
        "owner": str(report_body.get("owner") or "")[:120],
        "turns_scanned": int(report_body.get("turns_scanned") or 0),
        "hit_count": int(report_body.get("hit_count") or 0),
        "signature_count": int(report_body.get("signature_count") or 0),
        "rows_read": int(report_body.get("rows_read") or 0),
        "coverage": str(report_body.get("coverage") or (
            "partial" if report_body.get("truncated") else "complete"
        )),
        "truncated": bool(report_body.get("truncated")),
        "next_cursor": report_body.get("next_cursor"),
        "hub_publish": report_body.get("hub_publish"),
        "group_offset": offset,
        "groups_returned": len(projected_groups),
        "next_group_offset": next_off if next_off < len(groups) else None,
        "by_signature": projected_groups,
        "generated_at": str(report_body.get("generated_at") or "")[:40],
    }


def build_hub_payload(report_body: Mapping[str, Any]) -> Dict[str, Any]:
    """Compact, secret-free payload for ``faustus.diag.failure_sweep``."""
    by_sig = []
    for group in (report_body.get("by_signature") or [])[:40]:
        if not isinstance(group, Mapping):
            continue
        by_sig.append({
            "signature": str(group.get("signature") or "")[:80],
            "tool": str(group.get("tool") or "")[:80],
            "count": int(group.get("count") or 0),
        })
    return {
        "ts": round(time.time(), 3),
        "owner": str(report_body.get("owner") or "")[:120],
        "days": int(report_body.get("days") or 0),
        "since": str(report_body.get("since") or "")[:40],
        "turns_scanned": int(report_body.get("turns_scanned") or 0),
        "signature_count": int(report_body.get("signature_count") or 0),
        "hit_count": int(report_body.get("hit_count") or 0),
        "coverage": str(report_body.get("coverage") or "complete")[:20],
        "truncated": bool(report_body.get("truncated")),
        "rows_read": int(report_body.get("rows_read") or 0),
        "by_signature": by_sig,
    }


def emit_hub_report(report_body: Mapping[str, Any], *, owner: Optional[str] = None) -> str:
    """Publish to the Hub bus and return a verified status string.

    ``delivered`` — Hub accepted the event (blocking emit returned True).
    ``skipped`` — Hub disabled / not configured.
    ``failed`` — emit raised or Hub rejected.
    Never raises to the caller.
    """
    _ = owner  # payload already carries owner; keep kw for call-site symmetry
    try:
        payload = build_hub_payload(report_body)
        from src import family_services
        from src.hoard_link import family

        family_services.configure()
        if not family.status().get("enabled", True):
            return "skipped"
        body = {key: payload[key] for key in _HUB_KEYS if key in payload}
        ok = family.emit(EVENT_TYPE, body, block=True, timeout=SEND_TIMEOUT_S)
        return "delivered" if ok else "failed"
    except Exception:  # noqa: BLE001
        logger.debug("failure_sweep hub emit failed", exc_info=True)
        return "failed"
