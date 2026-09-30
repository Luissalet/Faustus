"""Query-aware page pruning: read less, keep what answers the question.

Deep Research used to hand the whole extracted page (up to 15 000 characters)
to the local model for every source, and on a large local model that is the
dominant cost of a run: one page is 40-150 s of reading, most of it menus,
footers, cookie notices, related-link lists and comment threads. This module
cuts a page down BEFORE the model sees it, in two deterministic passes that
use no model and no extra dependency:

1. **Block scoring** (query independent). The page is split into blocks
   (paragraphs, headings, list items, table rows, code) and each block gets a
   score in [0, 1] from five signals:

   ============== ====== ====================================================
   signal         weight what it measures
   ============== ====== ====================================================
   text density    0.4   text over markup, and characters per tag, in the
                         block's container (prose is dense, menus are not)
   link density    0.2   1 - share of the block's text that is link text
   tag             0.2   article/main/p/h1-h3 good; nav/footer/aside/form bad
   class/id hint   0.1   content/article/post good; nav/menu/footer/sidebar/
                         comment/ad/cookie bad (nearest matching ancestor)
   length          0.1   very short blocks are rarely a fact
   ============== ====== ====================================================

   A block scoring at least ``threshold`` (0.48) survives. A block whose
   nearest context is explicitly boilerplate (a ``<nav>``, ``<footer>``, a
   ``cookie`` banner ...) is additionally clamped below the threshold, so a
   long cookie notice cannot pass on text density alone.

2. **BM25 ranking** (query dependent). Surviving paragraphs are ranked
   against the current sub-question with Okapi BM25 (k1 = 1.5, b = 0.75). A
   term that also appears in the paragraph's section headings counts extra
   (h1 x5, h2 x3, h3 x2). The best blocks are kept in document order up to a
   character cap; the page title and the best-matching block are always kept.

The same two passes run on already extracted Markdown/plain text when the raw
markup is not available (cache hit, scraped text); there the "tag" signal
comes from the Markdown syntax and the "hint" from a short boilerplate
phrase list.

Every outcome carries the numbers a run trace needs: original characters,
pruned characters, blocks kept and the top BM25 score. The function never
raises on odd input and never returns less than it should silently: when
pruning would leave almost nothing it says so (``fallback``) and returns the
original text, capped.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Tunables (every one can be overridden per call through ``PruneConfig``)
# ---------------------------------------------------------------------------

BLOCK_THRESHOLD = 0.48
WEIGHT_TEXT_DENSITY = 0.4
WEIGHT_LINK_DENSITY = 0.2
WEIGHT_TAG = 0.2
WEIGHT_HINT = 0.1
WEIGHT_LENGTH = 0.1
#: Ceiling for a block whose nearest context is boilerplate. Below the default
#: threshold by design, so such a block never survives at the default.
NEGATIVE_CAP = 0.30
DEFAULT_MAX_CHARS = 6000
BM25_K1 = 1.5
BM25_B = 0.75
#: Multipliers for a query term found in the section heading of a paragraph.
HEADING_WEIGHTS: Dict[int, float] = {1: 5.0, 2: 3.0, 3: 2.0}
#: Below this many characters the threshold pass is judged to have removed too
#: much and a more permissive pass (then the original text) takes over.
MIN_KEEP_CHARS = 300
#: Longest block kept whole; longer ones are split on sentence boundaries so a
#: single giant ``<div>`` of text can be ranked by parts.
MAX_BLOCK_CHARS = 900
TITLE_MAX_CHARS = 200


@dataclass(frozen=True)
class PruneConfig:
    threshold: float = BLOCK_THRESHOLD
    max_chars: int = DEFAULT_MAX_CHARS
    w_density: float = WEIGHT_TEXT_DENSITY
    w_link: float = WEIGHT_LINK_DENSITY
    w_tag: float = WEIGHT_TAG
    w_hint: float = WEIGHT_HINT
    w_length: float = WEIGHT_LENGTH
    negative_cap: float = NEGATIVE_CAP
    k1: float = BM25_K1
    b: float = BM25_B
    heading_weights: Tuple[Tuple[int, float], ...] = tuple(sorted(HEADING_WEIGHTS.items()))
    min_keep_chars: int = MIN_KEEP_CHARS
    max_block_chars: int = MAX_BLOCK_CHARS

    @classmethod
    def from_settings(cls, settings: Optional[Dict[str, Any]] = None, **overrides: Any) -> "PruneConfig":
        """Build a config from the ``research_prune_*`` settings (bad values
        fall back to the defaults instead of raising)."""
        s = settings or {}

        def num(key: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(s.get(key, default))
            except (TypeError, ValueError):
                return default
            return min(hi, max(lo, v))

        cfg = {
            "threshold": num("research_prune_threshold", BLOCK_THRESHOLD, 0.0, 1.0),
            "max_chars": int(num("research_prune_max_chars", DEFAULT_MAX_CHARS, 500, 60000)),
        }
        cfg.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**cfg)


# ---------------------------------------------------------------------------
# Tokenisation (Spanish + English)
# ---------------------------------------------------------------------------

_STOP_EN = """
a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i
if in into is it its itself just me more most my no nor not now of off on once only or other our out over own same
she should so some such than that the their them then there these they this those through to too under until up
very was we were what when where which while who whom why will with would you your yours
""".split()

_STOP_ES = """
a al algo algunas algunos ante antes como con contra cual cuando de del desde donde durante e el ella ellas ellos en
entre era eran es esa esas ese eso esos esta estaba estan este esto estos fue fueron ha han hasta hay la las le les lo
los mas me mi mis mucho muy ni no nos nosotros nuestro o otra otras otro otros para pero por porque que quien se sea
segun ser si sin sobre son su sus tambien te tiene tienen todo todos tu tus un una unas uno unos y ya yo
""".split()


def fold(text: str) -> str:
    """Lowercase and strip accents (``Canción`` -> ``cancion``, ``ñ`` -> ``n``)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


