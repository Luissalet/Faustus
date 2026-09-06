"""
src/project_context — which sources belong to a project, and under what policy.

The layer of identity and membership, sitting immediately before the Context
Engine (plan §5.6). The division is strict and worth restating because every
one of these has been conflated somewhere before:

* **Project Context Links** decide membership, identity, version and access.
* **Context Engine** decides retrieval, ranking, budget and injection.
* **Memory Engine** keeps conclusions, not references to sources.
* **Artifact Store** keeps the object and its lineage, not whether it belongs
  to a project's knowledge.

What lives here
---------------
``models``      the typed contracts — and the type that refuses to carry a
                title in a refusal.
``resolvers``   one adapter per source kind (file, folder, document, artifact,
                gallery image), each of which checks the owner *before* it
                touches the source.
``references``  ``TurnReferenceRegistry``: what "this document" means, decided
                rather than guessed, and an honest "which one?" when two
                candidates were created by the same operation.
``service``     ``ProjectContextService``: attach, detach, update, list,
                inspect, refresh — the one place ownership, deduplication and
                validation live, so the tool dispatch branch only adapts
                arguments.

Two rules that are easy to lose in the details:

1. **The project always arrives resolved by the server.** Nothing here accepts
   a project id in text from a model.
2. **Everything a resolver returns is untrusted data**, even when another agent
   produced it. Linked content is quoted context, never instruction.

Importing this package is cheap: no database module, no tool runtime and no
filesystem access happens at import time.
"""

from __future__ import annotations

from .models import (  # noqa: F401
    ACCESS_MODES, ACTOR_KINDS, INDEX_STATUSES, LINK_KINDS, PATCHABLE_FIELDS,
    PATH_KINDS, REF_KINDS, RELATIONS, RETRIEVAL_POLICIES, ROLES, SOURCE_STATES,
    VERSION_POLICIES, ActorRef, AttachResult, ContextLinkStatus, DetachResult,
    ExtractedChunk, ExtractedCorpus, ProjectContextError, ProjectContextLink,
    RefreshResult, SourceContent, SourceMatch, SourceMetadata, SourceRef,
)
from .references import (  # noqa: F401
    TurnReference, TurnReferenceRegistry, note_created, registry,
)
from .resolvers import (  # noqa: F401
    ArtifactResolver, ContextSourceResolver, DocumentResolver, FilesystemResolver,
    GalleryImageResolver, get_resolver, register_resolver, resolvers,
)
from .service import (  # noqa: F401
    CONTEXT_EVENT_NAMES, ProjectContextService, service, unrouted_events,
)

__all__ = [
    "ACCESS_MODES", "ACTOR_KINDS", "CONTEXT_EVENT_NAMES", "INDEX_STATUSES",
    "LINK_KINDS", "PATCHABLE_FIELDS", "PATH_KINDS", "REF_KINDS", "RELATIONS",
    "RETRIEVAL_POLICIES", "ROLES", "SOURCE_STATES", "VERSION_POLICIES",
    "ActorRef", "ArtifactResolver", "AttachResult", "ContextLinkStatus",
    "ContextSourceResolver", "DetachResult", "DocumentResolver", "ExtractedChunk",
    "ExtractedCorpus", "FilesystemResolver", "GalleryImageResolver",
    "ProjectContextError", "ProjectContextLink", "ProjectContextService",
    "RefreshResult", "SourceContent", "SourceMatch", "SourceMetadata", "SourceRef",
    "TurnReference", "TurnReferenceRegistry", "get_resolver", "note_created",
    "register_resolver", "registry", "resolvers", "service", "unrouted_events",
]
