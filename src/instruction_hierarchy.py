"""instruction_hierarchy.py — instruction files per directory, with a visible order.

A repository can keep standing instructions at its root (AGENTS.md, CLAUDE.md,
…, see ``src/project_instructions.py``) and, in a larger tree, in the
directories that have their own conventions. Putting every one of them in every
turn would bury the model in rules for code it is not touching, so this module
does the opposite: the root file stays in the system prompt as before, and a
nested file is delivered when the work reaches its directory.

Precedence, stated once and repeated wherever a nested file is shown:

1. what the user says in the conversation;
2. the instruction file in the directory closest to the file being worked on;
3. instruction files in the directories above it, up to the workspace root.

Every scope is listed root first and the closest last, and the closest wins on
a conflict. A directory contributes one file: the first existing candidate in
the configured lookup order, exactly like the root.

Trust: a nested file travels with a clone just like the root file, so it may
not reach the model through this path unless the folder's instruction files are
approved. With the setting on, nested files are part of the digest the approval
covers (``src/workspace_trust.py`` asks :func:`nested_parts`): a new or edited
nested file turns an approved folder into ``changed``, the same as a root file.
All rendering here works from the bytes captured by one
``instructions_snapshot``; nothing is reopened after the trust verdict.

Delivery goes through the tool result of the call that touched the directory
(``path_note``), once per conversation and file version, like path-scoped
project rules. The system prompt carries only an index: names, scopes, sizes
and hashes, never the text.

Off by default (``agent_instruction_hierarchy``): with it off nothing in the
prompt, the digest or the tool results changes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

KIND = "nested_instruction"
MAX_DEPTH = 6
MAX_DIRECTORIES = 2000
MAX_FILES = 64
INDEX_LINES = 20

#: Directories never scanned: vendored or generated trees whose instruction
#: files are not the project's own.
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "target", "vendor", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", ".idea", ".vscode",
})

PRECEDENCE_TEXT = (
    "Precedence: what the user says in the conversation comes first; then the instruction file "
    "in the directory closest to the file you are working on; then the files in the directories "
    "above it, up to the project root. On a conflict the closest directory wins."
)


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_instruction_hierarchy", False))
    except Exception:  # noqa: BLE001
        return False


def _candidates() -> List[str]:
    from src.project_instructions import candidate_files
    return list(candidate_files())


def _normalise(root: str) -> str:
    return os.path.realpath(os.path.expanduser(root or ""))


def scan_nested(root: str, *, _candidates_override: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Nested instruction files under ``root`` (excluding the root itself).

    Bounded by depth, directory count and file count; symlinked directories are
    not followed, so a link cannot bring in a tree from outside the workspace.
    Returns ``{"files": [{path, rel, scope, depth}], "limited": bool}`` sorted
    by relative path.
    """
    base = _normalise(root)
    out: Dict[str, Any] = {"files": [], "limited": False}
    if not base or not os.path.isdir(base):
        return out
    rels = list(_candidates_override) if _candidates_override is not None else _candidates()
    stack: List[Tuple[str, int]] = [(base, 0)]
    seen_dirs = 0
    while stack:
        directory, depth = stack.pop()
        seen_dirs += 1
        if seen_dirs > MAX_DIRECTORIES:
            out["limited"] = True
            break
        if depth > 0:
            for rel in rels:
                candidate = os.path.join(directory, rel)
                try:
                    if os.path.isfile(candidate) and not os.path.islink(candidate) \
                            and os.path.getsize(candidate) > 0:
                        out["files"].append({
                            "path": candidate,
                            "rel": os.path.relpath(candidate, base).replace(os.sep, "/"),
                            "scope": os.path.relpath(directory, base).replace(os.sep, "/"),
                            "depth": depth,
                        })
                        break
                except OSError:
                    continue
            if len(out["files"]) >= MAX_FILES:
                out["limited"] = True
                break
        if depth >= MAX_DEPTH:
            continue
        try:
            with os.scandir(directory) as it:
                children = sorted(
                    (e for e in it
                     if e.is_dir(follow_symlinks=False) and e.name not in IGNORED_DIRS
                     and not e.name.startswith(".")),
                    key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for entry in children:
            stack.append((entry.path, depth + 1))
    out["files"].sort(key=lambda f: f["rel"])
    return out


def nested_parts(root: str) -> List[Dict[str, Any]]:
    """Digest parts for the trust approval: one per nested file, rel-sorted.

    Empty when the setting is off, so every existing digest is unchanged.
    """
    if not enabled():
        return []
    try:
        from src.workspace_trust import _file_part
        base = _normalise(root)
        parts = []
        for item in scan_nested(base)["files"]:
            part = _file_part(base, item["path"])
            if part is not None:
                part["kind"] = KIND
                part["scope"] = item["scope"]
                part["depth"] = item["depth"]
                parts.append(part)
        return parts
    except Exception:  # noqa: BLE001 - never worth a turn
        logger.debug("instruction hierarchy: scan failed", exc_info=True)
        return []


def _nested(snapshot: Any) -> List[Any]:
    return sorted(
        (f for f in getattr(snapshot, "files", ()) if getattr(f, "kind", "") == KIND),
        key=lambda f: (f.rel.count("/"), f.rel))


def _scope_of(snapshot_file: Any) -> str:
    return os.path.dirname(snapshot_file.rel).replace(os.sep, "/")


def _text_of(snapshot_file: Any, limit: int) -> Tuple[str, bool]:
    text = snapshot_file.data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    truncated = len(text) > limit
    return text[:limit].strip(), truncated


def _limit() -> int:
    try:
        from src.settings import get_setting
        value = int(get_setting("agent_project_instructions_max_chars", 6000) or 6000)
    except Exception:  # noqa: BLE001
        value = 6000
    return max(500, min(value, 60_000))


def provenance(snapshot: Any) -> List[Dict[str, Any]]:
    """Where each instruction comes from and in which order it applies.

    One row per instruction file in the captured snapshot that can reach the
    model (the selected root file and, with the setting on, every nested file),
    ordered from lowest to highest precedence. No text, only identity.
    """
    rows: List[Dict[str, Any]] = []
    if snapshot is None:
        return rows
    limit = _limit()
    selected = getattr(snapshot, "selected", None)
    trust = "approved" if getattr(snapshot, "trusted", True) else "unapproved"
    files: List[Tuple[Any, str]] = []
    if selected is not None:
        files.append((selected, "root"))
    files.extend((f, "nested") for f in _nested(snapshot))
    for index, (item, source) in enumerate(files, start=1):
        text, truncated = _text_of(item, limit)
        rows.append({
            "rel": item.rel,
            "source": source,
            "scope": "." if source == "root" else _scope_of(item),
            "precedence": index,
            "sha256": hashlib.sha256(item.data).hexdigest(),
            "bytes": int(item.size),
            "chars": len(text),
            "truncated": truncated,
            "trust": trust,
            "delivery": "system_prompt" if source == "root" else "on_touch",
        })
    return rows


def index_block(snapshot: Any) -> str:
    """System-prompt index of the nested files: names, scopes and sizes, no text."""
    if snapshot is None or not enabled() or not getattr(snapshot, "trusted", True):
        return ""
    nested = _nested(snapshot)
    if not nested:
        return ""
    try:
        from src.project_instructions import _safe_rel
    except Exception:  # noqa: BLE001
        def _safe_rel(root, path):  # type: ignore[misc]
            return os.path.basename(path)
    lines = []
    for item in nested[:INDEX_LINES]:
        name = _safe_rel(snapshot.workspace, item.path)
        lines.append(f"- {name} (applies under {_scope_of(item) or '.'}/, {item.size} bytes, "
                     f"sha256 {hashlib.sha256(item.data).hexdigest()[:12]})")
    more = f"\n(+{len(nested) - INDEX_LINES} more)" if len(nested) > INDEX_LINES else ""
    return (
        "\n\n## Instructions for subdirectories\n"
        "These directories have their own instruction files. They are not included above: the text "
        "of each one is added to the result of the first tool call that touches a file in its "
        "directory. " + PRECEDENCE_TEXT + "\n" + "\n".join(lines) + more
    )


class _Delivered:
    """Which nested file versions were already shown in which conversation."""

    def __init__(self) -> None:
        self._seen: Dict[str, set] = {}
        self._lock = threading.Lock()

    def claim(self, conversation: str, key: str) -> bool:
        with self._lock:
            bucket = self._seen.setdefault(conversation, set())
            if key in bucket:
                return False
            if len(self._seen) > 500:  # bounded: forget the oldest conversation
                self._seen.pop(next(iter(self._seen)), None)
            bucket.add(key)
            return True

    def seen(self, conversation: str, key: str) -> bool:
        with self._lock:
            return key in self._seen.get(conversation, ())

    def forget(self, conversation: str) -> None:
        with self._lock:
            self._seen.pop(conversation, None)


DELIVERED = _Delivered()


def _applies(scope: str, workspace: str, target: str, *, is_dir: bool) -> bool:
    """True when ``target`` lies inside the directory ``scope`` (relative to the workspace)."""
    if not target:
        return False
    absolute = target if os.path.isabs(target) else os.path.join(workspace, target)
    try:
        absolute = os.path.normcase(os.path.realpath(absolute))
        scope_abs = os.path.normcase(os.path.realpath(os.path.join(workspace, scope)))
        return os.path.commonpath([absolute, scope_abs]) == scope_abs
    except (ValueError, OSError):
        return False


def path_note(conversation: str, snapshot: Any, paths: Sequence[str], *,
              dirs: Sequence[str] = ()) -> Tuple[str, List[Dict[str, Any]]]:
    """Text to append to a tool result that touched ``paths`` or ``dirs``.

    Every not-yet-delivered nested file whose directory contains one of them,
    root side first so the closest directory comes last, once per conversation
    and file version. Returns ``(text, receipts)``; ``("", [])`` when there is
    nothing new, the folder is not approved or the setting is off. Never raises.
    """
    try:
        if (snapshot is None or not conversation or not enabled()
                or not getattr(snapshot, "trusted", True) or not (paths or dirs)):
            return "", []
        limit = _limit()
        pieces: List[str] = []
        receipts: List[Dict[str, Any]] = []
        workspace = snapshot.workspace
        for item in _nested(snapshot):
            scope = _scope_of(item)
            if not any(_applies(scope, workspace, p, is_dir=False) for p in paths) and \
                    not any(_applies(scope, workspace, d, is_dir=True) for d in dirs):
                continue
            digest = hashlib.sha256(item.data).hexdigest()
            key = f"{item.rel}:{digest[:16]}"
            if not DELIVERED.claim(conversation, key):
                continue
            text, truncated = _text_of(item, limit)
            note = " (cut at the size limit; read the file for the rest)" if truncated else ""
            pieces.append(f"### {item.rel}{note}\n\n{text}")
            receipts.append({"rel": item.rel, "scope": scope, "sha256": digest,
                             "chars": len(text), "truncated": truncated})
        if not pieces:
            return "", []
        header = ("[Directory instructions for the file(s) you just touched -- the project's own "
                  "standing instructions for that directory, shown once per conversation. "
                  + PRECEDENCE_TEXT + "]\n\n")
        return header + "\n\n".join(pieces), receipts
    except Exception:  # noqa: BLE001 - a note never costs a tool result
        logger.debug("instruction hierarchy: path note failed", exc_info=True)
        return "", []


def instructions_for(workspace: str, path: str = "") -> Dict[str, Any]:
    """The instruction chain that applies to ``path`` (or the root), from one snapshot.

    Text is returned only when the folder's instruction files are approved;
    otherwise each row says ``withheld``. Used by the route and the MCP tool.
    """
    from src import workspace_trust
    snapshot = workspace_trust.instructions_snapshot(workspace)
    limit = _limit()
    chain: List[Dict[str, Any]] = []
    rows = provenance(snapshot)
    by_rel = {f.rel: f for f in getattr(snapshot, "files", ())}
    for row in rows:
        item = by_rel.get(row["rel"])
        if row["source"] == "nested" and path and not _applies(
                row["scope"], snapshot.workspace, path, is_dir=False):
            continue
        entry = dict(row)
        if not snapshot.trusted:
            entry["withheld"] = "the folder's instruction files are not approved"
        elif item is not None:
            entry["text"], entry["truncated"] = _text_of(item, limit)
        chain.append(entry)
    other = sum(1 for r in rows if r["source"] == "nested") - sum(
        1 for r in chain if r["source"] == "nested")
    return {
        "workspace": snapshot.workspace,
        "path": path,
        "hierarchy_enabled": enabled(),
        "state": snapshot.state,
        "trusted": bool(snapshot.trusted),
        "digest": snapshot.digest,
        "precedence": PRECEDENCE_TEXT,
        "chain": chain,
        "other_nested_files": max(0, other),
    }
