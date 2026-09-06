"""
context_engine/transforms.py — making something fit without making it up.

`ranking.py` decides what deserves a slot.  This module decides what actually
goes in it once the slot turns out to be smaller than the thing.  There are
only four honest answers to "it does not fit", and the whole file is about
never reaching for a fifth:

1. put it in whole;
2. put in a literal, contiguous piece of it and say that is what you did;
3. put in a summary somebody else wrote, labelled as a summary;
4. put in a pointer and record, in the packet, that the content was left out.

`TRANSFORMATIONS` is ordered least-distorting first and this module may only
ever move an item *down* it, never up, and the resulting `ContextItem` records
where it stopped.  `reference` sits at index 0 and is not a rung: it invents
nothing at all, which is why it is above `verbatim` in fidelity, and it carries
no content, which is why it is the floor.  Answer 4 is therefore always
available and always costs an omission.

**Nothing here calls a model.**  Not a small one, not a local one, not "just
for the summary".  A generative step on the turn path is a second inference
with its own latency, its own failure mode and its own hallucinations, inside
the component whose entire job is to be the thing you can trust about what the
model was told.  `generated` exists as a *label* for a summary an adapter
computed elsewhere and handed over in `meta["summary"]`; this module attaches
the label, checks it fits, and never writes the prose.

**Protected sources never get summarised.**  `PROTECTED_SOURCE_TYPES` is the
list of things whose literal text is the material: a decision's wording, a
file's bytes, a symbol's signature, a state projection.  Summarising those
produces a sentence that is plausible, fits the budget, and is wrong — and it
is wrong in the way that is hardest to catch, because it reads like the source.
When one of them does not fit, it degrades to a reference and an omission with
`recoverable=True`: "I did not read this, and you can" beats a paraphrase that
sounds like it was read.

**`fit()` is deterministic.**  Same candidate, same budget, same estimator,
same query gives the same item byte for byte, `item_id` included — the id is a
fingerprint of what the item says, not a fresh uuid.  Branching Futures uses
this to prove two branches started from the same context, and a random id would
make every comparison fail for a reason that has nothing to do with context.
The consequence is deliberate: two items saying exactly the same thing about
the same revision of the same source share an id, because they are the same
item, and two of them cannot appear in one packet anyway — `ranking.dedupe()`
runs first and that is precisely what it removes.

One rung is deliberately absent from `fit()`'s ladder: `extractive`.  It is
lossier than an excerpt and cannot fit anywhere an excerpt cannot — both need
the same header plus at least one whole unit of text — so choosing it inside
`fit()` would always mean choosing more distortion for no extra room.  A caller
that wants sentence selection can build it explicitly with `to_item()`.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from src.contracts.base import fingerprint
from src.memory import tokenize

from .budgets import CONSERVATIVE_CHARS_PER_TOKEN, TokenEstimator
from .contracts import (
    TRANSFORMATIONS,
    ContextCandidate,
    ContextItem,
    ContextOmission,
)

logger = logging.getLogger(__name__)

#: Source types whose literal text is the material: they never go through a
#: generative summary, and never through a cut that lands mid-line.
PROTECTED_SOURCE_TYPES: Tuple[str, ...] = (
    "capability", "decision", "delta", "experience", "file", "instruction",
    "state", "symbol",
)

#: `experience` is in that list unconditionally, even though only an experience
#: carrying a verdict is strictly material.  Over-protecting costs a reference
#: and an honest omission; under-protecting costs a fabricated verdict, and the
#: reader of a packet cannot tell a fabricated verdict from a real one.

#: Binary search depth when shrinking an excerpt to a token budget.  A body is
#: capped at 1,000,000 chars by the contract, so 24 halvings is exhaustive.
_MAX_SEARCH_STEPS = 24

#: Below this, an excerpt has stopped being information.  A budget that leaves
#: room for "line 7:" out of a 500-character file produces something that fits,
#: costs tokens, says nothing, and reads as though the source was consulted.  A
#: reference of the same size says "I did not read this, and you can", which is
#: both shorter and true.  Relaxed for bodies that are themselves tiny: half of
#: a short source is still most of it.
MIN_EXCERPT_CHARS = 32

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_LAST_SPACE = re.compile(r"\s\S*$")


# ── measuring ──────────────────────────────────────────────────────────────

def _count(estimator: TokenEstimator, text: str) -> int:
    """Never raises.  An estimator that throws on the turn path would take the
    turn with it, so a failure buys a conservative number instead."""
    if not text:
        return 0
    try:
        return max(0, int(estimator.count(text)))
    except Exception:  # noqa: BLE001 - see above
        logger.debug("token estimator failed; falling back to the char heuristic")
        return int(math.ceil(len(text) / CONSERVATIVE_CHARS_PER_TOKEN))


def _header(candidate: ContextCandidate) -> str:
    """What the item costs before a word of its body is printed.

    Provenance is not free and pretending it is makes every budget optimistic
    by exactly the number of items in the packet."""
    return f"{candidate.title} [{candidate.source_ref}]".strip()


def _cost(estimator: TokenEstimator, candidate: ContextCandidate, body: str) -> int:
    return _count(estimator, _header(candidate)) + _count(estimator, body)


# ── building an item ───────────────────────────────────────────────────────

def _item_id(candidate: ContextCandidate, body: str, transformation: str) -> str:
    """A fingerprint of what the item says, so that two identical compilations
    produce two identical items.  See the module docstring on determinism."""
    digest = fingerprint([
        ("source_type", candidate.source_type),
        ("source_ref", candidate.source_ref),
        ("source_revision", candidate.source_revision),
        ("transformation", transformation),
        ("title", candidate.title),
        ("body", body),
    ])
    return f"ctxitem_{digest[:16]}"


def _build(candidate: ContextCandidate, body: str, transformation: str, *,
           estimator: TokenEstimator, reason: str = "") -> ContextItem:
    kind = transformation if transformation in TRANSFORMATIONS else "verbatim"
    text = body or ""
    return ContextItem(
        item_id=_item_id(candidate, text, kind),
        source_type=candidate.source_type,
        source_ref=candidate.source_ref,
        title=candidate.title[:512],
        body=text,
        transformation=kind,
        lanes=tuple(candidate.lanes),
        scores=dict(candidate.scores),
        trust_class=candidate.trust_class,
        authority=candidate.authority,
        source_revision=candidate.source_revision,
        observed_at=candidate.observed_at,
        chars=len(text),
        tokens=_cost(estimator, candidate, text),
        reason=(reason or "")[:512],
        degraded=candidate.degraded,
    )


def to_item(candidate: ContextCandidate, *, estimator: TokenEstimator,
            transformation: str = "verbatim", reason: str = "") -> ContextItem:
    """Turn a candidate into an item, labelled with how it was transformed.

    This labels; it does not transform.  `transformation="reference"` drops the
    body because a reference by definition has none; every other value keeps
    the candidate's text as it stands and records the caller's claim about it.
    `fit()` is the function that actually shortens anything."""
    kind = transformation if transformation in TRANSFORMATIONS else "verbatim"
    body = "" if kind == "reference" else (candidate.body or "")
    return _build(candidate, body, kind, estimator=estimator, reason=reason)


def reference_only(candidate: ContextCandidate, *,
                   estimator: TokenEstimator) -> ContextItem:
    """Title and provenance, no content.

    Still costs tokens — the pointer is printed — and still carries the
    revision, so the agent can open exactly the version that was considered."""
    return _build(candidate, "", "reference", estimator=estimator,
                  reason="content not included; open the source to read it")


# ── excerpting ─────────────────────────────────────────────────────────────

def _units(text: str) -> List[str]:
    """Text split into the pieces an excerpt is allowed to start and end on.

    Lines when the text has any, otherwise sentences.  The pieces concatenate
    back to the original exactly, which is what lets the result be called a
    literal slice rather than a reconstruction."""
    if "\n" in text:
        return text.splitlines(keepends=True)
    parts: List[str] = []
    last = 0
    for match in _SENTENCE_END.finditer(text):
        parts.append(text[last:match.end()])
        last = match.end()
    if last < len(text):
        parts.append(text[last:])
    return parts or [text]


def _query_tokens(query: str) -> frozenset:
    return frozenset(t for t in tokenize((query or "").lower()) if t)


def _window(units: Sequence[str], max_chars: int, query: str) -> str:
    """The contiguous run of whole units that fits and covers the most of the
    query.  Ties go to the longer run, then to the earlier one — the same input
    always yields the same excerpt.

    Coverage is counted in *distinct* query terms, not in term occurrences: a
    passage that repeats one word forty times is not a better answer than one
    that mentions three of the words asked about."""
    if not units or max_chars <= 0:
        return ""
    prefix = [0]
    for unit in units:
        prefix.append(prefix[-1] + len(unit))
    wanted = _query_tokens(query)
    per_unit: List[frozenset] = [
        frozenset(t for t in tokenize(unit.lower()) if t) & wanted if wanted
        else frozenset()
        for unit in units
    ]

    counts: Dict[str, int] = {}
    covered = 0
    best: Optional[Tuple[Tuple[int, int, int], int, int]] = None
    start = 0
    for end in range(1, len(units) + 1):
        for token in per_unit[end - 1]:
            counts[token] = counts.get(token, 0) + 1
            if counts[token] == 1:
                covered += 1
        while start < end and prefix[end] - prefix[start] > max_chars:
            for token in per_unit[start]:
                counts[token] -= 1
                if counts[token] == 0:
                    covered -= 1
            start += 1
        if start >= end:
            continue  # this unit alone is longer than the allowance
        key = (covered, prefix[end] - prefix[start], -start)
        if best is None or key > best[0]:
            best = (key, start, end)
    if best is None:
        return ""
    return "".join(units[best[1]:best[2]])


def _word_cut(text: str, max_chars: int) -> str:
    """A last-resort cut that lands on whitespace, or nothing at all.

    Rule 6: an excerpt that splits an identifier down the middle is worse than
    a shorter one.  `some_function_na` is not a shorter `some_function_name`,
    it is a different symbol that does not exist."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    if text[max_chars].isspace():
        return head.rstrip()
    match = _LAST_SPACE.search(head)
    if match is None:
        return ""  # one unbroken token: there is no honest cut
    return head[:match.start()].rstrip()


