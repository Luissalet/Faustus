import { Link2, Tag } from 'lucide-react';
import type { ReadNote } from '../../adapters/brain';
import { plainText } from '../../lib/wikilinks';
import { EntityPanel } from './EntityPanel';
import { t, tn } from '../../i18n';

/**
 * The right pane: what points at this note, what it points at, its tags,
 * and — only for an entity note — the relations-over-time panel. Kept as
 * one component so `Brain.tsx` does not have to branch on kind itself.
 */
export function RightPanel({ note, onOpenNote, onEntityChanged }: { note: ReadNote; onOpenNote: (path: string) => void; onEntityChanged?: () => void }) {
  const entityId = note.kind === 'entity' && note.source.startsWith('ent:') ? note.source.slice(4) : null;

  return (
    <aside className="fs-brain__right" aria-label={t('Note details')} data-testid="brain-right-panel">
      {entityId && <EntityPanel entityId={entityId} onChanged={onEntityChanged} />}

      <section className="fs-brain__right-section">
        <h4>
          <Link2 size={13} aria-hidden="true" /> {t('Outgoing links')} <span className="fs-muted">({note.links.length})</span>
        </h4>
        {note.links.length === 0 && <p className="fs-muted">{t('This note links to nothing yet.')}</p>}
        <ul className="fs-brain__link-list">
          {note.links.map((link, i) => (
            <li key={i}>
              {link.resolved && link.path ? (
                <button type="button" className="fs-brain__link-item" onClick={() => onOpenNote(link.path!)}>
                  {link.label}
                </button>
              ) : (
                <span className="fs-brain__unresolved-chip" title={t('Not created yet')}>
                  {link.label}
                </span>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="fs-brain__right-section">
        <h4>
          <Link2 size={13} aria-hidden="true" /> {t('Backlinks')} <span className="fs-muted">({note.backlinks.length})</span>
        </h4>
        {note.backlinks.length === 0 && <p className="fs-muted">{t('Nothing links here yet.')}</p>}
        <ul className="fs-brain__link-list">
          {note.backlinks.map((bl, i) => (
            <li key={i}>
              <button type="button" className="fs-brain__link-item" onClick={() => onOpenNote(bl.path)}>
                {bl.title}
              </button>
              {bl.context && <p className="fs-muted fs-brain__backlink-context">{plainText(bl.context)}</p>}
            </li>
          ))}
        </ul>
      </section>

      {note.tags.length > 0 && (
        <section className="fs-brain__right-section">
          <h4>
            <Tag size={13} aria-hidden="true" /> {tn(note.tags.length, '{n} tag', '{n} tags')}
          </h4>
          <p className="fs-brain__tag-row">
            {note.tags.map((tag) => (
              <span key={tag} className="fs-brain__tag-chip">
                #{tag}
              </span>
            ))}
          </p>
        </section>
      )}
    </aside>
  );
}
