"""src/goldfish.py -- draft with an elephant, review with goldfish.

The session that wrote a document knows things the page does not say; rereading in the
same context cannot see those gaps. A *goldfish* is a reviewer that starts empty: one
bare model call that gets only the document (with numbered lines), its reading list and
one review angle -- never the conversation, the author's intentions or another
reviewer's output. The *elephant* is the side that holds the context the user gives it
(`context`: decisions, constraints, the brief); it drafts, and it judges every finding.
Faustus runs both with its own workers (`src.fresh_llm`), so a whole review happens
without anyone pasting prompts around.

Flows (`flow`):

* ``review`` -- the five review stages, run without stopping: 5 initial zoom out
  (zoom_out + inventive_catalyst on the whole document), 6 comprehension (the reader
  paraphrases the document first; no reading list), 7 critic (design_critic and
  structural against the feature-spec standard, style) on the feature part, 8 zoom
  out again, 9 implementation readiness (implementation_readiness and
  codebase_details against the implementation-readiness standard, style) on the
  implementation part. The split is the first ``## Detailed Implementation Plan`` /
  ``## Implementation Plan`` / ``## Work Breakdown`` / ``## Implementation Steps``
  heading. ``preset`` = full | feature_spec (5-8 on the whole document) |
  implementation (9 only), or ``stages`` = [5, 7].
* ``loop`` -- the sharpening loop: chosen reviewers (and standards) review, the
  elephant classifies each finding (PROPOSE FIX with exact edits, CONTEST with
  evidence the reviewer could read, ESCALATE when the evidence is outside every file
  the reviewer had, DECLINE for a nit), the fixes are applied, and new goldfish review
  again. A finding that was contested or declined is *settled* and never reopened; a
  fixed finding that comes back is reopened. A round is clean when no unsettled
  blocker or major finding is raised and every reviewer ran; two clean rounds in a
  row converge; the cap is 5 rounds; a new finding at the scope of a fix just applied
  is *churn* and stops the loop. With ``auto_apply`` false the run waits after each
  round (``awaiting_decision``) for ``resolve`` with a decision per finding.
* ``draft`` -- the elephant writes the document section by section (overview and
  problem, goals and non-goals, open questions, user journeys, technical plan,
  alternatives, detailed implementation) from ``brief`` + ``context``, then compares
  the draft with the brief and lists contradictions, then (``review_after``, default)
  runs the sharpening loop on it.
* ``brainstorm`` -- seven goldfish lenses look at a raw concept (YAGNI and reuse,
  ergonomics, architecture and cost, coupling and tech debt, readiness, framing and
  scope, false compromises) and the elephant writes a concepts brief with goals,
  non-goals, alternatives weighed and key decisions.
* ``panel`` -- several *different* models review the same packet blind and
  independently; findings are matched by location and mechanism; one raised by two or
  more models is *agreed*; a single one survives only if a fresh worker fails to
  disprove it with a proof table (premise, counter-evidence searched, before/after,
  reachability, consequence, remedy, confidence). At most ``max_findings`` (5) are
  reported, agreed first. Silence is a valid result.
* ``drift`` -- a goldfish compares a document with the source files it describes and
  lists statements the code contradicts, references that do not exist and behaviour
  the document leaves out.

A reviewer that fails is retried once and then recorded as *not run*, never as a
reviewer with no findings. Everything a run writes (document versions, one report per
stage or round, the consolidated report, findings) is kept under
DATA_DIR/goldfish/<owner>/<run id>/; with a file ``path`` the fixes go back to the file
(``write_back``), and a Cerebro note path (``brain:Notes/x.md``) goes back through the
Cerebro.

The method, reviewer roles and document standards follow google/elephant-goldfish-model
(Apache-2.0); the panel follows the cross-model review of github/lever (MIT).
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from src import constants

FLOWS = ("review", "loop", "draft", "brainstorm", "panel", "drift")
SEVERITIES = ("blocker", "major", "nit")
MAX_ROUNDS = 5
MAX_DOC_CHARS = 120_000
MAX_CONTEXT_FILE_CHARS = 24_000
MAX_CONTEXT_TOTAL = 80_000
RUN_ID_RE = re.compile(r"^gf-\d{8}-\d{6}-[0-9a-f]{6}$")
SPLIT_HEADINGS = ("detailed implementation plan", "implementation plan", "work breakdown", "implementation steps",
                  "plan de implementación detallado", "plan de implementación", "pasos de implementación")

_TASKS: Dict[str, asyncio.Task] = {}
_CANCEL: set = set()
_RESUME: Dict[str, asyncio.Event] = {}

Ask = Callable[..., Awaitable[str]]


class GoldfishError(Exception):
    def __init__(self, detail: str, error_class: str = "goldfish.bad_args"):
        super().__init__(detail)
        self.detail, self.error_class = detail, error_class


# ---------------------------------------------------------------------------
# reviewer roles and standards
# ---------------------------------------------------------------------------
GOLDFISH_CORE = (
    "You are a goldfish reviewer: you have never seen the conversation that produced this document and you know "
    "nothing about the project except what you are given. Read it as a competent newcomer would. You are read-only: "
    "you report, you never rewrite. No preamble, no praise, no filler.")

ROLES: Dict[str, Dict[str, str]] = {
    "zoom_out": {
        "label": "Zoom out",
        "review": "Judge the document from high altitude. Is there enough context to know what is being attempted and why "
                  "(if not, that alone is a blocker)? Does it solve the root problem or patch a symptom? Flag speculative "
                  "features and abstractions (YAGNI), premature optimisation (caches, queues, sharding without a measured "
                  "bottleneck), new utilities that probably already exist, high-level sections lost in code-level detail, "
                  "and couplings that block later change.",
        "brainstorm": "Contrarian and YAGNI view: does this need to exist now? Could an existing library, service or "
                      "component do 90 % of it? What is the simplest version?",
    },
    "inventive_catalyst": {
        "label": "Inventive catalyst",
        "review": "Look for false compromises: places where the document accepts a trade-off (speed vs safety, freshness vs "
                  "cost, simplicity vs flexibility) that a known pattern removes -- segmentation, extraction, preliminary "
                  "action (precompute, warm, pool), cushioning in advance (circuit breaker, stale-while-revalidate), "
                  "inversion of control, dynamic adaptation, turning a failure signal into a useful action, an "
                  "intermediary, self-service (reconciliation loops, watchdogs), cheap disposable components. For each, name "
                  "the compromise, the pattern that resolves it and the new liability it brings. Severity at most major. "
                  "If there is none, return no findings.",
        "brainstorm": "Find the contradiction in the concept (parameter X improves only if Y gets worse) and a pattern that "
                      "removes it instead of trading it off; say what the ideal self-sufficient result would look like.",
    },
    "comprehension": {
        "label": "Comprehension",
        "review": "First paraphrase, in your own words, what the document proposes and how the existing system works as the "
                  "document describes it; a section you cannot paraphrase is a blocker. Then flag project-specific terms, "
                  "components or systems used without a definition or a link, steps that assume setup the reader was never "
                  "told, and vague claims such as 'the system handles this automatically'. Do not flag general engineering "
                  "terms.",
        "brainstorm": "Ergonomics and cognitive load: is the proposed interface or workflow what a user or developer would "
                      "expect? Which errors would confuse them?",
    },
    "design_critic": {
        "label": "Design critic",
        "review": "Check that the document answers every core question of the standard. Stress-test its assumptions: "
                  "claims about behaviour, performance, data or external systems that have no justification. Name failure "
                  "modes, scalability limits, security exposure and operational complexity the design introduces but does "
                  "not mitigate.",
        "brainstorm": "Architecture and economics: load, storage, token or compute budgets, single points of failure, state "
                      "that will not scale.",
    },
    "structural": {
        "label": "Structure",
        "review": "Compare the heading structure with the sections the standard requires: missing or misnamed sections, "
                  "skipped heading levels, sections out of order, broken tables, unclosed code blocks, inconsistent lists, "
                  "malformed links.",
        "brainstorm": "Maintenance surface: what will this couple together, and what will it cost to keep alive in two years?",
    },
    "style": {
        "label": "Style",
        "review": "Flag hyperbole and marketing words, buzzwords that hide meaning, filler, subjective judgements of quality, "
                  "redundant synonyms, and claims about existing or tested behaviour stronger than the evidence given. For "
                  "each caveat or claim say DROP (no reader decision depends on it), CALIBRATE (narrow it to what was "
                  "verified), RELOCATE (move it to the step where the reader decides) or KEEP.",
        "brainstorm": "Framing: strip buzzwords, state the problem and goals in measurable terms, mark where the scope is "
                      "ambiguous enough to grow on its own.",
    },
    "implementation_readiness": {
        "label": "Implementation readiness",
        "review": "Ask: could I write exactly the right code from this text alone, on the first attempt? Flag every step "
                  "that leaves a design decision to the implementer (a schema, an API path, a dependency, error handling, "
                  "an edge case), vague steps ('update the service') instead of 'add field X to Y in file Z', and missing "
                  "required elements of the standard (file list with create/modify/delete, test plan, phases that each "
                  "leave the code buildable).",
        "brainstorm": "What would have to be decided before anyone could start building this?",
    },
    "codebase_details": {
        "label": "Codebase fit",
        "review": "Compare the document with the source files in the reading list. Blockers: statements about current "
                  "behaviour that the code contradicts, and files, functions, routes or tables cited as existing that do "
                  "not exist. Also flag logic, constants, environment variables, edge cases and failure handling present "
                  "in the code but missing from the document. Files the plan says it will create are not findings; a "
                  "file list without create/modify/delete marks is one major finding.",
        "brainstorm": "Which existing code would this reuse, change or duplicate?",
    },
}

STANDARDS: Dict[str, str] = {
    "feature-spec": "Feature specification standard: a newcomer can understand the feature, the motivation and the "
                    "trade-offs. It must state the problem, goals and non-goals, the proposed architecture (components, "
                    "data flow), alternatives considered with concrete reasons for rejecting each (not a single path), and "
                    "risks and limits with mitigations (failure modes, security, scale).",
    "implementation-readiness": "Implementation readiness standard: a newcomer could execute the plan correctly on the "
                                "first attempt without making any design decision (zero-decision rule). It must list every "
                                "file to create, modify or delete, the precise change per file, phases that each leave the "
                                "code buildable and testable, and the test cases (happy path and edge cases).",
    "zoom-out": "Zoom-out standard: the objective is clear; the design solves the root problem in the simplest way, "
                "reuses what exists, avoids speculative features and premature optimisation, keeps high-level sections "
                "at the level of interfaces and data flow, and places logic in the right layer.",
    "heilmeier": "Heilmeier standard: objectives with no jargon; how it is done today and its limits; what is new and why "
                 "it should work; who cares and what changes if it succeeds; risks and payoffs; cost; timeline; mid-term "
                 "and final checks of success.",
    "re-implementation": "Re-implementation standard: a newcomer could write functionally identical code from the "
                         "document alone: external contracts, behavioural rules, data shapes, side effects, configuration, "
                         "limits and errors are explicit (not syntax or private helpers).",
}

STAGES: Dict[int, Dict[str, Any]] = {
    5: {"name": "Initial zoom out", "reviewers": [("zoom_out", "zoom-out"), ("inventive_catalyst", None)], "range": "full"},
    6: {"name": "Comprehension", "reviewers": [("comprehension", None)], "range": "full", "no_context": True},
    7: {"name": "Critic review", "reviewers": [("design_critic", "feature-spec"), ("structural", "feature-spec"), ("style", None)],
        "range": "feature"},
    8: {"name": "Intermediate zoom out", "reviewers": [("zoom_out", "zoom-out")], "range": "full"},
    9: {"name": "Implementation readiness", "reviewers": [("implementation_readiness", "implementation-readiness"),
                                                          ("codebase_details", "implementation-readiness"), ("style", None)],
        "range": "detailed"},
}
PRESETS = {"full": [5, 6, 7, 8, 9], "feature_spec": [5, 6, 7, 8], "implementation": [9]}
DEFAULT_LOOP_REVIEWERS = ["comprehension", "design_critic", "zoom_out"]

FINDINGS_FORMAT = (
    "Reply with ONLY a JSON object: {\"findings\": [{\"title\": short name, \"severity\": \"blocker\"|\"major\"|\"nit\", "
    "\"lines\": \"12-18\" or a section heading, \"description\": what is wrong, \"rationale\": why it matters to a reader "
    "who has nothing else and what would fix it}]}. Blocker: the document cannot be approved or followed. Major: a reader "
    "will get it wrong or a real risk is unaddressed. Nit: wording or polish. Report at most {max} findings, the most "
    "important first. No findings: {\"findings\": []}.")

PARAPHRASE_FORMAT = (
    "Reply with ONLY a JSON object: {\"paraphrase_proposal\": what the document proposes, in your words, "
    "\"paraphrase_system\": how the existing system works as the document describes it, \"findings\": [...]} where each "
    "finding is {\"title\", \"severity\": \"blocker\"|\"major\"|\"nit\", \"lines\", \"description\", \"rationale\"}. At "
    "most {max} findings.")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "")).strip("._")[:80] or "_"


def _root(owner: str) -> Path:
    return Path(constants.DATA_DIR) / "goldfish" / _safe(owner or "_")


def _run_dir(owner: str, run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id or ""):
        raise GoldfishError(f"no such run: {run_id}", "goldfish.not_found")
    return _root(owner) / run_id


def _save(owner: str, run: Dict[str, Any]) -> None:
    d = _run_dir(owner, run["id"])
    d.mkdir(parents=True, exist_ok=True)
    run["heartbeat"] = time.time()
    tmp = d / "run.json.tmp"
    public = {k: v for k, v in run.items() if not str(k).startswith("_")}
    tmp.write_text(json.dumps(public, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, d / "run.json")


def _write(owner: str, run_id: str, name: str, text: str) -> str:
    d = _run_dir(owner, run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return name


def get_run(owner: str, run_id: str) -> Dict[str, Any]:
    p = _run_dir(owner, run_id) / "run.json"
    if not p.is_file():
        raise GoldfishError(f"no such run: {run_id}", "goldfish.not_found")
    run = json.loads(p.read_text(encoding="utf-8"))
    if run.get("status") in ("queued", "running") and run_id not in _TASKS and time.time() - float(run.get("heartbeat") or 0) > 900:
        run["status"] = "interrupted"
    return run


def read_artifact(owner: str, run_id: str, name: str) -> str:
    if not re.fullmatch(r"[\w.-]+\.(md|json|txt)", name or ""):
        raise GoldfishError("bad artifact name")
    p = _run_dir(owner, run_id) / name
    if not p.is_file():
        raise GoldfishError(f"no artifact {name}", "goldfish.not_found")
    return p.read_text(encoding="utf-8")


def list_runs(owner: str, limit: int = 100) -> List[Dict[str, Any]]:
    root = _root(owner)
    rows = []
    if root.is_dir():
        for d in root.iterdir():
            p = d / "run.json"
            if not p.is_file():
                continue
            try:
                run = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({k: run.get(k) for k in ("id", "flow", "title", "status", "phase", "created", "finished",
                                                 "counts", "round", "error", "target")})
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)
    return rows[:limit]


def delete_run(owner: str, run_id: str) -> Dict[str, Any]:
    get_run(owner, run_id)
    if run_id in _TASKS:
        raise GoldfishError("the run is still going; cancel it first")
    shutil.rmtree(_run_dir(owner, run_id), ignore_errors=True)
    return {"deleted": run_id}


def cancel_run(owner: str, run_id: str) -> Dict[str, Any]:
    run = get_run(owner, run_id)
    _CANCEL.add(run_id)
    ev = _RESUME.get(run_id)
    if ev:
        ev.set()
    if run_id not in _TASKS and run.get("status") in ("queued", "running", "awaiting_decision"):
        run["status"] = "cancelled"
        _save(owner, run)
    return {"id": run_id, "cancel_requested": True}


# ---------------------------------------------------------------------------
# documents and reading lists
# ---------------------------------------------------------------------------
def _read_target(owner: str, target: str) -> Tuple[str, str]:
    """(text, kind) for a file path or a `brain:` note path."""
    if target.startswith("brain:"):
        from src.brain import notes
        return notes.read_note(owner, target[len("brain:"):])["content"], "brain"
    p = Path(target)
    if not p.is_file():
        raise GoldfishError(f"the document does not exist: {target}")
    return p.read_text(encoding="utf-8", errors="replace"), "file"


def _write_target(owner: str, target: str, text: str) -> None:
    if target.startswith("brain:"):
        from src.brain import notes
        notes.write_note(owner, target[len("brain:"):], text)
        return
    Path(target).write_text(text, encoding="utf-8", newline="")


def numbered(text: str, start: int = 1, end: Optional[int] = None) -> str:
    lines = text.splitlines()
    end = len(lines) if end is None else min(end, len(lines))
    width = len(str(end or 1))
    return "\n".join(f"{i:>{width}} | {lines[i - 1]}" for i in range(max(1, start), end + 1))


def split_ranges(text: str) -> Dict[str, Optional[Tuple[int, int]]]:
    lines = text.splitlines()
    n = max(1, len(lines))
    for i, line in enumerate(lines, start=1):
        m = re.match(r"^#{1,3}\s+(.+?)\s*#*\s*$", line)
        if m and m.group(1).strip().lower() in SPLIT_HEADINGS and i > 1:
            return {"full": (1, n), "feature": (1, i - 1), "detailed": (i, n), "split_line": i}  # type: ignore[dict-item]
    return {"full": (1, n), "feature": (1, n), "detailed": None, "split_line": None}  # type: ignore[dict-item]


def linked_files(text: str, doc_path: Optional[str]) -> List[str]:
    """Relative markdown links of the document that resolve to files inside its folder tree."""
    if not doc_path or doc_path.startswith("brain:"):
        return []
    base = Path(doc_path).resolve().parent
    out = []
    for m in re.finditer(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text):
        href = m.group(1).split("#", 1)[0]
        if not href or re.match(r"^[a-z]+:", href):
            continue
        p = (base / href).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            continue
        if p.is_file() and str(p) not in out:
            out.append(str(p))
    return out


def reading_list(owner: str, paths: List[str]) -> Tuple[str, List[str]]:
    blocks, used, total = [], [], 0
    for raw in paths:
        path = str(raw).strip()
        if not path:
            continue
        try:
            if path.startswith("brain:"):
                text = _read_target(owner, path)[0]
            else:
                p = Path(path)
                if p.is_dir():
                    names = sorted(x.name for x in p.iterdir())[:200]
                    text = "Folder listing:\n" + "\n".join(names)
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - an unreadable file is skipped and named
            blocks.append(f"=== {path} (could not be read) ===")
            continue
        text = text[:MAX_CONTEXT_FILE_CHARS]
        if total + len(text) > MAX_CONTEXT_TOTAL:
            blocks.append(f"=== {path} (left out: the reading list is full) ===")
            continue
        total += len(text)
        used.append(path)
        blocks.append(f"=== {path} ===\n{text}")
    return "\n\n".join(blocks), used


# ---------------------------------------------------------------------------
# model calls
# ---------------------------------------------------------------------------
async def _default_ask(owner: str, run: Mapping[str, Any], messages: List[Dict[str, Any]], *, role: str = "goldfish",
                       model: str = "", tag: str = "") -> str:
    from src import fresh_llm
    if not model:
        model = run.get("elephant_model") if role == "elephant" else run.get("model")
    text, _meta = await fresh_llm.ask(owner, messages, model=model or "", run_id=f"{run['id']}-{tag}", timeout=600,
                                      parallel=int(run.get("parallel") or 3))
    return text


def _norm_sev(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s.startswith(("block", "p1", "crit", "🔴")):
        return "blocker"
    if s.startswith(("maj", "p2", "high", "🟡")):
        return "major"
    return "nit"


def _norm_findings(data: Any, reviewer: str, prefix: str) -> List[Dict[str, Any]]:
    items = data.get("findings") if isinstance(data, Mapping) else data
    out = []
    for i, f in enumerate(items or []):
        if not isinstance(f, Mapping):
            continue
        desc = str(f.get("description") or f.get("discrepancy") or f.get("issue") or f.get("text") or "").strip()
        title = str(f.get("title") or f.get("name") or desc[:60]).strip()
        if not (title or desc):
            continue
        out.append({"id": f"{prefix}-{i + 1}", "reviewer": reviewer, "title": title[:160],
                    "severity": _norm_sev(f.get("severity")), "lines": str(f.get("lines") or f.get("scope") or f.get("section") or "")[:80],
                    "description": desc[:1500], "rationale": str(f.get("rationale") or f.get("recommendation") or "")[:1500]})
    return out


async def _goldfish(owner: str, run: Dict[str, Any], ask: Ask, role: str, standard: Optional[str], doc_block: str,
                    context_block: str, prefix: str, *, mode: str = "review", paraphrase: bool = False,
                    concept: str = "", model: str = "", max_findings: int = 8) -> Dict[str, Any]:
    """One fresh reviewer; retried once; {status, findings, paraphrase?, raw}."""
    spec = ROLES[role]
    sys_prompt = GOLDFISH_CORE + "\n\nYour angle: " + spec["label"] + ". " + (spec["brainstorm"] if mode == "brainstorm" else spec["review"])
    if standard:
        sys_prompt += "\n\nStandard to apply: " + STANDARDS[standard]
    fmt = (PARAPHRASE_FORMAT if paraphrase else FINDINGS_FORMAT).replace("{max}", str(max_findings))
    if mode == "brainstorm":
        user = f"Feature concept to examine:\n{concept}\n\n" + (f"Reading list:\n{context_block}\n\n" if context_block else "") + fmt
    else:
        user = (f"Document to review (line numbers on the left):\n{doc_block}\n\n"
                + (f"Reading list (source material the document relies on):\n{context_block}\n\n" if context_block else "")
                + fmt)
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
    from src import fresh_llm
    last_error = ""
    for attempt in (1, 2):
        try:
            text = await ask(owner, run, messages, role="goldfish", model=model, tag=f"{prefix}-a{attempt}")
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            continue
        data = fresh_llm.parse_json(text)
        if isinstance(data, (Mapping, list)):
            res = {"status": "completed", "findings": _norm_findings(data, role, prefix), "raw": text[:6000]}
            if paraphrase and isinstance(data, Mapping):
                res["paraphrase"] = {"proposal": str(data.get("paraphrase_proposal") or "")[:3000],
                                     "system": str(data.get("paraphrase_system") or "")[:3000]}
            return res
        last_error = "the reply was not the JSON asked for"
        messages = messages + [{"role": "assistant", "content": text[:4000]},
                               {"role": "user", "content": "Reply again with ONLY the JSON object, nothing else."}]
    return {"status": "not run", "findings": [], "error": last_error}


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-záéíóúñü0-9]{4,}", (s or "").lower())}


def _line_span(lines: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"(\d+)\s*(?:[-–]\s*(\d+))?", lines or "")
    if not m:
        return None
    a = int(m.group(1))
    return a, int(m.group(2) or a)


def same_issue(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Same affected scope (overlapping lines or same section) and a similar issue."""
    sa, sb = _line_span(a.get("lines", "")), _line_span(b.get("lines", ""))
    scope = (sa and sb and sa[0] <= sb[1] + 2 and sb[0] <= sa[1] + 2) or \
            (not sa and not sb and str(a.get("lines", "")).strip().lower() == str(b.get("lines", "")).strip().lower())
    ta, tb = _tokens(a.get("title", "") + " " + a.get("description", "")), _tokens(b.get("title", "") + " " + b.get("description", ""))
    sim = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return bool(scope and sim >= 0.3) or sim >= 0.6


