"""src/doubt_review.py — a fresh-context "doubt review" before a risky edit
lands, on a HIGH-risk file (per `src.code_graph.risk.change_risk`).

The idea (a "doubt-driven" review): rather than a second pass AFTER the turn
is done reading the whole diff (`src/auto_review.py`), this asks a reviewer
to look at ONE proposed edit to a file `change_risk` already scored as
high-blast-radius, with a refutation bias — its only job is to find reasons
the change is wrong, not to confirm it looks fine. The reviewer gets no
tools, no conversation history and no memory of anything else this turn did:
only the task statement (when available), the file's risk summary, and the
unified diff of the one edit being proposed. That narrow context is the
point — a model that has spent the whole turn making the edit and believing
in it is not the model that should also be the one double-checking it.

This is advisory by default (`agent_doubt_review_block=False`): the edit
still applies, and the verdict is appended to the tool result as a "Second
look (high-risk file):" section for the agent to read and reconsider. When
`agent_doubt_review_block` is on, a "concerns" verdict makes the edit tool
refuse the edit instead (nothing is written) and returns the concerns asking
the agent to revise, or to re-issue the same call with `confirm_risky: true`
to apply it anyway.

Cost control: `change_risk` walks the call graph and parses git history, so
it is cached per (turn, workspace, path) here (`get_risk_summary`) rather
than recomputed for every edit call in a turn, and `should_review` is cheap
otherwise — no model call, no risk computation when the diff is trivial or
the file is a test/docs file, both checked before the risk lookup.

Never raises: `review()` catches everything and returns an "error" field
plus a timed-out/failed verdict of "ok" (fail OPEN — a broken reviewer must
never turn into a silent block of every edit) so the caller can always
append a section or decide to skip it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 45
#: Below this many non-trivial changed lines, a diff is not worth a second
#: reviewer's time -- matches the spirit of `agent_auto_review`'s diff gate,
#: kept local so this module has no runtime dependency on auto_review.py.
DEFAULT_MIN_CHANGED_LINES = 3
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
_VERDICTS = ("ok", "concerns")

# Same test-path heuristic `code_graph.query._is_test_path` uses, duplicated
# rather than imported so a caller that has no code_graph available (a bare
# path string, no workspace) can still call `should_review` for gating.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)tests?/|(?:^|/)test_[^/]+\.py$|[^/]+_test\.py$"
    r"|[^/]+\.test\.(?:ts|tsx|js)$|[^/]+\.spec\.[^/]+$"
)
_DOCS_PATH_RE = re.compile(
    r"\.(?:md|mdx|rst|txt|adoc)$|(?:^|/)docs?/", re.I
)

_TIER_RANK = {"low": 0, "medium": 1, "high": 2}


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def is_test_or_docs_path(path: str) -> bool:
    norm = (path or "").replace("\\", "/")
    return bool(_TEST_PATH_RE.search(norm) or _DOCS_PATH_RE.search(norm))


# ---------------------------------------------------------------------------
# Diff triviality
# ---------------------------------------------------------------------------

def _diff_text(diff: Any) -> str:
    """Accept either a raw unified-diff string or the `{"text": ...}` shape
    the edit tools' own `_unified_diff` returns, so a caller can hand this
    the tool result's `diff` field directly."""
    if isinstance(diff, dict):
        return str(diff.get("text") or "")
    return str(diff or "")


def _is_noise_line(body: str) -> bool:
    """A changed line that is blank, or (for the common comment markers)
    entirely a comment -- these never carry logic a reviewer needs to see."""
    s = body.strip()
    if not s:
        return True
    return bool(re.match(r"^(#|//|\*|/\*|\*/|--)", s))


def changed_line_count(diff_text: str) -> int:
    """Non-trivial +/- lines in a unified diff: file-header lines (+++/---)
    and blank/comment-only changed lines don't count."""
    n = 0
    for line in (diff_text or "").splitlines():
        if not line or line[0] not in "+-":
            continue
        if line.startswith(("+++", "---")):
            continue
        if _is_noise_line(line[1:]):
            continue
        n += 1
    return n


