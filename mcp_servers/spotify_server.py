"""spotify_server.py

Built-in MCP server for Spotify (src/spotify.py): spotify_status, spotify_playback,
spotify_devices, spotify_queue, spotify_search, spotify_playlists, spotify_albums
and spotify_library. It reads the tokens the app stored after the owner's login
(``/api/spotify/login``) and refreshes them itself; without a login every tool
answers with what to do to connect the account.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised around the session.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import spotify
from src.stdio_guard import guard

server = Server("spotify")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=name, description=spec["description"], inputSchema=spec["parameters"])
            for name, spec in spotify.TOOLS.items()]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    try:
        payload, error = await asyncio.to_thread(spotify.call, name, dict(arguments or {})), False
    except (spotify.SpotifyError, ValueError, TypeError) as exc:
        payload, error = {"error": str(exc)}, True
    except Exception as exc:  # noqa: BLE001 - a network failure is an answer, not a crash
        payload, error = {"error": f"{type(exc).__name__}: {exc}"}, True
    return CallToolResult(isError=error, content=[TextContent(
        type="text", text=json.dumps(payload, ensure_ascii=False, indent=2, default=str))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
