"""Per-turn reasoning mode: Auto / Fast / Think / Deep.

A thinking-capable model (the local 27B above all) pays for its reasoning in
seconds before the first word, and reasoning and answer share one output
budget. A greeting or a one-line lookup should not pay that; a debugging
session or a proof should get a real budget. The person can pick the mode per
turn in the composer (Auto / Rápido / Pensar / A fondo) or with
``/think auto|fast|think|deep``; ``auto`` asks :func:`decide`.

``decide`` is a pure, deterministic, bilingual (ES/EN) rule. It never raises:
anything unexpected falls back to ``fast`` with low confidence for a short
turn and ``think`` for a long one, and a coding turn never drops below
``think`` unless it is plainly small talk -- a turn that thinks when it did
not need to costs seconds, a coding turn that does not think costs a wrong
edit, and those are not the same mistake (the same asymmetry
``src/turn_effort.py`` documents).

:func:`to_overrides` maps a mode to the ``gen_overrides`` vocabulary
``src/llm_core.py`` already understands (``think``, ``reasoning_budget``,
``reasoning_effort``) -- the same vocabulary ``src/effort_profile.py`` uses
for delegated workers:

* ``fast``  -> ``think: False``
* ``think`` -> ``think: True`` + ``reasoning_budget`` = ``think_mode_budget_think``
* ``deep``  -> ``think: True`` + ``reasoning_budget`` = ``think_mode_budget_deep``
  + ``reasoning_effort: "high"``

The budget only reaches llama-server / other self-hosted OpenAI-compatible
engines (``reasoning_budget`` next to ``chat_template_kwargs.enable_thinking``).
Ollama's native ``/api/chat`` has no budget field: there ``deep`` is simply
thinking on, the same as ``think``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "MODES", "EXPLICIT_MODES", "normalize", "decide", "to_overrides",
    "resolve_turn", "DEFAULT_BUDGET_THINK", "DEFAULT_BUDGET_DEEP", "DEFAULT_BUDGET_LIGHT",
]

MODES = ("auto", "fast", "think", "deep")
EXPLICIT_MODES = ("fast", "think", "deep")

DEFAULT_BUDGET_THINK = 4096
DEFAULT_BUDGET_LIGHT = 1024
DEFAULT_BUDGET_DEEP = 16384

_ALIASES = {
    "auto": "auto", "automatico": "auto", "automático": "auto", "default": "auto",
    "fast": "fast", "rapido": "fast", "rápido": "fast", "quick": "fast", "off": "fast",
    "no": "fast", "false": "fast", "low": "fast",
    "think": "think", "pensar": "think", "on": "think", "true": "think", "si": "think",
    "sí": "think", "medium": "think",
    "deep": "deep", "a fondo": "deep", "fondo": "deep", "profundo": "deep", "high": "deep",
}


def normalize(value: Any) -> str:
    """Canonical mode for a user- or client-supplied value, or "" when it is
    missing or not recognised (the caller then keeps its own default)."""
    try:
        key = re.sub(r"\s+", " ", str(value or "").strip().lower())
    except Exception:  # noqa: BLE001
        return ""
    return _ALIASES.get(key, "")


def _rx(*parts: str) -> "re.Pattern[str]":
    return re.compile("|".join(parts), re.IGNORECASE)


# Asking outright for depth. Wins over everything else.
_DEEP_ASK = _rx(
    r"\ba fondo\b", r"\ben profundidad\b", r"\bexhaustiv[oa]s?\b", r"\bexhaustivamente\b",
    r"\bpi[eé]nsa(?:lo)? (?:bien|a fondo|con calma|detenidamente)\b",
    r"\bpaso a paso\b", r"\bdemu[eé]stra(?:lo|me)?\b", r"\bdemostraci[oó]n\b",
    r"\brazona(?:lo)? (?:con cuidado|detenidamente|paso a paso)\b", r"\bminuciosa(?:mente)?\b",
    r"\bthink (?:hard|harder|deeply|carefully|it through)\b", r"\bstep[- ]by[- ]step\b",
    r"\bprove\b", r"\bproof\b", r"\bin[- ]depth\b", r"\bdeep[- ]dive\b", r"\bthorough(?:ly)?\b",
    r"\bexhaustive(?:ly)?\b", r"\brigorous(?:ly)?\b",
)

# Asking outright for speed. Phrase-shaped on purpose: "el algoritmo más
# rápido" is a question about speed, not a request to answer fast.
_FAST_ASK = _rx(
    r"^\s*(?:r[aá]pido|quick(?:ly)?|breve(?:mente)?|briefly)\s*[,:.!\-]",
    r"\b(?:responde|contesta|dime|di|answer|tell me|reply)\s+(?:r[aá]pido|quick(?:ly)?|brevemente|briefly|en corto)\b",
    r"\b(?:respuesta|answer|reply)\s+(?:r[aá]pida|corta|breve|short|quick)\b",
    r"\bsin pensar(?:lo)?(?: mucho)?\b", r"\bno (?:lo )?pienses(?: mucho)?\b",
    r"\bdon'?t (?:over)?think\b", r"\bno need to think\b",
    r"\bquick question\b", r"\bpregunta r[aá]pida\b",
    r"\bs[ií] o no\b", r"\byes or no\b", r"\btl;?dr\b", r"\(r[aá]pido\)",
)
# "In one line" asks for a SHORT ANSWER, not for less thinking: it only means
# fast when nothing in the message is work ("read these two files and tell me
# in one line what each does" is still work).
_BRIEF_ASK = _rx(
    # "Una frase, sin herramientas" asks for brevity as plainly as "en una
    # frase" (seen 26-09: it fell through to a full 4,096-token think).
    r"\b(?:en )?una (?:palabra|frase|l[ií]nea)\b", r"\b(?:in )?one (?:word|sentence|line)\b",
)

# Work that benefits from reasoning, by family (the family names become the
# decision's `reasons`).
_THINK_SIGNALS = (
    ("code", _rx(
        r"```", r"\b\w+\.(?:py|js|ts|tsx|jsx|java|go|rs|rb|php|cs|cpp|c|h|sql|sh|ps1|yml|yaml|toml)\b",
        r"\btraceback\b", r"\bstack ?trace\b", r"\b\w+(?:Error|Exception)\b", r"\bsegfault\b",
        r"\b(?:funci[oó]n|function|clase|class|m[eé]todo|method|script|regex|endpoint|api|query|"
        r"consulta sql|compil\w*|bug|test(?:s)?|unit test|refactor\w*|algoritmo|algorithm)\b",
        r"\b(?:def|return|import|const|let|var|async|await|lambda)\s", r"[{};]\s*$",
    )),
    ("debug", _rx(
        r"\b(?:debug\w*|depura\w*|fix\w*|arregl\w*|corrig\w*|falla\w*|fails?|failing|broken|"
        r"no funciona|doesn'?t work|not working|crash\w*|error(?:es)?)\b",
    )),
    ("implement", _rx(
        r"\b(?:implement\w*|refactoriz\w*|migra\w*|optimiz\w*|optimis\w*|build|construye|"
        r"programa(?:r|me)?|code|codifica)\b",
    )),
    ("math", _rx(
        r"\d+\s*[-+*/^×÷]\s*\d+", r"\b(?:integral|derivad\w*|derivative|ecuaci[oó]n\w*|equations?|"
        r"resuelve|solve|probabilidad|probability|estad[ií]stic\w*|statistic\w*|matriz|matrix|"
        r"teorema|theorem|l[ií]mite|limit of|calcula\w*|calculate|compute)\b",
    )),
    ("planning", _rx(
        r"\b(?:plan|planifica\w*|planning|estrategia|strategy|roadmap|hoja de ruta|arquitectura|"
        r"architecture|dise[ñn]a\w*|design|organiza\w*|cronograma|schedule a|trade-?offs?)\b",
    )),
    # Dates and calendars: which weekday a date falls on, long weekends,
    # "next week", N days from now. Seen live with thinking off ("fast" for a
    # short question): "el 2 de noviembre cae en sábado", "Lunes 29 sep" --
    # both wrong -- while the same model with thinking on got every weekday
    # right in a direct A/B.
    ("dates", _rx(
        r"\bqu[eé] d[ií]a (?:de la semana )?(?:es|cae|caer[aá]|ser[aá]|fue|era|toca)\b",
        r"\ben qu[eé] d[ií]a (?:de la semana )?cae\b", r"\bpuentes?\b", r"\bfestivos?\b",
        r"\bd[ií]as? (?:laborables|h[aá]biles|naturales)\b",
        r"\b(?:dentro de|hace|en|faltan|quedan) \d+ (?:d[ií]as|semanas|meses)\b",
        r"\bcu[aá]ntos d[ií]as\b", r"\b(?:semana|mes) que viene\b", r"\bpr[oó]xim[oa] (?:semana|mes)\b",
        r"\b\d{1,2} de (?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|sept?iembre|octubre|noviembre|diciembre)\b",
        r"\bwhat day (?:of the week )?(?:is|was|will)\b", r"\bhow many days\b", r"\bdays (?:until|left|from now)\b",
        r"\bnext (?:week|month)\b", r"\bbank holiday\b", r"\blong weekend\b",
    )),
    # Quantities for N people, a recipe scaled, a shopping list with amounts:
    # "somos 8, pásame la lista de la compra con cantidades" came back with
    # "6 muslos (2,5–3 kg)".
    ("quantities", _rx(
        r"\bpara \d+ (?:personas|comensales|invitados|raciones)\b", r"\bsomos \d+\b",
        r"\blista de la compra\b", r"\bcantidades\b", r"\braciones\b", r"\bescala\w*\b",
        r"\bfor \d+ (?:people|guests|servings)\b", r"\bshopping list\b", r"\bscale (?:it|the recipe)\b",
    )),
    ("compare", _rx(
        r"\bcompar\w*\b", r"\bvs\.?\b", r"\bversus\b", r"\bdiferencias? entre\b", r"\bdifference between\b",
        r"\bpros y contras\b", r"\bpros and cons\b", r"\bventajas\b", r"\bwhich is better\b",
        r"\bcu[aá]l es mejor\b", r"\bqu[eé] es mejor\b", r"\bor should i\b", r"\bo deber[ií]a\b",
    )),
    ("analysis", _rx(
        r"\b(?:analiza\w*|an[aá]lisis|analy[sz]\w*|eval[uú]a\w*|evaluat\w*|revisa\w*|review\w*|"
        r"audit\w*|diagnos\w*|investiga\w*|explica por qu[eé]|explain why|por qu[eé]|why does|why is|"
        r"c[oó]mo funciona|how does .{1,40} work|razona|reason about|justifica\w*|justify)\b",
    )),
)

# Signs that a request stacks several constraints on one answer.
_CONSTRAINT = _rx(
    r"\bsin (?:usar|que)\b", r"\bwithout\b", r"\bmust\b", r"\btiene que\b", r"\bdebe(?:n|r[aá])?\b",
    r"\bcomo m[aá]ximo\b", r"\bat most\b", r"\bal menos\b", r"\bat least\b", r"\bno m[aá]s de\b",
    r"\bno more than\b", r"\bexcept\w*\b", r"\bexcepto\b", r"\bsalvo\b", r"\bonly if\b",
    r"\bsolo si\b", r"\ba la vez\b", r"\bat the same time\b", r"\bteniendo en cuenta\b",
    r"\btaking into account\b", r"\brespetando\b",
)
_LIST_ITEM = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+\S")

# A short "who/what/when/where" lookup: answerable from memory, no reasoning.
_LOOKUP = _rx(
    r"^\s*¿?\s*(?:qu[eé]|qui[eé]n|cu[aá]ndo|d[oó]nde|cu[aá]l|cu[aá]nt[oa]s?|what|who|when|where|which|"
    r"how many|how much|how old|how tall|how far)\b",
    r"^\s*(?:define|definici[oó]n de|significado de|meaning of|capital (?:de|of)|traduce|translate)\b",
)

# A bare go-ahead: small talk anywhere else, an approval once work started.
_GO_AHEAD = _rx(
    r"^\s*(?:s[ií]|ok|okay|vale|claro|adelante|sigue|contin[uú]a|dale|hazlo|yes|yep|go ahead|"
    r"continue|do it|proceed)\s*[.!]*\s*$",
)

_DEEP_WORDS = 250
_LONG_BRIEF_WORDS = 120
_SHORT_WORDS = 12
_FAST_MAX_WORDS = 40
_BIG_ATTACHMENTS = 3


# A word problem: several quantities plus a relation or a "how much" --
# "A weighs twice B, C is 4 kg more, together 44 kg: how much is each?"
# carries no operator and no "solve", yet is exactly what reasoning is for.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_QUANT_WORDS = _rx(
    r"\b(?:cu[aá]nt[oa]s?|how (?:much|many|long|old|far)|doble|triple|mitad|tercio|cuarto de|"
    r"twice|double|triple|half|third|quarter|m[aá]s que|menos que|more than|less than|fewer than|"
    r"juntos?|juntas|together|in total|en total|por ciento|percent|porcentaje|percentage|"
    r"proporci[oó]n|ratio|promedio|average|media de|velocidad|speed|tarda\w*|takes?)\b",
    r"%",
)


def _word_problem(text: str) -> bool:
    numbers = len(_NUMBER.findall(text))
    if numbers >= 2 and len(_QUANT_WORDS.findall(text)) >= 2:
        return True
    return numbers >= 3 and "?" in text and bool(_QUANT_WORDS.search(text))


def _small_talk(text: str) -> bool:
    try:
        from src.turn_effort import is_small_talk
        return bool(is_small_talk(text))
    except Exception:  # noqa: BLE001
        return False


def _result(mode: str, reasons: List[str], why: str, confidence: str) -> Dict[str, Any]:
    return {"mode": mode, "source": "rule", "reasons": reasons, "why": why, "confidence": confidence}


def _light(reasons: List[str], why: str, confidence: str) -> Dict[str, Any]:
    """``think`` at the lowest reasoning effort: a short question still gets
    a quick look before the answer."""
    out = _result("think", reasons + ["light"], why, confidence)
    out["effort"] = "low"
    return out


def _decide(text: str, attachments: int, agent: bool, coding: bool, history_len: int) -> Dict[str, Any]:
    raw = str(text or "")
    stripped = raw.strip()
    words = len(stripped.split())
    try:
        attachments = max(0, int(attachments or 0))
    except (TypeError, ValueError):
        attachments = 0

    if not stripped and not attachments:
        return _result("fast", ["empty"], "nothing to reason about", "low")

    if _DEEP_ASK.search(stripped):
        return _result("deep", ["asked_depth"], "the message asks for depth", "high")

    families = [name for name, rx in _THINK_SIGNALS if rx.search(stripped)]
    if "math" not in families and _word_problem(stripped):
        families.append("math")
    constraints = len(_CONSTRAINT.findall(stripped))
    list_items = len(_LIST_ITEM.findall(raw))
    questions = stripped.count("?")
    multi_constraint = constraints >= 2 or list_items >= 3 or questions >= 3

    if _FAST_ASK.search(stripped):
        return _result("fast", ["asked_speed"], "the message asks for a quick answer", "high")
    if _BRIEF_ASK.search(stripped) and not families and not multi_constraint:
        return _result("fast", ["asked_brevity"], "the message asks for a one-line answer", "medium")

    if words >= _DEEP_WORDS and (families or multi_constraint or list_items >= 2):
        return _result("deep", ["long_brief"] + families, "a long multi-part brief", "medium")
    if words >= _LONG_BRIEF_WORDS and list_items >= 3 and families:
        return _result("deep", ["multi_part"] + families, "a brief with many parts", "medium")
    if attachments >= _BIG_ATTACHMENTS and ("analysis" in families or "compare" in families):
        return _result("deep", ["attachments", "analysis"], "several attachments to analyse", "medium")

    if _GO_AHEAD.search(stripped) and (coding or agent) and history_len > 0:
        # "sí" after an approval card is the go-ahead for the next action.
        return _result("think", ["go_ahead"], "a go-ahead in a running task", "medium")

    if _small_talk(stripped) and not families:
        return _result("fast", ["small_talk"], "small talk", "high")

    if families or multi_constraint:
        reasons = list(families) + (["constraints"] if multi_constraint else [])
        confidence = "high" if len(reasons) >= 2 else "medium"
        return _result("think", reasons, "work that benefits from reasoning", confidence)

    if attachments:
        return _result("think", ["attachments"], "attachments to read", "low")

    # A short question in a chat bound to a folder is not a coding task by
    # itself: "¿Y la imagen 3?" got the full 4,096-token budget and took
    # minutes on the local 27B (26-09).
    if coding and words <= _SHORT_WORDS and stripped.endswith("?"):
        return _light(["short_question"], "a short question in a coding chat", "medium")

    if coding:
        return _result("think", ["coding_context"], "a coding turn", "low")

    # A short question with no sign of work still gets a little reasoning
    # (effort "low"), not none. Measured on the 27B, same server, eight
    # questions with one exact answer (weekdays, counting, ordering, dates,
    # VAT): thinking off 6/24, low effort 6/6, full effort 16/16. With
    # thinking off it named the wrong weekday for a date the user asked
    # about; that is what "fast" meant for any question under 40 words.
    if words <= _SHORT_WORDS and _LOOKUP.search(stripped):
        return _light(["lookup"], "a short factual lookup", "medium")

    if words <= _FAST_MAX_WORDS:
        return _light(["short"], "a short turn with no sign of work", "low")
    return _result("think", ["long"], "a long turn", "low")


def decide(text: str, *, attachments: int = 0, agent: bool = False, coding: bool = False,
           history_len: int = 0) -> Dict[str, Any]:
    """Pick ``fast``/``think``/``deep`` for one user turn. Deterministic,
    never raises. Returns ``{mode, source: "rule", reasons, why, confidence}``."""
    try:
        return _decide(text, attachments, agent, coding, history_len)
    except Exception:  # noqa: BLE001 -- never fail a turn over this
        return _result("think" if coding else "fast", ["fallback"], "rule unavailable", "low")


def _int_setting(settings: Optional[Mapping[str, Any]], key: str, fallback: int) -> int:
    value: Any = None
    if isinstance(settings, Mapping):
        value = settings.get(key)
    else:
        try:
            from src.settings import get_setting
            value = get_setting(key, fallback)
        except Exception:  # noqa: BLE001
            value = None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    return out if out >= 0 else fallback


def to_overrides(mode: str, model: str = "", settings: Optional[Mapping[str, Any]] = None, *,
                 effort: bool = True) -> Dict[str, Any]:
    """``gen_overrides`` for ``mode``; ``{}`` for ``auto`` or anything unknown.

    ``settings`` may be a plain mapping (tests) or ``None`` (read the saved
    settings). ``effort=False`` leaves out ``reasoning_effort`` for a backend
    that would reject the field (a strict remote provider). ``model`` is
    accepted for symmetry with ``src/effort_profile.resolve``; llm_core
    already drops thinking fields a model has no use for.
    """
    m = normalize(mode)
    if m == "fast":
        return {"think": False}
    if m == "think":
        return {"think": True,
                "reasoning_budget": _int_setting(settings, "think_mode_budget_think", DEFAULT_BUDGET_THINK)}
    if m == "deep":
        out: Dict[str, Any] = {
            "think": True,
            "reasoning_budget": _int_setting(settings, "think_mode_budget_deep", DEFAULT_BUDGET_DEEP),
        }
        if effort:
            out["reasoning_effort"] = "high"
        return out
    return {}


def resolve_turn(requested: Any, gen_overrides: Optional[Dict[str, Any]], text: str, *,
                 model: str = "", supports_thinking: bool = True, attachments: int = 0,
                 agent: bool = False, coding: bool = False, history_len: int = 0,
                 settings: Optional[Mapping[str, Any]] = None, effort: bool = True,
                 default_mode: str = "auto") -> Dict[str, Any]:
    """The whole per-turn decision the chat route makes, in one pure call.

    Precedence: an explicit mode (fast/think/deep) > an explicit ``think`` in
    ``gen_overrides`` (``/think on|off``, the Generation switch) > the auto
    rule > today's defaults. Returns ``{"overrides": <new gen_overrides>,
    "event": <think_mode SSE payload or None>}``; ``overrides`` is a new dict,
    the input is never mutated. A model without a thinking mode gets no
    change and no event.
    """
    base = dict(gen_overrides) if isinstance(gen_overrides, dict) else {}
    req = normalize(requested) or normalize(default_mode) or "auto"
    if not supports_thinking:
        return {"overrides": base, "event": None}
    if req in EXPLICIT_MODES:
        ov = to_overrides(req, model, settings, effort=effort)
        merged = {k: v for k, v in base.items() if k not in ("think", "reasoning_budget", "reasoning_effort")}
        merged.update(ov)
        return {"overrides": merged, "event": {
            "mode": req, "requested": req, "source": "explicit", "reasons": [],
            "budget": ov.get("reasoning_budget"),
        }}
    if base.get("think") is not None:
        on = bool(base.get("think"))
        return {"overrides": base, "event": {
            "mode": "think" if on else "fast", "requested": "auto", "source": "override",
            "reasons": ["think_override"], "budget": base.get("reasoning_budget") if on else None,
        }}
    decision = decide(text, attachments=attachments, agent=agent, coding=coding, history_len=history_len)
    mode = decision.get("mode") or "fast"
    ov = to_overrides(mode, model, settings, effort=effort)
    if mode == "think" and decision.get("effort") == "low" and effort:
        ov["reasoning_effort"] = "low"
        ov["reasoning_budget"] = _int_setting(settings, "think_mode_budget_light", DEFAULT_BUDGET_LIGHT)
    merged = dict(base)
    for k, v in ov.items():
        # A budget/effort the client pinned itself still wins over the rule's.
        if k in ("reasoning_budget", "reasoning_effort") and k in base:
            continue
        merged[k] = v
    return {"overrides": merged, "event": {
        "mode": mode, "requested": "auto", "source": "rule",
        "reasons": list(decision.get("reasons") or []), "why": decision.get("why", ""),
        "confidence": decision.get("confidence", ""),
        "budget": merged.get("reasoning_budget") if merged.get("think") else None,
    }}