STOPWORDS = frozenset(fold(w) for w in (_STOP_EN + _STOP_ES))
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _stem(tok: str) -> str:
    # Plural -s only: cheap, identical on both languages and never produces
    # an empty or one-letter stem.
    if len(tok) > 4 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def tokenize(text: str, *, drop_stopwords: bool = True) -> List[str]:
    """Terms of ``text``: accents folded, lowercase, stopwords (Spanish and
    English) and one-letter words dropped, plural ``s`` removed."""
    out: List[str] = []
    for tok in _TOKEN_RE.findall(fold(text)):
        if drop_stopwords and tok in STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        out.append(_stem(tok))
    return out


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

class BM25:
    """Okapi BM25 over a small in-memory collection.

    ``docs`` are token lists. ``boosts`` optionally adds weighted extra term
    frequency per document (used for heading terms: a term in a section
    heading counts ``weight`` times). Length normalisation uses the body
    length only, so a long heading chain does not shorten a paragraph's
    relative length.
    """

    def __init__(self, docs: Sequence[Sequence[str]], *, boosts: Optional[Sequence[Dict[str, float]]] = None,
                 k1: float = BM25_K1, b: float = BM25_B):
        self.k1 = k1
        self.b = b
        self.n = len(docs)
        self._tf: List[Counter] = [Counter(d) for d in docs]
        self._boost: List[Dict[str, float]] = [dict(x) for x in boosts] if boosts else [{} for _ in docs]
        self._len = [len(d) for d in docs]
        self.avgdl = (sum(self._len) / self.n) if self.n else 0.0
        df: Counter = Counter()
        for tf, bo in zip(self._tf, self._boost):
            for term in set(tf) | set(bo):
                df[term] += 1
        self._df = df

    def idf(self, term: str) -> float:
        n_t = self._df.get(term, 0)
        # The "+1" form stays positive for terms present in most documents.
        return math.log(1.0 + (self.n - n_t + 0.5) / (n_t + 0.5))

    def score(self, index: int, terms: Iterable[str]) -> float:
        if not (0 <= index < self.n):
            return 0.0
        tf = self._tf[index]
        bo = self._boost[index]
        dl = self._len[index]
        norm = 1.0 - self.b + self.b * (dl / self.avgdl if self.avgdl else 1.0)
        total = 0.0
        for term in terms:
            f = tf.get(term, 0) + bo.get(term, 0.0)
            if f <= 0:
                continue
            total += self.idf(term) * (f * (self.k1 + 1.0)) / (f + self.k1 * norm)
        return total


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

