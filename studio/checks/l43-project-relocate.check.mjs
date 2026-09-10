// IDX-01 UI — Project.tsx's "The folder has moved…" action.
// relocateResultMessage() turns POST /api/projects/{id}/relocate's response
// into the one sentence the dialog's toast shows. QA-48 (per lote 43's
// instructions: "conserva memorias y relaciones — QA-48 por pantalla: check
// .mjs") is the acceptance scenario this screen action serves: the message
// must say identity/memories/relations followed the project, not the path,
// specifically when the OLD folder is already gone — that is the case a
// relocate is usually FOR, and it must read as informational, never as a
// warning that something was lost.
//
// Bundled with esbuild on the fly; run by hand:
//   node studio/checks/l43-project-relocate.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['studio/src/screens/Project.tsx'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  write: false,
  jsx: 'automatic',
  loader: { '.css': 'empty' },
});
const { relocateResultMessage } = await import(
  'data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64')
);

const plainMove = relocateResultMessage({
  project: { id: 'p1', workspace: '/mnt/new_disk/Faustus' },
  old_workspace: '/mnt/old_disk/Faustus',
  old_path_missing: false,
  marker_conflict: false,
});
assert.match(plainMove, /\/mnt\/new_disk\/Faustus/, 'names the new path');
assert.equal(/lost|gone/i.test(plainMove), false, 'a plain move never sounds like something went wrong');

// The QA-48 case: the old folder is already gone (the disk migration already
// happened) — this is informational, not an error, and must say identity
// survived rather than merely staying silent about the gap.
const migrated = relocateResultMessage({
  project: { id: 'p1', workspace: '/mnt/new_disk/Faustus' },
  old_workspace: '/mnt/old_disk/Faustus',
  old_path_missing: true,
  marker_conflict: false,
});
assert.match(migrated, /memories and relations/i);
assert.equal(/error|failed|lost/i.test(migrated), false, 'informational, not alarming');

const conflict = relocateResultMessage({
  project: { id: 'p1', workspace: '/mnt/shared/Folder' },
  old_workspace: '/mnt/old/Folder',
  old_path_missing: false,
  marker_conflict: true,
});
assert.match(conflict, /marker/i, 'a reused folder\'s marker conflict is surfaced, not silently overwritten');

// A response with no workspace at all (defensive: the field is optional on
// the wire type) must not throw or print "undefined".
const noWorkspace = relocateResultMessage({
  project: { id: 'p1' },
  old_workspace: '',
  old_path_missing: false,
  marker_conflict: false,
});
assert.equal(/undefined/.test(noWorkspace), false);

console.log('Project relocateResultMessage(): new-path text, QA-48 migrated-folder wording, marker conflict — all passed');
