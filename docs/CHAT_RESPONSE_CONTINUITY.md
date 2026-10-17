# Chat response continuity

Studio keeps the currently visible answer while the server drafts a replacement. Empty `response_replace` events start a buffered revision; a complete non-empty replacement or successful stream completion publishes the new text. Tool activity and reasoning continue to update. A failed revision retains the visible draft together with the error; canonical history remains authoritative after completion.

Group snapshots retain `detail.display_text` during revision so opening the room while the new answer is being drafted does not blank it. Terminal steps and canonical history supersede the live preview. This does not change model execution, retries, stop controls or stored answers.

Regression checks: `node studio/checks/group-continuity.check.mjs` and `pytest tests/test_group_native_bridge.py`. The regression was observed in a native run replay with three empty replacements followed by its final saved answer.
