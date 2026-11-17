import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router';
import { loadNote, loadTree, type ReadNote } from '../../adapters/brain';
import { buildTitleIndex, foldTitle, parseWikiHref, plainText, rewriteWikilinks, type TitleIndex } from '../../lib/wikilinks';
import { t } from '../../i18n';
import { Rich } from '../rich';
import './agora-threads.css';

/** The vault's titles, asked once a minute at most: a message that names a page needs to know which one it is. */
let treeCache: { at: number; index: Promise<TitleIndex> } | null = null;
function vaultIndex(): Promise<TitleIndex> {
  if (!treeCache || Date.now() - treeCache.at > 60_000) {
    treeCache = { at: Date.now(), index: loadTree().then((tree) => withPaths(tree.notes)).catch(() => new Map() as TitleIndex) };
  }
  return treeCache.index;
}
/** A page is named by its title or by its path (`Folder/Title`, with or without `.md`). */
function withPaths(notes: Array<{ path: string; title: string }>): TitleIndex {
  const index = buildTitleIndex(notes);
  for (const note of notes) {
    const key = foldTitle(note.path.replace(/\.md$/i, ''));
    if (!index.has(key)) index.set(key, { path: note.path, title: note.title });
  }
  return index;
}
const noteCache = new Map<string, Promise<ReadNote | null>>();
function previewOf(path: string): Promise<ReadNote | null> {
  let found = noteCache.get(path);
  if (!found) { found = loadNote(path).catch(() => null); noteCache.set(path, found); window.setTimeout(() => noteCache.delete(path), 60_000); }
  return found;
}

interface Hover { href: string; label: string; x: number; y: number }

/** A message's text with its `[[Page]]` links alive: they open the page in the Brain and show a preview when hovered. */
export function BrainText({ text }: { text: string }) {
  const hasLinks = text.includes('[[');
  const [index, setIndex] = useState<TitleIndex | null>(null);
  const [hover, setHover] = useState<Hover | null>(null);
  const timer = useRef<number>(0);
  const navigate = useNavigate();
  useEffect(() => { if (!hasLinks) return; let alive = true; void vaultIndex().then((i) => { if (alive) setIndex(i); }); return () => { alive = false; }; }, [hasLinks]);
  const body = useMemo(() => (hasLinks ? rewriteWikilinks(text, index ?? new Map()) : text), [hasLinks, text, index]);
  const anchor = (event: MouseEvent): HTMLAnchorElement | null => {
    const a = (event.target as HTMLElement).closest('a');
    return a && parseWikiHref(a.getAttribute('href') || '') ? (a as HTMLAnchorElement) : null;
  };
  const leave = () => { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => setHover(null), 120); };
  if (!hasLinks) return <Rich text={text} />;
  return (
    <div className="fs-agt-brain" data-testid="agora-brain-text"
      onClick={(event) => {
        const a = anchor(event);
        const info = a && parseWikiHref(a.getAttribute('href') || '');
        if (!a || !info) return;
        event.preventDefault();
        navigate('/brain?note=' + encodeURIComponent(info.path));
      }}
      onMouseOver={(event) => {
        const a = anchor(event);
        if (!a) return;
        window.clearTimeout(timer.current);
        const box = a.getBoundingClientRect();
        timer.current = window.setTimeout(() => setHover({ href: a.getAttribute('href') || '', label: a.textContent || '', x: box.left, y: box.bottom + 6 }), 220);
      }}
      onMouseOut={leave}>
      <Rich text={body} />
      {hover && <BrainPreview hover={hover} onEnter={() => window.clearTimeout(timer.current)} onLeave={leave} />}
    </div>
  );
}

function BrainPreview({ hover, onEnter, onLeave }: { hover: Hover; onEnter: () => void; onLeave: () => void }) {
  const info = parseWikiHref(hover.href);
  const [note, setNote] = useState<ReadNote | null | undefined>(undefined);
  const resolved = info?.kind === 'note';
  useEffect(() => {
    let alive = true;
    setNote(undefined);
    if (!info || !resolved) { setNote(null); return; }
    void previewOf(info.path).then((n) => { if (alive) setNote(n); });
    return () => { alive = false; };
  }, [info?.path, resolved]);   // eslint-disable-line react-hooks/exhaustive-deps
  const excerpt = note ? plainText(note.content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '')).replace(/\s+/g, ' ').trim().slice(0, 420) : '';
  const left = Math.max(8, Math.min(hover.x, window.innerWidth - 360));
  return createPortal(
    <div className="fs-agt-preview" role="note" style={{ left, top: Math.min(hover.y, window.innerHeight - 180) }} onMouseEnter={onEnter} onMouseLeave={onLeave} data-testid="agora-brain-preview">
      <strong>{note?.title || info?.path.replace(/\.md$/, '').split('/').pop() || hover.label}</strong>
      {note === undefined && <p className="fs-muted">{t('Loading the page…')}</p>}
      {note === null && <p className="fs-muted">{resolved ? t('That page cannot be read now.') : t('The Brain has no page with that name yet.')}</p>}
      {note && <p>{excerpt || t('The page is empty.')}</p>}
      {note && note.tags.length > 0 && <p className="fs-muted">{note.tags.slice(0, 6).map((tag) => '#' + tag).join(' ')}</p>}
    </div>,
    document.getElementById('fs-overlay-root') ?? document.body,
  );
}
