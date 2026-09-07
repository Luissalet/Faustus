---
version: 1
slug: "chat-model-connections"
primary_target: "studio/src/screens/ModelConnections.tsx"
related_targets: ["studio/src/screens/ModelPicker.tsx", "studio/src/screens/ModelPalette.tsx", "studio/src/screens/settings/ProviderConnect.tsx"]
---

# Connections from chat

Mode: Operate. Narrow hardening extension of the existing picker and guided
provider forms. Existing DESIGN.md, Dialog and Button own the visual system.
No global restyling or new credential store. Product context remains the incumbent.

THESIS: connect a private provider without losing the current conversation or
silently changing its model. API and official-client subscription are distinct.

FORM: protected-focus dialog launched from the model palette. Reuse API form;
Claude text connection progressively reveals access method, optional model and
official setup help. Explicit action, bounded request, actionable errors and
uncertain-save message. Controls remain usable at mobile width.

FINISH: batched desktop/mobile inspection, one grouped correction and final
confirmation at 1920/390 px. Fields/buttons measure 44 px; document width equals
390 px with no horizontal overflow. Repeated heading removed; selected route has
aria-pressed. No actual account connection or paid generation was attempted.
Backend contracts, TypeScript, translations and build pass. No independent review.

Scope and unverified Codex/live-generation work: docs/design/official-client-chat.md.
