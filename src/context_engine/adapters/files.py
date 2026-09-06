"""
context_engine/adapters/files.py — the file as it is on disk right now.

Everything else in this package retrieves something somebody wrote down
earlier.  This one reads the workspace, which makes it the only source whose
candidates are ``observed``: the bytes were on disk a millisecond ago, so when
a memory claims the function is called ``load_all`` and the file says
``load_state``, §14.2 has to let the file win.  ``authority="observed_state"``
is that rule, encoded once.

The guards are **imported, not reimplemented**.  ``src/file_mentions.py``
already learned, the expensive way, what it costs to inline a file a user
named: the workspace index comes from ``os.walk``, which reports a symlink to a
file as an ordinary file, and every name-based filter matches on the *mention*,
so ``notas.md`` can be a link to ``~/.ssh/id_rsa`` whose entire content would
otherwise ride into a prompt and on to a model endpoint that may well be
remote.  ``_contained`` (realpath containment, via the same
``tool_execution._path_is_within_root`` every file tool trusts), ``_sensitive``
(the resolved-path deny-list) and ``_SECRET_NAME_RE`` (the name-level one) are
that lesson.  A second copy of them here would be a second copy to forget to
update.

A file that fails a guard is still **named** — the user pointed at it, and
"there is a file here I will not show you" is information — but its body is
empty and ``meta["withheld"]`` says why.  The compiler can render that as a
reference the model can open with a tool if it is entitled to; what it cannot
do is quietly pretend the file was not mentioned.

Two ways in, both from the runtime:

* ``explicit_refs`` of the form ``file:<relative/path>`` or
  ``file:<relative/path>#L10-L40``.
* ``@mentions`` in the round's query, resolved by ``file_mentions.resolve``,
  which handles the exact / case-insensitive / unique-basename cascade and
  reports an ambiguous basename instead of guessing.  Guessing which file the
  user meant is the substitution failure that feature exists to prevent.

``source_ref`` scheme: ``file:<relative/path>`` or, when a range was named,
``file:<relative/path>#L<first>-L<last>``.  Paths are workspace-relative and
forward-slashed, so the reference means the same thing on both platforms.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

REF_PREFIX = "file:"

#: What one file may contribute before it stops being context and starts being
#: a dataset.  `file_mentions` uses the same ceiling for the same reason.
MAX_FILE_CHARS = 12_000


def _parse_ref(ref: str) -> Tuple[str, Optional[Tuple[int, int]]]:
    """``file:src/app.py#L10-L40`` -> ``("src/app.py", (10, 40))``.

    A malformed range is dropped rather than rejected: the file is still the
    file the caller named, and refusing it over a typo in the line numbers
    would be the pedantic reading.
    """
    body = ref[len(REF_PREFIX):] if ref.startswith(REF_PREFIX) else ref
    rel, _, fragment = body.partition("#")
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if not fragment:
        return rel, None
    marks = fragment.upper().replace("L", "").split("-")
    try:
        first = int(marks[0])
        last = int(marks[1]) if len(marks) > 1 and marks[1] else first
    except (TypeError, ValueError, IndexError):
        return rel, None
    if first < 1 or last < first:
        return rel, None
    return rel, (first, last)


def _now() -> str:
    from src.contracts.base import now_iso
    return now_iso()


class FileSource(ThreadedSource):
    """Workspace files the request named, read now, guarded by file_mentions."""

    source_id = "files"
    sections = ("code_map",)
    handles = (REF_PREFIX,)

    def available(self) -> bool:
        try:
            import src.file_mentions  # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("file_mentions unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_project_sources:
            return "policy.allow_project_sources is false"
        if not req.wants("code_map"):
            return "code_map not requested"
        if not (req.allows("explicit") or req.allows("exact")):
            return "neither the explicit nor the exact lane is open"
        if not req.workspace:
            # A relative path with no root is not a file, it is a string.
            return "no workspace on the request"
        return ""

    # ── what was named ─────────────────────────────────────────────────────

    def _targets(self, req: RetrievalRequest
                 ) -> List[Tuple[str, Optional[Tuple[int, int]], str]]:
        """``[(relative path, line span or None, lane)]``, explicit refs first.

        Explicit refs are trusted to name a real relative path and are still
        resolved through the same guards below; ``@mentions`` go through
        ``file_mentions.resolve`` first, which is what turns ``@app.py`` into
        the one file in the workspace that it can only be.
        """
        import src.file_mentions as mentions

        out: List[Tuple[str, Optional[Tuple[int, int]], str]] = []
        seen: set = set()
        for ref in req.refs_for(REF_PREFIX):
            rel, span = _parse_ref(ref)
            if rel and rel not in seen:
                seen.add(rel)
                out.append((rel, span, "explicit"))

        query = str(req.query or "")
        if query and req.allows("exact"):
            try:
                resolution = mentions.resolve(req.workspace, query)
                ranges = mentions.extract_ranges(query)
            except Exception as exc:                           # noqa: BLE001
                logger.debug("file mention resolution failed: %s", exc)
                return out
            for rel in list(resolution.get("resolved") or []):
                if rel in seen:
                    continue
                seen.add(rel)
                span = (ranges.get(rel.lower())
                        or ranges.get(rel.rsplit("/", 1)[-1].lower()))
                out.append((rel, span, "exact"))
        return out

    # ── reading one, safely ────────────────────────────────────────────────

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        import src.file_mentions as mentions

        targets = self._targets(req)
        if not targets:
            return ()
        try:
            root = os.path.realpath(os.path.expanduser(req.workspace))
        except (OSError, ValueError):
            return ()

        out: List[ContextCandidate] = []
        for rel, span, lane in targets[:req.top()]:
            candidate = self._read(mentions, root, rel, span, lane, req)
            if candidate is not None:
                out.append(candidate)
        return tuple(out)

    def _read(self, mentions: Any, root: str, rel: str,
              span: Optional[Tuple[int, int]], lane: str,
              req: RetrievalRequest) -> Optional[ContextCandidate]:
        abs_path = os.path.join(root, *rel.split("/"))
        try:
            real_path = os.path.realpath(abs_path)
        except (OSError, ValueError):
            real_path = ""

        # RESOLVE BEFORE READING, and in this order: containment first, because
        # a path that escaped the workspace must not even be stat'ed against the
        # deny-list, and the name pattern last, because a symlink picks its own
        # name freely.
        withheld = ""
        if not real_path or not mentions._contained(real_path, root):
            withheld = "outside_workspace"
        elif mentions._SECRET_NAME_RE.search(rel) or mentions._sensitive(real_path):
            withheld = "sensitive"

        size = -1
        modified: Any = ""
        if not withheld:
            try:
                stat = os.stat(real_path)
                size, modified = stat.st_size, stat.st_mtime
            except OSError as exc:
                logger.debug("file %s not readable: %s", rel, exc)
                withheld = "unreadable"

        body = ""
        if not withheld:
            if span is not None:
                body = mentions._window(real_path, span, MAX_FILE_CHARS) or ""
            elif 0 <= size <= MAX_FILE_CHARS:
                body = mentions._read_head(real_path, MAX_FILE_CHARS) or ""
            else:
                # A 200-character head of a 400 kB file is noise, and the point
                # of inlining is to save a read_file the model would otherwise
                # still have to make.
                withheld = "too_large"

        ref = f"{REF_PREFIX}{rel}"
        if span is not None:
            ref += f"#L{span[0]}-L{span[1]}"
        where = f" (lines {span[0]}-{span[1]})" if span else ""
        return make_candidate(
            source_type="file",
            source_ref=ref,
            section="code_map",
            title=f"{rel}{where}",
            body=body,
            lanes=(lane,),
            trust_class="observed",
            authority="observed_state",
            source_revision=f"{size}:{int(modified) if modified else 0}",
            # Read now, so "now" is the honest observation time; the file's own
            # mtime lives in meta, where it cannot be mistaken for it.
            observed_at=_now(),
            owner=req.owner,
            project_id=req.project_id,
            meta={"path": rel, "bytes": size, "modified": modified,
                  "lines": list(span) if span else [],
                  **({"withheld": withheld} if withheld else {})},
        )

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        import src.file_mentions as mentions

        ref = str(source_ref or "")
        if not ref.startswith(REF_PREFIX) or not req.workspace:
            return None
        rel, span = _parse_ref(ref)
        if not rel:
            return None
        try:
            root = os.path.realpath(os.path.expanduser(req.workspace))
        except (OSError, ValueError):
            return None
        return self._read(mentions, root, rel, span, "exact", req)


__all__ = ["FileSource", "REF_PREFIX", "MAX_FILE_CHARS"]
