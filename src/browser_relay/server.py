"""Stdio MCP builtin for the existing browser session. Never launches a browser."""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import hashlib
from pathlib import Path
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations

from src.browser_relay.protocol import SCHEMA, failure, validate_arguments
from src.browser_relay.presentation import present_payload
from src.browser_relay.transport import BrowserRelay
from src.stdio_guard import guard


def mcp_result(payload: dict, detail: str = "compact", view: dict | None = None) -> CallToolResult:
    # The wire image appears once as an MCP image, never again in text/structuredContent.
    payload = dict(present_payload(payload, detail, view))
    images = []
    if payload.get("ok") and "image" in payload.get("result", {}):
        result = dict(payload["result"])
        image = result.pop("image")
        try:
            if not isinstance(image, dict) or image.get("mimeType") != "image/png":
                raise ValueError("Expected a PNG image")
            data = image["data"]
            if not isinstance(data, str) or not base64.b64decode(data, validate=True).startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError("Invalid PNG image")
        except (KeyError, TypeError, ValueError, binascii.Error):
            payload = failure("invalid_screenshot", "Extension returned an invalid PNG image", "not_dispatched")
        else:
            images.append(ImageContent(type="image", data=data, mimeType="image/png"))
            payload["result"] = result
    return CallToolResult(isError=not payload["ok"], structuredContent=payload,
                          content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)), *images])



def upload_inputs(files: list[str]) -> list[dict]:
    """Observe the real local inputs, without copying them or claiming app acceptance."""
    result = []
    for filename in files:
        path = Path(filename)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("upload requires existing absolute file paths")
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        result.append({"path": str(path), "name": path.name, "bytes": size, "sha256": digest.hexdigest()})
    return result


def download_target(filename: str) -> Path:
    path = Path(filename)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("download requires an absolute output file in an existing directory")
    if path.exists():
        raise FileExistsError("download target already exists; choose a new file")
    return path


def save_download(payload: dict, filename: str) -> dict:
    """Consume the private wire bytes. Only the actual disk receipt reaches MCP."""
    if not payload.get("ok"):
        return payload
    phase = "action_confirmed"
    try:
        result = dict(payload["result"])
        transfer = result.pop("download")
        # Data URLs embed the file itself; never publish them in an MCP receipt,
        # including a transfer from an older extension peer.
        source = result.get("source")
        if isinstance(source, dict) and isinstance(source.get("url"), str) and source["url"].lower().startswith("data:"):
            result["source"] = {**{key: value for key, value in source.items() if key != "url"},
                                "scheme": "data:", "url_omitted": True}
        if (not isinstance(transfer, dict) or not isinstance(transfer.get("name"), str)
                or not isinstance(transfer.get("mime"), str) or type(transfer.get("bytes")) is not int
                or transfer["bytes"] < 0 or not isinstance(transfer.get("parts"), list)):
            raise ValueError("invalid download metadata")
        # Validate every part and the complete byte count BEFORE creating any file.
        chunks = [base64.b64decode(part, validate=True) for part in transfer["parts"]
                  if isinstance(part, str)]
        if len(chunks) != len(transfer["parts"]) or sum(map(len, chunks)) != transfer["bytes"]:
            raise ValueError("incomplete download parts")
    except (KeyError, TypeError, ValueError, binascii.Error):
        return failure("invalid_download", "Browser returned incomplete or invalid file bytes", phase)
    try:
        path = download_target(filename)
        digest = hashlib.sha256()
        # Exclusive creation also protects an existing file appearing after preflight.
        # If disk writing fails, retain the incomplete owned file and report failure;
        # never delete a path another process could have replaced in the meantime.
        with path.open("xb") as handle:
            for chunk in chunks:
                handle.write(chunk)
                digest.update(chunk)
        actual, size = hashlib.sha256(), 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                actual.update(chunk)
                size += len(chunk)
        if size != transfer["bytes"] or actual.digest() != digest.digest():
            raise OSError("written file does not match the complete transfer")
        result["file"] = {"path": str(path), "name": transfer["name"], "mime": transfer["mime"],
                          "bytes": transfer["bytes"], "sha256": digest.hexdigest(), "complete": True}
        return {**payload, "result": result}
    except (OSError, ValueError) as exc:
        return failure("download_write_failed", "File was not completed at " + filename + "; inspect it before retrying: " + str(exc), phase)


