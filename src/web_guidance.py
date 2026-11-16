"""web_guidance.py - modern web platform guidance, searched and read locally.

`skills/guidance/modern-web-guidance/` holds a vendored copy of the Chrome team's
web-platform guides (Apache-2.0, see its LICENSE and SOURCE.json): 209 short,
current how-tos for dialogs and popovers, anchor positioning, view transitions,
scroll-driven effects, forms and autofill, accessibility, performance, security
and passkeys, with the fallbacks each feature needs. A model's training weights
hold older patterns for most of these; reading the guide before writing the UI
code is what the `modern-web-guidance` library skill asks for.

Nothing here reaches the network: the guides and a small index (`index.json`:
id, category, description, headings) are files on disk, and
`scripts/update_web_guidance.py` refreshes them on demand.

    status()                         what is vendored (version, count, categories)
    list_guides(category=None)       every guide with its one-line description
    search(query, limit, category)   ranked guides for a short capability query
    retrieve(ids, offset, max_chars) the guide text, in slices when it is long

The same three operations are the `web_guidance` chat tool, the built-in MCP
server `web_guidance`, and `/api/web-guidance/*`.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:  # pragma: no cover - runtime_paths always imports in the app
    from src.runtime_paths import get_app_root
    _APP_ROOT = get_app_root()
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The vendored tree. Reassigned by tests that want a disposable one.
GUIDANCE_DIR = os.path.join(_APP_ROOT, "skills", "guidance", "modern-web-guidance")

DEFAULT_LIMIT = 8
MAX_LIMIT = 30
#: One retrieve slice. The longest guide is ~50 KB; a slice keeps a turn's context sane.
DEFAULT_MAX_CHARS = 24_000
MAX_CHARS_CAP = 60_000

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from how i in into is it of on or so that the this to use using "
    "with when where which while you your can do does make want need add build create".split())

# Query words people use for a UI primitive the guides name differently. Each maps to extra
# search words (weighted below the written words), so "modal" finds the dialog guides.
_SYNONYMS: Dict[str, Sequence[str]] = {
    "modal": ("dialog", "top", "layer"),
    "popup": ("popover", "dialog"),
    "pop": ("popover",),
    "overlay": ("dialog", "popover"),
    "lightbox": ("dialog",),
    "tooltip": ("popover", "anchor", "hovercard"),
    "dropdown": ("select", "menu", "popover"),
    "combobox": ("select", "menu"),
    "toggle": ("switch", "checkbox"),
    "animation": ("animate", "transition", "motion"),
    "animated": ("animate", "transition"),
    "fade": ("animate", "transition"),
    "slide": ("animate", "transition"),
    "darkmode": ("dark", "mode", "color", "scheme"),
    "theme": ("dark", "color", "scheme", "token"),
    "sticky": ("sticky", "scroll", "header"),
    "parallax": ("scroll", "driven"),
    "responsive": ("container", "query", "layout"),
    "validation": ("validate", "user", "invalid", "required"),
    "login": ("sign", "passkey", "autofill"),
    "password": ("passkey", "autofill"),
    "sanitize": ("trusted", "html", "xss"),
    "xss": ("sanitize", "trusted", "html"),
    "lcp": ("performance", "image", "priority"),
    "inp": ("interaction", "long", "task"),
    "cls": ("layout", "shift", "font"),
    "datepicker": ("date", "calendar", "input"),
    "carousel": ("scroll", "snap"),
    "slider": ("range", "input"),
    "ai": ("built", "model", "prompt"),
}

_lock = threading.Lock()
_cache: Dict[str, Any] = {"key": None, "docs": None, "idf": None, "avgdl": 0.0, "index": None}


# --- the index ------------------------------------------------------------------------------------------------------

def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> List[str]:
    return [_stem(w) for w in _WORD_RE.findall(str(text or "").lower()) if w not in _STOP and len(w) > 1]


def _title_and_headings(text: str) -> tuple[str, List[str]]:
    title, heads = "", []
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line)
        if not m:
            continue
        if len(m.group(1)) == 1 and not title:
            title = m.group(2).strip()
        else:
            heads.append(m.group(2).strip())
    return title, heads[:40]


def _first_paragraph(text: str) -> str:
    in_code = False
    para: List[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        s = line.strip()
        if in_code or s.startswith("#") or s.startswith("- [") or s.startswith("  - ["):
            continue
        if not s:
            if para:
                break
            continue
        para.append(s)
    return re.sub(r"\s+", " ", " ".join(para))[:300]


def build_index(guides_dir: str | os.PathLike, upstream: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """The index of every `<category>/<id>.md` under `guides_dir`.

    `upstream` is the publisher's own list (id, description, category, featuresUsed, tokenCount); its
    descriptions are kept because they were written for search. A guide the list does not name still gets
    an entry, described by its first paragraph. Sorted, so a refresh produces a reviewable diff.
    """
    root = Path(guides_dir)
    named = {str(g.get("id")): g for g in (upstream or []) if g.get("id")}
    rows: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        gid = path.stem
        text = path.read_text(encoding="utf-8")
        title, heads = _title_and_headings(text)
        meta = named.get(gid) or {}
        rows.append({
            "id": gid,
            "category": path.parent.name,
            "path": path.relative_to(root).as_posix(),
            "title": title or gid,
            "description": str(meta.get("description") or _first_paragraph(text)),
            "features": [str(f) for f in (meta.get("featuresUsed") or [])],
            "headings": heads,
            "tokens": int(meta.get("tokenCount") or max(1, len(text) // 4)),
        })
    rows.sort(key=lambda r: (r["category"], r["id"]))
    return rows


RAW_URL = "https://raw.githubusercontent.com/GoogleChrome/modern-web-guidance/main/skills/modern-web-guidance/guides"


def guide_index_markdown(index: Iterable[Dict[str, Any]]) -> str:
    """`references/guide-index.md` of the library skill: every guide id with its description, for a copy of
    the skill exported to a host that has no `web_guidance` tool. Rebuilt by the update script."""
    lines = [
        "# Modern web guidance: guide index", "",
        "Every guide id with its one-line description, by category. Inside Faustus read one with the "
        "`web_guidance` tool (`action=retrieve`). Elsewhere, read it at "
        f"`{RAW_URL}/<category>/<id>.md`.", "",
    ]
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for row in index:
        by_cat.setdefault(row["category"], []).append(row)
    for cat in sorted(by_cat):
        lines += [f"## {cat}", ""]
        for r in by_cat[cat]:
            text = " ".join(str(r["description"]).split())
            lines.append(f"- `{r['id']}`: {text}")
        lines.append("")
    return "\n".join(lines)


def _meta_path(name: str) -> str:
    return os.path.join(GUIDANCE_DIR, name)


def source() -> Dict[str, Any]:
    """SOURCE.json: package, version, licence, where the guides came from."""
    try:
        with open(_meta_path("SOURCE.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _load() -> Dict[str, Any]:
    """The index and a BM25 table over it, rebuilt when `index.json` changes."""
    path = _meta_path("index.json")
    try:
        key = (GUIDANCE_DIR, os.stat(path).st_mtime_ns)
    except OSError:
        return {"index": [], "docs": [], "idf": {}, "avgdl": 0.0}
    with _lock:
        if _cache["key"] == key:
            return _cache
        with open(path, "r", encoding="utf-8") as fh:
            index = json.load(fh)
        docs: List[Dict[str, Any]] = []
        for row in index:
            tf: Dict[str, float] = {}

            def add(text: str, weight: float) -> None:
                for tok in _tokens(text):
                    tf[tok] = tf.get(tok, 0.0) + weight

            add(row["id"].replace("-", " "), 4.0)
            add(row.get("title", ""), 3.0)
            add(row.get("description", ""), 2.0)
            add(" ".join(row.get("features") or []), 2.0)
            add(" ".join(row.get("headings") or []), 1.0)
            docs.append({"row": row, "tf": tf, "len": sum(tf.values()) or 1.0})
        df: Dict[str, int] = {}
        for d in docs:
            for tok in d["tf"]:
                df[tok] = df.get(tok, 0) + 1
        n = max(1, len(docs))
        idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        _cache.update(key=key, index=index, docs=docs, idf=idf, avgdl=sum(d["len"] for d in docs) / n)
        return _cache


def _row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row[k] for k in ("id", "category", "description", "tokens") if k in row}


# --- the operations -------------------------------------------------------------------------------------------------

def status() -> Dict[str, Any]:
    state = _load()
    index = state.get("index") or []
    cats: Dict[str, int] = {}
    for row in index:
        cats[row["category"]] = cats.get(row["category"], 0) + 1
    meta = source()
    return {
        "available": bool(index),
        "guides": len(index),
        "categories": dict(sorted(cats.items())),
        "version": meta.get("version"),
        "skill_version": meta.get("skill_version"),
        "license": meta.get("license"),
        "source": meta.get("repository"),
        "update": "python scripts/update_web_guidance.py",
    }


def list_guides(category: Optional[str] = None) -> Dict[str, Any]:
    index = _load().get("index") or []
    cat = str(category or "").strip().lower()
    rows = [_row(r) for r in index if not cat or r["category"] == cat]
    return {"category": cat or None, "count": len(rows), "guides": rows}


def search(query: str, limit: int = DEFAULT_LIMIT, category: Optional[str] = None) -> Dict[str, Any]:
    """Guides for a short capability query ("light dismiss dialog", "responsive data table").

    BM25 over each guide's id, title, description, features and headings; an exact id wins outright.
    Words people use for a UI primitive ("modal", "tooltip", "dropdown") also search the guides' own words.
    """
    state = _load()
    docs, idf, avgdl = state.get("docs") or [], state.get("idf") or {}, state.get("avgdl") or 1.0
    q = str(query or "").strip()
    cat = str(category or "").strip().lower()
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if not q:
        return {"query": q, "results": [], "note": "give a 2-5 word query naming one web platform capability"}
    wanted: Dict[str, float] = {}
    for word in _WORD_RE.findall(q.lower()):
        if word in _STOP:
            continue
        wanted[_stem(word)] = max(wanted.get(_stem(word), 0.0), 1.0)
        for extra in _SYNONYMS.get(word, ()):
            wanted[_stem(extra)] = max(wanted.get(_stem(extra), 0.0), 0.5)
    qslug = "-".join(_WORD_RE.findall(q.lower()))
    said = set(_WORD_RE.findall(q.lower()))
    k1, b = 1.4, 0.6
    scored: List[tuple[float, Dict[str, Any]]] = []
    for d in docs:
        row = d["row"]
        if cat and row["category"] != cat:
            continue
        score = 0.0
        for tok, weight in wanted.items():
            f = d["tf"].get(tok)
            if f:
                score += weight * idf.get(tok, 0.0) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * d["len"] / avgdl))
        if qslug and qslug == row["id"]:
            score += 100.0
        elif score and row["id"] in said:
            score += 6.0  # the query names this guide's own subject ("... dialog" -> the dialog guide)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda s: (-s[0], s[1]["id"]))
    results = [{**_row(row), "score": round(score, 3)} for score, row in scored[:limit]]
    out: Dict[str, Any] = {"query": q, "results": results}
    if not results:
        out["note"] = ("no guide matches; rephrase once with the concrete HTML/CSS/UI noun (dialog, popover, table, "
                       "form), or list the guides of one category")
    return out


_NPX_RE = re.compile(r"npx\s+(?:-y\s+)?modern-web-guidance@latest\s+retrieve\s+(\"[^\"]+\")")


def _local(text: str) -> str:
    """The guides cross-reference each other with the publisher's command line; point them at this tool."""
    return _NPX_RE.sub(lambda m: f"web_guidance action=retrieve ids={m.group(1)}", text)


