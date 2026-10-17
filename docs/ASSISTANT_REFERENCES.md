# Assistant references — 17 October 2026

The user-requested references were inspected before implementing assistant
configuration. No reference repository was cloned, executed, installed as an
operational dependency or copied into the runtime. The implementation uses
Faustus stores, owner identity, skills, harness and persistent scheduler.

| Reference and inspected revision | Source inspected | Native implementation |
| --- | --- | --- |
| [OpenDots](https://github.com/CopilotKit/OpenDots/tree/ab39f2508f3fd23a949837622456f3d61a108e9a) | `src/server/dot-agent.ts`, `headless.ts`, `runner.ts`, `store.ts`, `src/shared/scheduled-message.ts`; role configuration, executor reuse and scheduled-message identity. | Instructions enter the existing turn context. Scheduled work returns to the same canonical chat with source metadata. |
| [nanobot](https://github.com/HKUDS/nanobot/tree/faf1efaf09d2d69d1887b6ac6b0c6441e3ccccb2) | `nanobot/agent/skills.py`, `nanobot/cron/service.py`, `session_delivery.py`; skill disclosure and persisted schedules with origin-chat delivery. | Explicit skill bodies supplement normal retrieval. Stable native tasks retain owner/session and existing leases. |
| [Codync](https://github.com/leepokai/Codync/tree/c9944ea2c4c194512e4fcada12b9a5a0c22c011e) | `host/src/background.rs`, `host/src/routines/schedule.rs`, `docs/features/bot-collaboration.md`; executor reuse, calendar schedules and timezone retention. | Native scheduler/timezone/DST handling, normal harness and visible failures; failed tool runs are not replayed as text-only completions. |

URLs, pinned identities and hashes of inspected files are recorded in LocalAI
`research-inbox/dots-20261017/reference-inventory.json`,
`reference-files-read.json` and `opendots-code-read.json`. Source inspection is
distinct from our own functional verification; none of the reference applications
was benchmarked. OpenDots is the CopilotKit repository; no verified separate
“GrokBots” repository is claimed.

The product contract and limits are in [personal assistants](api/personal-assistants.md).
No general event planner, automatic goal decomposition, messaging bridge or
third-party integration parity is implied by adding scheduled proactive turns.
