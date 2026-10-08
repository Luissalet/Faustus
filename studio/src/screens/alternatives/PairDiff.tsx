import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { ArrowLeftRight, RefreshCw, TriangleAlert } from 'lucide-react';
import { Button, Skeleton } from '../../components';
import * as api from '../../adapters/alternatives';
import { t } from '../../i18n';
import {
  defaultPair, filterFiles, formatBytes, keepSelection, moveIndex, noDiffReason, overlapTone, parseUnifiedDiff,
  pickSide, statusCounts, statusGlyph, swapPair, toSideBySide, type StatusFilter,
} from '../../lib/altpair';

/**
 * PairDiff (OBJ-47) — one alternative against another, not each against the
 * base. Pick A and B, read what differs file by file (unified or side by
 * side), and see where choosing both would collide: the verdict per file is
 * the same `git merge-file` result `apply`/`combine` use. Nothing here writes
 * anything; every cut the server made (file list, diff length, oversize or
 * binary files) is said out loud, never silently dropped.
 *
 * Keyboard: the two pickers and the swap are native controls; the file list
 * is a listbox (Up/Down/Home/End move and select); the diff is a focusable
 * region, so its own scrollbar answers the arrow keys. Colour never carries
 * meaning alone: every file shows its status word and a glyph, every diff
 * line its +/− sign.
 */

function statusWord(status: api.PairStatus): string {
  switch (status) {
    case 'added': return t('only in B');
    case 'removed': return t('only in A');
    case 'renamed': return t('renamed');
    default: return t('different');
  }
}

function overlapWord(overlap: api.PairOverlap): string {
  switch (overlap) {
    case 'conflict': return t('Conflict');
    case 'mergeable': return t('Merges cleanly');
    case 'identical': return t('Same edit');
    default: return t('Unknown');
  }
}

function DiffTable({ file, layout }: { file: api.PairFile; layout: 'unified' | 'side' }) {
  const rows = useMemo(() => parseUnifiedDiff(file.diff), [file.diff]);
  const side = useMemo(() => (layout === 'side' ? toSideBySide(rows) : []), [rows, layout]);

  if (layout === 'unified') {
    return (
      <table className="fs-alt__diff" data-layout="unified">
        <tbody>
          {rows.map((row, index) => (
            row.kind === 'hunk' || row.kind === 'note' ? (
              <tr key={index} className="fs-alt__diff-meta" data-kind={row.kind}>
                <td colSpan={4}>{row.text}</td>
              </tr>
            ) : (
              <tr key={index} data-kind={row.kind}>
                <td className="fs-alt__diff-no" aria-hidden="true">{row.oldNo ?? ''}</td>
                <td className="fs-alt__diff-no" aria-hidden="true">{row.newNo ?? ''}</td>
                <td className="fs-alt__diff-sign">
                  <span aria-hidden="true">{row.kind === 'add' ? '+' : row.kind === 'del' ? '−' : ' '}</span>
                  <span className="fs-alt__sr">{row.kind === 'add' ? t('added line') : row.kind === 'del' ? t('removed line') : t('unchanged line')}</span>
                </td>
                <td className="fs-alt__diff-code">{row.text}</td>
              </tr>
            )
          ))}
        </tbody>
      </table>
    );
  }
  return (
    <table className="fs-alt__diff" data-layout="side">
      <tbody>
        {side.map((row, index) => (
          row.hunk !== null || row.note !== null ? (
            <tr key={index} className="fs-alt__diff-meta" data-kind={row.hunk !== null ? 'hunk' : 'note'}>
              <td colSpan={4}>{row.hunk ?? row.note}</td>
            </tr>
          ) : (
            <tr key={index}>
              <td className="fs-alt__diff-no" aria-hidden="true">{row.left.no ?? ''}</td>
              <td className="fs-alt__diff-code" data-kind={row.left.kind}>{row.left.text}</td>
              <td className="fs-alt__diff-no" aria-hidden="true">{row.right.no ?? ''}</td>
              <td className="fs-alt__diff-code" data-kind={row.right.kind}>{row.right.text}</td>
            </tr>
          )
        ))}
      </tbody>
    </table>
  );
}

