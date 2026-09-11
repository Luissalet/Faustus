"""agent_tools/board_tools.py — project work board tools for the agent
(Lote 92, OBJ-6).

BOARD_RESEARCH.md's own framing: "what's pending on this project" should not
mean the agent rereads a markdown backlog in full — it should be a handful of
small tools over `src.project_board`, the same shape `git_tools.py` already
gives git. Eight thin executors:

    board_list     read   filtered listing of this project's issues
    board_ready    read   issues with no open blocker -- what to work on next
    board_get      read   one issue's full detail (body, comments, links, refs)
    board_create   write  file a new issue; returns its id
    board_update   write  change status/priority/assignee/title/body
    board_comment  write  add a comment
    board_link     write  relate two issues (blocks/relates_to/...)
    board_claim    write  atomically claim an issue (mark in_progress + assign)

The project is resolved from `ctx["project_id"]` -- the same context key
`git_tools._resolve_repo_root` already reads to find "the project's repo"
(src/agent_tools/git_tools.py). A chat with no project bound has no board to
act on: every tool here refuses up front with `error_class` `board.no_project`
rather than guessing a project or falling back to some global list.

No approval gate, no agent git-style policy check: a board issue is the
user's own private data with no external side effect (no remote host, no
message sent to anyone) -- the same class `manage_documents`/`manage_notes`
sit in, not the class `git_push`/`send_email` do. See
`src/tool_capabilities.py` and `src/tool_security.py` for the classification.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from src import project_board

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument parsing (same permissive JSON-object shape every action-dispatched
# tool in this codebase accepts — see git_tools.py / spreadsheet_tools.py)
# ---------------------------------------------------------------------------
def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _owner(ctx: dict) -> str:
    return str((ctx or {}).get("owner") or "")


def _project_id(ctx: dict) -> Optional[str]:
    pid = str((ctx or {}).get("project_id") or "").strip()
    return pid or None


def _no_project(tool: str) -> Dict[str, Any]:
    return {
        "error": f"{tool}: this chat has no project -- the board belongs to a project. "
                 "Open or create a project for this chat first.",
        "exit_code": 1, "error_class": "board.no_project",
    }


def _key_for(project_id: str, owner: str) -> str:
    from services.projects import board_key_for_project
    return board_key_for_project(project_id, owner or None) or "TASK"


def _board_error(tool: str, exc: project_board.BoardError) -> Dict[str, Any]:
    return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": exc.error_class}


def _issue_in_project(tool: str, issue_id: str, project_id: str):
    """`(issue, None)` when `issue_id` exists and belongs to `project_id`,
    else `(None, error_result)` -- a board tool must never read or mutate an
    issue from a DIFFERENT project just because the caller guessed its id."""
    issue = project_board.get(issue_id)
    if not issue or issue.get("project_id") != project_id:
        return None, {
            "error": f"{tool}: {issue_id} not found in this project", "exit_code": 1,
            "error_class": "board.not_found",
        }
    return issue, None


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
class BoardListTool:
    """`board_list`: filtered listing of this project's issues."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_list")
        try:
            limit = int(args.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        issues, next_cursor = project_board.list_issues(
            project_id,
            status=str(args.get("status") or ""), type=str(args.get("type") or ""),
            assignee=str(args.get("assignee") or ""), q=str(args.get("q") or ""),
            priority=str(args.get("priority") or ""), label=str(args.get("label") or ""),
            limit=limit,
        )
        lines = [f"{i['id']} [{i['priority']}] {i['type']}: {i['title']} ({i['status']})" for i in issues]
        return {"output": "\n".join(lines) or "(no issues)", "exit_code": 0,
                "issues": issues, "next_cursor": next_cursor}


class BoardReadyTool:
    """`board_ready`: issues with no open blocker -- what to pick up next,
    without reasoning over the whole backlog (BOARD_RESEARCH.md's `bd ready`)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_ready")
        issues = project_board.ready_issues(project_id)
        lines = [f"{i['id']} [{i['priority']}] {i['type']}: {i['title']}" for i in issues]
        return {"output": "\n".join(lines) or "(nothing ready)", "exit_code": 0, "issues": issues}


class BoardGetTool:
    """`board_get`: one issue's full detail (body, comments, events, links, refs)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_get")
        issue_id = str(args.get("id") or args.get("issue_id") or "").strip()
        if not issue_id:
            return {"error": "board_get: `id` is required", "exit_code": 1}
        issue, err = _issue_in_project("board_get", issue_id, project_id)
        if err:
            return err
        lines = [f"{issue['id']} [{issue['priority']}] {issue['type']}: {issue['title']} ({issue['status']})"]
        if issue.get("assignee"):
            lines.append(f"assignee: {issue['assignee']}")
        if issue.get("blocked_by"):
            lines.append("blocked by: " + ", ".join(issue["blocked_by"]))
        if issue.get("comments"):
            lines.append(f"{len(issue['comments'])} comment(s)")
        if issue.get("body_md"):
            lines.append("")
            lines.append(issue["body_md"])
        return {"output": "\n".join(lines), "exit_code": 0, "issue": issue}


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------
class BoardCreateTool:
    """`board_create`: file a new issue. Returns its id -- the agent should
    cite it back to the user ("Apuntado como FAU-14"), never invent one."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_create")
        title = str(args.get("title") or "").strip()
        if not title:
            return {"error": "board_create: `title` is required", "exit_code": 1}
        owner = _owner(ctx)
        labels = args.get("labels")
        links = args.get("links")
        try:
            issue = project_board.create_issue(
                project_id, _key_for(project_id, owner),
                type=str(args.get("type") or project_board.DEFAULT_TYPE),
                title=title,
                body_md=str(args.get("body") or args.get("body_md") or ""),
                priority=str(args.get("priority") or project_board.DEFAULT_PRIORITY),
                assignee=str(args.get("assignee") or ""),
                labels=labels if isinstance(labels, list) else [],
                created_by=owner or "agent",
                links=links if isinstance(links, list) else [],
            )
        except project_board.BoardError as exc:
            return _board_error("board_create", exc)
        return {"output": f"Created {issue['id']}: {issue['title']}", "exit_code": 0, "issue": issue}


class BoardUpdateTool:
    """`board_update`: change status/priority/assignee/title/body/labels of
    an existing issue. Refused (`board.invalid_transition`) if `status` tries
    to leave a terminal state (done/wontfix/duplicate) any way but reopening
    to open/in_progress."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_update")
        issue_id = str(args.get("id") or args.get("issue_id") or "").strip()
        if not issue_id:
            return {"error": "board_update: `id` is required", "exit_code": 1}
        _issue, err = _issue_in_project("board_update", issue_id, project_id)
        if err:
            return err
        patch: Dict[str, Any] = {}
        if args.get("title") is not None:
            patch["title"] = args["title"]
        body = args.get("body", args.get("body_md"))
        if body is not None:
            patch["body_md"] = body
        for field in ("type", "status", "priority", "assignee"):
            if args.get(field) is not None:
                patch[field] = args[field]
        if isinstance(args.get("labels"), list):
            patch["labels"] = args["labels"]
        if not patch:
            return {"error": "board_update: nothing to update", "exit_code": 1}
        try:
            issue = project_board.update_issue(issue_id, patch, actor=_owner(ctx) or "agent")
        except project_board.BoardError as exc:
            return _board_error("board_update", exc)
        return {"output": f"Updated {issue['id']} ({issue['status']})", "exit_code": 0, "issue": issue}


