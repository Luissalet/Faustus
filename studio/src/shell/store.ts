import { create } from 'zustand';

/**
 * Shell state (UI-020).
 *
 * Interface context only. Projects, runs and artifacts remain authoritative
 * on the server; what lives here is what the interface is currently showing,
 * so a screen never has to write into five modules to change view.
 *
 * Only layout preferences are persisted. Caches and permissions are not
 * truth and must never be stored as if they were.
 */

export type NavMode = 'full' | 'rail';
export type Density = 'comfortable' | 'default' | 'compact';

interface ShellContext {
  projectId: string | null;
  intention: string | null;
  skillId: string | null;
  selectedArtifactIds: string[];
  backendId: string | null;
}

interface ShellLayout {
  navMode: NavMode;
  inspectorOpen: boolean;
  inspectorTab: string | null;
  density: Density;
}

interface ShellState {
  context: ShellContext;
  layout: ShellLayout;
  paletteOpen: boolean;
  setContext: (patch: Partial<ShellContext>) => void;
  setLayout: (patch: Partial<ShellLayout>) => void;
  setPaletteOpen: (open: boolean) => void;
}

const LAYOUT_KEY = 'faustus_studio_layout';

function readLayout(): ShellLayout {
  const fallback: ShellLayout = {
    navMode: 'full',
    inspectorOpen: false,
    inspectorTab: null,
    density: 'default',
  };
  try {
    const raw = window.localStorage.getItem(LAYOUT_KEY);
    if (!raw) return fallback;
    return { ...fallback, ...(JSON.parse(raw) as Partial<ShellLayout>) };
  } catch {
    return fallback;
  }
}

function persistLayout(layout: ShellLayout): void {
  try {
    window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    /* a remembered sidebar width is not worth an exception */
  }
}

export const useShell = create<ShellState>((set, get) => ({
  context: {
    projectId: null,
    intention: null,
    skillId: null,
    selectedArtifactIds: [],
    backendId: null,
  },
  layout: readLayout(),
  paletteOpen: false,
  setContext: (patch) => set({ context: { ...get().context, ...patch } }),
  setLayout: (patch) => {
    const layout = { ...get().layout, ...patch };
    persistLayout(layout);
    set({ layout });
  },
  setPaletteOpen: (paletteOpen) => set({ paletteOpen }),
}));
