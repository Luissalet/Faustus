// Actual library components; synthetic pages; loopback only, no private data.
import {build} from 'esbuild';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve, extname, sep} from 'node:path';
const built = await build({stdin:{contents:`
import React,{useState} from 'react'; import {createRoot} from 'react-dom/client'; import {MemoryRouter} from 'react-router';
import {ImageGallery} from './studio/src/screens/library/Gallery'; import {DocumentsLibrary} from './studio/src/screens/library/Documents';
import {setLang} from './studio/src/i18n'; import './studio/src/styles/index.css'; import './studio/src/screens/library.css';
setLang(new URLSearchParams(location.search).get('lang')==='es'?'es':'en',{persist:false}); document.documentElement.dataset.theme='dark';
function App(){const [query,setQuery]=useState(''),[tab,setTab]=useState('images'),[notice,setNotice]=useState(''),[measurement,setMeasurement]=useState('');
const measure=()=>{const rows=[...document.querySelectorAll('.fs-lib__item,.fs-gal__card')];const skipped=rows.filter(row=>!row.firstElementChild?.checkVisibility({contentVisibilityAuto:true})).length;setMeasurement(rows.length+' rows; '+skipped+' offscreen subtrees skipped');};
return <MemoryRouter><main className="fs-app fs-screen"><h1>Library pagination QA</h1><p>Synthetic data · no model calls or real files</p>
<div className="fs-inline"><button onClick={()=>setTab('images')}>Images</button><button onClick={()=>setTab('docs')}>Documents</button>
<input className="fs-field" aria-label="Search fixtures" value={query} onChange={e=>setQuery(e.target.value)}/><button onClick={measure}>Measure rendered rows</button></div><output aria-label="Rendering measurement">{measurement}</output><p role="status">{notice}</p>
{tab==='images'?<ImageGallery query={query} say={setNotice}/>:<DocumentsLibrary query={query} say={setNotice}/>}</main></MemoryRouter>}
createRoot(document.getElementById('root')).render(<App/>);
`,resolveDir:process.cwd(),loader:'tsx'}, bundle:true, platform:'browser',format:'esm',write:false,outfile:'/app.js',external:['/static/*'],logLevel:'silent'});
const assets=new Map(built.outputFiles.map(f=>[extname(f.path),f.contents]));
const requests=[];
createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1');
  const json=data=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
  if(url.pathname==='/qa/requests')return json(requests);
  if(url.pathname.startsWith('/static/fonts/')){
    const base=resolve('static/fonts'),file=resolve('.'+url.pathname);
    if(file.startsWith(base+sep)){try{return res.end(await readFile(file));}catch{}}
    res.writeHead(404);return res.end();
  }
  if(url.pathname==='/app.js'||url.pathname==='/app.css'){
    res.writeHead(200,{'Content-Type':url.pathname.endsWith('.js')?'text/javascript':'text/css'});return res.end(assets.get(extname(url.pathname)));
  }
  if(url.pathname==='/tile.svg'){
    res.writeHead(200,{'Content-Type':'image/svg+xml'});
    return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="150"><rect width="200" height="150" fill="#22262d"/><text x="80" y="80" fill="#f2f1ed" font-size="24">QA</text></svg>');
  }
  if(url.pathname.endsWith('/library')){
    const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||48),search=url.searchParams.get('search')||'';
    requests.push({path:url.pathname,offset,search});
    const total=search==='filtered'?3:120;
    const rows=Array.from({length:Math.max(0,Math.min(limit,total-offset))},(_,i)=>({id:search+'-'+(offset+i),filename:'Image '+(offset+i),title:'Document '+(offset+i),url:'/tile.svg',width:200,height:150,language:'markdown',preview:'Synthetic document',caption:'Image '+(offset+i)}));
    setTimeout(()=>json({items:rows,documents:rows,total,tags:[],models:[],languages:{markdown:total},session_count:0}),search==='slow'?1000:100);
    return;
  }
  if(url.pathname==='/api/gallery/stats')return json({total_photos:120});
  if(url.pathname==='/api/gallery/albums')return json([]);
  if(url.pathname.startsWith('/api/')){res.writeHead(404);return res.end('{}');}
  res.writeHead(200,{'Content-Type':'text/html'});
  res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>');
}).listen(7006,'127.0.0.1',()=>console.log('Library QA: http://127.0.0.1:7006/'));
