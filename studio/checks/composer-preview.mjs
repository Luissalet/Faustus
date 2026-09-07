// Synthetic browser QA of the real Composer. Uploads stay in memory; no LLM.
// node studio/checks/composer-preview.mjs <explicit local PNG fixture>
import {build} from 'esbuild';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname,sep} from 'node:path';
if (!process.argv[2]) throw new Error('Supply an explicit local PNG fixture');
const picture = await readFile(resolve(process.argv[2]));
const bundled = await build({stdin:{contents:`
import React,{useState,useRef} from 'react';
import {createRoot} from 'react-dom/client';
import {Composer} from './studio/src/screens/studio/Composer';
import {setLang} from './studio/src/i18n';
import './studio/src/styles/index.css';
import './studio/src/screens/studio.css';
setLang(new URLSearchParams(location.search).get('lang') === 'es' ? 'es' : 'en',{persist:false});
document.documentElement.dataset.theme='dark';
function Preview(){
 const [draft,setDraft]=useState(''),[attachments,setAttachments]=useState([]),[session,setSession]=useState('qa-a');
 const [knobs,setKnobs]=useState({mode:'chat',web:false,bash:false,plan:false,rag:false,incognito:false,research:false});
 const [notice,setNotice]=useState(''),[sent,setSent]=useState(''); const textarea=useRef(null);
 const copy = async()=>{try{await navigator.clipboard.write([new ClipboardItem({'image/png':new Blob([await(await fetch('/fixture/image')).arrayBuffer()],{type:'image/png'})})]);setNotice('Sample image copied. Paste it in Message with Ctrl+V.');textarea.current.focus();}catch(error){setNotice(error.message);}};
 return <div className="fs-app"><aside className="fixture-controls">Synthetic clipboard QA · No real uploads or model calls
 <button onClick={copy}>Copy sample image</button><button onClick={()=>fetch('/fixture/release',{method:'POST'})}>Finish uploads</button>
 <button onClick={()=>fetch('/fixture/fail',{method:'POST'})}>Fail next upload</button><button onClick={()=>{setSession(session==='qa-a'?'qa-b':'qa-a');setAttachments([]);}}>Switch session</button></aside>
 <main className="fs-main"><div className="fs-main__inner fixture-composer"><h1>Capturas en el chat</h1><p>Preview with the production composer. Session: {session}</p><p role="status">{notice}</p>
 <Composer draft={draft} setDraft={setDraft} busy={false} pending={false} knobs={knobs} setKnobs={setKnobs} workspace="" onPickWorkspace={()=>{}} onClearWorkspace={()=>{}} gen={{}} onClearGen={()=>{}} attachments={attachments} setAttachments={setAttachments} sessionId={session} onSend={text=>{setSent(JSON.stringify({text,attachments:attachments.map(a=>a.name)}));setDraft('');setAttachments([]);}} onStop={()=>{}} onNotice={setNotice} modelPicker={<span>Local model (synthetic)</span>} textareaRef={textarea}/>
 <output aria-label="Sent fixture message">{sent}</output></div></main></div>;
}createRoot(document.getElementById('root')).render(<React.StrictMode><Preview/></React.StrictMode>);
`,resolveDir:process.cwd(),sourcefile:'composer-preview.tsx',loader:'tsx'},bundle:true,format:'esm',platform:'browser',write:false,outfile:'/fixture/app.js',jsx:'automatic',logLevel:'silent',external:['/static/*']});
const assets = new Map(bundled.outputFiles.map(file=>[extname(file.path),file.contents]));
let failNext=false, serial=0;
const waiting=[],files=new Map();
const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Composer clipboard QA</title><link rel="stylesheet" href="/fixture.css"><style>.fixture-controls{display:flex;flex-wrap:wrap;gap:12px;padding:16px;color:var(--fs-text-2);background:var(--fs-surface-2)}.fixture-controls button{padding:8px;background:var(--fs-surface-1);color:inherit;border:1px solid var(--fs-border)}.fixture-composer{max-width:960px;margin:auto;display:flex;flex-direction:column;gap:24px;padding:40px 16px}.fixture-composer h1{font:var(--fs-display);margin:0}.fixture-composer p{color:var(--fs-text-2);margin:0}.fixture-composer output{white-space:pre-wrap;overflow-wrap:anywhere}body{overflow:auto}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>`;
createServer(async(req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1');
 const json=(body,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
 if(url.pathname==='/fixture/image'){res.writeHead(200,{'Content-Type':'image/png'});return res.end(picture);}
 if(url.pathname==='/fixture/fail'){failNext=true;return json({ok:true});}
 if(url.pathname==='/fixture/release'){for(const release of waiting.splice(0))release();return json({ok:true});}
 if(url.pathname==='/fixture.js'||url.pathname==='/fixture.css'){const ext=extname(url.pathname);res.writeHead(200,{'Content-Type':ext==='.js'?'text/javascript':'text/css'});return res.end(assets.get(ext));}
 if(url.pathname.startsWith('/static/fonts/')){const base=resolve('static/fonts'),path=resolve(base,url.pathname.slice(14));if(!path.startsWith(base+sep))return json({},404);try{res.writeHead(200,{'Content-Type':'font/woff2'});return res.end(await readFile(path));}catch{return res.end();}}
 if(url.pathname==='/api/upload'&&req.method==='POST'){
  if(failNext){failNext=false;req.resume();return json({detail:'Synthetic upload failure. Retry this attachment.'},503);}
  const chunks=[];let size=0;for await(const chunk of req){size+=chunk.length;if(size>8*1024*1024)return json({detail:'Fixture limit'},413);chunks.push(chunk);}
  const form=await new Request('http://127.0.0.1/upload',{method:'POST',headers:{'Content-Type':req.headers['content-type']},body:Buffer.concat(chunks)}).formData();
  const uploaded=[];for(const file of form.getAll('files')){const id='qa-'+(++serial),bytes=Buffer.from(await file.arrayBuffer());files.set(id,{bytes,mime:file.type});uploaded.push({id,name:file.name,mime:file.type,size:file.size});}
  await new Promise(resolve=>waiting.push(resolve));return json({files:uploaded});
 }
 if(url.pathname.startsWith('/api/upload/')){const file=files.get(url.pathname.slice(12));if(!file)return json({},404);res.writeHead(200,{'Content-Type':file.mime});return res.end(file.bytes);}
 res.writeHead(200,{'Content-Type':'text/html'});res.end(html);
}).listen(7003,'127.0.0.1',()=>console.log('Synthetic Composer QA: http://127.0.0.1:7003/?lang=es'));
