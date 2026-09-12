import { ArrowDown, BookmarkPlus, Check, ChevronDown, Copy, Expand, FileText, FoldVertical, GitBranch, GitBranchPlus, GitCommit, GitFork, Pencil, Quote, RefreshCw, Telescope, Trash2, UploadCloud, Volume2, VolumeX, X } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Link } from 'react-router';
import { Fragment, lazy, Suspense, useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { Button, describeError, ExecutionTimeline, friendlyError, IconButton } from '../../components';
import { fetchCompactionEvent, pinCompactionFragment, type AskUser, type CompactionEvent, type ContextLedger, type ContextReceipt, type DelegationTask } from '../../adapters/chat';
import { createRecipeFromRun } from '../../adapters/strategy';
import type { EvidenceRef } from '../../adapters/evidence';
import { attachmentUrl, isImage } from '../../adapters/composer';
import { Rich } from '../rich';
import { issueIdRegex } from '../../adapters/board';
import { IssueChip, renderIssueSegments } from '../board/IssueChips';
import { splitMentions } from '../../lib/mentions';
import { safeExternal } from '../../lib/markdown';
import { stripExecutedFences, toolFenceRegex } from '../../lib/fences';
import { frameBatcher } from '../../lib/frame-batch';
import { formatMetrics, liveTps, type CoverageItem, type LiveRate, type PlanStepView, type Step, type Turn, type TurnStrategy } from './model';
import { t, tn } from '../../i18n';
import { getDisplay } from '../../shell/display';
import { nextStreamAnnouncement } from '../../adapters/streamAnnounce';

/**
 * A11Y-02 — a `polite` live region fed grouped chunks
 * (`adapters/streamAnnounce.ts::nextStreamAnnouncement`), never the whole
 * growing message: an assistive tech re-reading the full text on every
 * delta is exactly the "hundreds of announcements" this requirement rules
 * out. On the turn's last delta (`active` going false) whatever tail never
 * made it past the interval gate is flushed once, so the ending of a short
 * answer is never silently skipped.
 */
function useGroupedStreamAnnouncement(text: string, active: boolean): string {
  const [announcement, setAnnouncement] = useState('');
  const progress = useRef({ length: 0, at: 0 });
  const wasActive = useRef(active);
  useEffect(() => {
    if (active) {
      const next = nextStreamAnnouncement(progress.current.length, text, progress.current.at, Date.now());
      if (next) {
        progress.current = { length: next.length, at: next.at };
        setAnnouncement(next.chunk);
      }
    } else if (wasActive.current && text.length > progress.current.length) {
      setAnnouncement(text.slice(progress.current.length));
      progress.current = { length: 0, at: 0 };
    } else if (wasActive.current) {
      progress.current = { length: 0, at: 0 };
    }
    wasActive.current = active;
  }, [text, active]);
  return announcement;
}

/**
 * PERF-01/UX-05: paint a fast-changing value at most once per animation
 * frame instead of once per state update — see `lib/frame-batch.ts`'s doc
 * comment for why. `active` opts a value in only while it is worth
 * batching (a turn still streaming); once it settles, the exact final
 * value is delivered immediately, with nothing left pending in the batcher.
 */
function useFrameBatched<T>(value: T, active: boolean): T {
  const [shown, setShown] = useState(value);
  const batcherRef = useRef<ReturnType<typeof frameBatcher<T>> | null>(null);
  if (!batcherRef.current) batcherRef.current = frameBatcher<T>((v) => setShown(v));
  useEffect(() => () => batcherRef.current?.cancel(), []);
  useEffect(() => {
    if (!active) {
      batcherRef.current?.cancel();
      setShown(value);
      return;
    }
    batcherRef.current?.push(value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, active]);
  return active ? shown : value;
}

/** Loaded on the first click: the speech adapter is not part of the eager bundle. */
const speak = (text: string) => import('../../adapters/speech').then((m) => m.speak(text));

/* The harness card carries diff, revert and commit: a chunk that arrives
   with the first agent turn that has something to show, not on page load.
   Same for the sub-agent board: most turns never delegate. */
const Harness = lazy(() => import('./Harness'));
const SubagentBoard = lazy(() => import('./SubagentBoard'));

export type Decision = 'approve' | 'approve_task' | 'deny';

export interface TranscriptProps {
  turns: Turn[];
  busy: boolean;
  /** CTX-02: which session's compaction log/pins the Ledger's "Qué se
   *  compactó" reads and writes — `null` before a session exists yet
   *  (nothing to fetch, the affordance simply does not render). */
  sessionId: string | null;
  onApproval: (turn: Turn, decision: Decision) => void;
  /** CALL-07/TASK-04: `optionIds` are the stable `AskOption.id`s the user
   *  picked (Studio.tsx pairs these with `turn.ask?.questionId` to answer a
   *  specific question); absent for a free-text answer. */
  onAnswer: (turn: Turn, text: string, optionIds?: string[]) => void;
  /** Save the user's edit; with `regenerate` the reply is redone from it. */
  onEdit: (turn: Turn, text: string, regenerate: boolean) => void;
  onRegenerate: (turn: Turn) => void;
  onDelete: (turn: Turn) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  /** Side panel hooks: a workspace file, a living document, a worker to re-run. */
  onOpenFile?: (path: string) => void;
  onOpenDoc?: (docId: string) => void;
  /** Lote 50 (BENCH-03 wiring): open the evidence inspector for one
   *  `EvidenceRef` a tool call attached to its result (`step.evidenceRefs`). */
  onOpenEvidence?: (ref: EvidenceRef) => void;
  onRerun?: (task: DelegationTask) => void;
  /** A new conversation with everything up to and including this reply. */
  onFork?: (turn: Turn) => void;
  /** Selected text from a reply, quoted into the composer. */
  onQuote?: (text: string) => void;
  /** B2 (CONTRATO_EXCURSOS.md): branch a new excurso from this point — a
   *  quoted passage (the floating selection button, alongside "Citar") or
   *  the whole turn (the turn's own "Explorar desde aquí", no `passage`).
   *  `historyIndex` is the SAME 0-based position `forkFrom` (Studio.tsx)
   *  already reads off `turn.historyIndex ?? index` — the excurso's anchor
   *  is "everything up to and including this reply", exactly what a fork
   *  from here would have kept. */
  onExplore?: (input: { historyIndex: number; passage?: string }) => void;
  /** F3 (CONTRATO_CABLES2): "Condense up to here" on an assistant turn —
   *  opens `CondenseDialog.tsx` seeded with `historyIndex` as the range's
   *  end (Studio.tsx picks the start). */
  onCondense?: (input: { historyIndex: number }) => void;
  /** F3: "Expand" on a condensed summary row — `historyIndex` is that row's
   *  own position (`turn.historyIndex`), the `summary_index` `POST
   *  .../condense/{index}/expand` expects. */
  onExpandCondensed?: (historyIndex: number) => void;
  /** Lote 86: a `git_policy` chip opens the Source control panel. */
  onOpenSourceControl?: () => void;
  /** Lote 93: the project's board key ("FAU") — `FAU-12`-shaped ids in the
   *  transcript (user text and assistant replies) become clickable chips.
   *  Absent/empty is a no-op: nothing is scanned, zero risk to existing
   *  renders until a caller actually has a key to give. */
  boardKey?: string;
  /** The project these ids belong to — the fallback navigation target
   *  (`/projects/{id}?tab=board&issue=…`) when `onOpenBoardIssue` is not
   *  given (e.g. the assistant's markdown body, which always navigates
   *  rather than opening inline — see `AssistantTurn`'s doc comment). */
  projectId?: string;
  /** Opens the issue inline (the chat's own Board tab) instead of
   *  navigating away. Used for the user's own text; the assistant's body
   *  goes through `<Rich>` (a fichero ajeno to this lote) and always falls
   *  back to a plain navigable link instead. */
  onOpenBoardIssue?: (id: string) => void;
}

/** Read a reply aloud; the button flips to stop while it plays. */
function SpeakButton({ text }: { text: string }) {
  const [stop, setStop] = useState<(() => void) | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => () => stop?.(), [stop]);
  return (
    <IconButton
      icon={stop ? VolumeX : Volume2}
      label={stop ? t('Stop reading') : error ? t('No voice available') : t('Read aloud')}
      size="sm"
      onClick={() => {
        if (stop) {
          stop();
          setStop(null);
          return;
        }
        speak(text)
          .then((fn) => setStop(() => () => {
            fn();
            setStop(null);
          }))
          .catch(() => setError(true));
      }}
      testId="turn-speak"
    />
  );
}

/** Selecting text inside a reply offers to quote it into the composer, or
 *  (B2, CONTRATO_EXCURSOS.md) to branch a side thread from it. `turnId` is
 *  the selected assistant turn's `data-nav-id` (set on `AssistantTurn`'s
 *  root `<article>`), captured here so the caller can resolve it back to a
 *  `Turn`/`historyIndex` without this hook knowing anything about `Turn`
 *  itself. */
function useQuoteSelection(onQuote?: (text: string) => void, exploreEnabled?: boolean) {
  const [pos, setPos] = useState<{ x: number; y: number; text: string; turnId: string | null } | null>(null);
  const holder = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!onQuote && !exploreEnabled) return;
    const onUp = () => {
      window.setTimeout(() => {
        const sel = window.getSelection();
        const text = sel?.toString().trim() ?? '';
        if (!text || !sel || sel.rangeCount === 0 || !holder.current) {
          setPos(null);
          return;
        }
        const range = sel.getRangeAt(0);
        const node = range.commonAncestorContainer;
        const el = node.nodeType === 1 ? (node as Element) : node.parentElement;
        const turnEl = el?.closest('.fs-turn--assistant');
        if (!el || !holder.current.contains(el) || !turnEl) {
          setPos(null);
          return;
        }
        const rect = range.getBoundingClientRect();
        const host = holder.current.getBoundingClientRect();
        setPos({ x: rect.left - host.left + rect.width / 2, y: rect.top - host.top, text, turnId: turnEl.getAttribute('data-nav-id') });
      }, 0);
    };
    const onDown = (e: MouseEvent) => {
      if (!(e.target as Element).closest?.('.fs-studio__quote')) setPos(null);
    };
    document.addEventListener('mouseup', onUp);
    document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('mouseup', onUp);
      document.removeEventListener('mousedown', onDown);
    };
  }, [onQuote, exploreEnabled]);
  return { holder, pos, clear: () => setPos(null) };
}

