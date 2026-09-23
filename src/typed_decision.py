"""src/typed_decision.py — typed decisions: answer a closed question about a
text by reading the next-token probabilities of the allowed answers.

Why this exists
---------------
Several places in Faustus classify text with keyword lists: "does this turn
need a web search?", "is this new entity a person or a place?", "do these two
memories disagree?". A keyword list is fast and predictable but brittle at
the edges (an unseen phrasing, the other language), and asking a model to
*write* its answer is slow, needs parsing and carries no confidence at all.

A typed decision sits in between. The model sees the context, the question
and the allowed answers labelled with single-token letters (``A``, ``B``,
``C``...; a yes/no question is ``A`` = yes, ``B`` = no). We request exactly
ONE output token at temperature 0 with the top log-probabilities, and read
the probability the model put on each allowed letter in that single forward
pass (one prefill, no generated reasoning):

* ``distribution`` — the letters' probabilities renormalised over the
  allowed answers;
* ``value`` — the argmax answer, ``confidence`` its renormalised probability;
* ``mass`` — the total (un-renormalised) probability the model put on ANY
  allowed letter. Low mass means the model wanted to say something else
  (a word, a refusal, a thinking preamble): the answer is treated as
  unknown, however peaked the renormalised distribution looks.

Several fields about the same context share one byte-identical prompt
prefix (instructions + context) and differ only in the trailing question,
so a server with a prompt cache (``cache_prompt: true`` on a llama-server,
Ollama's own prefix reuse) only prefills the short tail after the first
field.

Two wire shapes are spoken, detected from the resolved endpoint URL the same
way ``src/llm_core.py`` tells them apart:

* OpenAI-compatible ``/v1/chat/completions`` (llama-server, the loopback
  utility helper): ``logprobs: true, top_logprobs: N, max_tokens: 1``;
  the answer is ``choices[0].logprobs.content[0].top_logprobs``. A thinking
  model gets ``chat_template_kwargs.enable_thinking = false``.
* native Ollama ``/api/chat``: ``think: false``, ``options.num_predict: 1``,
  ``logprobs: true, top_logprobs: N``; the answer is
  ``logprobs[0].top_logprobs``. (Ollama's OpenAI-compatible surface lets a
  thinking model spend the single token on reasoning, which is why a local
  Ollama ``/v1`` URL is moved to the native endpoint here.)

A server that returns no log-probabilities at all still gets a chance: the
single generated letter is parsed (``method = "letter"``, no confidence).
Anything else is ``unknown``.

Etiquette — a typed decision is a tiny, optional helper, so it must never be
the reason a model is loaded or evicted. Before any request it asks
``src.background_job_guard`` which models the runner already holds (Ollama's
``/api/ps``, a loopback llama-server's ``/health`` + ``/v1/models``) and
answers ``method = "unavailable"`` without calling the model when the
configured one is not resident (``typed_decisions_may_load`` overrides).

A typed decision is ADVISORY. It never grants a permission, never approves
anything and never replaces a rule that is already confident: every caller
keeps its deterministic logic as the first pass and as the fallback.
``decide`` never raises.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field as dc_field
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_CHOICES = 20
_LETTERS = tuple(chr(ord("A") + i) for i in range(MAX_CHOICES))
BOOL_CHOICES: Tuple[str, str] = ("yes", "no")

#: How many alternatives we ask the server for. OpenAI-shaped servers cap
#: ``top_logprobs`` at 20; a few more than the number of letters leaves room
#: for the " A" / "A" tokenizer variants and for the tokens the model
#: preferred instead (which is exactly what ``mass`` measures).
_MAX_TOP_LOGPROBS = 20

#: Context is cut to this many characters: a decision is about a sentence or
#: a message, and an unbounded prefill would blow the latency budget.
MAX_CONTEXT_CHARS = 6000

DEFAULTS: Dict[str, Any] = {
    "typed_decisions_enabled": True,
    "typed_decision_timeout_ms": 1500,
    "typed_decision_min_confidence": 0.7,
    "typed_decision_min_mass": 0.5,
    "typed_decisions_may_load": False,
    "typed_decision_freshness": True,
    "typed_decision_entity_types": True,
    "typed_decision_memory_conflicts": True,
}

_DEFAULT_INSTRUCTIONS = (
    "You classify text. Read the context, then answer the question with the "
    "letter of exactly one option. Reply with that single letter and nothing else."
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One closed question about the shared context.

    ``choices`` is the list of allowed answers, or the string ``"bool"`` for
    a yes/no question (answers ``"yes"`` / ``"no"``). ``descriptions``
    optionally explains each choice (same order, or a dict keyed by choice);
    it is shown next to the letter and helps a small model a lot."""

    name: str
    question: str
    choices: Union[Sequence[str], str] = "bool"
    descriptions: Optional[Union[Sequence[str], Dict[str, str]]] = None

    def options(self) -> List[str]:
        if isinstance(self.choices, str):
            return list(BOOL_CHOICES) if self.choices.strip().lower() == "bool" else [self.choices]
        return [str(c) for c in self.choices][:MAX_CHOICES]

    def description_for(self, index: int, choice: str) -> str:
        desc = self.descriptions
        if not desc:
            return ""
        if isinstance(desc, dict):
            return str(desc.get(choice) or "").strip()
        try:
            return str(list(desc)[index] or "").strip()
        except (IndexError, TypeError):
            return ""


