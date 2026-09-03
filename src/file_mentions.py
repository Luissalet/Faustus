"""file_mentions.py — `@path` mentions of workspace files.

Every other coding workspace (Claude Code's `@`, Cursor's `@`, ChatGPT's `#`)
lets you point at a file from the composer instead of describing it in prose.
Two things follow from that, and both matter more for a small local model than
they do for a frontier one:

  * the model is handed a path that provably exists, so it cannot substitute a
    neighbouring file for the one you meant (the failure this repo already
    guards against after the fact with `check_target_substitution`); and
  * small mentioned files can ride along with the turn, removing a `read_file`
    round from a loop that costs ~30 s a round on a 9B model.

This module is the whole feature's brain: ranking for the picker, parsing the
`@tokens` back out of the sent message, resolving them against the workspace
index, and rendering the reference block the agent loop injects.

Stdlib only, never raises out of the public helpers.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# A mention is "@" + a path-ish run. Paths with spaces are written @"a b/c.py".
# The leading char class keeps emails (`a@b.com`) and decorators from matching:
# a mention starts a token, it never sits tight against a word character.
MENTION_RE = re.compile(
    r'(?<![\w@/\\.-])@(?:"([^"\n]{1,300})"|([A-Za-z0-9_.][\w./\\-]{0,299}))'
    # …optionally followed by the line the user cares about: `@src/app.py:42`
    # or `@src/app.py:120-160`. The path class has no ":", so before this the
    # line number was silently dropped and the whole file was inlined instead.
    r'(?::(\d{1,7})(?:-(\d{1,7}))?)?'
)

_MAX_RESULTS = 200
_DEFAULT_INLINE_CHARS = 6000
_MAX_INLINE_FILE_CHARS = 12000
_MENTION_LINE_RADIUS = 12          # context around a bare `@file.py:42`
_BINARY_SNIFF = 4000

# Ranked ahead of everything else when the query is empty, so the bare "@"
# menu opens on the files someone actually wants rather than on ./.gitignore.
_SOURCE_DIR_BONUS = (
    "src/", "app/", "lib/", "routes/", "services/", "core/", "api/", "server/",
    "static/js/", "components/", "pages/", "cmd/", "pkg/", "internal/",
)
_TEST_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs|e2e|fixtures?)(?:/|$)"
    r"|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$",
    re.I,
)
_VENDOR_RE = re.compile(
    r"(?:^|/)(?:vendor|third_party|thirdparty|external|licenses|node_modules|"
    r"dist|build|__pycache__|migrations?|locales?|i18n)(?:/|$)|\.min\.\w+$",
    re.I,
)
# Never inlined into the prompt, even when mentioned: the path is listed so the
# model knows which file is meant, but the secret does not ride along into a
# request (and its logs) that the user only meant as "this file". read_file
# still works — reading a secret should be a deliberate act, not a side effect
# of naming it.
_SECRET_NAME_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\.[\w-]+)?|\.netrc|\.npmrc|\.pypirc|\.htpasswd|"
    r"id_[a-z0-9]+|credentials|secrets?\.(?:ya?ml|json|toml|ini)|"
    r"[^/]*\.(?:pem|key|pfx|p12|jks|keystore|ppk))$",
    re.I,
)
_NOISE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "composer.lock", "go.sum",
}


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def enabled() -> bool:
    return bool(_setting("agent_file_mentions", True))


# ── ranking ───────────────────────────────────────────────────────────────

def _subsequence_score(hay: str, needle: str) -> float:
    """fzf-ish: every char of `needle` in order inside `hay`. 0 when it isn't.

    Denser (fewer gaps) and earlier matches score higher, so `wsrt` prefers
    `workspace_routes.py` over `web_socket_retry_helper.py`.
    """
    if not needle:
        return 0.0
    i = 0
    first = -1
    last = -1
    for pos, ch in enumerate(hay):
        if ch == needle[i]:
            if first < 0:
                first = pos
            last = pos
            i += 1
            if i == len(needle):
                break
    if i < len(needle):
        return 0.0
    span = max(1, last - first + 1)
    density = len(needle) / span                    # 1.0 == contiguous
    head = 1.0 / (1.0 + first * 0.05)               # matching near the start wins
    return 120.0 * density + 40.0 * head


def _base_penalty(rel: str) -> float:
    """Shared shaping: depth, tests, vendored code, lockfiles."""
    p = 0.0
    p -= 6.0 * rel.count("/")
    if _TEST_RE.search(rel):
        p -= 45.0
    if _VENDOR_RE.search(rel):
        p -= 120.0
    if rel.rsplit("/", 1)[-1] in _NOISE_NAMES:
        p -= 200.0
    for d in _SOURCE_DIR_BONUS:
        if rel.startswith(d):
            p += 22.0
            break
    return p


def score_path(rel: str, query: str) -> float:
    """Rank `rel` for `query` (already lowercased, no leading @). 0 == no match."""
    low = rel.lower()
    base = low.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    if not query:
        return 100.0 + _base_penalty(rel)
    s = 0.0
    if base == query or stem == query:
        s = 1000.0
    elif base.startswith(query):
        s = 700.0 - min(60.0, len(base) - len(query))
    elif low.startswith(query):
        s = 620.0
    elif query in base:
        s = 500.0 - min(60.0, base.index(query))
    elif query in low:
        s = 380.0
    else:
        sub = _subsequence_score(low, query)
        if sub <= 0:
            return 0.0
        s = 150.0 + sub
    return s + _base_penalty(rel)


def _index(workspace: str) -> List[str]:
    try:
        from src.agent_harness import workspace_file_index
        return workspace_file_index(workspace) or []
    except Exception as e:                                # pragma: no cover
        logger.debug("[file-mentions] index failed: %s", e)
        return []


def search(workspace: str, query: str = "", limit: int = 12) -> List[Dict[str, Any]]:
    """Ranked picker rows: [{rel, name, dir}] — the popup's data source."""
    if not workspace:
        return []
    q = (query or "").strip().lstrip("@").replace("\\", "/").lower()
    files = _index(workspace)
    if not files:
        return []
    scored: List[Tuple[float, str]] = []
    for rel in files:
        sc = score_path(rel, q)
        if sc > 0:
            scored.append((sc, rel))
    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    out: List[Dict[str, Any]] = []
    for sc, rel in scored[: max(1, min(int(limit or 12), _MAX_RESULTS))]:
        name = rel.rsplit("/", 1)[-1]
        out.append({"rel": rel, "name": name,
                    "dir": rel[: -len(name)].rstrip("/"), "score": round(sc, 1)})
    return out


