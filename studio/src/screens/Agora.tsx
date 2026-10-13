import {useEffect, useState} from 'react';
import {useSearchParams} from 'react-router';
import {MessageSquare, Plus, Search, Users, ListChecks} from 'lucide-react';
import {Button} from '../components';
import {CoordinationSpaces} from '../components/CoordinationSpaces';
import {rooms, type CoordinationRoom} from '../adapters/coordination';
import {t} from '../i18n';
import {CouncilScreen} from './Council';
import {GroupScreen} from './group/Group';
import './agora.css';

const modes = [
  {id: 'groups', label: 'Conversation', icon: MessageSquare},
  {id: 'council', label: 'Deliberation', icon: Users},
  {id: 'development', label: 'Tasks and decisions', icon: ListChecks},
];

export function AgoraScreen() {
  const [params, setParams] = useSearchParams();
  const spaceId = params.get('space_id') || 'ecosystem';
  const mode = modes.some((m) => m.id === params.get('mode')) ? params.get('mode')! : 'groups';
  const selected = params.get('s') || '';
  const [all, setAll] = useState<CoordinationRoom[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('');
  const [roomsOpen, setRoomsOpen] = useState(false);
  function lastRoom(space: string, nextMode: string) {
    try {return sessionStorage.getItem('agora:selection:' + space + ':' + nextMode) || '';}
    catch {return '';}
  }
  function remember(space: string, nextMode: string, id: string) {
    try {sessionStorage.setItem('agora:selection:' + space + ':' + nextMode, id);} catch { /* Navigation still works. */ }
  }
  useEffect(() => {if (selected) remember(spaceId, mode, selected);}, [spaceId, mode, selected]);
  useEffect(() => {
    const controller = new AbortController();
    setAll([]); setError(''); setLoading(true);
    const refresh = () => rooms(spaceId, controller.signal).then((result) => {
      if (!controller.signal.aborted) {setAll(result); setError(''); setLoading(false);}
    }).catch((error: Error) => {if (!controller.signal.aborted) {setError(error.message); setLoading(false);}});
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => {controller.abort(); clearInterval(timer);};
  }, [spaceId, selected]);
  const scope = 'space_id=' + encodeURIComponent(spaceId);
  function choose(next: string, id?: string) {
    const room = id === undefined ? lastRoom(spaceId, next) : id;
    if (id !== undefined) remember(spaceId, next, id);
    setRoomsOpen(false); setParams({space_id: spaceId, mode: next, ...(room ? {s: room} : {})});
  }
  function chooseSpace(id: string) {
    const room = lastRoom(id, mode);
    setRoomsOpen(false); setParams({space_id: id, mode, ...(room ? {s: room} : {})});
  }
  const visible = all.filter((room) => (room.title || room.id).toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  return <div className="fs-screen fs-agora" data-testid="agora">
    <header className="fs-screen__head"><h1 className="fs-screen__title">{t('Agora')}</h1>
      <CoordinationSpaces value={spaceId} onChange={chooseSpace} homeLink={false} />
    </header>
    <div className="fs-agora__workspace">
      <aside className={'fs-agora__rooms' + (roomsOpen ? ' fs-agora__rooms--open' : '')} aria-label={t('Rooms')}>
        <div className="fs-agora__rooms-head"><h2>{t('Rooms')}</h2><button className="fs-agora__rooms-toggle fs-chip" type="button" aria-expanded={roomsOpen} aria-controls="agora-room-browser" onClick={() => setRoomsOpen(!roomsOpen)}><Users size={16} aria-hidden="true" />{t('Rooms')} ({all.length})</button><Button size="sm" variant="ghost" icon={Plus} label={t('New room')} onClick={() => choose(mode === 'development' ? 'groups' : mode, '')} /></div>
        <div id="agora-room-browser" className="fs-agora__room-browser">
        <label className="fs-agora__search"><Search size={16} aria-hidden="true" /><input aria-label={t('Find a room')} placeholder={t('Find a room')} value={filter} onChange={(e) => setFilter(e.target.value)} /></label>
        {loading && <p className="fs-muted" role="status">{t('Loading rooms…')}</p>}
        {error && <p className="fs-prose" role="alert">{error}</p>}
        {!loading && !error && !all.length && <p className="fs-muted">{t('No rooms in this space yet.')}</p>}
        {!!all.length && !visible.length && <p className="fs-muted">{t('No rooms match this search.')}</p>}
        <ul className="fs-agora__room-list">{visible.map((room) => <li key={room.kind + ':' + room.id}><button type="button" aria-current={selected === room.id && mode === room.kind ? 'page' : undefined} onClick={() => choose(room.kind, room.id)}><strong>{room.title || room.id}</strong><span>{t(room.kind === 'groups' ? 'Conversation' : 'Deliberation')} · {t(room.status)}</span></button></li>)}</ul>
        </div>
      </aside>
      <section className="fs-agora__main" aria-label={t('Coordination modes')}>
        <nav className="fs-agora__modes" aria-label={t('Coordination modes')}>{modes.map(({id, label, icon: Icon}) => <button key={id} type="button" aria-current={mode === id ? 'page' : undefined} onClick={() => choose(id)}><Icon size={16} aria-hidden="true" />{t(label)}</button>)}</nav>
        {mode === 'groups' && <GroupScreen key={spaceId} embedded />}
        {mode === 'council' && <CouncilScreen key={spaceId} embedded />}
        {mode === 'development' && <section className="fs-agora__development"><a className="fs-chip" href={'/api/hoard/agora/open?' + scope} target="_blank" rel="noopener">{t('Open in a separate window')}</a><iframe title={t('Tasks and decisions')} src={'/api/hoard/agora/open?' + scope + '&embedded=1'} /></section>}
      </section>
    </div>
  </div>;
}
