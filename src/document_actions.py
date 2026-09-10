"""
document_actions.py

Reusable document actions callable from both REST routes and the task scheduler.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

logger = logging.getLogger(__name__)

#: Points of slack before a glyph or table edge counts as "outside the page"
#: (ART-03). A hairline over the mediabox from font hinting/kerning rounding
#: is not what this check exists to catch — a whole word or column sitting
#: past the margin is.
_OVERFLOW_TOLERANCE_PT = 1.0


class DocumentActionError(ValueError):
    pass


def detect_page_overflow(pdf_path: str, *, tolerance: float = _OVERFLOW_TOLERANCE_PT) -> Dict[str, Any]:
    """ART-03: "un documento que abre pero tiene texto fuera de página o
    tablas cortadas no pasa control visual automáticamente."

    A PDF can open cleanly (pypdf/pdfplumber parse it, page count is sane)
    and still be visually broken — a long unwrapped line or an over-wide
    table pushed past the page's own mediabox. Opening successfully says
    nothing about that; this reads each page's actual character and table
    bounding boxes and compares them against the page's own dimensions.

    Returns ``{"pages": n, "overflow_pages": [...], "table_cut_pages": [...],
    "passes_visual_check": bool}`` — ``passes_visual_check`` is the gate a
    caller uses to withhold automatic pass/export approval; it is never True
    just because pypdf could parse the bytes.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise DocumentActionError("pdfplumber is not installed") from exc

    overflow_pages: List[Dict[str, Any]] = []
    table_cut_pages: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            for index, page in enumerate(pdf.pages):
                page_no = index + 1
                width, height = float(page.width), float(page.height)

                worst_char = None
                for char in page.chars:
                    over_right = char["x1"] - width
                    over_bottom = char["bottom"] - height
                    over_left = -char["x0"]
                    over_top = -char["top"]
                    worst = max(over_right, over_bottom, over_left, over_top)
                    if worst > tolerance and (worst_char is None or worst > worst_char["overflow_pt"]):
                        worst_char = {
                            "text": char.get("text", ""), "overflow_pt": round(worst, 1),
                            "bbox": [round(char["x0"], 1), round(char["top"], 1),
                                    round(char["x1"], 1), round(char["bottom"], 1)],
                        }
                if worst_char is not None:
                    overflow_pages.append({
                        "page": page_no, "reason": "text_outside_page",
                        "page_size": [round(width, 1), round(height, 1)],
                        "detail": worst_char,
                    })

                try:
                    tables = page.find_tables()
                except Exception:
                    tables = []
                for t_idx, table in enumerate(tables):
                    x0, top, x1, bottom = table.bbox
                    over_right = x1 - width
                    over_bottom = bottom - height
                    worst = max(over_right, over_bottom)
                    if worst > tolerance:
                        table_cut_pages.append({
                            "page": page_no, "reason": "table_cut",
                            "table_index": t_idx,
                            "page_size": [round(width, 1), round(height, 1)],
                            "table_bbox": [round(x0, 1), round(top, 1), round(x1, 1), round(bottom, 1)],
                            "overflow_pt": round(worst, 1),
                        })
    except DocumentActionError:
        raise
    except Exception as exc:
        raise DocumentActionError(f"could not open PDF for visual check: {exc}") from exc

    return {
        "pages": page_count,
        "overflow_pages": overflow_pages,
        "table_cut_pages": table_cut_pages,
        "passes_visual_check": not overflow_pages and not table_cut_pages,
    }


# ── ART-07: PDFs and complex files — split/merge with declared budgets ────
#
# "conversiones con presupuesto de tamaño y páginas": both operations refuse
# BEFORE writing anything (or, for split, before creating any output file)
# once a call would exceed its stated budget, rather than silently producing
# a huge merged file or thousands of one-page splits. Uses `pypdf`, already a
# dependency (see src/document_processor.py, src/pdf_forms.py) — no new one.

_DEFAULT_MAX_SPLIT_FILES = 200
_DEFAULT_MAX_MERGE_PAGES = 2000
_DEFAULT_MAX_MERGE_BYTES = 200 * 1024 * 1024


