/**
 * Guided tours.
 *
 * The previous interface had eleven of them, each a copy-pasted 150-line
 * async function inside `slashCommands.js`: each one re-injected the same
 * `<style id="tour-styles">`, each one hand-rolled its own halo,
 * tooltip-positioning and wait-for-the-modal loop, and `tourAutoplay.js`
 * fired one at you the first time you opened a tool — a walkthrough that
 * starts itself.
 *
 * Here a tour is data: a list of steps, each with the path it lives on, a
 * selector, and a sentence. One runner (`shell/Tour.tsx`) draws them, and
 * the offer to take one is an offer, not an ambush.
 *
 * Every selector is a `data-testid` or an `.fs-` class that already exists;
 * a step whose target never appears is skipped rather than fatal, so a
 * tour survives a screen that changes underneath it.
 */

export interface Step {
  /** Where this step lives. Omitted = wherever the previous step was. */
  route?: string;
  /** What to point at. */
  target: string;
  /** What to say about it. One or two sentences. */
  text: string;
}

export interface Tour {
  id: string;
  title: string;
  /** The path the tour starts on; also what `tourForPath` matches. */
  route: string;
  steps: Step[];
}

export const TOURS: Tour[] = [
  {
    id: 'demo',
    title: 'The whole tour',
    route: '/studio',
    steps: [
      { route: '/studio', target: '[data-testid="studio-composer"]', text: 'Everything starts here. Type, and this is a chat; the switches below decide how much more than a chat it is.' },
      { target: '[data-testid="studio-mode-agent"]', text: 'Chat only talks. Agent uses tools — reads files, runs commands, browses — and shows you every step it took.' },
      { target: '[data-testid="studio-model"]', text: 'The model of this conversation. Local or remote, one list.' },
      { target: '[data-testid="studio-knob-web"]', text: 'Web search, the terminal, your indexed documents, proposal mode: each one on its own, per conversation.' },
      { target: '[data-testid="studio-workspace"]', text: 'The folder the agent works in. With one bound, @file mentions a file and every change gets a checkpoint.' },
      { target: '[data-testid="vitals"]', text: 'What the machine is doing: GPU, VRAM, the model that is loaded. Click it for the detail.' },
      { route: '/projects', target: '[data-testid="projects-filter"]', text: 'A project ties a folder of chats to a folder on disk, its standing instructions and its own memory.' },
      { route: '/library', target: '.fs-tabs', text: 'The Library keeps what came out: documents, images, chats, research reports and the archive.' },
      { route: '/agents', target: '[data-testid="agents-tab-tournament"]', text: 'Agents: the workers board, the experts, and the tournament that makes several models compete on one prompt.' },
      { route: '/memory', target: '[data-testid="memory-new"]', text: 'What the assistant remembers about you, and the rules it has learned. Both editable, both deletable.' },
      { route: '/cookbook', target: '[data-testid="cookbook-fit"]', text: 'The Cookbook says what fits this machine, downloads it and launches it with the right engine.' },
      { route: '/settings', target: '.fs-set__nav', text: 'And Settings, where the endpoints, the appearance and the shortcuts live. That is the tour.' },
    ],
  },
  {
    id: 'tour-compare',
    title: 'Compare',
    route: '/compare',
    steps: [
      { route: '/compare', target: '[data-testid="compare-prompt"]', text: 'One prompt, several models, side by side. Write it here.' },
      { target: '.fs-cmp__slots', text: 'Each slot is a model — or a search provider, in Search mode. Add up to eight; they run in parallel unless you say otherwise.' },
      { target: '.fs-cmp__options', text: 'Blind mode hides the names until you have voted. That is the only honest way to compare.' },
      { target: '[data-testid="compare-pane"]', text: 'Every answer gets its own pane, with its time and its tokens under it, and a vote at the end.' },
    ],
  },
  {
    id: 'tour-cookbook',
    title: 'Cookbook',
    route: '/cookbook',
    steps: [
      { route: '/cookbook?t=fit', target: '[data-testid="cookbook-fit"]', text: 'What fits: your VRAM against every model, with the quantisation each one would need.' },
      { route: '/cookbook?t=models', target: '[data-testid="cookbook-models"]', text: 'What you already have, and the form that launches it with the right engine and flags.' },
      { route: '/cookbook?t=download', target: '[data-testid="cookbook-download"]', text: 'Pull a model from Ollama or Hugging Face, GGUF file by GGUF file if you want.' },
      { route: '/cookbook?t=running', target: '[data-testid="cookbook-running"]', text: 'What is up right now, its output, and a diagnosis when a launch fails.' },
      { route: '/cookbook?t=deps', target: '[data-testid="cookbook-deps"]', text: 'What each engine needs installed, and the recipe to install it.' },
      { route: '/cookbook?t=servers', target: '[data-testid="cookbook-servers"]', text: 'Other machines: add one over SSH and everything above works there too.' },
    ],
  },
  {
    id: 'tour-research',
    title: 'Deep Research',
    route: '/research',
    steps: [
      { route: '/research', target: '[data-testid="research-query"]', text: 'Ask a real question. Research does several rounds of searching and reading before it answers.' },
      { target: '[data-testid="research-running"]', text: 'While it runs you see the round, what it is reading and what it has found.' },
      { target: '[data-testid="research-recent"]', text: 'Every report is kept, with its sources, and lands in the Library.' },
    ],
  },
  {
    id: 'tour-library',
    title: 'Library',
    route: '/library',
    steps: [
      { route: '/library', target: '[data-testid="library-search"]', text: 'Everything that came out of a conversation lives here, and this searches all of it.' },
      { route: '/library?type=documento', target: '[data-testid="documents"]', text: 'Documents: the ones the agent wrote and the ones you imported. Opening one is a full editor.' },
      { route: '/library?type=chats', target: '[data-testid="chats-library"]', text: 'The conversations, with their folders, and the export that takes a whole folder as a zip.' },
      { route: '/library?type=historial', target: '[data-testid="history-library"]', text: 'And conversations imported from elsewhere: ChatGPT, Claude, another Faustus.' },
    ],
  },
  {
    id: 'tour-theme',
    title: 'Appearance',
    route: '/settings?s=appearance',
    steps: [
      { route: '/settings?s=appearance', target: '.fs-themes', text: 'A palette to start from. Any theme you saved in the previous interface is the same theme here.' },
      { target: '.fs-colors', text: 'Then every colour on its own. The whole interface follows these tokens, so nothing is left behind.' },
      { target: '.fs-harmony', text: 'Or let it build a palette from one colour, and pick the one you like.' },
    ],
  },
  {
    id: 'tour-settings',
    title: 'Settings',
    route: '/settings',
    steps: [
      { route: '/settings?s=models', target: '.fs-set__nav', text: 'Settings is one screen with sections down the side. Each has its own address.' },
      { target: '[data-testid="models-search"]', text: 'Models: the endpoints, local or remote, and everything they serve.' },
      { route: '/settings?s=integrations', target: '.fs-set__section', text: 'Integrations: MCP servers, mail, calendar, search providers.' },
      { route: '/settings?s=shortcuts', target: '.fs-set__section', text: 'And the keyboard shortcuts, which are the same ones the previous interface used.' },
    ],
  },
  {
    id: 'tour-gallery',
    title: 'Gallery',
    route: '/library?type=imagen',
    steps: [
      { route: '/library?type=imagen', target: '[data-testid="library-type-imagen"]', text: 'Every image the assistant made or you uploaded, newest first.' },
      { target: '[data-testid="gallery-card"]', text: 'Click one to look at it properly: tags, album, favourite, and send it back to a conversation.' },
      { target: '[data-testid="gallery-new-album"]', text: 'Albums are just a name. An image can be in one, or in none.' },
    ],
  },
  {
    id: 'tour-brain',
    title: 'Memory',
    route: '/memory',
    steps: [
      { route: '/memory', target: '[data-testid="memory-new"]', text: 'What the assistant remembers about you. Add one by hand, or let it pull them out of a conversation.' },
      { target: '[data-testid="memory-row"]', text: 'Every memory is editable and deletable. Nothing is remembered that you cannot see.' },
      { target: '[data-testid="memory-tab-provenance"]', text: 'And provenance: where each thing came from, as a graph you can walk.' },
    ],
  },
  {
    id: 'tour-task-1',
    title: 'Automations',
    route: '/automations',
    steps: [
      { route: '/automations', target: '[data-testid="automations-search"]', text: 'An automation is something that runs on its own: on a clock, on a webhook, or when you press it.' },
      { target: '[data-testid="automation-row"]', text: 'Each one shows its last run and whether it worked. Pausing one stops the clock without losing it.' },
    ],
  },
  {
    id: 'tour-task-2',
    title: 'Making an automation',
    route: '/automations',
    steps: [
      { route: '/automations', target: '[data-testid="automation-new"]', text: 'A new automation starts as a sentence: say what should happen and when.' },
      { target: '[data-testid="automation-sentence"]', text: 'The model turns it into a schedule and a prompt, and you get to correct both before it runs.' },
      { target: '[data-testid="automation-webhook"]', text: 'Or give it a webhook, and something else decides when it runs.' },
    ],
  },
];

