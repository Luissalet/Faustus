"""Registrations for the tool families the authority owns (H05).

Families migrated so far, each registered as data:

  core_exec   bash, python, powershell
  core_fs     read_file, write_file, edit_file, apply_patch, grep, glob, ls,
              get_workspace
  core_web    web_search, web_fetch
  media       plan_media_transform, transform_media, inspect_media, image_job
  documents   inspect_deliverable
  pdf         pdf_outline, pdf_read_section, pdf_find_section (spec owned by
              `src.pdf_tool_contracts`; adopted here, never copied)

Every other tool is registered as `wrapped` by `src.tool_authority_catalog`
until its family is migrated; the authority then emits its schema from the
registry instead of the literal list in `src.tool_schemas`.

Pure module: no handler, dispatcher or settings import at import time. Limits
that belong to a handler module are references resolved when read.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from src.tool_authority import (
    AUTHORITY, Exposure, LimitRef, ParserContract, ToolEffects, ToolLimits, ToolResources, make_tool,
)
from src.tool_authority_schemas import CORE_SCHEMAS

#: Hits a code-navigation tool (grep, glob, ls) returns at most, and the
#: longest matched line grep prints. The handlers read these same numbers.
CODENAV_MAX_HITS = 200
CODENAV_MAX_LINE_CHARS = 400

_OUTPUT = LimitRef("src.constants:MAX_OUTPUT_CHARS")
_READ = LimitRef("src.constants:MAX_READ_CHARS")
_DIFF = LimitRef("src.constants:MAX_DIFF_LINES")
_IDLE_SETTING = "agent_subprocess_idle_timeout_seconds"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# Parser contracts: how a native call becomes the text a handler reads.
# ---------------------------------------------------------------------------
def _json_args(args: Mapping[str, Any]) -> str:
    return json.dumps(dict(args)) if args else "{}"


def _encode_bash(args: Mapping[str, Any]) -> str:
    return args.get("command", "")


def _encode_python(args: Mapping[str, Any]) -> str:
    return args.get("code", "")


def _encode_powershell(args: Mapping[str, Any]) -> str:
    return args.get("script") or args.get("command") or args.get("code") or ""


def _encode_web_search(args: Mapping[str, Any]) -> str:
    queries = args.get("queries")
    if isinstance(queries, list) and queries:
        content = str(queries[0])
    elif queries:
        content = str(queries)
    else:
        content = args.get("query", "")
    # The schema advertises time_filter and the executor parses
    # {"query","time_filter"}; a bare query string would drop it.
    time_filter = args.get("time_filter")
    if content and isinstance(time_filter, str) and time_filter in ("day", "week", "month", "year"):
        return json.dumps({"query": content, "time_filter": time_filter})
    return content


def _encode_read_file(args: Mapping[str, Any]) -> str:
    # A plain path unless a line range is requested.
    if args.get("offset") or args.get("limit"):
        return json.dumps(dict(args))
    return args.get("path", "")


def _encode_write_file(args: Mapping[str, Any]) -> str:
    # `path\ncontent` is the legacy shape and stays the default. A call that
    # carries a precondition (`base_revision`) or an override (`confirm_risky`)
    # needs the structured shape, or the handler never sees it.
    if args.get("base_revision") or args.get("confirm_risky"):
        payload = {"path": _text(args.get("path")), "content": _text(args.get("content"))}
        for key in ("base_revision", "confirm_risky"):
            if args.get(key):
                payload[key] = args[key]
        return json.dumps(payload)
    return _text(args.get("path")) + "\n" + _text(args.get("content"))


def _encode_apply_patch(args: Mapping[str, Any]) -> str:
    patch = args.get("patch_text") or args.get("patchText") or args.get("patch") or ""
    if args.get("base_revision") or args.get("confirm_risky"):
        payload = {"patch_text": patch}
        for key in ("base_revision", "confirm_risky"):
            if args.get(key):
                payload[key] = args[key]
        return json.dumps(payload)
    return patch


def _encode_empty(args: Mapping[str, Any]) -> str:
    return ""


_NO_ALIAS: tuple = ()

#: name -> (family, canonical id, parser, resources, limits, aliases)
_CORE: dict = {
    "bash": ("core_exec", "shell.bash", ParserContract(_encode_bash, required_any=()),
             ToolResources(process=True),
             ToolLimits.of(hard_timeout_s=LimitRef("src.agent_tools.subprocess_tools:DEFAULT_BASH_TIMEOUT"),
                           idle_timeout_setting=_IDLE_SETTING, max_output_chars=_OUTPUT),
             ("shell", "terminal", "command", "execute", "run")),
    "python": ("core_exec", "shell.python", ParserContract(_encode_python),
               ToolResources(process=True),
               ToolLimits.of(hard_timeout_s=LimitRef("src.agent_tools.subprocess_tools:DEFAULT_PYTHON_TIMEOUT"),
                             idle_timeout_setting=_IDLE_SETTING, max_output_chars=_OUTPUT),
               ("code",)),
    "powershell": ("core_exec", "shell.powershell",
                   ParserContract(_encode_powershell, arg_aliases=(("command", "script"), ("code", "script"))),
                   ToolResources(process=True),
                   ToolLimits.of(hard_timeout_s=LimitRef("src.agent_tools.subprocess_tools:DEFAULT_POWERSHELL_TIMEOUT"),
                                 idle_timeout_setting=_IDLE_SETTING, max_output_chars=_OUTPUT),
                   ("pwsh", "ps1")),
    "web_search": ("core_web", "web.search",
                   ParserContract(_encode_web_search, required_any=(("query", "queries"),),
                                  arg_aliases=(("queries", "query"),)),
                   ToolResources(network=True), ToolLimits.of(max_output_chars=_OUTPUT),
                   ("search", "websearch", "google_search", "google_search_retrieval",
                    "google_search_grounding")),
    "web_fetch": ("core_web", "web.fetch",
                  ParserContract(_json_args, required_any=(("url",),)),
                  ToolResources(network=True),
                  ToolLimits.of(max_output_chars=_OUTPUT,
                                max_bytes=LimitRef("src.constants:WEB_FETCH_HARD_MAX_BYTES")),
                  ("webfetch", "fetch_url", "fetch")),
    "read_file": ("core_fs", "fs.read_file",
                  ParserContract(_encode_read_file, required_any=(("path",),)),
                  ToolResources(path_args=("path",), access="read"),
                  ToolLimits.of(max_output_chars=_READ,
                                max_image_bytes=LimitRef("src.agent_tools.filesystem_tools:_MAX_IMAGE_BYTES")),
                  ("read", "cat")),
    "write_file": ("core_fs", "fs.write_file",
                   ParserContract(_encode_write_file, required_any=(("path",),),
                                  internal_keys=frozenset({"confirm_risky"})),
                   ToolResources(path_args=("path",), access="write"),
                   ToolLimits.of(max_diff_lines=_DIFF), ("write", "save")),
    "edit_file": ("core_fs", "fs.edit_file",
                  ParserContract(_json_args, required_any=(("path",),),
                                 internal_keys=frozenset({"confirm_risky"})),
                  ToolResources(path_args=("path",), access="write"),
                  ToolLimits.of(max_diff_lines=_DIFF), ()),
    "apply_patch": ("core_fs", "fs.apply_patch",
                    ParserContract(_encode_apply_patch, required_any=(("patch_text", "patchText", "patch"),),
                                   arg_aliases=(("patchText", "patch_text"), ("patch", "patch_text")),
                                   internal_keys=frozenset({"confirm_risky"})),
                    ToolResources(access="write"), ToolLimits.of(max_diff_lines=_DIFF), ("patch",)),
    "grep": ("core_fs", "fs.grep", ParserContract(_json_args),
             ToolResources(path_args=("path",), access="read"),
             ToolLimits.of(max_results=CODENAV_MAX_HITS, max_line_chars=CODENAV_MAX_LINE_CHARS), ()),
    "glob": ("core_fs", "fs.glob", ParserContract(_json_args),
             ToolResources(path_args=("path",), access="read"),
             ToolLimits.of(max_results=CODENAV_MAX_HITS), ()),
    "ls": ("core_fs", "fs.ls", ParserContract(_json_args),
           ToolResources(path_args=("path",), access="read"),
           ToolLimits.of(max_results=CODENAV_MAX_HITS), ()),
    "get_workspace": ("core_fs", "fs.get_workspace", ParserContract(_encode_empty),
                      ToolResources(), ToolLimits(), ()),
    "plan_media_transform": ("media", "media.plan_transform", ParserContract(_json_args),
                             ToolResources(path_args=("source", "path"), access="read"), ToolLimits(), ()),
    "transform_media": ("media", "media.transform", ParserContract(_json_args),
                        ToolResources(path_args=("source", "path"), access="write"), ToolLimits(), ()),
    "inspect_media": ("media", "media.inspect",
                      ParserContract(_json_args, required_any=(("path",),)),
                      ToolResources(path_args=("path",), access="read"), ToolLimits(), ()),
    "inspect_deliverable": ("documents", "deliverable.inspect",
                             ParserContract(_json_args, required_any=(("path",),)),
                             ToolResources(path_args=("path",), access="read"), ToolLimits(), ()),
    "image_job": ("media", "media.image_job", ParserContract(_json_args),
                  ToolResources(network=True), ToolLimits(), ()),
}


def _register_core() -> None:
    for name, (family, canonical, parser, resources, limits, aliases) in _CORE.items():
        schema = CORE_SCHEMAS[name]
        AUTHORITY.register(make_tool(
            name=name, canonical_id=canonical, family=family, description=schema["description"],
            parameters=schema["parameters"], parser=parser, effects=ToolEffects(), resources=resources,
            limits=limits, exposure=Exposure.DIRECT, aliases=aliases, origin="authored"))


def _register_pdf() -> None:
    from src.pdf_tool_contracts import function_definition, tool_names

    def _encode_pdf(args: Mapping[str, Any]) -> str:
        return json.dumps(dict(args)) if args else "{}"

    for name in sorted(tool_names(), key=lambda n: ("pdf_outline", "pdf_read_section", "pdf_find_section").index(n)):
        definition = function_definition(name)
        suffix = name[len("pdf_"):]
        AUTHORITY.register(make_tool(
            name=name, canonical_id=f"pdf.{suffix}", family="pdf", description=definition["description"],
            parameters=definition["parameters"],
            parser=ParserContract(_encode_pdf, required_any=tuple((key,) for key in definition["parameters"]["required"])),
            resources=ToolResources(path_args=("path",), access="read"),
            limits=ToolLimits(), exposure=Exposure.DIRECT, origin="contract"))


_registered = False


def register_all() -> None:
    """Idempotent. Runs at import of this module."""
    global _registered
    if _registered:
        return
    _register_core()
    _register_pdf()
    _registered = True


register_all()

from src.tool_authority_catalog import install_populator  # noqa: E402

install_populator()
