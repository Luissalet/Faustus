// Isolated QA: actual screen/CSS + deterministic fixture transport; no Git or backend.
import {createRoot} from 'react-dom/client';
import {PluginMarketplace} from '../src/screens/connectors/PluginMarketplace';
import {setLang} from '../src/i18n';
import catalogueSource from '../../plugins/marketplace.json';
import '../src/styles/fonts.css';
import '../src/styles/tokens.css';
import '../src/styles/base.css';
import '../src/styles/components.css';
import '../src/screens/settings.css';
import '../src/screens/connectors/connectors.css';

document.body.classList.add('fs-app');
document.body.style.margin = '0';
const params = new URLSearchParams(location.search);
setLang(params.get('lang') === 'en' ? 'en' : 'es', {persist:false});
document.documentElement.dataset.theme = params.get('theme') === 'light' ? 'light' : 'dark';
const state = params.get('state') ?? 'missing';
// Every catalogue row's manifest, resolved from the row id (the bundler includes all
// plugins/*/plugin.json), so a newly listed Hoard needs no edit here.
const manifests = await Promise.all(catalogueSource.plugins.map(row =>
  import(`../../plugins/${row.id}/plugin.json`).then(module => module.default ?? module)));
export const previewManifestRegistry = new Map(manifests.map(manifest => [manifest.id, manifest]));
export const catalogue = {root:'D:/Fixture/plugins', plugins:catalogueSource.plugins.map(row => {
  const manifest = previewManifestRegistry.get(row.id);
  if (!manifest) throw new Error(`Preview manifest missing: ${row.id}`);
  return {
  ...row, name: manifest.name, purpose: manifest.purpose, source_kind:null as string|null,
  local_path:null as string|null, clone_path:`D:/Fixture/plugins/${row.id}`, state:'not_installed', can_install:true,
};})};
if (state === 'linked') {
  Object.assign(catalogue.plugins[0], {state:'linked',source_kind:'linked',local_path:'D:/Fixture/Existing Hoards/HoardLink',can_install:false});
  Object.assign(catalogue.plugins[1], {state:'cloned',source_kind:'cloned',local_path:'D:/Fixture/plugins/argus',can_install:false});
}
window.fetch = async (input, init) => {
  const url = String(input);
  if (url === '/api/plugin-marketplace' && (!init?.method || init.method === 'GET')) {
    if (state === 'failed') return Response.json({detail:params.get('lang') === 'en' ? 'The plugin catalogue could not be loaded. Try again.' : 'No se pudo cargar el catálogo de plugins. Vuelve a intentarlo.'},{status:502});
    return Response.json(catalogue);
  }
  const match = url.match(/^\/api\/plugin-marketplace\/([^/]+)\/(install|link|unlink)$/);
  if (!match || init?.method !== 'POST') throw new Error('Unexpected fixture request');
  const row = catalogue.plugins.find(item => item.id === decodeURIComponent(match[1]));
  if (!row) return Response.json({detail:'Fixture plugin unavailable'},{status:404});
  const action = match[2];
  if (action === 'install') Object.assign(row,{state:'cloned',source_kind:'cloned',local_path:row.clone_path,can_install:false});
  if (action === 'link') Object.assign(row,{state:'linked',source_kind:'linked',local_path:JSON.parse(String(init.body)).path,can_install:false});
  if (action === 'unlink') Object.assign(row,{state:'not_installed',source_kind:null,local_path:null,can_install:true});
  return Response.json({files_preserved:true});
};
export const previewRoot = createRoot(document.getElementById('fixture')!);
previewRoot.render(<main className="fs-set" style={{maxWidth:1040,padding:24,margin:'auto'}}>
  <PluginMarketplace onConfigure={(id,path) => {
    const message=document.getElementById('fixture-configured')!;
    message.textContent=`${id} · ${path}`;
  }} />
  <p id="fixture-configured" role="status" />
</main>);
