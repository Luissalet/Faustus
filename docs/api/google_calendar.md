# Google Calendar (OAuth2 + Calendar API v3)

Google's CalDAV endpoint rejects Basic Auth, so Google Calendar is *not*
wired up as a generic CalDAV account (that path 401s). Instead it gets its
own OAuth2 flow and talks to the REST **Calendar API v3** directly over
`httpx` — no new dependency (`google-api-python-client` is deliberately not
used).

The account model is a `source="google"` calendar sitting next to
`source="caldav"` and `source="local"` ones: same `Calendar`/`CalendarEvent`
tables, same write-back dispatch (`routes/calendar_routes.py`), same
`/api/calendar/sync` endpoint.

## Setup

As of G3 (`src/google_oauth_client.py`), `.env` is optional: **Settings ›
Integrations › Google** is an in-app wizard that shows the exact Google
Cloud steps, your redirect URIs with a Copy button, and accepts either the
pasted `client_id`/`client_secret` or the `client_secret_*.json` file
Google offers to download — saved encrypted, live immediately, no restart.
Full walkthrough (with the Google Cloud menu names in English and Spanish):
`docs/guides/google-oauth.md`. The short version:

1. In [console.cloud.google.com](https://console.cloud.google.com) → APIs &
   Services → Library, enable the **Google Calendar API** for your project.
   (If you've already set up Gmail OAuth for email, this is the *same*
   OAuth client — you're only adding a Calendar scope and a new redirect
   URI to it, not creating a second client.)
2. APIs & Services → Credentials → your OAuth 2.0 Client ID (Web
   application) → **Authorized redirect URIs** → add:
   `http://localhost:7000/api/calendar/oauth/google/callback`
   (replace host/port for a hosted install; see below).
3. If your OAuth consent screen has an explicit scope allowlist, add
   `https://www.googleapis.com/auth/calendar`.
4. Paste the client ID and secret into Settings › Integrations › Google —
   or, for Docker/CI deployments, `GOOGLE_OAUTH_CLIENT_ID`/
   `GOOGLE_OAUTH_CLIENT_SECRET` in `.env` still work exactly as before and
   take priority when nothing is saved in-app.

## Redirect URI resolution

Centralized in `src/google_oauth_client.py::redirect_uris` (G3.1) — both
`GET /api/calendar/oauth/google/authorize`/its callback and the equivalent
email routes call the same function, checked in this order:

1. `GOOGLE_CALENDAR_OAUTH_REDIRECT_URI`, if set — used verbatim.
2. Else, if `GOOGLE_OAUTH_REDIRECT_URI` (the email OAuth redirect) is set,
   its **path** is swapped for `/api/calendar/oauth/google/callback`,
   keeping its scheme and host. This is a deliberate substitution, not a
   string-replace of `/email/` → `/calendar/` — nothing guarantees that
   substring is present.
3. Else, inferred from the incoming request — `X-Forwarded-Proto`/
   `X-Forwarded-Host` when present (reverse proxy), else the request's own
   scheme and `Host` header. Fine behind a correctly configured proxy or a
   plain local/LAN install; pin (1)/(2) when it isn't.

The same function returns the `origin` (`scheme://host`) the Settings ›
Integrations › Google wizard shows the redirect URIs against, with a note
that Google matches a redirect URI letter-for-letter — `localhost` and
`127.0.0.1` are different origins to Google even though both reach the
same machine.

## Routes (`routes/calendar_routes.py`, prefix `/api/calendar`)

| Route | Notes |
|---|---|
| `GET /oauth/google/authorize?account_id=` | `require_user`. 400 with a clear message if the OAuth client isn't configured. `account_id` reconnects an existing account (email must match on callback); omitted, a new account is created. |
| `GET /oauth/google/callback` | Verifies the signed state, exchanges the code, resolves the email via `userinfo`, encrypts + stores the tokens, redirects to `/settings?s=integrations&calendar_oauth=ok` or `&calendar_oauth_error=<code>`. |
| `GET /config/google` | `{configured: bool, accounts: [{id,label,email,status,last_sync_at,selected_calendars}]}` — no tokens, ever. |
| `DELETE /config/google/{id}` | Revokes at Google (best-effort) and removes the account. |
| `POST /config/google/{id}/test` | `{ok, calendars: [{id, summary, primary}]}` (or `{ok:false, error:"needs_reauth"}`) — lists the account's calendars without writing anything; also the source for the "which calendars to sync" checklist. |
| `PUT /config/google/{id}` | `{label?, selected_calendars?}`. `selected_calendars: null` means "all"; the primary calendar is always included regardless. |
| `POST /sync?direction=pull\|push\|both` | Now syncs CalDAV **and** Google; the historical top-level shape (`calendars`/`events`/`deleted`/`errors` for `pull`, `events`/`errors` for `push`, `push`/`pull` for `both`) is the CalDAV+Google *total*, with `caldav`/`google` keys added alongside carrying each provider's own breakdown. |

Error codes on the callback redirect (`calendar_oauth_error=<code>`):
`google_error`, `missing_code`, `invalid_state`, `not_configured`,
`token_exchange_failed`, `identity_verification_failed` (reconnect with a
different Google account than the one already on file),
`account_not_found`.

## The OAuth client itself (G3)

`src/google_oauth_client.py` / `routes/google_oauth_routes.py` — see
`docs/guides/google-oauth.md`. Not specific to Calendar: the same client
also backs Gmail OAuth (`routes/email_routes.py`), one scope and redirect
URI added per feature.

## Storage (`src/google_calendar_accounts.py`)

Accounts live in the owner's prefs (`routes.prefs_routes`, same mechanism as
`caldav_accounts`) under the `google_calendar_accounts` key:

