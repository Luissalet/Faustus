"""Native coordination client over the already configured MCP connector."""
from __future__ import annotations

import json
import hashlib

from .authority import authorize, observed_action, positive_id
from .bindings import CoordinationError, READ_OPERATIONS, StaleBinding, UnknownOutcome


REMOTE = {"board": "board", "heartbeat": "heartbeat", "add": "task_add", "claim": "task_claim",
          "checkpoint": "checkpoint", "post": "post", "submit": "task_submit", "review": "task_review",
          "done": "task_done", "lock": "lock", "unlock": "unlock"}
TASK_FIELDS = ("id", "title", "repo", "paths", "kind", "status", "owner", "reviewer", "thread_id",
               "submission_revision", "reviewed_submission_revision", "commits", "latest_checkpoint")


def compact_task(task):
    result = {key: task.get(key) for key in TASK_FIELDS}
    if isinstance(result.get("latest_checkpoint"), dict):
        cp = result["latest_checkpoint"]
        result["latest_checkpoint"] = {k: cp.get(k) for k in ("revision", "owner", "created")}
        result["latest_checkpoint"]["summary"] = str(cp.get("summary") or "")[:280]
    return result


def compact_board(board, binding):
    project, context = [], []
    for original in board.get("tasks", ()):
        in_project = str(original.get("repo") or "").casefold() in binding.repos
        task = compact_task(original)
        task["scope"] = "project" if in_project else "global_context"
        if not in_project:
            # Global Hub reading is authorized, but its context is not an
            # expansion of the bound project's mutation authority.
            task["read_only"] = True
        (project if in_project else context).append(task)
    tasks = project + context
    return {"counts": board.get("counts", {}), "tasks": tasks[:100],
            "tasks_omitted": max(0, len(tasks) - 100),
            "agents": [{k: a.get(k) for k in ("id", "state", "doing", "stale")} for a in board.get("agents", ())][:40],
            "locks": [{k: lock.get(k) for k in ("resource", "owner", "task_id", "expires")}
                      for lock in board.get("locks", ()) if binding.permits_resource(lock.get("resource"))][:100]}