const FILE_TOOLS = /^(read_file|write_file|edit_file|apply_patch|create_file|multi_edit|replace_across_files)$/;

/**
 * EXEC-02: a command preview must not hand a secret to whoever is looking
 * over the approver's shoulder — or sit in a screenshot — just because the
 * model happened to interpolate one into an argument. Value-shaped, not
 * key-shaped: `sk_…`/`pk_…`/`AKIA…` tokens are masked wherever they occur,
 * and `token=`/`password=`/… are masked by what follows the `=`/`:`, never
 * the key name itself, so the command still reads as what it does.
 * Client-side only, and only for what is SHOWN: the real argv still runs
 * unmodified (`src/agent_tools/subprocess_tools.py`'s `create_subprocess_exec`,
 * never shell=True) — this never touches execution, only the preview.
 */
const SECRET_PATTERNS: [RegExp, string][] = [
  [/\b((?:sk|pk|rk)[-_][A-Za-z0-9_-]{10,})\b/gi, '****'],
  [/\b(AKIA[0-9A-Z]{16})\b/g, '****'],
  [/\b(Bearer\s+)[A-Za-z0-9._-]{10,}/gi, '$1****'],
  [/\b((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)(['"]?)([^\s'"]{3,})(\2)/gi, '$1$2****$4'],
];

export function maskSecrets(command: string): string {
  return SECRET_PATTERNS.reduce((out, [re, replacement]) => out.replace(re, replacement), command);
}

/**
 * Lote 93: the assistant's reply goes through `<Rich>` (markdown → React, a
 * fichero ajeno to this lote — no hook there to turn a matched run into a
 * clickable button), so a `FAU-12` id becomes a real markdown link instead,
 * pointing at the board tab on this project — `<Rich>`'s own `RichLink`
 * then renders it exactly like any other link in a reply. A no-op unless
 * BOTH `boardKey` and `projectId` are known (no id is ever falsely turned
 * into a broken link with nowhere to point). Known limitation: a plain
 * string replace does not know about code fences/inline code, so an id
 * quoted verbatim in code would also linkify — cosmetic, not functional.
 */
export function linkifyBoardIds(text: string, boardKey: string | undefined, projectId: string | undefined): string {
  if (!boardKey || !projectId) return text;
  return text.replace(issueIdRegex(boardKey), (m) => `[${m}](/projects/${encodeURIComponent(projectId)}?tab=board&issue=${encodeURIComponent(m)})`);
}

/** EXEC-01: a short label for `Step.executionTarget.kind` — the words a
 *  person reads next to the command, not the wire's own vocabulary. */
function executionTargetLabel(kind: string): string {
  switch (kind) {
    case 'windows':
      return t('Windows');
    case 'wsl':
      return t('WSL');
    case 'posix':
      return t('Linux/macOS');
    case 'container':
      return t('sandboxed container');
    case 'remote':
      return t('a remote worker');
    default:
      return kind;
  }
}

/** The unified diff of a file write, coloured line by line.
 *
 * A11Y-01/QA-44: `.fs-diff` scrolls its own box (`overflow: auto;
 * max-block-size: 320px` in studio.css) once a diff runs long, so without a
 * `tabIndex` it is a control a keyboard-only reader cannot reach at all —
 * the mouse-only trap this lote's Playwright walkthrough checks for. Same
 * fix as `rich.tsx`'s `.fs-rich__tablewrap` (a fichero ajeno already doing
 * this correctly): `role="region"` + `tabIndex={0}` so Tab lands on it and
 * the arrow/Page keys scroll it, with a live label instead of a mystery box. */
export function DiffLines({ text }: { text: string }) {
  return (
    <pre
      className="fs-diff"
      role="region"
      tabIndex={0}
      aria-label={t('Diff')}
      data-testid="step-diff"
      // A11Y-01/QA-44: the `<details>` this sits in already nudges itself
      // into view on open, but Tab moving focus one step further, onto
      // this box specifically, can re-scroll past that — found live at
      // 200% zoom, ~20px of the box left below the fold either way. This
      // is the tab stop that actually matters, so it gets the final say.
      onFocus={(e) => e.currentTarget.scrollIntoView({ block: 'nearest' })}
    >
      {text.split('\n').map((line, i) => {
        let cls = 'fs-diff-ctx';
        let body = line;
        if (line.startsWith('+++') || line.startsWith('---')) cls = 'fs-diff-meta';
        else if (line.startsWith('@@')) cls = 'fs-diff-hunk';
        else if (line.startsWith('+')) {
          cls = 'fs-diff-add';
          body = line.slice(1);
        } else if (line.startsWith('-')) {
          cls = 'fs-diff-del';
          body = line.slice(1);
        } else if (line.startsWith(' ')) body = line.slice(1);
        return (
          <span key={i} className={cls}>
            {body || ' '}
          </span>
        );
      })}
    </pre>
  );
}

/** A value from a repair's `from`/`to` (or an error `detail`), rendered as
 *  the model sent it — `""` for an empty string reads as nothing happened,
 *  so an explicit empty case is spelled out. */
function argValue(v: unknown): string {
  if (v === undefined) return t('(missing)');
  if (v === '') return t('(empty)');
  if (typeof v === 'string') return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/**
 * CALL-03: what the argument check found for this call, next to the field
 * it touched — a repair shows original → corrección, and an error the
 * repair could not resolve shows on its own. `field`s covered by a repair
 * are not repeated as bare errors: `_validate_native_tool_call`
 * (src/agent_loop.py) reports the FULL error list even for fields it went
 * on to fix, so without this filter every repaired field would also read
 * as still broken.
 */
function ArgumentRepairs({ step }: { step: Step }) {
  const repairs = step.repairs ?? [];
  const fixedFields = new Set(repairs.map((r) => r.field));
  const openErrors = (step.argumentErrors ?? []).filter((e) => !fixedFields.has(e.field));
  if (!repairs.length && !openErrors.length) return null;
  return (
    <div className="fs-studio__arg-repairs" data-testid="tool-argument-repairs">
      {repairs.map((r, i) => (
        <p key={`r${i}`} className="fs-studio__arg-repair">
          <strong>{r.field}</strong>
          {' '}
          {t('{from} → {to} (auto-corrected)', { from: argValue(r.from), to: argValue(r.to) })}
        </p>
      ))}
      {openErrors.map((e, i) => (
        <p key={`e${i}`} className="fs-studio__arg-error" data-tone="warning">
          <strong>{e.field}</strong> {e.detail || t('Argument error')}
        </p>
      ))}
    </div>
  );
}

function ToolRail({ steps, live, sessionId, onOpenFile, onOpenDoc, onOpenEvidence }: { steps: Step[]; live: boolean; sessionId?: string | null; onOpenFile?: (path: string) => void; onOpenDoc?: (docId: string) => void; onOpenEvidence?: (ref: EvidenceRef) => void }) {
  const [expanded, setExpanded] = useState(false);
  const leadingDone = steps.findIndex((s) => s.state !== 'succeeded');
  const doneCount = leadingDone === -1 ? steps.length : leadingDone;
  const collapse = !expanded && !live && doneCount > 3;
  const visible = collapse ? steps.slice(doneCount) : steps;

  return (
    <div className="fs-trace fs-studio__trace" data-testid="studio-trace">
      {collapse && (
        <button type="button" className="fs-trace__collapsed" onClick={() => setExpanded(true)} aria-expanded={false} data-testid="trace-expand">
          <span aria-hidden="true" />
          <span>
            <ChevronDown size={13} aria-hidden="true" /> {doneCount} pasos completados
          </span>
        </button>
      )}
      {visible.map((step) =>
        step.output || step.command || step.diff || step.screenshot ? (
          <details
            key={step.id}
            className="fs-trace__step fs-studio__step"
            data-state={step.state}
            // A11Y-01/QA-44: opening this by keyboard (Enter on the
            // <summary>) can reveal a tall diff/output the browser's own
            // focus-scroll only partly brought into view — found live at
            // 200% zoom, the box's own bottom edge a few px below the
            // fold. Nudging on open (never on close) covers it without
            // fighting the reader's scroll position the rest of the time.
            onToggle={(e) => {
              if (!e.currentTarget.open) return;
              e.currentTarget.scrollIntoView({ block: 'nearest' });
              // QA-44 hueco 2: at 200% zoom the whole card (command + output
              // + diff) can be taller than the effective viewport, so
              // bringing ITS edge into view still leaves a long diff below
              // the fold — the diff's own box (studio.css's `.fs-diff`,
              // capped by `max-block-size`) always fits on its own, so bring
              // that in too once the card's content is actually visible.
              e.currentTarget.querySelector('.fs-diff')?.scrollIntoView({ block: 'nearest' });
            }}
          >
            <summary>
              <span className="fs-trace__node" aria-hidden="true" />
              <span className="fs-trace__label">{step.label}</span>
              {step.executionTarget && (
                <span className="fs-trace__meta" data-testid="tool-execution-target" title={[step.executionTarget.cwd, step.executionTarget.shell].filter(Boolean).join(' · ')}>
                  {t('runs on {target}', { target: executionTargetLabel(step.executionTarget.kind) })}
                </span>
              )}
              {step.diff && (
                <span className="fs-trace__meta fs-diff-stat">
                  {step.diff.newFile && <em>nuevo</em>}
                  {step.diff.added > 0 && <ins>+{step.diff.added}</ins>}
                  {step.diff.removed > 0 && <del>−{step.diff.removed}</del>}
                </span>
              )}
              {step.meta && <span className="fs-trace__meta">{step.meta}</span>}
            </summary>
            {(onOpenFile && FILE_TOOLS.test(step.tool) && step.command) || (onOpenDoc && step.docId) ? (
              <p className="fs-studio__step-links">
                {onOpenFile && FILE_TOOLS.test(step.tool) && step.command && (
                  <button type="button" className="fs-link" onClick={() => onOpenFile((step.diff?.file || step.command || '').split('\n')[0].trim())}>
                    Ver el fichero
                  </button>
                )}
                {onOpenDoc && step.docId && (
                  <button type="button" className="fs-link" onClick={() => onOpenDoc(step.docId as string)}>
                    Abrir el documento
                  </button>
                )}
              </p>
            ) : null}
            {step.diff ? (
              <DiffLines text={step.diff.text} />
            ) : (
              step.command &&
              step.command !== step.label && (
                <pre className="fs-studio__cmd" data-testid="step-command">
                  {maskSecrets(step.command)}
                </pre>
              )
            )}
            {(step.repairs?.length || step.argumentErrors?.length) ? <ArgumentRepairs step={step} /> : null}
            {onOpenEvidence && step.evidenceRefs?.length ? (
              <p className="fs-studio__step-links" data-testid="tool-evidence-links">
                {step.evidenceRefs.map((ref) => (
                  <button
                    key={ref.evidence_id}
                    type="button"
                    className="fs-link"
                    onClick={() => onOpenEvidence(ref)}
                  >
                    {t('View evidence')}
                  </button>
                ))}
              </p>
            ) : null}
            {step.callId && (
              <p className="fs-studio__step-links" data-testid="tool-trace-link">
                <Link
                  className="fs-link"
                  to={`/activity?trace=${encodeURIComponent(step.callId)}${sessionId ? `&session=${encodeURIComponent(sessionId)}` : ''}`}
                >
                  {t('View trace')}
                </Link>
              </p>
            )}
            {step.output && <pre className="fs-studio__out">{step.output.slice(0, 6000)}</pre>}
            {step.screenshot && <img className="fs-studio__shot" src={step.screenshot} alt={t('Tool screenshot')} loading="lazy" />}
          </details>
        ) : (
          <div key={step.id} className="fs-trace__step" data-state={step.state}>
            <span className="fs-trace__node" aria-hidden="true" />
            <span className="fs-trace__label">{step.label}</span>
            {step.meta && <span className="fs-trace__meta">{step.meta}</span>}
          </div>
        ),
      )}
    </div>
  );
}

/**
 * A11Y-01/QA-44: scroll a newly-arrived card into view the moment it
 * mounts, instead of counting on the browser's own focus-triggered
 * scrolling. Found live with `scripts/ui_a11y.py` at 200% zoom: a card
 * appearing near the foot of a tall reply can render with its action
 * buttons already below the fold, and Tab-focusing one of them is not
 * guaranteed to bring it fully into view by itself (`scrollIntoView` on
 * focus is a courtesy some engines/zoom modes skip, not a spec guarantee)
 * — every keyboard user's very next Tab stop would then be a button they
 * cannot see. `block: 'nearest'` never fights the reader's own scroll
 * position once the card is already visible; it only acts the first time.
 */
function useScrollIntoViewOnMount<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  useEffect(() => {
    ref.current?.scrollIntoView({ block: 'nearest' });
  }, []);
  return ref;
}

export function AskCard({
  ask,
  busy,
  onApproval,
  onAnswer,
}: {
  ask: AskUser;
  busy: boolean;
  onApproval: (decision: Decision) => void;
  onAnswer: (text: string, optionIds?: string[]) => void;
}) {
  const ref = useScrollIntoViewOnMount<HTMLDivElement>();
  if (ask.kind === 'tool_approval') {
    return (
      <div className="fs-studio__ask" ref={ref} data-testid="studio-approval">
        <p className="fs-studio__ask-title">{t('Needs your permission')}</p>
        {ask.question && <p className="fs-prose">{ask.question}</p>}
        <div className="fs-studio__ask-actions">
          <Button variant="primary" icon={Check} label={t('Approve')} disabled={busy} onClick={() => onApproval('approve')} />
          <Button label={t('Approve the whole task')} disabled={busy} onClick={() => onApproval('approve_task')} />
          <Button variant="danger" icon={X} label={t('Deny')} disabled={busy} onClick={() => onApproval('deny')} />
        </div>
      </div>
    );
  }
  return <QuestionCard ask={ask} busy={busy} onAnswer={onAnswer} rootRef={ref} />;
}

/**
 * The model's question with its options — the shape of AskUserQuestion:
 * one click per option when one answer is wanted, a checklist with "Send"
 * when several may apply (`multi`), and always a line to write your own
 * answer, because the option the person needs is often the one the model
 * did not think of (Luis, 10-09-2026: "varias opciones para elegir y una
 * para que escribas tú, o una checklist").
 */
function QuestionCard({
  ask,
  busy,
  onAnswer,
  rootRef,
}: {
  ask: AskUser;
  busy: boolean;
  onAnswer: (text: string, optionIds?: string[]) => void;
  /** See `useScrollIntoViewOnMount`'s doc comment — `AskCard` owns the ref
   *  so it can pass the SAME one down whichever branch it renders. */
  rootRef?: RefObject<HTMLDivElement | null>;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [own, setOwn] = useState('');
  const toggle = (label: string) => setPicked((cur) => (cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label]));
  const sendOwn = () => {
    const text = own.trim();
    if (text) onAnswer(text);
  };
  const sendPicked = () => {
    if (!picked.length) return;
    const pickedIds = ask.options
      .filter((o) => picked.includes(o.label))
      .map((o) => o.id)
      .filter((id): id is string => Boolean(id));
    onAnswer(picked.join('; '), pickedIds.length ? pickedIds : undefined);
  };
  return (
    <div className="fs-studio__ask" ref={rootRef} data-testid="studio-question" data-multi={ask.multi || undefined}>
      <p className="fs-studio__ask-title">{ask.multi ? t('Asks you — pick all that apply') : t('Asks you')}</p>
      <p className="fs-prose">{ask.question}</p>
      {ask.options.length > 0 && !ask.multi && (
        <div className="fs-studio__ask-options" role="group">
          {ask.options.map((option) => (
            <button
              key={option.label}
              type="button"
              className="fs-studio__ask-option"
              disabled={busy}
              onClick={() => onAnswer(option.label, option.id ? [option.id] : undefined)}
              data-testid="studio-question-option"
            >
              <span className="fs-studio__ask-option-label">{option.label}</span>
              {option.description && <span className="fs-studio__ask-option-desc">{option.description}</span>}
            </button>
          ))}
        </div>
      )}
      {ask.options.length > 0 && ask.multi && (
        <div className="fs-studio__ask-options" role="group">
          {ask.options.map((option) => (
            <label key={option.label} className="fs-studio__ask-option" data-checked={picked.includes(option.label) || undefined}>
              <input type="checkbox" checked={picked.includes(option.label)} disabled={busy} onChange={() => toggle(option.label)} data-testid="studio-question-check" />
              <span>
                <span className="fs-studio__ask-option-label">{option.label}</span>
                {option.description && <span className="fs-studio__ask-option-desc">{option.description}</span>}
              </span>
            </label>
          ))}
          <div className="fs-studio__ask-actions">
            <Button variant="primary" size="sm" icon={Check} label={picked.length ? t('Send {n} picked', { n: picked.length }) : t('Send')} disabled={busy || picked.length === 0} onClick={sendPicked} testId="studio-question-send" />
          </div>
        </div>
      )}
      <form
        className="fs-studio__ask-own"
        onSubmit={(e) => {
          e.preventDefault();
          sendOwn();
        }}
      >
        <input
          className="fs-field"
          value={own}
          disabled={busy}
          placeholder={t('Or write your own answer…')}
          onChange={(e) => setOwn(e.target.value)}
          data-testid="studio-question-own"
        />
        <Button size="sm" label={t('Answer')} disabled={busy || !own.trim()} onClick={sendOwn} testId="studio-question-own-send" />
      </form>
    </div>
  );
}

/**
 * A permission that was already answered.
 *
 * Not a card with dead buttons and not silence either: the gate's question
 * is saved as the message's own text, so without this the conversation came
 * back reading "Allow this task to continue?" with nothing under it — the
 * exact shape of a chat that looks hung when it is not.
 */
export function AnsweredCard({ decision }: { decision: string }) {
  if (decision === 'expired') {
    // The grant died with a server restart or its TTL: the card must not
    // look answerable, and the way forward has to be on it.
    return <p className="fs-studio__answered" data-testid="studio-approval-answered" data-decision="expired">
      {t('This permission request expired before it was answered — nothing was executed. Ask again to get a fresh card.')}
    </p>;
  }
  if (!['approve', 'approve_task', 'deny'].includes(decision)) {
    return <p className="fs-studio__answered" data-testid="studio-approval-answered">
      {t('This permission request is closed. No approval was granted.')}
    </p>;
  }
  const said =
    decision === 'deny'
      ? t('You denied it.')
      : decision === 'approve_task'
        ? t('You allowed it for the whole task.')
        : t('You allowed it.');
  return (
    <p className="fs-studio__answered" data-testid="studio-approval-answered">
      <Check size={13} aria-hidden="true" />
      {t('Permission answered')} · {said}
      {decision !== 'deny' && ` ${t('The work carried on from here.')}`}
    </p>
  );
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function CopyButton({ text, label = t('Copy') }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <IconButton
      icon={done ? Check : Copy}
      label={done ? t('Copied') : label}
      size="sm"
      onClick={() => {
        void copyText(text).then((ok) => {
          if (!ok) return;
          setDone(true);
          setTimeout(() => setDone(false), 1400);
        });
      }}
    />
  );
}

function Editor({
  initial,
  onCancel,
  onSave,
}: {
  initial: string;
  onCancel: () => void;
  onSave: (text: string, regenerate: boolean) => void;
}) {
  const [text, setText] = useState(initial);
  return (
    <div className="fs-studio__edit" data-testid="turn-editor">
      <textarea
        className="fs-studio__input fs-studio__edit-input"
        value={text}
        rows={3}
        aria-label={t('Edit message')}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') onCancel();
          if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) onSave(text, true);
        }}
        autoFocus
      />
      <div className="fs-studio__ask-actions">
        <Button variant="primary" icon={RefreshCw} label={t('Save and regenerate')} size="sm" disabled={!text.trim()} onClick={() => onSave(text, true)} />
        <Button label={t('Just save')} size="sm" disabled={!text.trim()} onClick={() => onSave(text, false)} />
        <Button variant="ghost" label={t('Cancel')} size="sm" onClick={onCancel} />
      </div>
    </div>
  );
}

/**
 * What the user typed, with the `@path` tokens turned into chips that open
 * the file, and (Lote 93) `FAU-12`-shaped ids turned into chips that open
 * the issue. Gated the same way the file-mention chip always was: with
 * nothing to open them against (no `onOpenFile`, no `boardKey`), the text
 * renders exactly as before.
 */
function Said({
  text,
  onOpenFile,
  boardKey,
  projectId,
  onOpenBoardIssue,
}: {
  text: string;
  onOpenFile?: (path: string) => void;
  boardKey?: string;
  projectId?: string;
  onOpenBoardIssue?: (id: string) => void;
}) {
  const hasMentions = Boolean(onOpenFile) && text.includes('@');
  if (!hasMentions && !boardKey) return <>{text}</>;
  const parts = hasMentions ? splitMentions(text) : [{ text, mention: undefined, token: '' } as ReturnType<typeof splitMentions>[number]];

  const renderIssue = (id: string) =>
    onOpenBoardIssue ? (
      <IssueChip key={id} id={id} onClick={() => onOpenBoardIssue(id)} />
    ) : projectId ? (
      <Link key={id} className="fs-issue-chip" to={`/projects/${encodeURIComponent(projectId)}?tab=board&issue=${encodeURIComponent(id)}`}>
        {id}
      </Link>
    ) : (
      <span key={id} className="fs-issue-chip" data-testid="issue-id-chip">{id}</span>
    );

  return (
    <>
      {parts.map((part, i) =>
        part.mention ? (
          <button
            key={i}
            type="button"
            className="fs-turn__mention"
            title={t('Open {path}').replace('{path}', part.mention)}
            onClick={() => onOpenFile?.(part.mention as string)}
          >
            {part.token}
          </button>
        ) : (
          <Fragment key={i}>{renderIssueSegments(part.text ?? '', boardKey, renderIssue)}</Fragment>
        ),
      )}
    </>
  );
}

/**
 * The server names the sections in English (src/context_ledger.py). They are a
 * closed list, so they can be translated here rather than shipping the locale
 * to the backend; anything new falls through as it came.
 */
function sectionLabel(label: string): string {
  switch (label) {
    case 'System prompt':
      return t('System prompt');
    case 'Tool schemas':
      return t('Tool schemas');
    case 'Project instructions':
      return t('Project instructions');
    case 'Skills':
      return t('Skills');
    case 'Memories':
      return t('Memories');
    case 'Documents & files':
      return t('Documents & files');
    case 'Web & research':
      return t('Web & research');
    case 'Attachments':
      return t('Attachments');
    case 'Other retrieved context':
      return t('Other retrieved context');
    case 'Tool results':
      return t('Tool results');
    case 'Conversation history':
      return t('Conversation history');
    case 'Your message':
      return t('Your message');
    default:
      return label;
  }
}

/**
 * Where the context went.
 *
 * "The local model ignored my instructions" is usually not a model
 * problem: it is nine thousand tokens of tool schemas, skills and
 * documents spent before the question. The server counts them
 * (`context_ledger`); this puts the number on screen at the round it
 * happens, folded away unless something is actually wrong.
 */
/**
 * CTX-02: what the most recent compaction pass actually did to this
 * session, and a way to protect one more fragment from the next pass — a
 * `<details>` of its own inside the Ledger, closed by default (checking is
 * an extra fetch, not free) so it costs nothing until someone actually
 * wonders "what did it throw away".
 */
function CompactionInspector({ sessionId, role, content }: { sessionId: string; role: string; content?: string }) {
  const [opened, setOpened] = useState(false);
  const [loading, setLoading] = useState(false);
  const [event, setEvent] = useState<CompactionEvent | null | 'error'>(null);
  const [pinned, setPinned] = useState<'idle' | 'pinning' | 'pinned' | 'failed'>('idle');

  const load = () => {
    if (loading) return;
    setLoading(true);
    fetchCompactionEvent(sessionId)
      .then((e) => setEvent(e))
      .catch(() => setEvent('error'))
      .finally(() => setLoading(false));
  };

  return (
    <details
      className="fs-ctx__compaction"
      data-testid="compaction-inspector"
      onToggle={(e) => {
        const next = e.currentTarget.open;
        setOpened(next);
        if (next && event === null && !loading) load();
      }}
    >
      <summary>{t('What was compacted')}</summary>
      {opened && (
        <div className="fs-ctx__compaction-body">
          {loading && <p className="fs-ctx__note">{t('Checking…')}</p>}
          {!loading && event === 'error' && <p className="fs-ctx__note" data-level="warn">{t('Could not read the compaction log.')}</p>}
          {!loading && event === null && <p className="fs-ctx__note">{t('Compaction has not run for this conversation yet.')}</p>}
          {!loading && event && event !== 'error' && (
            <p className="fs-ctx__note">
              {event.foldedCount > 0
                ? t('The last pass folded {n} earlier messages into a summary, keeping {refs} evidence reference(s) to the originals.', {
                    n: event.foldedCount,
                    refs: event.evidenceRefs.length,
                  })
                : t('Nothing has been folded away for this conversation yet.')}
              {event.pinnedSkipped > 0 && ` ${t('{n} pinned fragment(s) were kept untouched.', { n: event.pinnedSkipped })}`}
            </p>
          )}
          {content && (
            <Button
              size="sm"
              variant={pinned === 'pinned' ? 'primary' : undefined}
              icon={pinned === 'pinned' ? Check : undefined}
              label={pinned === 'pinning' ? t('Pinning…') : pinned === 'pinned' ? t('Pinned') : pinned === 'failed' ? t('Retry pin') : t('Pin this message')}
              disabled={pinned === 'pinning' || pinned === 'pinned'}
              title={t('Keeps this exact message out of future compaction passes.')}
              onClick={() => {
                setPinned('pinning');
                void pinCompactionFragment(sessionId, role, content, content.slice(0, 200)).then((fp) =>
                  setPinned(fp ? 'pinned' : 'failed'),
                );
              }}
              testId="compaction-pin"
            />
          )}
        </div>
      )}
    </details>
  );
}

function Ledger({ ledger, sessionId, role, content }: { ledger: ContextLedger; sessionId?: string | null; role?: string; content?: string }) {
  const tok = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n)));
  const warn = ledger.advice.some((a) => a.level === 'warn');
  return (
    <details className="fs-ctx" data-warn={warn || undefined} open={warn} data-testid="turn-ledger">
      <summary>
        <span className="fs-ctx__title">{t('Context')}</span>
        <span className="fs-ctx__head">
          {ledger.window ? t('{used} of {window} · {pct}% of the window', { used: tok(ledger.total), window: tok(ledger.window), pct: Math.round(ledger.percent) }) : t('{used} tokens', { used: tok(ledger.total) })}
        </span>
      </summary>
      <ul className="fs-ctx__rows">
        {ledger.sections.map((section) => (
          <li key={section.label}>
            <span className="fs-ctx__label" title={sectionLabel(section.label)}>{sectionLabel(section.label)}</span>
            <span className="fs-ctx__bar" aria-hidden="true">
              <span style={{ inlineSize: `${Math.min(100, section.percent)}%` }} />
            </span>
            <span className="fs-ctx__tok">{tok(section.tokens)}</span>
            <span className="fs-ctx__pct">{Math.round(section.percent)}%</span>
          </li>
        ))}
      </ul>
      {ledger.slim && (
        <p className="fs-ctx__note">
          {t('Tool prose trimmed to fit the window: {before} → {after} tokens (descriptions capped at {limit} characters, no tool removed).', {
            before: tok(ledger.slim.before),
            after: tok(ledger.slim.after),
            limit: ledger.slim.limit,
          })}
        </p>
      )}
      {ledger.advice.map((a, i) => (
        <p key={i} className="fs-ctx__note" data-level={a.level}>
          {a.text}
        </p>
      ))}
      {sessionId && role && <CompactionInspector sessionId={sessionId} role={role} content={content} />}
    </details>
  );
}