@dataclass
class Block:
    index: int
    text: str
    tag: str = "p"
    level: int = 0                # 1-6 for headings, 0 otherwise
    text_density: float = 0.0
    link_density: float = 0.0
    tag_score: float = 0.0
    hint_score: float = 0.5
    length_score: float = 0.0
    negative: bool = False
    score: float = 0.0
    passed: bool = False
    bm25: float = 0.0
    selected: bool = False
    headings: Tuple[Tuple[int, int], ...] = ()   # (level, block index) of the section headings above

    @property
    def is_heading(self) -> bool:
        return self.level > 0

    def brief(self, limit: int = 120) -> Dict[str, Any]:
        t = self.text if len(self.text) <= limit else self.text[: limit - 1].rstrip() + "…"
        return {
            "index": self.index, "tag": self.tag, "score": round(self.score, 3),
            "bm25": round(self.bm25, 3), "passed": self.passed, "selected": self.selected,
            "negative_context": self.negative, "chars": len(self.text), "text": t,
        }


def _clamp(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_TAG_BASE: Dict[str, float] = {
    "h1": 1.0, "h2": 1.0, "h3": 1.0, "p": 1.0, "article": 1.0, "main": 1.0,
    "blockquote": 0.8, "pre": 0.8, "dd": 0.7, "dt": 0.7, "h4": 0.7, "h5": 0.7, "h6": 0.7,
    "li": 0.6, "figcaption": 0.6, "caption": 0.6, "summary": 0.6,
    "tr": 0.5, "td": 0.5, "th": 0.5, "div": 0.5, "section": 0.5, "span": 0.5, "text": 0.5,
}
_NEG_CTX_TAGS = frozenset({"nav", "footer", "aside", "menu", "dialog"})
_NEG_CTX_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alert"})
_POS_CTX_TAGS = frozenset({"article", "main"})

_STRONG_NEG = frozenset({
    "nav", "navbar", "navigation", "menu", "footer", "comment", "comments", "respond", "ad", "ads", "advert",
    "advertisement", "adsense", "sponsored", "cookie", "cookies", "consent", "gdpr", "breadcrumb", "breadcrumbs",
    "skip", "sr", "screenreader", "hidden",
})
_WEAK_NEG = frozenset({
    "sidebar", "side", "banner", "popup", "modal", "promo", "share", "sharing", "social", "related", "newsletter",
    "subscribe", "subscription", "widget", "toolbar", "pagination", "pager", "tags", "tagcloud", "author",
    "byline", "meta", "signup", "login",
})
_POS_HINT = frozenset({
    "content", "article", "post", "entry", "story", "text", "main", "prose", "markdown", "body", "blog",
    "documentation", "docs",
})

_INLINE_TAGS = frozenset({
    "a", "span", "b", "i", "em", "strong", "code", "small", "sup", "sub", "br", "u", "mark", "abbr", "time", "cite",
    "q", "label", "s", "del", "ins", "kbd", "var", "samp", "bdi", "bdo", "data", "wbr", "font", "big", "tt",
    "img", "picture", "source",
})
_LEAF_BLOCK_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "dd", "dt", "figcaption", "caption",
    "summary", "tr",
})
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "iframe", "canvas", "head", "meta", "link", "object",
    "embed", "select", "option", "button", "input", "textarea", "video", "audio", "map", "math",
})


def _hint_of(el: Any) -> Optional[str]:
    """``'pos'``, ``'neg'``, ``'neg!'`` (strong boilerplate token) or None
    for one element's class/id tokens."""
    try:
        classes = el.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        raw = " ".join(list(classes) + [str(el.get("id") or "")])
    except Exception:  # noqa: BLE001
        return None
    if not raw.strip():
        return None
    tokens = {t for t in re.split(r"[^a-z0-9]+", fold(raw)) if t}
    if not tokens:
        return None
    strong = bool(tokens & _STRONG_NEG)
    weak = bool(tokens & _WEAK_NEG)
    pos = bool(tokens & _POS_HINT)
    if strong:
        return "neg!"
    if weak and not pos:
        return "neg"
    if pos:
        return "pos"
    return None


