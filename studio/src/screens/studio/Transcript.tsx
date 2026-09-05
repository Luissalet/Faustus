import { Check, ChevronDown, Copy, FileText, GitFork, Pencil, Quote, RefreshCw, Telescope, Trash2, Volume2, VolumeX, X } from 'lucide-react';
import { Fragment, lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Button, IconButton } from '../../components';
import type { AskUser, DelegationTask } from '../../adapters/chat';
import { attachmentUrl, isImage } from '../../adapters/composer';
import { Rich } from '../rich';
import { splitMentions } from '../../lib/mentions';
import { formatMetrics, type Step, type Turn } from './model';
import { t } from '../../i18n';
import { getDisplay } from '../../shell/display';

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
  onApproval: (turn: Turn, decision: Decision) => void;
  onAnswer: (text: string) => void;
  /** Save the user's edit; with `regenerate` the reply is redone from it. */
  onEdit: (turn: Turn, text: string, regenerate: boolean) => void;
  onRegenerate: (turn: Turn) => void;
  onDelete: (turn: Turn) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  /** Side panel hooks: a workspace file, a living document, a worker to re-run. */
  onOpenFile?: (path: string) => void;
  onOpenDoc?: (docId: string) => void;
  onRerun?: (task: DelegationTask) => void;
  /** A new conversation with everything up to and including this reply. */
  onFork?: (turn: Turn) => void;
  /** Selected text from a reply, quoted into the composer. */
  onQuote?: (text: string) => void;
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

/** Selecting text inside a reply offers to quote it into the composer. */
function useQuoteSelection(onQuote?: (text: string) => void) {
  const [pos, setPos] = useState<{ x: number; y: number; text: string } | null>(null);
  const holder = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!onQuote) return;
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
        if (!el || !holder.current.contains(el) || !el.closest('.fs-turn--assistant')) {
          setPos(null);
          return;
        }
        const rect = range.getBoundingClientRect();
        const host = holder.current.getBoundingClientRect();
        setPos({ x: rect.left - host.left + rect.width / 2, y: rect.top - host.top, text });
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
  }, [onQuote]);
  return { holder, pos, clear: () => setPos(null) };
}

const FILE_TOOLS = /^(read_file|write_file|edit_file|apply_patch|create_file|multi_edit|replace_across_files)$/;