def _excerpt(text: str, max_chars: int, query: str, allow_partial_unit: bool) -> str:
    units = _units(text)
    window = _window(units, max_chars, query)
    if window or not allow_partial_unit:
        return window
    # Nothing whole fits.  Cut inside the unit that carries most of the query,
    # on a word boundary, rather than inside whichever unit happens to be first.
    wanted = _query_tokens(query)
    if wanted:
        ordered = sorted(
            range(len(units)),
            key=lambda n: (-len(frozenset(t for t in tokenize(units[n].lower()) if t)
                               & wanted), n),
        )
    else:
        ordered = list(range(len(units)))
    for index in ordered[:8]:
        cut = _word_cut(units[index], max_chars)
        if cut.strip():
            return cut
    return ""


def excerpt(text: str, *, max_chars: int, query: str = "") -> str:
    """A literal slice of `text`, at most `max_chars` long, cut on boundaries.

    Whole lines when the text has lines, whole sentences otherwise, preferring
    the region with the most lexical overlap with `query`.  Only when no single
    whole unit fits does it fall back to a cut on a word boundary, and if even
    that would split a token it returns nothing: a caller that gets `""` is
    being told to use a reference, which is honest, instead of a fragment that
    reads like text the source does not contain."""
    body = text or ""
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        return ""
    if limit <= 0 or not body.strip():
        return ""
    if len(body) <= limit:
        return body
    return _excerpt(body, limit, query, allow_partial_unit=True)


