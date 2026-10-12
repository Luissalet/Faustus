"""Observations: what each tool call taught the agent, kept and found again.

After a useful tool call of the agent loop (or of a sub-agent, which runs the
same loop) the small local model writes ONE structured note: a type, a title, a
subtitle, self-contained facts, a short narrative, concepts and the files read
and modified. At the end of a turn it writes one progress summary from that
turn's notes. Both are stored in ``OBSERVATIONS_DB`` with a full-text index.

Finding them again is progressive, so a model spends tokens only on what it
chose to open (``retrieval``):

    search(query)               -> [{id, created, type, title}]
    timeline(anchors, 2, 2)     -> chronological neighbours of the anchors
    details(ids)                -> full records, at most five

Plus ``observations_for_file(path)`` and ``context_block(project, max_chars)``.

Modules:

* ``schema``    — the note's shape and the strict parser of the model's reply;
* ``capture``   — the deterministic pre-filter and the condensed job;
* ``store``     — SQLite + FTS5 (``LIKE`` fallback);
* ``worker``    — bounded queue, one daemon thread, admission, the model call;
* ``retrieval`` — the three steps, the file lookup and the session recap;
* ``listeners`` — the lifecycle-hook callbacks; :func:`install` registers them.

Settings: the ``agent_observations_*`` group (Settings > Agent Tools). The
agent loop never waits on any of this: see ``worker`` for the admission rules.
"""
from __future__ import annotations

from .listeners import install, uninstall
from .retrieval import (
    build_context,
    context_block,
    details,
    file_context_text,
    observations_for_file,
    recent,
    search,
    timeline,
)

__all__ = [
    "install", "uninstall", "search", "timeline", "details", "recent",
    "observations_for_file", "file_context_text", "context_block", "build_context",
]
