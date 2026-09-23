"""brain/wiki.py — short entity pages, kept current, always cited.

``entities.profile(entity_id)`` already assembles everything known about an
entity — its facts (the memory sentences that mention it) and its relations
(with their validity windows). What it does not do is turn that into
readable prose; that is this module's one job, in two tiers:

* :func:`render_summary_fallback` — a deterministic bullet list. Always
  available, always instant, never wrong (it just restates the facts).
* :func:`refresh_entity` — asks the cheap utility model for 2-5 sentences
  in the language of the facts, each claim followed by a
  ``[mem:<id8>]``-style citation. The model's output is used ONLY if every
  citation it wrote resolves to a fact that is actually in the profile —
  a citation of anything else means the model is making something up, and
  the whole summary is rejected in favour of the deterministic fallback.
  This is the same "grounding decides whether a background model result is
  trusted" discipline as ``brain/extract.py``'s entity/relation filter.

Both the request and the acceptance check run only in the background
(``refresh_entity``/``refresh_stale`` are async, called from a maintenance
sweep, never from a chat turn), and a locked summary (``summary_locked`` —
a human edited it) or an unchanged fact set (``facts_hash``) is left alone.
The sweep is also polite with the machine: each model call first asks
``extract.background_llm_gate``, so none happens while a turn is in flight
or when the utility model would have to be loaded (and something else
evicted) to answer.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Set

from src.brain.db import dumps, sha
from src.brain.entities import get_entity, profile, update_entity, list_entities

logger = logging.getLogger(__name__)

_MAX_SUMMARY_TOKENS = 500
_CITATION_RE = re.compile(r"\[([a-zA-Z0-9_./:-]+)\]")


def _citation_ref(fact: Dict[str, Any]) -> str:
    """The citation token a fact is cited by: ``mem:<id8>`` for a learned
    memory (the short id the rest of the UI already shows), the full
    ``source_ref`` for anything else (a personal memory or a vault note
    does not have a shorter public id)."""
    source_ref = str(fact.get("source_ref") or "")
    if source_ref.startswith("mem:"):
        return f"mem:{source_ref[4:12]}"
    return source_ref


def _fact_refs(profile_data: Dict[str, Any]) -> List[str]:
    return [_citation_ref(f) for f in profile_data.get("facts") or [] if f.get("source_ref")]


def _facts_hash(profile_data: Dict[str, Any]) -> str:
    """A stable fingerprint of everything the summary could cite — the
    facts AND the relations valid now. Changes exactly when there is new
    material worth re-summarising."""
    facts_key = sorted(
        (f.get("source_ref", ""), f.get("text", ""), f.get("valid_from", ""),
        f.get("valid_until", ""))
        for f in profile_data.get("facts") or []
    )
    rel_key = sorted(
        (r.get("rel", ""), r.get("dst", ""), r.get("dst_value", ""),
        r.get("valid_from", ""), r.get("valid_until", ""), r.get("status", ""))
        for r in profile_data.get("relations") or []
    )
    return sha(dumps({"facts": facts_key, "relations": rel_key}))


def render_summary_fallback(profile_data: Dict[str, Any]) -> str:
    """Deterministic bullets — always correct, never citing anything that
    is not literally in `profile_data`."""
    entity = profile_data.get("entity") or {}
    name = str(entity.get("name") or "?")
    lines = [f"- {name} ({entity.get('type', 'other')})"]

    for rel in profile_data.get("relations") or []:
        if not rel.get("valid_at"):
            continue
        label = rel.get("dst_name") or rel.get("dst_value") or ""
        if not label:
            continue
        lines.append(f"- {rel.get('rel', '')}: {label}")

    shown = 0
    for fact in profile_data.get("facts") or []:
        if not fact.get("valid_now") or not fact.get("text"):
            continue
        lines.append(f"- {fact['text']}")
        shown += 1
        if shown >= 5:
            break

    return "\n".join(lines)


def _build_wiki_prompt(profile_data: Dict[str, Any]) -> str:
    entity = profile_data.get("entity") or {}
    lines = [
        f"Write a short wiki-style summary (2 to 5 sentences) of \"{entity.get('name', '')}\", "
        f"in the SAME language as the facts below.",
        "Every claim you make must be followed by a citation of the fact it comes from, "
        "in square brackets, using EXACTLY the reference shown before that fact — "
        "for example: \"Works at Cordera Labs [mem:ab12cd34].\"",
        "Never write a citation that is not one of the references listed below. "
        "If you are not sure a claim is supported, leave it out.",
        "Return ONLY the summary text, no heading, no extra commentary.",
        "",
        "Facts:",
    ]
    for fact in profile_data.get("facts") or []:
        ref = _citation_ref(fact)
        if ref and fact.get("text"):
            lines.append(f"- [{ref}] {fact['text']}")
    lines.append("")
    lines.append("Relations currently valid:")
    for rel in profile_data.get("relations") or []:
        if not rel.get("valid_at"):
            continue
        label = rel.get("dst_name") or rel.get("dst_value") or ""
        if label:
            lines.append(f"- {rel.get('rel', '')}: {label}")
    return "\n".join(lines)


async def refresh_entity(entity_id: Any, *, force: bool = False, background: bool = False,
                         defer_reason: str = "") -> Dict[str, Any]:
    """Rewrite one entity's summary with the utility model, or fall back to
    the deterministic bullets. Never raises; always returns a `status`:

    - ``not_found`` — no such entity.
    - ``locked`` — `summary_locked` is set and `force` was not passed.
    - ``unchanged`` — the facts/relations have not changed since the last
      summary; nothing was rewritten.
    - ``disabled`` — `brain_wiki_summaries` is off.
    - ``fallback`` — no endpoint configured, or the model call failed: the
      deterministic bullets were written instead.
    - ``rejected`` — the model answered but cited something not in the
      profile; the deterministic bullets were written instead.
    - ``updated`` — the model's cited summary was accepted and stored.
    - ``deferred`` (only with `background`) — the model may not be called
      right now (``reason``: a turn is in flight, or the model is not
      already resident — see ``extract.background_llm_gate``). An entity
      with no summary yet gets the deterministic bullets; ``facts_hash`` is
      left alone either way, so a later sweep retries with the model.
      `defer_reason` lets a sweep that already got a residency refusal
      defer the rest without asking the runner again.
    """
    entity_id = str(entity_id or "")
    try:
        entity = get_entity(entity_id)
        if not entity:
            return {"status": "not_found", "entity_id": entity_id}
        if entity.get("summary_locked") and not force:
            return {"status": "locked", "entity_id": entity_id}

        profile_data = profile(entity_id)
        facts_hash = _facts_hash(profile_data)
        if not force and entity.get("facts_hash") == facts_hash and entity.get("summary"):
            return {"status": "unchanged", "entity_id": entity_id}

        try:
            from src.settings import get_setting
            wiki_on = bool(get_setting("brain_wiki_summaries", True))
        except Exception:  # noqa: BLE001
            wiki_on = True
        if not wiki_on:
            return {"status": "disabled", "entity_id": entity_id}

        fallback = render_summary_fallback(profile_data)
        refs = _fact_refs(profile_data)
        owner = entity.get("owner", "")

        def _store_fallback() -> Dict[str, Any]:
            update_entity(entity_id, summary=fallback, summary_sources=refs,
                         facts_hash=facts_hash)
            return {"status": "fallback", "entity_id": entity_id}

        try:
            from src.endpoint_resolver import resolve_endpoint
            url, model, headers = resolve_endpoint("utility", owner=owner)
        except Exception:  # noqa: BLE001
            return _store_fallback()
        if not url or not model:
            return _store_fallback()

        if background:
            from src.brain.extract import background_llm_gate

            reason = defer_reason or background_llm_gate(url, model)
            if reason:
                if not entity.get("summary"):
                    update_entity(entity_id, summary=fallback, summary_sources=refs)
                logger.debug("brain.wiki: %s deferred (%s)", entity_id, reason)
                return {"status": "deferred", "entity_id": entity_id, "reason": reason}

        try:
            from src.llm_core import llm_call_async
            raw = await llm_call_async(
                url=url, model=model,
                messages=[{"role": "user", "content": _build_wiki_prompt(profile_data)}],
                headers=headers, temperature=0.2, max_tokens=_MAX_SUMMARY_TOKENS,
                timeout=45, max_retries=1, workload="background",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain.wiki: model call failed for %s (%s)", entity_id, exc)
            return _store_fallback()
        if isinstance(raw, tuple):
            raw = raw[0]
        text = str(raw or "").strip()
        if not text:
            return _store_fallback()

        cited: Set[str] = set(_CITATION_RE.findall(text))
        valid_refs = set(refs)
        if valid_refs:
            if not cited or not cited.issubset(valid_refs):
                return _store_fallback()
        elif cited:
            # No facts to cite at all, yet the model cited something -> not
            # grounded in anything real about this entity.
            return _store_fallback()

        update_entity(entity_id, summary=text, summary_sources=sorted(cited),
                     summary_locked=False, facts_hash=facts_hash)
        return {"status": "updated", "entity_id": entity_id}
    except Exception as exc:  # noqa: BLE001 - a maintenance pass must never raise
        logger.debug("brain.wiki: refresh_entity(%s) failed (%s)", entity_id, exc)
        return {"status": "error", "entity_id": entity_id}


async def refresh_stale(owner: Any, *, limit: int = 5, budget_s: float = 30.0,
                        background: bool = True) -> Dict[str, Any]:
    """Refresh up to `limit` entities whose facts changed since their last
    summary. Never raises.

    `background` (the default: this is the maintenance sweep) makes every
    model call ask ``extract.background_llm_gate`` first. A turn in flight
    stops the sweep; a model that is not resident (or whose residency is
    unknown) defers the remaining entities without calling it.
    ``report["llm_skipped"]`` says why, ``report["deferred"]`` how many."""
    start = time.monotonic()
    report: Dict[str, Any] = {"checked": 0, "updated": 0, "skipped": 0, "errors": 0,
                              "deferred": 0, "llm_skipped": ""}
    try:
        candidates = []
        for entity in list_entities(str(owner or ""), limit=2000, include_hidden=True):
            if entity.get("summary_locked"):
                continue
            try:
                facts_hash = _facts_hash(profile(entity["id"]))
            except Exception:  # noqa: BLE001
                continue
            if entity.get("facts_hash") != facts_hash or not entity.get("summary"):
                candidates.append(entity)

        defer_reason = ""
        for entity in candidates[:max(0, int(limit or 5))]:
            if time.monotonic() - start > budget_s:
                break
            report["checked"] += 1
            try:
                result = await refresh_entity(entity["id"], background=background,
                                              defer_reason=defer_reason)
            except Exception:  # noqa: BLE001
                report["errors"] += 1
                continue
            status = result.get("status")
            if status == "deferred":
                report["deferred"] += 1
                report["llm_skipped"] = str(result.get("reason") or "")
                if report["llm_skipped"] == "interactive_turn":
                    break  # somebody is waiting on this machine: stop here
                defer_reason = report["llm_skipped"]
            elif status in ("updated", "fallback", "rejected"):
                report["updated"] += 1
            elif status == "error":
                report["errors"] += 1
            else:
                report["skipped"] += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("brain.wiki: refresh_stale failed (%s)", exc)
        report["errors"] += 1
    return report


__all__ = ["render_summary_fallback", "refresh_entity", "refresh_stale"]
