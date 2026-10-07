"""Resolve literal tool names for schema selection, never for authorization."""
import re
from collections import defaultdict
from typing import AbstractSet, Iterable


def named_tools_in_request(query: str, available: Iterable[str], disabled: AbstractSet[str] = frozenset()) -> set[str]:
    names = {name for name in available if isinstance(name, str) and name not in disabled}
    exact = defaultdict(set)
    aliases = defaultdict(set)
    for name in names:
        exact[name.lower()].add(name)
        if name.startswith("mcp__"):
            aliases[name.rsplit("__", 1)[-1].lower()].add(name)
    tokens = {token for token in re.findall(r"\b[a-z][a-z0-9_]{2,}\b", str(query or "").lower()) if "_" in token}
    matched = set()
    for token in tokens:
        # Existing native/full names take precedence over a short MCP alias.
        candidates = exact[token] if token in exact else aliases[token]
        if len(candidates) == 1:
            matched.update(candidates)
    return matched
