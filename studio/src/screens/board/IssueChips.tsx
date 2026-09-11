import { Fragment, type ReactNode } from 'react';
import { linkIssueIds } from '../../adapters/board';

/**
 * Lote 93 — the one place `\bFAU-12\b`-shaped ids (matching a project's own
 * board key) turn into something clickable, shared by
 * `studio/src/screens/studio/Transcript.tsx` (chat text) and
 * `studio/src/screens/source-control/CommitGraph.tsx` (commit messages) so
 * neither file needs its own copy of the splitting logic — only the pure
 * `linkIssueIds` (adapters/board.ts) plus whatever each caller wants to
 * render for a match (a button that opens the issue inline, or a `<Link>`
 * that navigates to it).
 *
 * `boardKey` absent/empty is a no-op: the text renders unchanged, so a
 * caller that has not been wired with a key yet costs nothing and risks no
 * false positives.
 */
export function renderIssueSegments(
  text: string,
  boardKey: string | undefined,
  renderIssue: (id: string) => ReactNode,
): ReactNode {
  if (!boardKey) return text;
  const segments = linkIssueIds(text, boardKey);
  if (segments.length === 0) return null;
  if (segments.length === 1 && segments[0].kind === 'text') return segments[0].text;
  return segments.map((seg, i) => <Fragment key={i}>{seg.kind === 'text' ? seg.text : renderIssue(seg.id)}</Fragment>);
}

/** The default chip look, for callers that just need a clickable button
 *  (Transcript's own text) rather than a router `<Link>` (CommitGraph,
 *  which always navigates rather than opening something inline). */
export function IssueChip({ id, onClick, testId }: { id: string; onClick?: () => void; testId?: string }) {
  return (
    <button
      type="button"
      className="fs-issue-chip"
      data-testid={testId ?? 'issue-id-chip'}
      onClick={onClick}
      disabled={!onClick}
    >
      {id}
    </button>
  );
}
