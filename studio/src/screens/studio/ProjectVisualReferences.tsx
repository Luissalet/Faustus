import {useEffect,useRef,useState} from 'react';
import {Images} from 'lucide-react';
import {Button,Popover} from '../../components';
import {attachContextSource,listContextLinks,removeContextRoot,type ContextLink} from '../../adapters/projects';
import {saveCopy} from '../../adapters/imageTools';
import {getImage} from '../../adapters/gallery';
import {galleryAttachment} from '../../lib/gallery-attachment';
import {REFERENCE_ROLES} from '../../lib/image-references';
import type {Attachment} from '../../adapters/composer';
import {t} from '../../i18n';

const catalogTag='faustus:visual-reference';
export function visualAlias(value:string):string {
  const alias=value.trim().replace(/^@/,'').normalize('NFC');
  if(!/^[\p{L}\p{N}][\p{L}\p{N}_-]{0,47}$/u.test(alias))throw new Error(t('Use 1–48 letters, numbers, underscores or hyphens for the reference name.'));
  return '@'+alias;
}
export function visualRole(link:ContextLink):Attachment['referenceRole'] {
  return REFERENCE_ROLES.find(role=>link.tags.includes('visual-role:'+role.value))?.value||'subject';
}
export default function ProjectVisualReferences({projectId,onUse}:{projectId:string;onUse:(file:File,role:Attachment['referenceRole'])=>Promise<void>}) {
  return <ReferenceCatalog key={projectId} projectId={projectId} onUse={onUse}/>;
}
function ReferenceCatalog({projectId,onUse}:{projectId:string;onUse:(file:File,role:Attachment['referenceRole'])=>Promise<void>}) {
  const [links,setLinks]=useState<ContextLink[]|null>(null),[name,setName]=useState(''),[role,setRole]=useState('subject');
  const [file,setFile]=useState<File|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[reload,setReload]=useState(0);
  const alive=useRef(true);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  useEffect(()=>{const controller=new AbortController();setLinks(null);setError('');listContextLinks(projectId,controller.signal).then(result=>{if(!controller.signal.aborted)setLinks(result.links.filter(link=>link.kind==='gallery_image'&&link.tags.includes(catalogTag)));}).catch(e=>{if(!controller.signal.aborted)setError(e.message);});return()=>controller.abort();},[projectId,reload]);
  const action=async(work:()=>Promise<void>)=>{if(busy)return;setBusy(true);setError('');setNotice('');try{await work();}catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy(false);}};
  return <Popover side="top" className="fs-media-recipes" trigger={<button type="button" className="fs-studio__chip"><Images size={13}/>{t('Visual references')}</button>}><section aria-label={t('Visual references')}><h3>{t('Visual references')}</h3>
    <p>{t('Save named images in this project and attach them to any of its chats. Consistency depends on the selected image model.')}</p>
    {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {!links?(error?<Button label={t('Retry')} onClick={()=>setReload(n=>n+1)}/>:<p role="status">{t('Loading…')}</p>):<>
      <Button label={t('Refresh')} disabled={busy} onClick={()=>setReload(n=>n+1)}/>
      {!links.length&&<p>{t('No saved visual references yet.')}</p>}
      {links.map(link=><div key={link.id} className="fs-panel__row"><span>{link.label} · {t(REFERENCE_ROLES.find(item=>item.value===visualRole(link))!.label)}</span>
        <Button label={t('Attach')} disabled={busy||!link.enabled} onClick={()=>void action(async()=>{const image=await getImage(link.refId);const selected=await galleryAttachment(image.url,link.label+'.png',location.origin);if(!alive.current)return;await onUse(selected,visualRole(link));if(alive.current)setNotice(t('Reference attached to your draft.'));})}/>
        <Button label={t('Remove reference')} disabled={busy} onClick={()=>void action(async()=>{await removeContextRoot(projectId,link.id);if(alive.current)setReload(n=>n+1);})}/></div>)}
      <details><summary>{t('Save a visual reference')}</summary>
        <label>{t('Reference name')}<input value={name} maxLength={49} placeholder="@personaje" disabled={busy} onChange={e=>setName(e.target.value)}/></label>
        <label>{t('Reference role')}<select value={role} disabled={busy} onChange={e=>setRole(e.target.value)}>{REFERENCE_ROLES.map(item=><option key={item.value} value={item.value}>{t(item.label)}</option>)}</select></label>
        <label>{t('Reference image')}<input type="file" accept="image/png,image/jpeg" disabled={busy} onChange={e=>setFile(e.target.files?.[0]||null)}/></label>
        <p>{t('PNG or JPEG, up to 20 MB. Removing a reference keeps the original image in the gallery.')}</p>
        <Button label={t('Save reference')} loading={busy} disabled={!file||!name.trim()} onClick={()=>void action(async()=>{
          const alias=visualAlias(name);if(links.some(link=>link.label.toLocaleLowerCase()===alias.toLocaleLowerCase()))throw new Error(t('That reference name is already used in this project.'));
          if(!file||!['image/png','image/jpeg'].includes(file.type)||!file.size||file.size>20*1024*1024)throw new Error(t('Choose a PNG or JPEG image up to 20 MB.'));
          const id=await saveCopy(file,file.type==='image/jpeg'?'jpg':'png');if(!id)throw new Error(t('The gallery did not return an image identifier.'));
          const saved=await attachContextSource(projectId,{kind:'gallery_image',id,label:alias,role:role==='style'?'style_reference':'reference',retrievalPolicy:'on_demand',tags:[catalogTag,'visual-role:'+role]});
          if(alive.current){setReload(n=>n+1);setName('');setNotice(saved.label===alias?t('Visual reference saved.'):t('This image is already linked to the project. Its existing name and settings were preserved.'));}
        })}/>
      </details>
    </>}
  </section></Popover>;
}
