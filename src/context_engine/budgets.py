"""
context_engine/budgets.py — how many tokens there are, and who gets them.

Two jobs, deliberately in one file because they are the same decision twice:

1. **Measure.**  `estimator_for(model)` returns something that can count a
   string.  There is no real tokenizer in this project for most local models,
   so the honest default is a *conservative* heuristic — one that overcounts —
   and every packet records which lane measured it (`ContextBudget.estimator`).
   A budget computed with an optimistic estimator is not a budget; it is a
   promise the provider will break at the worst possible moment.

2. **Divide.**  `resolve_budget()` turns "the model has a window" into
   "the input may use N tokens", and `allocate()` splits N between sections
   according to the intent.  §6.4 of the plan gives starting percentages and
   then says not to apply them blindly, which is why they are named profiles
   rather than constants: a code review needs diff and symbols, an image
   generation needs the recipe, a two-line chat needs almost nothing.

The rule that matters more than either: reserves come off the top.  Output and
tool results are not what is left over after context; context is what is left
over after them.  A model with a perfect context and no room to answer has
been handed a very expensive way to say nothing.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from .contracts import (
    MANDATORY_SECTIONS,
    SECTION_KINDS,
    ContextBudget,
)

# Chars per token.  3.0 is deliberately below the ~3.3 that
# `src.model_context.estimate_tokens` assumes: this one is used to decide what
# fits, so it has to err towards "fits less".  The app-parity lane below keeps
# the old number for shadow comparisons.
CONSERVATIVE_CHARS_PER_TOKEN = 3.0
APP_PARITY_CHARS_PER_TOKEN = 1.0 / 0.3  # what estimate_tokens() has always used

#: Per-message overhead for role/delimiter framing, same figure the rest of the
#: app uses so that shadow packets and the context ledger are comparable.
MESSAGE_OVERHEAD_TOKENS = 4

#: One image block, as charged by `src.model_context`.  Kept in sync by
#: `tests/test_context_engine_budgets.py`, which reads the constant from there.
IMAGE_BLOCK_TOKENS = 1200

#: Floors.  A window we cannot prove is not a window we may spend.
DEFAULT_UNKNOWN_WINDOW = 8192
MIN_OUTPUT_RESERVE = 512
MIN_INPUT_BUDGET = 512


# ── measuring ──────────────────────────────────────────────────────────────

class TokenEstimator(Protocol):
    """Anything that can price a string.  `name` ends up in the packet, so a
    packet compiled six months ago still says how it was measured."""

    name: str

    def count(self, text: str) -> int: ...

    def count_messages(self, messages: Sequence[Mapping[str, Any]]) -> int: ...


class _BaseEstimator:
    name = "heuristic"
    chars_per_token = CONSERVATIVE_CHARS_PER_TOKEN

    def count(self, text: str) -> int:
        if not text:
            return 0
        return int(math.ceil(len(text) / self.chars_per_token))

    def count_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        total = 0
        for message in messages or ():
            if not isinstance(message, Mapping):
                continue
            total += MESSAGE_OVERHEAD_TOKENS
            total += self._count_content(message.get("content"))
            for call in message.get("tool_calls") or ():
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function") or {}
                if isinstance(function, Mapping):
                    total += self.count(str(function.get("name") or ""))
                    total += self.count(str(function.get("arguments") or ""))
                total += MESSAGE_OVERHEAD_TOKENS
        return total

    def _count_content(self, content: Any) -> int:
        if content is None:
            return 0
        if isinstance(content, str):
            return self.count(content)
        if isinstance(content, (list, tuple)):
            total = 0
            for block in content:
                if isinstance(block, Mapping):
                    kind = str(block.get("type") or "")
                    if kind in ("image_url", "image", "input_image"):
                        total += IMAGE_BLOCK_TOKENS
                        continue
                    total += self.count(str(block.get("text") or ""))
                elif isinstance(block, str):
                    total += self.count(block)
            return total
        return self.count(str(content))


class ConservativeEstimator(_BaseEstimator):
    """The default.  Overcounts on purpose; see the module docstring."""

    name = "heuristic"
    chars_per_token = CONSERVATIVE_CHARS_PER_TOKEN


class AppParityEstimator(_BaseEstimator):
    """Reproduces `src.model_context.estimate_tokens` exactly.

    "Exactly" is the whole point and it costs a second implementation, because
    the conservative lane and the app lane disagree in three places that each
    look like rounding and are not:

    - the app truncates (`int(n * 0.3)`), this file's default rounds up;
    - the app charges a tool call as ONE truncation over `name + arguments`,
      not two;
    - the app counts a content block only when its `type` is `"text"`, so a
      bare string inside a content list is free there and is not here.

    A shadow report whose difference is an artefact of two different rulers is
    worse than no shadow report: someone would spend an afternoon chasing a
    120-token gap that was the measurement. `tests/test_context_engine_budgets.py`
    pins this against the real function on generated inputs.
    """

    name = "heuristic_app_parity"
    chars_per_token = APP_PARITY_CHARS_PER_TOKEN

    def count(self, text: str) -> int:
        if not text:
            return 0
        return int(len(text) * 0.3)

    def count_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        total = 0
        for message in messages or ():
            if not isinstance(message, Mapping):
                continue
            total += MESSAGE_OVERHEAD_TOKENS
            content = message.get("content", "")
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, (list, tuple)):
                for block in content:
                    if not isinstance(block, Mapping):
                        continue
                    kind = block.get("type")
                    if kind == "text":
                        total += self.count(str(block.get("text", "") or ""))
                    elif kind in ("image_url", "image", "input_image"):
                        total += IMAGE_BLOCK_TOKENS
            calls = message.get("tool_calls")
            if isinstance(calls, (list, tuple)):
                for call in calls:
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function")
                    function = function if isinstance(function, Mapping) else call
                    name = function.get("name", "") or ""
                    args = function.get("arguments", "") or ""
                    if not isinstance(args, str):
                        args = str(args)
                    total += MESSAGE_OVERHEAD_TOKENS
                    total += self.count(str(name) + args)
        return total


class TokenizerEstimator(_BaseEstimator):
    """The exact lane, when a real tokenizer file is available on disk.

    Deliberately *not* a download: this project runs offline by design, and a
    context compiler that blocks on a network fetch during the hot path is a
    worse failure than an approximate count.  Point
    `ODYSSEUS_TOKENIZER_MAP` at a JSON `{"model-substring": "path/to/tokenizer.json"}`
    and matching models get counted exactly; everything else falls back and
    says so."""

    name = "exact"

    def __init__(self, tokenizer: Any, label: str = "exact") -> None:
        self._tokenizer = tokenizer
        self.name = label

    def count(self, text: str) -> int:
        if not text:
            return 0
        try:
            return len(self._tokenizer.encode(text).ids)
        except Exception:
            # A tokenizer that throws once will throw again; the caller gets a
            # conservative number rather than an exception in the hot path.
            return int(math.ceil(len(text) / CONSERVATIVE_CHARS_PER_TOKEN))


_ESTIMATOR_LOCK = threading.RLock()
_TOKENIZER_CACHE: Dict[str, Optional[TokenEstimator]] = {}
_CONSERVATIVE = ConservativeEstimator()
_APP_PARITY = AppParityEstimator()


def _tokenizer_map() -> Dict[str, str]:
    raw = os.getenv("ODYSSEUS_TOKENIZER_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str)}


def _load_tokenizer(path: str) -> Optional[Any]:
    try:
        from tokenizers import Tokenizer  # type: ignore
    except Exception:
        return None
    try:
        return Tokenizer.from_file(path)
    except Exception:
        return None


def estimator_for(model: str = "") -> TokenEstimator:
    """The estimator for one model.  Cached per model string: loading a
    tokenizer costs tens of milliseconds and the hot path runs it per turn."""
    key = (model or "").strip().lower()
    with _ESTIMATOR_LOCK:
        if key in _TOKENIZER_CACHE:
            return _TOKENIZER_CACHE[key] or _CONSERVATIVE
        chosen: Optional[TokenEstimator] = None
        for fragment, path in _tokenizer_map().items():
            if fragment.lower() in key and os.path.isfile(path):
                tokenizer = _load_tokenizer(path)
                if tokenizer is not None:
                    chosen = TokenizerEstimator(tokenizer, f"exact:{os.path.basename(path)}")
                break
        _TOKENIZER_CACHE[key] = chosen
        return chosen or _CONSERVATIVE


def app_parity_estimator() -> TokenEstimator:
    return _APP_PARITY


def reset_estimator_cache() -> None:
    """For tests, and for the settings screen when the map changes."""
    with _ESTIMATOR_LOCK:
        _TOKENIZER_CACHE.clear()


# ── dividing ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BudgetProfile:
    """A named split of the input budget.

    `shares` need not sum to 1: whatever is unallocated is slack the compiler
    hands back to whichever section overflows first, in priority order.  That
    is on purpose — a profile that sums to exactly 1 leaves no room for a
    mandatory section to grow by one line without evicting something."""

    profile_id: str
    shares: Mapping[str, float]
    priorities: Mapping[str, int]
    description: str = ""

    def share(self, section: str) -> float:
        return float(self.shares.get(section, 0.0))

    def priority(self, section: str) -> int:
        if section in self.priorities:
            return int(self.priorities[section])
        return 90 if section in MANDATORY_SECTIONS else 40


#: Priorities are "who survives a trim", not "who is printed first".  Rendering
#: order is fixed by `SECTION_KINDS`; eviction order is this.
_BASE_PRIORITIES: Dict[str, int] = {
    "system_constraints": 100,
    "role_and_permissions": 98,
    "active_goal": 95,
    "current_state": 92,
    "decisions": 90,
    "recent_messages": 80,
    "project_rules": 70,
    "tool_guidance": 65,
    "code_map": 55,
    "retrieved_documents": 50,
    "retrieved_memory": 45,
    "past_experiences": 40,
    "peer_findings": 38,
    "multimodal_recipes": 35,
}

PROFILES: Dict[str, BudgetProfile] = {
    # §6.4's starting point, for a coding/agentic turn.
    "balanced_v1": BudgetProfile(
        profile_id="balanced_v1",
        description="Agentic default: instructions, goal, evidence, a little memory.",
        shares={
            "system_constraints": 0.06,
            "role_and_permissions": 0.04,
            "active_goal": 0.08,
            "current_state": 0.05,
            "decisions": 0.05,
            "recent_messages": 0.14,
            "project_rules": 0.07,
            "code_map": 0.10,
            "retrieved_documents": 0.15,
            "retrieved_memory": 0.08,
            "past_experiences": 0.05,
            "peer_findings": 0.03,
            "multimodal_recipes": 0.0,
            "tool_guidance": 0.05,
        },
        priorities=_BASE_PRIORITIES,
    ),
    # A plain conversation.  RAG and code map are close to free here because
    # pulling five document chunks into "what's the weather like" is the exact
    # failure the plan opens with.
    "conversation_v1": BudgetProfile(
        profile_id="conversation_v1",
        description="Chat: history and instructions, almost no retrieval.",
        shares={
            "system_constraints": 0.08,
            "role_and_permissions": 0.04,
            "active_goal": 0.04,
            "current_state": 0.02,
            "decisions": 0.02,
            "recent_messages": 0.45,
            "project_rules": 0.05,
            "code_map": 0.0,
            "retrieved_documents": 0.10,
            "retrieved_memory": 0.10,
            "past_experiences": 0.02,
            "peer_findings": 0.0,
            "multimodal_recipes": 0.0,
            "tool_guidance": 0.02,
        },
        priorities=_BASE_PRIORITIES,
    ),
    "code_review_v1": BudgetProfile(
        profile_id="code_review_v1",
        description="Review: diff, symbols and tests beat memory every time.",
        shares={
            "system_constraints": 0.05,
            "role_and_permissions": 0.03,
            "active_goal": 0.06,
            "current_state": 0.08,
            "decisions": 0.06,
            "recent_messages": 0.08,
            "project_rules": 0.10,
            "code_map": 0.22,
            "retrieved_documents": 0.16,
            "retrieved_memory": 0.05,
            "past_experiences": 0.07,
            "peer_findings": 0.02,
            "multimodal_recipes": 0.0,
            "tool_guidance": 0.02,
        },
        priorities=_BASE_PRIORITIES,
    ),
    "research_v1": BudgetProfile(
        profile_id="research_v1",
        description="Research: documents dominate, code map is noise.",
        shares={
            "system_constraints": 0.05,
            "role_and_permissions": 0.03,
            "active_goal": 0.07,
            "current_state": 0.03,
            "decisions": 0.04,
            "recent_messages": 0.10,
            "project_rules": 0.03,
            "code_map": 0.0,
            "retrieved_documents": 0.40,
            "retrieved_memory": 0.10,
            "past_experiences": 0.05,
            "peer_findings": 0.05,
            "multimodal_recipes": 0.0,
            "tool_guidance": 0.02,
        },
        priorities=_BASE_PRIORITIES,
    ),
    "media_v1": BudgetProfile(
        profile_id="media_v1",
        description="Image/video/audio: the recipe and its references are the context.",
        shares={
            "system_constraints": 0.06,
            "role_and_permissions": 0.03,
            "active_goal": 0.10,
            "current_state": 0.04,
            "decisions": 0.04,
            "recent_messages": 0.12,
            "project_rules": 0.03,
            "code_map": 0.0,
            "retrieved_documents": 0.05,
            "retrieved_memory": 0.08,
            "past_experiences": 0.05,
            "peer_findings": 0.0,
            "multimodal_recipes": 0.35,
            "tool_guidance": 0.03,
        },
        priorities={**_BASE_PRIORITIES, "multimodal_recipes": 75},
    ),
    # Voice: the same selection as chat, but a tight ceiling — a spoken turn
    # that takes nine seconds to compile has already failed.
    "voice_v1": BudgetProfile(
        profile_id="voice_v1",
        description="Voice: chat's shape, half the room, latency over recall.",
        shares={
            "system_constraints": 0.10,
            "role_and_permissions": 0.05,
            "active_goal": 0.06,
            "current_state": 0.04,
            "decisions": 0.03,
            "recent_messages": 0.45,
            "project_rules": 0.05,
            "code_map": 0.0,
            "retrieved_documents": 0.08,
            "retrieved_memory": 0.10,
            "past_experiences": 0.02,
            "peer_findings": 0.0,
            "multimodal_recipes": 0.0,
            "tool_guidance": 0.02,
        },
        priorities=_BASE_PRIORITIES,
    ),
}

#: Which profile an intent picks when the caller does not name one.  Unknown
#: intents get the agentic default rather than an error: a new intent should
#: degrade to "reasonable", not to "no context".
INTENT_PROFILES: Dict[str, str] = {
    "chat": "conversation_v1",
    "casual": "conversation_v1",
    "code_change": "balanced_v1",
    "bugfix": "balanced_v1",
    "implement": "balanced_v1",
    "review": "code_review_v1",
    "code_review": "code_review_v1",
    "research": "research_v1",
    "deep_research": "research_v1",
    "image": "media_v1",
    "video": "media_v1",
    "audio": "media_v1",
    "media": "media_v1",
    "voice": "voice_v1",
}


def profile_for(*, profile_id: str = "", intent: str = "") -> BudgetProfile:
    """An explicit profile wins; otherwise the intent picks one."""
    if profile_id and profile_id in PROFILES:
        return PROFILES[profile_id]
    mapped = INTENT_PROFILES.get((intent or "").strip().lower(), "")
    if mapped in PROFILES:
        return PROFILES[mapped]
    return PROFILES["balanced_v1"]


def resolve_budget(*, model: str = "", context_length: int = 0,
                   window_known: bool = False,
                   max_output_tokens: int = 0,
                   tool_schema_tokens: int = 0,
                   configured_input_budget: int = 0,
                   hard_max: int = 200_000) -> ContextBudget:
    """Turn a model's window into an input budget, reserves first.

    `window_known=False` means nobody could prove the window — `budget_context_for_model`
    returns 0 in that case — and the answer is a small, safe floor rather than
    the 128k default the provider tables guess at.  Spending a window you
    cannot prove is how a turn dies at round nine with the tools already run."""
    window = int(context_length or 0)
    if window <= 0 or not window_known:
        window = min(window or DEFAULT_UNKNOWN_WINDOW, DEFAULT_UNKNOWN_WINDOW)
        window_known = False

    reserved_output = max(int(max_output_tokens or 0), MIN_OUTPUT_RESERVE)
    reserved_tools = max(int(tool_schema_tokens or 0), 0)

    # Reserves may not eat the whole window.  If they would, they are clamped
    # to 60% of it and the caller finds out through `input_budget` being tiny
    # rather than through a negative number.
    if reserved_output + reserved_tools > int(window * 0.6):
        room = max(int(window * 0.6), MIN_OUTPUT_RESERVE)
        if reserved_tools >= room:
            reserved_tools = max(room - MIN_OUTPUT_RESERVE, 0)
            reserved_output = MIN_OUTPUT_RESERVE
        else:
            reserved_output = max(room - reserved_tools, MIN_OUTPUT_RESERVE)

    available = max(window - reserved_output - reserved_tools, 0)
    budget = available
    if configured_input_budget and configured_input_budget > 0:
        budget = min(budget, int(configured_input_budget))
    budget = min(budget, int(hard_max or 200_000))
    if available >= MIN_INPUT_BUDGET:
        budget = max(budget, MIN_INPUT_BUDGET)

    return ContextBudget(
        max_tokens=window,
        reserved_output=reserved_output,
        reserved_tools=reserved_tools,
        input_budget=budget,
        estimator=estimator_for(model).name,
        window_known=bool(window_known),
    )


def allocate(budget: ContextBudget, profile: BudgetProfile, *,
             present: Optional[Iterable[str]] = None) -> Dict[str, int]:
    """Split `budget.input_budget` between sections.

    Only sections that actually have candidates get a share: handing 15% to
    `multimodal_recipes` on a Python bugfix wastes it, because unallocated
    slack is redistributed and a reserved-but-empty section's share is not."""
    kinds = [k for k in SECTION_KINDS if present is None or k in set(present)]
    if not kinds or budget.input_budget <= 0:
        return {k: 0 for k in SECTION_KINDS}

    weights = {k: max(profile.share(k), 0.0) for k in kinds}
    total_weight = sum(weights.values())
    out: Dict[str, int] = {k: 0 for k in SECTION_KINDS}
    if total_weight <= 0:
        even = budget.input_budget // max(len(kinds), 1)
        for kind in kinds:
            out[kind] = even
        return out

    for kind in kinds:
        out[kind] = int(budget.input_budget * (weights[kind] / total_weight))

    # Integer division loses a few tokens; give them to the highest-priority
    # section present rather than dropping them on the floor.
    spent = sum(out.values())
    leftover = budget.input_budget - spent
    if leftover > 0 and kinds:
        top = max(kinds, key=lambda k: (profile.priority(k), weights[k]))
        out[top] += leftover
    return out


