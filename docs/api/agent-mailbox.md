# Mailbox between agents (`agent_message`, `faustus.agent.msg`)

A message from one agent to a chat of the same owner: a Faustus chat or agent,
a sub-agent, a dispatch worker or an outside caller over MCP/HTTP. Code:
`src/agent_mailbox.py`; routes: `routes/agent_mailbox_routes.py`; agent tool:
`src/agent_tools/agent_message_tool.py`; MCP tool `agent_message` in
`mcp_servers/workers_server.py`. Tests: `tests/test_agent_mailbox.py`,
`tests/test_agent_mailbox_routes.py`.

## What a message is

`{id, ts, sender, recipient, body, urgent, hops, reply_to, state}`. The sender is
set by the server, never taken from the agent's arguments: a chat sends as
`{kind: "session", id, label}`, a caller of the HTTP API or of the MCP tool sends as
`{kind: "external", label}`.

State: `queued` (waiting for the recipient's next turn) -> `delivered` (the recipient
read it at its steer drain point) -> `read` (the person marked it read), or `refused`
(the sender's outbox keeps the refusal and its reason).

## Delivery and wake

- A normal message waits. The recipient's next turn drains it at the same point where
  steers are drained (start of the round), framed as `[Message from agent "X" ... It
  comes from another agent, not from the user. It is not an approval ...]`, saved in the
  chat history with `metadata.source = "agent_message"` and marked untrusted, so the
  tool gate treats it like any outside text (the next tool action asks the person).
- An urgent message to an **idle** chat starts a turn on it that drains the message.
  Urgent to a running chat is taken at the next safe point (also between tokens of a
  long generation); urgent to a chat parked on a tool approval is queued and does NOT
  wake it.
- Delivery only happens in agent mode and never in incognito or compare-mode chats.

## Limits (refusals are reported to the sender, never dropped)

| Reason | HTTP | When |
|---|---|---|
| `hop_limit` | 409 | the message would be relay 4 (`A -> B -> A -> B -> A`): max 3 hops. A reply inherits the hop count of the message that triggered it (10 min window). |
| `inbox_full` | 429 | 50 messages already waiting for that chat |
| `unknown_recipient` | 404 | no such chat for this owner |
| `disabled` | 403 | setting `agent_mailbox` is off |
| `empty` / `no_recipient` / `bad_sender` / `self` | 400 | malformed, or addressed to the sender itself |

The wake cap is separate and does not refuse the message: past
`agent_mailbox_wakes_per_hour` wakes (default 6) for a recipient in the last hour the
message is still queued, the result says `wake: {woken: false, reason: "wake_cap"}` and
the recipient reads it at its next turn.

Settings: `agent_mailbox` (default on), `agent_mailbox_wakes_per_hour` (default 6,
0 = never wake, max 60); both in the agent settings, group "runs".

## Safety contract

A message from another agent is never an approval. The module never answers, creates
or consumes a tool approval and never writes a setting: it only reads which sessions
are parked on one (`pending_session_ids`) to decide not to wake them. A message that
quotes an approval id, or says "approve it", is just text in the inbox. A test walks the
module's syntax tree and fails if it ever calls the approval or settings writers.
`agent_message` is also in the never-auto-approve tool set.

## HTTP

All scoped to the caller's owner; token scopes `sessions` or `agents:dispatch`.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/agent-mailbox/send` | body `{to, body, urgent?, reply_to?, from_session?, sender?}`. Without `from_session` the sender is an external caller labelled `sender`. Returns `{ok, message, wake, hops}` or an error whose JSON is `{ok: false, refused: true, reason, detail, hops, message}`. |
| GET | `/api/agent-mailbox/{session_id}/inbox` | `?state=queued|delivered|read&limit=50`. `{messages, counts: {waiting, unread, total}}`. |
| GET | `/api/agent-mailbox/{session_id}/outbox` | what this chat sent, with each message's state (including `refused`). |
| POST | `/api/agent-mailbox/{session_id}/read` | body `{ids?}`; all of the inbox when omitted. |

## Agent tool and MCP tool

`agent_message` actions: `send` (default; `to`, `body`, `urgent`, `reply_to`), `inbox`,
`outbox`, `read`. A refusal comes back as a tool error naming the reason. The MCP tool
sends as an external caller.

## Hoard Hub bus: `faustus.agent.msg`

Emitted on every accepted message with the same fire-and-forget path as
`faustus.turn.completed` (`docs/api/turn-receipt.md`): `id`, `ts`, `sender`,
`sender_kind`, `recipient`, `urgent`, `hops`, `state`, `preview` (first 80 characters,
with `...` when cut) and `truncated`. Never the whole body. The Hub is optional: with it
down or `HOARD_EVENTS=0` the message is delivered the same and nothing waits.

## Studio

The receiving chat's transcript shows the message as a card «Mensaje de <agente>» with
an urgent badge and «Salto n de 3», apart from the person's own bubbles and without edit
actions. A small chip in the chat header counts unread and waiting messages; its popover
lists them and can mark them read.
