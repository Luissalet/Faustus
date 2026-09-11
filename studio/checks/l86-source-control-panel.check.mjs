// Lote 86 (OBJ-4, CONTRATO_GIT_4.md) — Studio: SourceControlPanel reusable,
// a "Repositories" section in Project.tsx (scoped to that project's linked
// folders, replacing the plain lote-81 button), a live compact panel inside
// the project's chat (same panel system Studio already uses for
// Browser/Document/File — `panelDispatch`, `panel-storage.ts`,
// `SidePanel.tsx`), a command-palette shortcut and the `git_policy` chip
// linking to it. None of this is pure logic a bundled import can exercise
// (it is JSX wiring across files, some of them large — Studio.tsx,
// Transcript.tsx), so, like `l65-source-wiring.check.mjs`, it is checked by
// source inspection instead of a DOM render.
//
// Run by tests/test_l86_source_control_panel_js.py, or by hand:
//   node studio/checks/l86-source-control-panel.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8');

// ── SourceControlPanel.tsx exists and exposes the exact contract props ──
{
  const p = 'studio/src/screens/source-control/SourceControlPanel.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.match(src, /export function SourceControlPanel/, 'must export SourceControlPanel');
  for (const prop of ['projectId?:', 'repoId?:', 'compact?:', 'workspace?:']) {
    assert.ok(src.includes(prop), `SourceControlPanelProps must declare ${prop}`);
  }
  // No duplicated logic: the panel calls the same adapters the old screen
  // did, not a re-implementation of stage/commit/push/sync.
  for (const call of ['listRepos(', 'getStatus(', 'stage as stageFiles', 'commit as commitRepo', 'sync as syncRepo']) {
    assert.ok(src.includes(call), `SourceControlPanel must reuse adapters/git.ts's ${call}`);
  }
  // The compact branch is genuinely condensed: no New repository/branch/
  // GitHub/policy dialogs, per CONTRATO_GIT_4.md point 3's exact list.
  const compactBranch = src.slice(src.indexOf('if (compact) {'), src.indexOf('return (\n    <div className="fs-sc" data-testid="source-control-panel">'));
  assert.ok(compactBranch.length > 200, 'the compact branch must exist and not be empty');
  for (const heavy of ['NewRepositoryDialog', 'RepoPolicyDialog', 'PublishToGithubDialog', 'CreateBranchDialog']) {
    assert.ok(!compactBranch.includes(heavy), `compact mode must not render ${heavy} — only branch/ahead-behind/changes/log/Fetch-Pull-Push-Sync`);
  }
  assert.ok(compactBranch.includes('BranchPopover'), 'compact mode must still offer a clickable branch (BranchPopover)');
  assert.ok(compactBranch.includes('ChangesPane'), 'compact mode must still offer stage/commit/push (ChangesPane)');
}

// ── SourceControl.tsx became a thin wrapper: no more state/handlers of its
// own, just the page chrome mounting the panel full width ──
{
  const src = read('studio/src/screens/SourceControl.tsx');
  assert.match(src, /<SourceControlPanel\s+projectId=\{projectId\}\s*\/>/, 'the screen must mount SourceControlPanel');
  for (const gone of ['useState', 'listRepos(', 'commitRepo(', 'stage as stageFiles']) {
    assert.ok(!src.includes(gone), `SourceControl.tsx must no longer contain its own ${gone} — that moved to SourceControlPanel.tsx`);
  }
}

