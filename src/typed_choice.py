"""src/typed_choice.py — typed choice decisions.

Lets internal code ask a local model to pick among a small, fixed set of
options in ONE forward pass, by reading the log-probability the model put on
each option's letter token, instead of generating a sentence and parsing it.

Why this exists: a free-text "answer yes or no" call pays for a whole
generation (and sometimes still can't be parsed — the classic "Sure, I think
it's..." problem), and its answer carries no real confidence signal. Asking
for exactly one token, with a grammar that makes any other token impossible,
gets the same decision in a fraction of the tokens and turns the option
log-probabilities into a genuine (if uncalibrated) relative-preference score.

Wire shape
----------
``typed_choice()`` labels each option A, B, C... and asks a llama.cpp server's
OpenAI-compatible ``/v1/chat/completions`` endpoint for exactly one token,
constrained by a GBNF grammar to the option letters, with
``logprobs``/``top_logprobs`` requested so the full distribution over letters
comes back on the FIRST call — no retry, no re-prompting. When the server
does not return logprobs for the position (seen on some non-llama.cpp
OpenAI-compatible servers, including Ollama's ``/v1`` surface on older
builds), this falls back to reading the generated letter with probability
1.0 and says so (``method: "generated"``).

The probabilities this returns are a CONDITIONAL OPTION SCORE — how much more
probability mass the model put on this token than the others, given this
exact prompt — never a calibrated confidence that the choice is correct.
Every result says this explicitly (``probability_status``) so a caller does
not quietly treat 0.97 as "97% likely to be right".

This module never raises for a normal failure (network error, timeout, no
usable letter in the response): the result dict carries an ``error`` field
instead. It DOES let a genuine programming error through (e.g. a caller
passing an empty ``options`` list), because that is not something a retry or
a fallback could ever fix.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Never say more about a raw model score than this — it is a relative
#: preference over the offered options, not a calibrated confidence.
PROBABILITY_STATUS = "conditional option score; not a calibrated confidence"

MAX_OPTIONS = 26
_LETTERS = [chr(ord("A") + i) for i in range(MAX_OPTIONS)]


# ---------------------------------------------------------------------------
# Pure helpers — unit-testable with no network / event loop involved.
# ---------------------------------------------------------------------------


def letters_for(count: int) -> List[str]:
    """The A, B, C... labels for ``count`` options, capped at 26."""
    return _LETTERS[: max(0, min(int(count), MAX_OPTIONS))]


def build_prompt(question: str, options: List[str], context: str = "") -> str:
    """The exact prompt sent for a typed choice: context, question, the
    lettered options, and a closing instruction to answer with the letter
    only. Kept as a standalone function so a test (or a caller building its
    own request) can see exactly what was asked."""
    letters = letters_for(len(options))
    lines: List[str] = []
    ctx = str(context or "").strip()
    if ctx:
        lines.append(ctx)
        lines.append("")
    lines.append(str(question or "").strip())
    lines.append("")
    for letter, option in zip(letters, options):
        lines.append(f"{letter}) {str(option)}")
    lines.append("")
    lines.append("Answer with the letter only.")
    return "\n".join(lines)


def build_grammar(letters: List[str]) -> str:
    """A GBNF grammar restricting the output to exactly one of ``letters``.

    ``root ::= "A" | "B" | ...`` — llama.cpp compiles this into a token mask,
    so the model literally cannot emit anything else, whatever the sampler
    would otherwise have picked.
    """
    alts = " | ".join(f'"{letter}"' for letter in letters)
    return f"root ::= {alts}"


def score_from_top_logprobs(top_logprobs: Any, letters: List[str]) -> Dict[str, float]:
    """Turn one response position's ``top_logprobs`` list into a probability
    per option letter, softmax-renormalised over the letters actually
    present. A letter the server never surfaced (it fell outside
    ``top_logprobs``, or the model put ~0 mass on it) gets probability 0.0 —
    never an invented small number.

    ``top_logprobs`` is the OpenAI-shaped list of ``{"token": ..., "logprob":
    ...}`` entries for one output position. A token is mapped to a letter by
    stripping surrounding whitespace only (case-sensitive: `" A"` and `"a"`
    are not the same option) — llama.cpp/most tokenizers emit the option
    letter itself, sometimes with a leading space from the tokenizer's word
    boundary, but never a different case for a single capital-letter
    grammar answer.
    """
    letters = list(letters or [])
    probs = {letter: 0.0 for letter in letters}
    if not letters:
        return probs

    best_logprob: Dict[str, float] = {}
    for entry in top_logprobs or []:
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        logprob = entry.get("logprob")
        if not isinstance(token, str) or not isinstance(logprob, (int, float)):
            continue
        letter = token.strip()
        if letter not in probs:
            continue
        if letter not in best_logprob or logprob > best_logprob[letter]:
            best_logprob[letter] = float(logprob)

    if not best_logprob:
        return probs

    top = max(best_logprob.values())
    exp_vals = {letter: math.exp(lp - top) for letter, lp in best_logprob.items()}
    total = sum(exp_vals.values())
    if total <= 0:
        return probs
    for letter, value in exp_vals.items():
        probs[letter] = value / total
    return probs


def parse_letter(text: Any, letters: List[str]) -> Optional[str]:
    """Best-effort recovery of a single option letter out of a short piece
    of generated text (the ``generated`` fallback path). ``None`` when no
    letter from ``letters`` can be found."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw in letters:
        return raw
    import re
    m = re.search(r"\b([A-Za-z])\b", raw)
    if m:
        cand = m.group(1).upper()
        if cand in letters:
            return cand
    cand = raw[0].upper()
    if cand in letters:
        return cand
    return None