def _largest_fitting_excerpt(candidate: ContextCandidate, body: str, budget: int,
                             estimator: TokenEstimator, query: str,
                             allow_partial_unit: bool) -> str:
    """The longest excerpt whose measured cost still fits the budget.

    Binary search over the character allowance rather than arithmetic on a
    chars-per-token constant: the estimator may be a real tokenizer, and
    dividing by 3.0 to guess what it will say is how a packet ends up 4% over
    the window with no way to find out until the provider rejects it."""
    low, high = 1, len(body)
    best = ""
    for _ in range(_MAX_SEARCH_STEPS):
        if low > high:
            break
        middle = (low + high) // 2
        trial = _excerpt(body, middle, query, allow_partial_unit)
        if trial and _cost(estimator, candidate, trial) <= budget:
            best = trial
            low = middle + 1
        else:
            high = middle - 1
    return best


# ── fitting ────────────────────────────────────────────────────────────────

def _omission(candidate: ContextCandidate, detail: str, *,
              recoverable: bool = True) -> ContextOmission:
    return ContextOmission(
        source_type=candidate.source_type,
        source_ref=candidate.source_ref,
        reason="budget",
        score=0.0,
        recoverable=recoverable,
        detail=detail[:512],
    )


def fit(candidate: ContextCandidate, *, budget_tokens: int,
        estimator: TokenEstimator, query: str = "",
        allow_generative: bool = False, reason: str = ""
        ) -> Tuple[Optional[ContextItem], Optional[ContextOmission]]:
    """The most faithful form of this candidate that fits `budget_tokens`.

    Returns `(item, omission)`.  An omission accompanies the item exactly when
    content was left out of the packet: a reference carries one, a verbatim
    item never does, and an excerpt does not either — the shortening is
    recorded in the item's own `transformation` and `reason`, and inventing an
    omission for something that *is* present would break the packet's one
    absence, one omission accounting.

    `(None, omission)` means not even a pointer fits, which happens when the
    section's budget has already been spent."""
    body = candidate.body or ""
    protected = candidate.source_type in PROTECTED_SOURCE_TYPES
    try:
        budget = max(int(budget_tokens or 0), 0)
    except (TypeError, ValueError):
        budget = 0

    if budget > 0 and _cost(estimator, candidate, body) <= budget:
        return _build(candidate, body, "verbatim", estimator=estimator,
                      reason=reason), None

    if budget > 0 and body.strip():
        # Protected types may only be cut on whole-line boundaries; everything
        # else may fall back to a word boundary inside one long line.
        slice_ = _largest_fitting_excerpt(candidate, body, budget, estimator,
                                          query, allow_partial_unit=not protected)
        if len(slice_) < min(MIN_EXCERPT_CHARS, len(body) // 2):
            slice_ = ""  # too small to be information; see MIN_EXCERPT_CHARS
        if slice_:
            return _build(
                candidate, slice_, "excerpt", estimator=estimator,
                reason=reason or (f"excerpt of {len(body)} chars, cut to fit "
                                  f"{budget} tokens"),
            ), None

    if budget > 0 and allow_generative and not protected:
        summary = str((candidate.meta or {}).get("summary") or "").strip()
        if summary and _cost(estimator, candidate, summary) <= budget:
            return _build(
                candidate, summary, "generated", estimator=estimator,
                reason=reason or "summary supplied by the source, not the original text",
            ), None

    pointer = reference_only(candidate, estimator=estimator)
    detail = (f"{'protected source type; ' if protected else ''}"
              f"{_cost(estimator, candidate, body)} tokens do not fit in {budget}")
    if budget <= 0 or pointer.tokens > budget:
        return None, _omission(candidate, f"{detail}; not even a reference fits")
    return pointer, _omission(candidate, detail)


def fit_all(candidates_with_budget: Sequence[Tuple[ContextCandidate, int]], *,
            estimator: TokenEstimator, query: str = ""
            ) -> Tuple[List[ContextItem], List[ContextOmission]]:
    """`fit()` over a priced list, keeping the caller's order.

    Each entry is `(candidate, budget_tokens)`: the budget is per item because
    by the time this runs the section split has already happened, and a single
    shared budget here would silently let the first long document eat the
    section that was allocated to five short ones."""
    items: List[ContextItem] = []
    omissions: List[ContextOmission] = []
    for entry in (candidates_with_budget or ()):
        try:
            candidate, budget = entry
        except (TypeError, ValueError):
            logger.debug("fit_all skipped a malformed (candidate, budget) entry")
            continue
        if not isinstance(candidate, ContextCandidate):
            continue
        item, omission = fit(candidate, budget_tokens=budget, estimator=estimator,
                             query=query)
        if item is not None:
            items.append(item)
        if omission is not None:
            omissions.append(omission)
    return items, omissions


__all__ = [
    "PROTECTED_SOURCE_TYPES", "MIN_EXCERPT_CHARS",
    "to_item", "reference_only", "excerpt", "fit", "fit_all",
]
