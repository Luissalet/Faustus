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
    "poll": "Check current Agora work and pending details. thread_ids may select configured or task-linked threads in repo/path scope — read those, not private chat history. Compact omission counts; focus a thread filter for its recent window. Reads do not acknowledge excerpts; inbox cursors stay separate.",
    "detail": "Read a stored Agora message by its id and digest from coordination_poll, in chunks. Preserve the poll's thread_ids filter when supplied. Follow next_offset until complete. No Hub acknowledgement is sent.",
    "task": "Read the full current Agora task snapshot before acting. Page discussion with next_details_offset + task_digest + task_id from the same snapshot. Before voting, read every complete detail and test that submission. Task reads do not acknowledge excerpts.",
    "board": "Paged read of Agora tasks, agents and scoped locks (default 8/list). Follow next_offset + digest; a changed snapshot rejects paging. full=true for unabridged fields. Other repos are read-only context without mutation authority.",
    "heartbeat": "Report working, waiting or idle to Agora. Renew only scoped locks; other owned locks are an advisory, not a heartbeat block.",
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
    guidance = (" action_scope reports claim/checkpoint/submit/review/done eligibility in this project's scope; choose work there (other repos are context). Hints ≠ permission or delivery receipt — read the task, full discussion, and test the submission before reviewing. Reviewers keep their own identities." if action in {"board", "task", "poll"} else "")
    return {"description": DESCRIPTIONS[action] + guidance + " Identity and connector come from the saved project binding; never impersonate another agent.",
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
