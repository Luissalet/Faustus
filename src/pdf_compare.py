"""src/pdf_compare.py — word-by-word comparison of two PDFs.

For every page index present in either document the text layer is split into
words and diffed; runs of inserted, deleted and changed words are reported
with a little context, together with a per-document summary. Pages are
matched by position (page 3 of A against page 3 of B): a page inserted in the
middle shows up as changes from that page on, which the summary makes visible
through `pages_changed`.

Only the text layer is compared. A scanned page with no text layer has zero
words on both sides and is reported as `no_text` instead of "identical", so an
empty comparison is never mistaken for a match.
"""
from __future__ import annotations

import difflib
import html
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_RUNS_PER_PAGE = 200
MAX_RUN_TEXT = 400
CONTEXT_WORDS = 4


class CompareError(ValueError):
    """A comparison that cannot be carried out."""


def extract_words(path: str) -> List[List[str]]:
    """Words per page, from the text layer."""
    import pypdf
    try:
        reader = pypdf.PdfReader(path)
    except Exception as exc:  # noqa: BLE001
        raise CompareError(f"could not open '{path}' as a PDF: {exc}") from exc
    if reader.is_encrypted:
        raise CompareError(f"'{path}' is encrypted; decrypt it before comparing")
    pages: List[List[str]] = []
    for page in reader.pages:
        try:
            pages.append((page.extract_text() or "").split())
        except Exception as exc:  # noqa: BLE001 — unreadable page = no words, flagged below
            logger.warning("pdf_compare: could not extract a page: %s", exc)
            pages.append([])
    return pages


def _clip(words: List[str]) -> str:
    text = " ".join(words)
    return text if len(text) <= MAX_RUN_TEXT else text[:MAX_RUN_TEXT] + "..."


def diff_words(a: List[str], b: List[str]) -> Dict[str, Any]:
    """Diff two word lists into runs. `replace` runs are "changed" words."""
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    runs: List[Dict[str, Any]] = []
    inserted = deleted = changed = 0
    truncated = False
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            kind, inserted = "inserted", inserted + (j2 - j1)
        elif tag == "delete":
            kind, deleted = "deleted", deleted + (i2 - i1)
        else:
            kind, changed = "changed", changed + max(i2 - i1, j2 - j1)
        if len(runs) >= MAX_RUNS_PER_PAGE:
            truncated = True
            continue
        runs.append({
            "type": kind,
            "a": _clip(a[i1:i2]),
            "b": _clip(b[j1:j2]),
            "a_words": [i1, i2],
            "b_words": [j1, j2],
            "context": _clip(a[max(0, i1 - CONTEXT_WORDS):i1]),
        })
    return {"runs": runs, "inserted": inserted, "deleted": deleted, "changed": changed,
            "truncated": truncated, "ratio": sm.ratio() if (a or b) else 1.0}


def compare(path_a: str, path_b: str) -> Dict[str, Any]:
    pages_a, pages_b = extract_words(path_a), extract_words(path_b)
    n = max(len(pages_a), len(pages_b))
    pages: List[Dict[str, Any]] = []
    totals = {"inserted": 0, "deleted": 0, "changed": 0}
    identical = changed_pages = no_text = 0
    words_a = words_b = 0
    for i in range(n):
        a = pages_a[i] if i < len(pages_a) else []
        b = pages_b[i] if i < len(pages_b) else []
        words_a += len(a)
        words_b += len(b)
        entry: Dict[str, Any] = {"page": i + 1}
        if i >= len(pages_a):
            entry["status"] = "only_in_b"
        elif i >= len(pages_b):
            entry["status"] = "only_in_a"
        if not a and not b:
            entry.setdefault("status", "no_text")
            no_text += 1
            pages.append(entry)
            continue
        d = diff_words(a, b)
        if not d["runs"]:
            entry["status"] = "identical"
            identical += 1
        else:
            entry.setdefault("status", "changed")
            changed_pages += 1
            for k in totals:
                totals[k] += d[k]
            entry.update({"inserted": d["inserted"], "deleted": d["deleted"],
                          "changed": d["changed"], "runs": d["runs"],
                          "runs_truncated": d["truncated"]})
        pages.append(entry)
    total_changed_words = sum(totals.values())
    summary = {
        "pages_a": len(pages_a), "pages_b": len(pages_b),
        "pages_identical": identical, "pages_changed": changed_pages, "pages_without_text": no_text,
        "words_a": words_a, "words_b": words_b,
        "words_inserted": totals["inserted"], "words_deleted": totals["deleted"],
        "words_changed": totals["changed"],
        "identical": total_changed_words == 0 and len(pages_a) == len(pages_b),
    }
    return {"a": path_a, "b": path_b, "summary": summary, "pages": pages}


def render_html(result: Dict[str, Any], *, title: str = "PDF comparison") -> str:
    """A self-contained HTML report (everything escaped, no scripts)."""
    e = html.escape
    s = result["summary"]
    rows = []
    for p in result["pages"]:
        head = f"<h3>Page {p['page']} &mdash; {e(p['status'])}</h3>"
        body = ""
        for run in p.get("runs", []):
            cls = run["type"]
            body += (f"<div class='run {cls}'><span class='tag'>{e(cls)}</span> "
                     f"<span class='ctx'>{e(run['context'])}</span> "
                     f"<del>{e(run['a'])}</del> <ins>{e(run['b'])}</ins></div>")
        if p.get("runs_truncated"):
            body += "<p class='note'>More differences on this page were not listed.</p>"
        rows.append(f"<section>{head}{body}</section>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{e(title)}</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}"
        ".run{margin:.3rem 0;padding:.3rem .5rem;border-left:4px solid #888;background:#f6f6f6}"
        ".inserted{border-color:#2a8a3a}.deleted{border-color:#b33}.changed{border-color:#c80}"
        ".tag{font-size:.75rem;text-transform:uppercase;color:#555}.ctx{color:#777}"
        "del{background:#fdd}ins{background:#dfd;text-decoration:none}"
        "</style></head><body>"
        f"<h1>{e(title)}</h1>"
        f"<p>A: {e(os.path.basename(result['a']))} ({s['pages_a']} pages) &middot; "
        f"B: {e(os.path.basename(result['b']))} ({s['pages_b']} pages)</p>"
        f"<p>Identical pages: {s['pages_identical']} &middot; changed pages: {s['pages_changed']} &middot; "
        f"inserted words: {s['words_inserted']} &middot; deleted: {s['words_deleted']} &middot; "
        f"changed: {s['words_changed']}</p>"
        + "".join(rows) + "</body></html>"
    )


__all__ = ["CompareError", "extract_words", "diff_words", "compare", "render_html"]