def split_pdf(input_path: str, output_dir: str, *, pages_per_file: int = 1,
             max_files: int = _DEFAULT_MAX_SPLIT_FILES) -> Dict[str, Any]:
    """Split `input_path` into consecutive `pages_per_file`-page PDFs under
    `output_dir`. Refuses up front (creates nothing) when the resulting file
    count would exceed `max_files` — the page/size budget ART-07 asks for."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise DocumentActionError("pypdf is not installed") from exc
    if pages_per_file < 1:
        raise DocumentActionError("pages_per_file must be at least 1")

    try:
        reader = PdfReader(input_path)
    except Exception as exc:
        raise DocumentActionError(f"could not open PDF: {exc}") from exc
    total_pages = len(reader.pages)
    n_files = max(1, -(-total_pages // pages_per_file))  # ceil div
    if n_files > max_files:
        raise DocumentActionError(
            f"splitting {total_pages} page(s) at {pages_per_file}/file would "
            f"produce {n_files} files, over the budget of {max_files}")

    os.makedirs(output_dir, exist_ok=True)
    outputs: List[Dict[str, Any]] = []
    for i in range(n_files):
        start = i * pages_per_file
        end = min(start + pages_per_file, total_pages)
        writer = PdfWriter()
        for p in range(start, end):
            writer.add_page(reader.pages[p])
        out_path = os.path.join(output_dir, f"part_{i + 1:03d}.pdf")
        with open(out_path, "wb") as fh:
            writer.write(fh)
        outputs.append({"path": out_path, "first_page": start + 1, "last_page": end})
    return {"input_pages": total_pages, "outputs": outputs}


def merge_pdfs(input_paths: Sequence[str], output_path: str, *,
               max_total_pages: int = _DEFAULT_MAX_MERGE_PAGES,
               max_total_bytes: int = _DEFAULT_MAX_MERGE_BYTES) -> Dict[str, Any]:
    """Concatenate `input_paths` in order into `output_path`. Refuses before
    writing anything when the inputs' combined byte size exceeds
    `max_total_bytes`, and refuses before the output is written when the
    combined page count exceeds `max_total_pages` — in both cases nothing
    lands on disk at `output_path`."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise DocumentActionError("pypdf is not installed") from exc
    if not input_paths:
        raise DocumentActionError("no input PDFs given")

    total_bytes_in = 0
    for p in input_paths:
        try:
            total_bytes_in += os.path.getsize(p)
        except OSError as exc:
            raise DocumentActionError(f"could not read {p}: {exc}") from exc
    if total_bytes_in > max_total_bytes:
        raise DocumentActionError(
            f"input PDFs total {total_bytes_in} bytes, over the budget of {max_total_bytes}")

    writer = PdfWriter()
    total_pages = 0
    for p in input_paths:
        try:
            reader = PdfReader(p)
        except Exception as exc:
            raise DocumentActionError(f"could not open {p}: {exc}") from exc
        total_pages += len(reader.pages)
        if total_pages > max_total_pages:
            raise DocumentActionError(
                f"merging {p} would bring the total past {max_total_pages} pages")
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as fh:
        writer.write(fh)
    return {"output_path": output_path, "pages": total_pages, "input_count": len(input_paths)}


_JUNK_TITLES = {
    "untitled", "untitled document", "new document", "document",
    "new email", "new mail", "new message", "reply", "fwd", "re:",
    "test", "testing", "asdf", "asd", "foo", "bar", "baz",
    "tmp", "temp", "scratch", "scratchpad", "draft", "delete",
    "remove", "junk", "trash", "xxx", "abc", "qwerty",
}


def _norm_title(t: str) -> str:
    """Normalize a title for grouping: trim, collapse whitespace, lowercase."""
    t = t if isinstance(t, str) else ""
    return re.sub(r"\s+", " ", t.strip()).lower()


def _content_fingerprint(content: str) -> str:
    """A stable fingerprint of document content for duplicate detection.

    Strips bits that differ between otherwise-identical copies — chiefly the
    `upload_id` of a re-imported PDF and the random `id=` of annotations — so
    that N imports of the same file collapse to one fingerprint. Whitespace is
    collapsed and the result lowercased.
    """
    c = content if isinstance(content, str) else ""
    c = re.sub(r'upload_id="[^"]*"', "upload_id", c)          # pdf_source re-imports
    c = re.sub(r"\bid=ann-[A-Za-z0-9_-]+", "id=ann", c)        # annotation ids
    c = re.sub(r"\s+", " ", c).strip().lower()
    return c


