import { Clock, Database, Eye, Play, RefreshCw, Scale, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, Skeleton, Toast } from '../components';
import * as api from '../adapters/stateMirror';
import { t } from '../i18n';
import './stateMirror.css';

/**
 * State Mirror: what is true right now, when we last looked, and how we know.
 *
 * The page answers five questions in the order a person actually asks them:
 * what is running right now, what is waiting for me, what is stale or never
 * observed, what is in conflict, and what the machine has. The order is not a
 * layout preference; it is `adapters/stateMirror.ts::SITUATIONS`, and the
 * screen renders the groups in the order it is handed them.
 *
 * The rule everything else here serves: A STALE VALUE IS NEVER RENDERED AS IF
 * IT WERE CURRENT. Every value carries its rating as a word, a tone and a
 * strike-through, and its tooltip says when it was observed and by which
 * source. Three signals rather than one, because a reader who cannot see the
 * colour still has to be told, and this is the screen where not being told
 * costs a wrong decision.
 *
 * Nothing here derives anything. Freshness, situations, ids and cursors are
 * all pure functions in the adapter, driven by
 * `studio/checks/stateMirror.check.mjs`; a panel whose arithmetic lived inside
 * a component would be a panel nobody could check.
 */

type Tone = api.Tone;

function Tag({ tone, icon: Icon, title, children }: {
  tone: Tone;
  icon?: typeof Clock;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span className="fs-stm__tag" data-tone={tone} title={title}>
      {Icon ? <Icon size={11} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

/** The four freshness words, each spelled out. Never a colour on its own. */
function freshnessWord(rating: string): string {
  switch (rating) {
    case 'fresh': return t('current');
    case 'aging': return t('ageing');
    case 'stale': return t('stale');
    case 'mixed': return t('mixed');
    default: return t('never observed');
  }
}

/** How strongly a value is known. `reported` is not evidence and says so. */
function epistemicWord(epistemic: string): string {
  switch (epistemic) {
    case 'observed': return t('observed');
    case 'derived': return t('derived');
    case 'reported': return t('reported, not checked');
    case 'inferred': return t('inferred');
    default: return t('not known');
  }
}

function situationHeading(situation: string): string {
  switch (situation) {
    case 'running': return t('Running right now');
    case 'waiting': return t('Waiting for you');
    case 'stale': return t('Stale or never observed');
    case 'conflict': return t('In conflict');
    default: return t('What the machine has');
  }
}

function situationLede(situation: string): string {
  switch (situation) {
    case 'running': return t('Work happening on this machine, as seen within its guarantee.');
    case 'waiting': return t('Stopped, and waiting for a person to decide.');
    case 'stale': return t('Nothing known about these is current. They are shown as they were, with their age, and not as the state now.');
    case 'conflict': return t('Two sources disagree. Nothing here has been chosen for you.');
    default: return t('Everything else the mirror holds: the devices, models, services and connections it has looked at.');
  }
}

/**
 * A value, and never a bare one.
 *
 * `null` is not `false` and not `0`: "we have never looked" and "we looked and
 * it was empty" are different facts, and only one of them is safe to act on.
 * So an unobserved value is drawn as a sentence, in italics, and never as a
 * blank that a reader would fill in themselves.
 */
function shownValue(value: unknown): string {
  if (value === null || value === undefined) return t('not observed');
  if (typeof value === 'boolean') return value ? t('yes') : t('no');
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return value || t('(empty)');
  if (Array.isArray(value)) return value.length ? value.map((v) => String(v)).join(', ') : t('(empty)');
  try {
    return JSON.stringify(value);
  } catch {
    return t('unreadable');
  }
}

/**
 * One field: its name, its value, and the provenance that qualifies it.
 *
 * The tooltip is the whole of section 6 in one sentence, because a rating a
 * person cannot interrogate is one they will either over-trust or ignore.
 */
function Field({ field }: { field: api.FieldValue }) {
  const p = api.provenanceOf(field);
  const current = api.isCurrent(field);
  const title = p.observedAt
    ? t('{rating}. Observed {age}s ago by {source}, and guaranteed for {ttl}s. Known: {epistemic}.', {
      rating: freshnessWord(field.freshness),
      age: p.ageSeconds === null ? '?' : Math.round(p.ageSeconds),
      source: p.source || t('an unnamed source'),
      ttl: p.ttlSeconds || t('no stated time'),
      epistemic: epistemicWord(field.epistemic),
    })
    : t('Nothing has observed this, so there is no value to show and none is being implied.');

  return (
    <div className="fs-stm__field">
      <span className="fs-stm__field-name">{field.name}</span>
      <span className="fs-stm__value" data-freshness={field.freshness} data-current={current ? 'yes' : 'no'} title={title}>
        {shownValue(field.value)}
      </span>
      <Tag tone={p.freshness.tone} title={title}>{freshnessWord(field.freshness)}</Tag>
    </div>
  );
}

/**
 * One entity, as a card.
 *
 * The card is tinted by its WORST field, not by its best: a row is as
 * trustworthy as the least trustworthy thing on it, and a green card with one
 * stale number in it is exactly the lie this screen exists to prevent.
 */
function Card({ entity, onOpen }: { entity: api.StateEntity; onOpen: (id: string) => void }) {
  /* The field that says what this row is DOING, and failing that the first
     field it has at all. The fallback matters: a session carries `active` and
     nothing else in this build, so a schema whose deciding field was never
     observed used to draw "nothing has been observed about this yet" directly
     above "1 field(s)" -- two true sentences that contradict each other, on a
     screen whose entire purpose is being trusted about what is known. */
  const deciding = api.decidingField(entity) ?? entity.fields[0] ?? null;
  const reading = api.freshnessReading(entity.worst);
  return (
    <button type="button" className="fs-stm__card" data-worst={entity.worst} onClick={() => onOpen(entity.id)}>
      <span className="fs-stm__card-top">
        <span className="fs-stm__card-name">{api.label(entity)}</span>
        <span className="fs-spacer" />
        <Tag tone={reading.tone}>{freshnessWord(entity.worst)}</Tag>
      </span>
      <span className="fs-stm__card-id">{entity.id}</span>
      {deciding ? <Field field={deciding} /> : (
        <span className="fs-stm__provenance">{t('Nothing has been observed about this yet.')}</span>
      )}
      <span className="fs-stm__provenance">
        {t('{kind} · {n} field(s) · {unknown} never observed', {
          kind: entity.kind || t('entity'), n: entity.fields.length, unknown: entity.unknownFields.length,
        })}
      </span>
    </button>
  );
}

function Section({ group, onOpen }: { group: api.SituationGroup; onOpen: (id: string) => void }) {
  return (
    <section className="fs-stm__section" aria-label={situationHeading(group.situation)}>
      <header className="fs-stm__head">
        <h2 className="fs-stm__title">{situationHeading(group.situation)}</h2>
        <span className="fs-stm__count">{group.entities.length}</span>
      </header>
      <p className="fs-muted fs-stm__provenance">{situationLede(group.situation)}</p>
      {group.entities.length === 0
        ? <p className="fs-muted">{t('Nothing.')}</p>
        : (
          <div className="fs-stm__cards">
            {group.entities.map((entity) => <Card key={entity.id} entity={entity} onOpen={onOpen} />)}
          </div>
        )}
    </section>
  );
}

/** A conflict: both claims, side by side, and neither of them chosen. */
function ConflictCard({ conflict }: { conflict: api.Conflict }) {
  return (
    <div className="fs-stm__conflict">
      <b>
        {t('{field} on {entity}', { field: conflict.field, entity: conflict.entityId })}
      </b>
      <span className="fs-stm__provenance">
        {conflict.nextCheck
          ? t('Nothing was chosen. What would settle it: {check}', { check: conflict.nextCheck })
          : t('Nothing was chosen, and nothing has been named that would settle it.')}
      </span>
      <div className="fs-stm__claims">
        {conflict.claims.map((claim, index) => (
          <div className="fs-stm__claim" key={`${claim.source}-${index}`}>
            <b className="fs-stm__mono">{shownValue(claim.value)}</b>
            <div className="fs-stm__provenance">
              {t('{source} · {epistemic} · {at}', {
                source: claim.source || t('an unnamed source'),
                epistemic: epistemicWord(claim.epistemic),
                at: claim.observedAt || t('no time recorded'),
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Where all of this came from, and which of those sources is answering. */
function Sources({ diagnostics, openConflicts }: {
  diagnostics: api.Diagnostics | null;
  openConflicts: number;
}) {
  if (!diagnostics) return null;
  const tone = (health: string): Tone => {
    if (health === 'ok') return 'ok';
    if (health === 'degraded') return 'warn';
    if (health === 'down') return 'danger';
    return 'neutral';
  };
  const word = (health: string): string => {
    switch (health) {
      case 'ok': return t('answering');
      case 'degraded': return t('degraded');
      case 'down': return t('not answering');
      default: return t('never heard from');
    }
  };
  return (
    <section className="fs-stm__section" aria-label={t('Where this comes from')}>
      <header className="fs-stm__head">
        <h2 className="fs-stm__title">{t('Where this comes from')}</h2>
        <span className="fs-stm__count">
          {t('{n} entities · {o} observations · {c} open conflicts', {
            n: diagnostics.entities, o: diagnostics.observations, c: openConflicts,
          })}
        </span>
      </header>
      <p className="fs-muted fs-stm__provenance">
        {t('A source that is not answering does not freeze what it last said: every field it owns ages to stale on its own, which is why a panel can be honest about a probe that died.')}
        {' '}
        {diagnostics.sweepSeconds > 0
          ? t('The sweep looks again every {n}s.', { n: diagnostics.sweepSeconds })
          : t('No sweep is running.')}
      </p>
      <div className="fs-stm__sources">
        {diagnostics.sources.map((source) => (
          <div className="fs-stm__source" key={source.name}>
            <span className="fs-stm__card-top">
              <b>{source.name}</b>
              <span className="fs-spacer" />
              <Tag tone={tone(source.health)}>{word(source.health)}</Tag>
            </span>
            <span className="fs-stm__provenance">
              {source.lastSuccessAt
                ? t('last answered {at}', { at: source.lastSuccessAt })
                : t('has never answered')}
            </span>
            <span className="fs-stm__provenance">
              {t('{n} observation(s), {f} failure(s)', { n: source.observations, f: source.failures })}
            </span>
            {source.detail ? <span className="fs-stm__provenance">{source.detail}</span> : null}
          </div>
        ))}
      </div>
    </section>
  );
}

/**
 * One entity, in full: every field with its provenance, the fields the schema
 * declares that nothing has ever observed, and the observations behind them.
 *
 * The unobserved fields are LISTED rather than omitted. An absence a consumer
 * cannot see is one they will eventually read as a `false`.
 */
function Recovery({ entityId, onRebuilt }: { entityId: string; onRebuilt: () => void }) {
  const [receipt, setReceipt] = useState<api.ReplayCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  const pending = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const check = async (repair = false) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError('');
    try {
      const answer = await api.verifyMaterialization(entityId, repair ? receipt?.receipt.sha256 : '');
      if (!alive.current) return;
      setReceipt(answer);
      if (answer.repaired) onRebuilt();
    } catch {
      if (!alive.current) return;
      setReceipt(null);
      setError(t('The journal could not be verified. It may be unavailable, damaged or changed. No state was replaced. Check the connection and try again.'));
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  };
  return <section className="fs-stm__recovery" aria-label={t('State integrity')}>
    <h3 className="fs-stm__label">{t('State integrity')}</h3>
    <p className="fs-muted fs-stm__provenance">{t('Compare this saved view with its verified journal. Repair restores the saved state, not the original files or services.')}</p>
    <div className="fs-inline">
      <Button size="sm" label={t('Verify saved state')} loading={busy} onClick={() => void check()} />
      {receipt && !receipt.matches && !receipt.repaired && <Button size="sm" variant="danger"
        label={t('Restore verified state')} disabled={busy} onClick={() => void check(true)} />}
    </div>
    <div role="status">
      {error && <p className="fs-stm__notice" data-tone="warning">{error}</p>}
      {receipt && <p className="fs-stm__provenance">{receipt.repaired
        ? t('Saved state restored from the verified journal.')
        : receipt.matches ? t('Saved state matches the verified journal.')
          : t('The saved view differs from its journal. You can restore the verified state.')}</p>}
      {receipt?.receipt.origin === 'legacy_checkpoint' && <p className="fs-muted fs-stm__provenance">
        {t('Verification starts from a checkpoint created during upgrade; older probe history is not reconstructed.')}
      </p>}
    </div>
  </section>;
}

function Detail({ entity, history, conflicts, busy, canRefresh, onRefresh, onClose, onRebuilt }: {
  entity: api.StateEntity;
  history: api.Observation[] | null;
  conflicts: api.Conflict[];
  busy: boolean;
  canRefresh: boolean;
  onRefresh: () => void;
  onClose: () => void;
  onRebuilt: () => void;
}) {
  const current = api.currentFields(entity);
  const aged = api.agedFields(entity);
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={api.label(entity)}
      testId="state-detail"
      footer={(
        <Button
          variant="primary" size="sm" icon={RefreshCw} label={t('Look again')}
          loading={busy} disabled={!canRefresh}
          title={canRefresh
            ? t('Ask the source that owns this to look again now.')
            : t('The mirror is switched off, so nothing may probe this machine. What is shown stays readable.')}
          onClick={onRefresh}
        />
      )}
    >
      <div className="fs-stm__detail">
        <p className="fs-stm__card-id">{entity.id}</p>
        <p className="fs-muted fs-stm__provenance">
          {t('{kind} · schema {schema} · revision {revision} · namespace {namespace}', {
            kind: entity.kind || t('entity'), schema: entity.schema || t('none'),
            revision: entity.revision, namespace: entity.namespace,
          })}
        </p>

        {conflicts.length > 0 && (
          <>
            <p className="fs-stm__label">{t('In conflict')}</p>
            {conflicts.map((conflict) => <ConflictCard key={conflict.id} conflict={conflict} />)}
          </>
        )}

        <p className="fs-stm__label">{t('Current')}</p>
        {current.length === 0
          ? <p className="fs-stm__notice" data-tone="warning">{t('Nothing about this is current. Everything below is being shown as it was, not as it is.')}</p>
          : <div className="fs-stm__fields">{current.map((f) => <Field key={f.name} field={f} />)}</div>}

        {aged.length > 0 && (
          <>
            <p className="fs-stm__label">{t('Not current')}</p>
            <p className="fs-muted fs-stm__provenance">
              {t('Shown as it was last seen. Do not act on any of it without looking again first.')}
            </p>
            <div className="fs-stm__fields">{aged.map((f) => <Field key={f.name} field={f} />)}</div>
          </>
        )}

        {entity.unknownFields.length > 0 && (
          <>
            <p className="fs-stm__label">{t('Never observed')}</p>
            <p className="fs-muted fs-stm__provenance">
              {t('Declared by the schema and never looked at. Listed here rather than left out, because an absence nobody can see gets read as a no.')}
            </p>
            <p className="fs-stm__mono fs-stm__provenance">{entity.unknownFields.join(', ')}</p>
          </>
        )}

        <Recovery key={entity.id} entityId={entity.id} onRebuilt={onRebuilt} />
        <p className="fs-stm__label">{t('How we know')}</p>
        {history === null
          ? <Skeleton label={t('Reading the observations')} count={2} height="18px" />
          : history.length === 0
            ? <p className="fs-muted">{t('No observation has been recorded for this yet.')}</p>
            : (
              <ul className="fs-stm__history">
                {history.map((observation) => (
                  <li className="fs-stm__observation" key={observation.id}>
                    <span className="fs-stm__mono">{observation.observedAt}</span>
                    <Tag tone={api.epistemicReading(observation.epistemic).tone} icon={Eye}>
                      {epistemicWord(observation.epistemic)}
                    </Tag>
                    <span>{observation.source}</span>
                    <span className="fs-muted">
                      {observation.partial
                        ? t('{n} field(s) sampled', { n: Object.keys(observation.state).length })
                        : t('{n} field(s), a complete snapshot', { n: Object.keys(observation.state).length })}
                    </span>
                  </li>
                ))}
              </ul>
            )}
      </div>
    </Dialog>
  );
}

export function StateMirrorScreen() {
  const [params, setParams] = useSearchParams();
  const selectedId = api.decodeEntityId(params.get('e') ?? '');

  const [entities, setEntities] = useState<api.StateEntity[] | null>(null);
  const [conflicts, setConflicts] = useState<api.Conflict[]>([]);
  const [diagnostics, setDiagnostics] = useState<api.Diagnostics | null>(null);
  const [history, setHistory] = useState<api.Observation[] | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [busy, setBusy] = useState('');
  const [live, setLive] = useState(true);
  const [dropped, setDropped] = useState(0);
  // Two cursors, because they count different things: `seq` resumes the event
  // stream, and `changes` is the store's own monotonic revision counter. Using
  // one for the other would either replay events or skip changes.
  const seq = useRef(0);
  const changes = useRef(0);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    window.setTimeout(() => setNotice(null), 4000);
  }, []);

  /**
   * Two reads and not one, because they carry different halves of a row.
   * `/entities` names the things (kind, display name, schema) and summarises
   * their state; `/changes` from the beginning carries the materialised state
   * itself, with every field's value, age and source. Merging them is how a
   * card can show a value AND be labelled, and the merge is in the adapter so
   * a state arriving without a name does not blank the name on screen.
   */
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [rows, batch, open, diag] = await Promise.all([
        api.loadEntities({ limit: 200 }, signal),
        api.loadChanges(0, signal),
        api.loadConflicts('', signal),
        api.loadDiagnostics(signal),
      ]);
      setEntities(api.mergeEntities(rows, batch.states));
      setConflicts(open);
      setDiagnostics(diag);
      changes.current = Math.max(changes.current, batch.cursor, diag.cursor);
    } catch (error) {
      if (signal?.aborted) return;
      setEntities((current) => current ?? []);
      say((error as Error).message, 'warn');
    }
  }, [say]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  /**
   * Follow the mirror live, and fall back to the change feed when the stream
   * cannot be held open. Either way the page pulls the CHANGED ROWS from
   * `/changes` rather than trusting an event payload to carry a whole state:
   * an event says something moved, the store says what it now is, and only
   * one of those two can be behind.
   */
  useEffect(() => {
    let stopped = false;
    let closeStream: (() => void) | null = null;
    let pending: number | null = null;

    const pull = async () => {
      try {
        const batch = await api.loadChanges(changes.current);
        if (stopped) return;
        if (batch.cursor > changes.current) changes.current = batch.cursor;
        if (batch.states.length) {
          setEntities((current) => api.mergeEntities(current ?? [], batch.states));
          const open = await api.loadConflicts('');
          if (!stopped) setConflicts(open);
        }
      } catch {
        /* one failed poll is not a dead page; the next event asks again */
      }
    };

    const soon = () => {
      if (pending !== null) window.clearTimeout(pending);
      pending = window.setTimeout(() => { pending = null; void pull(); }, 250);
    };

    const apply = (events: api.StateEvent[]) => {
      const resume = api.advanceCursor(seq.current, events);
      seq.current = resume.cursor;
      if (resume.duplicates) setDropped((n) => n + resume.duplicates);
      if (resume.applied.length) soon();
    };

    const openStream = () => {
      closeStream = api.followState(
        seq.current,
        (event) => apply([event]),
        () => { if (!stopped) setLive(false); },
        () => { if (stopped) return; setLive(true); openStream(); },
      );
    };
    openStream();

    // The poll is the floor under the stream, not a duplicate of it: with the
    // stream up it finds nothing and costs one query, and with the stream down
    // it is the only thing keeping the page from quietly going out of date.
    const tick = window.setInterval(() => { void pull(); }, 15000);
    return () => {
      stopped = true;
      if (pending !== null) window.clearTimeout(pending);
      window.clearInterval(tick);
      closeStream?.();
    };
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setHistory(null);
      return;
    }
    const controller = new AbortController();
    setHistory(null);
    api.loadHistory(selectedId, 50, controller.signal)
      .then((rows) => setHistory(rows))
      .catch(() => setHistory([]));
    return () => controller.abort();
  }, [selectedId]);

  const enabled = diagnostics?.enabled ?? false;

  const lookAgain = useCallback(async (entityId: string) => {
    setBusy(entityId || 'all');
    try {
      await api.refresh(entityId);
      say(entityId ? t('It was looked at again.') : t('Everything aged out was looked at again.'));
      await load();
    } catch (error) {
      const refusal = error as api.StateRefusal;
      say(refusal.code ? `${refusal.code}: ${refusal.message}` : (error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [load, say]);

  const settle = useCallback(async () => {
    setBusy('reconcile');
    try {
      const answer = await api.reconcile();
      say(t('Reconciliation ran. {n} conflict(s) were settled.', { n: Number(answer.resolved ?? 0) }));
      await load();
    } catch (error) {
      const refusal = error as api.StateRefusal;
      say(refusal.code ? `${refusal.code}: ${refusal.message}` : (error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [load, say]);

  const groups = useMemo(
    () => api.groupBySituation(api.withConflicts(entities ?? [], conflicts)),
    [entities, conflicts],
  );
  const selected = useMemo(
    () => (entities ?? []).find((entity) => entity.id === selectedId) ?? null,
    [entities, selectedId],
  );
  const selectedConflicts = useMemo(
    () => conflicts.filter((conflict) => conflict.entityId === selectedId),
    [conflicts, selectedId],
  );

  if (entities === null) {
    return <Skeleton label={t('Reading the mirror')} count={4} height="48px" />;
  }

  return (
    <div className="fs-screen fs-stm" data-testid="state-mirror">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('State Mirror')}</h1>
          <p className="fs-prose fs-stm__lede">
            {t('What is true right now, when we last looked, and how we know. Nothing here is the original: every value is a projection of the system that owns it, and anything about to act checks that system first.')}
          </p>
        </div>
        <div className="fs-inline">
          <Button
            variant="secondary" size="sm" icon={RefreshCw} label={t('Look again')}
            loading={busy === 'all'} disabled={!enabled}
            title={enabled
              ? t('Re-observe everything that has aged past its guarantee.')
              : t('The mirror is switched off, so nothing may probe this machine.')}
            onClick={() => void lookAgain('')}
          />
          <Button
            variant="secondary" size="sm" icon={Scale} label={t('Reconcile')}
            loading={busy === 'reconcile'} disabled={!enabled}
            title={enabled
              ? t('Ask the sources that disagree, and settle what can be settled.')
              : t('The mirror is switched off, so nothing may probe this machine.')}
            onClick={() => void settle()}
          />
        </div>
      </header>

      {!enabled && (
        <p className="fs-stm__notice" data-tone="warning" role="status">
          {t('The mirror is switched off (Settings → Agent & automation → State Mirror). Nothing is probing this machine, so nothing below will get any fresher; everything already observed keeps answering, with its age on it.')}
        </p>
      )}

      {!live && (
        <p className="fs-stm__notice" data-tone="warning" role="status">
          {t('The live stream dropped. The page is being kept up to date by polling the change feed from revision {cursor} on, so nothing is repeated and nothing is skipped.', { cursor: changes.current })}
        </p>
      )}

      {dropped > 0 && (
        <p className="fs-stm__notice" role="status">
          {t('{n} repeated event(s) were discarded on reconnecting. Nothing was applied twice.', { n: dropped })}
        </p>
      )}

      {entities.length === 0
        ? (
          <EmptyState
            icon={Database}
            title={t('Nothing has been observed yet')}
            body={enabled
              ? t('The sweep has not run, or nothing on this machine has been looked at. Look again to make it happen now.')
              : t('The mirror is switched off, so nothing has been observed. Turn it on in Settings → Agent & automation.')}
          />
        )
        : groups.map((group) => <Section key={group.situation} group={group} onOpen={(id) => setParams({ e: id }, { replace: true })} />)}

      {conflicts.length > 0 && (
        <section className="fs-stm__section" aria-label={t('Every open conflict')}>
          <header className="fs-stm__head">
            <h2 className="fs-stm__title">{t('Every open conflict')}</h2>
            <span className="fs-stm__count">{conflicts.length}</span>
          </header>
          <p className="fs-muted fs-stm__provenance">
            {t('Recorded rather than decided: when two sources disagree the mirror keeps both claims and says what would settle it, because a wrong answer chosen silently is worse than a disagreement shown plainly.')}
          </p>
          {conflicts.map((conflict) => <ConflictCard key={conflict.id} conflict={conflict} />)}
        </section>
      )}

      <Sources diagnostics={diagnostics} openConflicts={conflicts.length} />

      {selected && (
        <Detail
          entity={selected}
          history={history}
          conflicts={selectedConflicts}
          busy={busy === selected.id}
          canRefresh={enabled}
          onRefresh={() => void lookAgain(selected.id)}
          onClose={() => setParams({}, { replace: true })}
          onRebuilt={() => void load()}
        />
      )}

      {selectedId && !selected && (
        <p className="fs-stm__notice" data-tone="warning" role="status">
          {t('No entity by that id is visible to you. It may never have existed, or it may belong to somebody else; the mirror answers the same either way.')}
        </p>
      )}

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Play size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
