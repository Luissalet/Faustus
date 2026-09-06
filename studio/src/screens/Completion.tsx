import {
  Ban, Check, ChevronLeft, Coins, EyeOff, Hourglass, ListChecks,
  RefreshCw, TriangleAlert,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, Skeleton, Toast } from '../components';
import * as api from '../adapters/completion';
import { t } from '../i18n';
import './completion.css';

/**
 * Completion: what a turn did beyond the literal ask, what it refused, and
 * why it stopped.
 *
 * The engine's whole point is honesty about over-delivery. Three rules follow
 * from that, and they are rules of product rather than of layout:
 *
 * 1. **`converged`, `budget` and `unfinished` are three endings and never look
 *    alike.** `converged` and `core_only` are the honest stops -- the work was
 *    done. `budget` means it ran out of money. `unfinished` means the turn
 *    ended with work this mode calls for still open, and neither the budget
 *    nor the scope stopped it. Each gets its own colour, its own edge style
 *    and its own SENTENCE; the raw enum never appears on its own, because
 *    `core_only` is not English.
 *
 * 2. **Shadow and real are never mixed silently.** The list defaults to real.
 *    A shadow row carries a hatched edge and a badge that says the word, and
 *    the moment both kinds are on screen the page says so at the top -- shadow
 *    measures what the engine WOULD have done, and counting those beside what
 *    it did ruins the measurement.
 *
 * 3. **Executed, refused and deferred are three blocks.** Executed is grouped
 *    by layer, so what was asked for and what was added beyond it are never
 *    one list. Every refusal carries its reason from the closed vocabulary,
 *    and one that arrived without a reason is drawn as `nobody recorded why`
 *    rather than passing quietly for an ordinary refusal.
 *
 * Nothing on this screen derives anything. Every ending, ordering, count and
 * budget figure is a pure function in `adapters/completion.ts`, driven by
 * `studio/checks/completion.check.mjs`; a panel whose arithmetic lived inside
 * a component would be a panel nobody could check.
 *
 * The detail is a PANEL and not a dialog: a decision carries three candidate
 * lists, a five-line budget and a closeout, and a modal would either crop them
 * or scroll twice. It is deep-linkable -- `?d=<id>` -- so a decision can be
 * pasted to somebody.
 *
 * There is deliberately no button here that RUNS anything. The engine decides
 * inside a turn, at the moment the model stops calling tools; a "run" on this
 * screen would be a second way to reach a decision -- one with no turn behind
 * it, no ledger, no proof and no budget -- and its answer would look exactly
 * like the real one.
 */

type Tone = api.Tone;

