const {URL}=require('node:url');
function localNavigation(value,origin){try{const url=new URL(value);return url.origin===origin&&!url.username&&!url.password;}catch{return false;}}
function externalNavigation(value){try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)&&!url.username&&!url.password;}catch{return false;}}
module.exports={localNavigation,externalNavigation};
