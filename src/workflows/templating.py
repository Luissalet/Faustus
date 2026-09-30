"""
workflows/templating.py — the only way a model node reads the rest of the run.

A prompt template can say `{{ inputs.ticket }}` or `{{ results.fetch.text }}`
and nothing else: dotted paths into the same context a `condition` reads
(`handlers.resolve`), no indexing, no calls, no expressions. A workflow file is
data somebody can paste in, and a template language with logic in it is an
execution surface.

Two rules keep a model from being fed the wrong thing quietly:

* **A missing value is an error, not an empty string.** A prompt that silently
  lost `{{ results.fetch.text }}` would send the model a question about
  nothing and the run would record a confident answer.
* **Structured values are rendered as JSON**, exactly, so the model sees what
  the upstream node produced rather than a Python repr of it. `| json` forces
  that for a string too.
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Mapping, Tuple

from .handlers import resolve, _MISSING

__all__ = ["render", "references", "MAX_RENDERED_CHARS", "TemplateError"]

#: Hard ceiling on a rendered prompt. A node that interpolates a whole
#: upstream transcript into a prompt is a bug worth stopping, not an
#: out-of-memory worth waiting for.
MAX_RENDERED_CHARS = 200_000

_TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*(?:\|\s*(json))?\s*\}\}")


class TemplateError(ValueError):
    """The template could not be filled from this run."""


def references(template: Any) -> List[str]:
    """The paths a template reads, in order, without repeats."""
    seen: List[str] = []
    for match in _TOKEN.finditer(str(template or "")):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def _show(value: Any, as_json: bool) -> str:
    if isinstance(value, str) and not as_json:
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def render(template: Any, context: Mapping[str, Any]) -> str:
    """Fill `template` from `context`, or raise :class:`TemplateError` naming
    every path that resolved to nothing."""
    text = str(template if template is not None else "")
    missing: List[Tuple[str, str]] = []

    def fill(match: "re.Match[str]") -> str:
        path = match.group(1)
        value = resolve(path, context)
        if value is _MISSING:
            missing.append((path, match.group(0)))
            return ""
        return _show(value, bool(match.group(2)))

    out = _TOKEN.sub(fill, text)
    if missing:
        names = sorted({p for p, _ in missing})
        raise TemplateError(
            "the template refers to " + ", ".join(repr(n) for n in names) +
            ", which nothing in this run has produced (check `needs` and the spelling)")
    if len(out) > MAX_RENDERED_CHARS:
        raise TemplateError(
            f"the rendered text is {len(out)} characters; the limit is {MAX_RENDERED_CHARS}")
    return out
