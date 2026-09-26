# Reply blocks: `choices` and `decision`

A reply can carry a small JSON object in a fenced block, and Studio draws it
with fixed components (`studio/src/components/UiBlock.tsx`, validator in
`studio/src/lib/uiBlocks.ts`). The model never ships markup. Invalid JSON, or a
block with fewer than two usable options, is shown as the plain code block.

A pick never sends a message by itself: it puts the answer in the composer
(appended if the user was already writing), and the user presses Enter. In
readers with no composer (Home cards, reports, the board) the buttons are
disabled with a hint.

## `choices` (alias `faustus-choices`)

```json
{"question": "Which database?", "options": ["Postgres", {"label": "SQLite", "detail": "one file, no server"}], "multi": false}
```

Two to eight options, duplicates dropped, strings capped at 200 characters.
With `"multi": true` the options toggle and a "Use these (n)" button writes
them joined by commas.

## `decision` (alias `faustus-decision`)

```json
{"title": "Hosting", "options": [
  {"name": "VPS", "summary": "full control", "pros": ["cheap"], "cons": ["you patch it"], "cost": "5 €/month", "recommended": true},
  {"name": "PaaS", "pros": ["no ops"], "cons": ["lock-in"]}
], "verdict": "VPS unless nobody wants to maintain it."}
```

Two to six options, up to eight pros and eight cons each, one recommendation
at most (the first marked wins). "Choose" writes "Let's go with <name>." in
the composer.

The agent's base rules tell the model when to use them: only for a real
choice, never instead of answering.
