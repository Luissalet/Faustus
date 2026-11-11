import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { PenTool } from 'lucide-react';
import { listBoards, type SurfaceMeta } from '../../adapters/surfaces';
import { t } from '../../i18n';
import './whiteboard-shelf.css';

/** The whiteboards of every conversation, next to the documents: opening one shows it beside its chat. */
export function WhiteboardShelf({ query }: { query: string }) {
  const navigate = useNavigate();
  const [boards, setBoards] = useState<SurfaceMeta[]>([]);
  useEffect(() => {
    let live = true;
    listBoards('').then((r) => { if (live) setBoards(r.boards); }).catch(() => { if (live) setBoards([]); });
    return () => { live = false; };
  }, []);
  const q = query.trim().toLowerCase();
  const shown = boards.filter((b) => b.session_id && (!q || b.title.toLowerCase().includes(q)));
  if (!shown.length) return null;
  return (
    <section className="fs-lib__shelf" aria-label={t('Whiteboards')} data-testid="library-whiteboards">
      <h3 className="fs-lib__shelf-title"><PenTool size={14} aria-hidden="true" /> {t('Whiteboards')}</h3>
      <ul className="fs-lib__list">
        {shown.slice(0, 12).map((b) => (
          <li key={b.id} className="fs-lib__row">
            <button type="button" className="fs-lib__shelf-item" data-testid="library-whiteboard" onClick={() => navigate(`/studio?s=${encodeURIComponent(b.session_id ?? '')}&pizarra=${encodeURIComponent(b.id)}`)}>
              <strong>{b.title}</strong>
              <small> · {t('{n} items', { n: b.nodes ?? 0 })}</small>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
