"""autonomy_budget.py — how far one agent turn may go before it must stop and
report back, instead of asking a person every few seconds or running forever.

TASK-06 / PERF-04 (parcial): `src/agent_settings_schema.py` already exposes
loose ceilings (`agent_max_tool_calls`, `agent_input_token_hard_max`,
`agent_subagent_depth`, several `*_timeout_seconds`), but nothing ties them
together into a single named posture a person can pick before a turn starts,
and nothing budgets active time, sub-agent count or spend on a paid endpoint
as SEPARATE limits that can each run out on their own. This module is that:

* `Budget` — six independent ceilings. ANY of them may be `None`/0 (=
  unlimited). Reaching one does not affect the others: a turn that is still
  under its token budget but has made its Nth tool call stops on
  `tool_calls`, not `tokens`.
* `Ledger` — what one turn has actually spent so far. `Ledger.check(budget)`
  returns the first `Exhausted` dimension it finds (fixed order, so two
  ledgers with the same numbers always report the same cause), or `None`.
* Three presets (`PRESETS`): `supervised` (medium budgets — the default),
  `bounded_autonomous` (roughly 3x), `read_only` (roughly 1/3, AND only
  read-effect tools are offered — see `read_only_disabled_names`).
* `build_checkpoint` — what `src/agent_loop.py` saves when a budget runs out
  mid-turn: the plan as structured steps (via `src/plan_state.py`), the
  files this turn actually touched, and a short "how far I got" note. The
  turn that reads it back resumes from here; nothing about it discards work
  or grants a wider permission than the turn already had.

`Ledger.active_seconds` is charged only for spans the caller explicitly adds
(model inference, tool execution) — never for wall-clock time. A human pause
(`ask_user`, a pending tool approval) ENDS the current turn in this codebase
and the answer arrives as a brand new call later; there is no "paused" clock
running inside a finished call to subtract from, so simply never charging
those pauses in the first place keeps the ledger honest without needing a
pause/resume state machine.

All six dimensions derive from settings that exist and are read live:
`max_tool_calls`/`max_tokens` from `agent_max_tool_calls`/
`agent_input_token_hard_max`, and the other four (`max_active_seconds`,
`max_subagents`, `max_remote_spend`, `max_memory_mb`) from their own
`agent_autonomy_max_*` settings, registered in `src.settings.DEFAULT_SETTINGS`
and `src.agent_settings_schema` the same way. The module constants below are
only the fallback used when a setting is absent or left at the "0 =
unlimited" sentinel (`_base_from_setting`) — still overridable per call via
`resolve_budget`'s `overrides`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from src.plan_state import from_markdown
from src.tool_capabilities import ToolEffect, capabilities_for_tool

PRESETS: tuple[str, ...] = ("supervised", "bounded_autonomous", "read_only")
DEFAULT_PRESET = "supervised"

#: One line of consequence per preset, for the Studio selector.
PRESET_CONSEQUENCE: Mapping[str, str] = {
    "supervised": "Asks before anything with an external or destructive effect; medium budgets.",
    "bounded_autonomous": "Runs without asking except for irreversible effects; higher budgets.",
    "read_only": "Only read tools are offered — nothing changes on disk or anywhere else; low budgets.",
}

_MULTIPLIER: Mapping[str, float] = {
    "supervised": 1.0,
    "bounded_autonomous": 3.0,
    "read_only": 1.0 / 3.0,
}

# Fallbacks for the two settings-backed dimensions, used only when the
# setting itself is unset/unlimited (0) — an unlimited base would otherwise
# make every preset unlimited, defeating the point of a "budget".
_FALLBACK_TOOL_CALLS = 40
_FALLBACK_TOKENS = 200_000

# Fallbacks for the four dimensions with no settings-backed ceiling yet.
_FALLBACK_ACTIVE_SECONDS = 900.0
_FALLBACK_SUBAGENTS = 4
_FALLBACK_REMOTE_SPEND = 20_000.0
_FALLBACK_MEMORY_MB = 2048.0

#: Effects a `read_only` turn may still use. Everything else (any write,
#: code execution, network egress beyond a brokered read, an external or UI
#: side effect, an admin change, anything destructive) is filtered out.
#: `USER_INTERACTION` stays in: `ask_user`/`update_plan`/`todowrite` change
#: nothing outside the chat itself.
READ_ONLY_ALLOWED_EFFECTS: frozenset[ToolEffect] = frozenset({
    ToolEffect.READ_PUBLIC,
    ToolEffect.READ_WORKSPACE,
    ToolEffect.READ_PRIVATE,
    ToolEffect.BROKERED_NETWORK_READ,
    ToolEffect.USER_INTERACTION,
})


def normalize_preset(value: Any) -> str:
    """A valid preset name, or `DEFAULT_PRESET` for anything else (unset,
    misspelled, a legacy client that never sends this field at all — that is
    the whole back-compat story for this parameter)."""
    name = str(value or "").strip().lower()
    return name if name in PRESETS else DEFAULT_PRESET


def preset_consequence(preset: Any) -> str:
    return PRESET_CONSEQUENCE.get(normalize_preset(preset), "")


def is_read_only_tool(name: Any) -> bool:
    """Whether `name` may still run under the `read_only` preset. Unknown
    tools (no entry in `src.tool_capabilities`, including any MCP tool this
    process has not classified) fail high — excluded, not allowed — the same
    "fail high" rule `tool_capabilities.capabilities_for_tool` documents for
    everything else."""
    if not isinstance(name, str) or not name:
        return False
    caps = capabilities_for_tool(name)
    return caps.effects <= READ_ONLY_ALLOWED_EFFECTS


def read_only_disabled_names(names: Iterable[Any]) -> set[str]:
    """Of `names`, the ones `read_only` must refuse — everything that is not
    `is_read_only_tool`. Callers fold this into `disabled_tools` /
    `tool_policy` BEFORE either the offered tool schemas or the execution
    gate are built from them, so offered == executable stays true (see
    tests/test_agent_loop_offer_execute_coherence.py)."""
    return {str(n) for n in names if n and not is_read_only_tool(n)}


@dataclass(frozen=True)
class Budget:
    """Six independent ceilings for one agent turn. `None` (or `0`, matching
    the rest of this codebase's "0 = unlimited" convention) means that
    dimension is never checked."""

    max_tool_calls: Optional[int] = None
    max_tokens: Optional[int] = None
    max_active_seconds: Optional[float] = None
    max_subagents: Optional[int] = None
    max_remote_spend: Optional[float] = None
    # Informative unless a real measurement is ever fed into a Ledger — this
    # process has no memory-per-turn instrumentation today, so in practice
    # this ceiling is carried but never trips.
    max_memory_mb: Optional[float] = None

    def as_dict(self) -> dict[str, Optional[float]]:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "max_active_seconds": self.max_active_seconds,
            "max_subagents": self.max_subagents,
            "max_remote_spend": self.max_remote_spend,
            "max_memory_mb": self.max_memory_mb,
        }

    def without_tool_calls(self) -> "Budget":
        """A copy with `max_tool_calls` cleared. `src/agent_loop.py` merges
        this budget's tool-call ceiling into the pre-existing
        `max_tool_calls`/`total_tool_calls` mechanism (the one
        `budget_exceeded` already used before this module existed) instead
        of running a second, independent counter for the same thing — so the
        copy this module's own `Ledger.check` sees during that loop excludes
        the dimension the older mechanism already owns."""
        return Budget(
            max_tool_calls=None,
            max_tokens=self.max_tokens,
            max_active_seconds=self.max_active_seconds,
            max_subagents=self.max_subagents,
            max_remote_spend=self.max_remote_spend,
            max_memory_mb=self.max_memory_mb,
        )


@dataclass(frozen=True)
class Exhausted:
    """What `Ledger.check` found: which dimension, how much was used, and
    against what limit — enough for both the SSE event and a log line to
    name the cause without the reader reconstructing it from two numbers."""

    kind: str
    used: float
    limit: float

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "used": self.used, "limit": self.limit}


def _base_from_setting(
    key: str, fallback: float, get_setting: Optional[Callable[[str, Any], Any]],
) -> float:
    """The configured value for `key` when it is set and not the "0 =
    unlimited" sentinel, else `fallback`. A preset multiplies THIS, so an
    unlimited setting does not make every preset unlimited."""
    if get_setting is not None:
        try:
            value = float(get_setting(key, fallback))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return fallback


def resolve_budget(
    preset: Any,
    *,
    get_setting: Optional[Callable[[str, Any], Any]] = None,
    overrides: Optional[Mapping[str, Any]] = None,
) -> Budget:
    """Build the `Budget` for `preset`. `max_tool_calls`/`max_tokens` scale
    the existing `agent_max_tool_calls`/`agent_input_token_hard_max`
    settings (read through `get_setting` exactly like every other reader in
    this codebase, so a machine with no settings file still gets sane
    numbers); the other four dimensions scale fixed fallbacks (see the
    module docstring). `overrides` replaces individual fields afterwards —
    the "sobreescribibles" part — by field name
    (`max_tool_calls`/`max_tokens`/`max_active_seconds`/`max_subagents`/
    `max_remote_spend`/`max_memory_mb`); unknown keys and `None` values are
    ignored."""
    name = normalize_preset(preset)
    mult = _MULTIPLIER[name]

    base_tool_calls = _base_from_setting("agent_max_tool_calls", _FALLBACK_TOOL_CALLS, get_setting)
    base_tokens = _base_from_setting("agent_input_token_hard_max", _FALLBACK_TOKENS, get_setting)
    base_active_seconds = _base_from_setting(
        "agent_autonomy_max_active_seconds", _FALLBACK_ACTIVE_SECONDS, get_setting)
    base_subagents = _base_from_setting(
        "agent_autonomy_max_subagents", _FALLBACK_SUBAGENTS, get_setting)
    base_remote_spend = _base_from_setting(
        "agent_autonomy_max_remote_spend", _FALLBACK_REMOTE_SPEND, get_setting)
    base_memory_mb = _base_from_setting(
        "agent_autonomy_max_memory_mb", _FALLBACK_MEMORY_MB, get_setting)

    values: dict[str, float] = {
        "max_tool_calls": max(1, round(base_tool_calls * mult)),
        "max_tokens": max(1, round(base_tokens * mult)),
        "max_active_seconds": max(1.0, round(base_active_seconds * mult, 1)),
        "max_subagents": max(0, round(base_subagents * mult)),
        "max_remote_spend": max(1.0, round(base_remote_spend * mult, 1)),
        "max_memory_mb": max(1.0, round(base_memory_mb * mult, 1)),
    }
    for key, value in (overrides or {}).items():
        if key in values and value is not None:
            values[key] = value
    return Budget(**values)


@dataclass
class Ledger:
    """What one turn has spent against a `Budget` so far. Every `add_*`
    method is additive and clamps negative input to zero — a caller passing
    a stray negative delta (a clock that went backwards, a malformed usage
    record) never makes the ledger's own total go down."""

    tool_calls: int = 0
    tokens: int = 0
    active_seconds: float = 0.0
    subagents: int = 0
    remote_spend: float = 0.0
    memory_mb: float = 0.0

    def add_tool_call(self, n: int = 1) -> None:
        self.tool_calls += max(0, int(n))

    def add_tokens(self, n: int) -> None:
        self.tokens += max(0, int(n))

    def add_active_seconds(self, seconds: float) -> None:
        """Charge real work only. Never call this for time spent waiting on
        `ask_user` or a pending tool approval — both end the turn in this
        codebase (see the module docstring), so the only way to keep a human
        pause off this clock is to never hand it a span that included one."""
        if seconds and seconds > 0:
            self.active_seconds += float(seconds)

    def add_subagents(self, n: int) -> None:
        self.subagents += max(0, int(n))

    def add_remote_spend(self, units: float) -> None:
        if units and units > 0:
            self.remote_spend += float(units)

    def set_memory_mb(self, mb: float) -> None:
        """Informative: the highest measurement seen, not a sum — memory
        usage is a level, not something that accumulates call over call."""
        self.memory_mb = max(self.memory_mb, float(mb or 0))

    def check(self, budget: Budget) -> Optional[Exhausted]:
        """The first dimension that has reached or passed its limit, in a
        fixed order (tool_calls, tokens, active_seconds, subagents,
        remote_spend, memory_mb) so the same numbers always name the same
        cause. `None` when nothing in `budget` is both set and reached."""
        for kind, used, limit in (
            ("tool_calls", self.tool_calls, budget.max_tool_calls),
            ("tokens", self.tokens, budget.max_tokens),
            ("active_seconds", self.active_seconds, budget.max_active_seconds),
            ("subagents", self.subagents, budget.max_subagents),
            ("remote_spend", self.remote_spend, budget.max_remote_spend),
            ("memory_mb", self.memory_mb, budget.max_memory_mb),
        ):
            if limit and used >= limit:
                return Exhausted(kind=kind, used=used, limit=limit)
        return None


def remote_spend_units(*, endpoint_cost_tracked: Any, input_tokens: int, output_tokens: int) -> float:
    """Estimated spend for one usage bucket, in the same "cost unit" the
    `max_remote_spend` budget is denominated in. A bucket whose endpoint is
    not cost-tracked (a local model, or a route this process never learned
    the cost of — see `endpoint_cost_tracked` on the usage buckets built by
    `src/agent_loop.py::_usage_bucket`) contributes nothing: this budget
    exists to bound spend on a PAID endpoint, not local inference."""
    if endpoint_cost_tracked is not True:
        return 0.0
    return max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))


