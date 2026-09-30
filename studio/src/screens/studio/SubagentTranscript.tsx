import { ArrowLeft } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { fetchSubagentTranscript, type SubagentTranscript as Transcript, type TranscriptMessage } from '../../adapters/agents';
import { t, tn } from '../../i18n';

/**
 * Read-only transcript of one sub-agent: its messages, every tool call with
 * its result, and where its tokens went (one bar per recorded model call,
 * darker = heavier). Nothing on this panel can change the worker.
 */

const fmtTok = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v));

export function heatLevel(heat: number): 0 | 1 | 2 | 3 | 4 {
  if (!(heat > 0)) return 0;
  if (heat < 0.25) return 1;
  if (heat < 0.5) return 2;
  if (heat < 0.8) return 3;
  return 4;
}

function roleLabel(role: string): string {
  if (role === 'user') return t('Task');
  if (role === 'assistant') return t('Sub-agent');
  return role;
}

function Message({ m }: { m: TranscriptMessage }) {
  return (
    <article className="fs-sat__msg" data-role={m.role}>
      <header className="fs-sat__msg-head">
        <strong>{roleLabel(m.role)}</strong>
        {m.model && <code>{m.model}</code>}
        {m.tokens && <span className="fs-sa__muted">{fmtTok(m.tokens.input)} in · {fmtTok(m.tokens.output)} out</span>}
      </header>
      {m.content && <pre className="fs-sat__text">{m.content}</pre>}
      {m.contentTruncated && <p className="fs-sa__muted">{t('Shortened: {n} characters in total.', { n: m.contentChars })}</p>}
      {m.toolEvents.map((e, i) => (
        <details key={i} className="fs-sat__tool" data-ok={e.exitCode === null ? undefined : e.exitCode === 0}>
          <summary>
            <code>{e.tool}</code>
            {e.round !== null && <span className="fs-sa__muted"> r{e.round}</span>}
            <span className="fs-sat__cmd">{e.input}</span>
            {e.exitCode !== null && e.exitCode !== 0 && <span className="fs-sat__bad"> ✗ {e.exitCode}</span>}
            {e.durationMs !== null && <span className="fs-sa__muted"> {Math.round(e.durationMs)} ms</span>}
          </summary>
          {e.input && <pre className="fs-sat__text">{e.input}</pre>}
          {e.output && <pre className="fs-sat__text">{e.output}</pre>}
          {e.outputTruncated && <p className="fs-sa__muted">{t('Shortened: {n} characters in total.', { n: e.outputChars })}</p>}
        </details>
      ))}
      {m.toolEventsOmitted > 0 && <p className="fs-sa__muted">{tn(m.toolEventsOmitted, '{n} more tool call not shown', '{n} more tool calls not shown')}</p>}
    </article>
  );
}

export default function SubagentTranscript({ sessionId, title, onBack }: { sessionId: string; title: string; onBack: () => void }) {
  const [data, setData] = useState<Transcript | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);

  useEffect(() => {
    const ctl = new AbortController();
    setData(null);
    setError(null);
    fetchSubagentTranscript(sessionId, 0, ctl.signal)
      .then(setData)
      .catch((e: Error) => {
        if (!ctl.signal.aborted) setError(e.message || t('Could not load the transcript.'));
      });
    return () => ctl.abort();
  }, [sessionId]);

  const loadMore = async () => {
    if (!data) return;
    setMore(true);
    try {
      const next = await fetchSubagentTranscript(sessionId, data.offset + data.messages.length);
      setData({ ...next, offset: data.offset, messages: [...data.messages, ...next.messages] });
    } catch (e) {
      setError((e as Error).message || t('Could not load the transcript.'));
    } finally {
      setMore(false);
    }
  };

  return (
    <section className="fs-sat" data-testid="subagent-transcript">
      <header className="fs-sat__head">
        <Button size="sm" icon={ArrowLeft} label={t('Back to sub-agents')} onClick={onBack} />
        <strong>{title}</strong>
        <span className="fs-sa__muted">{t('read-only')}</span>
      </header>
      {error && <p className="fs-sat__bad" role="alert">{error}</p>}
      {!data && !error && <p className="fs-sa__muted">{t('Loading…')}</p>}
      {data && (
        <>
          <div className="fs-sat__usage" data-testid="subagent-token-heat">
            {data.usage.source === 'none' ? (
              <span className="fs-sa__muted">{t('No token counts were recorded for this sub-agent.')}</span>
            ) : (
              <>
                <span>{fmtTok(data.usage.inputTokens)} in · {fmtTok(data.usage.outputTokens)} out · {fmtTok(data.usage.totalTokens)} {t('total')}</span>
                {data.usage.calls.length > 0 && (
                  <span className="fs-sat__bars" role="img" aria-label={t('Tokens per model call')}>
                    {data.usage.calls.map((c, i) => (
                      <span
                        key={i}
                        className="fs-sat__bar"
                        data-heat={heatLevel(c.heat)}
                        title={`#${c.seq ?? i + 1} · ${fmtTok(c.input)} in · ${fmtTok(c.output)} out${c.cached ? ` · ${fmtTok(c.cached)} ${t('cached')}` : ''}${c.model ? ` · ${c.model}` : ''}`}
                      />
                    ))}
                  </span>
                )}
                {data.usage.source === 'messages' && <span className="fs-sa__muted">{t('From the per-turn counts (model calls were not traced).')}</span>}
              </>
            )}
          </div>
          {data.messages.length === 0 && <p className="fs-sa__muted">{t('This sub-agent has no messages yet.')}</p>}
          {data.messages.map((m) => <Message key={m.index} m={m} />)}
          {data.hasMoreAfter && <Button size="sm" label={t('Load more')} loading={more} onClick={() => void loadMore()} />}
        </>
      )}
    </section>
  );
}
