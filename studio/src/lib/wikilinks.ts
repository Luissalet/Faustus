/**
 * Wiki links, for the note vault reader/editor.
 *
 * Pure logic only — no DOM, no fetch, no React — so it can be exercised
 * directly by `studio/checks/brain.check.mjs` (bundled with esbuild and run
 * under node) the same way `lib/markdown.ts` is. Three independent jobs live
 * here:
 *
 *   - parsing `[[target]]` / `[[target|label]]` / `![[target]]` occurrences
 *     out of a note body (the shape Lot A's `wikilinks.parse` in Python
 *     returns, mirrored here for the client);
 *   - rewriting a body so the shared markdown parser (`lib/markdown.ts`)
 *     turns each occurrence into a real link or image node, resolved or not,
 *     WITHOUT changing that parser at all — the trick is an href/src the
 *     parser's own `safeHref`/`safeSrc` already lets straight through (see
 *     `wikiHref` below);
 *   - the quick switcher's fuzzy match over note titles.
 */

export interface WikiLinkMatch {
  /** The raw text between the brackets, before the `|label` split. */
  target: string;
  /** `target#Heading` splits here; '' when there is no heading. */
  heading: string;
  /** The `|label` half, or the target itself when there is none. */
  label: string;
  /** `![[…]]` rather than `[[…]]`. */
  isEmbed: boolean;
  /** Position in the source string, for callers that need to slice around it. */
  start: number;
  end: number;
}

/** `[[Target]]`, `[[Target|Label]]`, `[[Target#Heading]]`, and the `!`-prefixed
 *  embed form of each. Deliberately simple: a target never contains `]` or
 *  `|`, matching the vault's own basename-based link convention. */
const WIKILINK = /(!)?\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]/g;

export function parseWikilinks(body: string): WikiLinkMatch[] {
  const out: WikiLinkMatch[] = [];
  for (const m of body.matchAll(WIKILINK)) {
    const target = (m[2] ?? '').trim();
    if (!target) continue;
    const heading = (m[3] ?? '').trim();
    const label = (m[4] ?? '').trim();
    out.push({
      target,
      heading,
      label: label || target,
      isEmbed: m[1] === '!',
      start: m.index ?? 0,
      end: (m.index ?? 0) + m[0].length,
    });
  }
  return out;
}

/** Every distinct link target a body names, in first-seen order — what a
 *  note's outgoing-links panel and the import-side backlink index both need. */
export function linkTargets(body: string): string[] {
  const seen: string[] = [];
  for (const link of parseWikilinks(body)) if (!seen.includes(link.target)) seen.push(link.target);
  return seen;
}

/**
 * A note's title as a vault-relative path, resolved by whatever titles the
 * caller currently knows about — the FUNCTION, not the tree, so both the
 * reading view and the editor's autocomplete share the exact same rule for
 * "does this title exist".
 */
export type TitleIndex = Map<string, { path: string; title: string }>;

/** Case/diacritic-insensitive fold, matching the server's `fold()` closely
 *  enough for the client-side index to agree with it on the common case. */
export function foldTitle(value: string): string {
  return value
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .trim()
    .toLowerCase();
}

export function buildTitleIndex(notes: Array<{ path: string; title: string }>): TitleIndex {
  // Same rule as the server: when two notes share a title, the first path in
  // sorted order wins, so a click and a server-side link resolve alike.
  const index: TitleIndex = new Map();
  const sorted = [...notes].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  for (const note of sorted) {
    const key = foldTitle(note.title);
    if (!index.has(key)) index.set(key, { path: note.path, title: note.title });
  }
  return index;
}

export function resolveTitle(index: TitleIndex, target: string): { path: string; title: string } | null {
  return index.get(foldTitle(target)) ?? null;
}

/** `encodeURIComponent` deliberately leaves `( ) ! * ' ~` unescaped — they
 *  are valid in a URI component by spec — but the shared markdown parser's
 *  link-target regex (`lib/markdown.ts`, `[^\s)]*`) ends a URL at the very
 *  first unescaped `)`. A vault path built from a note's title routinely
 *  has one: every memory note is titled `<Title> (<id8>).md`, and a free
 *  note can be titled anything a person types, parentheses included. Left
 *  unescaped, `Rust (794da9ce).md` truncates the href at `Rust (794da9ce`
 *  and spills `).md)` out as plain text right after the link. Percent-encode
 *  `(`/`)` on top of whatever `encodeURIComponent` already escapes;
 *  `decodeURIComponent` undoes a `%28`/`%29` exactly like any other escape,
 *  so nothing on the decode side has to change. */
function encodeHrefPath(path: string): string {
  return encodeURIComponent(path).replace(/[()]/g, (c) => (c === '(' ? '%28' : '%29'));
}

/** The scheme `lib/markdown.ts#safeHref` leaves untouched: it only rewrites a
 *  Windows path or a `file:line` reference, and turns any *other* scheme it
 *  does not recognise (`mailto:`/`http(s):` excepted) into `#`. A hash
 *  fragment matches none of those cases and is returned byte-for-byte, so a
 *  `#brain-note=…` href survives the shared parser untouched — this module
 *  never has to fork or modify it. */
export function wikiHref(path: string, resolved: boolean): string {
  const tag = resolved ? 'brain-note' : 'brain-note-new';
  return `#${tag}=${encodeHrefPath(path)}`;
}

