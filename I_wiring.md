# Lot I wiring — for the integrator

Everything in this lot lives in files this branch owns (`src/instincts.py`,
`src/agent_tools/instinct_tools.py`, `routes/instincts_routes.py`, the
append-only registries). Three things still need an integrator-owned file
touched; the third of those three turns out to need **no change at all** —
see below.

## 1. `src/tool_execution.py` — dispatch: **no change needed**

`manage_instincts` is registered in `TOOL_HANDLERS` (see
`src/agent_tools/__init__.py`, right after
`TOOL_HANDLERS.update(DESKTOP_SEMANTIC_TOOL_HANDLERS)`) via a thin adapter:

```python
async def _manage_instincts_adapter(content, ctx):
    from src.agent_tools.instinct_tools import do_manage_instincts
    owner = ctx.get("owner") if isinstance(ctx, dict) else None
    return await do_manage_instincts(content, owner=owner)


TOOL_HANDLERS["manage_instincts"] = _manage_instincts_adapter
```

`src/tool_execution.py`'s big dispatch chain already ends in a generic
fallback that this new name falls through to untouched — none of the
earlier `elif tool == "..."` branches name `"manage_instincts"`, so it
reaches:

```python
    elif tool in dynamic_handlers:          # dynamic_handlers = agent_tools.TOOL_HANDLERS
        ...
        res = await _direct_fallback(
            tool, content, progress_cb=progress_cb,
            session_id=session_id, owner=owner, disabled_tools=disabled_tools,
            tool_policy=tool_policy, security_context=security_context,
        )
        ...
```

`_direct_fallback` builds `ctx` (a dict, with `"owner"` in it) and calls
`TOOL_HANDLERS[tool](content, ctx)` — exactly the adapter's signature. So
this dispatches correctly **today**, with zero changes to
`tool_execution.py`. (This is the "check first" case COMMON.md's rule 
calls out — confirmed by reading the full dispatch chain end to end.)
`tests/test_i_wiring.py` asserts this behaviour directly (not `xfail` —
it already passes).

## 2. `src/agent_loop.py` — inject `render_block` after the repository map

Exact context, in `_stream_agent_loop_body` right after the existing
repo-map injection (before the "Project concepts" block):

```diff
     _t0 = time.time()
     _needs_admin = _detect_admin_intent(messages)
     _last_user = _extract_last_user_message(messages)
     # Repository map (src/repo_map.py): files + top-level symbols of the
     # workspace, ranked for this request, injected once per turn as reference
     # data right before the user's message. Frozen for the whole turn so the
     # prompt prefix stays identical across rounds (local KV cache).
     if workspace and not guide_only and _hopts.get("repo_map", True):
         try:
             from src import repo_map as _repo_map
             _repo_map_text = await asyncio.to_thread(_repo_map.build, workspace, _last_user)
         except Exception as _rm_err:
             logger.debug("[repo-map] build failed: %s", _rm_err)
             _repo_map_text = ""
         if _repo_map_text:
             messages = _insert_before_latest_user(
                 messages,
                 untrusted_context_message("repository map", _repo_map_text, arm_tool_gate=False),
             )
+    # Lot I: learned instincts (src/instincts.py) -- small, per-project/
+    # global behaviours mined from past sessions, injected right after the
+    # repo map as reference data, never as a gate. Respects the same
+    # incognito/no_memory harness flags the personal-memory suppression
+    # below already reads (`_hopts.get("incognito")` /
+    # `_hopts.get("no_memory")`, see `suppress_personal_memory` further
+    # down this function), plus its own `instincts_enabled` setting so a
+    # fresh install pays nothing for the lookup with the feature off.
+    if (
+        owner
+        and get_setting("instincts_enabled", True)
+        and not _hopts.get("incognito")
+        and not _hopts.get("no_memory")
+    ):
+        try:
+            from src.instincts import project_key, render_block
+            _instincts_text = render_block(
+                owner,
+                project_key(workspace, _hopts.get("project_id")),
+                user_message=_last_user,
+            )
+        except Exception:
+            logger.debug("[instincts] render_block failed", exc_info=True)
+            _instincts_text = ""
+        if _instincts_text:
+            messages = _insert_before_latest_user(
+                messages,
+                untrusted_context_message("learned instincts", _instincts_text, arm_tool_gate=False),
+            )
     # Project concepts (src/project_concepts.py): the agent's own persistent,
     # per-project graph of architecture concepts. Off by default
     # (agent_project_concepts_inject) -- the agent can always call
```