/**
 * The tool-fence regex, once per page.
 *
 * A hook rather than a module-level await: the tag list comes from the
 * server (`GET /api/tools`), and nothing should be stripped until it has
 * actually arrived — a fence shown for a moment is a blemish, a paragraph
 * wrongly deleted is a lie.
 */
function useFenceRegex(): RegExp | null {
  const [re, setRe] = useState<RegExp | null>(null);
  useEffect(() => {
    let alive = true;
    void toolFenceRegex().then((r) => {
      if (alive) setRe(r);
    });
    return () => {
      alive = false;
    };
  }, []);
  return re;
}

function UserTurn({
  turn,
  busy,
  enter,
  onEdit,
  onRegenerate,
  onDelete,
  onOpenFile,
  boardKey,
  projectId,
  onOpenBoardIssue,
}: {
  turn: Turn;
  busy: boolean;
  /** Play the arrival animation: only true the render a turn's id was first
   *  seen, never again when virtualization remounts it on scroll (PERF-01). */
  enter?: boolean;
  onEdit: TranscriptProps['onEdit'];
  onRegenerate: TranscriptProps['onRegenerate'];
  onDelete: TranscriptProps['onDelete'];
  onOpenFile?: TranscriptProps['onOpenFile'];
  boardKey?: TranscriptProps['boardKey'];
  projectId?: TranscriptProps['projectId'];
  onOpenBoardIssue?: TranscriptProps['onOpenBoardIssue'];
}) {
  const [editing, setEditing] = useState(false);
  return (
    <article className="fs-turn fs-turn--user" data-enter={enter || undefined} data-nav-id={turn.id} data-db-id={turn.dbId} data-testid="turn-user">
      <div className="fs-turn__user-wrap">
        {editing ? (
          <Editor
            initial={turn.text}
            onCancel={() => setEditing(false)}
            onSave={(text, regenerate) => {
              setEditing(false);
              onEdit(turn, text, regenerate);
            }}
          />
        ) : (
          <>
            <div className="fs-turn__bubble">
              {turn.attachments.length > 0 && (
                <ul className="fs-studio__attachments fs-studio__attachments--sent" aria-label={t('Attachments')}>
                  {turn.attachments.map((a) => (
                    <li key={a.id} className="fs-studio__attachment">
                      {isImage(a.mime) ? (
                        <a href={attachmentUrl(a.id)} target="_blank" rel="noreferrer">
                          <img src={attachmentUrl(a.id)} alt={a.name} width={72} height={72} loading="lazy" />
                        </a>
                      ) : (
                        <a className="fs-link" href={attachmentUrl(a.id)} target="_blank" rel="noreferrer">
                          <FileText size={14} aria-hidden="true" /> {a.name}
                        </a>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              <Said text={turn.text} onOpenFile={onOpenFile} boardKey={boardKey} projectId={projectId} onOpenBoardIssue={onOpenBoardIssue} />
              {turn.edited && <span className="fs-turn__edited">{t('edited')}</span>}
            </div>
            <div className="fs-turn__actions" data-testid="turn-actions">
              <CopyButton text={turn.text} />
              {turn.dbId && !busy && (
                <>
                  <IconButton icon={Pencil} label={t('Edit')} size="sm" onClick={() => setEditing(true)} />
                  <IconButton icon={RefreshCw} label={t('Regenerate from here')} size="sm" onClick={() => onRegenerate(turn)} />
                  <IconButton icon={Trash2} label={t('Delete message')} size="sm" onClick={() => onDelete(turn)} />
                </>
              )}
            </div>
          </>
        )}
      </div>
    </article>
  );
}

/**
 * TASK-01: the plan by structured steps, foldable, with a step the MODEL
 * marked done but nobody verified shown as such rather than as a plain
 * checkmark. Only populated on history restore today (see `PlanStepView`'s
 * doc comment in model.ts for why a still-streaming turn doesn't have it) —
 * additive to the existing `<Harness plan={turn.plan} .../>` markdown card
 * above it, which keeps rendering exactly as before either way.
 */
/**
 * PLAN-01: a long plan (dozens of steps, e.g. a many-section research brief)
 * used to dump every step at once inside the expanded card — correct, but
 * unreadable past a screenful. `planStepsPreview` slices to the first `limit`
 * and reports how many are hidden, so the card can show a short list plus a
 * counter and let a click reveal the rest, instead of an ever-growing wall.
 * Pure so it is checked directly (studio/checks/l71-plan-steps-preview.check.mjs)
 * without mounting the component.
 */
export const PLAN_STEPS_PREVIEW_LIMIT = 12;

export function planStepsPreview<T>(steps: T[], limit: number = PLAN_STEPS_PREVIEW_LIMIT): { shown: T[]; hidden: number } {
  if (steps.length <= limit) return { shown: steps, hidden: 0 };
  return { shown: steps.slice(0, limit), hidden: steps.length - limit };
}

function PlanStepsCard({ steps, revision, warnings }: { steps: PlanStepView[]; revision?: number; warnings?: string[] }) {
  const done = steps.filter((step) => step.status === 'done').length;
  const [expanded, setExpanded] = useState(false);
  const preview = planStepsPreview(steps);
  const visible = expanded ? steps : preview.shown;
  return (
    <details className="fs-studio__thinking" data-testid="plan-steps-card">
      <summary>
        {t('Plan steps')} ({done}/{steps.length}){typeof revision === 'number' ? ` · rev ${revision}` : ''}
      </summary>
      <ul style={{ listStyle: 'none', margin: '6px 0 0', padding: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
        {visible.map((step) => (
          <li
            key={step.id}
            style={{ display: 'flex', alignItems: 'baseline', gap: 6, paddingLeft: step.dependsOn.length ? 16 : 0 }}
          >
            <span aria-hidden="true">{step.status === 'done' ? '☑' : step.status === 'blocked' ? '⛔' : '☐'}</span>
            {/* The title is the model's own markdown (`**1. Schema**`, backticks);
                render it, don't print the asterisks. Seen live on the 7001. */}
            <span className="fs-studio__plan-step" style={{ textDecoration: step.status === 'done' ? 'line-through' : undefined }}>
              <Rich text={step.title} />
            </span>
            {step.status === 'done' && !step.verified && (
              <span style={{ opacity: 0.7, fontSize: '0.85em' }}>{t('Marked done — not yet verified')}</span>
            )}
            {step.status === 'blocked' && <span style={{ opacity: 0.7, fontSize: '0.85em' }}>{t('Blocked')}</span>}
          </li>
        ))}
      </ul>
      {!expanded && preview.hidden > 0 && (
        <button
          type="button"
          className="fs-link"
          style={{ marginTop: 6, fontSize: '0.85em' }}
          data-testid="plan-steps-expand"
          onClick={() => setExpanded(true)}
        >
          {t('+{n} more · Show all', { n: preview.hidden })}
        </button>
      )}
      {expanded && preview.hidden > 0 && (
        <button
          type="button"
          className="fs-link"
          style={{ marginTop: 6, fontSize: '0.85em' }}
          data-testid="plan-steps-collapse"
          onClick={() => setExpanded(false)}
        >
          {t('Show fewer')}
        </button>
      )}
      {warnings && warnings.length > 0 && (
        <p className="fs-prose" style={{ opacity: 0.7, fontSize: '0.85em', marginTop: 6 }}>
          {t('Some lines could not be parsed as steps.')}
        </p>
      )}
    </details>
  );
}

// CMP-09/CMP-12 (W3-A): fast/balanced/deep_review, same three values
// Composer.tsx's own STRATEGY_PROFILE_CHOICES chip names — a short,
// translated word here rather than that chip's longer one-line consequence,
// since this line is meant to stay discreet and collapsed.
const STRATEGY_PROFILE_WORD: Record<string, string> = {
  fast: t('fast'),
  balanced: t('balanced'),
  deep_review: t('deep review'),
};

/**
 * CMP-09/CMP-12 (W3-A): a single discreet, collapsible line under a finished
 * turn — "Estrategia: {method} · {profile} — {reason}" — showing what
 * `strategy_policy.choose_strategy` decided for this turn (`turn.strategy`,
 * from the live `strategy` SSE event or restored from `metadata.strategy`).
 * Collapsed by default like `ContextReceiptCard` right below; the full
 * reasons/steps/budget sit inside for whoever opens it.
 */
function StrategyLine({ strategy }: { strategy: TurnStrategy }) {
  const profile = STRATEGY_PROFILE_WORD[strategy.profile] ?? strategy.profile;
  const reason = strategy.reasons[0] ?? '';
  return (
    <details className="fs-studio__thinking" data-testid="turn-strategy">
      <summary>
        {t('Strategy: {method} · {profile}{reason}', { method: strategy.method, profile, reason: reason ? ` — ${reason}` : '' })}
      </summary>
      {strategy.recipeId && <p className="fs-prose">{t('Active recipe: {id}', { id: strategy.recipeId })}</p>}
      {strategy.reasons.length > 1 && (
        <ul className="fs-ctx__compaction-body">
          {strategy.reasons.map((r, i) => (
            <li key={i} className="fs-ctx__note">{r}</li>
          ))}
        </ul>
      )}
      {strategy.steps.length > 0 && (
        <ol className="fs-prose">
          {strategy.steps.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      )}
    </details>
  );
}

/**
 * CMP-04: "Contexto usado (n) — por qué" — a compact, collapsed-by-default
 * card listing what the turn's delivered context packets actually put in
 * front of the model (`event: context_receipts`, `src/agent_loop.py`),
 * each row with the short reason the server already wrote and, when its
 * `ref` is a URL, a link to open it.
 *
 * Reads `turn.contextReceipts` defensively (`unknown` cast) rather than off
 * a typed `Turn` field: the live-stream reducer that turns
 * `ChatEvent['context_receipts']` into that field lives in
 * `studio/src/screens/studio/model.ts`, outside this lot's file list
 * (CONTRATO_CMP_W2.md, W2-D) — see the final report's "wiring pendiente"
 * note. Until `model.ts` adds the field this renders nothing, exactly as a
 * server that predates the event already does.
 */
function ContextReceiptCard({ turn }: { turn: Turn }) {
  const receipts = turn.contextReceipts;
  if (!receipts || receipts.length === 0) return null;
  return (
    <details className="fs-ctx__compaction" data-testid="context-receipts">
      <summary>{t('Context used ({n}) — why', { n: receipts.length })}</summary>
      <ul className="fs-ctx__compaction-body">
        {receipts.slice(0, 40).map((r, i) => {
          const href = /^https?:\/\//i.test(r.ref) ? r.ref : '';
          return (
            <li key={`${r.kind}:${r.ref}:${i}`} className="fs-ctx__note">
              {r.why || r.source}
              {' — '}
              {href ? (
                <a className="fs-link" href={href} target="_blank" rel="noreferrer">
                  {t('Open source')}
                </a>
              ) : (
                <code>{r.ref}</code>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

/** F3 (CONTRATO_CABLES2): a condensed-range summary row (`role === 'system'`,
 *  `metadata.condensed`) — a compact card, never rendered through
 *  `AssistantTurn`'s full machinery (it has no steps, no metrics, nothing
 *  streaming). "Expand" is the only action: it hands the range back exactly
 *  as it was, byte-for-byte (`POST .../condense/{index}/expand`). */
function CondensedTurn({
  turn,
  onExpand,
  busy,
}: {
  turn: Turn;
  onExpand?: () => void;
  busy: boolean;
}) {
  const info = turn.condensed;
  const body = turn.text.replace(/^\[Condensed:[^\n]*\]\n?/, '');
  const heading = info
    ? tn(info.count, 'Summary of {n} turn ({a}–{b})', 'Summary of {n} turns ({a}–{b})', { n: info.count, a: info.from + 1, b: info.to + 1 })
    : t('Condensed turns');
  return (
    <article className="fs-turn fs-turn--condensed" data-nav-id={turn.id} data-testid="turn-condensed">
      <span className="fs-turn__node" aria-hidden="true" />
      <div className="fs-turn__body">
        <details className="fs-condensed">
          <summary className="fs-condensed__heading">{heading}</summary>
          <div className="fs-condensed__body">
            <Said text={body} />
          </div>
        </details>
        {onExpand && !busy && (
          <div className="fs-turn__foot">
            <span className="fs-turn__actions" data-testid="turn-actions">
              <Button variant="ghost" size="sm" icon={Expand} label={t('Expand the original turns')} onClick={onExpand} testId="turn-expand" />
            </span>
          </div>
        )}
      </div>
    </article>
  );
}

function AssistantTurn({
  turn: liveTurn,
  busy,
  enter,
  sessionId,
  onApproval,
  onAnswer,
  onRegenerate,
  onDelete,
  onNotice,
  onOpenFile,
  onOpenDoc,
  onOpenEvidence,
  onRerun,
  onFork,
  onExplore,
  onCondense,
  onOpenSourceControl,
  boardKey,
  projectId,
}: {
  turn: Turn;
  busy: boolean;
  /** See `UserTurn`'s doc comment for the same prop. */
  enter?: boolean;
  /** CTX-02: threaded to `Ledger`'s "Qué se compactó". */
  sessionId: string | null;
  onApproval: (decision: Decision) => void;
  onAnswer: (text: string, optionIds?: string[]) => void;
  onRegenerate: () => void;
  onDelete: () => void;
  onNotice: TranscriptProps['onNotice'];
  onOpenFile?: TranscriptProps['onOpenFile'];
  onOpenDoc?: TranscriptProps['onOpenDoc'];
  onOpenEvidence?: TranscriptProps['onOpenEvidence'];
  onRerun?: TranscriptProps['onRerun'];
  onFork?: () => void;
  /** B2: "Explorar desde aquí" — bound with this turn's own `historyIndex`
   *  and no `passage`, unlike the floating selection button's call. */
  onExplore?: () => void;
  /** F3: "Condense up to here" — bound with this turn's own `historyIndex`. */
  onCondense?: () => void;
  onOpenSourceControl?: TranscriptProps['onOpenSourceControl'];
  boardKey?: TranscriptProps['boardKey'];
  projectId?: TranscriptProps['projectId'];
}) {
  // PERF-01/UX-05: while streaming, repaint this card at most once per
  // frame — see `useFrameBatched`'s doc comment. A settled turn (most of a
  // long transcript, at any moment) renders straight off the prop, no
  // batching in the way.
  const turn = useFrameBatched(liveTurn, liveTurn.streaming);
  // CMP-12 (W3-A): "Guardar como receta" on a finished turn's TURN SUMMARY —
  // `POST /api/recipes/from-run/{run_id}` (`adapters/strategy.ts`'s
  // `createRecipeFromRun`). `run_id` here IS the session id: the run log
  // `src/recipes.py::from_run` reads is `DATA_DIR/runs/<session>.jsonl`
  // (`src/agent_runs.py`'s own documented shape, `docs/api/strategy.md`'s
  // "Límites"), so no separate per-turn run id needs threading through —
  // this session's own `sessionId` prop already names the right log.
  const [savingRecipe, setSavingRecipe] = useState(false);
  const saveAsRecipe = async () => {
    if (!sessionId || savingRecipe) return;
    setSavingRecipe(true);
    try {
      const recipe = await createRecipeFromRun(sessionId);
      onNotice(t('Saved as a draft recipe: {id}', { id: recipe.id }));
    } catch (e) {
      onNotice((e as Error).message, 'danger');
    } finally {
      setSavingRecipe(false);
    }
  };
  // The tool call has already run and is in the rail; its fence is leftovers.
  const fences = useFenceRegex();
  const body = linkifyBoardIds(stripExecutedFences(turn.text, fences), boardKey, projectId);
  // A11Y-02: grouped, not per-token — see useGroupedStreamAnnouncement above.
  const streamAnnouncement = useGroupedStreamAnnouncement(body, turn.streaming);
  return (
    <article className="fs-turn fs-turn--assistant" data-enter={enter || undefined} data-nav-id={turn.id} data-db-id={turn.dbId} data-streaming={turn.streaming || undefined} data-testid="turn-assistant">
      <span className="fs-turn__node" aria-hidden="true" />
      <div className="fs-turn__body">
        {turn.speaker && <p className="fs-turn__speaker">{turn.speaker}</p>}
        {turn.thinking && getDisplay().thinking && (
          <details className="fs-studio__thinking">
            <summary>{t('Reasoning')} {turn.streaming && !turn.text ? <span className="fs-studio__pulse" /> : null}</summary>
            <p className="fs-prose">{turn.thinking}</p>
          </details>
        )}
        {(turn.plan || (turn.todos && turn.todos.length > 0) || (turn.streaming && turn.checks.length > 0)) && (
          <Suspense fallback={null}>
            <Harness
              mode="live"
              plan={turn.plan}
              todos={turn.todos}
              checks={turn.streaming ? turn.checks.slice(-3) : []}
              answer={turn.text}
              onNotice={onNotice}
            />
          </Suspense>
        )}
        {turn.planSteps && turn.planSteps.length > 0 && (
          <PlanStepsCard steps={turn.planSteps} revision={turn.planRevision} warnings={turn.planWarnings} />
        )}
        {turn.steps.length > 0 && <ToolRail steps={turn.steps} live={turn.streaming} sessionId={sessionId} onOpenFile={onOpenFile} onOpenDoc={onOpenDoc} onOpenEvidence={onOpenEvidence} />}
        {turn.workers.length > 0 && (
          <Suspense fallback={null}>
            <SubagentBoard workers={turn.workers} live={turn.streaming} onRerun={onRerun ?? (() => undefined)} onNotice={onNotice} />
          </Suspense>
        )}
        {turn.research && !turn.research.done && turn.streaming && <ResearchLine research={turn.research} />}
        {body && <Rich text={body} onOpenFile={onOpenFile} />}
        {turn.streaming && body && <span className="fs-studio__cursor" aria-hidden="true" />}
        {turn.streaming && <span className="fs-sr-only" aria-live="polite" data-testid="stream-announcement">{streamAnnouncement}</span>}
        {turn.images.map((url) => (
          <img key={url} className="fs-studio__image" src={url} alt={t('Generated image')} loading="lazy" />
        ))}
        {turn.sources.length > 0 && (
          <p className="fs-studio__sources">
            {turn.sources.slice(0, 6).map((s) => {
              // A citation's URL came from outside; a `javascript:` one is a
              // script, so it is shown as text rather than made clickable.
              const href = safeExternal(s.url);
              return href ? (
                <a key={s.url} className="fs-link" href={href} target="_blank" rel="noreferrer">
                  {s.title.slice(0, 48)}
                </a>
              ) : (
                <span key={s.url} className="fs-studio__source-flat">{s.title.slice(0, 48)}</span>
              );
            })}
          </p>
        )}
        {!turn.streaming && turn.strategy && <StrategyLine strategy={turn.strategy} />}
        {!turn.streaming && <ContextReceiptCard turn={turn} />}
        {turn.ask && <AskCard ask={turn.ask} busy={busy} onApproval={onApproval} onAnswer={onAnswer} />}
        {!turn.ask && turn.approval && <AnsweredCard decision={turn.approval.decision} />}
        {!turn.streaming && (turn.summary || turn.checks.length > 0) && (
          <Suspense fallback={null}>
            <Harness mode="final" summary={turn.summary} checks={turn.checks} answer={turn.text} onNotice={onNotice} permissionAnswered={Boolean(turn.approval)} />
          </Suspense>
        )}
        {turn.note && (
          <p className="fs-notice" data-tone="warning">
            {turn.note}
          </p>
        )}
        {/* F1 (CONTRATO_CABLES2): this reply was saved against an earlier
            version of a wire (reference/document/note) it used — a discreet
            line, not an alarm, since nothing is actually wrong: the source
            simply moved on after this was written. Regenerating resends the
            CURRENT block, so the banner clears on its own once the fresh
            reply is saved (`note_wires_used` re-stamps the snapshot). */}
        {!turn.streaming && turn.staleWires && turn.staleWires.length > 0 && (
          <p className="fs-notice" data-tone="warning" data-testid="turn-stale-wire">
            {t('Written against an earlier version of "{names}"', {
              names: turn.staleWires.map((w) => w.label).join(', '),
            })}
            {' '}
            <Button size="sm" label={t('Regenerate with the current version')} onClick={onRegenerate} />
          </p>
        )}
        {turn.uncertain && (
          <p className="fs-notice" data-tone="warning" data-testid="turn-uncertain">
            {t('Uncertain outcome, checking…')}
          </p>
        )}
        {turn.capabilitiesChanged && (
          <p className="fs-notice" data-tone="warning" data-testid="turn-capabilities-changed">
            {t('Switched from {from} to {to}: lost {lost}.', {
              from: turn.capabilitiesChanged.fromModel || t('the previous model'),
              to: turn.capabilitiesChanged.toModel || t('another model'),
              lost: turn.capabilitiesChanged.lost.join(', '),
            })}
          </p>
        )}
        {turn.gitPolicy.length > 0 && (
          <div className="fs-studio__git-policy" data-testid="turn-git-policy">
            {turn.gitPolicy.map((ev, i) => {
              const Icon = ev.action === 'branch' ? GitBranch : ev.action === 'commit' ? GitCommit : UploadCloud;
              const short = (ev.sha ?? '').slice(0, 7);
              const label = !ev.ok
                ? ev.action === 'branch'
                  ? t('Could not switch to a working branch{detail}', { detail: ev.detail ? `: ${ev.detail}` : '' })
                  : ev.action === 'commit'
                    ? t('Could not commit{detail}', { detail: ev.detail ? `: ${ev.detail}` : '' })
                    : t('Could not push{detail}', { detail: ev.detail ? `: ${ev.detail}` : '' })
                : ev.action === 'branch'
                  ? t('Switched to {branch}', { branch: ev.branch ?? '' })
                  : ev.action === 'commit'
                    ? t('Committed {sha} on {branch}', { sha: short, branch: ev.branch ?? '' })
                    : t('Pushed {branch}', { branch: ev.branch ?? '' });
              return onOpenSourceControl ? (
                <button
                  key={i}
                  type="button"
                  className="fs-studio__git-chip"
                  data-ok={ev.ok}
                  data-testid="git-policy-chip"
                  onClick={onOpenSourceControl}
                  title={t('Open Source control')}
                >
                  <Icon size={12} aria-hidden="true" /> {label}
                </button>
              ) : (
                <span key={i} className="fs-studio__git-chip" data-ok={ev.ok} data-testid="git-policy-chip">
                  <Icon size={12} aria-hidden="true" /> {label}
                </span>
              );
            })}
          </div>
        )}
        {turn.error && (() => {
          // UX-08: the taxonomy (`src/contracts/errors.py`, mirrored client-side
          // in errorTaxonomy.ts) instead of raw provider prose — same pattern as
          // Activity.tsx's task-failure card. `turn.errorClass` is authoritative
          // when a decoded event carried it (OBS-03); `friendlyError` also sniffs
          // a JSON-blob `turn.error` for the same field, so a turn from before
          // that plumbing still gets a useful title + action instead of nothing.
          const friendly = turn.errorClass
            ? { ...describeError(turn.errorClass), message: turn.error }
            : friendlyError(turn.error);
          return (
            <p className="fs-notice" data-tone="danger">
              {friendly.category
                ? t('{title}: {message} — {action}', { title: friendly.title, message: friendly.message, action: friendly.action })
                : friendly.message}
            </p>
          );
        })()}
        {turn.ledger && <Ledger ledger={turn.ledger} sessionId={sessionId} role="assistant" content={turn.text} />}
        {/* The heartbeat, last of all: it sits exactly where the turn's own
            numbers will appear when it finishes. */}
        {turn.streaming && turn.live && !(turn.research && !turn.research.done) && <LiveLine live={turn.live} contextTokens={turn.ledger?.total} />}
        {!turn.streaming && (
          <div className="fs-turn__foot">
            {turn.metrics && (
              <span className="fs-turn__metrics">
                {formatMetrics(turn.metrics)}
                {turn.rounds > 1 ? ` · ${tn(turn.rounds, '{n} round', '{n} rounds')}` : ''}
              </span>
            )}
            <span className="fs-turn__actions" data-testid="turn-actions">
              {turn.text && <CopyButton text={turn.text} label={t('Copy reply')} />}
              {turn.text && <SpeakButton text={turn.text} />}
              {turn.dbId && !busy && (
                <>
                  <IconButton icon={RefreshCw} label={t('Regenerate')} size="sm" onClick={onRegenerate} />
                  {onFork && <IconButton icon={GitFork} label={t('Fork from here')} size="sm" onClick={onFork} testId="turn-fork" />}
                  {onExplore && <IconButton icon={GitBranchPlus} label={t('Explore from here')} size="sm" onClick={onExplore} testId="turn-explore" />}
                  {onCondense && <IconButton icon={FoldVertical} label={t('Condense up to here')} size="sm" onClick={onCondense} testId="turn-condense" />}
                  {sessionId && (
                    <IconButton
                      icon={BookmarkPlus}
                      label={t('Save as recipe')}
                      size="sm"
                      disabled={savingRecipe}
                      onClick={() => void saveAsRecipe()}
                      testId="turn-save-recipe"
                    />
                  )}
                  <IconButton icon={Trash2} label={t('Delete message')} size="sm" onClick={onDelete} />
                </>
              )}
            </span>
          </div>
        )}
        {!turn.streaming && turn.metrics?.execution && <ExecutionTimeline execution={turn.metrics.execution} />}
      </div>
    </article>
  );
}

/**
 * What the turn is doing right now, and how fast.
 *
 * A turn between two tool calls emits nothing the transcript drew: the rail
 * showed three finished reads and then silence, which is exactly what a dead
 * turn looks like. This line is the heartbeat — it names the phase, keeps a
 * clock on it, and while the model is decoding it says the speed.
 *
 * The speed is measured here, from the gaps between the chunks that arrive,
 * so it carries a `~`. The server's own figure lands in the footer when the
 * turn ends, and that one is the number of record.
 */
function LiveLine({ live, contextTokens }: { live: LiveRate; contextTokens?: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 500);
    return () => window.clearInterval(timer);
  }, []);
  const decoding = live.phase === 'thinking' || live.phase === 'writing';
  const tps = decoding ? liveTps(live) : null;
  const secs = Math.max(0, Math.floor((Date.now() - live.phaseAt) / 1000));
  const silentFor = Math.max(0, Math.floor((Date.now() - live.lastAt) / 1000));
  const clock = `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`;
  const what =
    live.phase === 'tool'
      ? live.label || t('Running a tool')
      : live.phase === 'thinking'
        ? t('Thinking')
        : live.phase === 'writing'
          ? t('Writing')
          // Before the first token the model is reading the whole context
          // (prefill). On a model spilling to RAM that takes minutes for a
          // long chat, and "waiting" alone reads as a hang — say what it is
          // doing once the wait is long enough to wonder.
          // The server's heartbeat says what the model is really doing when
          // it knows (loading / reading the context / spilling to RAM / the
          // VRAM gate); that beats "waiting" every time.
          : live.label
            ? contextTokens && live.label === t('The model is reading the context')
              ? t('The model is reading {n} tokens of context', { n: contextTokens.toLocaleString() })
              : live.label
            : secs >= 8 && contextTokens
              ? t('Waiting for the model — reading {n} tokens of context', { n: contextTokens.toLocaleString() })
              : t('Waiting for the model');
  return (
    <p className="fs-studio__waiting fs-studio__live" data-phase={live.phase} data-testid="turn-live">
      <span className="fs-studio__pulse" aria-hidden="true" />
      <span className="fs-studio__live-what" role="status">
        {what}
      </span>
      {tps !== null && (
        <span
          className="fs-studio__clock"
          title={t('Measured here, from the stream. The server gives its own figure when the turn ends.')}
        >
          · ~{tps.toFixed(1)} tok/s
        </span>
      )}
      <span className="fs-studio__clock"> · {clock}</span>
      {silentFor >= 20 && (
        <span className="fs-studio__clock" data-stale="true">
          {' '}· {t('No server signal for {n}s', { n: silentFor })}
        </span>
      )}
    </p>
  );
}

/**
 * RES-01: the schema's coverage as of the latest `analyzing` event — one
 * line per subquestion, marked pending/thin/covered, so the reader can see
 * which parts of the brief still have nothing behind them without waiting
 * for the final report to find out. Same collapsible-list shape as
 * `PlanStepsCard` above, deliberately: both are "here is the checklist, and
 * where it stands".
 */
function CoverageMap({ coverage }: { coverage: CoverageItem[] }) {
  const covered = coverage.filter((c) => c.status === 'covered').length;
  const insufficient = coverage.filter((c) => c.status === 'insufficient').length;
  const pending = coverage.length - covered - insufficient;
  return (
    <details className="fs-studio__thinking" data-testid="research-coverage">
      <summary>
        {t('Coverage map')} · {t('{covered} covered, {thin} thin, {pending} pending', { covered, thin: insufficient, pending })}
      </summary>
      <ul style={{ listStyle: 'none', margin: '6px 0 0', padding: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
        {coverage.map((item, i) => (
          <li key={i} style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <span aria-hidden="true">{item.status === 'covered' ? '☑' : item.status === 'insufficient' ? '◐' : '☐'}</span>
            <span className="fs-prose">{item.question}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

/** RES-04: `_final_report_in_parts` (src/deep_research.py) names the part it
 *  is on in its own progress message ("Writing sections 5-8 of 12 (part 2
 *  of 3)") — no separate structured field exists for it, so this reads the
 *  same message `ResearchLine` already shows and turns "part N of M" into a
 *  written/pending count: N-1 parts are already on disk (a part fails only
 *  into a placeholder, never a gap — RES-04's backend half), part N is the
 *  one being written now. `null` on any other phase, or a message shape
 *  this does not recognise — nothing to show, not a guess. */
export function reportPartsProgress(message: string): { written: number; total: number } | null {
  const m = /part (\d+) of (\d+)/i.exec(message);
  if (!m) return null;
  const idx = Number(m[1]);
  const total = Number(m[2]);
  if (!Number.isFinite(idx) || !Number.isFinite(total) || total <= 0) return null;
  return { written: Math.max(0, Math.min(total, idx - 1)), total };
}

/** Deep Research before the answer: the phase, the round and a clock. */
function ResearchLine({ research }: { research: NonNullable<Turn['research']> }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const secs = Math.max(0, Math.floor((Date.now() - research.startedAt) / 1000));
  const clock = `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`;
  const avg = research.avgDuration ? ` / ~${String(Math.floor(research.avgDuration / 60)).padStart(2, '0')}:${String(Math.round(research.avgDuration % 60)).padStart(2, '0')}` : '';
  const phase: Record<string, string> = {
    probing: t('probing the model'),
    loading_model: t('loading the model into memory'),
    vram_blocked: t('no room in VRAM — waiting for you to choose what to unload'),
    unloading_model: t('unloading models to make room'),
    planning: t('planning'),
    searching: t('searching'),
    reading: t('reading {n} sources', { n: research.totalSources }),
    analyzing: t('analysing'),
    writing: t('writing the report'),
  };
  // RES-04: "informe vivo" — the only structured signal for it is the part
  // count folded into the writing-phase message itself (see
  // `reportPartsProgress`'s doc comment); nothing to show on any other phase.
  const parts = research.phase === 'writing' ? reportPartsProgress(research.message) : null;
  return (
    <>
      <p className="fs-studio__waiting fs-studio__research" aria-live="polite">
        <Telescope size={13} aria-hidden="true" />
        {t('Deep Research')}
        {research.round ? ` · ${t('round {n}', { n: research.round })}` : ''} · {research.message || phase[research.phase] || research.phase} · <span className="fs-studio__clock">{clock}{avg}</span>
        {parts && (
          <span className="fs-studio__clock" data-testid="research-report-parts">
            {' '}· {t('report: {written}/{total} sections written', { written: parts.written, total: parts.total })}
          </span>
        )}
      </p>
      {research.coverage && research.coverage.length > 0 && <CoverageMap coverage={research.coverage} />}
    </>
  );
}

/** No turn has been measured yet at this height — corrected the moment
 *  each row mounts and reports its real size (`measureElement`); only
 *  changes how many off-screen rows the very first paint guesses at. */
const ESTIMATED_TURN_HEIGHT = 180;
/** Same "close enough to the bottom" reading as Studio.tsx's own
 *  pinned-scroll effect (`pinnedRef`, a fichero ajeno to this lote), so the
 *  back-to-bottom button and the auto-scroll agree on what "at the bottom"
 *  means without the two files sharing state. */
const BOTTOM_THRESHOLD = 80;

export function Transcript({ turns, busy, sessionId, onApproval, onAnswer, onEdit, onRegenerate, onDelete, onNotice, onOpenFile, onOpenDoc, onOpenEvidence, onRerun, onFork, onQuote, onExplore, onCondense, onExpandCondensed, onOpenSourceControl, boardKey, projectId, onOpenBoardIssue }: TranscriptProps) {
  const quote = useQuoteSelection(onQuote, Boolean(onExplore));

  // PERF-01/QA-37: Studio.tsx owns the actual scrolling element
  // (`.fs-studio__scroll`) and is off-limits to this lote, so it is found
  // from here instead of threaded down as a prop — this root node is
  // always its direct content, whether loading/hero/empty siblings are
  // present or not (see Studio.tsx's `fs-studio__transcript-stage`).
  const scrollElRef = useRef<HTMLElement | null>(null);
  const setRootRef = useCallback(
    (el: HTMLDivElement | null) => {
      quote.holder.current = el;
      scrollElRef.current = el ? el.closest<HTMLElement>('.fs-studio__scroll') : null;
    },
    [quote.holder],
  );

  const rowVirtualizer = useVirtualizer({
    count: turns.length,
    getScrollElement: () => scrollElRef.current,
    estimateSize: () => ESTIMATED_TURN_HEIGHT,
    overscan: 8,
    getItemKey: useCallback((index: number) => turns[index]?.id ?? index, [turns]),
  });

  // A turn's id enters this set the first render it is seen at all; a LATER
  // render — virtualization mounting it again after a scroll, or a delta
  // updating some other turn — never replays its arrival animation (see
  // `.fs-turn[data-enter]` in studio.css). Updated in an effect, which runs
  // after the render that reads it, so a genuinely new id still reads as new.
  const seenIds = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const turn of turns) seenIds.current.add(turn.id);
  }, [turns]);

  // The "new messages" button: independent of Studio.tsx's own `pinnedRef`
  // (which drives the actual auto-scroll and is a fichero ajeno), reading
  // the same element and the same threshold so the two never disagree about
  // what "at the bottom" means.
  const [pastBottom, setPastBottom] = useState(false);
  useEffect(() => {
    const el = scrollElRef.current;
    if (!el) return;
    const check = () => setPastBottom(el.scrollHeight - el.scrollTop - el.clientHeight >= BOTTOM_THRESHOLD);
    check();
    el.addEventListener('scroll', check, { passive: true });
    return () => el.removeEventListener('scroll', check);
  }, []);

  const items = rowVirtualizer.getVirtualItems();

  return (
    <div className="fs-studio__turns" ref={setRootRef} style={{ blockSize: rowVirtualizer.getTotalSize() }}>
      {quote.pos && (onQuote || onExplore) && (
        <div
          className="fs-studio__quote"
          role="group"
          aria-label={t('Selection actions')}
          style={{ insetInlineStart: quote.pos.x, insetBlockStart: quote.pos.y }}
        >
          {onQuote && (
            <button
              type="button"
              className="fs-studio__quote-btn"
              onClick={() => {
                onQuote(quote.pos?.text ?? '');
                quote.clear();
                window.getSelection()?.removeAllRanges();
              }}
              data-testid="turn-quote"
            >
              <Quote size={13} aria-hidden="true" /> Citar
            </button>
          )}
          {onExplore && quote.pos.turnId && (
            <button
              type="button"
              className="fs-studio__quote-btn"
              onClick={() => {
                const turnId = quote.pos?.turnId;
                const passage = quote.pos?.text ?? '';
                const index = turns.findIndex((tu) => tu.id === turnId);
                const turn = index === -1 ? undefined : turns[index];
                quote.clear();
                window.getSelection()?.removeAllRanges();
                if (turn) onExplore({ historyIndex: turn.historyIndex ?? index, passage });
              }}
              data-testid="explore-selection"
            >
              <GitBranchPlus size={13} aria-hidden="true" /> {t('Explore separately')}
            </button>
          )}
        </div>
      )}
      {items.map((virtualItem) => {
        const turn = turns[virtualItem.index];
        if (!turn) return null;
        const index = virtualItem.index;
        const enter = !seenIds.current.has(turn.id);
        return (
          <div
            key={virtualItem.key}
            ref={rowVirtualizer.measureElement}
            data-index={virtualItem.index}
            className="fs-studio__turn-row"
            style={{ transform: `translateY(${virtualItem.start}px)` }}
          >
            {turn.role === 'user' ? (
              <UserTurn turn={turn} busy={busy} enter={enter} onEdit={onEdit} onRegenerate={onRegenerate} onDelete={onDelete} onOpenFile={onOpenFile} boardKey={boardKey} projectId={projectId} onOpenBoardIssue={onOpenBoardIssue} />
            ) : turn.role === 'system' ? (
              <CondensedTurn
                turn={turn}
                busy={busy}
                onExpand={onExpandCondensed ? () => onExpandCondensed(turn.historyIndex ?? index) : undefined}
              />
            ) : (
              <AssistantTurn
                turn={turn}
                busy={busy}
                enter={enter}
                sessionId={sessionId}
                onApproval={(decision) => onApproval(turn, decision)}
                onAnswer={(text, optionIds) => onAnswer(turn, text, optionIds)}
                onRegenerate={() => {
                  // Regenerating a reply means redoing it from the user turn before it.
                  for (let i = index - 1; i >= 0; i--) {
                    if (turns[i].role === 'user') {
                      onRegenerate(turns[i]);
                      return;
                    }
                  }
                }}
                onDelete={() => onDelete(turn)}
                onNotice={onNotice}
                onOpenFile={onOpenFile}
                onOpenDoc={onOpenDoc}
                onOpenEvidence={onOpenEvidence}
                onRerun={onRerun}
                onFork={onFork ? () => onFork(turn) : undefined}
                onExplore={onExplore ? () => onExplore({ historyIndex: turn.historyIndex ?? index }) : undefined}
                onCondense={onCondense ? () => onCondense({ historyIndex: turn.historyIndex ?? index }) : undefined}
                onOpenSourceControl={onOpenSourceControl}
                boardKey={boardKey}
                projectId={projectId}
              />
            )}
          </div>
        );
      })}
      {pastBottom && (
        <button
          type="button"
          className="fs-studio__back-to-bottom"
          onClick={() => {
            const el = scrollElRef.current;
            if (el) el.scrollTop = el.scrollHeight;
          }}
          data-testid="turn-back-to-bottom"
        >
          <ArrowDown size={14} aria-hidden="true" /> {t('New messages')}
        </button>
      )}
    </div>
  );
}
