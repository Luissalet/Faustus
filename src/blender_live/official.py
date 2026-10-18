"""Use the installed Blender Lab distribution, including its offline documentation.

This is the official engine integration. Asset workflows remain native Faustus
operations; they are not delegated to a third-party agent or hosted service.
"""
import importlib
import os
import pkgutil
import subprocess
from types import SimpleNamespace
from functools import lru_cache


@lru_cache(maxsize=1)
def engine():
    from mcp.server.fastmcp import FastMCP
    try:
        import blmcp.tools
    except ImportError as exc:
        raise RuntimeError("Instala la distribución oficial con Install-BlenderBridge.ps1.") from exc
    os.environ.setdefault("BLENDER_MCP_HOST", "127.0.0.1")
    from .client import BridgeClient
    os.environ.setdefault("BLENDER_MCP_PORT", str(BridgeClient().port))
    from src.blender_scene.discovery import candidates, read_version
    # Older headless scene setups can still name Blender 4.x. The official
    # server requires 5.1+, and CLI tools must use the same supported generation.
    paths = candidates(setting="")
    for candidate in paths:
        if not os.path.isfile(candidate.path):
            continue
        version = read_version(candidate.path)
        if version and tuple(int(part) for part in version.split('.')) >= (5, 1, 0):
            os.environ["BLENDER_PATH"] = candidate.path
            break
    from blmcp.tools_helpers import blender_cli

    def run_process(*args, **kwargs):
        # A child Blender must not inherit the MCP JSON-RPC stdin pipe. Keep
        # this adaptation local to the official CLI helper, not subprocess globally.
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return subprocess.run(*args, **kwargs)

    blender_cli.subprocess = SimpleNamespace(run=run_process, TimeoutExpired=subprocess.TimeoutExpired)
    server = FastMCP("Faustus Blender")
    for item in pkgutil.iter_modules(blmcp.tools.__path__):
        if item.name.endswith("_toolcode") or item.name.startswith("_template_"):
            continue
        module = importlib.import_module("blmcp.tools." + item.name)
        if hasattr(module, "register"):
            module.register(server)
    return server


async def tools():
    return await engine().list_tools()


async def call(name, arguments):
    return await engine().call_tool(name, arguments)
