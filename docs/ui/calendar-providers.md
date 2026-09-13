# Calendar providers (Studio) — CONTRATO_GOOGLE_CALENDAR, lote G2

The Studio's "Add → Calendar" flow (`studio/src/screens/settings/Integrations.tsx`)
offers four providers behind one entry point. All four end up as one of two
`IntegrationKind`s in the unified Integrations list
(`studio/src/adapters/integrations.ts`):

| Provider | `IntegrationKind` | Form | Notes |
|---|---|---|---|
| Google Calendar | `google_calendar` | `GoogleCalendarForm` (`IntegrationsMore.tsx`) | OAuth only — no password field exists. Disabled with a `.env` hint when `GET /api/calendar/config/google` reports `configured: false`. |
| iCloud | `caldav` | `CalDavForm` (`IntegrationForms.tsx`), `preset="icloud"` | Prefills `https://caldav.icloud.com`; help text points at an app-specific password. |
| Nextcloud | `caldav` | `CalDavForm`, `preset="nextcloud"` | Help text shows the `/remote.php/dav` URL shape; URL is left blank. |
| CalDAV (other) | `caldav` | `CalDavForm`, `preset="other"` | The form exactly as it always was. |

The preset only prefills the URL and help text on a *new* account
(`CalDavForm`'s `preset` prop is ignored once `existing` is set) — CalDAV
itself does not know which of the three presets an already-saved account
came from, and does not need to: it is one account type, `kind: 'caldav'`,
same as before this lote.

## Why Google gets its own kind

Google's CalDAV endpoint requires OAuth 2.0; a username/app-password pair
gets a 401. Google Calendar is therefore its own `source` in the backend
(`Calendar.source === 'google'`, alongside `'caldav'` and `'local'`) and its
own `IntegrationKind` in the Studio, with its own adapter functions
(`listGoogleCalendar`, `googleCalendarAuthorizeUrl`, `deleteGoogleCalendar`,
`testGoogleCalendar`, `saveGoogleCalendar` — `studio/src/adapters/
integrations.ts`). See `Calendar.tsx`'s `hasRemote` (both `caldav` and
`google` disable-vs-enable the "Sync remote calendars" button) and its
`CalendarsDialog` row, which now labels a calendar "Google" / "CalDAV" /
local by `source`.

## The OAuth round trip

1. `Integrations.tsx` never calls the authorize route with `fetch` — a full
   navigation (`window.location.assign`) is required, because the next hop
   is Google's own consent screen.
2. G1's callback (`routes/calendar_routes.py`) is expected to land back on
   **`/settings?s=integrations&calendar_oauth=ok`** on success, or
   **`&calendar_oauth_error=<code>`** on failure — `s=integrations`, not
   `section=integrations`: `SettingsScreen` (`studio/src/screens/
   Settings.tsx`) reads the section from the `s` query parameter (see
   `SectionKey`, `SECTIONS`), not `section`. `?section=integrations` is the
   *previous* interface's own parameter (still used by the existing
   `routes/email_routes.py` Google OAuth callback, which redirects to `/`,
   not `/settings` — a page the Studio's router does not special-case, so
   today even that older flow lands on Studio Home without a banner).
3. `IntegrationsSection` reads `calendar_oauth` / `calendar_oauth_error` off
   `useSearchParams()`, shows a banner through the section's own `say()`,
   and strips both params from the URL so a later reload does not replay
   the banner.

## Degrading without G1

`listGoogleCalendar()` is wrapped in `fetchAll`'s existing `safe()` helper
(the same one every other integration list already uses) and degrades to
`{configured: false, accounts: []}` when the route 404s or the network call
otherwise fails — the unified list still renders every other integration,
Google Calendar just doesn't appear until the route exists, and the
"Connect with Google" button stays disabled with the `.env` hint until
`configured` is `true`.
