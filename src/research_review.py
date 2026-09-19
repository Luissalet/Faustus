"""src/research_review.py — blind review of a finished deep-research report.

A model that writes a research report and then grades its own work is a
biased grader: it knows what it meant to say, what it searched for and
skipped, and how confident it already told itself it was. `blind_review`
asks a SEPARATE pass to score the report with none of that context -- only
the original question, the finished report text (with process/self-
assessment content stripped by `strip_process`) and its list of sources
(with citation verdicts, when the caller has them from
`src.research_citations`). It never sees the plan, the search trace, or
which model wrote the report.

Writer self-score
------------------
`src.deep_research.DeepResearcher` does not compute a numeric confidence or
run any self-critique pass over its own report. The closest thing it
computes is `self.evidence_quality` (`assess_evidence_quality`, RES-06): a
heuristic grade -- "none" / "low" / "medium" / "high" -- over the gathered
findings (source count, domain quality, date coverage), set once findings
are final and before the report is written. `normalize_writer_self_score`
maps that grade onto the same 0..1 scale as the reviewer's `overall` score
so `calibration_gap` can compare them; when `evidence_quality` is missing or
unrecognised the gap is `None` rather than a made-up number.

Never raises
------------
Every public function here returns a value on any input; `blind_review`
specifically never lets a network error, a timeout or a garbled model
response escape as an exception -- it goes into the result's `error` field
instead, because a blind review must never be allowed to fail the research
run it is reviewing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: No setting for this -- deliberately fixed, so a blind review can never be
#: configured into blocking the research run it is attached to.
DEFAULT_TIMEOUT_S = 120

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


# ---------------------------------------------------------------------------
# strip_process
# ---------------------------------------------------------------------------

# Full heading text (after stripping leading #'s and trailing punctuation,
# case-insensitive) that marks a markdown section as process/self-assessment
# meta rather than research content. Matched WHOLE, never as a substring --
# "Confidence Intervals in Clinical Trials" is a real subject-matter heading
# and must survive; "Confidence" on its own is the writer grading itself and
# must not reach the reviewer. Bilingual because reports are written in
# either language (see `report_language` in src/deep_research.py).
PROCESS_HEADINGS: Tuple[str, ...] = (
    "confidence",
    "confidence level",
    "confidence assessment",
    "self-assessment",
    "self assessment",
    "self-evaluation",
    "self evaluation",
    "methodology",
    "methodology notes",
    "process notes",
    "research process",
    "limitations of this run",
    "limitations of this research",
    "verification summary",
    "quality assessment",
    "quality self-assessment",
    "about this report",
    "how this report was made",
    "about this research",
    # Spanish equivalents.
    "confianza",
    "nivel de confianza",
    "autoevaluación",
    "autoevaluacion",
    "metodología",
    "metodologia",
    "notas del proceso",
    "proceso de investigación",
    "proceso de investigacion",
    "limitaciones de esta ejecución",
    "limitaciones de esta ejecucion",
    "resumen de verificación",
    "resumen de verificacion",
    "evaluación de calidad",
    "evaluacion de calidad",
    "sobre este informe",
    "cómo se elaboró este informe",
    "como se elaboro este informe",
)
_PROCESS_HEADING_SET = frozenset(h.lower() for h in PROCESS_HEADINGS)

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]*(.+?)[ \t]*$", re.M)

# A self-referential phrase and the rest of the sentence it opens (up to the
# next sentence-ending punctuation, never crossing a newline) -- removed
# wherever it occurs, not only inside a dedicated section, without taking
# the rest of the paragraph's real content down with it.
_SELF_REFERENCE_RE = re.compile(
    r"(?i)\b(as an ai(?: language model)?|as a large language model|"
    r"como (?:una )?ia\b|como modelo de lenguaje)\b[^.!?\n]*[.!?]\s*"
)


def _heading_key(text: str) -> str:
    return re.sub(r"[:\-–—\s]+$", "", text.strip()).strip().lower()


def strip_process(report_text: str) -> str:
    """Remove process/self-assessment sections and self-referential asides
    from a finished report, leaving the substantive research content a
    blind reviewer should see.

    Conservative and table-driven: a markdown section (from its heading to
    the next heading of equal-or-shallower level, or the end of the text)
    is removed only when its heading text exactly matches an entry in
    `PROCESS_HEADINGS` (case-insensitive, whole heading -- never a substring
    match). Everything else, including headings that merely mention these
    words as part of a longer subject-matter title, is left untouched.
    """
    text = str(report_text or "")
    if not text.strip():
        return text

    matches = list(_HEADING_RE.finditer(text))
    spans_to_remove: List[Tuple[int, int]] = []
    for i, m in enumerate(matches):
        if _heading_key(m.group(2)) not in _PROCESS_HEADING_SET:
            continue
        level = len(m.group(1))
        start = m.start()
        end = len(text)
        for nxt in matches[i + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        spans_to_remove.append((start, end))

    for start, end in reversed(spans_to_remove):
        text = text[:start] + text[end:]

    text = _SELF_REFERENCE_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# ---------------------------------------------------------------------------
# Writer self-score normalisation
# ---------------------------------------------------------------------------

#: `assess_evidence_quality`'s grade evenly spread across 0..1 -- "none"
#: never reads as any nonzero confidence, "high" is the top of the same
#: scale the reviewer's overall score is normalised onto.
_GRADE_TO_SCORE: Dict[str, float] = {"none": 0.0, "low": 1.0 / 3, "medium": 2.0 / 3, "high": 1.0}


def normalize_writer_self_score(evidence_quality: Optional[Dict[str, Any]]) -> Optional[float]:
    """0..1 normalisation of a `DeepResearcher.evidence_quality` dict (see
    module docstring for why this is the writer's closest thing to a self-
    score). `None` when there is nothing usable to normalise -- never a
    guessed default."""
    if not isinstance(evidence_quality, dict):
        return None
    grade = str(evidence_quality.get("grade") or "").strip().lower()
    return _GRADE_TO_SCORE.get(grade)


# ---------------------------------------------------------------------------
# The reviewer call
# ---------------------------------------------------------------------------

_SCORE_KEYS: Tuple[str, ...] = (
    "answers_question", "evidence_support", "source_quality",
    "internal_consistency", "completeness",
)

#: Constrained-decoding schema (see src/auto_review.py's REVIEW_SCHEMA for
#: the same pattern): only honoured on a native Ollama endpoint
#: (src/llm_core.py's `_resolve_response_schema`) -- every other provider
#: drops it silently and `_parse_review` below does the parsing either way.
REVIEWER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {
                "answers_question": {"type": "integer"},
                "evidence_support": {"type": "integer"},
                "source_quality": {"type": "integer"},
                "internal_consistency": {"type": "integer"},
                "completeness": {"type": "integer"},
            },
            "required": list(_SCORE_KEYS),
        },
        "overall": {"type": "integer"},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "overall", "weaknesses", "unsupported_claims"],
}


def _format_sources(sources: Optional[Sequence[Any]], limit: int = 40) -> str:
    items = list(sources or [])[:limit]
    if not items:
        return "(no sources listed)"
    lines: List[str] = []
    for s in items:
        if isinstance(s, dict):
            title = str(s.get("title") or "").strip()
            url = str(s.get("url") or "").strip()
            verdict = str(s.get("verdict") or "").strip()
            bit = f"- {title or url or '(untitled)'}"
            if url and title:
                bit += f" — {url}"
            if verdict:
                bit += f" [{verdict}]"
        else:
            bit = f"- {s}"
        lines.append(bit)
    return "\n".join(lines)


def build_reviewer_prompt(question: str, report_text: str, sources: Optional[Sequence[Any]]) -> str:
    """The exact prompt sent to the reviewer. A standalone function so a
    test can assert what it does and does not contain (no process/self-
    assessment text, no writer model name) without a network call.
    """
    stripped = strip_process(report_text)
    return (
        "You are an independent reviewer grading a finished research report. You were NOT "
        "involved in producing it: you do not know what plan or process was used, what was "
        "searched, how many rounds ran, or which model wrote it. Judge only what is in front of "
        "you -- the question, the report text and its list of sources -- exactly as a reader with "
        "no other context would.\n\n"
        f"<question>\n{(question or '').strip()[:2000]}\n</question>\n\n"
        f"<report>\n{stripped[:16000]}\n</report>\n\n"
        f"<sources>\n{_format_sources(sources)}\n</sources>\n\n"
        "Score the report 1 (poor) to 5 (excellent) on each of:\n"
        "- answers_question: does it actually answer what was asked\n"
        "- evidence_support: are its claims backed by the sources it cites\n"
        "- source_quality: are the sources credible and relevant to the question\n"
        "- internal_consistency: does the report avoid contradicting itself\n"
        "- completeness: does it cover the question's scope\n"
        "Then give one overall score 1-5, the top 3 weaknesses as short phrases, and a list of any "
        "specific claims in the report that look unsupported by its own cited sources.\n\n"
        "Answer with ONLY a JSON object, no prose before or after:\n"
        '{"scores": {"answers_question": <1-5>, "evidence_support": <1-5>, '
        '"source_quality": <1-5>, "internal_consistency": <1-5>, "completeness": <1-5>}, '
        '"overall": <1-5>, "weaknesses": ["<short phrase>", ...], '
        '"unsupported_claims": ["<claim>", ...]}'
    )


def _clamp_score(value: Any, default: int = 3) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(1, min(5, n))


def _parse_review(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the reviewer's answer into `{scores, overall, weaknesses,
    unsupported_claims}`, tolerating a `<think>` preamble, a fenced code
    block, and trailing commas -- the same lenient recovery
    `src/auto_review.py::_parse` uses for the same class of model output.
    Returns `None` (never raises) when nothing usable can be recovered.
    """
    text = _THINK_RE.sub("", raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M)
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    blob = m.group(0)
    try:
        data = json.loads(blob)
    except ValueError:
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", blob))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None

    raw_scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
    scores = {k: _clamp_score(raw_scores.get(k)) for k in _SCORE_KEYS}
    default_overall = round(sum(scores.values()) / len(scores)) if scores else 3
    overall = _clamp_score(data.get("overall"), default=default_overall)
    weaknesses = [str(w).strip()[:300] for w in (data.get("weaknesses") or [])[:3] if str(w).strip()]
    unsupported = [str(c).strip()[:300]
                   for c in (data.get("unsupported_claims") or [])[:20] if str(c).strip()]
    return {"scores": scores, "overall": overall, "weaknesses": weaknesses,
            "unsupported_claims": unsupported}


