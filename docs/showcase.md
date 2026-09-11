# Showcase (CMP-14)

Three short, offline-reproducible walkthroughs of what changed in the
CMP-01..14 comparison pass, each pointing at the real tests that back it,
the setting that turns it on, and what it does NOT do yet. None of these is
an installer — see [Three separate things](#three-separate-things-do-not-confuse-them)
below before promising a user any of them will "just work everywhere."

`examples/showcase/` holds one tiny sample project used by all three
recorrido below — real files small enough to read in one sitting, marked as
demo data throughout (never presented as a real user's work).

## 1. Documento con revisión real

**What it shows:** a shared document session between the side panel and the
full editor (one `docId`, one draft, one selection, one undo/redo stack —
switching modes never re-writes an unchanged file), a suggestion applier
that refuses to guess when `find` matches more than once, and comments
anchored to a quote+context in the document with an explicit "orphan —
relocate by hand" state when the anchor no longer matches.

**Files:** `studio/src/lib/docSession.ts`, `studio/src/screens/documents/
Editor.tsx`, `studio/src/screens/documents/ReviewPane.tsx`,
`src/document_comments.py`, `src/document_links.py`,
`routes/document_comments_routes.py`, `routes/document_links_routes.py`.

**Try it (offline, no live model needed):** open `examples/showcase/
sample-project/README.md` as a Document, select a sentence, use "Sobre
esta selección…", then check `ReviewPane` for the comment thread it
created. Switch to the full editor and back — the draft and selection
survive; `Save` is not called (compare `data/` — nothing changes on disk).

**Tests:** `tests/test_cmp01_doc_session_js.py` (studio/checks), `tests/
test_adp06_document_links.py`, `docs/api/document_links.md`.

**Limits:** the visual/structured editor ADP-04 asks for is still a plain
`<textarea>` with a Markdown command bar — this journey is about SESSION
and REVIEW continuity, not a new document format.

## 2. Agente supervisado de principio a fin

**What it shows:** the Activity view separating `lifecycle` (queued/
running/waiting/finished/failed/cancelled) from `wait_cause` (approval/
question/gpu_queue/dependency/none) and `connection_health`
(live/stale/disconnected, with a dated signal) — so a run that is stuck
says WHY and offers the one useful next action (approve/answer/open/
retry/reconnect), instead of a flat "running" dot. List order freezes
while you are interacting with it.

**Files:** `src/attention.py`, `routes/attention_routes.py`,
`studio/src/adapters/attention.ts`, `studio/src/screens/Activity.tsx`,
`src/tool_approvals.py` (the exact-approval gate this journey surfaces,
never bypasses).

**Try it (offline):** run the eval harness fixtures
(`tests/eval/tasks.py` via `scripts/eval_run.py`) with a scripted model —
no live API key needed — and open Activity while a task is mid-run with a
pending approval; the card names the approval and the button that resolves
it, without opening the chat.

**Tests:** `tests/test_adp11_attention.py`, `tests/test_cmp05_attention.py`,
`tests/test_tool_approvals.py`, `docs/api/attention.md`.

**Limits:** own Faustus runs get structured events; an external runtime
(CMP-06 below) only ever gets a `heuristic`-certainty read — it is not
folded into `attention` as if it were the same confidence level.

## 3. Escritorio Windows semántico

**What it shows:** naming a control by identity (role/name/automation_id/
path) instead of a pixel — `desktop_snapshot` → `ref` → `desktop_act`
re-resolves the SAME control against a fresh read, refuses to guess when
several controls are equally plausible, and reports `delivery ∈
{delivered, not_delivered, unknown}` rather than a single boolean. This
lote (CMP-10) adds explicit channel selection: `desktop_act` only ever
uses the semantic (`native_a11y`) channel and refuses rather than silently
degrading to pixels; a would-be fallback is recorded visibly
(`channel_fallback: true`) and invalidates the session's refs.

**Files:** `src/desktop_semantics/` (`contracts.py`, `session.py`,
`channel.py`, `evidence.py`, `windows_uia.py`), `src/agent_tools/
desktop_semantic_tools.py`.

**Try it (offline, no Windows needed):** the whole acceptance suite runs
against `src/desktop_semantics/fake_backend.py`, an in-memory app with
duplicate controls, a window swap and a delayed response — see the test
files below for exact scenarios. **Physical verification on a real Windows
desktop is DECLARED PENDING** — nothing in this lote, or the one before
it, has run against real UIA; `windows_uia.py`'s own docstring says so.

**Tests:** `tests/test_adp08_desktop_semantics.py`, `tests/
test_cmp10_channel.py`, `docs/api/desktop_semantics.md`.

**Limits:** UIA-only (no other platform's native accessibility API is
wired yet); a control canvas/game draws by hand still needs the
coordinate-based `desktop_click`/`desktop_screenshot` tools this layer sits
next to, not replaces.

## Related, read-only: an external runtime (CMP-06)

Not a fourth demo (there is nothing to show without a real Herdr instance
to point at), but worth naming here since it shares the "supervised agent"
journey's vocabulary: `src/external_runtimes/herdr.py` is a READ-ONLY
client to an externally configured Herdr runtime — version negotiation,
a session/presence listing with `certainty ∈ {structured, heuristic}` and a
dated signal, and explicitly NEVER a channel that sends anything into
Herdr. **Sin investigación externa, por instrucción del encargo**: the
wire contract (`GET /version`, `GET /sessions`) is inferred strictly from
INFORME_COMPARATIVO_V2.md §3.5 and is **pending validation against a real
Herdr instance** — see `docs/api/external_runtimes.md`. Tests:
`tests/test_cmp06_herdr_adapter.py` (fakes only, no network).

## Three separate things (do not confuse them)

1. **Conectar un modelo existente** — point Faustus at an Ollama/LM
   Studio/OpenAI-compatible server you already run, or a cloud API key.
   Nothing here installs anything; see the [setup guide](../website/setup.md).
2. **Instalar un motor opcional** — ComfyUI for media, the Windows UIA
   backend's `pywinauto` dependency, an external runtime like Herdr — each
   is optional, each has its own install step, and `src/doctor.py` reports
   an uninstalled one as `absent` (a fact about the machine), never `fail`.
3. **Probar una demo** — the three walkthroughs above, against
   `examples/showcase/`, with no live model or real desktop required.

Faustus is not a universal installer for someone else's runtime: nothing
in this repo downloads, builds or manages a Herdr install, and the Windows
UIA backend is optional and degrades explicitly (`available()`) rather
than claiming universal desktop coverage.

## Doctor check

`src/doctor.py`'s `("docs", "showcase demo")` finding reports whether this
file and `examples/showcase/` are both present — `ok` when both exist,
`fail` naming whichever is missing. It never claims the WALKTHROUGHS
themselves were exercised (that is what the test files above are for) —
only that the demo materials a person would need are actually on disk.
