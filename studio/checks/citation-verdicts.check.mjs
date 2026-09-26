// Research report citation verdicts (§144 follow-up): the per-citation
// supported/not_supported/unverifiable outcome the blind-review checking
// pass computes (src/research_citations.py::per_source_verdicts) must reach
// the adapter's ResearchSource type and be rendered as a badge next to each
// source in the report view. Static source assertions only, no bundling —
// the same style process-center.check.mjs uses.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const adapter = readFileSync(new URL('../src/adapters/research.ts', import.meta.url), 'utf8');
assert.match(adapter, /citationVerdict\?:\s*'supported'\s*\|\s*'not_supported'\s*\|\s*'unverifiable'/,
  'ResearchSource carries the per-citation verdict from src/research_handler.py');
assert.match(adapter, /s\.citation_verdict/, 'sourceFrom() reads citation_verdict off the raw payload');

const screen = readFileSync(new URL('../src/screens/research/Research.tsx', import.meta.url), 'utf8');
assert.match(screen, /s\.citationVerdict\s*&&/, 'a source with a verdict renders it');
assert.match(screen, /data-testid="research-citation-verdict"/, 'the verdict badge has its own testid');

console.log('citation-verdicts: ALL OK');
