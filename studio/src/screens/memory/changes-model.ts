import { MemoryUndoError, type MemoryChange, type MemoryStore } from '../../adapters/memoryJournal';
import { locale, t } from '../../i18n';

/**
 * The words and the grouping of the Changes tab (Memory → `?t=changes`),
 * without React so `tests/memory_changes.check.mjs` can run them in node.
 */

export const STORE_LABEL: Record<MemoryStore, string> = { facts: 'Facts', rules: 'Learned', instincts: 'Instincts', notes: 'Notes' };

// Spanish: a change is «cambio», so every label agrees in the masculine
// (Añadido / Cambiado / Borrado / Deshecho).
export const OP_LABEL: Record<MemoryChange['op'], string> = { create: 'Added', edit: 'Changed', delete: 'Deleted' };

/** Who made the change, as a person would say it. Unknown values show as written. */
export const SOURCE_LABEL: Record<string, string> = {
  ai_agent: 'Agent',
  agent_assertion: 'Agent',
  agent_validated: 'Agent',
  memory_extractor: 'Memory extractor',
  instinct_extractor: 'Instinct extractor',
  user: 'You',
  human_explicit: 'You',
  auto: 'Auto',
  restored: 'Restored',
  legacy_import: 'Legacy import',
  manual: 'Entered by hand',
  promoted: 'Promoted by use',
  'session-observation': 'Observed in a session',
  import: 'Imported from file',
  unknown: 'Unknown',
};

export function sourceLabel(source: string): string {
  if (!source) return '';
  return SOURCE_LABEL[source] ? t(SOURCE_LABEL[source]) : source.replace(/[_-]/g, ' ');
}

/** A change's time in the interface language. */
export function when(ts: number): string {
  const d = new Date(ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(locale(), { dateStyle: 'short', timeStyle: 'short' });
}

/** The refusal of an undo, worded in the interface language. */
export function undoMessage(exc: unknown): string {
  if (exc instanceof MemoryUndoError) {
    switch (exc.code) {
      case 'later_change': {
        const later = exc.blocking;
        if (later) {
          return t('Undo the later change first: {op} “{label}”.', {
            op: t(OP_LABEL[later.op] ?? 'Changed').toLowerCase(), label: later.label || later.id.slice(0, 8),
          });
        }
        break;
      }
      case 'changed_elsewhere': return t('The item changed outside the journal after this change, so it cannot be undone.');
      case 'conflict': return t('The item changed after this change, so it cannot be undone.');
      case 'duplicate': return t('The same text was added again under another id, so there is nothing to bring back.');
      case 'store_wiped': return t('This store was wiped after the change, so it cannot be undone.');
      case 'purged': return t('The memory was forgotten for good and its text erased, so this cannot be undone.');
      case 'truncated': return t('Only the start of this very long text was kept, so it cannot be undone.');
      case 'already_undone': return t('This change was already undone.');
      case 'unknown': return t('This change no longer exists.');
      case 'trash_gone': return t('The deleted note is no longer in the trash.');
      default: break;
    }
  }
  return (exc as Error)?.message || t('Could not undo it.');
}

export type Block = { turnId: string; rows: MemoryChange[] };

/** Rows that share a turn become one block, placed where the turn's newest change is. */
export function groupByTurn(changes: MemoryChange[]): Block[] {
  const blocks: Block[] = [];
  const byTurn = new Map<string, Block>();
  for (const change of changes) {
    if (!change.turnId) {
      blocks.push({ turnId: '', rows: [change] });
      continue;
    }
    const existing = byTurn.get(change.turnId);
    if (existing) {
      existing.rows.push(change);
    } else {
      const block = { turnId: change.turnId, rows: [change] };
      byTurn.set(change.turnId, block);
      blocks.push(block);
    }
  }
  return blocks;
}
