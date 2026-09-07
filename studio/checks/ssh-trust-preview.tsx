// Local QA only: actual component, deterministic transport, no SSH or trust writes.
import {createRoot} from 'react-dom/client';
import {SshTrust} from '../src/screens/cookbook/SshTrust';
import {setLang} from '../src/i18n';
import '../src/screens/cookbook.css';
const params = new URLSearchParams(location.search);
setLang(params.get('lang') === 'en' ? 'en' : 'es', {persist:false});
const fingerprint = 'SHA256:'+'a'.repeat(43);
let paired = params.get('state') === 'paired';
const changed = params.get('state') === 'changed';
window.fetch = async (input, init) => {
  const url = String(input);
  if (params.get('state') === 'error') return Response.json({detail:'fixture unavailable'}, {status:502});
  if (url.endsWith('/fingerprint')) return Response.json({host:'[gpu-box.example]:2222', state:changed?'changed':paired?'paired':'unpaired',
    paired:changed?['SHA256:'+'b'.repeat(43)]:paired?[fingerprint]:[],offered:[{type:'ssh-ed25519',fingerprint}]});
  if (url.endsWith('/pair')) {paired=true; return Response.json({state:'paired'});}
  if (url.endsWith('/unpair')) {paired=false; return Response.json({removed:1});}
  throw new Error('Unexpected fixture request');
};
createRoot(document.getElementById('fixture')!).render(<main className="fs-ck" style={{maxWidth:760,padding:24,margin:'auto'}}>
  <h1>SSH identity · isolated QA</h1><p>No real server or trust changes.</p>
  <SshTrust host="alice@gpu-box.example" port="2222" disabled={false} />
</main>);
