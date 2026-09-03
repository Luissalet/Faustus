# src/deep_research.py
"""
IterResearch-style deep research engine.

Implements an iterative Think→Search→Extract→Synthesize loop where the LLM
drives every decision: what to search, what's relevant, what's missing, and
when to stop.  Inspired by Alibaba's IterResearch approach.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.research_utils import strip_thinking, is_low_quality

from src.goal_based_extractor import EXTRACTOR_SYSTEM
from src.prompt_security import untrusted_context_message
from src.research_citations import (
    LANGUAGE_NAMES,
    SourceRegistry,
    detect_language,
    finalize_report,
    implication_label,
)

logger = logging.getLogger(__name__)


def current_date_context() -> str:
    """Preamble that grounds query-generation/planning LLMs in the real current
    date. Without it the model falls back to its training-cutoff year and emits
    queries like "best Python tutorials 2025" when the year is actually 2026.
    System TZ-local so it matches what the user sees. Portable strftime only."""
    now = datetime.now().astimezone()
    return (
        f"Today's date is {now.strftime('%B %d, %Y')} ({now.strftime('%Y-%m-%d')}). "
        f"When a search query needs a year or refers to 'latest'/'current'/"
        f"'this year', use {now.strftime('%Y')} or relative wording — never a "
        f"year inferred from training data.\n\n"
    )

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
RESEARCH_PLAN_PROMPT = """\
You are a research strategist. Before searching, analyze this question and create a research plan.

**Question:** {question}

Break this question down:
1. What are the key sub-topics that need to be covered for a comprehensive answer?
2. What specific data points, facts, or perspectives should we look for?
3. What would a complete, high-quality answer include?

Return a JSON object with:
- "sub_questions": Array of 3-6 specific sub-questions to investigate
- "key_topics": Array of key topics/angles to cover
- "success_criteria": One sentence describing what a complete answer looks like

Example:
{{
  "sub_questions": ["What is the cost of living in X?", "How is the healthcare system?"],
  "key_topics": ["economy", "healthcare", "safety", "culture"],
  "success_criteria": "A balanced comparison covering cost, quality of life, and practical considerations."
}}
"""

QUERY_GEN_PROMPT = """\
You are a research assistant planning web searches.

**Original question:** {question}

**Sub-questions the user asked — every one of them needs evidence:**
{sections}

**Research plan:**
{research_plan}

**What we know so far:**
{report}

**Round:** {round_num}

Generate {num_queries} focused search queries that will help answer the question.
{round_instruction}

Return ONLY a JSON array of query strings, nothing else.
Example: ["query one", "query two", "query three"]
"""

SYNTHESIZE_PROMPT = """\
You are updating an evolving research report.

**Original question:** {question}

**Sub-questions the report must end up covering:**
{sections}

**Current report:**
{report}

**New evidence from this round — each source carries the number you cite it by:**
{sources}

{language_line}

Integrate the new evidence into the existing report. Produce an updated, \
well-organized report that answers the original question as completely as \
possible given all evidence so far. Remove redundancy, resolve contradictions, \
and maintain logical flow.

Citations: end every factual sentence with a numbered marker like [3], or \
[3, 5] when two sources support it. Use ONLY numbers from the evidence list \
above or numbers already present in the current report. Never invent one, and \
keep every figure in the same sentence as the source it came from.

Write only the updated report — no preamble or meta-commentary.
"""

STOP_PROMPT = """\
You are deciding whether a research report is comprehensive enough.

**Original question:** {question}

**Current report:**
{report}

**Rounds completed:** {round_num} of {max_rounds}

Based on the report so far, do we have enough information to answer the question \
comprehensively?  Consider:
- Are the key aspects of the question addressed?
- Are there obvious gaps or unanswered sub-questions?
- Is the evidence sufficient and from multiple sources?

If rounds completed is well below the target, prefer continuing unless the \
report is already exhaustive.

Reply with ONLY "YES" or "NO" followed by a brief one-sentence reason.
Example: "YES — The report covers all major aspects with evidence from multiple sources."
Example: "NO — We still lack information about the economic impact."
"""

FINAL_REPORT_PROMPT = """\
Write a long, detailed, comprehensive research report answering this question.

**Question:** {question}

**Sections the report must have, as ## headings, in exactly this order:**
{sections}

**Numbered evidence — the only sources you may cite:**
{sources}

**All collected analysis so far:**
{report}

{language_line}

Requirements:
- At MINIMUM 1500 words. Multiple detailed paragraphs per section, not bullet
  lists. Use ### subheadings inside a section when it needs them.
- CITATIONS: every factual sentence ends with a numbered marker — [3], or
  [3, 5] when two sources support it. Use ONLY numbers from the evidence list
  above. Never invent a number and never cite a source you did not use.
- Every figure travels with its source in the same sentence: write "the trial
  reported a 40% drop over 12 weeks [3]", never a bare "40%" that the reader
  cannot trace back.
- When the question involves more than one option, protocol or approach,
  include at least one markdown comparison table. Its LAST column says what the
  row means in practice for the reader — it does not restate the data.
- Close each major section with a one-line blockquote callout in this shape:
  > **{implication}:** what someone should actually do with this.
- Say where the sources agree and where they disagree, naming the numbers.
- BREADTH: every numbered source above was fetched and read for THIS question,
  so each one has something the others do not. Draw on the whole list rather
  than leaning on the first two — where a later source covers a section better,
  cite it there. This is not a licence to pad: never attach a number to a
  sentence it did not support. A source with genuinely nothing to add is left
  out, and the report says so in one clause rather than citing it emptily.
- Open with a short executive summary and end with a conclusion that answers
  the question directly.
- Do NOT write a "how to read this report" section, and do NOT write a
  sources list at the end. Both are generated afterwards, in code, from the
  citations you actually used.
