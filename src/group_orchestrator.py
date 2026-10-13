"""Durable group scheduling; participant execution stays in the native chat harness."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from src.constants import DATA_DIR


class GroupStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(DATA_DIR) / "groups.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, definition TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                    driver TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS group_turns (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    group_id TEXT NOT NULL, request_id TEXT NOT NULL, message TEXT NOT NULL,
                    definition TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(group_id, request_id));
                CREATE TABLE IF NOT EXISTS group_steps (
                    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, member_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(turn_id, member_id));
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim_driver(self, group_id, identity):
        now = time.time()
        with self.lock, self.db() as db:
            return db.execute("UPDATE groups SET driver=?,lease_until=? WHERE id=? AND (driver='' OR driver=? OR lease_until<?)",
                              (identity, now + 180, group_id, identity, now)).rowcount == 1

    def release_driver(self, group_id, identity):
        with self.lock, self.db() as db:
            db.execute("UPDATE groups SET driver='',lease_until=0 WHERE id=? AND driver=?", (group_id, identity))

    def group(self, group_id, owner):
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM groups WHERE id=? AND owner=?", (group_id, owner or "")).fetchone()
        if not row:
            raise KeyError("Group not found")
        return {**json.loads(row["definition"]), "archived": bool(row["archived"]), "revision": row["revision"]}

    def list(self, owner):
        with self.lock, self.db() as db:
            return [json.loads(row["definition"]) for row in db.execute(
                "SELECT definition FROM groups WHERE owner=? AND archived=0 ORDER BY rowid DESC", (owner or "",))]

    def save(self, definition, owner):
        group_id = definition["parentId"]
        with self.lock, self.db() as db:
            existing = db.execute("SELECT owner FROM groups WHERE id=?", (group_id,)).fetchone()
            if existing and existing["owner"] != (owner or ""):
                raise KeyError("Group not found")
            db.execute("INSERT INTO groups(id,owner,definition) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,revision=groups.revision+1",
                       (group_id, owner or "", json.dumps(definition, ensure_ascii=False)))
        return self.group(group_id, owner)

    def enqueue(self, group_id, owner, message, request_id, mentions=()):
        definition = self.group(group_id, owner)
        if definition["archived"]:
            raise ValueError("Group is archived")
        if not message.strip():
            raise ValueError("Message is required")
        members = definition["participants"]
        if mentions:
            selected = [p for p in members if p["sessionId"] in mentions or p["personaName"] in mentions]
            if not selected:
                raise ValueError("Mentioned participant not found")
            members = selected
        turn_id = uuid.uuid4().hex
        with self.lock, self.db() as db:
            prior = db.execute("SELECT id FROM group_turns WHERE group_id=? AND request_id=?", (group_id, request_id)).fetchone()
            if prior:
                return prior["id"], False
            db.execute("INSERT INTO group_turns(id,group_id,request_id,message,definition,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       (turn_id, group_id, request_id, message, json.dumps(definition, ensure_ascii=False), time.time()))
            for i, member in enumerate(members):
                db.execute("INSERT INTO group_steps(id,turn_id,member_id,ordinal,status) VALUES(?,?,?,?,'queued')",
                           (uuid.uuid4().hex, turn_id, member["sessionId"], i))
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=?", (group_id,))
        return turn_id, True

    def turns(self, group_id):
        with self.lock, self.db() as db:
            rows = db.execute("SELECT * FROM group_turns WHERE group_id=? ORDER BY ordinal", (group_id,)).fetchall()
        return [{**dict(row), "definition": json.loads(row["definition"])} for row in rows]

    def steps(self, turn_id):
        with self.lock, self.db() as db:
            rows = db.execute("SELECT * FROM group_steps WHERE turn_id=? ORDER BY ordinal", (turn_id,)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def update_step(self, step_id, **patch):
        allowed = {"status", "text", "error", "detail"}
        if not patch or set(patch) - allowed:
            raise ValueError("Invalid step update")
        if "detail" in patch:
            patch["detail"] = json.dumps(patch["detail"], ensure_ascii=False)
        with self.lock, self.db() as db:
            db.execute("UPDATE group_steps SET " + ",".join(key + "=?" for key in patch) + " WHERE id=?", (*patch.values(), step_id))
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=(SELECT group_id FROM group_turns WHERE id=(SELECT turn_id FROM group_steps WHERE id=?))", (step_id,))

    def update_turn(self, turn_id, status):
        with self.lock, self.db() as db:
            db.execute("UPDATE group_turns SET status=? WHERE id=?", (status, turn_id))
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=(SELECT group_id FROM group_turns WHERE id=?)", (turn_id,))

    def stop(self, group_id):
        with self.lock, self.db() as db:
            active = [dict(row) for row in db.execute(
                "SELECT s.* FROM group_steps s JOIN group_turns t ON t.id=s.turn_id WHERE t.group_id=? AND s.status='running'", (group_id,))]
            db.execute("UPDATE group_steps SET status='stopped' WHERE turn_id IN (SELECT id FROM group_turns WHERE group_id=?) AND status IN ('queued','running','waiting_user')", (group_id,))
            db.execute("UPDATE group_turns SET status='stopped' WHERE group_id=? AND status IN ('queued','running','waiting_user')", (group_id,))
            db.execute("UPDATE groups SET revision=revision+1 WHERE id=?", (group_id,))
        return [{**row, "detail": json.loads(row["detail"])} for row in active]

    def recoverable(self):
        with self.lock, self.db() as db:
            return [(r["id"], r["owner"]) for r in db.execute("SELECT DISTINCT g.id,g.owner FROM groups g JOIN group_turns t ON t.group_id=g.id WHERE t.status IN ('queued','running') AND g.archived=0")]


class GroupCoordinator:
    """Strongly owned drivers survive observers; all generation uses the supplied native invoker."""
    def __init__(self, invoker, transcript, *, store=None):
        self.store = store or GroupStore()
        self.invoker = invoker
        self.transcript = transcript
        self.tasks = {}
        self.accepting = True
        self.identity = uuid.uuid4().hex
        self.recovery_task = None

    def adopt(self, definition, owner):
        definition = copy.deepcopy(definition)
        if definition.get("mode") not in ("parallel", "round-robin"):
            raise ValueError("Choose parallel or round-robin")
        members = definition.get("participants") or []
        if len(members) < 2 or len({p["sessionId"] for p in members}) != len(members):
            raise ValueError("A group needs distinct participant sessions")
        definition.setdefault("executionMode", "agent")
        definition.setdefault("workspace", "")
        for p in members:
            p["personaName"] = p.get("personaName") or p["route"]["model"].rsplit("/", 1)[-1]
        if definition["executionMode"] not in ("chat", "agent"):
            raise ValueError("Choose chat or agent")
        self.transcript.validate(definition, owner)
        return self.store.save(definition, owner)

    async def submit(self, group_id, owner, message, request_id="", mentions=()):
        if not self.accepting:
            raise RuntimeError("Server is restarting; the saved group remains available")
        turn_id, fresh = self.store.enqueue(group_id, owner, message, request_id or uuid.uuid4().hex, mentions)
        self.kick(group_id, owner)
        return {"turn_id": turn_id, "fresh": fresh, "group_id": group_id}

    def kick(self, group_id, owner):
        current = self.tasks.get(group_id)
        if current and not current.done():
            return
        if not self.store.claim_driver(group_id, self.identity):
            return
        async def heartbeat():
            while self.accepting:
                await asyncio.sleep(30)
                if not self.store.claim_driver(group_id, self.identity):
                    driver = self.tasks.get(group_id)
                    if driver:
                        driver.cancel()
                    return
        async def drive():
            hb = asyncio.create_task(heartbeat())
            try:
                await self._drive(group_id, owner)
            finally:
                hb.cancel()
                await asyncio.gather(hb, return_exceptions=True)
                self.store.release_driver(group_id, self.identity)
        task = asyncio.create_task(drive(), name="group-" + group_id)
        self.tasks[group_id] = task
        def finished(done):
            if self.tasks.get(group_id) is done:
                self.tasks.pop(group_id, None)
            if not done.cancelled():
                done.exception()  # Consume failures; persisted steps retain their diagnostic.
                turns = self.store.turns(group_id)
                if self.accepting and not any(t["status"] == "waiting_user" for t in turns) and any(t["status"] in ("queued", "running") for t in turns):
                    asyncio.get_running_loop().call_soon(self.kick, group_id, owner)
        task.add_done_callback(finished)

    async def _drive(self, group_id, owner):
        while self.accepting:
            turns = self.store.turns(group_id)
            if any(t["status"] == "waiting_user" for t in turns):
                return
            pending = next((t for t in turns if t["status"] in ("queued", "running")), None)
            if pending is None:
                return
            self.store.update_turn(pending["id"], "running")
            try:
                self.transcript.user(pending, owner)
                steps = self.store.steps(pending["id"])
                for step in steps:
                    if step["status"] in ("done", "error"):
                        self.transcript.reply(pending, step, owner)
                if pending["definition"]["mode"] == "parallel":
                    await asyncio.gather(*(self._speak(pending, step, owner) for step in steps if step["status"] in ("queued", "running")))
                else:
                    for step in steps:
                        if self._stopped(pending["id"]):
                            break
                        if step["status"] in ("queued", "running"):
                            await self._speak(pending, step, owner)
                            current = next(s for s in self.store.steps(pending["id"]) if s["id"] == step["id"])
                            if current["status"] == "waiting_user":
                                break
                if not self._stopped(pending["id"]):
                    steps = self.store.steps(pending["id"])
                    status = "waiting_user" if any(s["status"] == "waiting_user" for s in steps) else "completed_with_errors" if any(s["status"] == "error" for s in steps) else "done"
                    self.store.update_turn(pending["id"], status)
                    if status == "waiting_user":
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                for step in self.store.steps(pending["id"]):
                    if step["status"] in ("queued", "running"):
                        self.store.update_step(step["id"], status="error", error=str(exc))
                self.store.update_turn(pending["id"], "completed_with_errors")

    def _stopped(self, turn_id):
        with self.store.db() as db:
            row = db.execute("SELECT status FROM group_turns WHERE id=?", (turn_id,)).fetchone()
        return not row or row["status"] == "stopped"

    async def _speak(self, turn, step, owner):
        if self._stopped(turn["id"]):
            return
        recovering = step["status"] == "running"
        self.store.update_step(step["id"], status="running")
        member = next(p for p in turn["definition"]["participants"] if p["sessionId"] == step["member_id"])
        async def progress(text, detail):
            if not self._stopped(turn["id"]):
                self.store.update_step(step["id"], text=text, detail=detail)
        try:
            result = await self.invoker.invoke(turn=turn, step=step, member=member, owner=owner,
                                               progress=progress, recovering=recovering)
            if self._stopped(turn["id"]):
                return
            state = "waiting_user" if result.get("question") else "error" if result.get("error") else "done"
            self.store.update_step(step["id"], status=state, text=result.get("text", step["text"]),
                                   error=result.get("error", ""), detail=result.get("detail", {}))
            if state in ("done", "error"):
                finished = next(s for s in self.store.steps(turn["id"]) if s["id"] == step["id"])
                self.transcript.reply(turn, finished, owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._stopped(turn["id"]):
                self.store.update_step(step["id"], status="error", error=str(exc))
                failed = next(s for s in self.store.steps(turn["id"]) if s["id"] == step["id"])
                self.transcript.reply(turn, failed, owner)

    async def stop(self, group_id, owner):
        self.store.group(group_id, owner)
        active = self.store.stop(group_id)
        for step in active:
            await self.invoker.stop(step["member_id"], step["detail"].get("run_id", ""))
        task = self.tasks.get(group_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"stopped": True}

    async def archive(self, group_id, owner):
        await self.stop(group_id, owner)
        with self.store.lock, self.store.db() as db:
            db.execute("UPDATE groups SET archived=1,revision=revision+1 WHERE id=?", (group_id,))
        return {"archived": True}

    async def resume(self, group_id, owner, message, answer=None):
        self.store.group(group_id, owner)
        turn = next((t for t in self.store.turns(group_id) if t["status"] == "waiting_user"), None)
        if turn is None:
            raise ValueError("This group has no pending question")
        step = next(s for s in self.store.steps(turn["id"]) if s["status"] == "waiting_user")
        continuation = {"message": message, "request_id": uuid.uuid4().hex}
        fields = {k: v for k, v in (answer or {}).items() if k in
                  {"question_id", "question_revision", "option_ids", "tool_approval_id", "tool_approval_decision"}}
        question = step["detail"].get("question") or {}
        if not fields.get("question_id") and question.get("question_id"):
            fields["question_id"] = question["question_id"]
            fields["question_revision"] = question.get("revision")
        continuation.update(fields)
        self.store.update_step(step["id"], status="queued", error="",
                               detail={**step["detail"], "continuation": continuation})
        self.store.update_turn(turn["id"], "queued")
        self.transcript.answer(turn, message, continuation["request_id"], owner)
        self.kick(group_id, owner)
        return {"turn_id": turn["id"], "request_id": continuation["request_id"]}

    async def recover(self):
        self.accepting = True
        def scan():
            for group_id, owner in self.store.recoverable():
                self.kick(group_id, owner)
        scan()
        async def retry_orphaned_drivers():
            while self.accepting:
                await asyncio.sleep(30)
                scan()  # A crashed process's lease expires without another browser request.
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = asyncio.create_task(retry_orphaned_drivers())

    async def shutdown(self):
        self.accepting = False
        tasks = list(self.tasks.values())
        if self.recovery_task:
            tasks.append(self.recovery_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self, group_id, owner):
        group = self.store.group(group_id, owner)
        turns = self.store.turns(group_id)
        return {"group": group, "turns": [{k: v for k, v in t.items() if k != "definition"} for t in turns],
                "steps": [s for t in turns for s in self.store.steps(t["id"])],
                "busy": any(t["status"] in ("queued", "running", "waiting_user") for t in turns)}


def native_request(app, owner, payload):
    """An internal invocation with the already authenticated group's stored identity."""
    from starlette.requests import Request
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    async def receive():
        await asyncio.Future()  # There is no browser observer whose departure can cancel this call.
    request = Request({"type": "http", "method": "POST", "path": "/api/chat_stream",
                       "scheme": "http", "server": ("127.0.0.1", 0), "client": ("127.0.0.1", 0),
                       "query_string": b"", "headers": [(b"content-type", b"application/json")],
                       "state": {"current_user": owner or None}, "app": app}, receive=receive)
    request._body = body
    return request


