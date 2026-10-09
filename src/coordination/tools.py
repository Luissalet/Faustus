"""Typed native tools. Their schemas confer no authority without a binding."""
from .authority import FIELDS
from .bindings import OPERATIONS, READ_OPERATIONS

NAMES = {f"coordination_{op}": op for op in sorted(OPERATIONS)}
REQUIRED = {"task": ["task_id"], "detail": ["message_id", "digest"], "add": ["title", "repo"],
            "claim": ["task_id"], "checkpoint": ["task_id", "expected_revision", "payload"],
            "post": ["thread_id", "body"], "submit": ["task_id", "summary"],
            "review": ["task_id", "expected_submission_revision", "verdict", "body"],
            "done": ["task_id", "result"], "lock": ["resources"], "unlock": ["resources"]}
DESCRIPTIONS = {
    "poll": "Check current Agora work and pending details. Explicit thread_ids may select configured threads or threads linked to current tasks inside the repository/path scope. Read latest discussion there rather than searching private chat history for current Agora state. Omission counts expose compact limits; focus a thread filter to read its recent window. Read complete stored details before responding. No excerpts are acknowledged; inbox cursors stay independent from recent reading.",
    "detail": "Read a stored Agora message by its id and digest from coordination_poll, in chunks. Preserve the poll's thread_ids filter when supplied. Follow next_offset until complete. No Hub acknowledgement is sent.",
    "task": "Read the current full task snapshot before acting. Discussion references are paged: follow next_details_offset with task_digest and task_id to read all references from the same delivered snapshot. Before voting, read every complete discussion detail and test that submission. Task reads do not acknowledge excerpts.",
    "board": "Read current Agora tasks, agents and scoped locks in pages (default eight entries per list). Follow next_offset with the returned digest; a changed snapshot rejects paging. full=true retrieves unabridged fields. Other repositories remain read-only global context and never confer mutation authority.",
    "heartbeat": "Report working, waiting or idle to Agora and renew only the bound actor's scoped locks.",
    "add": "Create an Agora task in the project's authorized repositories and paths. claim optionally takes scoped locks. Do this before changing a repository.",
    "claim": "Claim a task previously read with coordination_task and take scoped locks atomically. Another actor's task cannot be taken over.",
    "checkpoint": "Persist progress, evidence and next steps for your task. Supply the checkpoint revision you read, zero initially. Declared evidence is not independently verified.",
    "post": "Reply to the project's coordination thread or a task's observed thread. Use mentions to distribute authorized work or request cross-review.",
    "submit": "Submit your task and full commit hashes for cross-review with concrete changes and validation evidence.",
    "review": "Vote on another actor's submission after reading and testing that revision. Supply that observed submission revision; a stale vote is rejected, never refreshed automatically.",
    "done": "Close your integrated task with result and hashes, subject to the Hub's review requirements. No force or review exemption is accepted.",
    "lock": "Acquire explicitly authorized coordination resources before model evaluation or integration. Does not start a model or modify a repository.",
    "unlock": "Release explicitly authorized resources owned by your coordination actor.",
}


def definition(action):
    properties = {}
    for key in sorted(FIELDS[action]):
        if key in {"task_id", "thread_id", "message_id", "limit", "ttl_s", "priority", "expected_revision", "expected_submission_revision", "offset", "length", "details_offset"}:
            properties[key] = {"type": "integer", "minimum": 0 if key in {"priority", "expected_revision", "offset", "details_offset"} else 1}
        elif key in {"thread_ids", "resources", "paths", "locks", "mentions", "commits"}:
            properties[key] = {"type": "array", "maxItems": 100, "items": {"type": "integer", "minimum": 1} if key == "thread_ids" else {"type": "string"}}
        elif key in {"claim", "full"}:
            properties[key] = {"type": "boolean"}
        elif key == "payload":
            properties[key] = {"type": "object", "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
                "workspace": {"type": "string", "maxLength": 2000},
                "branch": {"type": "string", "maxLength": 500},
                "base_head": {"type": "string", "pattern": "^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$"},
                "head": {"type": "string", "pattern": "^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$"},
                "next_steps": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 2000}},
                "artifacts": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 2000}},
                "tests": {"type": "array", "maxItems": 50, "items": {
                    "type": "object", "properties": {
                        "name": {"type": "string", "maxLength": 300},
                        "status": {"type": "string", "enum": ["passed", "failed", "not_run"]},
                        "evidence": {"type": "string", "maxLength": 4000}},
                    "required": ["name", "status", "evidence"], "additionalProperties": False}},
            }, "required": ["summary"], "additionalProperties": False}
        else:
            properties[key] = {"type": "string"}
    return {"description": DESCRIPTIONS[action] + " Identity and connector come from the saved project binding; never impersonate another agent.",
            "parameters": {"type": "object", "properties": properties, "required": REQUIRED.get(action, []), "additionalProperties": False}}


def parse(name, content):
    import json
    from .bindings import CoordinationError
    if name not in NAMES:
        raise CoordinationError("unknown coordination tool")
    args = json.loads(content) if isinstance(content, str) else content
    if not isinstance(args, dict):
        raise CoordinationError("coordination arguments must be an object")
    return NAMES[name], args
