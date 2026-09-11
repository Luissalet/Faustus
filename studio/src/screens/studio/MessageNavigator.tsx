import {useEffect, useRef, useState, type RefObject} from 'react';
import {Search, X} from 'lucide-react';
import type {Turn} from './model';
import {t} from '../../i18n';

export function navigationIndex(y:number, top:number, height:number, count:number) {
  return Math.max(0, Math.min(count-1, Math.round((y-top)/Math.max(1,height)*(count-1))));
}

/** Plain text a turn can be searched by: its own message, plus tool step
 *  labels — enough to find "that bash command" without needing its exact
 *  wording, without reaching into every attachment/diff. */
function searchableText(turn:Turn):string {
  return [turn.text, ...turn.steps.map(s=>s.label), ...turn.attachments.map(a=>a.name)].join(' ').toLowerCase();
}

/** One keyboard stop, with a continuous pointer track even for very long chats. */
export function MessageNavigator({turns,scrollRef,onJump}:{turns:Turn[];scrollRef:RefObject<HTMLDivElement|null>;onJump:()=>void}) {
  const [active,setActive]=useState(0);
  const [preview,setPreview]=useState<number|null>(null);
  const [focused,setFocused]=useState(false);
  const rail=useRef<HTMLDivElement>(null);
  const dragging=useRef(false);
  const key=turns.map(turn=>turn.id).join('|');
  // UX-05: a search over the conversation's OWN text, not the browser's —
  // a long chat is virtualized (Transcript.tsx), so most turns render
  // nothing at all off-screen and the browser's Ctrl+F silently finds
  // nothing past whatever happens to be mounted right now. Its own Ctrl+F
  // steps in only when nothing else already wants the keystroke (an input,
  // a textarea, anything contentEditable — the composer above all), so
  // typing a message is never interrupted.
  const [searchOpen,setSearchOpen]=useState(false);
  const [query,setQuery]=useState('');
  const [matchAt,setMatchAt]=useState(0);
  const searchInputRef=useRef<HTMLInputElement>(null);
  useEffect(()=>{setMatchAt(0);},[query]);
  useEffect(()=>{
    const onKey=(e:KeyboardEvent)=>{
      if(!(e.ctrlKey||e.metaKey)||e.key.toLowerCase()!=='f')return;
      const el=document.activeElement as HTMLElement|null;
      const typingElsewhere=el&&el!==searchInputRef.current&&(el.tagName==='TEXTAREA'||el.tagName==='INPUT'||el.isContentEditable);
      if(typingElsewhere)return;
      e.preventDefault();
      setSearchOpen(true);
      requestAnimationFrame(()=>searchInputRef.current?.focus());
    };
    document.addEventListener('keydown',onKey);
    return ()=>document.removeEventListener('keydown',onKey);
  },[]);
  useEffect(()=>{
    const scroll=scrollRef.current;
    if(!scroll)return;
    let frame=0;
    const update=()=>{
      cancelAnimationFrame(frame);
      frame=requestAnimationFrame(()=>{
        const nodes=Array.from(scroll.querySelectorAll<HTMLElement>('[data-nav-id]'));
        const top=scroll.getBoundingClientRect().top+32;
        let index=0;
        for(let i=0;i<nodes.length;i++){if(nodes[i].getBoundingClientRect().top>top)break;index=i;}
        if(scroll.scrollTop>0&&scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<4)index=nodes.length-1;
        setActive(Math.max(0,index));
      });
    };
    scroll.addEventListener('scroll',update,{passive:true});
    const observer=new ResizeObserver(update);
    observer.observe(scroll);
    if(scroll.firstElementChild)observer.observe(scroll.firstElementChild);
    update();
    return()=>{scroll.removeEventListener('scroll',update);observer.disconnect();cancelAnimationFrame(frame);};
  },[key,scrollRef]);
  if(turns.length<2)return null;
  const current=Math.min(active,turns.length-1);
  const hovered=preview===null?(focused?current:null):Math.min(preview,turns.length-1);
  const excerpt=(index:number)=>{
    const turn=turns[index];
    return (turn.text.trim()||turn.attachments.map(a=>a.name).join(', ')||turn.steps.map(s=>s.label).join(', ')||t('Message without text')).replace(/\s+/g,' ').slice(0,240);
  };
  const go=(index:number)=>{
    index=Math.max(0,Math.min(turns.length-1,index));
    const scroll=scrollRef.current;
    const node=scroll&&Array.from(scroll.querySelectorAll<HTMLElement>('[data-nav-id]')).find(node=>node.dataset.navId===turns[index].id);
    if(!scroll||!node)return;
    onJump();
    setActive(index);setPreview(index);
    scroll.scrollTo({top:Math.max(0,scroll.scrollTop+node.getBoundingClientRect().top-scroll.getBoundingClientRect().top-24),behavior:'instant'});
  };
  const point=(y:number)=>{const bounds=rail.current!.getBoundingClientRect();return navigationIndex(y,bounds.top,bounds.height,turns.length);};
  const markers=Math.min(turns.length,180);
  const trimmedQuery=query.trim().toLowerCase();
  const matches=trimmedQuery?turns.map((turn,i)=>searchableText(turn).includes(trimmedQuery)?i:-1).filter(i=>i!==-1):[];
  const jumpToMatch=(delta:number)=>{
    if(!matches.length)return;
    const next=((matchAt+delta)%matches.length+matches.length)%matches.length;
    setMatchAt(next);
    go(matches[next]);
  };
  const closeSearch=()=>{setSearchOpen(false);setQuery('');};
  return <nav className="fs-message-nav" aria-label={t('Message navigation')}>
    {searchOpen&&<form className="fs-message-nav__search" role="search" aria-label={t('Search in this conversation')}
      onSubmit={event=>{event.preventDefault();jumpToMatch(1);}}>
      <Search size={13} aria-hidden="true"/>
      <input ref={searchInputRef} value={query} onChange={event=>setQuery(event.target.value)}
        placeholder={t('Search in this conversation…')} aria-label={t('Search in this conversation')}
        onKeyDown={event=>{
          if(event.key==='Escape'){event.stopPropagation();closeSearch();}
          if(event.key==='Enter'&&event.shiftKey){event.preventDefault();jumpToMatch(-1);}
        }}
        data-testid="transcript-search-input"/>
      <span className="fs-message-nav__search-count" aria-live="polite">
        {trimmedQuery?(matches.length?`${matchAt+1}/${matches.length}`:t('No matches')):''}
      </span>
      <button type="button" onClick={()=>jumpToMatch(-1)} disabled={!matches.length} aria-label={t('Previous match')} data-testid="transcript-search-prev">‹</button>
      <button type="button" onClick={()=>jumpToMatch(1)} disabled={!matches.length} aria-label={t('Next match')} data-testid="transcript-search-next">›</button>
      <button type="button" onClick={closeSearch} aria-label={t('Close search')} data-testid="transcript-search-close"><X size={12} aria-hidden="true"/></button>
    </form>}
    {!searchOpen&&<button type="button" className="fs-message-nav__search-toggle"
      aria-label={t('Search in this conversation')} title={t('Search in this conversation')}
      onClick={()=>{setSearchOpen(true);requestAnimationFrame(()=>searchInputRef.current?.focus());}}
      data-testid="transcript-search-toggle"><Search size={13} aria-hidden="true"/></button>}
    <div className="fs-message-nav__track" ref={rail} role="slider" tabIndex={0} aria-label={t('Jump to message')}
      aria-orientation="vertical" aria-valuemin={1} aria-valuemax={turns.length} aria-valuenow={current+1}
      aria-valuetext={`${current+1} / ${turns.length}: ${excerpt(current)}`}
      onFocus={()=>setFocused(true)} onBlur={()=>{setFocused(false);setPreview(null);}}
      onPointerMove={event=>{const index=point(event.clientY);setPreview(index);if(dragging.current)go(index);}}
      onPointerLeave={()=>{if(!dragging.current)setPreview(null);}}
      onPointerDown={event=>{if(event.button!==0)return;dragging.current=true;event.currentTarget.setPointerCapture(event.pointerId);event.currentTarget.focus();go(point(event.clientY));}}
      onPointerUp={()=>{dragging.current=false;}} onPointerCancel={()=>{dragging.current=false;setPreview(null);}}
      onKeyDown={event=>{
        const next=event.key==='Home'?0:event.key==='End'?turns.length-1:event.key==='ArrowUp'?current-1:event.key==='ArrowDown'?current+1:event.key==='PageUp'?current-10:event.key==='PageDown'?current+10:null;
        if(event.key==='Escape'){setFocused(false);setPreview(null);}
        if(next!==null){event.preventDefault();go(next);}
      }}>
      {Array.from({length:markers},(_,i)=><i key={i} aria-hidden="true" style={{top:`${i/(markers-1)*100}%`}}/>)}
      <b aria-hidden="true" style={{top:`${current/(turns.length-1)*100}%`}}/>
    </div>
    {hovered!==null&&<div className="fs-message-nav__preview" aria-hidden="true" style={{top:`clamp(0px, ${hovered/(turns.length-1)*100}%, calc(100% - 130px))`}}>
      <strong>{t(turns[hovered].role==='user'?'You':'Assistant')} · {hovered+1} / {turns.length}</strong>
      <p>{excerpt(hovered)}</p>
    </div>}
  </nav>;
}
