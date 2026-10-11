"""Source map v3 reader for ``web_explain``.

A bundled site ships minified code; when it also publishes source maps, every
position in the bundle can be traced back to the original module, line and
symbol. This module does exactly that and nothing else:

* :func:`decode_vlq` / :func:`encode_vlq` -- the base64 VLQ of the ``mappings``
  field (encode exists for tests and for building small maps by hand).
* :class:`SourceMap` -- parses a map (plain or index map with ``sections``),
  answers :meth:`SourceMap.lookup` for a generated (line, column), returns
  numbered snippets of the original files when ``sourcesContent`` is present,
  and splits the sources into first-party modules and packages.
* :func:`package_of` -- ``(name, version)`` of a path inside ``node_modules``,
  reading the version from pnpm / yarn-berry style paths when it is there.
* :func:`banner_versions` -- ``/*! name vX.Y.Z */`` license banners in a
  minified bundle; a weaker signal, reported as inferred by the caller.

Positions: ``lookup`` takes a 1-based generated line and a 0-based column (what
browsers and stack traces print) and answers with a 1-based original line.
"""

from __future__ import annotations

import base64
import bisect
import json
import re
from typing import Iterable
from urllib.parse import unquote, urljoin

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64 = {c: i for i, c in enumerate(_ALPHABET)}

# Prefixes bundlers put in front of source paths; stripped for display only.
_SCHEME = re.compile(r"^(?:webpack|rollup|vite|turbopack|esbuild|parcel)://(?:[^/]*/)?", re.I)
_PNPM = re.compile(r"\.pnpm/((?:@[^/+]+\+)?[^/@]+)@(\d[^/_(]*)")
_PLUMBING = re.compile(r"^(?:import\b|export\s+\*|export\s*\{|\}\s*from\b|require\()")
_BANNER = re.compile(r"/\*[!*]?\s*(?:@license\s+)?((?:@[\w.-]+/)?[A-Za-z][\w.-]*)\s+v?(\d+\.\d+\.\d+(?:-[\w.]+)?)\b")


def decode_vlq(segment: str) -> list[int]:
    """Decode one comma-free VLQ segment into its signed integers."""
    values: list[int] = []
    shift = value = 0
    for ch in segment:
        digit = _B64.get(ch)
        if digit is None:
            raise ValueError(f"bad base64 VLQ character {ch!r}")
        value += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        values.append(-(value >> 1) if value & 1 else value >> 1)
        shift = value = 0
    if shift:
        raise ValueError("truncated VLQ segment")
    return values


def encode_vlq(values: Iterable[int]) -> str:
    out = []
    for v in values:
        n = (-v << 1) | 1 if v < 0 else v << 1
        while True:
            digit = n & 31
            n >>= 5
            out.append(_ALPHABET[digit | (32 if n else 0)])
            if not n:
                break
    return "".join(out)


def parse_mappings(mappings: str) -> list[list[tuple[int, int, int, int, int]]]:
    """Per generated line: sorted ``(gen_col, source, orig_line, orig_col, name)``, all 0-based, -1 when absent."""
    lines: list[list[tuple[int, int, int, int, int]]] = []
    src = oline = ocol = name = 0
    for raw in mappings.split(";"):
        col = 0
        row: list[tuple[int, int, int, int, int]] = []
        for seg in raw.split(",") if raw else ():
            if not seg:
                continue
            v = decode_vlq(seg)
            col += v[0]
            if len(v) >= 4:
                src += v[1]
                oline += v[2]
                ocol += v[3]
                if len(v) >= 5:
                    name += v[4]
                    row.append((col, src, oline, ocol, name))
                else:
                    row.append((col, src, oline, ocol, -1))
            else:
                row.append((col, -1, -1, -1, -1))
        row.sort()
        lines.append(row)
    return lines


def clean_path(path: str) -> str:
    """Readable form of a source path: bundler scheme, query and leading ./ or ../ removed."""
    p = unquote(path or "").replace("\\", "/")
    p = _SCHEME.sub("", p)
    p = p.split("?", 1)[0]
    while p.startswith(("./", "../")):
        p = p[2:] if p.startswith("./") else p[3:]
    return p.lstrip("/") or path


