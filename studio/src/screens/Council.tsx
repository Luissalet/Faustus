import {
  ArrowRightLeft, Ban, Check, Eye, Gauge, Gavel, Hand, ListChecks, Lock, Megaphone,
  MessageSquare, Pause, Play, Plus, RefreshCw, Scale, Send, ShieldAlert, ShieldCheck,
  Square, UserCog, Users, X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton, Toast } from '../components';
import { listModels, type ModelRoute } from '../adapters/chat';
import * as api from '../adapters/council';
import { hueIndex, initials } from '../lib/mail';
import ModelPalette from './ModelPalette';
import { Rich } from './rich';
import { t, tn } from '../i18n';
import './council.css';

/**
 * Council: several models think about one matter, and exactly one of them may
 * act on each resource.
 *
 * This is the successor to Group chat and not a bigger version of it. Group
 * chat put a peer's answer into the next model's mouth as `[Claude]: …` in a
 * `user` message, so nothing in the room could tell who had said what or who
 * was allowed to do anything about it. Here authorship is a field: every
 * message shows its author, the kind of utterance it is and who it is
 * addressed to, and every seat shows the permission it actually holds.
 *
 * The screen has four parts, in the order the work happens: the room and its
 * seats, the transcript in turns, the ledger of what is claimed, owed and
 * decided, and the close. The ledger is beside the transcript rather than
 * under it because a claim on a file and an unanswered objection are what the
 * reader has to see WHILE the room talks — a decision that quietly vanished
 * from the last round is exactly what a summary hides.
 *
 * Nothing here decides anything: the arithmetic is in `adapters/council.ts`
 * and the rules are on the server.
 */

type Tone = api.Tone;

