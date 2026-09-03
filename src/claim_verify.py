"""Claim verification — five layers, cheap before expensive (FAUSTUS).

The question this answers is narrow and useful: *the model just asserted X from
this document — is X actually in the document?* Answering it with a model call
is the expensive way and the least trustworthy one, so four deterministic
layers run first and a model is only ever asked what none of them could settle.

The ladder
----------
1. **exact substring** — the claim occurs verbatim in the source.
2. **normalised substring** — it occurs after folding case, accents,
   punctuation and whitespace.
3. **token overlap** — enough of the claim's content words occur in the source.
   Layer 3 never overrules layer 4: a claim whose figures or names are missing
   from the source is not allowed to be carried over the threshold by its other
   words, which is precisely how an invented number slips past a bag-of-words
   check.
4. **numbers and names** — every number and every capitalised name in the claim
   must occur in the source. This is the layer that catches a fabricated
   figure, and it is the only layer that can settle a claim *against* the
   model: when a figure or a name is missing, the answer is "not supported"
   with the offending terms named in ``unsupported_terms``. When nothing is
   missing, layer 4 settles nothing — everything it checked being present is
   not evidence that the sentence built from them is true.
5. **model judgement** — "is this explicitly stated or logically derivable from
   the source?", asked of an injected ``judge`` callable and **only** if one is
   injected. Its verdict is labelled a model judgement and is NEVER merged into
   the deterministic confidence: ``confidence`` stays the deterministic score
   (0.0, because nothing deterministic settled it) and the model's own number
   lives in ``judgement.confidence``. Without a judge, no result ever claims
   layer 5.

How this relates to ``src/expert_review.py``
--------------------------------------------
``expert_review._supports`` is the same ladder at three rungs, for a narrower
question — *may this correction present itself as coming from the corpus?* —
and it deliberately stops before the expensive ones: its layer "literal" is
layers 1–2 here, its "fuzzy" is layer 3, and it has no layers 4 and 5 because
a rubric rationale rarely carries figures and because that module may never
call a model. Both modules share the same honesty rule and say it the same
way: a verdict that came from a model is *labelled* as one and shown, never
laundered into the deterministic score. ``LABEL_MODEL`` here plays the role
``expert_review.LABEL_OPINION`` plays there, and renderers should print it
verbatim rather than deciding for themselves what ``supported`` means.

``verify`` is deterministic for layers 1–4, pure stdlib, and never raises: junk
in (``None``, bytes, a number, a megabyte of noise) yields an unsupported
result with a reason, not an exception. A ``judge`` that raises is caught and
reported in ``judge_error``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

LABEL_DETERMINISTIC = "deterministic"
LABEL_MODEL = "model judgement, not deterministic evidence"

# Layer 3 fires when at least this share of the claim's content words is in the
# source. High on purpose: layer 3 asserts support, and a loose threshold here
# would let a paraphrase that reverses the meaning through.
TOKEN_RATIO = 0.75

CONFIDENCE = {1: 1.0, 2: 0.9, 4: 0.8}
MIN_TOKEN_LEN = 3
MAX_TEXT_CHARS = 2_000_000

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_NUMBER_RE = re.compile(r"(?<![\w.])[+-]?\d[\d.,]*\s?%?")
_TOKEN_RE = re.compile(r"\d[\d.,]*|[^\W\d_]+", re.UNICODE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Function words carry no evidence; keeping them would let "the of and in"
# carry a claim over the layer-3 threshold.
_STOPWORDS = frozenset("""
the a an and or but if then than that this these those of in on at to for from by with
as is are was were be been being it its his her their our your my not no nor so such
there here when while which who whom whose what how why can could may might must shall
should will would do does did done have has had about into over under again further
el la los las un una unos unas y o pero si entonces que este esta estos estas de en
para por con como es son era eran ser sido su sus mi tu nuestro vuestro no ni hay
cuando mientras cual quien cuyo como porque puede podria debe sera seria tiene tienen
""".split())


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Anything at all into a bounded string. Never raises."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    try:
        out = value if isinstance(value, str) else str(value)
    except Exception:  # noqa: BLE001
        return ""
    return out[:MAX_TEXT_CHARS]


def _fold(text: str) -> str:
    """Case- and accent-folded, so 'Café' and 'cafe' are the same word."""
    try:
        decomposed = unicodedata.normalize("NFD", text)
    except (TypeError, ValueError):
        return text.casefold()
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def normalise(text: Any) -> str:
    """Folded, punctuation dropped, whitespace collapsed — layer 2's view."""
    folded = _fold(_text(text))
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", folded)).strip()


