"""src/grounding_ledger.py — where does each figure in an answer come from?

A local model that has just read a tool result will still state a total it
never saw, or round a figure into a different one. This module lists the
figures of a final answer and labels each against the evidence of the turn:

* ``observed``    the same number appears in a tool result of this turn
                  (allowing for the rounding the answer shows, "1,235" for
                  1234.567, and for "1.2M" style suffixes);
* ``cited``       the number appears in the user's own message;
* ``derived``     it follows from numbers of the evidence by one bounded
                  operation (a sum of two or three, a difference, a product,
                  a ratio, a percentage, a percentage change, the total or the
                  mean); the label carries the formula so it can be judged;
* ``count``       a whole number that equals how many items the answer lists,
                  or how many lines a tool listed;
* ``unsupported`` none of the above.

Only significant figures are listed: anything with a unit, a currency or a
percent sign, any decimal, and whole numbers of 13 or more. Small bare
integers ("3 steps"), years, dates, times, versions, list markers, code and
URLs are left out, so the ledger stays about claims, not about formatting.

The check is skipped when the turn used no tool at all (an answer from general
knowledge has nothing to be grounded in).

Behaviour, all behind the setting ``agent_answer_grounding_ledger`` (off by
default): the first time figures are unsupported, ask for one corrected answer
through the existing rewrite path (`correction_note`); when the correction is
not available or did not help, strike the figures through and append a short
note (`mark_unsupported`). `grounding_review` bundles the decision.

Limits: at most 30 distinct evidence values feed the derivations (the first 30
in order of appearance), at most 60 figures are listed, and evidence text is
cut at 200 000 characters. A coincidental match is possible for a derived
figure, which is why its formula is always shown.
"""
from __future__ import annotations

import bisect
import itertools
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SETTING_KEY = "agent_answer_grounding_ledger"
MAX_FIGURES = 60
MAX_EVIDENCE_VALUES = 30
MAX_EVIDENCE_CHARS = 200_000
MIN_SIGNIFICANT_INTEGER = 13

LABELS = ("observed", "cited", "derived", "count", "unsupported")

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|"
    "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|"
    "ene|abr|ago|dic"
)

_CURRENCY_SYMBOLS = "$€£¥"
_CURRENCY_WORDS = {"eur", "euro", "euros", "usd", "dollar", "dollars", "dolar", "dolares", "dólar", "dólares",
                   "gbp", "pound", "pounds", "libras", "mxn", "cad", "chf", "jpy"}
_UNIT_WORDS = {
    "kg", "g", "mg", "lb", "lbs", "oz", "t", "km", "m", "cm", "mm", "mi", "ft", "in", "l", "ml", "cl", "dl",
    "h", "hr", "hrs", "min", "mins", "s", "sec", "secs", "ms", "hz", "khz", "mhz", "ghz",
    "b", "kb", "mb", "gb", "tb", "kib", "mib", "gib", "tib", "w", "kw", "mw", "wh", "kwh", "v", "a", "ma",
    "gbps", "mbps", "kbps", "fps", "rpm", "px", "pt", "dpi", "tokens", "token",
    "horas", "hora", "minutos", "minuto", "segundos", "segundo", "hours", "hour", "minutes", "minute",
    "seconds", "second", "dias", "días", "dia", "día", "days", "day", "semanas", "weeks", "meses", "months",
    "años", "year", "years", "km/h", "kmh", "mph", "°c", "°f", "°",
}
_SCALE_WORDS = {"k": Decimal(1000), "mil": Decimal(1000), "thousand": Decimal(1000),
                "million": Decimal(10) ** 6, "millon": Decimal(10) ** 6, "millón": Decimal(10) ** 6,
                "millones": Decimal(10) ** 6, "bn": Decimal(10) ** 9, "billion": Decimal(10) ** 9,
                "mm": None}  # "mm" stays a unit (millimetres); listed only to be explicit

