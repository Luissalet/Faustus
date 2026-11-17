import { ListChecks, MessageSquareQuote } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { brainBacklinks, type BrainBacklink } from '../../adapters/agoraThreads';
import { t, tn } from '../../i18n';
import './agora-threads.css';

/** On a Brain page: the messages and tasks of the Agora that name it with `[[Page]]`. */
export function AgoraBacklinks({ title, path }: { title: string; path: string }) {
  const [rows, setRows] = useState<BrainBacklink[] | null>(null);
  const navigate = useNavigate();
  useEffect(() => {
    const controller = new AbortController();
    setRows(null);
    brainBacklinks(title, [path.replace(/\.md$/i, '')], controller.signal).then((out) => setRows(out.backlinks)).catch(() => { if (!controller.signal.aborted) setRows([]); });
    return () => controller.abort();
  }, [title, path]);
  if (rows === null || rows.length === 0) return null;     // a page nobody cites there shows nothing
  const open = (row: BrainBacklink) => {
    const view = row.kind === 'task' || !row.channel ? (row.thread_id ? 't:' + row.thread_id : '') : 'ch:' + row.channel;
    navigate('/agora?space_id=' + encodeURIComponent(row.space_id) + (view ? '&c=' + encodeURIComponent(view) : ''));
  };
  return (
    <section className="fs-brain__right-section" data-testid="brain-agora-backlinks">
      <h4><MessageSquareQuote size={13} aria-hidden="true" /> {t('Cited in the Agora')} <span className="fs-muted">({rows.length})</span></h4>
      <ul className="fs-brain__link-list">
        {rows.map((row) => (
          <li key={row.kind + row.id}>
            <button type="button" className="fs-brain__link-item" onClick={() => open(row)}>
              {row.kind === 'task' ? <><ListChecks size={12} aria-hidden="true" /> #{row.id} {row.title}</> : <>{row.author}{row.channel ? ' · #' + row.channel : ''}</>}
            </button>
            {row.kind === 'message' && row.excerpt && <p className="fs-muted fs-brain__backlink-context">{row.excerpt}</p>}
            <p className="fs-muted fs-brain__backlink-context">{row.space_id}{row.kind === 'task' && row.status ? ' · ' + row.status : ''}</p>
          </li>
        ))}
      </ul>
      <span className="fs-visually-hidden">{tn(rows.length, '{n} citation', '{n} citations')}</span>
    </section>
  );
}
