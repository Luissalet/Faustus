import {ApiError, getJson} from './api';

export interface TeamMember {id:string; name:string; role:string; model:string; endpoint_id:string; tools:string[]; files:string[]; write:boolean}
export interface ChatTeam {enabled:boolean; members:TeamMember[]; max_parallel:number; max_rounds:number; timeout_s:number}
export interface TeamSnapshot {revision:number; team:ChatTeam}
export const emptyTeam:ChatTeam = {enabled:false,members:[],max_parallel:2,max_rounds:12,timeout_s:600};
export const loadTeam = (id:string,signal?:AbortSignal) => getJson<TeamSnapshot>(`/api/session/${encodeURIComponent(id)}/team`,signal);
export async function saveTeam(id:string, snapshot:TeamSnapshot):Promise<TeamSnapshot> {
  const response=await fetch(`/api/session/${encodeURIComponent(id)}/team`,{method:'PUT',credentials:'same-origin',signal:AbortSignal.timeout(20000),headers:{'Content-Type':'application/json'},body:JSON.stringify(snapshot)});
  if(!response.ok) {let detail='';try{detail=String((await response.json()).detail || '');}catch{/* status below */}throw new ApiError(detail || 'Could not save the team',response.status);}
  return response.json();
}