def content_tokens(text: Any) -> List[str]:
    """Words and numbers worth matching on: folded, long enough, not filler."""
    out: List[str] = []
    for token in _TOKEN_RE.findall(_fold(_text(text))):
        if token[0].isdigit():
            out.append(token)
        elif len(token) >= MIN_TOKEN_LEN and token not in _STOPWORDS:
            out.append(token)
    return out


# ---------------------------------------------------------------------------
# Layer 4's two vocabularies
# ---------------------------------------------------------------------------


def _number_forms(raw: str) -> Set[str]:
    """Every spelling of one number we are willing to call the same number.

    Deliberately generous — layer 4 only ever REFUTES, so a spelling we fail to
    recognise as equal would produce a false accusation, which is the expensive
    error here. ``1,234.5``, ``1234.5`` and ``1.234,5`` all reduce together.
    """
    token = raw.strip().rstrip("%").strip()
    forms = {token, token.replace(" ", "")}
    stripped = token.replace(",", "").replace(" ", "")
    forms.add(stripped)
    forms.add(token.replace(".", "").replace(",", "."))
    for candidate in list(forms):
        try:
            forms.add(f"{float(candidate):g}")
        except (TypeError, ValueError):
            continue
    return {f for f in forms if f}


def numbers_in(text: Any) -> List[str]:
    """The numeric literals of a text, in order, as they were written."""
    return [m.group(0).strip() for m in _NUMBER_RE.finditer(_text(text)) if m.group(0).strip()]


def names_in(text: Any) -> List[str]:
    """Capitalised words — the cheap stand-in for named entities.

    A word that starts a sentence is capitalised too, but the check is done
    case-insensitively against the source, so a sentence-initial ordinary word
    costs nothing: it only has to occur in the source *at all*.
    """
    out: List[str] = []
    for match in _WORD_RE.finditer(_text(text)):
        word = match.group(0)
        if len(word) < 2 or not word[0].isupper():
            continue
        folded = _fold(word)
        if folded in _STOPWORDS or folded in out:
            continue
        out.append(folded)
    return out


def _missing_terms(claim: str, source: str) -> Tuple[List[str], List[str]]:
    """``(missing_numbers, missing_names)`` of the claim against the source."""
    source_number_forms: Set[str] = set()
    for raw in numbers_in(source):
        source_number_forms |= _number_forms(raw)
    source_tokens = set(content_tokens(source))
    folded_source = _fold(_text(source))

    missing_numbers: List[str] = []
    for raw in numbers_in(claim):
        if not (_number_forms(raw) & source_number_forms):
            if raw not in missing_numbers:
                missing_numbers.append(raw)

    missing_names: List[str] = []
    for name in names_in(claim):
        if name in source_tokens or name in folded_source:
            continue
        if name not in missing_names:
            missing_names.append(name)
    return missing_numbers, missing_names


# ---------------------------------------------------------------------------
# Layer 5 — the only rung that talks to a model
# ---------------------------------------------------------------------------

JUDGE_QUESTION = ("Is this claim explicitly stated in, or logically derivable "
                  "from, the source below? Answer yes or no and say why in one "
                  "sentence.")


