import { ChevronRight, LoaderCircle } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { t } from '../../i18n';
import { SCROLL_TO_TURN_EVENT, type Turn } from '../studio/model';
import './debate.css';

const opened = new Map<string, boolean>();

export function DebatePanel({debate, index, sessionId, renderReply}: {
  debate: NonNullable<Turn['debate']>; index:number; sessionId:string|null;
  renderReply:(turn:Turn,last:boolean)=>ReactNode;
}) {
  const key = `${sessionId ?? ''}:${debate.origin}`;
  const [open,setOpen] = useState(opened.get(key) ?? false);
  const active = debate.state === 'working';
  const status = active ? (debate.finalizing ? t('Preparing the final answer') : t('Debating')) : debate.state === 'waiting_user' ? t('Needs your decision')
    : debate.state === 'stopped' ? t('Cancelled') : debate.state === 'error' ? t('Error') : t('Completed');
  useEffect(() => {
    const jump = (event:Event) => {
      if ((event as CustomEvent<{index?:number}>).detail?.index === index) {
        opened.set(key,true); setOpen(true);
      }
    };
    window.addEventListener(SCROLL_TO_TURN_EVENT,jump);
    return () => window.removeEventListener(SCROLL_TO_TURN_EVENT,jump);
  },[index,key]);
  return <details className="fs-debate" open={open} data-state={debate.state} data-testid="group-debate"
    onToggle={event=>{const value=event.currentTarget.open;opened.set(key,value);setOpen(value);}}>
    <summary className="fs-debate__summary">
      <ChevronRight className="fs-debate__chevron" size={17} aria-hidden="true"/>
      <strong>{t('Debate')}</strong>
      <span className="fs-debate__status" role="status">
        {active && <LoaderCircle className="fs-debate__spinner" size={14} aria-hidden="true"/>}{status}
      </span>
      {debate.speakers.length > 0 && <span className="fs-debate__speakers">{debate.speakers.join(' · ')}</span>}
    </summary>
    <div className="fs-debate__replies">
      {debate.replies.map((reply,i)=><div key={reply.id}>{renderReply(reply,i === debate.replies.length-1)}</div>)}
      {debate.replies.length === 0 && <p className="fs-debate__empty">{t('Waiting for the models')}</p>}
    </div>
  </details>;
}