# ── parsing / resolving ───────────────────────────────────────────────────

# `@expert:corrector` names a specialist agent (services/experts.py), not a
# file. The path class has no ":", so the regex matched the bare word `expert`
# and `resolve()` then reported "expert" as a file the workspace does not have
# — a mention the user typed correctly, blamed on them. Namespaced mentions are
# recognised here and skipped by the file resolver; `expert_mentions()` reads
# the slugs back out for whoever wants them.
_NAMESPACES = ("expert",)
_NAMESPACED_RE = re.compile(
    r'(?<![\w@/\\.-])@(' + "|".join(_NAMESPACES) + r'):([A-Za-z0-9][\w.-]{0,99})'
)


def namespaced_mentions(text: str, namespace: str) -> List[str]:
    """The slugs of `@<namespace>:<slug>` mentions, in order, de-duplicated."""
    out: List[str] = []
    if not text or "@" not in text:
        return out
    for m in _NAMESPACED_RE.finditer(text):
        if m.group(1) != namespace:
            continue
        slug = m.group(2).rstrip(".,;:!?")
        if slug and slug not in out:
            out.append(slug)
    return out


def expert_mentions(text: str) -> List[str]:
    """`@expert:corrector` → ["corrector"]."""
    return namespaced_mentions(text, "expert")


def extract(text: str) -> List[str]:
    """The raw mention bodies in `text`, in order, de-duplicated.

    A namespaced mention (`@expert:corrector`) is not a path and is left out:
    it belongs to whoever owns that namespace, not to the file index.
    """
    if not text or "@" not in text:
        return []
    skip = {m.start() for m in _NAMESPACED_RE.finditer(text)}
    seen: List[str] = []
    for m in MENTION_RE.finditer(text):
        if m.start() in skip:
            continue
        raw = (m.group(1) or m.group(2) or "").strip()
        # A trailing period is nearly always sentence punctuation, not a path.
        while raw and raw[-1] in ".,;:!?":
            raw = raw[:-1]
        if raw and raw not in seen:
            seen.append(raw)
    return seen


def resolve(workspace: str, text: str) -> Dict[str, List[str]]:
    """Split the mentions in `text` into workspace-relative hits and misses.

    Matching is: exact relative path, then case-insensitive relative path, then
    a unique basename. An ambiguous basename (`@utils.py` with four of them) is
    reported in `ambiguous` rather than guessed — guessing which file the user
    meant is exactly the substitution failure this feature exists to prevent.
    """
    out: Dict[str, List[str]] = {"resolved": [], "missing": [], "ambiguous": []}
    mentions = extract(text)
    if not mentions or not workspace:
        return out
    files = _index(workspace)
    if not files:
        return out
    by_lower = {f.lower(): f for f in files}
    by_base: Dict[str, List[str]] = {}
    for f in files:
        by_base.setdefault(f.rsplit("/", 1)[-1].lower(), []).append(f)
    exact = set(files)
    for raw in mentions:
        rel = _normalise_rel(raw)
        if rel in exact:
            _add(out["resolved"], rel)
            continue
        hit = by_lower.get(rel.lower())
        if hit:
            _add(out["resolved"], hit)
            continue
        if "/" not in rel:
            cands = by_base.get(rel.lower(), [])
            if len(cands) == 1:
                _add(out["resolved"], cands[0])
                continue
            if len(cands) > 1:
                _add(out["ambiguous"], raw)
                continue
        _add(out["missing"], raw)
    return out


