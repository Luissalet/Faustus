"""Adaptive input-token budget for the agent loop (#1170).

The agent soft-trims its input context to ``agent_input_token_budget`` (default
6000). The old computation was ``min(context_length or budget, budget)``, which
made the 6000 default a hard ceiling for *every* model — so a 128K or 1M context
model was silently capped at 6000 input tokens even though it can hold far more.

This derives the effective budget from the model's discovered context window when
the user has NOT set an explicit budget, while still honouring an explicit setting
exactly (clamped to the window). Pure and side-effect free so it is unit-testable.
"""

from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.model_context import IMAGE_BLOCK_TOKENS

# Generous ceiling so long-context models are unblocked without sending a
# pathologically large prompt every agent turn. Tunable; chosen to fully cover
# 128K models and give 1M models a large but bounded budget.
DEFAULT_HARD_MAX = 200_000
DEFAULT_BUDGET = 6000
DEFAULT_HEADROOM = 0.85


def parse_turn_input_budget(value):
    """An optional per-turn soft budget; never mutate the operator's defaults."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not str(value).strip().isdigit():
        raise ValueError("input_token_budget must be an integer between 4096 and 200000")
    result = int(value)
    if not 4096 <= result <= 200_000:
        raise ValueError("input_token_budget must be an integer between 4096 and 200000")
    return result


def _int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def compute_input_token_budget(
    configured: int,
    context_length: int,
    explicit: bool,
    *,
    default: int = DEFAULT_BUDGET,
    headroom: float = DEFAULT_HEADROOM,
    hard_max: int = DEFAULT_HARD_MAX,
) -> int:
    """Return the effective soft input-token budget.

    Args:
        configured: the value read from settings (may be the default).
        context_length: the model's discovered context window. Pass 0 when the
            window is unknown / only a bare fallback — auto-scaling then stays
            conservative instead of trusting an unproven window (review on #4122).
        explicit: True if the user set a NON-default budget. The default value is
            the "auto" sentinel (scale to the window); any other value is an
            explicit cap. (A deliberately-chosen default can't be distinguished
            from a materialized default by value, so the default reads as auto.)

    Rules:
        - Explicit user budget is honoured exactly, only clamped to the model's
          window when that window is known (the user's deliberate choice wins;
          ``hard_max`` is an auto-budget ceiling only — see #1230).
        - Otherwise (auto), scale to ``headroom`` of the context window, capped at
          ``hard_max`` — so long-context models use their capacity.
        - When the window is unknown (context_length <= 0), use the conservative
          ``default`` budget and do NOT scale off the fallback.
    """
    configured = _int_or_zero(configured)
    context_length = _int_or_zero(context_length)

    if explicit and configured > 0:
        return min(configured, context_length) if context_length > 0 else configured

    if context_length > 0:
        scaled = int(context_length * headroom)
        return max(1, min(scaled, hard_max))

    return configured if configured > 0 else default


def budget_is_explicit(configured: int, *, default: int = DEFAULT_BUDGET) -> bool:
    """Whether a configured agent_input_token_budget is a deliberate explicit cap.

    The default value is the "auto" sentinel (scale to the model's window), so only
    a NON-default positive value counts as explicit. This keys off the VALUE, not
    settings *presence* — the settings-save path materializes every default into
    settings.json, so a persisted default must still read as auto (the regression
    #4121 / #1230 are about). Centralised here so the materialized-default contract
    is unit-testable and can't silently regress to a presence check.
    """
    configured = int(configured or 0)
    return configured > 0 and configured != default


# ---------------------------------------------------------------------------
# CTX-01: real per-model, per-modality budget (docs/spec/v2 backlog)
#
# ``compute_input_token_budget`` above answers "how many input tokens may
# this turn spend" from a single already-resolved context_length. It stays
# exactly as it is (agent_loop.py's existing call site is unaffected — rule
# 3, no capability lost). ``budget_for`` answers the fuller question CTX-01
# actually asks: the REAL number, broken down by what eats it — the response
# the model is about to produce, the tool schemas sent alongside the
# messages, and one modality's per-unit cost (an image, mainly) — with an
# explicit "unknown" wherever Faustus genuinely does not know, instead of a
# silent 0 that reads as "free" or a made-up number that reads as measured.
# ---------------------------------------------------------------------------

DEFAULT_RESPONSE_RESERVE = 1024
DEFAULT_TOOLS_RESERVE_FRACTION = 0.10
MIN_TOOLS_RESERVE = 256

# Per-image token cost by provider family. These are the providers' own
# published vision-pricing figures, not a Faustus guess:
#   - OpenAI (GPT-4o/4.1-class "high" detail): base 85 + 170 tokens per
#     512x512 tile; a typical ~1024x1024 screenshot lands near 765-1105
#     tokens (OpenAI's vision pricing guide). src/model_context.py already
#     charges every image a flat IMAGE_BLOCK_TOKENS (1200) for exactly this
#     family of models, so this table reuses that constant instead of
#     drifting a second number for the same thing.
#   - Anthropic (Claude vision): tokens ≈ (width_px * height_px) / 750
#     (Anthropic's vision guide). Without pixel dimensions the exact number
#     cannot be computed here, so the flat figure below is only the same
#     order of magnitude as a common ~1024x768 screenshot (≈1050 tokens).
#   - Google Gemini: images <=384x384 cost a flat 258 tokens; larger images
#     are tiled at 768x768 per 258-token tile (Gemini's "Understand and
#     count tokens" guide) — 258 is used here as the per-tile unit.
# Local/self-hosted VLMs (Ollama, llama.cpp, vLLM, ...) publish no per-image
# token cost at all, so no entry exists for them here on purpose: a caller
# asking about "ollama" gets ``None`` ("unknown"), never a number borrowed
# from an unrelated provider.
_PROVIDER_IMAGE_TOKENS: Dict[str, int] = {
    "openai": IMAGE_BLOCK_TOKENS,      # 1200 — see src/model_context.py
    "azure": IMAGE_BLOCK_TOKENS,       # same models, same API shape
    "copilot": IMAGE_BLOCK_TOKENS,     # GPT-4o-class under the hood
    "chatgpt-subscription": IMAGE_BLOCK_TOKENS,
    "anthropic": 1200,                 # flat fallback; see docstring above
    "google": 258,                     # per 768x768 tile
    "openrouter": IMAGE_BLOCK_TOKENS,  # aggregator; mixed backends, OpenAI-class default
}


def modality_unit_cost(provider: str, modality: str) -> Tuple[Optional[int], str]:
    """Per-unit token cost for one modality, and where the number came from.

    Returns ``(cost, source)``. ``cost`` is ``None`` — never ``0`` — when
    Faustus has no documented figure for this (provider, modality) pair;
    ``source`` always explains the answer, including the unknown case, so a
    caller can show "unknown" instead of silently budgeting the modality as
    free.
    """
    modality = (modality or "text").strip().lower()
    provider = (provider or "").strip().lower()
    if modality == "text":
        return None, "text is priced per character (see model_context.estimate_tokens), not per unit"
    if modality == "image":
        cost = _PROVIDER_IMAGE_TOKENS.get(provider)
        if cost is not None:
            return cost, f"documented estimate for provider={provider!r} (see _PROVIDER_IMAGE_TOKENS)"
        return None, f"no documented per-image token cost for provider={provider!r}"
    # audio/video/anything else: no provider Faustus talks to publishes a
    # stable per-second token cost that could be cited here, so this is
    # honestly unknown rather than an invented number.
    return None, f"no documented per-unit token cost for modality={modality!r}"


def budget_for(
    model_manifest: Mapping[str, Any],
    modality: str = "text",
    *,
    tool_schema_tokens: int = 0,
    response_reserve: Optional[int] = None,
) -> Dict[str, Any]:
    """The real, itemised token budget for one request (CTX-01).

    ``model_manifest`` is the caller's best knowledge of the model, built
    from ``src.model_context.get_context_length_known`` (never resolved
    here — this module stays pure/model-free so it is unit-testable without
    a network call):

    - ``context_length``: int, the discovered window (``0`` = nothing known).
    - ``context_known``: bool, True only when ``context_length`` was actually
      proven (endpoint-reported / matched in the known-models table), never
      a bare ``DEFAULT_CONTEXT`` fallback (mirrors
      ``get_context_length_known``'s own ``known`` flag).
    - ``context_measured``: bool, True only when the window was read LIVE
      from the serving endpoint (llama.cpp ``/slots``, Ollama ``/api/ps``, an
      API's own ``/models`` field) rather than a static model-card guess.
    - ``provider``: str, e.g. ``"openai"``/``"anthropic"``/``"google"`` (see
      ``src.llm_core._detect_provider``), used only to price ``modality``.

    ``tool_schema_tokens`` is the caller's own measured tool-schema size
    (e.g. from ``context_ledger._tool_tokens``); pass it when known so the
    tools reserve is exact rather than an estimated fraction of the window.

    Returns a dict with ``context_length``, ``context_source`` (``"measured"``
    / ``"known_default"`` / ``"unknown"`` — never invented), ``reserve_response``,
    ``reserve_tools``, ``tools_reserve_source``, ``input_budget`` (never 0 or
    negative when ``context_length`` ends up positive, which it always does —
    see below), ``modality``, ``modality_unit_cost`` (an int, or ``None``; see
    ``modality_unit_cost()``), ``modality_source``, ``provider``,
    ``warnings`` (plain-English notes for every place a number had to be
    estimated instead of measured), and ``estimated`` — a plain bool, True
    whenever ``input_budget`` rests on any non-measured number, so a caller
    (a log line, a UI) never has to infer "was this real?" from parsing
    ``context_source``/``warnings`` itself.
    """
    manifest = model_manifest if isinstance(model_manifest, Mapping) else {}
    context_length = _int_or_zero(manifest.get("context_length"))
    known = bool(manifest.get("context_known"))
    measured = bool(manifest.get("context_measured"))
    provider = str(manifest.get("provider") or "")

    warnings: List[str] = []
    if known and measured and context_length > 0:
        context_source = "measured"
    elif known and context_length > 0:
        context_source = "known_default"
    else:
        context_source = "unknown"
        if context_length <= 0:
            # Conservative, never 0 — same floor compute_input_token_budget
            # falls back to when the window is unknown (module docstring).
            context_length = DEFAULT_BUDGET
        warnings.append(
            "model context window is unknown; using a conservative "
            f"{context_length}-token budget instead of the model's real window"
        )

    reserve_response = _int_or_zero(response_reserve) if response_reserve is not None else DEFAULT_RESPONSE_RESERVE
    if reserve_response <= 0:
        reserve_response = DEFAULT_RESPONSE_RESERVE

    tool_schema_tokens = _int_or_zero(tool_schema_tokens)
    if tool_schema_tokens > 0:
        reserve_tools = tool_schema_tokens
        tools_reserve_source = "measured"
    else:
        reserve_tools = max(MIN_TOOLS_RESERVE, int(context_length * DEFAULT_TOOLS_RESERVE_FRACTION))
        tools_reserve_source = "estimated"

    input_budget = context_length - reserve_response - reserve_tools
    if input_budget < 1:
        # The default reserves genuinely do not fit a tiny/unknown window —
        # scale both down proportionally rather than returning a 0 or
        # negative budget (rule: never 0, never invented — a scaled-down
        # reserve is honestly derived from the real numbers involved).
        total_reserve = reserve_response + reserve_tools
        scale = (context_length - 1) / total_reserve if total_reserve > 0 else 0.0
        scale = max(0.0, scale)
        reserve_response = max(1, int(reserve_response * scale))
        reserve_tools = max(1, int(reserve_tools * scale))
        input_budget = max(1, context_length - reserve_response - reserve_tools)
        warnings.append("window too small for the default reserves; both were scaled down to fit")

    unit_cost, modality_source = modality_unit_cost(provider, modality)

    # CTX-01: a single explicit boolean a caller can branch on without
    # parsing `context_source`/`tools_reserve_source`/`warnings` prose — the
    # acceptance's "no se comunica una cifra estimada como conteo exacto"
    # needs a literal flag, not a string a UI would have to pattern-match.
    # True whenever ANY input to `input_budget` was not a live-measured
    # number: the context window came from a model-card default rather than
    # the endpoint itself, the tool-schema reserve is a guessed fraction of
    # the window rather than a measured schema size, or (when a non-text
    # modality was asked about) its per-unit cost is a published flat figure
    # rather than one computed from this specific attachment's real size.
    modality_normalized = (modality or "text").strip().lower()
    estimated = (
        context_source != "measured"
        or tools_reserve_source == "estimated"
        or (modality_normalized != "text" and unit_cost is not None)
    )

    return {
        "context_length": context_length,
        "context_source": context_source,
        "reserve_response": reserve_response,
        "reserve_tools": reserve_tools,
        "tools_reserve_source": tools_reserve_source,
        "input_budget": input_budget,
        "modality": modality_normalized,
        "modality_unit_cost": unit_cost,
        "modality_source": modality_source,
        "provider": provider or "unknown",
        "warnings": warnings,
        "estimated": bool(estimated),
    }
