import {
  ArrowRight, Ban, Check, ChevronLeft, Eye, GitCompare, Play, Plus, RefreshCw,
  ScanSearch, ShieldAlert, TriangleAlert,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, Skeleton, Toast } from '../components';
import * as api from '../adapters/deltas';
import { t } from '../i18n';
import './deltas.css';

/**
 * Deltas: what changed, whether it is what was asked for, and how well we know.
 *
 * The screen exists so that what it says about a change can be BELIEVED. Three
 * rules follow from that, and they are rules of product rather than of layout:
 *
 * 1. `unknown` is never drawn as `preserved` -- not in a colour near it, not
 *    grouped with the good news, not behind a "show more". "We did not check
 *    this" is an answer and it keeps its own heading, above the greens. Every
 *    card that summarises says how many unknowns it is summarising.
 *
 * 2. Coverage and confidence are two columns and stay two columns. A structural
 *    coverage of 100% from a `perceptual` tier at `low` confidence is a real and
 *    frequent situation -- every region was looked at, by a method that cannot
 *    be certain about any of them -- and the two halves call for different next
 *    actions. Nothing here multiplies them into a single percentage.
 *
 * 3. A blocking finding is never summarised. It is drawn first and whole: its
 *    path, its before and after, the invariant it broke, the method and tier
 *    that produced it, and the evidence behind it.
 *
 * Nothing on this screen derives anything. Every grouping, ordering, count and
 * downgrade is a pure function in `adapters/deltas.ts`, driven by
 * `studio/checks/deltas.check.mjs`; a panel whose arithmetic lived inside a
 * component would be a panel nobody could check.
 *
 * The detail is a PANEL and not a dialog, unlike the State Mirror's: the
 * assertions table has seven columns and a modal would either crop it or
 * scroll it twice. It is still deep-linkable -- `?d=<id>` -- so a delta can be
 * pasted to somebody.
 */

type Tone = api.Tone;

