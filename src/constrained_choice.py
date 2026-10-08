"""src/constrained_choice.py — pick ONE option from a closed set, valid by construction.

Why this exists
---------------
Several places in Faustus ask a model to *choose* (which research format, which
tool of N, which route) and then parse whatever prose comes back. That pays for
a generation and leaves the door open to a value that is not in the list
("shopping", "The category is product."). ``src/typed_decision.py`` reads the
next-token probabilities of lettered answers and ``src/typed_choice.py`` forces
one letter with a grammar; both are about *confidence*. This layer is about the
other half of OBJ-27 phase 1: make the answer itself legal, whatever the label
looks like, on whichever backend is already serving the model.

``choose_one(options, prompt)`` builds the constraint from the closed set and
sends it in the field the resolved backend understands:

* llama-server (a managed engine, or a loopback server answering ``/props``):
  a GBNF ``grammar`` — ``root ::= "a" | "b" ...`` — so the sampler can only emit
  one of the labels. The completion is the label and nothing else.
* Ollama (native ``/api/chat``, or its ``/v1`` surface moved there): ``format``
  with the JSON Schema ``{"type": "string", "enum": [labels]}``, sent
  through ``llm_core.llm_call_async(response_schema=...)`` so it keeps the
  existing routing, thinking switch, retries and usage accounting.
* anything else (a hosted API, another local server, an endpoint nobody could
  identify, or ``constrained_choice_backend = "free"``): plain generation, then
  a validated parse (exact, case-folded, unique whole-word match)
  and ONE repair call that restates the allowed answers. Strict providers
  reject unknown top-level fields, so a ``grammar`` is never sent to a server
  that was not positively identified as llama.cpp.

The result says which path answered (``path``), whether the constraint was
honoured (the raw output was exactly an option), whether a repair was needed,
and the per-attempt tokens and milliseconds. ``choice`` is ALWAYS one of the
options or ``None``; it is never a free string.

A request that the server rejects for carrying a grammar (HTTP 400/422) is
remembered for ten minutes and goes straight to the free-text path next time.

Etiquette — this is not an optional advisory helper (that is
``typed_decision``): the caller has already decided to spend a model call, so
the call goes through the local model gate like every other generation. It
never raises for a normal failure (network, timeout, unparsable answer); it
DOES raise ``ValueError`` for a programming error (empty or oversized option
set, empty prompt), because no fallback could ever fix that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Beyond this the closed set is a retrieval problem, not a choice, and the
#: prompt that lists it would dwarf the saving.
MAX_OPTIONS = 512
MAX_LABEL_CHARS = 200

PATH_LLAMACPP = "llamacpp_grammar"
PATH_OLLAMA = "ollama_format"
PATH_FREE = "free_text"
PATH_NONE = "none"

DEFAULTS: Dict[str, Any] = {
    # Master switch: False sends every choice down the free-text path
    # (validated parse + one repair), i.e. no constraint on any backend.
    "constrained_choice_enabled": True,
    # "auto" detects the backend from the URL; "llamacpp" / "ollama" force it;
    # "free" never constrains.
    "constrained_choice_backend": "auto",
}

_SCHEMA_KEY = "choice"
_REJECT_TTL_S = 600.0
_PROMPT_SUFFIX = "\n\nOptions: {options}\nReply with exactly one option."
_ANSWER_KEYS = ("choice", "answer", "label", "value", "category", "option", "result")


def _setting(key: str) -> Any:
    default = DEFAULTS.get(key)
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
    except Exception:  # noqa: BLE001 - unreadable settings = defaults
        return default
    return default if value is None else value


# ---------------------------------------------------------------------------
# Option set and constraint builders — pure
# ---------------------------------------------------------------------------


def normalize_options(options: Sequence[Any]) -> List[str]:
    """The cleaned closed set: strings, stripped, de-duplicated in order.

    Raises ``ValueError`` for an empty set, a blank label, a label that is too
    long, or a set larger than ``MAX_OPTIONS`` — all programming errors."""
    if options is None or isinstance(options, (str, bytes)):
        raise ValueError("constrained_choice: 'options' must be a list of labels")
    seen: Dict[str, None] = {}
    for raw in options:
        label = str(raw).strip()
        if not label:
            raise ValueError("constrained_choice: an option label is empty")
        if len(label) > MAX_LABEL_CHARS:
            raise ValueError(f"constrained_choice: an option label exceeds {MAX_LABEL_CHARS} characters")
        seen.setdefault(label, None)
    cleaned = list(seen)
    if not cleaned:
        raise ValueError("constrained_choice: 'options' must not be empty")
    if len(cleaned) > MAX_OPTIONS:
        raise ValueError(f"constrained_choice: at most {MAX_OPTIONS} options are supported "
                         f"(got {len(cleaned)})")
    return cleaned


def gbnf_literal(text: str) -> str:
    """A GBNF string literal for ``text``.

    ``\\`` and ``"`` are escaped, the usual control characters get their named
    escape and any other control character a ``\\xNN`` escape, so a label can
    never close the literal early or smuggle a rule. Printable non-ASCII stays
    as UTF-8, which the llama.cpp grammar parser decodes code point by code
    point."""
    out: List[str] = []
    for ch in str(text):
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append("\\x%02X" % code)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def build_gbnf(options: Sequence[str]) -> str:
    """``root ::= "a" | "b" | ...`` for the (already normalised) options.

    Labels that are prefixes of each other (``a`` / ``ab``) are fine: the
    grammar accepts either complete string, and the sampler may stop after the
    shorter one or continue into the longer."""
    opts = normalize_options(options)
    return "root ::= " + " | ".join(gbnf_literal(o) for o in opts)


def build_enum_schema(options: Sequence[str]) -> Dict[str, Any]:
    """The JSON Schema Ollama's ``format`` decodes under: a string that must be
    one of the labels. The answer is then ``"label"`` (the label and two
    quotes), which costs the same tokens as the bare label; an object wrapper
    ``{"choice": "label"}`` was measured against the stub at 3x the completion
    tokens. A server that refuses the shape answers 4xx and the call falls back
    to plain generation, so the cheaper shape cannot lose a decision."""
    opts = normalize_options(options)
    return {"type": "string", "enum": list(opts)}


def build_messages(prompt: str, options: Sequence[str], system: Optional[str] = None,
                   append_options: bool = True) -> List[Dict[str, str]]:
    text = str(prompt or "").strip()
    if append_options:
        text += _PROMPT_SUFFIX.format(options=" | ".join(options))
    messages: List[Dict[str, str]] = []
    if system and str(system).strip():
        messages.append({"role": "system", "content": str(system).strip()})
    messages.append({"role": "user", "content": text})
    return messages


def build_repair_messages(messages: Sequence[Dict[str, str]], options: Sequence[str],
                          previous: str) -> List[Dict[str, str]]:
    clipped = str(previous or "").strip().replace("\n", " ")[:160]
    note = ("Your previous reply was not one of the allowed options"
            + (f" ({clipped!r})" if clipped else "") + ". Reply with exactly one of: "
            + " | ".join(options) + " — the option text only, nothing else.")
    return list(messages) + [{"role": "user", "content": note}]


# ---------------------------------------------------------------------------
# Reading an answer — pure
# ---------------------------------------------------------------------------

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_EDGE = " \t\r\n.,;:!?\"'`*_()[]{}<>•–—"


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _clean(text: Any) -> str:
    raw = str(text or "")
    raw = _THINK_BLOCK_RE.sub("", raw)
    if "<think>" in raw.lower():  # unterminated: there is no answer after it
        return ""
    raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip())
    return raw.strip()


class _Pairs(dict):
    """A JSON object that remembers every ``key: value`` pair it was written
    with, duplicates included (``json`` keeps only the last of a repeated key)."""

    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = list(pairs)


# Returned by ``_json_answer`` when the JSON names two different answers.
_CONFLICT = object()


def _answer_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    return None


def _same_answer(a: str, b: str) -> bool:
    return _fold(a).strip(_EDGE) == _fold(b).strip(_EDGE)


def _json_answer(text: str) -> Any:
    """The answer a JSON reply carries: a string, ``None`` when it carries none
    (the plain-text reader takes over) or ``_CONFLICT`` when two answer fields
    (or one repeated key) name different answers."""
    if text[:1] not in ('{', '[', '"'):
        return None
    try:
        obj = json.loads(text, object_pairs_hook=_Pairs)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return obj[0] if len(obj) == 1 and isinstance(obj[0], str) else None
    if isinstance(obj, _Pairs):
        wanted = set(_ANSWER_KEYS)
        found = [t for k, v in obj.pairs
                 if str(k).casefold() in wanted and (t := _answer_text(v)) is not None]
        if not found:
            return None
        if any(not _same_answer(found[0], other) for other in found[1:]):
            return _CONFLICT
        return found[0]
    return None


def exact_option(text: Any, options: Sequence[str]) -> Optional[str]:
    """``text`` (outer whitespace ignored) when it is exactly one of the
    options — the test for "the constraint was honoured"."""
    value = str(text or "").strip()
    return value if value in options else None


def match_option(text: Any, options: Sequence[str]) -> Optional[str]:
    """Best-effort recovery of exactly one option from model text, or ``None``.

    Order: exact; case/width-folded (when unambiguous); the same after trimming
    quotes, markdown and trailing punctuation; and finally a single whole-word
    occurrence anywhere in the reply (several different
    options mentioned = ambiguous = ``None``, never a guess). A JSON object or
    string carrying the answer is unwrapped first."""
    opts = list(options)
    cleaned = _clean(text)
    if not cleaned:
        return None
    unwrapped = _json_answer(cleaned)
    if unwrapped is _CONFLICT:
        return None  # two different answers in one reply: ambiguous, never a guess
    if unwrapped is not None:
        cleaned = _clean(unwrapped)
        if not cleaned:
            return None

    folded: Dict[str, List[str]] = {}
    for o in opts:
        folded.setdefault(_fold(o), []).append(o)

    def lookup(candidate: str) -> Optional[str]:
        if candidate in opts:
            return candidate
        hit = folded.get(_fold(candidate))
        return hit[0] if hit and len(hit) == 1 else None

    for candidate in (cleaned, cleaned.strip(_EDGE)):
        if candidate:
            found = lookup(candidate)
            if found:
                return found
    # One whole-word occurrence anywhere, longest labels first so "ab" is not
    # read as "a". Two different options in one reply is ambiguous: refused,
    # even when the first word is one of them.
    keys = sorted(folded, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in keys) + r")(?!\w)")
    hits = set(pattern.findall(_fold(cleaned)))
    if len(hits) == 1:
        group = folded[next(iter(hits))]
        return group[0] if len(group) == 1 else None
    return None


# ---------------------------------------------------------------------------
# Result and stats
# ---------------------------------------------------------------------------


@dataclass
class ChoiceResult:
    """The outcome of one ``choose_one``. ``choice`` is an element of the
    options or ``None`` (see ``reason``)."""

    choice: Optional[str]
    index: Optional[int] = None
    path: str = PATH_NONE            # which path produced the final answer
    constrained: bool = False        # the final request carried a constraint
    honoured: Optional[bool] = None  # constrained and the raw output was exactly an option
    repaired: bool = False
    backend: str = ""                # "llamacpp" | "ollama" | "other" | ""
    model: str = ""
    ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_exact: bool = False       # every count came from the server's usage block
    raw: str = ""
    reason: str = ""                 # "" when known
    attempts: List[Dict[str, Any]] = dc_field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.choice is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "choice": self.choice, "index": self.index, "path": self.path,
            "constrained": self.constrained, "honoured": self.honoured,
            "repaired": self.repaired, "backend": self.backend, "model": self.model,
            "ms": round(self.ms, 2), "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens, "tokens_exact": self.tokens_exact,
            "reason": self.reason, "attempts": list(self.attempts),
        }


_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {}
_REJECTED: Dict[str, float] = {}


def _reset_stats() -> None:
    with _STATS_LOCK:
        _STATS.clear()
        _STATS.update({"calls": 0, "known": 0, "unknown": 0, "repaired": 0,
                       "grammar_rejected": 0, "not_honoured": 0,
                       "paths": {PATH_LLAMACPP: 0, PATH_OLLAMA: 0, PATH_FREE: 0, PATH_NONE: 0},
                       "reasons": {}})
        _REJECTED.clear()


_reset_stats()


def stats() -> Dict[str, Any]:
    with _STATS_LOCK:
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _STATS.items()}
    out["settings"] = {k: _setting(k) for k in DEFAULTS}
    return out


def _record(result: ChoiceResult) -> None:
    with _STATS_LOCK:
        _STATS["calls"] += 1
        _STATS["known" if result.known else "unknown"] += 1
        _STATS["paths"][result.path] = _STATS["paths"].get(result.path, 0) + 1
        if result.repaired:
            _STATS["repaired"] += 1
        if result.constrained and result.honoured is False:
            _STATS["not_honoured"] += 1
        if result.reason:
            _STATS["reasons"][result.reason] = _STATS["reasons"].get(result.reason, 0) + 1


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


def _host_key(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(str(url or ""))
        return f"{p.hostname or ''}:{p.port or ''}"
    except ValueError:
        return str(url or "")


def _grammar_rejected(url: str) -> bool:
    key = _host_key(url)
    with _STATS_LOCK:
        until = _REJECTED.get(key, 0.0)
        if until and until < time.monotonic():
            _REJECTED.pop(key, None)
            return False
    return bool(until)


def _remember_rejection(url: str) -> None:
    with _STATS_LOCK:
        _REJECTED[_host_key(url)] = time.monotonic() + _REJECT_TTL_S
        _STATS["grammar_rejected"] += 1


def resolve_backend(url: str, forced: Optional[str] = None) -> str:
    """``"llamacpp"`` | ``"ollama"`` | ``"other"`` for ``url``.

    Blocking (an unidentified local server is probed once, cached by
    ``model_backend``): call it from a thread. ``forced`` (or the
    ``constrained_choice_backend`` setting) skips the detection; ``"free"``
    always answers ``"other"``."""
    mode = str(forced or _setting("constrained_choice_backend") or "auto").strip().lower()
    if mode in ("free", "off", "none", "other"):
        return "other"
    if mode in ("llamacpp", "ollama"):
        return mode
    try:
        from src import llm_core
        if llm_core._is_ollama_native_url(url) or llm_core._is_local_ollama_target(url):
            return "ollama"
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.model_backend import serving_backend
        kind = serving_backend(url, probe=False).get("backend")
        if kind == "unknown":
            kind = serving_backend(url, probe=True).get("backend")
        if kind in ("ollama", "llamacpp"):
            return kind
    except Exception as exc:  # noqa: BLE001
        logger.debug("constrained_choice: backend detection failed for %s: %s", url, exc)
    return "other"


def _resolve_endpoint(url: Optional[str], model: Optional[str], headers: Optional[Dict[str, str]],
                      purpose: str, owner: Optional[str]):
    if url and model:
        return url, model, dict(headers or {})
    try:
        from src.endpoint_resolver import resolve_endpoint
        r_url, r_model, r_headers = resolve_endpoint(purpose or "utility", fallback_url=url,
                                                     fallback_model=model, owner=owner)
        return (url or r_url), (model or r_model), dict(headers or r_headers or {})
    except Exception:  # noqa: BLE001
        logger.debug("constrained_choice: endpoint resolution failed", exc_info=True)
        return url, model, dict(headers or {})


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------


def _estimate_prompt_tokens(messages: Sequence[Dict[str, str]]) -> int:
    try:
        from src.model_context import estimate_tokens
        return int(estimate_tokens(list(messages)))
    except Exception:  # noqa: BLE001
        return int(sum(len(str(m.get("content") or "")) for m in messages) * 0.3)


def _estimate_completion_tokens(text: str) -> int:
    return max(1, int(len(str(text or "")) * 0.3)) if text else 0


def _max_tokens_for(options: Sequence[str], wrapper: int = 0) -> int:
    """Enough budget to finish the LONGEST label (a token covers at least one
    byte), so a legal answer is never cut off mid-label."""
    longest = max(len(o.encode("utf-8")) for o in options)
    return min(1024, longest + 4 + wrapper)


class _Attempt:
    __slots__ = ("path", "raw", "prompt_tokens", "completion_tokens", "exact", "ms", "error",
                 "status", "finish")

    def __init__(self, path: str) -> None:
        self.path = path
        self.raw = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.exact = False
        self.ms = 0.0
        self.error = ""
        self.status: Optional[int] = None
        self.finish = ""

    def public(self, ok: bool) -> Dict[str, Any]:
        return {"path": self.path, "ok": ok, "ms": round(self.ms, 2),
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "tokens_exact": self.exact, "error": self.error, "status": self.status,
                "finish_reason": self.finish}


async def _attempt_llamacpp(url: str, model: str, headers: Dict[str, str],
                            messages: List[Dict[str, str]], options: Sequence[str],
                            timeout: float, workload: str, max_tokens: Optional[int]) -> _Attempt:
    from src import llm_core
    att = _Attempt(PATH_LLAMACPP)
    chat_url = llm_core._normalize_openai_chat_url(url)
    payload: Dict[str, Any] = {
        "model": model, "messages": messages, "temperature": 0, "stream": False,
        "max_tokens": max_tokens or _max_tokens_for(options),
        "grammar": build_gbnf(options),
        "cache_prompt": True,
    }
    llm_core._suppress_thinking(payload, model)
    llm_core._apply_llamacpp_slot(payload, chat_url, None)
    t0 = time.monotonic()
    try:
        client = llm_core._get_http_client()
        async with llm_core._local_model_slot(chat_url, model, workload=workload):
            response = await client.post(chat_url, json=payload, headers=headers or {}, timeout=timeout)
        att.status = getattr(response, "status_code", None)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - classified by the caller
        att.ms = (time.monotonic() - t0) * 1000
        att.error = f"{type(exc).__name__}: {exc}"[:200]
        resp = getattr(exc, "response", None)
        if resp is not None:
            att.status = getattr(resp, "status_code", att.status)
        return att
    att.ms = (time.monotonic() - t0) * 1000
    choices = data.get("choices") if isinstance(data, dict) else None
    c0 = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = c0.get("message") or {}
    att.raw = str(message.get("content") or c0.get("text") or "")
    att.finish = str(c0.get("finish_reason") or "")
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int) \
            and isinstance(usage.get("completion_tokens"), int):
        att.prompt_tokens, att.completion_tokens, att.exact = (
            usage["prompt_tokens"], usage["completion_tokens"], True)
    else:
        att.prompt_tokens = _estimate_prompt_tokens(messages)
        att.completion_tokens = _estimate_completion_tokens(att.raw)
    return att


async def _attempt_generation(path: str, url: str, model: str, headers: Dict[str, str],
                              messages: List[Dict[str, str]], options: Sequence[str],
                              timeout: float, workload: str, max_tokens: Optional[int],
                              schema: Optional[Dict[str, Any]],
                              spend_purpose: Optional[str] = None) -> _Attempt:
    """Plain ``llm_call_async`` — used for Ollama's ``format`` (``schema``) and
    for the unconstrained free-text path, so both keep the provider routing,
    retries and usage accounting every other call has."""
    from src import llm_core
    att = _Attempt(path)
    seen: Dict[str, Any] = {}

    def _observe(usage: Dict[str, Any], **_: Any) -> None:
        seen.update(usage or {})

    wrapper = 2 if schema else 0  # the two quotes around a JSON string
    kwargs: Dict[str, Any] = dict(
        temperature=0, max_tokens=max_tokens or max(24, _max_tokens_for(options, wrapper)),
        headers=headers or None, timeout=max(1, int(round(timeout))), workload=workload,
        _usage_observer=_observe,
    )
    if schema:
        kwargs["response_schema"] = schema
    if spend_purpose:
        kwargs["_spend_purpose"] = spend_purpose
    t0 = time.monotonic()
    try:
        text = await llm_core.llm_call_async(url, model, messages, **kwargs)
    except Exception as exc:  # noqa: BLE001
        att.ms = (time.monotonic() - t0) * 1000
        att.error = f"{type(exc).__name__}: {exc}"[:200]
        att.status = getattr(exc, "status_code", None)
        return att
    att.ms = (time.monotonic() - t0) * 1000
    if isinstance(text, tuple):
        text = text[0]
    att.raw = str(text or "")
    in_t, out_t = seen.get("input_tokens"), seen.get("output_tokens")
    if isinstance(in_t, int) and isinstance(out_t, int):
        att.prompt_tokens, att.completion_tokens, att.exact = in_t, out_t, True
    else:
        att.prompt_tokens = _estimate_prompt_tokens(messages)
        att.completion_tokens = _estimate_completion_tokens(att.raw)
    return att


def _ollama_value(raw: str) -> Optional[str]:
    """The label of an Ollama ``format`` answer (a JSON string), or None. A
    server that wrapped it in an object anyway is read through the same rules as
    any JSON reply, so a self-contradicting wrapper gives None."""
    text = _clean(raw)
    if text[:1] not in ('"', '{'):
        return None
    value = _json_answer(text)
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# choose_one
# ---------------------------------------------------------------------------


def _is_timeout(att: _Attempt) -> bool:
    return "timeout" in att.error.lower() or "timed out" in att.error.lower()


async def choose_one(options: Sequence[Any], prompt: str, *, system: Optional[str] = None,
                     url: Optional[str] = None, model: Optional[str] = None,
                     headers: Optional[Dict[str, str]] = None, purpose: str = "utility",
                     owner: Optional[str] = None, backend: Optional[str] = None,
                     repair: bool = True, append_options: bool = True,
                     max_tokens: Optional[int] = None, timeout: float = 30.0,
                     workload: str = "foreground", caller: str = "",
                     spend_purpose: Optional[str] = None) -> ChoiceResult:
    """Choose exactly one of ``options`` for ``prompt``. See the module doc.

    ``url``/``model``/``headers`` default to the endpoint ``purpose`` resolves
    to. ``backend`` overrides detection (``"llamacpp"``, ``"ollama"``,
    ``"free"``). ``append_options`` adds the one-line list of labels to the
    prompt; pass False when the prompt already lists them. ``repair`` allows
    one extra call when the answer is not legal. ``spend_purpose`` names the
    call in the turn's spend account (generation paths only). Never raises for a runtime
    failure; ``ValueError`` for an empty/oversized option set or empty prompt."""
    opts = normalize_options(options)
    if not str(prompt or "").strip():
        raise ValueError("constrained_choice: 'prompt' must not be empty")
    started = time.monotonic()
    result = ChoiceResult(choice=None)
    try:
        await _choose(result, opts, prompt, system, url, model, headers, purpose, owner, backend,
                      repair, append_options, max_tokens, timeout, workload, spend_purpose)
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        logger.debug("constrained_choice: failed (%s)", exc, exc_info=True)
        result.choice, result.index = None, None
        result.reason = result.reason or "error"
    result.ms = (time.monotonic() - started) * 1000
    _record(result)
    logger.debug("constrained_choice[%s] -> %s (path=%s honoured=%s repaired=%s %.0fms p=%d c=%d reason=%s)",
                 caller or purpose, result.choice, result.path, result.honoured, result.repaired,
                 result.ms, result.prompt_tokens, result.completion_tokens, result.reason or "-")
    return result


def _settle(result: ChoiceResult, opts: Sequence[str], choice: Optional[str]) -> None:
    result.choice = choice
    result.index = opts.index(choice) if choice is not None else None


def _book(result: ChoiceResult, att: _Attempt, ok: bool) -> None:
    result.attempts.append(att.public(ok))
    result.prompt_tokens += att.prompt_tokens
    result.completion_tokens += att.completion_tokens
    result.tokens_exact = att.exact if len(result.attempts) == 1 else (result.tokens_exact and att.exact)
    result.raw = att.raw[:400]


async def _choose(result: ChoiceResult, opts: List[str], prompt: str, system: Optional[str],
                  url: Optional[str], model: Optional[str], headers: Optional[Dict[str, str]],
                  purpose: str, owner: Optional[str], backend: Optional[str], repair: bool,
                  append_options: bool, max_tokens: Optional[int], timeout: float,
                  workload: str, spend_purpose: Optional[str] = None) -> None:
    url, model, headers = _resolve_endpoint(url, model, headers, purpose, owner)
    if not url or not model:
        result.reason = "no_endpoint"
        return
    result.model = model
    messages = build_messages(prompt, opts, system, append_options)

    kind = "other"
    if _setting("constrained_choice_enabled"):
        kind = await asyncio.to_thread(resolve_backend, url, backend)
    result.backend = kind

    att: Optional[_Attempt] = None
    # --- constrained attempt -------------------------------------------------
    if kind == "llamacpp" and not _grammar_rejected(url):
        att = await _attempt_llamacpp(url, model, headers, messages, opts, timeout, workload, max_tokens)
        exact = exact_option(att.raw, opts) if not att.error else None
        lenient = exact or (match_option(att.raw, opts) if not att.error else None)
        if lenient:
            result.path, result.constrained, result.honoured = PATH_LLAMACPP, True, exact is not None
            _book(result, att, True)
            _settle(result, opts, lenient)
            return
        _book(result, att, False)
        result.path, result.constrained = PATH_LLAMACPP, True
        if att.error:
            if att.status in (400, 422, 501):
                _remember_rejection(url)  # this server does not take a grammar
                result.reason = "grammar_rejected"
            elif att.status is not None and att.status >= 400:
                result.reason = "constrained_failed"  # the server answered: try plain generation
            elif _is_timeout(att):
                result.reason = "timeout"
                return
            else:
                result.reason = "error"
                return
        else:
            result.honoured = False
            result.reason = "grammar_ignored"
    elif kind == "ollama":
        try:
            from src import llm_core
            effective = llm_core._route_for_response_schema(url, model)
            schema = build_enum_schema(opts)
            wired = llm_core._resolve_response_schema(effective, schema) is not None
        except Exception:  # noqa: BLE001
            wired = False
        if wired:
            att = await _attempt_generation(PATH_OLLAMA, url, model, headers, messages, opts,
                                            timeout, workload, max_tokens, schema, spend_purpose)
            value = _ollama_value(att.raw) if not att.error else None
            exact = value if value in opts else None
            lenient = exact or (match_option(att.raw, opts) if not att.error else None)
            if lenient:
                result.path, result.constrained, result.honoured = PATH_OLLAMA, True, exact is not None
                _book(result, att, True)
                _settle(result, opts, lenient)
                return
            _book(result, att, False)
            result.path, result.constrained = PATH_OLLAMA, True
            if att.error:
                if att.status is not None and att.status >= 400:
                    result.reason = "constrained_failed"  # e.g. a build that refuses the schema
                elif _is_timeout(att):
                    result.reason = "timeout"
                    return
                else:
                    result.reason = "error"
                    return
            else:
                result.honoured = False
                result.reason = "format_ignored"

    # --- free-text attempt (backend without a constraint, or the constrained one failed)
    if att is None or result.reason in ("grammar_rejected", "constrained_failed"):
        free = await _attempt_generation(PATH_FREE, url, model, headers, messages, opts,
                                         timeout, workload, max_tokens, None, spend_purpose)
        found = match_option(free.raw, opts) if not free.error else None
        _book(result, free, found is not None)
        result.path, result.constrained, result.honoured = PATH_FREE, False, None
        if found:
            result.reason = ""
            _settle(result, opts, found)
            return
        if free.error:
            result.reason = "timeout" if _is_timeout(free) else "error"
            return
        result.reason = "unparsed"
        last_raw = free.raw
    else:
        last_raw = att.raw
        result.reason = result.reason or "unparsed"

    # --- one repair ---------------------------------------------------------------
    if not repair:
        return
    fix = await _attempt_generation(PATH_FREE, url, model, headers,
                                    build_repair_messages(messages, opts, last_raw), opts,
                                    timeout, workload, max_tokens, None, spend_purpose)
    found = match_option(fix.raw, opts) if not fix.error else None
    _book(result, fix, found is not None)
    result.repaired = True
    result.path, result.constrained, result.honoured = PATH_FREE, False, None
    if found:
        result.reason = ""
        _settle(result, opts, found)
    elif fix.error:
        result.reason = "timeout" if _is_timeout(fix) else "error"
    else:
        result.reason = "unparsed"


def choose_one_sync(options: Sequence[Any], prompt: str, **kwargs: Any) -> ChoiceResult:
    """``choose_one`` for synchronous callers; safe inside a running loop (the
    call then runs on a short-lived worker thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(choose_one(options, prompt, **kwargs))
    box: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = asyncio.run(choose_one(options, prompt, **kwargs))
        except ValueError as exc:
            box["error"] = exc
        except Exception as exc:  # noqa: BLE001
            logger.debug("constrained_choice: choose_one_sync failed (%s)", exc)
            box["result"] = ChoiceResult(choice=None, reason="error")

    thread = threading.Thread(target=_runner, name="constrained-choice", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result") or ChoiceResult(choice=None, reason="error")


__all__ = [
    "ChoiceResult", "choose_one", "choose_one_sync", "normalize_options", "build_gbnf",
    "gbnf_literal", "build_enum_schema", "build_messages", "build_repair_messages",
    "match_option", "exact_option", "resolve_backend", "stats", "MAX_OPTIONS",
    "PATH_LLAMACPP", "PATH_OLLAMA", "PATH_FREE", "PATH_NONE",
]