def _real_len(content: str) -> int:
    """Length of content with markdown noise stripped — a 'completeness' proxy."""
    content = content if isinstance(content, str) else ""
    stripped = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)
    stripped = re.sub(r"[*_`>\-=]+", "", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return len(stripped)


async def run_document_tidy(owner: str) -> str:
    """Remove clearly-junk documents and redundant duplicates for an owner.

    Conservative rules (no length-based deletion — short notes are valid):
    - Empty / whitespace-only / placeholder ("# Untitled")
    - Title is a throwaway name (test, asdf, …) or the content itself is one
    - Email reply-chain with no original content
    - Duplicates: docs sharing the same normalized title AND the same content
      fingerprint (ignoring volatile upload/annotation ids). The most complete
      copy (longest real content, then most recent) is kept; the rest deleted.
    """
    from core.database import SessionLocal, Document, Session as DbSession

    db = SessionLocal()
    try:
        if owner:
            # Documents now carry their own owner column (robust to a deleted
            # session). Match on it directly; orphaned legacy rows are swept
            # to the admin at boot so they're attributed too.
            docs = db.query(Document).filter(Document.owner == owner).all()
        else:
            docs = db.query(Document).all()

        deleted_examples = []
        deleted = 0
        kept = 0
        survivors = []  # docs that pass the junk rules, considered for dedup
        now = datetime.now(timezone.utc)

        for doc in docs:
            created = doc.created_at
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)

            # Skip freshly created documents to avoid deleting them while the user is actively editing
            if created and (now - created).total_seconds() < 900:  # 15 minutes
                survivors.append(doc)
                continue

            content = (doc.current_content or "").strip()
            title = (doc.title or "").strip().lower()
            is_fresh_empty = (
                not content
                and created is not None
                and (now - created).total_seconds() < 1800
            )
            if is_fresh_empty:
                survivors.append(doc)
                continue

            # Strip markdown noise to get "real" character count
            stripped = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)  # headers
            stripped = re.sub(r"[*_`>\-=]+", "", stripped)  # markdown chars
            stripped = re.sub(r"\s+", " ", stripped).strip()
            real_len = len(stripped)

            # Detect emails-saved-as-documents (quote chains with no original content)
            lines = [ln for ln in content.split("\n") if ln.strip()]
            quoted_lines = [ln for ln in lines if ln.lstrip().startswith(">")]
            header_lines = [ln for ln in lines if re.match(r"^On .+ wrote:?\s*$", ln.strip())]
            non_quote_content = "\n".join(
                ln for ln in lines
                if not ln.lstrip().startswith(">")
                and not re.match(r"^On .+ wrote:?\s*$", ln.strip())
            ).strip()
            quote_ratio = len(quoted_lines) / max(len(lines), 1)

            should_delete = False
            reason = ""

            if not content or content in ("", "# Untitled"):
                should_delete = True
                reason = "empty"
            elif title in _JUNK_TITLES:
                # If you named it "test" or "asdf" etc, you don't care about it
                should_delete = True
                reason = f"junk title '{title}'"
            elif stripped.lower() in _JUNK_TITLES:
                should_delete = True
                reason = "throwaway content"
            # No length-based deletion: short notes are legitimate content.
            elif (quoted_lines or header_lines) and len(non_quote_content) < 50 and quote_ratio > 0.4:
                # Email reply chain with no original content
                should_delete = True
                reason = "email quote-chain only"

            if should_delete:
                if len(deleted_examples) < 5:
                    label = (doc.title or "(no title)")[:40]
                    deleted_examples.append(f"{label} ({reason})")
                db.delete(doc)
                deleted += 1
            else:
                survivors.append(doc)

        # --- Duplicate pass: group survivors by (normalized title, content
        # fingerprint) and keep only the most complete copy of each group. ---
        groups: dict = {}
        for doc in survivors:
            key = (_norm_title(doc.title), _content_fingerprint(doc.current_content))
            groups.setdefault(key, []).append(doc)

        for (title_key, _fp), members in groups.items():
            if len(members) < 2:
                kept += 1
                continue
            # Keep the most complete (longest real content), then most recent.
            def _updated(d):
                return d.updated_at or d.created_at
            # Sort key must be total-order safe: a document with both
            # updated_at and created_at NULL would otherwise make Python
            # compare None against a datetime on a real-length tie, raising
            # TypeError and aborting the whole tidy run. Rank "has a
            # timestamp" before the timestamp itself so a None is never
            # compared against a datetime.
            members.sort(
                key=lambda d: (
                    _real_len(d.current_content),
                    _updated(d) is not None,
                    _updated(d) or datetime.min,
                ),
                reverse=True,
            )
            keeper = members[0]
            kept += 1
            dupes = members[1:]
            if len(deleted_examples) < 5:
                label = (keeper.title or "(no title)")[:40]
                deleted_examples.append(f"{label} (+{len(dupes)} duplicate copies)")
            for d in dupes:
                db.delete(d)
                deleted += 1

        if deleted:
            db.commit()

        if deleted == 0:
            # Use sentinel so the scheduler can drop the run row entirely.
            from src.builtin_actions import TaskNoop
            raise TaskNoop(f"scanned {len(docs)} document(s), no junk")
        preview = "; ".join(deleted_examples)
        extra = f" (+{deleted - len(deleted_examples)} more)" if deleted > len(deleted_examples) else ""
        return f"Removed {deleted} of {len(docs)}: {preview}{extra} · {kept} kept"
    finally:
        db.close()