@dataclass
class Decision:
    """The answer to one field. ``value`` is None when unknown (see
    ``reason``); ``best`` is the argmax even when it was not trusted, for
    auditing."""

    field: str
    value: Optional[str]
    confidence: Optional[float]
    mass: Optional[float]
    distribution: Dict[str, float] = dc_field(default_factory=dict)
    method: str = "unavailable"  # "logprobs" | "letter" | "unavailable"
    ms: float = 0.0
    reason: str = ""
    best: Optional[str] = None
    model: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _setting(key: str) -> Any:
    default = DEFAULTS.get(key)
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
    except Exception:  # noqa: BLE001 - unreadable settings = defaults
        return default
    return default if value is None else value


def _float_setting(key: str) -> float:
    try:
        return float(_setting(key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def enabled() -> bool:
    return bool(_setting("typed_decisions_enabled"))


def default_timeout_s() -> float:
    try:
        ms = float(_setting("typed_decision_timeout_ms"))
    except (TypeError, ValueError):
        ms = float(DEFAULTS["typed_decision_timeout_ms"])
    return max(0.05, ms / 1000.0)


def default_min_confidence() -> float:
    return _float_setting("typed_decision_min_confidence")


def default_min_mass() -> float:
    return _float_setting("typed_decision_min_mass")


# ---------------------------------------------------------------------------
# Prompt — pure, so tests can check the prefix bytes
# ---------------------------------------------------------------------------


def letters_for(count: int) -> List[str]:
    return list(_LETTERS[: max(0, min(int(count), MAX_CHOICES))])


def _clip_context(context: str) -> str:
    text = str(context or "").strip()
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS].rstrip() + " …"
    return text


def system_text(instructions: str = "") -> str:
    extra = str(instructions or "").strip()
    return _DEFAULT_INSTRUCTIONS + ("\n" + extra if extra else "")


def prompt_prefix(context: str) -> str:
    """The part of the user message every field of one ``decide`` call shares,
    byte for byte. The field's question always comes AFTER it."""
    return "Context:\n\"\"\"\n" + _clip_context(context) + "\n\"\"\"\n\n"


def field_suffix(fld: Field) -> str:
    options = fld.options()
    lines = ["Question: " + str(fld.question or "").strip()]
    for i, (letter, choice) in enumerate(zip(letters_for(len(options)), options)):
        desc = fld.description_for(i, choice)
        lines.append(f"{letter}) {choice}" + (f" — {desc}" if desc else ""))
    lines.append("Answer with one letter:")
    return "\n".join(lines)


def build_messages(context: str, fld: Field, instructions: str = "") -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": system_text(instructions)},
        {"role": "user", "content": prompt_prefix(context) + field_suffix(fld)},
    ]


# ---------------------------------------------------------------------------
# Response reading — pure
# ---------------------------------------------------------------------------

_LETTER_TOKEN_RE = re.compile(r"^\s*\(?([A-Z])[\)\.:]?\s*$")