def consolidate(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge duplicates across reviewers: the most severe wording leads, the others are traced."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    groups: List[Dict[str, Any]] = []
    for f in sorted(findings, key=lambda x: order[x["severity"]]):
        for g in groups:
            if same_issue(g, f):
                g["sources"].append(f["id"])
                g["reviewers"] = sorted(set(g["reviewers"]) | {f["reviewer"]})
                break
        else:
            groups.append({**f, "sources": [f["id"]], "reviewers": [f["reviewer"]]})
    for i, g in enumerate(groups, start=1):
        g["cid"] = f"C{i}"
    return groups


def _report(title: str, target: str, statuses: Mapping[str, str], findings: List[Dict[str, Any]],
            extra: str = "", decisions: Optional[Mapping[str, Mapping[str, Any]]] = None) -> str:
    out = [f"# {title}", "", f"**Target:** `{target}`", "", "## Reviewer status", "", "| Reviewer | Status |", "| --- | --- |"]
    out += [f"| `{r}` | {s} |" for r, s in statuses.items()]
    if extra:
        out += ["", extra]
    out += ["", "## Findings (blockers, then major, then nits)", ""]
    if not findings:
        out.append("No findings.")
    for f in findings:
        out += [f"### {f.get('cid', f['id'])}. {f['title']} — {f['severity']}",
                f"* **Reviewer:** {', '.join('`' + r + '`' for r in f.get('reviewers') or [f['reviewer']])}",
                f"* **Affected scope:** {f.get('lines') or '-'}",
                f"* **Description:** {f['description']}"]
        if f.get("rationale"):
            out.append(f"* **Rationale:** {f['rationale']}")
        d = (decisions or {}).get(f.get("cid") or f["id"])
        if d:
            out.append(f"* **Elephant:** {d.get('category')} — {d.get('justification', '')}")
            for e in d.get("edits") or []:
                out.append(f"  * replace `{str(e.get('find'))[:120]}` → `{str(e.get('replace'))[:120]}`"
                           + (f" ({e['status']})" if e.get("status") else ""))
        if f.get("sources") and len(f["sources"]) > 1:
            out.append(f"* **Raw findings merged:** {', '.join(f['sources'])}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the elephant: classify and fix
# ---------------------------------------------------------------------------
RESOLVE_SYSTEM = (
    "You are the elephant: the author's side, holding the context below. Fresh reviewers who never saw that context "
    "reviewed the document. For each finding choose exactly one category:\n"
    "- PROPOSE FIX: the critique is valid; give exact edits as find/replace pairs (find = text copied exactly from the "
    "document, unique in it; replace = the new text). Add the missing fact rather than rewording around it.\n"
    "- CONTEST: the critique is wrong and you can cite evidence the reviewer could read (a line of the document or a "
    "file of its reading list). 'This was intentional' is not evidence.\n"
    "- ESCALATE: the critique is wrong but the evidence is only in your context (a verbal decision, a convention); "
    "the user must decide whether to write it down.\n"
    "- DECLINE: a nit not worth fixing.\n"
    "Never contest a comprehension gap: if a fresh reader could not follow it, the document is missing context. Never "
    "change a severity. You wrote the document: wanting to contest is not evidence.\n"
    "Reply with ONLY JSON: {\"decisions\": [{\"id\": finding id, \"category\": \"PROPOSE FIX\"|\"CONTEST\"|\"ESCALATE\"|"
    "\"DECLINE\", \"justification\": one or two sentences, \"edits\": [{\"find\": ..., \"replace\": ...}]}]}")


def _norm_category(c: Any) -> str:
    s = str(c or "").upper().replace("_", " ")
    for cat in ("PROPOSE FIX", "CONTEST", "ESCALATE", "DECLINE"):
        if cat.split()[0] in s:
            return cat
    return "PROPOSE FIX"


async def elephant_resolve(owner: str, run: Dict[str, Any], ask: Ask, doc: str, findings: List[Dict[str, Any]],
                           tag: str) -> Dict[str, Dict[str, Any]]:
    from src import fresh_llm
    listing = "\n".join(f"[{f['cid']}] ({f['severity']}, lines {f.get('lines') or '-'}) {f['title']}: {f['description']} "
                        f"-- {f.get('rationale', '')}" for f in findings)
    ctx = run.get("context") or "(no extra context was given)"
    user = (f"Your context (the reviewers never saw it):\n{ctx}\n\nDocument:\n{doc[:MAX_DOC_CHARS]}\n\nFindings:\n{listing}")
    decisions: Dict[str, Dict[str, Any]] = {}
    for attempt in (1, 2):
        text = await ask(owner, run, [{"role": "system", "content": RESOLVE_SYSTEM}, {"role": "user", "content": user}],
                         role="elephant", tag=f"{tag}-r{attempt}")
        data = fresh_llm.parse_json(text)
        items = data.get("decisions") if isinstance(data, Mapping) else data if isinstance(data, list) else None
        if items:
            for d in items:
                if not isinstance(d, Mapping):
                    continue
                fid = str(d.get("id") or "").strip("[] ")
                edits = [{"find": str(e.get("find") or ""), "replace": str(e.get("replace") or "")}
                         for e in (d.get("edits") or []) if isinstance(e, Mapping) and e.get("find")]
                decisions[fid] = {"category": _norm_category(d.get("category")), "justification": str(d.get("justification") or "")[:800],
                                  "edits": edits}
            break
    for f in findings:
        if f["cid"] not in decisions:
            decisions[f["cid"]] = {"category": "ESCALATE", "justification": "the elephant gave no decision for this finding",
                                   "edits": []}
        elif decisions[f["cid"]]["category"] == "PROPOSE FIX" and not decisions[f["cid"]]["edits"]:
            decisions[f["cid"]]["category"] = "ESCALATE"
            decisions[f["cid"]]["justification"] += " (no exact edit was proposed, so the user decides)"
    return decisions


def apply_edits(doc: str, decisions: Mapping[str, Dict[str, Any]], accepted: Optional[set] = None) -> Tuple[str, List[str]]:
    applied = []
    for fid, d in decisions.items():
        if d.get("category") != "PROPOSE FIX" or (accepted is not None and fid not in accepted):
            continue
        for e in d.get("edits") or []:
            find = e.get("find") or ""
            n = doc.count(find)
            if n == 1:
                doc = doc.replace(find, e.get("replace") or "")
                e["status"] = "applied"
                if fid not in applied:
                    applied.append(fid)
            else:
                stripped = find.strip()
                if stripped and doc.count(stripped) == 1:
                    doc = doc.replace(stripped, (e.get("replace") or "").strip())
                    e["status"] = "applied"
                    if fid not in applied:
                        applied.append(fid)
                else:
                    e["status"] = "not found" if n == 0 else f"found {n} times"
    return doc, applied


# ---------------------------------------------------------------------------
# flows
# ---------------------------------------------------------------------------
def _range_block(doc: str, ranges: Mapping[str, Any], which: str) -> Optional[str]:
    r = ranges.get(which)
    if not r:
        return None
    return numbered(doc, r[0], r[1])


async def _stage(owner: str, run: Dict[str, Any], ask: Ask, doc: str, ranges: Mapping[str, Any], context_block: str,
                 stage_no: int, reviewers: List[Tuple[str, Optional[str]]], range_name: str, no_context: bool,
                 label: str) -> Dict[str, Any]:
    block = _range_block(doc, ranges, range_name)
    if block is None:
        return {"stage": stage_no, "name": label, "skipped": "the document has no implementation section"}
    instances = 2 if run.get("double") else 1
    jobs = []
    for role, standard in reviewers:
        for inst in range(instances):
            suffix = "" if instances == 1 else ("A" if inst == 0 else "B")
            jobs.append((f"{role}{suffix}", role, standard))
    results = await asyncio.gather(*(
        _goldfish(owner, run, ask, role, standard, block, "" if no_context else context_block,
                  f"s{stage_no}-{name}", paraphrase=(role == "comprehension"), max_findings=int(run.get("max_findings") or 8))
        for name, role, standard in jobs))
    statuses = {name: res["status"] for (name, _r, _s), res in zip(jobs, results)}
    raw = [f for res in results for f in res["findings"]]
    merged = consolidate(raw)
    extra = ""
    para = next((res.get("paraphrase") for res in results if res.get("paraphrase")), None)
    if para:
        extra = ("## What the document proposes (in the reader's words)\n\n" + para["proposal"]
                 + "\n\n## How the existing system works (as the document describes it)\n\n" + para["system"])
    return {"stage": stage_no, "name": label, "range": ranges.get(range_name), "statuses": statuses, "raw": raw,
            "findings": merged, "paraphrase": para, "report": _report(f"Stage {stage_no}: {label}", run["target_label"],
                                                                      statuses, merged, extra)}


async def flow_review(owner: str, run: Dict[str, Any], ask: Ask, log: Callable[..., None]) -> None:
    doc = run["_doc"]
    ranges = split_ranges(doc)
    run["ranges"] = {k: v for k, v in ranges.items()}
    stages = [int(s) for s in (run.get("stages") or PRESETS.get(run.get("preset") or "full", PRESETS["full"]))]
    if run.get("preset") == "feature_spec":
        ranges = {**ranges, "feature": ranges["full"], "detailed": None}
    context_block = run["_context_block"]
    all_raw, stage_rows = [], []
    for s in stages:
        if s not in STAGES or _cancelled(run):
            continue
        st = STAGES[s]
        log(f"stage {s}: {st['name']} ({', '.join(r for r, _ in st['reviewers'])})", f"stage {s}")
        res = await _stage(owner, run, ask, doc, ranges, context_block, s, st["reviewers"], st["range"],
                           bool(st.get("no_context")), st["name"])
        if res.get("skipped"):
            log(f"stage {s} skipped: {res['skipped']}")
            stage_rows.append({"stage": s, "name": st["name"], "skipped": res["skipped"]})
            continue
        res["file"] = _write(owner, run["id"], f"stage{s}_report.md", res.pop("report"))
        all_raw += res["raw"]
        stage_rows.append({k: res.get(k) for k in ("stage", "name", "range", "statuses", "file")} |
                          {"counts": _counts(res["findings"]), "findings": res["findings"], "paraphrase": res.get("paraphrase")})
        run["stages_done"] = stage_rows
        log(f"stage {s}: {_count_text(res['findings'])}")
    merged = consolidate(all_raw)
    statuses = {f"stage {r['stage']} {k}": v for r in stage_rows for k, v in (r.get("statuses") or {}).items()}
    trace = "## Traceability\n\n| Action item | Raw findings |\n| --- | --- |\n" + "\n".join(
        f"| {f['cid']} | {', '.join(f['sources'])} |" for f in merged)
    run["consolidated_file"] = _write(owner, run["id"], "consolidated_review_report.md",
                                      _report("Consolidated goldfish review", run["target_label"], statuses, merged, trace))
    run["findings"] = merged
    run["counts"] = _counts(merged)
    if run.get("resolve"):
        log("the elephant classifies the findings", "resolving")
        decisions = await elephant_resolve(owner, run, ask, doc, merged, "final")
        run["decisions"] = decisions
        await _apply_and_store(owner, run, decisions, accepted=None if run.get("auto_apply") else set())


def _counts(findings: List[Mapping[str, Any]]) -> Dict[str, int]:
    return {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES}


def _count_text(findings: List[Mapping[str, Any]]) -> str:
    c = _counts(findings)
    return f"{c['blocker']} blockers, {c['major']} major, {c['nit']} nits"


async def _apply_and_store(owner: str, run: Dict[str, Any], decisions: Dict[str, Dict[str, Any]],
                           accepted: Optional[set]) -> List[str]:
    doc = run["_doc"]
    new, applied = apply_edits(doc, decisions, accepted)
    if applied and new != doc:
        run["version"] = int(run.get("version") or 0) + 1
        _write(owner, run["id"], f"doc_v{run['version']}.md", new)
        run["_doc"] = new
        if run.get("write_back") and run.get("target"):
            await asyncio.to_thread(_write_target, owner, run["target"], new)
            run["written_back"] = True
    return applied


def _cancelled(run: Mapping[str, Any]) -> bool:
    return run["id"] in _CANCEL


async def _wait_decision(owner: str, run: Dict[str, Any], log: Callable[..., None]) -> Optional[Dict[str, Any]]:
    """Pause until `resolve` is called; returns its payload (or None when cancelled)."""
    ev = asyncio.Event()
    _RESUME[run["id"]] = ev
    run["status"] = "awaiting_decision"
    log("waiting for your decision on each finding", "awaiting decision")
    while not ev.is_set():
        try:
            await asyncio.wait_for(ev.wait(), 30)
        except asyncio.TimeoutError:
            fresh = get_run(owner, run["id"])
            if fresh.get("pending_resolution"):
                run["pending_resolution"] = fresh["pending_resolution"]
                break
            run["heartbeat"] = time.time()
            _save(owner, run)
    _RESUME.pop(run["id"], None)
    if _cancelled(run):
        return None
    fresh = get_run(owner, run["id"])
    payload = fresh.get("pending_resolution") or run.get("pending_resolution") or {}
    run["pending_resolution"] = None
    run["status"] = "running"
    return payload


async def flow_loop(owner: str, run: Dict[str, Any], ask: Ask, log: Callable[..., None]) -> None:
    reviewers = [r for r in (run.get("reviewers") or DEFAULT_LOOP_REVIEWERS) if r in ROLES]
    standards = run.get("standards") or {}
    cap = max(1, min(MAX_ROUNDS, int(run.get("max_rounds") or MAX_ROUNDS)))
    settled: List[Dict[str, Any]] = []
    fixed: List[Dict[str, Any]] = []
    clean_streak = 0
    rounds: List[Dict[str, Any]] = run.setdefault("rounds", [])
    run["outcome"] = None
    last_fix_scopes: List[Dict[str, Any]] = []
    for rnd in range(1, cap + 1):
        if _cancelled(run):
            return
        run["round"] = rnd
        doc = run["_doc"]
        block = numbered(doc)
        log(f"round {rnd}: {len(reviewers)} new goldfish ({', '.join(reviewers)})", f"round {rnd}")
        results = await asyncio.gather(*(
            _goldfish(owner, run, ask, role, standards.get(role), block, run["_context_block"], f"r{rnd}-{role}",
                      max_findings=int(run.get("max_findings") or 8))
            for role in reviewers))
        statuses = {role: res["status"] for role, res in zip(reviewers, results)}
        merged = consolidate([f for res in results for f in res["findings"]])
        for f in merged:
            f["cid"] = f"R{rnd}-{f['cid']}"
            f["state"] = "new"
            if any(same_issue(f, s) for s in settled):
                f["state"] = "settled"
            elif any(same_issue(f, x) for x in fixed):
                f["state"] = "reopened"
        unsettled = [f for f in merged if f["state"] != "settled"]
        clean = all(s == "completed" for s in statuses.values()) and not any(
            f["severity"] in ("blocker", "major") for f in unsettled)
        churn = [f for f in unsettled if f["severity"] in ("blocker", "major") and any(same_issue(f, x) for x in last_fix_scopes)]
        row: Dict[str, Any] = {"round": rnd, "statuses": statuses, "findings": merged, "clean": clean,
                               "counts": _counts(unsettled)}
        rounds.append(row)
        log(f"round {rnd}: {_count_text(unsettled)} not yet settled; {'clean' if clean else 'not clean'}")
        decisions: Dict[str, Dict[str, Any]] = {}
        if unsettled:
            log(f"round {rnd}: the elephant classifies {len(unsettled)} findings", "resolving")
            decisions = await elephant_resolve(owner, run, ask, doc, unsettled, f"r{rnd}")
        row["decisions"] = decisions
        accepted: Optional[set] = None
        if decisions and not run.get("auto_apply"):
            _save_round_report(owner, run, row)
            payload = await _wait_decision(owner, run, log)
            if payload is None:
                return
            if payload.get("mode") == "continue":
                accepted = set()
            elif payload.get("mode") == "auto_accept":
                accepted = None
            else:
                per = payload.get("decisions") or {}
                accepted = {fid for fid, v in per.items() if v in ("accept", "overrule")}
                for fid, v in per.items():
                    if v == "overrule" and fid in decisions and decisions[fid]["category"] != "PROPOSE FIX":
                        decisions[fid]["category"] = "PROPOSE FIX"
                    if v == "skip" and fid in decisions:
                        decisions[fid]["user"] = "skipped"
                for fid, v in (payload.get("notes") or {}).items():
                    if fid in decisions:
                        decisions[fid]["user_note"] = str(v)[:500]
        applied = await _apply_and_store(owner, run, decisions, accepted)
        row["applied"] = applied
        last_fix_scopes = [f for f in unsettled if f["cid"] in applied]
        for f in unsettled:
            d = decisions.get(f["cid"]) or {}
            if f["cid"] in applied:
                fixed.append(f)
            elif d.get("category") in ("CONTEST", "DECLINE") or d.get("user") == "skipped":
                settled.append(f)
        row["file"] = _save_round_report(owner, run, row)
        log(f"round {rnd}: {len(applied)} fixes applied")
        if churn and rnd > 1:
            run["outcome"] = "churn"
            log(f"stopped: {len(churn)} new findings sit where the last fixes were (churn)", "churn")
            break
        clean_streak = clean_streak + 1 if clean else 0
        if clean_streak >= 2:
            run["outcome"] = "converged"
            log("converged: two clean rounds in a row", "converged")
            break
        if rnd == cap:
            run["outcome"] = "cap"
            log(f"stopped at the cap of {cap} rounds", "cap")
    open_findings = [f for r in rounds[-1:] for f in r["findings"] if f.get("state") != "settled"
                     and f["cid"] not in (r.get("applied") or [])]
    run["findings"] = open_findings
    run["counts"] = _counts(open_findings)
    run["settled"] = len(settled)
    run["fixed"] = len(fixed)
    summary = (f"Rounds: {len(rounds)} ({run.get('outcome')}). Fixed: {len(fixed)}. Settled (contested or declined): "
               f"{len(settled)}. Open after the last round: {_count_text(open_findings)}.")
    run["consolidated_file"] = _write(owner, run["id"], "goldfish_review_report.md",
                                      _report("Goldfish review loop", run["target_label"],
                                              {f"round {r['round']} {k}": v for r in rounds for k, v in r["statuses"].items()},
                                              open_findings, summary,
                                              rounds[-1].get("decisions") if rounds else None))


def _save_round_report(owner: str, run: Dict[str, Any], row: Dict[str, Any]) -> str:
    return _write(owner, run["id"], f"round{row['round']}_report.md",
                  _report(f"Round {row['round']}", run["target_label"], row["statuses"], row["findings"],
                          f"Clean round: {'yes' if row['clean'] else 'no'}.", row.get("decisions")))


DRAFT_SECTIONS = [
    ("Overview and problem", "## Overview\n## The Problem", "Summarise the feature and the problem it solves; why, not how."),
    ("Goals and non-goals", "## Goals\n## Non-Goals", "Specific, checkable goals; explicit non-goals that stop scope creep."),
    ("Open questions", "## Open Questions", "Unresolved questions and unknowns; keep answered ones out."),
    ("User journeys", "## Critical User Journeys", "Two or three journeys, each Given / When / Then."),
    ("Technical plan", "## Technical Plan", "Components, data flow, interfaces and API changes in prose, with a text diagram."),
    ("Alternatives", "## Alternatives Considered", "Each rejected alternative with its concrete technical reason "
                                                   "('Rejected by author: …')."),
    ("Detailed implementation", "## Detailed Implementation Plan",
     "One list of every file to create, modify or delete with what changes and why, phases that keep the code "
     "buildable, and the test cases. No real code; pseudocode, schemas and signatures are fine."),
]

DRAFT_SYSTEM = (
    "You are the elephant: you hold the project context and draft a design document for people who were not in the "
    "conversation. Plain, direct English (or the language of the brief); no promotional adjectives, no filler; explain "
    "mechanisms directly. Where a decision has more than one defensible answer and the context does not settle it, write "
    "the options with their trade-offs under Open Questions instead of picking one. Reply with only the Markdown of the "
    "requested section(s).")


async def flow_draft(owner: str, run: Dict[str, Any], ask: Ask, log: Callable[..., None]) -> None:
    brief = run.get("brief") or ""
    ctx = run.get("context") or ""
    title = run.get("title") or "Design"
    doc = f"# {title}\n\n*   **Status:** Draft\n*   **Date:** {time.strftime('%Y-%m-%d')}\n\n"
    for name, headings, guide in DRAFT_SECTIONS:
        if _cancelled(run):
            return
        log(f"drafting: {name}", "drafting")
        user = (f"Brief:\n{brief}\n\nContext:\n{ctx or '(none)'}\n\nReading list:\n{run['_context_block'] or '(none)'}\n\n"
                f"Document so far:\n{doc}\n\nWrite the next section(s) with these headings:\n{headings}\nGuidance: {guide}")
        text = await ask(owner, run, [{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}],
                         role="elephant", tag=f"d-{name[:8]}")
        text = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", text.strip())
        doc += text.strip() + "\n\n"
    run["_doc"] = doc
    run["version"] = 0
    _write(owner, run["id"], "doc_v0.md", doc)
    if brief.strip():
        log("comparing the draft with the brief", "reconciling")
        rec = await ask(owner, run, [{"role": "system", "content":
                                      "Compare a brief with the design drafted from it. List every CONTRADICTION (the brief "
                                      "decided a question and the draft decides it differently), quoting both, one line each; "
                                      "then ADDITIONS that change the architecture, an external interface or a dependency. If "
                                      "nothing diverges, reply exactly 'No divergence.'"},
                                     {"role": "user", "content": f"Brief:\n{brief}\n\nDraft:\n{doc}"}], role="elephant", tag="rec")
        run["reconciliation_file"] = _write(owner, run["id"], "reconciliation.md", "# Draft vs brief\n\n" + rec.strip() + "\n")
        run["reconciliation"] = rec.strip()[:4000]
    if run.get("target") and run.get("write_back"):
        await asyncio.to_thread(_write_target, owner, run["target"], doc)
        run["written_back"] = True
    if run.get("review_after", True):
        run["reviewers"] = run.get("reviewers") or ["comprehension", "design_critic", "implementation_readiness", "zoom_out"]
        run["standards"] = run.get("standards") or {"design_critic": "feature-spec",
                                                     "implementation_readiness": "implementation-readiness",
                                                     "zoom_out": "zoom-out"}
        await flow_loop(owner, run, ask, log)


BRAINSTORM_SYSTEM = (
    "You are the elephant. Several fresh reviewers examined a feature concept, each from one angle. Write a concepts "
    "brief in Markdown with exactly these sections: '# <feature name>', '## Overview', '## Analysis' (the reviewers' "
    "points, deduplicated, risks first), '## Goals vs. Non-Goals', '## Alternatives Weighed' (each with why it was kept "
    "or rejected), '## Key Decisions & Staging for Next Steps' (decisions the user must take, as questions with options "
    "and trade-offs, never picking for them). Plain language, no praise.")


async def flow_brainstorm(owner: str, run: Dict[str, Any], ask: Ask, log: Callable[..., None]) -> None:
    concept = run.get("brief") or run["_doc"]
    roles = run.get("reviewers") or ["design_critic", "codebase_details", "zoom_out", "comprehension",
                                     "implementation_readiness", "structural", "inventive_catalyst", "style"]
    roles = [r for r in roles if r in ROLES]
    log(f"{len(roles)} goldfish lenses look at the concept", "lenses")
    results = await asyncio.gather(*(
        _goldfish(owner, run, ask, role, None, "", run["_context_block"], f"b-{role}", mode="brainstorm",
                  concept=concept, max_findings=6) for role in roles))
    statuses = {r: res["status"] for r, res in zip(roles, results)}
    merged = consolidate([f for res in results for f in res["findings"]])
    lens_text = "\n".join(f"[{f['reviewer']}] ({f['severity']}) {f['title']}: {f['description']} {f.get('rationale', '')}"
                          for f in merged)
    log("the elephant writes the concepts brief", "synthesis")
    brief = await ask(owner, run, [{"role": "system", "content": BRAINSTORM_SYSTEM},
                                   {"role": "user", "content": f"Concept:\n{concept}\n\nContext:\n{run.get('context') or '(none)'}\n\n"
                                                               f"Reviewer points:\n{lens_text or '(none)'}"}],
                      role="elephant", tag="b-synth")
    brief = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", brief.strip())
    status_md = "\n\n## Lens status\n\n| Lens | Status |\n| --- | --- |\n" + "\n".join(f"| {r} | {s} |" for r, s in statuses.items())
    run["consolidated_file"] = _write(owner, run["id"], "concepts_brief.md", brief + status_md + "\n")
    run["findings"] = merged
    run["counts"] = _counts(merged)
    run["lens_status"] = statuses


PANEL_PROMPT = (
    "Find defects that would cause wrong behaviour, data loss, security exposure or a broken contract. For each give the "
    "location (file and lines, or section), the mechanism, a concrete failing input or sequence, severity P1 (must fix), "
    "P2 or P3, and your confidence (high, medium, low). No style comments. Reply with ONLY JSON: {\"findings\": "
    "[{\"location\", \"mechanism\", \"failing_case\", \"severity\", \"confidence\"}]}; no defects: {\"findings\": []}.")
PLAN_PROMPT = (
    "Critique this plan. What assumption is wrong? What will be impossible to measure? Which constraints are unnecessary? "
    "What is the simplest plan that reaches the same outcome? Reply with ONLY JSON: {\"findings\": [{\"location\", "
    "\"mechanism\" (the objection), \"failing_case\" (what goes wrong if ignored), \"severity\" P1|P2|P3, \"confidence\"}]}.")
PROOF_SYSTEM = (
    "You try to DISPROVE a reported defect. Fill a proof table: premise (what is claimed), counter_evidence (where you "
    "looked for proof that it is fine and what you found), before_after (concrete input, old vs new behaviour), "
    "reachability (a real caller or path that hits it), consequence, remedy (smallest fix), confidence (high|medium|low), "
    "and survives (true only if every row holds and confidence is high). Reply with ONLY that JSON object.")
MATCH_SYSTEM = (
    "Group findings that point at the same location AND the same mechanism (wording may differ). Reply with ONLY JSON: "
    "{\"groups\": [[ids...], ...], \"contradicted\": [ids of findings another finding explicitly argues against]}. Every id "
    "appears in exactly one group.")


async def flow_panel(owner: str, run: Dict[str, Any], ask: Ask, log: Callable[..., None]) -> None:
    from src import fresh_llm
    models = [m for m in (run.get("models") or []) if m]
    if len(models) < 2:
        found = [m["model"] for m in fresh_llm.local_models(owner)]
        models = list(dict.fromkeys(models + found))[:3]
    if len(models) < 2:
        raise GoldfishError("a panel needs at least two different models; name them in `models`")
    run["models"] = models
    packet = f"Material to review (line numbers on the left):\n{numbered(run['_doc'])}\n\n" + (
        f"Surrounding material:\n{run['_context_block']}\n\n" if run["_context_block"] else "")
    prompt = PLAN_PROMPT if run.get("panel_mode") == "plan" else PANEL_PROMPT
    log(f"{len(models)} models review the same packet blind: {', '.join(models)}", "panel")

    async def one(i: int, m: str) -> Tuple[str, Any, str]:
        msgs = [{"role": "system", "content": "You are an independent reviewer. " + prompt}, {"role": "user", "content": packet}]
        for attempt in (1, 2):
            try:
                text = await ask(owner, run, msgs, role="goldfish", model=m, tag=f"p{i}-a{attempt}")
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"[:300]
                continue
            data = fresh_llm.parse_json(text)
            if isinstance(data, (Mapping, list)):
                return m, data, ""
            err = "the reply was not JSON"
        return m, None, err

    replies = await asyncio.gather(*(one(i, m) for i, m in enumerate(models)))
    statuses = {m: ("completed" if data is not None else f"not run ({err})") for m, data, err in replies}
    answered = [m for m, data, _err in replies if data is not None]
    flat: List[Dict[str, Any]] = []
    for mi, (m, data, _err) in enumerate(replies):
        items = (data.get("findings") if isinstance(data, Mapping) else data) or []
        for j, f in enumerate(items):
            if isinstance(f, Mapping) and (f.get("mechanism") or f.get("location")):
                flat.append({"id": f"M{mi + 1}.{j + 1}", "model": m, "location": str(f.get("location") or "")[:200],
                             "mechanism": str(f.get("mechanism") or "")[:1200], "failing_case": str(f.get("failing_case") or "")[:800],
                             "severity": str(f.get("severity") or "P3").upper()[:2], "confidence": str(f.get("confidence") or "").lower()})
    groups: List[List[str]] = []
    contradicted: set = set()
    if flat:
        listing = "\n".join(f"{f['id']} [{f['location']}] {f['mechanism']}" for f in flat)
        text = await ask(owner, run, [{"role": "system", "content": MATCH_SYSTEM}, {"role": "user", "content": listing}],
                         role="goldfish", tag="match")
        data = fresh_llm.parse_json(text)
        ids = {f["id"] for f in flat}
        if isinstance(data, Mapping):
            for g in data.get("groups") or []:
                g = [x for x in (g if isinstance(g, list) else []) if x in ids and not any(x in gg for gg in groups)]
                if g:
                    groups.append(g)
            contradicted = {x for x in data.get("contradicted") or [] if x in ids}
        for f in flat:
            if not any(f["id"] in g for g in groups):
                groups.append([f["id"]])
    by_id = {f["id"]: f for f in flat}
    candidates = []
    for g in groups:
        members = [by_id[x] for x in g]
        models_in = sorted({x["model"] for x in members})
        lead = members[0]
        bucket = "agreed" if len(models_in) >= 2 else ("contradicted" if any(x in contradicted for x in g) else "single")
        candidates.append({"cid": f"F{len(candidates) + 1}", "bucket": bucket, "models": models_in, "members": g,
                           "location": lead["location"], "mechanism": lead["mechanism"], "failing_case": lead["failing_case"],
                           # several models disagreeing on severity: the lower one counts
                           "severity": max((x["severity"] for x in members), key=lambda s: int(s[1]) if s[1:].isdigit() else 3)})
    to_prove = [c for c in candidates if c["bucket"] != "agreed"]
    if to_prove:
        log(f"{len(to_prove)} single-model findings face a fresh worker that tries to disprove them", "proof")

        async def prove(c: Dict[str, Any]) -> None:
            msg = (f"Reported defect at {c['location']}: {c['mechanism']}\nFailing case: {c['failing_case']}\n\n{packet}")
            # The prover is another model that answered; one that is down cannot disprove anything.
            pool = [m for m in answered if m not in c["models"]] or answered or models
            prover = pool[(models.index(c["models"][0]) + 1) % len(pool)]
            try:
                text = await ask(owner, run, [{"role": "system", "content": PROOF_SYSTEM}, {"role": "user", "content": msg}],
                                 role="goldfish", model=prover, tag=f"proof-{c['cid']}")
            except Exception as exc:  # noqa: BLE001 - a failed proof drops that finding, never the run
                c["proof"] = {"error": f"{type(exc).__name__}: {exc}"[:300], "prover": prover}
                c["survives"] = False
                return
            data = fresh_llm.parse_json(text)
            c["proof"] = data if isinstance(data, Mapping) else {"error": "no proof table"}
            c["survives"] = bool(isinstance(data, Mapping) and data.get("survives") is True
                                 and str(data.get("confidence") or "").lower().startswith("high"))
        await asyncio.gather(*(prove(c) for c in to_prove))
    kept = [c for c in candidates if c["bucket"] == "agreed" or c.get("survives")]
    sev_rank = lambda s: int(s[1]) if s[1:].isdigit() else 3  # noqa: E731
    kept.sort(key=lambda c: (0 if c["bucket"] == "agreed" else 1, sev_rank(c["severity"])))
    cap = int(run.get("max_findings") or 5)
    reported = kept[:cap]
    run["panel"] = {"statuses": statuses, "raw": flat, "candidates": candidates, "reported": [c["cid"] for c in reported]}
    run["outcome"] = "ACTED" if reported else "NOOP"
    run["findings"] = [{"cid": c["cid"], "id": c["cid"], "reviewer": ", ".join(c["models"]), "reviewers": c["models"],
                        "title": c["mechanism"][:120], "severity": {"P1": "blocker", "P2": "major"}.get(c["severity"], "nit"),
                        "lines": c["location"], "description": c["mechanism"],
                        "rationale": f"Failing case: {c['failing_case']}. Bucket: {c['bucket']}."} for c in reported]
    run["counts"] = _counts(run["findings"])
    dropped = len(candidates) - len(reported)
    lines = [f"# Panel review ({'plan' if run.get('panel_mode') == 'plan' else 'defects'})", "",
             f"**Target:** `{run['target_label']}`", "", "| Model | Status |", "| --- | --- |"]
    lines += [f"| {m} | {s} |" for m, s in statuses.items()]
    lines += ["", f"**{run['outcome']}**: {sum(1 for c in reported if c['bucket'] == 'agreed')} agreed, "
                  f"{sum(1 for c in reported if c['bucket'] != 'agreed')} survived proof, {dropped} dropped.", ""]
    for c in reported:
        lines += [f"## {c['cid']}. {c['severity']} — {c['location']}", f"* **Mechanism:** {c['mechanism']}",
                  f"* **Failing case:** {c['failing_case']}", f"* **Models:** {', '.join(c['models'])} ({c['bucket']})", ""]
    run["consolidated_file"] = _write(owner, run["id"], "panel_report.md", "\n".join(lines))
    _write(owner, run["id"], "findings.json", json.dumps(run["panel"], ensure_ascii=False, indent=1))


async def flow_drift(owner: str, run: Dict[str, Any], ask: Ask, log: Callable[..., None]) -> None:
    if not run["_context_block"]:
        raise GoldfishError("drift needs the source files the document describes in `context_files`")
    log("a goldfish compares the document with the code", "drift")
    res = await _goldfish(owner, run, ask, "codebase_details", "re-implementation", numbered(run["_doc"]),
                          run["_context_block"], "drift", max_findings=int(run.get("max_findings") or 12))
    merged = consolidate(res["findings"])
    run["findings"] = merged
    run["counts"] = _counts(merged)
    run["consolidated_file"] = _write(owner, run["id"], "drift_report.md",
                                      _report("Drift between document and code", run["target_label"],
                                              {"codebase_details": res["status"]}, merged))


FLOW_FUNCS = {"review": flow_review, "loop": flow_loop, "draft": flow_draft, "brainstorm": flow_brainstorm,
              "panel": flow_panel, "drift": flow_drift}


# ---------------------------------------------------------------------------
# start / run / resolve
# ---------------------------------------------------------------------------
def start(owner: str, args: Mapping[str, Any], *, ask: Optional[Ask] = None, background: bool = True) -> Dict[str, Any]:
    flow = str(args.get("flow") or "review").lower()
    if flow not in FLOWS:
        raise GoldfishError(f"`flow` must be one of {', '.join(FLOWS)}")
    target = str(args.get("path") or "").strip()
    text = str(args.get("text") or "")
    if flow in ("draft", "brainstorm"):
        if not (str(args.get("brief") or "").strip() or text.strip()):
            raise GoldfishError("`brief` is required: the concept or notes to start from")
    elif not target and not text.strip():
        raise GoldfishError("give the document: `path` (a file, or brain:<note path>) or `text`")
    doc = text
    if target and flow not in ("draft",):
        doc = _read_target(owner, target)[0]
    if len(doc) > MAX_DOC_CHARS:
        raise GoldfishError(f"the document is longer than {MAX_DOC_CHARS} characters; review it in parts")
    ctx_files = args.get("context_files") or []
    if isinstance(ctx_files, str):
        ctx_files = [x.strip() for x in re.split(r"[\n,;]", ctx_files) if x.strip()]
    if target and args.get("follow_links", True):
        ctx_files = list(dict.fromkeys(list(ctx_files) + linked_files(doc, target)))
    context_block, used = reading_list(owner, ctx_files)
    stages = args.get("stages")
    if isinstance(stages, str):
        stages = [int(x) for x in re.findall(r"\d", stages)]
    reviewers = args.get("reviewers")
    if isinstance(reviewers, str):
        reviewers = [x.strip() for x in re.split(r"[,\s]+", reviewers) if x.strip()]
    if reviewers:
        bad = [r for r in reviewers if r not in ROLES]
        if bad:
            raise GoldfishError(f"unknown reviewers: {', '.join(bad)}; known: {', '.join(ROLES)}")
    standards = args.get("standards") or {}
    if isinstance(standards, Mapping):
        bad = [s for s in standards.values() if s not in STANDARDS]
        if bad:
            raise GoldfishError(f"unknown standards: {', '.join(bad)}; known: {', '.join(STANDARDS)}")
    models = args.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    run_id = f"gf-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    title = str(args.get("title") or (Path(target).stem if target else "") or (doc.strip().splitlines() or ["Document"])[0][:80]
                or flow).lstrip("# ").strip()
    run: Dict[str, Any] = {
        "id": run_id, "flow": flow, "title": title[:120], "target": target or None,
        "target_label": target or "inline text", "created": now, "status": "queued", "phase": "queued",
        "preset": str(args.get("preset") or "full"), "stages": stages or None, "reviewers": reviewers or None,
        "standards": dict(standards) if isinstance(standards, Mapping) else {}, "max_rounds": int(args.get("max_rounds") or MAX_ROUNDS),
        "auto_apply": bool(args.get("auto_apply", True)), "resolve": bool(args.get("resolve", False)),
        "write_back": bool(args.get("write_back", bool(target))), "double": bool(args.get("double", False)),
        "max_findings": int(args.get("max_findings") or (5 if flow == "panel" else 8)),
        "model": str(args.get("model") or ""), "elephant_model": str(args.get("elephant_model") or args.get("model") or ""),
        "models": models, "panel_mode": str(args.get("panel_mode") or "defects"),
        "brief": str(args.get("brief") or ""), "context": str(args.get("context") or "")[:20000],
        "context_files": used, "review_after": bool(args.get("review_after", True)), "parallel": int(args.get("parallel") or 3),
        "version": 0, "log": [], "counts": None, "findings": [], "error": None,
    }
    if flow == "draft" and not target and args.get("save_to"):
        run["target"] = str(args["save_to"])
        run["target_label"] = run["target"]
        run["write_back"] = True
    _save(owner, run)
    _write(owner, run_id, "doc_v0.md", doc)
    _write(owner, run_id, "context.txt", context_block)
    if background:
        task = asyncio.ensure_future(_execute(owner, run_id, ask=ask))
        _TASKS[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: (_TASKS.pop(rid, None), _CANCEL.discard(rid)))
    return run


async def _execute(owner: str, run_id: str, *, ask: Optional[Ask] = None) -> Dict[str, Any]:
    run = get_run(owner, run_id)
    run["_doc"] = read_artifact(owner, run_id, "doc_v0.md")
    run["_context_block"] = read_artifact(owner, run_id, "context.txt")
    ask = ask or _default_ask

    def persist() -> None:
        _save(owner, run)

    def log(msg: str, phase: Optional[str] = None) -> None:
        if phase:
            run["phase"] = phase
        run["log"].append({"at": time.strftime("%H:%M:%S"), "msg": msg[:500]})
        run["log"] = run["log"][-300:]
        persist()

    run["status"] = "running"
    try:
        await FLOW_FUNCS[run["flow"]](owner, run, ask, log)
        run["status"] = "cancelled" if _cancelled(run) else "done"
        run["phase"] = run.get("outcome") or run["status"]
        if run.get("version"):
            run["final_file"] = f"doc_v{run['version']}.md"
        elif run["flow"] == "draft":
            run["final_file"] = "doc_v0.md"
    except GoldfishError as exc:
        run["status"], run["error"] = "failed", exc.detail
    except Exception as exc:  # noqa: BLE001 - the run record must say what happened
        run["status"], run["error"] = "failed", f"{type(exc).__name__}: {exc}"[:500]
    run["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    persist()
    return run


def resolve(owner: str, run_id: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    run = get_run(owner, run_id)
    if run.get("status") != "awaiting_decision":
        raise GoldfishError(f"the run is {run.get('status')}, not waiting for a decision")
    mode = str(args.get("mode") or ("decisions" if args.get("decisions") else "auto_accept"))
    if mode not in ("auto_accept", "continue", "decisions"):
        raise GoldfishError("`mode` must be auto_accept, continue or decisions")
    decisions = args.get("decisions") or {}
    if mode == "decisions" and not isinstance(decisions, Mapping):
        raise GoldfishError("`decisions` must map a finding id to accept, skip or overrule")
    run["pending_resolution"] = {"mode": mode, "decisions": dict(decisions), "notes": dict(args.get("notes") or {})}
    _save(owner, run)
    ev = _RESUME.get(run_id)
    if ev:
        ev.set()
    return {"id": run_id, "resolution": run["pending_resolution"]}


async def wait(run_id: str, timeout: float) -> bool:
    task = _TASKS.get(run_id)
    if task is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def run_action(owner: str, args: Mapping[str, Any], **kw) -> Dict[str, Any]:
    action = str(args.get("action") or "start").lower()
    try:
        if action == "start":
            run = start(owner, args, **kw)
            if args.get("wait_s"):
                await wait(run["id"], float(args["wait_s"]))
                run = get_run(owner, run["id"])
            return run
        rid = str(args.get("id") or args.get("run_id") or "")
        if action == "status" or action == "get":
            run = get_run(owner, rid)
            if args.get("artifact"):
                run["artifact"] = {"name": args["artifact"], "text": read_artifact(owner, rid, str(args["artifact"]))}
            return run
        if action == "list":
            return {"runs": list_runs(owner)}
        if action == "resolve":
            return resolve(owner, rid, args)
        if action == "cancel":
            return cancel_run(owner, rid)
        if action == "delete":
            return delete_run(owner, rid)
        if action == "roles":
            return {"reviewers": {k: v["label"] for k, v in ROLES.items()}, "standards": list(STANDARDS),
                    "stages": {k: v["name"] for k, v in STAGES.items()}, "presets": PRESETS, "flows": list(FLOWS)}
    except GoldfishError as exc:
        return {"error": exc.detail, "error_class": exc.error_class}
    return {"error": "action must be start, status, list, resolve, cancel, delete or roles", "error_class": "goldfish.bad_args"}


def render(data: Mapping[str, Any]) -> str:
    if data.get("error"):
        return f"error: {data['error']}"
    if "runs" in data:
        return "\n".join(f"{r['id']}  {r['flow']}  {r['status']}  {r.get('title')}  {r.get('counts') or ''}"
                         for r in data["runs"]) or "no goldfish runs"
    if "reviewers" in data and "flows" in data:
        return json.dumps(data, ensure_ascii=False)
    if "deleted" in data or "cancel_requested" in data or "resolution" in data:
        return json.dumps(data, ensure_ascii=False)
    lines = [f"goldfish run {data['id']} ({data['flow']}): {data['status']} — {data.get('phase')}"]
    if data.get("counts"):
        c = data["counts"]
        lines.append(f"findings: {c.get('blocker', 0)} blockers, {c.get('major', 0)} major, {c.get('nit', 0)} nits")
    for f in (data.get("findings") or [])[:12]:
        lines.append(f"- {f.get('cid', f.get('id'))} [{f['severity']}] {f['title']} (lines {f.get('lines') or '-'}; {f.get('reviewer')})")
    if data.get("outcome"):
        lines.append(f"outcome: {data['outcome']}")
    for key in ("consolidated_file", "final_file", "reconciliation_file"):
        if data.get(key):
            lines.append(f"{key}: {data[key]} (read with action=status, artifact=<name>)")
    if data.get("artifact"):
        lines.append(f"\n--- {data['artifact']['name']} ---\n{data['artifact']['text'][:12000]}")
    if data.get("log"):
        lines.append(f"last: {data['log'][-1]['msg']}")
    return "\n".join(lines)