async def blind_review(
    question: str,
    report_text: str,
    sources: Optional[Sequence[Any]] = None,
    *,
    url: Optional[str] = None,
    model: Optional[str] = None,
    owner: Optional[str] = None,
    writer_endpoint_url: Optional[str] = None,
    writer_model: Optional[str] = None,
    writer_self_score: Optional[float] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Score a finished research report without seeing how it was produced.

    Endpoint resolution (when `url`/`model` are not both given directly):
    `src.endpoint_resolver.resolve_endpoint("research_blind_review", ...)`,
    the same helper/utility-endpoint fallback chain every other internal,
    tool-less pass uses (`src/typed_choice.py`, `src/task_endpoint.py`,
    `src/context_compactor.py`) -- Research Blind Review endpoint/model
    settings, then Utility, then Default Chat Model, then (last resort)
    `writer_endpoint_url`/`writer_model` if the caller supplied them. The
    writer's own identity is never put in the prompt regardless of which
    endpoint answers it.

    Never raises: every failure path (no endpoint configured, timeout,
    network error, unparsable response) is recorded in the result's
    `error` field and returned, not thrown.
    """
    t0 = time.time()
    reviewer_url, reviewer_model = url, model
    if not reviewer_url or not reviewer_model:
        try:
            from src.endpoint_resolver import resolve_endpoint
            reviewer_url, reviewer_model, _headers = resolve_endpoint(
                "research_blind_review",
                fallback_url=writer_endpoint_url, fallback_model=writer_model,
                owner=owner,
            )
        except Exception:  # noqa: BLE001 - never let settings/DB trouble break this
            logger.debug("[research_review] endpoint resolution failed", exc_info=True)

    result: Dict[str, Any] = {
        "model": reviewer_model or "",
        "overall": None,
        "scores": None,
        "weaknesses": [],
        "unsupported_claims": [],
        "calibration_gap": None,
        "duration_s": 0.0,
    }
    if not reviewer_url or not reviewer_model:
        result["error"] = "no reviewer endpoint/model configured"
        result["duration_s"] = round(time.time() - t0, 1)
        return result

    try:
        from src.llm_core import llm_call_async
        raw = await asyncio.wait_for(
            llm_call_async(
                url=reviewer_url, model=reviewer_model,
                messages=[{"role": "user",
                           "content": build_reviewer_prompt(question, report_text, sources)}],
                temperature=0.1, max_tokens=900, timeout=int(timeout_s),
                max_retries=1, workload="background",
                response_schema=REVIEWER_SCHEMA,
            ),
            timeout=timeout_s + 15,
        )
    except asyncio.TimeoutError:
        result["error"] = f"blind review timed out after {int(timeout_s) + 15}s"
        result["duration_s"] = round(time.time() - t0, 1)
        return result
    except Exception as e:  # noqa: BLE001 - a review must never fail the research run
        result["error"] = f"{type(e).__name__}: {e}"[:300]
        result["duration_s"] = round(time.time() - t0, 1)
        return result

    if isinstance(raw, tuple):
        raw = raw[0]
    parsed = _parse_review(raw if isinstance(raw, str) else "")
    if parsed is None:
        result["error"] = "reviewer response could not be parsed as JSON"
        result["duration_s"] = round(time.time() - t0, 1)
        return result

    result.update(scores=parsed["scores"], overall=parsed["overall"],
                   weaknesses=parsed["weaknesses"],
                   unsupported_claims=parsed["unsupported_claims"])
    if writer_self_score is not None:
        try:
            reviewer_normalized = (float(parsed["overall"]) - 1.0) / 4.0
            result["calibration_gap"] = round(float(writer_self_score) - reviewer_normalized, 4)
        except (TypeError, ValueError):
            result["calibration_gap"] = None
    result["duration_s"] = round(time.time() - t0, 1)
    return result
