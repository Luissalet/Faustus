---
version: 1
slug: "studio-src-screens-agents-runnerconnections-tsx"
primary_target: "studio/src/screens/agents/RunnerConnections.tsx"
related_targets: ["studio/src/screens/agents/Runners.tsx", "studio/src/screens/agents.css", "studio/src/adapters/runner-connections.ts"]
---

# Official client status

Mode: Operate. Scoped harden → polish extension of the existing Runners surface.
Context: existing DESIGN.md and application components; no PRODUCT.md. No global
design changes, new identity, decorative media or new credential store.

THESIS: an explicit, cancellable check explains what the installed official client
actually reports without pretending that it enables execution or guarantees billing.

FORM: two lightweight rows with provider name, live textual status and help.
Existing type/color/spacing tokens; 44 px actions. Mobile actions wrap underneath.

STORY: unchecked → checking → subscription/API, actionable error or unknown.
Cancel restores usable controls. Old responses do not overwrite a newer check.
Subscription and API are not interchangeable; execution remains a separate workflow.

FINISH: real-component browser checks with synthetic local responses, desktop dark
and 390 px mobile light. Mobile conflict text wraps without horizontal overflow;
actions measure 44 px. Explicit cancellation works. TypeScript, adapter contracts
and production build pass. Detector once: only six incumbent side-border findings
outside the changed surface, no new-row findings. No independent agent review.

Implementation and honest scope boundaries: `docs/ui/runner-connections.md`.
