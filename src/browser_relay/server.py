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
import uuid

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations

from src.browser_relay.protocol import SCHEMA, failure, validate_arguments
from src.browser_relay.presentation import present_payload
from src.browser_relay.transport import BrowserRelay
from src.browser_relay.video import encoder, save_video
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
    recording_paths = {}

    @server.list_tools()
    async def list_tools():
        return [Tool(name="browser_session", description=(
            "Use the existing browser session through its installed extension. tabs lists real tabs; "
            "tab actions require an explicit tab_id. Use refs from the latest snapshot for click/type; "
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
            "To READ a page (a product list, prices, the state of buy buttons, an article) use view:{text:true}: one "
            "call returns the page as reading lines in document order, controls as [role ref] label (disabled marked), "
            "paged by offset/limit (400 lines) and filtered by query; prefer it over paging AX rows. "
            "Look and move like a person, in the background: reload (waits for the new page), back/forward (tab history), "
            "scroll {direction up|down|left|right, amount ticks, optional ref to turn the wheel over that element; a ref "
            "alone brings it into view}, hover {ref} (menus, tooltips), select {ref, value: option value or visible text}, "
            "screenshot with region [x0,y0,x1,y1] or ref to zoom on a part (see what the text does not say: images, "
            "colours, a greyed button), open_tab {url} opens a page in a new background tab (read a product page without "
            "touching the person's tab) and close_tab closes only tabs opened that way. tabs marks opened_by_faustus. "
            "wait {seconds <= 10} gives a page time (a loading list, a check page) and reads it again. "
            "Text view shows where links go (-> url), so a page can be opened with open_tab instead of clicking. "
            "reload_extension makes the browser load new extension code; the relay reconnects by itself. "
            "Mutations and navigations accept view too, so a reload can come back already as text. "
            "view.visible keeps only what is on screen now (combine with text after a scroll) and returns viewport "
            "{x, y, width, height, page_width, page_height}: y/page_height says how far down the page you are. "
            "sessions lists durable names; session_save {tab_id,name} saves the current URL in this browser profile; "
            "session_open {name} reuses that saved tab or restores it in the background, including after restart; "
            "session_forget removes only the name. adopt_tab {tab_id,name?,reason?} asks the person to cede a tab "
            "from the extension toolbar. handoff {tab_id,reason?} gives them a Te toca card for login/2FA/payment; "
            "handoff_status observes it. Only a real Continue click in the extension finishes a human turn; "
            "while waiting, observe but do not send input, and take a fresh snapshot after Continue. "
            "har_start {tab_id,include_bodies?} begins explicit network capture; har_stop {tab_id,output_path} "
            "writes a new local HAR with redirects, timings, failures and optionally available bodies. "
            "recording_start {tab_id,output_path,cursor?} records this tab and buffers the last minute; "
            "recording_stop exports WebM to that path (or a new output_path); last_minute {tab_id,output_path} "
            "exports the recent/frozen minute without stopping. Recording needs local ffmpeg, captures no audio, "
            "and starts only on request. A failing action automatically writes the buffered minute beside the "
            "chosen recording path, including frames received before disconnect. File receipts contain bytes/SHA; "
            "video frames and HAR bytes are never inline in MCP. Names survive restarts; recordings do not resume themselves. "
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
        if isinstance(view, dict) and view.get("visible"):
            args["view"] = {"visible": True}         # the extension measures the viewport only when asked
        try:
            inputs = upload_inputs(args["files"]) if args["action"] == "upload" else None
            if "output_path" in args:
                download_target(args["output_path"])
            if args["action"] == "recording_start":
                await asyncio.to_thread(encoder)
        except (OSError, ValueError) as exc:
            return mcp_result(failure("invalid_file_path", str(exc)))
        payload = await relay.call(args)
        if args["action"] == "last_minute" and not payload.get("ok") and payload.get("error", {}).get("code") in {"extension_unavailable", "extension_disconnected", "capture_not_started"}:
            recent = relay.recent_video(args["tab_id"])
            if recent:
                payload = {"ok": True, "phase": "action_confirmed", "result": {"tab_id": args["tab_id"], "video": recent, "source": "relay_buffer"}}
        if args["action"] == "recording_start" and payload.get("ok"):
            recording_paths[args["tab_id"]] = args["output_path"]
        if args["action"] in {"download", "har_stop"}:
            payload = save_download(payload, args["output_path"])
        elif args["action"] in {"recording_stop", "last_minute"} and payload.get("ok"):
            result = dict(payload["result"])
            video = result.pop("video", None)
            target = args.get("output_path") or recording_paths.get(args["tab_id"])
            try:
                if not target:
                    raise ValueError("Choose an output_path for this video")
                receipt = await save_video(video or {}, target)
            except (OSError, ValueError) as exc:
                payload = failure("video_write_failed", str(exc), "action_confirmed")
            else:
                payload = {**payload, "result": {**result, "file": receipt}}
                if args["action"] == "recording_stop":
                    recording_paths.pop(args["tab_id"], None)
                    relay.forget_video(args["tab_id"])
        elif payload.get("ok") and inputs is not None:
            try:
                if upload_inputs(args["files"]) != inputs:
                    raise ValueError("local input changed during selection")
            except (OSError, ValueError) as exc:
                payload = failure("upload_input_changed", str(exc), "action_confirmed")
            else:
                payload = {**payload, "result": {**payload["result"], "selected_files": inputs}}
        # Strip private evidence even when encoding fails. Preserve the original
        # action error and its phase, with a separate artifact receipt/error.
        video = payload.pop("failure_video", None)
        if not payload.get("ok") and payload.get("error", {}).get("code") != "human_turn":
            tab_id = args.get("tab_id")
            target = recording_paths.get(tab_id)
            video = video or (relay.recent_video(tab_id) if target else None)
            if video and target:
                path = Path(target)
                failure_path = path.with_name(path.stem + ".failure-" + uuid.uuid4().hex[:12] + ".webm")
                try:
                    payload["failure_recording"] = await save_video(video, str(failure_path))
                except (OSError, ValueError) as exc:
                    payload["artifact_error"] = str(exc)
        if payload.get("ok") and args["action"] in {"release", "close_tab"}:
            recording_paths.pop(args["tab_id"], None)
            relay.forget_video(args["tab_id"])
        return mcp_result(payload, detail, view)

    return server


async def main(port: int = 7016, timeout: float = 30, peer_wait: float = 8):
    relay = BrowserRelay(port=port, timeout=timeout, peer_wait=peer_wait)
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
    parser.add_argument("--peer-wait", type=float, default=8,
                        help="seconds a call waits for the extension to (re)connect before extension_unavailable")
    options = parser.parse_args()
    try:
        asyncio.run(main(options.port, options.timeout, options.peer_wait))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