function Tag({ tone, icon: Icon, title, children }: {
  tone: Tone;
  icon?: typeof Eye;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span className="fs-dlt__tag" data-tone={tone} title={title}>
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

/** The verdict about the change. Five words, each its own sentence. */
function assessmentWord(assessment: string): string {
  switch (assessment) {
    case 'matched': return t('matched what was asked');
    case 'partial': return t('partly matched');
    case 'mismatched': return t('did not match');
    case 'regressed': return t('regressed');
    default: return t('could not be determined');
  }
}

/** What the classification means. Never implied by a tint alone. */
function classificationWord(classification: string): string {
  switch (classification) {
    case 'requested': return t('Asked for');
    case 'required': return t('Required by the change');
    case 'incidental': return t('Changed without being asked');
    case 'regression': return t('Regression');
    case 'preserved': return t('Held still, and checked');
    default: return t('Not checked');
  }
}

/** The line under each heading: what the group is, in one sentence. */
function classificationLede(classification: string): string {
  switch (classification) {
    case 'requested': return t('Asked for, and it happened.');
    case 'required': return t('Not asked for, but the change could not be made without it.');
    case 'incidental': return t('Changed without being asked. Not necessarily wrong, and never invisible.');
    case 'regression': return t('Something that used to hold and no longer does.');
    case 'preserved': return t('Held still, and measured to be. This word costs an observation and a threshold; a row that could not pay is in "Not checked" instead.');
    default: return t('Nobody checked these. Not evidence that they are fine, and not filed with the ones that are.');
  }
}

/** What the extractor SAW. The other axis, and never merged with the first. */
function operationWord(operation: string): string {
  switch (operation) {
    case 'added': return t('added');
    case 'missing': return t('missing');
    case 'modified': return t('modified');
    case 'moved': return t('moved');
    case 'reencoded': return t('re-encoded');
    case 'unchanged': return t('unchanged');
    default: return t('not stated');
  }
}

function severityWord(severity: string): string {
  switch (severity) {
    case 'blocking': return t('blocking');
    case 'material': return t('material');
    case 'minor': return t('minor');
    case 'info': return t('info');
    default: return t('not stated');
  }
}

/** How well it is known. `unknown` says the words, never a blank. */
function confidenceWord(confidence: string): string {
  switch (confidence) {
    case 'exact': return t('exact');
    case 'high': return t('high');
    case 'medium': return t('medium');
    case 'low': return t('low');
    default: return t('not measured');
  }
}

function invariantWord(status: string): string {
  switch (status) {
    case 'preserved': return t('held');
    case 'violated': return t('violated');
    case 'not_applicable': return t('does not apply here');
    default: return t('not checked');
  }
}

/** How the finding was produced, and therefore how far it can be trusted. */
function tierWord(tier: string): string {
  switch (tier) {
    case 'hash': return t('byte identity');
    case 'parser': return t('parsed structure');
    case 'algorithm': return t('algorithm');
    case 'perceptual': return t('perceptual metric');
    case 'model': return t('a model');
    case 'human': return t('a person');
    default: return t('an unnamed method');
  }
}

function dimensionWord(dimension: string): string {
  switch (dimension) {
    case 'structural': return t('structure');
    case 'semantic': return t('meaning');
    case 'temporal': return t('time');
    case 'identity': return t('identity');
    case 'spatial': return t('space');
    case 'behavioral': return t('behaviour');
    default: return dimension;
  }
}

/** The sentences `describeCoverageGap` returns, each a key of its own. */
function gapWord(gap: string): string {
  switch (gap) {
    case 'The source could not be read, so nothing here is a comparison.':
      return t('The source could not be read, so nothing here is a comparison.');
    case 'The target could not be read, so nothing here is a comparison.':
      return t('The target could not be read, so nothing here is a comparison.');
    case 'No axis was measured, so how much of this could be compared is not known.':
      return t('No axis was measured, so how much of this could be compared is not known.');
    case 'Some axes were not measured at all. They are shown as not measured, never as zero.':
      return t('Some axes were not measured at all. They are shown as not measured, never as zero.');
    case 'At least one axis was only partly covered, so an absence of findings on it is not evidence of no change.':
      return t('At least one axis was only partly covered, so an absence of findings on it is not evidence of no change.');
    default:
      return t('Some regions were excluded from the comparison and nothing is claimed about them.');
  }
}

/** A value that is empty is drawn as an absence, never as a blank cell. */
function shown(value: string): string {
  return value ? value : t('not stated');
}

/* -- the card -------------------------------------------------------------- */

/**
 * The two ends of the comparison: a name a person recognises, and the head of
 * the digest that makes it mean anything. Both, always -- two runs against
 * `main` are two different comparisons, and a card showing only the word `main`
 * would present them as one.
 */
function Revisions({ source, target }: { source: string; target: string }) {
  return (
    <span className="fs-dlt__revisions">
      <span className="fs-dlt__rev" title={source}>{shown(source)}</span>
      <ArrowRight size={12} aria-hidden="true" className="fs-dlt__arrow" />
      <span className="fs-dlt__rev" title={target}>{shown(target)}</span>
    </span>
  );
}

/**
 * The counts, with `unknown` amongst them and never folded away.
 *
 * A card that reported only its assertions and its regressions would let a
 * reader infer that everything else had been checked and was fine. The unknown
 * count is drawn even when it is zero, because a zero somebody read is a fact
 * and an absent number is a guess.
 */
function Counts({ counts }: { counts: api.SummaryCounts }) {
  return (
    <span className="fs-dlt__counts">
      <span className="fs-dlt__count">
        {t('{n} finding(s)', { n: counts.assertions })}
      </span>
      <span className="fs-dlt__count" data-tone={counts.regressions > 0 ? 'bad' : 'quiet'}>
        {t('{n} regression(s)', { n: counts.regressions })}
      </span>
      <span className="fs-dlt__count" data-tone={counts.material > 0 ? 'bad' : 'quiet'}>
        {t('{n} you must read', { n: counts.material })}
      </span>
      <span className="fs-dlt__count" data-tone={counts.unknowns > 0 ? 'warn' : 'quiet'}>
        {t('{n} not checked', { n: counts.unknowns })}
      </span>
      <span className="fs-dlt__count" data-tone={counts.invariantsViolated > 0 ? 'bad' : 'quiet'}>
        {t('{n} invariant(s) broken', { n: counts.invariantsViolated })}
      </span>
    </span>
  );
}

/** One comparison, as a card. Tinted by its verdict, labelled by its words. */
function Card({ row, onOpen }: { row: api.Summary; onOpen: (id: string) => void }) {
  const tone = api.assessmentTone(row.assessment);
  return (
    <button
      type="button"
      className="fs-dlt__card"
      data-tone={tone}
      onClick={() => onOpen(row.id)}
      data-testid={`delta-${row.id}`}
    >
      <span className="fs-dlt__card-top">
        <Tag tone={tone}>{assessmentWord(row.assessment)}</Tag>
        <span className="fs-dlt__domain">{row.domain}</span>
        <span className="fs-spacer" />
        <span className="fs-dlt__muted">{row.createdAt}</span>
      </span>
      <Revisions source={row.sourceLabel} target={row.targetLabel} />
      <Counts counts={row.counts} />
      <span className="fs-dlt__card-foot">
        <Tag tone={api.severityTone(row.severity)}>
          {t('worst: {severity}', { severity: severityWord(row.severity) })}
        </Tag>
        {/* Two tags and never one number: the severity says how bad the worst
            finding is, the confidence how much to believe the measurements.
            A single score would hide whichever of the two was the good half. */}
        <Tag tone={api.confidenceTone(row.confidence)}>
          {t('confidence: {confidence}', { confidence: confidenceWord(row.confidence) })}
        </Tag>
        {row.projectId ? <span className="fs-dlt__muted">{row.projectId}</span> : null}
      </span>
    </button>
  );
}

/* -- the detail's blocks --------------------------------------------------- */

/** Evidence is a POINTER, never the thing itself. Listed so a claim can be checked. */
function Evidence({ refs }: { refs: api.EvidenceRef[] }) {
  if (!refs.length) {
    return <p className="fs-dlt__muted">{t('Nothing was recorded as evidence for this.')}</p>;
  }
  return (
    <ul className="fs-dlt__evidence">
      {refs.map((ref, index) => (
        <li key={`${ref.kind}-${ref.ref}-${index}`}>
          <span className="fs-dlt__kind">{ref.kind}</span>
          <span className="fs-dlt__mono">{ref.ref}</span>
          {ref.detail ? <span className="fs-dlt__muted">{ref.detail}</span> : null}
          {ref.hash ? <span className="fs-dlt__mono fs-dlt__muted">{api.shortHash(ref.hash)}</span> : null}
        </li>
      ))}
    </ul>
  );
}

/**
 * One invariant result, whole.
 *
 * `unknown` is drawn with the same weight as the rest and the word `not
 * checked` on it. It is never tinted towards `held`, and `orderedInvariants`
 * has already put it above them rather than amongst them.
 */
function Invariant({ result }: { result: api.InvariantResult }) {
  const tone = api.invariantTone(result.status);
  return (
    <div className="fs-dlt__invariant" data-status={result.status}>
      <span className="fs-dlt__card-top">
        <span className="fs-dlt__mono fs-dlt__grow">{result.invariantId}</span>
        <Tag tone={tone}>{invariantWord(result.status)}</Tag>
        <Tag tone={api.severityTone(result.severity)}>{severityWord(result.severity)}</Tag>
        <Tag tone={api.confidenceTone(result.confidence)}>{confidenceWord(result.confidence)}</Tag>
      </span>
      <span className="fs-dlt__muted">
        {t('by {method} ({tier})', {
          method: shown(result.method), tier: tierWord(result.tier),
        })}
      </span>
      {result.observations.length > 0 && (
        <ul className="fs-dlt__observations">
          {result.observations.map((line, index) => <li key={index}>{line}</li>)}
        </ul>
      )}
      {Object.keys(result.threshold).length > 0 && (
        <span className="fs-dlt__muted fs-dlt__mono">
          {t('threshold: {threshold}', { threshold: JSON.stringify(result.threshold) })}
        </span>
      )}
      {result.status === 'unknown' && (
        <span className="fs-dlt__muted">
          {t('Nothing measured this, so nothing is claimed about it. It is not evidence that the property held.')}
        </span>
      )}
      {result.limitations.map((line, index) => (
        <span className="fs-dlt__muted" key={index}>{line}</span>
      ))}
      <Evidence refs={result.evidenceRefs} />
    </div>
  );
}

/**
 * A finding a reader must have SEEN, drawn whole and drawn first.
 *
 * Everything that would let the page shrink this is deliberately absent: no
 * truncation, no "show more", no count standing in for the row. The invariant
 * it broke, the method that produced it and the evidence behind it are all
 * here, because a blocking claim a reader cannot check is one they will either
 * over-trust or ignore.
 */
function Blocking({ delta, assertion, evidence }: {
  delta: api.Delta;
  assertion: api.Assertion;
  evidence: api.EvidenceRef[];
}) {
  const invariants = api.invariantsFor(delta, assertion);
  return (
    <div className="fs-dlt__blocking">
      <span className="fs-dlt__card-top">
        <ShieldAlert size={14} aria-hidden="true" />
        <span className="fs-dlt__mono fs-dlt__grow">{assertion.path}</span>
        <Tag tone="bad">{classificationWord(api.effectiveClassification(assertion))}</Tag>
        <Tag tone={api.severityTone(assertion.severity)}>{severityWord(assertion.severity)}</Tag>
        <Tag tone={api.confidenceTone(assertion.confidence)}>{confidenceWord(assertion.confidence)}</Tag>
      </span>
      <span className="fs-dlt__beforeafter">
        <span className="fs-dlt__was">{shown(assertion.before)}</span>
        <ArrowRight size={12} aria-hidden="true" className="fs-dlt__arrow" />
        <span className="fs-dlt__now">{shown(assertion.after)}</span>
        <span className="fs-dlt__muted">{operationWord(assertion.operation)}</span>
      </span>
      <span className="fs-dlt__muted">
        {t('by {method} ({tier})', { method: shown(assertion.method), tier: tierWord(assertion.tier) })}
      </span>
      {assertion.detail ? <span>{assertion.detail}</span> : null}
      {invariants.length > 0 && (
        <div className="fs-dlt__stack">
          <p className="fs-dlt__label">{t('The invariant it broke')}</p>
          {invariants.map((result) => <Invariant key={result.invariantId} result={result} />)}
        </div>
      )}
      <div className="fs-dlt__stack">
        <p className="fs-dlt__label">{t('Evidence')}</p>
        <Evidence refs={api.evidenceFor(assertion, evidence)} />
      </div>
    </div>
  );
}

/**
 * The assertions of one classification, as a table.
 *
 * Seven columns, and the last three are the point: severity, confidence and
 * the method with its tier. A table that stopped at "before / after" would be
 * a diff, and a diff is exactly the thing this subsystem exists not to be.
 *
 * The table scrolls inside its own box rather than widening the page: a screen
 * that scrolls horizontally as a whole loses the headings a reader is using to
 * tell these rows apart.
 */
function Assertions({ rows, onReclassify }: {
  rows: api.Assertion[];
  onReclassify: (assertion: api.Assertion) => void;
}) {
  return (
    <div className="fs-dlt__scroll">
      <table className="fs-dlt__table">
        <thead>
          <tr>
            <th scope="col">{t('Path')}</th>
            <th scope="col">{t('Seen')}</th>
            <th scope="col">{t('Before')}</th>
            <th scope="col">{t('After')}</th>
            <th scope="col">{t('Severity')}</th>
            <th scope="col">{t('Confidence')}</th>
            <th scope="col">{t('How')}</th>
            <th scope="col"><span className="fs-dlt__sr">{t('Actions')}</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            /* The id, then the path, then the position: the contract always
               mints an id, and a row that arrived without one must still be
               drawn as its own row rather than collapsing into its neighbour. */
            <tr key={row.id || `${row.path}-${index}`} data-severity={row.severity}>
              <td className="fs-dlt__mono">{shown(row.path)}</td>
              <td>{operationWord(row.operation)}</td>
              <td className="fs-dlt__mono fs-dlt__was">{shown(row.before)}</td>
              <td className="fs-dlt__mono fs-dlt__now">{shown(row.after)}</td>
              <td><Tag tone={api.severityTone(row.severity)}>{severityWord(row.severity)}</Tag></td>
              {/* Its own column, beside coverage and never multiplied into it. */}
              <td>
                <Tag
                  tone={api.confidenceTone(row.confidence)}
                  title={api.cappedByTier(row.confidence, row.tier)
                    ? t('It could not have claimed more: {tier} is capped here.', { tier: tierWord(row.tier) })
                    : undefined}
                >
                  {confidenceWord(row.confidence)}
                </Tag>
              </td>
              <td>
                <span className="fs-dlt__how">{shown(row.method)}</span>
                <span className="fs-dlt__muted">{tierWord(row.tier)}</span>
              </td>
              <td>
                <Button
                  variant="ghost" size="sm" label={t('Disagree')}
                  testId={`reclassify-${row.id}`}
                  title={t('Record that this was classified wrongly, and why.')}
                  onClick={() => onReclassify(row)}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Coverage: how much could be compared, on each axis, and never as a score.
 *
 * An axis nobody measured is drawn as the words "not measured" with no bar at
 * all. An empty bar would be a zero, and "we did not look" and "we looked and
 * found none of it" are opposite findings that lead to opposite next actions.
 *
 * The confidence lives in the assertions table and in the card, deliberately
 * not here: a percentage in this block times a word in that one is a number
 * nobody measured, and it is the number a reader would most like to be given.
 */
function CoverageBlock({ coverage }: { coverage: api.Coverage }) {
  const rows = api.coverageRows(coverage);
  const gaps = api.describeCoverageGap(coverage);
  return (
    <section className="fs-dlt__block" aria-label={t('How much could be compared')}>
      <h3 className="fs-dlt__title">{t('How much could be compared')}</h3>
      <p className="fs-dlt__muted">
        {t('This is coverage, not confidence. Full coverage by a method that cannot be certain is a real situation, and so is a certain answer about one small region; the two are never combined into one number here.')}
      </p>
      <div className="fs-dlt__ends">
        <Tag tone={coverage.sourceReadable ? 'good' : 'bad'}>
          {coverage.sourceReadable ? t('the source was readable') : t('the source could not be read')}
        </Tag>
        <Tag tone={coverage.targetReadable ? 'good' : 'bad'}>
          {coverage.targetReadable ? t('the target was readable') : t('the target could not be read')}
        </Tag>
      </div>
      <ul className="fs-dlt__coverage">
        {rows.map((row) => (
          <li key={row.dimension} data-measured={row.ratio === null ? 'no' : 'yes'}>
            <span className="fs-dlt__dimension">{dimensionWord(row.dimension)}</span>
            {row.ratio === null
              ? <span className="fs-dlt__unmeasured">{t('not measured')}</span>
              : (
                <>
                  <span className="fs-dlt__bar" aria-hidden="true">
                    <span className="fs-dlt__bar-fill" style={{ inlineSize: `${Math.round(row.ratio * 100)}%` }} />
                  </span>
                  <span className="fs-dlt__ratio">{t('{n}%', { n: Math.round(row.ratio * 100) })}</span>
                </>
              )}
          </li>
        ))}
      </ul>
      {gaps.length > 0 && (
        <ul className="fs-dlt__gaps">
          {gaps.map((gap) => <li key={gap}>{gapWord(gap)}</li>)}
        </ul>
      )}
      {coverage.excluded.length > 0 && (
        <p className="fs-dlt__muted">
          {t('Excluded: {what}', { what: coverage.excluded.join(', ') })}
        </p>
      )}
      {coverage.regionsAnalyzed.length > 0 && (
        <p className="fs-dlt__muted">
          {t('Regions looked at: {what}', { what: coverage.regionsAnalyzed.join(', ') })}
        </p>
      )}
      {coverage.notes.map((note, index) => <p className="fs-dlt__muted" key={index}>{note}</p>)}
    </section>
  );
}

/**
 * One comparison, in full.
 *
 * The order of the blocks IS the argument the page is making: the verdict and
 * the sentence behind it, then anything blocking (whole, never a count), then
 * the invariants -- violated, then unchecked, then held -- then the findings
 * grouped by what they mean, then how much could be compared at all, then what
 * the comparison could not do, and last the provenance a reader needs to
 * reproduce it.
 */
function Detail({ detail, evidence, busy, enabled, onBack, onRun, onInvalidate, onReclassify }: {
  detail: api.DeltaDetail;
  evidence: api.EvidenceRef[];
  busy: string;
  enabled: boolean;
  onBack: () => void;
  onRun: () => void;
  onInvalidate: () => void;
  onReclassify: (assertion: api.Assertion) => void;
}) {
  const delta = detail.delta;
  const tone = api.assessmentTone(delta.assessment);
  const groups = api.groupByClassification(delta.assertions);
  const invariants = api.orderedInvariants(delta.invariants);
  const blocking = api.blockingFindings(delta);
  const c = api.counts(delta);
  const unverified = api.unverifiedPreserved(delta);
  const versions = Object.entries(delta.extractorVersions);

  return (
    <div className="fs-dlt__detail" data-testid="delta-detail">
      <header className="fs-dlt__detail-head">
        <div className="fs-inline">
          <Button variant="ghost" size="sm" icon={ChevronLeft} label={t('All comparisons')} onClick={onBack} />
          <span className="fs-spacer" />
          <Button
            variant="secondary" size="sm" icon={RefreshCw} label={t('Compare again')}
            loading={busy === 'run'} disabled={!enabled}
            title={enabled
              ? t('Run this comparison again against the same two revisions.')
              : t('New comparisons are switched off, so nothing may be recomputed. What is below stays readable.')}
            onClick={onRun}
          />
          <Button
            variant="danger" size="sm" icon={Ban} label={t('Mark as superseded')}
            loading={busy === 'invalidate'}
            title={t('Say that this no longer describes its two revisions. It is kept, not deleted.')}
            onClick={onInvalidate}
          />
        </div>
        <div className="fs-dlt__verdict">
          <Tag tone={tone}>{assessmentWord(delta.assessment)}</Tag>
          <span className="fs-dlt__domain">{delta.domain}</span>
          {detail.cached && (
            <Tag tone="unknown" title={t('Nothing was recomputed: the source, the target, the intent and the extractor versions are all the ones this was measured against.')}>
              {t('served from cache')}
            </Tag>
          )}
        </div>
        <Revisions source={api.revisionLabel(delta.source)} target={api.revisionLabel(delta.target)} />
        <p className="fs-prose">
          {detail.explanation || t('The engine did not explain this one in words. The findings below are the whole of what it said.')}
        </p>
        <Counts counts={api.summarize(delta).counts} />
      </header>

      {unverified.length > 0 && (
        <p className="fs-dlt__notice" data-tone="warning" role="status">
          {t('{n} finding(s) arrived claiming to have been preserved with nothing measured behind the claim. They are shown under "Not checked", because not detected is not preserved.', { n: unverified.length })}
        </p>
      )}

      {(blocking.assertions.length > 0 || blocking.invariants.length > 0) && (
        <section className="fs-dlt__block" data-tone="bad" aria-label={t('You must read these')}>
          <h3 className="fs-dlt__title">{t('You must read these')}</h3>
          <p className="fs-dlt__muted">
            {t('Shown whole and shown first. Nothing here is summarised, collapsed or counted: a finding that stops a caller is a finding they have to have seen.')}
          </p>
          {blocking.assertions.map((assertion) => (
            <Blocking key={assertion.id} delta={delta} assertion={assertion} evidence={evidence} />
          ))}
          {blocking.invariants
            .filter((result) => !blocking.assertions.some((a) => a.invariantRefs.indexOf(result.invariantId) >= 0))
            .map((result) => <Invariant key={result.invariantId} result={result} />)}
        </section>
      )}

      <section className="fs-dlt__block" aria-label={t('What had to hold')}>
        <h3 className="fs-dlt__title">{t('What had to hold')}</h3>
        <p className="fs-dlt__muted">
          {t('Broken first, then the ones nobody could check, then the ones that held. An unchecked property is never listed amongst the ones that held: "we did not look" is not the same answer as "it is fine".')}
        </p>
        {invariants.length === 0
          ? <p className="fs-dlt__muted">{t('No invariant was declared for this comparison, so none was checked.')}</p>
          : (
            <>
              <p className="fs-dlt__muted">
                {t('{v} broken · {u} not checked · {p} held · {n} do not apply', {
                  v: c.invariantsViolated, u: c.invariantsUnknown,
                  p: c.invariantsPreserved, n: c.invariantsNotApplicable,
                })}
              </p>
              {invariants.map((result) => <Invariant key={result.invariantId} result={result} />)}
            </>
          )}
      </section>

      <section className="fs-dlt__block" aria-label={t('Everything that was found')}>
        <h3 className="fs-dlt__title">{t('Everything that was found')}</h3>
        <p className="fs-dlt__muted">
          {t('What was SEEN and what it MEANS are two columns, and they disagree often. A change that was asked for and a change that broke something look identical to an extractor; only the frozen intent tells them apart.')}
        </p>
        {api.CLASSIFICATION_ORDER.map((name) => (
          <div className="fs-dlt__group" key={name} data-classification={name}>
            <header className="fs-dlt__group-head">
              <h4 className="fs-dlt__group-title">{classificationWord(name)}</h4>
              <span className="fs-dlt__count">{groups[name].length}</span>
            </header>
            <p className="fs-dlt__muted">{classificationLede(name)}</p>
            {groups[name].length === 0
              ? <p className="fs-dlt__muted">{t('None.')}</p>
              : <Assertions rows={groups[name]} onReclassify={onReclassify} />}
          </div>
        ))}
      </section>

      <CoverageBlock coverage={delta.coverage} />

      <section className="fs-dlt__block" aria-label={t('What this comparison could not do')}>
        <h3 className="fs-dlt__title">{t('What this comparison could not do')}</h3>
        {delta.limitations.length === 0
          ? <p className="fs-dlt__muted">{t('None were recorded. That is not the same as there being none.')}</p>
          : <ul className="fs-dlt__gaps">{delta.limitations.map((line, index) => <li key={index}>{line}</li>)}</ul>}
      </section>

      <footer className="fs-dlt__foot">
        <span className="fs-dlt__mono">{delta.id}</span>
        <span className="fs-dlt__muted">
          {t('took {ms} ms', { ms: delta.elapsedMs })}
        </span>
        {delta.proofRef ? (
          <span className="fs-dlt__mono" title={t('The run this comparison was recorded against.')}>{delta.proofRef}</span>
        ) : (
          <span className="fs-dlt__muted">{t('no proof was attached')}</span>
        )}
        {delta.intentContractId ? <span className="fs-dlt__mono">{delta.intentContractId}</span> : null}
        <span className="fs-dlt__muted">
          {versions.length
            ? t('extractors: {what}', { what: versions.map(([name, v]) => `${name} ${String(v)}`).join(', ') })
            : t('no extractor version was recorded, so this cannot be reproduced exactly')}
        </span>
      </footer>
    </div>
  );
}

/* -- the panels around the list -------------------------------------------- */

/**
 * What this build can actually compare, and why it cannot compare the rest.
 *
 * The reason is the whole point. A domain listed as merely absent reads as "not
 * built yet"; the same domain listed with `ImportError: no module named av`
 * reads as one `pip install` away, and only one of those two sentences lets
 * anybody do anything.
 */
function Extractors({ rows }: { rows: api.ExtractorStatus[] }) {
  if (!rows.length) return null;
  return (
    <section className="fs-dlt__block" aria-label={t('What can be compared here')}>
      <h3 className="fs-dlt__title">{t('What can be compared here')}</h3>
      <div className="fs-dlt__extractors">
        {rows.map((row) => (
          <div className="fs-dlt__extractor" key={row.domain} data-available={row.available ? 'yes' : 'no'}>
            <span className="fs-dlt__card-top">
              <b className="fs-dlt__grow">{row.domain}</b>
              <Tag tone={row.available ? 'good' : 'bad'}>
                {row.available ? t('available') : t('not available')}
              </Tag>
            </span>
            <span className="fs-dlt__muted">
              {row.version ? t('version {v}', { v: row.version }) : t('no version reported')}
            </span>
            {row.module ? <span className="fs-dlt__muted fs-dlt__mono">{row.module}</span> : null}
            {!row.available && (
              <span className="fs-dlt__muted">
                {row.reason || t('No reason was given, which is itself worth chasing.')}
              </span>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

interface Draft {
  domain: string;
  sourceKind: string;
  sourceRef: string;
  sourceHash: string;
  sourceLabel: string;
  targetKind: string;
  targetRef: string;
  targetHash: string;
  targetLabel: string;
  intentText: string;
  projectId: string;
  run: boolean;
}

const EMPTY: Draft = {
  domain: 'image',
  sourceKind: 'checkpoint', sourceRef: '', sourceHash: '', sourceLabel: '',
  targetKind: 'checkpoint', targetRef: '', targetHash: '', targetLabel: '',
  intentText: '', projectId: '', run: true,
};

/**
 * Ask for a comparison, without curl.
 *
 * Both hashes are required by the form because they are required by the
 * contract: a comparison against a moving `latest` produces a result that was
 * true for nobody, and the caller cannot tell afterwards which bytes it saw.
 *
 * "Read my intent back" compiles the sentence WITHOUT running anything, so the
 * fragments the compiler could not turn into a checkable condition can be read
 * before the contract is frozen against them. A contract with unknowns still
 * runs; what it cannot do is come back `matched`.
 */
function NewComparison({ domains, kinds, busy, onClose, onSubmit, onCompile, unknowns }: {
  domains: string[];
  kinds: string[];
  busy: string;
  onClose: () => void;
  onSubmit: (draft: Draft) => void;
  onCompile: (draft: Draft) => void;
  unknowns: string[] | null;
}) {
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const set = (patch: Partial<Draft>): void => setDraft((current) => ({ ...current, ...patch }));
  const ready = Boolean(draft.sourceRef && draft.sourceHash && draft.targetRef && draft.targetHash);

  const end = (which: 'source' | 'target') => (
    <div className="fs-dlt__end">
      <p className="fs-dlt__label">{which === 'source' ? t('Before') : t('After')}</p>
      <label className="fs-dlt__row">
        <span>{t('Kind')}</span>
        <select
          className="fs-field"
          value={which === 'source' ? draft.sourceKind : draft.targetKind}
          onChange={(event) => set(which === 'source'
            ? { sourceKind: event.target.value } : { targetKind: event.target.value })}
        >
          {kinds.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
        </select>
      </label>
      <label className="fs-dlt__row">
        <span>{t('Reference')}</span>
        <input
          className="fs-field" type="text"
          placeholder={t('how its owner names it')}
          value={which === 'source' ? draft.sourceRef : draft.targetRef}
          onChange={(event) => set(which === 'source'
            ? { sourceRef: event.target.value } : { targetRef: event.target.value })}
        />
      </label>
      <label className="fs-dlt__row">
        <span>{t('Digest')}</span>
        <input
          className="fs-field fs-dlt__mono" type="text"
          placeholder={t('the sha256 that makes it immutable')}
          value={which === 'source' ? draft.sourceHash : draft.targetHash}
          onChange={(event) => set(which === 'source'
            ? { sourceHash: event.target.value } : { targetHash: event.target.value })}
        />
      </label>
      <label className="fs-dlt__row">
        <span>{t('Label')}</span>
        <input
          className="fs-field" type="text"
          placeholder={t('what a person would call it')}
          value={which === 'source' ? draft.sourceLabel : draft.targetLabel}
          onChange={(event) => set(which === 'source'
            ? { sourceLabel: event.target.value } : { targetLabel: event.target.value })}
        />
      </label>
    </div>
  );

  return (
    <Dialog
      open
      onOpenChange={(isOpen) => !isOpen && onClose()}
      title={t('Compare two revisions')}
      testId="delta-new"
      footer={(
        <>
          <Button
            variant="secondary" size="sm" icon={ScanSearch} label={t('Read my intent back')}
            loading={busy === 'compile'} disabled={!draft.intentText}
            title={t('Compile the sentence into checkable conditions without running anything, and show what could not be compiled.')}
            onClick={() => onCompile(draft)}
          />
          <Button
            variant="primary" size="sm" icon={Play} label={t('Compare')}
            loading={busy === 'create'} disabled={!ready}
            title={ready
              ? t('Freeze the intent and compare.')
              : t('Both revisions need a reference and a digest: a comparison against a moving target was true for nobody.')}
            onClick={() => onSubmit(draft)}
          />
        </>
      )}
    >
      <div className="fs-dlt__form">
        <label className="fs-dlt__row">
          <span>{t('Domain')}</span>
          <select className="fs-field" value={draft.domain} onChange={(event) => set({ domain: event.target.value })}>
            {domains.map((domain) => <option key={domain} value={domain}>{domain}</option>)}
          </select>
        </label>
        <div className="fs-dlt__ends">{end('source')}{end('target')}</div>
        <label className="fs-dlt__row">
          <span>{t('What you asked for')}</span>
          <textarea
            className="fs-field fs-dlt__textarea" rows={3}
            placeholder={t('e.g. make the jacket red and leave the face alone')}
            value={draft.intentText}
            onChange={(event) => set({ intentText: event.target.value })}
          />
        </label>
        <label className="fs-dlt__row">
          <span>{t('Project')}</span>
          <input
            className="fs-field" type="text" value={draft.projectId}
            onChange={(event) => set({ projectId: event.target.value })}
          />
        </label>
        <label className="fs-switch">
          <input type="checkbox" checked={draft.run} onChange={(event) => set({ run: event.target.checked })} />
          <span>{t('Run it now, rather than only recording the request')}</span>
        </label>
        {unknowns !== null && (
          unknowns.length === 0
            ? (
              <p className="fs-dlt__notice" role="status">
                {t('Every part of that turned into a condition something can be checked against.')}
              </p>
            )
            : (
              <div className="fs-dlt__notice" data-tone="warning" role="status">
                <p>
                  {t('These parts could not be turned into anything checkable. They are kept on the record and not silently dropped, and a comparison carrying them cannot come back as a full match:')}
                </p>
                <ul className="fs-dlt__gaps">
                  {unknowns.map((line, index) => <li key={index}>{line}</li>)}
                </ul>
              </div>
            )
        )}
      </div>
    </Dialog>
  );
}

/**
 * A person disagreeing with the classifier, on the record.
 *
 * The reason is required, and not out of ceremony: this edits the only record
 * of what was decided about a change, and an unexplained edit to it is
 * indistinguishable afterwards from the classifier having said so itself. The
 * original is kept beside it.
 */
function Reclassify({ assertion, classifications, busy, onClose, onSubmit }: {
  assertion: api.Assertion;
  classifications: string[];
  busy: boolean;
  onClose: () => void;
  onSubmit: (classification: string, reason: string) => void;
}) {
  const [classification, setClassification] = useState(api.effectiveClassification(assertion));
  const [reason, setReason] = useState('');
  return (
    <Dialog
      open
      onOpenChange={(isOpen) => !isOpen && onClose()}
      title={t('Say what this really was')}
      testId="delta-reclassify"
      footer={(
        <Button
          variant="primary" size="sm" label={t('Record it')}
          loading={busy} disabled={!reason.trim()}
          title={reason.trim()
            ? t('Keep both the original classification and yours.')
            : t('A reason is required: this edits the only record of what was decided.')}
          onClick={() => onSubmit(classification, reason.trim())}
        />
      )}
    >
      <div className="fs-dlt__form">
        <p className="fs-dlt__mono">{assertion.path}</p>
        <p className="fs-dlt__muted">
          {t('Classified {what} by {method}. Your reading is recorded beside it, never instead of it.', {
            what: classificationWord(api.effectiveClassification(assertion)),
            method: shown(assertion.method),
          })}
        </p>
        <label className="fs-dlt__row">
          <span>{t('It was really')}</span>
          <select className="fs-field" value={classification} onChange={(event) => setClassification(event.target.value)}>
            {classifications.map((name) => (
              <option key={name} value={name}>{classificationWord(name)}</option>
            ))}
          </select>
        </label>
        <label className="fs-dlt__row">
          <span>{t('Because')}</span>
          <textarea
            className="fs-field fs-dlt__textarea" rows={3} value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      </div>
    </Dialog>
  );
}

/* -- the screen ------------------------------------------------------------ */

export function DeltasScreen() {
  const [params, setParams] = useSearchParams();
  const openId = params.get('d') ?? '';
  const domain = params.get('domain') ?? '';
  const assessment = params.get('assessment') ?? '';
  const projectId = params.get('project') ?? '';

  const [page, setPage] = useState<api.DeltaPage | null>(null);
  const [config, setConfig] = useState<api.Config | null>(null);
  const [extractors, setExtractors] = useState<api.ExtractorStatus[]>([]);
  const [detail, setDetail] = useState<api.DeltaDetail | null>(null);
  const [evidence, setEvidence] = useState<api.EvidenceRef[]>([]);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [busy, setBusy] = useState('');
  const [formOpen, setFormOpen] = useState(false);
  const [unknowns, setUnknowns] = useState<string[] | null>(null);
  const [reclassifying, setReclassifying] = useState<api.Assertion | null>(null);
  const cursor = useRef('');

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    window.setTimeout(() => setNotice(null), 5000);
  }, []);

  const fail = useCallback((error: unknown) => {
    const refusal = error as api.DeltaRefusal;
    say(refusal?.code ? `${refusal.code}: ${refusal.message}` : (error as Error).message, 'warn');
  }, [say]);

  const loadList = useCallback(async (signal?: AbortSignal) => {
    try {
      const answer = await api.loadDeltas({ domain, assessment, projectId, limit: 100 }, signal);
      setPage(answer);
    } catch (error) {
      if (signal?.aborted) return;
      // An empty list and a failed read are different facts, so the list is
      // left at whatever it held and the reason is said out loud rather than
      // presented as "no comparisons".
      setPage((current) => current ?? { enabled: false, deltas: [], nextCursor: '' });
      fail(error);
    }
  }, [domain, assessment, projectId, fail]);

  useEffect(() => {
    const controller = new AbortController();
    void loadList(controller.signal);
    return () => controller.abort();
  }, [loadList]);

  useEffect(() => {
    const controller = new AbortController();
    api.loadConfig(controller.signal).then(setConfig).catch(() => setConfig(null));
    api.loadExtractors(controller.signal).then(setExtractors).catch(() => setExtractors([]));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!openId) {
      setDetail(null);
      setEvidence([]);
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    setEvidence([]);
    api.loadDelta(openId, controller.signal).then(setDetail).catch((error) => {
      if (controller.signal.aborted) return;
      fail(error);
    });
    // Evidence is a second read on purpose: it is the expensive half, it is
    // owner-scoped on the server, and a delta whose evidence could not be
    // fetched is still worth reading. A failure here empties the list rather
    // than emptying the delta.
    api.loadEvidence(openId, controller.signal).then(setEvidence).catch(() => setEvidence([]));
    return () => controller.abort();
  }, [openId, fail]);

  /**
   * Follow the engine live, and reload rather than trust a frame's payload.
   *
   * An event says something moved; the store says what it now is, and only one
   * of those two can be behind. The reload is debounced so a burst of frames
   * from one comparison costs one read.
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
      close = api.followDeltas(
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

  const compile = useCallback(async (draft: Draft) => {
    setBusy('compile');
    setUnknowns(null);
    try {
      const answer = await api.compileIntent({
        domain: draft.domain, text: draft.intentText, projectId: draft.projectId || undefined,
      });
      setUnknowns(answer.unknowns);
    } catch (error) {
      fail(error);
    } finally {
      setBusy('');
    }
  }, [fail]);

  const create = useCallback(async (draft: Draft) => {
    setBusy('create');
    try {
      const answer = await api.createDelta({
        domain: draft.domain,
        source: {
          kind: draft.sourceKind, ref: draft.sourceRef,
          hash: draft.sourceHash.trim(), label: draft.sourceLabel || undefined,
        },
        target: {
          kind: draft.targetKind, ref: draft.targetRef,
          hash: draft.targetHash.trim(), label: draft.targetLabel || undefined,
        },
        intentText: draft.intentText || undefined,
        projectId: draft.projectId || undefined,
        run: draft.run,
      });
      setFormOpen(false);
      setUnknowns(null);
      say(answer.delta
        ? t('Compared.')
        : t('The request was recorded. Nothing has been compared yet.'));
      await loadList();
      if (answer.delta) open(answer.delta.id);
    } catch (error) {
      fail(error);
    } finally {
      setBusy('');
    }
  }, [fail, loadList, open, say]);

  const run = useCallback(async () => {
    if (!detail) return;
    setBusy('run');
    try {
      const fresh = await api.runDelta(detail.delta.id);
      setDetail((current) => (current ? { ...current, delta: fresh, cached: false } : current));
      say(t('Compared again.'));
      await loadList();
    } catch (error) {
      fail(error);
    } finally {
      setBusy('');
    }
  }, [detail, fail, loadList, say]);

  const supersede = useCallback(async () => {
    if (!detail) return;
    setBusy('invalidate');
    try {
      const superseded = await api.invalidate(detail.delta.id);
      say(superseded
        ? t('Marked as no longer describing those two revisions. It is kept, not deleted.')
        : t('Nothing was superseded.'));
      await loadList();
      back();
    } catch (error) {
      fail(error);
    } finally {
      setBusy('');
    }
  }, [detail, fail, loadList, say, back]);

  const record = useCallback(async (classification: string, reason: string) => {
    if (!detail || !reclassifying) return;
    setBusy('reclassify');
    try {
      const fresh = await api.reclassify(detail.delta.id, reclassifying.id, classification, reason);
      setDetail((current) => (current ? { ...current, delta: fresh } : current));
      setReclassifying(null);
      say(t('Recorded, beside the original.'));
    } catch (error) {
      fail(error);
    } finally {
      setBusy('');
    }
  }, [detail, reclassifying, fail, say]);

  const rows = useMemo(() => page?.deltas ?? [], [page]);
  const domains = config?.domains ?? api.DOMAINS;
  const assessments = config?.assessments ?? api.ASSESSMENTS;
  const classifications = config?.classifications ?? api.CLASSIFICATIONS;

  if (page === null) {
    return <Skeleton label={t('Reading the comparisons')} count={4} height="72px" />;
  }

  return (
    <div className="fs-screen fs-dlt" data-testid="deltas">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Deltas')}</h1>
          <p className="fs-prose fs-dlt__lede">
            {t('What changed between two revisions, whether it is what was asked for, and how well we know. Every finding carries how it was measured and how far that method can be trusted; what nobody checked says so, in its own place, rather than passing for what held.')}
          </p>
        </div>
        <div className="fs-inline">
          <Button
            variant="secondary" size="sm" icon={RefreshCw} label={t('Reload')}
            onClick={() => void loadList()}
          />
          <Button
            variant="primary" size="sm" icon={Plus} label={t('Compare')}
            disabled={!enabled}
            title={enabled
              ? t('Compare two revisions against what you asked for.')
              : t('New comparisons are switched off. Everything already compared is still readable below.')}
            onClick={() => { setUnknowns(null); setFormOpen(true); }}
          />
        </div>
      </header>

      {/* Off means: no NEW comparisons. Everything already stored keeps
          answering, which is literally what the server does, and saying
          otherwise would send somebody looking for data that is right here. */}
      {!enabled && (
        <p className="fs-dlt__notice" data-tone="warning" role="status">
          {t('New comparisons are switched off (Settings → Agent & automation → Delta engine). Nothing new will be measured and nothing will be recomputed. Every comparison already stored is still below, still complete and still readable.')}
        </p>
      )}

      {openId && detail === null && <Skeleton label={t('Reading the comparison')} count={3} height="64px" />}

      {openId && detail && (
        <Detail
          detail={detail}
          evidence={evidence}
          busy={busy}
          enabled={enabled}
          onBack={back}
          onRun={() => void run()}
          onInvalidate={() => void supersede()}
          onReclassify={setReclassifying}
        />
      )}

      {!openId && (
        <>
          <section className="fs-dlt__filters" aria-label={t('Narrow the list')}>
            <label className="fs-dlt__row">
              <span>{t('Domain')}</span>
              <select className="fs-field" value={domain} onChange={(event) => filter('domain', event.target.value)}>
                <option value="">{t('any')}</option>
                {domains.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            <label className="fs-dlt__row">
              <span>{t('Verdict')}</span>
              <select className="fs-field" value={assessment} onChange={(event) => filter('assessment', event.target.value)}>
                <option value="">{t('any')}</option>
                {assessments.map((name) => (
                  <option key={name} value={name}>{assessmentWord(name)}</option>
                ))}
              </select>
            </label>
            <label className="fs-dlt__row">
              <span>{t('Project')}</span>
              <input
                className="fs-field" type="text" value={projectId}
                onChange={(event) => filter('project', event.target.value)}
              />
            </label>
          </section>

          {rows.length === 0
            ? (
              <EmptyState
                icon={GitCompare}
                title={t('Nothing has been compared yet')}
                body={enabled
                  ? t('Compare two revisions to find out what changed between them and whether it is what you asked for.')
                  : t('New comparisons are switched off, and nothing was stored before they were. Turn them on in Settings → Agent & automation.')}
              />
            )
            : (
              <section className="fs-dlt__list" aria-label={t('Comparisons')}>
                {rows.map((row) => <Card key={row.id} row={row} onOpen={open} />)}
              </section>
            )}

          {page.nextCursor && (
            <p className="fs-dlt__muted">
              {t('There are older comparisons than these.')}
            </p>
          )}

          <Extractors rows={extractors} />
        </>
      )}

      {formOpen && (
        <NewComparison
          domains={domains}
          kinds={api.REVISION_KINDS}
          busy={busy}
          unknowns={unknowns}
          onClose={() => { setFormOpen(false); setUnknowns(null); }}
          onCompile={(draft) => void compile(draft)}
          onSubmit={(draft) => void create(draft)}
        />
      )}

      {reclassifying && (
        <Reclassify
          assertion={reclassifying}
          classifications={classifications}
          busy={busy === 'reclassify'}
          onClose={() => setReclassifying(null)}
          onSubmit={(classification, reason) => void record(classification, reason)}
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
