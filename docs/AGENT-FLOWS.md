# Agent flows by app

Skills → Agent flows by app shows the Hub's catalogue of the written procedures
on each app's current skill shelf. Search by app or flow, read its trigger, complete
steps and expected outputs/checks, read the full skill, or open its shelf in the Hub.

`GET /api/skills/library/agent-flows?app=ID&q=WORDS` proxies the configured Hub;
`GET /api/skills/library/agent-flow-skill?app=ID&slug=SLUG` reads the source skill.
They use the saved Hub connection and return a retryable error when unavailable.
The catalogue includes empty shelves and never starts apps or runs flows.

The Hub derives flows from `When to Use`, `Procedure`, and `Outputs` or (when
absent) `Verification`. It returns version, source hash and missing-section status.
Changing an app's shelf changes the catalogue; Faustus keeps no duplicate map.
The original Markdown is preserved, including numbered steps and references.

The panel uses the Skills search and button styles. Each app has its icon and flow
count; expandable rows show readable names, skill descriptions and formatted steps.
Empty shelves are grouped at the end with links to configure them. Hub connection
errors follow the interface language. The panel supports light/dark and mobile.
