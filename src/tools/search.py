"""Search-domain tool implementations.

Extracted from tool_implementations.py as part of slice 1 (#4082/#4071).
Holds the search_chats tool.
``src.tool_implementations`` re-exports these for backward compatibility.
"""
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)


async def do_search_chats(
    query: str,
    limit: int = 20,
    owner: str | None = None,
    folder: str | None = None,
) -> Dict:
    """Search past session transcripts for the calling user's sessions only.

    Without an owner filter this used to leak EVERY user's chat history
    into the agent's `search_chats` results (v2 review HIGH-11). The
    caller in `tool_execution.execute_tool_block` now plumbs the owner
    through; legacy callers without owner pass through as before but
    will only see legacy/null-owner rows.
    """
    try:
        from src.session_search import search_session_messages

        kwargs = {"limit": limit, "owner": owner}
        if folder:
            kwargs["folder"] = folder
        results = search_session_messages(query, **kwargs)

        # In local single-user mode the Imported library is the user's chat
        # history too. Its store is global, so keep it out of project-scoped
        # and multi-user searches where the caller's ownership is ambiguous.
        imported = []
        if not folder:
            from src.owner_identity import auth_disabled
            if auth_disabled():
                from src import history_import
                if history_import.enabled() and os.path.exists(history_import.db_path()):
                    imported = history_import.search(query, k=min(limit, 10))["hits"]
        if not results and not imported:
            return {"results": f"No chats found matching \"{query}\"."}

        # Group by session to avoid duplicate links
        seen_sessions = {}
        for result in results:
            if result.session_id not in seen_sessions:
                seen_sessions[result.session_id] = result

        lines = [f"Found {len(seen_sessions)} live chat(s) and {len(imported)} imported message(s) matching \"{query}\":\n"]
        for sid, result in seen_sessions.items():
            lines.append(f"- [**{result.session_name}**](#session-{sid})")
            lines.append(f"  Open: [Open chat](#session-{sid})")
            lines.append(f"  Match ({result.role}): {result.content_snippet}")
            if result.context_before:
                before = result.context_before[-1]
                lines.append(f"  Before ({before['role']}): {before['content'][:180]}")
            if result.context_after:
                after = result.context_after[0]
                lines.append(f"  After ({after['role']}): {after['content'][:180]}")
            lines.append("")

        if imported:
            lines.append("Imported conversations (Library → Imported):")
            for hit in imported:
                lines.append(
                    f"- [{hit['source']}] {hit['title'][:120]} — {hit['role']}: {hit['snippet'][:240]}"
                )
            lines.append("")

        return {"results": "\n".join(lines)}
    except Exception as e:
        logger.error(f"search_chats failed: {e}")
        return {"error": str(e), "exit_code": 1}
