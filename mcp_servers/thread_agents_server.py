"""Per-thread agent control plane through Faustus's own API (/api/threads)."""
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_servers.app_dock_server import _headers
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("thread_agents")
S = {"type": "string"}
SID = {"session_id": {"type": "string", "description": "Studio conversation (session) id."}}
# name: (method, path, properties, required, read_only, description)
SPECS = {
    "thread_agents_list": ("GET", "/agents", {}, [], True,
        "List the agents a thread can use: 'faustus' (the thread's own model) and every CLI runner "
        "Faustus can run one task with (Claude Code, Codex, OpenCode…), with whether it is installed and where "
        "it lives: 'local' (this machine), 'pc' (the person's PC, reached through the Hub when Faustus runs on "
        "a Spark) or none."),
    "thread_agent_get": ("GET", "/{session_id}/agent", SID, ["session_id"], True,
        "Read who answers this thread now (agent, model), its workspace, its worktree and recent runs."),
    "thread_loop": ("GET", "/{session_id}/loop", SID, ["session_id"], True,
        "Read the agent loop of the thread's running or last turn: phase plan|act|observe|done (from the turn's own "
        "events: model text = plan, tool call = act, tool result = observe), lap number, tool in flight, elapsed "
        "seconds, tokens when reported, the steps taken (plan, tools, results, commit, delivery) and a checklist of "
        "work practices (tests, browser check, docs, commit identity, no evidence files, Agora delivery) each "
        "ok|fail|unknown|pending with the call that proves it. loop is null when this server has not run a turn "
        "for the thread since it started."),
    "thread_agent_set": ("PUT", "/{session_id}/agent", {**SID, "agent": S, "model": S, "workspace": S},
        ["session_id", "agent"], False,
        "Change the agent that works this thread mid-conversation: 'faustus' or a runner key. With a runner, "
        "thread_send messages go to it with the thread's context and its answers return to the thread; its own "
        "session is resumed on later turns. workspace is the folder it works in when the thread has no worktree."),
    "thread_send": ("POST", "/{session_id}/turn", {**SID, "text": S}, ["session_id", "text"], False,
        "Send a message to the thread's current runner agent. Returns a job; the answer is appended to the "
        "thread when it ends (poll thread_job)."),
    "thread_handoff": ("POST", "/{session_id}/handoff",
        {**SID, "agent": S, "task": S, "model": S, "endpoint_id": S}, ["session_id", "agent", "task"], False,
        "Pass one concrete task to another agent with a summary of the thread. The result is appended to the "
        "same thread; the thread's own agent does not change. Returns a job. agent 'faustus' runs a Faustus "
        "subagent on the thread's model, or on model / endpoint_id when given; a runner on the PC is run there "
        "through the Hub."),
    "thread_job": ("GET", "/{session_id}/jobs/{job_id}", {**SID, "job_id": S}, ["session_id", "job_id"], True,
        "Read a thread run: status running|done|failed|cancelled, agent, seconds and error."),
    "thread_job_cancel": ("POST", "/{session_id}/jobs/{job_id}/cancel", {**SID, "job_id": S},
        ["session_id", "job_id"], False, "Stop a running thread run."),
    "thread_worktree_create": ("POST", "/{session_id}/worktree", {**SID, "repo": S, "base": S, "branch": S},
        ["session_id", "repo"], False,
        "Give the thread its own git worktree of repo on a new branch (default thread/<id>) from base "
        "(default the repo's current HEAD). Runner turns then work inside it."),
    "thread_worktree_status": ("GET", "/{session_id}/worktree", SID, ["session_id"], True,
        "The thread worktree's branch, changed files, commits and the diff against the commit it started from."),
    "thread_worktree_commit": ("POST", "/{session_id}/worktree/commit", {**SID, "message": S},
        ["session_id", "message"], False, "Stage everything in the thread worktree and commit it."),
    "thread_worktree_push": ("POST", "/{session_id}/worktree/push", SID, ["session_id"], False,
        "Push the thread branch (publishes it on origin the first time)."),
    "thread_worktree_pr": ("POST", "/{session_id}/worktree/pr",
        {**SID, "title": S, "body": S, "base": S, "draft": {"type": "boolean"}}, ["session_id", "title"], False,
        "Open a pull request from the thread branch (already pushed) into base, default the branch the "
        "worktree started from. Uses the GitHub token or the gh CLI."),
    "thread_worktree_remove": ("DELETE", "/{session_id}/worktree", {**SID, "force": {"type": "boolean"}},
        ["session_id"], False, "Remove the thread worktree folder; the branch and its commits stay."),
}


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=spec[5],
                 inputSchema={"type": "object", "properties": spec[2], "required": spec[3]},
                 annotations=ToolAnnotations(readOnlyHint=spec[4], destructiveHint=False))
            for name, spec in SPECS.items()]


@server.call_tool()
async def call_tool(name, arguments):
    if name not in SPECS:
        return [TextContent(type="text", text=json.dumps({"error": "Unknown thread tool", "exit_code": 1}))]
    method, path, *_ = SPECS[name]
    args = dict(arguments or {})
    for key in ("session_id", "job_id"):
        if "{" + key + "}" in path:
            path = path.replace("{" + key + "}", quote(str(args.pop(key, "")), safe=""))
    try:
        base = os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.request(method, base.rstrip("/") + "/api/threads" + path, headers=_headers(),
                                            **({"params": args} if method in ("GET", "DELETE") else {"json": args}))
        payload = response.json()
        if not response.is_success:
            payload = {"exit_code": 1, "status_code": response.status_code, "error": payload}
    except (httpx.HTTPError, ValueError, OSError) as error:
        payload = {"exit_code": 1, "error": "Thread API request failed: " + type(error).__name__}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
