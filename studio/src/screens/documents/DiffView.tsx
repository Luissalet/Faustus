import { AlertTriangle, Check, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Button } from '../../components';
import { approxSize, applyChunks, diffChunks, DIFF_LARGE_FILE_CHARS, diffSummary, isFormatOnlyChunk, lineDiff, looksBinary, wordDiff, type DiffChunk } from '../../lib/diff';
import { t, tn } from '../../i18n';

/**
 * Review mode: the difference between two texts as chunks to accept or keep,
 * with the changed words highlighted inside each chunk. "Apply" hands back
 * the merged text. Binary content and files too large to diff line by line
 * get a dedicated notice instead of an attempted render (EDIT-03); a
 * formatting-only chunk (whitespace/EOL/BOM) can be collapsed out of the
 * review with the "ignore formatting" toggle. `readOnly` (a pure before/after
 * comparison, e.g. VER-06's review panel) hides the accept/keep/apply
 * controls — there is nothing here to merge back into an editor.
 */
export function DiffView({ oldText, newText, oldLabel, newLabel, onApply, onCancel, readOnly }: { oldText: string; newText: string; oldLabel: string; newLabel: string; onApply: (text: string) => void; onCancel: () => void; readOnly?: boolean }) {
  const binary = useMemo(() => looksBinary(oldText) || looksBinary(newText), [oldText, newText]);
  const tooLarge = oldText.length > DIFF_LARGE_FILE_CHARS || newText.length > DIFF_LARGE_FILE_CHARS;
  const entries = useMemo(() => (binary || tooLarge ? null : lineDiff(oldText, newText)), [oldText, newText, binary, tooLarge]);
  const [chunks, setChunks] = useState<DiffChunk[]>(() => (entries ? diffChunks(entries) : []));
  const [ignoreFormat, setIgnoreFormat] = useState(false);
  // Computed unconditionally (rules of hooks) even though it's only used past the early return below.
  const formatOnlyIds = useMemo(() => new Set(chunks.filter(isFormatOnlyChunk).map((c) => c.id)), [chunks]);

  if (binary || !entries) {
    return (
      <div className="fs-docs__diff" data-testid="doc-diff" data-mode={binary ? 'binary' : 'too-large'}>
        <p className="fs-notice" data-tone="warning">
          <AlertTriangle size={14} aria-hidden="true" />{' '}
          {binary ? t('This looks like binary content — line-by-line differences would not be meaningful.') : t('These texts are too long to compare line by line.')}
        </p>
        <p className="fs-docs__diff-sizes">{t('{old}: {a} · {new}: {b}', { old: oldLabel, a: approxSize(oldText), new: newLabel, b: approxSize(newText) })}</p>
        <Button variant="ghost" label={t('Back')} onClick={onCancel} />
      </div>
    );
  }
  const summary = diffSummary(entries);
  const decide = (id: number, accepted: boolean) => setChunks((cur) => cur.map((c) => (c.id === id ? { ...c, resolved: true, accepted } : c)));
  const decideAll = (accepted: boolean) => setChunks((cur) => cur.map((c) => (ignoreFormat && formatOnlyIds.has(c.id) ? c : { ...c, resolved: true, accepted })));
  /* Under "ignore formatting", a formatting-only chunk is treated as already
   * decided (take the reformatted version) so it never blocks Apply and
   * never counts as a change left to review. */
  const effectiveChunks = chunks.map((c) => (ignoreFormat && formatOnlyIds.has(c.id) ? { ...c, resolved: true, accepted: true } : c));
  const pending = effectiveChunks.filter((c) => !c.resolved).length;

  return (
    <div className="fs-docs__diff" data-testid="doc-diff">
      <div className="fs-docs__diff-bar">
        <span>
          {t('{old} → {new}', { old: oldLabel, new: newLabel })} · <span className="fs-diff-add">+{summary.added}</span> <span className="fs-diff-del">−{summary.removed}</span> · {pending ? tn(pending, '{n} change to decide', '{n} changes to decide') : t('All decided')}
        </span>
        {formatOnlyIds.size > 0 && <span className="fs-docs__diff-formatcount">{tn(formatOnlyIds.size, '{n} formatting-only change', '{n} formatting-only changes')}</span>}
        <label className="fs-docs__diff-toggle">
          <input type="checkbox" checked={ignoreFormat} onChange={(e) => setIgnoreFormat(e.target.checked)} disabled={formatOnlyIds.size === 0} />
          {t('Ignore formatting')}
        </label>
        <span className="fs-spacer" />
        {readOnly ? (
          <Button size="sm" variant="ghost" label={t('Close')} onClick={onCancel} />
        ) : (
          <>
            <Button size="sm" variant="ghost" label={t('Take all')} onClick={() => decideAll(true)} />
            <Button size="sm" variant="ghost" label={t('Keep all')} onClick={() => decideAll(false)} />
            <Button size="sm" variant="ghost" label={t('Cancel')} onClick={onCancel} />
            <Button size="sm" variant="primary" label={t('Apply')} disabled={pending > 0} onClick={() => onApply(applyChunks(entries, effectiveChunks))} />
          </>
        )}
      </div>
      <div className="fs-docs__diff-body">
        {entries.map((e, i) => {
          const chunk = chunks.find((c) => c.at === i);
          if (e.type === 'equal') return <div key={i} className="fs-docs__diff-line">{e.line || ' '}</div>;
          if (!chunk) return null;
          if (ignoreFormat && formatOnlyIds.has(chunk.id)) {
            if (i !== chunk.at) return null;
            return (
              <div key={i} className="fs-docs__chunk fs-docs__chunk--format" data-state="format-only">
                {tn(chunk.oldLines.length + chunk.newLines.length, '{n} formatting-only line hidden (whitespace/EOL)', '{n} formatting-only lines hidden (whitespace/EOL)')}
              </div>
            );
          }
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
              {!readOnly && (
                <div className="fs-docs__chunk-actions">
                  <Button size="sm" variant={chunk.resolved && chunk.accepted ? 'primary' : 'secondary'} icon={Check} label={t('Take')} onClick={() => decide(chunk.id, true)} />
                  <Button size="sm" variant={chunk.resolved && !chunk.accepted ? 'primary' : 'ghost'} icon={X} label={t('Keep mine')} onClick={() => decide(chunk.id, false)} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
