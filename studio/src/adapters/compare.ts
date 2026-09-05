import { ApiError, asArray } from './api';
import type { ModelRoute, TurnMetrics } from './chat';

/**
 * Compare: the same prompt to several models (or several search
 * providers) side by side, blind if you want, and a vote at the end. The
 * server has no compare state: each pane is an ordinary session streamed
 * with `compare_mode`, and votes live in this browser under the same key
 * the previous interface used, so the scoreboard keeps its history.
 */

export type CompareMode = 'chat' | 'agent' | 'search' | 'research';

export interface EvalPrompt {
  sub: string;
  label: string;
  answer?: string;
  prompt: string;
}

/** The evaluation prompts of the previous interface, per mode. Data, not UI. */
export const EVAL_PROMPTS: Record<CompareMode, EvalPrompt[]> = {
  chat: [
    { sub: "★ Featured", label: "Sum digits 2^100", answer: "115", prompt: "Compute the sum of the decimal digits of 2^100. Do NOT use code execution — work it out by reasoning about the number. Show every step, then end with the final number on its own line." },
    { sub: "★ Featured", label: "Three jugs", answer: "2 pours: 7→5, 7→3", prompt: "You have three jugs of capacities 7, 5, and 3 liters. The 7-liter jug starts full; the others empty. Using only pouring (no markings), produce the shortest sequence of pours that leaves exactly 2 liters in the 3-liter jug. Output each step as `pour A → B` on its own line. Then state the total number of pours on a final line." },
    { sub: "Visual", label: "Draw SVG", prompt: "Output a complete self-contained HTML file (```html block, no explanation, no other text) that centers a single SVG illustration on a simple background. The SVG must use only inline shapes — no <img>, no external assets, no JavaScript. Make it expressive and detailed. The SVG should depict: a friendly robot" },
    { sub: "Visual explain", label: "Black hole HTML", prompt: "Output a complete HTML file (```html block, no explanation outside the code) that visually explains how a black hole forms. Use four labeled \"frames\" laid out left-to-right (or stacked on small screens) showing: 1) a glowing massive star, 2) the star going supernova with shockwave rings, 3) collapse into a singularity, 4) the final black hole with a curved accretion disk and bent light around it. Use only vanilla HTML, CSS, and inline SVG — no JavaScript, no images. Each frame should have a one-sentence caption." },
    { sub: "Visual explain", label: "Butterfly ASCII", prompt: "Explain the butterfly lifecycle using ASCII art. Produce four separate frames in fenced code blocks, in order: egg, caterpillar, chrysalis, adult butterfly. Each frame must be drawn with monospace ASCII characters only and be visually recognizable as the creature/stage. Below each frame add one playful one-line caption (no longer than 15 words) describing what is happening at that stage." },
    { sub: "Algorithms", label: "LRU cache", prompt: "Implement an LRU cache with O(1) get and put operations. Support a configurable max capacity. Write it in any language with full comments." },
    { sub: "Debugging", label: "Race condition", prompt: "This Go code has a race condition. Find it, explain why it happens, and fix it:\n\nvar counter int\nfunc increment(wg *sync.WaitGroup) {\n    defer wg.Done()\n    for i := 0; i < 1000; i++ {\n        counter++\n    }\n}" },
    { sub: "Debugging", label: "Security review", prompt: "Review this code for bugs, security issues, and performance problems:\n\napp.get(\"/user/:id\", (req, res) => {\n  const query = `SELECT * FROM users WHERE id = ${req.params.id}`;\n  db.query(query, (err, result) => {\n    res.json(result[0]);\n  });\n});" },
    { sub: "Architecture", label: "URL shortener", prompt: "Design a URL shortener service. Cover the API, database schema, and how you would handle 1000 requests per second." },
    { sub: "Refactoring", label: "Clean up", prompt: "Refactor this code to be more idiomatic and efficient:\n\nresults = []\nfor i in range(len(data)):\n    if data[i][\"status\"] == \"active\":\n        if data[i][\"score\"] > 50:\n            results.append(data[i][\"name\"].upper())" },
  ],
  agent: [
    { sub: "Code tasks", label: "Script + run", prompt: "Write a Python script that generates a bar chart of the 5 most common programming languages in 2025 and save it as chart.png. Then run it." },
    { sub: "Math", label: "Proof + verify", prompt: "Prove that the square root of 2 is irrational. Then write a Python program that approximates it using Newton's method to 50 decimal places and verify." },
    { sub: "Games", label: "Snake", prompt: "Output a complete HTML file (```html block) for a Snake game. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, neon green snake on dark grid, glowing food, score counter, speed increases, game over + restart. Skip any explanation, just output the code." },
    { sub: "Games", label: "Breakout", prompt: "Output a complete HTML file (```html block) for a Breakout brick breaker game. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, colorful gradient brick rows, glowing paddle, ball with trail, score + lives, particle explosions on break. Skip any explanation, just output the code." },
    { sub: "Animation", label: "Solar system", prompt: "Output a complete HTML file (```html block) for an animated solar system. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, glowing Sun center, 8 planets orbiting at correct relative speeds with real colors, orbit trails, starfield background, labels on hover. Skip any explanation, just output the code." },
    { sub: "Animation", label: "Matrix rain", prompt: "Output a complete HTML file (```html block) for the Matrix digital rain effect. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Full-screen canvas, green katakana characters falling at varying speeds, glowing heads, fading trails, scan-line overlay. Skip any explanation, just output the code." },
    { sub: "Generative", label: "Fractal tree", prompt: "Output a complete HTML file (```html block) for an interactive fractal tree. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, tree grows from bottom with recursive branches, sliders for angle/depth/length/wind, gradient colors from brown trunk to green leaves. Skip any explanation, just output the code." },
  ],
  search: [
    { sub: "Factual", label: "Current events", prompt: "latest AI regulation news 2025" },
    { sub: "Technical", label: "Programming", prompt: "Rust vs Go performance benchmarks 2025" },
    { sub: "Research", label: "Academic", prompt: "transformer architecture improvements since attention is all you need" },
    { sub: "Comparison", label: "GPU providers", prompt: "cloud GPU providers pricing comparison 2025" },
    { sub: "Factual", label: "Science", prompt: "CRISPR gene therapy breakthroughs" },
  ],
  research: [
  ],
};

