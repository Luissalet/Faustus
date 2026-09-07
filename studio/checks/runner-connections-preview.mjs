// Loopback-only UI QA with synthetic account states. Never invokes a real CLI.
import {build} from 'esbuild';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname,sep} from 'node:path';
const bundle=await build({stdin:{contents:`
import React from 'react';import {createRoot} from 'react-dom/client';
import {RunnerConnections} from './studio/src/screens/agents/RunnerConnections';
import {setLang} from './studio/src/i18n';import './studio/src/styles/index.css';import './studio/src/screens/agents.css';
const query=new URLSearchParams(location.search);setLang(query.get('lang')==='en'?'en':'es',{persist:false});document.documentElement.dataset.theme=query.get('theme')==='light'?'light':'dark';
createRoot(document.getElementById('root')).render(<React.StrictMode><main className="fs-app"><div className="fixture"><p>Synthetic client connection QA · no real account calls</p><RunnerConnections/></div></main></React.StrictMode>);
`,resolveDir:process.cwd(),loader:'tsx',sourcefile:'runner-fixture.tsx'},bundle:true,platform:'browser',format:'esm',write:false,outfile:'/app.js',external:['/static/*'],logLevel:'silent'});
const assets=new Map(bundle.outputFiles.map(f=>[extname(f.path),f.contents]));
createServer(async(req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1');
 if(url.pathname.startsWith('/static/fonts/')){const root=resolve('static/fonts'),file=resolve('.'+url.pathname);if(!file.startsWith(root+sep)){res.writeHead(404);return res.end();}try{return res.end(await readFile(file));}catch{res.writeHead(404);return res.end();}}
 if(url.pathname==='/app.js'||url.pathname==='/app.css'){const ext=extname(url.pathname);res.writeHead(200,{'Content-Type':ext==='.js'?'text/javascript':'text/css'});return res.end(assets.get(ext));}
 if(url.pathname.startsWith('/api/agent-runners/')){
  const scenario=new URL(req.headers.referer||'http://127.0.0.1').searchParams.get('case');
  if(scenario==='old'||scenario==='denied'){res.writeHead(scenario==='old'?404:403,{'Content-Type':'application/json'});return res.end('{}');}
  if(scenario==='slow')await new Promise(resolve=>setTimeout(resolve,15000));
  const runner=url.pathname.split('/')[3];
  res.writeHead(200,{'Content-Type':'application/json'});return res.end(JSON.stringify({connection:{runner,installed:true,authenticated:true,
   state:scenario==='conflict'?'configuration_conflict':'connected',auth_method:runner==='codex'?'subscription':'api',external_runners_enabled:false,
   environment_override_names:scenario==='conflict'?['ANTHROPIC_API_KEY','CLAUDE_CODE_USE_FOUNDRY']:[]}}));
 }
 res.writeHead(200,{'Content-Type':'text/html'});res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"><style>body{overflow:auto}.fixture{width:min(960px,100%);padding:24px;margin:auto}.fixture>p{color:var(--fs-text-3);margin-block-end:24px}</style></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>');
}).listen(7005,'127.0.0.1',()=>console.log('Synthetic client connection QA http://127.0.0.1:7005/'));
