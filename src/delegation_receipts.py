"""delegation_receipts.py — deterministic verdicts on what a delegated
sub-agent worker actually produced (H3, born from the Silhouettes fan-out
failure: `src/agent_tools/subagent_tools.py::delegate_agents` sent 3 workers
— `backend-core`, `fit-tests`, `frontend` — precise technical specs, and all
three came back with zero mutations: one replied only
`<<faustus_ctx_ack>><<faustus_ctx_ack>>`, two replied with no final text at
all after a few reads. Nothing in the parent turn noticed).

Pure and side-effect free on purpose, matching the module doctrine already in
`subagent_tools.py` ("evidence over narrative"): every function here takes
plain values (or duck-types a `SubagentRun`-shaped object) and returns plain
data — no I/O, no settings lookups, no imports of `subagent_tools` itself, so
this module can be imported and unit-tested without pulling in the agent
loop, a session manager or a workspace.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

VERDICT_PRODUCED = "produced"
VERDICT_EMPTY = "empty"
VERDICT_ACK_ONLY = "ack_only"
VERDICT_ERROR = "error"

# Ritual phrases a local model echoes back instead of acting, seen verbatim in
# the Silhouettes transcripts: `<<faustus_ctx_ack>>` (repeated), "Reference
# context received.", "Done." with zero mutations, and the harness's own
# placeholder for a response with no text at all, "(no final text)". Matched
# against the whitespace-normalised, stripped final text so padding or
# repetition ("<<faustus_ctx_ack>><<faustus_ctx_ack>>") still matches.
_ACK_PATTERNS = (
    re.compile(r"^(<<\s*faustus_ctx_ack\s*>>\s*)+$", re.I),
    re.compile(r"^reference context received\.?$", re.I),
    re.compile(r"^done\.?$", re.I),
    re.compile(r"^\(no final text\)$", re.I),
    re.compile(r"^acknowledged\.?$", re.I),
    re.compile(r"^context received\.?$", re.I),
    re.compile(r"^understood\.?$", re.I),
    re.compile(r"^ok(ay)?\.?$", re.I),
)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


def is_ack_only(final_text: Any) -> bool:
    """True when `final_text` is nothing but a ritual acknowledgement — the
    exact shape of Silhouettes chat `0f5553df` (`<<faustus_ctx_ack>>`) and the
    "Reference context received." replies of chats `c0d914fd`/`ea43ef6c`."""
    text = _norm(final_text)
    if not text:
        return False
    return any(p.match(text) for p in _ACK_PATTERNS)


def classify(final_text: Any, mutations: Any, tool_calls: int, *, error: Optional[str] = None) -> str:
    """The four-value verdict this module exists to compute — deterministic,
    never asked of the model (rule 1 of the H3 contract: the harness
    compensates a local model, it never trusts it to self-report).

    - ``error``: the run itself ended in error (crash, timeout, model
      failure). Never retried by the caller — an error already explains
      itself and retrying a crashed worker is not what this measure is for.
    - ``produced``: at least one real file mutation is on record. Wins over
      everything else, including an error recorded alongside it (a worker
      that wrote files and then hit a late error still produced something).
    - ``ack_only``: no mutation, and the final text is empty or nothing but a
      ritual acknowledgement (see `is_ack_only`) — chats `0f5553df`,
      `c0d914fd`, `ea43ef6c`.
    - ``empty``: no mutation, and the text is neither empty nor a ritual
      phrase — the worker made tool calls (reads, `ls`, `grep`) or wrote
      prose, but never a file. Chats `9eeffac5`/`5391068e`: a few reads, then
      literally "(no final text)", which IS ack_only; a worker that instead
      wrote three paragraphs analysing the task without ever calling
      write_file is `empty`, not `ack_only` — the two want different retry
      prompts (one needs "stop acknowledging", the other needs "stop
      analysing and act").
    """
    muts = [m for m in (mutations or []) if str(m).strip()]
    if muts:
        return VERDICT_PRODUCED
    if error:
        return VERDICT_ERROR
    text = _norm(final_text)
    if not text or is_ack_only(text):
        return VERDICT_ACK_ONLY
    return VERDICT_EMPTY


@dataclass(frozen=True)
class DelegationReceipt:
    worker: str
    task: str
    mutations: List[str]
    tool_calls: int
    final_text_chars: int
    stop_reason: str
    verdict: str
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker": self.worker,
            "task": self.task,
            "mutations": list(self.mutations),
            "tool_calls": self.tool_calls,
            "final_text_chars": self.final_text_chars,
            "stop_reason": self.stop_reason,
            "verdict": self.verdict,
            **({"error": self.error} if self.error else {}),
        }


def _short(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def receipt_for(run: Any) -> DelegationReceipt:
    """Build a receipt from a `SubagentRun`-shaped object: duck-typed on
    `.name`, `.instruction`, `.mutations`, `.tool_calls`, `.text`,
    `.stop_reason`, `.error` (exactly `src.agent_tools.subagent_tools
    .SubagentRun`'s attributes) so this module never imports that one —
    keeping the dependency one-directional and this file testable alone.
    """
    text = getattr(run, "text", "") or ""
    mutations = list(getattr(run, "mutations", None) or ())
    tool_calls = int(getattr(run, "tool_calls", 0) or 0)
    error = getattr(run, "error", None)
    verdict = classify(text, mutations, tool_calls, error=error)
    return DelegationReceipt(
        worker=str(getattr(run, "name", "") or ""),
        task=_short(getattr(run, "instruction", "") or "", 200),
        mutations=mutations,
        tool_calls=tool_calls,
        final_text_chars=len(text.strip()),
        stop_reason=str(getattr(run, "stop_reason", "") or "unknown"),
        verdict=verdict,
        error=(_short(error, 300) if error else None),
    )


def failure_message(verdict: str) -> str:
    """The sentence `delegate_agents` puts in front of the coordinator when a
    worker is STILL empty/ack_only after its one retry — the H3 contract's
    literal wording, so the coordinator (a 27B model that is itself prone to
    accepting silence as success) cannot read it as anything but a command:
    do the work itself, or split the task differently."""
    return f"worker produced nothing ({verdict}) — do it yourself or split the task"


# ---------------------------------------------------------------------------
# Retry heuristics (H3 measure 5 / Silhouettes #17→#18,19,20)
# ---------------------------------------------------------------------------
# A worker whose task plainly asked it to WRITE something, and that came back
# `empty`/`ack_only`, gets exactly one short, imperative retry — never a task
# that was open-ended analysis, where an empty mutation list may be correct.

_WRITE_VERB_RE = re.compile(
    r"\b(create|implement|write|add|build|generate|refactor|fix|update|modify|edit|delete|rename|"
    r"crea|crear|implementa|implementar|escribe|escribir|añade|añadir|agrega|agregar|"
    r"corrige|corregir|arregla|arreglar|modifica|modificar|actualiza|actualizar|genera|generar)\b",
    re.I,
)
# The same shape `src.agent_tools.subagent_tools._CRITERION_FILE_RE` already
# uses to spot a file name inside free text — kept independent here (this
# module imports nothing from that one) rather than shared, because the two
# call sites are allowed to diverge without either one silently breaking the
# other.
_PATH_TOKEN_RE = re.compile(r"[\w][\w./\\-]*\.[A-Za-z0-9]{1,8}\b")


def looks_like_write_task(instruction: Any) -> bool:
    """A conservative heuristic, never authoritative on its own (the caller
    still checks the run's own verdict first): a task that names a file path
    (``mesh_adapter.py``, ``tests/editor/test_stack.py`` — exactly the shape
    of the #18-20 tasks) or uses an implementation verb is one where "the
    worker changed nothing" is presumptively a failure worth one retry."""
    text = str(instruction or "")
    if not text.strip():
        return False
    return bool(_WRITE_VERB_RE.search(text) or _PATH_TOKEN_RE.search(text))


def extract_paths(instruction: Any, limit: int = 8) -> List[str]:
    """File-like tokens named in a task's own text, best-effort, order
    preserving, deduplicated — used to tell the retry prompt exactly what to
    write when the caller did not pass an explicit `files` list."""
    seen: List[str] = []
    for m in _PATH_TOKEN_RE.finditer(str(instruction or "")):
        tok = m.group(0).strip().rstrip(".,;:)")
        if tok and tok not in seen:
            seen.append(tok)
        if len(seen) >= limit:
            break
    return seen


def short_imperative_prompt(instruction: Any, files: Optional[List[str]] = None) -> str:
    """A short, imperative retry instruction: no restated spec, no large
    material — an order to act now. Per the H3 contract: the retry must never
    carry the large material again, only enough of the original task to name
    the target."""
    paths = list(files or []) or extract_paths(instruction)
    target = ", ".join(paths[:6]) if paths else "the file(s) this task names"
    gist = _short(instruction, 400)
    return (
        f"Your previous reply produced no file changes. Write {target} now, using "
        "write_file/edit_file/apply_patch. Do not summarize the plan, do not "
        "acknowledge or ask for confirmation — act. End your reply with the exact "
        "list of files you wrote.\n\n"
        f"Task (for reference, not new instructions): {gist}"
    )
