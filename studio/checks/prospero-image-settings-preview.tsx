// QA only: the actual Settings screen, with deterministic local responses.
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router';
import {SettingsScreen} from '../src/screens/Settings';
import {setLang} from '../src/i18n';
import '../src/styles/fonts.css';
import '../src/styles/tokens.css';
import '../src/styles/base.css';
import '../src/styles/components.css';
import '../src/screens/settings.css';

document.body.classList.add('fs-app');
document.body.style.margin = '0';
const params = new URLSearchParams(window.location.search);
setLang(params.get('lang') === 'en' ? 'en' : 'es', {persist:false});
document.documentElement.dataset.theme = params.get('theme') === 'light' ? 'light' : 'dark';
export const fixtureSettings: Record<string, unknown> = {
  image_gen_enabled: true,
  image_execution_backend: params.get('backend') === 'configured' || params.get('backend') === 'legacy' ? 'configured' : 'prospero',
  image_model: 'saved-image-model', image_quality: 'high',
  default_endpoint_id: 'synthetic-text', default_model: 'saved-chat-model',
  vision_enabled: true, vision_endpoint_id: 'synthetic-text', vision_model: 'saved-vision-model',
  local_temperature_default: 0.6, local_top_p_default: 0.8, local_top_k_default: 20,
  local_repeat_penalty_default: 1.05, local_min_p_default: 0,
};
export const fixtureRequests: {url:string;method:string;body:unknown;credentials?:RequestCredentials;contentType:string|null}[] = [];
export const unexpectedRequests: string[] = [];
const transport: typeof fetch = async (input, init) => {
  const url = String(input), method = init?.method ?? 'GET';
  const body = init?.body ? JSON.parse(String(init.body)) : null;
  fixtureRequests.push({url,method,body,credentials:init?.credentials,contentType:new Headers(init?.headers).get('Content-Type')});
  if (url === '/api/auth/settings') {
    if (method === 'POST') Object.assign(fixtureSettings, body);
    else if (method !== 'GET') throw new Error('Unexpected settings method');
    return Response.json({...fixtureSettings});
  }
  if (method !== 'GET') throw new Error(`Unexpected fixture mutation: ${url}`);
  if (url === '/api/auth/status') return Response.json({auth_enabled:false,is_admin:true,username:'synthetic-admin'});
  if (url === '/api/model-endpoints') return Response.json({endpoints:[{id:'synthetic-text',name:'Fixture text endpoint',base_url:'http://text.invalid/v1',is_enabled:true,models:['saved-chat-model','saved-vision-model'],online:false}]});
  if (url === '/api/vision/status') return Response.json({enabled:true,source:'configured',model:'saved-vision-model',endpoint_name:'Fixture text endpoint'});
  if (url === '/api/models/default/residency') return Response.json({enabled:false,loaded:false});
  if (url === '/api/doctor?verbose=true') return Response.json({ok:true,findings:[],counts:{},worst:'ok',checked_at:'synthetic'});
  unexpectedRequests.push(url);
  throw new Error(`Unexpected fixture request: ${url}`);
};
window.fetch = transport;
globalThis.fetch = transport;
export const previewRoot = createRoot(document.getElementById('fixture')!);
previewRoot.render(<MemoryRouter initialEntries={['/settings?s=defaults']}><SettingsScreen /></MemoryRouter>);
