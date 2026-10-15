"""Look at the page: when the text of a web page is not where its information is, read a screenshot of it.

The text extractor (``fetch_webpage_content``) misses what a page only shows as a picture: a chart drawn on a
``<canvas>``, a table a script fills in after loading, a data page made of images, a scanned PDF. This module decides when
that is likely (:func:`assess_page`), captures the page with the browser Faustus already has in strips (the same
rendering and cutting as the visual index), shows the strips to the resident vision model together with the question of the
research, and hands back what it read (:func:`look_at_page`) -- marked as read from a capture, never as extracted text.

Honesty rules, all enforced here:

* No vision model available: nothing is captured and nothing is invented; the result says so, with the reason, so the report
  can say that the page's data is visual only and could not be read.
* Every strip is read separately and a strip with nothing relevant answers ``NONE`` and is dropped.
* The budget is bounded per page and per run (:class:`LookBudget`); when strips were left unread the result says how many.
* What the vision model returns derives from a web page, so callers treat it like page text (untrusted), not as an instruction.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .render import Renderers, render_source
from .tiling import Strip, strips_from_image
from .vision import ask as default_ask
from .vision import vision_ready as default_vision_ready

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------------ settings
DEFAULTS = {
    "research_look_at_pages": True,
    "research_look_min_chars": 600,
    "research_look_max_strips_per_page": 4,
    "research_look_max_strips_per_run": 16,
    "research_look_max_pages_per_run": 8,
    "research_look_max_tiles": 3,
    "research_look_timeout_seconds": 180,
    "web_fetch_look_at_page": True,
}


def setting(key: str) -> Any:
    try:
        from src.settings import get_setting

        return get_setting(key, DEFAULTS[key])
    except Exception:  # noqa: BLE001
        return DEFAULTS[key]


def _int(key: str, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(setting(key)), hi))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


# ------------------------------------------------------------------------------------------------ the decision
#: Names of the libraries pages use to draw tables and charts with a script (matched in the page source).
_SCRIPTED_DATA = re.compile(
    r"ag-grid|handsontable|datatables|dataTable|tabulator|react-table|tanstack|role=[\"']grid[\"']|highcharts|chart\.js|"
    r"chartjs|plotly|echarts|apexcharts|d3\.js|d3\.min|datawrapper|flourish|tableau|powerbi|power-bi|infogram|google\.visualization",
    re.I)
_TABLE_TAG = re.compile(r"<table\b", re.I)
_CANVAS_TAG = re.compile(r"<canvas\b", re.I)
_SVG_TAG = re.compile(r"<svg\b([^>]*)>", re.I)
_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.I)
_DIM_ATTR = re.compile(r"\b(width|height)\s*=\s*[\"']?(\d+)", re.I)
_CHARTY = re.compile(r"chart|graph|plot|diagram|figure|infographic", re.I)
_MD_TABLE_LINE = re.compile(r"^\s*\|.+\|\s*$", re.M)


@dataclass
class PageAssessment:
    reason: str = ""                       # "" | no_text | little_text | canvas | scripted_data | tables_not_in_text | image_data
    signals: Dict[str, Any] = field(default_factory=dict)

    @property
    def wants_look(self) -> bool:
        return bool(self.reason)

    @property
    def visual_signals(self) -> bool:
        """The markup itself shows tables, canvases, charts or big pictures (or a script builds them): a gap in the text is
        then probably data kept in a visual form, not just a short page."""
        s = self.signals
        return bool(s.get("tables") or s.get("canvases") or s.get("svgs") or s.get("images") or s.get("scripted")
                    or s.get("js_rendered"))

    def describe(self) -> str:
        return {
            "no_text": "the page has no extractable text",
            "little_text": "the extracted text is very short",
            "canvas": "the page draws its data on a canvas",
            "scripted_data": "the page builds its tables or charts with a script",
            "tables_not_in_text": "the page has tables that did not come out in the text",
            "image_data": "the page is mostly pictures with little text",
        }.get(self.reason, "")


def _big_declared(attrs: str, floor: int = 150) -> bool:
    dims = {k.lower(): int(v) for k, v in _DIM_ATTR.findall(attrs or "")}
    return bool(dims) and max(dims.values()) >= floor


def assess_page(text: str, html: str = "", *, min_chars: Optional[int] = None, js_rendered: bool = False) -> PageAssessment:
    """Whether the text of a fetched page is probably missing its data. Cheap: counts, no parsing."""
    floor = int(min_chars if min_chars is not None else _int("research_look_min_chars", 50, 20000))
    body = (text or "").strip()
    chars = len(body)
    md_table_lines = len(_MD_TABLE_LINE.findall(body))
    sig: Dict[str, Any] = {"text_chars": chars, "md_table_lines": md_table_lines, "min_chars": floor}
    if html:
        sig["tables"] = len(_TABLE_TAG.findall(html))
        sig["canvases"] = len(_CANVAS_TAG.findall(html))
        sig["svgs"] = sum(1 for m in _SVG_TAG.finditer(html) if _big_declared(m.group(1)) or _CHARTY.search(m.group(1)))
        sig["images"] = sum(1 for m in _IMG_TAG.finditer(html) if _big_declared(m.group(1), 200))
        sig["scripted"] = bool(_SCRIPTED_DATA.search(html))
    sig["js_rendered"] = bool(js_rendered)
    if chars == 0:
        return PageAssessment("no_text", sig)
    if chars < floor:
        return PageAssessment("little_text", sig)
    if sig.get("canvases", 0) >= 1 and chars < floor * 6:
        return PageAssessment("canvas", sig)
    if sig.get("tables", 0) >= 1 and md_table_lines == 0 and chars < floor * 20:
        return PageAssessment("tables_not_in_text", sig)
    if (sig.get("scripted") or js_rendered) and md_table_lines == 0 and chars < floor * 8:
        return PageAssessment("scripted_data", sig)
    if (sig.get("images", 0) + sig.get("svgs", 0)) >= 3 and chars < floor * 4:
        return PageAssessment("image_data", sig)
    return PageAssessment("", sig)


# ------------------------------------------------------------------------------------------------ budget
class LookBudget:
    """How much looking one research run may do. Counted before the work starts, so concurrent reads cannot overspend."""

    def __init__(self, max_pages: Optional[int] = None, max_strips: Optional[int] = None):
        self.pages_left = int(max_pages if max_pages is not None else _int("research_look_max_pages_per_run", 0, 100))
        self.strips_left = int(max_strips if max_strips is not None else _int("research_look_max_strips_per_run", 0, 400))
        self.pages_used = 0
        self.strips_used = 0

    def take_page(self, strips: int) -> int:
        """Reserve one page and up to ``strips`` strips; returns how many strips were granted (0 = refused)."""
        if self.pages_left <= 0 or self.strips_left <= 0 or strips <= 0:
            return 0
        granted = min(strips, self.strips_left)
        self.pages_left -= 1
        self.strips_left -= granted
        self.pages_used += 1
        return granted

    def give_back(self, strips: int) -> None:
        self.strips_left += max(0, strips)

    def spent(self, strips: int) -> None:
        self.strips_used += max(0, strips)


# ------------------------------------------------------------------------------------------------ the look
GENERIC_QUESTION = ("Transcribe the page's data: tables, chart values, figures and captions, with their headings.")

LOOK_PROMPT = (
    "This image is strip {i} of {n} of a screenshot of the web page {url}.\n"
    "Question being researched: {question}\n\n"
    "Transcribe what is visible in this strip that bears on the question: table rows and columns as a Markdown table, chart "
    "titles, axes, legends and the values printed on or beside the marks, figure captions and the text of any picture. Copy "
    "numbers and units exactly as shown. If a value is only implied by the height of a bar or the position of a line and is "
    "not printed, say it is approximate and how you read it. If something is hard to read, write \"unclear\" instead of "
    "guessing. Ignore menus, advertisements and cookie notices. If nothing in this strip bears on the question, answer "
    "exactly: NONE")

MAX_TEXT_CHARS = 8000


@dataclass
class LookResult:
    url: str
    status: str                             # read | nothing_relevant | no_vision | budget | capture_failed | disabled
    reason: str = ""                        # why a look was wanted (PageAssessment.reason)
    text: str = ""
    model: str = ""
    strips_read: int = 0
    strips_total: int = 0
    note: str = ""
    height: int = 0

    @property
    def has_text(self) -> bool:
        return self.status == "read" and bool(self.text.strip())

    def as_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "status": self.status, "reason": self.reason, "model": self.model,
                "strips_read": self.strips_read, "strips_total": self.strips_total, "note": self.note,
                "chars": len(self.text)}

    def marker(self) -> str:
        """The line that labels the text as read from a capture, never as extracted page text."""
        part = f"{self.strips_read} of {self.strips_total} strips" if self.strips_total else f"{self.strips_read} strips"
        return (f"[Read from a screenshot of the page by the vision model {self.model or '(unknown)'} ({part}); "
                f"this is not extracted text and may contain reading errors]")


_GAP_TEXT = {
    "en": {
        "no_vision": "The page {url} seems to hold its data in tables or charts the text extractor cannot read ({reason}), and no "
                     "vision model is available to read a screenshot of it ({note}).",
        "budget": "The page {url} seems to hold its data in tables or charts, but the limit of screenshots read per research was "
                  "reached before it.",
        "capture_failed": "The page {url} seems to hold its data visually, but it could not be captured ({note}).",
        "nothing_relevant": "A screenshot of {url} was read ({n} strips) and none of it bears on the question.",
        "heading": "Pages whose data is visual",
        "intro": "These pages seemed to keep their data in a visual form that the text extraction could not read:",
    },
    "es": {
        "no_vision": "La página {url} solo tiene el dato en una tabla o gráfico visual ({reason}) y no hay modelo de visión para "
                     "leer una captura de ella ({note}).",
        "budget": "La página {url} parece guardar su dato en tablas o gráficos, pero se agotó el límite de capturas leídas por "
                  "investigación antes de llegar a ella.",
        "capture_failed": "La página {url} parece guardar su dato en forma visual, pero no se pudo capturar ({note}).",
        "nothing_relevant": "Se leyó una captura de {url} ({n} franjas) y nada de ella responde a la pregunta.",
        "heading": "Páginas con el dato en forma visual",
        "intro": "Estas páginas parecían guardar su dato en una forma visual que la extracción de texto no pudo leer:",
    },
}
_REASON_ES = {
    "no_text": "la página no tiene texto extraíble", "little_text": "el texto extraído es muy corto",
    "canvas": "la página dibuja sus datos en un canvas", "scripted_data": "la página construye tablas o gráficos con un script",
    "tables_not_in_text": "hay tablas que no salieron en el texto", "image_data": "la página es sobre todo imágenes",
}


def gap_sentence(result: LookResult, url: str, lang: str = "en") -> str:
    """One honest sentence for the report when the page's data could not be read (``lang``: "es" or anything else = English)."""
    table = _GAP_TEXT["es" if lang == "es" else "en"]
    template = table.get(result.status)
    if not template or result.status in ("read", "disabled"):
        return ""
    reason_en = PageAssessment(result.reason).describe() or "visual content"
    reason = _REASON_ES.get(result.reason, "contenido visual") if lang == "es" else reason_en
    note = result.note or ("none configured" if lang != "es" else "ninguno configurado")
    return template.format(url=url, reason=reason, note=note, n=result.strips_read)