function Tag({ tone, icon: Icon, title, children }: {
  tone: Tone;
  icon?: typeof Lock;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span className="fs-cnc__tag" data-tone={tone} title={title}>
      {Icon ? <Icon size={11} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

function Avatar({ name }: { name: string }) {
  return (
    <span className="fs-avatar" data-hue={hueIndex(name)} data-size="sm" aria-hidden="true">
      {initials(name)}
    </span>
  );
}

/** The eight message kinds, each with a word of its own. Never colour alone. */
function typeLabel(kind: string): string {
  switch (kind) {
    case 'proposal': return t('Proposal');
    case 'critique': return t('Critique');
    case 'rebuttal': return t('Rebuttal');
    case 'synthesis': return t('Synthesis');
    case 'decision': return t('Decision');
    case 'objection': return t('Objection');
    case 'evidence': return t('Evidence');
    case 'abstention': return t('Abstained');
    case 'status': return t('Status');
    default: return t('Message');
  }
}

function typeTone(kind: string): Tone {
  if (kind === 'objection') return 'danger';
  if (kind === 'critique' || kind === 'rebuttal') return 'warn';
  if (kind === 'decision' || kind === 'synthesis' || kind === 'evidence') return 'ok';
  return 'neutral';
}

function closeLabel(status: string): string {
  switch (status) {
    case 'verified': return t('Verified');
    case 'decided': return t('Decided');
    case 'unverified': return t('Unverified');
    case 'blocked': return t('Blocked');
    case 'disputed': return t('Disputed');
    default: return t('No close recorded');
  }
}

function stopLabel(reason: string): string {
  switch (reason) {
    case 'completed': return t('It finished.');
    case 'convergence': return t('The round stopped adding anything.');
    case 'judge_verdict': return t('A judge gave its verdict.');
    case 'proof_passed': return t('A proof came back proved.');
    case 'budget_exhausted': return t('It ran out of budget.');
    case 'max_rounds': return t('It used up its rounds.');
    case 'max_turns': return t('It used up its turns.');
    case 'user_stopped': return t('You stopped it.');
    case 'blocked': return t('Something is waiting for a person.');
    case 'failed': return t('It failed.');
    default: return t('No stop reason was recorded.');
  }
}

/* ── Opening a room ─────────────────────────────────────────────────────── */

interface Seat extends api.SeatRequest {
  route: ModelRoute | null;
}

function policyLine(policy: string): string {
  switch (policy) {
    case 'chat': return t('A moderated conversation. No mutating tools are granted.');
    case 'consult': return t('Each participant answers on its own, then one synthesis.');
    case 'debate': return t('Proposal, critique, rebuttal, synthesis, verdict.');
    case 'collaborate': return t('Plan, execute, review, verify. This room may change things.');
    case 'pair': return t('A driver and a navigator on one task.');
    case 'tournament': return t('A blind round, contrast, and a verdict.');
    default: return '';
  }
}

function Setup({ config, routes, onOpened, say }: {
  config: api.CouncilConfig;
  routes: ModelRoute[];
  onOpened: (id: string) => void;
  say: (message: string, tone?: 'ok' | 'warn') => void;
}) {
  const [title, setTitle] = useState('');
  const [policy, setPolicy] = useState(config.policies[0] ?? 'chat');
  const [workspace, setWorkspace] = useState('');
  const [budgets, setBudgets] = useState<api.Budgets>(config.defaultBudgets);
  const [seats, setSeats] = useState<Seat[]>(() =>
    routes.slice(0, 2).map((route) => ({ route, model: route.model, endpointId: route.endpointId, displayName: '', roles: [] })));
  const [pick, setPick] = useState<number | null>(null);
  const [opening, setOpening] = useState(false);

  const ceiling = config.policyCeilings[policy] ?? 'none';
  const patch = (index: number, over: Partial<Seat>) =>
    setSeats((current) => current.map((seat, i) => (i === index ? { ...seat, ...over } : seat)));

  const toggleRole = (index: number, role: string) =>
    setSeats((current) => current.map((seat, i) => (
      i === index
        ? { ...seat, roles: seat.roles.includes(role) ? seat.roles.filter((r) => r !== role) : [...seat.roles, role] }
        : seat)));

  const open = async () => {
    setOpening(true);
    try {
      const detail = await api.createRoom({
        title: title.trim() || t('Untitled room'),
        policy,
        workspace: workspace.trim(),
        budgets,
        participants: seats.filter((s) => s.model).map((s) => ({
          model: s.model, endpointId: s.endpointId, displayName: s.displayName?.trim() ?? '', roles: s.roles,
        })),
      });
      onOpened(detail.room.id);
    } catch (error) {
      say((error as Error).message || t('The room could not be opened.'), 'warn');
      setOpening(false);
    }
  };

  return (
    <section className="fs-cnc__setup" aria-label={t('Open a council room')}>
      <div className="fs-cnc__grid2">
        <label className="fs-cnc__field">
          <span className="fs-cnc__label">{t('What is this room about')}</span>
          <input className="fs-field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t('Should the OAuth state be validated on the client?')} />
        </label>
        <label className="fs-cnc__field">
          <span className="fs-cnc__label">{t('Workspace')}</span>
          <input className="fs-field" value={workspace} onChange={(e) => setWorkspace(e.target.value)} placeholder={t('Optional: the folder claims are counted against')} />
        </label>
      </div>

      <div className="fs-cnc__field">
        <span className="fs-cnc__label">{t('What this room does')}</span>
        <div className="fs-cnc__policies" role="radiogroup" aria-label={t('What this room does')}>
          {config.policies.map((name) => (
            <button key={name} type="button" role="radio" aria-checked={policy === name} className="fs-cnc__policy" onClick={() => setPolicy(name)}>
              <b>{name}</b>
              <small>{policyLine(name)}</small>
            </button>
          ))}
        </div>
        <p className="fs-muted">
          {t('Whatever a seat asks for, this room grants at most {ceiling}: the ceiling is the policy’s, and the server lowers a profile and never raises one.', { ceiling })}
        </p>
      </div>

      <div className="fs-cnc__field">
        <span className="fs-cnc__label">{t('Who sits at the table')}</span>
        <ul className="fs-cnc__seats-edit">
          {seats.map((seat, index) => (
            <li key={index} className="fs-cnc__seat-edit">
              <button type="button" className="fs-cnc__pick" onClick={() => setPick(index)} data-testid="council-model">
                {seat.model || t('Choose a model')} <small>{seat.route?.endpointName ?? ''}</small>
              </button>
              <input
                className="fs-field fs-cnc__name"
                value={seat.displayName ?? ''}
                onChange={(e) => patch(index, { displayName: e.target.value })}
                placeholder={t('Name in the room')}
                aria-label={t('Name for {model}', { model: seat.model || t('this seat') })}
              />
              <div className="fs-cnc__roles" role="group" aria-label={t('Roles for {model}', { model: seat.model || t('this seat') })}>
                {config.roles.map((role) => (
                  <button
                    key={role.id}
                    type="button"
                    className="fs-chip"
                    aria-pressed={seat.roles.includes(role.id)}
                    title={t('{role} is {profile} by default', { role: role.id, profile: role.defaultProfile })}
                    onClick={() => toggleRole(index, role.id)}
                  >
                    {role.id}
                  </button>
                ))}
              </div>
              <IconButton icon={X} label={t('Remove this seat')} size="sm" disabled={seats.length <= 1} onClick={() => setSeats((c) => c.filter((_, i) => i !== index))} />
            </li>
          ))}
        </ul>
        <Button
          variant="ghost" size="sm" icon={Plus} label={t('Add a seat')} testId="council-add-seat"
          disabled={!routes.length || seats.length >= 8}
          onClick={() => setSeats((c) => [...c, { route: routes[c.length % routes.length] ?? null, model: routes[c.length % routes.length]?.model ?? '', endpointId: routes[c.length % routes.length]?.endpointId ?? '', displayName: '', roles: [] }])}
        />
      </div>

      <div className="fs-cnc__field">
        <span className="fs-cnc__label">{t('What it may spend')}</span>
        <div className="fs-cnc__budgets">
          {([
            ['maxRounds', t('Rounds')],
            ['maxTurns', t('Turns')],
            ['maxWallSeconds', t('Seconds')],
            ['maxTotalTokens', t('Tokens')],
            ['maxParallel', t('At once')],
          ] as [keyof api.Budgets, string][]).map(([key, label]) => (
            <label key={key} className="fs-cnc__budget">
              <span>{label}</span>
              <input
                className="fs-field" type="number" min={0} value={budgets[key]}
                onChange={(e) => setBudgets((b) => ({ ...b, [key]: Math.max(0, Number(e.target.value) || 0) }))}
              />
            </label>
          ))}
        </div>
        <p className="fs-muted">{t('A room without limits is a bill. Zero means «no more of this», and it is a real answer.')}</p>
      </div>

      <div className="fs-inline">
        {!config.orchestratorAvailable && (
          <Tag tone="warn" icon={ShieldAlert}>{t('This build has no council engine: rooms open and read, and no turn runs.')}</Tag>
        )}
        <span className="fs-spacer" />
        <Button variant="primary" size="sm" icon={Play} label={t('Open the room')} loading={opening} testId="council-open" disabled={seats.filter((s) => s.model).length < 2} onClick={() => void open()} />
      </div>

      <ModelPalette
        open={pick !== null}
        onOpenChange={(o) => !o && setPick(null)}
        routes={routes}
        current={pick !== null ? seats[pick]?.route ?? null : null}
        onPick={(route) => {
          if (pick !== null) patch(pick, { route, model: route.model, endpointId: route.endpointId });
          setPick(null);
        }}
      />
    </section>
  );
}

/* ── The ledger, beside the room and not under it ───────────────────────── */

function LedgerPanel({ ledger, participants, onHandoff }: {
  ledger: api.Ledger | null;
  participants: api.Participant[];
  onHandoff: (taskId: string) => void;
}) {
  const [showSuperseded, setShowSuperseded] = useState(false);
  const name = (id: string) => participants.find((p) => p.id === id)?.displayName || id || t('unassigned');

  if (!ledger) return <Skeleton label={t('Reading the ledger')} count={3} height="48px" />;
  if (ledger.unreadable) {
    return <p className="fs-cnc__notice" data-tone="danger">{t('The ledger could not be read. Nothing below is trustworthy until it can.')}</p>;
  }

  const block = api.roomBlock(ledger);
  const resources = api.resourcesHeld(ledger.heldClaims.length ? ledger.heldClaims : ledger.claims);
  const live = ledger.decisions.filter((d) => d.status !== 'superseded');
  const buried = ledger.decisions.filter((d) => d.status === 'superseded');
  const open = ledger.openObjections.length ? ledger.openObjections : ledger.objections.filter((o) => api.isOpenObjection(o.status));

  return (
    <aside className="fs-cnc__ledger" aria-label={t('The ledger')}>
      {!block.verifiable && (
        <p className="fs-cnc__notice" data-tone="danger">
          <ShieldAlert size={12} aria-hidden="true" />{' '}
          {t('Nothing here may be called verified while a blocking objection is open.')}
        </p>
      )}

      <section className="fs-cnc__book">
        <h2 className="fs-cnc__book-head"><ListChecks size={13} aria-hidden="true" /> {t('Tasks')}</h2>
        {ledger.tasks.length === 0 && <p className="fs-muted">{t('No task has been opened.')}</p>}
        <ul className="fs-cnc__rows">
          {ledger.tasks.map((task) => (
            <li key={task.id} className="fs-cnc__row" data-tone={api.taskTone(task.status)}>
              <div className="fs-cnc__row-head">
                <b>{task.title || task.id}</b>
                <Tag tone={api.taskTone(task.status)}>{task.status}</Tag>
              </div>
              <p className="fs-muted">
                {t('Owner: {owner} · Reviewer: {reviewer}', { owner: name(task.owner), reviewer: task.reviewer ? name(task.reviewer) : t('nobody') })}
              </p>
              {task.resources.length > 0 && <p className="fs-muted fs-cnc__paths">{task.resources.join(' · ')}</p>}
              <Button variant="ghost" size="sm" icon={ArrowRightLeft} label={t('Hand it over')} onClick={() => onHandoff(task.id)} />
            </li>
          ))}
        </ul>
      </section>

      <section className="fs-cnc__book">
        <h2 className="fs-cnc__book-head"><Lock size={13} aria-hidden="true" /> {t('Who is holding what')}</h2>
        {resources.length === 0 && <p className="fs-muted">{t('No resource is claimed.')}</p>}
        <ul className="fs-cnc__rows">
          {resources.map((row) => (
            <li key={row.key} className="fs-cnc__row">
              <div className="fs-cnc__row-head">
                <span className="fs-cnc__paths">{row.resource}</span>
                <Tag tone="neutral">{row.kind}</Tag>
              </div>
              <p className="fs-muted">{t('Held by {who} · {state}', { who: name(row.holderId), state: row.state })}</p>
              {row.contested.length > 0 && (
                <p className="fs-cnc__notice" data-tone="danger">
                  {t('Two active claims name this resource; {who} also appears as a holder.', { who: row.contested.map(name).join(', ') })}
                </p>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="fs-cnc__book">
        <h2 className="fs-cnc__book-head"><Hand size={13} aria-hidden="true" /> {t('Objections still owed an answer')}</h2>
        {open.length === 0 && <p className="fs-muted">{t('None open.')}</p>}
        <ul className="fs-cnc__rows">
          {open.map((objection) => (
            <li key={objection.id} className="fs-cnc__row" data-tone={api.severityTone(objection.severity)}>
              <div className="fs-cnc__row-head">
                <Tag tone={api.severityTone(objection.severity)} icon={objection.severity === 'blocking' ? Ban : undefined}>
                  {objection.severity}
                </Tag>
                <span className="fs-muted">{t('by {who}', { who: name(objection.authorId) })}</span>
              </div>
              <p>{objection.claim || t('(no claim text)')}</p>
              <p className="fs-muted">{t('Against {kind} {id} · {status}', { kind: objection.targetKind, id: objection.targetId, status: objection.status })}</p>
              {objection.proposedResolution && <p className="fs-muted">{t('Proposed: {text}', { text: objection.proposedResolution })}</p>}
            </li>
          ))}
        </ul>
      </section>

      <section className="fs-cnc__book">
        <h2 className="fs-cnc__book-head"><Gavel size={13} aria-hidden="true" /> {t('Decisions in force')}</h2>
        {live.length === 0 && <p className="fs-muted">{t('Nothing has been decided.')}</p>}
        <ul className="fs-cnc__rows">
          {live.map((decision) => (
            <li key={decision.id} className="fs-cnc__row" data-tone={decision.dissenters.length ? 'warn' : 'neutral'}>
              <div className="fs-cnc__row-head">
                <b>{decision.question || t('(no question recorded)')}</b>
                <Tag tone="neutral">{decision.status}</Tag>
              </div>
              <p>{decision.chosen || t('(nothing recorded)')}</p>
              <p className="fs-muted">
                {t('For: {yes}', { yes: decision.supporters.map(name).join(', ') || t('nobody named') })}
              </p>
              {decision.dissenters.length > 0 && (
                <p className="fs-cnc__notice" data-tone="warning">
                  {t('Against: {no}. A decision does not erase the dissent it was taken over.', { no: decision.dissenters.map(name).join(', ') })}
                </p>
              )}
            </li>
          ))}
        </ul>
        {buried.length > 0 && (
          <>
            <Button
              variant="ghost" size="sm" icon={Eye}
              label={showSuperseded ? t('Hide what was superseded') : tn(buried.length, '{n} superseded decision', '{n} superseded decisions')}
              onClick={() => setShowSuperseded((v) => !v)}
            />
            {showSuperseded && (
              <ul className="fs-cnc__rows fs-cnc__rows--muted">
                {buried.map((decision) => (
                  <li key={decision.id} className="fs-cnc__row">
                    <b>{decision.question || decision.id}</b>
                    <p className="fs-muted">{decision.chosen}</p>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
    </aside>
  );
}

/* ── The transcript: authorship is a field, never a prefix in the text ──── */

function Transcript({ messages, participants, config }: {
  messages: api.CouncilMessage[];
  participants: api.Participant[];
  config: api.CouncilConfig | null;
}) {
  const groups = useMemo(() => api.groupByTurn(messages), [messages]);
  if (groups.length === 0) {
    return <p className="fs-muted fs-cnc__empty">{t('Nothing has been said yet. Put the matter to the room.')}</p>;
  }
  return (
    <>
      {groups.map((group, index) => (
        <section key={group.turnId || `loose-${index}`} className="fs-cnc__turn" aria-label={t('Turn {n}', { n: index + 1 })}>
          <p className="fs-cnc__turn-head">
            {group.turnId ? t('Turn {n}', { n: index + 1 }) : t('Outside any turn')}
            {' · '}
            {tn(group.authors.length, '{n} voice', '{n} voices')}
          </p>
          {group.messages.map((message) => {
            const who = api.attributionOf(message, participants, config);
            const audience = api.audienceOf(message, participants).filter((a) => a !== 'room');
            return (
              <article key={message.id} className="fs-cnc__msg" data-kind={message.type} data-author={who.kind} data-hue={who.kind === 'user' ? undefined : hueIndex(who.name)}>
                <header className="fs-cnc__msg-head">
                  {who.kind === 'user' ? null : <Avatar name={who.name} />}
                  <b>{who.kind === 'user' ? t('You') : who.name}</b>
                  <Tag tone={typeTone(message.type)}>{typeLabel(message.type)}</Tag>
                  {who.roles.length > 0 && <span className="fs-muted">{who.roles.join(', ')}</span>}
                  {who.kind !== 'user' && who.readOnly && (
                    <Tag tone="neutral" icon={Lock} title={t('This seat holds no tool that can modify anything.')}>{t('read-only')}</Tag>
                  )}
                  {audience.length > 0 && <span className="fs-muted">{t('to {who}', { who: audience.join(', ') })}</span>}
                  {message.visibility !== 'room' && (
                    <Tag tone="warn" icon={Eye} title={t('Not every seat in the room can read this.')}>{message.visibility}</Tag>
                  )}
                </header>
                {who.claimsIdentity && (
                  <p className="fs-cnc__notice" data-tone="warning">
                    {t('This message opens with «{prefix}». It was written by {who} and nothing about that changed.', { prefix: who.claimsIdentity, who: who.name })}
                  </p>
                )}
                {message.content ? <Rich text={message.content} /> : <p className="fs-muted">{t('(nothing was said)')}</p>}
              </article>
            );
          })}
        </section>
      ))}
    </>
  );
}

/* ── The close: what was decided, what was proved, and why it stopped ───── */

function ClosePanel({ close, status, stopReason, usage, onSynthesise, busy }: {
  close: api.CouncilClose | null;
  status: string;
  stopReason: string;
  usage: api.Usage | null;
  onSynthesise: () => void;
  busy: boolean;
}) {
  const reading = api.closeReading(close?.status || status);
  return (
    <section className="fs-cnc__close" data-tone={reading.tone} aria-label={t('The close')}>
      <header className="fs-cnc__close-head">
        {reading.trusted ? <ShieldCheck size={14} aria-hidden="true" /> : <Scale size={14} aria-hidden="true" />}
        <b>{closeLabel(reading.value)}</b>
        <span className="fs-muted">{reading.note}</span>
        <span className="fs-spacer" />
        <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Close what there is')} loading={busy} onClick={onSynthesise} testId="council-synthesise" />
      </header>

      <p className="fs-cnc__stop">
        <b>{t('Why it stopped')}</b>{' '}
        {stopLabel(close?.stopReason || stopReason)}{' '}
        <span className="fs-muted">{close?.stopReason || stopReason || 'unknown'}</span>
      </p>

      {!close && (
        <p className="fs-muted">
          {t('Only the verdict word arrived with the event. Ask for the close to read the decisions, the changes and the verification behind it.')}
        </p>
      )}

      {close && (
        <>
          <p>{close.result}</p>

          <div className="fs-cnc__close-grid">
            <div>
              <h3 className="fs-cnc__label">{t('Decisions')}</h3>
              {close.decisions.length === 0 && <p className="fs-muted">{t('(none recorded)')}</p>}
              {close.decisions.map((decision) => (
                <p key={decision.id}>
                  <b>{decision.question || decision.id}</b> — {decision.chosen || t('(nothing recorded)')}
                  {decision.dissenters.length > 0 && (
                    <>
                      {' '}
                      <Tag tone="warn">{t('dissent: {who}', { who: decision.dissenters.join(', ') })}</Tag>
                    </>
                  )}
                </p>
              ))}
            </div>

            <div>
              <h3 className="fs-cnc__label">{t('Changes accounted for')}</h3>
              {close.changes.length === 0 && <p className="fs-muted">{t('(none recorded)')}</p>}
              {close.changes.map((change) => (
                <p key={change.taskId}>
                  <b>{change.title || change.taskId}</b>{' '}
                  <Tag tone={api.taskTone(change.status)}>{change.status}</Tag>{' '}
                  <span className="fs-muted fs-cnc__paths">{change.resources.join(' · ') || t('no resource')}</span>
                </p>
              ))}
            </div>

            <div>
              <h3 className="fs-cnc__label">{t('Verification')}</h3>
              <p>
                <Tag tone={api.verdictReading(close.verification.verdict).tone}>
                  {api.verdictReading(close.verification.verdict).label}
                </Tag>{' '}
                <span className="fs-muted">{api.verdictReading(close.verification.verdict).note}</span>
              </p>
              {close.verification.note && <p className="fs-muted">{close.verification.note}</p>}
              {close.verification.uncertainty > 0 && (
                <p className="fs-muted">{tn(close.verification.uncertainty, '{n} thing left uncertain', '{n} things left uncertain')}</p>
              )}
            </div>

            <div>
              <h3 className="fs-cnc__label">{t('Still open')}</h3>
              {close.openObjections.length === 0 && <p className="fs-muted">{t('(none open)')}</p>}
              {close.openObjections.map((objection) => (
                <p key={objection.id}>
                  <Tag tone={api.severityTone(objection.severity)}>{objection.severity}</Tag>{' '}
                  {objection.claim || t('(no claim text)')}
                </p>
              ))}
            </div>
          </div>

          <p className="fs-muted">
            {t('Tokens in/out/total: {in}/{out}/{total} ({source}) · {calls} model call(s) · {seconds}s', {
              in: close.usage.inputTokens, out: close.usage.outputTokens, total: close.usage.totalTokens,
              source: close.usage.source, calls: close.usage.calls, seconds: Math.round(close.usage.wallSeconds),
            })}
          </p>
        </>
      )}

      {usage && (
        <p className="fs-muted">
          <Gauge size={11} aria-hidden="true" />{' '}
          {t('Spent so far: {rounds}/{maxRounds} rounds · {turns}/{maxTurns} turns · {tokens}/{maxTokens} tokens{exhausted}', {
            rounds: usage.spent.rounds, maxRounds: usage.budgets.maxRounds,
            turns: usage.spent.turns, maxTurns: usage.budgets.maxTurns,
            tokens: usage.spent.totalTokens, maxTokens: usage.budgets.maxTotalTokens,
            exhausted: usage.exhausted ? t(' · {limit} ran out', { limit: usage.exhausted }) : '',
          })}
        </p>
      )}
    </section>
  );
}

/* ── The screen ─────────────────────────────────────────────────────────── */

/* Events after which the ledger on screen is stale.
 *
 * The ledger publishes finer names than §1.8's list — a task added, a claim
 * refused, an objection answered, a decision superseded — and every one of them
 * changes something this screen is showing. Leaving them out does not break
 * anything visibly: the panel simply keeps showing the previous state until
 * some other event happens to refresh it, which is the kind of staleness a user
 * reads as "the app is wrong" rather than "the app is late". */
const LEDGER_EVENTS = [
  'council_task_handed_off', 'council_claim_acquired', 'council_claim_released',
  'council_objection_recorded', 'council_decision_recorded', 'council_participant_resolved',
  'council_task_added', 'council_task_assigned', 'council_task_status',
  'council_claim_conflicted', 'council_claim_handoff_refused', 'council_claim_transferred',
  'council_objection_resolved', 'council_decision_superseded',
  'council_activity_verified',
];

export function CouncilScreen() {
  const [params, setParams] = useSearchParams();
  const roomId = params.get('s') ?? '';

  const [config, setConfig] = useState<api.CouncilConfig | null>(null);
  const [routes, setRoutes] = useState<ModelRoute[] | null>(null);
  const [rooms, setRooms] = useState<api.CouncilRoom[] | null>(null);
  const [detail, setDetail] = useState<api.RoomDetail | null>(null);
  const [messages, setMessages] = useState<api.CouncilMessage[]>([]);
  const [ledger, setLedger] = useState<api.Ledger | null>(null);
  const [usage, setUsage] = useState<api.Usage | null>(null);

  const [turn, setTurn] = useState<{ id: string; state: string }>({ id: '', state: '' });
  const [closeStatus, setCloseStatus] = useState('');
  const [stopReason, setStopReason] = useState('');
  const [close, setClose] = useState<api.CouncilClose | null>(null);
  const [gap, setGap] = useState<api.Gap | null>(null);
  const [live, setLive] = useState(true);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [steering, setSteering] = useState<{ id: string; text: string } | null>(null);
  const [rerole, setRerole] = useState<{ id: string; role: string } | null>(null);
  const [handoff, setHandoff] = useState<{ taskId: string; to: string } | null>(null);

  const cursor = useRef(0);
  const endRef = useRef<HTMLDivElement>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    api.loadConfig(controller.signal).then(setConfig).catch(() => setConfig(null));
    listModels(controller.signal).then(setRoutes).catch(() => setRoutes([]));
    api.listRooms(controller.signal).then(setRooms).catch(() => setRooms([]));
    return () => controller.abort();
  }, []);

  const refresh = useCallback(async (id: string) => {
    if (!id) return;
    const [room, transcript, book, spend] = await Promise.all([
      api.loadRoom(id).catch(() => null),
      api.loadMessages(id).catch(() => [] as api.CouncilMessage[]),
      api.loadLedger(id).catch(() => null),
      api.loadUsage(id).catch(() => null),
    ]);
    if (room) setDetail(room);
    setMessages(transcript);
    if (book) setLedger(book);
    if (spend) setUsage(spend);
  }, []);

  /* Opening a room: everything is read once, then the stream keeps it fresh. */
  useEffect(() => {
    if (!roomId) {
      setDetail(null); setMessages([]); setLedger(null); setUsage(null);
      setClose(null); setCloseStatus(''); setStopReason(''); setGap(null);
      cursor.current = 0;
      return;
    }
    cursor.current = 0;
    setGap(null);
    void refresh(roomId);
  }, [roomId, refresh]);

  /**
   * The live half. `advanceCursor` is what keeps a reconnection honest: an
   * event at or before the cursor is dropped instead of shown twice, and the
   * gap marker the server sends when its buffer overflowed is surfaced rather
   * than smoothed over — a hole nobody is told about costs a decision nobody
   * knows was taken.
   */
  useEffect(() => {
    if (!roomId) return;
    let stopped = false;
    let closeStream: (() => void) | null = null;
    const controller = new AbortController();
    let pending: number | null = null;

    const soon = () => {
      if (pending !== null) return;
      pending = window.setTimeout(() => { pending = null; void refresh(roomId); }, 600);
    };

    const apply = (events: api.CouncilEvent[]) => {
      const resume = api.advanceCursor(cursor.current, events);
      cursor.current = resume.cursor;
      if (resume.gap) setGap(resume.gap);
      for (const event of resume.applied) {
        const payload = event.payload as Record<string, unknown>;
        if (event.name === 'council_turn_state') {
          setTurn({ id: String(payload.turn_id ?? ''), state: String(payload.state ?? '') });
        } else if (event.name === 'council_activity_completed') {
          setCloseStatus(String(payload.status ?? ''));
          setStopReason(String(payload.stop_reason ?? ''));
        } else if (event.name === 'council_activity_blocked') {
          setStopReason(String(payload.stop_reason ?? '') || 'blocked');
          if (payload.reason) say(String(payload.reason), 'warn');
        } else if (event.name === 'council_activity_verified') {
          // The one good-news event in the stream, and the only one a reader
          // must not confuse with a task reporting itself finished.
          say(t('A task was verified against what changed on disk.'));
        } else if (event.name === 'council_error' && payload.detail) {
          say(String(payload.detail), 'warn');
        }
        if (event.name === 'council_message' || event.name === 'council_usage'
            || LEDGER_EVENTS.indexOf(event.name) >= 0 || event.name === 'council_activity_completed') {
          soon();
        }
      }
    };

    const poll = async () => {
      while (!stopped) {
        try {
          apply(await api.waitForEvents(roomId, cursor.current, controller.signal));
        } catch {
          if (stopped) return;
          await new Promise((resolve) => window.setTimeout(resolve, 3000));
        }
      }
    };

    // The stream has a deadline and closes itself with a named `end` frame
    // asking to be reopened from the cursor we now hold. Reopening from that
    // number is what makes the reconnection cost nothing: the server answers
    // strictly what follows it.
    const openStream = () => {
      closeStream = api.followRoom(
        roomId,
        cursor.current,
        (event) => apply([event]),
        () => { if (stopped) return; setLive(false); void poll(); },
        () => { if (stopped) return; setLive(true); openStream(); },
      );
    };
    openStream();

    return () => {
      stopped = true;
      if (pending !== null) window.clearTimeout(pending);
      controller.abort();
      closeStream?.();
    };
  }, [roomId, refresh, say]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [messages]);

  const run = useCallback(async (label: string, name: string, args: Record<string, unknown> = {}) => {
    if (!roomId) return;
    setBusy(name);
    try {
      await api.command(roomId, name, args);
      say(label);
      void refresh(roomId);
    } catch (error) {
      const refusal = error as api.CouncilRefusal;
      say(refusal.code ? `${refusal.code}: ${refusal.message}` : (error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [roomId, refresh, say]);

  const send = async () => {
    const content = draft.trim();
    if (!content || !roomId) return;
    setDraft('');
    setBusy('send');
    try {
      // One key per composed message: a retry of the same POST lands on the
      // turn the first one opened instead of paying for a second round.
      const answer = await api.postMessage(roomId, content, {
        idempotencyKey: `${roomId}:${Date.now().toString(36)}`,
      });
      setTurn({ id: answer.turnId, state: answer.status });
      setClose(null);
      setCloseStatus('');
      setStopReason('');
      void refresh(roomId);
    } catch (error) {
      const refusal = error as api.CouncilRefusal;
      say(refusal.code ? `${refusal.code}: ${refusal.message}` : (error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  };

  const synthesise = async () => {
    if (!roomId) return;
    setBusy('synthesis');
    try {
      const answer = await api.requestSynthesis(roomId);
      setClose(answer.close);
      if (answer.close) setCloseStatus(answer.close.status);
      if (answer.stopReason) setStopReason(answer.stopReason);
      void refresh(roomId);
    } catch (error) {
      say((error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  };

  const room = detail?.room ?? null;
  const seats = detail?.participants ?? [];
  const block = useMemo(() => api.roomBlock(ledger), [ledger]);
  const paused = room?.status === 'paused';

  if (routes === null || rooms === null || config === null) {
    return <Skeleton label={t('Loading the council')} count={4} height="48px" />;
  }

  return (
    <div className="fs-screen fs-cnc" data-testid="council">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{room ? room.title || t('Council') : t('Council')}</h1>
          <p className="fs-prose fs-cnc__lede">
            {room
              ? t('{policy} · {status} · revision {revision} · {n} seats', { policy: room.policy, status: room.status, revision: room.revision, n: seats.length })
              : t('Several models think about one matter, and exactly one of them may act on each resource. Group chat could not tell you who had said what; here authorship and permission are fields, not prose.')}
          </p>
        </div>
        {room && (
          <div className="fs-inline">
            <Button variant="ghost" size="sm" icon={Users} label={t('Rooms')} onClick={() => setParams({}, { replace: true })} />
            <Menu
              trigger={<Button variant="secondary" size="sm" icon={Hand} label={t('Take the floor')} />}
              align="end"
              items={[
                paused
                  ? { label: t('Resume'), icon: Play, onSelect: () => void run(t('The room is running again.'), 'resume') }
                  : { label: t('Pause (nothing new starts; what is running finishes)'), icon: Pause, onSelect: () => void run(t('Paused. No new call will start.'), 'pause') },
                { label: t('Cancel this turn'), icon: Square, variant: 'danger', onSelect: () => void run(t('The turn was cancelled.'), 'cancel_turn', { turn_id: turn.id }) },
                { label: t('Ask for a synthesis'), icon: Scale, onSelect: () => void synthesise() },
              ]}
            />
          </div>
        )}
      </header>

      {!roomId && (
        <>
          {rooms.length > 0 && (
            <section className="fs-cnc__list" aria-label={t('Your rooms')}>
              {rooms.map((entry) => (
                <button key={entry.id} type="button" className="fs-cnc__card" onClick={() => setParams({ s: entry.id }, { replace: true })}>
                  <b>{entry.title || entry.id}</b>
                  <span className="fs-muted">{t('{policy} · {status} · {n} seats', { policy: entry.policy, status: entry.status, n: entry.participantIds.length })}</span>
                </button>
              ))}
            </section>
          )}
          {routes.length === 0
            ? <EmptyState icon={Users} title={t('No models')} body={t('Add an endpoint in Settings → Models first.')} />
            : <Setup config={config} routes={routes} say={say} onOpened={(id) => setParams({ s: id }, { replace: true })} />}
        </>
      )}

      {room && (
        <>
          {gap && (
            <p className="fs-cnc__notice" data-tone="warning" role="status">
              {t('{n} event(s) between {from} and {to} were dropped by the stream ({reason}). What follows is complete from there on; the hole is not filled in.', {
                n: gap.missed, from: gap.fromSeq, to: gap.toSeq, reason: gap.reason,
              })}
            </p>
          )}
          {!live && (
            <p className="fs-cnc__notice" data-tone="warning" role="status">
              {t('The live stream dropped. The room is being read by polling from event {seq} on, so nothing is repeated and nothing is skipped.', { seq: cursor.current })}
            </p>
          )}
          {paused && <p className="fs-cnc__notice" data-tone="warning">{t('Paused by you. Work already in flight finishes and is recorded.')}</p>}

          <div className="fs-cnc__seats" aria-label={t('Who is in the room')}>
            {seats.map((seat) => {
              const writes = api.canWrite(seat.toolProfile, config);
              return (
                <div key={seat.id} className="fs-cnc__seat" data-write={writes ? 'yes' : 'no'}>
                  <Avatar name={seat.displayName} />
                  <div className="fs-cnc__seat-id">
                    <b>{seat.displayName}</b>
                    <small className="fs-muted">{seat.model}</small>
                  </div>
                  <Tag tone={writes ? 'warn' : 'neutral'} icon={writes ? UserCog : Lock} title={writes ? t('This seat may modify things it owns.') : t('This seat holds no tool that can modify anything.')}>
                    {writes ? seat.toolProfile : t('read-only')}
                  </Tag>
                  {seat.roles.length > 0 && <span className="fs-muted">{seat.roles.join(', ')}</span>}
                  <span className="fs-spacer" />
                  <IconButton icon={Megaphone} label={t('Steer {name}', { name: seat.displayName })} size="sm" onClick={() => setSteering({ id: seat.id, text: '' })} />
                  <IconButton icon={UserCog} label={t('Give {name} another role', { name: seat.displayName })} size="sm" onClick={() => setRerole({ id: seat.id, role: config.roles[0]?.id ?? '' })} />
                  <IconButton icon={Ban} label={t('Stop {name}', { name: seat.displayName })} size="sm" onClick={() => void run(t('{name} was stopped.', { name: seat.displayName }), 'stop_participant', { participant_id: seat.id })} />
                </div>
              );
            })}
          </div>

          <div className="fs-cnc__body">
            <section className="fs-cnc__room" aria-live="polite">
              {turn.state && turn.state !== 'completed' && (
                <p className="fs-muted fs-cnc__turn-state">
                  <MessageSquare size={11} aria-hidden="true" /> {t('This turn is {state}', { state: turn.state })}
                </p>
              )}
              <Transcript messages={messages} participants={seats} config={config} />
              <div ref={endRef} />
            </section>
            <LedgerPanel ledger={ledger} participants={seats} onHandoff={(taskId) => setHandoff({ taskId, to: seats[0]?.id ?? '' })} />
          </div>

          <ClosePanel
            close={close}
            status={closeStatus || (block.blocked ? 'blocked' : block.disputed ? 'disputed' : '')}
            stopReason={stopReason}
            usage={usage}
            busy={busy === 'synthesis'}
            onSynthesise={() => void synthesise()}
          />

          <footer className="fs-cnc__composer">
            <textarea
              className="fs-cnc__textarea"
              rows={2}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t('Put the matter to the room…')}
              aria-label={t('Put the matter to the room')}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              data-testid="council-draft"
            />
            <Button variant="primary" size="sm" icon={Send} label={t('Send')} loading={busy === 'send'} disabled={!draft.trim()} onClick={() => void send()} testId="council-send" />
          </footer>
        </>
      )}

      {steering && (
        <Dialog
          open onOpenChange={(o) => !o && setSteering(null)} title={t('Steer a participant')} testId="council-steer"
          footer={<Button variant="primary" size="sm" label={t('Say it')} disabled={!steering.text.trim()} onClick={() => { void run(t('The message was delivered.'), 'steer', { participant_id: steering.id, message: steering.text.trim() }); setSteering(null); }} />}
        >
          <p className="fs-muted">{t('This arrives as an instruction from you, and it does not change what that seat is allowed to do.')}</p>
          <textarea className="fs-cnc__textarea" rows={3} value={steering.text} autoFocus aria-label={t('What to say')} onChange={(e) => setSteering({ ...steering, text: e.target.value })} />
        </Dialog>
      )}

      {rerole && (
        <Dialog
          open onOpenChange={(o) => !o && setRerole(null)} title={t('Give another role')} testId="council-role"
          footer={<Button variant="primary" size="sm" label={t('Assign it')} disabled={!rerole.role} onClick={() => { void run(t('The role was recorded.'), 'assign_role', { participant_id: rerole.id, role: rerole.role }); setRerole(null); }} />}
        >
          <p className="fs-muted">{t('A role says what is expected. What the seat may reach is recomputed on the server and can only go down.')}</p>
          <select className="fs-field" value={rerole.role} aria-label={t('Role')} onChange={(e) => setRerole({ ...rerole, role: e.target.value })}>
            {config.roles.map((role) => <option key={role.id} value={role.id}>{role.id}</option>)}
          </select>
        </Dialog>
      )}

      {handoff && (
        <Dialog
          open onOpenChange={(o) => !o && setHandoff(null)} title={t('Hand the task over')} testId="council-handoff"
          footer={<Button variant="primary" size="sm" label={t('Hand it over')} disabled={!handoff.to} onClick={() => { void run(t('The task changed hands.'), 'handoff_task', { task_id: handoff.taskId, to: handoff.to }); setHandoff(null); }} />}
        >
          <p className="fs-muted">{t('A handoff is refused while a mutating tool is still running: the owner never changes by writing another id.')}</p>
          <select className="fs-field" value={handoff.to} aria-label={t('New owner')} onChange={(e) => setHandoff({ ...handoff, to: e.target.value })}>
            {seats.map((seat) => <option key={seat.id} value={seat.id}>{seat.displayName}</option>)}
          </select>
        </Dialog>
      )}

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
