"""Native coordination client over the already configured MCP connector."""
from __future__ import annotations

import json
import hashlib

from .authority import authorize, observed_action, positive_id, paths, repositories, task_action_scope
from .bindings import CoordinationError, READ_OPERATIONS, StaleBinding, UnknownOutcome


REMOTE = {"board": "board", "heartbeat": "heartbeat", "add": "task_add", "claim": "task_claim",
          "checkpoint": "checkpoint", "post": "post", "submit": "task_submit", "review": "task_review",
          "done": "task_done", "lock": "lock", "unlock": "unlock"}
TASK_FIELDS = ("id", "title", "repo", "paths", "kind", "status", "owner", "reviewer", "thread_id",
               "submission_revision", "reviewed_submission_revision", "commits", "latest_checkpoint")


def compact_task(task):
    result = {key: task.get(key) for key in TASK_FIELDS}
    title = str(result.get("title") or "")
    result["title"] = title[:120]
    if len(title) > 120:
        result["title_chars"] = len(title)
    for key, maximum in (("paths", 4), ("commits", 2)):
        values = result.get(key)
        if isinstance(values, list) and len(values) > maximum:
            result[key] = values[:maximum]
            result[key + "_omitted"] = len(values) - maximum
    if isinstance(result.get("latest_checkpoint"), dict):
        cp = result["latest_checkpoint"]
        result["latest_checkpoint"] = {k: cp.get(k) for k in ("revision", "owner", "created")}
        result["latest_checkpoint"]["summary"] = str(cp.get("summary") or "")[:280]
    return result