def create_server(relay: BrowserRelay) -> Server:
    server = Server("browser_session")

    @server.list_tools()
    async def list_tools():
        return [Tool(name="browser_session", description=(
            "Use the existing browser session through its installed extension. tabs lists real tabs; "
            "all other actions require an explicit tab_id. Use refs from the latest snapshot for click/type; "
            "press optionally focuses ref. type inserts Unicode text. Mutations return automatic readback. "
            "release detaches this tool without closing the tab. Main-frame snapshots only. "
            "upload clicks a current ref with an intercepted file chooser and selects absolute local files; "
            "its receipt confirms selection, not application import. download reads the current anchor href "
            "(including blob) into a new absolute output_path and returns path/bytes/SHA without inline file data. "
            "Only download runs fixed page JavaScript to read the resource; caller scripts are never accepted. "
            "Ephemeral button-only downloads and POST are outside this link-read contract. "
            "detail defaults to compact (original refs and useful text/controls); full returns the complete AX graph. "
            "For snapshot or mutation readback, optional view selects flat original AX rows: query is a literal "
            "case-insensitive name/value substring; roles are case-sensitive, e.g. StaticText or button, and combine with query. "
            "offset defaults to 0; limit, if given, is at least 1; omitted means all matches. "
            "selection reports total, matches and next_offset; matched_roles diagnoses roles that eliminate all query matches. "
            "For split text use view:{query:'visibles',roles:['StaticText'],context:true}: context_nodes adds original "
            "direct parents and siblings, while nodes remains flat. No ancestry is invented for null refs. "
            "An outcome_unknown must be observed before deciding any explicit retry; never replay it automatically."),
            inputSchema=SCHEMA, annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name != "browser_session":
            return mcp_result(failure("unknown_tool", f"Unknown tool: {name}"))
        try:
            args = validate_arguments(arguments)
        except ValueError as exc:
            return mcp_result(failure("invalid_arguments", str(exc)))
        detail = args.pop("detail", "compact")
        view = args.pop("view", None)
        try:
            inputs = upload_inputs(args["files"]) if args["action"] == "upload" else None
            if args["action"] == "download":
                download_target(args["output_path"])
        except (OSError, ValueError) as exc:
            return mcp_result(failure("invalid_file_path", str(exc)))
        payload = await relay.call(args)
        if args["action"] == "download":
            payload = save_download(payload, args["output_path"])
        elif payload.get("ok") and inputs is not None:
            try:
                if upload_inputs(args["files"]) != inputs:
                    raise ValueError("local input changed during selection")
            except (OSError, ValueError) as exc:
                payload = failure("upload_input_changed", str(exc), "action_confirmed")
            else:
                payload = {**payload, "result": {**payload["result"], "selected_files": inputs}}
        return mcp_result(payload, detail, view)

    return server


async def main(port: int = 7016, timeout: float = 30):
    relay = BrowserRelay(port=port, timeout=timeout)
    await relay.start()
    # Also permits tests to request port0 without reserving a racy candidate port.
    print(f"browser_relay_listening=127.0.0.1:{relay.port}", file=sys.stderr, flush=True)
    try:
        server = create_server(relay)
        async with stdio_server() as (read, write):
            with guard(force=True):
                await server.run(read, write, server.create_initialization_options())
    finally:
        await relay.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7016)
    parser.add_argument("--timeout", type=float, default=30)
    options = parser.parse_args()
    try:
        asyncio.run(main(options.port, options.timeout))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