def package_of(path: str) -> tuple[str, str | None] | None:
    """``(name, version or None)`` when ``path`` lives in ``node_modules``; None for first-party code."""
    p = unquote(path or "").replace("\\", "/")
    if "node_modules/" not in p:
        return None
    tail = p.rsplit("node_modules/", 1)[1]
    if tail.startswith(".vite/deps/"):
        stem = tail[len(".vite/deps/"):].split("?", 1)[0].rsplit(".", 1)[0]
        if stem.startswith("chunk-") or not stem:
            return None
        if stem.startswith("@") and "_" in stem:
            scope, rest = stem[1:].split("_", 1)
            return f"@{scope}/{rest.split('_', 1)[0]}", None
        return stem.split("_", 1)[0], None
    parts = tail.split("/")
    if parts[0].startswith("@") and len(parts) > 1:
        name = f"{parts[0]}/{parts[1]}"
    else:
        name = parts[0]
    if not name or name.startswith("."):
        return None
    version = None
    for m in _PNPM.finditer(p):
        if m.group(1).replace("+", "/") == name:
            version = m.group(2)
    if version is None:
        m = re.search(r"(?:^|/)" + re.escape(name) + r"@(\d[\w.+-]*)/", p)
        if m:
            version = m.group(1)
    return name, version


def banner_versions(code: str, limit: int = 40) -> list[dict]:
    """``{name, version}`` from license banners at the head of bundled modules (deduplicated)."""
    seen: dict[str, str] = {}
    for m in _BANNER.finditer(code or ""):
        name, version = m.group(1), m.group(2)
        if name.lower() in {"license", "copyright", "version"}:
            continue
        seen.setdefault(name, version)
        if len(seen) >= limit:
            break
    return [{"name": k, "version": v} for k, v in seen.items()]