def token_letter(token: Any) -> Optional[str]:
    """``"A"``, ``" A"``, ``"A)"``, ``"(B"`` -> the letter; anything else None.
    Case-sensitive on purpose: a lowercase ``a`` is an English article far
    more often than an answer."""
    if not isinstance(token, str):
        return None
    m = _LETTER_TOKEN_RE.match(token)
    return m.group(1) if m else None


def score_top_logprobs(top: Any, letters: Sequence[str]) -> Tuple[Dict[str, float], float]:
    """(distribution over ``letters`` renormalised, mass). The same letter
    reached through several tokens (``"A"`` and ``" A"``) adds up."""
    raw = {letter: 0.0 for letter in letters}
    for entry in top or []:
        if not isinstance(entry, dict):
            continue
        letter = token_letter(entry.get("token"))
        lp = entry.get("logprob")
        if letter not in raw or not isinstance(lp, (int, float)) or math.isnan(lp):
            continue
        raw[letter] += math.exp(min(0.0, float(lp)))
    mass = sum(raw.values())
    if mass <= 0:
        return {letter: 0.0 for letter in letters}, 0.0
    return {letter: value / mass for letter, value in raw.items()}, min(1.0, mass)


def parse_letter(text: Any, letters: Sequence[str]) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    letter = token_letter(raw[:3]) or token_letter(raw)
    if letter in letters:
        return letter
    m = re.match(r"^\s*(?:answer\s*:?\s*)?\(?([A-Z])\b", raw, re.IGNORECASE)
    if m and m.group(1).upper() in letters and m.group(1).isupper():
        return m.group(1)
    return None


def extract_answer(data: Any, wire: str) -> Tuple[Optional[list], str]:
    """(top_logprobs of the first generated token or None, generated text)."""
    if not isinstance(data, dict):
        return None, ""
    if wire == "ollama":
        text = str((data.get("message") or {}).get("content") or "")
        lps = data.get("logprobs")
        if isinstance(lps, list) and lps and isinstance(lps[0], dict):
            top = lps[0].get("top_logprobs")
            return (top if isinstance(top, list) and top else None), text
        return None, text
    choices = data.get("choices") or []
    c0 = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = c0.get("message") or {}
    text = str(message.get("content") or c0.get("text") or "")
    lp = c0.get("logprobs")
    content = lp.get("content") if isinstance(lp, dict) else None
    if isinstance(content, list) and content and isinstance(content[0], dict):
        top = content[0].get("top_logprobs")
        return (top if isinstance(top, list) and top else None), text
    return None, text


# ---------------------------------------------------------------------------
# Stats — tiny, in memory, per process
# ---------------------------------------------------------------------------

_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {}
_LATENCIES: Deque[float] = deque(maxlen=256)
_RECENT: Deque[Dict[str, Any]] = deque(maxlen=50)


def _reset_stats() -> None:
    with _STATS_LOCK:
        _STATS.clear()
        _STATS.update({"calls": 0, "fields": 0, "logprobs": 0, "letter": 0,
                       "unavailable": 0, "unknown": 0, "reasons": {}})
        _LATENCIES.clear()
        _RECENT.clear()


_reset_stats()


def _record(decision: Decision, caller: str) -> None:
    with _STATS_LOCK:
        _STATS["fields"] += 1
        _STATS[decision.method] = int(_STATS.get(decision.method, 0)) + 1
        if decision.value is None:
            _STATS["unknown"] += 1
        if decision.reason:
            reasons = _STATS["reasons"]
            reasons[decision.reason] = int(reasons.get(decision.reason, 0)) + 1
        if decision.method != "unavailable":
            _LATENCIES.append(float(decision.ms))
        # Never the context: only which question, what came out and how sure.
        _RECENT.append({
            "at": time.time(), "caller": caller, "field": decision.field,
            "value": decision.value, "best": decision.best,
            "confidence": None if decision.confidence is None else round(decision.confidence, 4),
            "mass": None if decision.mass is None else round(decision.mass, 4),
            "method": decision.method, "reason": decision.reason, "ms": round(decision.ms, 1),
        })


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[k], 1)


