// Synthetic, loopback-only QA. Never contacts providers or stores real credentials.
import {build} from 'esbuild';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname,sep} from 'node:path';
const bundle=await build({stdin:{contents:`
import React,{useState} from 'react';import {createRoot} from 'react-dom/client';
import {ProviderConnect} from './studio/src/screens/settings/ProviderConnect';
import {setLang} from './studio/src/i18n';import './studio/src/styles/index.css';import './studio/src/screens/settings.css';
setLang(new URLSearchParams(location.search).get('lang')==='es'?'es':'en',{persist:false});document.documentElement.dataset.theme='dark';
function App(){const [count,setCount]=useState(0);return <main className="fs-app"><div className="fixture"><p>Synthetic provider QA · use dummy keys only · no external requests</p><ProviderConnect onDone={()=>setCount(n=>n+1)}/><output aria-label="Connections saved">{count}</output></div></main>}
createRoot(document.getElementById('root')).render(<React.StrictMode><App/></React.StrictMode>);
`,resolveDir:process.cwd(),loader:'tsx',sourcefile:'provider-fixture.tsx'},bundle:true,platform:'browser',format:'esm',write:false,outfile:'/app.js',external:['/static/*'],logLevel:'silent'});
const assets=new Map(bundle.outputFiles.map(f=>[extname(f.path),f.contents]));
createServer(async(req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1');const json=(x,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(x));};
 if(url.pathname.startsWith('/static/fonts/')){const root=resolve('static/fonts'),file=resolve('.'+url.pathname);if(!file.startsWith(root+sep)){res.writeHead(404);return res.end();}try{return res.end(await readFile(file));}catch{res.writeHead(404);return res.end();}}
 if(url.pathname==='/app.js'||url.pathname==='/app.css'){const ext=extname(url.pathname);res.writeHead(200,{'Content-Type':ext==='.js'?'text/javascript':'text/css'});return res.end(assets.get(ext));}
 if(req.method==='POST'&&url.pathname.startsWith('/api/model-endpoints')){
  const chunks=[];for await(const chunk of req)chunks.push(chunk);
  const form=await new Request('http://127.0.0.1',{method:'POST',headers:{'Content-Type':req.headers['content-type']},body:Buffer.concat(chunks)}).formData();
  if(form.get('api_key')==='invalid')return json({detail:'Invalid synthetic credential'},401);
  if(url.pathname.endsWith('/test'))return json({online:true,private_connections_supported:form.get('api_key')!=='oldserver',models:form.get('api_key')==='empty'?[]:['example-model-alpha','example-model-beta-with-a-long-name-for-mobile-testing']});
  if(form.get('shared')!=='false'||!form.get('pinned_models'))return json({detail:'Expected private pinned configuration'},400);
  return json({id:'synthetic-connection'});
 }
 res.writeHead(200,{'Content-Type':'text/html'});res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"><style>body{overflow:auto}.fixture{width:min(880px,100%);padding:24px;margin:auto}.fixture p{color:var(--fs-text-2)}.fixture output{display:block;margin-block:24px}</style></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>');
}).listen(7004,'127.0.0.1',()=>console.log('Synthetic providers QA http://127.0.0.1:7004/?lang=es'));
