---
version: 1
slug: "studio-src-screens-settings-providerconnect-tsx"
primary_target: "studio/src/screens/settings/ProviderConnect.tsx"
related_targets: ["studio/src/screens/Settings.tsx","studio/src/screens/settings.css","studio/src/adapters/settings.ts"]
---

# Guided AI provider connections

Mode: Operate. Implemented refinement of Settings → Models using the incumbent
Faustus identity and components. Product context came from existing code and
`DESIGN.md`; `PRODUCT.md` was absent. No global design files were changed.

THESIS: select OpenAI, Claude or Gemini, paste one API key, discover and choose
models, connect. Preset URLs remove protocol entry; no invented model catalog.

OWN-WORLD: existing settings spacing, fields, buttons and inline help/status
copy. A brand-bordered pressed provider is the local selection cue, not a new
identity. Native checkboxes and labelled fieldsets carry model selection.

STORY: provider choice → official credential link → verify catalog and server
private-connection support → explicit model selection → save private endpoint
→ refresh connections. The check is metadata-only; it does not test generation
access or model capabilities. No model is preselected and no global default is
assigned by private creation.

FIRST VIEWPORT: three named provider buttons and API/subscription billing
explanation above the advanced endpoint form. Selecting one reveals the key
field and progressive form; existing endpoint management remains available.

FORM: code-led extension of the existing surface. Form width caps at 640 px;
catalog height caps at 280 px with scrolling. Model rows and buttons are at least
44 px high. Long IDs and action buttons wrap at mobile widths. No new assets,
token primitives, visual seed or decorative motion.

FINISH: independent reviewer SHIP, no blocking scoped findings. Real-component
and CSS synthetic browser review: `provider-desktop.png` at 1920 px and final
`provider-models-mobile.png` at 390 px in `.impeccable/review/`. The earlier
`provider-error-mobile.png` is 390 px; its error layout remained unchanged.
No real provider credentials/accounts were used. Adapter/privacy checks passed
(171, rerun after the final guard), broader focused tests passed (286), and
TypeScript plus production build passed.

## Shipped state boundaries

- API keys stay in component state until sent to the server; this component
  does not persist them in browser storage or logs. Provider changes, cancel and
  confirmed success clear the key. Raw provider error text is not rendered.
- The client deadline is 45 seconds. Checks can be cancelled; saving disables
  provider changes/cancel. An uncertain save asks the user to refresh connections
  before retrying. Unmount aborts work and stale responses cannot update state.
- Private saves send explicit pinned IDs, `shared: false` and
  `require_models: true`. Server deduplication excludes shared rows for private
  requests; private creation does not seed the global default. Admin-only gates
  remain unchanged; this is not regular-user self-serve onboarding.
- A successful check must include `private_connections_supported: true` before
  the UI offers private creation. The running backend was not restarted; users
  must restart the updated Faustus backend. Production private creation and
  generation access were not tested.
- API usage is separate from consumer chat subscriptions. Existing DeviceSignIn
  is a distinct path. Official CLI/subscription integration was requested later
  but is not implemented by this API-key slice.

Full implementation record: `docs/ui/provider-connect.md`.
