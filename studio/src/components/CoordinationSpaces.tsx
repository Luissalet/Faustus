import {useEffect, useRef, useState} from 'react';
import {Link} from 'react-router';
import {Plus, X} from 'lucide-react';
import {Button} from './Button';
import {createSpace, spaces, type CoordinationSpace} from '../adapters/coordination';
import {t} from '../i18n';

export function CoordinationSpaces({value, onChange, homeLink = true}: {value: string; onChange: (id: string) => void; homeLink?: boolean}) {
  const [all, setAll] = useState<CoordinationSpace[]>([]);
  const [error, setError] = useState('');
  const [title, setTitle] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const active = useRef(value);
  active.current = value;
  useEffect(() => {
    const controller = new AbortController();
    spaces(controller.signal).then(setAll).catch((error: Error) => {if (!controller.signal.aborted) setError(error.message);});
    return () => controller.abort();
  }, []);
  async function create() {
    if (!title?.trim()) return;
    const startedIn = value;
    setBusy(true);
    try {
      const created = await createSpace(title.trim());
      setAll((current) => [...current, created]); setTitle(null); setError('');
      if (active.current === startedIn) onChange(created.id);
    } catch (error) {setError((error as Error).message);}
    finally {setBusy(false);}
  }
  return <section className="fs-inline" aria-label={t('Coordination spaces')} data-testid="coordination-spaces">
    <label className="fs-inline">{t('Agora')} <select className="fs-field" value={value} onChange={(event) => onChange(event.target.value)} aria-label={t('Agora')}>
      {!all.length && <option value={value}>{value === 'ecosystem' ? t('Ecosystem') : value}</option>}
      {all.map((space) => <option key={space.id} value={space.id}>{space.title}</option>)}
    </select></label>
    <Button size="sm" variant="ghost" icon={Plus} label={t('New Agora')} onClick={() => setTitle('')} />
    {homeLink && <Link className="fs-chip" to={'/agora?space_id=' + encodeURIComponent(value)}>{t('Agora')}</Link>}
    {title !== null && <form className="fs-inline" onSubmit={(event) => {event.preventDefault(); void create();}}>
      <label>{t('Title')} <input className="fs-field" value={title} onChange={(event) => setTitle(event.target.value)} autoFocus /></label>
      <Button size="sm" variant="primary" type="submit" label={t('Create')} loading={busy} />
      <Button size="sm" variant="ghost" icon={X} label={t('Cancel')} onClick={() => setTitle(null)} />
    </form>}
    {error && <span className="fs-prose" role="status">{error}</span>}
  </section>;
}
