import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {Users,Plus,Trash2,X} from 'lucide-react';
import {Button,IconButton} from '../../components';
import {emptyTeam,loadTeam,saveTeam,type ChatTeam as Team,type TeamMember} from '../../adapters/chat-team';
import type {ModelRoute} from '../../adapters/chat';
import {t} from '../../i18n';

export default function ChatTeam({sessionId,routes,coordinator,busy,ensureSession,onEnabled}:{sessionId:string|null;routes:ModelRoute[];coordinator:ModelRoute|null;busy:boolean;ensureSession:()=>Promise<string|null>;onEnabled:(enabled:boolean)=>void}) {
  const [open,setOpen]=useState(false),[team,setTeam]=useState<Team>(emptyTeam),[revision,setRevision]=useState(0);
  const [loading,setLoading]=useState(false),[saving,setSaving]=useState(false),[error,setError]=useState(''),[dirty,setDirty]=useState(false);
  const current=useRef(sessionId);current.current=sessionId;
  const drafts=useRef(new Map<string,Team>());
  const writes=useRef(new Set<string>());
  const saved=useRef(new Map<string,{revision:number;team:Team}>());
  const [loadFailed,setLoadFailed]=useState(false);
  const anchor=useRef<HTMLDivElement>(null);
  const [position,setPosition]=useState({left:12,bottom:100,maxHeight:500});
  useLayoutEffect(()=>{
    if(!open)return;
    const place=()=>{const rect=anchor.current?.getBoundingClientRect();if(rect)setPosition({left:Math.max(12,Math.min(rect.left,innerWidth-592)),bottom:Math.max(12,innerHeight-rect.top+8),maxHeight:Math.max(160,Math.min(rect.top-24,innerHeight*.68))});};
    const dismiss=(event:KeyboardEvent)=>{if(event.key==='Escape'){event.preventDefault();setOpen(false);anchor.current?.querySelector('button')?.focus();}};
    place();window.addEventListener('resize',place);window.addEventListener('keydown',dismiss);
    return()=>{window.removeEventListener('resize',place);window.removeEventListener('keydown',dismiss);};
  },[open]);
  useEffect(()=>{
    const id=sessionId;const abort=new AbortController();setError('');setLoadFailed(false);setRevision(0);setTeam(emptyTeam);setDirty(false);onEnabled(false);
    if(!id){setLoading(false);return;}
    setLoading(true);
    loadTeam(id,abort.signal).then(response=>{if(abort.signal.aborted||writes.current.has(id))return;const cached=saved.current.get(id);const s=cached&&cached.revision>response.revision?cached:response;setRevision(s.revision);const draft=drafts.current.get(id);setTeam(draft || s.team);setDirty(Boolean(draft));onEnabled(s.team.enabled);})
      .catch(()=>{if(!abort.signal.aborted){setLoadFailed(true);setError(t('Could not load the team. Reopen the conversation to retry.'));}})
      .finally(()=>{if(!abort.signal.aborted)setLoading(false);});
    return()=>abort.abort();
  },[sessionId]);
  const change=(next:Team)=>{setTeam(next);setDirty(true);if(sessionId)drafts.current.set(sessionId,next);};
  const member=(id:string,patch:Partial<TeamMember>)=>change({...team,members:team.members.map(m=>m.id===id?{...m,...patch}:m)});
  const save=async()=>{if(saving)return;setSaving(true);setError('');const started=sessionId;let id=started;
    try {id=started || await ensureSession();if(!id)throw Error(t('Choose a coordinator model first.'));
      writes.current.add(id);
      const clean={...team,members:team.members.map(m=>({...m,tools:m.tools.map(s=>s.trim()).filter(Boolean),files:m.files.map(s=>s.trim()).filter(Boolean)}))};
      const result=await saveTeam(id,{revision:started?revision:0,team:clean});saved.current.set(id,result);drafts.current.delete(id);
      if(current.current===id || current.current===started){setTeam(result.team);setRevision(result.revision);setDirty(false);onEnabled(result.team.enabled);}
    }catch(e){if(current.current===started||current.current===id){setTeam(team);setDirty(true);setError((e as Error).message);}}finally{if(id)writes.current.delete(id);setSaving(false);}};
  return <div className="fs-chat-team" ref={anchor}>
    <button type="button" className="fs-studio__chip" aria-expanded={open} onClick={()=>setOpen(v=>!v)}><Users size={15}/>{t(team.enabled?'Orchestrator and team':'Configure team')}{dirty?' •':''}</button>
    {open&&<section className="fs-chat-team__editor" style={position} aria-label={t('Chat team')}>
      <header className="fs-chat-team__head"><h3>{t('Chat team')}</h3><IconButton icon={X} label={t('Close team settings')} onClick={()=>setOpen(false)}/></header><p>{t('Coordinator')}: <strong>{coordinator?.model || t('Choose a model')}</strong></p>
      <p>{t('The selected chat model coordinates this team. Changes apply to the next turn; running agents keep their configuration.')}</p>
      {error&&<p role="alert" className="fs-notice" data-tone="danger">{error}</p>}
      {loading?<p role="status">{t('Loading team…')}</p>:<fieldset disabled={saving||busy||loadFailed}>
        <label><input type="checkbox" checked={team.enabled} onChange={e=>change({...team,enabled:e.target.checked})}/>{t('Use this model as orchestrator')}</label>
        {team.members.map((m,i)=><div className="fs-chat-team__member" key={m.id}>
          <div className="fs-panel__row"><label>{t('Name')}<input aria-label={t('Agent {n} name',{n:i+1})} value={m.name} maxLength={80} onChange={e=>member(m.id,{name:e.target.value})}/></label><IconButton icon={Trash2} label={t('Remove {name}',{name:m.name})} onClick={()=>change({...team,members:team.members.filter(v=>v.id!==m.id)})}/></div>
          <label>{t('Model')}<select value={m.endpoint_id?`${m.endpoint_id}::${m.model}`:''} onChange={e=>{const r=routes.find(r=>r.id===e.target.value);member(m.id,{model:r?.model||'',endpoint_id:r?.endpointId||''});}}><option value="">{t('Same as coordinator')}</option>{m.endpoint_id&&!routes.some(r=>r.id===`${m.endpoint_id}::${m.model}`)&&<option value={`${m.endpoint_id}::${m.model}`}>{m.model} — {t('Unavailable')}</option>}{routes.map(r=><option key={r.id} value={r.id}>{r.model} · {r.endpointName}</option>)}</select></label>
          <label>{t('Role and instructions')}<textarea value={m.role} maxLength={2000} onChange={e=>member(m.id,{role:e.target.value})}/></label>
          <label><input type="checkbox" checked={m.write} onChange={e=>member(m.id,{write:e.target.checked})}/>{t('May edit files within inherited permissions')}</label>
          <details><summary>{t('Tools and file assignments')}</summary><label>{t('Allowed tools (comma-separated; empty inherits)')}<input value={m.tools.join(',')} onChange={e=>member(m.id,{tools:e.target.value.split(',')})}/></label><label>{t('Assigned files (one per line)')}<textarea value={m.files.join('\n')} onChange={e=>member(m.id,{files:e.target.value.split('\n')})}/></label></details>
        </div>)}
        <Button icon={Plus} label={t('Add agent')} disabled={team.members.length>=8} onClick={()=>change({...team,members:[...team.members,{id:crypto.randomUUID(),name:t('Agent {n}',{n:team.members.length+1}),role:'',model:'',endpoint_id:'',tools:[],files:[],write:true}]})}/>
        <div className="fs-chat-team__limits">{(['max_parallel','max_rounds','timeout_s'] as const).map((key,i)=><label key={key}>{t(['Parallel agents','Rounds per agent','Seconds per agent'][i])}<input type="number" min={[1,3,60][i]} max={[8,40,7200][i]} value={team[key]} onChange={e=>change({...team,[key]:Number(e.target.value)})}/></label>)}</div>
        <Button variant="primary" label={t('Save team for this chat')} disabled={!dirty} loading={saving} onClick={()=>void save()}/>
      </fieldset>}
      {busy&&<p role="status">{t('Team changes are available after the current turn.')}</p>}
    </section>}
  </div>;
}
