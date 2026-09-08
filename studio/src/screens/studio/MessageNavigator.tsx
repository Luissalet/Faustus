import {useEffect, useRef, useState, type RefObject} from 'react';
import type {Turn} from './model';
import {t} from '../../i18n';

export function navigationIndex(y:number, top:number, height:number, count:number) {
  return Math.max(0, Math.min(count-1, Math.round((y-top)/Math.max(1,height)*(count-1))));
}

/** One keyboard stop, with a continuous pointer track even for very long chats. */
export function MessageNavigator({turns,scrollRef,onJump}:{turns:Turn[];scrollRef:RefObject<HTMLDivElement|null>;onJump:()=>void}) {
  const [active,setActive]=useState(0);
  const [preview,setPreview]=useState<number|null>(null);
  const [focused,setFocused]=useState(false);
  const rail=useRef<HTMLDivElement>(null);
  const dragging=useRef(false);
  const key=turns.map(turn=>turn.id).join('|');
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
  return <nav className="fs-message-nav" aria-label={t('Message navigation')}>
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