export function wikiEmbedSrc(path: string, resolved: boolean): string {
  const tag = resolved ? 'brain-embed' : 'brain-embed-new';
  return `#${tag}=${encodeHrefPath(path)}`;
}

export interface WikiHrefInfo {
  kind: 'note' | 'note-new' | 'embed' | 'embed-new';
  path: string;
}

/** The inverse of `wikiHref`/`wikiEmbedSrc` — what the reading view's click
 *  handler and embed-card renderer decode back out of a link/image node. */
export function parseWikiHref(href: string): WikiHrefInfo | null {
  const m = /^#(brain-note-new|brain-note|brain-embed-new|brain-embed)=(.*)$/.exec(href);
  if (!m) return null;
  const kind = m[1] === 'brain-note-new' ? 'note-new' : m[1] === 'brain-note' ? 'note' : m[1] === 'brain-embed-new' ? 'embed-new' : 'embed';
  try {
    return { kind, path: decodeURIComponent(m[2]) };
  } catch {
    return null;
  }
}

/**
 * Turn `[[Target|Label]]` into a markdown link the shared parser already
 * understands, `![[Target]]` into a markdown image (the reading view turns
 * that into an embed card, see `screens/brain/NoteMarkdown.tsx`), leaving
 * every other character of the body untouched. Runs once per render, before
 * `parseMarkdown`.
 */
export function rewriteWikilinks(body: string, index: TitleIndex): string {
  let out = '';
  let last = 0;
  for (const link of parseWikilinks(body)) {
    out += body.slice(last, link.start);
    const resolvedNote = resolveTitle(index, link.target);
    const path = resolvedNote?.path ?? link.target;
    if (link.isEmbed) {
      out += `![${escapeLabel(link.label)}](${wikiEmbedSrc(path, Boolean(resolvedNote))})`;
    } else {
      out += `[${escapeLabel(link.label)}](${wikiHref(path, Boolean(resolvedNote))})`;
    }
    last = link.end;
  }
  out += body.slice(last);
  return out;
}

/** A label must not itself look like it closes the `]` early. */
function escapeLabel(label: string): string {
  return label.replace(/\]/g, '\\]');
}

/* ── `[[` autocomplete while typing in the editor ───────────────────────── */

export interface WikiAutocomplete {
  /** Index of the `[[` that opened this span. */
  start: number;
  /** Index right after the caret — where the inserted title is spliced in. */
  end: number;
  query: string;
}

/**
 * Is the caret inside an unfinished `[[query`? Only ever true up to the
 * first `]`, newline or a closing `]]` already typed — so autocomplete never
 * fires again once a link is already finished and the caret merely sits
 * inside its label.
 */
export function activeWikiAutocomplete(text: string, caret: number): WikiAutocomplete | null {
  const before = text.slice(0, caret);
  const open = before.lastIndexOf('[[');
  if (open < 0) return null;
  const between = before.slice(open + 2);
  if (between.includes(']') || between.includes('\n') || between.includes('[[')) return null;
  const pipe = between.indexOf('|');
  const query = pipe >= 0 ? between.slice(0, pipe) : between;
  return { start: open, end: caret, query };
}

/** Splice a chosen title into an in-progress `[[query` span. */
export function applyWikiAutocomplete(text: string, autocomplete: WikiAutocomplete, title: string): { text: string; caret: number } {
  const insert = `[[${title}]]`;
  const next = text.slice(0, autocomplete.start) + insert + text.slice(autocomplete.end);
  return { text: next, caret: autocomplete.start + insert.length };
}

/* ── Fuzzy match: the quick switcher (Ctrl+O) and the autocomplete list ── */

export interface FuzzyHit<T> {
  item: T;
  score: number;
}

/**
 * A subsequence match, scored so that an exact prefix beats a scattered
 * subsequence, and a scattered subsequence still beats nothing: every
 * character of `query` must occur in `text`, in order, but not necessarily
 * adjacent. Deterministic and cheap enough to run over the whole vault on
 * every keystroke.
 */
export function fuzzyScore(query: string, text: string): number {
  const q = foldTitle(query);
  const t = foldTitle(text);
  if (!q) return 1;
  if (t === q) return 1000;
  if (t.startsWith(q)) return 500 - t.length * 0.01;
  const at = t.indexOf(q);
  if (at >= 0) return 300 - at * 2 - t.length * 0.01;

  let ti = 0;
  let score = 0;
  let streak = 0;
  for (let qi = 0; qi < q.length; qi += 1) {
    const ch = q[qi];
    const found = t.indexOf(ch, ti);
    if (found < 0) return -1;
    streak = found === ti ? streak + 1 : 1;
    score += 10 + streak * 2 - (found - ti);
    ti = found + 1;
  }
  return score - t.length * 0.02;
}

export function fuzzySearch<T>(query: string, items: T[], text: (item: T) => string, limit = 20): T[] {
  if (!query.trim()) return items.slice(0, limit);
  const hits: FuzzyHit<T>[] = [];
  for (const item of items) {
    const score = fuzzyScore(query, text(item));
    if (score >= 0) hits.push({ item, score });
  }
  hits.sort((a, b) => b.score - a.score);
  return hits.slice(0, limit).map((h) => h.item);
}
