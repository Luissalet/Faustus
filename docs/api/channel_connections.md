# Native channel connections

Faustus connects directly to Slack, Discord and Matrix REST APIs. The native
implementation uses saved Integrations credentials and the normal HTTP executor;
it does not run another agent, gateway or CLI. No reference-project dependency
or copied upstream runtime is required. Telegram and WhatsApp retain their
existing implementations.

## Configure and use

Save a credential in Integrations using Slack messages, Discord messages or
Matrix messages. Slack uses a bearer token; Discord uses the complete
Authorization value (`Bot <token>` or `Bearer <token>`); Matrix uses its homeserver
URL and bearer access token. The token must have the service permissions needed
for the requested conversations. No new account or approval mechanism is added.

Call `channel_connection_save` with provider (`slack`, `discord`, `matrix`) and
the saved integration ID. Connections store metadata and that ID only; secrets
remain in the existing encrypted credential store. Updating a connection keeps
its ID. A registered credential is not a claim that a service connection works.

The built-in `channel_connections` MCP server provides eight tools:

| Tool | Result |
| --- | --- |
| `channel_providers` | Provider names and credential presets |
| `channel_connections` | Saved profiles and credential registration status |
| `channel_connection_save` | Create/update a profile referencing an Integration |
| `channel_spaces` | Slack conversations, Discord guilds/channels, Matrix joined rooms |
| `channel_history` | Full text, opaque message IDs/cursors and attachment metadata |
| `channel_message_send` | Send text and return the actual service message/event ID |
| `channel_message_edit` | Edit an existing message; Matrix uses an `m.replace` event |
| `channel_reaction_add` | Slack reaction name, Discord emoji or Matrix annotation |

Discord `channel_spaces` without `parent_id` lists guilds; provide a guild ID to
list its channels. The guild endpoint needs the appropriate OAuth scope and may
require a shard parameter for large sharded bots; shard configuration is not
implemented here. Messages use each service's normal access rules. Cursor values
and IDs are strings, never converted into timestamps/numbers. History is requested
newest first. A page size is passed to the service, which may apply its own limits.

Matrix send/edit/reaction accept an optional `transaction_id` for service-side
idempotency. Without it, a new ID is generated. A lost write reply is not retried;
`outcome_unknown` remains visible. A successful HTTP response without a message ID
also remains uncertain. No success receipt is invented.

## API

`GET /api/channel-connections/providers`, `GET /api/channel-connections`, and
`POST /api/channel-connections` mirror provider/profile operations. Profile fields:
`provider`, `integration_id`, optional `name` and `connection_id`.

`POST /api/channel-connections/run` accepts `connection_id`, `operation`
(`spaces`, `history`, `send`, `edit`, `react`), and the applicable `channel_id`,
`parent_id`, `cursor`, `limit`, `text`, `message_id`, `reaction`, `transaction_id`.
The local no-login installation keeps the existing Integrations access mode;
built-in MCP uses the parent API's internal identity and actual configured port.

Typed clients request full response data from Integrations while its existing
preview continues to be shortened. Service content is marked `untrusted_content`.
HTTP/service errors and ambiguous outcomes are returned rather than treated as
successful messages. HTTP 429 is a service error; this implementation adds no
automatic write replay or permanent polling process.

## Scope and verification

This is an initial native REST capability, not parity with all reference channels.
Incoming push subscriptions (Slack Socket Mode, Discord Gateway, Matrix sync),
attachment download/upload, Matrix encryption/decryption and channel-to-task
automation are not implemented. Encrypted Matrix events retain their raw fields;
they are not presented as decrypted text. No real account has been exercised.

Tests use isolated data and three protocol-faithful HTTP services. Native stdio
MCP makes 21 authenticated calls through the actual product API/HTTP executor.
Browser Swagger executes native history and verifies 16,000 characters plus
opaque cursor preservation. Additional regressions cover missing receipts,
provider errors, escaped IDs/reactions, edits and Integration data previews.

Provider contracts: [Slack Web API](https://docs.slack.dev/reference/methods/),
[Discord API](https://docs.discord.com/developers/resources/message),
[Matrix client-server API](https://spec.matrix.org/v1.16/client-server-api/).
