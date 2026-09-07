# Guided AI provider connections

Implemented API-key slice in Settings → Models. This is an Operate refinement
of the existing Faustus settings surface, not a new visual identity.
`DESIGN.md` and `.impeccable/design.json` remain unchanged.

## Implemented flow

The first view presents OpenAI, Claude and Gemini above the existing advanced
endpoint form. Selecting a provider reveals a password field, its official key
page link, and an explanation that API billing is separate from consumer chat
subscriptions. Provider URLs are presets; model inventories are not bundled.

1. Select a provider and paste an API key.
2. **Check connection** asks the Faustus server for the provider's model list.
3. A successful check must also return `private_connections_supported: true`.
   Without that marker, the UI asks the user to restart the updated server and
   does not advance to model selection or private creation.
4. Select one or more returned model IDs. Nothing is selected automatically.
5. **Connect selected models** saves an API/LLM endpoint with explicit pinned
   IDs, `shared: false` and `require_models: true`, then refreshes the connections.

Model-list verification is metadata access, not a generation test or a claim
about every model's capabilities. An empty catalog cannot be connected through
this flow. Existing endpoint management and DeviceSignIn remain separate; this
increment does not integrate official CLI/subscription sign-in.

## Privacy and request states

The API key is transient React state and is sent to the existing server-owned
credential store; this component does not write it to browser storage or logs.
Switching providers, cancelling, or completing a save clears the entered key
and model selection. Raw provider errors are not displayed by this component.

Check and save requests have a 45-second client deadline. Switching providers
or cancelling aborts a check; controls that could interrupt a save are disabled
while saving. Unmount aborts the active request and stale responses are ignored.
Progress and result copy use inline status announcements. A failed or timed-out
save is described as unconfirmed, with instructions to refresh connections
before retrying because server completion may already have occurred.

The existing server routes remain admin-only. Private creation requires a
signed-in caller, considers only that caller's owned rows for deduplication, and
cannot reuse a shared row. Creating a private connection does not assign it as
the global default. This is not regular-user self-serve onboarding or a secrecy
boundary against server administrators.

## Layout and components

The surface reuses settings typography, fields, help text, buttons, spacing and
border tokens. The selected provider has a brand border, a raised surface fill
and `aria-pressed`; provider choices have an accessible group label. The key
field has a visible associated label. Models use native checkboxes inside a
labelled fieldset, with a selected-count summary beside the actions.

The form is capped at 640 px. The catalog scrolls within a 280 px maximum height;
model rows and quick-connect buttons have a 44 px minimum height. Long model IDs
can wrap, shrinkable grid children prevent horizontal overflow, and action rows
wrap at narrow widths. No new token system, assets or decorative motion were added.

## Verification and release boundary

The independent finish reviewer returned **SHIP**, with no blocking findings
within this API-connection scope. Browser evidence uses the real component and
CSS with synthetic responses, not real accounts or providers:

- `.impeccable/review/provider-desktop.png`: 1920 px desktop.
- `.impeccable/review/provider-models-mobile.png`: final 390 px mobile catalog,
  including long model IDs and wrapped buttons.
- `.impeccable/review/provider-error-mobile.png`: earlier 390 px mobile error
  capture; the error layout was unchanged in the final pass.

Pure adapter checks and endpoint privacy tests passed (171 tests), including a
rerun after the server-version guard. The broader focused suite passed (286
tests); TypeScript and the production build also passed. These results do not
establish successful production private creation or provider generation access.

The running backend was **not restarted** during this work. Restart the updated
Faustus backend before using the new private-connection flow. Official CLI and
subscription integration remains unimplemented in this slice.

## Implementation sources

- `studio/src/screens/settings/ProviderConnect.tsx`
- `studio/src/screens/Settings.tsx`
- `studio/src/screens/settings.css`
- `studio/src/adapters/settings.ts`
- `studio/src/lib/provider-presets.ts`
- `routes/model_routes.py`
