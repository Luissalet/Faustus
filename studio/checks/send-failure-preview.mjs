// Production UI against read-only local APIs; session creation always fails.
// No model calls, uploads or other mutations are forwarded.
import {createServer} from 'node:http';
let attempts=0;
createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1:7007');
  if(url.pathname==='/qa/attempts') {
    res.writeHead(200,{'Content-Type':'application/json'});return res.end(JSON.stringify({attempts}));
  }
  if(req.method==='POST' && url.pathname==='/api/session') {
    attempts++; req.resume();
    await new Promise(resolve=>setTimeout(resolve,1500));
    res.writeHead(503,{'Content-Type':'application/json'});return res.end(JSON.stringify({detail:'Synthetic session failure'}));
  }
  if(!['GET','HEAD'].includes(req.method)) {req.resume();res.writeHead(405);return res.end();}
  try {
    const headers={};
    if(req.headers.cookie) headers.cookie=req.headers.cookie;
    const response=await fetch('http://127.0.0.1:7000'+url.pathname+url.search,{headers,redirect:'manual'});
    res.writeHead(response.status,{'Content-Type':response.headers.get('content-type')||'text/plain',
      ...(response.headers.get('location')?{Location:response.headers.get('location')}:{})});
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch {res.writeHead(502);res.end('Local Faustus server unavailable');}
}).listen(7007,'127.0.0.1',()=>console.log('Read-only session failure QA: http://127.0.0.1:7007/studio'));
