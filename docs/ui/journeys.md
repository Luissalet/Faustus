# Baseline journeys — current Faustus UI

Date: 04-09-2026. Branch: `feat/studio-ui`. Commit: measured against the
UI before any Studio changes.

Test harness: `ODYSSEUS_E2E=1 python -m pytest tests/e2e/test_studio_baseline.py -q`
(Playwright 1.62, headless Chromium, scripted fake model, temp data dir, no auth).

Screenshots: `docs/ui/baseline/` — three viewports per step (1400×900 desktop,
1024×768 tablet, 390×844 mobile).

## Journey 1: Create a project

**Goal:** create a new named project from scratch.

| Step | Action | Clicks | Screenshot |
|------|--------|--------|------------|
| 1 | Land on app — welcome screen shows brand, composer and sidebar | 0 | `01_landing` |
| 2 | Click **Projects** in sidebar | 1 | `02_projects_modal` |
| 3 | Click **New project** | 2 | `03_new_project_form` |
| 4 | Fill name, folder, instructions, agent trust | 2+ typing | `04_form_not_found`* |

**Total clicks to reach the form: 2.** The form itself (step 3) is rich: name,
conversation group, working folder with Browse, instructions textarea, and an
Agent section with trust toggles (Trusted folder, Trusted sub-agents, Review
mode, Checkpoints).

**Confusion points observed:**

- Projects opens as a **full-screen modal** over the chat. No URL change, no
  browser back. Closing the modal returns to wherever you were — but you can't
  bookmark or share the projects view.
- The "New project" form has a placeholder path `D:\Projects\my-project` which
  is a reasonable default but assumes Windows. No visual distinction between
  required (Name) and optional fields beyond a small "Optional"/"Required" label.
- The empty-state card ("Create your first project") is clear and friendly.
- *Step 4 screenshot is labelled `form_not_found` because the test could not
  locate `#project-name-input` — the actual input has placeholder text
  "My project" and no id matching the initial selector. The form was visible and
  photographed at step 3; the name-fill just didn't execute. This is a test
  fragility, not a UI bug.*

**Mobile (390×844):** sidebar is hidden behind hamburger. The user would need
hamburger → Projects → New project = 3 clicks minimum. The modal is not
visible at all in the mobile landing screenshot — the user sees only the
welcome screen and composer.

**Time (automated):** ~2 s from landing to projects modal open; ~1 s more to
form. In a real session with network and auth, add startup warm-up.

---

## Journey 2: Creative work

**Goal:** ask Faustus to create a visual asset.

| Step | Action | Clicks | Screenshot |
|------|--------|--------|------------|
| 1 | Chat ready — composer visible with Agent/Chat toggle | 0 | `01_chat_ready` |
| 2 | Type creative prompt | 0 + typing | `02_prompt_typed` |
| 3 | Send and receive text response | 1 (send) | `03_response_received` |
| 4 | Open Gallery from sidebar | 2 | `04_gallery_empty` |

**Total clicks: 2** (send + gallery). But the journey does not actually
complete: the fake model cannot generate images, so the response is text
advice. A real creative journey requires selecting a skill, setting
references, choosing a backend and waiting for generation — none of which
are reachable from the plain chat composer.

**Confusion points observed:**

- The response is a plain chat bubble. There is no "artifact card," no
  thumbnail, no "Vary / Correct / Use in…" action. The result is a
  message, not a reusable object.
- The Gallery (sidebar → Gallery) opens as yet another panel but is empty
  because no image was generated. There is no connection between the chat
  prompt and the gallery — the user must know that images live in a
  separate tool.
- No context bar visible: the user cannot see which project, skill,
  backend, or budget will be used before sending.
- The chat composer shows `T auto` and `Select model` — technical controls
  (temperature, model picker) are prominent; the creative intent is not.

**Mobile:** the composer is visible and functional but the sidebar tools
are hidden behind the hamburger. Reaching Gallery takes hamburger → scroll
to Tools → Gallery = 3+ clicks.

**Not testable with fake model:** skill selection, reference attachment with
roles, generation progress timeline, variant comparison, inpainting, recipe
inspection.

---

## Journey 3: Code task

**Goal:** ask the agent to fix a bug in a file, approve the change, see
tests pass.

| Step | Action | Clicks | Screenshot |
|------|--------|--------|------------|
| 1 | Chat ready with workspace attached | 0 | `01_chat_with_workspace` |
| 2 | Send agent message "Fix the add function in calc.py" | 1 (send) | `02_agent_working` |
| 3 | Approval card appears — review the proposed edit | 0 (wait) | `03_approval_card` |
| 4 | Click "Allow for this task" → checkpoint + verified card | 1 | `04_verified_result` |

**Total clicks: 2** (send + approve). This is the best-supported journey.

**What works well:**

- The approval card shows the exact tool call (`edit_file`), the old/new
  strings, the workspace path, and three clear options: Allow for task /
  Allow for session / Deny.
- After approval: checkpoint marker, `EDIT_FILE done`, "Verified: 1 file
  changed · 1 syntax-checked · tests passed" in green.
- Turn summary shows the edited file as a clickable chip that opens a
  diff viewer. "Restore to before this turn" and "Commit these changes"
  are both available.
- The verified card with `Claims vs. the diff: proved (1.00)` is
  auditable and honest.

**Confusion points observed:**

- The approval card uses raw JSON: `{"path": "calc.py", "old_string":
  "return a - b", "new_string": "return a + b"}`. A developer reads this
  fine; a non-technical user would not.
- "Allow for this task" vs. "Allow for this chat session" — the
  distinction is correct but the wording is dense. The explanatory text
  below each button helps.