export const MODE_LABEL: Record<CompareMode, string> = { chat: 'Chat', agent: 'Agent', search: 'Search', research: 'Research' };
export const MODE_HELP: Record<CompareMode, string> = {
  chat: 'Plain answers: no tools, no memory, no documents. The fairest test of the model itself.',
  agent: 'Tools on (terminal, web): who gets the job done, not who writes nicest.',
  search: 'The same query to several search providers, then a model summarises each result set.',
  research: 'Each model runs Deep Research on the question before answering. Slow, thorough.',
};

export const MAX_PANES = 8;
export const VOTES_KEY = 'odysseus-compare-votes';
const VOTES_MAX = 500;
const EXCLUDED_KEY = 'odysseus-compare-excluded';
const OPTIONS_KEY = 'fs-compare-options';

export interface Vote {
  models: string[];
  winner: string;
  prompt: string;
  blind: boolean;
  mode: CompareMode;
  timestamp: number;
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode */
  }
}

export function loadVotes(): Vote[] {
  return readJson<Record<string, unknown>[]>(VOTES_KEY, []).map((v) => ({
    models: asArray<unknown>(v.models).map(String),
    winner: String(v.winner ?? ''),
    prompt: String(v.prompt ?? ''),
    blind: Boolean(v.blind),
    mode: (['chat', 'agent', 'search', 'research'].includes(String(v.mode)) ? String(v.mode) : 'chat') as CompareMode,
    timestamp: Number(v.timestamp) || 0,
  }));
}

export function saveVote(v: Vote): void {
  const votes = readJson<unknown[]>(VOTES_KEY, []);
  votes.push(v);
  if (votes.length > VOTES_MAX) votes.splice(0, votes.length - VOTES_MAX);
  writeJson(VOTES_KEY, votes);
}

export function clearVotes(): void {
  writeJson(VOTES_KEY, []);
}

export interface ScoreRow {
  model: string;
  wins: number;
  losses: number;
  ties: number;
  matches: number;
}

/** Wins, losses and ties per model, for one mode. */
export function scoreboard(votes: Vote[], mode: CompareMode | 'all'): ScoreRow[] {
  const rows = new Map<string, ScoreRow>();
  const row = (m: string) => {
    let r = rows.get(m);
    if (!r) {
      r = { model: m, wins: 0, losses: 0, ties: 0, matches: 0 };
      rows.set(m, r);
    }
    return r;
  };
  for (const v of votes) {
    if (mode !== 'all' && v.mode !== mode) continue;
    for (const m of v.models) {
      const r = row(m);
      r.matches += 1;
      if (v.winner === 'tie') r.ties += 1;
      else if (v.winner === m) r.wins += 1;
      else r.losses += 1;
    }
  }
  return [...rows.values()].sort((a, b) => b.wins - a.wins || a.losses - b.losses || b.matches - a.matches);
}

export function getExcluded(): string[] {
  return readJson<string[]>(EXCLUDED_KEY, []);
}

