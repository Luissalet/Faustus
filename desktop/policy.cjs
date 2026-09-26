const {URL}=require('node:url');
function localNavigation(value,origin){try{const url=new URL(value);return url.origin===origin&&!url.username&&!url.password;}catch{return false;}}
function externalNavigation(value){try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)&&!url.username&&!url.password;}catch{return false;}}
// clipboard-sanitized-write is navigator.clipboard.writeText: the same as
// Ctrl+C, so the local page gets it without a prompt. clipboard-read, media
// and notifications still ask. Everything else is denied.
const PROMPTED=new Set(['media','notifications','clipboard-read']);
function permissionCheck(permission,requestingOrigin,origin,granted){
  if(!localNavigation(requestingOrigin,origin))return false;
  if(permission==='clipboard-sanitized-write')return true;
  return Boolean(granted&&granted.has(permission));
}
function permissionRequest(permission,pageUrl,origin){
  if(!localNavigation(pageUrl,origin))return 'deny';
  if(permission==='clipboard-sanitized-write')return 'allow';
  if(PROMPTED.has(permission))return 'prompt';
  return 'deny';
}
// faustus:// links (from another app, a notification or a Hoard) open a page
// of the local app. Only reading places are reachable and only the query keys
// that select something: never `send=1` (which would post a message) or a
// remote `image`. Anything else is refused, not rewritten.
const DEEP_LINK_PATH=/^\/(studio|library|home|research|documents|activity|projects?|agents|board|settings)(\/[A-Za-z0-9._~-]+)*$/;
const DEEP_LINK_QUERY=new Set(['s','draft','doc','panel','issue','img','album','tag','q','tab','id','project','type']);
function deepLinkPath(value){
  try{
    const url=new URL(String(value||''));
    if(url.protocol!=='faustus:'||url.username||url.password)return null;
    const parts=[url.hostname,...url.pathname.split('/')].filter(Boolean);
    if(parts.some(p=>p==='.'||p==='..'))return null;
    const path='/'+(parts.join('/')||'studio');
    if(!DEEP_LINK_PATH.test(path))return null;
    const query=new URLSearchParams();
    for(const [key,val] of url.searchParams)if(DEEP_LINK_QUERY.has(key)&&val.length<=4000)query.append(key,val);
    const qs=query.toString();
    return path+(qs?'?'+qs:'');
  }catch{return null;}
}
function findDeepLink(argv){
  for(const arg of argv||[])if(typeof arg==='string'&&arg.toLowerCase().startsWith('faustus:')){const path=deepLinkPath(arg);if(path)return path;}
  return null;
}
module.exports={localNavigation,externalNavigation,permissionCheck,permissionRequest,deepLinkPath,findDeepLink};
