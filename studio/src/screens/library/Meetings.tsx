import { ChevronDown, ChevronUp, Mic, Square, Upload } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { relativeTime } from '../../adapters/home';
import { formatMeetingDuration, getMeeting, listMeetings, uploadMeeting, waitForMeetingJob, type MeetingDetail, type MeetingSummary } from '../../adapters/meetings';
import { Rich } from '../rich';
import { t, tn } from '../../i18n';
import { Highlight } from './parts';

/**
 * Meeting notes (FEATURE B): record or upload audio, watch it turn into
 * timestamped notes in the background, then read the result — summary,
 * decisions, action items, open questions, and the cleaned transcript.
 */
export function MeetingsLibrary({ query, say }: { query: string; say: (m: string) => void }) {
  const [items, setItems] = useState<MeetingSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, MeetingDetail | string>>({});
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const mediaRecorder = useRef<MediaRecorder | null>(null);
  const recordedChunks = useRef<Blob[]>([]);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const out = await listMeetings(signal);
      if (signal?.aborted) return;
      setItems(out);
      setError(null);
    } catch (e) {
      if (!signal?.aborted) setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);

  const visible = (items ?? []).filter((m) => {
    const needle = query.trim().toLowerCase();
    if (!needle) return true;
    return [m.title, m.source_filename].filter(Boolean).some((f) => String(f).toLowerCase().includes(needle));
  });

  const runUpload = useCallback(
    async (blob: Blob, filename: string) => {
      setBusy(true);
      setProgress(t('Uploading…'));
      try {
        const { job_id: jobId } = await uploadMeeting(blob, filename);
        setProgress(t('Transcribing and writing notes…'));
        const job = await waitForMeetingJob(jobId);
        if (job.status === 'done') {
          say(t('Meeting notes ready'));
          await load();
        } else {
          say(job.error || t('Could not process the meeting.'));
        }
      } catch (e) {
        say((e as Error).message);
      } finally {
        setBusy(false);
        setProgress(null);
      }
    },
    [load, say],
  );

  const onFilePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) void runUpload(file, file.name);
  };

  const startRecording = async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      say(t('Recording needs a supported browser and microphone access.'));
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recordedChunks.current = [];
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (ev) => {
        if (ev.data.size > 0) recordedChunks.current.push(ev.data);
      };
      rec.onstop = () => {
        stream.getTracks().forEach((tr) => tr.stop());
        const blob = new Blob(recordedChunks.current, { type: rec.mimeType || 'audio/webm' });
        void runUpload(blob, `meeting-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.webm`);
      };
      mediaRecorder.current = rec;
      rec.start();
      setRecording(true);
    } catch {
      say(t('Could not access the microphone.'));
    }
  };

  const stopRecording = () => {
    mediaRecorder.current?.stop();
    setRecording(false);
  };

  const expand = async (item: MeetingSummary) => {
    if (expanded === item.id) {
      setExpanded(null);
      return;
    }
    setExpanded(item.id);
    if (!(item.id in detail)) {
      try {
        const d = await getMeeting(item.id);
        setDetail((cur) => ({ ...cur, [item.id]: d }));
      } catch (e) {
        setDetail((cur) => ({ ...cur, [item.id]: (e as Error).message }));
      }
    }
  };

  return (
    <div className="fs-gal fs-lib" data-testid="meetings-library">
      <div className="fs-gal__toolbar">
        <p className="fs-gal__stats" style={{ margin: 0 }}>
          {items ? tn(items.length, '{n} meeting', '{n} meetings') : ''}
        </p>
        <span className="fs-gal__spacer" />
        <input ref={fileInput} type="file" accept="audio/*" style={{ display: 'none' }} onChange={onFilePicked} />
        {!recording ? (
          <Button variant="ghost" size="sm" icon={Mic} label={t('Record#audio')} disabled={busy} onClick={() => void startRecording()} />
        ) : (
          <Button variant="danger" size="sm" icon={Square} label={t('Stop recording')} onClick={stopRecording} />
        )}
        <Button variant="ghost" size="sm" icon={Upload} label={t('Upload audio')} disabled={busy || recording} onClick={() => fileInput.current?.click()} />
      </div>

      {progress && <p className="fs-notice">{progress}</p>}
      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!items && !error && <Skeleton label={t('Loading meetings')} count={3} height="64px" radius="panel" />}
      {items && !items.length && (
        <EmptyState
          icon={Mic}
          title={t('No meeting notes yet')}
          body={t('Record or upload an audio file; the transcript and notes collect here.')}
        />
      )}

      {items && visible.length > 0 && (
        <ul className="fs-lib__list">
          {visible.map((item) => {
            const open = expanded === item.id;
            const d = detail[item.id];
            return (
              <li key={item.id} className="fs-lib__item" data-open={open || undefined}>
                <div className="fs-lib__row">
                  <button type="button" className="fs-lib__main" onClick={() => void expand(item)} aria-expanded={open}>
                    <Mic size={16} aria-hidden="true" className="fs-lib__icon" />
                    <span className="fs-lib__text">
                      <span className="fs-lib__title">
                        <Highlight text={item.title || t('Untitled meeting')} needle={query} />
                        {item.model_ok === false && <span className="fs-lib__badge" data-tone="warning">{t('transcript only')}</span>}
                      </span>
                      <span className="fs-lib__meta">
                        {[item.date, formatMeetingDuration(item.duration_seconds), relativeTime(item.created_at)].filter(Boolean).join(' · ')}
                      </span>
                    </span>
                    {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                  </button>
                </div>
                {open && (
                  <div className="fs-lib__peek">
                    {typeof d === 'string' && <p className="fs-gal__muted">{d}</p>}
                    {d === undefined && <p className="fs-gal__muted">{t('Loading…')}</p>}
                    {typeof d === 'object' && <Rich text={d.markdown} />}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