function Tag({ tone, shadow, icon: Icon, title, children }: {
  tone: Tone;
  shadow?: boolean;
  icon?: typeof EyeOff;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span className="fs-cmp__tag" data-tone={tone} data-shadow={shadow ? 'yes' : undefined} title={title}>
      {Icon ? <Icon size={11} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

/* -- the words --------------------------------------------------------------
   Written out here as literal `t()` calls rather than through the adapter's
   label functions, because `scripts/i18n_es.py` reads the call sites: a key
   assembled from a variable is a key the Spanish check cannot know is missing.
   The vocabularies themselves still come from the adapter. */

/** What KIND of ending this was. Four words, and no two of them overlap. */
function endingWord(ending: api.Ending): string {
  switch (ending) {
    case 'finished': return t('finished');
    case 'budget': return t('ran out of budget');
    case 'unfinished': return t('left work open');
    case 'contested': return t('the record contradicts itself');
    default: return t('interrupted');
  }
}

/** What each ending asks of the reader. A next action, never a mood. */
function endingLede(ending: api.Ending): string {
  switch (ending) {
    case 'finished': return t('The work was done and nothing left cleared the bar.');
    case 'budget': return t('The work was worth doing and the money ran out. Raise the budget.');
    case 'unfinished': return t('The turn ended with work this mode calls for still open, and neither the budget nor the scope stopped it. This is the ending shadow mode exists to count; it is worth finding out what stopped the turn.');
    case 'contested': return t('This record says it finished and its own budget says a line ran out. Both cannot be true, so neither is drawn as the answer here.');
    default: return t('Something outside the engine ended this run.');
  }
}

/** How it stopped, in a sentence. The raw enum never stands alone on screen. */
function stopWord(stopReason: string): string {
  switch (stopReason) {
    case 'converged': return t('finished: nothing left was worth doing');
    case 'core_only': return t('finished the ask, and this mode goes no further');
    case 'budget': return t('ran out of budget');
    case 'unfinished': return t('ended with work still open');
    case 'scope': return t('the next useful thing was out of scope');
    case 'risk': return t('a blocking risk stopped it');
    case 'blocked': return t('blocked on something outside this run');
    case 'user': return t('a person stopped it');
    case 'cancelled': return t('the run was cancelled');
    case 'failed': return t('the core did not complete');
    default: return t('ended for a reason this build cannot name');
  }
}

/** What a layer IS. The two beyond the ask say so in their own words. */
function layerWord(layer: string): string {
  switch (layer) {
    case 'core': return t('What was asked');
    case 'professional': return t('What a professional would not ship without');
    case 'bonus': return t('Beyond the ask: adjacent work with a return');
    case 'exploratory': return t('Beyond the ask: ambition');
    default: return t('A layer this build cannot name');
  }
}

/** Why a candidate did not run. `unrecorded` says so out loud. */
function rejectionWord(reason: string): string {
  switch (reason) {
    case 'out_of_scope': return t('outside the envelope');
    case 'no_permission': return t('the run did not hold the permission');
    case 'forbidden_effect': return t('the effect itself is denied here');
    case 'below_threshold': return t('the value did not clear the bar');
    case 'dominated': return t('another candidate was better on every axis');
    case 'duplicate': return t('already covered by something selected');
    case 'resolved': return t('something else fixed it on the way past');
    case 'quarantined': return t('depends on a capability that is not healthy');
    case 'budget': return t('would not fit');
    case 'round_full': return t('wanted, and this round had taken its batch');
    case 'risk': return t('a blocking risk, whatever the value');
    case 'stale': return t('the evidence aged out and did not revalidate');
    case 'superseded': return t('the state it was about has moved');
    // Second person, deliberately: this is the one reason the reader supplied
    // rather than the engine, and phrasing it as a judgement would let the
    // engine take credit for a decision it was handed.
    case 'declined': return t('you said no to it, and it will not be offered again');
    default: return t('nobody recorded why');
  }
}

/** What each mode is for. Read off the server's policies, named here. */
function modeWord(mode: string): string {
  switch (mode) {
    case 'literal': return t('the ask, and nothing else');
    case 'professional': return t('the ask, and what a professional would not ship without');
    case 'greedy': return t('the ask, plus adjacent work with a real return');
    case 'maximalist': return t('the ask, plus ambition');
    default: return t('a mode this build cannot name');
  }
}

/** A unit of budget. Four, because they run out at different times. */
function unitWord(unit: string): string {
  switch (unit) {
    case 'rounds': return t('rounds');
    case 'tool_calls': return t('tool calls');
    case 'tokens': return t('tokens');
    case 'seconds': return t('seconds');
    default: return unit;
  }
}

/** How far the candidate sat from what was asked. A structural judgement. */
function relationWord(relation: string): string {
  switch (relation) {
    case 'direct': return t('named by the result');
    case 'adjacent': return t('same component');
    case 'downstream': return t('makes the result usable');
    case 'similar_case': return t('the same pattern elsewhere');
    case 'opportunistic': return t('useful, not necessary');
    case 'unrelated': return t('outside');
    default: return t('a distance this build cannot name');
  }
}

/** A value that is empty is drawn as an absence, never as a blank. */
function shown(value: string): string {
  return value ? value : t('not stated');
}

/* -- the card -------------------------------------------------------------- */

/**
 * The counts a card carries.
 *
 * `extras` is drawn even when it is zero, and it is drawn beside the executed
 * total rather than inside it. §30's rule is that work nobody asked for is
 * never folded into the core summary, and the strongest form of folding is not
 * printing the number: a reader who has never seen an extras count cannot tell
 * a run that added nothing from a run that did not look.
 */
function Counts({ row }: { row: api.Summary }) {
  const executed = api.LAYERS.reduce((total, layer) => total + (row.executed[layer] ?? 0), 0);
  return (
    <span className="fs-cmp__counts">
      <span className="fs-cmp__count">{t('{n} executed', { n: executed })}</span>
      <span className="fs-cmp__count" data-tone={row.extras > 0 ? 'warn' : 'quiet'}>
        {t('{n} beyond the ask', { n: row.extras })}
      </span>
      <span className="fs-cmp__count">{t('{n} refused', { n: row.rejected })}</span>
      <span className="fs-cmp__count">{t('{n} deferred', { n: row.deferred })}</span>
      <span className="fs-cmp__count" data-tone={row.degraded.length > 0 ? 'bad' : 'quiet'}>
        {t('{n} system(s) not consulted', { n: row.degraded.length })}
      </span>
    </span>
  );
}

/** One decision, as a card. Tinted AND shaped by its ending, labelled by its
 *  words, and marked as shadow whenever it is one. */
function Card({ row, onOpen }: { row: api.Summary; onOpen: (id: string) => void }) {
  const ending = api.endingForStop(row.stopReason);
  const tone = api.endingTone(ending);
  return (
    <button
      type="button"
      className="fs-cmp__card"
      data-ending={ending}
      data-shadow={row.shadow ? 'yes' : undefined}
      onClick={() => onOpen(row.id)}
      data-testid={`decision-${row.id}`}
    >
      <span className="fs-cmp__card-top">
        <Tag tone={tone}>{endingWord(ending)}</Tag>
        {row.shadow && (
          <Tag tone="unknown" shadow icon={EyeOff} title={t('Shadow: what the engine WOULD have done. Nothing here ran.')}>
            {t('shadow')}
          </Tag>
        )}
        <span className="fs-cmp__mode">{row.mode}</span>
        <span className="fs-spacer" />
        <span className="fs-cmp__muted">{row.createdAt}</span>
      </span>
      <span className="fs-cmp__muted">{stopWord(row.stopReason)}</span>
      <Counts row={row} />
      <span className="fs-cmp__card-foot">
        {/* The server sent a boolean and the screen recomputed it from the stop
            reason. When the two disagree, something wrote that row without
            going through the contract, and that is worth a reader's attention
            for its own sake rather than a silent correction. */}
        {api.disputedHonesty(row) && (
          <Tag tone="bad" icon={TriangleAlert} title={t('This row says one thing about how it ended and its stop reason says another.')}>
            {t('the record disagrees with itself')}
          </Tag>
        )}
        {row.projectId ? <span className="fs-cmp__muted">{row.projectId}</span> : null}
        {row.runId ? <span className="fs-cmp__muted fs-cmp__mono fs-cmp__grow">{row.runId}</span> : null}
      </span>
    </button>
  );
}

/* -- the detail's blocks --------------------------------------------------- */

/**
 * One candidate, whole.
 *
 * The layer and the funding LINE are both printed, side by side, because they
 * answer different questions: the layer says what kind of work it was, and the
 * line says whose money it spent. They are not the same axis -- `professional`
 * work has no budget line of its own and comes out of the core pot -- and a
 * row that printed only one of them would leave the reader unable to see how
 * an extra was paid for.
 *
 * The reject affordance is here and not in a menu: refusing an improvement is
 * a thing a person does about one row, and it belongs on that row.
 */
function CandidateRow({ candidate, kind, onReject }: {
  candidate: api.Candidate;
  kind: 'executed' | 'rejected' | 'deferred';
  onReject?: (candidate: api.Candidate) => void;
}) {
  const reason = api.effectiveRejection(candidate);
  const unrecorded = kind === 'rejected' && reason === api.UNRECORDED_REASON;
  const line = api.lineForLayer(candidate.layer);
  const pot = api.potForLayer(candidate.layer);
  return (
    <div
      className="fs-cmp__candidate"
      data-layer={candidate.layer}
      data-kind={kind}
      data-unrecorded={unrecorded ? 'yes' : undefined}
      data-testid={`candidate-${candidate.id}`}
    >
      <span className="fs-cmp__card-top">
        <span className="fs-cmp__candidate-title fs-cmp__grow" title={candidate.title}>
          {shown(candidate.title)}
        </span>
        <Tag tone={api.EXTRA_LAYERS.indexOf(candidate.layer) >= 0 ? 'warn' : 'good'}>
          {layerWord(candidate.layer)}
        </Tag>
        {onReject && (
          <Button
            variant="ghost" size="sm" icon={Ban} label={t('Refuse')}
            testId={`reject-${candidate.id}`}
            title={t('Say no to this improvement, with a reason that will still mean something next month.')}
            onClick={() => onReject(candidate)}
          />
        )}
      </span>
      <span className="fs-cmp__funding">
        {/* `funded from` and not `costs`: the line is where the spend was
            BOOKED, and two lines can share one pot. */}
        {line === pot
          ? t('funded from the {line} line', { line })
          : t('funded from the {line} line, which spends the {pot} pot', { line, pot })}
      </span>
      {candidate.detail ? <span className="fs-cmp__muted">{candidate.detail}</span> : null}
      <span className="fs-cmp__scores">
        <span>{t('value {n}', { n: candidate.expectedValue })}</span>
        <span>{t('cost {n}', { n: candidate.estimatedCost })}</span>
        <span>{t('risk {n}', { n: candidate.risk })}</span>
        <span>{t('distance: {what}', { what: relationWord(candidate.relation) })}</span>
        <span>{t('undo: {what}', { what: candidate.reversibility })}</span>
      </span>
      {kind !== 'executed' && (
        <Tag tone={unrecorded ? 'warn' : 'unknown'} icon={unrecorded ? TriangleAlert : undefined}>
          {kind === 'rejected'
            ? t('refused: {why}', { why: rejectionWord(reason) })
            : t('deferred: {why}', { why: rejectionWord(reason) })}
        </Tag>
      )}
      {unrecorded && (
        <span className="fs-cmp__muted">
          {t('The record carries no reason from the closed vocabulary. A refusal nobody justified cannot be told, a month later, from one nobody meant, and the engine refuses to build a row like this -- so this one did not come through the contract.')}
        </span>
      )}
      {candidate.evidenceRefs.length > 0 && (
        <ul className="fs-cmp__refs">
          {candidate.evidenceRefs.map((ref, index) => (
            <li key={`${ref}-${index}`} className="fs-cmp__mono">{ref}</li>
          ))}
        </ul>
      )}
      {/* What it would have NEEDED. There is no control on this screen that
          could grant one: a candidate arguing its way past a missing
          permission with a high value is the shape §5.4 exists to prevent. */}
      {candidate.requiredPermissions.length > 0 && (
        <span className="fs-cmp__muted">
          {t('would need: {what}', { what: candidate.requiredPermissions.join(', ') })}
        </span>
      )}
      {candidate.verification.length > 0 && (
        <span className="fs-cmp__muted">
          {t('checked by: {what}', { what: candidate.verification.join(', ') })}
        </span>
      )}
      {candidate.evidenceRefs.length === 0 && api.REQUIRED_LAYERS.indexOf(candidate.layer) >= 0 && (
        <span className="fs-cmp__muted">
          {t('Nothing was recorded as evidence for this, on a layer that can block the close.')}
        </span>
      )}
    </div>
  );
}

/**
 * The budget: what was SPENT, beside the ceiling it was spent against.
 *
 * Never a bare remainder. A remainder is only meaningful next to the limit it
 * came from, and two numbers that must be read together end up read apart; a
 * stored one also goes wrong the moment two writers disagree about the total.
 *
 * Five lines and three pots, and the table says which is which. A reader who
 * saw `core` and `recovery` with identical numbers and no note would
 * reasonably conclude the run had two of them.
 */
function BudgetBlock({ budget }: { budget: api.Budget }) {
  const rows = api.budgetRows(budget);
  const dry = api.exhaustedLines(budget);
  return (
    <section className="fs-cmp__block" aria-label={t('What it spent')}>
      <h3 className="fs-cmp__title">{t('What it spent')}</h3>
      <p className="fs-cmp__muted">
        {t('Spends, not remainders, each beside the ceiling it was spent against. Five lines and three pots: `recovery` spends core money because repairing a regression IS core work, and `exploration` spends the bonus share. `verification` is the one line nothing else may borrow from, in every mode.')}
      </p>
      <div className="fs-cmp__scroll">
        <table className="fs-cmp__table">
          <thead>
            <tr>
              <th scope="col">{t('Line')}</th>
              <th scope="col">{t('Pot')}</th>
              {api.SPENDABLE_UNITS.map((unit) => (
                <th scope="col" key={unit}>{unitWord(unit)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.line}
                data-exhausted={row.exhausted.length > 0 ? 'yes' : undefined}
                data-alias={row.alias ? 'yes' : undefined}
              >
                <td>{row.line}</td>
                <td className="fs-cmp__mono">
                  {row.alias ? t('{pot} (a view of it)', { pot: row.fundedBy }) : row.fundedBy}
                </td>
                {row.units.map((cell) => (
                  <td key={cell.unit}>
                    {/* A unit the turn declared no total for is NOT zero: it is
                        not being counted, so nothing can run out of it. Drawing
                        `0 / 0` would report every uncounted unit as spent. */}
                    {cell.counted
                      ? t('{used} of {ceiling}', { used: cell.used, ceiling: cell.ceiling })
                      : <span className="fs-cmp__uncounted">{t('not counted')}</span>}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {dry.length > 0 && (
        <p className="fs-cmp__notice" data-tone="warning" role="status">
          {t('Ran out on: {what}. A run that ran out did not converge, and calling it convergence is what stops anyone from raising the budget.', { what: dry.join(', ') })}
        </p>
      )}
    </section>
  );
}

/** One list of candidates under its own heading, or the word `None.` -- never
 *  an absent heading, which a reader reads as a zero anyway. */
function Group({ title, lede, layer, rows, kind, onReject }: {
  title: string;
  lede: string;
  layer?: string;
  rows: api.Candidate[];
  kind: 'executed' | 'rejected' | 'deferred';
  onReject?: (candidate: api.Candidate) => void;
}) {
  return (
    <div className="fs-cmp__group" data-layer={layer}>
      <header className="fs-cmp__group-head">
        <h4 className="fs-cmp__group-title">{title}</h4>
        <span className="fs-cmp__count">{rows.length}</span>
      </header>
      <p className="fs-cmp__muted">{lede}</p>
      {rows.length === 0
        ? <p className="fs-cmp__muted">{t('None.')}</p>
        : rows.map((candidate) => (
          <CandidateRow
            key={candidate.id || candidate.title}
            candidate={candidate}
            kind={kind}
            onReject={onReject}
          />
        ))}
    </div>
  );
}

/**
 * How this run ended, drawn first and drawn whole.
 *
 * The three endings that matter get three treatments and three sentences, and
 * the sentence is a next action rather than a mood. Nothing here summarises
 * it into a badge: a reader who skims past a `budget` stop never raises the
 * budget, and one who skims past an `unfinished` one never finds out why the
 * turn stopped.
 */
function EndingBlock({ decision }: { decision: api.Decision }) {
  const ending = api.endingOf(decision);
  const dry = api.exhaustedLines(decision.budget);
  return (
    <div className="fs-cmp__ending" data-ending={ending} data-testid="decision-ending">
      <span className="fs-cmp__card-top">
        <Tag tone={api.endingTone(ending)} icon={ending === 'budget' ? Coins : ending === 'unfinished' ? Hourglass : Check}>
          {endingWord(ending)}
        </Tag>
        <span className="fs-cmp__mono">{decision.stopReason}</span>
      </span>
      <p className="fs-prose">{endingLede(ending)}</p>
      <span className="fs-cmp__muted">{stopWord(decision.stopReason)}</span>
      {decision.stopDetail ? <span className="fs-cmp__muted">{decision.stopDetail}</span> : null}
      {ending === 'contested' && (
        <p className="fs-cmp__notice" data-tone="bad" role="status">
          {t('It says `{stop}` and the {what} budget line(s) have nothing left. The engine refuses to record that combination, so this row did not come through it; neither half is drawn as the answer here.', {
            stop: decision.stopReason, what: dry.join(', '),
          })}
        </p>
      )}
    </div>
  );
}

/**
 * One decision, in full.
 *
 * The order of the blocks IS the argument the page is making: how it ended
 * (because that is the question), then the closeout the server wrote, then
 * what ran -- grouped by layer, so extras never hide inside the core -- then
 * what was refused and what was deferred, then what it spent, then what was
 * not consulted, and last the provenance a reader needs to find the turn.
 */
function Detail({ detail, busy, onBack, onReject }: {
  detail: api.DecisionDetail;
  busy: string;
  onBack: () => void;
  onReject: (candidate: api.Candidate) => void;
}) {
  const decision = detail.decision;
  const c = api.counts(decision);
  const unexplained = api.unexplainedRefusals(decision);
  const proof = api.proofStatus(decision);

  return (
    <div className="fs-cmp__detail" data-testid="decision-detail">
      <header className="fs-cmp__detail-head">
        <div className="fs-inline">
          <Button variant="ghost" size="sm" icon={ChevronLeft} label={t('All decisions')} onClick={onBack} />
          <span className="fs-spacer" />
          <span className="fs-cmp__mono">{decision.mode}</span>
          {decision.policyVersion ? <span className="fs-cmp__muted">{decision.policyVersion}</span> : null}
        </div>
        {/* Shadow is said at the TOP of the detail and not only on the card:
            a decision opened from a deep link arrives without the card that
            would have marked it, and nothing below is true of anything that
            actually happened. */}
        {decision.shadow && (
          <p className="fs-cmp__notice" data-tone="warning" role="status">
            {t('This is a SHADOW decision: what the engine would have done if it had been switched on. Nothing below ran, nothing was spent and nothing was refused to anybody. It exists to be counted, not to be acted on.')}
          </p>
        )}
        <EndingBlock decision={decision} />
        <span className="fs-cmp__counts">
          <span className="fs-cmp__count">{t('{n} executed', { n: c.executed })}</span>
          <span className="fs-cmp__count" data-tone={c.extras > 0 ? 'warn' : 'quiet'}>
            {t('{n} beyond the ask', { n: c.extras })}
          </span>
          <span className="fs-cmp__count">{t('{n} refused', { n: c.rejected })}</span>
          <span className="fs-cmp__count">{t('{n} deferred', { n: c.deferred })}</span>
          <span className="fs-cmp__count" data-tone={c.unexplained > 0 ? 'warn' : 'quiet'}>
            {t('{n} refused with no reason recorded', { n: c.unexplained })}
          </span>
        </span>
      </header>

      {unexplained.length > 0 && (
        <p className="fs-cmp__notice" data-tone="warning" role="status">
          {t('{n} refusal(s) arrived with no reason from the closed vocabulary. They are shown under `nobody recorded why`, because a refusal nobody justified is indistinguishable next month from one nobody meant.', { n: unexplained.length })}
        </p>
      )}

      <section className="fs-cmp__block" aria-label={t('The closeout')}>
        <h3 className="fs-cmp__title">{t('The closeout')}</h3>
        <p className="fs-cmp__muted">
          {t('One line per question a reader has, written by the server and printed here unchanged. The Extras line is unconditional: a reader who has never seen one cannot tell a run that added nothing from a run that did not look.')}
        </p>
        {detail.closeout
          ? <pre className="fs-cmp__closeout">{detail.closeout}</pre>
          : <p className="fs-cmp__muted">{t('The server wrote no closeout for this one.')}</p>}
      </section>

      <section className="fs-cmp__block" aria-label={t('What it did')}>
        <h3 className="fs-cmp__title">{t('What it did')}</h3>
        <p className="fs-cmp__muted">
          {t('Grouped by layer, always, and never as one list with a column. The first two layers are the request; the last two are work nobody asked for, and the whole reason the layers exist is that a reader can tell which they got.')}
        </p>
        {api.LAYERS.map((layer) => (
          <Group
            key={layer}
            layer={layer}
            title={layerWord(layer)}
            lede={api.REQUIRED_LAYERS.indexOf(layer) >= 0
              ? t('Part of the request. A failure here is a request that was not met.')
              : t('Beyond the request. A failure here is a bonus that failed, and the two are never reported the same way.')}
            rows={api.byLayer(decision, layer)}
            kind="executed"
          />
        ))}
      </section>

      <section className="fs-cmp__block" aria-label={t('What it refused')}>
        <h3 className="fs-cmp__title">{t('What it refused')}</h3>
        <Group
          title={t('Refused')}
          lede={t('Each with the reason it did not run. The vocabulary is closed, because a rejection nobody wrote down comes back next round and is rejected again, forever.')}
          rows={api.orderedCandidates(decision.rejected)}
          kind="rejected"
          onReject={onReject}
        />
        <Group
          title={t('Deferred')}
          lede={t('Worth doing, and not now. These are handed on to be reconsidered rather than thrown away, so filing one as a refusal would tell the next reader that good work was judged not worth doing.')}
          rows={api.orderedCandidates(decision.deferred)}
          kind="deferred"
          onReject={onReject}
        />
      </section>

      <BudgetBlock budget={decision.budget} />

      <section className="fs-cmp__block" aria-label={t('What stood behind it')}>
        <h3 className="fs-cmp__title">{t('What stood behind it')}</h3>
        {/* `referenced` is not `proved`. A run whose proof refs are non-empty
            has pointed at a proof somebody else holds; the verdict, when there
            is one, is in the closeout above. Upgrading the first into the
            second here would be the screen inventing the one word this whole
            engine is careful about. */}
        <p className="fs-cmp__muted">
          {proof === 'referenced'
            ? t('Proof: not established here; the run points at {n} proof reference(s). The verdict, if one was computed, is on the Proof line of the closeout above.', { n: decision.proofRefs.length })
            : t('Proof: not established — nothing recorded a verdict for this run, and it does not point at one somebody else holds either.')}
        </p>
        {decision.proofRefs.length > 0 && (
          <ul className="fs-cmp__refs">
            {decision.proofRefs.map((ref) => <li key={ref} className="fs-cmp__mono">{ref}</li>)}
          </ul>
        )}
        {decision.deltaRefs.length > 0 && (
          <p className="fs-cmp__muted fs-cmp__mono">
            {t('deltas: {what}', { what: decision.deltaRefs.join(', ') })}
          </p>
        )}
        {decision.changesetRefs.length > 0 && (
          <p className="fs-cmp__muted fs-cmp__mono">
            {t('changesets: {what}', { what: decision.changesetRefs.join(', ') })}
          </p>
        )}
        {/* A frontier computed without the Delta Engine could not see scope
            creep. Reporting that as a clean stop would be the same lie as a
            `preserved` with no observation behind it. */}
        {decision.degradedIntegrations.length > 0 ? (
          <p className="fs-cmp__notice" data-tone="warning" role="status">
            {t('Not consulted: {what}. This close was written without them and does not claim what they would have seen.', {
              what: decision.degradedIntegrations.join(', '),
            })}
          </p>
        ) : (
          <p className="fs-cmp__muted">{t('Every system it would have consulted was up.')}</p>
        )}
      </section>

      <footer className="fs-cmp__foot">
        <span className="fs-cmp__mono">{decision.id}</span>
        {decision.contractId ? <span className="fs-cmp__mono">{decision.contractId}</span> : null}
        {decision.scopeEnvelopeId ? <span className="fs-cmp__mono">{decision.scopeEnvelopeId}</span> : null}
        {decision.runId ? <span className="fs-cmp__mono">{decision.runId}</span> : null}
        <span className="fs-cmp__muted">{decision.createdAt}</span>
        <span className="fs-cmp__muted">
          {decision.completedLayers.length
            ? t('layers opened: {what}', { what: decision.completedLayers.join(', ') })
            : t('no layer was recorded as completed')}
        </span>
      </footer>
    </div>
  );
}

/* -- the panels around the list -------------------------------------------- */

/**
 * The four modes and what each one's policy actually says.
 *
 * `bonus_budget_share` is printed with its denominator spelled out, because
 * the number spent weeks in this repository with none: it is a share OF THE
 * TURN'S TOTAL, not of whatever happens to be left when core finishes. A run
 * that overspent on core getting a bigger bonus allowance is exactly backwards,
 * and that is the reading a bare percentage invites.
 */
function Modes({ modes }: { modes: api.Modes | null }) {
  if (!modes) return null;
  return (
    <section className="fs-cmp__block" aria-label={t('The four modes')}>
      <h3 className="fs-cmp__title">{t('The four modes')}</h3>
      <p className="fs-cmp__muted">
        {t('How far each mode goes past the literal ask, and what it will spend on going there. Every mode requires verification: depth is negotiable, evidence is not.')}
      </p>
      <div className="fs-cmp__modes">
        {modes.modes.map((mode) => {
          const policy = modes.policies[mode];
          return (
            <div className="fs-cmp__mode-card" key={mode} data-active={policy?.exploreFrontier ? 'yes' : undefined}>
              <span className="fs-cmp__card-top">
                <b className="fs-cmp__grow">{mode}</b>
                {policy?.requiresVerification && <Tag tone="good">{t('verified')}</Tag>}
              </span>
              <span className="fs-cmp__muted">{modeWord(mode)}</span>
              {policy ? (
                <>
                  <span className="fs-cmp__muted">{policy.description}</span>
                  <span className="fs-cmp__muted">
                    {t('may spend {pct}% of the turn on work beyond the ask', {
                      pct: Math.round(policy.bonusBudgetShare * 100),
                    })}
                  </span>
                  <span className="fs-cmp__muted">
                    {policy.exploreFrontier
                      ? t('looks for work beyond the ask')
                      : t('does not look beyond the ask')}
                  </span>
                  <span className="fs-cmp__muted fs-cmp__mono">{policy.policyVersion}</span>
                </>
              ) : (
                <span className="fs-cmp__muted">{t('This build sent no policy for it.')}</span>
              )}
            </div>
          );
        })}
      </div>
      <p className="fs-cmp__muted">
        {t('Layers, from obligatory to optional: {what}', { what: modes.layers.join(' → ') })}
      </p>
    </section>
  );
}

/**
 * A person saying no to one improvement, on the record.
 *
 * The reason is required, and not out of ceremony: §1.8's rule is that a
 * rejected opportunity must not reappear without new evidence, and a refusal
 * nobody justified is indistinguishable next month from one nobody meant. The
 * route is `require_human` -- a model that could reject its own improvements
 * could also quietly delete the record of having been told to do them.
 */
function Refuse({ candidate, busy, onClose, onSubmit }: {
  candidate: api.Candidate;
  busy: boolean;
  onClose: () => void;
  onSubmit: (reason: string) => void;
}) {
  const [reason, setReason] = useState('');
  return (
    <Dialog
      open
      onOpenChange={(isOpen) => !isOpen && onClose()}
      title={t('Say no to this improvement')}
      testId="completion-refuse"
      footer={(
        <Button
          variant="primary" size="sm" label={t('Record the refusal')}
          loading={busy} disabled={!reason.trim()}
          title={reason.trim()
            ? t('Keep the refusal and the reason for it, beside the candidate the engine proposed.')
            : t('A reason is required: a refusal nobody justified cannot be told, a month later, from one nobody meant.')}
          onClick={() => onSubmit(reason.trim())}
        />
      )}
    >
      <div className="fs-cmp__form">
        <p className="fs-cmp__candidate-title">{shown(candidate.title)}</p>
        <p className="fs-cmp__muted">
          {t('On the {layer} layer, funded from the {line} line. Refusing it is never destructive: the candidate keeps its evidence and its score, and what it should lose is only the chance to come back without new evidence.', {
            layer: candidate.layer, line: api.lineForLayer(candidate.layer),
          })}
        </p>
        {/* Said before they type, not after: a person spending a sentence on a
            reason deserves to know where that sentence is going to end up, and
            what it will and will not change. */}
        <p className="fs-cmp__notice" role="status">
          {t('Your reason is stored against the improvement itself, not against this decision — so this record keeps showing that it was offered here and turned down, and future runs stop proposing it. If anything fails to store, the screen says so instead of claiming otherwise.')}
        </p>
        {candidate.detail ? <p className="fs-cmp__muted">{candidate.detail}</p> : null}
        <label className="fs-cmp__row">
          <span>{t('Because')}</span>
          <textarea
            className="fs-field fs-cmp__textarea" rows={3} value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      </div>
    </Dialog>
  );
}

/* -- the screen ------------------------------------------------------------ */

export function CompletionScreen() {
  const [params, setParams] = useSearchParams();
  const openId = params.get('d') ?? '';
  /* Three states and not a checkbox. `false` is the default because mixing
     shadow with real ruins the measurement shadow mode exists to produce, and
     a two-state control could not offer the third at all. */
  const shadow = params.get('shadow') ?? 'false';
  const mode = params.get('mode') ?? '';
  const stopReason = params.get('stop') ?? '';
  const projectId = params.get('project') ?? '';

  const [page, setPage] = useState<api.DecisionPage | null>(null);
  const [config, setConfig] = useState<api.Config | null>(null);
  const [modes, setModes] = useState<api.Modes | null>(null);
  const [settings, setSettings] = useState<api.Settings | null>(null);
  const [detail, setDetail] = useState<api.DecisionDetail | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [busy, setBusy] = useState('');
  const [refusing, setRefusing] = useState<api.Candidate | null>(null);
  const cursor = useRef(0);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    window.setTimeout(() => setNotice(null), 5000);
  }, []);

  const fail = useCallback((error: unknown) => {
    const refusal = error as api.CompletionRefusal;
    say(refusal?.code ? `${refusal.code}: ${refusal.message}` : (error as Error).message, 'warn');
  }, [say]);

  const loadList = useCallback(async (signal?: AbortSignal) => {
    try {
      const answer = await api.loadDecisions(
        { shadow, mode, stopReason, projectId, limit: 100 }, signal,
      );
      setPage(answer);
    } catch (error) {
      if (signal?.aborted) return;
      // An empty list and a failed read are different facts, so the list is
      // left at whatever it held and the reason is said out loud rather than
      // presented as "nothing has been decided".
      setPage((current) => current ?? {
        enabled: false, shadowEnabled: false, decisions: [], nextCursor: '',
      });
      fail(error);
    }
  }, [shadow, mode, stopReason, projectId, fail]);

  useEffect(() => {
    const controller = new AbortController();
    void loadList(controller.signal);
    return () => controller.abort();
  }, [loadList]);

  useEffect(() => {
    const controller = new AbortController();
    api.loadConfig(controller.signal).then(setConfig).catch(() => setConfig(null));
    api.loadModes(controller.signal).then(setModes).catch(() => setModes(null));
    // The settings are the two switches and the two numbers, read live. A
    // failure here empties the bar rather than emptying the page: knowing what
    // was decided does not depend on knowing what is switched on now.
    api.loadSettings(controller.signal).then(setSettings).catch(() => setSettings(null));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!openId) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    api.loadDecision(openId, controller.signal).then(setDetail).catch((error) => {
      if (controller.signal.aborted) return;
      fail(error);
    });
    return () => controller.abort();
  }, [openId, fail]);

  /**
   * Follow the engine live, and reload rather than trust a frame's payload.
   *
   * An event says something moved; the store says what it now is, and only one
   * of those two can be behind. The reload is debounced so a burst of frames
   * from one turn costs one read.
   */
  useEffect(() => {
    let stopped = false;
    let close: (() => void) | null = null;
    let pending: number | null = null;

    const soon = (): void => {
      if (pending !== null) window.clearTimeout(pending);
      pending = window.setTimeout(() => {
        pending = null;
        if (!stopped) void loadList();
      }, 300);
    };

    const open = (): void => {
      close = api.followCompletion(
        cursor.current,
        (event) => {
          cursor.current = api.advanceCursor(cursor.current, [event]);
          soon();
        },
        () => { /* one dead stream is not a dead page; the poll below carries on */ },
        () => { if (!stopped) open(); },
      );
    };
    open();

    // The floor under the stream: with the stream up it finds nothing and
    // costs one query, and with it down this is the only thing keeping the
    // page from quietly going out of date.
    const tick = window.setInterval(() => { void loadList(); }, 20000);
    return () => {
      stopped = true;
      if (pending !== null) window.clearTimeout(pending);
      window.clearInterval(tick);
      close?.();
    };
  }, [loadList]);

  const enabled = page?.enabled ?? config?.enabled ?? false;
  const shadowEnabled = page?.shadowEnabled ?? config?.shadowEnabled ?? false;

  const open = useCallback((id: string) => {
    const next = new URLSearchParams(params);
    next.set('d', id);
    setParams(next, { replace: false });
  }, [params, setParams]);

  const back = useCallback(() => {
    const next = new URLSearchParams(params);
    next.delete('d');
    setParams(next, { replace: false });
  }, [params, setParams]);

  const filter = useCallback((key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete('d');
    setParams(next, { replace: true });
  }, [params, setParams]);

  const refuse = useCallback(async (reason: string) => {
    if (!detail || !refusing) return;
    setBusy('refuse');
    try {
      const answer = await api.rejectImprovement(detail.decision.id, refusing.id, reason);
      setRefusing(null);
      // Re-read rather than patching the row in place, and read `stored` rather
      // than the row's status. The refusal is deliberately NOT written into the
      // decision: the decision records what one turn concluded, and a refusal
      // has to outlive it, so it lands in its own table keyed by the
      // candidate's structural key and is enforced next time at admission. So
      // the improvement still appears here, exactly as it was offered -- that
      // is the point, the record of having been offered and turned down is the
      // interesting one a month later -- and the thing to check is whether the
      // server says it stored anything. It answered `ok: true` while storing
      // nothing for a few hours; `stored` is the flag that made that visible
      // and it is the one this screen believes.
      const fresh = await api.loadDecision(detail.decision.id);
      setDetail(fresh);
      if (answer.stored) {
        say(t('Refused, with your reason on the record: {what}', { what: answer.recorded || reason }));
      } else {
        say(t('The server accepted the refusal and echoed your reason back, but stored nothing. It can come back next round.'), 'warn');
      }
      await loadList();
    } catch (error) {
      fail(error);
    } finally {
      setBusy('');
    }
  }, [detail, refusing, fail, say, loadList]);

  const rows = useMemo(() => page?.decisions ?? [], [page]);
  const mixed = api.mixesShadow(rows);
  const disputed = useMemo(() => rows.filter(api.disputedHonesty), [rows]);
  const availableModes = config?.modes ?? api.COMPLETION_MODES;
  const stopReasons = config?.stopReasons ?? api.STOP_REASONS;

  if (page === null) {
    return <Skeleton label={t('Reading the decisions')} count={4} height="72px" />;
  }

  return (
    <div className="fs-screen fs-cmp" data-testid="completion">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Completion')}</h1>
          <p className="fs-prose fs-cmp__lede">
            {t('What each turn did beyond the literal ask, what it refused and why, and how it ended. A run that ran out of budget, a run that had nothing left worth doing and a run that stopped with work still open are three different endings here, and they never wear each other’s colours.')}
          </p>
        </div>
        <div className="fs-inline">
          <Button
            variant="secondary" size="sm" icon={RefreshCw} label={t('Reload')}
            onClick={() => void loadList()}
          />
        </div>
      </header>

      {/* Off means: no NEW decisions. Everything already recorded keeps
          answering, which is literally what the server does, and saying
          otherwise would send somebody looking for data that is right here. */}
      {!enabled && (
        <p className="fs-cmp__notice" data-tone="warning" role="status">
          {t('The completion engine is switched off (Settings → Agent & automation). No new turn will be judged by it. Every decision already recorded is still below, still complete and still readable.')}
        </p>
      )}

      {settings && (
        <p className="fs-cmp__notice" role="status">
          {t('Verification reserve {reserve} of the turn · at most {rounds} bonus round(s) · shadow mode {shadow}', {
            reserve: settings.verificationReserve,
            rounds: settings.maxBonusRounds,
            shadow: settings.shadowEnabled ? t('on') : t('off'),
          })}
        </p>
      )}

      {openId && detail === null && <Skeleton label={t('Reading the decision')} count={3} height="64px" />}

      {openId && detail && (
        <Detail
          detail={detail}
          busy={busy}
          onBack={back}
          onReject={setRefusing}
        />
      )}

      {!openId && (
        <>
          <section className="fs-cmp__filters" aria-label={t('Narrow the list')}>
            <label className="fs-cmp__row">
              <span>{t('Shadow')}</span>
              {/* Three options and never a checkbox: `all` has to be reachable,
                  and a two-state control would make one of the three
                  unreachable while looking complete. */}
              <select className="fs-field" value={shadow} onChange={(event) => filter('shadow', event.target.value)}>
                <option value="false">{t('what actually ran')}</option>
                <option value="true">{t('shadow only')}</option>
                <option value="all">{t('both, marked apart')}</option>
              </select>
            </label>
            <label className="fs-cmp__row">
              <span>{t('Mode')}</span>
              <select className="fs-field" value={mode} onChange={(event) => filter('mode', event.target.value)}>
                <option value="">{t('any')}</option>
                {availableModes.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            <label className="fs-cmp__row">
              <span>{t('Ending')}</span>
              <select className="fs-field" value={stopReason} onChange={(event) => filter('stop', event.target.value)}>
                <option value="">{t('any')}</option>
                {stopReasons.map((name) => (
                  <option key={name} value={name}>{stopWord(name)}</option>
                ))}
              </select>
            </label>
            <label className="fs-cmp__row">
              <span>{t('Project')}</span>
              <input
                className="fs-field" type="text" value={projectId}
                onChange={(event) => filter('project', event.target.value)}
              />
            </label>
          </section>

          {/* Both kinds on one screen. Said at the top rather than left to the
              badges: a reader counting rows counts them before they read any
              one of them, and a shadow row counted as real is the measurement
              shadow mode exists to produce, spoiled. */}
          {mixed && (
            <p className="fs-cmp__notice" data-tone="warning" role="status">
              {t('This list holds both real decisions and shadow ones. Shadow rows are what the engine WOULD have done and nothing in them ran; every one of them is marked, and none of them belongs in a count of what happened.')}
            </p>
          )}

          {shadow === 'true' && (
            <p className="fs-cmp__notice" data-tone="warning" role="status">
              {t('Shadow only. Nothing below ran: this is the engine’s account of what it would have done, kept so that turning it on is a decision somebody can make with numbers.')}
            </p>
          )}

          {disputed.length > 0 && (
            <p className="fs-cmp__notice" data-tone="bad" role="status">
              {t('{n} row(s) claim an ending their own stop reason does not support. They are drawn by their stop reason, and they are flagged, because a row whose two fields contradict each other was written by something that did not go through the contract.', { n: disputed.length })}
            </p>
          )}

          {rows.length === 0
            ? (
              <EmptyState
                icon={ListChecks}
                title={t('Nothing has been decided yet')}
                body={enabled
                  ? t('The engine decides inside a turn, at the moment the model stops calling tools. Run one, and what it did beyond the ask will be here.')
                  : t('The completion engine is switched off, and nothing was recorded before it was. Turn it on in Settings → Agent & automation.')}
              />
            )
            : (
              <section className="fs-cmp__list" aria-label={t('Decisions')}>
                {rows.map((row) => <Card key={row.id} row={row} onOpen={open} />)}
              </section>
            )}

          {page.nextCursor && (
            <p className="fs-cmp__muted">{t('There are older decisions than these.')}</p>
          )}

          {!shadowEnabled && (
            <p className="fs-cmp__muted">
              {t('Shadow mode is off, so nothing new is being measured about what the engine would have done.')}
            </p>
          )}

          <Modes modes={modes} />
        </>
      )}

      {refusing && (
        <Refuse
          candidate={refusing}
          busy={busy === 'refuse'}
          onClose={() => setRefusing(null)}
          onSubmit={(reason) => void refuse(reason)}
        />
      )}

      {notice && (
        <Toast>
          {notice.tone === 'warn'
            ? <TriangleAlert size={12} aria-hidden="true" />
            : <Check size={12} aria-hidden="true" />}
          {' '}
          {notice.text}
        </Toast>
      )}
    </div>
  );
}