_NUM = r"\d{1,3}(?:[.,  ']\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_FIGURE = re.compile(
    r"(?P<cur>[" + re.escape(_CURRENCY_SYMBOLS) + r"]\s?)?"
    r"(?P<num>" + _NUM + r")(?!\d)"
    r"(?:(?P<pct>\s?%)|(?P<word>\s?[A-Za-zµ°€$£]+(?:/[A-Za-z]+)?[²³]?))?"
)

# Regions that never hold a claimed figure. Each is blanked (same length) before scanning.
_FENCED = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_URL = re.compile(r"(?:https?|file|sandbox)://\S+|\bwww\.\S+", re.IGNORECASE)
_STRUCK = re.compile(r"~~[^~\n]+~~")
_DATE_NUMERIC = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?m\.?)?", re.IGNORECASE)
_DATE_DAY_MONTH = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:de\s+|of\s+)?(?:" + _MONTHS + r")\b\.?(?:\s+(?:de(?:l)?\s+)?\d{4})?",
    re.IGNORECASE)
_DATE_MONTH_DAY = re.compile(
    r"\b(?:" + _MONTHS + r")\b\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?", re.IGNORECASE)
_VERSIONISH = re.compile(r"\bv?\d+(?:\.\d+){2,}\b")
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-*+][ \t]+)?\d{1,3}[.)](?=[ \t])")
_HEADING_NUMBER = re.compile(r"(?m)^[ \t]*#*[ \t]*\d+(?:\.\d+)+(?=[ \t])")
_LIST_ITEM = re.compile(r"(?m)^[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+\S")


def _blank(match: "re.Match[str]") -> str:
    return " " * len(match.group(0))


def _blank_version(match: "re.Match[str]") -> str:
    raw = match.group(0)
    groups = raw.lstrip("vV").split(".")
    if len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]):
        return raw  # 1.234.567 is a number written with thousands separators
    return " " * len(raw)


def _mask(text: str) -> str:
    out = text
    for pattern in (_FENCED, _INLINE_CODE, _URL, _STRUCK, _DATE_NUMERIC, _TIME, _DATE_DAY_MONTH,
                    _DATE_MONTH_DAY, _LIST_MARKER, _HEADING_NUMBER):
        out = pattern.sub(_blank, out)
    return _VERSIONISH.sub(_blank_version, out)


def _candidates(token: str) -> List[Tuple[Decimal, int]]:
    """Every reading of a written number as (value, digits after the decimal mark)."""
    t = re.sub(r"[  ']", "", token)
    has_dot, has_comma = "." in t, "," in t
    try:
        if has_dot and has_comma:
            dec = "." if t.rfind(".") > t.rfind(",") else ","
            thou = "," if dec == "." else "."
            whole, _, frac = t.rpartition(dec)
            return [(Decimal(whole.replace(thou, "") + "." + frac), len(frac))]
        for sep in (".", ","):
            if sep not in t:
                continue
            parts = t.split(sep)
            if len(parts) > 2:
                return [(Decimal("".join(parts)), 0)]
            whole, frac = parts
            decimal_reading = (Decimal(whole + "." + frac), len(frac))
            if len(frac) == 3 and 1 <= len(whole) <= 3 and not whole.startswith("0"):
                return [(Decimal(whole + frac), 0), decimal_reading]
            return [decimal_reading]
        return [(Decimal(t), 0)]
    except InvalidOperation:
        return []


def _classify_word(word: str) -> Tuple[str, Decimal]:
    """(kind, scale) for the word after a number: kind is scale/currency/unit/word."""
    raw = word.strip()
    low = raw.lower()
    if raw == "M" or raw == "B":
        return "scale", (Decimal(10) ** 6 if raw == "M" else Decimal(10) ** 9)
    if low in _SCALE_WORDS and _SCALE_WORDS[low] is not None:
        return "scale", _SCALE_WORDS[low]
    if low in _CURRENCY_WORDS or raw in tuple(_CURRENCY_SYMBOLS):
        return "currency", Decimal(1)
    if low in _UNIT_WORDS:
        return "unit", Decimal(1)
    return "word", Decimal(1)


def _is_year(value: Decimal, decimals: int) -> bool:
    return decimals == 0 and Decimal(1900) <= value <= Decimal(2100)


