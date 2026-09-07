export interface PendingAttachment {
  id: string;
  file: File;
  preview: string;
  state: 'queued' | 'uploading' | 'failed';
  error?: string;
}

/** Bounded queue with session-local lifetime. Removed/stale completions are ignored. */
export function createAttachmentUploads<T>(options: {
  upload: (file: File, signal: AbortSignal) => Promise<T[]>;
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
      const controller = new AbortController();
      const ownEpoch = epoch;
      requests.set(entry.id, controller);
      let timedOut = false;
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 60000);
      void options.upload(entry.file, controller.signal).then((uploaded) => {
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
    retry(id: string) {
      const entry = entries.get(id);
      if (entry?.state !== 'failed' || requests.has(id)) return;
      entry.state = 'queued'; entry.error = undefined;
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
