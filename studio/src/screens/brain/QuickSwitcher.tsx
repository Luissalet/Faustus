import { CalendarDays, FileText, Search } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { NoteSummary } from '../../adapters/brain';
import { fuzzySearch } from '../../lib/wikilinks';
import { t } from '../../i18n';

/**
 * Ctrl+O: fuzzy-jump to any note by title, or straight to today's daily
 * note. Pure client-side over the already-fetched tree — the same list the
 * explorer draws from, so this never issues its own request.
 */
export function QuickSwitcher({ notes, onPick, onDaily, onClose }: { notes: NoteSummary[]; onPick: (path: string) => void; onDaily: () => void; onClose: () => void }) {
  const [query, setQuery] = useState('');
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const hits = fuzzySearch(query, notes, (n) => n.title, 30);
  const showDaily = !query.trim() || t("Today's note").toLowerCase().includes(query.trim().toLowerCase());

  function commit(path: string) {
    onPick(path);
    onClose();
  }

  return (
    <div className="fs-brain__switcher-backdrop" role="presentation">
      <button type="button" className="fs-brain__scrim" aria-label={t('Close')} tabIndex={-1} onClick={onClose} />
      <div
        className="fs-brain__switcher"
        role="dialog"
        aria-modal="true"
        aria-label={t('Quick switcher')}
        data-testid="brain-quick-switcher"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          const total = hits.length + (showDaily ? 1 : 0);
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            setIndex((i) => Math.min(total - 1, i + 1));
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setIndex((i) => Math.max(0, i - 1));
          } else if (e.key === 'Enter') {
            e.preventDefault();
            if (showDaily && index === 0) {
              onDaily();
              onClose();
            } else {
              const pick = hits[showDaily ? index - 1 : index];
              if (pick) commit(pick.path);
            }
          } else if (e.key === 'Escape') {
            onClose();
          }
        }}
      >
        <label className="fs-search fs-brain__switcher-input">
          <Search size={14} aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setIndex(0);
            }}
            placeholder={t('Jump to a note…')}
            aria-label={t('Jump to a note')}
          />
        </label>
        <ul className="fs-brain__switcher-list">
          {showDaily && (
            <li>
              <button type="button" data-active={index === 0 || undefined} onClick={() => { onDaily(); onClose(); }}>
                <CalendarDays size={14} aria-hidden="true" /> {t("Today's note")}
              </button>
            </li>
          )}
          {hits.map((note, i) => {
            const pos = showDaily ? i + 1 : i;
            return (
              <li key={note.path}>
                <button type="button" data-active={index === pos || undefined} onClick={() => commit(note.path)}>
                  <FileText size={14} aria-hidden="true" />
                  <span>{note.title}</span>
                  <span className="fs-muted">{note.path}</span>
                </button>
              </li>
            );
          })}
          {hits.length === 0 && !showDaily && <li className="fs-muted fs-brain__switcher-empty">{t('No note matches "{query}".', { query })}</li>}
        </ul>
      </div>
    </div>
  );
}
