import { Check, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Button } from '../../components';
import { applyChunks, diffChunks, diffSummary, lineDiff, wordDiff, type DiffChunk } from '../../lib/diff';
import { t, tn } from '../../i18n';

/**
 * Review mode: the difference between two texts as chunks to accept or keep,
 * with the changed words highlighted inside each chunk. "Apply" hands back
 * the merged text.
 */
export function DiffView({ oldText, newText, oldLabel, newLabel, onApply, onCancel }: { oldText: string; newText: string; oldLabel: string; newLabel: string; onApply: (text: string) => void; onCancel: () => void }) {
  const entries = useMemo(() => lineDiff(oldText, newText), [oldText, newText]);
  const [chunks, setChunks] = useState<DiffChunk[]>(() => (entries ? diffChunks(entries) : []));
  if (!entries) {
    return (
      <div className="fs-docs__diff">
        <p className="fs-notice" data-tone="warning">
          {t('These texts are too long to compare line by line.')}
        </p>
        <Button variant="ghost" label={t('Back')} onClick={onCancel} />
      </div>
    );
  }
  const summary = diffSummary(entries);
  const decide = (id: number, accepted: boolean) => setChunks((cur) => cur.map((c) => (c.id === id ? { ...c, resolved: true, accepted } : c)));
  const decideAll = (accepted: boolean) => setChunks((cur) => cur.map((c) => ({ ...c, resolved: true, accepted })));
  const pending = chunks.filter((c) => !c.resolved).length;

  return (
    <div className="fs-docs__diff" data-testid="doc-diff">
      <div className="fs-docs__diff-bar">
        <span>
          {t('{old} → {new}', { old: oldLabel, new: newLabel })} · <span className="fs-diff-add">+{summary.added}</span> <span className="fs-diff-del">−{summary.removed}</span> · {pending ? tn(pending, '{n} change to decide', '{n} changes to decide') : t('All decided')}
        </span>
        <span className="fs-spacer" />
        <Button size="sm" variant="ghost" label={t('Take all')} onClick={() => decideAll(true)} />
        <Button size="sm" variant="ghost" label={t('Keep all')} onClick={() => decideAll(false)} />
        <Button size="sm" variant="ghost" label={t('Cancel')} onClick={onCancel} />
        <Button size="sm" variant="primary" label={t('Apply')} disabled={pending > 0} onClick={() => onApply(applyChunks(entries, chunks))} />
      </div>
      <div className="fs-docs__diff-body">
        {entries.map((e, i) => {
          const chunk = chunks.find((c) => c.at === i);
          if (e.type === 'equal') return <div key={i} className="fs-docs__diff-line">{e.line || ' '}</div>;
          if (!chunk) return null;
          const oldJoined = chunk.oldLines.join('\n'), newJoined = chunk.newLines.join('\n');
          const pieces = wordDiff(oldJoined, newJoined);
          return (
            <div key={i} className="fs-docs__chunk" data-state={chunk.resolved ? (chunk.accepted ? 'accepted' : 'kept') : undefined}>
              <div className="fs-docs__chunk-text">
                {chunk.oldLines.length > 0 && (
                  <pre className="fs-docs__chunk-old">
                    {pieces.filter((p) => p.type !== 'insert').map((p, k) => (p.type === 'delete' ? <mark key={k} className="fs-diff-del">{p.text}</mark> : <span key={k}>{p.text}</span>))}
                  </pre>
                )}
                {chunk.newLines.length > 0 && (
                  <pre className="fs-docs__chunk-new">
                    {pieces.filter((p) => p.type !== 'delete').map((p, k) => (p.type === 'insert' ? <mark key={k} className="fs-diff-add">{p.text}</mark> : <span key={k}>{p.text}</span>))}
                  </pre>
                )}
              </div>
              <div className="fs-docs__chunk-actions">
                <Button size="sm" variant={chunk.resolved && chunk.accepted ? 'primary' : 'secondary'} icon={Check} label={t('Take')} onClick={() => decide(chunk.id, true)} />
                <Button size="sm" variant={chunk.resolved && !chunk.accepted ? 'primary' : 'ghost'} icon={X} label={t('Keep mine')} onClick={() => decide(chunk.id, false)} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