def extract_figures(text: str) -> List[Dict[str, Any]]:
    """Significant figures of `text`, each with its span and readings."""
    body = str(text or "")
    masked = _mask(body)
    figures: List[Dict[str, Any]] = []
    for m in _FIGURE.finditer(masked):
        num = m.group("num")
        digits = re.sub(r"\D", "", num)
        if len(digits) >= 10 and not re.search(r"[.,]", num):
            continue  # an identifier or a phone number, not a quantity
        start, end = m.start(), m.end()
        before = masked[start - 1] if start else " "
        if before.isalnum() or before in "_/.":
            continue  # part of a word or path ("v2", "x3", "a.5")
        kind, scale, unit = "number", Decimal(1), ""
        if m.group("cur"):
            kind, unit = "money", m.group("cur").strip()
        if m.group("pct"):
            kind, unit = "percent", "%"
        elif m.group("word"):
            wkind, wscale = _classify_word(m.group("word"))
            if wkind == "scale":
                scale = wscale
                if kind == "number":
                    unit = m.group("word").strip()
            elif wkind == "currency":
                kind, unit = "money", m.group("word").strip()
            elif wkind == "unit":
                kind, unit = "unit", m.group("word").strip()
            else:
                # An ordinary word after the number ("250 users"): keep the number only.
                end = m.end("num")
        readings = [(v * scale, d) for v, d in _candidates(num)]
        if not readings:
            continue
        value0, decimals0 = readings[0]
        has_unit = kind != "number" or scale != 1
        significant = (has_unit or decimals0 > 0 or abs(value0) >= MIN_SIGNIFICANT_INTEGER
                       or any(d > 0 for _, d in readings))
        if not significant:
            continue
        if not has_unit and all(_is_year(abs(v), d) for v, d in readings):
            continue
        figures.append({
            "text": body[start:end], "start": start, "end": end,
            "kind": kind, "unit": unit, "scale": scale,
            "readings": readings,
        })
        if len(figures) >= MAX_FIGURES:
            break
    return figures


def _evidence_values(texts: Iterable[str]) -> List[Decimal]:
    """Distinct numbers of the evidence, in order of appearance."""
    seen: Dict[Decimal, None] = {}
    budget = MAX_EVIDENCE_CHARS
    for text in texts:
        chunk = str(text or "")[:budget]
        budget -= len(chunk)
        masked = _mask(chunk)
        for m in _FIGURE.finditer(masked):
            scale = Decimal(1)
            if m.group("word"):
                kind, wscale = _classify_word(m.group("word"))
                if kind == "scale":
                    scale = wscale
            for value, _ in _candidates(m.group("num")):
                seen.setdefault(value * scale, None)
        if budget <= 0:
            break
    return list(seen)


def _tolerance(decimals: int, scale: Decimal) -> Decimal:
    """Half a unit of the last digit the answer shows, in the answer's own scale."""
    return Decimal("0.5") * (Decimal(10) ** -decimals) * scale + Decimal("1e-9")


def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _derived_table(values: Sequence[Decimal]) -> List[Tuple[float, str]]:
    """Sorted (result, formula) pairs for the bounded set of operations."""
    vals = [float(v) for v in values[:MAX_EVIDENCE_VALUES]]
    rows: List[Tuple[float, str]] = []
    for a in vals:
        rows.append((a * 100, f"{_fmt(a)} x 100"))
        rows.append((a / 100, f"{_fmt(a)} / 100"))
    for a, b in itertools.permutations(vals, 2):
        rows.append((a - b, f"{_fmt(a)} - {_fmt(b)}"))
        if b:
            rows.append((a / b, f"{_fmt(a)} / {_fmt(b)}"))
            rows.append((a / b * 100, f"{_fmt(a)} / {_fmt(b)} as a percentage"))
            rows.append(((a - b) / b * 100, f"change from {_fmt(b)} to {_fmt(a)} as a percentage"))
    for a, b in itertools.combinations(vals, 2):
        rows.append((a + b, f"{_fmt(a)} + {_fmt(b)}"))
        rows.append((a * b, f"{_fmt(a)} x {_fmt(b)}"))
    for a, b, c in itertools.combinations(vals, 3):
        rows.append((a + b + c, f"{_fmt(a)} + {_fmt(b)} + {_fmt(c)}"))
    if len(vals) >= 2:
        rows.append((sum(vals), "total of the listed values"))
        rows.append((sum(vals) / len(vals), "mean of the listed values"))
    rows.sort(key=lambda r: r[0])
    return rows