- There is no ContextBar: before sending, the user does not see which
  workspace, model, or permissions are active. The `ws ×` chip in the
  composer footer is the only hint.
- `fake-coder` appears as the model name — in production this would be
  a real model name, which is fine.
- The progress popup ("Files edited in this chat (1)") overlaps the top
  of the chat on desktop.

**Mobile:** the approval card renders fully and is scrollable. The three
action buttons are tappable. The verified result is readable but the
turn summary and file chips require scrolling through the chat history.

**Time (automated):** ~15 s end-to-end including fake model latency and
test execution. In a real session, model inference and test execution
dominate.

---

## Journey 4: Find a result

**Goal:** find a specific piece of information produced in a previous chat.

| Step | Action | Clicks | Screenshot |
|------|--------|--------|------------|
| 1 | Result exists in a chat | 0 | `01_result_in_chat` |
| 2 | Navigate to a different chat | 1 | `02_different_chat` |
| 3 | Open search (Ctrl+K) | 1 (keyboard) | `03_search_open` |
| 4 | Type search query | 0 + typing | `04_search_results` |
| 5 | Open Library from sidebar | 1 | `05_library` |

**Total clicks: 3** (navigate away + search + library), plus the keyboard
shortcut. But the journey has significant friction.

**Confusion points observed:**

- The search overlay (Ctrl+K) blurs the entire UI and shows a single
  input "Search conversations…" — the background content is unreadable.
  The blur is aggressive.
- Search is text-only across conversations. There is no filter by
  project, by type (image/code/document), by date, or by skill.
- There is no concept of "result" or "artifact" as a findable object.
  The user searches for text within chat messages. If the result was an
  image, a generated file, or a structured output, it cannot be found
  through search.
- The Library (sidebar → Library) is a separate tool for documents. It
  does not include chat outputs, gallery images, or code diffs. A user
  looking for "that thing Faustus made" must know which subsystem
  produced it: Gallery for images, Library for documents, chat history
  for text, workspace for code.
- No breadcrumb, no "recent results" surface, no unified artifact view.

**Mobile:** Ctrl+K is not available; the user must use the sidebar Search
button (hamburger → Search). The search overlay fills the screen. The
Library requires hamburger → scroll → Library.

**Not testable with fake model:** finding a generated image, finding a
code diff from a previous session, searching by project scope.

---

## Journey 5: Resolve an approval

**Goal:** an agent run is blocked by an approval gate; the user reviews
and approves the proposed action.

| Step | Action | Clicks | Screenshot |
|------|--------|--------|------------|
| 1 | Chat ready with workspace | 0 | `01_chat_ready` |
| 2 | Agent triggers approval (after sending task) | 1 (send) | `02_approval_pending` |
| 3 | Review the approval detail | 0 (read) | `03_approval_detail` |
| 4 | Click "Allow for this task" → run completes | 1 | `04_approved_and_done` |

**Total clicks: 2** (send + approve).

**What works well:**

- The approval card is impossible to miss: it's inline in the chat with
  a highlighted border and clear call-to-action buttons.
- Three-tier approval (this task / this session / deny) is a good
  progressive-trust model.
- The "Turn summary" line at the bottom shows tool call count and that
  approval is pending — useful for at-a-glance status.

**Confusion points observed:**

- Same as Journey 3: the raw JSON and the dense "Effects: write_workspace"
  line are honest but not scannable. A diff preview would be clearer
  than a JSON blob.
- The approval card and the model response bubble (`Allow this task to
  continue?`) are visually similar but semantically different — the bubble
  is the model's output, the card is the system's gate. They could be
  confused.
- On mobile the approval card is well-rendered and readable, but the
  user must scroll past the model bubble to reach the action buttons.
- There is no notification mechanism for pending approvals beyond the
  chat itself. If the user has navigated away, they must come back to
  the right chat to find and resolve the approval.

**Mobile:** fully functional. The card fills the viewport width. Buttons
are tappable. The vertical stack (model bubble → approval card → turn
summary → composer) is long but scrollable.

---

## Summary table

| Journey | Clicks | Completable with fake model? | Biggest friction |
|---------|--------|------------------------------|------------------|
| Create project | 2–3 | Partially — form visible, submit untested | Projects is a modal, no URL |
| Creative work | 2 | No — text response only, no generation | No artifact card, no context bar, no skill selector |
| Code task | 2 | Yes — full approval → verify → diff cycle | Raw JSON in approval, no pre-send context |
| Find a result | 3+ keyboard | Partially — search works but results depend on index | No unified artifact search, subsystem silos |
| Resolve approval | 2 | Yes — full gate → approve → complete cycle | No off-chat notification for pending approvals |

## Cross-cutting observations

1. **The sidebar has 18+ entries** (New Chat, Search, Projects, Email,
   then 14 tools). A new user must scan all of them to find a capability.
   This is the "subsystem directory" problem the overhaul targets.

2. **Every tool opens as a modal or panel, not as a route.** Nothing
   except the chat has a URL. Browser back, bookmarks and link-sharing
   do not work for Projects, Gallery, Library, Tasks or Settings.

3. **No context bar.** Before sending, the user cannot see project,
   skill, references, backend, permissions or cost. The only visible
   controls are temperature and model.

4. **Results are messages, not objects.** Chat outputs disappear into the
   scroll. There is no artifact card, no lineage, no "Vary / Correct /
   Use in…" action. Finding a previous result means searching text.

5. **The approval flow is strong.** It is clear, inline, three-tiered
   and auditable. The checkpoint and verified card are well-designed.
   This is the part of the UI that most closely matches the overhaul
   vision.

6. **Mobile is functional but access is buried.** All tools require
   hamburger → scroll → click. The composer and chat work well; the
   rest of the app is behind a long sidebar.