const byId = new Map(TOURS.map((tour) => [tour.id, tour]));

export function tourById(id: string): Tour | null {
  return byId.get(id) ?? null;
}

/** The tour that belongs to a path, for the "first time here?" offer. */
export function tourForPath(pathname: string, search = ''): Tour | null {
  if (pathname.startsWith('/library') && search.includes('type=imagen')) return byId.get('tour-gallery') ?? null;
  if (pathname.startsWith('/settings') && search.includes('s=appearance')) return byId.get('tour-theme') ?? null;
  const exact = TOURS.find((tour) => tour.id !== 'demo' && tour.route.split('?')[0] === pathname);
  return exact ?? null;
}

/* ── What has already been offered ── */

const SEEN_KEY = 'faustus_studio_tours';

export function seenTours(): string[] {
  try {
    const raw = window.localStorage.getItem(SEEN_KEY);
    const list = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(list) ? list.map(String) : [];
  } catch {
    return [];
  }
}

export function markTourSeen(id: string): void {
  try {
    const list = new Set(seenTours());
    list.add(id);
    window.localStorage.setItem(SEEN_KEY, JSON.stringify([...list]));
  } catch {
    /* a remembered tour is not worth an exception */
  }
}

export function resetTours(): void {
  try {
    window.localStorage.removeItem(SEEN_KEY);
  } catch {
    /* nothing to do */
  }
}