def judge_prompt(claim: Any, source: Any, *, source_chars: int = 6000) -> str:
    """The exact question layer 5 asks, so every caller asks the same one."""
    return (f"{JUDGE_QUESTION}\n\nCLAIM:\n{_text(claim)}\n\n"
            f"SOURCE:\n{_text(source)[:max(0, int(source_chars))]}")


def _clamp01(value: Any, default: float = 0.5) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return max(0.0, min(1.0, num))


def _read_judgement(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalise whatever the judge returned. ``None`` = it said nothing."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return {"supported": raw, "confidence": 0.5, "why": ""}
    if isinstance(raw, dict):
        value = raw.get("supported")
        if value is None:
            value = raw.get("verdict")
        if isinstance(value, str):
            supported = value.strip().lower() in ("yes", "true", "supported", "y", "si", "sí")
        elif value is None:
            return None
        else:
            supported = bool(value)
        return {
            "supported": supported,
            "confidence": _clamp01(raw.get("confidence"), 0.5),
            "why": str(raw.get("why") or raw.get("reason") or "")[:500],
        }
    if isinstance(raw, str):
        head = raw.strip().lower()
        if not head:
            return None
        supported = head.startswith(("yes", "y ", "true", "supported", "si", "sí"))
        return {"supported": supported, "confidence": 0.5, "why": raw.strip()[:500]}
    return None


def _call_judge(judge: Callable, claim: str, source: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        try:
            raw = judge(claim, source)
        except TypeError:
            raw = judge(claim=claim, source=source)
    except Exception as exc:  # noqa: BLE001 - a judge is optional, never fatal
        logger.debug("claim verify: judge failed: %s", exc)
        return None, f"{type(exc).__name__}: {exc}"[:300]
    return _read_judgement(raw), ""


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def _result(*, supported: bool, layer: Optional[int], confidence: float, why: str,
            unsupported_terms: Optional[Sequence[str]] = None,
            judgement: Optional[Dict[str, Any]] = None,
            judge_error: str = "") -> Dict[str, Any]:
    model = judgement is not None
    deterministic = {
        "supported": False if model else supported,
        "layer": None if model else layer,
        "confidence": 0.0 if model else round(float(confidence), 4),
    }
    return {
        "supported": bool(supported),
        "layer": layer,
        # Always the DETERMINISTIC confidence. A model judgement contributes
        # nothing to it, by design; its own number is in judgement.confidence.
        "confidence": deterministic["confidence"],
        "why": why,
        "unsupported_terms": list(unsupported_terms or []),
        "label": LABEL_MODEL if model else LABEL_DETERMINISTIC,
        "model_judgement": model,
        "judgement": judgement,
        "judge_error": judge_error,
        "deterministic": deterministic,
    }


def verify(claim: Any, source: Any, *, judge: Optional[Callable] = None) -> Dict[str, Any]:
    """Is ``claim`` supported by ``source``? Cheap layers first.

    Returns ``{"supported", "layer", "confidence", "why", "unsupported_terms",
    "label", "model_judgement", "judgement", "judge_error", "deterministic"}``.

    ``layer`` is 1..4 when a deterministic layer settled it, 5 when only the
    injected ``judge`` did, and ``None`` when nothing settled it — which,
    without a judge, is what a claim that is merely *plausible* gets. Never
    raises.
    """
    claim_text = _text(claim)
    source_text = _text(source)

    if not claim_text.strip():
        return _result(supported=False, layer=None, confidence=0.0,
                       why="the claim is empty")
    if not source_text.strip():
        return _result(supported=False, layer=None, confidence=0.0,
                       why="the source is empty, so nothing can support the claim",
                       unsupported_terms=sorted(set(content_tokens(claim_text)))[:20])

    claim_tokens = content_tokens(claim_text)
    if not claim_tokens:
        return _result(supported=False, layer=None, confidence=0.0,
                       why="the claim carries no content words to check")

    # Layer 1 — exact substring.
    needle = claim_text.strip()
    if needle in source_text:
        return _result(supported=True, layer=1, confidence=CONFIDENCE[1],
                       why="the claim appears in the source verbatim")

    # Layer 2 — normalised substring.
    norm_claim, norm_source = normalise(claim_text), normalise(source_text)
    if norm_claim and norm_claim in norm_source:
        return _result(supported=True, layer=2, confidence=CONFIDENCE[2],
                       why="the claim appears in the source once case, accents "
                           "and punctuation are folded")

    # Layer 4's vocabulary check is computed here, before layer 3 may answer,
    # because layer 3 must never overrule it. A claim with one fabricated
    # figure among a dozen borrowed words scores far above any bag-of-words
    # threshold — which is exactly how an invented number gets past a fuzzy
    # matcher. So the figures and the names are checked first, and only a claim
    # that survives them is allowed to be carried by its other words.
    missing_numbers, missing_names = _missing_terms(claim_text, source_text)
    missing_vocabulary = bool(missing_numbers or missing_names)

    # Layer 3 — token overlap.
    if not missing_vocabulary:
        source_tokens = set(content_tokens(source_text))
        present = [t for t in claim_tokens if t in source_tokens]
        ratio = len(present) / float(len(claim_tokens))
        if ratio >= TOKEN_RATIO:
            return _result(
                supported=True, layer=3, confidence=round(0.45 + 0.3 * ratio, 4),
                why=f"{len(present)} of the claim's {len(claim_tokens)} content words "
                    f"occur in the source ({ratio:.0%}), and every figure and name "
                    f"in it occurs there too")

    # Layer 4 — numbers and names. The only layer that settles a claim against
    # the model, and the only one that can catch an invented figure.
    if missing_vocabulary:
        bits = []
        if missing_numbers:
            bits.append("figure" + ("s" if len(missing_numbers) > 1 else "")
                        + " " + ", ".join(missing_numbers))
        if missing_names:
            bits.append("name" + ("s" if len(missing_names) > 1 else "")
                        + " " + ", ".join(missing_names))
        return _result(
            supported=False, layer=4, confidence=CONFIDENCE[4],
            why="the source does not contain " + " or ".join(bits),
            unsupported_terms=missing_numbers + missing_names)

    # Layer 5 — a model, and only if one was handed to us.
    missing_words = sorted({t for t in claim_tokens
                            if t not in set(content_tokens(source_text))})
    if judge is not None:
        judgement, error = _call_judge(judge, claim_text, source_text)
        if judgement is not None:
            judgement = dict(judgement)
            judgement["source"] = "model"
            judgement["question"] = JUDGE_QUESTION
            return _result(
                supported=bool(judgement["supported"]), layer=5, confidence=0.0,
                why=("no deterministic layer could settle this; the verdict is a "
                     "model judgement and is not part of the deterministic score"),
                unsupported_terms=missing_words[:20],
                judgement=judgement, judge_error=error)
        return _result(
            supported=False, layer=None, confidence=0.0,
            why=("no deterministic layer could settle this and the judge returned "
                 "no usable verdict"),
            unsupported_terms=missing_words[:20], judge_error=error)

    return _result(
        supported=False, layer=None, confidence=0.0,
        why=("every figure and name in the claim occurs in the source, but no "
             "deterministic layer could settle the claim itself and no judge "
             "was supplied"),
        unsupported_terms=missing_words[:20])


def verify_claims(claims: Any, source: Any, *,
                  judge: Optional[Callable] = None) -> List[Dict[str, Any]]:
    """:func:`verify` over a list, keeping each claim beside its verdict."""
    out: List[Dict[str, Any]] = []
    try:
        items = list(claims) if not isinstance(claims, (str, bytes)) else [claims]
    except TypeError:
        items = [claims]
    for item in items:
        row = verify(item, source, judge=judge)
        row["claim"] = _text(item)[:1000]
        out.append(row)
    return out