def retrieve(ids: Any, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
    """The text of one or more guides (`ids` is a list, or a comma-separated string).

    A long guide comes back in slices: `truncated` is true and `next_offset` is where the next call
    continues. Unknown ids are listed in `missing`, with the closest existing ids.
    """
    if isinstance(ids, str):
        wanted = [p.strip().lower() for p in re.split(r"[,\s]+", ids) if p.strip()]
    else:
        wanted = [str(p).strip().lower() for p in (ids or []) if str(p).strip()]
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        max_chars = max(2_000, min(int(max_chars), MAX_CHARS_CAP))
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    index = {r["id"]: r for r in (_load().get("index") or [])}
    guides: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for gid in dict.fromkeys(wanted):
        row = index.get(gid) if _ID_RE.match(gid) else None
        if row is None:
            near = difflib.get_close_matches(gid, list(index), n=3, cutoff=0.7)
            near += [r["id"] for r in (search(gid.replace("-", " "), limit=3).get("results") or []) if r["id"] not in near]
            missing.append({"id": gid, "closest": near[:3]})
            continue
        path = Path(GUIDANCE_DIR) / "guides" / row["path"]
        try:
            text = _local(path.read_text(encoding="utf-8"))
        except OSError:
            missing.append({"id": gid, "closest": []})
            continue
        piece = text[offset: offset + max_chars]
        end = offset + len(piece)
        entry: Dict[str, Any] = {"id": gid, "category": row["category"], "title": row["title"], "chars": len(text),
                                 "offset": offset, "text": piece, "truncated": end < len(text)}
        if entry["truncated"]:
            entry["next_offset"] = end
        guides.append(entry)
    out: Dict[str, Any] = {"guides": guides}
    if missing:
        out["missing"] = missing
    return out


def render(action: str, result: Dict[str, Any]) -> str:
    """A model-readable rendering of the result of `search`, `retrieve` or `list`."""
    if action == "retrieve":
        parts: List[str] = []
        for g in result.get("guides") or []:
            head = f"=== {g['id']} ({g['category']}) ==="
            if g.get("truncated"):
                head += f"  [chars {g['offset']}-{g['offset'] + len(g['text'])} of {g['chars']}; continue with offset={g['next_offset']}]"
            parts.append(head + "\n" + g["text"])
        for m in result.get("missing") or []:
            parts.append(f"(no guide '{m['id']}'" + (f"; closest: {', '.join(m['closest'])})" if m["closest"] else ")"))
        return "\n\n".join(parts) or "no guide given"
    if action == "search":
        rows = result.get("results") or []
        if not rows:
            return result.get("note") or "no guide matches"
        return ("\n".join(f"{r['id']} [{r['category']}] - {r['description']}" for r in rows)
                + "\nRead one with web_guidance action=retrieve ids=\"<id>\", plus the discipline guide that applies "
                  "(html, css, forms, accessibility, performance, security).")
    return "\n".join(f"{r['id']} [{r['category']}] - {r['description']}" for r in result.get("guides") or []) or "no guides"
