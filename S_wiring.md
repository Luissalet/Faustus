# Lot S wiring — for the integrator

Everything below is a change to a file this lot is not allowed to edit
(`src/agent_loop.py`, `app.py`). `tests/test_s_wiring.py` pins the two exact
diffs (marked `xfail(strict=True)`) so it turns green the moment they land
and fails loudly if they're reverted afterwards.

## 1. `src/agent_loop.py` — replace both `sm.get_relevant_skills(...)` calls

Two call sites use the old Jaccard-only method directly. Both get the exact
same drop-in replacement, `src.skills_runtime.selector.select(sm, owner,
query, ...)` — same keyword args, same return shape (plus a harmless
`_selector` diagnostics key in hybrid mode), so nothing else in either
call site needs to change. The existing pref/settings gates around each
call (`_skills_on`, `auto_approve_skills`, `skill_min_confidence`,
`skill_max_injected`) are untouched — this only swaps the ranking function.

### 1a. Level-1 skill injection (around line 4540, inside `_build_base_prompt`)

```diff
@@ -4537,7 +4537,8 @@
                 _skill_max_injected = max(0, min(12, _skill_max_injected))
-                relevant_skills = sm.get_relevant_skills(
-                    last_user,
+                from src.skills_runtime import selector as _skill_selector
+                relevant_skills = _skill_selector.select(
+                    sm, owner, last_user,
                     skills=sm.load(owner=owner),
                     threshold=0.25,
                     max_items=_skill_max_injected,
                     min_confidence=_skill_min_conf,
                 ) if _skill_max_injected > 0 else []
```

### 1b. Tool-RAG skill-aware toolset include (around line 7456)

```diff
@@ -7453,8 +7453,9 @@
                     from src.tool_policy import known_tool_names
                     _known = known_tool_names()
-                    for _sk in _sm.get_relevant_skills(
-                        _retrieval_query, skills=_owner_skills,
+                    from src.skills_runtime import selector as _skill_selector
+                    for _sk in _skill_selector.select(
+                        _sm, owner, _retrieval_query, skills=_owner_skills,
                         threshold=0.25, max_items=3,
                     ):
                         _skill_tools = {
```

(Two `from ... import selector as _skill_selector` lines, one per call
site — both are cheap: the module does no I/O at import time, everything
in it is lazy-imported per call.)

## 2. `app.py` — register the new router

Same shape as the existing skills router two lines above it:

```diff
@@ -806,6 +806,8 @@
 app.include_router(memory_router)
 from routes.skills_routes import setup_skills_routes
 app.include_router(setup_skills_routes(skills_manager))
+from routes.skill_selector_routes import setup_skill_selector_routes
+app.include_router(setup_skill_selector_routes())
 # A25: git-backed skill sources (pinned revision, verified update, rollback).
 from routes.skill_source_routes import setup_skill_source_routes
 app.include_router(setup_skill_source_routes())
```

## 3. Outcome prior loop — proposal, not a verified diff

This one is advisory: the assistant message's metadata (`metrics` dict) is
assembled at three separate call sites in `agent_loop.py` (roughly lines
5547, 6555, 6921), and picking the "one true" insertion point is an
integrator call, not something this lot can pin with a grep-testable diff.
The shape that fits the rest of the file:

1. Where `relevant_skills` is computed (§1a above), also keep the plain id
   list next to it:
   ```python
   _surfaced_skill_ids = [s.get("name") for s in relevant_skills if s.get("name")]
   ```
2. Wherever each `metrics = {...}` dict is built for the assistant message
   that turn, add:
   ```python
   metrics["surfaced_skills"] = _surfaced_skill_ids
   ```
   (empty list when no skills were surfaced — never omit the key, so the
   next turn can tell "no skills surfaced" apart from "we don't know".)
3. On the **next** turn, before building the new prompt, read the
   previous assistant message's `metadata.get("surfaced_skills")` (same
   place `last_meta`/`tool_events` is already read back, e.g. around line
   4489) and call:
   ```python
   from src.skills_runtime.selector import record_outcome_from_reaction
   record_outcome_from_reaction(owner, previous_surfaced_skills, last_user_message)
   ```
   `record_outcome_from_reaction` already no-ops on an empty list, a
   missing embedder, or a neutral reaction, so this is safe to call every
   turn unconditionally once `previous_surfaced_skills` is in hand.

No test pins this part — see `tests/test_s_wiring.py`'s comment for why.