class BoardCommentTool:
    """`board_comment`: add a comment to an issue."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_comment")
        issue_id = str(args.get("id") or args.get("issue_id") or "").strip()
        body = str(args.get("body") or args.get("body_md") or "").strip()
        if not issue_id or not body:
            return {"error": "board_comment: `id` and `body` are required", "exit_code": 1}
        _issue, err = _issue_in_project("board_comment", issue_id, project_id)
        if err:
            return err
        try:
            comment = project_board.add_comment(issue_id, body, author=_owner(ctx) or "agent")
        except project_board.BoardError as exc:
            return _board_error("board_comment", exc)
        return {"output": f"Commented on {issue_id}", "exit_code": 0, "comment": comment}


class BoardLinkTool:
    """`board_link`: relate two issues of this project (blocks/blocked_by/
    relates_to/duplicate_of/discovered_from)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_link")
        issue_id = str(args.get("id") or args.get("issue_id") or "").strip()
        kind = str(args.get("kind") or "").strip().lower()
        target = str(args.get("target") or "").strip()
        if not issue_id or not kind or not target:
            return {"error": "board_link: `id`, `kind` and `target` are required", "exit_code": 1}
        _issue, err = _issue_in_project("board_link", issue_id, project_id)
        if err:
            return err
        try:
            link = project_board.add_link(issue_id, kind, target, actor=_owner(ctx) or "agent")
        except project_board.BoardError as exc:
            return _board_error("board_link", exc)
        return {"output": f"Linked {issue_id} {kind} {target}", "exit_code": 0, "link": link}


class BoardClaimTool:
    """`board_claim`: atomically mark an issue in_progress and assign it.
    Refused (`board.claimed`) when another assignee already holds it."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("board_claim")
        issue_id = str(args.get("id") or args.get("issue_id") or "").strip()
        if not issue_id:
            return {"error": "board_claim: `id` is required", "exit_code": 1}
        _issue, err = _issue_in_project("board_claim", issue_id, project_id)
        if err:
            return err
        assignee = str(args.get("assignee") or _owner(ctx) or "agent").strip()
        try:
            issue = project_board.claim(issue_id, assignee, actor=_owner(ctx) or "agent")
        except project_board.BoardError as exc:
            return _board_error("board_claim", exc)
        return {"output": f"Claimed {issue['id']} for {assignee}", "exit_code": 0, "issue": issue}
