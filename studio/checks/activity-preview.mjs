// Local-only, synthetic browser fixture. No model calls or production writes.
// Run: node studio/checks/activity-preview.mjs
import { build } from 'esbuild';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

const result = await build({
  stdin: { contents: `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {BrowserRouter, Routes, Route, Link} from 'react-router';
    import {ActivityScreen} from './studio/src/screens/Activity';
    import {setLang} from './studio/src/i18n';
    import './studio/src/styles/index.css';
    setLang(new URLSearchParams(location.search).get('lang') === 'es' ? 'es' : 'en', {persist:false});
    document.documentElement.dataset.theme='dark';
    createRoot(document.getElementById('root')).render(<BrowserRouter><div className="fs-app">
      <aside className="fixture-controls">Synthetic QA only · No real tasks
        <button onClick={() => fetch('/fixture/offline', {method:'POST'})}>Disconnect fixture</button>
        <button onClick={() => fetch('/fixture/online', {method:'POST'})}>Reconnect fixture</button>
        <button onClick={() => fetch('/fixture/chat-offline', {method:'POST'})}>Disconnect conversations only</button>
      </aside>
      <main className="fs-main"><div className="fs-main__inner"><Routes><Route path="/studio" element={<main><h1>Conversation destination</h1><Link to="/activity">Return to activity</Link></main>}/>
        <Route path="*" element={<ActivityScreen/>}/></Routes></div></main>
    </div></BrowserRouter>);`, resolveDir: process.cwd(), sourcefile: 'activity-preview.tsx', loader: 'tsx' },
  bundle: true, format: 'esm', platform: 'browser', write: false,
  outfile: '/fixture/app.js', jsx: 'automatic', logLevel: 'silent', external: ['/static/*'],
});
const assets = new Map(result.outputFiles.map((file) => [extname(file.path), file.contents]));
let offline = false;
let chatOffline = false;
let stopped = false;
let workflowCancelled = false;
const started = Date.now() / 1000 - 83;
const sessions = [
  { id: 'working', name: 'Preparar referencias visuales para el vídeo', model: 'Qwen local' },
  { id: 'waiting', name: 'Revisar el proyecto antes de modificar archivos', model: 'Modelo local' },
  { id: 'queued', name: 'Resumen de documentos para la siguiente sesión', model: 'Qwen local' },
];
const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Faustus · Activity QA (synthetic)</title><link rel="stylesheet" href="/fixture.css"><style>.fixture-controls{display:flex;flex-wrap:wrap;gap:1rem;padding:1rem;color:var(--fs-text-2);background:var(--fs-surface-2)}.fixture-controls button{background:var(--fs-surface-1);color:inherit;padding:.4rem;border:1px solid var(--fs-border)}body{overflow:auto}.fs-screen{margin-inline:auto}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>`;
createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const json = (body, code=200) => { res.writeHead(code, {'Content-Type':'application/json'}); res.end(JSON.stringify(body)); };
  if (url.pathname === '/fixture/offline') { offline = true; return json({ok:true}); }
  if (url.pathname === '/fixture/online') { offline = false; chatOffline = false; return json({ok:true}); }
  if (url.pathname === '/fixture/chat-offline') { chatOffline = true; return json({ok:true}); }
  if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
    const ext = extname(url.pathname); res.writeHead(200, {'Content-Type':ext === '.js' ? 'text/javascript' : 'text/css'}); return res.end(assets.get(ext));
  }
  if (url.pathname.startsWith('/static/fonts/')) {
    const base = resolve('static/fonts'); const file = resolve(base, url.pathname.slice('/static/fonts/'.length));
    if (!file.startsWith(base + '/')) { /* Windows uses a different separator. */
      if (!file.startsWith(base + '\\')) return json({}, 404);
    }
    try { res.writeHead(200, {'Content-Type':'font/woff2'}); return res.end(await readFile(file)); } catch { return res.end(); }
  }
  if (url.pathname.startsWith('/api/')) {
    if (offline) return json({detail:'Synthetic offline mode'}, 503);
    if (chatOffline && url.pathname === '/api/chat/activity') return json({detail:'Synthetic conversations outage'}, 503);
    if (url.pathname === '/api/chat/activity') return json({
      running: stopped ? ['waiting','queued'] : ['working','waiting','queued'], awaiting_approval:['waiting'],
      queued:{queued:1}, runs:{working:'run-working',waiting:'run-waiting',queued:'run-queued'},
      details:Object.fromEntries(sessions.map((s) => [s.id, {
        run_id:'run-'+s.id, phase:s.id === 'working' ? 'tool' : s.id === 'waiting' ? 'waiting_approval' : 'queued',
        started_at:started, elapsed_s:Math.floor(Date.now()/1000-started), last_event_at:started+52,
        round:3, tool:'read_file', detail:s.id === 'working' ? 'referencias/guion-visual.md' : '',
      }])),
    });
    if (url.pathname === '/api/sessions') return json(sessions);
    if (url.pathname === '/api/chat/stop/working') {
      if (req.headers['x-odysseus-run-id'] !== 'run-working') return json({stopped:false}, 409);
      stopped = true; return json({stopped:true});
    }
    if (url.pathname.includes('/tasks/runs')) return json({runs:[{id:'done',task_name:'Exportar notas del proyecto',status:'completed',result:'Archivo listo.',started_at:new Date((started-300)*1000).toISOString(),finished_at:new Date((started-250)*1000).toISOString()}]});
    if (url.pathname === '/api/workflows/runs') return json({ok:true,runs:[{
      id:'workflow-qa', title:'Preparar vídeo y revisar su publicación', workflow_id:'video.review',
      status:workflowCancelled ? 'cancelled' : 'paused', reason:workflowCancelled ? 'Cancelled' : 'Waiting for review',
      started_at:new Date(started*1000).toISOString(), nodes:[
        {id:'prepare',title:'Preparar referencias',status:'completed',artifacts:[{id:'occ_qa_report',label:'Informe de referencias y decisiones para la producción audiovisual — revisión final.md'}]},
        {id:'review',title:'Revisar el resultado antes de continuar',status:'paused',approval_id:'review-qa',needs:['prepare'],reason:'Se necesita tu decisión; no se ha publicado nada.'},
        {id:'wait',title:'Esperar al momento elegido',status:'pending',needs:['review']},
      ],
    }]});
    if (url.pathname === '/api/workflows/runs/workflow-qa/cancel') { workflowCancelled = true; return json({ok:true,status:'cancelled'}); }
    if (url.pathname === '/api/workflows/runs/workflow-qa/resume/review') return json({ok:true,status:'paused'});
    return json({});
  }
  res.writeHead(200, {'Content-Type':'text/html'}); res.end(html);
}).listen(7002, '127.0.0.1', () => console.log('Synthetic activity preview: http://127.0.0.1:7002/activity'));
