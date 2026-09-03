"""Learned memory — layer 2: the Curator (FAUSTUS).

100% deterministic and LLM-free. Everything here is arithmetic over the
records ``src/memory_engine.py`` already keeps, so the same store and the same
clock always produce the same report — which is what lets a user trust a
process that DELETES their memories.

Five passes, in order, because each one changes what the next one sees:

1. **dedupe** — exact text match first, then Jaccard (``get_text_similarity``)
   above 0.85. The survivor is the higher ``effective_score``; the loser's
   evidence and feedback events are merged into it before it is deleted, so
   consolidating never throws away the proof that earned the score.
2. **conflict** — an active item and an anti-pattern inverted from that same
   text cannot both stand. The anti-pattern wins: what was learned by being
   burned outranks what was merely asserted.
3. **maturity** — the ladder (candidate → established → proven) counts
   DISTINCT refs, not events, so a single looping session cannot promote a
   rule. Demotion to deprecated is by score, with one exception (below).
4. **inversion** — a rule that is mostly harmful becomes ``AVOID: <text>``
   instead of disappearing.
5. **prune** — deprecated items untouched for 90 days are deleted for real.

Judgment call worth naming: an anti-pattern is never deprecated by the score
rule. Inversion by definition leaves the score deeply negative (that is what
triggered it), so applying the rule would delete every warning the moment it
was created — the opposite of the point.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.memory import get_text_similarity
from src import memory_engine as engine

logger = logging.getLogger(__name__)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _merge_events(survivor: Dict[str, Any], loser: Dict[str, Any]) -> None:
    """Fold the loser's evidence and feedback into the survivor, dropping
    exact duplicates so a re-run of the Curator is idempotent."""
    for key in ("evidence", "helpful", "harmful"):
        merged: List[Dict[str, Any]] = []
        seen = set()
        for event in list(survivor.get(key) or []) + list(loser.get(key) or []):
            if not isinstance(event, dict):
                continue
            fingerprint = tuple(sorted((str(k), str(v)) for k, v in event.items()))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(event)
        survivor[key] = merged[-engine.MAX_EVENTS:]


def _dedupe_key(item: Dict[str, Any]) -> str:
    return "\x00".join((str(item.get("status") or ""), str(item.get("level") or ""),
                        str(item.get("owner") or ""), str(item.get("project") or ""),
                        _norm(item.get("text"))))


def _same_scope(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Only ever compare like with like.

    Status is part of this on purpose: ``AVOID: <text>`` is one token away
    from ``<text>``, so an anti-pattern and the rule it was inverted from
    always clear the similarity bar. Merging them would silently swallow the
    pair the CONFLICT pass exists to resolve — and that pass keeps both rows,
    which is what makes the history readable.
    """
    return (a.get("status") == b.get("status")
            and a.get("level") == b.get("level")
            and (a.get("owner") or "") == (b.get("owner") or "")
            and (a.get("project") or "") == (b.get("project") or ""))


def _dedupe(items: List[Dict[str, Any]], now: datetime,
            report: Dict[str, int]) -> List[Dict[str, Any]]:
    """Exact-text match first (a dict lookup), then Jaccard similarity above
    0.85 against what is already kept. Returns the survivors.

    Items are walked in id order so the same store always dedupes the same
    way; only items of the same level and scope are ever compared — a
    semantic fact and a procedural rule that happen to read alike are not the
    same memory.
    """
    kept: List[Dict[str, Any]] = []
    by_text: Dict[str, int] = {}

    for item in sorted(items, key=lambda i: str(i.get("id") or "")):
        index = by_text.get(_dedupe_key(item))
        if index is None:
            for position, candidate in enumerate(kept):
                if not _same_scope(candidate, item):
                    continue
                if get_text_similarity(str(candidate.get("text") or ""),
                                       str(item.get("text") or "")) > engine.DEDUPE_SIMILARITY:
                    index = position
                    break
        if index is None:
            kept.append(item)
            by_text[_dedupe_key(item)] = len(kept) - 1
            continue
        winner, loser = _rank(kept[index], item, now)
        _absorb(winner, loser, report)
        kept[index] = winner
        by_text[_dedupe_key(winner)] = index
    return kept


def _rank(a: Dict[str, Any], b: Dict[str, Any], now: datetime):
    """Higher effective_score wins; the id breaks a tie so the outcome is
    stable across runs."""
    sa = engine.effective_score(a, now)
    sb = engine.effective_score(b, now)
    if sb > sa or (sb == sa and str(b.get("id")) < str(a.get("id"))):
        return b, a
    return a, b


def _absorb(winner: Dict[str, Any], loser: Dict[str, Any],
            report: Dict[str, int]) -> None:
    """Fold the loser into the winner and delete it. The evidence and the
    feedback events move first — consolidating must never throw away the
    proof that earned the surviving score."""
    _merge_events(winner, loser)
    if loser.get("inverted_from") and not winner.get("inverted_from"):
        winner["inverted_from"] = loser["inverted_from"]
    engine.save_item(winner)
    engine.delete_item(loser.get("id"))
    report["deduped"] += 1


