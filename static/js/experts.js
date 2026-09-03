// static/js/experts.js
// Experts — a specialist with its OWN corpus, and the review panel that shows
// what it found.
//
// The backend is authoritative (services/experts.py, routes/expert_routes.py):
// the profile, the corpus folder, the chunk index that remembers which PAGE a
// chunk came from, and the accepted/rejected counters all live on disk. This
// module is the human end of it — a gallery, an editor, and the track-changes
// panel for a review result (src/expert_review.py).
//
// Two honesty rules run through the whole file and are not negotiable:
//
//   * a correction's `label` is printed VERBATIM — it is either "corpus" or
//     "model's opinion, not the corpus" and the renderer never re-derives it
//     from `anchored`; and
//   * a page number is never invented. A citation without a page renders
//     "page unknown", never a number that reads plausible.
//
// The renderers are pure and live between the marked region below, so
// tests/test_experts_page_js.py can run them in bare node.

import uiModule from './ui.js';
import { registerMentionSource } from './fileMentions.js';

const API = `${window.location.origin}/api/experts`;

// ── Experts: pure helpers (dependency-free; extracted and run under node by tests) ──
// Everything between these markers must stay free of DOM, module and window
// references so tests/test_experts_page_js.py can execute it in bare node.

/** Local escape: same table as ui.js esc(), but import-free for tests. */
function expEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** Sanitize a server-provided word (severity, op, label) for use in a class. */
function expToken(value) {
  return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

/** A number, or `fallback` when the value is not one. Never NaN. */
function expNum(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** An integer offset, or null when the value cannot be one (never a guess). */
function expInt(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

function expBytes(value) {
  const size = expNum(value, 0);
  if (size < 1024) return `${Math.round(size)} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10240 ? 1 : 0)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

const EXP_SEVERITIES = ['low', 'medium', 'high'];
const EXP_SORTS = ['name', 'corpus', 'accepted'];

/** One roster row (services/experts.summary), with every field defaulted. */
function normalizeExpert(raw) {
  const row = raw && typeof raw === 'object' ? raw : {};
  return {
    slug: String(row.slug == null ? '' : row.slug),
    name: String(row.name || row.slug || ''),
    description: String(row.description || ''),
    model: String(row.model || ''),
    enabled: row.enabled === undefined ? true : Boolean(row.enabled),
    owner: String(row.owner || ''),
    corpus_files: expNum(row.corpus_files, 0),
    chunks: expNum(row.chunks, 0),
    indexed_at: row.indexed_at || null,
    invocations: expNum(row.invocations, 0),
    accepted: expNum(row.accepted, 0),
    rejected: expNum(row.rejected, 0),
    updated_at: row.updated_at || '',
  };
}

/** GET /api/experts: {"experts":[…]}, a bare list, or a {"data": …} wrapper. */
function normalizeExperts(raw) {
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (raw && typeof raw === 'object') {
    if (Array.isArray(raw.experts)) list = raw.experts;
    else if (Array.isArray(raw.items)) list = raw.items;
    else if (Array.isArray(raw.data)) list = raw.data;
    else if (raw.data && typeof raw.data === 'object' && Array.isArray(raw.data.experts)) list = raw.data.experts;
  }
  return list
    .filter(row => row && typeof row === 'object' && row.slug != null && String(row.slug))
    .map(normalizeExpert);
}

/** Accept both a bare profile and an {"expert": …} wrapper. */
function unwrapExpert(raw) {
  if (raw && typeof raw === 'object' && raw.expert && typeof raw.expert === 'object') return raw.expert;
  return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
}

/** `[{name, bytes, modified, pages, chunks}]` — a wrapper or a bare list. */
function normalizeCorpusFiles(raw) {
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (raw && typeof raw === 'object' && Array.isArray(raw.files)) list = raw.files;
  return list.filter(row => row && typeof row === 'object' && row.name).map(row => ({
    name: String(row.name),
    bytes: expNum(row.bytes, 0),
    modified: expNum(row.modified, 0),
    // pages stays null when the file's pages could not be determined.
    pages: expInt(row.pages),
    chunks: expNum(row.chunks, 0),
    indexed_at: row.indexed_at || null,
  }));
}

/** GET /api/experts/{slug}: the profile + its corpus, defaulted throughout. */
function normalizeDetail(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const profile = unwrapExpert(data);
  const usage = data.usage && typeof data.usage === 'object' ? data.usage : {};
  return {
    expert: {
      slug: String(profile.slug == null ? '' : profile.slug),
      name: String(profile.name || profile.slug || ''),
      description: String(profile.description || ''),
      model: String(profile.model || ''),
      temperature: expNum(profile.temperature, 0.2),
      top_p: expNum(profile.top_p, 1),
      enabled: profile.enabled === undefined ? true : Boolean(profile.enabled),
      instructions: String(profile.instructions || ''),
      rubric: rubricLines(profile.rubric),
      owner: String(profile.owner || ''),
      updated_at: profile.updated_at || '',
    },
    usage: {
      invocations: expNum(usage.invocations, 0),
      accepted: expNum(usage.accepted, 0),
      rejected: expNum(usage.rejected, 0),
      last_used: usage.last_used || null,
    },
    files: normalizeCorpusFiles(data.files),
    chunks: expNum(data.chunks, 0),
    indexed_at: data.indexed_at || null,
    collection: String(data.collection || ''),
  };
}

/** A rubric may arrive as a list or as the textarea's newline-separated text. */
function rubricLines(value) {
  if (Array.isArray(value)) {
    return value.map(item => String(item == null ? '' : item).trim()).filter(Boolean);
  }
  return String(value == null ? '' : value)
    .split('\n')
    .map(line => line.replace(/^\s*(?:[-*]|\d+[.)])\s+/, '').trim())
    .filter(Boolean);
}

const rubricText = (value) => rubricLines(value).join('\n');

/** GET /{slug}/search: hits + the tier that answered. `degraded` is not an error. */
function normalizeSearch(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const hits = Array.isArray(data.hits) ? data.hits : [];
  return {
    query: String(data.query || ''),
    tier: String(data.tier || 'lexical'),
    degraded: Boolean(data.degraded),
    hits: hits.filter(hit => hit && typeof hit === 'object').map(hit => ({
      chunk_id: String(hit.chunk_id || ''),
      source: String(hit.source || ''),
      page: expInt(hit.page),           // null when the page is unknown
      start_line: expNum(hit.start_line, 0),
      end_line: expNum(hit.end_line, 0),
      score: expNum(hit.score, 0),
      text: String(hit.text || ''),
    })),
  };
}

/**
 * "page 42" or "page unknown" — NEVER a number that was not extracted.
 * A `page_label` supplied by the backend wins verbatim.
 */
function pageLabelOf(row) {
  const item = row && typeof row === 'object' ? row : {};
  const given = String(item.page_label == null ? '' : item.page_label).trim();
  if (given) return given;
  const page = expInt(item.page);
  return page == null ? 'page unknown' : `page ${page}`;
}

/** "source, page N" for a hit or a citation, with the same page honesty. */
function refOf(row) {
  const item = row && typeof row === 'object' ? row : {};
  const given = String(item.ref == null ? '' : item.ref).trim();
  if (given) return given;
  const source = String(item.source || item.chunk_id || 'unknown source');
  return `${source}, ${pageLabelOf(item)}`;
}

const corpusUrl = (slug, filename) =>
  `/api/experts/${encodeURIComponent(String(slug || ''))}/corpus/${encodeURIComponent(String(filename || ''))}`;

/** name | corpus size | accepted corrections. Unknown modes sort by name. */
function sortExperts(list, mode) {
  const rows = (list || []).slice();
  const byName = (a, b) => String((a && a.name) || '').localeCompare(String((b && b.name) || ''));
  if (mode === 'corpus') {
    return rows.sort((a, b) => (expNum(b && b.chunks) - expNum(a && a.chunks)) || byName(a, b));
  }
  if (mode === 'accepted') {
    return rows.sort((a, b) => (expNum(b && b.accepted) - expNum(a && a.accepted)) || byName(a, b));
  }
  return rows.sort(byName);
}

/** Substring match over the fields a person would search by. */
function filterExperts(list, query) {
  const needle = String(query == null ? '' : query).trim().toLocaleLowerCase();
  if (!needle) return (list || []).slice();
  return (list || []).filter(row => [row && row.name, row && row.slug, row && row.description, row && row.model]
    .some(value => String(value == null ? '' : value).toLocaleLowerCase().includes(needle)));
}

// ── gallery ───────────────────────────────────────────────────────────────

function expertCardHtml(raw) {
  const row = normalizeExpert(raw);
  const description = row.description || 'No description yet — say what this expert knows.';
  return `
    <div class="exp-card-wrap">
      <button type="button" class="exp-card${row.enabled ? '' : ' is-off'}" data-exp-open="${expEsc(row.slug)}">
        <span class="exp-card-head">
          <strong class="exp-card-name">${expEsc(row.name)}</strong>
          ${row.enabled ? '' : '<span class="exp-card-off">off</span>'}
        </span>
        <span class="exp-card-desc">${expEsc(description)}</span>
        <span class="exp-card-meta">
          <span class="exp-card-model" title="Model this expert reviews with">${expEsc(row.model || 'auto')}</span>
          <span title="Corpus files">${row.corpus_files} file${row.corpus_files === 1 ? '' : 's'}</span>
          <span title="Indexed chunks">${row.chunks} chunk${row.chunks === 1 ? '' : 's'}</span>
        </span>
        <span class="exp-card-counters">
          <span class="exp-count-ok" title="Corrections accepted">✓ ${row.accepted}</span>
          <span class="exp-count-no" title="Corrections rejected">✕ ${row.rejected}</span>
        </span>
      </button>
      <button type="button" class="exp-card-delete" data-exp-delete="${expEsc(row.slug)}" aria-label="Delete ${expEsc(row.name)}" title="Delete expert">✕</button>
    </div>`;
}

function expertsGalleryHtml(rows, opts = {}) {
  const state = opts || {};
  const all = (rows || []).map(normalizeExpert);
  const visible = sortExperts(filterExperts(all, state.query), state.sort);
  const sortOptions = EXP_SORTS
    .map(mode => `<option value="${mode}"${mode === (state.sort || 'name') ? ' selected' : ''}>${
      mode === 'name' ? 'Name' : mode === 'corpus' ? 'Corpus size' : 'Accepted'}</option>`).join('');
  const cards = state.loading
    ? '<div class="exp-empty">Loading experts…</div>'
    : (visible.map(expertCardHtml).join('') || `<div class="exp-empty">${
      all.length ? 'No expert matches that search.'
        : 'No experts yet — create one, then drop its books into the corpus.'}</div>`);
  const off = state.enabled === false
    ? '<p class="exp-note">Experts are switched off in Settings (<code>agent_experts</code>): nothing is injected into a turn. Everything here still edits fine.</p>'
    : '';
  return `
    <div class="exp-head">
      <div>
        <h2 class="exp-title">Experts</h2>
        <p class="exp-desc">A specialist with its own corpus: your books, indexed by page, and corrections that cite the page they came from.</p>
      </div>
      <div class="exp-toolbar">
        <input type="search" class="exp-search" data-exp-query placeholder="Search experts" aria-label="Search experts" value="${expEsc(state.query || '')}" spellcheck="false" />
        <select class="exp-sort" data-exp-sort aria-label="Sort experts">${sortOptions}</select>
        <button type="button" class="exp-btn exp-btn-primary" data-exp-new>New expert</button>
        <button type="button" class="exp-btn" data-exp-open-review>Review panel</button>
      </div>
    </div>
    ${off}
    <p class="exp-error" data-exp-error${state.error ? '' : ' hidden'}>${expEsc(state.error || '')}</p>
    <div class="exp-grid">${cards}</div>`;
}

// ── detail: corpus, search, block ─────────────────────────────────────────

function corpusFileRowHtml(file, slug) {
  const row = normalizeCorpusFiles([file])[0];
  if (!row) return '';
  const pages = row.pages == null ? 'pages unknown' : `${row.pages} page${row.pages === 1 ? '' : 's'}`;
  return `
    <li class="exp-file">
      <a class="exp-file-name" href="${expEsc(corpusUrl(slug, row.name))}" target="_blank" rel="noopener">${expEsc(row.name)}</a>
      <span class="exp-file-meta">${expEsc(expBytes(row.bytes))} · ${expEsc(pages)} · ${row.chunks} chunk${row.chunks === 1 ? '' : 's'}</span>
      <button type="button" class="exp-file-del" data-exp-file-delete="${expEsc(row.name)}" aria-label="Delete ${expEsc(row.name)} from the corpus" title="Delete from the corpus">✕</button>
    </li>`;
}

/** POST /{slug}/reindex → {indexed, skipped, removed, chunks, seconds}. */
function reindexSummaryHtml(result) {
  if (!result || typeof result !== 'object') return '';
  const cell = (key) => `<span><b>${expNum(result[key], 0)}</b> ${key}</span>`;
  const seconds = expNum(result.seconds, 0);
  return `<span class="exp-reindex-out">${['indexed', 'skipped', 'removed', 'chunks'].map(cell).join('')}<span><b>${seconds.toFixed(2)}s</b></span></span>`;
}

function searchHitsHtml(payload, slug) {
  const data = normalizeSearch(payload);
  if (!data.hits.length) {
    return `<p class="exp-empty">Nothing in this corpus matches ${data.query ? `“${expEsc(data.query)}”` : 'that'}.</p>`;
  }
  const degraded = data.degraded
    ? '<p class="exp-note">Lexical only — this expert has no embedding collection yet. The hits below are real, the ranking is just simpler.</p>'
    : '';
  const rows = data.hits.map(hit => `
    <li class="exp-hit">
      <a class="exp-hit-ref" href="${expEsc(corpusUrl(slug, hit.source))}" target="_blank" rel="noopener">${expEsc(`${hit.source || 'unknown source'}, ${pageLabelOf(hit)}`)}</a>
      <span class="exp-hit-score" title="Score (${expEsc(data.tier)})">${hit.score.toFixed(3)}</span>
      <p class="exp-hit-text">${expEsc(hit.text.slice(0, 400))}</p>
    </li>`).join('');
  return `${degraded}<ul class="exp-hits">${rows}</ul>`;
}

/** GET /{slug}/block → the EXACT text the model would be handed. */
function blockPreviewHtml(block) {
  const data = block && typeof block === 'object' ? block : {};
  const text = String(data.text || '');
  if (!text) return '<p class="exp-empty">The block is empty for that query — the model would be given nothing from this corpus.</p>';
  const ids = Array.isArray(data.chunk_ids) ? data.chunk_ids.length : 0;
  const chars = expNum(data.chars, text.length);
  const budget = expNum(data.budget, 0);
  return `<p class="exp-note">${chars} of ${budget} chars · ${ids} chunk${ids === 1 ? '' : 's'}${data.degraded ? ' · lexical only' : ''}</p>
    <pre class="exp-block">${expEsc(text)}</pre>`;
}

function expertDetailHtml(detail, opts = {}) {
  const state = opts || {};
  const data = normalizeDetail(detail);
  const expert = data.expert;
  const files = data.files;
  const total = files.reduce((sum, file) => sum + file.bytes, 0);
  return `
    <div class="exp-detail-head">
      <button type="button" class="exp-btn exp-back" data-exp-back>← Experts</button>
      <h2 class="exp-title">${expEsc(expert.name)}</h2>
      <code class="exp-slug">${expEsc(expert.slug)}</code>
      <span class="exp-detail-counters">✓ ${data.usage.accepted} accepted · ✕ ${data.usage.rejected} rejected · ${data.usage.invocations} run${data.usage.invocations === 1 ? '' : 's'}</span>
    </div>
    <p class="exp-error" data-exp-error${state.error ? '' : ' hidden'}>${expEsc(state.error || '')}</p>
    <form class="exp-form" data-exp-form>
      <label class="exp-field"><span>Name</span>
        <input type="text" data-exp-field="name" value="${expEsc(expert.name)}" maxlength="120" required /></label>
      <label class="exp-field"><span>Description</span>
        <input type="text" data-exp-field="description" value="${expEsc(expert.description)}" maxlength="300" placeholder="What this expert knows" /></label>
      <label class="exp-field"><span>Model</span>
        <input type="text" data-exp-field="model" value="${expEsc(expert.model)}" placeholder="auto" spellcheck="false" /></label>
      <label class="exp-field exp-field-narrow"><span>Temperature</span>
        <input type="number" data-exp-field="temperature" value="${expert.temperature}" min="0" max="2" step="0.05" /></label>
      <label class="exp-field exp-field-narrow"><span>Top&nbsp;p</span>
        <input type="number" data-exp-field="top_p" value="${expert.top_p}" min="0" max="1" step="0.05" /></label>
      <label class="exp-field exp-field-check"><input type="checkbox" data-exp-field="enabled"${expert.enabled ? ' checked' : ''} /><span>Enabled</span></label>
      <label class="exp-field exp-field-wide"><span>Instructions — standing orders this expert reviews by</span>
        <textarea data-exp-field="instructions" rows="5" placeholder="How this specialist reads a passage.">${expEsc(expert.instructions)}</textarea></label>
      <label class="exp-field exp-field-wide"><span>Rubric — one item per line</span>
        <textarea data-exp-field="rubric" rows="5" placeholder="One rule per line. Without a rubric a local corrector rambles.">${expEsc(rubricText(expert.rubric))}</textarea></label>
      <div class="exp-form-actions">
        <button type="submit" class="exp-btn exp-btn-primary" data-exp-save${state.saving ? ' disabled' : ''}>${state.saving ? 'Saving…' : 'Save expert'}</button>
      </div>
    </form>

    <section class="exp-section">
      <h3>Corpus <span class="exp-count">${files.length} file${files.length === 1 ? '' : 's'} · ${data.chunks} chunk${data.chunks === 1 ? '' : 's'} · ${expEsc(expBytes(total))}</span></h3>
      <ul class="exp-files">${files.map(file => corpusFileRowHtml(file, expert.slug)).join('')
        || '<li class="exp-empty">No files yet — add the books this expert corrects by.</li>'}</ul>
      <div class="exp-corpus-actions">
        <input type="file" data-exp-upload multiple class="exp-upload" aria-label="Add files to the corpus" />
        <button type="button" class="exp-btn" data-exp-upload-btn${state.uploading ? ' disabled' : ''}>${state.uploading ? 'Uploading…' : 'Add to corpus'}</button>
        <button type="button" class="exp-btn" data-exp-reindex${state.reindexing ? ' disabled' : ''}>${state.reindexing ? 'Reindexing…' : 'Reindex'}</button>
        <span class="exp-inline-out" data-exp-reindex-slot>${reindexSummaryHtml(state.reindex)}</span>
      </div>
      <p class="exp-note">Indexed ${expEsc(data.indexed_at || 'never')}${data.collection ? ` · ${expEsc(data.collection)}` : ''}</p>
    </section>

    <section class="exp-section">
      <h3>Search the corpus</h3>
      <form class="exp-search-form" data-exp-search-form>
        <input type="search" class="exp-search" data-exp-search-q value="${expEsc(state.query || '')}" placeholder="A phrase from the books" aria-label="Search this corpus" />
        <button type="submit" class="exp-btn">Search</button>
        <button type="button" class="exp-btn" data-exp-block>Show what the model sees</button>
      </form>
      <div class="exp-search-out" data-exp-search-slot>${state.search ? searchHitsHtml(state.search, expert.slug) : ''}</div>
      <div class="exp-block-out" data-exp-block-slot>${state.block ? blockPreviewHtml(state.block) : ''}</div>
    </section>`;
}

// ── review: typed span deltas over the ORIGINAL text ──────────────────────

/** One delta (src/expert_review.py), every field defaulted, nothing invented. */
function normalizeDelta(raw) {
  const delta = raw && typeof raw === 'object' ? raw : {};
  const span = delta.span && typeof delta.span === 'object' ? delta.span : {};
  const start = expInt(span.start);
  const end = expInt(span.end);
  const citations = Array.isArray(delta.citations) ? delta.citations : [];
  const severity = String(delta.severity || '').toLowerCase();
  return {
    id: String(delta.id == null ? '' : delta.id),
    op: String(delta.op || 'EDIT').toUpperCase(),
    span: { start, end },
    quote: String(delta.quote == null ? '' : delta.quote),
    replacement: String(delta.replacement == null ? '' : delta.replacement),
    rationale: String(delta.rationale == null ? '' : delta.rationale),
    rule: String(delta.rule == null ? '' : delta.rule),
    severity: EXP_SEVERITIES.includes(severity) ? severity : 'medium',
    citations: citations.filter(c => c && typeof c === 'object'),
    // `label` is copied, never derived from `anchored`.
    label: String(delta.label == null ? '' : delta.label),
    anchored: Boolean(delta.anchored),
    confidence: expNum(delta.confidence, 0),
    relocated: Boolean(delta.relocated),
    notes: Array.isArray(delta.notes) ? delta.notes.map(n => String(n)) : [],
  };
}

/** A rejected correction: review() gives {raw:{quote}}, compact_result flattens it. */
function normalizeRejected(raw) {
  const row = raw && typeof raw === 'object' ? raw : {};
  const inner = row.raw && typeof row.raw === 'object' ? row.raw : {};
  return {
    id: String(row.id == null ? '' : row.id),
    op: String(row.op || ''),
    reason: String(row.reason == null ? '' : row.reason),
    quote: String(row.quote == null ? (inner.quote == null ? '' : inner.quote) : row.quote),
  };
}

/** The review result, bare or wrapped in {"result": …} / {"data": …}. */
function normalizeReview(raw) {
  let data = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  if (data.result && typeof data.result === 'object') data = data.result;
  else if (data.data && typeof data.data === 'object' && (data.data.deltas || data.data.expert)) data = data.data;
  const expert = data.expert && typeof data.expert === 'object' ? data.expert : {};
  const deltas = (Array.isArray(data.deltas) ? data.deltas : []).map(normalizeDelta);
  const anchored = deltas.filter(delta => delta.anchored).length;
  return {
    expert: {
      slug: String(expert.slug == null ? '' : expert.slug),
      name: String(expert.name || expert.slug || ''),
      model: String(expert.model || ''),
    },
    deltas,
    rejected: (Array.isArray(data.rejected) ? data.rejected : []).map(normalizeRejected),
    anchored_count: data.anchored_count == null ? anchored : expNum(data.anchored_count, anchored),
    opinion_count: data.opinion_count == null ? deltas.length - anchored : expNum(data.opinion_count, 0),
    degraded: Boolean(data.degraded),
    chunks: expNum(data.chunks, 0),
    errors: Array.isArray(data.errors) ? data.errors : [],
    text: String(data.text == null ? (data.original == null ? '' : data.original) : data.text),
  };
}

/**
 * Apply the accepted deltas to the original, RIGHT TO LEFT.
 *
 * The same rule as src/expert_review.apply_deltas, for the same reason: every
 * splice shifts the offsets after it, so applying in reverse keeps the
 * remaining spans valid without a fixup pass. `acceptIds` of null means all of
 * them; a delta with an unusable span is skipped rather than corrupting text.
 */
function applyAcceptedDeltas(original, deltas, acceptIds) {
  let text = String(original == null ? '' : original);
  const wanted = acceptIds == null ? null : new Set(Array.from(acceptIds).map(id => String(id)));
  const chosen = [];
  for (const raw of (deltas || [])) {
    if (!raw || typeof raw !== 'object') continue;
    if (wanted !== null && !wanted.has(String(raw.id == null ? '' : raw.id))) continue;
    const span = raw.span && typeof raw.span === 'object' ? raw.span : {};
    const start = expInt(span.start);
    const end = expInt(span.end);
    if (start === null || end === null) continue;
    if (!(start >= 0 && start <= end && end <= text.length)) continue;
    chosen.push({
      start,
      end,
      replacement: String(raw.op || '').toUpperCase() === 'KILL'
        ? '' : String(raw.replacement == null ? '' : raw.replacement),
    });
  }
  chosen.sort((a, b) => (b.start - a.start) || (b.end - a.end));
  for (const delta of chosen) {
    text = text.slice(0, delta.start) + delta.replacement + text.slice(delta.end);
  }
  return text;
}

/** {accepted, rejected} from the per-delta decisions — what feedback reports. */
function reviewCounts(decisions) {
  const map = decisions && typeof decisions === 'object' ? decisions : {};
  let accepted = 0;
  let rejected = 0;
  for (const key of Object.keys(map)) {
    if (map[key] === 'accepted') accepted += 1;
    else if (map[key] === 'rejected') rejected += 1;
  }
  return { accepted, rejected };
}

/** Ids of the accepted corrections, in document order. */
function acceptedIds(deltas, decisions) {
  const map = decisions && typeof decisions === 'object' ? decisions : {};
  return (deltas || []).map(normalizeDelta)
    .filter(delta => map[delta.id] === 'accepted')
    .map(delta => delta.id);
}

/** One citation as its `ref` string, linked to the corpus file it names. */
function citationHtml(citation, slug) {
  const item = citation && typeof citation === 'object' ? citation : {};
  const ref = refOf(item);
  const source = String(item.source || '');
  const marker = String(item.marker || '');
  const known = item.known === undefined ? true : Boolean(item.known);
  const inner = source
    ? `<a href="${expEsc(corpusUrl(slug, source))}" target="_blank" rel="noopener">${expEsc(ref)}</a>`
    : expEsc(ref);
  const flag = known ? '' : '<span class="exr-cite-unknown" title="This marker is not in the block the model was given">unknown marker</span>';
  return `<span class="exr-cite">${marker ? `<span class="exr-cite-marker">${expEsc(marker)}</span>` : ''}${inner}${flag}</span>`;
}

/**
 * One correction. `label` is printed verbatim — the renderer never decides for
 * itself what `anchored` means.
 */
function deltaCardHtml(raw, decision, slug) {
  const delta = normalizeDelta(raw);
  const state = decision === 'accepted' || decision === 'rejected' ? decision : 'pending';
  const label = delta.label;
  const labelClass = label === 'corpus' ? 'is-corpus' : 'is-opinion';
  const labelHtml = label
    ? `<span class="exr-label ${labelClass}">${expEsc(label)}</span>`
    : '<span class="exr-label is-unlabelled">no label in the result</span>';
  const before = delta.op === 'ADD' ? '' :
    `<del class="exr-before">${expEsc(delta.quote)}</del>`;
  const after = delta.op === 'KILL' ? '' :
    `<ins class="exr-after">${expEsc(delta.replacement)}</ins>`;
  const cites = delta.citations.length
    ? `<div class="exr-cites">${delta.citations.map(c => citationHtml(c, slug)).join('')}</div>`
    : '<div class="exr-cites exr-cites-none">No citation — nothing in the corpus was named.</div>';
  const notes = delta.notes.slice();
  if (delta.relocated) notes.unshift('span relocated to the quote');
  return `
    <article class="exr-card is-${expToken(delta.severity)} is-${state}" data-exr-card="${expEsc(delta.id)}">
      <header class="exr-card-head">
        <span class="exr-sev exr-sev-${expToken(delta.severity)}">${expEsc(delta.severity)}</span>
        <span class="exr-op">${expEsc(delta.op)}</span>
        <span class="exr-rule">${expEsc(delta.rule || 'no rubric rule named')}</span>
        ${labelHtml}
      </header>
      <p class="exr-rationale">${expEsc(delta.rationale || 'No rationale given.')}</p>
      <div class="exr-diff">${before}${after}</div>
      ${cites}
      ${notes.length ? `<p class="exr-notes">${expEsc(notes.join(' · '))}</p>` : ''}
      <footer class="exr-card-actions">
        <button type="button" class="exr-btn exr-accept${state === 'accepted' ? ' is-on' : ''}" data-exr-accept="${expEsc(delta.id)}" aria-pressed="${state === 'accepted'}">Accept</button>
        <button type="button" class="exr-btn exr-reject${state === 'rejected' ? ' is-on' : ''}" data-exr-reject="${expEsc(delta.id)}" aria-pressed="${state === 'rejected'}">Reject</button>
        <span class="exr-conf" title="Confidence from the anchoring layer that passed">${delta.confidence.toFixed(2)}</span>
      </footer>
    </article>`;
}

/** The original text with every delta's span marked, in document order. */
function markedTextHtml(text, deltas, decisions) {
  const body = String(text == null ? '' : text);
  const map = decisions && typeof decisions === 'object' ? decisions : {};
  const spans = (deltas || []).map(normalizeDelta)
    .filter(delta => delta.span.start !== null && delta.span.end !== null
      && delta.span.start >= 0 && delta.span.start <= delta.span.end && delta.span.end <= body.length)
    .sort((a, b) => (a.span.start - b.span.start) || (a.span.end - b.span.end));
  let cursor = 0;
  let html = '';
  for (const delta of spans) {
    if (delta.span.start < cursor) continue;      // overlaps are resolved server-side
    html += expEsc(body.slice(cursor, delta.span.start));
    const state = map[delta.id] === 'accepted' ? 'accepted' : map[delta.id] === 'rejected' ? 'rejected' : 'pending';
    const piece = body.slice(delta.span.start, delta.span.end);
    html += `<mark class="exr-mark is-${expToken(delta.severity)} is-${state}" data-exr-mark="${expEsc(delta.id)}" title="${expEsc(delta.id)}: ${expEsc(delta.rule || delta.op)}">${
      piece ? expEsc(piece) : '<span class="exr-caret" aria-hidden="true">⟨insert⟩</span>'}</mark>`;
    cursor = delta.span.end;
  }
  html += expEsc(body.slice(cursor));
  return html;
}

/** The failed corrections, collapsed. They are failures, not noise. */
function rejectedListHtml(rows) {
  const list = (rows || []).map(normalizeRejected);
  if (!list.length) return '';
  const items = list.map(row => `
    <li class="exr-dropped-row">
      <code>${expEsc(row.id || '?')}</code>
      <span class="exr-dropped-reason">${expEsc(row.reason || 'no reason given')}</span>
      ${row.quote ? `<span class="exr-dropped-quote">“${expEsc(row.quote)}”</span>` : ''}
    </li>`).join('');
  return `
    <details class="exr-dropped">
      <summary>${list.length} correction${list.length === 1 ? '' : 's'} the parser refused</summary>
      <ul class="exr-dropped-list">${items}</ul>
    </details>`;
}

function reviewPanelHtml(result, opts = {}) {
  const state = opts || {};
  const data = normalizeReview(result);
  const decisions = state.decisions && typeof state.decisions === 'object' ? state.decisions : {};
  const text = String(state.text == null ? data.text : state.text);
  const counts = reviewCounts(decisions);
  const slug = data.expert.slug;
  const head = `
    <div class="exp-detail-head">
      <button type="button" class="exp-btn exp-back" data-exp-back>← Experts</button>
      <h2 class="exp-title">${expEsc(data.expert.name || 'Review')}</h2>
      ${data.expert.model ? `<code class="exp-slug">${expEsc(data.expert.model)}</code>` : ''}
    </div>`;
  if (!data.deltas.length && !data.rejected.length) {
    return `${head}
      <p class="exp-error" data-exp-error${state.error ? '' : ' hidden'}>${expEsc(state.error || '')}</p>
      <p class="exp-empty">No review loaded. Ask an expert to review a passage (the <code>expert_review</code> tool), or paste a result below.</p>
      <form class="exr-paste" data-exr-paste>
        <textarea data-exr-paste-json rows="6" placeholder='{"expert": {...}, "deltas": [...], "text": "the passage"}' spellcheck="false"></textarea>
        <button type="submit" class="exp-btn">Render this review</button>
      </form>`;
  }
  if (!text) {
    return `${head}
      <p class="exp-note">${data.deltas.length} correction${data.deltas.length === 1 ? '' : 's'} — but the result does not carry the text they were made against. Paste the reviewed passage to see the spans.</p>
      <form class="exr-paste" data-exr-paste>
        <textarea data-exr-paste-text rows="8" placeholder="The passage that was reviewed" spellcheck="false"></textarea>
        <button type="submit" class="exp-btn">Use this text</button>
      </form>`;
  }
  const applied = applyAcceptedDeltas(text, data.deltas, acceptedIds(data.deltas, decisions));
  const cards = data.deltas.map(delta => deltaCardHtml(delta, decisions[delta.id], slug)).join('');
  const degraded = data.degraded
    ? '<p class="exp-note">The corpus answered degraded for at least one scene — lexical only, or a scene whose model call failed. Read the labels below accordingly.</p>'
    : '';
  return `${head}
    <p class="exp-error" data-exp-error${state.error ? '' : ' hidden'}>${expEsc(state.error || '')}</p>
    <p class="exr-counts">
      <span><b>${data.deltas.length}</b> correction${data.deltas.length === 1 ? '' : 's'}</span>
      <span class="exr-count-corpus"><b>${data.anchored_count}</b> anchored to the corpus</span>
      <span class="exr-count-opinion"><b>${data.opinion_count}</b> the model's own opinion</span>
      <span><b>${data.rejected.length}</b> refused</span>
      <span class="exr-count-decided"><b>${counts.accepted}</b> accepted · <b>${counts.rejected}</b> rejected</span>
    </p>
    ${degraded}
    <div class="exr-text" data-exr-text>${markedTextHtml(text, data.deltas, decisions)}</div>
    <div class="exr-cards">${cards}</div>
    ${rejectedListHtml(data.rejected)}
    <div class="exr-result-head">
      <h3>Result</h3>
      <button type="button" class="exp-btn" data-exr-copy>Copy result</button>
      <button type="button" class="exp-btn" data-exr-feedback${(!slug || state.sent) ? ' disabled' : ''}>${state.sent ? 'Feedback sent' : 'Send feedback'}</button>
    </div>
    <pre class="exr-result" data-exr-result>${expEsc(applied)}</pre>`;
}

// ── the rows the composer's "@" popup is offered ──────────────────────────
// fileMentions.js owns the popup; this only hands it rows. `rel` is inserted
// verbatim after the "@", so a row whose rel is "expert:brenner" writes
// "@expert:brenner " into the composer.

const MENTION_LIMIT = 4;

function expertMentionRows(rows, query) {
  const needle = String(query == null ? '' : query).trim().toLocaleLowerCase();
  const matches = (row) => !needle || [row.slug, row.name, row.description]
    .some(value => String(value == null ? '' : value).toLocaleLowerCase().includes(needle));
  return (rows || [])
    .map(normalizeExpert)
    .filter(row => row.slug && row.enabled && matches(row))
    .slice(0, MENTION_LIMIT)
    .map(row => ({
      rel: `expert:${row.slug}`,
      name: `expert:${row.slug}`,
      dir: row.name === row.slug ? '' : row.name,
      cat: 'Experts',
    }));
}
// ── Experts: end pure helpers ──

export { applyAcceptedDeltas, expertMentionRows, reviewPanelHtml, expertsGalleryHtml, expertDetailHtml };

const $ = (id) => document.getElementById(id);

const MODAL_ID = 'experts-modal';
const GALLERY_ID = 'experts-gallery';
const DETAIL_ID = 'experts-detail';
const REVIEW_ID = 'experts-review';

let _rows = [];
let _enabled = true;
let _loaded = false;
let _wired = false;
let _returnFocus = null;
let _gallery = { query: '', sort: 'name', error: '', loading: false };
let _detail = null;                 // the normalized detail payload
let _detailState = { error: '', saving: false, uploading: false, reindexing: false, query: '', search: null, block: null, reindex: null };
let _review = { result: null, decisions: {}, text: '', error: '', sent: false };

/** fetch wrapper for /api/experts/*: a non-2xx becomes an Error with {detail}. */
async function req(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) {
    const detail = data && data.detail != null
      ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      : '';
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

export const listExperts = () => req('');
export const createExpert = (body) => req('', { method: 'POST', body: JSON.stringify(body) });
export const readExpert = (slug) => req(`/${encodeURIComponent(slug)}`);
export const updateExpert = (slug, body) =>
  req(`/${encodeURIComponent(slug)}`, { method: 'PATCH', body: JSON.stringify(body) });
export const deleteExpert = (slug) => req(`/${encodeURIComponent(slug)}`, { method: 'DELETE' });
export const deleteCorpusFile = (slug, name) =>
  req(`/${encodeURIComponent(slug)}/corpus/${encodeURIComponent(name)}`, { method: 'DELETE' });
export const reindexExpert = (slug) => req(`/${encodeURIComponent(slug)}/reindex`, { method: 'POST' });
export const searchCorpus = (slug, q) =>
  req(`/${encodeURIComponent(slug)}/search?q=${encodeURIComponent(q)}`);
export const previewBlock = (slug, q) =>
  req(`/${encodeURIComponent(slug)}/block?q=${encodeURIComponent(q)}`);
export const sendFeedback = (slug, accepted, rejected) =>
  req(`/${encodeURIComponent(slug)}/feedback?accepted=${encodeURIComponent(accepted)}&rejected=${encodeURIComponent(rejected)}`,
      { method: 'POST' });

/** Multipart upload — the one call that must NOT send a JSON content type. */
async function uploadCorpus(slug, fileList) {
  const form = new FormData();
  for (const file of Array.from(fileList || [])) form.append('files', file, file.name);
  const res = await fetch(`${API}/${encodeURIComponent(slug)}/corpus`, {
    method: 'POST',
    credentials: 'same-origin',
    body: form,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

// ── rendering ─────────────────────────────────────────────────────────────

function showView(which) {
  const gallery = $(GALLERY_ID);
  const detail = $(DETAIL_ID);
  const review = $(REVIEW_ID);
  if (gallery) gallery.classList.toggle('hidden', which !== 'gallery');
  if (detail) detail.classList.toggle('hidden', which !== 'detail');
  if (review) review.classList.toggle('hidden', which !== 'review');
}

function renderGallery() {
  const host = $(GALLERY_ID);
  if (!host) return;
  host.innerHTML = expertsGalleryHtml(_rows, { ..._gallery, enabled: _enabled });
  showView('gallery');
}

function renderDetail() {
  const host = $(DETAIL_ID);
  if (!host || !_detail) return;
  host.innerHTML = expertDetailHtml(_detail, _detailState);
  showView('detail');
}

function renderReview() {
  const host = $(REVIEW_ID);
  if (!host) return;
  host.innerHTML = reviewPanelHtml(_review.result, {
    decisions: _review.decisions,
    text: _review.text,
    error: _review.error,
    sent: _review.sent,
  });
  showView('review');
}

/** Inline error slot for whichever view is on screen — never a native dialog. */
function inlineError(message) {
  const modal = $(MODAL_ID);
  const slots = modal ? modal.querySelectorAll('[data-exp-error]') : [];
  slots.forEach(slot => {
    if (slot.closest('.hidden')) return;
    slot.textContent = message || '';
    slot.hidden = !message;
  });
}

// ── loading ───────────────────────────────────────────────────────────────

export async function loadExperts(force = false) {
  if (_loaded && !force) return _rows;
  _gallery.loading = true;
  _gallery.error = '';
  renderGallery();
  try {
    const data = await listExperts();
    _rows = normalizeExperts(data);
    _enabled = !(data && data.enabled === false);
    _loaded = true;
  } catch (error) {
    _rows = [];
    _gallery.error = `Could not load the experts: ${error.message || error}`;
  } finally {
    _gallery.loading = false;
    renderGallery();
  }
  return _rows;
}

async function openExpert(slug) {
  _detailState = { error: '', saving: false, uploading: false, reindexing: false, query: '', search: null, block: null, reindex: null };
  try {
    _detail = normalizeDetail(await readExpert(slug));
    renderDetail();
  } catch (error) {
    _gallery.error = `Could not open ${slug}: ${error.message || error}`;
    renderGallery();
  }
}

/**
 * Copy what the user has typed into the working profile.
 *
 * Uploading a file or running a search re-renders the whole detail view, and
 * without this the half-written rubric in the textarea would silently go with
 * it — the kind of loss that teaches people not to trust a form.
 */
function captureForm() {
  const host = $(DETAIL_ID);
  if (!host || !_detail) return;
  const field = (name) => host.querySelector(`[data-exp-field="${name}"]`);
  const expert = _detail.expert;
  for (const name of ['name', 'description', 'model', 'instructions']) {
    const input = field(name);
    if (input) expert[name] = String(input.value == null ? '' : input.value);
  }
  for (const name of ['temperature', 'top_p']) {
    const input = field(name);
    const value = input ? Number(input.value) : NaN;
    if (Number.isFinite(value)) expert[name] = value;
  }
  const enabled = field('enabled');
  if (enabled) expert.enabled = Boolean(enabled.checked);
  const rubric = field('rubric');
  if (rubric) expert.rubric = rubricLines(rubric.value);
}

/** Reload the profile + corpus. `keepForm` re-applies the unsaved edits. */
async function refreshDetail(keepForm = false) {
  if (!_detail) return;
  const draft = keepForm ? { ..._detail.expert } : null;
  try {
    _detail = normalizeDetail(await readExpert(_detail.expert.slug));
    if (draft) _detail.expert = { ..._detail.expert, ...draft };
  } catch (error) {
    _detailState.error = `Could not reload: ${error.message || error}`;
  }
  renderDetail();
}

// ── mutations ─────────────────────────────────────────────────────────────

async function newExpert() {
  const name = await uiModule.styledPrompt?.('What should this expert be called?',
    { title: 'New expert', placeholder: 'e.g. Brenner on craft', confirmText: 'Create' });
  if (!name || !String(name).trim()) return;
  try {
    const created = unwrapExpert(await createExpert({ name: String(name).trim() }));
    await loadExperts(true);
    if (created.slug) await openExpert(created.slug);
  } catch (error) {
    _gallery.error = `Could not create the expert: ${error.message || error}`;
    renderGallery();
  }
}

async function removeExpert(slug) {
  const row = _rows.find(item => item.slug === slug);
  const ok = await uiModule.styledConfirm?.(
    `Delete "${row ? row.name : slug}"? Its corpus files and index go with it.`,
    { confirmText: 'Delete', danger: true });
  if (!ok) return;
  try {
    await deleteExpert(slug);
    uiModule.showToast?.('Expert deleted');
    await loadExperts(true);
  } catch (error) {
    _gallery.error = `Could not delete ${slug}: ${error.message || error}`;
    renderGallery();
  }
}

async function saveExpert(host) {
  if (!_detail) return;
  const value = (name) => host.querySelector(`[data-exp-field="${name}"]`);
  const body = {
    name: String(value('name')?.value || '').trim(),
    description: String(value('description')?.value || ''),
    model: String(value('model')?.value || '').trim(),
    temperature: Number(value('temperature')?.value),
    top_p: Number(value('top_p')?.value),
    enabled: Boolean(value('enabled')?.checked),
    instructions: String(value('instructions')?.value || ''),
    rubric: rubricLines(value('rubric')?.value || ''),
  };
  if (!body.name) {
    inlineError('An expert needs a name.');
    value('name')?.focus();
    return;
  }
  if (!Number.isFinite(body.temperature)) delete body.temperature;
  if (!Number.isFinite(body.top_p)) delete body.top_p;
  // The button is disabled in place rather than through a re-render: a failed
  // save must leave the user's edits in the form, not replace them with what
  // the server still has.
  const button = host.querySelector('[data-exp-save]');
  _detailState.saving = true;
  _detailState.error = '';
  inlineError('');
  if (button) { button.disabled = true; button.textContent = 'Saving…'; }
  try {
    await updateExpert(_detail.expert.slug, body);
    _detailState.saving = false;
    uiModule.showToast?.('Expert saved');
    await refreshDetail();
    await loadExperts(true);
    showView('detail');
  } catch (error) {
    _detailState.saving = false;
    if (button) { button.disabled = false; button.textContent = 'Save expert'; }
    inlineError(`Could not save: ${error.message || error}`);
  }
}

async function addCorpusFiles(host) {
  if (!_detail) return;
  const input = host.querySelector('[data-exp-upload]');
  const files = input && input.files ? input.files : null;
  if (!files || !files.length) {
    inlineError('Choose one or more files first.');
    return;
  }
  captureForm();
  _detailState.uploading = true;
  _detailState.error = '';
  renderDetail();
  try {
    const data = await uploadCorpus(_detail.expert.slug, files);
    _detailState.reindex = data;
    const refused = Array.isArray(data && data.rejected) ? data.rejected : [];
    if (refused.length) {
      _detailState.error = `Not stored: ${refused.map(row => `${row.name} (${row.reason})`).join(', ')}`;
    }
    uiModule.showToast?.(`${(data && data.uploaded ? data.uploaded.length : 0)} file(s) added`);
  } catch (error) {
    _detailState.error = `Upload failed: ${error.message || error}`;
  } finally {
    _detailState.uploading = false;
    await refreshDetail(true);
  }
}

async function removeCorpusFile(name) {
  if (!_detail) return;
  const ok = await uiModule.styledConfirm?.(
    `Delete "${name}" from this corpus? Its chunks go with it.`,
    { confirmText: 'Delete', danger: true });
  if (!ok) return;
  captureForm();
  try {
    await deleteCorpusFile(_detail.expert.slug, name);
    uiModule.showToast?.('File removed from the corpus');
  } catch (error) {
    _detailState.error = `Could not delete ${name}: ${error.message || error}`;
  }
  await refreshDetail(true);
}

async function reindex() {
  if (!_detail) return;
  captureForm();
  _detailState.reindexing = true;
  _detailState.error = '';
  renderDetail();
  try {
    _detailState.reindex = await reindexExpert(_detail.expert.slug);
  } catch (error) {
    _detailState.error = `Reindex failed: ${error.message || error}`;
  } finally {
    _detailState.reindexing = false;
    await refreshDetail(true);
  }
}

async function runSearch(host) {
  if (!_detail) return;
  captureForm();
  const query = String(host.querySelector('[data-exp-search-q]')?.value || '').trim();
  _detailState.query = query;
  if (!query) {
    _detailState.search = null;
    renderDetail();
    return;
  }
  try {
    _detailState.search = await searchCorpus(_detail.expert.slug, query);
    _detailState.error = '';
  } catch (error) {
    _detailState.error = `Search failed: ${error.message || error}`;
  }
  renderDetail();
}

async function showBlock(host) {
  if (!_detail) return;
  captureForm();
  const query = String(host.querySelector('[data-exp-search-q]')?.value || '').trim();
  _detailState.query = query;
  try {
    _detailState.block = await previewBlock(_detail.expert.slug, query);
    _detailState.error = '';
  } catch (error) {
    _detailState.error = `Could not build the block: ${error.message || error}`;
  }
  renderDetail();
}

// ── review panel ──────────────────────────────────────────────────────────

/**
 * Show a review result. `options.text` is the passage the review was made
 * against — the deltas are offsets into it, so without it the panel asks for
 * it rather than guessing.
 */
export function openReviewPanel(result, options = {}) {
  _review = {
    result: result || null,
    decisions: {},
    text: String(options.text == null ? normalizeReview(result).text : options.text),
    error: '',
    sent: false,
  };
  openExpertsPanel({ view: 'review' });
  renderReview();
}

function decide(id, choice) {
  if (!id) return;
  if (_review.decisions[id] === choice) delete _review.decisions[id];
  else _review.decisions[id] = choice;
  _review.sent = false;
  renderReview();
}

async function copyResult(host) {
  const text = host.querySelector('[data-exr-result]')?.textContent || '';
  try {
    await navigator.clipboard.writeText(text);
    uiModule.showToast?.('Result copied');
  } catch (_) {
    _review.error = 'The browser refused the clipboard — select the result and copy it by hand.';
    renderReview();
  }
}

async function reportFeedback() {
  const data = normalizeReview(_review.result);
  const slug = data.expert.slug;
  const counts = reviewCounts(_review.decisions);
  if (!slug) {
    _review.error = 'This result does not name an expert, so there is nothing to report the outcome to.';
    renderReview();
    return;
  }
  try {
    await sendFeedback(slug, counts.accepted, counts.rejected);
    _review.sent = true;
    _review.error = '';
    uiModule.showToast?.(`Reported ${counts.accepted} accepted, ${counts.rejected} rejected`);
    await loadExperts(true);
    showView('review');
  } catch (error) {
    _review.error = `Could not report the outcome: ${error.message || error}`;
  }
  renderReview();
}

function acceptPastedReview(host) {
  const jsonBox = host.querySelector('[data-exr-paste-json]');
  const textBox = host.querySelector('[data-exr-paste-text]');
  if (textBox) {
    _review.text = String(textBox.value || '');
    renderReview();
    return;
  }
  try {
    _review.result = JSON.parse(String(jsonBox?.value || ''));
    _review.decisions = {};
    _review.text = normalizeReview(_review.result).text;
    _review.error = '';
  } catch (error) {
    _review.error = `That is not a review result: ${error.message || error}`;
  }
  renderReview();
}

// ── wiring: one delegated listener pair on the modal ──────────────────────

function wire() {
  if (_wired) return;
  const modal = $(MODAL_ID);
  if (!modal) return;
  _wired = true;

  modal.addEventListener('click', (event) => {
    const target = event.target;
    if (target.closest('#close-experts-modal')) { closeExpertsPanel(); return; }
    if (target.closest('[data-exp-back]')) { renderGallery(); return; }
    if (target.closest('[data-exp-new]')) { newExpert(); return; }
    if (target.closest('[data-exp-open-review]')) { renderReview(); return; }
    const del = target.closest('[data-exp-delete]');
    if (del) { event.stopPropagation(); removeExpert(del.dataset.expDelete); return; }
    const open = target.closest('[data-exp-open]');
    if (open) { openExpert(open.dataset.expOpen); return; }
    const fileDel = target.closest('[data-exp-file-delete]');
    if (fileDel) { removeCorpusFile(fileDel.dataset.expFileDelete); return; }
    if (target.closest('[data-exp-upload-btn]')) { addCorpusFiles($(DETAIL_ID)); return; }
    if (target.closest('[data-exp-reindex]')) { reindex(); return; }
    if (target.closest('[data-exp-block]')) { showBlock($(DETAIL_ID)); return; }
    const accept = target.closest('[data-exr-accept]');
    if (accept) { decide(accept.dataset.exrAccept, 'accepted'); return; }
    const reject = target.closest('[data-exr-reject]');
    if (reject) { decide(reject.dataset.exrReject, 'rejected'); return; }
    if (target.closest('[data-exr-copy]')) { copyResult($(REVIEW_ID)); return; }
    if (target.closest('[data-exr-feedback]')) reportFeedback();
  });

  modal.addEventListener('submit', (event) => {
    if (event.target.closest('[data-exp-form]')) {
      event.preventDefault();
      saveExpert($(DETAIL_ID));
      return;
    }
    if (event.target.closest('[data-exp-search-form]')) {
      event.preventDefault();
      runSearch($(DETAIL_ID));
      return;
    }
    if (event.target.closest('[data-exr-paste]')) {
      event.preventDefault();
      acceptPastedReview($(REVIEW_ID));
    }
  });

  modal.addEventListener('input', (event) => {
    if (!event.target.matches('[data-exp-query]')) return;
    _gallery.query = event.target.value;
    const host = $(GALLERY_ID);
    const grid = host && host.querySelector('.exp-grid');
    if (!grid) return;
    const visible = sortExperts(filterExperts(_rows, _gallery.query), _gallery.sort);
    grid.innerHTML = visible.map(expertCardHtml).join('')
      || '<div class="exp-empty">No expert matches that search.</div>';
  });

  modal.addEventListener('change', (event) => {
    if (!event.target.matches('[data-exp-sort]')) return;
    _gallery.sort = event.target.value;
    renderGallery();
  });

  $('tool-experts-btn')?.addEventListener('click', () => openExpertsPanel());
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const confirmBox = $('styled-confirm-overlay');
    if (confirmBox && !confirmBox.classList.contains('hidden') && confirmBox.style.display !== 'none') return;
    const open = $(MODAL_ID);
    if (open && !open.classList.contains('hidden')) closeExpertsPanel();
  });
}

export async function openExpertsPanel(options = {}) {
  const modal = $(MODAL_ID);
  if (!modal) return;
  wire();
  _returnFocus = document.activeElement;
  modal.classList.remove('hidden');
  if (options.view === 'review') { showView('review'); return; }
  await loadExperts(true);
  if (options.slug) await openExpert(options.slug);
}

export function closeExpertsPanel() {
  $(MODAL_ID)?.classList.add('hidden');
  try { _returnFocus?.focus?.(); } catch (_) {}
}

// ── "@expert:<slug>" in the composer ──────────────────────────────────────

function registerMentions() {
  if (typeof registerMentionSource !== 'function') return;
  let kicked = false;
  registerMentionSource((query) => {
    // The roster is loaded once, in the background: the popup is synchronous,
    // so an expert that has not loaded yet simply is not offered. One kick per
    // page — a non-admin whose roster 403s must not re-ask on every keystroke.
    if (!_loaded) {
      if (!kicked) { kicked = true; loadExperts().catch(() => {}); }
      return [];
    }
    return expertMentionRows(_rows, query);
  });
}

export function initExperts() {
  wire();
  registerMentions();
  loadExperts().catch(() => {});
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initExperts);
} else {
  initExperts();
}

const expertsModule = {
  initExperts,
  openExpertsPanel,
  closeExpertsPanel,
  openReviewPanel,
  loadExperts,
  listExperts,
  createExpert,
  readExpert,
  updateExpert,
  deleteExpert,
  deleteCorpusFile,
  reindexExpert,
  searchCorpus,
  previewBlock,
  sendFeedback,
  applyAcceptedDeltas,
  expertMentionRows,
};

if (typeof window !== 'undefined') window.expertsModule = expertsModule;

export default expertsModule;
