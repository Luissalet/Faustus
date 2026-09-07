import {FileText,Image,Paperclip} from 'lucide-react';
import {attachmentUrl} from '../../adapters/composer';
import type {Project} from '../../adapters/projects';
import type {Turn} from './model';
import {fileKey,type PanelState,type PanelAction} from './panel';
import {t} from '../../i18n';

export function outputFiles(turns:Turn[]):string[] {
  const paths=new Set<string>();
  for(const turn of turns)for(const step of turn.steps){
    if(step.diff?.file)paths.add(step.diff.file);
    if(/^(write_file|edit_file|create_file|transform_media)$/.test(step.tool)){
      for(const raw of [step.command,step.output]){try{const data=JSON.parse(raw||'');if(typeof data.path==='string')paths.add(data.path);}catch{/* some tools use line headers */}}
      if(step.command&&!step.command.trim().startsWith('{')&&step.tool!=='transform_media') paths.add(step.command.split('\n')[0].trim());
    }
  }
  return [...paths].filter(p=>p&&p.length<4096);
}
const external=(url:string)=>/^https?:\/\//i.test(url);
export function WorkbenchResources({kind,state,turns,workspace,project,dispatch}:{kind:'outputs'|'sources';state:PanelState;turns:Turn[];workspace:string;project:Project|null;dispatch:(a:PanelAction)=>void}) {
  const docs=new Map(state.documents.map(d=>[d.id!,d]));
  for(const turn of turns)for(const step of turn.steps)if(step.docId&&!docs.has(step.docId)) docs.set(step.docId,{id:step.docId,title:t('Document'),language:'',content:'',version:0,streaming:false,suggestions:[]});
  const files=[...new Map([...outputFiles(turns).map(path=>({workspace,path})),...state.files].map(f=>[fileKey(f),f])).values()];
  const images=[...new Set(turns.flatMap(turn=>turn.images))];
  const sources=new Map(turns.flatMap(turn=>turn.sources).filter(s=>external(s.url)).map(s=>[s.url,s]));
  const attachments=new Map(turns.flatMap(turn=>turn.attachments).map(a=>[a.id,a]));
  return <div className="fs-panel__body fs-workbench-resources">
    {kind==='outputs'?<><h3>{t('Results')}</h3><p>{t('Files and documents produced in this conversation. Open one to read or edit it alongside the chat.')}</p>
      {!docs.size&&!files.length&&!images.length&&<p>{t('No results yet. Generated work will appear here.')}</p>}
      {[...docs.values()].map(d=><button className="fs-workbench-item" key={d.id} onClick={()=>dispatch({type:'doc',doc:d})}><FileText size={16}/><span>{d.title}<small>{t('Document')} · v{d.version||'…'}</small></span></button>)}
      {files.map(file=><button className="fs-workbench-item" key={fileKey(file)} disabled={!file.workspace} onClick={()=>dispatch({type:'file',...file})}><FileText size={16}/><span>{file.path}<small>{file.workspace?t('Workspace file'):t('Select the workspace to open this file')}</small></span></button>)}
      {images.filter(url=>external(url)||url.startsWith('/api/')).map(url=><a className="fs-workbench-item" key={url} href={url} target="_blank" rel="noreferrer"><Image size={16}/><img src={url} alt={t('Generated image')} loading="lazy"/></a>)}
    </>:<><h3>{t('Sources and context')}</h3>
      {project&&<><h4>{project.name}</h4><p>{t('This conversation belongs to this project.')}</p>{project.context_items?.map(item=><button className="fs-workbench-item" key={item.id} disabled={!workspace||item.kind==='folder'||!item.path} onClick={()=>dispatch({type:'file',workspace,path:item.path})}><FileText size={16}/><span>{item.name||item.path}<small>{item.kind}</small></span></button>)}</>}
      {[...attachments.values()].map(a=><a className="fs-workbench-item" key={a.id} href={attachmentUrl(a.id)} target="_blank" rel="noreferrer"><Paperclip size={16}/><span>{a.name}<small>{a.mime} · {a.size} B</small></span></a>)}
      {[...sources.values()].map(s=><a className="fs-workbench-item" key={s.url} href={s.url} target="_blank" rel="noreferrer"><span>{s.title||s.url}<small>{s.url}</small></span></a>)}
      {!sources.size&&!attachments.size&&!project&&<p>{t('References, attachments and project context will appear here.')}</p>}
    </>}
  </div>;
}