def _find_derived(table: List[Tuple[float, str]], target: Decimal, tol: Decimal) -> Optional[str]:
    t, tl = float(target), float(tol)
    keys = [r[0] for r in table]
    i = bisect.bisect_left(keys, t - tl)
    best: Optional[Tuple[float, str]] = None
    while i < len(table) and table[i][0] <= t + tl:
        gap = abs(table[i][0] - t)
        if best is None or gap < best[0]:
            best = (gap, table[i][1])
        i += 1
    return best[1] if best else None


def _list_counts(answer: str) -> set:
    """How many items the answer lists: each list block and the overall total."""
    counts: set = set()
    block = 0
    total = 0
    for line in str(answer or "").splitlines():
        if _LIST_ITEM.match(line):
            block += 1
            total += 1
        elif line.strip() == "":
            continue
        else:
            if block:
                counts.add(block)
            block = 0
    if block:
        counts.add(block)
    if total:
        counts.add(total)
    return counts


def _output_counts(outputs: Sequence[str]) -> set:
    counts: set = set()
    for out in outputs:
        text = str(out or "")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if lines:
            counts.add(len(lines))
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            counts.add(len(data))
        elif isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    counts.add(len(v))
    return counts


def _snippet(text: str, figure: Dict[str, Any], width: int = 24) -> str:
    return text[max(0, figure["start"] - width):figure["end"] + width].replace("\n", " ").strip()


def build_ledger(answer: str, question: str = "", tool_outputs: Sequence[str] = ()) -> Dict[str, Any]:
    """Label every significant figure of `answer` against this turn's evidence."""
    outputs = [str(o or "") for o in tool_outputs if str(o or "").strip()]
    if not outputs:
        return {"status": "skipped", "reason": "no_tool_evidence", "entries": [], "unsupported": [],
                "counts": {}}
    figures = extract_figures(answer)
    observed = _evidence_values(outputs)
    cited = _evidence_values([question])
    derived_table: Optional[List[Tuple[float, str]]] = None
    list_counts = _list_counts(answer)
    out_counts = _output_counts(outputs)
    entries: List[Dict[str, Any]] = []
    for fig in figures:
        label, via = "unsupported", ""
        for value, decimals in fig["readings"]:
            tol = _tolerance(decimals, fig["scale"])
            mag = abs(value)
            hit = next((e for e in observed if _close(abs(e), mag, tol)), None)
            if hit is not None:
                label, via = "observed", f"tool result value {_fmt(float(hit))}"
                break
            hit = next((e for e in cited if _close(abs(e), mag, tol)), None)
            if hit is not None:
                label, via = "cited", "stated in your message"
                break
            if (fig["kind"] == "number" and fig["scale"] == 1 and decimals == 0
                    and int(mag) in (list_counts | out_counts)):
                label, via = "count", "number of listed items"
                break
            if derived_table is None:
                derived_table = _derived_table(observed + [c for c in cited if c not in observed])
            formula = _find_derived(derived_table, mag, tol)
            if formula:
                label, via = "derived", formula
                break
        entries.append({
            "text": fig["text"], "value": _fmt(float(fig["readings"][0][0])), "kind": fig["kind"],
            "unit": fig["unit"], "label": label, "via": via,
            "start": fig["start"], "end": fig["end"], "context": _snippet(answer, fig),
        })
    counts = {k: sum(1 for e in entries if e["label"] == k) for k in LABELS}
    return {"status": "checked", "reason": "", "entries": entries,
            "unsupported": [e for e in entries if e["label"] == "unsupported"], "counts": counts}