class NativeGroupBridge:
    """Canonical sessions and the ordinary chat endpoint, not a second agent loop."""
    def __init__(self, session_manager, chat_endpoint, app):
        self.sessions = session_manager
        self.chat_endpoint = chat_endpoint
        self.app = app
        self.member_locks = {}

    def validate(self, definition, owner):
        for sid in [definition["parentId"], *(p["sessionId"] for p in definition["participants"])]:
            session = self.sessions.get_session(sid)
            if owner and session.owner != owner:
                raise KeyError("Session not found")

    def _append_once(self, sid, role, text, metadata, marker):
        from core.models import ChatMessage
        session = self.sessions.get_session(sid)
        if any((m.metadata or {}).get(marker[0]) == marker[1] for m in session.history):
            return
        session.add_message(ChatMessage(role, text, metadata=metadata))
        self.sessions.save_sessions()

    def user(self, turn, owner):
        self._append_once(turn["group_id"], "user", turn["message"],
                          {"group_turn_id": turn["id"]}, ("group_turn_id", turn["id"]))

    def reply(self, turn, step, owner):
        member = next(p for p in turn["definition"]["participants"] if p["sessionId"] == step["member_id"])
        self._append_once(turn["group_id"], "assistant", step["text"],
                          {"group_step_id": step["id"], "group_turn_id": turn["id"],
                           "group_model": member["personaName"], "model": member["route"]["model"],
                           "group_step_status": step.get("status", "done"), "group_error": step.get("error", ""),
                           "group_tool_results": step["detail"].get("tool_results", [])},
                          ("group_step_id", step["id"]))

    def answer(self, turn, message, request_id, owner):
        self._append_once(turn["group_id"], "user", message,
                          {"group_answer_id": request_id}, ("group_answer_id", request_id))

    def create(self, participants, mode, owner, *, name="", workspace="", execution_mode="agent"):
        from services.hoard_copilot import global_model
        from src.endpoint_resolver import resolve_endpoint_by_id
        selected = []
        names = {}
        for source in participants:
            route = dict(source.get("route") or {})
            if route.get("endpointId"):
                resolved = resolve_endpoint_by_id(route["endpointId"], route.get("model") or "", owner=owner, require_exact_model=True)
                if not resolved:
                    raise ValueError("The selected model endpoint is unavailable")
                url, model, headers = resolved
            elif route.get("endpointUrl") and route.get("model"):
                # Same configured/raw route capability as normal local session creation.
                url, model, headers = route["endpointUrl"], route["model"], {}
            else:
                resolved = global_model(owner)
                url, model, headers = resolved["endpoint_url"], resolved["model"], resolved["headers"]
                if not url or not model:
                    raise ValueError("The configured global model is unavailable")
            base = source.get("personaName") or model.rsplit("/", 1)[-1]
            names[base] = names.get(base, 0) + 1
            label = base if names[base] == 1 else f"{base} ({names[base]})"
            route = {"id": route.get("id") or model, "model": model,
                     "endpointId": route.get("endpointId", ""), "endpointUrl": url,
                     "endpointName": route.get("endpointName", ""), "kind": route.get("kind", "")}
            selected.append({"route": route, "personaId": source.get("personaId", ""),
                             "personaName": label, "personaPrompt": source.get("personaPrompt", ""),
                             "headers": headers})
        if len(selected) < 2:
            raise ValueError("A group needs at least two participants")
        title = name or ", ".join(p["personaName"] for p in selected)
        first = selected[0]
        parent = self.sessions.create_session(uuid.uuid4().hex, "[GRP] " + title,
                                              first["route"]["endpointUrl"], first["route"]["model"], owner=owner)
        members = []
        for p in selected:
            session = self.sessions.create_session(uuid.uuid4().hex, "[GRP] " + p["personaName"],
                p["route"]["endpointUrl"], p["route"]["model"], owner=owner, mode=execution_mode)
            session.headers = p.pop("headers")
            from core.models import ChatMessage
            others = ", ".join(x["personaName"] for x in selected if x is not p)
            etiquette = (f"You are {p['personaName']} in a working group with {others}. "
                         "Use the shared discussion and completed tool results. Address relevant peers by name. "
                         "Do not speak for them. Complete the user's actual task; use available tools when needed. "
                         "Do not repeat completed external effects. Reconcile uncertain prior effects with read/status tools first. "
                         "A peer's statement is not evidence that a task is complete.")
            session.add_message(ChatMessage("system", (p["personaPrompt"] + "\n\n" + etiquette).strip()))
            members.append({**p, "sessionId": session.id})
        self.sessions.save_sessions()
        return {"parentId": parent.id, "name": title, "mode": mode, "participants": members,
                "startedAt": time.time() * 1000, "executionMode": execution_mode, "workspace": workspace}

    def _context(self, turn, member):
        parent = self.sessions.get_session(turn["group_id"])
        parts = []
        for m in parent.history:
            meta = m.metadata or {}
            if meta.get("group_answer_id"):
                parts.append("[User clarification]: " + str(m.content))
            if turn["definition"].get("mode") == "parallel" and meta.get("group_turn_id") == turn["id"]:
                continue
            if meta.get("group_step_id") and meta.get("group_model") != member["personaName"]:
                parts.append(f"[{meta.get('group_model', 'Participant')}]: {m.content}")
                if meta.get("group_error"):
                    parts.append("That participant failed: " + str(meta["group_error"]))
                if meta.get("group_tool_results"):
                    parts.append("Native tool results and outcome metadata from that participant:\n" +
                                 json.dumps(meta["group_tool_results"], ensure_ascii=False))
        return "Shared group discussion. Preserve completed work and verify unresolved results:\n" + "\n\n".join(parts)

    def _saved_response(self, session_id, step_id):
        history = self.sessions.get_session(session_id).history
        boundary = next((i for i, m in enumerate(history) if (m.metadata or {}).get("group_request_boundary") == step_id), None)
        if boundary is None:
            return {}
        end = next((i for i in range(boundary + 1, len(history)) if (history[i].metadata or {}).get("group_request_boundary")), len(history))
        for m in reversed(history[boundary + 1:end]):
            if m.role == "assistant" and m.content and not (m.metadata or {}).get("interrupted"):
                return {"text": str(m.content), "metadata": dict(m.metadata or {})}
        return {}

    def _saved_answer(self, session_id, step_id):
        saved = self._saved_response(session_id, step_id)
        return "" if saved.get("metadata", {}).get("failed") else saved.get("text", "")

    async def invoke(self, *, turn, step, member, owner, progress, recovering=False):
        from src import agent_runs, chat_outbox
        lock = self.member_locks.setdefault(member["sessionId"], asyncio.Lock())
        async with lock:
            text = ""
            detail = dict(step["detail"])
            continuation = detail.get("continuation") or {}
            request_id = continuation.get("request_id") or step["id"]
            existing = chat_outbox.get(owner=owner or "", session_id=member["sessionId"], client_message_id=request_id)
            run = getattr(agent_runs, "_RUNS", {}).get(member["sessionId"])
            wanted_run = detail.get("run_id") or (existing or {}).get("run_id")
            if recovering and existing:
                if run and wanted_run and run.run_id == wanted_run:
                    stream = agent_runs.subscribe(member["sessionId"], expected_run=run)
                elif existing.get("status") == "finished":
                    saved = self._saved_response(member["sessionId"], request_id)
                    metadata = saved.get("metadata") or {}
                    native = metadata or detail.get("native_terminal") or {}
                    if native.get("tool_events") and not detail.get("tool_results"):
                        detail["tool_results"] = native["tool_events"]
                    if native.get("failed"):
                        failure = native.get("failure") or {}
                        return {"text": saved.get("text") or step["text"],
                                "error": str(failure.get("message") or "Participant failed") if isinstance(failure, dict) else str(failure),
                                "detail": {**detail, "native_terminal": native}}
                    answer = (existing.get("result") or {}).get("response") or self._saved_answer(member["sessionId"], request_id)
                    return {"text": answer or step["text"], "detail": detail}
                else:
                    return {"text": step["text"], "error": "The previous participant attempt was interrupted; its effects were not repeated.",
                            "detail": {**detail, "outcome_unknown": True}}
            else:
                self._append_once(member["sessionId"], "system", self._context(turn, member),
                                  {"group_request_boundary": request_id}, ("group_request_boundary", request_id))
                body = {"session": member["sessionId"],
                    "message": continuation.get("message", turn["message"]), "client_message_id": request_id,
                    "mode": turn["definition"]["executionMode"], "model": member["route"]["model"],
                    "selected_endpoint_id": member["route"].get("endpointId", ""),
                    "workspace": turn["definition"].get("workspace", ""), "allow_bash": True, "allow_web_search": True}
                body.update({k: v for k, v in continuation.items() if k not in {"request_id", "message"}})
                request = native_request(self.app, owner, body)
                try:
                    response = await self.chat_endpoint(request, initial_response_timeout_s=None)
                except Exception as exc:
                    if detail.get("question") and getattr(exc, "status_code", None) in (400, 409, 422):
                        return {"text": step["text"], "question": detail["question"],
                                "error": str(getattr(exc, "detail", exc)), "detail": detail}
                    raise
                if response.status_code >= 400:
                    raise RuntimeError(f"Native participant request failed: HTTP {response.status_code}")
                detail["run_id"] = response.headers.get("X-Odysseus-Run-Id", "")
                await progress(text, detail)
                stream = response.body_iterator
            from src.council.adapters import _sse_payloads
            pending = ""
            terminal = False
            error = ""
            question = None
            last_save = 0.0
            async for chunk in stream:
                pending += (chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)).replace("\r\n", "\n")
                while "\n\n" in pending:
                    frame, pending = pending.split("\n\n", 1)
                    critical = False
                    if "data: [DONE]" in frame:
                        terminal = True
                    event_kind = next((line.split(":", 1)[1].strip().casefold()
                                       for line in frame.splitlines() if line.startswith("event:")), "")
                    for payload in _sse_payloads(frame):
                        if payload.get("type") in ("tool_output", "agent_terminal", "ask_user", "response_replace", "error", "uncertain") or event_kind == "error":
                            critical = True
                        if payload.get("type") == "response_replace":
                            text = str(payload.get("text") or "")
                        if not payload.get("thinking"):
                            if isinstance(payload.get("delta"), str):
                                text += payload["delta"]
                            elif isinstance(payload.get("response"), str):
                                text += payload["response"]
                        if event_kind == "error" or payload.get("type") == "error" or (payload.get("error") and payload.get("type") != "tool_output"):
                            error = str(payload.get("error") or payload.get("message") or payload.get("text") or "Participant failed")
                        if payload.get("type") == "agent_terminal":
                            outcome = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                            detail["native_terminal"] = outcome
                            if outcome.get("failed"):
                                failure = outcome.get("failure") or {}
                                error = str((failure.get("message") or "Participant failed") if isinstance(failure, dict) else (failure or "Participant failed"))
                        if payload.get("type") == "uncertain":
                            error = str(payload.get("detail") or "The prior attempt has an unknown outcome")
                            detail["outcome_unknown"] = True
                        if payload.get("ask_user") or payload.get("type") == "ask_user":
                            question = payload.get("ask_user") or payload.get("data") or payload
                            detail["question"] = question
                        if payload.get("type") == "usage":
                            detail["usage"] = payload.get("data", payload)
                        if payload.get("type") == "tool_output":
                            result = dict(payload)
                            results = detail.setdefault("tool_results", [])
                            if result not in results:
                                results.append(result)
                        detail["last_event"] = payload
                    if critical or time.monotonic() - last_save >= .2:
                        await progress(text, detail)
                        last_save = time.monotonic()
            if not terminal and not question:
                error = error or "Participant stream ended without completion"
            return {"text": text or self._saved_answer(member["sessionId"], request_id),
                    "error": error, "question": question, "detail": detail}

    async def stop(self, session_id, run_id):
        from src import agent_runs
        # A seat waiting on another use of its session has no generation to cancel.
        if run_id:
            agent_runs.stop(session_id, expected_run_id=run_id)