_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def _is_hidden(el: Any) -> bool:
    try:
        if el.has_attr("hidden") or str(el.get("aria-hidden") or "").lower() == "true":
            return True
        style = el.get("style")
        return bool(style and _HIDDEN_STYLE.search(str(style)))
    except Exception:  # noqa: BLE001
        return False


def _context_of(el: Any, *, depth_limit: int = 8) -> Tuple[str, str]:
    """(tag_context, hint) of the nearest decisive ancestor-or-self.

    tag_context is ``'pos'`` (article/main), ``'neg'`` (nav/footer/aside...),
    ``'header'`` or ``''``. ``hint`` is ``'pos'``/``'neg'``/``''`` from
    class/id tokens. Each is decided by the NEAREST element that has an
    opinion, so an ``entry-content`` inside a ``has-sidebar`` wrapper reads
    as content, and a ``cookie-banner`` inside a content wrapper reads as a
    banner. The exception is a strong boilerplate token (``nav``, ``footer``,
    ``comment``, ``cookie``, ``ad`` ...) on ANY ancestor: a ``modal-body`` or
    ``comment-content`` inside it is still boilerplate.
    """
    tag_ctx = ""
    hint = ""
    strong_neg = False
    node = el
    steps = 0
    while node is not None and steps < depth_limit + 4:
        name = getattr(node, "name", None)
        if not name or name in ("[document]", "html", "body"):
            break
        if not tag_ctx:
            role = str(node.get("role") or "").lower() if hasattr(node, "get") else ""
            if name in _NEG_CTX_TAGS or role in _NEG_CTX_ROLES:
                tag_ctx = "neg"
            elif name in _POS_CTX_TAGS or role == "main":
                tag_ctx = "pos"
            elif name == "header":
                tag_ctx = "header"
            elif name == "form" and _form_is_widget(node):
                tag_ctx = "neg"
        h = _hint_of(node)
        if h == "neg!":
            strong_neg = True
        elif h and not hint:
            hint = h
        node = getattr(node, "parent", None)
        steps += 1
    if strong_neg:
        hint = "neg"
    return tag_ctx, hint


def _form_is_widget(form: Any) -> bool:
    """A ``<form>`` is boilerplate (search, login, newsletter) unless it is
    one of those page-wide wrappers that hold the whole article."""
    try:
        text_len = len(_collapse(form.get_text(" ")))
    except Exception:  # noqa: BLE001
        return False
    return text_len < 1500


def _sentence_split(text: str, limit: int) -> List[str]:
    """Cut an overlong block on sentence boundaries into parts <= ``limit``."""
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    parts: List[str] = []
    cur = ""
    for sent in sentences:
        while len(sent) > limit:               # no punctuation at all: hard cut on a space
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if not sent:
            continue
        if cur and len(cur) + 1 + len(sent) > limit:
            parts.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# HTML -> blocks
# ---------------------------------------------------------------------------