def is_trivial_diff(diff: Any, *, min_changed_lines: Optional[int] = None) -> bool:
    text = _diff_text(diff)
    if not text.strip():
        return True
    threshold = DEFAULT_MIN_CHANGED_LINES if min_changed_lines is None else int(min_changed_lines)
    return changed_line_count(text) < max(1, threshold)


# ---------------------------------------------------------------------------
# Risk summary, cached per (turn, workspace, path)
# ---------------------------------------------------------------------------

@dataclass
class DoubtReviewState:
    """One instance per TURN (mirrors `RewritePolicy`'s own contract) --
    caches `change_risk` per path so several edits to the same file within a
    turn cost one risk computation, and caps how many reviews the turn may
    trigger. Never shared across turns or across a coordinator and its
    delegated workers."""
    max_per_turn: int = 2
    _risk_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _reviewed_cache: Dict[Tuple[str, str], Dict[str, Any]] = field(default_factory=dict)
    reviews_run: int = 0

    def cached_risk(self, path: str) -> Optional[Dict[str, Any]]:
        return self._risk_cache.get(path)

    def store_risk(self, path: str, summary: Dict[str, Any]) -> None:
        self._risk_cache[path] = summary

    def cached_review(self, path: str, diff_hash: str) -> Optional[Dict[str, Any]]:
        return self._reviewed_cache.get((path, diff_hash))

    def store_review(self, path: str, diff_hash: str, result: Dict[str, Any]) -> None:
        self._reviewed_cache[(path, diff_hash)] = result

    def budget_left(self) -> bool:
        return self.reviews_run < max(0, int(self.max_per_turn))

    def note_review_run(self) -> None:
        self.reviews_run += 1

    def reset(self) -> None:
        self._risk_cache.clear()
        self._reviewed_cache.clear()
        self.reviews_run = 0


def get_risk_summary(path: str, workspace: str, *, project_id: str = "",
                      state: Optional[DoubtReviewState] = None,
                      risk_fn: Optional[Callable[..., Dict[str, Any]]] = None
                      ) -> Dict[str, Any]:
    """`change_risk([path], workspace=...)`, cached on `state` (one call per
    (turn, path) rather than one per edit). `risk_fn` is an injection seam
    for tests -- defaults to `code_graph.change_risk`. Never raises: a
    failure is reported as `{"error": ..., "level": "low", "score": 0}` so a
    broken risk computation can never itself trigger a block."""
    if state is not None:
        cached = state.cached_risk(path)
        if cached is not None:
            return cached
    fn = risk_fn
    if fn is None:
        try:
            from src.code_graph import change_risk as fn  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[doubt_review] change_risk import failed: %s", exc)
            return {"error": str(exc), "level": "low", "score": 0}
    try:
        summary = fn([path], workspace=workspace, project_id=project_id) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[doubt_review] change_risk(%r) failed: %s", path, exc)
        summary = {"error": str(exc), "level": "low", "score": 0}
    if state is not None:
        state.store_risk(path, summary)
    return summary


# ---------------------------------------------------------------------------
# should_review gate
# ---------------------------------------------------------------------------

def diff_hash(diff: Any) -> str:
    import hashlib
    return hashlib.sha256(_diff_text(diff).encode("utf-8", "replace")).hexdigest()[:16]


