"""
github_server.py

MCP server for the `/github` family (src/github_ops.py): a repository's
labels, release notes drafted from merged PRs/commits, and the release-ready
checklist. Built on the GitHub CLI (`gh`) and local git.

Read-only by construction: it does NOT expose the label sync. Applying a
sync is a remote write that needs the repository policy and a human's
explicit approval, and an MCP call has no approval card to carry that -- the
agent's `github_labels_sync` tool and the Studio `/github labels sync`
command do. Here `github_labels` with `file` returns the dry-run plan only.

Same two constraints as the other built-in servers:

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported.
* **`root` is required** (a stdio server has no active workspace to default
  to) and must lie under the allowed workspace roots.
* **workspace confinement is the tool layer's.** `root` is resolved through
  the same guard the `git_*` agent tools use (`git_tools._confined_path`),
  then to the repository's top level; a path outside the workspace, or
  outside any repository, is a message, not a traceback.
"""

import asyncio
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("github")

_engine: dict = {}
_initialized = False


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload) -> list[TextContent]:
    import json

    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _ensure_init() -> None:
    """Import the engine on first use, not at server start."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    from src import git_panel, github_ops
    from src.agent_tools import git_tools

    _engine["ops"] = github_ops
    _engine["git_panel"] = git_panel
    _engine["git_tools"] = git_tools


_ROOT_PROP = {"root": {"type": "string",
                       "description": ("REQUIRED over MCP: an absolute path inside a git repository that lies under the "
                                       "allowed workspace roots. A stdio server has no active workspace, so omitting it "
                                       "answers 'no active workspace is bound to this turn'; a repository outside the "
                                       "allowed roots is refused.")}}
_LANG_PROP = {"lang": {"type": "string", "description": "'en' (default) or 'es' for the markdown headings."}}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="github_labels",
            description=(
                "List the repository's GitHub labels with gh; with `file` (a YAML/JSON label file in the repo) "
                "return the DRY-RUN plan of a label sync (create / update / unchanged / only on GitHub). Never writes. "
                "Etiquetas del repositorio de GitHub / comparar con labels.yml. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Label file inside the repo to compare against; omit to list."},
                    **_ROOT_PROP, **_LANG_PROP,
                },
                "required": [],
            },
        ),
        Tool(
            name="github_notes",
            description=(
                "Draft release notes (markdown) from the merged PRs and commits between two refs, grouped by label "
                "or conventional-commit type. Default range: last tag .. HEAD. Falls back to local git when gh is "
                "unavailable and says so. Notas de la versión / changelog desde la última etiqueta. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "Start tag/ref (exclusive); default the last tag."},
                    "to": {"type": "string", "description": "End ref (inclusive); default HEAD."},
                    **_ROOT_PROP, **_LANG_PROP,
                },
                "required": [],
            },
        ),
        Tool(
            name="github_release_check",
            description=(
                "Is this release ready? Checklist: clean working tree, branch pushed, tag free (local AND on origin) or on HEAD, CI of HEAD, "
                "open blocker issues/PRs, version strings consistent. GitHub items are skipped with the reason when gh "
                "is unavailable. Comprobar que la versión está lista. Read-only; never tags or publishes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "The tag you intend to create, e.g. v1.2.0."},
                    "blocker_labels": {"type": "array", "items": {"type": "string"},
                                       "description": "Labels that block a release (default: blocker, release-blocker)."},
                    **_ROOT_PROP, **_LANG_PROP,
                },
                "required": [],
            },
        ),
    ]


def _repo_root(args: dict) -> str:
    gt = _engine["git_tools"]
    try:
        confined = gt._confined_path(str(args.get("root") or ""))
    except gt._OutsideWorkspace as exc:
        raise ValueError(str(exc))
    top = _engine["git_panel"].repo_toplevel(confined)
    if not top:
        raise ValueError(f"no git repository found at or above {confined!r}")
    return top


def _lang(args: dict) -> str:
    return "es" if str(args.get("lang") or "").lower().startswith("es") else "en"


def _fail(name: str, res: dict) -> list[TextContent]:
    return _text_result(f"{name}: [{res.get('error_class', 'error')}] {res.get('error', 'failed')}")


def _tool_labels(args: dict) -> list[TextContent]:
    ops = _engine["ops"]
    root = _repo_root(args)
    file = str(args.get("file") or "").strip()
    if file:
        res = ops.prepare_label_sync("", root, file, _lang(args))
        if res.get("approval_id"):  # an MCP call is not an approval; do not leave one armed
            with ops._PENDING_LOCK:
                ops._PENDING.pop(res["approval_id"], None)
            res = {**res, "approval_id": None}
        if not res.get("ok"):
            return _fail("github_labels", res)
        return _json_result({k: res[k] for k in ("slug", "file", "plan", "policy_allowed", "markdown")})
    res = ops.list_labels(root)
    if not res.get("ok"):
        return _fail("github_labels", res)
    return _json_result({"slug": res.get("slug"), "labels": res["labels"],
                         "markdown": ops.labels_markdown(res["labels"], res.get("slug") or "", _lang(args))})


def _tool_notes(args: dict) -> list[TextContent]:
    ops = _engine["ops"]
    res = ops.release_notes(_repo_root(args), str(args.get("from") or "").strip() or None,
                            str(args.get("to") or "").strip() or None, _lang(args))
    if not res.get("ok"):
        return _fail("github_notes", res)
    return _json_result({k: res[k] for k in ("from", "to", "commits", "pull_requests", "gh", "entries", "markdown")})


def _tool_release_check(args: dict) -> list[TextContent]:
    ops = _engine["ops"]
    labels = args.get("blocker_labels")
    res = ops.release_check(_repo_root(args), str(args.get("tag") or "").strip() or None,
                            labels if isinstance(labels, list) else None, _lang(args))
    if not res.get("ok"):
        return _fail("github_release_check", res)
    return _json_result({k: res[k] for k in ("tag", "head", "ready", "blocking", "counts", "items", "markdown")})


_HANDLERS = {
    "github_labels": _tool_labels,
    "github_notes": _tool_notes,
    "github_release_check": _tool_release_check,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill every other
    tool on this server, not just the one call that failed."""
    if name not in _HANDLERS:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}

    try:
        _ensure_init()
    except Exception as exc:  # noqa: BLE001 - an unimportable engine is a message
        return _text_result(f"Error: the GitHub tools could not be loaded: {exc}")

    try:
        return await asyncio.to_thread(_HANDLERS[name], args)
    except ValueError as exc:
        return _text_result(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
