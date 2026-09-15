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
module.exports={localNavigation,externalNavigation,permissionCheck,permissionRequest};
