"""Agent catalogue entry; implementation is shared with the lightweight MCP server."""
from src.blender_scene.tool import BlenderSceneTool
from src.blender_scene import tool as _implementation


def __getattr__(name):
    return getattr(_implementation, name)