function FileViewer({ file, layout }: { file: api.PairFile; layout: 'unified' | 'side' }) {
  const reason = noDiffReason(file);
  return (
    <div className="fs-alt__pair-view" data-testid="pair-viewer">
      <div className="fs-alt__pair-view-head">
        <b className="fs-alt__mono">{file.path}</b>
        {file.old_path && (
          <span className="fs-alt__muted fs-alt__mono">{t('renamed from {path}', { path: file.old_path })}</span>
        )}
        <span className="fs-alt__muted">
          {t('size in A: {a}, in B: {b}', { a: formatBytes(file.size_a), b: formatBytes(file.size_b) })}
        </span>
        {file.touched_by.length > 0 && (
          <span className="fs-alt__muted">
            {t('Changed against the base by: {who}', { who: file.touched_by.map((s) => s.toUpperCase()).join(', ') })}
          </span>
        )}
      </div>
      <div
        className="fs-alt__pair-viewer"
        role="region"
        tabIndex={0}
        aria-label={t('Diff of {path}', { path: file.path })}
        data-testid="pair-diff"
      >
        {reason === 'binary' && <p className="fs-alt__pair-empty">{t('Binary file: the contents differ but cannot be shown as text.')}</p>}
        {reason === 'too_large' && (
          <p className="fs-alt__pair-empty">{t('This file is too large to compare line by line: the contents differ, no diff is made.')}</p>
        )}
        {reason === 'same_content' && <p className="fs-alt__pair-empty">{t('Same content in both; only the path changed.')}</p>}
        {reason === 'empty' && <p className="fs-alt__pair-empty">{t('No textual difference to show.')}</p>}
        {reason === null && <DiffTable file={file} layout={layout} />}
      </div>
      {file.diff_truncated && (
        <p className="fs-alt__pair-cut" role="note">
          <TriangleAlert size={12} aria-hidden="true" />
          {t('Diff cut: showing {shown} of {total} lines.', {
            shown: file.diff_total_lines - file.omitted_lines, total: file.diff_total_lines,
          })}
        </p>
      )}
    </div>
  );
}