def stats() -> Dict[str, Any]:
    with _STATS_LOCK:
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _STATS.items()}
        lat = list(_LATENCIES)
        recent = list(_RECENT)
    out["fallbacks"] = int(out.get("letter", 0))
    out["p50_ms"] = _percentile(lat, 0.5)
    out["p95_ms"] = _percentile(lat, 0.95)
    out["recent"] = recent
    out["settings"] = {k: _setting(k) for k in DEFAULTS}
    return out


# ---------------------------------------------------------------------------
# Endpoint, wire shape, residency
# ---------------------------------------------------------------------------

#: Tests replace this with ``httpx.MockTransport``.
_TRANSPORT: Any = None

def _loopback(url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def wire_for(url: str) -> Tuple[str, str]:
    """(wire, request_url): ``("ollama", ".../api/chat")``,
    ``("openai", ".../chat/completions")`` or ``("unsupported", url)``."""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "unsupported", url
    try:
        from src import llm_core
        provider = llm_core._detect_provider(url)
        if provider == "ollama" or llm_core._is_ollama_native_url(url):
            return "ollama", llm_core._normalize_ollama_url(url)
        if llm_core._is_local_ollama_target(url):
            return "ollama", llm_core._ollama_native_url_for_compat(url)
        if provider in {"anthropic", "chatgpt-subscription"}:
            return "unsupported", url
        return "openai", llm_core._normalize_openai_chat_url(url)
    except Exception:  # noqa: BLE001 - detection helpers moved: plain heuristics
        path = (urlparse(url).path or "").rstrip("/")
        if path.endswith("/api/chat"):
            return "ollama", url
        return "openai", url if path.endswith("/chat/completions") else url.rstrip("/") + "/chat/completions"


def _resolve(purpose: str, owner: Optional[str]) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint(purpose or "utility", owner=owner)
    except Exception:  # noqa: BLE001
        logger.debug("typed_decision: endpoint resolution failed", exc_info=True)
        return None, None, {}
    return url, model, dict(headers or {})


def _resident_names(url: str) -> Optional[List[str]]:
    """Asked fresh on every call (a loopback probe costs milliseconds): a
    cached "resident" answer could outlive an eviction, and the request that
    followed would make the runner load the model again."""
    try:
        from src import background_job_guard
        return background_job_guard._resident_model_names(url)
    except Exception:  # noqa: BLE001
        return None


def residency_reason(url: str, model: str) -> str:
    """"" when a request would not load anything; otherwise why not."""
    if _setting("typed_decisions_may_load"):
        return ""
    names = _resident_names(url)
    if names is None:
        return "residency_unknown"
    try:
        from src.llm_core import _same_model_identity
    except Exception:  # noqa: BLE001
        def _same_model_identity(a: str, b: str) -> bool:
            return (a or "").strip().lower() == (b or "").strip().lower()
    if not any(_same_model_identity(n, model) for n in names if n):
        return "model_not_resident"
    try:
        from src import background_job_guard
        if background_job_guard.model_busy(url):
            return "model_busy"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _prepare(purpose: str, owner: Optional[str]) -> Dict[str, Any]:
    """Blocking part (settings, DB, residency probes) — run in a thread."""
    url, model, headers = _resolve(purpose, owner)
    if not url or not model:
        return {"reason": "no_endpoint"}
    wire, request_url = wire_for(url)
    if wire == "unsupported":
        return {"reason": "unsupported_endpoint", "model": model}
    reason = residency_reason(url, model)
    if reason:
        return {"reason": reason, "model": model}
    return {"reason": "", "url": request_url, "model": model, "headers": headers, "wire": wire}


def build_payload(wire: str, model: str, messages: List[Dict[str, str]], n_letters: int,
                  request_url: str = "", think: bool = True) -> Dict[str, Any]:
    top_n = max(5, min(_MAX_TOP_LOGPROBS, n_letters + 8))
    if wire == "ollama":
        payload: Dict[str, Any] = {
            "model": model, "messages": messages, "stream": False,
            "options": {"num_predict": 1, "temperature": 0},
            "logprobs": True, "top_logprobs": top_n,
        }
        if think:
            payload["think"] = False
        return payload
    payload = {
        "model": model, "messages": messages, "stream": False,
        "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": top_n,
    }
    if _loopback(request_url):
        # llama-server: reuse the shared prefix's KV cache, and keep a
        # thinking template from opening a reasoning block.
        payload["cache_prompt"] = True
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


async def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Any:
    import httpx
    try:
        from src.tls_overrides import llm_verify
        verify = llm_verify()
    except Exception:  # noqa: BLE001
        verify = True
    kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(timeout, connect=min(timeout, 1.0))}
    if _TRANSPORT is not None:
        kwargs["transport"] = _TRANSPORT
    else:
        kwargs["verify"] = verify
    async with httpx.AsyncClient(**kwargs) as client:
        response = await client.post(url, json=payload, headers=headers or {})
        if response.status_code == 400 and payload.get("think") is False and "think" in response.text.lower():
            # A model without a thinking switch can refuse the flag outright.
            retry = {k: v for k, v in payload.items() if k != "think"}
            response = await client.post(url, json=retry, headers=headers or {})
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


