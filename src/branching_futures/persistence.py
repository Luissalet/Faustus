from __future__ import annotations

from typing import Optional

from src.constants import BRANCHING_FUTURES_DB
from src.durable_feature_store import DurableFeatureStore

_store: Optional[DurableFeatureStore] = None
_path_override: Optional[str] = None


def store() -> DurableFeatureStore:
    global _store
    if _store is None:
        _store = DurableFeatureStore(_path_override or BRANCHING_FUTURES_DB)
    return _store


def use_path(path: Optional[str]) -> None:
    global _store, _path_override
    _path_override = path
    _store = None