def _resolve_conflicts(items: List[Dict[str, Any]], report: Dict[str, int]) -> None:
    """An anti-pattern beats the active item it was inverted from."""
    anti_texts = {
        _norm(item.get("inverted_from")): item
        for item in items
        if item.get("status") == "anti_pattern" and item.get("inverted_from")
    }
    if not anti_texts:
        return
    for item in items:
        if item.get("status") != "active":
            continue
        if _norm(item.get("text")) not in anti_texts:
            continue
        item["status"] = "deprecated"
        item["maturity"] = "deprecated"
        engine.save_item(item)
        report["conflicts"] += 1
        report["demoted"] += 1


def _invert(item: Dict[str, Any], now: datetime, report: Dict[str, int]) -> bool:
    """Mostly harmful with enough events → keep it as an anti-pattern."""
    if item.get("status") == "anti_pattern":
        return False
    harmful = item.get("harmful") or []
    if len(harmful) < engine.INVERT_MIN_HARMFUL:
        return False
    if engine.harmful_ratio(item, now) <= engine.INVERT_HARM_RATIO:
        return False
    original = str(item.get("text") or "")
    item["inverted_from"] = original
    item["text"] = f"AVOID: {original}"[:engine.MAX_TEXT_CHARS]
    item["status"] = "anti_pattern"
    item["maturity"] = "candidate"
    item["evidence"] = (list(item.get("evidence") or []) + engine.normalize_evidence(
        [{"kind": "chat", "excerpt": f"inverted from: {original}"}]))[-engine.MAX_EVENTS:]
    engine.save_item(item)
    report["inverted"] += 1
    return True


def _has_recent_helpful(item: Dict[str, Any], now: datetime) -> bool:
    for event in item.get("helpful") or []:
        if not isinstance(event, dict):
            continue
        parsed = engine.parse_iso(event.get("ts"))
        if parsed is None:
            continue
        if (now - parsed).total_seconds() / 86400.0 <= engine.RECENT_HELPFUL_DAYS:
            return True
    return False


def _maturity(item: Dict[str, Any], now: datetime, report: Dict[str, int]) -> None:
    """The ladder. Promotions count distinct refs; demotion is by score, with
    the procedural-with-recent-help exception."""
    if item.get("status") == "anti_pattern":
        return
    score = engine.effective_score(item, now)
    if score < engine.SCORE_FLOOR:
        protected = (item.get("level") == "procedural" and _has_recent_helpful(item, now))
        if not protected:
            if item.get("status") != "deprecated" or item.get("maturity") != "deprecated":
                item["status"] = "deprecated"
                item["maturity"] = "deprecated"
                engine.save_item(item)
                report["demoted"] += 1
            return
    if item.get("status") != "active":
        return
    refs = engine.distinct_refs(item.get("helpful"))
    ratio = engine.harmful_ratio(item, now)
    maturity = item.get("maturity")
    target = maturity
    if refs >= engine.PROVEN_MIN_REFS and ratio < engine.PROVEN_MAX_HARM_RATIO:
        target = "proven"
    elif refs >= engine.ESTABLISHED_MIN_REFS:
        target = "established" if maturity in ("candidate", "deprecated") else maturity
    elif maturity == "deprecated":
        target = "candidate"
    if target != maturity:
        rank = {"deprecated": 0, "candidate": 1, "established": 2, "proven": 3}
        item["maturity"] = target
        engine.save_item(item)
        if rank.get(target, 0) > rank.get(maturity, 0):
            report["promoted"] += 1
        else:
            report["demoted"] += 1


def _prune(item: Dict[str, Any], now: datetime, report: Dict[str, int]) -> bool:
    if item.get("status") != "deprecated":
        return False
    stale_since = item.get("last_accessed") or item.get("updated_at")
    if engine._days_since(stale_since, now) <= engine.PRUNE_AFTER_DAYS:
        return False
    engine.delete_item(item.get("id"))
    report["pruned"] += 1
    return True


def curate(owner: Optional[str] = None, project: Optional[str] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run every pass over one scope and report what changed.

    Report: ``{deduped, conflicts, inverted, promoted, demoted, pruned,
    total_active}``. Raises only if the store itself is unusable — callers on
    a hot path should use :func:`safe_curate`.
    """
    now = now or engine._utcnow()
    report = {"deduped": 0, "conflicts": 0, "inverted": 0,
              "promoted": 0, "demoted": 0, "pruned": 0, "total_active": 0}

    items = engine.scoped_items(owner, project, engine.STATUSES)
    items = _dedupe(items, now, report)

    # Inversion before the ladder: an item that just became an anti-pattern
    # must not also be demoted for the score that made it one.
    for item in items:
        _invert(item, now, report)

    _resolve_conflicts(items, report)

    remaining: List[Dict[str, Any]] = []
    for item in items:
        _maturity(item, now, report)
        if not _prune(item, now, report):
            remaining.append(item)

    report["total_active"] = len([i for i in remaining if i.get("status") == "active"])
    return report


def safe_curate(owner: Optional[str] = None, project: Optional[str] = None,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """:func:`curate` that never raises — for schedulers and hot paths."""
    try:
        return curate(owner=owner, project=project, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory curator failed: %s", exc)
        return {"deduped": 0, "conflicts": 0, "inverted": 0, "promoted": 0,
                "demoted": 0, "pruned": 0, "total_active": 0, "error": str(exc)}
