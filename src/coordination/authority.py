"""Per-operation authority, applied to fixed-connector data before dispatch.

Task and held-lock observations are supplied by the native client, never by
model-authored arguments. This module grants no execution or account rights.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from .bindings import Binding, CoordinationError, READ_OPERATIONS


FIELDS = {
    "poll": {"thread_ids", "limit"}, "detail": {"message_id", "digest", "offset", "length", "thread_ids"},
    "task": {"task_id", "details_offset", "task_digest"}, "board": {"offset", "limit", "digest", "full"},
    "heartbeat": {"doing", "state", "note"},
    "add": {"title", "body", "repo", "paths", "kind", "priority", "mentions", "claim", "locks", "ttl_s"},
    "claim": {"task_id", "locks", "ttl_s"},
    "checkpoint": {"task_id", "expected_revision", "payload"},
    "post": {"thread_id", "body", "kind", "mentions", "task_id"},
    "submit": {"task_id", "summary", "branch", "commits", "reviewer"},
    "review": {"task_id", "expected_submission_revision", "verdict", "body"},
    "done": {"task_id", "result", "commits"},
    "lock": {"resources", "task_id", "ttl_s", "note"}, "unlock": {"resources"},
}


def positive_id(value, field):
    if type(value) is not int or value < 1:
        raise CoordinationError(f"{field} must be a positive integer")
    return value


def strings(value, field, maximum=100):
    if not isinstance(value, (list, tuple)) or len(value) > maximum or not all(isinstance(s, str) and s.strip() for s in value):
        raise CoordinationError(f"invalid {field}")
    return value


def paths(binding, repo, values):
    if not isinstance(repo, str) or repo.strip().casefold() not in binding.repos:
        raise CoordinationError("repository is outside the coordination scope")
    for path in strings(values, "paths"):
        if path.startswith(("/", "\\")) or ":" in path:
            raise CoordinationError("task paths must be relative to their repository")
        if not binding.permits_resource(f"path:{repo.strip()}/{path}"):
            raise CoordinationError("task path is outside the coordination scope")


def scoped_task(binding, task, task_id, *, own=False, claim=False):
    positive_id(task_id, "task_id")
    if not isinstance(task, Mapping) or task.get("id") != task_id:
        raise CoordinationError("read this task from the bound connector before acting")
    paths(binding, task.get("repo"), task.get("paths") or [])
    if own and task.get("owner") != binding.actor:
        raise CoordinationError("task is not owned by this coordination actor")
    if claim and task.get("owner") not in (None, "", binding.actor):
        raise CoordinationError("task is already owned by another actor")
    if task.get("status") in {"done", "dropped"}:
        raise CoordinationError("task is already closed")


def resources(binding, values):
    values = strings(values, "resources")
    if not values or not all(binding.permits_resource(r) for r in values):
        raise CoordinationError("resource is outside the coordination scope")


def observed_action(store, binding, operation, args):
    """Resolve a task only from a delivered native observation, never text."""
    args = dict(args)
    task = None
    if operation == "post" and args.get("thread_id") not in binding.thread_ids and "task_id" not in args:
        task = store.observed_task_for_thread(binding, args.get("thread_id"))
        args["task_id"] = task["id"]
    elif operation in {"claim", "checkpoint", "submit", "review", "done"} or (
            operation == "lock" and "task_id" in args) or (
            operation == "post" and args.get("thread_id") not in binding.thread_ids):
        task_id = positive_id(args.get("task_id"), "task_id")
        task = (store.reviewable_task if operation == "review" else store.observed_task)(binding, task_id)
    return args, task


def authorize_operation(binding, operation, *, read_only=False):
    if not binding.active or operation not in binding.operations or operation not in FIELDS:
        raise CoordinationError("operation is outside the active coordination scope")
    if read_only and operation not in READ_OPERATIONS:
        raise CoordinationError("this turn allows coordination reads only")


TASK_ACTIONS = ("claim", "checkpoint", "submit", "review", "done")


def authorize_task_context(binding, operation, args, task):
    """The same task authority checks for dispatch and informational scope hints."""
    if operation == "claim":
        scoped_task(binding, task, args["task_id"], claim=True)
        if "locks" not in args:
            args["locks"] = [f"path:{task['repo']}/{p}" for p in task.get("paths", ())]
        resources(binding, args["locks"])
    if operation in {"checkpoint", "submit", "done"}:
        scoped_task(binding, task, args["task_id"], own=True)
    if operation == "checkpoint":
        observed = (task.get("latest_checkpoint") or {}).get("revision", 0)
        supplied = args.get("expected_revision")
        if type(supplied) is not int or supplied != observed:
            raise CoordinationError("checkpoint revision was not observed; reload and review it")
    if operation == "review":
        scoped_task(binding, task, args["task_id"])
        if task.get("owner") == binding.actor:
            raise CoordinationError("an actor cannot cross-review its own submission")
        revision = args.get("expected_submission_revision")
        if type(revision) is not int or revision < 1 or revision != task.get("submission_revision"):
            raise CoordinationError("submission changed; review the new revision before voting")


def task_action_scope(binding, task, *, read_only=False):
    """Describe task-specific eligibility, never a delivered receipt or a grant.

    Only task metadata read from the fixed connector is used. A board hint
    cannot replace a delivered task/discussion or the live checks at dispatch.
    Payloads, votes and checkpoints are neither fabricated nor dispatched.
    """
    allowed, unavailable = [], {}
    for operation in TASK_ACTIONS:
        args = {"task_id": task.get("id") if isinstance(task, Mapping) else None}
        if operation == "checkpoint":
            cp = task.get("latest_checkpoint") if isinstance(task, Mapping) else None
            args["expected_revision"] = cp.get("revision", 0) if isinstance(cp, Mapping) else 0
        if operation == "review":
            args["expected_submission_revision"] = task.get("submission_revision") if isinstance(task, Mapping) else None
        try:
            authorize_operation(binding, operation, read_only=read_only)
            authorize_task_context(binding, operation, args, task)
        except CoordinationError as exc:
            unavailable.setdefault(str(exc), []).append(operation)
        except (AttributeError, KeyError, TypeError, ValueError):
            unavailable.setdefault("task metadata is incomplete or malformed; reread it before choosing an action", []).append(operation)
        else:
            allowed.append(operation)
    return {"allowed": allowed, "read_only": not allowed,
            "unavailable": [{"actions": actions, "reason": reason} for reason, actions in unavailable.items()],
            "requires_task_delivery": bool(allowed),
            "review_requires_complete_discussion_and_testing": "review" in allowed}


def authorize(binding: Binding, operation: str, args, *, task=None, held_locks=None, read_only=False):
    authorize_operation(binding, operation, read_only=read_only)
    if not isinstance(args, Mapping) or set(args) - FIELDS[operation]:
        raise CoordinationError("unknown or authority-changing coordination arguments")
    args = dict(args)
    for key in ("task_id", "thread_id", "message_id"):
        if key in args:
            positive_id(args[key], key)
    if "ttl_s" in args and (type(args["ttl_s"]) is not int or not 60 <= args["ttl_s"] <= 10800):
        raise CoordinationError("lock lifetime must be between 60 and 10800 seconds")
    if "commits" in args:
        if not all(re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", c)
                   for c in strings(args["commits"], "commits")):
            raise CoordinationError("commits must be full hashes")
    if "thread_ids" in args:
        ids = args["thread_ids"]
        if not isinstance(ids, (tuple, list)) or len(ids) > 100 or not all(type(i) is int and i > 0 for i in ids):
            raise CoordinationError("invalid poll thread ids")
    if operation in {"task", "claim", "checkpoint", "submit", "review", "done"} and "task_id" not in args:
        raise CoordinationError("task_id is required")
    for key in ("offset", "details_offset"):
        if key in args and (type(args[key]) is not int or not 0 <= args[key] <= 100_000):
            raise CoordinationError("invalid coordination page offset")
    if operation == "board" and "limit" in args and (type(args["limit"]) is not int or not 1 <= args["limit"] <= 25):
        raise CoordinationError("board limit must be between 1 and 25")
    if "full" in args and type(args["full"]) is not bool:
        raise CoordinationError("full must be boolean")
    for key in ("digest", "task_digest"):
        if key in args and (not isinstance(args[key], str) or not re.fullmatch('[a-f0-9]{64}', args[key])):
            raise CoordinationError("invalid coordination snapshot digest")
    if operation == "add":
        paths(binding, args.get("repo"), args.get("paths") or [])
        if type(args.get("claim", False)) is not bool:
            raise CoordinationError("claim must be boolean")
        if args.get("claim") and not args.get("paths") and not args.get("locks"):
            raise CoordinationError("a claim needs explicit scoped locks")
        if args.get("locks") is not None:
            resources(binding, args["locks"])
        for field in ("title", "body"):
            if field == "title" and (not isinstance(args.get(field), str) or not args[field].strip()):
                raise CoordinationError("title is required")
        if args.get("claim") and not args.get("locks"):
            args["locks"] = [f"path:{args['repo']}/{p}" for p in args["paths"]]
    if operation in TASK_ACTIONS:
        authorize_task_context(binding, operation, args, task)
    if operation == "checkpoint":
        if not isinstance(args.get("payload"), dict):
            raise CoordinationError("checkpoint payload is required")
    if operation == "review":
        if args.get("verdict") not in {"approve", "changes"}:
            raise CoordinationError("review verdict must be approve or changes")
    if operation == "post":
        thread = args.get("thread_id")
        positive_id(thread, "thread_id")
        if thread not in binding.thread_ids:
            scoped_task(binding, task, args.get("task_id"))
            if task.get("thread_id") != thread:
                raise CoordinationError("thread is outside the coordination scope")
        if args.get("kind", "comment") not in {"comment", "proposal", "agree", "disagree"}:
            raise CoordinationError("invalid reply kind")
        if not isinstance(args.get("body"), str) or not args["body"].strip():
            raise CoordinationError("reply body is required")
        args.pop("task_id", None)
    if operation in {"lock", "unlock"}:
        resources(binding, args.get("resources"))
        if operation == "lock" and "task_id" in args:
            scoped_task(binding, task, args["task_id"], own=True)
    if operation == "heartbeat":
        if held_locks is None or not isinstance(held_locks, (tuple, list)):
            raise CoordinationError("read current actor locks before renewing")
        selected = []
        for lock in held_locks:
            if not isinstance(lock, dict) or not isinstance(lock.get("owner"), str) or not isinstance(lock.get("resource"), str):
                raise CoordinationError("invalid current lock observation")
            if lock["owner"] == binding.actor and binding.permits_resource(lock["resource"]):
                selected.append(lock["resource"])
        # Derived by the native client, never accepted from model arguments.
        # An exact list prevents locks acquired after this read from widening
        # renewal. Outside-scope locks do not block heartbeat metadata.
        args["renew_resources"] = sorted(set(selected))
        if args.get("state", "working") not in {"working", "idle", "waiting", "away"}:
            raise CoordinationError("invalid heartbeat state")
    return args
