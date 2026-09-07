// Real components and synthetic state, loopback only, no provider calls.
import {build} from 'esbuild';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname,sep} from 'node:path';
const bundle=await build({stdin:{contents:`
import React,{useEffect,useState} from 'react';import {createRoot} from 'react-dom/client';import {MemoryRouter} from 'react-router';
import SidePanel from './studio/src/screens/studio/SidePanel';import ChatTeam from './studio/src/screens/studio/ChatTeam';import {useChatPanel} from './studio/src/screens/studio/useChatPanel';import {blankTurn,newWorker} from './studio/src/screens/studio/model';import {Rich} from './studio/src/screens/rich';
import {setLang} from './studio/src/i18n';import './studio/src/styles/index.css';import './studio/src/screens/studio.css';import './studio/src/screens/agents.css';
const query=new URLSearchParams(location.search);setLang(query.get('lang')==='en'?'en':'es',{persist:false});document.documentElement.dataset.theme=query.get('theme')==='light'?'light':'dark';
const routes=[{id:'local::local-model',model:'local-model',endpointId:'local',endpointName:'Local',endpointUrl:'http://localhost',kind:'openai'}];
const turn={...blankTurn('assistant',''),streaming:false,steps:[{id:'write',tool:'write_file',label:'Plan',state:'succeeded',round:1,command:'plan.md'}],sources:[{url:'https://example.com/reference',title:'Referencia del proyecto'}],workers:[{...newWorker('worker1','delegation1',Date.now()),status:'done',name:'Revisor',model:'local-model',finalText:'Revisión completada. Sin conflictos en los archivos asignados.',files:['plan.md'],durationS:42}]};
function App(){const [sid,setSid]=useState('chat-a'),[notice,setNotice]=useState('');const [state,dispatch]=useChatPanel(sid,false);
useEffect(()=>{if(!state.open)dispatch({type:'open',tab:'outputs'});},[sid]);
return <MemoryRouter><main className="fs-app"><div className="qa-layout" style={{'--fs-panel-width':state.width+'px'}}><section className="qa-chat"><p>QA local · datos de ejemplo · sin llamadas a modelos</p><h1>Faustus</h1><div><button onClick={()=>setSid('chat-a')}>Chat A</button><button onClick={()=>setSid('chat-b')}>Chat B</button><button onClick={()=>dispatch({type:'open'})}>Abrir panel</button></div><h2>{sid==='chat-a'?'Materiales de lanzamiento':'Otro proyecto'}</h2><Rich text={'He generado [el plan de trabajo](plan.md). Puedes leerlo y editarlo junto al chat.'} onOpenFile={path=>dispatch({type:'file',workspace:'D:/qa',path})}/><p role="status">{notice}</p><div className="qa-composer"><textarea aria-label="Mensaje" placeholder="Escribe a Faustus"/><ChatTeam sessionId={sid} routes={routes} coordinator={routes[0]} busy={false} ensureSession={async()=>sid} onEnabled={()=>{}}/></div></section>{state.open&&<SidePanel state={state} dispatch={dispatch} onNotice={setNotice} turns={[turn]} workspace="D:/qa" project={null} busy={false} onRerun={()=>setNotice('QA: nueva tarea solicitada')}/>}</div></main></MemoryRouter>}
createRoot(document.getElementById('root')).render(<React.StrictMode><App/></React.StrictMode>);
`,resolveDir:process.cwd(),loader:'tsx',sourcefile:'workbench-fixture.tsx'},bundle:true,platform:'browser',format:'esm',write:false,outfile:'/app.js',external:['/static/*'],logLevel:'silent'});
const assets=new Map(bundle.outputFiles.map(f=>[extname(f.path),f.contents]));
const teams=new Map();let content='# Plan de lanzamiento\n\n## Objetivo\n\nCrear una experiencia útil y versátil.\n\n- Revisar materiales\n- Preparar referencias\n- Comprobar resultados\n',revision=1;
createServer(async(req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1');
 const json=(status,data)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
 if(url.pathname.startsWith('/static/fonts/')){const root=resolve('static/fonts'),file=resolve('.'+url.pathname);if(!file.startsWith(root+sep)){res.writeHead(404);return res.end();}try{return res.end(await readFile(file));}catch{res.writeHead(404);return res.end();}}
 if(url.pathname==='/app.js'||url.pathname==='/app.css'){const ext=extname(url.pathname);res.writeHead(200,{'Content-Type':ext==='.js'?'text/javascript':'text/css'});return res.end(assets.get(ext));}
 if(url.pathname.startsWith('/api/')){
  let body='';for await(const part of req)body+=part;const input=body?JSON.parse(body):null;
  if(url.pathname.endsWith('/team')){const current=teams.get(url.pathname)||{revision:0,team:{enabled:false,members:[],max_parallel:2,max_rounds:12,timeout_s:600}};if(req.method==='PUT'){if(input.revision!==current.revision)return json(409,{detail:'Team changed elsewhere. Reload before saving.'});teams.set(url.pathname,{...input,revision:current.revision+1});}return json(200,teams.get(url.pathname)||current);}
  if(url.pathname==='/api/workspace/file'){if(req.method==='PUT'){if(input.revision!==String(revision))return json(409,{detail:'The file changed. Reload before saving.'});content=input.content;revision++;}return json(200,{path:'D:/qa/plan.md',rel:'plan.md',workspace:'D:/qa',size:content.length,text:content,lines:content.split('\n').length,binary:false,truncated:false,revision:String(revision)});}
  return json(404,{detail:'Synthetic endpoint not implemented'});
 }
 res.writeHead(200,{'Content-Type':'text/html'});res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"><style>body{overflow:hidden}.qa-layout{height:100dvh;display:grid;grid-template-columns:minmax(0,1fr) min(var(--fs-panel-width,520px),55vw)}.qa-chat{padding:32px;position:relative}.qa-chat h1{font-size:32px;margin:20px 0}.qa-chat h2{font-size:22px;margin:32px 0 20px}.qa-chat button{margin-right:12px;padding:8px;background:var(--fs-surface-2);color:var(--fs-text-1);border:1px solid var(--fs-border)}.qa-composer{position:absolute;inset:auto 24px 24px}.qa-composer>textarea{width:100%;height:80px;background:var(--fs-surface-2);color:var(--fs-text-1);padding:12px;margin-bottom:12px;border:1px solid var(--fs-border)}@media(max-width:700px){.qa-layout{display:block}.qa-chat{height:100dvh;padding:16px}}</style></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>');
}).listen(7005,'127.0.0.1',()=>console.log('Workbench QA http://127.0.0.1:7005/'));
