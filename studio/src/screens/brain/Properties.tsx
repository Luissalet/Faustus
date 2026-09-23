import { useState } from 'react';
import type { ReadNote } from '../../adapters/brain';
import { locale, t } from '../../i18n';

/**
 * The note's frontmatter, as a table: every key the vault stores, and — for
 * a memory or entity note — the handful the contract lets a person edit
 * directly (everything else about those notes flows from the store they
 * mirror, not from hand-editing YAML). A change here calls `onChange` with
 * the whole patched frontmatter object; `Brain.tsx` recomposes the file and
 * saves it, same as an edit to the body.
 */

const MEMORY_TYPES = ['preference', 'fact', 'procedure', 'decision', 'anti_pattern'];
const ENTITY_TYPES = ['person', 'project', 'organization', 'place', 'tool', 'concept', 'event', 'other'];

function dateValue(v: unknown): string {
  const s = typeof v === 'string' ? v : '';
  return s.slice(0, 10);
}

/** A stray field always shown read-only never gets an editable date input,
 *  so unlike `dateValue` above it can read as a locale date rather than the
 *  raw ISO timestamp the frontmatter stores it as. */
const READ_ONLY_DATE_KEYS = new Set(['created', 'updated']);

function readableDate(v: unknown): string {
  const s = typeof v === 'string' ? v : '';
  const at = Date.parse(s);
  if (Number.isNaN(at)) return s;
  return new Date(at).toLocaleDateString(locale(), { dateStyle: 'medium' });
}

export function Properties({ note, onChange }: { note: ReadNote; onChange: (patch: Record<string, unknown>) => void }) {
  const fm = note.frontmatter;
  const [open, setOpen] = useState(true);
  const editable = note.editable && (note.kind === 'memory' || note.kind === 'entity');
  const otherKeys = Object.keys(fm).filter((k) => !['id', 'source', 'kind', 'tags', 'aliases', 'valid_from', 'valid_until', 'type', 'pinned'].includes(k) && fm[k] !== undefined && fm[k] !== null && fm[k] !== '');

  return (
    <details className="fs-brain__properties" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)} data-testid="brain-properties">
      <summary>{t('Properties')}</summary>
      <table className="fs-brain__prop-table">
        <tbody>
          {note.kind === 'memory' && (
            <>
              <tr>
                <th>{t('Type')}</th>
                <td>
                  {editable ? (
                    <select value={String(fm.type ?? '')} onChange={(e) => onChange({ type: e.target.value })}>
                      {MEMORY_TYPES.map((tp) => (
                        <option key={tp} value={tp}>{tp}</option>
                      ))}
                    </select>
                  ) : (
                    String(fm.type ?? '—')
                  )}
                </td>
              </tr>
              <tr>
                <th>{t('Valid from')}</th>
                <td>{editable ? <input type="date" value={dateValue(fm.valid_from)} onChange={(e) => onChange({ valid_from: e.target.value || null })} /> : dateValue(fm.valid_from) || '—'}</td>
              </tr>
              <tr>
                <th>{t('Valid until')}</th>
                <td>{editable ? <input type="date" value={dateValue(fm.valid_until)} onChange={(e) => onChange({ valid_until: e.target.value || null })} /> : dateValue(fm.valid_until) || '—'}</td>
              </tr>
              <tr>
                <th>{t('Pinned')}</th>
                <td>
                  {editable ? (
                    <input type="checkbox" checked={Boolean(fm.pinned)} onChange={(e) => onChange({ pinned: e.target.checked })} />
                  ) : (
                    fm.pinned ? t('yes') : t('no')
                  )}
                </td>
              </tr>
            </>
          )}
          {note.kind === 'entity' && (
            <>
              <tr>
                <th>{t('Type')}</th>
                <td>
                  {editable ? (
                    <select value={String(fm.type ?? '')} onChange={(e) => onChange({ type: e.target.value })}>
                      {ENTITY_TYPES.map((tp) => (
                        <option key={tp} value={tp}>{tp}</option>
                      ))}
                    </select>
                  ) : (
                    String(fm.type ?? '—')
                  )}
                </td>
              </tr>
              <tr>
                <th>{t('Aliases')}</th>
                <td>
                  {editable ? (
                    <input
                      type="text"
                      value={Array.isArray(fm.aliases) ? fm.aliases.join(', ') : ''}
                      onChange={(e) => onChange({ aliases: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })}
                      placeholder={t('comma-separated')}
                    />
                  ) : (
                    Array.isArray(fm.aliases) && fm.aliases.length ? fm.aliases.join(', ') : '—'
                  )}
                </td>
              </tr>
            </>
          )}
          {note.tags.length > 0 && (
            <tr>
              <th>{t('Tags')}</th>
              <td>{note.tags.map((tg) => `#${tg}`).join(' ')}</td>
            </tr>
          )}
          {otherKeys.map((key) => (
            <tr key={key}>
              <th>{key}</th>
              <td>{Array.isArray(fm[key]) ? (fm[key] as unknown[]).join(', ') : READ_ONLY_DATE_KEYS.has(key) ? readableDate(fm[key]) : String(fm[key])}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
