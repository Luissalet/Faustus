"""Errors for the Creator document domain (WP02).

Kept tiny and dependency-free so ``routes/creator_routes.py`` (and any
future WP that imports this package) can catch a narrow, stable set of
exception classes instead of parsing message strings.
"""
from __future__ import annotations


class CreatorError(Exception):
    """Base class for every error this package raises on purpose."""


class DocumentNotFound(CreatorError):
    """No document with that id, or it belongs to someone else.

    Deliberately the SAME exception (and the same route response, 404) for
    both cases — see CONTRATO.md rule 3: "no es tuyo" y "no existe"
    responden igual.
    """


class RevisionConflict(CreatorError):
    """``apply_command`` was called with a stale ``expected_revision``.

    Carries the document's current revision so a caller can re-read and
    retry instead of losing work silently.
    """

    def __init__(self, current_revision: int, message: str = "") -> None:
        self.current_revision = int(current_revision)
        super().__init__(
            message or (
                f"expected_revision is stale; current revision is "
                f"{self.current_revision}"
            )
        )


class InvalidDocument(CreatorError):
    """Content failed structural validation for its ``kind``."""


class InvalidOperation(CreatorError):
    """The op dict passed to ``apply_command`` is not a recognised shape."""