def plan_snapshot(plan_update: Any) -> Optional[dict[str, Any]]:
    """The current plan, structured, for a checkpoint — `None` when there is
    none. `plan_update` is the payload `UpdatePlanTool` returns
    (`{"plan": markdown, "steps": [...], "revision", "warnings"}` —
    `src/agent_tools/interaction_tools.py`); when a caller already has the
    structured `steps` this just carries them through, and only falls back
    to parsing `plan` markdown via `src.plan_state.from_markdown` for a
    payload shaped without them."""
    if not isinstance(plan_update, Mapping):
        return None
    steps = plan_update.get("steps")
    if isinstance(steps, list):
        return {
            "steps": list(steps),
            "revision": plan_update.get("revision", 1),
            "warnings": list(plan_update.get("warnings") or []),
        }
    plan_text = str(plan_update.get("plan") or "").strip()
    if not plan_text:
        return None
    return from_markdown(plan_text).to_dict()


def build_checkpoint(
    *,
    plan_update: Any = None,
    touched_files: Iterable[str] = (),
    note: str = "",
) -> dict[str, Any]:
    """What a turn keeps when a budget runs out mid-work: the plan (if any),
    the files this turn actually touched, and a short account of how far it
    got. Never discards anything the turn already produced and never widens
    what the NEXT turn may do — a plain, inert snapshot for the turn that
    resumes from here to read."""
    return {
        "plan": plan_snapshot(plan_update),
        "touched_files": sorted({str(p) for p in touched_files if p}),
        "note": note,
    }
