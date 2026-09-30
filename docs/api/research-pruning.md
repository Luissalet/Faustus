# Research pruning and measured stopping

Deep Research used to read every source in full: the extractor returned up to
`max_content_chars` (15 000) characters per page and the local model read all
of them, menus and cookie notices included, at 40-150 s a page on a 27B model.
The loop also depended on `max_time` to end, because the model almost never
answers "enough". Two deterministic mechanisms, both without a model call,
change that:

1. **Page pruning** (`src/research_prune.py`): the page is cut down to the
   blocks that look like content and match the question before the model reads
   it.
2. **Measured stopping** (`src/research_saturation.py`): after each round that
   read pages, the run counts new facts and new sources and stops when the
   evidence has stopped growing.

`max_time` (the run's time budget) remains the hard ceiling for both.

## 1. Page pruning

### Block scoring (independent of the question)

The page is split into blocks: paragraphs, headings, list items, table rows,
code. From raw HTML the DOM is walked (scripts, styles, hidden elements and
form controls are dropped); from already extracted Markdown or plain text the
syntax is used instead. Each block gets a score in `[0, 1]`:

| Signal | Weight | Meaning |
| --- | --- | --- |
| text density | 0.4 | text over markup, and characters per tag, in the block's container |
| link density | 0.2 | `1 - link text / text` (penalty for link lists) |
| tag | 0.2 | `p`, `h1`-`h3`, `article`, `main` high; `li`, `td`, `div` middle; inside `nav`, `footer`, `aside`, a widget-sized `form` or `role=navigation` it is 0 |
| class / id hint | 0.1 | tokens such as `content`, `article`, `post` good; `nav`, `menu`, `footer`, `sidebar`, `comment`, `ad`, `cookie` bad |
| length | 0.1 | very short blocks are rarely a fact (headings are exempt) |

A block scoring at least **0.48** survives. A block whose nearest context is
explicit boilerplate (a `<nav>`, a `cookie` banner, a `comments` section...) is
also clamped to 0.30, so a long cookie notice cannot pass on text density
alone. The nearest matching ancestor decides the tag and class context, except
that a strong boilerplate token on any ancestor wins (a `comment-content`
inside `comments` is still a comment).

### Ranking against the question (BM25)

Surviving paragraphs are ranked with Okapi BM25 (`k1 = 1.5`, `b = 0.75`) against
the focus text. A term that also appears in the paragraph's section headings
counts extra: the term frequency becomes `tf + 5*h1 + 3*h2 + 2*h3`. Tokenisation
lowercases, folds accents (`clasificación` = `clasificacion`), drops Spanish and
English stopwords and strips a plural `s`.

The best paragraphs are kept **in document order** up to a character cap
(`research_prune_max_chars`, default 6000). The page title is always kept, and
so is the best-matching block (cut at a sentence boundary if it alone exceeds
the cap). Headings above a kept paragraph are emitted with it, so the model
still sees the section. When everything that survived already fits under the
cap it is all kept; when nothing matches the question the leading blocks are
kept and the trace says `no_query_match`.

### Fail-safes

* If the threshold pass leaves under 300 characters a more permissive pass
  (everything not explicitly boilerplate) runs (`fallback: relaxed`); if that is
  still thin the original text, capped, is returned (`fallback: unpruned`).
* If the DOM walk finds under a quarter of the text the extractor found (a page
  rendered by script) the extracted text is pruned instead (`mode: text`).
* Any error returns the unpruned page; pruning never fails a run.

### What Deep Research prunes against

`_fetch_and_extract` asks the fetcher for the raw markup (`keep_html=True`,
never cached) and prunes against the search query that returned the URL plus
the sub-question that query shares the most words with (the whole question only
when neither is known). A fetcher that does not return markup, or a cache hit,
falls back to pruning the extracted Markdown.

### Trace

Every pruned page appends a row to `researcher.research_trace["pages"]`:

```json
{"url": "...", "original_chars": 9292, "pruned_chars": 5985, "blocks_total": 61,
 "blocks_passed": 22, "blocks_kept": 9, "top_bm25": 7.41, "mode": "html",
 "fallback": "", "focus": "..."}
```

The stats line of the report shows the total (`Pruned: 12 page(s), 88000 ->
31000 chars`).

## 2. Measured stopping

A *fact* is a sentence of a finding's summary (or evidence when the summary is
empty) with at least four content words. It is **new** unless an earlier fact
has the same normalised text or a token-set Jaccard similarity of at least 0.8;
a near-duplicate from another source is not new but counts as corroboration. A
*source* is the canonical domain (lowercase, no `www.`), so another page of a
domain already read is not a new source.