def _html_blocks(html: str, cfg: PruneConfig) -> Tuple[str, List[Block], int]:
    """(title, blocks, visible_chars) from raw HTML. Blocks are scored."""
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html or "", "html.parser")
    title_tag = soup.find("title")
    title = _collapse(title_tag.get_text(" ")) if title_tag else ""
    root = soup.find("body") or soup
    density_cache: Dict[int, float] = {}
    raw_blocks: List[Tuple[Block, Any, int, int]] = []   # (block, context element, link chars, text chars)

    def density_of(container: Any) -> float:
        """Half text-over-markup ratio, half characters per tag: a menu has
        both few characters per tag and mostly markup, prose neither."""
        key = id(container)
        if key not in density_cache:
            try:
                text_len = len(_collapse(container.get_text(" ")))
                html_len = len(_collapse(str(container))) or 1
                tags = len(container.find_all(True)) + 1
                ratio = _clamp((min(1.0, text_len / html_len) - 0.10) / 0.50)
                per_tag = _clamp(text_len / tags / 50.0)
                density_cache[key] = 0.5 * ratio + 0.5 * per_tag
            except Exception:  # noqa: BLE001
                density_cache[key] = 0.0
        return density_cache[key]

    def link_chars(nodes: Sequence[Any]) -> int:
        total = 0
        for n in nodes:
            if isinstance(n, Tag):
                anchors = [n] if n.name == "a" else []
                anchors += n.find_all("a")
                for a in anchors:
                    total += len(_collapse(a.get_text(" ")))
        return total

    def emit(nodes: Sequence[Any], ctx_el: Any, tag: str) -> None:
        parts = []
        for n in nodes:
            parts.append(str(n) if isinstance(n, NavigableString) else n.get_text(" "))
        if tag == "tr":
            cells = [_collapse(c.get_text(" ")) for c in nodes[0].find_all(["td", "th"])] if nodes else []
            text = " | ".join(c for c in cells if c)
        elif tag == "pre":
            text = "\n".join(line.rstrip() for line in "".join(parts).strip("\n").splitlines())
        else:
            text = _collapse(" ".join(parts))
        if not text or not re.search(r"\w", text):
            return
        level = int(tag[1]) if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit() else 0
        chunks = [text] if (level or tag == "pre") else _sentence_split(text, cfg.max_block_chars)
        lchars = link_chars(nodes)
        for chunk in chunks:
            scale = len(chunk) / max(1, len(text))
            raw_blocks.append((Block(index=-1, text=chunk, tag=tag, level=level), ctx_el,
                               int(lchars * scale), len(chunk)))

    def has_block_child(el: Any) -> bool:
        for c in el.children:
            if isinstance(c, Tag) and c.name not in _INLINE_TAGS and c.name not in _SKIP_TAGS:
                return True
        return False

    def walk(el: Any, depth: int) -> None:
        run: List[Any] = []

        def flush() -> None:
            if run:
                emit(list(run), el, "div" if el.name not in _TAG_BASE else el.name)
                run.clear()

        for child in el.children:
            if isinstance(child, NavigableString):
                if type(child).__name__ in ("Comment", "Doctype", "CData", "ProcessingInstruction", "Declaration"):
                    continue
                run.append(child)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in _SKIP_TAGS or _is_hidden(child):
                continue
            if name in _INLINE_TAGS:
                run.append(child)
                continue
            flush()
            if name == "table":
                for tr in child.find_all("tr"):
                    emit([tr], tr, "tr")
                continue
            if name in _LEAF_BLOCK_TAGS:
                if name in ("li", "blockquote", "dd", "summary") and has_block_child(child) and depth < 150:
                    walk(child, depth + 1)
                else:
                    emit([child], child, name)
                continue
            if depth >= 150 or not has_block_child(child):
                emit([c for c in child.children if not (isinstance(c, Tag) and c.name in _SKIP_TAGS)],
                     child, name)
            else:
                walk(child, depth + 1)
        flush()

    walk(root, 0)

    visible = len(_collapse(root.get_text(" "))) if hasattr(root, "get_text") else 0
    blocks: List[Block] = []
    for i, (blk, ctx_el, lchars, tchars) in enumerate(raw_blocks):
        blk.index = i
        container = getattr(ctx_el, "parent", None) if ctx_el.name in _LEAF_BLOCK_TAGS else ctx_el
        blk.text_density = density_of(container or ctx_el)
        blk.link_density = min(1.0, lchars / tchars) if tchars else 0.0
        tag_ctx, hint = _context_of(ctx_el)
        base = _TAG_BASE.get(blk.tag, 0.5)
        factor = 0.0 if tag_ctx == "neg" else 0.4 if tag_ctx == "header" else 1.0
        blk.tag_score = base * factor
        blk.hint_score = 1.0 if hint == "pos" else 0.0 if hint == "neg" else 0.5
        blk.negative = tag_ctx == "neg" or hint == "neg"
        blocks.append(blk)
    _score_blocks(blocks, cfg)
    return title, blocks, visible


# ---------------------------------------------------------------------------
# Text / Markdown -> blocks
# ---------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*#*\s*$")
_MD_LIST = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_BOILERPLATE = re.compile(
    r"(?:cookies?\b.{0,60}\b(?:accept|consent|policy|preferences|use|usamos|utilizamos|aceptar)|"
    r"\b(?:accept|aceptar|rechazar|reject)\s+(?:all|todas?|cookies)\b|"
    r"\bskip to (?:main )?content\b|\bsaltar al contenido\b|"
    r"\ball rights reserved\b|\btodos los derechos reservados\b|"
    r"\b(?:subscribe|suscr[ií]bete|suscribirse)\b.{0,40}\b(?:newsletter|bolet[ií]n)\b|"
    r"\bsign up for (?:our|the) newsletter\b|"
    r"\bprivacy policy\b.{0,40}\bterms\b|\bpol[ií]tica de privacidad\b.{0,40}\b(?:t[eé]rminos|cookies|aviso)\b|"
    r"\bshare (?:on|this)\b.{0,30}\b(?:facebook|twitter|linkedin|whatsapp)\b|"
    r"^\s*(?:menu|men[uú]|home|inicio|search|buscar)\s*$)",
    re.I | re.S,
)