def load_map_text(text: str) -> dict:
    """Parse map JSON, tolerating the ``)]}'`` anti-XSSI prefix some servers add."""
    t = (text or "").lstrip("﻿")
    if t.startswith(")]}"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError("source map is not a JSON object")
    return data


def data_url_text(url: str) -> str | None:
    """Text of an inline ``data:`` source map URL, None when ``url`` is not one."""
    if not url.startswith("data:"):
        return None
    head, _, payload = url.partition(",")
    if ";base64" in head:
        return base64.b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")
    return unquote(payload)


_COMMENT = re.compile(r"[#@]\s*sourceMappingURL\s*=\s*(\S+)")


def map_url_from_code(code: str, script_url: str = "") -> str | None:
    """The source map URL a script declares in its last ``sourceMappingURL`` comment, made absolute."""
    tail = (code or "")[-4000:]
    found = _COMMENT.findall(tail)
    if not found:
        return None
    ref = found[-1].strip().rstrip("*/").strip()
    return ref if ref.startswith("data:") else urljoin(script_url, ref)


class SourceMap:
    """A parsed v3 source map (``sections`` index maps included)."""

    def __init__(self, data: dict, map_url: str = ""):
        self.map_url = map_url
        self._sections: list[tuple[int, int, SourceMap, int]] = []  # (line, column, child, first flat source index)
        self.sources: list[str] = []
        self.contents: list[str | None] = []
        self.names: list[str] = []
        self._lines: list[list[tuple[int, int, int, int, int]]] = []
        if isinstance(data.get("sections"), list):
            for sec in data["sections"]:
                off = sec.get("offset") or {}
                sub = sec.get("map")
                if isinstance(sub, dict):
                    child = SourceMap(sub, map_url)
                    self._sections.append((int(off.get("line", 0)), int(off.get("column", 0)), child, 0))
            self._sections.sort(key=lambda s: (s[0], s[1]))
            flat = []
            for sline, scol, child, _ in self._sections:
                flat.append((sline, scol, child, len(self.sources)))
                self.sources.extend(child.sources)
                self.contents.extend(child.contents)
            self._sections = flat
            return
        root = data.get("sourceRoot") or ""
        raw = [str(s or "") for s in data.get("sources") or []]
        self.sources = [clean_path((root.rstrip("/") + "/" + s) if root and not s.startswith(root) else s) for s in raw]
        contents = list(data.get("sourcesContent") or [])
        self.contents = [c if isinstance(c, str) else None for c in contents] + [None] * (len(raw) - len(contents))
        self.names = [str(n) for n in data.get("names") or []]
        self._lines = parse_mappings(str(data.get("mappings") or ""))

    # -- lookups -------------------------------------------------------------------------------------------------
    def lookup(self, line: int, column: int) -> dict | None:
        """Original position for generated ``line`` (1-based) and ``column`` (0-based), or None when unmapped."""
        if self._sections:
            best = None
            for section in self._sections:
                if (section[0], section[1]) <= (line - 1, column):
                    best = section
            if best is None:
                return None
            sline, scol, child, base = best
            hit = child.lookup(line - sline, column - scol if line - 1 == sline else column)
            return {**hit, "index": hit["index"] + base} if hit else None
        idx = line - 1
        if idx < 0 or idx >= len(self._lines):
            return None
        row = self._lines[idx]
        i = bisect.bisect_right(row, (column, 1 << 30)) - 1
        if i < 0:
            return None
        _, src, oline, ocol, name = row[i]
        if src < 0 or src >= len(self.sources):
            return None
        return {"source": self.sources[src], "line": oline + 1, "column": ocol,
                "name": self.names[name] if 0 <= name < len(self.names) else None, "index": src}

    def content(self, source: str | int) -> str | None:
        idx = source if isinstance(source, int) else (self.sources.index(source) if source in self.sources else -1)
        return self.contents[idx] if 0 <= idx < len(self.contents) else None

    def snippet(self, source: str | int, line: int, context: int = 3) -> str | None:
        """Numbered lines ``line-context .. line+context`` of an original file (``>`` marks ``line``)."""
        text = self.content(source)
        if text is None:
            return None
        lines = text.splitlines()
        lo, hi = max(1, line - context), min(len(lines), line + context)
        width = len(str(hi))
        return "\n".join(f"{'>' if n == line else ' '}{n:>{width}} | {lines[n - 1][:200]}" for n in range(lo, hi + 1))

    # -- inventory -----------------------------------------------------------------------------------------------
    def first_party(self) -> list[str]:
        """Original modules that are the site's own code (not packages, not bundler runtime)."""
        out = []
        for s in self.sources:
            if package_of(s) or not s or s.startswith(("(webpack)", "webpack/", "\0", "vite/", "__vite")) or "virtual:" in s:
                continue
            out.append(s)
        return out

    def packages(self) -> list[dict]:
        """``{name, version, files}`` for every package with modules in the bundle, most files first."""
        agg: dict[str, dict] = {}
        for s in self.sources:
            pkg = package_of(s)
            if not pkg:
                continue
            row = agg.setdefault(pkg[0], {"name": pkg[0], "version": pkg[1], "files": 0})
            row["files"] += 1
            if not row["version"] and pkg[1]:
                row["version"] = pkg[1]
        return sorted(agg.values(), key=lambda r: (-r["files"], r["name"]))

    def search(self, term: str, limit: int = 5, first_party_only: bool = False) -> list[dict]:
        """Case-insensitive hits of ``term`` in the original files: ``{source, line, text}``."""
        if not term:
            return []
        rx = re.compile(re.escape(term), re.I)
        own = set(self.first_party())
        order = sorted(range(len(self.sources)), key=lambda i: (self.sources[i] not in own, self.sources[i]))
        hits: list[dict] = []
        for i in order:
            if first_party_only and self.sources[i] not in own:
                continue
            text = self.contents[i] if i < len(self.contents) else None
            if not text:
                continue
            for n, row in enumerate(text.splitlines(), 1):
                if rx.search(row):
                    hits.append({"source": self.sources[i], "line": n, "text": row.strip()[:160]})
                    if len(hits) >= limit * 4:
                        break
            if len(hits) >= limit * 4:
                break
        # Code lines before import/export plumbing; stable otherwise (first-party first, then path order).
        hits.sort(key=lambda h: bool(_PLUMBING.match(h["text"])))
        return hits[:limit]
