"""Measured stopping for Deep Research: saturation and a confidence signal.

A research run used to stop when the round budget or the clock ran out, or
when the model said "enough" (it almost never does). This module measures
instead, with no model call:

* **Facts.** Every finding's summary (or, without one, its evidence) is split
  into sentences. A sentence is a *new fact* unless an earlier one has the
  same normalised text or a token-set Jaccard similarity of at least
  ``near_dup`` (0.8). A near-duplicate from a different source is not new but
  it *corroborates* the earlier fact.
* **Sources.** A source is the canonical form of its URL's domain (lowercase,
  no ``www.``); a page from a domain already read is not a new source.
* **Saturation rule.** A round that added fewer than ``min_new_facts`` new
  facts AND no new source is *saturated*. ``patience`` saturated rounds in a
  row stop the run (reason ``saturated``). Rounds that read nothing at all are
  not counted: an empty round says nothing about the topic, and the loop
  already has its own handling for searches that return nothing.
* **Confidence.** ``0.4 * coverage + 0.3 * consistency + 0.3 * saturation``
  where coverage is the share of sub-questions with at least one supporting
  fact, consistency the share of facts backed by two or more sources (there
  is no contradiction detector, so corroboration is the measurable proxy) and
  saturation ``1 - new/total`` facts of the last round. The run may stop once
  it reaches ``confidence_threshold`` (reason ``confidence_reached``).

Nothing here touches the clock: ``max_time`` stays the hard ceiling and the
caller checks it as before.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from src.research_prune import tokenize

STOP_SATURATED = "saturated"
STOP_CONFIDENCE = "confidence_reached"

WEIGHT_COVERAGE = 0.4
WEIGHT_CONSISTENCY = 0.3
WEIGHT_SATURATION = 0.3

NEAR_DUPLICATE_JACCARD = 0.8
MIN_FACT_TOKENS = 4
MAX_FACTS_PER_FINDING = 12
#: No stop decision before this many rounds have completed: one round's
#: numbers are all "new" by construction.
MIN_ROUNDS_FOR_STOP = 2

_TRACKING_PARAMS = frozenset({"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid",
                              "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid"})
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+|\s[•·]\s|\s-\s(?=[A-ZÁÉÍÓÚÑ])")


def canonical_url(url: str) -> str:
    """URL without fragment, tracking parameters, ``www.`` or a trailing
    slash: two spellings of one page compare equal."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(parts.query, keep_blank_values=True))
                     if k.lower() not in _TRACKING_PARAMS)
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower(), host + (f":{parts.port}" if parts.port else ""), path, query, ""))


def source_key(url: str) -> str:
    """The unit a "new source" is counted in: the canonical domain."""
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return host or canonical_url(url)


def normalise_fact(text: str) -> str:
    return " ".join(tokenize(text, drop_stopwords=False))


def split_facts(text: str) -> List[str]:
    """Candidate fact sentences of ``text`` (short fragments dropped)."""
    out: List[str] = []
    for piece in _SENTENCE_SPLIT.split(text or ""):
        piece = piece.strip(" \t-•·*")
        if len(tokenize(piece)) >= MIN_FACT_TOKENS:
            out.append(piece)
        if len(out) >= MAX_FACTS_PER_FINDING:
            break
    return out


def finding_facts(finding: Mapping[str, Any]) -> List[str]:
    """The facts one finding contributes: its summary, or (when the summary
    is empty) its evidence."""
    if not isinstance(finding, Mapping):
        return []
    for key in ("summary", "evidence", "content"):
        value = finding.get(key)
        if isinstance(value, str) and value.strip():
            facts = split_facts(value)
            if facts:
                return facts
    return []


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


@dataclass
class _Fact:
    text: str
    tokens: frozenset
    sources: Set[str] = field(default_factory=set)