def _text_blocks(text: str, cfg: PruneConfig) -> List[Block]:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw: List[Tuple[str, str, int]] = []   # (tag, text, level)
    para: List[str] = []
    in_fence = False
    fence: List[str] = []

    def flush_para() -> None:
        if para:
            raw.append(("p", _collapse(" ".join(para)), 0))
            para.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                raw.append(("pre", "\n".join(fence), 0))
                fence.clear()
                in_fence = False
            else:
                flush_para()
                in_fence = True
            continue
        if in_fence:
            fence.append(line.rstrip())
            continue
        if not stripped:
            flush_para()
            continue
        m = _MD_HEADING.match(stripped)
        if m:
            flush_para()
            raw.append((f"h{len(m.group(1))}", m.group(2).strip(), len(m.group(1))))
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            flush_para()
            if re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            raw.append(("tr", " | ".join(c for c in cells if c), 0))
            continue
        if _MD_LIST.match(line):
            flush_para()
            raw.append(("li", _collapse(_MD_LIST.sub("", line, count=1)), 0))
            continue
        para.append(stripped)
    if in_fence and fence:
        raw.append(("pre", "\n".join(fence), 0))
    flush_para()

    blocks: List[Block] = []
    for tag, body, level in raw:
        if not body or not re.search(r"\w", body):
            continue
        parts = [body] if (level or tag == "pre") else _sentence_split(body, cfg.max_block_chars)
        for part in parts:
            blk = Block(index=len(blocks), text=part, tag=tag, level=level)
            n = max(1, len(part))
            link_text = sum(len(m.group(1)) for m in _MD_LINK.finditer(part))
            visible = _MD_LINK.sub(lambda m: m.group(1), part)
            bare = sum(len(u.group(0)) for u in _BARE_URL.finditer(visible))
            alnum = sum(1 for ch in part if ch.isalnum() or ch.isspace())
            blk.text_density = _clamp((alnum / n - 0.55) / 0.35)
            blk.link_density = min(1.0, (link_text + bare) / max(1, len(visible)))
            blk.tag_score = _TAG_BASE.get(tag, 0.5)
            boiler = bool(_BOILERPLATE.search(part)) and len(part) < 400
            blk.hint_score = 0.0 if boiler else 0.5
            blk.negative = boiler
            blocks.append(blk)
    _score_blocks(blocks, cfg)
    return blocks


# ---------------------------------------------------------------------------
# Scoring, headings, BM25 selection
# ---------------------------------------------------------------------------

def _score_blocks(blocks: List[Block], cfg: PruneConfig) -> None:
    for blk in blocks:
        length = 1.0 if blk.is_heading else _clamp(len(blk.text) / 200.0)
        blk.length_score = length
        score = (cfg.w_density * blk.text_density
                 + cfg.w_link * (1.0 - blk.link_density)
                 + cfg.w_tag * blk.tag_score
                 + cfg.w_hint * blk.hint_score
                 + cfg.w_length * length)
        if blk.negative:
            score = min(score, cfg.negative_cap)
        blk.score = _clamp(score)
        blk.passed = blk.score >= cfg.threshold


def _attach_headings(blocks: List[Block], *, only_passed: bool = True) -> None:
    """Give every block the (level, index) of the h1/h2/h3 above it."""
    chain: Dict[int, int] = {}
    for blk in blocks:
        if blk.is_heading:
            if blk.passed or not only_passed:
                chain = {lvl: idx for lvl, idx in chain.items() if lvl < blk.level}
                chain[blk.level] = blk.index
            blk.headings = ()
        else:
            blk.headings = tuple(sorted(chain.items()))


def _truncate_at_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if m > limit * 0.6:
        return cut[: m + 1].rstrip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip() + "…"


