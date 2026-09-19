# Charts in chat answers

A model can put a chart in a reply with a fenced ` ```chart ` (or
` ```faustus-chart `) block containing JSON. Studio renders it as an inline
SVG chart; anything invalid falls back to a normal code block — never a
crash, never raw HTML.

## Spec

```json
{
  "type": "bar",
  "title": "Optional title",
  "x": ["Jan", "Feb", "Mar"],
  "series": [
    { "name": "Revenue", "values": [10, 12, 9] },
    { "name": "Cost", "values": [7, 8, 8] }
  ],
  "unit": "k€",
  "stacked": false
}
```

- `type`: `"bar" | "line" | "pie" | "area"` (required).
- `title`: optional string shown above the chart.
- `x`: optional array of labels, one per value. Defaults to `1, 2, 3, …`.
- `series`: required, non-empty array of `{name, values}`. Every series must
  have the same number of values as every other series (and as `x`, when
  given).
- `unit`: optional string appended to values in the axis, legend and tooltips.
- `stacked`: optional boolean, only meaningful for `bar`/`area`.

`pie` uses only `series[0]`; `x` (or `1, 2, 3, …` when absent) supplies the
slice labels.

## Caps

- At most 12 series.
- At most 200 points per series.
- At most 40 pie slices.
- Strings (`title`, `unit`, series names, `x` labels) are clipped to 60
  characters.
- All numbers must be finite (no `NaN`/`Infinity`); pie values must not be
  negative.

Anything outside these caps, or any JSON that doesn't parse or doesn't match
the shape above, is rejected by `studio/src/lib/chartSpec.ts` and the block
renders as plain code instead — the model's fenced block is never lost, it's
just shown as source.

## Implementation

- `studio/src/lib/chartSpec.ts` — pure validator, `parseChartSpec(raw) ->
  {ok:true,spec} | {ok:false,reason}`. No React, no DOM.
- `studio/src/components/ChartBlock.tsx` — pure inline SVG renderer (no
  charting library, same posture as `components/MermaidView.tsx`): axes with
  "nice number" ticks, gridlines, a legend, per-point/slice tooltips via
  `<title>`, a responsive `viewBox`, colors from the theme tokens
  (`styles/tokens.css`) so it matches light and dark automatically, and a
  "Show data"/"Hide data" toggle that renders the series as a table. The SVG
  carries `role="img"` with a `<title>`/`<desc>` pair for screen readers.
- The hook point is `studio/src/screens/rich.tsx`, in `One()`'s
  `case 'code'`: a fence whose language is `chart` or `faustus-chart` is
  parsed with `parseChartSpec`; on success it renders `<ChartBlock>`, on
  failure it falls through to the ordinary `<CodeBlock>` exactly like any
  other language.

No new dependency was added for this.
