# Navigation by working context

Luis requested this reorganisation on 17 October 2026: the permanent Tools
inventory and flat settings list made capabilities difficult to find. The rail
now contains Home, Studio, Projects, Library, Automations, Activity and Hoards.
Settings and search remain in its footer. On mobile, Home, Studio, Projects and
Library share the bottom bar with More, which opens search and navigation.

Search is clickable and keyboard accessible. It initially offers tools for the
current work; typing searches every capability. Existing URLs, API operations,
MCP tools and persisted data remain valid. Creator remains capability dependent.

## Capability inventory

| Previous Tool | Working context and access |
| --- | --- |
| Connectors | Hoards in the rail; real connected Hoards and other MCP servers |
| WhatsApp | Home → Personal tools; `/whatsapp` remains valid |
| Processes | Activity → Run details |
| Creator | Library tools, when enabled |
| Notes | Home → Personal tools; `/notes` |
| Brain | Library tools; vault, links and graph retained |
| Source control | Project → Source control; chat Git panel and `/git` use the working project |
| Calendar | Home → Personal tools; `/calendar` and `/event` |
| Mail | Home → Personal tools; `/email` |
| Memory | Chat actions; personal memory management retained |
| Agents | Automations → Automation tools; `/workers`, `/experts` and delegation commands |
| Skills | Chat actions and `/skills` |
| Extraction | `/extract` and Chat actions open the existing schema workspace beside the conversation; Library tools also reaches it |
| Visual index | Library tools; existing image/page index retained |
| Research | `/research question` runs in the same conversation; `/research` arms the next message. Library holds reports and the existing queue/workbench |
| Compare | `/compare` and Chat actions open the model comparison workspace beside the conversation |
| Agora | Projects → Project tools and a project's own Agora; existing global rooms remain reachable |
| State Mirror | Activity → Run details |
| Deltas | Activity → Run details and Project details, with the project filter |
| Alternatives | Project details, with the project selected |
| Completion | Activity → Run details and Project details, with the project filter |
| Cookbook | Settings → Connections → Connections and models; search and `/cookbook` |
| Context | Chat actions and `/context`, retaining the project filter |
| Workflows | Automations → Automation tools |

Extraction reuses the same editor, schemas, validation, evidence and API. Its
Add to conversation action carries only displayed, accepted fields, including
their evidence and inference markers, plus schema validity, errors, missing
required fields, conflicts and extraction limits. It does not reinterpret a
dropped value as data. This is a chat integration, not a migration of records
or an unimplemented claim that another Hoard now owns the extraction engine.

An old `/source-control?project=…` bookmark opens that project's repositories.
Without a project, the old URL offers a project picker. The install-wide
repository overview remains available through an explicit secondary link.

## Settings

Five areas replace the permanent list of every subsystem. Only the selected
area's sections are shown; search finds sections across all areas, including
English and Spanish synonyms. Existing `?s=` links and browser back/forward
select the corresponding section. Local-installation account filtering and
backend authorisation retain their existing behaviour.

| Area | Existing sections |
| --- | --- |
| General | Appearance, device, voice, reminders, shortcuts, account/users where applicable |
| AI and models | Models, defaults, Sparks, local models, OpenRouter and model router |
| Connections | Integrations and web search |
| Agent behaviour | Agent settings, behaviour modes, repository defaults and tool configuration |
| System and diagnostics | System, health, security and effective configuration |

Conversation-level selectors share Conversation settings in the composer;
the current model, reasoning, message, attachments and send controls remain
at hand. Project navigation retains its six main tabs and places objectives,
requirements, memory, rules, activity and context together in Project details.

Guided tours remain accessible from Help and tours in search and the existing
slash commands. They no longer offer themselves over the current screen.

## Verification

`node studio/checks/navigation.check.mjs` checks complete Tool/settings coverage,
project-scoped links, command registration, capability filtering, a mounted
settings navigation and access from mobile and mouse. Existing command and
area checks, TypeScript and the production build cover the integration. The
task evidence records native MCP and browser checks on an isolated instance;
simulated-provider checks assess navigation and request handling, not model
quality.