@dataclass
class PruneResult:
    text: str
    title: str = ""
    original_chars: int = 0
    pruned_chars: int = 0
    blocks_total: int = 0
    blocks_passed: int = 0
    blocks_kept: int = 0
    top_bm25: float = 0.0
    mode: str = "html"            # html | text
    fallback: str = ""            # "" | relaxed | unpruned | no_query_match | empty
    query_terms: List[str] = field(default_factory=list)
    blocks: List[Block] = field(default_factory=list)

    def trace(self) -> Dict[str, Any]:
        """The per-page row a research trace records."""
        return {
            "original_chars": self.original_chars,
            "pruned_chars": self.pruned_chars,
            "blocks_total": self.blocks_total,
            "blocks_passed": self.blocks_passed,
            "blocks_kept": self.blocks_kept,
            "top_bm25": round(self.top_bm25, 3),
            "mode": self.mode,
            "fallback": self.fallback,
        }

    def to_dict(self, *, include_blocks: bool = False, max_blocks: int = 60) -> Dict[str, Any]:
        d = {"text": self.text, "title": self.title, "query_terms": self.query_terms, **self.trace()}
        if include_blocks:
            d["blocks"] = [b.brief() for b in self.blocks[:max_blocks]]
        return d


def _render(title: str, selected: List[Block], all_blocks: List[Block]) -> str:
    by_index = {b.index: b for b in all_blocks}
    out: List[str] = []
    if title:
        out.append(f"# {title}")
    norm_title = fold(_collapse(title))
    emitted_headings: set = set()
    for blk in sorted(selected, key=lambda b: b.index):
        if not blk.is_heading:
            for level, idx in blk.headings:
                if idx in emitted_headings or idx not in by_index:
                    continue
                h = by_index[idx]
                emitted_headings.add(idx)
                if fold(_collapse(h.text)) == norm_title:
                    continue
                out.append(f"{'#' * max(2, level)} {h.text}")
        if blk.tag == "li":
            out.append(f"- {blk.text}")
        elif blk.tag == "pre":
            out.append(f"```\n{blk.text}\n```")
        else:
            out.append(blk.text)
    return "\n\n".join(out)


def _select(blocks: List[Block], query: str, title: str, cfg: PruneConfig, candidates: List[Block]) -> Tuple[str, List[Block], float, str]:
    """Rank ``candidates`` with BM25 and pick blocks under the char cap.

    Returns (text, selected blocks, top score, fallback note)."""
    _attach_headings(blocks)
    docs = [b for b in candidates if not b.is_heading]
    terms = list(dict.fromkeys(tokenize(query))) or list(dict.fromkeys(tokenize(query, drop_stopwords=False)))
    weights = dict(cfg.heading_weights)
    by_index = {b.index: b for b in blocks}
    body_tokens = [tokenize(b.text) for b in docs]
    boosts: List[Dict[str, float]] = []
    for b in docs:
        boost: Dict[str, float] = {}
        for level, idx in b.headings:
            w = weights.get(level)
            if not w:
                continue
            for tok in tokenize(by_index[idx].text):
                boost[tok] = boost.get(tok, 0.0) + w
        boosts.append(boost)
    bm = BM25(body_tokens, boosts=boosts, k1=cfg.k1, b=cfg.b)
    for i, b in enumerate(docs):
        b.bm25 = bm.score(i, terms) if terms else 0.0
    # Headings are context, not documents; their own score stays 0.

    clean_title = _truncate_at_sentence(_collapse(title), TITLE_MAX_CHARS) if title else ""
    budget = max(200, cfg.max_chars - (len(clean_title) + 4 if clean_title else 0))
    top = max((b.bm25 for b in docs), default=0.0)
    note = ""

    def cost(b: Block, have: set) -> int:
        c = len(b.text) + 2 + (2 if b.tag == "li" else 8 if b.tag == "pre" else 0)
        for _level, idx in b.headings:
            if idx not in have and idx in by_index:
                c += len(by_index[idx].text) + 6
        return c

    selected: List[Block] = []
    have_headings: set = set()

    def take(b: Block) -> None:
        selected.append(b)
        b.selected = True
        for _level, idx in b.headings:
            have_headings.add(idx)

    total_cost = 0
    simulated: set = set()
    for b in docs:                       # what rendering every passing block would cost, headings included
        total_cost += cost(b, simulated)
        simulated.update(idx for _level, idx in b.headings)
    if not docs:
        return _render(clean_title, [], blocks), [], 0.0, "empty"
    if total_cost <= budget:
        for b in docs:
            take(b)            # everything that passed already fits under the cap
    elif top <= 0.0:
        note = "no_query_match"
        used = 0
        for b in docs:         # leading blocks: the most likely summary of the page
            c = cost(b, have_headings)
            if used + c > budget:
                if not selected:
                    b.text = _truncate_at_sentence(b.text, budget)
                    take(b)
                break
            take(b)
            used += c
    else:
        ranked = sorted((b for b in docs if b.bm25 > 0.0), key=lambda b: (-b.bm25, b.index))
        used = 0
        for b in ranked:
            c = cost(b, have_headings)
            if used + c > budget:
                if not selected:               # the best block is always kept, cut to fit
                    b.text = _truncate_at_sentence(b.text, max(100, budget - 2))
                    take(b)
                    used = budget
                continue
            take(b)
            used += c
    text = _render(clean_title, selected, blocks)
    return text, selected, top, note


