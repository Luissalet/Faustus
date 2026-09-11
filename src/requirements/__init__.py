"""src.requirements — versioned requirements linked to a project (ADP-18/19/20).

Three modules, one job each:

  store.py     identity, versioning, sidecar parsing -- `Store`, `create`,
               `get`, `list_requirements`, `update`, `revisions`, `add_link`
  context.py   `for_task()` -- the budgeted, prioritised slice an agent gets
               instead of the whole spec
  evidence.py  `matrix()` -- the linked/implemented/tested/verified/stale
               coverage table for one requirement (or a whole project)

Re-exported here for the common `from src import requirements as req` shape
other packages in this repo use (`src.project_board`, `src.tool_approvals`).
"""

from __future__ import annotations

from src.requirements.store import (  # noqa: F401
    RequirementsError,
    NotFoundError,
    Store,
    SOURCES,
    STATUSES,
    PROPOSED_BY,
    LINK_KINDS,
    LINK_STATES,
    create,
    get,
    get_or_raise,
    list_requirements,
    revisions,
    update,
    add_link,
    list_links,
    remove_link,
    all_requirement_keys,
    read_sidecar,
    parse_sidecar,
)
from src.requirements.context import for_task, DEFAULT_BUDGET_CHARS  # noqa: F401
from src.requirements.evidence import (  # noqa: F401
    matrix,
    project_matrix,
    link_evidence,
    resolve_link_state,
    scan_implements_comments,
)
