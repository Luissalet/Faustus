# Deliverable inspection

`inspect_deliverable` is a read-only workspace tool for checking actual output files. It accepts a local `path`, an optional `max_content_chars` from 1,000 to 80,000 (default 24,000) and, for PowerPoint files, an optional `compare_with` reference template. Its result includes the resolved path, filename, extension, type, byte size, SHA-256, format facts, a bounded content payload, parser limitations, and an explicit inspection scope.

Supported extraction:

- PDF: page count, page dimensions, rotation, extracted page text, metadata, and AcroForm field count.
- PPTX: slide text in shape order, speaker notes, related media including SVG dimensions/viewBox, and cached chart titles, series, categories, and values; also `.pptm`, `.potx` and `.potm`, with the template structure described below.
- DOCX: paragraph text, tables, and embedded media inventory.
- XLSX: sheet order/names, cells, shared and inline strings, formulas, cached values, and formulas without cached values. Formulas are never recalculated.
- CSV/TSV: detected delimiter, header and rows, with a 20,000-row scan limit.
- Raster images/SVG: decoder metadata (dimensions, format, mode/frame count or SVG dimensions/viewBox).
- Audio/video: metadata provided by the existing media inspector; compressed formats may need FFprobe.
- DesignCraft `.designcraft`: page and spread counts, text/graphic/group item counts, story text, asset-record counts, and counts/bytes of non-metadata package parts. It reads only `document.json`; other package parts are counted but are not assumed to be assets, their bytes are not decoded, and linked paths are not followed.
- VectorCraft `.vectorcraft`: artboard and layer/group/path/text/image node counts plus image-record count. It reads the bounded JSON document and does not decode image pixels or rasterize geometry.

The tool reads files without modifying them. Office package relationships are treated as data; external relationships are ignored and macros are never run. Native project references are also treated as data; linked files are never opened and embedded code is never run. Package parts, file sizes, entries, native item counts, and returned content have explicit bounds. A hash is computed from the inspected file and a before/after stat check rejects files that changed during the operation.

This is structural and extractable-content evidence, not a quality gate. It does not render documents or native projects, verify visual layout, judge semantic correctness or completeness, recalculate spreadsheets, listen to media, transcribe audio, or interpret image pixels. A counted frame, node, story, asset record, or package part does not prove that it is visible, editable, complete, or faithful to a brief. A successful inspection means the listed parser facts were extracted; it does not certify that a deliverable meets its brief.

## PowerPoint template structure

It also reads the template structure of `.pptx`, `.pptm`, `.potx` and `.potm` packages, not only their
text, notes, images and charts. `facts.template` contains:

- `slide_size`: width and height in EMU and inches, the `type` attribute, a common name (`16:9`, `4:3`,
  `16:10`) when the size matches a standard one within 0.5 %, and `aspect`, the ratio of any canvas (a 20 × 11.25 in
  slide is `16:9` too).
- `masters`: each slide master with its name, theme part, layouts and fingerprint.
- `layouts`: each layout with its name, type, master, placeholders (`type`, `idx`), fingerprint and the slides that
  use it; `unused_layouts` lists the ones no slide uses.
- `themes`: name, colour scheme (`dk1` … `accent6`, `hlink`, `folHlink`), font scheme (major and minor Latin
  typefaces) and fingerprint.
- `template_fingerprint`: one hash over every master, layout and theme fingerprint.
- `unreadable_parts`: template parts that are missing or malformed (`part`, `kind`, `error`). They are left out instead of aborting the inspection; `template_fingerprint` is then null and the comparison never reports the template as preserved.

Every slide also reports its `layout`, `layout_name` and `master`.

A **fingerprint** is the SHA-256 of the part's canonical XML (C14N with insignificant whitespace removed). Each
relationship id inside the part is replaced by what it points to: the relationship type, plus the content hash for
images and other binary parts. A tool that re-serialises an unchanged part, renumbers its relationships or moves parts
inside the package keeps the same fingerprint; a change to its content or a swapped image gives a new one.

Parts are followed through their relationships and types, not their usual folders, so packages that use absolute
targets (`/ppt/...`) or keep themes next to the masters are read the same way. Masters and layouts without a name are
matched by their part path.

`compare_with` (optional) is the path of a reference template (`.pptx` or `.potx`). The result then carries
`template_comparison`:

- `template_preserved`: true when the slide size is the same and no theme, master or layout of the deck differs from
  the reference, and no slide uses a layout outside it. Reference layouts that the deck dropped do not count against
  it.
- `identical_template_fingerprint`: every master, layout and theme is identical, including the unused ones.
- For themes, masters and layouts: `identical`, `changed_same_name` (an edited copy of a reference part),
  `missing_from_deck` and `not_in_reference`.
- `theme_color_changes` and `theme_font_changes` between the first theme of each file.
- `slides_on_layouts_outside_reference`: slides on an edited layout or on one that is not in the reference.

Limits: these are facts about package parts. Equal fingerprints do not prove that the slides render well, that
placeholders were filled sensibly or that the text is right. Rendering and visual review stay separate steps.

Example:

```json
{"path":"data/output/final.pptx","max_content_chars":24000}
{"path":"data/output/final.pptx","compare_with":"data/templates/brand.potx"}
```

Errors include stable `error_code` values such as `path_not_allowed`, `invalid_pdf`, `encrypted_pdf`, `invalid_package`, `file_too_large`, `part_too_large`, `file_changed`, and `probe_unavailable`.
