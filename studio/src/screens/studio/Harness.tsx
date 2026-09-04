import { Check, ChevronDown, CircleDashed, FileDiff, GitCommitHorizontal, History, ShieldAlert, ShieldCheck, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { Button, IconButton } from '../../components';
import type { HarnessCheck, HarnessSummary, Todo } from '../../adapters/chat';
import { commitFiles, commitProposal, fileDiff, restoreCheckpoint, revertFile } from '../../adapters/workspace';
import { Rich } from '../rich';

/**
 * The reliability harness, shown where it happened: the turn summary
 * (what the tools really did, against what the answer claimed), the checks
 * that ran, the files that changed with a diff and a revert each, and
 * the turn's checkpoint with "back to before this turn" and "commit".
 *
 * The legacy shows this as 🛡 cards under the reply; here it is the same
 * information on the same rail vocabulary, with the actions inline.
 */

const CHECK_WORDS: Record<string, string> = {
  checkpoint: 'Punto de control guardado',
  auto_continue: 'Continúa solo',
  think_cutoff: 'Razonamiento cortado',
  unverified: 'Afirmaciones sin verificar',
  unknown_tool: 'Herramienta desconocida',
  empty_round: 'Ronda vacía',
  rejected: 'Respuesta rechazada',
  target_substituted: 'Fichero sustituido en silencio',
  syntax_error: 'Error de sintaxis tras editar',
  static_analysis: 'Análisis estático',
  tests_running: 'Ejecutando tests',
  tests_failed: 'Tests fallidos',
  review_running: 'Revisando el diff',
  review_issues: 'La revisión encontró problemas',
  verified: 'Verificado',
};

const REASON_WORDS: Record<string, string> = {
  claims_without_mutation: 'afirma cambios que no hizo',
  fabricated_paths: 'nombra ficheros que no existen',
  claimed_paths_untouched: 'nombra ficheros que no tocó',
  no_tools: 'no usó herramientas',
};

const STOP_WORDS: Record<string, string> = {
  complete: 'terminó',
  complete_unverified: 'terminó sin verificar',
  rounds: 'agotó las rondas',
  budget: 'agotó el presupuesto',
  stopped: 'parado',
  error: 'error',
  asked_user: 'te preguntó',
};

export function CheckList({ checks }: { checks: HarnessCheck[] }) {
  if (!checks.length) return null;
  return (
    <ul className="fs-harness__checks" aria-label="Comprobaciones del arnés">
      {checks.map((c, i) => {
        const bad = ['unverified', 'rejected', 'syntax_error', 'tests_failed', 'review_issues', 'target_substituted', 'unknown_tool'].includes(c.status);
        const good = c.status === 'verified';
        return (
          <li key={`${c.status}-${i}`} className="fs-harness__check" data-tone={bad ? 'danger' : good ? 'success' : 'info'}>
            {good ? <ShieldCheck size={13} aria-hidden="true" /> : bad ? <ShieldAlert size={13} aria-hidden="true" /> : <CircleDashed size={13} aria-hidden="true" />}
            <span>
              {CHECK_WORDS[c.status] ?? c.status}
              {c.label ? ` · ${c.label}` : ''}
              {c.model ? ` · ${c.model}` : ''}
              {c.reasons && c.reasons.length > 0 ? `: ${c.reasons.map((r) => REASON_WORDS[r] ?? r).join(', ')}` : ''}
              {c.detail && !c.reasons?.length ? ` · ${c.detail}` : ''}
            </span>
            {c.round !== undefined && <span className="fs-trace__meta">ronda {c.round}</span>}
          </li>
        );
      })}
    </ul>
  );
}

export function ProgressList({ todos }: { todos: Todo[] }) {
  if (!todos.length) return null;
  return (
    <div className="fs-trace fs-studio__trace" data-testid="studio-progress">
      {todos.map((t, i) => (
        <div
          key={`${t.content}-${i}`}
          className="fs-trace__step"
          data-state={t.status === 'completed' ? 'succeeded' : t.status === 'in_progress' ? 'running' : 'queued'}
        >
          <span className="fs-trace__node" aria-hidden="true" />
          <span className="fs-trace__label">{t.content}</span>
          {t.status === 'completed' && t.verified === false && <span className="fs-trace__meta">sin evidencia</span>}
        </div>
      ))}
    </div>
  );
}

export function PlanCard({ plan }: { plan: string }) {
  const done = (plan.match(/- \[[xX]\]/g) ?? []).length;
  const total = done + (plan.match(/- \[ \]/g) ?? []).length;
  const pretty = plan.replace(/- \[[xX]\] /g, '- ✅ ').replace(/- \[ \] /g, '- ⬜ ');
  return (
    <details className="fs-harness fs-harness--plan" open data-testid="studio-plan">
      <summary>
        <span className="fs-harness__title">Plan</span>
        {total > 0 && (
          <span className="fs-trace__meta">
            {done}/{total}
          </span>
        )}
      </summary>
      <Rich text={pretty} />
    </details>
  );
}

function ChangedFile({ path, workspace, checkpoint, onNotice }: { path: string; workspace?: string; checkpoint?: string; onNotice: (t: string, tone?: 'info' | 'warning' | 'danger') => void }) {
  const [diff, setDiff] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [gone, setGone] = useState(false);

  const showDiff = async () => {
    if (!workspace) return;
    setOpen((v) => !v);
    if (diff !== null) return;
    try {
      const result = await fileDiff(workspace, path, checkpoint);
      setDiff(result.diff || (result.git ? '(sin cambios respecto a la base)' : '(la carpeta no es un repositorio y no hay punto de control para este turno)'));
    } catch (error) {
      setDiff(`No he podido leer el diff: ${(error as Error).message}`);
    }
  };

  const revert = async () => {
    if (!workspace) return;
    setBusy(true);
    try {
      const action = await revertFile(workspace, path, checkpoint);
      setGone(true);
      onNotice(action === 'deleted_new_file' || action === 'deleted_untracked' ? `${path}: era nuevo y se ha borrado.` : `${path}: restaurado.`);
    } catch (error) {
      onNotice(`No he podido revertir ${path}: ${(error as Error).message}`, 'danger');
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="fs-harness__file" data-gone={gone || undefined}>
      <div className="fs-harness__file-row">
        <code className="fs-harness__path">{path}</code>
        <span className="fs-turn__actions" style={{ opacity: 1 }}>
          <IconButton icon={FileDiff} label={open ? 'Ocultar diff' : 'Ver diff'} size="sm" disabled={!workspace} onClick={() => void showDiff()} />
          <IconButton icon={Undo2} label="Revertir este fichero" size="sm" disabled={!workspace || busy || gone} onClick={() => void revert()} />
        </span>
      </div>
      {open && <pre className="fs-studio__out fs-harness__diff">{diff ?? 'Leyendo…'}</pre>}
    </li>
  );
}

export function HarnessCard({
  summary,
  checks,
  answer,
  onNotice,
}: {
  summary: HarnessSummary;
  checks: HarnessCheck[];
  answer: string;
  onNotice: (t: string, tone?: 'info' | 'warning' | 'danger') => void;
}) {
  const [committing, setCommitting] = useState(false);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [restored, setRestored] = useState(false);
  const verdict = summary.changeset?.verdict;
  const unverified = checks.some((c) => c.status === 'unverified') || summary.stopReason === 'complete_unverified';
  const verified = checks.some((c) => c.status === 'verified');
  const tests = summary.tests;
  const testsLine = tests && typeof tests === 'object' && ('passed' in tests || 'failed' in tests || 'status' in tests)
    ? `tests: ${String(tests.status ?? '')} ${tests.passed !== undefined ? `${tests.passed} ok` : ''} ${tests.failed !== undefined ? `${tests.failed} fallidos` : ''}`.replace(/\s+/g, ' ').trim()
    : null;

  const startCommit = async () => {
    if (!summary.workspace) return;
    setCommitting(true);
    try {
      const proposal = await commitProposal(summary.workspace, summary.mutations, answer);
      if (!proposal.git) {
        onNotice('La carpeta no es un repositorio git: no hay dónde confirmar.', 'warning');
        setCommitting(false);
        return;
      }
      setMessage(proposal.message);
    } catch (error) {
      onNotice(`No he podido preparar el commit: ${(error as Error).message}`, 'danger');
      setCommitting(false);
    }
  };

  const doCommit = async () => {
    if (!summary.workspace || !message.trim()) return;
    setBusy(true);
    try {
      const sha = await commitFiles(summary.workspace, summary.mutations, message.trim());
      onNotice(`Commit hecho (${sha.slice(0, 10)}).`);
      setCommitting(false);
    } catch (error) {
      onNotice(`El commit ha fallado: ${(error as Error).message}`, 'danger');
    } finally {
      setBusy(false);
    }
  };

  const restoreAll = async () => {
    if (!summary.workspace || !summary.checkpoint) return;
    setBusy(true);
    try {
      const res = await restoreCheckpoint(summary.workspace, summary.checkpoint);
      setRestored(true);
      onNotice(`Vuelto a antes de este turno: ${res.restored} restaurados, ${res.deleted} borrados${res.failed ? `, ${res.failed} fallidos` : ''}.`);
    } catch (error) {
      onNotice(`No he podido restaurar: ${(error as Error).message}`, 'danger');
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="fs-harness" open={unverified || summary.mutations.length > 0} data-testid="studio-harness" data-verdict={unverified ? 'unverified' : verified ? 'verified' : 'plain'}>
      <summary>
        {unverified ? <ShieldAlert size={14} aria-hidden="true" /> : verified ? <ShieldCheck size={14} aria-hidden="true" /> : <History size={14} aria-hidden="true" />}
        <span className="fs-harness__title">
          {unverified ? 'Sin verificar' : verified ? 'Verificado' : 'Resumen del turno'}
        </span>
        <span className="fs-trace__meta">
          {summary.toolCalls} {summary.toolCalls === 1 ? 'herramienta' : 'herramientas'}
          {summary.failedCalls ? ` · ${summary.failedCalls} fallidas` : ''}
          {summary.mutations.length ? ` · ${summary.mutations.length} ${summary.mutations.length === 1 ? 'cambio' : 'cambios'}` : ''}
          {` · ${STOP_WORDS[summary.stopReason] ?? summary.stopReason}`}
        </span>
        <ChevronDown size={13} aria-hidden="true" className="fs-harness__chev" />
      </summary>

      <CheckList checks={checks} />

      {(verdict || testsLine) && (
        <p className="fs-harness__line">
          {verdict && <span>Cambios frente a lo afirmado: <strong>{verdict}</strong>{summary.changeset?.confidence !== undefined ? ` (${Math.round(summary.changeset.confidence * 100)} %)` : ''}. </span>}
          {testsLine && <span>{testsLine}. </span>}
        </p>
      )}
      {summary.changeset && summary.changeset.unsupported.length > 0 && (
        <p className="fs-notice" data-tone="warning">
          Afirma haber cambiado y el punto de control no lo vio: {summary.changeset.unsupported.join(', ')}
        </p>
      )}
      {summary.changeset && summary.changeset.unclaimed.length > 0 && (
        <p className="fs-notice" data-tone="warning">
          Cambió sin decirlo: {summary.changeset.unclaimed.join(', ')}
        </p>
      )}

      {summary.mutations.length > 0 && (
        <ul className="fs-harness__files" aria-label="Ficheros cambiados">
          {summary.mutations.map((path) => (
            <ChangedFile key={path} path={path} workspace={summary.workspace} checkpoint={summary.checkpoint} onNotice={onNotice} />
          ))}
        </ul>
      )}

      {summary.workspace && summary.mutations.length > 0 && (
        <div className="fs-studio__ask-actions">
          {summary.checkpoint && (
            <Button variant="danger" icon={Undo2} label="Volver a antes de este turno" size="sm" disabled={busy || restored} onClick={() => void restoreAll()} />
          )}
          {!committing && <Button icon={GitCommitHorizontal} label="Confirmar en git…" size="sm" disabled={busy} onClick={() => void startCommit()} />}
        </div>
      )}
      {committing && (
        <div className="fs-studio__edit">
          <textarea className="fs-studio__input fs-studio__edit-input" value={message} rows={3} aria-label="Mensaje del commit" onChange={(e) => setMessage(e.target.value)} />
          <div className="fs-studio__ask-actions">
            <Button variant="primary" icon={Check} label="Commit" size="sm" loading={busy} disabled={!message.trim()} onClick={() => void doCommit()} />
            <Button variant="ghost" label="Cancelar" size="sm" onClick={() => setCommitting(false)} />
          </div>
        </div>
      )}
      {summary.notes.length > 0 && <p className="fs-harness__notes">{summary.notes.join(' · ')}</p>}
    </details>
  );
}

/** One lazy entry for the transcript: live pieces while streaming, the
 *  card once the turn has settled. */
export default function Harness(props: {
  mode: 'live' | 'final';
  plan?: string;
  todos?: Todo[];
  checks: HarnessCheck[];
  summary?: HarnessSummary;
  answer: string;
  onNotice: (t: string, tone?: 'info' | 'warning' | 'danger') => void;
}) {
  if (props.mode === 'live') {
    return (
      <>
        {props.plan && <PlanCard plan={props.plan} />}
        {props.todos && props.todos.length > 0 && <ProgressList todos={props.todos} />}
        {props.checks.length > 0 && <CheckList checks={props.checks} />}
      </>
    );
  }
  if (props.summary) {
    return <HarnessCard summary={props.summary} checks={props.checks} answer={props.answer} onNotice={props.onNotice} />;
  }
  return <CheckList checks={props.checks} />;
}
