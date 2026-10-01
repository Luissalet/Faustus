# Chat bridges: talk to Faustus from Telegram

People should be able to reach their agent from the chat app they already use,
not only from Studio. The Telegram bridge (`src/chat_bridges/`) does that with
the official Bot HTTP API and **long polling** (`getUpdates` with `timeout` and
`offset`): Faustus only opens outgoing connections, so no public address, port
forwarding or webhook is needed.

It is **off by default**. Nothing is started, and no request leaves the
machine, until you turn it on and save a bot token.

## Setup

1. In Telegram, open the bot-creation chat, send `/newbot`, pick a name and a
   username, and copy the token it gives you (it looks like `123456:ABC...`).
2. In Studio open **Settings → Agent → Chat bridges: Telegram**.
   - Paste the token into **Bot token** and switch **Enable the Telegram
     bridge** on. Save. The poller starts at once; no restart is needed. The
     status line at the top of the group shows `Running as @your_bot`, and
     **Test the token** calls `getMe` with the saved token.
   - The token is stored encrypted (`enc:` prefix, the same storage as the
     other credentials) and is shown masked from then on. It is never returned
     by any route, written to a log or included in the status.
3. Find your chat id. Send any message to your bot from the chat you want to
   use. The bot answers `This chat is not authorised. Your chat id is 123456789.`
   once, and the id is also logged and listed under the status line (**Chats
   that wrote and are not on the list**). A group's id is negative
   (`-100...`).
4. Put the id in **Allowed chat ids** (comma-separated) and save. It applies to
   the next message; no restart. Write to the bot again and it answers.

## What a chat can do

| Message | Result |
| --- | --- |
| any text | one agent turn in this chat's conversation; the answer is sent back |
| `/new` | start a fresh conversation for this chat |
| `/status` | model, mode, a link to the conversation, whether a turn is running and how many messages are queued |
| `/help` | the list above |

- **One chat, one conversation.** Each Telegram chat maps to one Faustus
  session, kept in `DATA_DIR/chat_bridges/telegram.sqlite3`. The session is
  named `Telegram · <chat title or person>` and appears in Studio's
  conversation list like any other, with the whole transcript and tool cards.
- **A normal turn.** The message is saved in the session and the agent loop
  runs on it with the session's history, as for a background follow-up. In
  `agent` mode (the default) the agent has its usual tools; in `chat` mode
  (`telegram_agent_mode`) the model answers alone, without tools.
- **Typing indicator.** The bot shows "typing" while a turn runs.
- **Answers.** Markdown is converted to the small HTML subset Telegram accepts
  (bold, italic, strike, code, code blocks, links, quotes). All text is escaped
  first, so nothing in an answer can inject a tag. Answers are cut into
  messages of at most 4096 characters, at paragraph breaks when possible and
  never in the middle of a code block without closing and reopening it. If
  Telegram refuses the markup, the same text is sent without it.
- **Images.** An image the turn generated (a generated image, a render) is sent
  with `sendPhoto` (or as a document above 10 MB). Only files in the
  generated-images folder are ever sent.
- **Queueing.** One turn at a time per chat. Messages that arrive during a turn
  are queued in order (up to 20 per chat) and the bot says how many are ahead.
  `/status` and `/help` answer immediately. Different chats do not wait for
  each other.

## Security model

- **Allow-list.** Only chats listed in `telegram_allowed_chat_ids` are served.
  The list is empty by default, which means nobody. A chat that is not on it is
  logged once (with its id), told once its own id, and ignored from then on.
  Messages from other bots are ignored.
- **A group is one principal.** Everyone in an allowed group can use the agent
  as that group. Allow a group only if you would give each member the same
  access to your agent.
- **Whose account.** Conversations belong to `telegram_owner`, or the first
  administrator when it is empty (the account ownerless events already go to);
  with login switched off, the local owner. The agent uses that account's
  settings, tools and models.
- **Outside text.** A message from a chat is treated like any other text from
  outside the app: it is saved marked untrusted, so a tool that changes
  something (writing files, running commands, sending mail) stops at an
  exact-approval card instead of running.
- **No approvals from the chat.** The bot never approves anything and there is
  no `/approve`. When a turn stops at an approval card, the bot replies with
  what is waiting and a link, `<base>/studio?s=<session id>`, where you answer
  it. The base is `app_public_url` when set, otherwise `http://127.0.0.1:<port>`.
  The same goes for a question the agent asks.
  If you chose "Allow for this chat session" in Studio for that conversation,
  that choice applies to later turns from Telegram too, as it does in Studio.
- **Revoking.** Remove an id from the list, or switch the bridge off, and it
  stops answering immediately. A leaked token is revoked with `/revoke` in the
  bot-creation chat; the poller then stops on the 401 (below).

## Failure behaviour

- A network error, a timeout or a server error backs off 1, 2, 5, 10, then 30 s
  between attempts (30 s from then on) and goes back to 1 s after the first
  success. A rate-limit answer to a poll is respected. It never spins.
- A **401** (wrong or revoked token) stops the poller instead of retrying: the
  status says `Telegram rejected the bot token (401 Unauthorized)`. Saving a
  corrected token starts it again.
- A turn that fails is reported in the chat with the error. A session that is
  busy with a turn in Studio gets a "try again in a moment" reply rather than
  two writers on one transcript.
- The update offset is saved, so a restart does not hand the same messages to
  the agent twice. Messages queued in memory when Faustus stops are not kept.

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `telegram_bridge_enabled` | `false` | start the poller (with a token) |
| `telegram_bot_token` | `""` | the bot token; secret, stored encrypted, masked on read |
| `telegram_allowed_chat_ids` | `[]` | chat ids that may talk to the agent; empty = nobody |
| `telegram_agent_mode` | `agent` | `agent` (tools) or `chat` (the model alone) |
| `telegram_model` | `""` | model on the default endpoint; empty = the default chat model |
| `telegram_owner` | `""` | account that owns the conversations; empty = first administrator |
| `telegram_api_base` | `https://api.telegram.org` | overridable, for a local test server |

Changing any `telegram_*` setting through the settings route starts, restarts
or stops the poller to match. The allow-list, mode and model are read for every
message.

## Routes (admin only)

- `GET /api/chat-bridges/telegram/status` returns `enabled`, `configured` (a
  token is saved, never the token), `running`, `bot_username`, `bot_name`,
  `last_error`, `disabled_reason`, `failures_in_a_row`, `mode`, `model`,
  `allowed_chats`, `mapped_sessions` (chat, session, link), `refused_chats`,
  `busy_chats` and `queued_messages`.
- `POST /api/chat-bridges/telegram/test` calls `getMe` with the saved token and
  returns `{ok, username, name, id}` or `{ok: false, error}`.

Neither is part of the API-token surface (`core/authz.py` denies by default).

## MCP

The built-in `chat_bridges` server has one read-only tool, `telegram_status`:
the same report, read from the snapshot the poller leaves in its sqlite file
(the MCP server is a separate process), the settings and the chat mapping. A
snapshot older than 90 s reads as not running. It never includes the token and
never talks to Telegram.

## Limits

- Text in, text and images out. Photos, voice notes and files sent *to* the bot
  are not read yet (the bot says so).
- Telegram caps messages at 4096 characters; bots cannot send more than about
  one message per second to a chat, so a very long answer takes a few seconds.
- Long polling needs Faustus to be running; messages sent while it is off wait
  on Telegram's side (for up to 24 hours) and are answered when it starts.
- The turn timeout is 30 minutes.