def should_review(path: str, workspace: str, diff: Any, *, project_id: str = "",
                   state: Optional[DoubtReviewState] = None,
                   min_tier: Optional[str] = None,
                   max_per_turn: Optional[int] = None,
                   min_changed_lines: Optional[int] = None,
                   risk_fn: Optional[Callable[..., Dict[str, Any]]] = None
                   ) -> Tuple[bool, Dict[str, Any]]:
    """Whether `path`'s proposed `diff` earns a doubt review. Returns
    `(decision, risk_summary)` -- the risk summary is always computed/looked
    up (even on a "no" decision) so a caller that wants to show the tier
    elsewhere does not pay for a second lookup.

    Gates, in cheapest-first order:
      1. diff is non-trivial (>= `min_changed_lines` non-blank, non-comment
         changed lines) -- checked before touching the risk index at all;
      2. path is not a test or docs file;
      3. this turn has not already spent its `max_per_turn` reviews;
      4. `change_risk` tier for `path` is at or above `min_tier`.
    A result already cached for this exact `(path, diff)` this turn is
    reused (`state.cached_review`) -- callers that go through `review()`
    below never need to call `should_review` twice for the same edit.
    """
    tier_floor = str(min_tier or _setting("agent_doubt_review_min_tier", "high") or "high").lower()

    if is_trivial_diff(diff, min_changed_lines=min_changed_lines):
        return False, {}
    if is_test_or_docs_path(path):
        return False, {}
    if state is not None:
        # An explicit `max_per_turn` from the caller re-caps an existing
        # per-turn state; with none given, the state's own cap (set once,
        # at turn start, from the same setting) is authoritative -- it must
        # NOT be reset back to the global default on every call, or a state
        # that already spent its budget this turn would silently regain it.
        if max_per_turn is not None:
            try:
                state.max_per_turn = int(max_per_turn)
            except (TypeError, ValueError):
                pass
        if not state.budget_left():
            return False, {}
    elif max_per_turn is not None:
        try:
            if int(max_per_turn) <= 0:
                return False, {}
        except (TypeError, ValueError):
            pass

    summary = get_risk_summary(path, workspace, project_id=project_id, state=state, risk_fn=risk_fn)
    if summary.get("error"):
        # A risk that could not be computed (a folder outside the allowed
        # roots, a broken index) is not evidence of risk: with a low tier
        # floor it used to earn a review of its own.
        return False, summary
    level = str(summary.get("level") or "low").lower()
    if _TIER_RANK.get(level, 0) < _TIER_RANK.get(tier_floor, 2):
        return False, summary
    return True, summary


# ---------------------------------------------------------------------------
# The reviewer call
# ---------------------------------------------------------------------------

def _prompt(task: str, path: str, risk_summary: Dict[str, Any], diff_text: str) -> str:
    reasons = risk_summary.get("top_reasons") or []
    reason_lines = "\n".join(
        f"- {r.get('factor', '?')}: {r.get('reason', '')}" for r in reasons[:3]
    ) or "- (no factor breakdown available)"
    task_line = f"The task this change is part of:\n{task.strip()}\n\n" if (task or "").strip() else ""
    return (
        "You are a skeptical second reviewer looking at ONE proposed code change before it "
        "is applied. You have no other context: not the conversation, not the rest of the "
        "codebase, only what is given here. Your job is to look for reasons this change is "
        "WRONG -- a bug, a broken caller, a contradiction with the stated task, a dropped "
        "edge case, a name that does not exist. Do not praise the change or restate what it "
        "does; only report concrete problems you can point at in the diff. If you find "
        "nothing wrong after actually trying to find something, say so.\n\n"
        f"{task_line}"
        f"File: {path}\n"
        f"Why this file is flagged high-risk:\n{reason_lines}\n\n"
        f"Proposed diff:\n```diff\n{diff_text}\n```\n\n"
        "Answer with ONLY a JSON object:\n"
        '{"verdict": "ok" | "concerns", "concerns": ["short, concrete point", ...], '
        '"confidence": 0.0-1.0}\n'
        "At most 5 items in \"concerns\". \"concerns\" MUST be empty when verdict is \"ok\"."
    )


DOUBT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "concerns"]},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["verdict", "concerns"],
}


def _parse(raw: str) -> Dict[str, Any]:
    text = _THINK_RE.sub("", raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M)
    m = _JSON_BLOCK_RE.search(text)
    data: Any = None
    if m:
        blob = m.group(0)
        try:
            data = json.loads(blob)
        except ValueError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", blob))
            except ValueError:
                data = None
    if not isinstance(data, dict):
        return {"verdict": "ok", "concerns": [], "confidence": None, "unparsed": True}
    concerns: List[str] = []
    for c in (data.get("concerns") or [])[:5]:
        s = str(c).strip()
        if s:
            concerns.append(s[:400])
    verdict = str(data.get("verdict") or "").lower().strip()
    if verdict not in _VERDICTS:
        verdict = "concerns" if concerns else "ok"
    if verdict == "ok" and concerns:
        verdict = "concerns"
    confidence = data.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
        if confidence is not None:
            confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = None
    return {"verdict": verdict, "concerns": concerns, "confidence": confidence}


