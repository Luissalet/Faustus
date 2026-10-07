# Deliverable inspection

`inspect_deliverable` is a read-only workspace tool for checking actual output files. It accepts a local `path` and an optional `max_content_chars` from 1,000 to 80,000 (default 24,000). Its result includes the resolved path, filename, extension, type, byte size, SHA-256, format facts, a bounded content payload, parser limitations, and an explicit inspection scope.

Supported extraction:

- PDF: page count, page dimensions, rotation, extracted page text, metadata, and AcroForm field count.
- PPTX: slide text in shape order, speaker notes, related media including SVG dimensions/viewBox, and cached chart titles, series, categories, and values.
- DOCX: paragraph text, tables, and embedded media inventory.
- XLSX: sheet order/names, cells, shared and inline strings, formulas, cached values, and formulas without cached values. Formulas are never recalculated.
- CSV/TSV: detected delimiter, header and rows, with a 20,000-row scan limit.
- Raster images/SVG: decoder metadata (dimensions, format, mode/frame count or SVG dimensions/viewBox).
- Audio/video: metadata provided by the existing media inspector; compressed formats may need FFprobe.

The tool reads files without modifying them. Office package relationships are treated as data; external relationships are ignored and macros are never run. Package parts, file sizes, entries, and returned content have explicit bounds. A hash is computed from the inspected file and a before/after stat check rejects files that changed during the operation.

This is structural and extractable-content evidence, not a quality gate. It does not render documents, verify visual layout, judge semantic correctness or completeness, recalculate spreadsheets, listen to media, transcribe audio, or interpret image pixels. A successful inspection means the listed parser facts were extracted; it does not certify that a deliverable meets its brief.

Example:

```json
{"path":"data/output/final.pptx","max_content_chars":24000}
```

Errors include stable `error_code` values such as `path_not_allowed`, `invalid_pdf`, `encrypted_pdf`, `invalid_package`, `file_too_large`, `part_too_large`, `file_changed`, and `probe_unavailable`.
