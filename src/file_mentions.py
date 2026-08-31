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
)

_MAX_RESULTS = 200
_DEFAULT_INLINE_CHARS = 6000
_MAX_INLINE_FILE_CHARS = 12000
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

def extract(text: str) -> List[str]:
    """The raw mention bodies in `text`, in order, de-duplicated."""
    if not text or "@" not in text:
        return []
    seen: List[str] = []
    for m in MENTION_RE.finditer(text):
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
        rel = raw.replace("\\", "/").lstrip("./")
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


def _add(lst: List[str], item: str) -> None:
    if item not in lst:
        lst.append(item)


def strip_markers(text: str) -> str:
    """`@src/x.py` → `src/x.py`, for prompts that read better without the sigil."""
    return MENTION_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), text or "")


# ── the reference block the agent loop injects ────────────────────────────

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


def context_text(workspace: str, resolution: Dict[str, List[str]],
                 inline_chars: Optional[int] = None) -> str:
    """The block injected before the user's turn. '' when there is nothing to say."""
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
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = -1
        # Inline only whole files that fit in what is left of the budget: a
        # 200-char head of a 12 kB file is noise, and the point of inlining is
        # to save the model a read_file round it would otherwise still need.
        fits = 0 <= size <= min(budget, _MAX_INLINE_FILE_CHARS)
        note = ""
        if size > 0 and not fits and budget > 0:
            note = " — too large to inline here, read_file it"
        lines.append(f"- {rel}" + (f" ({size} bytes)" if size >= 0 else "") + note)
        if fits:
            body = _read_head(abs_path, _MAX_INLINE_FILE_CHARS)
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
    return context_text(workspace, res), res
