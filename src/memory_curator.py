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
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.memory import get_text_similarity
from src import memory_engine as engine

logger = logging.getLogger(__name__)

# Different visible scopes may share global rows. Serialize the full pass,
# not individual mutations or per-owner calls. External writers/processes
# do not participate in this lock; this is not a database transaction.
_CURATION_LOCK = threading.RLock()


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


def _window_key(item: Dict[str, Any]) -> tuple:
    """Preserve declared windows without disabling ordinary repeated inserts.

    add_item defaults valid_from to created_at. The persisted schema does not
    distinguish that default from an explicitly supplied identical timestamp.
    Keep the existing dedup policy for that ambiguous, open-ended case only.
    """
    start, end = str(item.get("valid_from") or ""), str(item.get("valid_until") or "")
    provenance = item.get("provenance") or {}
    state = str(provenance.get("temporal_state") or "") if isinstance(provenance, dict) else ""
    if not end and not state and start == str(item.get("created_at") or ""):
        return ("implicit", "", "")
    return (start, end, state)


def _scope_key(item: Dict[str, Any]) -> tuple:
    """Mutation identity, distinct from visibility in ``scoped_items``.

    A session id is a boundary only for declared session scope; project
    records may retain a session id merely as their source. Legacy records
    without a scope inherit the project/global gate, never the source id.
    Keep both the label and id for session rows so inconsistent declarations
    cannot silently collapse into another session.
    """
    owner, project = str(item.get("owner") or ""), str(item.get("project") or "")
    scope = str(item.get("scope") or (f"project:{project}" if project else "global"))
    session = str(item.get("session_id") or "") if scope == "session" or scope.startswith("session:") else ""
    return owner, project, scope, session


def _identity_key(item: Dict[str, Any]) -> tuple:
    return (_scope_key(item), str(item.get("level") or ""), _window_key(item))


def _dedupe_key(item: Dict[str, Any]) -> tuple:
    return (_identity_key(item), str(item.get("status") or ""), _norm(item.get("text")))


def _same_scope(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Only ever compare like with like.

    Status is part of this on purpose: ``AVOID: <text>`` is one token away
    from ``<text>``, so an anti-pattern and the rule it was inverted from
    always clear the similarity bar. Merging them would silently swallow the
    pair the CONFLICT pass exists to resolve — and that pass keeps both rows,
    which is what makes the history readable.
    """
    return (a.get("status") == b.get("status")
            # Even overlapping periods carry different historical claims.
            # Absorption preserves only the winner's period, so require equality.
            and _identity_key(a) == _identity_key(b))


_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)*%?(?!\w)")
_QUALIFIER_RE = re.compile(
    r"\b(?:always|never|sometimes|only|not|without|no|siempre|nunca|"
    r"jamás|jamas|solo|sólo|sin)\b", re.IGNORECASE,
)
_CALENDAR_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december|enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|lunes|martes|miércoles|miercoles|jueves|"
    r"viernes|sábado|sabado|domingo)\b", re.IGNORECASE,
)


def _same_critical_details(a: str, b: str) -> bool:
    """Keep fuzzy matching from erasing numbers, calendar words or qualifiers.

    This is intentionally conservative: a missed merge is cheaper to review
    than deleting a distinct fact along with its provenance.
    """
    return (_NUMBER_RE.findall(a) == _NUMBER_RE.findall(b)
            and [m.casefold() for m in _CALENDAR_RE.findall(a)]
            == [m.casefold() for m in _CALENDAR_RE.findall(b)]
            and [m.casefold() for m in _QUALIFIER_RE.findall(a)]
            == [m.casefold() for m in _QUALIFIER_RE.findall(b)])


def _dedupe(items: List[Dict[str, Any]], now: datetime,
            report: Dict[str, int]) -> List[Dict[str, Any]]:
    """Exact-text match first (a dict lookup), then Jaccard similarity above
    0.85 against what is already kept. Returns the survivors.

    Items are walked in id order so the same store always dedupes the same
    way; only items of the same level, scope and validity window are compared — a
    semantic fact and a procedural rule that happen to read alike are not the
    same memory.
    """
    kept: List[Dict[str, Any]] = []
    by_text: Dict[tuple, int] = {}

    for item in sorted(items, key=lambda i: str(i.get("id") or "")):
        index = by_text.get(_dedupe_key(item))
        if index is None:
            for position, candidate in enumerate(kept):
                if not _same_scope(candidate, item):
                    continue
                candidate_text = str(candidate.get("text") or "")
                item_text = str(item.get("text") or "")
                if (_same_critical_details(candidate_text, item_text)
                        and get_text_similarity(candidate_text, item_text) > engine.DEDUPE_SIMILARITY):
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
    """An anti-pattern beats its rule only in the same mutation identity."""
    anti_texts = {
        (_identity_key(item), _norm(item.get("inverted_from")))
        for item in items
        if item.get("status") == "anti_pattern" and item.get("inverted_from")
    }
    if not anti_texts:
        return
    for item in items:
        if item.get("status") != "active":
            continue
        if (_identity_key(item), _norm(item.get("text"))) not in anti_texts:
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
    item["type"] = "anti_pattern"  # MEM-01: type tracks status here too
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
    with _CURATION_LOCK:
        return _curate(owner, project, now)


def _curate(owner: Optional[str], project: Optional[str],
            now: Optional[datetime]) -> Dict[str, Any]:
    """Run the passes while the process-wide curator lock is held."""
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
