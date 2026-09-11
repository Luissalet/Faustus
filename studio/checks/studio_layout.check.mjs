// CMP-01-layout (W2-A2, CONTRATO_CMP_W2.md) — three arrangements of the
// same work in the Studio: "conversation" (today), "document" (the active
// document from the side panel dominates, the conversation narrows to a
// side rail) and "review" (document + ReviewPane, W2-A1's CMP-01/02/03).
//
// Static source inspection, like studio/checks/topology.check.mjs and
// l86-source-control-panel.check.mjs: Studio.tsx is ~2700 lines of JSX
// wired to dozens of adapters (fetch, localStorage, router, lazy chunks) —
// not pure logic a bundled import can safely exercise in Node without a
// browser. This checks the source directly instead.
//
// Run by tests/test_cmp01b_layout_js.py, or by hand:
//   node studio/checks/studio_layout.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

let failed = 0;
const check = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const studio = read('studio/src/screens/Studio.tsx');
const css = read('studio/src/screens/studio.css');

// ── The layout state: a closed union, persisted per session, nowhere else ──
{
  check(/type StudioLayout = 'conversation' \| 'document' \| 'review'/.test(studio), 'StudioLayout must be exactly the three modes the contract names');
  check(studio.includes("const LAYOUT_KEY_PREFIX = 'faustus_studio_layout:'"), 'the localStorage key must be session-scoped (one slot per session id, like the draft/attachment slots)');
  check(/function readLayoutFor\(sessionId: string \| null\): StudioLayout/.test(studio), 'readLayoutFor must exist and return StudioLayout');
  check(/function writeLayoutFor\(sessionId: string \| null, layout: StudioLayout\): void/.test(studio), 'writeLayoutFor must exist');
  check(studio.includes("localStorage.getItem(layoutKeyFor(sessionId))"), 'the layout must be read from localStorage, not re-derived');
  // "conversation" (the default) never gets its own key — same convention
  // as the draft slot (`writeDraftFor`), so the store does not grow with
  // the common case.
  check(/if \(layout === 'conversation'\) localStorage\.removeItem\(layoutKeyFor\(sessionId\)\)/.test(studio), "writeLayoutFor must not persist a key for the 'conversation' default");
}

// ── Root data-layout, driven by state, not a second store ──
{
  check(studio.includes('data-layout={layout}'), 'the root .fs-studio must carry data-layout={layout} for studio.css to key off');
  check(/const \[layout, setLayoutState\] = useState<StudioLayout>\(\(\) => readLayoutFor\(sessionId\)\)/.test(studio), 'layout must be real component state read from the per-session store, not a prop drilled from elsewhere');
}

// ── Switching layout is presentation-only: no new session, document or
// draft, and no save call. The switcher is the ONLY place layout is set
// from the UI, so its body is checked directly. ──
{
  const start = studio.indexOf('const setLayout = useCallback(');
  check(start !== -1, 'setLayout must exist as the single place the UI changes layout');
  const end = studio.indexOf('[panelDispatch],\n  );', start);
  check(end !== -1, 'setLayout must close over panelDispatch only');
  const body = start !== -1 && end !== -1 ? studio.slice(start, end) : '';
  const forbidden = ['createSession(', 'createDoc(', 'ensureSession(', 'saveWorkspaceFile', '.save(', 'save(', 'setParams(', 'navigate(', 'uploadFiles('];
  for (const call of forbidden) {
    check(!body.includes(call), `setLayout must not call ${call} — a layout switch creates no session/document/draft and calls no save`);
  }
  check(body.includes('setLayoutState(next)'), 'setLayout must update the layout state');
  check(body.includes("panelDispatch({ type: 'open', tab: 'doc' })"), "setLayout may open the panel to the doc tab (the same reducer action the header's Source control chip already uses) but nothing more");
}

// ── The header switcher: three explicit, labelled, keyboard-reachable
// controls (role=radio in a radiogroup), not a cycling button with no
// visible state ──
{
  check(studio.includes(`role="radiogroup" aria-label={t('Layout')}`), 'the switcher must be a labelled radiogroup');
  for (const id of ['studio-layout-conversation', 'studio-layout-document', 'studio-layout-review']) {
    check(studio.includes(`data-testid="${id}"`), `missing the ${id} control`);
  }
  check(studio.includes("aria-checked={layout === 'conversation'}") && studio.includes("aria-checked={layout === 'document'}") && studio.includes("aria-checked={layout === 'review'}"), 'each control must reflect the current layout via aria-checked');
  check(studio.includes("<span className=\"fs-sr-only\">") , 'icon-only buttons must carry a screen-reader label (reusing the existing fs-sr-only utility, not a new one)');
}