export function PairDiff({ projectId, expId, alternatives, signature }: {
  projectId: string;
  expId: string;
  alternatives: { id: string; label: string }[];
  /** Changes whenever the alternatives' content may have changed, to refetch. */
  signature: string;
}) {
  const ids = useMemo(() => alternatives.map((a) => a.id), [alternatives]);
  const [chosen, setChosen] = useState<[string, string] | null>(null);
  const pair = defaultPair(ids, chosen);
  const [result, setResult] = useState<api.PairResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  const [filter, setFilter] = useState<{ query: string; status: StatusFilter }>({ query: '', status: 'all' });
  const [layout, setLayout] = useState<'unified' | 'side'>('unified');
  const [selected, setSelected] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  const pairKey = pair ? `${pair[0]}|${pair[1]}` : '';
  useEffect(() => {
    if (!pair) {
      setResult(null);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    setError('');
    api.comparePair(projectId, expId, pair[0], pair[1], controller.signal)
      .then((answer) => { setResult(answer); setLoading(false); })
      .catch((failure) => {
        if (controller.signal.aborted) return;
        setError((failure as Error).message);
        setResult(null);
        setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, expId, pairKey, signature, tick]);

  const files = result?.files ?? [];
  const visible = useMemo(() => filterFiles(files, filter), [files, filter]);
  const counts = useMemo(() => statusCounts(files), [files]);
  const current = keepSelection(visible, selected);
  const currentFile = visible.find((f) => f.path === current) ?? null;
  const currentIndex = visible.findIndex((f) => f.path === current);

  const onListKey = useCallback((event: KeyboardEvent<HTMLUListElement>) => {
    const next = moveIndex(currentIndex, event.key, visible.length);
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) || next < 0) return;
    event.preventDefault();
    const path = visible[next].path;
    setSelected(path);
    window.requestAnimationFrame(() => {
      const nodes = listRef.current?.querySelectorAll<HTMLElement>('[role="option"]');
      nodes?.[next]?.focus();
    });
  }, [currentIndex, visible]);

  if (ids.length < 2 || !pair) {
    return (
      <section className="fs-alt__pair" aria-labelledby="alt-pair-title" data-testid="alt-pair">
        <h3 id="alt-pair-title" className="fs-alt__pair-title">{t('Compare two alternatives')}</h3>
        <p className="fs-alt__muted">{t('Add a second alternative to compare two of them.')}</p>
      </section>
    );
  }

  const label = (id: string) => alternatives.find((a) => a.id === id)?.label ?? id;
  const chips: { key: StatusFilter; text: string; n: number }[] = [
    { key: 'all', text: t('All'), n: counts.all },
    { key: 'conflict', text: t('Conflicts'), n: counts.conflict },
    { key: 'added', text: t('Only in B'), n: counts.added },
    { key: 'removed', text: t('Only in A'), n: counts.removed },
    { key: 'changed', text: t('Different'), n: counts.changed },
    { key: 'renamed', text: t('Renamed files'), n: counts.renamed },
  ];
  const trunc = result?.truncation;

  return (
    <section className="fs-alt__pair" aria-labelledby="alt-pair-title" aria-busy={loading} data-testid="alt-pair">
      <h3 id="alt-pair-title" className="fs-alt__pair-title">{t('Compare two alternatives')}</h3>
      <p className="fs-alt__muted">
        {t('See, file by file, how one alternative differs from another, and where choosing both would collide.')}
      </p>

      <div className="fs-alt__pair-pick">
        <label className="fs-alt__row-field">
          <span>{t('Alternative A')}</span>
          <select
            className="fs-field" value={pair[0]} data-testid="pair-a"
            onChange={(event) => setChosen(pickSide(pair, 0, event.target.value, ids))}
          >
            {alternatives.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
        </label>
        <Button
          variant="ghost" size="sm" icon={ArrowLeftRight} label={t('Swap A and B')}
          onClick={() => setChosen(swapPair(pair))} testId="pair-swap"
        />
        <label className="fs-alt__row-field">
          <span>{t('Alternative B')}</span>
          <select
            className="fs-field" value={pair[1]} data-testid="pair-b"
            onChange={(event) => setChosen(pickSide(pair, 1, event.target.value, ids))}
          >
            {alternatives.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
        </label>
        <Button
          variant="secondary" size="sm" icon={RefreshCw} label={t('Reload comparison')}
          loading={loading} onClick={() => setTick((n) => n + 1)}
        />
      </div>

      {error && (
        <p className="fs-alt__notice" data-tone="danger" role="alert">{error}</p>
      )}

      {result === null && !error && (
        <Skeleton label={t('Comparing the two alternatives')} count={2} height="48px" />
      )}

      {result && (
        <>
          <div className="fs-alt__pair-summary" role="status" aria-live="polite" data-testid="pair-summary">
            {result.identical ? (
              <p><b>{t('These two alternatives are identical: no file differs.')}</b></p>
            ) : (
              <>
                <p>
                  <b>{t('{n} file(s) differ going from {a} to {b}', {
                    n: result.summary.files_differing, a: label(result.a.id), b: label(result.b.id),
                  })}</b>
                  {' '}
                  <span className="fs-alt__file-add">+{result.summary.additions}</span>
                  {' '}
                  <span className="fs-alt__file-del">−{result.summary.deletions}</span>
                </p>
              </>
            )}
            <p className="fs-alt__muted">
              {t('Only A changed {a} file(s); only B changed {b}.', { a: result.touched.only_a, b: result.touched.only_b })}
              {' '}
              {result.touched.both > 0 && t('Both changed {n} file(s) relative to the base: {i} the same edit, {m} merge cleanly, {c} conflict.', {
                n: result.touched.both, i: result.summary.overlap.identical, m: result.summary.overlap.mergeable,
                c: result.summary.overlap.conflict + result.summary.overlap.unknown,
              })}
            </p>
          </div>

          {trunc?.any && (
            <div className="fs-alt__notice" data-tone="warning" role="status" data-testid="pair-truncation">
              <TriangleAlert size={14} aria-hidden="true" />
              <ul className="fs-alt__gaps">
                {trunc.files && (
                  <li>{t('Showing the first {shown} of {total} files; {omitted} more are not listed.', {
                    shown: result.files.length, total: result.summary.files_differing, omitted: trunc.files_omitted,
                  })}</li>
                )}
                {trunc.diffs > 0 && (
                  <li>{t('{n} diff(s) were cut short: at most {lines} lines of each are shown.', {
                    n: trunc.diffs, lines: result.limits.max_diff_lines_per_file,
                  })}</li>
                )}
                {trunc.files_not_diffed > 0 && (
                  <li>{t('{n} file(s) are binary or larger than {size}: listed, not compared line by line.', {
                    n: trunc.files_not_diffed, size: formatBytes(result.limits.max_file_bytes),
                  })}</li>
                )}
                {trunc.diff_budget_exhausted && <li>{t('The total diff size limit was reached; later diffs are shortened.')}</li>}
              </ul>
            </div>
          )}

          {!result.identical && (
            <>
              <div className="fs-alt__pair-tools">
                <input
                  className="fs-field" type="search" value={filter.query}
                  aria-label={t('Filter files by path')}
                  placeholder={t('Filter files by path')}
                  onChange={(event) => setFilter({ ...filter, query: event.target.value })}
                />
                <div className="fs-alt__pair-chips" role="group" aria-label={t('Filter by kind of difference')}>
                  {chips.filter((c) => c.key === 'all' || c.n > 0).map((chip) => (
                    <button
                      key={chip.key} type="button" className="fs-alt__chip"
                      aria-pressed={filter.status === chip.key}
                      data-tone={chip.key === 'conflict' ? 'bad' : undefined}
                      onClick={() => setFilter({ ...filter, status: chip.key })}
                    >
                      {chip.text} <span className="fs-alt__chip-n">{chip.n}</span>
                    </button>
                  ))}
                </div>
                <div className="fs-alt__pair-chips" role="group" aria-label={t('Diff layout')}>
                  <button type="button" className="fs-alt__chip" aria-pressed={layout === 'unified'} onClick={() => setLayout('unified')}>
                    {t('Unified')}
                  </button>
                  <button type="button" className="fs-alt__chip" aria-pressed={layout === 'side'} onClick={() => setLayout('side')}>
                    {t('Side by side')}
                  </button>
                </div>
              </div>

              <div className="fs-alt__pair-split">
                {visible.length === 0 ? (
                  <p className="fs-alt__muted">{t('No file matches the filter.')}</p>
                ) : (
                  <ul
                    ref={listRef} className="fs-alt__pair-files" role="listbox"
                    aria-label={t('Files that differ')} onKeyDown={onListKey} data-testid="pair-files"
                  >
                    {visible.map((file) => {
                      const isCurrent = file.path === current;
                      return (
                        <li
                          key={file.path} role="option" aria-selected={isCurrent}
                          tabIndex={isCurrent ? 0 : -1} className="fs-alt__pair-file"
                          data-status={file.status} data-current={isCurrent ? 'yes' : 'no'}
                          onClick={() => setSelected(file.path)}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault();
                              setSelected(file.path);
                            }
                          }}
                        >
                          <span className="fs-alt__pair-glyph" aria-hidden="true">{statusGlyph(file.status)}</span>
                          <span className="fs-alt__pair-path fs-alt__mono">{file.path}</span>
                          <span className="fs-alt__pair-meta">
                            <span className="fs-alt__muted">{statusWord(file.status)}</span>
                            {file.overlap && file.overlap !== 'identical' && (
                              <span className="fs-alt__tag" data-tone={overlapTone(file.overlap)}>{overlapWord(file.overlap)}</span>
                            )}
                            {file.additions !== null && <span className="fs-alt__file-add">+{file.additions}</span>}
                            {file.deletions !== null && <span className="fs-alt__file-del">−{file.deletions}</span>}
                            {file.binary && <span className="fs-alt__tag">{t('binary')}</span>}
                            {file.too_large && <span className="fs-alt__tag" data-tone="warn">{t('too large')}</span>}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                )}
                {currentFile ? (
                  <FileViewer file={currentFile} layout={layout} />
                ) : (
                  visible.length > 0 && <p className="fs-alt__muted">{t('Select a file to see its diff.')}</p>
                )}
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}