/** The unified diff of a file write, coloured line by line. */
export function DiffLines({ text }: { text: string }) {
  return (
    <pre className="fs-diff" data-testid="step-diff">
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

function ToolRail({ steps, live, onOpenFile, onOpenDoc }: { steps: Step[]; live: boolean; onOpenFile?: (path: string) => void; onOpenDoc?: (docId: string) => void }) {
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
          <details key={step.id} className="fs-trace__step fs-studio__step" data-state={step.state}>
            <summary>
              <span className="fs-trace__node" aria-hidden="true" />
              <span className="fs-trace__label">{step.label}</span>
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
            {step.diff ? <DiffLines text={step.diff.text} /> : step.command && step.command !== step.label && <pre className="fs-studio__cmd">{step.command}</pre>}
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

export function AskCard({
  ask,
  busy,
  onApproval,
  onAnswer,
}: {
  ask: AskUser;
  busy: boolean;
  onApproval: (decision: Decision) => void;
  onAnswer: (text: string) => void;
}) {
  if (ask.kind === 'tool_approval') {
    return (
      <div className="fs-studio__ask" data-testid="studio-approval">
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
  return (
    <div className="fs-studio__ask" data-testid="studio-question">
      <p className="fs-studio__ask-title">{t('Asks you')}</p>
      <p className="fs-prose">{ask.question}</p>
      {ask.options.length > 0 && (
        <div className="fs-studio__ask-actions">
          {ask.options.map((option) => (
            <Button key={option} label={option} size="sm" disabled={busy} onClick={() => onAnswer(option)} />
          ))}
        </div>
      )}
    </div>
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
 * the file. Gated on onOpenFile the way the previous interface gated on a
 * bound workspace: with no folder there is nothing to open them against.
 */
function Said({ text, onOpenFile }: { text: string; onOpenFile?: (path: string) => void }) {
  if (!onOpenFile || !text.includes('@')) return <>{text}</>;
  const parts = splitMentions(text);
  if (!parts.some((part) => part.mention)) return <>{text}</>;
  return (
    <>
      {parts.map((part, i) =>
        part.mention ? (
          <button
            key={i}
            type="button"
            className="fs-turn__mention"
            title={t('Open {path}').replace('{path}', part.mention)}
            onClick={() => onOpenFile(part.mention as string)}
          >
            {part.token}
          </button>
        ) : (
          <Fragment key={i}>{part.text}</Fragment>
        ),
      )}
    </>
  );
}

function UserTurn({
  turn,
  busy,
  onEdit,
  onRegenerate,
  onDelete,
  onOpenFile,
}: {
  turn: Turn;
  busy: boolean;
  onEdit: TranscriptProps['onEdit'];
  onRegenerate: TranscriptProps['onRegenerate'];
  onDelete: TranscriptProps['onDelete'];
  onOpenFile?: TranscriptProps['onOpenFile'];
}) {
  const [editing, setEditing] = useState(false);
  return (
    <article className="fs-turn fs-turn--user" data-db-id={turn.dbId} data-testid="turn-user">
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
              <Said text={turn.text} onOpenFile={onOpenFile} />
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

function AssistantTurn({
  turn,
  busy,
  onApproval,
  onAnswer,
  onRegenerate,
  onDelete,
  onNotice,
  onOpenFile,
  onOpenDoc,
  onRerun,
  onFork,
}: {
  turn: Turn;
  busy: boolean;
  onApproval: (decision: Decision) => void;
  onAnswer: (text: string) => void;
  onRegenerate: () => void;
  onDelete: () => void;
  onNotice: TranscriptProps['onNotice'];
  onOpenFile?: TranscriptProps['onOpenFile'];
  onOpenDoc?: TranscriptProps['onOpenDoc'];
  onRerun?: TranscriptProps['onRerun'];
  onFork?: () => void;
}) {
  const waiting = turn.streaming && !turn.text && turn.steps.length === 0;
  return (
    <article className="fs-turn fs-turn--assistant" data-db-id={turn.dbId} data-streaming={turn.streaming || undefined} data-testid="turn-assistant">
      <span className="fs-turn__node" aria-hidden="true" />
      <div className="fs-turn__body">
        {turn.speaker && <p className="fs-turn__speaker">{turn.speaker}</p>}
        {turn.thinking && getDisplay().thinking && (
          <details className="fs-studio__thinking">
            <summary>Razonamiento {turn.streaming && !turn.text ? <span className="fs-studio__pulse" /> : null}</summary>
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
        {turn.steps.length > 0 && <ToolRail steps={turn.steps} live={turn.streaming} onOpenFile={onOpenFile} onOpenDoc={onOpenDoc} />}
        {turn.workers.length > 0 && (
          <Suspense fallback={null}>
            <SubagentBoard workers={turn.workers} live={turn.streaming} onRerun={onRerun ?? (() => undefined)} onNotice={onNotice} />
          </Suspense>
        )}
        {turn.research && !turn.research.done && turn.streaming && <ResearchLine research={turn.research} />}
        {waiting && !turn.thinking && !turn.research && (
          <p className="fs-studio__waiting" aria-live="polite">
            <span className="fs-studio__pulse" /> Pensando
          </p>
        )}
        {turn.text && <Rich text={turn.text} />}
        {turn.streaming && turn.text && <span className="fs-studio__cursor" aria-hidden="true" />}
        {turn.images.map((url) => (
          <img key={url} className="fs-studio__image" src={url} alt={t('Generated image')} loading="lazy" />
        ))}
        {turn.sources.length > 0 && (
          <p className="fs-studio__sources">
            {turn.sources.slice(0, 6).map((s) => (
              <a key={s.url} className="fs-link" href={s.url} target="_blank" rel="noreferrer">
                {s.title.slice(0, 48)}
              </a>
            ))}
          </p>
        )}
        {turn.ask && <AskCard ask={turn.ask} busy={busy} onApproval={onApproval} onAnswer={onAnswer} />}
        {!turn.streaming && (turn.summary || turn.checks.length > 0) && (
          <Suspense fallback={null}>
            <Harness mode="final" summary={turn.summary} checks={turn.checks} answer={turn.text} onNotice={onNotice} />
          </Suspense>
        )}
        {turn.note && (
          <p className="fs-notice" data-tone="warning">
            {turn.note}
          </p>
        )}
        {turn.error && (
          <p className="fs-notice" data-tone="danger">
            {turn.error}
          </p>
        )}
        {!turn.streaming && (
          <div className="fs-turn__foot">
            {turn.metrics && (
              <span className="fs-turn__metrics">
                {formatMetrics(turn.metrics)}
                {turn.rounds > 1 ? ` · ${turn.rounds} rondas` : ''}
              </span>
            )}
            <span className="fs-turn__actions" data-testid="turn-actions">
              {turn.text && <CopyButton text={turn.text} label={t('Copy reply')} />}
              {turn.text && <SpeakButton text={turn.text} />}
              {turn.dbId && !busy && (
                <>
                  <IconButton icon={RefreshCw} label={t('Regenerate')} size="sm" onClick={onRegenerate} />
                  {onFork && <IconButton icon={GitFork} label={t('Fork from here')} size="sm" onClick={onFork} testId="turn-fork" />}
                  <IconButton icon={Trash2} label={t('Delete message')} size="sm" onClick={onDelete} />
                </>
              )}
            </span>
          </div>
        )}
      </div>
    </article>
  );
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
    planning: t('planning'),
    searching: t('searching'),
    reading: t('reading {n} sources', { n: research.totalSources }),
    analyzing: t('analysing'),
    writing: t('writing the report'),
  };
  return (
    <p className="fs-studio__waiting fs-studio__research" aria-live="polite">
      <Telescope size={13} aria-hidden="true" />
      {t('Deep Research')}
      {research.round ? ` · ${t('round {n}', { n: research.round })}` : ''} · {research.message || phase[research.phase] || research.phase} · <span className="fs-studio__clock">{clock}{avg}</span>
    </p>
  );
}

export function Transcript({ turns, busy, onApproval, onAnswer, onEdit, onRegenerate, onDelete, onNotice, onOpenFile, onOpenDoc, onRerun, onFork, onQuote }: TranscriptProps) {
  const quote = useQuoteSelection(onQuote);
  return (
    <div className="fs-studio__turns" ref={quote.holder}>
      {quote.pos && onQuote && (
        <button
          type="button"
          className="fs-studio__quote"
          style={{ insetInlineStart: quote.pos.x, insetBlockStart: quote.pos.y }}
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
      {turns.map((turn, index) =>
        turn.role === 'user' ? (
          <UserTurn key={turn.id} turn={turn} busy={busy} onEdit={onEdit} onRegenerate={onRegenerate} onDelete={onDelete} onOpenFile={onOpenFile} />
        ) : (
          <AssistantTurn
            key={turn.id}
            turn={turn}
            busy={busy}
            onApproval={(decision) => onApproval(turn, decision)}
            onAnswer={onAnswer}
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
            onRerun={onRerun}
            onFork={onFork ? () => onFork(turn) : undefined}
          />
        ),
      )}
    </div>
  );
}
