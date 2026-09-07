"""Isolation boundary for future branch executors.

The production worktree/media adapters plug into this protocol later.  The
fixture implementation deliberately has no filesystem or network capability;
it lets lifecycle and fairness tests prove that branches cannot share mutable
outputs before a real isolator is trusted.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Mapping, Protocol


class BranchIsolator(Protocol):
    async def fork(self, snapshot: Mapping[str, Any], branch: Mapping[str, Any]) -> Dict[str, Any]: ...
    async def inspect(self, handle: Mapping[str, Any]) -> Dict[str, Any]: ...
    async def cleanup(self, handle: Mapping[str, Any]) -> Dict[str, Any]: ...


class InMemoryIsolator:
    """No-effect isolator for fixtures and plan/simulation branches."""

    def __init__(self) -> None:
        self._spaces: Dict[str, Dict[str, Any]] = {}

    async def fork(self, snapshot: Mapping[str, Any], branch: Mapping[str, Any]) -> Dict[str, Any]:
        namespace = str(branch.get("namespace") or "")
        if not namespace or namespace in self._spaces:
            raise ValueError("branch namespace must be new and non-blank")
        self._spaces[namespace] = {"snapshot": copy.deepcopy(dict(snapshot)), "outputs": {}}
        return {"namespace": namespace, "real_external_effects": False}

    async def inspect(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        namespace = str(handle.get("namespace") or "")
        return copy.deepcopy(self._spaces.get(namespace, {}))

    async def cleanup(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        namespace = str(handle.get("namespace") or "")
        removed = self._spaces.pop(namespace, None) is not None
        return {"removed": removed, "namespace": namespace}
