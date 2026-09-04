"""
workflows — processes that survive hours, restarts and a human saying "wait".

Phase 4 of the masterplan, and its stop condition is the whole design brief:
*do not advance if restarting repeats a publication, a render or an email.*
Everything here exists to make a second run of the same work harmless.

Three decisions carry it.

**The store writes before the engine acts.** A node's attempt is recorded as
`running` with its idempotency key before anything happens, and its result is
written before the next node starts. A process killed mid-node comes back to a
row that says what it was doing, not to silence.

**An idempotency key is derived from the plan, never from the clock.** Two
attempts at the same node in the same run carry the same key, so whatever
actually sends the email can refuse the second one without knowing that
workflows exist.

**Paused is not failed.** A workflow waiting on a human approval is working
correctly. Treating that as an error is how a system starts timing out the
person it is asking.

The engine is deliberately small and synchronous. n8n is a reference here, not
a thing to copy: the masterplan asks for a core that speaks Faustus's own runs,
artifacts and approvals, not a second orchestrator with its own vocabulary.
"""

from .engine import (  # noqa: F401
    NodeHandler, WorkflowEngine, ready_nodes,
)
from .store import (  # noqa: F401
    WorkflowStore,
)
from .handlers import (  # noqa: F401
    OPERATORS, default_handlers, evaluate, resolve,
)

__all__ = ["WorkflowEngine", "WorkflowStore", "NodeHandler", "ready_nodes",
           "default_handlers", "evaluate", "resolve", "OPERATORS"]
