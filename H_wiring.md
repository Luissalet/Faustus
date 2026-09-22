# H_wiring.md — lifecycle hooks (src/lifecycle_hooks.py)

Five insertion points in integrator-owned files. None of these are applied —
this file is the diff for the integrator to apply and un-xfail
`tests/test_h_wiring.py`.

Settings already appended (this lot may touch `src/settings.py`, append-only):
`lifecycle_hooks`, `lifecycle_hooks_enabled`, `lifecycle_hooks_command_timeout_seconds`,
`lifecycle_hooks_total_timeout_seconds`.

New routes module: `routes/lifecycle_hooks_routes.py` exports
`setup_lifecycle_hooks_routes() -> APIRouter`.

## 1. `app.py` — register the router

```diff
+from routes.lifecycle_hooks_routes import setup_lifecycle_hooks_routes
```
next to the other `routes.*_routes` imports, and
```diff
+app.include_router(setup_lifecycle_hooks_routes())
```
next to `app.include_router(setup_tool_arg_policy_routes())`.

## 2. `src/agent_loop.py` — `session_start` / `turn_start`

Right after the repository-map injection (`_insert_before_latest_user(...,
untrusted_context_message("repository map", ...))`, ~line 6376):

```diff
         if _repo_map_text:
             messages = _insert_before_latest_user(
                 messages,
                 untrusted_context_message("repository map", _repo_map_text, arm_tool_gate=False),
             )
+    # Lifecycle hooks (src/lifecycle_hooks.py, lot H): session_start fires
+    # once per session (no assistant message yet in `messages`), turn_start
+    # fires every turn. Hooks only ever ADD context/log — never block.
+    try:
+        from src import lifecycle_hooks as _hooks
+        _hooks_ctx = {
+            "workspace": workspace,
+            "session_id": session_id,
+            "user_message": _last_user or "",
+            "paths": [],
+        }
+        _has_assistant_reply = any(m.get("role") == "assistant" for m in messages)
+        _hook_events = ["turn_start"] if _has_assistant_reply else ["session_start", "turn_start"]
+        _hook_results = []
+        for _hev in _hook_events:
+            _hook_results.extend(await _hooks.run_async(_hev, _hooks_ctx))
+        _hooks_text = _hooks.render_context(_hook_results)
+        if _hooks_text:
+            messages = _insert_before_latest_user(
+                messages,
+                untrusted_context_message("lifecycle hooks", _hooks_text, arm_tool_gate=False),
+            )
+        if _hook_results:
+            yield "data: " + json.dumps({
+                "type": "harness_check", "status": "hooks", "event": _hook_events[-1],
+                "hooks": [{"id": r.hook_id, "name": r.name, "ok": r.ok, "duration_ms": r.duration_ms}
+                          for r in _hook_results],
+            }) + "\n\n"
+    except Exception:
+        logger.debug("[lifecycle_hooks] session_start/turn_start run failed", exc_info=True)
```

Note: this insertion point is inside an `async def` generator that already
`yield`s SSE lines elsewhere in the same function (confirmed at this call
site — `_insert_before_latest_user` and the surrounding block run inside the
`chat_stream` async generator), so `yield` here is valid; if the integrator's
current code path reaches this point before the generator starts yielding,
move the `yield` after the first `yield` already in the function instead of
dropping it.

## 3. `src/agent_loop.py` — `pre_tool`

In the execute branch, right after the `tool_start` SSE and
`_maybe_checkpoint` (~line 12590-12596):

```diff
                 yield (
                     f'data: {json.dumps({"type": "tool_start", "tool": block.tool_type, "command": cmd_display, "full_command": full_command, "round": round_num, "call_id": _call_id})}\n\n'
                 )
                 # Baseline before the first change of the turn.
                 _cp = await _maybe_checkpoint(block.tool_type, block.content)
                 if _cp:
                     yield "data: " + json.dumps({"type": "harness_check", "status": "checkpoint", "round": round_num,
                                                  "sha": _cp.get("sha"), "created": _cp.get("created"), "ms": _cp.get("ms")}) + "\n\n"
+
+                # Lifecycle hooks (src/lifecycle_hooks.py, lot H) — pre_tool.
+                # Augment only: warn/inject outputs are attached to `result`
+                # AFTER execution below (via attach_to_result), never used to
+                # deny or delay the call itself.
+                _pre_hook_results = []
+                try:
+                    from src import lifecycle_hooks as _hooks
+                    from src.agent_harness import _paths_from_args
+                    _pre_hooks_ctx = {
+                        "tool": block.tool_type,
+                        "content": block.content,
+                        "paths": _paths_from_args(block.tool_type, block.content),
+                        "command": block.content if block.tool_type in ("bash", "python", "powershell") else "",
+                        "workspace": workspace,
+                        "session_id": session_id,
+                    }
+                    _pre_hook_results = await _hooks.run_async("pre_tool", _pre_hooks_ctx)
+                    if _pre_hook_results:
+                        yield "data: " + json.dumps({
+                            "type": "harness_check", "status": "hooks", "event": "pre_tool",
+                            "hooks": [{"id": r.hook_id, "name": r.name, "ok": r.ok, "duration_ms": r.duration_ms}
+                                      for r in _pre_hook_results],
+                        }) + "\n\n"
+                except Exception:
+                    logger.debug("[lifecycle_hooks] pre_tool run failed", exc_info=True)
```

Then, after `desc, result = ...` is assigned for BOTH branches (the
`_prefetched` shortcut and the normal `_run_tool`/`_tool_task` path — i.e.
right before the shared `# CALL-02/CALL-03` comment at ~line 12678):

