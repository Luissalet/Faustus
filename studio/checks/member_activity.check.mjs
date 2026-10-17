import { build } from 'esbuild';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = resolve(import.meta.dirname, '../..');
const dir = mkdtempSync(join(tmpdir(), 'member-activity-'));
try {
  await build({ stdin: { resolveDir: root, contents: `
    import assert from 'node:assert/strict';
    import {createElement} from 'react';
    import {renderToStaticMarkup} from 'react-dom/server';
    import {MemberActivity, memberActivityRows, elapsed} from './studio/src/screens/group/MemberActivity';
    const now=Date.now()/1000;
    const group={participants:[{sessionId:'a',personaName:'GLM',route:{model:'glm'}},{sessionId:'b',personaName:'Qwen',route:{model:'qwen'}}]};
    const step=(id,member_id,status,activity={})=>({id,member_id,status,detail:{activity},text:'',error:''});
    const snapshot={group,turns:[],busy:true,steps:[step('a1','a','done',{started_at:now-300,finished_at:now-200}),step('b1','b','running',{phase:'thinking',started_at:now-180,last_event_at:now-120}),step('b2','b','queued')]};
    const rows=memberActivityRows(snapshot);
    assert.equal(rows[0].state,'done');
    assert.equal(rows[1].step.id,'b1','active work takes precedence over newer queued message');
    let html=renderToStaticMarkup(createElement(MemberActivity,{snapshot}));
    assert.match(html,/Turn finished/); assert.match(html,/Thinking/);
    assert.match(html,/Elapsed: 1m 40s/,'finished duration stays frozen');
    assert.match(html,/Last activity 2m 0s ago/); assert.match(html,/No recent activity; check the run/);
    assert.doesNotMatch(html,/stuck|Listening/);
    snapshot.steps=[step('a2','a','error'),step('b3','b','waiting_user')];
    html=renderToStaticMarkup(createElement(MemberActivity,{snapshot}));
    assert.match(html,/Failed/); assert.match(html,/Waiting for your answer/);
    snapshot.steps=[step('b4','b','running')];
    html=renderToStaticMarkup(createElement(MemberActivity,{snapshot}));
    assert.match(html,/No activity timestamp available/); assert.doesNotMatch(html,/NaN|Last activity/);
    html=renderToStaticMarkup(createElement(MemberActivity,{snapshot,connected:false}));
    assert.match(html,/Connection lost; status may have changed/);
    assert.equal(elapsed(-2),'0s'); assert.equal(elapsed(3661),'1h 1m');
    console.log('PASS: terminal/active/queued precedence, silent stream, frozen clock, error, question, legacy and disconnected states');
  ` }, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', loader: { '.css': 'empty' }, outfile: join(dir, 'check.cjs'), logLevel: 'silent' });
  await import(pathToFileURL(join(dir, 'check.cjs')).href);
} finally { rmSync(dir, { recursive: true, force: true }); }