// ── Project.tsx: a "Repositories" tab, scoped to this project, replacing
// the lote-81 button, with "New repository" defaulting into this project's
// own folders — and an "Open full screen" escape hatch ──
{
  const src = read('studio/src/screens/Project.tsx');
  assert.ok(src.includes("import { SourceControlPanel } from './source-control/SourceControlPanel';"), 'Project.tsx must import SourceControlPanel');
  assert.match(src, /\{\s*id:\s*'repos',\s*label:\s*'Repositories'/, "TABS must gain a 'repos' entry");
  assert.match(src, /<SourceControlPanel projectId=\{project\.id\}\s*\/>/, "the 'repos' tab must mount <SourceControlPanel projectId={project.id} />");
  assert.ok(!/icon=\{GitBranch\}\s*\n\s*label=\{t\('Source control'\)\}/.test(src), 'the old lote-81 "Source control" header button must be gone');
  assert.ok(src.includes('/source-control?project='), 'Project.tsx must still link to /source-control?project=<id> ("Open full screen")');
}

// New repository's parent-folder options are scoped to the project it opened from.
{
  const src = read('studio/src/screens/source-control/NewRepositoryDialog.tsx');
  assert.ok(/projectId\?:\s*string/.test(src), 'NewRepositoryDialog must accept an optional projectId prop');
  assert.ok(src.includes('f.project_id === projectId'), 'the folder list must be scoped to that project when given');
}

// ── panel.ts: a 'git' tab, and a UI-only ping on turn end / git_policy so
// the panel refreshes live without a dedicated prop (the panel's props are
// fixed to projectId/repoId/compact/workspace) ──
{
  const src = read('studio/src/screens/studio/panel.ts');
  assert.ok(/PanelTab\s*=[^;]*'git'/.test(src), "PanelTab must include 'git'");
  assert.ok(src.includes('pingGitRefresh'), 'panel.ts must ping the git-refresh signal');
  assert.ok(/action\.type===['"]turn-end['"]/.test(src) || src.includes("action.type==='turn-end'"), "the ping must fire on 'turn-end'");
  assert.ok(src.includes("action.event.type==='git_policy'"), 'the ping must also fire on a git_policy event');
}
{
  const src = read('studio/src/adapters/git.ts');
  assert.ok(src.includes('export const GIT_REFRESH_EVENT'), 'git.ts must export the GIT_REFRESH_EVENT signal name');
  assert.ok(src.includes('export function pingGitRefresh'), 'git.ts must export pingGitRefresh()');
}

// ── SidePanel.tsx: the git tab is registered in the same tab strip as
// Browser/Document/File, renders the compact panel, and is gated on
// having a workspace or project to look at ──
{
  const src = read('studio/src/screens/studio/SidePanel.tsx');
  assert.ok(src.includes("import { SourceControlPanel } from '../source-control/SourceControlPanel';"), 'SidePanel.tsx must import SourceControlPanel');
  assert.match(src, /\{\s*id:\s*'git',\s*label:\s*'Source control'/, "the TABS list must gain a 'git' entry");
  assert.ok(src.includes("tab.id!=='git'||Boolean(workspace||project)"), 'the git tab must only show with a workspace or project');
  assert.match(src, /<SourceControlPanel compact projectId=\{project\?\.id\} workspace=\{workspace\}\s*\/>/, 'the git tab must mount the compact panel');
}

// ── Studio.tsx: quick access from the composer/header bar (icon GitBranch),
// a ?panel=git deep link, and the Transcript wiring to open it. Both
// checked by source inspection since Studio.tsx is a large file this lot
// keeps its own edits in minimal, easy-to-audit slices of. ──
{
  const src = read('studio/src/screens/Studio.tsx');
  assert.ok(/icon=\{GitBranch\}[\s\S]{0,200}?panelDispatch\(\{ type: 'open', tab: 'git' \}\)/.test(src), 'Studio.tsx must offer a GitBranch icon button that opens the git panel tab');
  assert.ok(src.includes("value === 'git'") && src.includes("panelDispatch({ type: 'open', tab: 'git' })"), 'Studio.tsx must open the git tab from ?panel=git');
  assert.ok(src.includes('onOpenSourceControl={() => panelDispatch({ type: \'open\', tab: \'git\' })}'), 'Studio.tsx must wire onOpenSourceControl into <Transcript>');
}

// ── Transcript.tsx: the git_policy chip becomes a real, clickable button
// once onOpenSourceControl is given — not a plain non-interactive span ──
{
  const src = read('studio/src/screens/studio/Transcript.tsx');
  assert.ok(src.includes('onOpenSourceControl?: TranscriptProps[\'onOpenSourceControl\']') || src.includes('onOpenSourceControl?: () => void'), 'Transcript must declare onOpenSourceControl');
  const chipBlock = src.slice(src.indexOf("turn.gitPolicy.length > 0"), src.indexOf("turn.error && (()"));
  assert.ok(chipBlock.includes('onOpenSourceControl ?'), 'the git_policy chip must branch on onOpenSourceControl');
  assert.ok(chipBlock.includes("type=\"button\"") && chipBlock.includes('onClick={onOpenSourceControl}'), 'the clickable variant must be a real <button> wired to onOpenSourceControl');
}

// ── CommandPalette.tsx: a "Source control" shortcut that opens the live
// panel (?panel=git), distinct from the existing Tools entry that goes to
// the standalone full screen ──
{
  const src = read('studio/src/shell/CommandPalette.tsx');
  assert.ok(src.includes("go('/studio?panel=git')"), 'the command palette must offer a shortcut that opens ?panel=git');
}

console.log('ok l86-source-control-panel');
