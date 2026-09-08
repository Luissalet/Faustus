import {asArray,getJson,ApiError} from './api';
import {t} from '../i18n';

export interface MediaInput {name:string;type:string;title:string;required?:boolean;default?:unknown;choices?:unknown[];minimum?:number;maximum?:number;max_len?:number}
export interface MediaRecipe {id:string;version:string;title:string;description:string;inputs:MediaInput[];models:{name:string;license:string}[]}
export async function loadMediaRecipes(signal?:AbortSignal) {
  const body=await getJson<{workflows?:unknown;broken?:unknown}>('/api/media/workflows',signal);
  return {recipes:asArray<MediaRecipe>(body.workflows).filter(item=>typeof item.id==='string'&&Array.isArray(item.inputs)),
    broken:asArray<{file:string;reason:string}>(body.broken)};
}
export function recipeInputs(recipe:MediaRecipe,values:Record<string,string>):Record<string,unknown> {
  const result:Record<string,unknown>=Object.create(null);
  for(const field of recipe.inputs) {
    const value=values[field.name] ?? (field.default==null?'':String(field.default));
    if(!value.trim()) {if(field.required)throw new Error(t('Required: {field}',{field:field.title||field.name}));continue;}
    if(['number','integer','seed'].includes(field.type)) {
      const number=Number(value);
      if(!Number.isFinite(number)||field.type!=='number'&&!Number.isSafeInteger(number)||field.minimum!=null&&number<field.minimum||field.maximum!=null&&number>field.maximum)
        throw new Error(t('Invalid value: {field}',{field:field.title||field.name}));
      result[field.name]=number;
    } else if(field.type==='boolean') {
      if(!['true','false'].includes(value))throw new Error(t('Invalid value: {field}',{field:field.title||field.name}));
      result[field.name]=value==='true';
    }
    else if(field.type==='enum') {
      const match=field.choices?.find(choice=>String(choice)===value);
      if(match===undefined)throw new Error(t('Invalid value: {field}',{field:field.title||field.name}));
      result[field.name]=match;
    } else if(['text','artifact'].includes(field.type)) {
      if(field.max_len!=null&&value.length>field.max_len)throw new Error(t('Invalid value: {field}',{field:field.title||field.name}));
      result[field.name]=value;
    } else throw new Error(t('Unsupported input type: {type}',{type:field.type}));
  }
  return result;
}
export async function previewMediaRecipe(recipe:MediaRecipe,inputs:Record<string,unknown>,signal?:AbortSignal) {
  const response=await fetch('/api/media/plan',{method:'POST',credentials:'same-origin',signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({workflow:recipe.id,version:recipe.version,inputs})});
  if(!response.ok)throw new ApiError(t('Could not check the recipe ({status}).',{status:response.status}),response.status);
  return await response.json() as {ok:boolean;detail?:string;reason?:string;resolved_inputs?:Record<string,unknown>;seed?:number};
}
export function recipeMessage(recipe:MediaRecipe,inputs:Record<string,unknown>):string {
  return t('Use this media recipe for my request. Check the engine and required models first, then use the normal approval flow before rendering.')+'\n\n'+JSON.stringify({workflow:recipe.id,version:recipe.version,inputs},null,2);
}
