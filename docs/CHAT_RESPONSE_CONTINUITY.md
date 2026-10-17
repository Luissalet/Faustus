# Chat response continuity

Studio keeps the currently visible answer while the server drafts a replacement. Empty `response_replace` events start a buffered revision; a complete non-empty replacement or successful stream completion publishes the new text. Tool activity and reasoning continue to update. A failed revision retains the visible draft together with the error; canonical history remains authoritative after completion.

Groups reconstruct the visible draft through the existing native full stream replay when reopened. A blank stream handshake keeps the snapshot text. Terminal steps and canonical history supersede the live preview. This does not change server APIs, model execution, retries, stop controls or stored answers, and requires no server restart.

Regression check: `node studio/checks/group-continuity.check.mjs`. The regression was observed in a native run replay with three empty replacements followed by its final saved answer.