def _looks_spanish(text: str) -> bool:
    low = f" {str(text or '').lower()} "
    hits = sum(low.count(w) for w in (" el ", " la ", " de ", " que ", " los ", " las ", " es ", " en ", " y "))
    hits_en = sum(low.count(w) for w in (" the ", " of ", " and ", " is ", " are ", " to ", " that "))
    return hits > hits_en


def correction_note(unsupported: Sequence[Dict[str, Any]]) -> str:
    """The request added to the rewrite message when figures lack support."""
    shown = ", ".join(f'"{u["text"]}"' for u in list(unsupported)[:6])
    return (f"These figures in your answer do not appear in the tool results or in the user's message, "
            f"and do not follow from them by a simple calculation: {shown}. Use only figures that "
            "the tool results contain, or work a figure out from them and say how; if a figure is not "
            "available, say that it is not available instead of stating one.")


def mark_unsupported(answer: str, unsupported: Sequence[Dict[str, Any]], lang: Optional[str] = None) -> str:
    """Strike the unsupported figures through and add a short note."""
    text = str(answer or "")
    spans = sorted({(int(u["start"]), int(u["end"])) for u in unsupported}, reverse=True)
    for start, end in spans:
        if 0 <= start < end <= len(text):
            text = text[:start] + "~~" + text[start:end] + "~~" + text[end:]
    if not spans:
        return text
    spanish = (lang == "es") if lang else _looks_spanish(answer)
    note = ("Nota: las cifras tachadas no se pudieron comprobar con los resultados de las herramientas "
            "ni con tu mensaje." if spanish else
            "Note: the struck-through figures could not be checked against the tool results or your message.")
    return text.rstrip() + "\n\n> " + note


def is_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting(SETTING_KEY, False))
    except Exception:  # noqa: BLE001 - a settings problem never enables a check
        return False


def trace_entry(ledger: Dict[str, Any], action: str = "none") -> Dict[str, Any]:
    """A compact, serialisable form of the ledger for the turn trace."""
    return {
        "kind": "grounding_ledger", "status": ledger.get("status"), "reason": ledger.get("reason", ""),
        "action": action, "counts": ledger.get("counts", {}),
        "figures": [{"text": e["text"], "label": e["label"], "via": e["via"]}
                    for e in ledger.get("entries", [])][:MAX_FIGURES],
    }


def grounding_review(answer: str, question: str = "", tool_outputs: Sequence[str] = (), *,
                     retry_used: bool = False, enabled: Optional[bool] = None,
                     lang: Optional[str] = None) -> Dict[str, Any]:
    """Decide what to do with the figures of a final answer.

    action "none"   nothing to do (off, skipped, or every figure is supported);
    action "retry"  ask for one corrected answer (`note` is the request) — only
                    while `retry_used` is False;
    action "mark"   the correction was already asked for (or is not possible):
                    `answer` is the text with unsupported figures struck through.
    The ledger is always returned (when enabled) so the caller can record
    `trace` with the turn.
    """
    if enabled is None:
        enabled = is_enabled()
    if not enabled:
        return {"action": "none", "ledger": None, "trace": None, "note": "", "answer": str(answer or "")}
    try:
        ledger = build_ledger(answer, question, tool_outputs)
    except Exception as exc:  # noqa: BLE001 - a check never breaks a turn
        return {"action": "none", "ledger": None, "trace": {"kind": "grounding_ledger",
                                                              "status": "error", "reason": str(exc)[:200]},
                "note": "", "answer": str(answer or "")}
    unsupported = ledger["unsupported"]
    if ledger["status"] != "checked" or not unsupported:
        return {"action": "none", "ledger": ledger, "trace": trace_entry(ledger), "note": "",
                "answer": str(answer or "")}
    if not retry_used:
        return {"action": "retry", "ledger": ledger, "trace": trace_entry(ledger, "retry"),
                "note": correction_note(unsupported), "answer": str(answer or "")}
    return {"action": "mark", "ledger": ledger, "trace": trace_entry(ledger, "mark"), "note": "",
            "answer": mark_unsupported(answer, unsupported, lang)}


__all__ = ["SETTING_KEY", "LABELS", "extract_figures", "build_ledger", "correction_note",
           "mark_unsupported", "grounding_review", "trace_entry", "is_enabled"]
