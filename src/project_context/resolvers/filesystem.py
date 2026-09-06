"""
project_context/resolvers/filesystem.py — files and folders as project sources.

The guards are **not reimplemented here**. ``src.tool_execution.vet_project_root``
already decides what a project may be pointed at (it accepts files as well as
directories, and refuses sensitive locations and filesystem roots), and
``src.tool_execution._path_is_within_root`` already decides containment. A
second, subtly different copy of either is how one of them ends up fixed and
the other does not — so this module calls them and adds exactly one rule of its
own: a raw path containing a ``..`` segment is refused *before* it is resolved.

That last rule is not redundant. ``realpath`` collapses ``..`` happily, so a
traversal that lands on an accepted directory is accepted; refusing the syntax
means a request nobody meant to make cannot become a request that happens to
be legal.

Obligations of every resolver (see ``base.py``): owner checked before the
source is touched; ``missing``/``forbidden`` leak nothing; stable revision;
bounded reads; a stored label is never access control.

Attaching does not widen the run's work roots. Membership in a project's
context and permission to edit a file are different things (plan §8); the link
carries ``access_mode`` and this resolver only ever reads.

Revision, and why it is two rules
---------------------------------
* A file of at most ``SHA_MAX_BYTES`` (1 MiB) hashes its content:
  ``file:<path>:sha256:<hex>``. Content addressing survives a ``touch``, a
  restore-from-backup and a copy that resets the mtime — all of which would
  otherwise force a needless reindex.
* Anything larger uses ``file:<path>:<mtime_ns>:<size>``, because hashing a
  4 GB video on every revision check costs more than the false invalidation it
  prevents.
* A folder hashes its *shape*, not its contents: the sorted
  ``(relative path, size, mtime_ns)`` of the entries the index walk would keep,
  capped at ``MAX_WALK_ENTRIES``. Editing one file inside a linked folder does
  change its mtime, so the shape hash still moves.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, List, Mapping, Optional, Tuple

from ..models import (
    ExtractedChunk, ExtractedCorpus, SourceContent, SourceMatch, SourceMetadata, SourceRef,
)
from .base import MAX_READ_CHARS, ResolverBase

logger = logging.getLogger(__name__)

__all__ = ["FilesystemResolver", "MAX_WALK_ENTRIES", "SHA_MAX_BYTES"]

#: Content-hash a file up to this size; fall back to mtime+size above it.
SHA_MAX_BYTES = 1024 * 1024

#: Ceilings on a folder walk. A linked folder is a knowledge source, not a
#: filesystem crawl: past these the answer is honestly truncated rather than
#: slowly complete.
MAX_WALK_ENTRIES = 2000
MAX_EXTRACT_FILES = 200
MAX_EXTRACT_BYTES = 2_000_000

#: Extensions read as text. Everything else is described, never decoded — a
#: JPEG decoded with ``errors="ignore"`` is 400 KB of mojibake in a prompt.
TEXT_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".sql", ".py", ".js", ".ts",
    ".tsx", ".jsx", ".sh", ".ps1", ".bat", ".rs", ".go", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".html", ".css", ".scss", ".xml",
    ".patch", ".diff", ".gradle", ".properties",
})

_MEDIA_BY_EXT = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".json": "application/json",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".html": "text/html",
    ".xml": "application/xml", ".yaml": "application/yaml", ".yml": "application/yaml",
}


def _is_text(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in TEXT_EXTS


def _media_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _MEDIA_BY_EXT:
        return _MEDIA_BY_EXT[ext]
    return "text/plain" if ext in TEXT_EXTS else "application/octet-stream"


def _read_text(path: str, limit: int = MAX_READ_CHARS) -> str:
    """Read at most ``limit`` characters, or ``""`` for anything unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError as exc:
        logger.debug("project_context filesystem read failed: %s", exc)
        return ""


def has_traversal(raw: str) -> bool:
    """True when any segment of the raw path is ``..``.

    Checked on the *raw* string, before ``realpath`` has a chance to make it
    innocent. Both separators are examined because a Windows path may use
    either and a POSIX-looking ``../`` inside a Windows path still traverses.
    """
    return any(part == ".." for part in str(raw or "").replace("\\", "/").split("/"))


