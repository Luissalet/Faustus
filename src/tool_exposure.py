"""How a tool is exposed to the model: direct, deferred or code-only (H17).

`Exposure` is a field of the tool's descriptor (`src.tool_authority`). This
module is where the rest of the runtime reads it:

* DIRECT    the schema goes out natively whenever the tool is selected.
* DEFERRED  the tool is selected and executable, but unless the request gives
            lexical evidence for it, it is listed as a one-line catalog entry
            and its schema loads through `lookup_tools` (`demote`).
* CODE_ONLY never a native schema, never listed in the catalog, never
            proposed by discovery, refused when a model step calls it directly
            (`src.step_snapshot.authorize_call`); a Code Mode program reaches
            it through the bridge.

The evidence rule for a DEFERRED tool is lexical and deliberately cheap. A
word of the request is evidence for the tool when it shares its first five
letters with a word of the tool's own name or with a word that appears in
that tool's natural-language examples and in at most `RARE_DOCUMENT_FREQUENCY`
tools' examples (a word every tool's examples use, such as "project", says
nothing). Five letters absorb the plural and verb endings of both languages
("pendientes"/"pendiente", "apunta"/"apuntar").

Nothing here removes a DEFERRED tool from the executable set; it only decides
which selected tools carry a full schema this round.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import AbstractSet, Dict, FrozenSet, Iterable, Mapping, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: A word that appears in the examples of more tools than this is too common
#: to be evidence for any one of them. Measured on the tool-index benchmark
#: (`tests/test_tool_exposure.py`): 2 lost strict accuracy, 3 and 4 kept it,
#: 3 saves the most of the two that kept it.
RARE_DOCUMENT_FREQUENCY = 3
STEM_LENGTH = 5
MIN_WORD_LENGTH = 4

_WORD = re.compile(r"[a-z0-9]{%d,}" % MIN_WORD_LENGTH)
_cache: Dict[str, object] = {}


def enabled() -> bool:
    try:
        from src.chat_mode import get_setting  # pinned per chat while its turn runs
        return bool(get_setting("agent_tool_exposure", True))
    except Exception:  # noqa: BLE001 - an unreadable setting keeps the legacy offer
        return False


def _fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(text).lower()) if not unicodedata.combining(ch))


def words(text: str) -> Set[str]:
    return set(_WORD.findall(_fold(text)))


def stems(text: str) -> FrozenSet[str]:
    return frozenset(w[:STEM_LENGTH] for w in words(text))


def reset() -> None:
    """Forget the cached example statistics (tests that swap EXAMPLES)."""
    _cache.clear()


def _examples() -> Mapping[str, Iterable[str]]:
    try:
        from src.tool_index_examples import EXAMPLES
        return EXAMPLES
    except Exception:  # noqa: BLE001
        return {}


def _document_frequency() -> Mapping[str, int]:
    found = _cache.get("df")
    if found is None:
        counts: Dict[str, int] = {}
        for phrases in _examples().values():
            tool_words: Set[str] = set()
            for phrase in phrases or ():
                tool_words |= words(phrase)
            for word in tool_words:
                counts[word] = counts.get(word, 0) + 1
        _cache["df"] = found = counts
    return found  # type: ignore[return-value]


def trigger_stems(name: str) -> FrozenSet[str]:
    """The stems that count as evidence of a request for `name`."""
    per_tool: Dict[str, FrozenSet[str]] = _cache.setdefault("triggers", {})  # type: ignore[assignment]
    found = per_tool.get(name)
    if found is None:
        frequency = _document_frequency()
        collected: Set[str] = {part[:STEM_LENGTH] for part in name.split("_") if len(part) >= MIN_WORD_LENGTH}
        for phrase in _examples().get(name, ()) or ():
            collected |= {w[:STEM_LENGTH] for w in words(phrase) if frequency.get(w, 0) <= RARE_DOCUMENT_FREQUENCY}
        per_tool[name] = found = frozenset(collected)
    return found


def has_evidence(name: str, query: str) -> bool:
    """Does the request give lexical evidence that it wants `name`?"""
    return bool(trigger_stems(name) & stems(query or ""))


def code_only_names(names: Optional[Iterable[str]] = None) -> Set[str]:
    """The CODE_ONLY tools among `names` (all registered ones when omitted)."""
    from src.tool_authority import AUTHORITY, Exposure
    if names is None:
        return {n for n, e in AUTHORITY.exposure_map().items() if e is Exposure.CODE_ONLY}
    return {str(n) for n in names if AUTHORITY.exposure(str(n)) is Exposure.CODE_ONLY}


def demote(seed: AbstractSet[str], query: str, *, candidates: Optional[AbstractSet[str]] = None,
           keep: Iterable[str] = ()) -> Tuple[Set[str], Set[str]]:
    """Split a hot seed into (seed that stays native, tools demoted to the catalog).

    Only a DEFERRED tool is demoted, only when `candidates` (the tools
    retrieval alone picked) contains it, never when `keep` names it (forced,
    caller-pinned) and never when the request gives evidence for it. A
    CODE_ONLY tool is in neither set: it carries no native schema and is not
    listed either (`code_only_names` says which they are).
    """
    from src.tool_authority import AUTHORITY, Exposure
    pinned = {str(n) for n in keep if n}
    exposure = AUTHORITY.exposure_map()
    stay: Set[str] = set()
    demoted: Set[str] = set()
    for name in seed:
        kind = exposure.get(name)
        if kind is Exposure.CODE_ONLY:
            continue
        if (kind is Exposure.DEFERRED and name not in pinned
                and (candidates is None or name in candidates) and not has_evidence(name, query)):
            demoted.add(name)
        else:
            stay.add(name)
    return stay, demoted