async def _default_model_call(task: str, path: str, risk_summary: Dict[str, Any],
                               diff_text: str, *, owner: Optional[str], model: Optional[str],
                               timeout_s: float) -> str:
    """Resolve a model (an explicit `model` spec, else the
    `agent_doubt_review_model` setting, else the endpoint's own "auto"
    choice -- meant to land on a small local utility model such as a
    project's Ollama/llama.cpp alias) and ask it ONE tool-less completion."""
    from src.ai_interaction import _resolve_model
    from src.llm_core import llm_call_async
    spec = (model or str(_setting("agent_doubt_review_model", "auto") or "auto")).strip() or "auto"
    url, resolved_model, headers = await asyncio.to_thread(_resolve_model, spec, owner=owner)
    messages = [{"role": "user", "content": _prompt(task, path, risk_summary, diff_text)}]
    raw = await llm_call_async(
        url, resolved_model, messages, headers=headers, temperature=0.1, max_tokens=500,
        timeout=int(timeout_s), max_retries=1, workload="foreground",
        response_schema=DOUBT_SCHEMA,
    )
    return raw[0] if isinstance(raw, tuple) else raw


async def review(task: str, path: str, risk_summary: Dict[str, Any], diff: Any, *,
                  owner: Optional[str] = None, model: Optional[str] = None,
                  timeout_s: Optional[float] = None,
                  model_call: Optional[Callable[..., Awaitable[str]]] = None
                  ) -> Dict[str, Any]:
    """One doubt-review pass. Always returns
    `{verdict, concerns, confidence, model, elapsed_ms}` plus an `error` key
    on failure (verdict then stays "ok" -- fail open, never blocks on a
    broken reviewer). `model_call` is an injection seam for tests; it takes
    `(task, path, risk_summary, diff_text, owner=, model=, timeout_s=)` and
    returns the raw completion text."""
    t0 = time.monotonic()
    timeout = float(timeout_s if timeout_s is not None
                     else _setting("agent_doubt_review_timeout_seconds", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    diff_text = _diff_text(diff)
    result: Dict[str, Any] = {
        "verdict": "ok", "concerns": [], "confidence": None,
        "model": model or "", "elapsed_ms": 0,
    }
    if not diff_text.strip():
        result["error"] = "nothing to review"
        return result
    call = model_call or _default_model_call
    try:
        raw = await asyncio.wait_for(
            call(task, path, risk_summary, diff_text, owner=owner, model=model, timeout_s=timeout),
            timeout=timeout + 15,
        )
    except asyncio.TimeoutError:
        result["error"] = f"doubt review timed out after {int(timeout) + 15}s"
        result["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.debug("[doubt_review] model call failed for %s: %s", path, exc, exc_info=True)
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        result["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return result
    if not isinstance(raw, str) or not raw.strip():
        result["error"] = "the reviewer returned an empty answer"
        result["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return result
    parsed = _parse(raw)
    result.update(verdict=parsed["verdict"], concerns=parsed["concerns"], confidence=parsed["confidence"])
    if parsed.get("unparsed"):
        result["unparsed"] = True
    result["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return result


# ---------------------------------------------------------------------------
# Formatting the result section (advisory mode)
# ---------------------------------------------------------------------------

def format_section(review_result: Dict[str, Any], risk_summary: Dict[str, Any]) -> str:
    """The "Second look (high-risk file):" text appended to an edit tool's
    result in advisory mode."""
    level = str(risk_summary.get("level") or "high").upper()
    score = risk_summary.get("score")
    header = f"Second look (high-risk file, risk={level}" + (f" {score}/100" if score is not None else "") + "):"
    if review_result.get("error"):
        return f"{header} skipped ({review_result['error']})"
    if review_result.get("verdict") == "ok":
        return f"{header} looks right, no concerns raised."
    lines = [header]
    for c in review_result.get("concerns") or []:
        lines.append(f"  - {c}")
    if not review_result.get("concerns"):
        lines.append("  - concerns flagged but no detail returned")
    return "\n".join(lines)


def block_message(review_result: Dict[str, Any], risk_summary: Dict[str, Any], path: str) -> str:
    """The error text/result body when `agent_doubt_review_block` refuses the
    edit -- points the agent at `confirm_risky: true` to override."""
    level = str(risk_summary.get("level") or "high").upper()
    lines = [
        f"doubt_review: a second reviewer raised concerns about this change to {path} "
        f"(risk={level}); the edit was NOT applied.",
    ]
    for c in review_result.get("concerns") or []:
        lines.append(f"  - {c}")
    lines.append(
        "Revise the change to address these, or re-issue the exact same call with "
        '"confirm_risky": true to apply it anyway.'
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration entry point for the edit tools
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return bool(_setting("agent_doubt_review", False))


def block_mode_enabled() -> bool:
    return bool(_setting("agent_doubt_review_block", False))


async def check_edit(ctx: Optional[Dict[str, Any]], path: str, raw_path: str, diff: Any, *,
                      confirm_risky: bool = False) -> Dict[str, Any]:
    """Full doubt-review gate for one edit-tool call, driven by settings and
    the per-turn state carried on `ctx` (`doubt_review_state`, `task_text`,
    `owner`, `project_id`). Callers (the edit tools) drive this once per
    call and act on the result; this function never raises.

    Returns `{"proceed": True, "section": str|None, "review": dict|None}`
    when the edit should go ahead (the overwhelming majority of calls,
    including every call while the feature is off) -- `section`, when set,
    is the "Second look..." text to append to the tool's success result.

    Returns `{"proceed": False, "result": dict}` ONLY in block mode with an
    unconfirmed "concerns" verdict -- `result` is the ready-to-return tool
    error dict; the caller must not write anything.
    """
    proceed_noop = {"proceed": True, "section": None, "review": None}
    if not enabled():
        return proceed_noop
    ctx = ctx if isinstance(ctx, dict) else {}
    state: Optional[DoubtReviewState] = ctx.get("doubt_review_state")
    try:
        from src.tool_execution import get_active_workspace
        workspace = get_active_workspace() or ""
    except Exception:  # noqa: BLE001
        workspace = ""
    project_id = str(ctx.get("project_id") or "")
    try:
        decision, risk_summary = should_review(raw_path or path, workspace, diff,
                                                project_id=project_id, state=state)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[doubt_review] should_review failed for %s: %s", path, exc, exc_info=True)
        return proceed_noop
    if not decision:
        return proceed_noop

    dh = diff_hash(diff)
    cache_key = raw_path or path
    cached = state.cached_review(cache_key, dh) if state is not None else None
    if cached is not None:
        review_result = cached
    else:
        task = str(ctx.get("task_text") or "")
        owner = ctx.get("owner")
        try:
            review_result = await review(task, raw_path or path, risk_summary, diff,
                                         owner=owner, model=str(_setting("agent_doubt_review_model", "auto")),
                                         timeout_s=_setting("agent_doubt_review_timeout_seconds", DEFAULT_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[doubt_review] review() raised for %s: %s", path, exc, exc_info=True)
            review_result = {"verdict": "ok", "concerns": [], "confidence": None,
                             "model": "", "elapsed_ms": 0, "error": str(exc)[:300]}
        if state is not None:
            state.store_review(cache_key, dh, review_result)
            state.note_review_run()

    block_mode = bool(_setting("agent_doubt_review_block", False))
    if block_mode and review_result.get("verdict") == "concerns" and not confirm_risky:
        return {
            "proceed": False,
            "result": {
                "error": block_message(review_result, risk_summary, raw_path or path),
                "exit_code": 1,
                "status": "doubt_review_blocked",
                "doubt_review": review_result,
                "risk": risk_summary,
            },
        }
    return {"proceed": True, "section": format_section(review_result, risk_summary), "review": review_result}
