"""Query-ranked repository rationale from Keep the Why's context/*.md.

These are historical claims, never binding instructions. No index, database,
automatic capture or linter dependency; existing file tools can maintain them.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Sequence

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate
from ..planner import classify_intent
from .projects import _bm25

CODE_INTENTS = frozenset({"code_change", "bugfix", "implement", "review", "code_review"})
MAX_FILES = 64
MAX_CHARS = 12_000


class RepositoryRationaleSource(ThreadedSource):
    source_id = "repository_rationale"
    sections = ("retrieved_documents",)
    handles = ("rationale:",)

    def available(self) -> bool:
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_project_sources:
            return "project sources disabled"
        if not req.workspace or not req.wants("retrieved_documents"):
            return "no workspace or rationale section"
        if classify_intent(req.request) not in CODE_INTENTS:
            return "not a code or review task"
        if not req.query.strip() or not req.allows("lexical"):
            return "no lexical query"
        return ""

    def _fetch(self, source_ref: str, req: RetrievalRequest):
        prefix = "rationale:context/"
        name = source_ref[len(prefix):] if source_ref.startswith(prefix) else ""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.md", name) or name.lower() == "index.md":
            return None
        rows = self._search(req, filename=name)
        return rows[0] if rows else None

    def _search(self, req: RetrievalRequest, *, filename: str = "") -> Sequence[ContextCandidate]:
        import src.file_mentions as guards
        from src.contracts.base import now_iso

        root = os.path.realpath(req.workspace)
        base = os.path.realpath(os.path.join(root, "context"))
        if not guards._contained(base, root) or not os.path.isdir(base):
            return ()
        bodies = {}
        # Bound directory enumeration as well as file reads, fail closed for
        # huge directories instead of reading whichever entries happen to win.
        with os.scandir(base) as entries:
            names = []
            for i, entry in enumerate(entries):
                if i >= MAX_FILES * 4:
                    return ()
                if entry.name.lower().endswith(".md") and entry.name.lower() != "index.md":
                    names.append(entry.name)
        for name in sorted(names)[:MAX_FILES]:
            if filename and name != filename:
                continue
            path = os.path.realpath(os.path.join(base, name))
            if (not guards._contained(path, root) or not guards._contained(path, base)
                    or guards._sensitive(path) or guards._SECRET_NAME_RE.search(name)):
                continue
            try:
                if not os.path.isfile(path) or os.stat(path).st_size > MAX_CHARS:
                    continue
                body = guards._read_head(path, MAX_CHARS)
            except OSError:
                continue
            if body and body.strip():
                bodies[name] = body
        scores = ({filename: 1.0} if filename else _bm25(req.query, list(bodies.items())))
        ranked = sorted((n for n in bodies if scores.get(n, 0) > 0),
                        key=lambda n: (-scores[n], n))
        out = []
        for name in ranked[:min(req.top(), 3)]:
            body = bodies[name]
            candidate = make_candidate(
                source_type="file", source_ref=f"rationale:context/{name}",
                section="retrieved_documents", title=f"context/{name} (historical rationale)",
                body=body, lanes=(("explicit",) if filename else ("lexical",)), scores={"lexical": scores[name]},
                trust_class="untrusted", authority="agent_claim",
                owner=req.owner, project_id=req.project_id, observed_at=now_iso(),
                source_revision="captured_utf8_sha256:" + hashlib.sha256(body.encode()).hexdigest(),
                meta={"path": f"context/{name}", "instruction_authority": False,
                      "format": "keep-the-why", "claims_require_verification": True},
            )
            if candidate is not None:
                out.append(candidate)
        return tuple(out)
