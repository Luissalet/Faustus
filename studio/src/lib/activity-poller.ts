/** One owner for refresh, visibility and cancellation. No overlapping poll chains. */
export function createActivityPoller<T>(options: {
  load: (signal: AbortSignal) => Promise<T>;
  live: (data: T) => boolean;
  visible: () => boolean;
  data: (data: T) => void;
  error: () => void;
  refreshing: (value: boolean) => void;
  schedule: (fn: () => void, delay: number) => number;
  clear: (id: number) => void;
}) {
  let active = false;
  let revision = 0;
  let pending: AbortController | null = null;
  let next: number | undefined;
  let deadline: number | undefined;
  const clearTimers = () => {
    if (next !== undefined) options.clear(next);
    if (deadline !== undefined) options.clear(deadline);
    next = deadline = undefined;
  };
  const refresh = async (): Promise<T | null> => {
    if (!active) return null;
    const ownRevision = ++revision;
    pending?.abort();
    clearTimers();
    const controller = new AbortController();
    pending = controller;
    deadline = options.schedule(() => controller.abort(), 15000);
    options.refreshing(true);
    let data: T | null = null;
    try {
      data = await options.load(controller.signal);
      if (!active || ownRevision !== revision) return null;
      controller.signal.throwIfAborted();
      options.data(data);
      return data;
    } catch {
      data = null;
      if (active && ownRevision === revision) options.error();
      return null;
    } finally {
      if (active && ownRevision === revision) {
        clearTimers();
        pending = null;
        options.refreshing(false);
        if (options.visible()) next = options.schedule(() => { void refresh(); }, data && options.live(data) ? 5000 : 30000);
      }
    }
  };
  return {
    refresh,
    start() { active = true; if (options.visible()) void refresh(); },
    visibilityChanged() {
      if (!active) return;
      if (options.visible()) void refresh();
      else if (next !== undefined) { options.clear(next); next = undefined; }
    },
    dispose() { active = false; ++revision; pending?.abort(); pending = null; clearTimers(); },
  };
}