/* ── Placement ── */

export interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

export interface Placed {
  top: number;
  left: number;
  side: 'below' | 'above' | 'right' | 'left';
}

/**
 * Below the target if it fits, else above, else beside it; then clamped
 * into the viewport. The same order the previous interface used, because it
 * is the right one: the eye is already below the thing it just read.
 */
export function placeTooltip(target: Box, card: { width: number; height: number }, viewport: { width: number; height: number }, gap = 12, margin = 10): Placed {
  const centre = target.left + target.width / 2 - card.width / 2;
  let side: Placed['side'];
  let top: number;
  let left: number;
  if (target.top + target.height + gap + card.height < viewport.height - margin) {
    side = 'below';
    top = target.top + target.height + gap;
    left = centre;
  } else if (target.top - gap - card.height > margin) {
    side = 'above';
    top = target.top - gap - card.height;
    left = centre;
  } else {
    top = target.top + target.height / 2 - card.height / 2;
    if (target.left + target.width + gap + card.width < viewport.width - margin) {
      side = 'right';
      left = target.left + target.width + gap;
    } else {
      side = 'left';
      left = target.left - card.width - gap;
    }
  }
  left = Math.min(Math.max(left, margin), Math.max(margin, viewport.width - card.width - margin));
  top = Math.min(Math.max(top, margin), Math.max(margin, viewport.height - card.height - margin));
  return { top, left, side };
}