def _unavailable(fld: Field, reason: str, model: str = "", ms: float = 0.0) -> Decision:
    return Decision(field=fld.name, value=None, confidence=None, mass=None, distribution={},
                    method="unavailable", ms=ms, reason=reason, model=model)


def interpret(fld: Field, data: Any, wire: str, *, min_confidence: Optional[float],
              min_mass: float, ms: float = 0.0, model: str = "") -> Decision:
    """Turn one server response into a Decision (pure; tested directly)."""
    options = fld.options()
    letters = letters_for(len(options))
    top, text = extract_answer(data, wire)
    if top:
        dist_l, mass = score_top_logprobs(top, letters)
        if mass > 0:
            best_letter = max(letters, key=lambda l: dist_l[l])
            best = options[letters.index(best_letter)]
            confidence = dist_l[best_letter]
            distribution = {options[i]: round(dist_l[l], 6) for i, l in enumerate(letters)}
            value: Optional[str] = best
            reason = ""
            if mass < min_mass:
                value, reason = None, "low_mass"
            elif min_confidence is not None and confidence < min_confidence:
                value, reason = None, "low_confidence"
            return Decision(field=fld.name, value=value, confidence=round(confidence, 6),
                            mass=round(mass, 6), distribution=distribution, method="logprobs",
                            ms=ms, reason=reason, best=best, model=model)
        # Log-probabilities came back but none of them is an allowed letter:
        # the model wanted to say something else entirely.
        return Decision(field=fld.name, value=None, confidence=None, mass=0.0, distribution={},
                        method="logprobs", ms=ms, reason="low_mass", model=model)
    letter = parse_letter(text, letters)
    if letter:
        choice = options[letters.index(letter)]
        return Decision(field=fld.name, value=choice, confidence=None, mass=None, distribution={},
                        method="letter", ms=ms, reason="", best=choice, model=model)
    return Decision(field=fld.name, value=None, confidence=None, mass=None, distribution={},
                    method="letter", ms=ms, reason="unparsed", model=model)


