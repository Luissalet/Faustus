import { useEffect, useState } from 'react';
import { PenTool, Plus, Table2 } from 'lucide-react';
import { Button } from '../../components';
import { createBoard, createSheet, listBoards, listSheets, type SurfaceMeta } from '../../adapters/surfaces';
import { t } from '../../i18n';

/** What a surface tab shows before one is open: the chat's whiteboards or spreadsheets and a way to start a new one. */
export default function SurfaceHome({ kind, sessionId, onOpen }: { kind: 'pizarra' | 'hoja'; sessionId: string | null; onOpen: (id: string, title: string) => void }) {
  const [items, setItems] = useState<SurfaceMeta[] | null>(null);
  const [busy, setBusy] = useState(false);
  const Icon = kind === 'pizarra' ? PenTool : Table2;
  useEffect(() => {
    let live = true;
    if (!sessionId) { setItems([]); return; }
    const load = kind === 'pizarra' ? listBoards(sessionId).then((r) => r.boards) : listSheets(sessionId).then((r) => r.sheets);
    load.then((rows) => { if (live) setItems(rows); }).catch(() => { if (live) setItems([]); });
    return () => { live = false; };
  }, [kind, sessionId]);
  const make = async () => {
    if (!sessionId || busy) return;
    setBusy(true);
    try {
      const m = kind === 'pizarra' ? await createBoard(sessionId, t('Untitled whiteboard')) : await createSheet(sessionId, t('Untitled spreadsheet'));
      onOpen(m.id, m.title);
    } finally { setBusy(false); }
  };
  return (
    <div className="fs-panel__body fs-surfacehome" data-testid={kind + '-home'}>
      <h3><Icon size={15} aria-hidden="true" /> {kind === 'pizarra' ? t('Whiteboards') : t('Spreadsheets')}</h3>
      {!sessionId && <p className="fs-studio__hint">{kind === 'pizarra' ? t('Start a conversation first; the whiteboard belongs to it.') : t('Start a conversation first; the spreadsheet belongs to it.')}</p>}
      {sessionId && items && items.length === 0 && <p className="fs-studio__hint">{kind === 'pizarra' ? t('No whiteboards in this conversation yet.') : t('No spreadsheets in this conversation yet.')}</p>}
      {items && items.length > 0 && (
        <ul className="fs-surfacehome__list">
          {items.map((b) => (
            <li key={b.id}>
              <button type="button" onClick={() => onOpen(b.id, b.title)} data-testid={kind + '-home-item'}>
                {b.title}<small> · {kind === 'pizarra' ? t('{n} items', { n: b.nodes ?? 0 }) : t('{n} cells', { n: b.cells ?? 0 })}</small>
              </button>
            </li>
          ))}
        </ul>
      )}
      {sessionId && <Button size="sm" variant="primary" icon={Plus} label={kind === 'pizarra' ? t('New whiteboard') : t('New spreadsheet')} disabled={busy} onClick={() => void make()} testId={kind + '-new'} />}
    </div>
  );
}