class SaturationTracker:
    """Accumulates facts and sources round by round and decides when the
    evidence has stopped growing. See the module docstring for the rules."""

    def __init__(self, subquestions: Sequence[str] = (), *, min_new_facts: int = 2, patience: int = 1,
                 confidence_threshold: float = 0.0, near_dup: float = NEAR_DUPLICATE_JACCARD,
                 min_rounds: int = MIN_ROUNDS_FOR_STOP, saturation_stop: bool = True):
        self.subquestions = [q for q in (subquestions or []) if isinstance(q, str) and q.strip()]
        self._sub_tokens = [set(tokenize(q)) for q in self.subquestions]
        self.min_new_facts = max(0, int(min_new_facts))
        self.patience = max(1, int(patience))
        self.confidence_threshold = float(confidence_threshold or 0.0)
        self.near_dup = float(near_dup)
        self.min_rounds = max(1, int(min_rounds))
        self.saturation_stop = bool(saturation_stop)
        self._facts: List[_Fact] = []
        self._exact: Dict[str, _Fact] = {}
        self._sources: Set[str] = set()
        self._urls: Set[str] = set()
        self.rounds: List[Dict[str, Any]] = []
        self._streak = 0

    # -- facts ---------------------------------------------------------
    def _register(self, text: str, source: str) -> bool:
        """Add one fact; True if it is new, False if it (nearly) repeats an
        earlier one (which then gains ``source`` as a corroborating source)."""
        norm = normalise_fact(text)
        tokens = frozenset(norm.split())
        if not tokens:
            return False
        known = self._exact.get(norm)
        if known is None:
            for cand in self._facts:
                lo, hi = sorted((len(tokens), len(cand.tokens)))
                if hi and lo / hi < self.near_dup:
                    continue          # Jaccard cannot reach the bar with sets this different in size
                if jaccard(tokens, cand.tokens) >= self.near_dup:
                    known = cand
                    break
        if known is not None:
            if source:
                known.sources.add(source)
            return False
        fact = _Fact(text=text, tokens=tokens, sources={source} if source else set())
        self._facts.append(fact)
        self._exact[norm] = fact
        return True

    def seed(self, findings: Iterable[Mapping[str, Any]]) -> None:
        """Register findings that predate this run (a continuation) so they
        are not counted as this run's progress."""
        for f in findings or []:
            if not isinstance(f, Mapping):
                continue
            url = str(f.get("url") or "")
            key = source_key(url) if url else ""
            if key:
                self._sources.add(key)
            if url:
                self._urls.add(canonical_url(url))
            for fact in finding_facts(f):
                self._register(fact, key)

    # -- measures ------------------------------------------------------
    def coverage(self) -> float:
        """Share of sub-questions with at least one supporting fact. With no
        sub-questions the run is treated as covered once it has any fact."""
        if not self._sub_tokens:
            return 1.0 if self._facts else 0.0
        covered = 0
        for q in self._sub_tokens:
            if not q:
                continue
            need = max(1, int(round(0.4 * len(q))))
            if any(len(q & fact.tokens) >= need for fact in self._facts):
                covered += 1
        return covered / len(self._sub_tokens)

    def consistency(self) -> float:
        """Share of facts supported by two or more distinct sources."""
        if not self._facts:
            return 0.0
        return sum(1 for f in self._facts if len(f.sources) >= 2) / len(self._facts)

    @staticmethod
    def _saturation(new_facts: int, total_facts: int) -> float:
        if total_facts <= 0:
            return 0.0
        return max(0.0, 1.0 - new_facts / total_facts)

    def confidence(self, new_facts: int) -> Dict[str, float]:
        cov, cons = self.coverage(), self.consistency()
        sat = self._saturation(new_facts, len(self._facts))
        value = WEIGHT_COVERAGE * cov + WEIGHT_CONSISTENCY * cons + WEIGHT_SATURATION * sat
        return {"coverage": round(cov, 3), "consistency": round(cons, 3), "saturation": round(sat, 3),
                "confidence": round(value, 3)}

    # -- rounds --------------------------------------------------------
    def add_round(self, round_num: int, findings: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        """Account for one round's findings and return its trace row."""
        new_facts = 0
        new_sources = 0
        pages = 0
        for f in findings or []:
            if not isinstance(f, Mapping):
                continue
            pages += 1
            url = str(f.get("url") or "")
            key = source_key(url) if url else ""
            if key and key not in self._sources:
                self._sources.add(key)
                new_sources += 1
            if url:
                self._urls.add(canonical_url(url))
            for fact in finding_facts(f):
                if self._register(fact, key):
                    new_facts += 1
        measures = self.confidence(new_facts)
        saturated_round = pages > 0 and new_facts < self.min_new_facts and new_sources == 0
        if pages > 0:
            self._streak = self._streak + 1 if saturated_round else 0
        row = {
            "round": round_num,
            "pages": pages,
            "new_facts": new_facts,
            "total_facts": len(self._facts),
            "new_sources": new_sources,
            "total_sources": len(self._sources),
            "saturated_round": saturated_round,
            "saturated_streak": self._streak,
            **measures,
        }
        self.rounds.append(row)
        return row

    def should_stop(self) -> Optional[Dict[str, Any]]:
        """The stop decision after the last ``add_round``: None to go on, or
        ``{"code", "reason", ...}`` naming why the run may stop."""
        if not self.rounds or len(self.rounds) < self.min_rounds:
            return None
        last = self.rounds[-1]
        if self.saturation_stop and last["pages"] > 0 and self._streak >= self.patience:
            return {
                "code": STOP_SATURATED,
                "reason": (f"the last {self._streak} round(s) each added fewer than {self.min_new_facts} new "
                           f"fact(s) and no new source (saturated)"),
                "new_facts": last["new_facts"], "new_sources": last["new_sources"],
            }
        if self.confidence_threshold > 0 and last["confidence"] >= self.confidence_threshold:
            return {
                "code": STOP_CONFIDENCE,
                "reason": (f"confidence {last['confidence']:.2f} reached the {self.confidence_threshold:.2f} target "
                           f"(coverage {last['coverage']:.2f}, consistency {last['consistency']:.2f}, "
                           f"saturation {last['saturation']:.2f})"),
                "confidence": last["confidence"],
            }
        return None

    @property
    def total_facts(self) -> int:
        return len(self._facts)

    @property
    def total_sources(self) -> int:
        return len(self._sources)
