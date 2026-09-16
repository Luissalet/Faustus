import { useCallback, useEffect, useMemo, useState } from 'react';
import { Music as MusicIcon, Plus, Sparkles, Star } from 'lucide-react';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import * as api from '../../adapters/creator';
import * as musicApi from '../../adapters/creator_music';
import { t } from '../../i18n';
import './music.css';

/**
 * WP24 — Music Studio tab (MUS01-05): a minimal editor over a `song`
 * CreatorDocument — sections with per-section lyrics, style tags,
 * target BPM/key, seed — plus "Generar" (one take) and a takes list with
 * an inline `<audio>` player, each take's actual seed/engine/BPM/key, and
 * a favorite toggle. Every edit is a typed `song.*` op through
 * `adapters/creator_music.ts` with the document's OWN `expected_revision`
 * (a 409 reloads rather than overwrites, same discipline as `Timeline.tsx`).
 *
 * Generation gate: `generateMusic` may answer 403
 * `{reason: 'preflight_required', digest}` — this screen runs
 * `runPreflight`/`approvePreflight` (`adapters/creator.ts`, WP09) itself
 * and retries with the approved digest, so "Generar" reads as one action
 * even though it is two calls under the hood.
 */

type SongContent = musicApi.SongDocContent;

const SECTION_KINDS = ['intro', 'verse', 'pre_chorus', 'chorus', 'bridge', 'outro'];

function newId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

function defaultSongContent(): SongContent {
  return {
    language: 'en',
    sections: [{ id: newId('sec'), kind: 'verse', lyrics: '' }],
    takes: [],
    selected_take: null,
    style_tags: [],
    bpm_target: null,
    key_target: null,
    seed: null,
  };
}

export interface MusicProps {
  projectId: string;
}

