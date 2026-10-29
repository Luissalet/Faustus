"""Turn a UI source file into the HTML text the static design rules read.

The design rules of the design workshop read an HTML document and the CSS it
carries. A component file is not one, so each kind is reduced to the closest
honest document: the markup and styles a browser would see, with nothing
invented. Whatever cannot be read (a component, an expression, a prop) is
dropped instead of guessed, so a rule only fires on what is really written.
"""
from __future__ import annotations

import re
from typing import Optional

DESIGN_EXTS = (".tsx", ".jsx", ".css", ".html", ".htm", ".vue")
MAX_BYTES = 400_000

_WRAP = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>source</title>'
    "{head}</head><body>{body}</body></html>"
)

_UNITLESS = frozenset({
    "opacity", "z-index", "line-height", "flex", "flex-grow", "flex-shrink", "font-weight", "order",
    "zoom", "orphans", "widows", "scale", "aspect-ratio", "column-count", "tab-size",
})


def is_design_file(path: str) -> bool:
    return str(path or "").lower().endswith(DESIGN_EXTS)


def to_html(path: str, text: str) -> Optional[str]:
    """The HTML document for `path`, or None when the extension is not a UI source."""
    low = str(path or "").lower()
    if not text or len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        return None
    if low.endswith((".html", ".htm")):
        return text
    if low.endswith(".css"):
        return _WRAP.format(head=f"<style>\n{text}\n</style>", body="")
    if low.endswith(".vue"):
        return _from_vue(text)
    if low.endswith((".tsx", ".jsx")):
        return _from_jsx(text)
    return None


# ---------------------------------------------------------------------------
# Vue single-file components
# ---------------------------------------------------------------------------

def _block(text: str, tag: str) -> list:
    return [m.group(1) for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.S | re.I)]


def _from_vue(text: str) -> str:
    # The outermost template is the component's; nested <template v-if> stay inside it.
    outer = re.search(r"<template\b[^>]*>(.*)</template>", text, re.S | re.I)
    body = outer.group(1) if outer else ""
    # Bound attributes are expressions, not values.
    body = re.sub(r"\s(?::|v-bind:)[\w-]+=(\"[^\"]*\"|'[^']*')", "", body)
    styles = "".join(f"<style>\n{css}\n</style>" for css in _block(text, "style"))
    return _WRAP.format(head=styles, body=body)


# ---------------------------------------------------------------------------
# JSX / TSX
# ---------------------------------------------------------------------------

def _balanced(text: str, start: int) -> int:
    """Index just past the `}` that closes the `{` at `start`; len(text) when it never closes."""
    depth, i, quote = 0, start, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _literal(expr: str) -> Optional[str]:
    """The text of a plain string literal expression, else None."""
    e = expr.strip()
    if len(e) >= 2 and e[0] == e[-1] and e[0] in "\"'`" and "${" not in e and e[0] not in e[1:-1]:
        return e[1:-1]
    return None


def _style_from_object(expr: str) -> str:
    """`{ fontSize: 14, color: "red" }` to `font-size: 14px; color: red`; unreadable entries are dropped."""
    inner = expr.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    out = []
    for m in re.finditer(r"([A-Za-z_$][\w$]*|['\"][\w-]+['\"])\s*:\s*(\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?)", inner):
        key = m.group(1).strip("'\"")
        prop = re.sub(r"[A-Z]", lambda c: "-" + c.group(0).lower(), key)
        value = m.group(2)
        if value[0] in "\"'":
            value = value[1:-1]
        elif prop not in _UNITLESS and value not in ("0", "-0"):
            value += "px"
        out.append(f"{prop}: {value}")
    return "; ".join(out)


def _attr_values(text: str) -> str:
    """Rewrite `name={expr}` attributes: literals stay, style objects become CSS, the rest is dropped."""
    out, i = [], 0
    pattern = re.compile(r"([A-Za-z_:][\w:.-]*)=\{")
    while True:
        m = pattern.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        end = _balanced(text, m.end() - 1)
        expr = text[m.end():end - 1]
        name = m.group(1)
        if name == "style" and expr.strip().startswith("{"):
            css = _style_from_object(expr)
            out.append(f'style="{css}"' if css else "")
        else:
            value = _literal(expr)
            out.append(f'{name}="{value}"' if value is not None else "")
        i = end
    return "".join(out)


def _text_expressions(text: str) -> str:
    """Remove `{...}` expressions that sit between tags; a string literal keeps its text."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "{":
            end = _balanced(text, i)
            value = _literal(text[i + 1:end - 1])
            out.append(value if value is not None else "")
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _from_jsx(text: str) -> str:
    text = _strip_comments(text)
    text = re.sub(r"(?m)^\s*(?:import|export\s+\{)[^;\n]*;?\s*$", "", text)
    first = re.search(r"<[a-z][\w-]*[\s>/]", text)
    if not first:
        return _WRAP.format(head="", body="")
    markup = text[first.start():]
    markup = re.sub(r"\bclassName=", "class=", markup)
    markup = re.sub(r"\bhtmlFor=", "for=", markup)
    markup = _attr_values(markup)
    markup = _text_expressions(markup)
    # Components are not HTML: their tags go, their children stay.
    markup = re.sub(r"</?[A-Z][\w.]*(?:\s[^<>]*)?/?>", "", markup)
    markup = re.sub(r"</?>", "", markup)
    # A `<` that does not start a tag (generics, comparisons) is not markup.
    markup = re.sub(r"<(?![A-Za-z/!])", "&lt;", markup)
    css = "".join(f"<style>\n{m}\n</style>" for m in re.findall(r"<style[^>]*>(.*?)</style>", markup, re.S | re.I))
    markup = re.sub(r"<style[^>]*>.*?</style>", "", markup, flags=re.S | re.I)
    return _WRAP.format(head=css, body=markup)
