import { useMemo } from 'react';
import { Eye, EyeOff, Pin } from 'lucide-react';
import { t } from '../../i18n';
import type { ContextOverrides } from '../../adapters/chat';

/**
 * UX-07: control de contexto por turno.
 *
 * `@file` mentions already exist in the draft (Composer.tsx's own `@`
 * picker); this panel is what turns "the agent will read whatever it can
 * reach" into something the sender can see and narrow BEFORE sending — pin a
 * source so a budget cut can't drop it, or exclude one for this turn only,
 * without touching the global `noMemory`/`noSkills` toggles that already
 * live on `Knobs`.
 *
 * This panel only ever narrows. `excludeSources`/`excludeProjectMemory`/
 * `excludeSkills` remove; nothing here can add a source the normal turn
 * would not already have offered — matching the backend requirement text
 * ("sin modificar globales").
 *
 * The wire half (`ContextOverrides` travelling as `context_overrides` on
 * `sendTurn`) is already additive in adapters/chat.ts. Actually enforcing an
 * exclusion end to end — refusing to recover an excluded file even from a
 * derived summary/cache — is server work in the context engine and
 * `chat_routes.py`, which this lot does not own; see the batch report for
 * the exact call this needs once Studio.tsx forwards `knobs.contextOverrides`
 * into `sendTurn`.
 */

const MENTION_ALL = /(^|\s)@([^\s@]+)/g;
// A hand-typed mention often runs straight into sentence punctuation
// ("see @docs/spec.md, then…"); Composer.tsx's own picker never inserts
// these because it always appends a trailing space, so only free-typed
// mentions need this trimmed off the path.
const TRAILING_PUNCTUATION = /[,.;:!?)\]}]+$/;

/** Every distinct `@path` mentioned in the draft, in first-seen order. */
export function mentionedSources(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  MENTION_ALL.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = MENTION_ALL.exec(text))) {
    const path = m[2].replace(TRAILING_PUNCTUATION, '');
    if (path && !seen.has(path)) {
      seen.add(path);
      out.push(path);
    }
  }
  return out;
}

/** Drop overrides that no longer refer to anything mentioned in the draft —
 *  called when the draft changes so a stale exclusion from a deleted `@file`
 *  doesn't silently keep suppressing a source the sender can no longer see. */
export function pruneOverrides(overrides: ContextOverrides, draft: string): ContextOverrides {
  const live = new Set(mentionedSources(draft));
  const excludeSources = (overrides.excludeSources ?? []).filter((p) => live.has(p));
  const pinSources = (overrides.pinSources ?? []).filter((p) => live.has(p));
  return { ...overrides, excludeSources, pinSources };
}

export interface ContextPanelProps {
  draft: string;
  overrides: ContextOverrides;
  onChange: (next: ContextOverrides) => void;
  /** knobs.inputTokenBudget — the same soft budget the composer already offers. */
  tokenBudget?: number;
}

export function ContextPanel({ draft, overrides, onChange, tokenBudget }: ContextPanelProps) {
  const sources = useMemo(() => mentionedSources(draft), [draft]);
  const excluded = new Set(overrides.excludeSources ?? []);
  const pinned = new Set(overrides.pinSources ?? []);

  const toggleExclude = (path: string) => {
    const nextExcluded = new Set(excluded);
    const nextPinned = new Set(pinned);
    if (nextExcluded.has(path)) nextExcluded.delete(path);
    else {
      nextExcluded.add(path);
      nextPinned.delete(path);
    }
    onChange({ ...overrides, excludeSources: [...nextExcluded], pinSources: [...nextPinned] });
  };

  const togglePin = (path: string) => {
    const nextPinned = new Set(pinned);
    const nextExcluded = new Set(excluded);
    if (nextPinned.has(path)) nextPinned.delete(path);
    else {
      nextPinned.add(path);
      nextExcluded.delete(path);
    }
    onChange({ ...overrides, pinSources: [...nextPinned], excludeSources: [...nextExcluded] });
  };

  if (sources.length === 0) return null; // nothing to narrow yet — no clutter for the common case

  return (
    <section className="fs-studio__context-panel" aria-label={t('What this turn will send')} data-testid="context-panel">
      <p className="fs-studio__context-lede">{t('What this turn will send — remote or local, this is the same list.')}</p>
      <ul className="fs-studio__context-chips">
        {sources.map((path) => (
          <li key={path} className="fs-studio__context-chip" data-excluded={excluded.has(path) || undefined} data-testid="context-panel-chip">
            <span className="fs-studio__context-chip-name">{path}</span>
            <button
              type="button"
              aria-pressed={pinned.has(path)}
              aria-label={pinned.has(path) ? t('Unpin {name}', { name: path }) : t('Pin {name}: kept even if the budget is tight', { name: path })}
              onClick={() => togglePin(path)}
              data-testid="context-panel-pin"
            >
              <Pin size={12} aria-hidden="true" />
            </button>
            <button
              type="button"
              aria-pressed={excluded.has(path)}
              aria-label={excluded.has(path) ? t('Include {name} again in this turn', { name: path }) : t('Exclude {name} from this turn', { name: path })}
              onClick={() => toggleExclude(path)}
              data-testid="context-panel-exclude"
            >
              {excluded.has(path) ? <EyeOff size={12} aria-hidden="true" /> : <Eye size={12} aria-hidden="true" />}
            </button>
          </li>
        ))}
      </ul>
      {tokenBudget !== undefined && (
        <p className="fs-studio__context-budget">{t('Soft budget for this turn: {n} tokens', { n: tokenBudget.toLocaleString() })}</p>
      )}
    </section>
  );
}
