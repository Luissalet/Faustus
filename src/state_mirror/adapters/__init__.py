"""state_mirror/adapters -- one module per source of state, and no store here.

Each adapter answers one question: "what does the subsystem that already owns
this have to say right now?" It owns no data, opens no database of its own and
starts nothing. When `data/council.db` is deleted the council adapter returns
nothing on the next sweep; when a run finishes, the next sweep sees it finish.
That is the whole reason State Mirror is a projection and not a warehouse: a
warehouse would have to be kept in sync with seven subsystems, and the day it
drifted, "why did it think that?" would have no answer anybody could check.

Two conventions hold across every module here, and breaking either one turns a
projection back into a rumour.

**Heavy imports are function-local.** `import src.state_mirror.adapters` must
not pull in sqlalchemy, the council store, the session database or the media
backends. Every adapter imports its source inside the method that needs it, so
a process that never asks about renders never pays for the ORM, and a source
that fails to import costs its own adapter rather than the sweep.

**Every id scheme is documented in the module that mints it.** The entity ids,
collected here so a reader does not have to open seven files. All take the
shape `<kind>://<owner>/<namespace>/<identifier>` from `contracts.entity_id`,
and only the identifier half differs:

    run://.../dispatch:<job_id>          runs.py   (src/dispatch.py)
    run://.../agent:<run_id>             runs.py   (src/agent_runs.py)
    run://.../agent:session:<session_id> runs.py   (a busy chat with no run id)
    run://.../media:<run_id>             runs.py   (src/media_runs.py)
    run://.../bg:<job_id>                runs.py   (src/bg_jobs.py)
    approval://.../<approval_id>         approvals.py
    artifact://.../<artifact_id>         artifacts.py
    objective://.../OBJ-3                objectives.py
    council://.../<session_id>           council.py
    session://.../<session_id>           sessions.py
    project://.../<project_id|digest>    workspace.py  (git, tree, checkpoints)
    service://.../backend:<id>           services.py   (capability_registry)
    service://.../readiness:<check>      services.py   (readiness checks)
    model://.../<ollama model name>      models.py
    device://.../<hostname>              hardware.py
    connection://.../integration:<id>    connections.py
    connection://.../endpoint:<id>       connections.py

The `run` identifiers are engine-prefixed and the rest are not, for the reason
`runs.py` gives: four registries mint run ids independently and two of them
colliding would silently merge two runs into one entity. Nothing else in this
package has more than one source.

Every adapter's `source_refs` carry the same string as the identifier half,
except the runs, whose refs are `<engine>:<run_id>` -- so a projection can
always name the system it would revalidate against.

`ADAPTER_FACTORIES` is what `base.all_adapters()` instantiates. The order is
the order adapters appear in a sweep result, which matters only for the
readability of a log: nothing downstream reads it.
"""

from __future__ import annotations

from typing import Callable, Tuple

from .approvals import ApprovalsAdapter
from .artifacts import ArtifactsAdapter
from .base import (
    Scope,
    StateAdapter,
    ThreadedAdapter,
    all_adapters,
    get,
    register,
    registered,
    reset_adapters,
)
from .connections import ConnectionsAdapter
from .council import CouncilAdapter
from .hardware import HardwareAdapter
from .models import ModelsAdapter
from .objectives import ObjectivesAdapter
from .runs import RunsAdapter
from .services import ServicesAdapter
from .sessions import SessionsAdapter
from .workspace import WorkspaceAdapter

#: What `base.all_adapters()` instantiates, and therefore the ONLY thing that
#: makes an adapter real. An adapter module that exists, imports and passes its
#: own tests but is missing from this tuple is invisible to every sweep, every
#: query and every screen -- which is not a hypothetical: the five local ones
#: below were written, tested green and left out of this tuple, and the only
#: reason it was caught is that somebody went looking. `test_state_mirror_wiring`
#: now asserts that every `*Adapter` in this package is listed here.
#:
#: The order is the order adapters appear in a sweep result, which matters only
#: for the readability of a log; nothing downstream reads it. The internal
#: sources come first because they are the cheap ones -- no subprocess, no
#: filesystem walk -- so a sweep that runs out of budget has already answered
#: the questions that cost nothing.
ADAPTER_FACTORIES: Tuple[Callable[[], StateAdapter], ...] = (
    RunsAdapter,
    ApprovalsAdapter,
    ArtifactsAdapter,
    ObjectivesAdapter,
    CouncilAdapter,
    SessionsAdapter,
    WorkspaceAdapter,
    ServicesAdapter,
    ModelsAdapter,
    HardwareAdapter,
    ConnectionsAdapter,
)

__all__ = [
    "ADAPTER_FACTORIES", "all_adapters",
    "Scope", "StateAdapter", "ThreadedAdapter",
    "register", "registered", "get", "reset_adapters",
    "ApprovalsAdapter", "ArtifactsAdapter", "ConnectionsAdapter",
    "CouncilAdapter", "HardwareAdapter", "ModelsAdapter",
    "ObjectivesAdapter", "RunsAdapter", "ServicesAdapter",
    "SessionsAdapter", "WorkspaceAdapter",
]