def tool_schema_tokens(schemas: Sequence[Any], *, model: str = "") -> int:
    """What the tool list costs.  Serialised the same way the provider will
    serialise it, because a tool list measured as prose undercounts by a third."""
    if not schemas:
        return 0
    estimator = estimator_for(model)
    try:
        blob = json.dumps(list(schemas), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        blob = str(schemas)
    return estimator.count(blob)


def section_order(kinds: Iterable[str]) -> List[str]:
    """Rendering order.  Fixed by `SECTION_KINDS` so that two packets for the
    same turn differ in content, never in layout — a diff between them should
    show what changed, not that the sections moved."""
    wanted = set(kinds)
    return [k for k in SECTION_KINDS if k in wanted]


__all__ = [
    "CONSERVATIVE_CHARS_PER_TOKEN", "APP_PARITY_CHARS_PER_TOKEN",
    "MESSAGE_OVERHEAD_TOKENS", "IMAGE_BLOCK_TOKENS",
    "DEFAULT_UNKNOWN_WINDOW", "MIN_OUTPUT_RESERVE", "MIN_INPUT_BUDGET",
    "TokenEstimator", "ConservativeEstimator", "AppParityEstimator",
    "TokenizerEstimator", "estimator_for", "app_parity_estimator",
    "reset_estimator_cache",
    "BudgetProfile", "PROFILES", "INTENT_PROFILES", "profile_for",
    "resolve_budget", "allocate", "tool_schema_tokens", "section_order",
]
