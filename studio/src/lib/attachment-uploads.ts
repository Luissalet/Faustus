export interface PendingAttachment {
  id: string;
  file: File;
  preview: string;
  state: 'queued' | 'uploading' | 'failed';
  /** 0–1 while uploading; undefined until the first progress event. */
  progress?: number;
  error?: string;
}

/** Bounded queue with session-local lifetime. Removed/stale completions are ignored. */
export function createAttachmentUploads<T>(options: {
  upload: (file: File, signal: AbortSignal, onProgress: (ratio: number) => void) => Promise<T[]>;
  ready: (uploaded: T[]) => void;
  change: (entries: PendingAttachment[]) => void;
  preview: (file: File) => string;
  revoke: (url: string) => void;
  timeoutMessage: () => string;
  emptyMessage: () => string;
}) {
  const entries = new Map<string, PendingAttachment>();
  const requests = new Map<string, AbortController>();
  let serial = 0, active = true, epoch = 0;
  const emit = () => { if (active) options.change([...entries.values()]); };
  const release = (entry: PendingAttachment) => { if (entry.preview) options.revoke(entry.preview); };
  const pump = () => {
    if (!active) return;
    for (const entry of entries.values()) {
      if (requests.size >= 2) break;
      if (entry.state !== 'queued') continue;
      entry.state = 'uploading';
      entry.progress = 0;
      const controller = new AbortController();
      const ownEpoch = epoch;
      requests.set(entry.id, controller);
      let timedOut = false;
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 60000);
      const onProgress = (ratio: number) => {
        if (!active || epoch !== ownEpoch || !entries.has(entry.id)) return;
        entry.progress = Math.max(0, Math.min(1, ratio));
        emit();
      };
      void options.upload(entry.file, controller.signal, onProgress).then((uploaded) => {
        if (!active || epoch !== ownEpoch || !entries.has(entry.id)) return;
        if (controller.signal.aborted) {
          if (timedOut) throw new Error(options.timeoutMessage());
          return;
        }
        if (!uploaded.length) throw new Error(options.emptyMessage());
        options.ready(uploaded);
        entries.delete(entry.id);
        release(entry);
      }).catch((error: unknown) => {
        if (!active || epoch !== ownEpoch || !entries.has(entry.id)) return;
        // failFile() may already have set a specific reason before aborting.
        if (entry.state === 'failed' && entry.error) return;
        entry.state = 'failed';
        entry.error = timedOut ? options.timeoutMessage() : error instanceof Error ? error.message : String(error);
      }).finally(() => {
        clearTimeout(timer);
        if (epoch !== ownEpoch) return;
        requests.delete(entry.id);
        emit();
        pump();
      });
    }
    emit();
  };
  return {
    add(files: File[]) {
      if (!active) return;
      for (const file of files) {
        const id = `pending-${++serial}`;
        entries.set(id, {id, file, preview: options.preview(file), state:'queued'});
      }
      pump();
    },
    remove(id: string) {
      const entry = entries.get(id);
      if (!entry) return;
      entries.delete(id);
      requests.get(id)?.abort();
      release(entry);
      emit();
    },
    /** Mark matching pending files failed (e.g. audio without a transcoder). */
    failFile(file: File, error: string) {
      let changed = false;
      for (const entry of entries.values()) {
        if (entry.file !== file || entry.state === 'failed') continue;
        requests.get(entry.id)?.abort();
        requests.delete(entry.id);
        entry.state = 'failed';
        entry.error = error;
        changed = true;
      }
      if (changed) emit();
    },
    retry(id: string) {
      const entry = entries.get(id);
      if (entry?.state !== 'failed' || requests.has(id)) return;
      entry.state = 'queued'; entry.error = undefined; entry.progress = undefined;
      pump();
    },
    hasPending() { return entries.size > 0; },
    resume() { active = true; pump(); },
    dispose() {
      active = false; ++epoch;
      for (const controller of requests.values()) controller.abort();
      for (const entry of entries.values()) release(entry);
      requests.clear(); entries.clear();
    },
  };
}
