# Lot D wiring — fix memory

Two integrator diffs. `tests/test_d_wiring.py` (xfail strict) turns green
once both are applied.

## 1. `routes/chat_helpers.py` — record after a turn

Inside `_post_turn_automation()` (around line 1342), right after the
existing instincts `extract_from_turn` try/except block and before the
`try: asyncio.get_running_loop().create_task(...)` line, add:

```python
            try:
                if not _post_turn_owner:
                    return
                from src import fix_memory
                await fix_memory.record_from_turn(
                    _post_turn_owner,
                    project_key=fix_memory.project_key(_post_turn_workspace, _post_turn_project_id),
                    user_message=_post_turn_last_user,
                    tool_events=list(tool_events or []),
                    harness_meta=md,
                    workspace=_post_turn_workspace or "",
                )
            except Exception:
                logger.debug("[fix_memory] record_from_turn failed", exc_info=True)
```

14 lines. `tool_events`, `md`, `_post_turn_owner`, `_post_turn_workspace`,
`_post_turn_project_id`, `_post_turn_last_user` are all already in scope at
that point (see the instincts block right above it).

## 2. `src/agent_loop.py` — recall + inject before a coding task

Right after the existing "Learned instincts" block (around line 6885, the
`if _inst_text:` block that ends the instincts injection), add:

```python
    # Fix memory (src/fix_memory.py, Lot D): past solved issues for this
    # project, recalled and injected the same way instincts already are.
    if (workspace and owner and get_setting("fix_memory_enabled", True)
            and get_setting("fix_memory_auto_recall", True) and not _hopts.get("incognito")):
        try:
            from src import fix_memory as _fixmem
            _fm_project = _fixmem.project_key(workspace, _hopts.get("project_id"))
            _fm_entries = _fixmem.recall(owner, _fm_project, _last_user or "", k=5)
            _fm_text = _fixmem.prompt_block(
                _fm_entries, budget_tokens=int(get_setting("fix_memory_prompt_budget_tokens", 600)),
            )
        except Exception:
            logger.debug("[fix_memory] recall/prompt_block failed", exc_info=True)
            _fm_text = ""
        if _fm_text:
            messages = _insert_before_latest_user(
                messages,
                untrusted_context_message("fix memory", _fm_text, arm_tool_gate=False),
            )
```

`workspace`, `owner`, `get_setting`, `_hopts`, `_last_user`,
`_insert_before_latest_user`, `untrusted_context_message` are all already in
scope there (same names the instincts block right above it uses).

`untrusted_context_message("fix memory", ...)` is classified into the
"Memories" section by `src/context_ledger.py`'s existing `_LABEL_RULES`
(`"memor"` substring match on the source label) — no ledger change needed.

## 3. `app.py` — mount the routes

Anywhere the other per-feature routers are mounted (next to
`routes.instincts_routes`):

```python
from routes.fix_memory_routes import setup_fix_memory_routes
app.include_router(setup_fix_memory_routes())
```

## Verified

- `python3 -m pytest -q tests/test_fix_memory.py` — 30 passed (recording,
  extraction, recall scoring, prompt block budget, rotation, forget, tool
  JSON output, route smoke tests against a standalone `FastAPI()` app —
  same pattern as `tests/test_git_radar.py`).
- `python3 -m pytest -q tests/test_d_wiring.py` — 2 xfailed (expected until
  the two diffs above land).
- Guard tests green: `test_tool_registry.py`, `test_tool_index_schema_parity.py`,
  `test_l91_tool_index_examples.py`, `test_external_context_tool_gate.py`,
  `test_agent_settings_schema.py`.

## Could not verify live

No running Faustus instance / browser session was available in this
worktree, so the actual chat turn → record → recall round trip (chat
phrases like "have we seen this error before" triggering `recall_fixes`,
or a real coding turn producing a `fix-*.jsonl` line) was exercised only
through the unit/route tests above, not through a live chat session.
