import { AlertTriangle, Check, CheckSquare, GraduationCap, Plus, Search, SlidersHorizontal, Trash2, X, Zap } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, Popover, Skeleton, Toast } from '../components';
import {
  auditStatus,
  cancelAudit,
  deleteSkill,
  duplicateGroups,
  listSkills,
  loadSkillPrefs,
  matchesSkill,
  necessityKind,
  needsAttention,
  setSkillPref,
  setSkillStatus,
  sortSkills,
  startAudit,
  type AuditState,
  type DuplicateInfo,
  type Skill,
  type SkillPrefs,
  type SkillSort,
} from '../adapters/skills';
import { NewSkillPane, SkillDetail, confidenceTone, type Tab } from './skills/Detail';
import './projects.css';
import './skills.css';
import { t, tn } from '../i18n';

/**
 * Skills (the previous interface's Skills tab inside Brain, `/skills`).
 *
 * The job here is triage: which of the procedures the assistant learned
 * (or the teacher wrote) deserve to be trusted. So the page is a list
 * with the evidence per row — status, confidence, uses, audit — and a
 * pane with everything about the chosen one: overview, SKILL.md to read
 * and edit, and the test run with its approvals and verdict. "Needs
 * attention" is a filter, not a hidden rule inside a delete button.
 */

type Filter = 'all' | 'drafts' | 'published' | 'attention';
const SORTS: { value: SkillSort; label: string }[] = [
  { value: 'confidence', label: 'Confidence' },
  { value: 'uses', label: 'Most used' },
  { value: 'alpha', label: 'A–Z' },
  { value: 'recent', label: 'Recent' },
];
const CONF_MAX = [95, 90, 85, 80, 75, 70];

/** Why a row needs attention. `soft` is the quiet kind: nothing wrong yet, just untested. */
function attentionLabel(s: Skill, dup: DuplicateInfo | undefined, threshold: number): { text: string; soft: boolean } | null {
  const kind = necessityKind(s, dup);
  if (kind === 'duplicate') return { text: dup ? t('duplicate #{n}', { n: dup.group }) : t('duplicate'), soft: false };
  if (kind === 'trivial') return { text: t('generic'), soft: false };
  if (kind === 'irrelevant') return { text: t('possibly irrelevant'), soft: false };
  if (s.auditVerdict === 'fail') return { text: t('failed'), soft: false };
  if (s.auditVerdict === 'needs_work') return { text: t('needs work'), soft: false };
  if (s.confidence < Math.round(threshold * 100)) return { text: t('below {n}%', { n: Math.round(threshold * 100) }), soft: false };
  if (s.auditVerdict !== 'pass') return { text: t('not audited'), soft: true };
  return null;
}

/* ── One row of the list ── */

function SkillRow({ skill, dup, threshold, on, selecting, selected, onOpen, onToggle, auditing }: { skill: Skill; dup: DuplicateInfo | undefined; threshold: number; on: boolean; selecting: boolean; selected: boolean; onOpen: () => void; onToggle: () => void; auditing: boolean }) {
  const flag = attentionLabel(skill, dup, threshold);
  return (
    <div className="fs-sk__row-wrap" data-on={on || undefined} data-auditing={auditing || undefined}>
      {selecting && <input type="checkbox" className="fs-sk__check" checked={selected} onChange={onToggle} aria-label={t('Select {name}', { name: skill.name })} />}
      <button type="button" className="fs-sk__item" onClick={onOpen} aria-current={on || undefined} data-testid="skill-row">
        <span className="fs-sk__item-main">
          <code className="fs-sk__name">{skill.name}</code>
          {skill.description && <span className="fs-sk__item-desc">{skill.description}</span>}
          <span className="fs-sk__signals">
            <span className="fs-sk__status" data-status={skill.status}>
              {skill.status === 'published' ? t('Published') : skill.status === 'draft' ? t('Draft') : skill.status}
            </span>
            {skill.uses > 0 && <span>{tn(skill.uses, '{n} use', '{n} uses')}</span>}
            {skill.source === 'teacher-escalation' && (
              <span className="fs-sk__inline" title={t('Written by the teacher')}>
                <GraduationCap size={12} aria-hidden="true" />
              </span>
            )}
            {flag && (
              <span className="fs-sk__flag" data-soft={flag.soft || undefined}>
                {!flag.soft && <AlertTriangle size={11} aria-hidden="true" />}
                {flag.text}
              </span>
            )}
          </span>
        </span>
        <span className="fs-sk__conf" data-tone={confidenceTone(skill.confidence)} title={skill.auditVerdict === 'pass' ? t('Passed an automated test') : undefined}>
          {skill.auditVerdict === 'pass' && <Check size={12} aria-hidden="true" />}
          {skill.confidence}%
        </span>
      </button>
    </div>
  );
}