async def decide(context: str, fields: Sequence[Field], *, owner: Optional[str] = None,
                 purpose: str = "utility", instructions: str = "",
                 timeout_s: Optional[float] = None, min_confidence: Optional[float] = None,
                 min_mass: Optional[float] = None, caller: str = "") -> Dict[str, Decision]:
    """Answer every field about ``context``. Never raises.

    ``timeout_s`` (default: the ``typed_decision_timeout_ms`` setting) is the
    hard wall-clock budget for the WHOLE call, endpoint probing included;
    fields that do not fit come back ``unavailable`` / ``timeout``.
    ``min_confidence`` (default: the setting) turns an argmax below it into
    ``value=None, reason="low_confidence"``; pass ``0`` to always get the
    argmax. ``min_mass`` (default: the setting) does the same for mass."""
    started = time.monotonic()
    out: Dict[str, Decision] = {}
    try:
        flds = [f for f in (fields or []) if isinstance(f, Field) and f.name and f.options()]
    except Exception:  # noqa: BLE001
        flds = []
    if not flds:
        return out
    with _STATS_LOCK:
        _STATS["calls"] += 1

    def _finish(decision: Decision) -> None:
        out[decision.field] = decision
        _record(decision, caller or purpose)

    try:
        if not enabled():
            for f in flds:
                _finish(_unavailable(f, "disabled"))
            return out
        budget = default_timeout_s() if timeout_s is None else max(0.01, float(timeout_s))
        min_conf = default_min_confidence() if min_confidence is None else float(min_confidence)
        min_m = default_min_mass() if min_mass is None else float(min_mass)

        try:
            prep = await asyncio.wait_for(asyncio.to_thread(_prepare, purpose, owner), timeout=budget)
        except asyncio.TimeoutError:
            prep = {"reason": "timeout"}
        if prep.get("reason"):
            for f in flds:
                _finish(_unavailable(f, prep["reason"], prep.get("model", "")))
            return out

        wire, url, model, headers = prep["wire"], prep["url"], prep["model"], prep["headers"]
        for f in flds:
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0.02:
                _finish(_unavailable(f, "timeout", model))
                continue
            messages = build_messages(context, f, instructions)
            payload = build_payload(wire, model, messages, len(f.options()), url)
            t0 = time.monotonic()
            try:
                data = await asyncio.wait_for(_post(url, payload, headers, remaining), timeout=remaining)
            except asyncio.TimeoutError:
                _finish(_unavailable(f, "timeout", model, (time.monotonic() - t0) * 1000))
                continue
            except Exception as exc:  # noqa: BLE001 - network/HTTP trouble = unknown
                name = type(exc).__name__
                reason = "timeout" if "timeout" in name.lower() else "error"
                logger.debug("typed_decision: request failed (%s: %s)", name, exc)
                _finish(_unavailable(f, reason, model, (time.monotonic() - t0) * 1000))
                continue
            ms = (time.monotonic() - t0) * 1000
            _finish(interpret(f, data, wire, min_confidence=min_conf, min_mass=min_m,
                              ms=ms, model=model))
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        logger.debug("typed_decision: decide failed (%s)", exc, exc_info=True)
        for f in flds:
            if f.name not in out:
                _finish(_unavailable(f, "error"))
    for f in flds:
        d = out.get(f.name)
        if d is not None:
            logger.debug("typed_decision[%s] %s -> %s (best=%s p=%s mass=%s method=%s reason=%s %.0fms)",
                         caller or purpose, d.field, d.value, d.best, d.confidence, d.mass,
                         d.method, d.reason or "-", d.ms)
    return out


def decide_sync(context: str, fields: Sequence[Field], **kwargs: Any) -> Dict[str, Decision]:
    """``decide`` for synchronous callers. Safe inside a running event loop
    (the call then runs on a short-lived worker thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(decide(context, fields, **kwargs))
    box: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = asyncio.run(decide(context, fields, **kwargs))
        except Exception as exc:  # noqa: BLE001
            logger.debug("typed_decision: decide_sync failed (%s)", exc)
            box["result"] = {}

    thread = threading.Thread(target=_runner, name="typed-decision", daemon=True)
    thread.start()
    thread.join()
    return box.get("result") or {}


def field_from_dict(raw: Dict[str, Any]) -> Optional[Field]:
    """Build a Field from a JSON object (the HTTP route's input)."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    question = str(raw.get("question") or "").strip()
    choices = raw.get("choices", "bool")
    if isinstance(choices, str):
        choices = "bool" if choices.strip().lower() == "bool" else [choices]
    elif isinstance(choices, (list, tuple)):
        choices = [str(c).strip() for c in choices if str(c).strip()]
    else:
        return None
    if not name or not question or (not isinstance(choices, str) and len(choices) < 2):
        return None
    descriptions = raw.get("descriptions")
    if not isinstance(descriptions, (list, dict)):
        descriptions = None
    return Field(name=name, question=question, choices=choices, descriptions=descriptions)


__all__ = [
    "Field", "Decision", "decide", "decide_sync", "stats", "interpret", "build_messages",
    "prompt_prefix", "field_suffix", "wire_for", "build_payload", "score_top_logprobs",
    "field_from_dict", "enabled", "BOOL_CHOICES",
]
