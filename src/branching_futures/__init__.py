"""Isolated alternatives and evidence-based selection (plan 10)."""

from .service import BranchingService, service, reset_service
from .narrative_canon import (
    ALT_STATUS_DISCARDED,
    ALT_STATUS_DRAFT,
    ALT_STATUS_PROMOTED,
    ALT_STATUSES,
    alternative_status,
    canon_state,
    discarded_alternatives,
)

__all__ = [
    "BranchingService", "service", "reset_service",
    "ALT_STATUS_DISCARDED", "ALT_STATUS_DRAFT", "ALT_STATUS_PROMOTED", "ALT_STATUSES",
    "alternative_status", "canon_state", "discarded_alternatives",
]
