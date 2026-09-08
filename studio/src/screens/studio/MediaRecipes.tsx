import {useEffect,useRef,useState} from 'react';
import {Clapperboard} from 'lucide-react';
import {Button,Popover} from '../../components';
import {loadMediaRecipes,previewMediaRecipe,recipeInputs,recipeMessage,type MediaRecipe} from '../../adapters/media-recipes';
import {t} from '../../i18n';

export function MediaRecipes({onInsert}:{onInsert:(text:string)=>void}) {
  return <Popover side="top" className="fs-media-recipes" trigger={<button type="button" className="fs-studio__chip"><Clapperboard size={13}/>{t('Media recipe')}</button>}><RecipeForm onInsert={onInsert}/></Popover>;
}
function RecipeForm({onInsert}:{onInsert:(text:string)=>void}) {
  const [recipes,setRecipes]=useState<MediaRecipe[]|null>(null),[selected,setSelected]=useState('');
  const [values,setValues]=useState<Record<string,string>>({}),[error,setError]=useState(''),[status,setStatus]=useState('');
  const [checking,setChecking]=useState(false),[retry,setRetry]=useState(0);
  const [broken,setBroken]=useState<{file:string;reason:string}[]>([]);
  const pending=useRef<AbortController|null>(null);
  useEffect(()=>{const controller=new AbortController();setError('');setRecipes(null);
    loadMediaRecipes(controller.signal).then(items=>{if(!controller.signal.aborted){setRecipes(items.recipes);setBroken(items.broken);}}).catch(error=>{if(!controller.signal.aborted)setError(error.message);});
    return()=>{controller.abort();pending.current?.abort();};
  },[retry]);
  const recipe=recipes?.find(item=>item.id===selected);
  const change=(name:string,value:string)=>{pending.current?.abort();setChecking(false);setStatus('');setError('');setValues(old=>({...old,[name]:value}));};
  const check=async()=>{if(!recipe||checking)return;const controller=new AbortController();pending.current=controller;
    setChecking(true);setError('');setStatus('');
    try {const result=await previewMediaRecipe(recipe,recipeInputs(recipe,values),controller.signal);
      if(!controller.signal.aborted) {if(result.ok)setStatus(t('The engine can run this recipe. Nothing has been queued.'));else setError(result.detail||result.reason||t('The engine is not ready.'));}
    }catch(error){if(!controller.signal.aborted)setError((error as Error).message);}finally{if(!controller.signal.aborted)setChecking(false);}
  };
  return <section aria-label={t('Media recipe')}>
    <h3>{t('Media recipe')}</h3><p>{t('Choose a recipe, check its requirements and add it to your message. This does not start a render.')}</p>
    {!recipe&&error&&<p role="alert">{error}</p>}
    {!!broken.length&&<details><summary>{t('Recipes that could not be loaded')}</summary><ul>{broken.map(item=><li key={item.file}>{item.file}: {item.reason}</li>)}</ul></details>}
    {!recipes?(error?<Button label={t('Retry')} onClick={()=>setRetry(n=>n+1)}/>:<p role="status">{t('Loading…')}</p>):<>
      {!recipes.length?<p>{t('No media recipes are installed.')}</p>:<label>{t('Recipe')}<select value={selected} onChange={event=>{pending.current?.abort();setChecking(false);setSelected(event.target.value);setValues({});setError('');setStatus('');}}><option value="">{t('Choose a recipe')}</option>{recipes.map(item=><option key={item.id} value={item.id}>{item.title||item.id} · {item.version}</option>)}</select></label>}
      {recipe&&<><p>{recipe.description}</p>
        {recipe.inputs.map(field=><label key={field.name}>{field.title||field.name}{field.required?' *':''}
          {field.type==='enum'||field.type==='boolean'?<select value={values[field.name]??String(field.default??'')} onChange={event=>change(field.name,event.target.value)}><option value="">{t('Default')}</option>{(field.type==='boolean'?[true,false]:field.choices||[]).map(choice=><option key={String(choice)} value={String(choice)}>{String(choice)}</option>)}</select>:<input value={values[field.name]??String(field.default??'')} maxLength={field.max_len??2000} inputMode={['number','integer','seed'].includes(field.type)?'decimal':undefined} onChange={event=>change(field.name,event.target.value)}/>}
          {field.type==='artifact'&&<small>{t('Use the image name already uploaded to the engine, not a local file path or a chat attachment ID.')}</small>}
        </label>)}
        <details><summary>{t('Required models')}</summary><ul>{recipe.models?.map(model=><li key={model.name}>{model.name} · {model.license||t('Unknown')}</li>)}</ul></details>
        {status&&<p role="status">{status}</p>}
        {error&&<p role="alert">{error}</p>}
        <div className="fs-panel__row"><Button label={t('Check requirements')} loading={checking} onClick={()=>void check()}/><Button label={t('Add to message')} onClick={()=>{try{onInsert(recipeMessage(recipe,recipeInputs(recipe,values)));setStatus(t('Added to your draft. Review it before sending.'));}catch(error){setError((error as Error).message);}}}/></div>
      </>}
    </>}
  </section>;
}
