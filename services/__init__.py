# services/__init__.py
"""
Service layer — plug-in capabilities for the chat core.

Each service:
- Does one thing well
- Exposes a clean async interface
- Can run in-process or as a standalone HTTP service
"""

from importlib import import_module

# Importing one leaf service must not initialize document/vector backends.
# Requested exports still import the same real module, with its normal side
# effects, and are cached as the exact class objects that module publishes.
_EXPORT_MODULES = {
    **dict.fromkeys(("SearchService", "SearchResult", "SearchResponse"), "search"),
    **dict.fromkeys(("DocsService", "DocChunk", "IndexResult"), "docs"),
    **dict.fromkeys(("ResearchService", "ResearchResult", "ResearchSource"), "research"),
    **dict.fromkeys(("MemoryService", "Memory", "MemorySearchResult"), "memory"),
    **dict.fromkeys(("ShellService", "ShellResult"), "shell"),
}


def __getattr__(name):
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

__all__ = [
    # Search
    "SearchService",
    "SearchResult",
    "SearchResponse",
    # Docs
    "DocsService",
    "DocChunk",
    "IndexResult",
    # Research
    "ResearchService",
    "ResearchResult",
    "ResearchSource",
    # Memory
    "MemoryService",
    "Memory",
    "MemorySearchResult",
    # Shell
    "ShellService",
    "ShellResult",
]