// ── The shortcut: its own listener, not routed through adapters/settings.ts
// (this lot does not own that file — see CONTRATO_CMP_W2.md's ownership
// split) ──
{
  check(studio.includes("matchesCombo(e, 'ctrl+alt+l')"), 'Ctrl+Alt+L must cycle the layout');
  check(!studio.includes("toggle_layout: 'ctrl+alt+l'"), 'the combo must not be added to DEFAULT_KEYBINDS — settings.ts is out of this lot\'s scope');
}

// ── Review mode: ReviewPane is W2-A1's, imported lazily, never copied ──
{
  check(studio.includes("const ReviewPane = lazy(() => import('./documents/ReviewPane')"), 'ReviewPane must be lazy-imported from documents/ReviewPane (W2-A1\'s file)');
  check(studio.includes("layout === 'review' && panel.open"), 'the review column must only render in review layout, and only once the panel actually has something to show');
  check(studio.includes('<div className="fs-review"'), 'the review column needs its own grid-column element (see studio.css .fs-review)');
  const reviewPanePath = 'studio/src/screens/documents/ReviewPane.tsx';
  check(existsSync(path(reviewPanePath)), `${reviewPanePath} must exist (W2-A1's file, or this lot's coordination stub if W2-A1 has not landed yet)`);
  if (existsSync(path(reviewPanePath))) {
    const rp = read(reviewPanePath);
    check(/export function ReviewPane/.test(rp), 'ReviewPane must be a named export (Studio.tsx wraps it, it does not import a default)');
  }
}

// ── No duplicated state: layout never stores a document, a session id, or
// turns of its own — it is read together with `panel`/`turns` at render
// time only. ──
{
  check(!/type StudioLayout = \{[\s\S]{0,120}(doc|document|turns|session)/i.test(studio), 'StudioLayout must stay a plain string union, never grow a shadow copy of panel/session/turns state');
}

// ── studio.css: grid-template-columns and tokens, not fixed widths that
// ignore the existing panel resize (--fs-panel-width) ──
{
  check(css.includes('--fs-stage-col'), 'studio.css must define --fs-stage-col (the swappable conversation-column width)');
  check(css.includes('--fs-side-col'), 'studio.css must define --fs-side-col (the swappable document/panel-column width)');
  check(/\.fs-studio\[data-panel\]\[data-layout='document'\],\s*\n\s*\.fs-studio\[data-panel\]\[data-layout='review'\]\s*\{/.test(css), 'document and review must share the stage/side swap (one rule, not two divergent copies)');
  check(css.includes("grid-template-columns: 264px var(--fs-stage-col) var(--fs-side-col) minmax(280px, 360px)"), "review must add a fourth grid column for the review pane, sized with a token-bounded minmax, not a bare px value");
  check(css.includes('.fs-studio__layout-switch'), 'the header switcher needs its own CSS (a three-way variant of .fs-studio__seg, whose thumb math is fixed at 50%)');
  check(css.includes('.fs-review'), '.fs-review (the review column\'s own element) must be styled');
  // Scoped to the CSS this lot actually added — the repo-wide sweep
  // (tests/test_studio_guards.py) already covers pre-existing debt
  // elsewhere in the file; this is an early, lot-scoped signal only.
  const newBlock = css.slice(css.indexOf('CMP-01 (W2-A2): three layouts'));
  check(newBlock.length > 0, 'the CMP-01-layout CSS block must exist');
  check(!/transition\s*:\s*all/.test(newBlock), 'no transition: all in the new CSS');
  check(!/outline\s*:\s*none/.test(newBlock), 'no outline: none in the new CSS');
  check(!/\b\d+ms\b/.test(newBlock), 'no hardcoded duration in the new CSS — use var(--fs-duration-*)');
}

// ── i18n: the new strings this lot introduces have a Spanish row (the
// repo-wide gate is python3 scripts/i18n_es.py --check; this is the
// lot-scoped tripwire, same pattern as topology.check.mjs) ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  for (const key of ['Layout', 'Main conversation (Ctrl+Alt+L)', 'Main document (Ctrl+Alt+L)', 'Review (Ctrl+Alt+L)']) {
    check(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
  // 'Conversation', 'Document' and 'Review' already existed before this lot
  // (checked, not asserted here to avoid a false failure if another lot's
  // edit ever renames them) — reused rather than duplicated.
}

// ── This lot's own decision record exists ──
{
  check(existsSync(path('docs/adaptations/decisions/CMP-01-layout.md')), 'missing docs/adaptations/decisions/CMP-01-layout.md');
}

if (failed) {
  console.error(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('ok studio_layout');
