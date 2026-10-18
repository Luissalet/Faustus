# Reasoning between tool rounds

The agent retains reasoning from earlier assistant/tool rounds of the same
user turn when the managed endpoint has a verified native template profile
that supports interleaved thinking. It sends those blocks with the following
tool results. This fixes the previous producer behavior, which deleted the
first round's reasoning when the second tool round was appended.

The existing Qwen history behavior is unchanged. Other unverified models keep
the previous history policy. There is no new user flag or model-name exception
in the agent loop. The central engine-controls profile describes the template;
the existing managed-Sparks metadata sync verifies the served model identity
and typed native thinking controls in the endpoint's OpenAPI document.

This uses the existing metadata cache and its 60-second lifetime. Sparks sync
starts after 15 seconds and normally refreshes every 20 seconds. No HTTP probe
is added to generation. An expired, retired or changed endpoint/model identity
does not grant the retention capability. Metadata refresh failure can therefore
restore the previous stripping policy; this change does not pin stale proof
for an entire long turn.

Internal reasoning carries its originating endpoint/model identity. A fallback
candidate does not receive private reasoning produced by another route, while
tool calls and results remain paired. This filtering happens before candidate
compaction and context trimming. Provider projection removes the internal
origin field from the wire. Caller-supplied reasoning histories without that
private field retain their existing behavior.

The provider template controls clearing across user turns. The native adapter
still defaults `clear_thinking` to true for the verified profile and preserves
an explicit false already present in the native payload. This change adds no
new public override for that field. In the published GLM-5.3-Flash templates,
true removes reasoning before the last user message and retains reasoning
between subsequent tool rounds; false also retains previous-turn reasoning.

The context estimator already counts `reasoning_content`. Retained reasoning
is now included in the candidate's existing estimate and compaction path; a
fallback's removed blocks are excluded. The estimate remains a heuristic,
not an exact tokenizer measurement. It may conservatively count older blocks
that the template subsequently clears. Explicit context/output budgets and
the user-visible conversation are unchanged.

The API chat path, the `hoard_turn_start` MCP path and Studio use this same
agent loop. Private TCP tests cover two actual tool rounds, sync/async/stream
request bodies, fallback, cache invalidation and context estimates. Private
API/MCP/Studio checks use synthetic responses and real file tools; they prove
serialization and routing, not a model's reasoning quality. Reasoning remains
separate from the assistant's displayed answer and appears in the existing
collapsed thought/activity UI.

Primary contracts and template provenance:

- [Z.ai thinking mode](https://docs.z.ai/guides/capabilities/thinking-mode):
  return complete interleaved reasoning blocks with tool results; preserved
  thinking across user turns is a separate behavior.
- [Official GLM-5.3-Flash template, pinned revision](https://huggingface.co/zai-org/GLM-5.3-Flash/blob/46a050bec13002ad29ee795c16e61cf4fa3c4a9c/chat_template.jinja)
  and the deployed Kindling template at commit
  `c748079d45e6e070b2acb108a91edfe52f4a7747` were rendered against the actual
  private wire bodies. Both retain the two current-turn reasoning blocks and
  distinguish true/false clearing of earlier-turn reasoning.