def prune_page(query: str, *, html: Optional[str] = None, text: Optional[str] = None, title: str = "",
               original_chars: Optional[int] = None, config: Optional[PruneConfig] = None) -> PruneResult:
    """Prune one page against ``query``.

    Give ``html`` (raw markup, preferred: the DOM carries the signals) and/or
    ``text`` (already extracted Markdown/plain text). ``original_chars`` is
    what the caller would otherwise have read (defaults to the length of the
    text, or of the visible text of the markup).
    """
    cfg = config or PruneConfig()
    query = query or ""
    mode = "html"
    blocks: List[Block] = []
    page_title = _collapse(title)
    visible = 0
    if html:
        try:
            t, blocks, visible = _html_blocks(html, cfg)
            page_title = page_title or t
        except Exception:  # noqa: BLE001 - an odd page must never break a run
            blocks = []
    if not blocks and text:
        mode = "text"
        blocks = _text_blocks(text, cfg)
        visible = len(text)
    if mode == "html" and text and blocks and sum(len(b.text) for b in blocks) < 0.25 * len(text):
        # The DOM walk found a fraction of what the extractor read (script
        # rendered page, odd markup): trust the extracted text instead.
        mode = "text"
        blocks = _text_blocks(text, cfg)
        visible = len(text)
    original = original_chars if original_chars is not None else (len(text) if text else visible)

    def result(body: str, sel: List[Block], top: float, fallback: str) -> PruneResult:
        return PruneResult(
            text=body, title=page_title, original_chars=original, pruned_chars=len(body),
            blocks_total=len(blocks), blocks_passed=sum(1 for b in blocks if b.passed),
            blocks_kept=len(sel), top_bm25=top, mode=mode, fallback=fallback,
            query_terms=list(dict.fromkeys(tokenize(query))), blocks=blocks,
        )

    if not blocks:
        body = _truncate_at_sentence(text or "", cfg.max_chars) if text else ""
        return result(body, [], 0.0, "empty" if not body else "unpruned")

    passed = [b for b in blocks if b.passed]
    kept_chars = sum(len(b.text) for b in passed if not b.is_heading)
    fallback = ""
    if kept_chars < cfg.min_keep_chars and visible > kept_chars:
        # The threshold removed nearly everything; a thin page, or markup that
        # defeats the signals. Retry with every block outside explicit
        # boilerplate before giving up on pruning.
        relaxed = [b for b in blocks if not b.negative]
        relaxed_chars = sum(len(b.text) for b in relaxed if not b.is_heading)
        if relaxed_chars >= cfg.min_keep_chars:
            for b in relaxed:
                b.passed = True
            passed = relaxed
            fallback = "relaxed"
            kept_chars = relaxed_chars
    if kept_chars < cfg.min_keep_chars and text and len(text) > kept_chars:
        body = _truncate_at_sentence(text, cfg.max_chars)
        return result(body, [], 0.0, "unpruned")

    body, selected, top, note = _select(blocks, query, page_title, cfg, passed)
    return result(body, selected, top, note or fallback)
