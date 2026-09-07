---
version: 1
slug: "cookbook-ssh-trust"
primary_target: "studio/src/screens/cookbook/SshTrust.tsx"
related_targets: ["studio/src/screens/cookbook/Servers.tsx", "studio/src/screens/cookbook.css"]
---

# SSH identity verification

Mode: Operate. Close the missing trust-management path in existing server settings,
preserving DESIGN.md and the shared details/field/button/dialog patterns.

THESIS: a host fingerprint is evidence to compare through an independent channel,
not a suggestion to accept automatically. Separate host identity from login keys.

FORM: details section per saved remote, explicit inspection, offered/saved
fingerprints, manual matching input for new hosts and separate revoke confirmation.
Changed identities never expose a one-click replacement. Mutations are human-only.

FINISH: actual component with synthetic transport, 1920/390 px, Spanish new-host
flow and English changed-key flow. Checked disabled/non-matching state, matching
input, simulated success, refusal to replace and cancellation of revocation.
No real host or trust store touched. 44 px fields/actions, long fingerprints wrap.
No independent review. Contracts and limits: docs/ui/ssh-trust.md.