/* ── Learning preferences: the five switches of the old settings tab ── */

function LearningPopover({ prefs, onChange }: { prefs: SkillPrefs; onChange: (key: keyof SkillPrefs, value: boolean | number) => void }) {
  const pct = Math.round(prefs.minConfidence * 100);
  return (
    <Popover trigger={<Button variant="ghost" size="sm" icon={SlidersHorizontal} label={t('Learning')} testId="skills-learning" />} align="end" className="fs-sk__prefs">
      <label className="fs-switch">
        <input type="checkbox" checked={prefs.autoExtract} onChange={(e) => onChange('autoExtract', e.target.checked)} />
        <span>
          {t('Draft skills from my workflows on its own')}
          <small>{t('The library can grow; nothing is retired without review.')}</small>
        </span>
      </label>
      <label className="fs-switch">
        <input type="checkbox" checked={prefs.autoApprove} onChange={(e) => onChange('autoApprove', e.target.checked)} />
        <span>
          {t('Audit publishes the ones that pass')}
          <small>{t('Off: audit results stay as drafts until you approve them.')}</small>
        </span>
      </label>
      <label className="fs-sk__pref-range">
        <span>
          {t('Minimum confidence')} <output>≥ {pct}%</output>
        </span>
        <input type="range" min={50} max={100} step={5} value={pct} onChange={(e) => onChange('minConfidence', Number(e.target.value) / 100)} />
      </label>
      <label className="fs-sk__pref-number">
        <span>
          {t('Skills per request')}
          <small>{t('How many relevant published skills join each agent request. 0 turns injection off.')}</small>
        </span>
        <input type="number" className="fs-field" min={0} max={12} step={1} value={prefs.maxInjected} onChange={(e) => onChange('maxInjected', Math.max(0, Math.min(12, Number(e.target.value) || 0)))} />
      </label>
    </Popover>
  );
}

/* ── The screen ── */

