"""src/hoja_format.py -- number formats and dates of Hoja (the spreadsheet): the Excel format codes the cells carry
(``0.00``, ``#,##0.00``, ``$#,##0.00``, ``0.0%``, ``yyyy-mm-dd``, ``@``), shown and parsed the way a sheet does.

Dates are serial numbers (1900 system, day 0 = 1899-12-30, which is what Excel and LibreOffice agree on after
1900-02-28). Nothing here touches a file or a window.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import math
import re
from typing import Any, Optional, Tuple

EPOCH = _dt.date(1899, 12, 30)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

NAMED = {"general": "General", "number": "#,##0.00", "integer": "0", "thousands": "#,##0", "percent": "0.00%",
         "date": "yyyy-mm-dd", "datetime": "yyyy-mm-dd hh:mm", "time": "hh:mm:ss", "text": "@", "scientific": "0.00E+00"}
CURRENCY = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "$": "$", "€": "€", "£": "£", "¥": "¥"}

# What a cell SHOWS follows the interface language of whoever looks (the files keep real numbers and Excel codes).
# The language rides a context variable set by the route (header X-Hoja-Locale); the agent and the files use English.
_LOCALE: "contextvars.ContextVar[str]" = contextvars.ContextVar("hoja_locale", default="en")
NBSP = "\u00a0"
MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_SYMBOLS = {"$", "€", "£", "¥"}


def set_locale(value: Any) -> None:
    _LOCALE.set("es" if str(value or "").strip().lower().startswith("es") else "en")


def get_locale() -> str:
    return _LOCALE.get()


def serial_from_date(d: _dt.date) -> int:
    return (d - EPOCH).days


def date_from_serial(n: float) -> _dt.date:
    return EPOCH + _dt.timedelta(days=int(math.floor(n)))


def datetime_from_serial(n: float) -> _dt.datetime:
    day = math.floor(n)
    secs = round((n - day) * 86400)
    return _dt.datetime.combine(date_from_serial(day), _dt.time()) + _dt.timedelta(seconds=secs)


def serial_from_datetime(d: _dt.datetime) -> float:
    return serial_from_date(d.date()) + (d.hour * 3600 + d.minute * 60 + d.second) / 86400.0


def format_code(name: Any, decimals: Optional[int] = None, symbol: Optional[str] = None) -> str:
    """A format name (``currency``, ``percent`` ...) or a raw Excel code -> the Excel code."""
    if name is None or name == "":
        return "General"
    s = str(name).strip()
    low = s.lower()
    dec = None if decimals is None else max(0, min(15, int(decimals)))
    if low in ("currency", "money", "moneda") or low.startswith("currency:"):
        sym = CURRENCY.get((symbol or (low.split(":", 1)[1] if ":" in low else "usd")).lower(), symbol or "$")
        d = 2 if dec is None else dec
        return f"{sym}#,##0" + ("." + "0" * d if d else "")
    if low in ("percent", "percentage", "porcentaje"):
        d = 2 if dec is None else dec
        return "0" + ("." + "0" * d if d else "") + "%"
    if low in ("number", "numero", "número", "integer", "thousands") and dec is not None:
        return ("#,##0" if low != "integer" else "0") + ("." + "0" * dec if dec else "")
    if low in NAMED:
        return NAMED[low]
    return s


_TOKEN = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]|[yY]+|[dD]+|[hH]+|[sS]+|[mM]+|AM/PM|am/pm|A/P|[#0?,.%eE+\-]+|_.|\*.|.', re.S)


def _is_date_code(code: str) -> bool:
    c = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", code)
    return bool(re.search(r"[ydhs]", c, re.I)) or bool(re.search(r"m", c, re.I) and not re.search(r"0|#", c))


def is_date_format(code: str) -> bool:
    return _is_date_code(code or "")


def _num_section(x: float, sec: str, loc: str = "en") -> str:
    sec_clean = re.sub(r'"[^"]*"', lambda m: "\x00" + str(len(m.group(0))) + "\x00", sec)
    percent = "%" in sec_clean
    m = re.search(r"[#0?][#0?,]*(?:\.[0#?]*)?", sec_clean)
    if not m:
        return re.sub(r'"([^"]*)"', r"\1", sec).replace("\\", "")
    core = m.group(0)
    pre, post = sec[: sec.find(core)] if core in sec else "", sec[sec.find(core) + len(core):] if core in sec else ""
    dec = len(core.split(".")[1]) if "." in core else 0
    thousands = "," in core.split(".")[0]
    v = abs(x) * (100 if percent else 1)
    v = math.floor(v * 10 ** dec + 0.5) / 10 ** dec if dec < 15 else v
    s = f"{v:,.{dec}f}" if thousands else f"{v:.{dec}f}"
    int_part = core.split(".")[0].replace(",", "")
    min_int = int_part.count("0")
    head, _, tail = s.partition(".")
    if min_int == 0 and head == "0" and (dec > 0 or int_part):
        head = ""
    s = head + ("." + tail if dec else "")
    pre_t = re.sub(r'"([^"]*)"', r"\1", pre).replace("\\", "")
    post_t = re.sub(r'"([^"]*)"', r"\1", post).replace("\\", "")
    if loc == "es":
        s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")      # 1.234,56
        sym = pre_t.strip()
        if sym in _SYMBOLS and not post_t.strip():
            return s + NBSP + sym                                                 # 850,00 €
        if post_t.startswith("%"):
            post_t = NBSP + post_t                                                # 102,4 %
    return pre_t + s + post_t


def _date_text(n: float, code: str, loc: str = "en") -> str:
    dt = datetime_from_serial(n)
    if loc == "es":
        code = {NAMED["date"]: "dd/mm/yyyy", NAMED["datetime"]: "dd/mm/yyyy hh:mm"}.get(code, code)
    months, days = (MONTHS_ES, DAYS_ES) if loc == "es" else (MONTHS, DAYS)
    out = []
    ampm = bool(re.search(r"AM/PM|A/P", code, re.I))
    toks = _TOKEN.findall(code)
    for i, t in enumerate(toks):
        lo = t.lower()
        if t.startswith('"'):
            out.append(t[1:-1])
        elif t.startswith("\\"):
            out.append(t[1:])
        elif t.startswith("["):
            continue
        elif lo[:1] == "y":
            out.append(f"{dt.year:04d}" if len(t) > 2 else f"{dt.year % 100:02d}")
        elif lo[:1] == "d":
            wd = days[dt.weekday()]
            out.append({1: str(dt.day), 2: f"{dt.day:02d}", 3: wd[:3]}.get(len(t), wd))
        elif lo[:1] == "h":
            h = dt.hour % 12 or 12 if ampm else dt.hour
            out.append(f"{h:02d}" if len(t) > 1 else str(h))
        elif lo[:1] == "s":
            out.append(f"{dt.second:02d}" if len(t) > 1 else str(dt.second))
        elif lo[:1] == "m":
            prev = "".join(toks[:i]).lower()
            nxt = "".join(toks[i + 1:]).lower()
            minute = bool(re.search(r"h[^a-z]*$", prev)) or bool(re.match(r"[^a-z]*s", nxt))
            if minute:
                out.append(f"{dt.minute:02d}" if len(t) > 1 else str(dt.minute))
            else:
                mon = months[dt.month - 1]
                out.append({1: str(dt.month), 2: f"{dt.month:02d}", 3: mon[:3]}.get(len(t), mon))
        elif lo in ("am/pm", "a/p"):
            out.append(("AM" if dt.hour < 12 else "PM") if lo == "am/pm" else ("A" if dt.hour < 12 else "P"))
        else:
            out.append(t)
    return "".join(out)


def general(v: float) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if v != v or v in (math.inf, -math.inf):
        return "#NUM!"
    if float(v).is_integer() and abs(v) < 1e15:
        return str(int(v))
    s = f"{v:.15g}"
    if "e" in s:
        m, e = s.split("e")
        s = f"{m}E{int(e):+03d}"
    return s


def format_value(v: Any, code: str = "General", locale: Optional[str] = None) -> str:
    """What a cell shows for ``v`` under the Excel format ``code`` (separators and names of ``locale``, else the viewer's)."""
    loc = "es" if (locale or get_locale()) == "es" else "en"
    if v is None:
        return ""
    if hasattr(v, "code") and not isinstance(v, (int, float, str)):
        return v.code
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    code = code or "General"
    if isinstance(v, str):
        if code == "@" or code == "General":
            return v
        parts = code.split(";")
        if len(parts) >= 4:
            return parts[3].replace("@", v).replace('"', "")
        return v
    if code == "General" or code == "@":
        g = general(v)
        return g.replace(".", ",") if loc == "es" and not isinstance(v, int) else g
    parts = code.split(";")
    if _is_date_code(parts[0]):
        try:
            return _date_text(float(v), parts[0], loc)
        except (ValueError, OverflowError):
            return "#VALUE!"
    x = float(v)
    if x < 0 and len(parts) > 1:
        sec = parts[1]
        s = _num_section(x, sec, loc)
        return s
    if x == 0 and len(parts) > 2:
        return _num_section(0.0, parts[2], loc)
    s = _num_section(x, parts[0], loc)
    if x < 0 and s.strip("0.,% ") != "" and abs(x) * 10 ** 15 >= 1:
        neg = _num_section(x, parts[0], loc)
        # a negative with one section gets a leading minus unless rounding made it zero
        if re.search(r"[1-9]", neg):
            return "-" + s
    return s


_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "dmy"),
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$"), "dmy"),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$"), "dmy"),
)