```diff
+            if _pre_hook_results and isinstance(result, dict):
+                _hooks.attach_to_result(result, _pre_hook_results)
             # CALL-02/CALL-03: annotate the result with whatever the argument
```

## 4. `src/agent_loop.py` — `post_tool`

At ~line 12690, after `run_security.observe_tool_result(...)`, before the
evidence-ledger `_ledger.record(...)` call:

```diff
             run_security.observe_tool_result(block.tool_type, result, block.content)
+
+            # Lifecycle hooks (src/lifecycle_hooks.py, lot H) — post_tool.
+            try:
+                from src import lifecycle_hooks as _hooks
+                from src.agent_harness import _paths_from_args
+                _post_hooks_ctx = {
+                    "tool": block.tool_type,
+                    "content": block.content,
+                    "paths": _paths_from_args(block.tool_type, block.content),
+                    "command": block.content if block.tool_type in ("bash", "python", "powershell") else "",
+                    "workspace": workspace,
+                    "session_id": session_id,
+                }
+                _post_hook_results = await _hooks.run_async("post_tool", _post_hooks_ctx)
+                if _post_hook_results and isinstance(result, dict):
+                    _hooks.attach_to_result(result, _post_hook_results)
+                if _post_hook_results:
+                    yield "data: " + json.dumps({
+                        "type": "harness_check", "status": "hooks", "event": "post_tool",
+                        "hooks": [{"id": r.hook_id, "name": r.name, "ok": r.ok, "duration_ms": r.duration_ms}
+                                  for r in _post_hook_results],
+                    }) + "\n\n"
+            except Exception:
+                logger.debug("[lifecycle_hooks] post_tool run failed", exc_info=True)
 
             # Evidence ledger: record what really ran (success, kind, paths).
```

## 5. `src/agent_loop.py` — `pre_compact`

Right before `compact_with_integrity(...)` (~line 7921) AND right before
`maybe_compact(...)` (~line 7932) — both are candidate compaction paths for
the same event, so the hook must run ahead of whichever one actually fires:

```diff
                 if _pct_for_compaction >= COMPACT_THRESHOLD:
+                    try:
+                        from src import lifecycle_hooks as _hooks
+                        _compact_hook_results = await _hooks.run_async(
+                            "pre_compact",
+                            {"workspace": workspace, "session_id": session_id},
+                        )
+                        _compact_hooks_text = _hooks.render_context(_compact_hook_results)
+                        if _compact_hooks_text:
+                            compacted_source = compacted_source + [
+                                untrusted_context_message("lifecycle hooks", _compact_hooks_text, arm_tool_gate=False)
+                            ]
+                    except Exception:
+                        logger.debug("[lifecycle_hooks] pre_compact run failed", exc_info=True)
                     _integrity_messages, _integrity_evidence = compact_with_integrity(
                         compacted_source,
                         owner_id=owner or "system",
                         session_id=session_id or "",
                     )
```

`maybe_compact`'s own path (line 7932) reuses the same `compacted_source`
variable that was just appended to above when `compact_with_integrity` did
not run (`was_compacted` still `False`), so a second injection is not
needed — confirm this still holds against the integrator's current code
before skipping it there.

## 6. `routes/chat_helpers.py` — `turn_end`

Next to `notifications.emit("turn_finished", ...)` (fire-and-forget — no SSE
channel is open by the time `save_assistant_response` runs, so this is
log-only, no injection possible for this event):

```diff
     try:
         from src import notifications as _notifications
         _notifications.emit(
             "turn_finished",
             owner=getattr(sess, "owner", None),
             title=getattr(sess, "name", "") or "Chat",
             body=_content,
             session_id=session_id,
         )
     except Exception:
         logger.debug("notifications.emit(turn_finished) failed", exc_info=True)
+
+    # Lifecycle hooks (src/lifecycle_hooks.py, lot H) — turn_end. Nothing to
+    # inject into (the turn already finished), so this only runs `command`/
+    # `warn`/`inject` hooks for their side effects and the run log
+    # (GET /api/lifecycle-hooks/log). Fire-and-forget: never block finishing
+    # a turn on a user's own automation.
+    try:
+        from src import lifecycle_hooks as _hooks
+        _hooks_ctx = {
+            "session_id": session_id,
+            # Session has no workspace column; best-effort via its project,
+            # else None (scope="project" hooks then simply never match).
+            "workspace": _workspace_for_session(sess) if "_workspace_for_session" in dir() else None,
+            "user_message": _last_user_text if "_last_user_text" in dir() else "",
+        }
+        try:
+            asyncio.get_running_loop().create_task(_hooks.run_async("turn_end", _hooks_ctx))
+        except RuntimeError:
+            threading.Thread(
+                target=lambda: asyncio.run(_hooks.run_async("turn_end", _hooks_ctx)),
+                daemon=True,
+            ).start()
+    except Exception:
+        logger.debug("[lifecycle_hooks] turn_end run failed", exc_info=True)
```

`_workspace_for_session`/`_last_user_text` are placeholders: resolve them
however this integrator's code already gets a session's workspace/last user
text at this point in `save_assistant_response` (e.g. via `sess.project_id`
+ `ProjectStore`, or a `last_user_message` parameter already in scope) —
this lot does not touch `routes/chat_helpers.py` so it cannot pin the exact
expression.

## 7. Studio card

The UI lot builds its settings card from the routes in
`routes/lifecycle_hooks_routes.py` (`GET/PUT /api/lifecycle-hooks`, the
presets endpoint, `/test`, `/log`) — no additional wiring needed here beyond
registering the router (step 1).