export function Music({ projectId }: MusicProps) {
  const [docs, setDocs] = useState<api.CreatorDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [docId, setDocId] = useState<string | null>(null);
  const [doc, setDoc] = useState<api.CreatorDocument | null>(null);
  const [mode, setMode] = useState<'simple' | 'expert'>('simple');
  const [engine, setEngine] = useState<'ace_step' | 'musicgen'>('ace_step');
  const [durationS, setDurationS] = useState(60);
  const [steps, setSteps] = useState(27);
  const [busy, setBusy] = useState(false);
  const [genState, setGenState] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const content = doc?.content as SongContent | undefined;

  const reload = useCallback(() => {
    setLoading(true);
    api.listDocuments(projectId)
      .then((r) => setDocs(r.documents.filter((d) => d.kind === 'song')))
      .catch(() => setDocs([]))
      .finally(() => setLoading(false));
  }, [projectId]);
  useEffect(() => { reload(); }, [reload]);

  useEffect(() => {
    if (!docId) { setDoc(null); return; }
    api.getDocument(docId).then(setDoc).catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [docId]);

  const createSong = useCallback(async () => {
    try {
      const created = await api.createDocument(projectId, 'song', defaultSongContent());
      setDocs((prev) => [created, ...prev]);
      setDocId(created.id);
      setDoc(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [projectId]);

  const applyAndSet = useCallback(async (fn: () => Promise<api.ApplyCommandResult>) => {
    try {
      const result = await fn();
      setDoc(result.document);
    } catch (err) {
      if (err instanceof api.RevisionConflictError && docId) {
        const fresh = await api.getDocument(docId);
        setDoc(fresh);
        setToast(t('The document changed elsewhere — reloaded.'));
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [docId]);

  const editLyrics = useCallback((sectionId: string, lyrics: string) => {
    if (!doc) return;
    void applyAndSet(() => musicApi.editLyrics(doc.id, doc.revision, sectionId, lyrics));
  }, [doc, applyAndSet]);

  const addSection = useCallback((kind: string) => {
    if (!doc) return;
    void applyAndSet(() => musicApi.addSection(doc.id, doc.revision, newId('sec'), kind));
  }, [doc, applyAndSet]);

  const removeSection = useCallback((sectionId: string) => {
    if (!doc) return;
    void applyAndSet(() => musicApi.removeSection(doc.id, doc.revision, sectionId));
  }, [doc, applyAndSet]);

  const moveSection = useCallback((index: number, dir: -1 | 1) => {
    if (!doc || !content) return;
    const order = content.sections.map((s) => s.id);
    const j = index + dir;
    if (j < 0 || j >= order.length) return;
    [order[index], order[j]] = [order[j], order[index]];
    void applyAndSet(() => musicApi.reorderSections(doc.id, doc.revision, order));
  }, [doc, content, applyAndSet]);

  const saveStyle = useCallback((styleTags: string[], bpm: number | null, key: string | null) => {
    if (!doc) return;
    void applyAndSet(() => musicApi.setStyle(doc.id, doc.revision, { styleTags, bpmTarget: bpm, keyTarget: key }));
  }, [doc, applyAndSet]);

  const saveSeed = useCallback((seed: number | null) => {
    if (!doc) return;
    void applyAndSet(() => musicApi.setSeed(doc.id, doc.revision, seed));
  }, [doc, applyAndSet]);

  const selectTake = useCallback((takeId: string | null) => {
    if (!doc) return;
    void applyAndSet(() => musicApi.selectTake(doc.id, doc.revision, takeId));
  }, [doc, applyAndSet]);

  const toggleFavorite = useCallback((takeId: string, favorite: boolean) => {
    if (!doc) return;
    void applyAndSet(() => musicApi.markTakeFavorite(doc.id, doc.revision, takeId, favorite));
  }, [doc, applyAndSet]);

  const runPreflightAndApprove = useCallback(async (digest: string) => {
    const report = await api.runPreflight({
      project_id: projectId, operation: 'generate_music', engine, params: { duration_s: durationS },
    });
    if (report.requires_approval && report.approval_digest) {
      await api.approvePreflight(report.approval_digest, {
        project_id: projectId, operation: 'generate_music', engine, params: { duration_s: durationS },
      });
      return report.approval_digest;
    }
    return digest;
  }, [projectId, engine, durationS]);

  const generate = useCallback(async () => {
    if (!doc) return;
    setBusy(true);
    setError(null);
    setGenState(t('Starting…'));
    try {
      let job: musicApi.MusicJob;
      try {
        job = await musicApi.generateMusic({ docId: doc.id, projectId, engine, durationS, steps: mode === 'expert' ? steps : undefined });
      } catch (err) {
        if (err instanceof api.CreatorApiError && err.status === 403
            && (err.payload as { reason?: string }).reason === 'preflight_required') {
          const digest = String((err.payload as { digest?: string }).digest ?? '');
          const approved = await runPreflightAndApprove(digest);
          job = await musicApi.generateMusic({
            docId: doc.id, projectId, engine, durationS,
            steps: mode === 'expert' ? steps : undefined, preflightDigest: approved,
          });
        } else {
          throw err;
        }
      }
      await musicApi.pollMusicJob(job.job_id, (j) => setGenState(t(j.state)));
      const fresh = await api.getDocument(doc.id);
      setDoc(fresh);
      setToast(t('Take generated.'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setGenState(null);
    }
  }, [doc, projectId, engine, durationS, steps, mode, runPreflightAndApprove]);

  const styleTagsText = useMemo(() => (content?.style_tags ?? []).join(', '), [content]);

  if (loading) return <Skeleton label={t('Loading songs')} count={3} height="20px" />;

  if (!docId || !doc || !content) {
    return (
      <div className="fs-music" data-testid="creator-music">
        <div className="fs-music__list">
          <Button size="sm" variant="primary" icon={Plus} label={t('New song')} onClick={() => void createSong()} testId="music-new-song" />
          <ul>
            {docs.map((d) => (
              <li key={d.id}>
                <button type="button" onClick={() => setDocId(d.id)} data-testid={`music-open-${d.id}`}>
                  {t('Song')} {d.id.slice(-8)}
                </button>
              </li>
            ))}
          </ul>
        </div>
        {docs.length === 0 && (
          <EmptyState icon={MusicIcon} title={t('No songs yet')} body={t('Create a song to start writing lyrics and generating takes.')} />
        )}
      </div>
    );
  }

  return (
    <div className="fs-music" data-testid="creator-music">
      <header className="fs-music__top">
        <select value={docId} onChange={(e) => setDocId(e.target.value)} data-testid="music-doc-select">
          {docs.map((d) => <option key={d.id} value={d.id}>{t('Song')} {d.id.slice(-8)}</option>)}
        </select>
        <Button size="sm" icon={Plus} label={t('New song')} onClick={() => void createSong()} testId="music-new-song" />
        <div className="fs-music__mode" role="tablist" aria-label={t('Mode')}>
          <button type="button" role="tab" aria-selected={mode === 'simple'} onClick={() => setMode('simple')} data-testid="music-mode-simple">{t('Simple')}</button>
          <button type="button" role="tab" aria-selected={mode === 'expert'} onClick={() => setMode('expert')} data-testid="music-mode-expert">{t('Expert')}</button>
        </div>
      </header>

      {error && <p className="fs-music__error" role="alert">{error}</p>}

      <section className="fs-music__sections" aria-label={t('Lyrics')}>
        {content.sections.map((section, index) => (
          <div key={section.id} className="fs-music__section" data-testid={`music-section-${section.id}`}>
            <div className="fs-music__section-head">
              {/* Section `kind` is set at creation time (`song.add_section`)
                  and has no dedicated "rename kind" op in this lot — remove
                  and re-add under the intended kind instead. */}
              <span className="fs-music__section-kind">{t(section.kind)}</span>
              <div className="fs-music__section-actions">
                <button type="button" onClick={() => moveSection(index, -1)} aria-label={t('Move up')}>↑</button>
                <button type="button" onClick={() => moveSection(index, 1)} aria-label={t('Move down')}>↓</button>
                <button type="button" onClick={() => removeSection(section.id)} aria-label={t('Remove section')}
                        disabled={content.sections.length <= 1}>×</button>
              </div>
            </div>
            <textarea
              value={section.lyrics}
              onChange={(e) => editLyrics(section.id, e.target.value)}
              placeholder={t('Lyrics for this section (leave empty for instrumental)')}
              rows={3}
              data-testid={`music-lyrics-${section.id}`}
            />
          </div>
        ))}
        <div className="fs-music__add-section">
          {SECTION_KINDS.map((k) => (
            <button key={k} type="button" onClick={() => addSection(k)} data-testid={`music-add-${k}`}>+ {t(k)}</button>
          ))}
        </div>
      </section>

      <section className="fs-music__style" aria-label={t('Style')}>
        <label>
          {t('Style tags (comma-separated)')}
          <input
            defaultValue={styleTagsText}
            onBlur={(e) => saveStyle(
              e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
              content.bpm_target ?? null, content.key_target ?? null,
            )}
            data-testid="music-style-tags"
          />
        </label>
        {mode === 'expert' && (
          <>
            <label>
              {t('Target BPM')}
              <input type="number" min={20} max={300}
                     defaultValue={content.bpm_target ?? ''}
                     onBlur={(e) => saveStyle(content.style_tags ?? [], e.target.value ? Number(e.target.value) : null, content.key_target ?? null)}
                     data-testid="music-bpm" />
            </label>
            <label>
              {t('Target key')}
              <input defaultValue={content.key_target ?? ''}
                     onBlur={(e) => saveStyle(content.style_tags ?? [], content.bpm_target ?? null, e.target.value || null)}
                     data-testid="music-key" />
            </label>
            <label>
              {t('Seed')}
              <input type="number" min={0}
                     defaultValue={content.seed ?? ''}
                     onBlur={(e) => saveSeed(e.target.value ? Number(e.target.value) : null)}
                     data-testid="music-seed" />
            </label>
            <label>
              {t('Engine')}
              <select value={engine} onChange={(e) => setEngine(e.target.value as 'ace_step' | 'musicgen')} data-testid="music-engine">
                <option value="ace_step">ACE-Step</option>
                <option value="musicgen">MusicGen</option>
              </select>
            </label>
            <label>
              {t('Steps')}
              <input type="number" min={1} value={steps} onChange={(e) => setSteps(Number(e.target.value) || 27)} data-testid="music-steps" />
            </label>
          </>
        )}
        <label>
          {t('Duration (s)')}
          <input type="number" min={1} value={durationS} onChange={(e) => setDurationS(Number(e.target.value) || 60)} data-testid="music-duration" />
        </label>
      </section>

      <div className="fs-music__generate">
        <Button variant="primary" icon={Sparkles} label={t('Generate')} onClick={() => void generate()}
                loading={busy} testId="music-generate" />
        {genState && <span className="fs-music__gen-state">{genState}</span>}
      </div>

      <section className="fs-music__takes" aria-label={t('Takes')}>
        <h3>{t('Takes')}</h3>
        {content.takes.length === 0 && <p>{t('No takes yet — generate one above.')}</p>}
        <ul>
          {content.takes.map((take) => (
            <li key={take.id} className={take.id === content.selected_take ? 'fs-music__take fs-music__take--selected' : 'fs-music__take'}
                data-testid={`music-take-${take.id}`}>
              <audio controls src={`/api/artifacts/${take.occurrence_id}/download`} data-testid={`music-audio-${take.id}`} />
              <div className="fs-music__take-meta">
                <span>{t('seed')}: {take.seed ?? t('unknown')}</span>
                <span>{t('engine')}: {take.engine}</span>
                {take.bpm != null && <span>{take.bpm} BPM</span>}
                {take.key != null && <span>{take.key}</span>}
              </div>
              <div className="fs-music__take-actions">
                <button type="button" onClick={() => selectTake(take.id === content.selected_take ? null : take.id)}
                        data-testid={`music-select-${take.id}`}>
                  {take.id === content.selected_take ? t('Selected') : t('Select')}
                </button>
                <button type="button" onClick={() => toggleFavorite(take.id, !take.favorite)}
                        aria-pressed={take.favorite} data-testid={`music-favorite-${take.id}`}>
                  <Star size={14} fill={take.favorite ? 'currentColor' : 'none'} /> {t('Favorite')}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