# ── ART-02: warn before a lossy DOCX -> markdown conversion ───────────────
#
# The editor stores every document as markdown text (`Document.current_content`
# — see routes/document/document_routes.py), so opening a DOCX for editing
# today means converting it once on import. That conversion cannot carry a
# comment, a tracked change, or a numbering definition through to markdown —
# none of those have a markdown representation — and it silently drops them.
# `analyze_docx_preservation` is the check ART-02 asks for: run it BEFORE the
# conversion and show the caller exactly what would be lost, so a user can
# cancel instead of losing a reviewer's comments without ever being told.
def analyze_docx_preservation(path: str) -> Dict[str, Any]:
    """What a markdown round-trip of this DOCX cannot preserve.

    Returns ``{"paragraph_count", "image_count", "has_comments",
    "has_tracked_changes", "numbered_paragraph_count",
    "not_preservable_on_markdown_conversion": [...],
    "safe_to_convert_silently": bool}``. ``safe_to_convert_silently`` is
    False the moment ANY of comments/tracked-changes/numbering/images is
    present — the caller's cue to show the warning and let the user cancel,
    per ART-02's acceptance criterion, rather than converting first and
    explaining later.
    """
    import zipfile

    try:
        import docx
    except ImportError as exc:
        raise DocumentActionError("python-docx is not installed") from exc

    try:
        document = docx.Document(path)
    except Exception as exc:
        raise DocumentActionError(f"could not open DOCX: {exc}") from exc

    has_comments = False
    has_tracked_changes = False
    numbered_paragraphs = 0
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            has_comments = "word/comments.xml" in names
            if "word/document.xml" in names:
                xml = zf.read("word/document.xml").decode("utf-8", "ignore")
                has_tracked_changes = bool(re.search(r"<w:(ins|del)[ >]", xml))
                # Direct (inline) numbering: <w:numPr> in the paragraph itself.
                numbered_paragraphs = len(re.findall(r"<w:numPr[ >]", xml))
    except zipfile.BadZipFile as exc:
        raise DocumentActionError(f"not a valid DOCX (zip) file: {exc}") from exc

    # Numbering applied via a list PARAGRAPH STYLE (e.g. Word's built-in
    # "List Number"/"List Bullet") carries no <w:numPr> in the paragraph
    # itself — the numId lives in the style definition in styles.xml
    # instead. Direct-numPr detection alone misses this common case, so any
    # paragraph using a list-shaped style also counts.
    for paragraph in document.paragraphs:
        style = getattr(paragraph, "style", None)
        name = (getattr(style, "name", "") or "").lower()
        if "list" in name and ("number" in name or "bullet" in name):
            numbered_paragraphs += 1

    image_count = len(document.inline_shapes)

    lost: List[str] = []
    if has_comments:
        lost.append("comments")
    if has_tracked_changes:
        lost.append("tracked_changes")
    if numbered_paragraphs:
        lost.append("numbering")
    if image_count:
        lost.append("images")

    return {
        "paragraph_count": len(document.paragraphs),
        "image_count": image_count,
        "has_comments": has_comments,
        "has_tracked_changes": has_tracked_changes,
        "numbered_paragraph_count": numbered_paragraphs,
        "not_preservable_on_markdown_conversion": lost,
        "safe_to_convert_silently": not lost,
    }