```json
[{"id": "<uuid>", "label": "Google · luis@gmail.com", "email": "luis@gmail.com",
  "access_token": "enc:...", "refresh_token": "enc:...", "token_expiry": "<unix>",
  "status": "ok" | "needs_reauth", "sync_tokens": {"<google calendar id>": "<syncToken>"},
  "selected_calendars": null | ["<google calendar id>", ...],
  "created_at": <epoch>, "last_sync_at": <epoch|null>}]
```

`access_token`/`refresh_token` are always `src.secret_storage`-encrypted at
rest and are stripped from every API response (`list_accounts`/`get_account`
default to `public=True`). `access_token_for(owner, account_id)` refreshes
when the token expires within 60s; a refresh whose Google error is
specifically `invalid_grant` (the refresh token was revoked/expired) marks
the account `needs_reauth` — nothing else does, so a transient network
failure doesn't force a reconnect.

## Sync (`src/google_calendar_sync.py`)

- **Pull**: per account → `calendarList` → one local `CalendarCal`
  (`source="google"`, `account_id`, `id` = a stable hash of
  `(owner, account_id, google_calendar_id)`). Per calendar → `events.list`
  with the stored `syncToken` (first pull: `timeMin=now-90d`,
  `timeMax=now+365d`, `singleEvents=false`, `showDeleted=true`, paginated);
  a `410` drops the token and redoes that one calendar as a full window
  sync. `selected_calendars` (when not `null`) filters which of the
  account's Google calendars get pulled; the primary calendar is always
  included.
- Field mapping: Google event `id` → `remote_href`, `etag` → `remote_etag`
  (used for `If-Match` on push — a `412` is surfaced as a conflict, same
  CONN-04 shape CalDAV uses), local `uid = "google:{account_id}:{event_id}"`.
  `status="cancelled"` on a master event deletes it locally. A cancelled
  *exceptional instance* (`recurringEventId` + `status=cancelled`) becomes
  an `EXDATE` on the local master series. A **modified** (non-cancelled)
  exceptional instance is materialized as its own one-off local event
  (`origin="google-instance"`) — **known limitation**: Faustus's local model
  has no first-class "modified occurrence of a series" concept beyond
  RRULE+EXDATE, so this shows up and can be edited/deleted like any other
  event, but is not visually linked back to its parent series.
- **Push**: `push_event_create/update/delete(owner, uid)` — same call
  signature and return shape (`{"ok"}`/`{"conflict": True}`/
  `{"skipped": True}`/`{"ok": False, "error"}`) as `src.caldav_sync`, so
  `routes/calendar_routes.py::_push_caldav_event_after_commit` dispatches to
  whichever provider module actually owns the calendar without needing to
  know which one up front (see G1.4 below).
- A `401` triggers exactly one forced token refresh + retry; if that also
  fails, the event is left `caldav_sync_pending` for the next `/sync` and
  the account is left alone unless Google specifically said `invalid_grant`.

## G1.4 — dispatch by `source`

`_push_caldav_event_after_commit` tries `src.caldav_sync` **and**
`src.google_calendar_sync`'s `push_event_*` for the same `(owner, uid)`;
each resolves the event (or, for a delete, the `CalendarDeletedEvent`
tombstone → its calendar) and no-ops (`{"skipped": True}`) when that
calendar isn't theirs. Exactly one is expected to actually apply. This
avoids pre-resolving `source` (which would otherwise need the calendar row —
already gone for a delete by the time this hook runs) while keeping CalDAV's
own behavior byte-for-byte unchanged (its `push_event_*` is unaffected;
`google_calendar_sync`'s loaders reject non-Google calendars the same way
`caldav_sync`'s reject non-CalDAV ones).

The `_is_remote_source(source)` helper (`source in ("caldav", "google")`)
replaces the five internal `is_caldav`/gate checks that decide *whether* to
mark `caldav_sync_pending` and call the dispatcher at all — the one line
`caldav_sync_pending="create" if cal.source == "caldav" else None` is left
untouched (pinned verbatim by `tests/test_caldav_bidirectional_sync.py`);
Google gets the same `"create"` flag set right after, in a follow-up
assignment. `src/tools/calendar.py` (the agent's `manage_calendar` tool)
gets the identical treatment so agent-created events on a Google calendar
also sync, not just ones created through the HTTP route.

`CalendarDeletedEvent` (`caldav_deleted_events` table) is shared between
providers unchanged — it already stores a generic `remote_href`/
`remote_etag`/`last_error`, just keyed by whichever calendar the deleted
event belonged to.

## Testing

`tests/test_google_calendar.py` — no live network. OAuth token/userinfo
calls are mocked via `mock.patch("httpx.post"/"httpx.get")` (mirrors
`tests/test_email_oauth.py`); Calendar API v3 pull/push calls run through
`google_calendar_sync._CLIENT_FACTORY`, monkeypatched per-test to
`httpx.Client(transport=httpx.MockTransport(handler))`.
