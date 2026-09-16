# PDF operations (`src/pdf_ops.py`, `pdf_ops` tool)

R4, ola Reach. Cierra el hueco que el resto de la capa PDF de Faustus no
cubría: `src/pdf_forms.py` extrae AcroForms (PyMuPDF, opcional),
`src/document_actions.py` detecta overflow visual y ya trae `split_pdf`/
`merge_pdfs` con presupuesto de páginas/bytes (ART-07), y
`src/chat_export_pdf.py` **genera** PDFs nuevos desde un transcript
(reportlab). Ninguno de los tres transforma un PDF existente con
merge/split/rotate/compress/watermark — eso es este módulo.

## Qué hay

Un módulo puro, `src/pdf_ops.py`, y una sola tool de agente, `pdf_ops`
(`src/agent_tools/pdf_ops_tool.py`), con `{"op": ..., ...}` y `op` como enum
en el schema (`src/tool_schemas.py`).

Operaciones:

| op | descripción |
|---|---|
| `page_count` | número de páginas |
| `merge` | concatena `inputs` (≥2 PDFs) — delega en `document_actions.merge_pdfs` |
| `split` | un PDF por cada rango de `ranges` (`"1-3"`, `"4-6"`...) — **no** duplica `document_actions.split_pdf` (ese agrupa por N páginas fijas con presupuesto; este trocea por rango arbitrario) |
| `extract_pages` | subconjunto de páginas a un nuevo PDF |
| `rotate` | rota páginas (todas o un subconjunto), múltiplo de 90° |
| `reorder` | permuta TODAS las páginas (cada una una vez) |
| `delete_pages` | quita páginas |
| `metadata` | lee, o escribe (title/author/subject/keywords) a un fichero nuevo |
| `compress` | `compress_content_streams()` + dedup de objetos; reporta bytes antes/después |
| `watermark_text` | texto diagonal en cada página (reportlab) |
| `to_images` | rasteriza a PNG — opcional: `pypdfium2` o `pdf2image` |
| `ocr` | capa de texto buscable — opcional: CLI `ocrmypdf` en PATH |

## Confinamiento de rutas

Todo pasa por `src.tool_execution._resolve_tool_path` — la MISMA guarda que
usan `read_file`/`write_file` (workspace activo del turno, o el allowlist
por defecto que incluye `DATA_DIR`/`DATA_DIR/uploads`/tmp). `pdf_ops.py` no
inventa una segunda política.

## Salida

Toda operación que produce un fichero nuevo lo escribe JUNTO al origen
(`report.pdf` → `report.rotated.pdf`) salvo que se pase `output`/`output_dir`
explícito. Nunca sobrescribe un INPUT salvo `"overwrite": true`.

## Dependencias opcionales

- `to_images`: `pypdfium2` (preferido) o `pdf2image` (necesita
  `poppler-utils` en PATH). Sin ninguno: error explícito con instrucción de
  instalación.
- `ocr`: CLI `ocrmypdf` (MPL-2.0, necesita Tesseract), probado con
  `shutil.which` y ejecutado con `subprocess.run(..., shell=False)`. Sin él:
  «instala ocrmypdf».

## Tests

`tests/test_r4_pdf_ops.py` (25 tests, PDFs generados con reportlab en el
propio test) y `tests/test_r4_pdf_ops_tool.py` (7 tests de la tool). `ocr`/
`to_images` se prueban con monkeypatch de `shutil.which`/`__import__` —
nunca tocan la red ni requieren binarios reales.

## Qué queda

- `compress` es deflate + dedup de pypdf, no re-muestreo de imágenes al
  estilo Ghostscript — el ahorro real depende de cuánto texto vs. imágenes
  tenga el PDF.
- `PATH_ARGUMENT_FIELDS` (`src/tool_schemas.py`) no se amplió con las rutas
  de `pdf_ops` — el reparador de argumentos (JSON-string→objeto, etc.) no
  cubre esta tool todavía; no bloquea nada, es una mejora futura.
