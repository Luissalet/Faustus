import { ChevronDown, ShieldAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  approveSkillImport,
  getSkillDiff,
  listDiscoveredSkills,
  type SkillReview,
} from '../../adapters/skillImports';
import { t, tn } from '../../i18n';
import '../settings.css';

/**
 * ADP-25 skills import review: every skill discovered under this project's
 * `.faustus|.agents|.claude/skills` folders, pinned to exact bytes
 * (`src/skill_import_review.py`) and never runnable until a human approves
 * its current digest. See docs/api/skills_review.md.
 */

const STATUS_TONE: Record<string, 'ok' | 'warn' | 'bad'> = {
  approved: 'ok',
  needs_review: 'warn',
  unreviewed: 'bad',
};

function statusLabel(state: string): string {
  if (state === 'approved') return t('approved');
  if (state === 'needs_review') return t('needs review');
  return t('unreviewed');
}

function DiffPanel({ projectId, id }: { projectId: string; id: string }) {
  const [diff, setDiff] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSkillDiff(projectId, id)
      .then((d) => { if (!cancelled) setDiff(d.has_approved ? d.diff : d.reason); })
      .catch((e: Error) => { if (!cancelled) setErr(e.message); });
    return () => { cancelled = true; };
  }, [projectId, id]);

  if (err) return <p className="fs-set__err">{err}</p>;
  if (diff === null) return <Skeleton label={t('Loading')} count={1} height="24px" />;
  return <pre className="fs-set__pre">{diff || t('No changes.')}</pre>;
}

function SkillImportCard({ projectId, review, onApprove, busy }: { projectId: string; review: SkillReview; onApprove: (override: boolean) => void; busy: boolean }) {
  const [showFindings, setShowFindings] = useState(false);
  const [showDiff, setShowDiff] = useState(false);
  const [override, setOverride] = useState(false);
  const scan = review.security_scan;
  const findingCount = scan?.findings?.length ?? 0;
  const isCritical = scan?.risk_level === 'critical';
  const blocked = review.privilege_request.length > 0;

  return (
    <li className="fs-set__row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }} data-testid={`skill-import-${review.id}`}>
      <div className="fs-set__row" style={{ padding: 0 }}>
        <span className="fs-tools__text" style={{ flex: 1 }}>
          <strong><code>{review.id}</code></strong>
          <span className="fs-set__help">
            {t('{origin} · v{version}', { origin: review.origin, version: review.version || '0' })}
          </span>
        </span>
        <span className="fs-autonomy-badge" data-tone={review.status.state === 'approved' ? 'promoted' : 'not-promoted'}>
          {statusLabel(review.status.state)}
        </span>
      </div>

      {review.tools_required.length > 0 && (
        <div className="fs-inline">
          {review.tools_required.map((tool) => (
            <span key={tool} className="fs-chip" data-on>{tool}</span>
          ))}
        </div>
      )}

      {blocked && (
        <div className="fs-notice" data-tone="danger">
          <ShieldAlert size={14} aria-hidden="true" />
          <span>
            {t('This skill\'s own text asks to change how approval works ({keys}); it can never be approved as written.', { keys: review.privilege_request.join(', ') })}
          </span>
        </div>
      )}

      {scan && scan.risk_level !== 'none' && (
        <div className="fs-notice" data-tone={isCritical ? 'danger' : undefined}>
          <button
            type="button"
            className="fs-autonomy-expand"
            style={{ display: 'inline-flex', gap: 6, alignItems: 'center', color: 'inherit' }}
            onClick={() => setShowFindings((v) => !v)}
            data-testid={`skill-import-scan-toggle-${review.id}`}
          >
            <ShieldAlert size={14} aria-hidden="true" />
            {t('{level} risk ({n} findings)', { level: scan.risk_level, n: String(findingCount) })}
            <ChevronDown size={14} aria-hidden="true" style={{ transform: showFindings ? 'rotate(180deg)' : undefined }} />
          </button>
          {showFindings && (
            <ul className="fs-set__list">
              {scan.findings.map((f, i) => (
                <li key={i} className="fs-set__help">
                  <strong>{f.severity}</strong> · {f.category} · {f.file}:{f.line} — {f.description}
                </li>
              ))}
            </ul>
          )}
          {isCritical && (
            <label className="fs-set__help" style={{ display: 'flex', alignItems: 'center', gap: 4, marginTop: 6 }}>
              <input
                type="checkbox"
                checked={override}
                onChange={(e) => setOverride(e.target.checked)}
                data-testid={`skill-import-override-${review.id}`}
              />
              {t('I have reviewed the findings and want to approve anyway')}
            </label>
          )}
        </div>
      )}

      <div className="fs-set__row-actions">
        <Button
          size="sm"
          variant="secondary"
          label={review.status.approval ? t('View diff') : t('No approved version yet')}
          disabled={!review.status.approval}
          onClick={() => setShowDiff((v) => !v)}
          testId={`skill-import-diff-${review.id}`}
        />
        <Button
          size="sm"
          variant="primary"
          label={review.status.state === 'approved' ? t('Re-approve') : t('Approve')}
          loading={busy}
          disabled={busy || blocked || (isCritical && !override) || review.status.state === 'approved'}
          onClick={() => onApprove(override)}
          testId={`skill-import-approve-${review.id}`}
        />
      </div>
      {showDiff && <DiffPanel projectId={projectId} id={review.id} />}
    </li>
  );
}

export function ProjectSkillImports({ projectId, say }: { projectId: string; say: (m: string) => void }) {
  const [rows, setRows] = useState<SkillReview[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = () => {
    if (!projectId) { setRows(null); return; }
    setErr(null);
    listDiscoveredSkills(projectId)
      .then(setRows)
      .catch((e: Error) => setErr(e.message));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [projectId]);

  const approve = (id: string, override: boolean) => {
    setBusy(id);
    approveSkillImport(projectId, id, override)
      .then(() => { say(t('Approved.')); load(); })
      .catch((e: Error) => say(e.message))
      .finally(() => setBusy(null));
  };

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Skills import review')}</h3>
      <p className="fs-set__help">
        {t('Skills this project\'s own folders declare (.faustus, .agents or .claude/skills). None of them run until a human approves the exact bytes on disk; any later edit needs review again.')}
      </p>
      {err && <p className="fs-set__err">{err}</p>}
      {!rows && !err ? (
        <Skeleton label={t('Loading')} count={2} height="60px" />
      ) : rows && rows.length === 0 ? (
        <EmptyState title={t('No imported skills')} body={t('Nothing under .faustus, .agents or .claude/skills in this project\'s folder yet.')} />
      ) : (
        <ul className="fs-set__list">
          {(rows ?? []).map((r) => (
            <SkillImportCard key={r.id} projectId={projectId} review={r} busy={busy === r.id} onApprove={(override) => approve(r.id, override)} />
          ))}
        </ul>
      )}
      {rows && rows.length > 0 && (
        <p className="fs-set__help">{tn(rows.length, '{n} skill found', '{n} skills found')}</p>
      )}
    </div>
  );
}
