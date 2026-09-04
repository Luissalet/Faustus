import { Check, ChevronDown, Copy, FileText, Pencil, RefreshCw, Trash2, X } from 'lucide-react';
import { useState } from 'react';
import { Button, IconButton } from '../../components';
import type { AskUser } from '../../adapters/chat';
import { attachmentUrl, isImage } from '../../adapters/composer';
import { Rich } from '../rich';
import { formatMetrics, type Step, type Turn } from './model';

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
}

function ToolRail({ steps, live }: { steps: Step[]; live: boolean }) {
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
        step.output || step.command ? (
          <details key={step.id} className="fs-trace__step fs-studio__step" data-state={step.state}>
            <summary>
              <span className="fs-trace__node" aria-hidden="true" />
              <span className="fs-trace__label">{step.label}</span>
              {step.meta && <span className="fs-trace__meta">{step.meta}</span>}
            </summary>
            {step.command && step.command !== step.label && <pre className="fs-studio__cmd">{step.command}</pre>}
            {step.output && <pre className="fs-studio__out">{step.output.slice(0, 6000)}</pre>}
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

function AskCard({
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
        <p className="fs-studio__ask-title">Necesita tu permiso</p>
        {ask.question && <p className="fs-prose">{ask.question}</p>}
        <div className="fs-studio__ask-actions">
          <Button variant="primary" icon={Check} label="Aprobar" disabled={busy} onClick={() => onApproval('approve')} />
          <Button label="Aprobar toda la tarea" disabled={busy} onClick={() => onApproval('approve_task')} />
          <Button variant="danger" icon={X} label="Denegar" disabled={busy} onClick={() => onApproval('deny')} />
        </div>
      </div>
    );
  }
  return (
    <div className="fs-studio__ask" data-testid="studio-question">
      <p className="fs-studio__ask-title">Te pregunta</p>
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

function CopyButton({ text, label = 'Copiar' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <IconButton
      icon={done ? Check : Copy}
      label={done ? 'Copiado' : label}
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
        aria-label="Editar mensaje"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') onCancel();
          if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) onSave(text, true);
        }}
        autoFocus
      />
      <div className="fs-studio__ask-actions">
        <Button variant="primary" icon={RefreshCw} label="Guardar y regenerar" size="sm" disabled={!text.trim()} onClick={() => onSave(text, true)} />
        <Button label="Solo guardar" size="sm" disabled={!text.trim()} onClick={() => onSave(text, false)} />
        <Button variant="ghost" label="Cancelar" size="sm" onClick={onCancel} />
      </div>
    </div>
  );
}

function UserTurn({
  turn,
  busy,
  onEdit,
  onRegenerate,
  onDelete,
}: {
  turn: Turn;
  busy: boolean;
  onEdit: TranscriptProps['onEdit'];
  onRegenerate: TranscriptProps['onRegenerate'];
  onDelete: TranscriptProps['onDelete'];
}) {
  const [editing, setEditing] = useState(false);
  return (
    <article className="fs-turn fs-turn--user" data-testid="turn-user">
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
                <ul className="fs-studio__attachments fs-studio__attachments--sent" aria-label="Adjuntos">
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
              {turn.text}
              {turn.edited && <span className="fs-turn__edited">editado</span>}
            </div>
            <div className="fs-turn__actions" data-testid="turn-actions">
              <CopyButton text={turn.text} />
              {turn.dbId && !busy && (
                <>
                  <IconButton icon={Pencil} label="Editar" size="sm" onClick={() => setEditing(true)} />
                  <IconButton icon={RefreshCw} label="Regenerar desde aquí" size="sm" onClick={() => onRegenerate(turn)} />
                  <IconButton icon={Trash2} label="Borrar mensaje" size="sm" onClick={() => onDelete(turn)} />
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
}: {
  turn: Turn;
  busy: boolean;
  onApproval: (decision: Decision) => void;
  onAnswer: (text: string) => void;
  onRegenerate: () => void;
  onDelete: () => void;
}) {
  const waiting = turn.streaming && !turn.text && turn.steps.length === 0;
  return (
    <article className="fs-turn fs-turn--assistant" data-streaming={turn.streaming || undefined} data-testid="turn-assistant">
      <span className="fs-turn__node" aria-hidden="true" />
      <div className="fs-turn__body">
        {turn.thinking && (
          <details className="fs-studio__thinking">
            <summary>Razonamiento {turn.streaming && !turn.text ? <span className="fs-studio__pulse" /> : null}</summary>
            <p className="fs-prose">{turn.thinking}</p>
          </details>
        )}
        {turn.steps.length > 0 && <ToolRail steps={turn.steps} live={turn.streaming} />}
        {waiting && !turn.thinking && (
          <p className="fs-studio__waiting" aria-live="polite">
            <span className="fs-studio__pulse" /> Pensando
          </p>
        )}
        {turn.text && <Rich text={turn.text} />}
        {turn.streaming && turn.text && <span className="fs-studio__cursor" aria-hidden="true" />}
        {turn.images.map((url) => (
          <img key={url} className="fs-studio__image" src={url} alt="Imagen generada" loading="lazy" />
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
            {turn.metrics && <span className="fs-turn__metrics">{formatMetrics(turn.metrics)}</span>}
            <span className="fs-turn__actions" data-testid="turn-actions">
              {turn.text && <CopyButton text={turn.text} label="Copiar respuesta" />}
              {turn.dbId && !busy && (
                <>
                  <IconButton icon={RefreshCw} label="Regenerar" size="sm" onClick={onRegenerate} />
                  <IconButton icon={Trash2} label="Borrar mensaje" size="sm" onClick={onDelete} />
                </>
              )}
            </span>
          </div>
        )}
      </div>
    </article>
  );
}

export function Transcript({ turns, busy, onApproval, onAnswer, onEdit, onRegenerate, onDelete }: TranscriptProps) {
  return (
    <div className="fs-studio__turns">
      {turns.map((turn, index) =>
        turn.role === 'user' ? (
          <UserTurn key={turn.id} turn={turn} busy={busy} onEdit={onEdit} onRegenerate={onRegenerate} onDelete={onDelete} />
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
          />
        ),
      )}
    </div>
  );
}
