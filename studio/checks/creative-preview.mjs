// Isolated visual QA: real components, synthetic content, no accounts/providers.
import {build} from 'esbuild';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {extname,resolve,sep} from 'node:path';
const bundle=await build({stdin:{contents:`
import React,{useState} from 'react';import {createRoot} from 'react-dom/client';
import {DesktopBar} from './studio/src/shell/DesktopBar';import FrameSelection,{selectionImage,selectionPrompt} from './studio/src/screens/studio/FrameSelection';
import StyleLab from './studio/src/screens/studio/StyleLab';import LocalVideo from './studio/src/screens/studio/LocalVideo';
import {setLang} from './studio/src/i18n';import './studio/src/styles/index.css';import './studio/src/screens/studio.css';
const query=new URLSearchParams(location.search);setLang(query.get('lang')==='es'?'es':'en',{persist:false});document.documentElement.dataset.theme=query.get('theme')==='light'?'light':'dark';
window.faustusWindow={command:async()=>({maximized:false,fullscreen:false}),subscribe:()=>()=>{}};
const canvas=document.createElement('canvas');canvas.width=800;canvas.height=360;const context=canvas.getContext('2d');context.fillStyle='#28333b';context.fillRect(0,0,800,360);context.fillStyle='#e06c75';context.fillRect(60,140,200,70);context.font='24px sans-serif';context.fillStyle='#fff';context.fillText('Synthetic button',70,183);
const frame={src:canvas.toDataURL(),url:'https://example.invalid/qa',title:'Synthetic page',at:Date.now()};
function App(){const [draft,setDraft]=useState(''),[image,setImage]=useState('');return <><DesktopBar/><main className="fs-app fs-shell" style={{display:'block',overflow:'auto',padding:24}}><h1>Creative tools · isolated QA</h1><p>No provider calls or real project changes.</p><section style={{maxWidth:800,marginTop:24}}><FrameSelection frame={frame} onAdd={async value=>{const file=await selectionImage(value);setImage(URL.createObjectURL(file));setDraft(selectionPrompt(value));}}/><textarea aria-label="Draft" readOnly value={draft} style={{width:'100%',minHeight:100,marginTop:16}}/>{image&&<img src={image} alt="Annotated capture" style={{maxWidth:'100%'}}/>}</section><div style={{display:'flex',gap:8,marginTop:20}}><StyleLab model="synthetic-model" onSaved={()=>{}}/><LocalVideo/></div></main></>};createRoot(document.getElementById('studio-root')).render(<App/>);
`,resolveDir:process.cwd(),loader:'tsx'},bundle:true,platform:'browser',format:'esm',write:false,outfile:'/app.js',external:['/static/*'],logLevel:'silent'});
const assets=new Map(bundle.outputFiles.map(file=>[extname(file.path),file.contents]));
createServer(async(req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1');
 if(url.pathname.startsWith('/static/fonts/')){const root=resolve('static/fonts'),file=resolve('.'+url.pathname);if(file.startsWith(root+sep)){try{return res.end(await readFile(file));}catch{}}res.writeHead(404);return res.end();}
 if(['/app.js','/app.css'].includes(url.pathname)){res.setHeader('Content-Type',url.pathname.endsWith('.js')?'text/javascript':'text/css');return res.end(assets.get(extname(url.pathname)));}
 if(url.pathname.startsWith('/api/')){res.setHeader('Content-Type','application/json');if(url.pathname.endsWith('/capabilities'))return res.end(JSON.stringify({ffmpeg:true,whisper:false,system_voice:true,max_seconds:180}));res.writeHead(503);return res.end(JSON.stringify({detail:'QA fixture: no model connected'}));}
 res.setHeader('Content-Type','text/html');res.end('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"><div id="studio-root"></div><script type="module" src="/app.js"></script>');
}).listen(7006,'127.0.0.1',()=>console.log('Creative QA http://127.0.0.1:7006'));