export function SkillsScreen() {
  const [params, setParams] = useSearchParams();
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [prefs, setPrefs] = useState<SkillPrefs | null>(null);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [confMax, setConfMax] = useState<number | null>(null);
  const [sort, setSort] = useState<SkillSort>('confidence');
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<Tab>('overview');
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: 'delete'; names: string[]; nonPassing?: boolean } | { kind: 'audit'; names: string[]; scope: 'selected' | 'all' } | null>(null);
  const [skipAudited, setSkipAudited] = useState(true);
  const [audit, setAudit] = useState<AuditState | null>(null);
  const [auditOpen, setAuditOpen] = useState(false);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const reload = useCallback(async () => {
    try {
      setSkills(await listSkills());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void reload();
    void loadSkillPrefs().then(setPrefs);
    // A run may already be going from before this page opened: attach to it.
    void auditStatus().then((st) => {
      if (st.status === 'running') {
        setAudit(st);
        setAuditOpen(true);
      }
    });
  }, [reload]);

  // The audit runs on the server; poll while it does, refresh the list when it ends.
  useEffect(() => {
    if (audit?.status !== 'running') return;
    const id = window.setInterval(() => {
      void auditStatus().then((st) => {
        setAudit(st);
        if (st.status !== 'running') void reload();
      });
    }, 1500);
    return () => window.clearInterval(id);
  }, [audit?.status, reload]);

  const threshold = prefs?.minConfidence ?? 0.85;
  const dups = useMemo(() => (skills ? duplicateGroups(skills) : new Map<string, DuplicateInfo>()), [skills]);

  const visible = useMemo(() => {
    if (!skills) return [];
    const kept = skills.filter((s) => {
      if (!matchesSkill(s, query)) return false;
      if (filter === 'drafts' && s.status !== 'draft') return false;
      if (filter === 'published' && s.status !== 'published') return false;
      if (filter === 'attention' && !needsAttention(s, dups.get(s.name), threshold)) return false;
      if (confMax !== null && s.confidence > confMax) return false;
      return true;
    });
    return sortSkills(kept, sort);
  }, [skills, query, filter, confMax, sort, dups, threshold]);

  const counts = useMemo(() => {
    if (!skills) return { all: 0, drafts: 0, published: 0, attention: 0 };
    return {
      all: skills.length,
      drafts: skills.filter((s) => s.status === 'draft').length,
      published: skills.filter((s) => s.status === 'published').length,
      attention: skills.filter((s) => needsAttention(s, dups.get(s.name), threshold)).length,
    };
  }, [skills, dups, threshold]);

  const currentName = params.get('skill');
  const current = useMemo(() => (currentName && skills ? skills.find((s) => s.name === currentName) ?? null : null), [currentName, skills]);

  const open = (name: string | null) => {
    setCreating(false);
    if (name !== currentName) setTab('overview');
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (name) next.set('skill', name);
        else next.delete('skill');
        return next;
      },
      { replace: true },
    );
  };

  const prefTimer = useRef<number | null>(null);
  const savePref = (key: keyof SkillPrefs, value: boolean | number) => {
    if (!prefs) return;
    const before = prefs;
    setPrefs({ ...prefs, [key]: value });
    const serverKey = { enabled: 'skills_enabled', autoExtract: 'auto_skills', autoApprove: 'auto_approve_skills', minConfidence: 'skill_min_confidence', maxInjected: 'skill_max_injected' }[key];
    const write = async () => {
      try {
        await setSkillPref(serverKey, value);
        if (key === 'enabled') say(value ? t('Skills on') : t('Skills off'));
      } catch (e) {
        setPrefs(before);
        say(`${t('Could not save')}: ${(e as Error).message}`);
      }
    };
    // The slider and the number fire on every step; the server hears the last one.
    if (key === 'minConfidence' || key === 'maxInjected') {
      if (prefTimer.current) window.clearTimeout(prefTimer.current);
      prefTimer.current = window.setTimeout(() => void write(), 350);
    } else void write();
  };

  const publish = async (name: string, status: 'draft' | 'published') => {
    setBusy(`status:${name}`);
    try {
      await setSkillStatus(name, status);
      await reload();
      say(status === 'published' ? t('Published') : t('Back to draft'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (names: string[]) => {
    setBusy('delete');
    let deleted = 0;
    for (const name of names) {
      try {
        await deleteSkill(name);
        deleted++;
      } catch {
        /* keep going; the count says what happened */
      }
    }
    setConfirm(null);
    setSelecting(false);
    setSelected(new Set());
    if (currentName && names.includes(currentName)) open(null);
    await reload();
    setBusy(null);
    say(tn(deleted, 'Deleted {n} skill', 'Deleted {n} skills'));
  };

  const bulkPublish = async () => {
    if (!skills) return;
    setBusy('publish');
    let published = 0;
    for (const name of selected) {
      const s = skills.find((x) => x.name === name);
      if (!s || s.status === 'published') continue;
      try {
        await setSkillStatus(name, 'published');
        published++;
      } catch {
        /* counted below */
      }
    }
    setSelecting(false);
    setSelected(new Set());
    await reload();
    setBusy(null);
    say(tn(published, 'Published {n}', 'Published {n}#'));
  };

  const askAudit = (names: string[], scope: 'selected' | 'all') => {
    if (!names.length) {
      say(scope === 'selected' ? t('Nothing selected to audit') : t('Nothing visible to audit'));
      return;
    }
    setConfirm({ kind: 'audit', names, scope });
  };

  const runAudit = async () => {
    if (confirm?.kind !== 'audit') return;
    setBusy('audit');
    try {
      await startAudit(confirm.names, confirm.scope, skipAudited);
      setAudit(await auditStatus());
      setAuditOpen(true);
      setSelecting(false);
      setSelected(new Set());
      setConfirm(null);
    } catch (e) {
      say(`${t('The audit could not start')}: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const stopAudit = async () => {
    setBusy('cancel');
    try {
      await cancelAudit();
      const st = await auditStatus();
      setAudit({ ...st, status: st.status === 'none' ? 'cancelled' : st.status });
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const nonPassing = useMemo(() => (skills ? skills.filter((s) => selected.has(s.name) && needsAttention(s, dups.get(s.name), threshold)).map((s) => s.name) : []), [skills, selected, dups, threshold]);

  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  if (error && !skills) {
    return (
      <div className="fs-screen fs-sk" data-testid="skills">
        <EmptyState icon={Zap} title={t('Skills could not be loaded')} body={error} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />
      </div>
    );
  }

  const auditCounts = audit ? audit.results.reduce<Record<string, number>>((acc, r) => ((acc[r.result] = (acc[r.result] ?? 0) + 1), acc), {}) : {};

  return (
    <div className="fs-screen fs-sk" data-testid="skills" data-selecting={selecting || undefined}>
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Skills')}</h1>
          <p className="fs-prose fs-sk__lede">
            {skills ? `${tn(skills.length, '{n} skill', '{n} skills')} · ${tn(counts.published, '{n} published', '{n} published#')}${counts.attention ? ` · ${tn(counts.attention, '{n} needs attention', '{n} need attention')}` : ''}. ` : ''}
            {t('Procedures the assistant learned. Published ones join its requests when they fit.')}
          </p>
        </div>
        <div className="fs-sk__head-actions">
          <label className="fs-switch">
            <input type="checkbox" checked={prefs?.enabled === true} disabled={!prefs} onChange={(e) => savePref('enabled', e.target.checked)} data-testid="skills-enabled" />
            <span>{t('Skills on')}</span>
          </label>
          {prefs && <LearningPopover prefs={prefs} onChange={savePref} />}
          <Button variant="primary" size="sm" icon={Plus} label={t('New skill')} onClick={() => setCreating(true)} testId="skills-new" />
        </div>
      </header>

      <div className="fs-sk__toolbar">
        <label className="fs-sk__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Search skills…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} data-testid="skills-search" />
        </label>
        <div className="fs-sk__filters" role="group" aria-label={t('Filter')}>
          {(
            [
              ['all', t('All'), counts.all],
              ['drafts', t('Drafts'), counts.drafts],
              ['published', t('Published'), counts.published],
              ['attention', t('Needs attention'), counts.attention],
            ] as [Filter, string, number][]
          ).map(([key, label, n]) => (
            <button key={key} type="button" className="fs-chip" data-on={filter === key || undefined} data-warn={key === 'attention' && n > 0 ? '' : undefined} onClick={() => setFilter(key)} data-testid={`skills-filter-${key}`}>
              {label}
              <span className="fs-sk__chip-n">{n}</span>
            </button>
          ))}
        </div>
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as SkillSort)} aria-label={t('Sort')}>
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {t(s.label)}
            </option>
          ))}
        </select>
        <select className="fs-field" value={confMax ?? ''} onChange={(e) => setConfMax(e.target.value ? Number(e.target.value) : null)} aria-label={t('Confidence up to')}>
          <option value="">{t('Any confidence')}</option>
          {CONF_MAX.map((n) => (
            <option key={n} value={n}>
              {t('Confidence ≤ {n}%', { n })}
            </option>
          ))}
        </select>
        <span className="fs-sk__spacer" />
        <Button variant="ghost" size="sm" icon={Zap} label={t('Audit all')} title={t('Test every visible skill, fix the weak ones with the teacher, flag what still fails')} onClick={() => askAudit(visible.map((s) => s.name), 'all')} testId="skills-audit" />
        <Button
          variant="ghost"
          size="sm"
          icon={selecting ? X : CheckSquare}
          label={selecting ? t('Leave selection') : t('Select several')}
          onClick={() => {
            setSelecting((v) => !v);
            setSelected(new Set());
          }}
          testId="skills-select"
        />
      </div>

      {audit && audit.status !== 'none' && auditOpen && (
        <section className="fs-sk__audit" data-status={audit.status} aria-live="polite" data-testid="skills-audit-panel">
          <div className="fs-sk__audit-head">
            <strong>
              {audit.status === 'running'
                ? `${t('Auditing {done}/{total}', { done: audit.done, total: audit.total })}${audit.current ? ` — ${audit.current}` : ''}`
                : audit.status === 'cancelled'
                  ? t('Audit cancelled — {done}/{total}', { done: audit.done, total: audit.total })
                  : t('Audit complete — {n} skills', { n: audit.total })}
            </strong>
            {audit.status === 'running' ? (
              <Button variant="ghost" size="sm" label={t('Cancel')} loading={busy === 'cancel'} onClick={() => void stopAudit()} />
            ) : (
              <Button variant="ghost" size="sm" icon={X} label={t('Close')} onClick={() => setAuditOpen(false)} />
            )}
          </div>
          <div className="fs-sk__audit-bar" role="progressbar" aria-valuemin={0} aria-valuemax={audit.total || 1} aria-valuenow={audit.done}>
            <span style={{ inlineSize: `${audit.total ? Math.round((audit.done / audit.total) * 100) : 0}%` }} />
          </div>
          {Object.keys(auditCounts).length > 0 && (
            <p className="fs-sk__audit-summary">
              {Object.entries(auditCounts)
                .map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`)
                .join(' · ')}
              {audit.teacher ? ` · ${t('teacher')}: ${audit.teacher}` : ''}
            </p>
          )}
          {audit.log.length > 0 && (
            <details className="fs-sk__audit-log" open={audit.status === 'running'}>
              <summary>{tn(audit.log.length, '{n} log line', '{n} log lines')}</summary>
              <div className="fs-sk__log fs-sk__log--audit">
                {audit.log.slice(-40).map((l, i) => (
                  <div key={i} className="fs-sk__line">
                    {l}
                  </div>
                ))}
              </div>
            </details>
          )}
        </section>
      )}

      {selecting && (
        <div className="fs-sk__bulk" role="toolbar" aria-label={t('Selection')} data-testid="skills-bulk">
          <label className="fs-switch">
            <input type="checkbox" checked={visible.length > 0 && visible.every((s) => selected.has(s.name))} onChange={(e) => setSelected(e.target.checked ? new Set(visible.map((s) => s.name)) : new Set())} />
            <span>{t('All')}</span>
          </label>
          <span className="fs-sk__bulk-n">{tn(selected.size, '{n} selected', '{n} selected#')}</span>
          <span className="fs-sk__spacer" />
          <Button variant="ghost" size="sm" icon={Check} label={t('Approve')} title={t('Publish the selected drafts')} disabled={!selected.size} loading={busy === 'publish'} onClick={() => void bulkPublish()} />
          <Button variant="ghost" size="sm" icon={Zap} label={t('Audit')} disabled={!selected.size} onClick={() => askAudit(visible.filter((s) => selected.has(s.name)).map((s) => s.name), 'selected')} />
          <Button variant="danger" size="sm" icon={AlertTriangle} label={t('Delete non-passing')} title={t('Duplicates, generic or irrelevant ones, failed audits and anything below the threshold')} disabled={!nonPassing.length} onClick={() => setConfirm({ kind: 'delete', names: nonPassing, nonPassing: true })} />
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={!selected.size} onClick={() => setConfirm({ kind: 'delete', names: [...selected] })} />
        </div>
      )}

      <div className="fs-sk__layout" data-detail={creating || current ? '' : undefined}>
        <div className="fs-sk__list" role="list" aria-label={t('Skills')}>
          {skills === null ? (
            <Skeleton label={t('Loading skills')} height="56px" count={3} />
          ) : visible.length === 0 ? (
            <EmptyState
              icon={Zap}
              headingLevel={3}
              title={skills.length === 0 ? t('No skills yet') : t('Nothing matches')}
              body={skills.length === 0 ? t('The assistant drafts one when it solves something worth repeating — or write one yourself.') : t('Try another filter or search.')}
              primaryAction={skills.length === 0 ? { label: t('New skill'), icon: Plus, onClick: () => setCreating(true) } : { label: t('Show all'), onClick: () => { setFilter('all'); setConfMax(null); setQuery(''); } }}
            />
          ) : (
            visible.map((s) => (
              <SkillRow
                key={s.name}
                skill={s}
                dup={dups.get(s.name)}
                threshold={threshold}
                on={s.name === currentName && !creating}
                selecting={selecting}
                selected={selected.has(s.name)}
                onOpen={() => (selecting ? toggle(s.name) : open(s.name))}
                onToggle={() => toggle(s.name)}
                auditing={audit?.status === 'running' && audit.current === s.name}
              />
            ))
          )}
        </div>

        <div className="fs-sk__pane">
          {creating ? (
            <NewSkillPane
              say={say}
              onClose={() => setCreating(false)}
              onAdded={(name) => {
                void reload().then(() => {
                  if (name) open(name);
                  else setCreating(false);
                });
              }}
            />
          ) : current ? (
            <SkillDetail
              key={current.name}
              skill={current}
              dup={dups.get(current.name)}
              tab={tab}
              onTab={setTab}
              onPublish={() => void publish(current.name, current.status === 'published' ? 'draft' : 'published')}
              onAudit={() => askAudit([current.name], 'selected')}
              onDelete={() => setConfirm({ kind: 'delete', names: [current.name] })}
              onChanged={() => void reload()}
              onBack={() => open(null)}
              say={say}
              busy={busy === `status:${current.name}`}
            />
          ) : (
            <div className="fs-sk__blank">
              <Zap size={28} aria-hidden="true" />
              <p className="fs-prose">{t('Pick a skill to read it, edit its SKILL.md or run a test. The confidence is the assistant’s own; a check next to it means it passed an audit.')}</p>
            </div>
          )}
        </div>
      </div>

      {confirm?.kind === 'delete' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={tn(confirm.names.length, 'Delete {n} skill?', 'Delete {n} skills?')}
          testId="skills-confirm-delete"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} loading={busy === 'delete'} onClick={() => void remove(confirm.names)} testId="skills-confirm-delete-ok" />
            </>
          }
        >
          <p className="fs-prose">
            {confirm.nonPassing ? t('Duplicates, generic or irrelevant ones, failed audits and anything below {n}%. This cannot be undone.', { n: Math.round(threshold * 100) }) : t('This cannot be undone.')}
          </p>
          <ul className="fs-sk__confirm-list">
            {confirm.names.slice(0, 12).map((n) => (
              <li key={n}>
                <code>{n}</code>
              </li>
            ))}
            {confirm.names.length > 12 && <li>…</li>}
          </ul>
        </Dialog>
      )}

      {confirm?.kind === 'audit' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={tn(confirm.names.length, 'Audit {n} skill?', 'Audit {n} skills?')}
          testId="skills-confirm-audit"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button variant="primary" size="sm" icon={Zap} label={t('Start the audit')} loading={busy === 'audit'} onClick={() => void runAudit()} testId="skills-confirm-audit-ok" />
            </>
          }
        >
          <p className="fs-prose">{t('Each one is tested on a task of its own; the ones that fail are rewritten by the teacher model and tested again. What still fails gets flagged. It runs on the server and takes a while.')}</p>
          <label className="fs-switch">
            <input type="checkbox" checked={skipAudited} onChange={(e) => setSkipAudited(e.target.checked)} />
            <span>{t('Skip the ones already audited')}</span>
          </label>
        </Dialog>
      )}

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