* **Saturation.** A round that read pages and added fewer than
  `research_saturation_min_new_facts` (default 2) new facts and no new source is
  saturated. `research_saturation_patience` (default 1) consecutive saturated
  rounds stop the run with the stop reason `saturated`, without asking the
  model. Rounds that read nothing are not counted: an empty round says nothing
  about the topic and the loop already handles searches that return nothing. No
  stop is decided before two rounds of reading.
* **Confidence.** `0.4 * coverage + 0.3 * consistency + 0.3 * saturation`.
  Coverage is the share of sub-questions with a supporting fact (a fact sharing
  at least 40 % of the sub-question's words). Consistency is the share of facts
  backed by two or more sources (the codebase has no contradiction detector, so
  corroboration is the measurable proxy). Saturation is `1 - new / total` facts
  of the last round. The run stops with `confidence_reached` once the value
  reaches `research_confidence_stop` (default 0.7; 0 turns it off).

Each round appends a row to `research_trace["rounds"]`:

```json
{"round": 2, "pages": 3, "new_facts": 1, "total_facts": 14, "new_sources": 0,
 "total_sources": 5, "saturated_round": true, "saturated_streak": 1,
 "coverage": 0.83, "consistency": 0.36, "saturation": 0.93, "confidence": 0.74}
```

The stop reason is the existing `researcher.stop_reason` record (`code`,
`reason`, `rounds_completed`, `sources_gathered`, `findings_gathered`, plus
`detail` with the round's numbers), with two new codes: `saturated` and
`confidence_reached`. It is also in the stats (`Stopped`, `Confidence`), in the
report's summary line and in the saved run (`stop_reason`, `trace`).

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `research_prune_pages` | `true` | prune pages before extraction; `false` restores the original path |
| `research_prune_max_chars` | `6000` | character cap of a pruned page (500-60000) |
| `research_prune_threshold` | `0.48` | minimum block score |
| `research_saturation_stop` | `true` | stop on measured saturation |
| `research_saturation_min_new_facts` | `2` | fewer new facts than this (and no new source) makes a round saturated |
| `research_saturation_patience` | `1` | consecutive saturated rounds before stopping |
| `research_confidence_stop` | `0.7` | stop at this confidence (0 = off) |

The `DeepResearcher` constructor has the matching arguments (`prune_pages`,
`prune_max_chars`, `prune_threshold`, `saturation_stop`,
`saturation_min_new_facts`, `saturation_patience`, `confidence_stop`); all are
off or neutral by default there, so code that builds the class directly keeps
its old behaviour.

## The `page_prune` tool

A builtin agent tool over the same code: give a `url` (fetched with the guarded
fetcher `web_fetch` uses), raw `html` or extracted `text`, plus the `query` the
page is read for. Optional: `max_chars`, `threshold`, `title`,
`include_blocks` (default true). It returns the pruned text, original versus
pruned characters, blocks kept, the top BM25 score and each block's score and
verdict (`KEPT`, `pass`, `drop`). Its result is untrusted external text, like
`web_fetch`'s, and it is read-only (allowed in plan mode).

## Measuring it

`scripts/bench_research_prune.py` replays page extraction
(`DeepResearcher._fetch_and_extract`) on saved HTML pages, once without and once
with pruning, and prints characters read, time per page, sources, facts, the
percentage of extracted sentences that appear in the original page (not
invented) and the recall of per-page gold phrases. Pages live in
`tests/fixtures/research_pages/` with a `queries.json` (`{"page.html":
{"query": "...", "gold": ["phrase"]}}`).

```bash
# offline, deterministic stand-in model; time is simulated from characters read
python scripts/bench_research_prune.py

# the real model: any OpenAI-compatible chat completions endpoint
python scripts/bench_research_prune.py --endpoint http://127.0.0.1:8080/v1/chat/completions \
    --model-name MODEL --timeout 300

# save real pages once, then measure them repeatedly (add gold phrases by hand)
python scripts/bench_research_prune.py --pages-dir D:\bench_pages --fetch https://example.org/a https://example.org/b
```

With a real endpoint the time column is measured wall time per extraction. The
stand-in model is instant, so its time column is simulated (`chars / --sim-rate`,
default 150 characters per second) and is labelled as such.
