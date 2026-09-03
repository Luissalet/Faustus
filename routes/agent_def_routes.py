"""Agent definitions API — /api/agent-defs/* (src/agent_defs.py).

  GET /api/agent-defs        every definition this machine has: the built-ins,
                             the user's own under DATA_DIR/agents, and — only
                             for a workspace whose instruction files the user
                             has approved — the ones a repo carries. Plus the
                             files that would NOT load, each with its reason.
  GET /api/agent-defs/{slug} one of them, with its rules RESOLVED into
                             sentences and its system prompt.

Two things this API refuses to do, both for the same reason:

* it never returns a definition it could not parse as if it were fine. A file
  that would not load appears in ``errors`` with the reason, because a
  definition that vanishes from a list without a word is how someone ends up
  believing a restriction is in force that is not;
* it never presents the frontmatter as the answer. ``rules`` is the resolved
  reading — "may use only these tools", "deny write src/**", "cannot start
  another worker" — because a reader who has to compile an allowlist and an
  ordered rule list in their head will get it wrong, and the whole point of
  putting an agent in a file is that they do not have to.

The one sentence this feature must not hide travels on every payload as
``shell_note``: a path rule does not reach inside `bash` or `python`. A
definition that keeps a shell and denies a path carries that as a caveat of
its own, printed next to it.

Admin-only: a definition says what a worker on this machine may do, and the
repo lane reads files out of the linked folder.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from core.middleware import require_admin

logger = logging.getLogger(__name__)

#: Said on every payload, printed on the page, and never softened: no path
#: pattern reaches inside another program's shell.
SHELL_NOTE = ("a path rule governs the file tools. `bash` and `python` run their own shell and no "
              "pattern reaches inside one — deny the tool if the pattern has to hold.")


def _row(definition: Any, *, vocabulary: Optional[List[str]] = None,
         full: bool = False) -> Dict[str, Any]:
    from src import agent_defs
    row = definition.to_dict()
    row["rules"] = agent_defs.explain(definition, tools=vocabulary)
    if not full:
        # The prompt is the body of the file and can be long; the list does not
        # need it and a list endpoint that ships four system prompts is a list
        # endpoint nobody polls.
        row.pop("prompt", None)
    return row


def _payload(workspace: Optional[str] = None, *, slug: Optional[str] = None) -> Dict[str, Any]:
    from src import agent_defs
    from src import subagent_permissions as perms
    result = agent_defs.load_all(workspace)
    vocabulary = sorted(agent_defs.known_tools())
    if slug is not None:
        definition = result.by_slug().get(agent_defs.clean_slug(slug))
        if definition is None:
            return {}
        return {"status": "success", "agent": _row(definition, vocabulary=vocabulary, full=True),
                "shell_note": SHELL_NOTE, "max_depth": perms.max_depth(),
                "depth_setting": perms.DEPTH_SETTING}
    return {
        "status": "success",
        "agents": [_row(d, vocabulary=vocabulary) for d in result.agents],
        "errors": list(result.errors),
        "shell_note": SHELL_NOTE,
        "max_depth": perms.max_depth(),
        "depth_setting": perms.DEPTH_SETTING,
        "workspace": str(workspace or ""),
        "tools_known": len(vocabulary),
    }


def setup_agent_def_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent-defs", tags=["agent-defs"])

    @router.get("")
    async def list_defs(workspace: str = Query(default=""),
                        _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Every definition, with the files that would not load and why.

        Never fails: with no store on disk and no workspace the answer is the
        built-ins, which is the honest state of a fresh install.
        """
        return await asyncio.to_thread(_payload, workspace or None)

    @router.get("/{slug}")
    async def read_def(slug: str, workspace: str = Query(default=""),
                       _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        payload = await asyncio.to_thread(_payload, workspace or None, slug=slug)
        if not payload:
            raise HTTPException(status_code=404, detail="no such agent definition")
        return payload

    return router