def _normalise_rel(raw: str) -> str:
    """`@.\\src\\a.py` → `src/a.py`, and crucially `@.env` → `.env`.

    lstrip("./") strips a *character set*, so it ate the dot of every dotfile:
    ".env" became "env" and was reported as a file that does not exist.
    """
    rel = str(raw or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def _add(lst: List[str], item: str) -> None:
    if item not in lst:
        lst.append(item)


def strip_markers(text: str) -> str:
    """`@src/x.py` → `src/x.py`, for prompts that read better without the sigil.

    A `:42` / `:120-160` suffix is kept: it is what the user is pointing at.
    """
    def _sub(m: "re.Match[str]") -> str:
        body = m.group(1) or m.group(2) or ""
        if m.group(3):
            body += ":" + m.group(3) + ("-" + m.group(4) if m.group(4) else "")
        return body
    return MENTION_RE.sub(_sub, text or "")


def extract_ranges(text: str) -> Dict[str, Tuple[int, int]]:
    """`@src/app.py:42` / `@src/app.py:120-160` → {key: (first, last)}.

    Keyed by the normalised mention AND by its basename, so the caller can look
    a span up with whatever the index resolved the mention to.
    """
    out: Dict[str, Tuple[int, int]] = {}
    if not text or "@" not in text:
        return out
    for m in MENTION_RE.finditer(text):
        if not m.group(3):
            continue
        raw = (m.group(1) or m.group(2) or "").strip()
        while raw and raw[-1] in ".,;:!?":
            raw = raw[:-1]
        if not raw:
            continue
        first = int(m.group(3))
        last = int(m.group(4)) if m.group(4) else first
        if last < first:
            first, last = last, first
        key = _normalise_rel(raw).lower()
        out.setdefault(key, (first, last))
        out.setdefault(key.rsplit("/", 1)[-1], (first, last))
    return out


# ── the reference block the agent loop injects ────────────────────────────

def _contained(real_path: str, root: str) -> bool:
    """Is `real_path` (already realpath'd) inside `root`?

    Reuses tool_execution._path_is_within_root — the resolver every file tool
    already trusts (commonpath over normcase'd paths, so it is right on Windows
    too). Imported lazily to keep this module's import graph stdlib-only; the
    fallback repeats the minimum rather than failing open.
    """
    try:
        from src.tool_execution import _path_is_within_root
        return bool(_path_is_within_root(real_path, root))
    except Exception:                                     # pragma: no cover
        try:
            a, b = os.path.normcase(real_path), os.path.normcase(os.path.realpath(root))
            return os.path.commonpath([a, b]) == b
        except (ValueError, OSError):
            return False


def _sensitive(real_path: str) -> bool:
    """tool_execution's deny-list (.ssh/, credentials, *.pem, …) on the RESOLVED
    path. `_SECRET_NAME_RE` above only looks at the mention's own name, which a
    symlink chooses freely."""
    try:
        from src.tool_execution import _is_sensitive_path
        return bool(_is_sensitive_path(real_path))
    except Exception:                                     # pragma: no cover
        return False


def _read_head(abs_path: str, budget: int) -> Optional[str]:
    try:
        with open(abs_path, "rb") as fh:
            raw = fh.read(budget + 1)
    except OSError:
        return None
    if b"\x00" in raw[:_BINARY_SNIFF]:
        return None
    txt = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(txt) > budget:
        txt = txt[:budget] + "\n… (truncated — read_file for the rest)"
    return txt


def _window(abs_path: str, span: Tuple[int, int], budget: int) -> Optional[str]:
    """The lines of `span` only, numbered — src/code_refs.py owns the renderer.

    Imported lazily: code_refs imports THIS module for its helpers, so a
    top-level import here would be a cycle.
    """
    try:
        from src.code_refs import window as _win
        first, last = span
        body = (_win(abs_path, [first, last], radius=0, budget_chars=budget)
                if last > first
                else _win(abs_path, first, radius=_MENTION_LINE_RADIUS,
                          budget_chars=budget))
        return body or None
    except Exception:                                     # pragma: no cover
        return None


def context_text(workspace: str, resolution: Dict[str, List[str]],
                 inline_chars: Optional[int] = None,
                 ranges: Optional[Dict[str, Tuple[int, int]]] = None) -> str:
    """The block injected before the user's turn. '' when there is nothing to say.

    `ranges` (from `extract_ranges`) narrows a mention to the lines it named:
    `@src/app.py:120-160` inlines that window instead of the whole file.
    """
    resolved = list(resolution.get("resolved") or [])
    missing = list(resolution.get("missing") or [])
    ambiguous = list(resolution.get("ambiguous") or [])
    if not (resolved or missing or ambiguous):
        return ""
    if inline_chars is None:
        try:
            inline_chars = int(_setting("agent_file_mention_inline_chars",
                                        _DEFAULT_INLINE_CHARS))
        except (TypeError, ValueError):
            inline_chars = _DEFAULT_INLINE_CHARS
    inline_chars = max(0, min(int(inline_chars), 60000))

    lines = [
        "The user pointed at these workspace files with @ in the message below. "
        "They are the exact files meant — do not substitute a similarly named "
        "one, and do not go looking for the right file when it is listed here. "
        "Paths are relative to the workspace root.",
        "",
    ]
    root = os.path.realpath(os.path.expanduser(workspace)) if workspace else ""
    budget = inline_chars
    for rel in resolved[:20]:
        abs_path = os.path.join(root, *rel.split("/")) if root else ""
        # RESOLVE BEFORE READING. The index comes from os.walk, which reports a
        # symlink-to-a-file as an ordinary file, and every filter above matches
        # on the mention's *name*. So `notas.md` could be a link to
        # ~/.aws/credentials and its whole content would ride into the prompt —
        # and on to the model endpoint, which may well be remote. Name the file
        # (the user did point at it) but never inline what the link resolves to
        # outside the workspace, or to something the file tools deny-list.
        try:
            real_path = os.path.realpath(abs_path) if abs_path else ""
        except (OSError, ValueError):                     # pragma: no cover
            real_path = ""
        escapes = bool(root) and (not real_path or not _contained(real_path, root))
        secret = bool(_SECRET_NAME_RE.search(rel)) or (bool(real_path) and _sensitive(real_path))
        try:
            size = -1 if escapes else os.path.getsize(real_path or abs_path)
        except OSError:
            size = -1
        # Inline only whole files that fit in what is left of the budget: a
        # 200-char head of a 12 kB file is noise, and the point of inlining is
        # to save the model a read_file round it would otherwise still need.
        span = None
        if ranges:
            span = ranges.get(rel.lower()) or ranges.get(rel.rsplit("/", 1)[-1].lower())
        # A named line wins over the size rule: the point of `@src/app.py:900`
        # is precisely that the file is too big to hand over whole.
        fits = (not secret) and (not escapes) and (
            span is not None or 0 <= size <= min(budget, _MAX_INLINE_FILE_CHARS))
        note = ""
        if escapes:
            note = (" — this name is a link that resolves outside the workspace; "
                    "contents not included here")
        elif secret:
            note = " — contents not included here; read_file it if you need them"
        elif size > 0 and not fits and budget > 0:
            note = " — too large to inline here, read_file it"
        elif span:
            note = (f" — lines {span[0]}-{span[1]} below" if span[1] > span[0]
                    else f" — around line {span[0]} below")
        lines.append(f"- {rel}" + (f" ({size} bytes)" if size >= 0 else "") + note)
        if fits:
            # Read the RESOLVED path: it is the one just proven contained.
            body = (_window(real_path or abs_path, span, max(200, budget)) if span
                    else _read_head(real_path or abs_path, _MAX_INLINE_FILE_CHARS))
            if body is not None:
                budget -= len(body)
                lang = os.path.splitext(rel)[1].lstrip(".") or ""
                lines += ["", f"```{lang}", body.rstrip("\n"), "```", ""]
    if len(resolved) > 20:
        lines.append(f"- … {len(resolved) - 20} more mentioned file(s)")
    if ambiguous:
        lines += ["", "Mentioned by name only, and the workspace has more than one "
                      "file with that name — ask which one, or list them: "
                  + ", ".join(ambiguous[:10])]
    if missing:
        lines += ["", "Mentioned but NOT present in the workspace: "
                  + ", ".join(missing[:10])
                  + ". Say so plainly instead of editing a different file."]
    if budget < inline_chars:
        lines += ["", "The contents above are the files as they are on disk right "
                      "now; you do not need to read_file them again unless you "
                      "want a part that was truncated."]
    return "\n".join(lines)


def turn_context(workspace: str, user_text: str) -> Tuple[str, Dict[str, List[str]]]:
    """(block, resolution) for one turn. ('' , {}) when the feature is off."""
    if not enabled() or not workspace or not user_text:
        return "", {"resolved": [], "missing": [], "ambiguous": []}
    res = resolve(workspace, user_text)
    return context_text(workspace, res, ranges=extract_ranges(user_text)), res