export function setExcluded(ids: string[]): void {
  writeJson(EXCLUDED_KEY, ids);
}

export interface CompareOptions {
  mode: CompareMode;
  blind: boolean;
  parallel: boolean;
  timeout: number;
  keepSessions: boolean;
  /** Route ids per slot, per mode. */
  slots: Partial<Record<CompareMode, string[]>>;
  /** Synthesis model (route id) for search mode. */
  synthRoute: string;
}

export const DEFAULT_OPTIONS: CompareOptions = { mode: 'chat', blind: true, parallel: true, timeout: 300, keepSessions: false, slots: {}, synthRoute: '' };

export function loadOptions(): CompareOptions {
  return { ...DEFAULT_OPTIONS, ...readJson<Partial<CompareOptions>>(OPTIONS_KEY, {}) };
}

export function saveOptions(o: CompareOptions): void {
  writeJson(OPTIONS_KEY, o);
}

export const slotChar = (i: number, parallel: boolean) => (parallel ? String.fromCharCode(65 + i) : String(i + 1));

export function routeLabel(r: ModelRoute | null): string {
  if (!r) return '';
  return r.endpointName && r.endpointName !== r.model ? `${r.model} · ${r.endpointName}` : r.model;
}

/* ── Search mode ── */

export interface SearchHit {
  title: string;
  url: string;
  snippet: string;
}

export async function searchWith(provider: string, query: string, signal?: AbortSignal): Promise<{ hits: SearchHit[]; error: string; ms: number }> {
  const fd = new FormData();
  fd.append('query', query);
  fd.append('provider', provider);
  fd.append('count', '10');
  const t0 = performance.now();
  const res = await fetch('/api/search/query', { method: 'POST', body: fd, credentials: 'same-origin', signal });
  if (!res.ok) throw new ApiError(`search/query responded ${res.status}`, res.status);
  const data = (await res.json()) as { results?: Record<string, unknown>[]; error?: string };
  return {
    hits: (data.results ?? []).map((r) => ({ title: String(r.title ?? r.url ?? ''), url: String(r.url ?? ''), snippet: String(r.snippet ?? r.content ?? r.description ?? '') })).filter((h) => h.url),
    error: typeof data.error === 'string' ? data.error : '',
    ms: Math.round(performance.now() - t0),
  };
}

export function synthesisPrompt(query: string, hits: SearchHit[]): string {
  const list = hits
    .slice(0, 10)
    .map((h, i) => `${i + 1}. ${h.title}\n   ${h.url}\n   ${h.snippet}`)
    .join('\n');
  return `Analyze these search results for the query "${query}". Summarize the key findings, note any consensus or conflicting information, and say which results look most trustworthy. Be concise.\n\nResults:\n${list}`;
}

/* ── Grading an expected answer ── */

/** Loose match: the expected answer's digits/words appear at the end of the response. */
export function gradeAnswer(response: string, expected: string): 'pass' | 'fail' | null {
  const exp = expected.trim().toLowerCase();
  if (!exp) return null;
  const tail = response.trim().toLowerCase().slice(-600);
  if (tail.includes(exp)) return 'pass';
  const numbers = exp.match(/\d+/g);
  if (numbers && numbers.every((n) => tail.includes(n))) return 'pass';
  return 'fail';
}

export function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function metricsLine(m: TurnMetrics | undefined, ms: number): string {
  const parts: string[] = [];
  if (ms) parts.push(formatMs(ms));
  if (m?.outputTokens) parts.push(`${m.outputTokens} tok`);
  if (m?.tokensPerSecond) parts.push(`${m.tokensPerSecond.toFixed(1)} tok/s`);
  return parts.join(' · ');
}

/** The model list for a pane's swap menu: what `/api/models` knows, minus the excluded pool. */
export function eligibleRoutes(routes: ModelRoute[], excluded: string[]): ModelRoute[] {
  const ex = new Set(excluded);
  return routes.filter((r) => !ex.has(r.id));
}

export async function probeRoutes(routes: ModelRoute[]): Promise<Record<string, boolean>> {
  try {
    const res = await fetch('/api/probe-selected', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ models: routes.map((r) => ({ endpoint_id: r.endpointId, model: r.model })) }),
    });
    if (!res.ok) return {};
    const out = (await res.json()) as { results?: Record<string, unknown>[] };
    const map: Record<string, boolean> = {};
    for (const r of out.results ?? []) {
      const model = String(r.model ?? '');
      const ok = r.status === 'ok' || r.status === 'success' || r.ok === true;
      for (const route of routes) if (route.model === model) map[route.id] = ok;
    }
    return map;
  } catch {
    return {};
  }
}
