# Configuring Google (OAuth client)

Google Calendar and Gmail OAuth in Faustus both authenticate through one
Google Cloud "OAuth client" — one `client_id`/`client_secret` pair, with a
scope and a redirect URI added per feature, never a second client. This is
the same walkthrough **Settings › Integrations › Google** (the setup
wizard, `studio/src/screens/settings/GoogleOAuthSetup.tsx`) shows inline,
with your own redirect URIs already filled in and a Copy button on each —
paste them here for the menu names in both languages, or just open the
wizard and follow along there.

As of this guide, `.env` is optional: the client can be pasted straight
into the wizard, encrypted at rest, and used immediately — no restart. See
`src/google_oauth_client.py` for the storage and resolution order (the
in-app store first, the `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`
environment variables as a fallback, for Docker/CI deployments that already
set them).

## Steps (console.cloud.google.com)

1. **Project** / *Proyecto* — open
   [console.cloud.google.com](https://console.cloud.google.com) and create
   (or pick) a project.
2. **Enable the Calendar API** / *Habilitar la API de Calendar* —
   **APIs & Services** / *APIs y servicios* → **Library** / *Biblioteca* →
   enable the **Google Calendar API**. Enable the **Gmail API** too if you
   also want to connect email through this same client.
3. **Consent screen** / *Pantalla de consentimiento* —
   **APIs & Services** / *APIs y servicios* →
   **OAuth consent screen** / *Pantalla de consentimiento OAuth* →
   **User Type** / *Tipo de usuario*: **External** / *Externo*.
4. **Test user** / *Usuario de prueba* — **Publishing status** /
   *Estado de publicación*: **Testing** / *Prueba*, then add your own
   Google account under **Test users** / *Usuarios de prueba*. In Testing,
   Google expires the refresh token after 7 days; publishing the app
   without Google's verification review is fine for personal/self-hosted
   use and lifts that limit.
5. **Credentials** / *Credenciales* — **Credentials** / *Credenciales* →
   **Create Credentials** / *Crear credenciales* →
   **OAuth client ID** / *ID de cliente de OAuth* →
   **Application type** / *Tipo de aplicación*:
   **Web application** / *Aplicación web*. Under
   **Authorized redirect URIs** / *URI de redirección autorizados*, add
   both redirect URIs Faustus shows you (the wizard has a Copy button on
   each; they follow the pattern below).
6. **Paste it back** / *Pégalo de vuelta* — copy the **Client ID** and
   **Client secret** Google shows you into Settings › Integrations ›
   Google, or download the `client_secret_*.json` file Google offers and
   drop/paste it there instead — the wizard reads either shape Google
   downloads (`{"web": {...}}` or `{"installed": {...}}`) and pulls both
   values out of it.

## Redirect URIs

Two routes, one per feature, both under the same client:

```
<your origin>/api/calendar/oauth/google/callback
<your origin>/api/email/oauth/google/callback
```

`<your origin>` is exactly the scheme and host you use to reach Faustus —
`http://localhost:7000`, `https://your-domain.com`, a Tailscale hostname,
whatever it is *for you*. Google matches a redirect URI **letter for
letter**: `localhost` and `127.0.0.1` are two different origins to Google
even though they reach the same machine, so register the one you actually
type into the browser. The wizard computes both URIs from the request it
is itself being loaded from (`X-Forwarded-Proto`/`X-Forwarded-Host` behind
a reverse proxy, else the request's own scheme and `Host` header), so what
it shows you is always correct for however you're currently reaching
Faustus.

For a hosted or reverse-proxied install where the origin the wizard infers
is wrong (or you want to pin it), the environment variables still work
exactly as before and take priority over both the in-app store and the
inferred value:

- `GOOGLE_CALENDAR_OAUTH_REDIRECT_URI` — the calendar callback, used
  verbatim.
- `GOOGLE_OAUTH_REDIRECT_URI` — the email callback, used verbatim; with no
  calendar-specific override set, the calendar URI is derived from this one
  by swapping its *path* (keeping scheme and host).

## Where the client lives

`GET/PUT/DELETE /api/google/oauth-client` and
`POST /api/google/oauth-client/check` (`routes/google_oauth_routes.py`) are
the wizard's backend. `GET` never returns the secret — only a truncated
`client_id_hint`, a `configured`/`source` (`"stored"` or `"env"`) flag, and
the two redirect URIs. `check` makes one real, harmless call to Google (a
token refresh with a refresh token Google can never have issued) to tell
you whether the id/secret pair itself is valid, without needing to run a
whole OAuth round-trip first.

## Related

- `docs/api/google_calendar.md` — the Calendar OAuth flow and sync this
  client is used for.
- `.env.example` — the environment-variable fallback, still supported.