`get_setting`, `owner`, `_hopts`, `workspace`, `_last_user`,
`_insert_before_latest_user` and `untrusted_context_message` are all
already in scope at this point in `_stream_agent_loop_body` (same names the
repo-map block right above uses).

`tests/test_i_wiring.py::test_agent_loop_injects_instincts_block` is marked
`xfail(strict=True)` until this lands; it calls `render_block` directly and
asserts the *behaviour* (an instinct surfaces in the rendered prompt for a
fake turn), so un-xfail it once the diff is applied — it will then fail if
the wiring is wrong, not just absent.

## 3. `routes/chat_helpers.py` — fire-and-forget extraction after a turn

`round_count`/`tool_count`: **no single existing metadata key reliably
carries a scalar "how many rounds/tool calls" through `last_metrics` to
this function's `md` dict across every terminal-metrics shape in
`src/agent_loop.py`** (three near-identical `terminal_metadata = {...}`
literals were read; none of them stamp a `round_count`/`tool_count`
scalar — only a harness summary dict, `_hsum["round_count"]`, which is
emitted as a *separate* SSE event, not folded into `terminal_metadata`).
The robust proxy used below instead:

- `tool_count` = `len(tool_events or [])` — `tool_events` is already a
  parameter of `save_assistant_response` (and already stamped verbatim to
  `md["tool_events"]` a few lines above the hook point), so this is exact,
  not a guess.
- `round_count` = `len(md["round_texts"])` when that key is present (some
  `terminal_metadata` shapes include a `round_texts` list, one entry per
  round), else `md.get("round_count")` if some future path sets a scalar
  directly, else `0`.

Exact context, right after the existing `notifications.emit("turn_finished")`
hook:

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
 
+    # Lot I: background instinct extraction. Fires from the same "one turn
+    # just landed" point as the notification hook above, and is unreachable
+    # for incognito turns for the same reason that hook is -- `incognito`
+    # already returned this function early (see the `if incognito:` branch
+    # above `sess.add_message(...)`), so everything below only ever runs for
+    # a turn that is being persisted for real. `tool_count`/`round_count`:
+    # see I_wiring.md's note on why these are derived rather than read off
+    # one fixed metadata key.
+    try:
+        _owner_for_instincts = getattr(sess, "owner", None)
+        if _owner_for_instincts:
+            from src.instincts import project_key, should_extract
+            _tool_count = len(tool_events or [])
+            _round_texts = md.get("round_texts")
+            _round_count = len(_round_texts) if isinstance(_round_texts, list) else int(md.get("round_count") or 0)
+            if should_extract(_round_count, _tool_count, _content):
+                import asyncio as _asyncio
+
+                async def _run_instinct_extraction():
+                    try:
+                        from services.projects import project_context_for_session
+                        from src.instincts import extract_from_turn
+
+                        ctx = project_context_for_session(session_id, _owner_for_instincts)
+                        history = [
+                            {"role": m.role, "content": m.content}
+                            for m in sess.get_context_messages()
+                        ]
+                        await extract_from_turn(
+                            _owner_for_instincts, session_id, history,
+                            project=project_key(ctx.workspace, ctx.project_id),
+                            project_name=ctx.project_name, workspace=ctx.workspace,
+                        )
+                    except Exception:
+                        logger.debug("[instincts] extract_from_turn failed", exc_info=True)
+
+                _asyncio.ensure_future(_run_instinct_extraction())
+    except Exception:
+        logger.debug("[instincts] post-turn extraction dispatch failed", exc_info=True)
+
     if wires:
         try:
             from src.side_threads import note_wires_used
             note_wires_used(wires)
```

`sess.get_context_messages()` is the same accessor
`services/memory/skill_extractor.py::maybe_extract_skill` already uses for
"this session's recent messages" — reused here rather than a second
history read.

`tests/test_i_wiring.py::test_chat_helpers_fires_instinct_extraction` is
`xfail(strict=True)` until this lands.

## Settings appended (`src/settings.py`, already in this branch)

`instincts_enabled` (True), `instincts_extract_enabled` (True),
`instincts_inject_threshold` (0.7), `instincts_inject_max` (6),
`instincts_extract_max_per_turn` (3), `instincts_promote_min_projects` (2),
`instincts_promote_min_confidence` (0.8).
