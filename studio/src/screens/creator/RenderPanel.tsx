import { useCallback, useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { Button, Toast } from '../../components';
import { t } from '../../i18n';
import * as renderApi from '../../adapters/creator_render';
import { CreatorApiError } from '../../adapters/creator';

/**
 * WP14 — the render panel opened from Timeline.tsx's "Render" button:
 * pick a profile, see the plan (estimate, cache status), start a render
 * (walking through preflight/approve exactly like the rest of Creator when
 * the budget policy asks for it), and watch progress until it settles.
 */

const PROFILES: { id: string; label: string }[] = [
  { id: 'youtube_1080p', label: 'YouTube 1080p (16:9)' },
  { id: 'youtube_4k', label: 'YouTube 4K (16:9)' },
  { id: 'shorts_9x16', label: 'YouTube Shorts (9:16)' },
  { id: 'reels_9x16', label: 'Instagram Reels (9:16)' },
  { id: 'tiktok_9x16', label: 'TikTok (9:16)' },
  { id: 'instagram_1x1', label: 'Instagram Feed (1:1)' },
  { id: 'linkedin_16x9', label: 'LinkedIn (16:9)' },
  { id: 'cinema_21x9', label: 'Cinema (21:9)' },
];

export interface RenderPanelProps {
  projectId: string;
  docId: string;
  onClose: () => void;
}

export function RenderPanel({ projectId, docId, onClose }: RenderPanelProps) {
  const [profileId, setProfileId] = useState('youtube_1080p');
  const [plan, setPlan] = useState<renderApi.RenderPlanResponse | null>(null);
  const [job, setJob] = useState<renderApi.RenderJobResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPlan(null);
    setError(null);
    renderApi.getRenderPlan(projectId, docId, profileId)
      .then((p) => { if (!cancelled) setPlan(p); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); });
    return () => { cancelled = true; };
  }, [projectId, docId, profileId]);

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  const pollJob = useCallback((token: string) => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(() => {
      renderApi.getRenderJob(token).then((j) => {
        setJob(j);
        if (j.state === 'done' || j.state === 'error') {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setToast(j.state === 'done' ? t('Render finished.') : t('Render failed.'));
        }
      }).catch(() => { /* transient poll error: try again on the next tick */ });
    }, 1000);
  }, []);

  const startRender = useCallback(async (preflightDigest?: string) => {
    setBusy(true);
    setError(null);
    try {
      const started = await renderApi.startRender(projectId, docId, profileId, { preflightDigest });
      setJob({ render_token: started.render_token, state: 'starting', engine_state: 'starting',
                progress: 0, ffmpeg_job_id: null, result: null, error: null });
      pollJob(started.render_token);
    } catch (err) {
      if (err instanceof CreatorApiError && err.status === 403 && err.payload?.digest) {
        setError(t('Approval required — approve it in the Preflight panel, then render again.'));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }, [projectId, docId, profileId, pollJob]);

  const cancel = useCallback(() => {
    if (!job) return;
    renderApi.cancelRender(job.render_token)
      .then(() => setToast(t('Cancel requested.')))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [job]);

  const progressPct = job?.progress != null ? Math.round(job.progress * 100) : null;
  const running = job !== null && job.state !== 'done' && job.state !== 'error';

  return (
    <div className="fs-render-panel" role="dialog" aria-label={t('Render')} data-testid="render-panel">
      <div className="fs-render-panel__header">
        <h3>{t('Render')}</h3>
        <Button size="sm" variant="ghost" icon={X} label={t('Close')} onClick={onClose} testId="render-panel-close" />
      </div>

      <label className="fs-render-panel__field">
        {t('Profile')}
        <select
          value={profileId}
          onChange={(e) => setProfileId(e.target.value)}
          disabled={running}
          data-testid="render-profile-select"
        >
          {PROFILES.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
        </select>
      </label>

      {plan && (
        <dl className="fs-render-panel__plan" data-testid="render-plan">
          <dt>{t('Resolution')}</dt><dd>{plan.profile.width}×{plan.profile.height}</dd>
          <dt>{t('Estimated duration')}</dt><dd>{plan.estimated_duration_seconds.toFixed(1)}s</dd>
          <dt>{t('Estimated size')}</dt><dd>{Math.round(plan.estimated_size_bytes / 1024)} KB</dd>
          <dt>{t('Engine')}</dt>
          <dd>{plan.engine_available ? t('ffmpeg available') : t('ffmpeg not available')}</dd>
          {plan.cached_occurrence_id && <><dt>{t('Cache')}</dt><dd>{t('Cached — instant reuse')}</dd></>}
        </dl>
      )}

      {error && <p className="fs-render-panel__error" role="alert">{error}</p>}

      {!running && (
        <Button
          variant="primary" label={t('Render')} onClick={() => void startRender()}
          disabled={busy || !plan || !plan.engine_available} loading={busy} testId="render-start"
        />
      )}

      {job && (
        <div className="fs-render-panel__progress" data-testid="render-progress">
          <div className="fs-render-panel__progress-bar">
            <div
              className="fs-render-panel__progress-fill"
              style={{ width: `${progressPct ?? 0}%` }}
            />
          </div>
          <span>{job.state === 'done' ? t('Done') : job.state === 'error' ? t('Error') : `${progressPct ?? 0}%`}</span>
          {running && (
            <Button size="sm" variant="danger" label={t('Cancel')} onClick={cancel} testId="render-cancel" />
          )}
        </div>
      )}

      {job?.state === 'done' && job.result?.occurrence_id && (
        <p className="fs-render-panel__result" data-testid="render-result">
          {job.result.cache_hit ? t('Reused a cached render.') : t('Render complete.')}
          {' '}{t('Occurrence: {id}', { id: job.result.occurrence_id })}
        </p>
      )}
      {job?.state === 'error' && (
        <p className="fs-render-panel__error" role="alert">{job.error}</p>
      )}

      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