def _margin(probabilities: Dict[str, float]) -> float:
    ordered = sorted(probabilities.values(), reverse=True)
    if len(ordered) < 2:
        return ordered[0] if ordered else 0.0
    return round(ordered[0] - ordered[1], 6)


def _prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()


def _messages_for(prompt: str, system: Optional[str]) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    if system and str(system).strip():
        messages.append({"role": "system", "content": str(system)})
    messages.append({"role": "user", "content": prompt})
    return messages


def _default_endpoint(url: Optional[str], model: Optional[str]):
    """Resolve a missing url/model the same way other internal, tool-less
    passes do (``src/context_compactor.py``, ``src/task_endpoint.py``):
    ``src.endpoint_resolver.resolve_endpoint("utility", ...)``, which reads
    the Utility endpoint/model settings and falls back to the Default Chat
    Model when Utility is unset.

    ``resolve_endpoint`` returns the fully-built chat-completions URL for
    whatever provider is configured (``.../v1/chat/completions`` for an
    OpenAI-compatible server, ``.../api/chat`` for native Ollama). This
    module always talks to the OpenAI-compatible ``/v1/chat/completions``
    surface (the one that can return per-token logprobs + a GBNF grammar),
    so a resolved URL is trimmed back down to its base and
    ``/v1/chat/completions`` is appended fresh — same as an explicitly
    passed ``url``.
    """
    if url and model:
        return url, model, {}
    try:
        from src.endpoint_resolver import resolve_endpoint
        chat_url, resolved_model, headers = resolve_endpoint(
            "utility", fallback_url=url, fallback_model=model,
        )
    except Exception:  # noqa: BLE001 - never let settings/DB trouble break this
        logger.debug("[typed_choice] endpoint resolution failed", exc_info=True)
        return url, model, {}

    resolved_url = url
    if not resolved_url and chat_url:
        base = str(chat_url)
        for suffix in ("/v1/chat/completions", "/chat/completions", "/api/chat", "/api/generate"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        resolved_url = base
    return resolved_url, (model or resolved_model), (headers or {})


async def _post_chat_completion(base_url: str, model: str, payload: Dict[str, Any],
                                 headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    """POST ``payload`` to ``{base_url}/v1/chat/completions`` under the same
    local-model gate/engine-swap seam every other local call in this repo
    uses (``src.llm_core._local_model_slot``), so a typed choice never jumps
    the queue in front of foreground chat and never wakes an engine the gate
    would otherwise leave asleep."""
    from src import llm_core

    chat_url = f"{str(base_url).rstrip('/')}/v1/chat/completions"
    client = llm_core._get_http_client()
    async with llm_core._local_model_slot(base_url, model, workload="foreground"):
        response = await client.post(chat_url, json=payload, headers=headers or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _build_error(question: str, options: List[str], reason: str, *, model: str = "",
                  elapsed_ms: float = 0.0) -> Dict[str, Any]:
    letters = letters_for(len(options))
    prompt = build_prompt(question, options, "")
    return {
        "choice": None,
        "index": None,
        "letter": None,
        "probabilities": {opt: 0.0 for opt in options[: len(letters)]},
        "margin": 0.0,
        "method": None,
        "probability_status": PROBABILITY_STATUS,
        "prompt_sha256": _prompt_sha256(prompt),
        "model": model,
        "elapsed_ms": round(elapsed_ms, 2),
        "error": reason,
    }


async def typed_choice(question: str, options: List[str], *, context: str = "",
                        url: Optional[str] = None, model: Optional[str] = None,
                        system: Optional[str] = None, timeout: float = 30) -> Dict[str, Any]:
    """Ask a local model to choose among ``options`` in one forward pass.

    Returns a dict (never raises for a normal failure):
    ``{"choice", "index", "letter", "probabilities", "margin", "method",
    "probability_status", "prompt_sha256", "model", "elapsed_ms"}``, plus an
    ``"error"`` key when the call could not produce a usable answer.
    """
    options = list(options or [])
    if not options:
        raise ValueError("typed_choice: 'options' must be a non-empty list")
    if len(options) > MAX_OPTIONS:
        options = options[:MAX_OPTIONS]

    letters = letters_for(len(options))
    prompt = build_prompt(question, options, context)
    prompt_sha = _prompt_sha256(prompt)

    resolved_url, resolved_model, headers = _default_endpoint(url, model)
    if not resolved_url or not resolved_model:
        return _build_error(question, options, "no local model endpoint configured")

    payload = {
        "model": resolved_model,
        "messages": _messages_for(prompt, system),
        "max_tokens": 1,
        "temperature": 0,
        "logprobs": True,
        "top_logprobs": 20,
        "grammar": build_grammar(letters),
    }

    t0 = time.monotonic()
    try:
        data = await _post_chat_completion(resolved_url, resolved_model, payload, headers, timeout)
    except Exception as exc:  # noqa: BLE001 - network/HTTP failure is a normal failure here
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug("[typed_choice] request failed: %s", exc)
        return _build_error(question, options, f"{type(exc).__name__}: {exc}"[:300],
                             model=resolved_model, elapsed_ms=elapsed_ms)
    elapsed_ms = (time.monotonic() - t0) * 1000

    choices = data.get("choices") if isinstance(data, dict) else None
    choice0 = choices[0] if choices else {}
    message = (choice0.get("message") or {}) if isinstance(choice0, dict) else {}
    generated_text = message.get("content") or ""

    logprobs_obj = (choice0.get("logprobs") or {}) if isinstance(choice0, dict) else {}
    content_lp = logprobs_obj.get("content") if isinstance(logprobs_obj, dict) else None
    top_logprobs = None
    if isinstance(content_lp, list) and content_lp and isinstance(content_lp[0], dict):
        top_logprobs = content_lp[0].get("top_logprobs")

    if top_logprobs:
        probabilities_by_letter = score_from_top_logprobs(top_logprobs, letters)
        method = "logprobs"
        letter = max(probabilities_by_letter, key=probabilities_by_letter.get)
        if probabilities_by_letter.get(letter, 0.0) <= 0.0:
            # The grammar's own choice never surfaced in top_logprobs (a
            # server that ignores top_logprobs' size, or a very flat
            # distribution) — fall back to the letter it actually generated.
            fallback_letter = parse_letter(generated_text, letters)
            if fallback_letter:
                letter = fallback_letter
    else:
        method = "generated"
        letter = parse_letter(generated_text, letters)
        probabilities_by_letter = {l: 0.0 for l in letters}
        if letter:
            probabilities_by_letter[letter] = 1.0

    if not letter or letter not in letters:
        return _build_error(question, options,
                             f"could not parse a valid option letter from the response "
                             f"(generated: {generated_text!r})",
                             model=resolved_model, elapsed_ms=elapsed_ms)

    index = letters.index(letter)
    probabilities = {options[letters.index(l)]: p for l, p in probabilities_by_letter.items()
                      if l in letters}

    return {
        "choice": options[index],
        "index": index,
        "letter": letter,
        "probabilities": probabilities,
        "margin": _margin(probabilities),
        "method": method,
        "probability_status": PROBABILITY_STATUS,
        "prompt_sha256": prompt_sha,
        "model": resolved_model,
        "elapsed_ms": round(elapsed_ms, 2),
    }


async def generated_choice(question: str, options: List[str], *, context: str = "",
                            url: Optional[str] = None, model: Optional[str] = None,
                            system: Optional[str] = None, timeout: float = 30) -> Dict[str, Any]:
    """Baseline for ``src/bench/typed_choice.py``: the same prompt, a normal
    (unconstrained) short generation, and text parsing for the letter — the
    way this decision would be made without this module. No grammar, no
    logprobs request; ``method`` is always ``"generated"``."""
    options = list(options or [])
    if not options:
        raise ValueError("generated_choice: 'options' must be a non-empty list")
    if len(options) > MAX_OPTIONS:
        options = options[:MAX_OPTIONS]

    letters = letters_for(len(options))
    prompt = build_prompt(question, options, context)
    prompt_sha = _prompt_sha256(prompt)

    resolved_url, resolved_model, headers = _default_endpoint(url, model)
    if not resolved_url or not resolved_model:
        return _build_error(question, options, "no local model endpoint configured")

    payload = {
        "model": resolved_model,
        "messages": _messages_for(prompt, system),
        "max_tokens": 8,
        "temperature": 0,
    }

    t0 = time.monotonic()
    try:
        data = await _post_chat_completion(resolved_url, resolved_model, payload, headers, timeout)
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.monotonic() - t0) * 1000
        return _build_error(question, options, f"{type(exc).__name__}: {exc}"[:300],
                             model=resolved_model, elapsed_ms=elapsed_ms)
    elapsed_ms = (time.monotonic() - t0) * 1000

    choices = data.get("choices") if isinstance(data, dict) else None
    choice0 = choices[0] if choices else {}
    message = (choice0.get("message") or {}) if isinstance(choice0, dict) else {}
    generated_text = message.get("content") or ""

    letter = parse_letter(generated_text, letters)
    if not letter:
        return _build_error(question, options,
                             f"could not parse a valid option letter from the response "
                             f"(generated: {generated_text!r})",
                             model=resolved_model, elapsed_ms=elapsed_ms)

    index = letters.index(letter)
    probabilities = {opt: (1.0 if i == index else 0.0) for i, opt in enumerate(options)}

    return {
        "choice": options[index],
        "index": index,
        "letter": letter,
        "probabilities": probabilities,
        "margin": 1.0,
        "method": "generated",
        "probability_status": PROBABILITY_STATUS,
        "prompt_sha256": prompt_sha,
        "model": resolved_model,
        "elapsed_ms": round(elapsed_ms, 2),
    }
