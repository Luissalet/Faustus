"""
context_engine/adapters — one module per store, and no store of record here.

Each adapter answers exactly one question: "what does the thing you already
own have to say about this request?"  It owns no data, opens no new database
and builds no new index.  When ``memory_engine.db`` is deleted the memory
adapter returns nothing on the next turn; when an expert is reindexed the
expert adapter sees the new chunks immediately.  That is the whole reason the
Context Engine is a layer and not a warehouse: a warehouse would have to be
kept in sync with nine sources, and the day it drifted, "why did it know that?"
would have an answer nobody could check.

Two conventions hold across every module in this package, and breaking either
one turns a packet into a rumour:

**Heavy imports are function-local.**  ``import src.context_engine.adapters``
must not pull in Chroma, sqlite files, the provenance graph or the session
database.  Every adapter imports its store inside the method that needs it, so
a process that never asks about documents never pays for the vector client, and
a store that fails to import costs its own adapter rather than the retrieval.

**Every ``source_ref`` is documented in the module that mints it.**  The
schemes, collected here so a reader does not have to open nine files:

    mem:<item_id>                          memory.py   (learned memory)
    pmem:<entry_id>                        memory.py   (memory.json entries)
    objective:OBJ-3                        objectives.py
    project:<filename.md>                  projects.py
    project:instructions                   projects.py
    doc:<path>#chunk<n>                    documents.py
    expert:<slug>#<chunk_id>               experts.py
    session:<session_id>#<index>           sessions.py
    prov:<node_kind>:<node_key>            provenance.py
    file:<relative/path>                   files.py
    file:<relative/path>#L10-L40           files.py
    link:<link_id>                         project_links.py
    link:<link_id>#line=42                 project_links.py
    delta:<delta_id>                       deltas.py   (delta_engine/persistence.py)
    block:<block_id>                       derived.py  (context_engine/blocks.py)
    capsule:<scope_id>                     derived.py  (capsules.py)
    experience:<exp_id>, exp:<exp_id>      derived.py  (experiences.py)
    symbol:<path>#L10-L40                  derived.py  (code_index.py)
    finding:<finding_id>                   derived.py  (shared_memory.py)
    recipe:<recipe_id>                     derived.py  (multimodal_memory.py)

The last six read the Context Engine's own derived stores rather than a store
belonging to another subsystem, which is the only way they differ from the rest
— they own no data either: ``blocks.py`` and its five siblings do, and
``derived.py`` calls them exactly as ``objectives.py`` calls
``services/objectives.py``.  They were built with an ``as_candidates()`` each
and registered nowhere, so the compiler could not see them and ``/context``
reported six declared sources as unavailable; registering them here is what
makes ``planner.SOURCE_SECTIONS`` and ``candidates.registered_sources()`` agree.

``SOURCE_FACTORIES`` is what ``candidates.default_sources()`` instantiates.
Order is the order sources appear in a gather result, which matters only for
readability of a debug log — ranking happens later and does not look at it.
"""

from __future__ import annotations

from typing import Callable, List, Tuple

from ..candidates import ContextSource
from .derived import (
    BlockSource,
    CapsuleSource,
    CodeIndexSource,
    ExperienceSource,
    FindingSource,
    RecipeSource,
)
from .deltas import DeltaSource
from .documents import DocumentSource
from .experts import ExpertSource
from .files import FileSource
from .memory import MemoryEngineSource, PersonalMemorySource
from .objectives import ObjectivesSource
from .project_links import ProjectLinksSource
from .projects import ProjectMemorySource
from .provenance import ProvenanceSource
from .sessions import SessionSource, reset_history_provider, set_history_provider
from .state_mirror import StateMirrorSource

SOURCE_FACTORIES: Tuple[Callable[[], ContextSource], ...] = (
    ObjectivesSource,
    ProjectMemorySource,
    ProjectLinksSource,
    MemoryEngineSource,
    PersonalMemorySource,
    SessionSource,
    FileSource,
    DocumentSource,
    ExpertSource,
    ProvenanceSource,
    BlockSource,
    CapsuleSource,
    ExperienceSource,
    CodeIndexSource,
    FindingSource,
    RecipeSource,
    DeltaSource,
    StateMirrorSource,
)


def all_sources() -> List[ContextSource]:
    """A fresh, unregistered instance of every adapter.

    ``candidates.default_sources()`` is what callers want; this exists for a
    test that needs a clean set without touching the process-wide registry.
    """
    return [factory() for factory in SOURCE_FACTORIES]


__all__ = [
    "SOURCE_FACTORIES", "all_sources",
    "DocumentSource", "ExpertSource", "FileSource",
    "MemoryEngineSource", "PersonalMemorySource",
    "ObjectivesSource", "ProjectLinksSource", "ProjectMemorySource",
    "ProvenanceSource",
    "SessionSource", "set_history_provider", "reset_history_provider",
    "BlockSource", "CapsuleSource", "ExperienceSource", "CodeIndexSource",
    "FindingSource", "RecipeSource",
    "DeltaSource",
    "StateMirrorSource",
]