class FilesystemResolver(ResolverBase):
    """Resolver for ``kind='file'`` and ``kind='folder'``.

    One class, two registered instances: the questions are the same and only
    the shape of the answer differs, so a single implementation cannot drift
    between them.
    """

    def __init__(self, kind: str = "file") -> None:
        if kind not in ("file", "folder"):
            raise ValueError(f"FilesystemResolver handles file and folder, not {kind!r}")
        self.kind = kind

    # ── plumbing ───────────────────────────────────────────────────────────

    @staticmethod
    def _guards():
        # Late import: ``src.tool_execution`` pulls in the tool runtime, and the
        # contracts package must stay importable without it.
        from src.tool_execution import _path_is_within_root, vet_project_root
        return vet_project_root, _path_is_within_root

    def _resolve(self, ref: SourceRef) -> "Tuple[Optional[str], str, str]":
        """``(resolved_path, state, note)``. The path is None unless state is ok."""
        raw = (getattr(ref, "path", "") or "").strip()
        if not raw:
            return None, "missing", "no path was given"
        if has_traversal(raw):
            return None, "forbidden", "a path with a '..' segment is not accepted"
        vet_project_root, _ = self._guards()
        try:
            expanded = os.path.realpath(os.path.expanduser(raw))
        except (OSError, ValueError):
            return None, "missing", "the path could not be resolved"
        if not os.path.exists(expanded):
            return None, "missing", "not found"
        vetted = vet_project_root(raw)
        if not vetted:
            return None, "forbidden", "a sensitive location or a filesystem root"
        if self.kind == "folder" and not os.path.isdir(vetted):
            return None, "unsupported", "a folder link needs a directory"
        if self.kind == "file" and os.path.isdir(vetted):
            return None, "unsupported", "a file link needs a file"
        return vetted, "ok", ""

    def _within(self, root: str, candidate: str) -> bool:
        _, _path_is_within_root = self._guards()
        try:
            return bool(_path_is_within_root(candidate, root))
        except (OSError, ValueError):
            return False

    def _walk(self, root: str, *, cap: int = MAX_WALK_ENTRIES) -> "Tuple[List[str], bool]":
        """Relative paths under ``root``, pruned by the shared index policy.

        ``src.index_walk`` is the single source of that policy, shared with the
        vector and keyword indexers. A private copy here is how ``.git/`` came
        back into one of them once already.
        """
        from src.index_walk import is_indexable_file, prune_index_dirs

        found: List[str] = []
        truncated = False
        for dirpath, dirs, files in os.walk(root, topdown=True):
            prune_index_dirs(dirs)
            for name in sorted(files):
                if not is_indexable_file(name):
                    continue
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, root))
                if len(found) >= cap:
                    truncated = True
                    break
            if truncated:
                break
        found.sort()
        return found, truncated

    @staticmethod
    def _stat(path: str) -> "Tuple[int, int]":
        try:
            info = os.stat(path)
            return int(info.st_size), int(getattr(info, "st_mtime_ns", 0))
        except OSError:
            return 0, 0

    # ── the protocol ───────────────────────────────────────────────────────

    def validate(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        return self.metadata(ref, owner=owner, project=project)

    def metadata(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        # Owner first, before the disk is touched. A path has no owner column,
        # so the check is that there *is* an effective owner: an ownerless
        # caller must not be able to probe the filesystem through a project.
        if not (owner or "").strip():
            return self._denied("forbidden", "no effective owner for this request")
        resolved, state, note = self._resolve(ref)
        if resolved is None:
            return self._denied(state, note)
        size, mtime_ns = self._stat(resolved)
        if self.kind == "folder":
            entries, truncated = self._walk(resolved)
            return SourceMetadata(
                state="ok", kind=self.kind, canonical_ref=f"folder:{resolved}",
                label=os.path.basename(resolved) or resolved,
                media_type="inode/directory", byte_size=0, owner=owner,
                revision=self._folder_revision(resolved, entries),
                version=0,
                extra={"entries": len(entries), "truncated": truncated,
                       "mtime_ns": mtime_ns},
            )
        return SourceMetadata(
            state="ok", kind=self.kind, canonical_ref=f"file:{resolved}",
            label=os.path.basename(resolved) or resolved,
            media_type=_media_type(resolved), byte_size=size, owner=owner,
            revision=self._file_revision(resolved, size, mtime_ns), version=0,
            extra={"mtime_ns": mtime_ns, "text": _is_text(resolved)},
        )

    def _file_revision(self, path: str, size: int, mtime_ns: int) -> str:
        if size <= SHA_MAX_BYTES:
            digest = hashlib.sha256()
            try:
                with open(path, "rb") as fh:
                    digest.update(fh.read(SHA_MAX_BYTES + 1))
            except OSError:
                return f"file:{path}:{mtime_ns}:{size}"
            return f"file:{path}:sha256:{digest.hexdigest()}"
        return f"file:{path}:{mtime_ns}:{size}"

    def _folder_revision(self, root: str, entries: List[str]) -> str:
        digest = hashlib.sha256()
        for rel in entries:
            size, mtime_ns = self._stat(os.path.join(root, rel))
            digest.update(f"{rel}\0{size}\0{mtime_ns}\n".encode("utf-8", "replace"))
        return f"folder:{root}:{len(entries)}:{digest.hexdigest()}"

    def revision(self, ref: SourceRef, *, version_policy: str = "latest",
                 pinned_version: Optional[int] = None) -> str:
        resolved, state, _ = self._resolve(ref)
        if resolved is None or state != "ok":
            return ""
        if self.kind == "folder":
            entries, _ = self._walk(resolved)
            return self._folder_revision(resolved, entries)
        size, mtime_ns = self._stat(resolved)
        return self._file_revision(resolved, size, mtime_ns)

    def read(self, ref: SourceRef, *, start: int = 0, limit: int = 0,
             version_policy: str = "latest",
             pinned_version: Optional[int] = None) -> SourceContent:
        """A window of a file, or the *listing* of a folder.

        A folder is never read whole. Concatenating a documentation tree into
        one string is the failure this policy exists to prevent: it is both a
        context-window disaster and a way to smuggle a file the caller never
        asked about into a prompt. ``extract()`` is the one that walks, and it
        walks under caps.
        """
        resolved, state, _ = self._resolve(ref)
        if resolved is None or state != "ok":
            return SourceContent()
        if self.kind == "folder":
            entries, truncated = self._walk(resolved)
            lines = []
            for rel in entries:
                size, _ = self._stat(os.path.join(resolved, rel))
                lines.append(f"{rel}\t{size}")
            body = "\n".join(lines)
            window, begin, end, total, cut = self._window(body, start, limit)
            return SourceContent(text=window, start=begin, end=end, total=total,
                                 truncated=cut or truncated,
                                 revision=self._folder_revision(resolved, entries),
                                 media_type="text/tab-separated-values")
        size, mtime_ns = self._stat(resolved)
        revision = self._file_revision(resolved, size, mtime_ns)
        if not _is_text(resolved):
            return SourceContent(text="", start=0, end=0, total=size, truncated=False,
                                 revision=revision, media_type=_media_type(resolved))
        body = _read_text(resolved, MAX_READ_CHARS + max(0, int(start or 0)))
        window, begin, end, total, cut = self._window(body, start, limit)
        return SourceContent(text=window, start=begin, end=end, total=total,
                             truncated=cut, revision=revision,
                             media_type=_media_type(resolved))

    def search(self, ref: SourceRef, query: str, *, limit: int = 20) -> List[SourceMatch]:
        resolved, state, _ = self._resolve(ref)
        if resolved is None or state != "ok":
            return []
        if self.kind == "file":
            if not _is_text(resolved):
                return []
            size, mtime_ns = self._stat(resolved)
            revision = self._file_revision(resolved, size, mtime_ns)
            body = _read_text(resolved)
            hits = self._matches(body, query, limit,
                                 lambda line: {"path": resolved, "line": line})
            return [SourceMatch(location=h.location, snippet=h.snippet, score=h.score,
                                revision=revision) for h in hits]
        entries, _ = self._walk(resolved)
        out: List[SourceMatch] = []
        remaining = limit if limit > 0 else 20
        for rel in entries:
            if remaining <= 0:
                break
            full = os.path.join(resolved, rel)
            # Containment is re-checked per file: a symlink inside a linked
            # folder must not become a way out of it.
            if not _is_text(full) or not self._within(resolved, full):
                continue
            hits = self._matches(_read_text(full), query, remaining,
                                 lambda line, r=rel: {"path": r, "line": line})
            out.extend(hits)
            remaining -= len(hits)
        return out

    def extract(self, ref: SourceRef, *, version_policy: str = "latest",
                pinned_version: Optional[int] = None) -> ExtractedCorpus:
        resolved, state, note = self._resolve(ref)
        if resolved is None or state != "ok":
            return ExtractedCorpus(degraded=True, note=note or "the source could not be read")
        if self.kind == "file":
            size, mtime_ns = self._stat(resolved)
            revision = self._file_revision(resolved, size, mtime_ns)
            if not _is_text(resolved):
                return ExtractedCorpus(revision=revision, degraded=True,
                                       note="a binary file has no text to index here")
            body = _read_text(resolved)
            from .document import chunk_markdown
            chunks = tuple(
                ExtractedChunk(index=i, text=part, title=title,
                               location={"path": resolved, "chunk": i},
                               media_type=_media_type(resolved))
                for i, (title, part) in enumerate(chunk_markdown(body))
            )
            return ExtractedCorpus(revision=revision, chunks=chunks)

        entries, walk_truncated = self._walk(resolved)
        from .document import chunk_markdown
        chunks: List[ExtractedChunk] = []
        budget = MAX_EXTRACT_BYTES
        files_used = 0
        degraded = walk_truncated
        for rel in entries:
            if files_used >= MAX_EXTRACT_FILES or budget <= 0:
                degraded = True
                break
            full = os.path.join(resolved, rel)
            if not _is_text(full) or not self._within(resolved, full):
                continue
            body = _read_text(full, min(budget, MAX_READ_CHARS))
            if not body.strip():
                continue
            budget -= len(body)
            files_used += 1
            for title, part in chunk_markdown(body):
                chunks.append(ExtractedChunk(
                    index=len(chunks), text=part, title=title or rel,
                    location={"path": rel, "chunk": len(chunks)},
                    media_type=_media_type(full)))
        return ExtractedCorpus(
            revision=self._folder_revision(resolved, entries),
            chunks=tuple(chunks), degraded=degraded,
            note=(f"stopped after {files_used} files / {MAX_EXTRACT_BYTES} bytes"
                  if degraded else ""),
        )
