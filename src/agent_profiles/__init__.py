"""
src/agent_profiles — how far a run goes, and what it was allowed to do.

The decision this package is built on is a decision NOT to build something:
there is no second catalogue of agents here. `src/agent_defs.py` already says
who works — identity, model, runner, tools, permissions — and a parallel store
of "mission profiles" would have made two answers to "which agent is this?",
which is one more than any system can keep honest (§31).

What lives here is everything orthogonal to identity:

    contracts.py    the resolved execution, its permissions envelope, and the
                    vocabulary (completion modes, capabilities, precedence)
    completion.py   what each completion mode means and which level set it

An `AgentDef` says WHO. A completion mode says HOW FAR. Neither says the
other, and no file in this package may grant a permission — that is
`src/subagent_permissions.py` and the policy engine, unchanged.

Deliberately empty of imports: several modules land in this package over the
course of the plan, and a package `__init__` that pulls all of them makes the
import of any one of them the import of all. Import what you need directly:
``from src.agent_profiles.contracts import ResolvedAgentExecution``.
"""
from __future__ import annotations

__all__: list = []