class Client:
    def __init__(self, store, binding, manager, *, revalidate, disabled_tools=(), read_only=False, tool_policy=None):
        self.store, self.binding, self.manager = store, binding, manager
        self.revalidate = revalidate
        self.disabled_tools = set(disabled_tools)
        self.read_only = read_only
        self.tool_policy = tool_policy

    def live(self):
        self.store.live(self.binding)
        self.revalidate(self.binding)

    async def remote(self, suffix, args):
        self.live()
        name = f"mcp__{self.binding.connector_id}__hub_agora_{suffix}"
        if name in self.disabled_tools or f"hub_agora_{suffix}" in self.disabled_tools or self.binding.connector_id in self.disabled_tools:
            raise CoordinationError("coordination connector tool is disabled")
        if self.tool_policy is not None and self.tool_policy.blocks(name):
            raise CoordinationError("coordination connector tool is blocked by this turn's policy")
        catalog = self.manager.get_all_tools()
        if not any(t.get("qualified_name") == name and not t.get("is_disabled") for t in catalog):
            raise CoordinationError("the bound connector does not offer this coordination operation")
        writing = suffix not in {"inbox", "board", "task", "locks", "thread_recent"}
        try:
            result = await self.manager.call_tool(name, dict(args))
        except Exception as exc:
            if writing:
                raise UnknownOutcome("coordination write lost its result; inspect before retrying") from exc
            raise CoordinationError("coordination read failed") from exc
        if result.get("outcome_unknown") or (isinstance(result.get("structured_content"), dict) and result["structured_content"].get("outcome_unknown")):
            raise UnknownOutcome("coordination result was lost; inspect the task or thread before retrying this write")
        try:
            self.live()
        except CoordinationError as exc:
            if writing:
                raise UnknownOutcome("scope changed after dispatch; the write may have completed; inspect before retrying") from exc
            raise
        if result.get("exit_code") or result.get("error"):
            if writing:
                raise UnknownOutcome("coordination write failed without an effect receipt; inspect before retrying")
            raise CoordinationError(result.get("error") or result.get("stderr") or "coordination call failed; inspect before retrying")
        value = result.get("structured_content")
        if value is None:
            try:
                value = json.loads(result.get("stdout", ""))
            except (ValueError, TypeError) as exc:
                if writing:
                    raise UnknownOutcome("coordination write returned an unreadable result; inspect before retrying") from exc
                raise CoordinationError("coordination connector returned an invalid result") from exc
        if not isinstance(value, dict) or "ok" not in value:
            if writing:
                raise UnknownOutcome("coordination write returned an incomplete result; inspect before retrying")
            raise CoordinationError("coordination connector returned an invalid result")
        if value.get("ok") is not True:
            raise CoordinationError(str(value.get("error") or value.get("detail") or "coordination operation did not succeed")[:2000])
        return {k: v for k, v in value.items() if not k.startswith("_coordination_")}

    async def execute(self, operation, args):
        self.live()
        # Validate fields before even the helper reads; model args never pick a server or actor.
        if operation not in self.binding.operations or (self.read_only and operation not in READ_OPERATIONS):
            raise CoordinationError("operation is outside this turn's coordination scope")
        from .authority import FIELDS
        if not isinstance(args, dict) or operation not in FIELDS or set(args) - FIELDS[operation]:
            raise CoordinationError("unknown or authority-changing coordination arguments")
        args, task = observed_action(self.store, self.binding, operation, args)
        held_locks = None
        if operation == "heartbeat":
            held_locks = (await self.remote("locks", {})).get("locks")
        clean = authorize(self.binding, operation, args, task=task, held_locks=held_locks, read_only=self.read_only)
        if task is not None and operation not in READ_OPERATIONS:
            fresh = (await self.remote("task", {"task_id": task["id"]})).get("task")
            fields = ("id", "repo", "paths", "owner", "thread_id")
            if not isinstance(fresh, dict) or any(
                    type(fresh.get(key)) is not type(task.get(key)) or fresh.get(key) != task.get(key)
                    for key in fields):
                raise CoordinationError("task authority changed; reload with coordination_task before acting")
            # This is a preflight scope check, not a delivered new observation.
            # Keep the original checkpoint/review CAS exactly as the user saw it.
        if operation == "poll":
            return await self.poll(clean)
        if operation == "detail":
            detail = self.store.detail(self.binding, clean["message_id"], threads=clean.get("thread_ids", ()),
                                       offset=clean.get("offset", 0), length=clean.get("length", 2000), expected_digest=clean["digest"])
            return {**detail, "_coordination_delivery": {"binding": self.binding.id, "revision": self.binding.revision,
                    "receipt": detail["receipt"], "threads": clean.get("thread_ids", [])}}
        if operation == "task":
            result = await self.remote("task", {"task_id": clean["task_id"]})
            observed = result.get("task")
            messages = result.get("messages", [])
            if not isinstance(messages, list) or len(messages) > 500:
                raise CoordinationError("task discussion is too large for a complete review snapshot")
            import hashlib
            required = []
            for start in range(0, len(messages), 200):
                page = [{**m, "relevant": True} for m in messages[start:start + 200]]
                cursor = self.store.cursor(self.binding)
                self.store.receive(self.binding, expected_cursor=cursor, next_cursor=cursor,
                                   messages=[], pending_messages=page)
                required.extend({"id": m["id"], "digest": hashlib.sha256(m["body"].encode("utf-8")).hexdigest()} for m in page)
            receipt = self.store.observe_task(self.binding, observed, required_messages=required)
            observed = {k: v for k, v in observed.items() if not k.startswith("_coordination_")}
            return {"task": observed, "message_count": len(messages), "review_details": required,
                    "pending": self.store.pending(self.binding, limit=100),
                    "_coordination_delivery": {"binding": self.binding.id, "revision": self.binding.revision,
                                               "task_receipt": receipt}}
        if operation == "board":
            return compact_board(await self.remote("board", {}), self.binding)
        if operation not in REMOTE:
            raise CoordinationError("unsupported coordination operation")
        # Read revisions are never replaced with a fresh number while executing an old vote.
        # The Hub enforces task ownership and checkpoint/review CAS at the actual mutation.
        result = await self.remote(REMOTE[operation], {**clean, "agent": self.binding.actor})
        if isinstance(result.get("task"), dict):
            receipt = self.store.observe_task(self.binding, result["task"], required_messages=None)
            result["_coordination_delivery"] = {"binding": self.binding.id, "revision": self.binding.revision,
                                                "task_receipt": receipt}
        return result

    async def poll(self, args):
        threads = args.get("thread_ids", [])
        limit = args.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CoordinationError("poll limit must be between 1 and 100")
        # Inbox has a native read-only peek with an explicit cursor. Its read mark
        # and mention acknowledgements are left alone; no hidden heartbeat write.
        since = self.store.cursor(self.binding, threads)
        inbox = await self.remote("inbox", {"agent": self.binding.actor, "peek": True, "mine_only": False, "since_id": since})
        board = await self.remote("board", {})
        scope_tasks = {t["id"] for t in board.get("tasks", ()) if str(t.get("repo") or "").casefold() in self.binding.repos}
        page = []
        all_messages = inbox.get("messages", [])
        if not isinstance(all_messages, list):
            raise CoordinationError("invalid coordination inbox page")
        for original in all_messages:
            if not isinstance(original, dict):
                raise CoordinationError("invalid coordination inbox message")
            item = dict(original)
            item["relevant"] = (self.binding.actor in (item.get("mentions") or []) or item.get("author") == "luis"
                                or item.get("task_owner") == self.binding.actor or item.get("task_id") in scope_tasks)
            if not threads or item.get("thread_id") in threads:
                page.append(item)
        # The cursor covers the complete fetched page, including filtered-out
        # posts. A different thread filter always has a separate cursor.
        next_cursor = max([since, *[positive_id(m.get("id"), "message id") for m in all_messages]])
        pending_mentions = []
        for original in inbox.get("pending", ()):
            if not isinstance(original, dict):
                raise CoordinationError("invalid pending coordination mention")
            if not threads or original.get("thread_id") in threads:
                pending_mentions.append({**original, "relevant": True})
        recent = await self.recent(threads, limit)
        self.live()  # Connector/policy revocation is broader than the Store binding revision.
        self.store.receive(self.binding, expected_cursor=since, next_cursor=next_cursor, messages=page,
                           pending_messages=pending_mentions, threads=threads)
        for start in range(0, len(recent["cache"]), 200):
            self.store.receive(self.binding, expected_cursor=next_cursor, next_cursor=next_cursor, messages=[],
                               pending_messages=recent["cache"][start:start + 200], threads=threads)
        return {"actor": self.binding.actor, "binding_revision": self.binding.revision,
                "cursor": next_cursor, "page_received": len(all_messages), "has_more": len(all_messages) >= 100,
                "pending": self.store.pending(self.binding, threads, limit),
                "reviews": [compact_task(t) for t in inbox.get("reviews", ()) if t.get("id") in scope_tasks][:40],
                "changes": [compact_task(t) for t in inbox.get("changes", ()) if t.get("owner") == self.binding.actor][:40],
                "board": compact_board(board, self.binding), "hub_acknowledged": False,
                "recent": recent["refs"], "recent_unavailable": recent["unavailable"],
                "recent_detail_unavailable": recent["detail_unavailable"],
                "recent_threads_omitted": recent["threads_omitted"], "recent_pages": recent["pages"]}

    async def recent(self, threads, limit):
        """Read a small recent window only when the bound Hub advertises it.

        Never fall back to the full-thread reader. Recent cache entries and
        unread history have the same detail receipts but separate coverage:
        only the fetched inbox page can move its durable cursor.
        """
        selected = [tid for tid in self.binding.thread_ids if not threads or tid in threads]
        out = {"refs": [], "cache": [], "unavailable": [], "detail_unavailable": [], "pages": [],
               "threads_omitted": max(0, len(selected) - 4)}
        if not selected:
            return out
        name = f"mcp__{self.binding.connector_id}__hub_agora_thread_recent"
        tool = next((t for t in self.manager.get_all_tools() if t.get("qualified_name") == name), None)
        schema = (tool or {}).get("input_schema") or {}
        schema = schema if isinstance(schema, dict) else {}
        props = schema.get("properties", {})
        props = props if isinstance(props, dict) else {}
        integer_fields = all(isinstance(props.get(key), dict) and props[key].get("type") == "integer"
                             and props[key].get("minimum") == 1 for key in ("thread_id", "limit", "before_id"))
        available = (bool(tool) and not tool.get("is_disabled") and schema.get("additionalProperties") is False
                     and schema.get("required") == ["thread_id"] and set(props) == {"thread_id", "limit", "before_id"}
                     and integer_fields and props["limit"].get("maximum") == 200)
        if not available:
            out["unavailable"] = [{"thread_id": tid, "reason": "bounded recent reader is not available in the bound connector"}
                                  for tid in selected[:4]]
            return out
        for tid in selected[:4]:
            try:
                result = await self.remote("thread_recent", {"thread_id": tid, "limit": min(limit, 5)})
                messages = result.get("messages")
                if (not isinstance(messages, list) or len(messages) > min(limit, 5)
                        or not isinstance(result.get("thread"), dict) or result["thread"].get("id") != tid):
                    raise CoordinationError("invalid bounded recent thread page")
                refs, cache, unavailable = [], [], []
                for message in messages:
                    if (not isinstance(message, dict) or message.get("thread_id") != tid
                            or not isinstance(message.get("body"), str) or len(message["body"]) > 20_000
                            or type(message.get("body_chars")) is not int or message["body_chars"] < len(message["body"])
                            or type(message.get("body_truncated")) is not bool):
                        raise CoordinationError("invalid bounded recent message")
                    mid = positive_id(message.get("id"), "message id")
                    digest = message.get("body_digest")
                    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                        raise CoordinationError("invalid recent message digest")
                    complete = not message["body_truncated"] and message["body_chars"] == len(message["body"])
                    if complete and hashlib.sha256(message["body"].encode("utf-8")).hexdigest() != digest:
                        raise CoordinationError("recent message body does not match its digest")
                    refs.append({**{k: message.get(k) for k in ("id", "thread_id", "author", "kind", "created")},
                                 "body_excerpt": message["body"][:160], "body_chars": message["body_chars"],
                                 "digest": digest, "detail_ref": f"message:{mid}", "detail_available": complete})
                    if threads:
                        refs[-1]["thread_ids"] = list(threads)
                    if complete:
                        cache.append({**message, "relevant": True})
                    else:
                        unavailable.append({"thread_id": tid, "message_id": mid, "digest": digest,
                                            "reason": "recent body is truncated; complete detail remains unavailable"})
                pagination = result.get("pagination")
                if not isinstance(pagination, dict) or type(pagination.get("has_more")) is not bool:
                    raise CoordinationError("invalid recent pagination metadata")
                out["pages"].append({"thread_id": tid, **{k: pagination.get(k) for k in ("has_more", "next_before_id")}})
                out["refs"].extend(refs)
                out["cache"].extend(cache)
                out["detail_unavailable"].extend(unavailable)
            except StaleBinding:
                raise
            except CoordinationError as exc:
                out["unavailable"].append({"thread_id": tid, "reason": str(exc)[:240]})
        return out