def parse_date(text: str) -> Optional[int]:
    t = text.strip()
    for rx, order in _DATE_PATTERNS:
        m = rx.match(t)
        if m:
            a, b, c = (int(g) for g in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                return serial_from_date(_dt.date(y, mo, d))
            except ValueError:
                return None
    return None


_NUM = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_NUM_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def parse_input(text: str) -> Tuple[Any, Optional[str]]:
    """Typed text -> ``(value, format hint)``. Numbers, percentages, currency, ISO and d/m/y dates, TRUE/FALSE;
    anything else stays text. A leading apostrophe keeps the rest as text."""
    if text is None:
        return None, None
    s = str(text)
    if s.startswith("'"):
        return s[1:], "@"
    t = s.strip()
    if t == "":
        return (None, None) if s == "" else (s, None)
    low = t.lower()
    if low in ("true", "verdadero"):
        return True, None
    if low in ("false", "falso"):
        return False, None
    if get_locale() == "es":                # 1.234,56 · 12,5 % · 850,00 € typed in a Spanish interface; 3.14 still works
        m = re.match(r"^([+-]?)\s*([$€£¥]?)\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d+))?[\s\u00a0]*([%$€£¥]?)$", t)
        if m and (m.group(4) is not None or "." in m.group(3)):
            sign, lead, whole, dec, tail = m.groups()
            t = f"{sign}{lead}{whole.replace('.', '')}" + (f".{dec}" if dec else "") + tail
            low = t.lower()
    if _NUM.match(t):
        f = float(t)
        return (int(f) if f.is_integer() and "e" not in low and "." not in t and abs(f) < 1e15 else f), None
    if _NUM_THOUSANDS.match(t):
        return float(t.replace(",", "")), "#,##0.00" if "." in t else "#,##0"
    if t.endswith("%") and _NUM.match(t[:-1].strip()):
        dec = len(t[:-1].split(".")[1]) if "." in t[:-1] else 0
        return float(t[:-1]) / 100.0, "0" + ("." + "0" * dec if dec else "") + "%"
    m = re.match(r"^([$€£¥])\s*([+-]?[\d,]*\.?\d+)$", t) or re.match(r"^([+-]?[\d,]*\.?\d+)\s*([$€£¥])$", t)
    if m:
        a, b = m.groups()
        sym, num = (a, b) if a in "$€£¥" else (b, a)
        try:
            return float(num.replace(",", "")), f"{sym}#,##0.00"
        except ValueError:
            pass
    d = parse_date(t)
    if d is not None:
        return d, "yyyy-mm-dd"
    return s, None
