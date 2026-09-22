<p align="center">
  <img src="assets/branding/faustus-wordmark.png" alt="Faustus" width="300">
</p>

<p align="center">A self-hosted AI workstation for local models, cloud providers and agent teams.</p>

<p align="center">
  <a href="README.es.md">Español</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-you-can-do">Capabilities</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="website/setup.md">Setup guide</a>
</p>

![Faustus Studio](assets/screens/studio.png)

## What Faustus is

Faustus brings chat, coding agents, research, writing, image and video workflows, voice, and project knowledge into one workspace. It is a personal [Odysseus](https://github.com/odysseus-dev/odysseus) fork, with a Python/FastAPI backend and a React/TypeScript interface.

Use local models through Ollama and compatible servers, or connect cloud APIs. Configure an orchestrator and its specialist agents inside a conversation. Keep work, sources, generated files and project context together, and see what is running rather than guessing whether a model has frozen.

The recurring idea is that the agent has to show its work: which context a turn was built from, which tool produced which evidence, which model answered and what it cost, which approval unlocked which action. Every feature below has a test that fails when that stops being true, and a section in [FAUSTUS.md](FAUSTUS.md) that explains why it exists.

Local-first means you choose where inference happens. Cloud APIs and authenticated official clients use their provider's billing or subscription quota; local inference uses your own hardware. Selecting a remote provider sends it the context needed for that request.

## Quick start

Install Docker and Docker Compose, then:

```bash
git clone https://github.com/Luissalet/Faustus.git
cd Faustus
cp .env.example .env
docker compose up -d --build
```

On PowerShell, use `Copy-Item .env.example .env` instead of `cp`.

Open **http://localhost:7000**. The initial administrator password is printed in `docker compose logs odysseus`. Compose includes the search and vector-store services; it does not download a language model for you.

1. Connect your model server in Settings or Cookbook. For Ollama running on the Docker host, configure its reachable address using the [setup guide](website/setup.md).
2. Create a project and attach the files, documents or sources it should know.
3. Start a conversation. Choose **Chat** for conversation or **Agent** for tools and actions.
4. Use **Activity** to follow running conversations, workflows, renders and approvals.
5. Add image/video engines, voice services or external agent clients when you need them.

Native installation, Windows/macOS instructions, GPU setup, HTTPS and environment configuration: [setup guide](website/setup.md). Preserve your `data/` directory and back it up before upgrades.

## What you can do

### Windows desktop and web launchers

Launchers live in this repository and resolve paths relative to the checkout:

| Launcher | Action |
| --- | --- |
| `Start-Faustus-Desktop.bat` | Open a native Electron window with theme-aware minimize, maximize/restore, full-screen and close controls. |
| `Start-Faustus.bat` | Start Faustus and open the web interface in your browser. |
| `Stop-Faustus.bat` | Stop the server managed by these launchers. |
| `Restart-Faustus.bat` | Restart that server and open the web interface. |

Python and Node.js are required for first-time setup. The launchers prepare missing dependencies, rebuild stale Studio assets and install the pinned desktop runtime when needed. `Start-Faustus.ps1 -NoBrowser` starts web mode without opening a tab; `-Port 7001` selects another port.

Closing the desktop window stops the backend **only if that window started it**. An already-running web server is reused and remains running when the window closes. Process ownership is verified; unrelated Python processes and external model servers are not stopped. Desktop authentication is separate from your browser, so sign in once in the window.

Scheduled work requires a running Faustus server and an awake computer. Use web mode to leave the backend running after closing browser tabs; closing an owning desktop window stops scheduling too. In Agent chat, request a recurrence in English or Spanish, specify a time and time zone, and manage the saved task under **Automations**. Recurring tasks support IANA time zones and daylight-saving changes; existing tasks without a zone retain their UTC behavior.

### Install Faustus on your phone

Studio installs as a standalone app (a PWA) straight from the browser — no app store, no build step. On Chromium browsers (desktop or Android), **Settings → This device** offers an "Install Faustus" button once the browser has offered to; on iOS/iPadOS Safari, which never offers natively, the same screen shows the manual step (Share → Add to Home Screen). Once installed it opens full-screen with its own icon, keeps the shell working offline, and can receive real OS notifications — turn finished, an approval waiting, a reminder due — even with the app closed, via the standard Web Push protocol (VAPID + `aes128gcm`, implemented directly on `cryptography`, no third-party messaging service). Reaching it away from your own network still needs the server exposed over HTTPS (a VPN or tunnel to it), which "This device" mentions but does not set up. Below 767 px wide the navigation becomes a five-tab bottom bar (Home, Studio, Calendar, Notes, Settings). [Mobile API](docs/api/mobile.md) · [mobile layout notes](docs/ui/pwa.md).

### Creative tools inside the conversation

- **Point-to-edit:** select a point in a captured browser frame and describe the change. An annotated screenshot and capture provenance are added to the draft, not sent automatically. The agent must inspect the current page and project; screenshot coordinates are not invented source-code mappings.
- **Project visual references:** save named `@references` as project context links, distinguish subject, style and composition, and explicitly attach them from the picker. Removing a reference link does not delete its gallery image.
- **Learn a style:** derive editable style rules from TXT/Markdown examples using the selected model, compare baseline and styled answers, then save and select a preset. Comparison makes two model calls using the selected connection.
- **Local video:** transcribe with an already-installed Whisper model, edit or manually translate timed segments, export SRT/VTT, and render narration using installed Windows English or Spanish voices. No cloud API or automatic model downloads. Inputs are bounded to 64 MB, 3 minutes and 1080p; FFmpeg/FFprobe are required. This is practical local narration, not voice cloning or lip-sync. Original audio is replaced in the narrated export.
- **Meeting notes:** record or upload an audio file and get back Markdown notes — summary, decisions, action items (owner/due date when stated), open questions and the full timestamped transcript — from a background job that chunks long recordings, transcribes each chunk with the configured STT engine, and runs one local-model pass to write the notes. If that model pass is unavailable, the transcript is still saved with a warning instead of being lost. Listed under Library → Meetings.

### Chat and a persistent workbench

- Switch between local and API models; connect OpenAI, Claude, Gemini and OpenRouter through guided API setup with connection testing. OpenRouter calls report their real cost, per-endpoint preferences (data collection, provider order, web search only when asked) are explicit, and a local-only privacy profile refuses outbound calls instead of downgrading silently.
- A switch in Settings → Default AI (mirrored in Settings → Local models) chooses whether the default chat model loads at startup and stays resident — flips live, no restart: on loads it now, off releases it right away (VRAM freed within seconds, never mid chat turn). Works for both local backends: kept resident in Ollama via `keep_alive`, or a managed llama.cpp `llama-server` started and exempted from its own idle-unload timer while the switch is on. Every model listing (Default AI, Local models, the chat picker) shows who actually serves it — Ollama, llama.cpp, or a remote API — detected once and shared everywhere.
- Constrained decoding on both local backends: when an internal pass asks for a JSON object, the schema goes on the wire as a grammar — `format` on native Ollama, `response_format` on llama-server — so the model cannot emit a token that breaks it. Forcing plain JSON is not enough: it returns valid JSON carrying a value outside the allowed set.
- When the engine that serves the default chat model is stopped, Settings → Local models says so above the engine list and offers a button that starts it, instead of letting the next chat fail with nothing to act on.
- A local model stays loaded between agent turns: the keep-alive refresh sent when a run ends (or pauses on an approval or a question) carries the runner's own context size, so Ollama never mistakes it for a reload and evicts the model. Long batch runs no longer pay a full model load on every turn.
- Reasoning that goes round in circles is cut short: when the model's thinking restates the same long sentence for the third time, the round is retried with the sampler pushed out of the loop instead of burning the whole reasoning budget.
- Paste screenshots directly with **Ctrl+V**, upload attachments and reference workspace files.
- Navigate long chats with the message rail: hover previews, click/drag to jump, or use arrows, Home/End and Page Up/Down. Browsing older messages pauses automatic stream following.
- Read generated Markdown beside the chat, edit it and save it with conflict detection. Unsaved drafts belong to their conversation and survive panel navigation. Three Studio layouts (conversation, document, review) share one document session, so a selection in the document becomes a context chip in the composer and a suggestion from the agent lands with its anchor, never on the first matching line.
- A model answer can include a chart: a fenced ` ```chart ` block with small JSON (bar/line/pie/area, series, an optional unit) renders inline as an SVG with axes, legend, tooltips and a "Show data" table — no charting library, and anything malformed just falls back to a plain code block ([spec](docs/ui/charts.md)).
- Keep files, generated outputs, sources, project context, agent activity and browser captures in a resizable side panel.
- Move to another conversation while the server keeps the current turn running. See queue position, current activity, tool use and permission requests; reconnect to the existing work. An approval card whose permission died with a restart says so instead of offering dead buttons.
- Every turn shows the strategy the agent chose (direct edit, plan then execute, research, specialised review, explore alternatives) and why; a turn worth repeating can be saved as a recipe with its real inputs.
- **See every model call a session made** — a "Model calls" panel shows the exact request sent (secrets redacted), the assembled response, tool calls, timings and errors for each call, and any recorded call can be re-sent unchanged to a different model to compare answers side by side.
- Search and navigate with **Ctrl+K**. Use English or Spanish, themes, density controls, adjustable text and reduced motion.
- Pick a **behaviour mode** — a named conversational stance (adversarial, socratic, terse, mentor, red team, outside observer, editor, or one you write yourself) that changes how Faustus argues, never what it is allowed to do; it sits after the task preset and strictly before the untrusted-content policy in every turn's prompt, agent mode and Incognito included ([Behaviour modes API](docs/api/behavior_modes.md)).
- Plug in your **own local apps as connectors** — a `/connectors` screen over the existing MCP manager with presets for Jubhunter's Hoard and Writer's Hoard, real states that keep "the app is off" apart from "the adapter failed its handshake", user-configured launch profiles (structured argv, no shell, readiness, idempotent), and a per-chat / per-project / per-scheduled-task connector allowlist that the tool dispatcher enforces rather than merely hides ([Connectors API](docs/api/connectors.md), [tool selection](docs/api/tool_selection.md), [candidature recipe](docs/api/candidature_recipe.md)).
- See **what is running because of Faustus and stop it** — a `/processes` control center: listening ports with the process behind them, the agent's shells and MCP servers, background jobs, launched profiles and watched apps (Cursor, ChatGPT, node…), each with its origin and a Stop that only a person can press (the agent token is refused, the pid's creation time is the proof, the OS and Faustus itself are never targets). The same screen starts with **Apps**: your own local projects as cards with their icon, live status, Start / Stop / Restart, a console tail, and Open in a desktop window of their own (a generic Electron shell with the app's icon and taskbar identity) — add, edit and remove them freely; apps without an MCP server become connectors through a generic REST-to-MCP adapter fed by their OpenAPI or a small manifest.
- **Review your mail and update your job hunt** — `review_candidature_mail` reads the last N days of mail (never marking anything read), finds the employer replies (rejections, interviews, offers), matches or creates the application in Jobhunter's Hoard, records the reply there (idempotent by message id) and puts each interview with a stated date on the calendar (one event per interview slot); ambiguous employers and undated interviews are reported for manual review, never guessed. One tool call, so a local 27B does it in one turn.
- **Ask for things that repeat, and see them on Home** — "what's the weather in my town tomorrow, every day", "a daily news briefing on X", "tell me when this shop has it back in stock", "summarise my mail each morning": built-in watcher actions (weather from Open-Meteo, page watch that reports only on change, news brief with sources, mail digest) run on the scheduler without needing a model to browse, and any automation can be pinned as a card on the Home screen with its latest result, a Refresh button and its next run.
- **Your WhatsApp, read and answered from here** — pair your own account by QR (WhatsApp Web multi-device protocol, a local Node bridge on the loopback with a token), then ask "what did people write me today", "summarise what Ana said", or "tell Ana I'll be ten minutes late" — every send is approved by you; voice notes arrive transcribed; a daily WhatsApp digest can be a Home card; Tools → WhatsApp is a real chat client (profile pictures, groups, older messages, replies with quotes, reactions, forward, edit, delete, ticks, typing, search, attachments, recorded voice notes, dictation) with an "Ask Faustus" panel that summarises, drafts a reply in your voice or translates — text only, never sends by itself.
- Sync **Google Calendar the way Google allows it** — OAuth2 ("Connect with Google", the same client as Gmail) and the Calendar API v3, incremental `syncToken` pulls, recurrences and cancellations, etag-guarded write-back; iCloud, Nextcloud and any other CalDAV server keep the password form. The Google OAuth client is set up from Settings — the wizard hands you the exact redirect URIs, takes the `client_secret.json` Google downloads, and checks it against Google, no `.env` edit or restart ([Google Calendar](docs/api/google_calendar.md), [guide](docs/api/google_oauth_setup.md)).
- **Drive Faustus from your own code** — `sdk/ts` is a zero-dependency TypeScript client (ESM + CJS, typed events generated from the versioned SSE catalogue in `docs/api/sse_events.json`): create a session, stream a turn, answer questions and in-turn approvals, cancel with the run's fencing token, and resume by cursor after a dropped connection without ever repeating a POST. API tokens get a `sessions` scope for exactly that surface (`docs/api/sdk_surface.md`); the A20 acceptance test installs the packed tarball into clean CJS and ESM projects and runs them against a real server with auth on.
- **Parity with the reference harness, measured, not claimed** — a public parity matrix and 36 acceptance recipes from an independent audit live in `docs/spec/paridad/`; `scripts/acceptance_run.py` runs the ones that have real tests and writes one evidence row per case (commit, config hash, model, outcome, cost status). All 36 are green as of 16 September 2026 — tool discovery, code execution, artifact offload, sandboxing, MCP OAuth, the embeddable component, OIDC login, service identity, scheduling, skill sources, loop-breaking, learning with rollback, benchmarking, migration and distribution lifecycle — with the remaining honest gaps (SDK registry publication, a clean-machine install, session sharing, a git-backed sandbox for skills) named in the matrix rather than hidden ([acceptance runner](docs/api/acceptance_runner.md)).
- **See how long everything actually took** — every tool result starts with wall time (this call, the turn so far, the clock) so a slow command is visible to the model, not just to you; the same numbers ride the `tool_output` event and the Studio's step card.
- **Reach — eyes on the internet across 9 channels** (web, YouTube, GitHub, Reddit, X, Hacker News, RSS, arXiv, Wikipedia), each with an ordered, real fallback chain and no dependency on a third-party CLI.
- **Code graph** answers architecture and call-tracing questions — trace a path between two symbols, see what a diff's callers are, get a repo's route/hotspot map — without reading whole files. **Impact queries** (`code_graph_impact`) walk incoming call edges from a symbol or from everything a diff touches, follow aliased imports, and hand back the affected test files with a ready `pytest` command instead of a guess.
- **Fan-out** races one prompt across N candidate models (local and paid mixed freely), each in its own isolated worktree, and scores the results automatically instead of asking you to eyeball N diffs.
- **PDF operations** transform an existing PDF — merge, split by arbitrary range, rotate, reorder, compress, watermark, rasterize, OCR — the one thing the rest of the PDF layer never covered.
- **PDF structure navigation** (`pdf_outline`/`pdf_find_section`/`pdf_read_section`) builds a table-of-contents tree from a PDF's own bookmarks (or heading detection, or fixed chunks as a last resort) so a long PDF can be jumped to by section instead of read page by page, with page numbers that always come from the tree, never guessed by the model.
- **Precise RAG locators for PDFs** — every chunk indexed from a PDF carries a stable, deterministic `p12#b3` (or `p12-13#b3` when it spans pages) locator pointing at its exact page and block, shown alongside citations in retrieved context so an answer can point at the exact spot it came from. Optional `rag_pii_redaction` setting (off by default) sanitises emails, phone numbers, IBANs, card numbers (Luhn-checked), Spanish DNI/NIE (control-letter checked) and IPv4 addresses out of chunk text before it's embedded/indexed, without touching the original uploaded file.
- **Agent personas** — 16 built-in identities (backend, security, writing, product, ML…) that slot into an agent's own AGENT.md without replacing its tool permissions.
- **Code Mode** (`run_code`) composes tool calls inside an isolated subprocess under the same policy gate as a direct call, with wall-time, call-count and output-size quotas.
- **Embed Faustus in your own page** with `<faustus-chat>` (`studio/embed/`), a Shadow-DOM Web Component with its own SDK client, no global router or CSS, and a token that never touches `localStorage` or the DOM.
- **Sign in with your corporate identity provider** — OIDC with Authorization Code + PKCE, one-shot state/nonce, and email-verified account mapping.
- **Creator** (flag `creator_enabled`, off by default) — a media production domain, 27 of its 43 work packages implemented and mounted: document model with revisions, library and lineage over the existing artifact catalogue, model identity vs. deployment, capability and parameter contracts, pure preflight, an adapter port with a generic contract test-harness, non-destructive ops on an exact rational clock, timeline and clocks, a deterministic FFmpeg render graph, aligned ASR transcription, a subtitle editor with its own SRT/VTT/ASS exporters, voice casting with consent records, a layered image canvas, storyboard and production plan (validated DAG), a music studio, a model explorer, expressive ComfyUI recipes, goals with typed completion criteria, physical resource inventory, safe plugin lifecycle, and preset evaluation/discovery. No engine is bundled: every adapter reports `available=False` honestly until one is installed.
- **A harness for long implementations** — what 24 real chats of a from-scratch project taught it (FAUSTUS.md §95): a turn with red tests, a failed UI smoke test or a contradicted changeset can never close as complete; the harness itself starts the project's web server and checks every page and asset (status *and* Content-Type, console errors with Playwright) instead of trusting the model to open a browser; missing dependencies are reported before the first command; a delegated worker that returns nothing is retried once and then reported, never silently accepted; the second whole-file rewrite of a large file is refused in favour of a targeted edit; tests excused as pre-existing become high-priority todos after three turns; and a big implementation plan attached to a message is parsed once into a per-task tracker (`plan_status`/`plan_task`/`plan_done`…) so the model sees the current task and its acceptance criteria, not 172 KB of plan again — and "keep implementing the plan" that ends with zero tool calls is rejected, not accepted as an answer.
- **UI accessibility and performance audit** — the headless smoke check that already catches console errors now also flags missing alt text, unnamed buttons/links, unlabelled inputs, missing `lang`/`title`, duplicate ids, heading skips and WCAG AA contrast, plus navigation timing, LCP and JS/CSS byte counts. Findings surface as quality warnings and only block the turn when `agent_ui_smoke_a11y_blocking` is turned on; performance findings never block.

### Side threads and context wires

What the model sees next is exactly what is wired to the conversation — a rule borrowed from [ThoughtDAG](https://github.com/chenxiachan/thoughtdag) and applied to ordinary linear chats. The human draws the graph; no agent creates wires on its own.

- **Explore separately:** select a passage in a reply and open a side thread that sees the conversation up to that turn plus the passage, then grows on its own. The original conversation does not change. The thread list indents side threads under their parent, and a thought map shows the tree.
- **Bring it back:** wire the side thread into its parent as an explicit reference block — latest exchange, or the whole thread — change its depth, withdraw it or remove it. A withdrawn wire keeps the thread.
- **Materials:** pin a document selection (or the live document) and free-form notes as context that stays until you withdraw it. The *Context wires* panel previews the next turn layer by layer with token counts, and says what it does not count.
- **Replay under a button:** every reply records which wires it was written against. When a side thread or document moves on, the affected replies say so and offer *Regenerate with the current version*; nothing regenerates by itself.
- **Condense by hand:** pick a range of settled turns and fold them into one summary row using the same summarizer as automatic compaction, on your own model. *Expand* restores the originals byte for byte; the turn in progress is never eligible.

![Context wires panel over a conversation](assets/screens/studio-wires.png)

### Coding, tools and agent teams

- Define an **orchestrator and its minions from the chat**, choosing models, roles and tool restrictions.
- Delegate to child conversations with file ownership, progress, cancellation and steering.
- Use **Council** for blind first rounds, critique and synthesis across multiple models, with a designated executor for actions.
- Use skills, MCP tools, filesystem tools, shell execution and browser capabilities under the configured permissions.
- Connect official **Codex CLI and Claude Code workers** through the existing runner/dispatch system. Both also have private text-model connections for the Faustus chat loop, with an explicit subscription/API choice. Codex chat uses an ephemeral, environment-less App Server thread; Faustus retains tool execution and approvals.
- Keep subscription and API routes explicit. Official-client authentication is checked before work; a subscription error does not silently fall back to paid API usage.
- Inspect tool evidence, syntax checks, project tests and review results. Shadow-git checkpoints support diffs and restoration without replacing the project's own repository.
- Reopen original change evidence from a turn's summary. Owner-scoped receipts survive restarts, distinguish saved evidence from verified success, and feed the project's State Mirror; incognito turns are excluded. [Evidence history](docs/design/durable-change-evidence.md).
- **Source control** without leaving the workspace: repositories discovered from linked folders, identities from `~/.ssh/config`, manual entries and `gh` accounts, create/clone/publish on GitHub, branches, merges and pushes from a panel with a draggable edge, and a per-repository policy the agent's `git_*` tools obey (the shell refuses `git commit`/`push` on their behalf). [Git API](docs/api/git.md).
- **Code Mode pauses for approval, then resumes.** A script call that hits a per-call desktop approval gate opens a question, waits on the person without charging that wait to the script's own wall time, and on approval runs that exact call once through a sealed single-action approval before continuing.
- **Typed choice decisions**: internal code can ask a local model to pick among a few fixed options from a single constrained forward pass (option log-probabilities) instead of generating and parsing text; used as an opt-in extra judge layer for claim verification.
- **Optional DevTools browser server** (off by default): a second built-in browser MCP server for performance traces, network/console inspection and page audits, alongside the existing navigation and control server.
- **Argument-level tool policy**, beyond the per-tool on/off switch: an admin rule constrains one dotted argument of a tool (exact name or glob, e.g. `mcp__github__*`) — a domain allowlist for `web_fetch`, a path prefix for a write, a pattern or length cap on any field — and either denies the call outright or routes it into the same human-approval flow other gated actions use. Manage rules, and try a tool+arguments pair against them, from Settings → Tools. [tool_arg_policy.py](src/tool_arg_policy.py).
- **Ask for tools in plain English or Spanish.** Every built-in tool carries example phrasings and domain synonyms, tools you name or hint at are always offered, and a vague message with a workspace never gets `bash` or `write` on the strength of an embedding.
- **Runs where your project runs.** On Windows the agent's shell is Git Bash, a `powershell` tool covers `.bat`/`.cmd` launchers, `winget`, services and registry, and the `python` tool uses the project's own `.venv` rather than Faustus's. The Docker sandbox is an option, not a gate: `agent_sandbox_mode` defaults to `auto`, which runs the command on the host whenever the container cannot serve it — always on native Windows, where a Linux image has none of that toolchain — and says so on the result; `strict` keeps the original rule of refusing rather than falling back. The turn's prompt states which machine it is on, built from the executor rather than by hand.
- **Alternatives:** try several approaches to the same change in isolated worktrees or frozen copies, compare them against the base with contested files called out, and apply one with a three-way merge — a hand edit you made meanwhile survives. Document alternatives work the same way on document versions. [Alternatives API](docs/api/alternatives.md).
- **Semantic desktop control** through the accessibility tree first and pixels only as an explicit, riskier fallback; taking the desktop back invalidates the agent's stale references. [Desktop semantics](docs/api/desktop_semantics.md).
- **Structural code search and rewrite**: AST patterns with metavariables (`$VAR`, `$$$ARGS`), previewed as a diff and applied by Faustus itself inside the workspace confinement, not by the underlying CLI.
- **Change risk score** before editing: a 0-100 score with its top reasons (callers, tests, churn, coupling, diff size) from the code graph and its co-change history, alongside the existing impact query.
- **An agent turn never ends in silence.** With real progress (tool calls that advance the task), the round budget auto-extends up to a configurable ceiling, with a visible progress line instead of a blank card; with no progress, or a genuinely empty round, the turn ends with a concrete question to the user instead. For long, repetitive tasks, a standing strategy block in the prompt (agent mode only) guides the model to break the work into units, save a progress cursor, and resume from it.

These checks provide evidence about supported actions; they are not a proof that every model statement is true. Tool access, automatic verification and external runners are configurable rather than implicitly enabled by choosing a model.

### Project knowledge and extended context

Project identity is stored independently of the sidebar folder name. An agent can attach a generated document or another supported source to its current project's context by reference.

The context engine retrieves and budgets relevant material from project sources, history and memory. It tracks provenance, conflicts and compact context capsules instead of trying to place an entire disk in a model's prompt. Persistent storage extends what can be retrieved, **not the model's native context window**.

The MCP tools and integrations blocks in the prompt are scoped the same way: only the tools already selected for the turn get a one-line reminder (they already carry a full native schema), every other connected server collapses to a single "N more tools — call lookup_tools" line, and the whole block is capped by `agent_mcp_prompt_budget_tokens` (0 turns it off). Tool discovery (`lookup_tools`, tool-RAG) reads the full tool index regardless, so nothing becomes unreachable — this only trims what repeats in the prompt text. A full listing every turn is still available behind `agent_mcp_prompt_full_listing`.

Use **Skip memory recall** in the composer to suppress automatic personal-memory retrieval for subsequent messages, including live context compilation. Existing chat history and project sources remain available. This does not disable memory tools or saving the chat; use Incognito for its separate privacy behavior.

In Agent mode, **Agent context** also lets you skip automatic skills and select a soft input-token budget before sending. These controls travel with the turn without changing global settings. The budget is an estimate, remains bounded by the selected model's context window, and is not a billing cap. Explicit tools and project instructions remain available when automatic skills are skipped.

A project also carries:

- a **board** of typed issues (`KEY-N` ids, priorities, labels, links, comments) with a kanban and a table view, Markdown import/export, and commit messages that close issues with *fixes*/*closes*/*cierra*/*arregla*; the agent gets `board_*` tools and a summary of open work in its prompt ([Board API](docs/api/board.md));
- a **versioned requirements spec**: every requirement records who proposed it, who — a human, always — accepted or rejected it, an immutable revision history, and typed evidence linking it to code, tests and runs, with a coverage matrix and a budgeted "context for this task" the agent can ask for ([Requirements API](docs/api/requirements.md));
- a **knowledge neighbourhood** that joins requirements, decisions, symbols, tests and runs into one typed graph with `declared`/`located`/`verified` relations and an honest `stale` flag, plus per-turn context receipts that say which files actually fed an answer ([Knowledge API](docs/api/knowledge.md)).

**Attention** answers "what needs me now?" across every conversation, run and workflow along four axes — lifecycle, cause of waiting, connection health and next action — so a pending approval, a GPU queue and a dropped connection are one list, not three tabs. [Attention API](docs/api/attention.md).

![Versioned requirements with evidence](assets/screens/requirements.png)

### Connected systems

| System | Purpose | Implementation |
| --- | --- | --- |
| Project Context Links | Bind chats and reusable sources to stable projects; add and resolve context by reference. | [project_context](src/project_context/) |
| Context Engine | Retrieve, rank, budget and explain the context assembled for a turn. | [context_engine](src/context_engine/) |
| Side threads & context wires | Branch a conversation from a passage, wire references, documents and notes explicitly, replay stale answers on request, condense by hand. | [side_threads.py](src/side_threads.py), [condense.py](src/condense.py) |
| Model Router | Choose a local or remote model per turn from calibration, measured speed and history; never escalates to a paid route without an explicit allowance; explains the fit of every candidate. | [model_router.py](src/model_router.py), [Router API](docs/api/model_router.md) |
| Provider policy & admission | Local-only and subscription-vs-API decisions made once and enforced on every outbound call; pooled admission for GPU, CPU-heavy and foreground work. | [provider_policy.py](src/provider_policy.py), [resource_admission.py](src/resource_admission.py) |
| Agent Profiles & Completion Modes | Reusable specialist profiles and task-dependent completion policies, within existing permissions; a lint that catches cycles and unreachable roles. | [agent_profiles](src/agent_profiles/), [agent_profile_lint.py](src/agent_profile_lint.py) |
| Strategy & recipes | An observable per-turn strategy that escalates only on observed failure, and reusable recipes built from real runs. | [strategy_policy.py](src/strategy_policy.py), [recipes.py](src/recipes.py) |
| Requirements | A versioned, human-accepted spec with typed evidence and coverage. | [requirements](src/requirements/) |
| Project Board | Typed issues, kanban, commit-closing links and agent tools. | [project_board.py](src/project_board.py) |
| Git panel | Repositories, identities, GitHub, branches and a per-repository policy the agent obeys. | [git_panel.py](src/git_panel.py) |
| Teach Mode | Capture demonstrations as reusable procedures, with review and execution controls. | [teach mode routes](routes/teach_mode_routes.py) |
| Immune System | Record incidents, evidence and corrective rules for recurring failures. | [immune_system](src/immune_system/) |
| Branching Futures & Alternatives | Explore alternative approaches in isolated branches, worktrees or document versions before choosing one. | [branching_futures](src/branching_futures/), [alternatives.py](src/alternatives.py) |
| Council | Organize multi-model discussion, critique, decisions and controlled execution. | [council](src/council/) |
| Workflows | Durable graphs with structural simulation, preflight, cost estimates with separate counts for activations, model calls and external operations, plan comparison and a canvas. | [workflows](src/workflows/), [workflow_cost_estimate.py](src/workflow_cost_estimate.py) |
| Attention | One list of what needs a person, across everything that is running. | [attention.py](src/attention.py) |
| Desktop semantics | Accessibility-tree desktop control with explicit channel choice and evidence. | [desktop_semantics](src/desktop_semantics/) |
| State Mirror | Keep timestamped observations with freshness and provenance; verify and restore materialized state from its committed journal. | [Recovery](docs/design/state-mirror-recovery.md) |
| Universal Delta Engine | Compare intended and observed changes across supported domains. | [delta_engine](src/delta_engine/) |
| Greedy Completion Engine | Discover and assess useful follow-up work according to the chosen mode, scope and budget. | [completion_engine](src/completion_engine/) |
| Jarvis voice | Voice interaction in English and Spanish, spoken replies and a reactive sphere. | [voice guide](docs/design/voice-jarvis.md) |

For implementation details, see [FAUSTUS.md](FAUSTUS.md). Current verification work is tracked in [PENDIENTES.md](PENDIENTES.md).

### Images, video and audio

- Mark attached images as subject/character, style or composition references. The selected roles become visible guidance in the sent message; they do not guarantee pixel-level conditioning. Gallery images can start a new reference chat while retaining the link to their original conversation.
- Open an attached image's thumbnail in the full editor, including masks and inpainting, without losing the chat draft. **Attach result to chat** saves the layer/mask draft and returns a PNG copy without sending a message. Inpainting requires a configured compatible image service.
- Plan and run approved **ComfyUI recipes** for images, reference editing and short video. Inspect required models before queueing work.
- The chat's **Media recipe** picker exposes installed recipes and their inputs, checks engine requirements without queueing a job, and adds an editable request to the draft. Reference-edit recipes support variations with strength and seed controls; engine-side image names are distinguished from chat attachments.
- With multiple configured engines, select by availability, queue and capacity, retaining the reason for the choice.
- Preserve recipe, version, seed, model licence, engine job and input digest with generated artifacts.
- Collect submitted renders in the server even with no chat open. Interrupted downloads remain retryable; completed outputs appear in Activity.
- Inspect image, audio and video properties before deciding how to process them.
- Convert and resize PNG/JPEG/WebP images and extract WAV/MP3 audio with scoped media tools, progress and cancellation.
- Download outputs through owner-scoped artifact links. Identical bytes can be shared physically without merging ownership or provenance.
- Expand **File provenance** beside Activity downloads to inspect size, type, SHA-256, partial/complete status and originating run, project or conversation.

ComfyUI is a separate service; model weights, custom nodes and their licences are not bundled. Use [media recipes](config/media_workflows/) and [workers documentation](website/fable-workers.md) to configure the engines.

### Research, documents and everyday work

- **Searches the web on its own for anything time-sensitive** — sports results, news, prices, software releases, who currently holds a role, weather, schedules — instead of claiming it has no live access or asking permission first; a lightweight bilingual (Spanish/English) detector adds the web tools to the turn and nudges the model to search before answering. Sources read during a turn show up as favicons in the collapsible activity rail and next to each citation, served same-origin through a cached, SSRF-guarded favicon proxy. [Freshness battery](docs/evals/freshness.md).
- **Explainable web ranking** — every search result carries a score and the reasons behind it: a bounded boost when several engines agree on a result, and a demotion (never a drop) for results sharing no content term with the query, shop-like results on a short non-purchase query, or stale results on a time-sensitive one.
- **Source type and extraction quality** on every web result and fetched page — official, docs, academic, reference, forum, shop and more from a URL heuristic, plus flags for thin, boilerplate, paywalled or JavaScript-required pages that feed the ranking nudge above.
- Research with source tracking, citation checks and report export.
- **Perspective-guided research planning** (opt-in): before searching, a short set of distinct perspectives on the topic (practitioner, sceptic, regulator...) each contribute a few questions, merged with the usual subquestions and capped to the normal query budget.
- **Blind review of a finished research report** (opt-in): a second model scores the report from the question, the stripped report and its cited sources only — never the writer's plan or identity — and records unsupported claims and the gap with the writer's own evidence grade.
- **Doc-claims checker**: extracts path, symbol, settings-key and API-route claims from Markdown docs, grounds them against the real workspace, and flags broken references and sections whose cited code drifted after the doc was written.
- **Project concepts**: a persistent, per-project graph of architecture concepts (feature/module/pattern/config/decision/component) the agent writes itself as it learns a codebase, queried semantically (`concepts_understand`) so understanding survives across sessions — with typed relations, staleness detection against the real workspace, optional automatic context injection, and a force-directed graph view in Studio (Context → Concepts).
- Retain labelled original-source excerpts when extraction fails, preserve the previous report if final generation is empty, and pass bounded evidence alongside summaries. Shared scheduled lookups recover from cancellation without stranding other tasks. [Diogenes adaptations](docs/design/diogenes-adaptations.md).
- Write and edit documents; export supported content to Markdown, text, HTML, PDF, DOCX or JSON.
- Search imported ChatGPT, Claude, LM Studio and Faustus history alongside local knowledge.
- Organize notes, tasks and calendars; connect email with IMAP/SMTP and calendars through CalDAV.
- Compare models, run expert reviews and inspect provenance and learned rules. Memory flags it when a new learned rule contradicts an older one — the older item is deprioritised and marked contradicted, and you resolve it (keep newer / keep older / keep both) from a Conflicts section on the Memory screen; nothing is ever silently dropped.
- **Grounding lint**: every active memory item with cited evidence is checked, deterministically, for specifics its evidence does not actually contain — numbers, dates, quoted strings and proper-noun-ish names are extracted and matched against the excerpt(s) the item cites. Flagged items and their unsupported details show in a Grounding section next to Conflicts on the Memory screen; items with no evidence at all are counted separately and never accused.
- Monitor local model memory, GPU placement, fit estimates, downloads and service health through Cookbook.
- Launch local servers with a verified configuration: model architecture (dense/MoE, multi-token prediction) is read from metadata rather than the model name, each launch option is checked against a per-implementation capability manifest before the command is built, and a launch receipt shows what was requested, what was applied and what the server confirmed. Every reply carries measured phases (queue, load, prefill, generation, tools) with their source under "Why did it take this long?" — a phase the engine does not report is shown as absent, never as zero.
- **Managed llama.cpp engines start themselves on the first chat call and unload when idle** (`engine_autostart`, `engine_idle_ttl_minutes`), so a stopped engine never has to be started by hand and an idle one stops holding the GPU. Engines that support it can turn on **MTP speculative decoding** (detected from the GGUF itself, never guessed from the file name), with a warning when the server's default parallel slots would cancel most of the speed gain.
- Benchmark a running configuration on your own machine from Cookbook → *Optimize for my machine*: explicit plan and budget before anything runs, one model at a time, deterministic quality checks (no LLM judge), and a comparator that only marks a profile as recommended when speed improves beyond the observed noise without losing quality.

### Durable workflows

Compose triggers, conditions, waits, human approvals, media steps and stored reports. Server-side continuation advances started workflows and wakes timed steps. Attempts have leases and conditional writes so late results do not overwrite a cancellation or a newer attempt.

Two more wait steps poll instead of sleeping to a fixed instant: `wait_until` re-checks the same condition language as `condition` on an interval until it is true, with a bounded timeout that either fails the node or completes it onto a declared timeout branch; `wait_for_event` watches an event source (a `file_change` scanner ships by default, and more can be wired in) and only completes after a settle window of quiet since the last matching event, with its own overall timeout and a capped list of collected events. Both persist their deadline and progress in the paused node's own state, so a process restart mid-wait resumes exactly where it left off, and a run stuck past its deadline after a crash is found and resolved by the same periodic continuation that already wakes a plain `wait`.

Generated reports can resolve text from the run's inputs or previous results and save it as an owned artifact. Workflow outputs inherit the verified project and conversation scope. Activity shows dependencies, reasons for waiting and links to output files.

Project workflows can run declared Python, JavaScript and Bash skill scripts in Docker, with a bounded source snapshot, permissions bound to the exact code and command, and collected output files. [Script skill setup](docs/design/workflow-script-skills.md) describes the contract and prerequisites. Process output is drained continuously and retained as bounded tails so verbose scripts cannot exhaust host memory.

Cancelling a running script workflow also stops its container. The worker checks the exact live attempt and lease, and cancellation during container setup prevents the script from starting. Partial external effects remain explicit and are not automatically retried.

Script credentials are encrypted per owner and bound explicitly by name and revision to approvals. Rotation invalidates pending execution; workflows never inherit unrelated provider keys. Human-only credential endpoints and configuration are described in the script skill guide.

A skill sleep pass (`src/skills_runtime/sleep_optimize.py`, run on demand from the skill's "Proposals" tab or via `POST /api/skills/{id}/sleep-pass`) mines recent sessions for turns where a skill was consulted (`manage_skills` activity recorded in that turn's `tool_events`), classifies the user's next reply with deterministic en/es phrase tables (negative: "no funciona", "still broken", undo/retry signals; positive: "perfecto", "works now", "gracias") plus any tool errors in that turn, and asks a local model for ONE proposed `SKILL.md` revision. Nothing is ever applied automatically: the proposal is validated (frontmatter/name preserved, size bounded, no removed Pitfalls/Verification section, a security-scan pass) and stored pending, and approving it goes through the same skill-governance promotion gate plus a version history that can be rolled back byte-for-byte.

Project objectives accept typed updates from agents without losing simultaneous changes. Human edits are protected against stale agent updates, including edits within the same second; damaged state files are preserved during recovery.

The [email delivery step](docs/design/workflow-email-delivery.md) sends text with optional HTML, CC/BCC and bounded workflow-content attachments through an owned SMTP account. Approval binds every recipient and the exact content, including attachment hashes. Approvals are consumed once at execution, approved waits resume automatically, and an uncertain external effect is not retried automatically. SMTP acceptance is recorded separately from inbox delivery.

Workflow nodes that perform external actions still need the appropriate configured capability and authorization. Cancelling stops subsequent work; it cannot undo an external action that already occurred.

The **Workflows** screen draws a definition as a layered graph with three explicit modes — design, structural simulation, authorized real execution — the same definition throughout. Simulation walks the graph round by round without side effects, leaves undecided conditions undecided rather than guessed, and never bypasses an AND dependency because a second path happened to be clean. Definitions import from this module's canonical export or an aigraphstudio-shaped graph, export back, and deep-link to a run.

![Workflow canvas in structural simulation](assets/screens/workflows.png)

### Design before code, and a turn that only costs what it needs

The `design_canvas` tool makes the model declare a design before it touches a
file: requirements, entities, the approach with the alternative it rejected and
why, the files it will change, operations, norms and safeguards. The result is
stored in the project concept graph as a decision whose refs are those files, so
when one of them disappears the staleness check finds the design that no longer
matches. It is written by the model the turn is already running on, not by
whatever the global default happens to be.

At the other end of the scale, a turn that is plainly small talk is answered
without the model’s reasoning and without the toolset attached. Measured on a
local 27B, same question and same engine: 7.7 s before, 3.7 s after. A
conversation that has already called a tool keeps everything, whatever its last
message says.

Memory refuses to file a snapshot of the workspace as a fact about you. A count
of the files in a folder stops being true the moment you add one, and a stored
fact that contradicts reality is worse than no fact at all.

### Adaptation history and demos

Three offline-checkable walkthroughs — a document with real review, a supervised agent end to end, semantic desktop control — are described in [docs/showcase.md](docs/showcase.md), each citing the files that implement it and the test that covers it, plus a small sample project under `examples/showcase/`. The adaptation backlog behind these features, audited row by row against the real code rather than asserted, lives in [docs/adaptations/](docs/adaptations/) (baseline classification and per-feature decision records).

## Voice

Jarvis provides a voice session with English/Spanish recognition, spoken replies, interruption controls and a reactive visual sphere. Configure the available transcription and speech services in the app; browser microphone permission is required. Installed voices and local speech engines determine available languages and playback.

Speech providers: browser (Web Speech API), system (installed Windows voices, offline), local (Kokoro TTS / faster-whisper STT), **Piper** (fully local, MIT-licensed neural TTS, including Spanish voices — install the engine and download a voice from Settings → Voice, no built-in model bundled; synthesis always runs in an isolated child process, never inside the server), **command** (run your own local TTS/STT executable via a placeholder template), or a configured API endpoint. See [docs/ui/voice.md](docs/ui/voice.md).

Voice input enters the same conversation and tool-permission flow as typed input. A voice session is not blanket authorization for filesystem, desktop or external actions.

Every transcript — voice input and meeting notes alike — passes through a deterministic cleanup pass (`src/stt_cleanup.py`) before it reaches the conversation or the notes: repeated segments collapse to one, known silence/hallucination phrases (English/Spanish) and bare music markers are dropped, and an in-segment word/phrase loop ("the the the the…") is collapsed, with timestamps kept coherent throughout.

Windows desktop app: a global "dictate anywhere" hotkey (off by default; Settings → Voice) transcribes into whatever app has focus — email, a terminal, another program — not just Studio's own composer. It captures the target window before recording, then delivers the text by simulated paste (clipboard is saved and restored afterward) or direct keystrokes, for apps that block paste (`src/dictation_paste.py`, `routes/dictation_routes.py`).

## Architecture

| Area | Code |
| --- | --- |
| HTTP application and persistence | [app.py](app.py), [routes](routes/), [core](core/) |
| Studio interface and state | [studio/src](studio/src/) |
| Model transport and official-client bridge | [llm_core.py](src/llm_core.py), [cli_model.py](src/cli_model.py), [runner_billing.py](src/runner_billing.py) |
| Worker dispatch and process supervision | [dispatch.py](src/dispatch.py), [external_worker.py](src/external_worker.py) |
| Durable workflows | [workflows](src/workflows/) |
| Media engines and continuation | [media_runs.py](src/media_runs.py), [media_scheduler.py](src/media_scheduler.py), [media_backends](src/media_backends/) |
| Artifacts, identity and migration | [artifact_store.py](src/artifact_store.py), [artifact_identity.py](src/artifact_identity.py), [artifact_migration.py](src/artifact_migration.py) |
| Regression and interface checks | [tests](tests/), [studio/checks](studio/checks/) |

The artifact store separates content-addressed bytes from owned output occurrences. Additive migration preserves historical identifiers and metadata; removal markers prevent deleted occurrences from being recreated by migration.

Internal names such as `odysseus`, `ODYSSEUS_*`, existing API paths and storage keys are retained for compatibility. The product name is Faustus.

## Development and verification

Use the project's configured Python environment and install the development dependencies described in [tests/README.md](tests/README.md).

```bash
python -m pytest
npm ci
npx tsc --noEmit
npm run build
python -m src.doctor
```

The suite includes owner isolation, persistence, concurrency, cancellation, migration, HTTP integration and headless interface checks. Hardware engines and account integrations also need tests in their configured environment; an HTTP fixture does not demonstrate model quality or a provider's account access.

Measured on 12 September 2026: 16,993 tests pass on Linux (`-m "not slow"`, about ten minutes on two workers) and the same tree passes on Windows with Docker Desktop running the container-backed cases; 295 modules under `src/`, 116 route modules, 149 Studio screens, 21 API documents under `docs/api/`, and 76 dated sections in `FAUSTUS.md` recording what each change was for and how it was verified. Interface checks run the real TypeScript through esbuild under Node rather than re-implementing it in Python.

`python scripts/acceptance_run.py` runs the 36 parity acceptance cases (A01-A36) and writes one evidence row per case; `python scripts/benchmark_matrix.py` runs a case × system × model benchmark matrix that reports a cell as `MISSING` rather than shrinking the denominator, and an unpriceable cost as `"unknown"`, never `0`.

Current verification results and remaining checks are listed in [PENDIENTES.md](PENDIENTES.md).

## Screens

<details>
<summary>Activity, agents and automations</summary>

Follow tasks and approvals in Activity.

![Activity: task status and results](assets/screens/activity.png)

Configure workers, runners and their verification settings.

![Agents: worker setup](assets/screens/agents.png)

Inspect and control recurring work.

![Automations: active and paused tasks](assets/screens/automations.png)

</details>

## Security and data

Context packet summaries include a memory-selection receipt: included entries, omissions and reasons, character usage and degradation. It describes the final Context Engine packet without querying memory again or adding a second selection pass.

Keep `AUTH_ENABLED=true` for any network-accessible deployment.
Keep `LOCALHOST_BYPASS=false` outside local development.

Keep authentication enabled. Do not expose unauthenticated model, ComfyUI or internal service ports publicly. Keep credentials and private `data/` files out of Git.

Bitwarden sessions are encrypted at rest and expire after one hour, shared by settings and agent tools. Old plaintext sessions are discarded on access and require unlocking again. Protect `data/.app_key`: encryption does not protect against a compromised host.

Approve tools and project instructions deliberately. Per-agent restrictions, owned sources, approval gates, sandbox settings and process supervision are separate controls. When a required sandbox is unavailable, the configured sandbox path must not silently run the command on the host.

A third-party MCP server or imported skill gets a static security pre-scan (no network, no LLM) before it can be trusted: remote-script execution, obfuscated payloads, credential access, exfiltration, persistence, destructive commands, dynamic code, open network listeners, and prompt-injection markers in tool descriptions or skill text. A critical finding quarantines it — same gate a permission escalation already uses — and approving requires an explicit override, shown next to the finding count, never a silent block.

A repeatable prompt-injection probe battery (`python -m src.security_probes`) checks the END-TO-END defences rather than a single function: 12 data-driven probes cover every untrusted vector (fetched pages, documents, email bodies, tool results, MCP tool descriptions, workspace files) and payload style (direct override, forged system messages, hidden HTML comments, zero-width/Unicode-tag smuggling, markdown exfiltration URLs, role-play jailbreaks, "call this tool" instructions, settings-disabling instructions, delayed multi-turn triggers, and forged sandbox-marker breakouts). A deterministic mode runs in CI with no model — a scripted "always-compromised" model that tries the exact malicious call the instant it has seen the injected content — and a live mode drives the same probes through a real local model and the real agent loop; results and a markdown summary are saved under `DATA_DIR/security_probes/` for regression tracking.

A trajectory gate (`src/trajectory_gate.py`, `GET`/`POST /api/agent-runs/{run_id}/gate`) checks a recorded agent run against declarative, CI-style assertions — max error rate, step/tool-call/token/duration ceilings, forbidden/required tools, ordering rules ("`write_file` needs a prior `read_file`", "tests must run after the last edit"), no repeated identical calls, a final answer required — so a regression in agent behaviour is caught mechanically instead of by eye. `python -m src.trajectory_gate --run <id> --spec spec.json` (or `--recent 20` to aggregate a pass rate per check) runs in CI with a non-zero exit code on failure; a compact "Gate a run" panel in Activity shows the same checks for any run id.

Doubt review (`src/doubt_review.py`, off by default: `agent_doubt_review`) asks a fresh-context, tool-less reviewer — no conversation history, only the task, a HIGH-risk file's `code_graph_risk` summary and the proposed diff — to find reasons a non-trivial edit is wrong before it lands, biased toward refutation rather than approval. Advisory by default: the edit applies and the verdict is appended to the tool result as "Second look..."; `agent_doubt_review_block` can instead refuse the write on a "concerns" verdict, reversible per call with `confirm_risky: true`. Cheap: a per-turn cache keeps the risk score and the review to one call per file/diff, and `agent_doubt_review_model` targets a small local model instead of spending the turn's main model on every edit.

See the [threat model](THREAT_MODEL.md), [security policy](SECURITY.md) and [setup security notes](website/setup.md#security-notes).

## Credits and licence

Faustus builds on [Odysseus](https://github.com/odysseus-dev/odysseus). See [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) for additional credits, [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance and [LICENSE](LICENSE) for **AGPL-3.0-or-later**.
