import type { ToolsOffered } from '../../adapters/chat';
import { t } from '../../i18n';

/**
 * OBJ-56 (#327): which tools this turn really offered the model and what they
 * weighed — `metadata.tools_offered` from src/turn_tool_offer.py. The summary
 * names the distinct tools over the turn and their tokens; open, it lists the
 * heaviest tools, the totals per source and the cost of each round (a turn
 * with several rounds pays the list once per round: `sentTokens`).
 */
export function ToolsOfferedLine({ offered }: { offered: ToolsOffered }) {
  const fmt = (n: number) => n.toLocaleString();
  const multi = offered.rounds.length > 1;
  return (
    <details className="fs-timeline" data-testid="turn-tools-offered">
      <summary>
        <span className="fs-timeline__title">
          {t('Tools offered')}: {fmt(offered.count)} · {fmt(offered.tokens)} {t('tokens')}
          {multi ? ` · ${fmt(offered.sentTokens)} ${t('tokens over')} ${offered.rounds.length} ${t('rounds')}` : ''}
        </span>
      </summary>
      <div style={{ display: 'grid', gap: 6, padding: '6px 2px', fontSize: 12 }}>
        {offered.heaviest.length > 0 && (
          <div>
            <strong>{t('Heaviest')}</strong>
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {offered.heaviest.map((tool) => (
                <li key={tool.name}>
                  <code>{tool.name}</code> · {fmt(tool.tokens)} {t('tokens')}{tool.source && tool.source !== 'builtin' ? ` · ${tool.source}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
        {offered.bySource.length > 1 && (
          <div>
            <strong>{t('By source')}</strong>{' '}
            {offered.bySource.map((s) => `${s.source} ${s.count} (${fmt(s.tokens)})`).join(' · ')}
          </div>
        )}
        {multi && (
          <div>
            <strong>{t('Per round')}</strong>{' '}
            {offered.rounds.map((r) => `#${r.round}: ${r.count} · ${fmt(r.tokens)}`).join(' — ')}
          </div>
        )}
      </div>
    </details>
  );
}
