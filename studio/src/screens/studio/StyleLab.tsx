import {useEffect,useRef,useState} from 'react';
import {WandSparkles} from 'lucide-react';
import {Button,Popover} from '../../components';
import {saveTemplate,type Preset} from '../../adapters/presets';
import {t,locale} from '../../i18n';
import {Rich} from '../rich';

export default function StyleLab({model,onSaved}:{model:string;onSaved:(preset:Preset)=>void}) {
  return <StyleForm model={model} onSaved={onSaved}/>;
}
function StyleForm({model,onSaved}:{model:string;onSaved:(preset:Preset)=>void}) {
  const [examples,setExamples]=useState(''),[rules,setRules]=useState(''),[name,setName]=useState(''),[prompt,setPrompt]=useState('');
  const [result,setResult]=useState<{baseline:string;styled:string}|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(''),[notice,setNotice]=useState('');
  const active=useRef<AbortController|null>(null),mounted=useRef(true),fileVersion=useRef(0);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;active.current?.abort();fileVersion.current++;};},[]);
  useEffect(()=>{active.current?.abort();setBusy('');setResult(null);},[model]);
  const run=async(mode:'derive'|'compare')=>{if(busy)return;const controller=new AbortController();active.current=controller;setBusy(mode);setError('');setNotice('');setResult(null);
    try{const response=await fetch('/api/presets/style/'+mode,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify({model,examples,rules,prompt,language:locale().startsWith('es')?'es':'en'})});
      const body=await response.json();if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:t('The style request failed.'));
      if(!controller.signal.aborted){if(mode==='derive'){if(typeof body.rules!=='string'||!body.rules.trim())throw new Error(t('The model returned no style rules.'));setRules(body.rules);}else{if(typeof body.baseline!=='string'||typeof body.styled!=='string')throw new Error(t('The style comparison is incomplete.'));setResult(body);}}
    }catch(e){if(!controller.signal.aborted)setError((e as Error).message);}finally{if(!controller.signal.aborted)setBusy('');}};
  return <Popover side="top" className="fs-media-recipes" trigger={<button type="button" className="fs-studio__chip"><WandSparkles size={13}/>{t('Learn a style')}{busy?'…':''}</button>}><section aria-label={t('Learn a style')}><h3>{t('Learn a style')}</h3><p>{t('Extract editable style rules from examples, compare two responses, then save as a reusable preset. This does not train model weights.')}</p>
    {busy&&<p role="status">{t('Working… Closing this panel keeps your edits and processing continues while this chat stays open.')}</p>}
    {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
    <p>{t('Selected model:')} {model||t('Select a model first.')}</p>
    <label>{t('Writing examples')}<textarea maxLength={20000} value={examples} disabled={!!busy} onChange={e=>{fileVersion.current++;setExamples(e.target.value);}}/></label>
    <label>{t('Import TXT or Markdown')}<input type="file" accept=".txt,.md,.markdown,text/plain,text/markdown" disabled={!!busy} onChange={e=>{const file=e.target.files?.[0];if(!file)return;const version=++fileVersion.current;if(file.size>80000){setError(t('Examples must fit within 20,000 characters.'));return;}void file.text().then(text=>{if(version!==fileVersion.current||!mounted.current)return;if(text.length>20000)throw new Error(t('Examples must fit within 20,000 characters.'));setExamples(text);}).catch(e=>{if(mounted.current)setError(e.message);});}}/></label>
    <Button label={t('Derive style rules')} loading={busy==='derive'} disabled={!!busy||!model||!examples.trim()} onClick={()=>void run('derive')}/>
    <label>{t('Editable style rules')}<textarea maxLength={10000} value={rules} disabled={!!busy} onChange={e=>{setRules(e.target.value);setResult(null);}}/></label>
    <label>{t('Test prompt')}<textarea maxLength={4000} value={prompt} disabled={!!busy} onChange={e=>{setPrompt(e.target.value);setResult(null);}}/></label>
    <Button label={t('Compare (2 model responses)')} loading={busy==='compare'} disabled={!!busy||!model||!rules.trim()||!prompt.trim()} onClick={()=>void run('compare')}/>
    {result&&<><h4>{t('Without style')}</h4><Rich text={result.baseline}/><h4>{t('With style')}</h4><Rich text={result.styled}/></>}
    <label>{t('Preset name')}<input value={name} maxLength={100} disabled={!!busy} onChange={e=>setName(e.target.value)}/></label>
    <Button label={t('Save and use style')} loading={busy==='save'} disabled={!!busy||!name.trim()||!rules.trim()} onClick={()=>{setBusy('save');setError('');void saveTemplate({name:name.trim(),systemPrompt:rules}).then(saved=>{if(mounted.current){onSaved(saved);setNotice(t('Style saved and selected for this chat.'));}}).catch(e=>{if(mounted.current)setError(e.message);}).finally(()=>{if(mounted.current)setBusy('');});}}/>
  </section></Popover>;
}
