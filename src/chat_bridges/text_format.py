"""src/chat_bridges/text_format.py — turn an agent answer (Markdown) into messages a
chat app accepts.

Telegram caps a message at 4096 characters and understands only a small HTML
subset (`b`, `i`, `s`, `code`, `pre`, `a`, `blockquote`). So the answer is:

1. cut into pieces *as Markdown*, at blank lines when it can and never in the
   middle of a code fence without closing and re-opening it, so every piece is
   valid on its own;
2. converted piece by piece: ALL text is HTML-escaped first, and only then are
   the few supported constructs turned into tags, so nothing the model (or a
   web page it read) wrote can inject a tag;
3. re-cut smaller if escaping pushed a piece over the limit.

`plain_chunks` is the fallback for when the chat app rejects the HTML anyway:
the original text, cut to the limit, with no markup at all.
"""
from __future__ import annotations

import html
import re
from typing import List, Tuple

#: Telegram's hard limit for one text message.
MAX_MESSAGE = 4096
#: Markdown is cut at this size so the HTML (which grows when `&`, `<` and `>`
#: are escaped, and with tags) normally fits without a second pass.
_SOFT_LIMIT = 3400

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([A-Za-z0-9_+.#-]*)\s*$")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)\"<>]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.S)


def chunk_markdown(text: str, max_chars: int = _SOFT_LIMIT) -> List[str]:
    """Split Markdown into pieces of at most `max_chars`, each valid alone."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text:
        return []
    max_chars = max(200, int(max_chars))
    pieces: List[str] = []
    current: List[str] = []
    size = 0
    fence_open = ""        # the opening line of the code fence we are inside, if any

    def flush() -> None:
        nonlocal current, size
        body = "\n".join(current).strip("\n")
        if body.strip():
            if fence_open:
                body += "\n```"
            pieces.append(body)
        current = [fence_open] if fence_open else []
        size = len(fence_open) + 1 if fence_open else 0

    for raw in text.split("\n"):
        # a single line longer than a message is cut hard
        lines = [raw[i:i + max_chars - 10] for i in range(0, len(raw), max_chars - 10)] or [""]
        for line in lines:
            if size + len(line) + 1 > max_chars and current:
                flush()
            match = _FENCE.match(line)
            if match and not fence_open:
                fence_open = line.strip()
            elif match and fence_open and not match.group(2):
                fence_open = ""
            current.append(line)
            size += len(line) + 1
            # a paragraph break is the nicest place to cut once the piece is mostly full
            if not line.strip() and not fence_open and size > max_chars * 0.7:
                flush()
    body = "\n".join(current).strip("\n")
    if body.strip() and body.strip() != fence_open:
        pieces.append(body)
    return pieces


def _code_block(match_lang: str, code: str) -> str:
    lang = match_lang if re.fullmatch(r"[A-Za-z0-9_+-]{1,20}", match_lang or "") else ""
    esc = html.escape(code, quote=False)
    if lang:
        return f'<pre><code class="language-{lang}">{esc}</code></pre>'
    return f"<pre>{esc}</pre>"


def markdown_to_html(text: str) -> str:
    """Telegram-HTML for one piece of Markdown. Everything is escaped first."""
    text = (text or "").replace("\x00", "")
    fences: List[str] = []
    inlines: List[str] = []

    # 1. fenced code blocks, line by line (an unclosed fence runs to the end)
    out_lines: List[str] = []
    buf: List[str] = []
    lang = ""
    in_fence = False
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _FENCE.match(line)
        if match and not in_fence:
            in_fence, lang, buf = True, match.group(2), []
        elif match and in_fence and not match.group(2):
            fences.append(_code_block(lang, "\n".join(buf)))
            out_lines.append(f"\x00F{len(fences) - 1}\x00")
            in_fence = False
        elif in_fence:
            buf.append(line)
        else:
            out_lines.append(line)
    if in_fence:
        fences.append(_code_block(lang, "\n".join(buf)))
        out_lines.append(f"\x00F{len(fences) - 1}\x00")

    # 2. inline code
    def stash_inline(m: "re.Match[str]") -> str:
        inlines.append(f"<code>{html.escape(m.group(2).strip(), quote=False)}</code>")
        return f"\x00I{len(inlines) - 1}\x00"

    lines = [_INLINE_CODE.sub(stash_inline, ln) for ln in out_lines]

    # 3. escape everything else, then line-level and inline constructs
    result: List[str] = []
    quote: List[str] = []

    def end_quote() -> None:
        if quote:
            result.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for ln in lines:
        if ln.startswith("\x00F"):
            end_quote()
            result.append(ln)
            continue
        quoted = ln.lstrip().startswith("&gt;") or ln.lstrip().startswith(">")
        esc = html.escape(ln, quote=False)
        if quoted:
            esc = re.sub(r"^\s*&gt;\s?", "", esc)
            quote.append(_inline(esc))
            continue
        end_quote()
        if _RULE.match(ln):
            result.append("──────────")
            continue
        heading = _HEADING.match(esc)
        if heading:
            result.append("<b>" + _inline(heading.group(1)) + "</b>")
            continue
        esc = _BULLET.sub(lambda m: m.group(1) + "• ", esc)
        result.append(_inline(esc))
    end_quote()

    body = "\n".join(result)
    body = re.sub(r"\x00F(\d+)\x00", lambda m: fences[int(m.group(1))], body)
    body = re.sub(r"\x00I(\d+)\x00", lambda m: inlines[int(m.group(1))], body)
    return body.strip()


def _inline(escaped: str) -> str:
    """Bold / italic / strike / links on text that is ALREADY HTML-escaped."""
    escaped = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', escaped)
    escaped = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", escaped)
    escaped = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", escaped)
    escaped = _ITALIC_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)
    escaped = _ITALIC_UNDER.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)
    return escaped


def render_pairs(markdown: str, limit: int = MAX_MESSAGE) -> List[Tuple[str, str]]:
    """The answer as `(html, plain)` messages, each within `limit` characters.

    `plain` is the same piece without markup, for the case where the chat app
    refuses the HTML: sending it instead keeps the answer whole and in order."""
    limit = max(200, int(limit))
    out: List[Tuple[str, str]] = []

    def add(piece: str) -> None:
        rendered = markdown_to_html(piece)
        if not rendered:
            return
        if len(rendered) <= limit:
            out.append((rendered, piece))
            return
        # escaping and tags made the piece longer than the limit: cut it again,
        # in proportion to how much it grew
        smaller = int(len(piece) * (limit / len(rendered)) * 0.9)
        parts = chunk_markdown(piece, smaller) if smaller >= 200 else []
        if len(parts) <= 1:                  # cannot shrink further: cut the text itself
            for chunk in plain_chunks(piece, limit // 2):
                out.append((html.escape(chunk, quote=False), chunk))
            return
        for part in parts:
            add(part)

    for piece in chunk_markdown(markdown, min(_SOFT_LIMIT, limit - 300)):
        add(piece)
    return out


def render_messages(markdown: str, limit: int = MAX_MESSAGE) -> List[str]:
    """The answer as HTML messages, each within `limit` characters."""
    return [rendered for rendered, _ in render_pairs(markdown, limit)]


def plain_chunks(text: str, limit: int = MAX_MESSAGE) -> List[str]:
    """`text` cut into pieces of at most `limit`, at line breaks when possible."""
    limit = max(1, int(limit))
    text = (text or "").strip()
    out: List[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ")
    return [c for c in out if c]
