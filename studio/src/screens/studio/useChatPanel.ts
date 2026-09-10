import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {initialPanel,panelReducer,type PanelState,type PanelAction} from './panel';
import {readPanel,persistPanels} from './panel-storage';

function read(key:string,privateMode:boolean):PanelState {
  if(privateMode)return {...initialPanel};
  // BENCH-01 ("espacio de trabajo persistente"): localStorage, not
  // sessionStorage - a document/file/draft has to survive the tab actually
  // closing (not just navigating within it), which is the whole point of a
  // workspace the person can come back to. The per-conversation `key` below
  // is already a stable id independent of tab/session, so this changes only
  // where it is stored, not what identifies it.
  try {return readPanel(localStorage,key);}catch{/* no persisted panel */}
  return {...initialPanel};
}
/** Late callbacks stay bound to their conversation, not the newly visible one. */
export function useChatPanel(sessionId:string|null,privateMode:boolean) {
  const key=(privateMode?'private:':'')+(sessionId||'new');
  const initial=useMemo(()=>read(key,privateMode),[key,privateMode]);
  const [states,setStates]=useState<Record<string,PanelState>>({});
  const written=useRef(new Map<string,PanelState>());
  const state=states[key]||initial;
  const dispatch=useCallback((action:PanelAction)=>setStates(all=>({...all,[key]:panelReducer(all[key]||initial,action)})),[key,initial]);
  useEffect(()=>{try{persistPanels(localStorage,{...states,[key]:state},written.current);}catch{/* drafts remain in memory if browser storage is unavailable */}},[states,key,state]);
  useEffect(()=>{const warn=(event:BeforeUnloadEvent)=>{if(Object.values(states).some(s=>Object.keys(s.drafts).length)||Object.keys(state.drafts).length){event.preventDefault();event.returnValue='';}};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);},[states,state.drafts]);
  return [state,dispatch] as const;
}