def gaps_section(sentences: List[str], lang: str = "en") -> str:
    """The closing section of a report that lists the pages whose data was visual and not read ("" when none)."""
    items = [s for s in sentences if s]
    if not items:
        return ""
    table = _GAP_TEXT["es" if lang == "es" else "en"]
    return f"\n\n## {table['heading']}\n\n{table['intro']}\n\n" + "\n".join(f"- {s}" for s in items) + "\n"


def pick_evenly(items: List[Any], n: int) -> List[Any]:
    """``n`` items spread over the list (the first and the last included when n >= 2); all of them if there are fewer."""
    if n >= len(items):
        return list(items)
    if n <= 1:
        return items[:1]
    idx = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in idx]


async def look_at_page(url: str, question: str, *, owner: Optional[str] = None, reason: str = "",
                       budget: Optional[LookBudget] = None, max_strips: Optional[int] = None, timeout_s: Optional[float] = None,
                       renderers: Optional[Renderers] = None, ask: Optional[Callable[..., Dict[str, Any]]] = None,
                       vision_check: Optional[Callable[[Optional[str]], Tuple[bool, str, str]]] = None,
                       cancel: Any = None) -> LookResult:
    """Capture ``url``, show its strips to the vision model with ``question`` and return what it read. Never raises."""
    result = LookResult(url=url, status="capture_failed", reason=reason)
    per_page = int(max_strips if max_strips is not None else _int("research_look_max_strips_per_page", 1, 40))
    ok, why, model = (vision_check or default_vision_ready)(owner)
    if not ok:
        result.status, result.note = "no_vision", why
        return result
    granted = budget.take_page(per_page) if budget is not None else per_page
    if granted <= 0:
        result.status, result.note = "budget", "no screenshot budget left"
        return result
    spent = 0
    try:
        limit = float(timeout_s if timeout_s is not None else _int("research_look_timeout_seconds", 20, 1800))
        text_parts: List[str] = []
        used_model = model
        used_model_ref = [model]

        async def work() -> None:
            nonlocal spent, used_model
            strips, height = await asyncio.to_thread(_capture_strips, url, owner, renderers, cancel)
            result.height = height
            result.strips_total = len(strips)
            if not strips:
                result.status, result.note = "capture_failed", "the capture had nothing to read (blank page)"
                return
            chosen = pick_evenly(strips, granted)
            asker = ask or default_ask
            q = (question or "").strip() or GENERIC_QUESTION
            for n, strip in enumerate(chosen, 1):
                prompt = LOOK_PROMPT.format(i=n, n=len(chosen), url=url, question=q[:1200])
                answer = await asyncio.to_thread(asker, strip.image, prompt, owner)
                spent += 1
                got_model = str((answer or {}).get("model") or "")
                answer_text = str((answer or {}).get("text") or "").strip()
                if not got_model:
                    result.status, result.note = "no_vision", answer_text[:240] or "the vision model did not answer"
                    return
                used_model = got_model
                used_model_ref[0] = got_model
                result.strips_read += 1
                if not answer_text or re.match(r"^\W*NONE\b", answer_text, re.I):
                    continue
                text_parts.append(f"[strip {n}/{len(chosen)}, y={strip.y}px]\n{answer_text}")
            if text_parts:
                result.status = "read"
                result.text = "\n\n".join(text_parts)[:MAX_TEXT_CHARS]
            else:
                result.status = "nothing_relevant"
            result.model = used_model
            if result.strips_total > len(chosen):
                result.note = f"{result.strips_total - len(chosen)} strips of the page were not read (limit per page)"

        await asyncio.wait_for(work(), timeout=limit)
    except asyncio.TimeoutError:
        if text_parts:
            result.status, result.text, result.model = "read", "\n\n".join(text_parts)[:MAX_TEXT_CHARS], used_model_ref[0]
        else:
            result.status = "capture_failed"
        result.note = "timed out reading the page" + (" (partial)" if text_parts else "")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - looking is a bonus, never a reason to fail the read
        logger.warning("look_at_page failed for %s: %s", url, exc, exc_info=True)
        result.status, result.note = "capture_failed", f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        if budget is not None:
            budget.spent(spent)
            if spent < granted:
                budget.give_back(granted - spent)
    return result


def _capture_strips(url: str, owner: Optional[str], renderers: Optional[Renderers], cancel: Any) -> Tuple[List[Strip], int]:
    tiles = _int("research_look_max_tiles", 1, 16)
    rendered = render_source(url, owner=owner, pages=None, max_pages=3, max_tiles=tiles, renderers=renderers)
    strips: List[Strip] = []
    height = 0
    try:
        for piece in rendered.pieces:
            height = max(height, piece.y_offset + piece.image.height)
            strips.extend(strips_from_image(piece.image, page=piece.page, y_offset=piece.y_offset))
    finally:
        close = getattr(rendered.pieces, "close", None)
        if close:
            close()
    return strips, height
