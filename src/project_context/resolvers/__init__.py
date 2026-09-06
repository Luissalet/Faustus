"""
project_context/resolvers — one adapter per source kind, registered on import.

Importing this package registers the five built-in resolvers and costs nothing
beyond that: every one of them imports its database module *inside* a method,
so nothing here opens a connection, reads a file or pulls in the tool runtime
at import time. That is what lets ``src.project_context`` be imported from
route setup without dragging the app's init order around.

Registration is idempotent — ``register_resolver`` replaces by kind — so a
module reload in a test suite is harmless, and a test can swap one out with
``register_resolver`` or ``reset_resolvers`` instead of monkeypatching imports.
"""

from __future__ import annotations

from .base import (  # noqa: F401
    ContextSourceResolver, MAX_READ_CHARS, ResolverBase, get_resolver,
    register_resolver, resolvers, reset_resolvers,
)
from .artifact import ArtifactResolver  # noqa: F401
from .document import DocumentResolver  # noqa: F401
from .filesystem import FilesystemResolver  # noqa: F401
from .gallery import GalleryImageResolver  # noqa: F401

__all__ = [
    "ArtifactResolver", "ContextSourceResolver", "DocumentResolver",
    "FilesystemResolver", "GalleryImageResolver", "MAX_READ_CHARS", "ResolverBase",
    "get_resolver", "install_default_resolvers", "register_resolver", "resolvers",
    "reset_resolvers",
]


def install_default_resolvers() -> None:
    """(Re-)register the built-ins. Called at import; safe to call again."""
    register_resolver(FilesystemResolver("file"))
    register_resolver(FilesystemResolver("folder"))
    register_resolver(DocumentResolver())
    register_resolver(ArtifactResolver())
    register_resolver(GalleryImageResolver())


install_default_resolvers()
