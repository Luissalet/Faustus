"""
src/context_engine — the one place that decides what a model is told.

Faustus never lacked storage.  It had learned memory with maturity and
anti-patterns, project memory in `.odysseus/`, objectives with a typed log,
document RAG, expert corpora, a provenance graph, changesets and a `prove`
verdict.  What it lacked was a single answer to the question every one of those
systems was quietly answering on its own, in its own format, with its own idea
of a budget:

    what does THIS actor need for THIS step, inside THIS window, and how would
    we later prove where each sentence came from?

So this package adds no new store of record.  `memory_engine` still owns
learned rules; `objectives` still owns goal state; `prove` still owns verdicts;
the files on disk are still the truth about the code.  What lives here is the
layer above them: adapters that turn each source into `ContextCandidate`s, a
compiler that validates, deduplicates, ranks, budgets and trims them into one
`ContextPacket`, and a receipt that records what actually got used so the
selection can be measured instead of admired.

Three things are load-bearing and worth stating once:

**One compiler.**  If a second consumer starts assembling its own megaprompt
out of memory plus project rules plus state, the guarantees here are void — not
because the code breaks, but because "why did it know that?" stops having an
answer.

**Omissions are part of the output.**  A packet says what it left out and why.
The failure this subsystem exists to prevent is not "the model was not told
enough"; it is "nobody can tell what the model was told".

**Degradation is declared, never silent.**  Embeddings down, index stale, a
source unavailable: the lexical lane keeps working and the packet says it is
running on one leg.  A confident answer built on half a retrieval is the most
expensive failure mode in the whole system.

Imports are deliberately shallow here.  `contracts`, `budgets` and `store` are
the light core and are re-exported; everything else — adapters, the compiler,
the code index — is imported from its own module by whoever needs it, because
pulling `rag_vector`, `memory_engine` and `provenance_graph` into every
`import src.context_engine` would put a second of import time on the turn path
of processes that only wanted a dataclass.
"""

from .contracts import (  # noqa: F401
    AUTHORITY_ORDER,
    CONSUMERS,
    FEEDBACK_KINDS,
    GENERATIVE_TRANSFORMATIONS,
    MANDATORY_SECTIONS,
    OMISSION_REASONS,
    RETRIEVAL_LANES,
    SECTION_KINDS,
    SOURCE_TYPES,
    TASK_PHASES,
    TRANSFORMATIONS,
    TRIM_POLICIES,
    TRUST_CLASSES,
    ContextActor,
    ContextBudget,
    ContextCandidate,
    ContextExecution,
    ContextItem,
    ContextOmission,
    ContextPacket,
    ContextPolicy,
    ContextReceipt,
    ContextRequest,
    ContextSection,
    ContextTask,
    new_id,
)
from .budgets import (  # noqa: F401
    PROFILES,
    BudgetProfile,
    allocate,
    app_parity_estimator,
    estimator_for,
    profile_for,
    resolve_budget,
    section_order,
    tool_schema_tokens,
)
from .store import (  # noqa: F401
    ContextStoreError,
    db as store_db,
    db_path as store_path,
    register_schema,
)

__all__ = [
    "SECTION_KINDS", "MANDATORY_SECTIONS", "SOURCE_TYPES", "TRANSFORMATIONS",
    "GENERATIVE_TRANSFORMATIONS", "OMISSION_REASONS", "RETRIEVAL_LANES",
    "TASK_PHASES", "CONSUMERS", "FEEDBACK_KINDS", "TRIM_POLICIES",
    "TRUST_CLASSES", "AUTHORITY_ORDER",
    "ContextActor", "ContextExecution", "ContextTask", "ContextPolicy",
    "ContextRequest", "ContextCandidate", "ContextItem", "ContextSection",
    "ContextOmission", "ContextBudget", "ContextPacket", "ContextReceipt",
    "new_id",
    "BudgetProfile", "PROFILES", "profile_for", "resolve_budget", "allocate",
    "estimator_for", "app_parity_estimator", "tool_schema_tokens", "section_order",
    "ContextStoreError", "register_schema", "store_db", "store_path",
]