def compact_board(board, binding, *, offset=0, limit=8, full=False, read_only=False):
    project, context = [], []
    for original in board.get("tasks", ()):
        try:
            repositories(binding, original.get("repo"))
        except CoordinationError:
            in_project = False
        else:
            in_project = True
        task = ({key: original.get(key) for key in TASK_FIELDS} if full else compact_task(original))
        task["scope"] = "project" if in_project else "global_context"
        if not in_project:
            # Global Hub reading is authorized, but its context is not an
            # expansion of the bound project's mutation authority.
            task["read_only"] = True
        (project if in_project else context).append((original, task))
    tasks = project + context
    # Evaluate authority on the complete source paths, never on truncated rows,
    # and only for this bounded page rather than for the entire global board.
    shown = [{**row, "action_scope": task_action_scope(binding, original, read_only=read_only)}
             for original, row in tasks[offset:offset + limit]]
    agents = [{k: a.get(k) for k in ("id", "state", "doing", "stale")} for a in board.get("agents", ())]
    locks = [{k: lock.get(k) for k in ("resource", "owner", "task_id", "expires")}
             for lock in board.get("locks", ()) if binding.permits_resource(lock.get("resource"))]
    raw_tasks = [{key: original.get(key) for key in TASK_FIELDS} for original in board.get("tasks", ())]
    digest = hashlib.sha256(json.dumps({"tasks": raw_tasks, "agents": agents, "locks": locks},
                                     sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if not full:
        for agent in agents:
            doing = str(agent.get("doing") or "")
            agent["doing"] = doing[:160]
            if len(doing) > 160:
                agent["doing_chars"] = len(doing)
    result = {"space_id": binding.space_id, "counts": board.get("counts", {}), "tasks": shown,
            "tasks_total": len(tasks), "tasks_omitted": len(tasks) - len(shown),
            "offset": offset, "next_offset": offset + limit if offset + limit < max(len(tasks), len(agents), len(locks)) else None,
            "digest": digest,
            "agents": agents[offset:offset + limit], "agents_omitted": len(agents) - len(agents[offset:offset + limit]),
            "locks": locks[offset:offset + limit], "locks_omitted": len(locks) - len(locks[offset:offset + limit])}
    # A smaller page preserves every row and every existing snapshot digest.
    # All lists advance together, so reducing metadata cannot skip a task.
    if not full:
        while len(json.dumps(result, ensure_ascii=False)) > 6000 and limit > 1:
            limit -= 1
            for key, values in (("tasks", shown), ("agents", agents[offset:offset + limit]), ("locks", locks[offset:offset + limit])):
                result[key] = values[:limit]
                total = len(tasks) if key == "tasks" else len(agents) if key == "agents" else len(locks)
                result[key + "_omitted"] = total - len(result[key])
            result["next_offset"] = offset + limit if offset + limit < max(len(tasks), len(agents), len(locks)) else None
    return result


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
            result = await self.manager.call_tool(name, {**args, 'space_id': self.binding.space_id})
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
            lock_snapshot = await self.remote("locks", {})
            held_locks = lock_snapshot.get("locks")
        clean = authorize(self.binding, operation, args, task=task, held_locks=held_locks, read_only=self.read_only)
        outside_count = 0
        selective_heartbeat = False
        heartbeat_selection = []
        if operation == "heartbeat":
            outside_count = sum(lock["owner"] == self.binding.actor and not self.binding.permits_resource(lock["resource"])
                                for lock in held_locks)
            heartbeat_selection = clean["renew_resources"]
            name = f"mcp__{self.binding.connector_id}__hub_agora_heartbeat"
            offered = next((tool for tool in self.manager.get_all_tools() if tool.get("qualified_name") == name), {})
            schema = offered.get("input_schema") or offered.get("inputSchema") or {}
            props = schema.get("properties") if isinstance(schema, dict) else None
            selector = props.get("renew_resources", {}) if isinstance(props, dict) else {}
            capability = lock_snapshot.get("capabilities")
            selective_heartbeat = (isinstance(capability, dict) and capability.get("heartbeat_selective_renewal") is True
                and isinstance(selector, dict) and selector.get("type") == "array"
                and isinstance(selector.get("items"), dict) and selector["items"].get("type") == "string")
            if not selective_heartbeat:
                self.live()
                if outside_count:
                    return {"ok": True, "status": "partial", "result_status": "partial", "effect_certainty": "none",
                        "heartbeat_recorded": False, "renewal_mode": "not_dispatched",
                        "advisories": [{"code": "heartbeat_protocol_partial", "count": outside_count,
                            "message": "Heartbeat was not recorded and no lock renewal was dispatched. Owned locks exist outside this binding; update the Hub or refresh its connector for selective renewal. No owner approval is required."}]}
                # Legacy behaviour is compatible with the observed snapshot.
                # It is not an atomic selective-renewal guarantee: an outside
                # lock acquired after the read can still be renewed by an old Hub.
                clean.pop("renew_resources")
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
            if "details_offset" in clean:
                observed = self.store.observed_task(self.binding, clean["task_id"])
                required = observed.get("_coordination_required_messages")
                if not isinstance(required, list):
                    raise CoordinationError("read this task with coordination_task before paging its discussion references")
                digest = self.task_digest(observed, required)
                if clean.get("task_digest") != digest:
                    raise CoordinationError("task snapshot changed; reread it before paging discussion references")
                return {**self.task_references(observed["id"], required, clean["details_offset"]), "task_digest": digest,
                        "action_scope": task_action_scope(self.binding, observed, read_only=self.read_only)}
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
            return {"task": {**observed, "action_scope": task_action_scope(self.binding, observed, read_only=self.read_only)},
                    "message_count": len(messages), **self.task_references(observed["id"], required),
                    "task_digest": self.task_digest(observed, required),
                    **self.pending_page(limit=8),
                    "_coordination_delivery": {"binding": self.binding.id, "revision": self.binding.revision,
                                               "task_receipt": receipt}}
        if operation == "board":
            result = compact_board(await self.remote("board", {}), self.binding,
                                   offset=clean.get("offset", 0), limit=clean.get("limit", 8), full=clean.get("full", False), read_only=self.read_only)
            if clean.get("digest") and clean["digest"] != result["digest"]:
                raise CoordinationError("board changed; restart pagination rather than mixing snapshots")
            return result
        if operation not in REMOTE:
            raise CoordinationError("unsupported coordination operation")
        # Read revisions are never replaced with a fresh number while executing an old vote.
        # The Hub enforces task ownership and checkpoint/review CAS at the actual mutation.
        result = await self.remote(REMOTE[operation], {**clean, "agent": self.binding.actor})
        if operation == "heartbeat":
            renewal = result.get("lock_renewal")
            expected_mode = "selected" if selective_heartbeat else "all"
            if ((selective_heartbeat or renewal is not None) and (
                    not isinstance(renewal, dict) or renewal.get("mode") != expected_mode
                    or not isinstance(renewal.get("renewed_resources"), list)
                    or not all(isinstance(res, str) and res in heartbeat_selection for res in renewal["renewed_resources"]))):
                raise UnknownOutcome("heartbeat renewal receipt did not match the selected resources; inspect locks before retrying")
            result["heartbeat_recorded"] = True
            result["renewal_mode"] = "selected" if selective_heartbeat else "legacy_all"
            if not selective_heartbeat:
                result["advisories"] = [{"code": "legacy_renewal_snapshot", "count": 0,
                    "message": "Heartbeat recorded using legacy renewal. All owned locks were covered in the observed snapshot; a legacy Hub cannot guarantee selective renewal if locks change concurrently."}]
        if operation == "heartbeat" and outside_count:
            result["advisories"] = [{"code": "locks_outside_binding", "count": outside_count,
                "message": "Heartbeat recorded. Owned locks outside this binding were not renewed; their controller can renew them separately."}]
        if isinstance(result.get("task"), dict):
            receipt = self.store.observe_task(self.binding, result["task"], required_messages=None)
            result["_coordination_delivery"] = {"binding": self.binding.id, "revision": self.binding.revision,
                                                "task_receipt": receipt}
        return result

    def pending_page(self, threads=(), limit=8):
        total = self.store.pending_count(self.binding, threads)
        pending = self.store.pending(self.binding, threads, min(limit, 8))
        return {"pending": pending, "pending_count": total, "pending_omitted": total - len(pending)}

    @staticmethod
    def task_digest(task, required):
        clean = {k: v for k, v in task.items() if not k.startswith("_coordination_")}
        return hashlib.sha256(json.dumps({"task": clean, "required": required}, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def task_references(task_id, required, offset=0):
        shown = required[offset:offset + 8]
        return {"task_id": task_id, "review_details": shown, "review_details_count": len(required),
                "review_details_omitted": len(required) - len(shown), "details_offset": offset,
                "next_details_offset": offset + len(shown) if offset + len(shown) < len(required) else None}

    def linked_threads(self, board):
        """Fresh, fixed-connector metadata permits reads, never task mutation receipts."""
        linked = {}
        for task in board.get("tasks", ()):
            try:
                positive_id(task.get("id"), "task id")
                thread = positive_id(task.get("thread_id"), "thread id")
                task_paths = task.get("paths")
                paths(self.binding, task.get("repo"), [] if task_paths is None else task_paths)
                if thread in linked:
                    linked[thread] = None  # Ambiguous relationships do not confer access.
                else:
                    linked[thread] = {key: task.get(key) for key in ("id", "repo", "paths", "thread_id")}
            except (CoordinationError, AttributeError):
                continue
        return {tid: task for tid, task in linked.items() if task is not None}

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
        recent = await self.recent(threads, limit, board=board)
        self.live()  # Connector/policy revocation is broader than the Store binding revision.
        self.store.receive(self.binding, expected_cursor=since, next_cursor=next_cursor, messages=page,
                           pending_messages=pending_mentions, threads=threads)
        for start in range(0, len(recent["cache"]), 200):
            self.store.receive(self.binding, expected_cursor=next_cursor, next_cursor=next_cursor, messages=[],
                               pending_messages=recent["cache"][start:start + 200], threads=threads)
        reviews = [t for t in inbox.get("reviews", ()) if t.get("id") in scope_tasks]
        changes = [t for t in inbox.get("changes", ()) if t.get("owner") == self.binding.actor]
        def task_rows(values):
            return [{**compact_task(task), "action_scope": task_action_scope(self.binding, task, read_only=self.read_only)}
                    for task in values[:8]]
        recent_refs = recent["refs"]
        shown_recent = []
        for ref in recent_refs:
            if len(json.dumps(shown_recent + [ref], ensure_ascii=False)) > 2400:
                break
            shown_recent.append(ref)
        result = {"actor": self.binding.actor, "binding_revision": self.binding.revision,
                "cursor": next_cursor, "page_received": len(all_messages), "has_more": len(all_messages) >= 100,
                **self.pending_page(threads, limit),
                "reviews": task_rows(reviews), "reviews_omitted": max(0, len(reviews) - 8),
                "changes": task_rows(changes), "changes_omitted": max(0, len(changes) - 8),
                "board": {"counts": board.get("counts", {}), "tasks_total": len(board.get("tasks", ())),
                          "detail_tool": "coordination_board"}, "hub_acknowledged": False,
                "recent": shown_recent, "recent_omitted": len(recent_refs) - len(shown_recent),
                "recent_focus_threads": [page["thread_id"] for page in recent["pages"]],
                "recent_unavailable": recent["unavailable"],
                "recent_detail_unavailable": recent["detail_unavailable"],
                "recent_threads_omitted": recent["threads_omitted"], "recent_pages": recent["pages"]}
        # Preserve current discussion first; all omitted references stay in the
        # Store and the counts make partial coverage explicit. Focus a thread
        # filter, or drain complete pending details, to retrieve the rest.
        for field in ("pending", "reviews", "changes", "recent", "recent_unavailable", "recent_detail_unavailable"):
            while len(json.dumps(result, ensure_ascii=False)) > 6000 and result[field]:
                result[field].pop()
                key = field + "_omitted"
                result[key] = result.get(key, 0) + 1
        return result

    async def recent(self, threads, limit, *, board=None):
        """Read a small recent window only when the bound Hub advertises it.

        Never fall back to the full-thread reader. Recent cache entries and
        unread history have the same detail receipts but separate coverage:
        only the fetched inbox page can move its durable cursor.
        """
        linked = self.linked_threads(board or {})
        selected = ([tid for tid in threads if tid in self.binding.thread_ids or tid in linked]
                    if threads else list(self.binding.thread_ids))
        out = {"refs": [], "cache": [], "unavailable": [], "detail_unavailable": [], "pages": [],
               "threads_omitted": max(0, len(selected) - 4)}
        out["unavailable"] = [{"thread_id": tid, "reason": "thread is not configured or linked to a current scoped task; read its task first"}
                              for tid in threads if tid not in selected]
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
            out["unavailable"].extend({"thread_id": tid, "reason": "bounded recent reader is not available in the bound connector"}
                                     for tid in selected[:4])
            return out
        for tid in selected[:4]:
            try:
                result = await self.remote("thread_recent", {"thread_id": tid, "limit": min(limit, 5)})
                if tid not in self.binding.thread_ids:
                    current = self.linked_threads(await self.remote("board", {}))
                    if current.get(tid) != linked.get(tid):
                        raise CoordinationError("task/thread scope changed during recent reading; reload the current task")
                messages = result.get("messages")
                if (not isinstance(messages, list) or len(messages) > min(limit, 5)
                        or not isinstance(result.get("thread"), dict) or type(result["thread"].get("id")) is not int or result["thread"].get("id") != tid):
                    raise CoordinationError("invalid bounded recent thread page")
                refs, cache, unavailable = [], [], []
                for message in messages:
                    if (not isinstance(message, dict) or type(message.get("thread_id")) is not int or message.get("thread_id") != tid
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
