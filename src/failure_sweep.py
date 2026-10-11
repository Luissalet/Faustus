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
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 1
MAX_DAYS = 30
MAX_ROWS = 50000
MAX_EXAMPLES = 5
MAX_GROUP_EXAMPLES = 3
_ERROR_HEAD = 80
_REFUSAL_RE = re.compile(
    r"(?i)^\s*(i\s+can'?t\s+help\s+with\s+that|i\s+cannot\s+assist|"
    r"lo\s+siento,?\s+no\s+(puedo|debo)|as an ai (language )?model)"
)

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
    "hit_count", "by_signature",
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
    """Return (failed, fingerprint). Fingerprint is empty when not failed."""
    if not isinstance(event, Mapping):
        return False, ""
    code = event.get("exit_code")
    err = event.get("error")
    out = event.get("output")
    err_text = err if isinstance(err, str) else ""
    if code not in (None, 0, "0"):
        return True, _error_fingerprint(err_text or f"exit:{code}")
    if err_text.strip():
        return True, _error_fingerprint(err_text)
    status = str(event.get("status") or "").lower()
    if status in ("error", "failed", "failure"):
        return True, _error_fingerprint(err_text or status)
    if isinstance(out, str) and out.strip().lower().startswith("error:"):
        return True, _error_fingerprint(out)
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


def report(owner: Optional[str], days: Any = DEFAULT_DAYS, *, db=None,
           publish: bool = False) -> Dict[str, Any]:
    """Sweep every assistant turn for ``owner`` over the last ``days``."""
    from sqlalchemy import func

    from core.database import ChatMessage, Session, SessionLocal

    days = _clamp_days(days)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    own = db is None
    db = db or SessionLocal()
    try:
        query = (
            db.query(
                ChatMessage.id,
                ChatMessage.session_id,
                ChatMessage.role,
                func.substr(ChatMessage.content, 1, 500),
                ChatMessage.meta_data,
            )
            .join(Session, Session.id == ChatMessage.session_id)
            .filter(ChatMessage.timestamp >= since)
            .filter(ChatMessage.role == "assistant")
        )
        if owner:
            query = query.filter(Session.owner == owner)
        rows = query.order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc()).limit(MAX_ROWS).all()
    finally:
        if own:
            db.close()

    scanned = scan_rows({
        "id": r[0],
        "session_id": r[1],
        "role": r[2],
        "content": r[3] or "",
        "metadata": r[4],
    } for r in rows)

    out = {
        "days": days,
        "since": since.isoformat(timespec="seconds"),
        "owner": owner or "",
        "rows_read": len(rows),
        "truncated": len(rows) >= MAX_ROWS,
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        **scanned,
    }
    if publish:
        emit_hub_report(out, owner=owner)
    return out


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
        "by_signature": by_sig,
    }


def emit_hub_report(report_body: Mapping[str, Any], *, owner: Optional[str] = None) -> bool:
    """Best-effort Hub bus publish; never raises and never blocks the caller."""
    try:
        payload = build_hub_payload(report_body)
        threading.Thread(
            target=_send_hub,
            args=(payload, owner),
            name="faustus-failure-sweep",
            daemon=True,
        ).start()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("failure_sweep hub emit skipped", exc_info=True)
        return False


def _send_hub(payload: Dict[str, Any], owner: Optional[str]) -> None:
    try:
        from src import family_services
        from src.hoard_link import family

        family_services.configure()
        if not family.status().get("enabled", True):
            return
        body = {key: payload[key] for key in _HUB_KEYS if key in payload}
        family.emit(EVENT_TYPE, body, block=True, timeout=SEND_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        logger.debug("failure_sweep hub emit failed", exc_info=True)
