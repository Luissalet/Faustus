# Client integration guide (OPS-06)

Minimal guide for a client (SDK, script, third-party integration) talking to
this server's HTTP API.

## Identify your client (optional, recommended)

Send `X-Faustus-Client-Version: <version>` on requests, using the dotted
numeric scheme (`"2.0"`, `"1.10.3"`) `src/api_version.py` parses. This is
optional — omit it and you are treated as compatible, same as every client
written before this scheme existed. Sending it gets you:

- a clean `426 Upgrade Required` with an actionable message if the server
  has moved its floor past what you speak, instead of a stream you cannot
  parse;
- a non-fatal adaptation notice if you're behind current but still
  supported.

## Check the server's version

`GET /api/version` returns the app version and build info, and stamps the
`X-Faustus-Api-Version` response header with the server's current
`API_VERSION`. Cheap and non-streaming — probe it before opening a chat
stream if your integration needs to know what it's talking to ahead of time.

## Backward compatibility contract

- Fields already documented as part of a response never change meaning or
  get removed without an announced deprecation (see `deprecations.md`) and,
  for anything a client cannot safely ignore, a `MIN_CLIENT_VERSION` raise
  in the same change.
- New fields are additive by default — an integration that only reads
  fields it knows about is unaffected by a field it has never heard of.

## Migrations

Nothing in this lot requires a config or client migration — see
`deprecations.md`; there is nothing active to migrate away from yet. When a
future lot does add one, its migration steps will be documented in this
file, in a dated section below.
