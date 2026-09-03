# Faustus workers — the skill for Fable / Claude / any coordinating model

The skill lives at **`integrations/claude/skills/faustus-workers/SKILL.md`**
(next to the `odysseus` skill, so the Claude Code bundle ships it too).

How to give it to a model:

- **Claude Code**: Settings → Integrations → Add a Claude Agent → the setup
  commands download `/api/claude/plugin.zip` into `~/.claude/` — the
  `faustus-workers` skill is inside, Claude Code auto-loads it.
- **Cowork / Claude Desktop**: copy the folder
  `integrations/claude/skills/faustus-workers/` into the client's skills
  folder (in Cowork: Settings → Skills → add from folder), or paste
  `SKILL.md` into a new skill.
- **Any other model**: the same text is served live by
  `GET /api/dispatch/guide` and by the MCP tool `workers_guide`.

The MCP server the skill talks to is `mcp_servers/workers_server.py`; the
full setup (token, model, MCP config) is in `website/fable-workers.md`.