- Write in an engaging, informative style — not dry or robotic.
"""

# Appended to every category override: the format may change, the obligation to
# cite by number does not. Without this line a category prompt silently
# reintroduces its own citation style and the audit finds nothing to check.
_CATEGORY_CITATION_RULE = (
    "\n- Citations do not change with the format: every factual sentence still "
    "ends with a numbered marker like [3], taken only from the numbered "
    "evidence list, and figures stay in the same sentence as their source"
)

# The sections each category mandates. They are handed to the model through
# _sections_block, the one place in the prompt that names the report's headings
# and the one place the language instruction reaches — an "## The Claim" in a
# Spanish report is what happens when a second list of English section names is
# mandated further down the prompt.
_CATEGORY_SECTIONS = {
    "product": ("Top Picks", "The Options", "Verdict"),
    "comparison": ("Comparison Table", "The Options", "Best For",
                   "Shared Considerations"),
    "howto": ("Quick Guide", "Prerequisites", "Steps", "Common Mistakes"),
    "factcheck": ("The Claim", "Evidence For", "Evidence Against", "Verdict",
                  "Nuance and Caveats"),
}

# Not one line below contains a "#". A prompt line that spells a heading marker
# is a line the model can paste into the report as a heading, which is how
# "- Create ## Evidence For and ## Evidence Against sections" arrived in a
# finished report as the single heading "## Evidence For and ## Evidence
# Against". Section names live in _CATEGORY_SECTIONS, one per line; the rules
# only ever refer to them in quotes.
_CATEGORY_FORMATS = {
    "product": """IMPORTANT FORMAT OVERRIDE — this is a PRODUCT research report:
- Rank the options best first, and say what the ranking is based on
- Under "Top Picks", open with a markdown comparison table whose columns are Name, Price, Best For and Rating
- Under "The Options", give every product its own third-level heading carrying its name, then its approximate price, a two or three sentence summary, a "Pros:" bullet list, a "Cons:" bullet list, and where to buy it as links
- Under "Verdict", name a Best Overall and a Best Value, and say what decides between them""",

    "comparison": """IMPORTANT FORMAT OVERRIDE — this is a COMPARISON report:
- Under "Comparison Table", put one markdown table covering every option against the criteria that matter, rows for criteria and columns for options, with short values, ratings or checkmarks in the cells
- Under "The Options", give every option its own third-level heading with its strengths, its weaknesses and the reader it suits
- Under "Best For", name a winner for each kind of reader, in the shape "Best for small teams: Option A, because ..."
- Under "Shared Considerations", cover what applies to every option alike""",

    "howto": """IMPORTANT FORMAT OVERRIDE — this is a HOW-TO guide:
- Under "Quick Guide", a numbered list with one line per step, the action only and no detail, plus the estimated time and the difficulty
- Under "Prerequisites", everything the reader needs before starting
- Under "Steps", give every step its own numbered third-level heading with the full instructions under it
- Use blockquotes for tips and warnings, in the shape "> **Tip:** ..." or "> **Warning:** ..."
- Under "Common Mistakes", what goes wrong in practice and how to avoid it""",

    "factcheck": """IMPORTANT FORMAT OVERRIDE — this is a FACT-CHECK report:
- Under "The Claim", restate exactly what is being checked
- Under "Evidence For", give every supporting piece of evidence its own third-level heading naming the source, what it found and how strong it is
- Under "Evidence Against", do the same for every piece of evidence that cuts the other way
- Under "Verdict", choose exactly one of Supported, Mixed Evidence or Unsupported, and say what decided it
- Under "Nuance and Caveats", the context and the limits that change how the verdict should be read
- Stay balanced: the two evidence sections are weighed against each other, not stacked""",
}

CATEGORY_PROMPTS = {name: text + _CATEGORY_CITATION_RULE
                    for name, text in _CATEGORY_FORMATS.items()}

# A line that opens with "1.", "1)", "(1)", "-", "*", "•" or "a)" is an item in
# the user's own outline, so it counts as a sub-question even without a "?".
_SUBQ_MARKER_RE = re.compile(r"^\s{0,6}(?:\(?\d{1,2}[.)]|[-*\u2022\u2023+]|[a-hA-H][.)])\s+")

# The same outline typed on one line: "...posmenopausicas? 1) Que evidencia
# hay? 2) Cuanto tiempo?". Digits only, and only where the marker follows the
# end of a sentence rather than a word — that lookbehind is what keeps "el
# grupo 1) tuvo menos dolor" from being read as a list.
_INLINE_ENUM_RE = re.compile(r"(?:^|(?<=[?!.:;\uff1f]))\s*\(?(\d{1,2})[.)]\s+")
MAX_SUBQUESTIONS = 12
_MIN_SUBQUESTION_CHARS = 12
_MAX_SUBQUESTION_CHARS = 300


def _split_on_question_marks(text: str) -> List[str]:
    """One paragraph into its question-shaped pieces, each keeping its '?'."""
    parts: List[str] = []
    buf: List[str] = []
    for ch in text:
        buf.append(ch)
        if ch in "?\uff1f":
            parts.append("".join(buf))
            buf = []
    tail = "".join(buf)
    if tail.strip():
        parts.append(tail)
    return parts


def _enumerated_chunks(body: str) -> List[Tuple[str, bool]]:
    """One line into ``(text, came_from_an_enumeration)`` chunks.

    Two markers minimum and strictly ascending numbers, because one stray "1)"
    in prose is far more common than a one-item list, and a wrong split here
    costs the reader a whole bogus section of the report.

    The lead question is whatever precedes the first marker. Taking the whole
    line instead is what put the entire prompt in the report as section 1 and
    then repeated its tail as sections 2 to 4.
    """
    markers = list(_INLINE_ENUM_RE.finditer(body))
    numbers = [int(m.group(1)) for m in markers]
    if len(markers) < 2 or any(b <= a for a, b in zip(numbers, numbers[1:])):
        return [(body, False)]
    chunks: List[Tuple[str, bool]] = []
    lead = body[:markers[0].start()].strip()
    if lead:
        chunks.append((lead, False))
    for index, match in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        # match.end() is past the marker, so "1) " never survives into a
        # heading the report generator would then number a second time.
        chunks.append((body[match.end():end].strip(), True))
    return chunks

# ---------------------------------------------------------------------------
# DeepResearcher
# ---------------------------------------------------------------------------
class DeepResearcher:
    """
    Iterative research engine following the IterResearch pattern.

    Each round: LLM generates queries → SearXNG search → LLM extracts from
    top pages → LLM synthesizes into evolving report → LLM decides continue/stop.
    """

    def __init__(
        self,
        llm_endpoint: str,
        llm_model: str,
        llm_headers: Optional[Dict] = None,
        max_rounds: int = 8,
        max_time: int = 300,
        max_urls_per_round: int = 3,
        max_content_chars: int = 15000,
        max_report_tokens: int = 8192,
        extraction_timeout: int = 90,
        planning_timeout: int = 90,
        query_timeout: int = 120,
        extraction_concurrency: int = 3,
        min_rounds: int = 2,
        max_empty_rounds: int = 2,
        synthesis_window: int = 10,
        progress_callback: Optional[Callable] = None,
        search_provider: Optional[str] = None,
        category: Optional[str] = None,
    ):
        self.llm_endpoint = llm_endpoint
        self.llm_model = llm_model
        self.llm_headers = llm_headers
        self.search_provider_override = search_provider
        self.category = category
        self.max_rounds = max_rounds
        self.max_time = max_time
        self.max_urls_per_round = max_urls_per_round
        self.max_content_chars = max_content_chars
        self.max_report_tokens = max_report_tokens
        self.extraction_timeout = min(3600, max(15, int(extraction_timeout or 90)))
        self.planning_timeout = min(3600, max(15, int(planning_timeout or 90)))
        self.query_timeout = min(3600, max(15, int(query_timeout or 120)))
        self.extraction_concurrency = min(12, max(1, int(extraction_concurrency or 3)))
        self.min_rounds = min_rounds
        self.max_empty_rounds = max_empty_rounds
        self.synthesis_window = synthesis_window
        self._progress = progress_callback
        self._cancelled = False
        self._start_time: float = 0
        self.queries_used: Set[str] = set()
        self.urls_fetched: Set[str] = set()
        self.analyzed_urls: List[Dict[str, str]] = []
        self.round_count: int = 0
        # Track which search providers actually returned results during the
        # run, in arrival order — surfaced in the visual report so users can
        # see whether searxng / brave / tavily etc. carried the work.
        self.providers_used: List[str] = []
        self.findings: List[Dict] = []
        self.evolving_report: str = ""
        self.research_plan: str = ""
        # The user's own questions, in their order — they become the report's
        # ## sections, so a numbered list asked for survives to the contents.
        self.subquestions: List[str] = []
        self.report_language: str = "en"
        # Numbers are handed out as findings arrive, so a marker written in
        # round two still resolves in the report written after round eight.
        self.citations = SourceRegistry()
        self.citation_audit = None

    def cancel(self):
        """Request cooperative cancellation of the research loop."""
        self._cancelled = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def research(
        self,
        question: str,
        prior_report: str = "",
        prior_findings: Optional[List[Dict]] = None,
        prior_urls: Optional[Set[str]] = None,
    ) -> str:
        """Run iterative research and return a final report.

        Args:
            question: The research question.
            prior_report: Previous report to continue from (for follow-up research).
            prior_findings: Previous findings to build on.
            prior_urls: URLs already visited (won't be re-fetched).
        """
        self._start_time = time.time()
        self.report_language = detect_language(question)
        self.subquestions = self._extract_subquestions(question)
        findings: List[Dict] = list(prior_findings) if prior_findings else []
        report = prior_report or ""
        # A continuation inherits the earlier run's pages; registering them
        # first keeps their numbers below the ones this run hands out.
        for finding in findings:
            self.citations.add(finding)

        # PLAN: Analyze the question and create a research strategy
        if not prior_report:
            self._emit(phase="planning")
            self.research_plan = await self._create_plan(question)
            logger.info(f"Research plan: {self.research_plan[:200]}")
        else:
            # Continuation — plan around the follow-up
            self._emit(phase="planning")
            self.research_plan = await self._create_plan(question)
            logger.info(f"Continuation plan: {self.research_plan[:200]}")
        if not self.category and not prior_report:
            self.category = await self._classify_category(question)
            if self.category:
                logger.info(f"Auto-detected category: {self.category}")

        if prior_urls:
            self.urls_fetched.update(prior_urls)
        self.findings = findings  # expose for handler
        consecutive_empty_rounds = 0

        for round_num in range(1, self.max_rounds + 1):
            self.round_count = round_num
            if self._cancelled:
                logger.info(f"Research cancelled after {round_num - 1} rounds")
                break
            if self._time_exceeded():
                logger.info(f"Time limit reached after {round_num - 1} rounds")
                break

            logger.info(f"=== Research Round {round_num} ===")
            self._emit(phase="searching", round=round_num, total_sources=len(self.urls_fetched))

            # THINK: generate queries
            queries = await self._generate_queries(question, report, round_num)
            if not queries:
                logger.warning(f"Round {round_num}: no queries generated, stopping")
                break

            self._emit(phase="searching", round=round_num, queries=len(queries),
                       query_preview=queries[0] if queries else "",
                       total_sources=len(self.urls_fetched))

            # SEARCH + EXTRACT
            round_findings = await self._search_and_extract(queries, question)
            if round_findings:
                findings.extend(round_findings)
                for finding in round_findings:
                    self.citations.add(finding)
                consecutive_empty_rounds = 0
                logger.info(f"Round {round_num}: extracted {len(round_findings)} findings")
                self._emit(phase="reading", round=round_num,
                           new_sources=len(round_findings),
                           total_sources=len(self.urls_fetched),
                           total_findings=len(findings))
            else:
                consecutive_empty_rounds += 1
                logger.info(f"Round {round_num}: no new findings ({consecutive_empty_rounds} consecutive empty)")
                if consecutive_empty_rounds >= self.max_empty_rounds:
                    logger.warning(f"Search appears to be down — {self.max_empty_rounds} consecutive rounds with no results")
                    err_detail = getattr(self, '_last_search_error', 'unknown error')
                    self._emit(phase="error", message=f"Search engine unavailable: {err_detail}")
                    if not findings:
                        return (
                            f"**Search unavailable** — Web search failed after "
                            f"{round_num} rounds. Error: {err_detail}\n\n"
                            "Please check your search provider settings and ensure the service is running."
                        )
                    break

            # SYNTHESIZE
            if findings:
                self._emit(phase="analyzing", round=round_num,
                           total_sources=len(self.urls_fetched),
                           total_findings=len(findings))
                report = await self._synthesize(question, findings, report)

            # DECIDE
            if round_num >= self.min_rounds:
                should_stop = await self._should_stop(question, report, round_num)
                if should_stop:
                    logger.info(f"LLM decided to stop after round {round_num}")
                    break

        # FINAL REPORT
        self._emit(phase="writing", total_sources=len(self.urls_fetched),
                   total_findings=len(findings))
        if not report:
            # Synthesis can fail (e.g. the LLM timed out) even though the search
            # rounds did gather findings. Don't throw that work away — return the
            # gathered findings as a basic compiled report instead of claiming
            # nothing was found (#1551).
            if findings:
                logger.warning(
                    "Synthesis produced no report; returning %d gathered "
                    "finding(s) as a fallback", len(findings)
                )
                return self._fallback_report(question, findings)
            return "No information could be gathered for this question."

        self.evolving_report = report  # preserve pre-synthesis report
        final = await self._final_report(question, report)
        final = self._finalize_citations(final)
        elapsed = time.time() - self._start_time
        logger.info(
            f"Research complete: {self.round_count} rounds, "
            f"{len(findings)} findings, {len(self.urls_fetched)} URLs, "
            f"{elapsed:.1f}s"
        )
        return final

    # ------------------------------------------------------------------
    # LLM helper
    # ------------------------------------------------------------------
    async def _llm(self, messages: List[Dict], temperature: float = 0.3,
                   max_tokens: int = 4096, timeout: int = 60) -> str:
        """Call the LLM asynchronously and strip thinking tags."""
        from src.llm_core import llm_call_async
        response = await llm_call_async(
            url=self.llm_endpoint,
            model=self.llm_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            headers=self.llm_headers,
            timeout=timeout,
        )
        return strip_thinking(response)

    # ------------------------------------------------------------------
    # PLAN: create research strategy
    # ------------------------------------------------------------------
    async def _create_plan(self, question: str) -> str:
        """LLM analyzes the question and creates a research plan.

        The user's own sub-questions are extracted deterministically first and
        win over anything the planner invents: they are the shape the report's
        contents has to keep. The planner only supplies a decomposition when
        the question was one dense paragraph with none to extract.
        """
        self.report_language = detect_language(question)
        self.subquestions = self._extract_subquestions(question)
        prompt = current_date_context() + RESEARCH_PLAN_PROMPT.format(question=question)
        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                timeout=getattr(self, "planning_timeout", 90),
            )
            # Try to parse as JSON for structured plan
            parsed = self._parse_json_object(response)
            if parsed:
                if len(self.subquestions) < 2 and parsed.get("sub_questions"):
                    self.subquestions = self._clean_subquestions(parsed["sub_questions"])
                parts = []
                if self.subquestions:
                    parts.append("Sub-questions: " + "; ".join(self.subquestions))
                if parsed.get("key_topics"):
                    parts.append("Key topics: " + ", ".join(parsed["key_topics"]))
                if parsed.get("success_criteria"):
                    parts.append("Success: " + parsed["success_criteria"])
                return "\n".join(parts) if parts else response
            return response
        except Exception as e:
            # A degraded run the user can see beats a silent one: name what
            # broke and what we are doing instead, and hand the loop a real
            # plan built from the questions they actually asked rather than the
            # empty string that used to make every later round search blind.
            reason = f"{type(e).__name__}: {e}"
            logger.warning(f"Research planning failed: {reason}")
            fallback = self.subquestions or [str(question).strip()]
            fallback = [q for q in fallback if q] or ["the question as asked"]
            noun = "question" if len(fallback) == 1 else "questions"
            self._emit(
                phase="warning",
                message=(f"Planning step failed ({reason}); researching the "
                         f"{len(fallback)} {noun} directly."),
            )
            return "Sub-questions: " + "; ".join(fallback)

    # ------------------------------------------------------------------
    # Sub-questions — the user's outline, kept
    # ------------------------------------------------------------------
    def _extract_subquestions(self, question) -> List[str]:
        """The questions the user actually asked, in their order and wording.

        Deterministic on purpose. A numbered list, a bulleted list and a
        paragraph that packs several "?" all decompose here without a model;
        a dense paragraph with none returns [], and `_create_plan` then falls
        back to the planner's own decomposition. Never raises.
        """
        try:
            if question is None:
                return []
            text = question if isinstance(question, str) else str(question)
        except Exception:  # noqa: BLE001
            return []
        found: List[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            bulleted = bool(_SUBQ_MARKER_RE.match(line))
            body = (_SUBQ_MARKER_RE.sub("", line) if bulleted else line).strip()
            if not body:
                continue
            for chunk, enumerated in _enumerated_chunks(body):
                for piece in _split_on_question_marks(chunk):
                    piece = piece.strip(" \t-\u2013\u2014\u2022*")
                    if len(piece) < _MIN_SUBQUESTION_CHARS:
                        continue
                    if bulleted or enumerated or piece.endswith("?"):
                        found.append(piece)
        return self._clean_subquestions(found)

    @staticmethod
    def _clean_subquestions(items) -> List[str]:
        """Trim, deduplicate, drop any question that swallows another, cap at 12.

        The containment rule is the backstop for a question typed as one
        paragraph: however the split goes, an entry that contains another entry
        is the framing, not a question, and giving it a section of its own
        repeats the whole prompt as a heading.
        """
        out: List[str] = []
        keys: List[str] = []
        seen = set()
        for item in items or []:
            try:
                text = (item if isinstance(item, str) else str(item)).strip()
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            key = re.sub(r"\W+", " ", text.lower()).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(text[:_MAX_SUBQUESTION_CHARS])
            keys.append(key)
        kept = [text for index, text in enumerate(out)
                if not any(other != keys[index] and other in keys[index]
                           for other in keys)]
        return kept[:MAX_SUBQUESTIONS]

    async def _classify_category(self, question: str) -> Optional[str]:
        """Fast LLM call to classify the research question into a category."""
        valid = ", ".join(CATEGORY_PROMPTS.keys())
        prompt = (
            f"Classify this research question into exactly ONE category.\n"
            f"Categories: {valid}\n"
            f"If none fit well, respond with: general\n\n"
            f"Question: {question}\n\n"
            f"Respond with ONLY the category name, nothing else."
        )
        try:
            result = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0, max_tokens=20, timeout=15,
            )
            cat = (result or "").strip().lower()
            # Clean one-word answer first.
            parts = cat.split()
            first = parts[0].strip(".,\"'*:") if parts else ""
            if first in CATEGORY_PROMPTS:
                return first
            # Weak local models often wrap the label in preamble ("the category
            # is product") — scan the whole reply for any known category word
            # before giving up (which would default to the generic format).
            for c in CATEGORY_PROMPTS:
                if c in cat:
                    return c
            return None
        except Exception as e:
            logger.warning(f"Category classification failed: {e}")
            return None

    # ------------------------------------------------------------------
    # THINK: generate search queries
    # ------------------------------------------------------------------
    async def _generate_queries(self, question: str, report: str,
                                round_num: int) -> List[str]:
        if round_num == 1:
            num_queries = 4
            round_instruction = (
                "This is the first round — generate broad, diverse queries "
                "that explore the key facets of the question."
            )
        else:
            num_queries = 3
            round_instruction = (
                "We already have partial findings.  Generate targeted follow-up "
                "queries to fill gaps, verify claims, or explore specific aspects "
                "that the report doesn't yet cover well."
            )

        prompt = current_date_context() + QUERY_GEN_PROMPT.format(
            question=question,
            sections=self._sections_block(),
            research_plan=self.research_plan or "(No plan — search broadly.)",
            report=report or "(No findings yet.)",
            round_num=round_num,
            num_queries=num_queries,
            round_instruction=round_instruction,
        )

        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=4096,
                timeout=getattr(self, "query_timeout", 120),
            )
            queries = self._parse_json_array(response)
            # Deduplicate
            new_queries = [q for q in queries if q not in self.queries_used]
            self.queries_used.update(new_queries)
            logger.info(f"Round {round_num} queries: {new_queries}")
            return new_queries
        except Exception as e:
            logger.error(f"Query generation failed: {e}")
            self._emit(phase="warning", message=f"Query generation failed: {e}")
            return []

    # ------------------------------------------------------------------
    # SEARCH + EXTRACT
    # ------------------------------------------------------------------
    async def _search_and_extract(self, queries: List[str],
                                  question: str) -> List[Dict]:
        """Search each query and extract relevant info from top results."""
        all_findings: List[Dict] = []

        # Search all queries in parallel
        search_tasks = [self._search(q) for q in queries]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        # Collect URLs to fetch from all search results
        urls_to_fetch = []
        for result in search_results:
            if isinstance(result, Exception):
                logger.warning(f"Search error: {result}")
                continue
            if not result:
                continue
            for r in result:
                url = r.get("url", "")
                if url and url not in self.urls_fetched:
                    urls_to_fetch.append(r)
                    self.urls_fetched.add(url)
                    self.analyzed_urls.append({
                        "url": url,
                        "title": r.get("title", "") or url,
                    })
                if len(urls_to_fetch) >= self.max_urls_per_round * len(queries):
                    break

        if self._cancelled or self._time_exceeded():
            return all_findings

        # Fetch and extract URLs with backpressure. Local model servers often
        # serialize requests behind one GPU; flooding them makes every request
        # slower and can trip the extraction timeout.
        semaphore = asyncio.Semaphore(self.extraction_concurrency)

        async def _bounded_extract(result: Dict) -> Optional[Dict]:
            async with semaphore:
                return await self._fetch_and_extract(result["url"], question, result.get("title", ""))

        extract_tasks = [_bounded_extract(r) for r in urls_to_fetch]
        results_gathered = await asyncio.gather(*extract_tasks, return_exceptions=True)

        for result in results_gathered:
            if isinstance(result, Exception):
                logger.warning(f"Extraction error: {result}")
                continue
            if result:
                all_findings.append(result)

        return all_findings

    async def _search(self, query: str) -> List[Dict]:
        """Run a search query using the configured research search provider."""
        try:
            from src.search.core import _call_provider, _build_provider_chain
            provider = self._active_search_provider()

            if provider == "disabled":
                logger.info("Search is disabled for research")
                return []

            # Try primary provider, then fallbacks
            chain = _build_provider_chain(provider)
            raised = False
            for prov in chain:
                try:
                    results = await asyncio.to_thread(_call_provider, prov, query, 10)
                    if results:
                        logger.info(f"Research search: {prov} returned {len(results)} results")
                        if prov not in self.providers_used:
                            self.providers_used.append(prov)
                        return results
                except Exception as e:
                    raised = True
                    logger.warning(f"Research search: {prov} failed: {e}")
                    self._last_search_error = f"{prov}: {e}"
            # Every provider ran but none returned results. If none of them
            # raised, record an actionable reason here — otherwise this empty
            # path leaves `_last_search_error` unset and the caller surfaces a
            # bare "unknown error" (issue #344). This is exactly the SearXNG
            # case where the service is reachable but all its engines fail, so
            # each provider returns [] without throwing.
            if not raised:
                self._last_search_error = (
                    f"no results from search provider(s): "
                    f"{', '.join(chain) if chain else provider}"
                )
            return []
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            self._last_search_error = str(e)
            return []

    def _active_search_provider(self) -> str:
        """Resolve the research provider using the same precedence as search."""
        provider = (self.search_provider_override or "").strip()
        if provider:
            return provider

        from src.search.providers import _get_search_settings

        settings = _get_search_settings()
        if not provider:
            provider = (settings.get("research_search_provider") or "").strip()
        if not provider:
            provider = settings.get("search_provider", "searxng")
        return str(provider or "searxng").strip()

    async def _fetch_and_extract(self, url: str, question: str,
                                 title: str) -> Optional[Dict]:
        """Fetch a URL's content and use LLM to extract relevant info."""
        display = title or url
        self._emit(phase="reading", url=url, title=display,
                   total_sources=len(self.urls_fetched))
        from src.search import fetch_webpage_content

        page = None
        # Resolving the provider reads settings, which this per-source path
        # never had to do before. A settings layer that cannot be read must
        # mean "use the native fetcher", not lose the source.
        try:
            use_firecrawl = self._active_search_provider() == "firecrawl"
        except Exception:
            use_firecrawl = False
        if use_firecrawl:
            # Imported inside the branch, not beside fetch_webpage_content: a
            # run on any other provider must not depend on the Firecrawl symbol
            # resolving at all.
            from src.search import firecrawl_scrape

            scrape_timeout = min(180, max(30, self.extraction_timeout))
            page = await asyncio.to_thread(firecrawl_scrape, url, scrape_timeout)
            if not page.get("success") or not page.get("content"):
                logger.warning(
                    "Firecrawl could not scrape %s (%s); falling back to the "
                    "native bounded fetcher",
                    url,
                    page.get("error", "empty response"),
                )

        if not page or not page.get("success") or not page.get("content"):
            try:
                page = await asyncio.to_thread(fetch_webpage_content, url, 10)
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                return None

        if not page.get("success") or not page.get("content"):
            return None

        content = page["content"]
        # Truncate to avoid blowing up context, preferring paragraph boundary
        if len(content) > self.max_content_chars:
            truncated = content[:self.max_content_chars]
            last_para = truncated.rfind('\n\n')
            if last_para > self.max_content_chars * 0.8:
                content = truncated[:last_para]
            else:
                content = truncated

        try:
            response = await self._llm(
                [
                    {"role": "user", "content": EXTRACTOR_SYSTEM.format(goal=question)},
                    untrusted_context_message("webpage", content),
                ],
                temperature=0.2,
                max_tokens=2048,
                timeout=self.extraction_timeout,
            )
            parsed = self._parse_json_object(response)
            if parsed:
                parsed["url"] = url
                parsed["title"] = title or page.get("title", "")
                parsed["og_image"] = page.get("og_image", "")
                summary = parsed.get("summary")
                summary = summary.strip() if isinstance(summary, str) else ""
                evidence = parsed.get("evidence")
                evidence = evidence.strip() if isinstance(evidence, str) else ""
                # An empty `summary` beside real `evidence` is the extractor
                # skipping a field, not a verdict that the page was useless.
                # is_low_quality("") is True, so this finding used to be thrown
                # away along with the fetch we already paid for.
                if not summary and evidence:
                    return parsed
                # Skip findings where the LLM says the page is useless
                if is_low_quality(summary):
                    logger.info(f"Skipping low-quality extraction from {url}")
                    return None
                return parsed
            # If JSON parsing fails, treat entire response as evidence
            return {
                "url": url,
                "title": title or page.get("title", ""),
                "og_image": page.get("og_image", ""),
                "rational": "LLM extraction (raw)",
                "evidence": response[:3000],
                "summary": response[:500],
            }
        except Exception as e:
            logger.warning(f"LLM extraction failed for {url}: {e}")
            return None

    # ------------------------------------------------------------------
    # SYNTHESIZE
    # ------------------------------------------------------------------
    async def _synthesize(self, question: str, findings: List[Dict],
                          current_report: str) -> str:
        """LLM synthesizes all findings into an updated report."""
        # Format findings for the prompt
        window = findings[-self.synthesis_window:]
        if len(findings) > self.synthesis_window:
            logger.info(f"Synthesis using last {self.synthesis_window} of {len(findings)} findings")
        prompt = SYNTHESIZE_PROMPT.format(
            question=question,
            sections=self._sections_block(),
            report=current_report or "(First round — no report yet.)",
            sources=self._numbered_findings(window),
            language_line=self._language_line(),
        )

        try:
            return await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=self.max_report_tokens,
                # Synthesis is a heavy generation call like the final report
                # (which gets 180s); a slow local model (e.g. a 20B served from
                # LM Studio) routinely needs >60s for it. The old 60s cap timed
                # out mid-stream and discarded the round's findings (#1551).
                timeout=180,
            )
        except Exception as e:
            logger.error(f"Synthesis failed: {e}")
            self._emit(phase="warning", message="Synthesis failed, keeping previous report")
            return current_report  # keep the old report on failure

    # ------------------------------------------------------------------
    # DECIDE
    # ------------------------------------------------------------------
    async def _should_stop(self, question: str, report: str,
                           round_num: int) -> bool:
        """Let the LLM decide whether the report is comprehensive enough."""
        prompt = STOP_PROMPT.format(
            question=question,
            report=report,
            round_num=round_num,
            max_rounds=self.max_rounds,
        )

        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=128,
            )
            # Reasoning models prepend a <think>...</think> block — strip it
            # before checking for YES/NO, otherwise the answer always looks
            # like it starts with "<THINK>" and the engine never stops.
            clean = strip_thinking(response).strip()
            # Tolerate "**YES**", "Yes.", quotes, etc.
            answer = re.sub(r'^[\s*_`"\'>#\-]+', '', clean).upper()
            should_stop = answer.startswith("YES")
            logger.info(f"Stop decision (round {round_num}): {clean[:120]}")
            return should_stop
        except Exception as e:
            logger.warning(f"Stop decision failed: {e}")
            return False  # continue on error

    # ------------------------------------------------------------------
    # FINAL REPORT
    # ------------------------------------------------------------------
    async def _final_report(self, question: str, report: str) -> str:
        """LLM writes a polished final report, retrying if too short."""
        prompt = FINAL_REPORT_PROMPT.format(
            question=question,
            report=report,
            sections=self._sections_block(),
            sources=self._registered_sources_block(),
            language_line=self._language_line(),
            implication=implication_label(getattr(self, "report_language", "en")),
        )
        # No category override once the user asked their own sub-questions:
        # _sections_block has already made those the report's sections, and the
        # override would mandate a second, different skeleton over the top of
        # them. Sub-questions win — they are what was actually asked.
        if not (getattr(self, "subquestions", None) or []):
            cat_extra = CATEGORY_PROMPTS.get(self.category or "", "")
            if cat_extra:
                prompt += "\n\n" + cat_extra

        try:
            result = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=self.max_report_tokens,
                timeout=180,
            )

            # If report is too short, ask the LLM to expand it
            if len(result.split()) < 400:
                logger.info(f"Final report too short ({len(result.split())} words), requesting expansion")
                self._emit(phase="writing", message="Expanding report...")
                expanded = await self._llm(
                    [
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": result},
                        {"role": "user", "content":
                            "This report is too brief. Please expand it significantly:\n"
                            "- Add detailed paragraphs for each section (not just bullet points)\n"
                            "- Include specific data, numbers, and comparisons from the evidence\n"
                            "- Explain context and significance — don't just list facts\n"
                            "- Use ## headings and ### subheadings\n"
                            "- Target at least 1000 words\n"
                            "Write the full expanded report now."
                        },
                    ],
                    temperature=0.4,
                    max_tokens=self.max_report_tokens,
                    timeout=180,
                )
                if len(expanded.split()) > len(result.split()):
                    return expanded

            return result
        except Exception as e:
            logger.error(f"Final report generation failed: {e}")
            return report  # return the evolving report as-is

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _emit(self, **kwargs):
        """Send a progress event via the callback, if one is registered."""
        if self._progress:
            try:
                self._progress(kwargs)
            except Exception:
                pass

    def _time_exceeded(self) -> bool:
        return (time.time() - self._start_time) > self.max_time

    # _strip_think_tags removed — use research_utils.strip_thinking()

    @staticmethod
    def _strip_code_block(text: str) -> str:
        """Strip markdown code-block fences (```json ... ```) if present."""
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        return text.strip()

    def _parse_json_array(self, text: str) -> List[str]:
        """Extract a JSON array of strings from LLM output."""
        text = self._strip_code_block(text)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            pass

        # Handle truncated arrays — e.g. '["query one", "query two", "query thr'
        # Repair from the LAST array start so an echoed example array earlier
        # in the reply is not harvested into the real query set.
        last_start = text.rfind('[')
        truncated = last_start != -1 and ']' not in text[last_start:]
        if truncated:
            complete_items = re.findall(r'"([^"]*)"', text[last_start:])
            if complete_items:
                logger.info(f"Repaired truncated JSON array: recovered {len(complete_items)} items")
                return complete_items

        # Greedy match to capture the full outermost array
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except json.JSONDecodeError:
                pass

        # Multiple complete arrays in one reply (e.g. the model echoes the
        # prompt's Example: [...] before the real array). The greedy match
        # above spans them all and fails to parse, so scan non-greedily and
        # keep the LAST parseable array, which is the model's actual answer.
        last_parsed = None
        for m in re.finditer(r'\[[\s\S]*?\]', text):
            try:
                parsed = json.loads(m.group())
                if isinstance(parsed, list):
                    last_parsed = parsed
            except json.JSONDecodeError:
                continue
        if last_parsed is not None:
            return [str(item) for item in last_parsed]

        # Last resort: harvest quoted strings from the first array start
        arr_start = text.find('[')
        if arr_start != -1:
            fragment = text[arr_start:]
            # Find the last complete quoted string
            complete_items = re.findall(r'"([^"]*)"', fragment)
            if complete_items:
                logger.info(f"Repaired truncated JSON array: recovered {len(complete_items)} items")
                return complete_items

        logger.warning(f"Could not parse JSON array from: {text[:200]}")
        return []

    def _parse_json_object(self, text: str) -> Optional[Dict]:
        """Extract a JSON object from LLM output."""
        text = self._strip_code_block(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Greedy match to capture the full outermost object
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None

    def _format_findings(self, findings: List[Dict]) -> str:
        """Format findings list into readable text for synthesis prompt."""
        parts = []
        for i, f in enumerate(findings, 1):
            url = f.get("url", "unknown")
            title = f.get("title", "")
            summary = f.get("summary", "")
            evidence = f.get("evidence", "")
            # Use summary if available, fall back to truncated evidence
            content = summary if summary else (evidence[:1000] if evidence else "(no content)")
            parts.append(f"**Finding {i}** — [{title}]({url})\n{content}")
        return "\n\n".join(parts)

    def _sections_block(self) -> str:
        """The report's required ## sections — the user's questions, in order.

        Sub-questions beat the category's skeleton whenever the user asked any:
        their questions already impose a structure, and a model handed two
        structures obeys both. The run that motivated this rule came back with
        a fact-check skeleton *and* a section per question, in one report.
        """
        subs = getattr(self, "subquestions", None) or []
        if subs:
            return "\n".join(f"{i}. {q}" for i, q in enumerate(subs, 1))
        sections = _CATEGORY_SECTIONS.get(getattr(self, "category", "") or "")
        if sections:
            return ("\n".join(f"{i}. {name}" for i, name in enumerate(sections, 1))
                    + "\n" + self._translated_headings_line())
        return ("(The user did not ask separate sub-questions — organise the "
                "report by theme, one ## section per major aspect.)")

    def _translated_headings_line(self) -> str:
        """Why this exists: the section names above are written in English, and
        a Spanish report carrying an English "## The Claim" is the most visible
        way a category override goes wrong."""
        name = LANGUAGE_NAMES.get(getattr(self, "report_language", "") or "en",
                                  "English")
        return (f"Those section names are written above in English. Write them "
                f"in {name} in the report — a heading in any other language is "
                f"a defect.")

    def _language_line(self) -> str:
        """An explicit instruction, because a Spanish question used to come back
        as an English report — the most visible quality gap we had."""
        code = getattr(self, "report_language", "") or "en"
        name = LANGUAGE_NAMES.get(code, "English")
        return (f"**Language:** Write the report in {name} — the language the "
                f"question was asked in. Headings, tables and callouts included.")

    def _numbered_findings(self, findings: List[Dict]) -> str:
        """This round's evidence as `[n] Title — url` + content.

        The numbers come from the run's registry rather than from this list's
        position, so a marker the model writes now still points at the same
        page in the final report several rounds later.
        """
        registry = getattr(self, "citations", None)
        parts = []
        for index, finding in enumerate(findings or [], 1):
            if registry is not None:
                number = registry.add(finding)
                if not number:
                    # No URL means no marker could ever point at it; offering it
                    # for citation would produce a source the reader cannot open.
                    continue
            else:
                number = index
            url = finding.get("url", "")
            title = finding.get("title") or url or "Untitled"
            summary = finding.get("summary") or ""
            evidence = finding.get("evidence") or ""
            content = summary or (evidence[:1000] if evidence else "(no content)")
            parts.append(f"[{number}] {title} — {url}\n{content}")
        return "\n\n".join(parts) if parts else "(No citable evidence yet.)"

    def _registered_sources_block(self, limit: int = 80) -> str:
        """Every source the run registered, for the final report prompt."""
        registry = getattr(self, "citations", None)
        entries = registry.all()[:limit] if registry is not None else []
        if not entries:
            return ("(No numbered sources were registered — do not write any "
                    "[n] markers.)")
        parts = []
        for entry in entries:
            content = (entry.get("summary")
                       or (entry.get("evidence") or "")[:1000]
                       or "(no content)")
            title = entry.get("title") or entry.get("url") or "Untitled"
            parts.append(f"[{entry['n']}] {title} — {entry.get('url', '')}\n{content}")
        return "\n\n".join(parts)

    def _finalize_citations(self, report: str) -> str:
        """Repair, check and annotate the finished report.

        Runs after the expensive part of the job, so it must not be able to
        destroy it: if anything here throws, the user still gets the report the
        model wrote and a warning saying its citations went unchecked.
        """
        try:
            final, audit, _checked = finalize_report(
                report, self.citations, getattr(self, "report_language", "en"))
            self.citation_audit = audit
            coverage = audit.coverage or {}
            logger.info(
                "Citations: %d of %d sources cited, %d of %d sentences carry one",
                len([n for n in audit.used if self.citations.source(n)]),
                len(self.citations),
                int(coverage.get("cited_sentences") or 0),
                int(coverage.get("total_sentences") or 0),
            )
            if audit.removed:
                logger.info("Removed citations pointing at nothing: %s", audit.removed)
            return final
        except Exception as e:  # noqa: BLE001
            logger.error(f"Citation check failed: {e}", exc_info=True)
            self._emit(phase="warning",
                       message=(f"Citations could not be checked "
                                f"({type(e).__name__}: {e}); the report is "
                                f"returned unverified."))
            return report

    def _fallback_report(self, question: str, findings: List[Dict]) -> str:
        """Compile gathered findings into a basic report.

        Used when the LLM synthesis step produced no report (e.g. it timed out)
        but the search rounds did collect findings — so the user still gets the
        material that was gathered instead of "No information could be gathered"
        (#1551).
        """
        return (
            f"# {question}\n\n"
            "_Automatic synthesis did not complete, so this report lists the "
            f"{len(findings)} finding(s) gathered during research._\n\n"
            f"{self._format_findings(findings)}"
        )

    def get_stats(self) -> Dict:
        """Return research statistics."""
        elapsed = time.time() - self._start_time if self._start_time else 0
        stats = {
            "Duration": f"{elapsed:.1f}s",
            "Rounds": self.round_count,
            "Queries": len(self.queries_used),
            "URLs": len(self.urls_fetched),
            "Model": self.llm_model,
        }
        if self.providers_used:
            stats["Search"] = ", ".join(self.providers_used)
        if self.category:
            stats["Category"] = self.category.capitalize()
        audit = getattr(self, "citation_audit", None)
        registry = getattr(self, "citations", None)
        if audit is not None and registry is not None:
            cited = len([n for n in audit.used if registry.source(n)])
            stats["Citations"] = f"{cited} of {len(registry)} sources"
            coverage = audit.coverage or {}
            total = int(coverage.get("total_sentences") or 0)
            if total > 0:
                share = 100.0 * int(coverage.get("cited_sentences") or 0) / total
                stats["Claims cited"] = f"{int(round(share))}%"
        return stats
