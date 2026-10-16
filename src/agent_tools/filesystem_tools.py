import asyncio
import hashlib
import json
import logging
import os
import re
import difflib
import fnmatch
import shutil
import time
from typing import Optional, Dict, Any, Tuple, List

from src import read_plan
from src.file_mutation_locks import canonical_path, mutation_lock, mutation_locks
from src.constants import MAX_READ_CHARS, MAX_DIFF_LINES, MAX_OUTPUT_CHARS

logger = logging.getLogger(__name__)

_CODENAV_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".cache", "site-packages", ".idea", ".tox",
})
# The numbers the tool authority declares for grep/glob/ls: one source, so the
# limit a descriptor states is the limit this module applies.
from src.tool_authority_specs import CODENAV_MAX_HITS as _CODENAV_MAX_HITS
from src.tool_authority_specs import CODENAV_MAX_LINE_CHARS as _CODENAV_MAX_LINE

# The Python grep fallback (no ripgrep on PATH) gets the same wall-clock budget
# as ripgrep's subprocess timeout, and never reads a file that cannot be source
# text: on a workspace that holds model weights a single unbounded walk used
# to stall a whole agent turn for more than ten minutes.
GREP_FALLBACK_BUDGET_S = 20.0
GREP_FALLBACK_MAX_FILE_BYTES = 2 * 1024 * 1024
GREP_FALLBACK_MAX_FILES = 50_000


# ripgrep stderr is drained in its own thread and only this many bytes are kept:
# a chatty rg (hundreds of KB of warnings) must never fill the OS pipe buffer and
# deadlock the search until the timeout.
GREP_STDERR_KEEP_BYTES = 8 * 1024


def _utf8_bounded(text: str, max_bytes: int, *, keep_end: bool = False) -> str:
    """Trim `text` to at most `max_bytes` of its UTF-8 encoding, never mid-codepoint.

    The stderr budget is declared in bytes, so it must be enforced in bytes:
    astral-plane characters (U+1F525 is 4 bytes in UTF-8) would otherwise retain
    several times the cap while counting as few characters.
    """
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    raw = raw[-max_bytes:] if keep_end else raw[:max_bytes]
    # Drop an incomplete trailing (or leading) multi-byte sequence.
    return raw.decode("utf-8", "ignore")


def _bounded_stderr_chunks(chunks, keep_bytes: int):
    """Reduce stderr text chunks to (head, tail), each at most keep_bytes // 2 UTF-8 bytes.

    Head and tail are both kept because rg prints a generic header first and the
    cause ("error: ...") last, which a single huge warning line can push out of a
    head-only buffer. The caller keeps draining the pipe past the budget; this
    only decides what is retained.
    """
    half = keep_bytes // 2
    head: List[str] = []
    tail: List[str] = []
    kept = 0
    for chunk in chunks:
        if kept < half:
            part = _utf8_bounded(chunk, half - kept)
            head.append(part)
            kept += len(part.encode("utf-8", "replace"))
        tail.append(chunk)
        if sum(len(c.encode("utf-8", "replace")) for c in tail) > half:
            tail[:] = [_utf8_bounded("".join(tail), half, keep_end=True)]
    return head, tail


def _glob_to_regex(pat: str) -> "re.Pattern":
    """Translate a forward-slash glob (**, *, ?) into a compiled regex.
    `**/` matches zero or more complete directories.
    `*` matches within a single path segment (does not cross /).
    """
    i, n, out = 0, len(pat), []
    while i < n:
        if pat[i : i + 3] == "**/":
            out.append("(?:[^/]+/)*")
            i += 3
        elif pat[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out))

def _unified_diff(old: str, new: str, path: str) -> Optional[Dict[str, Any]]:
    if old == new:
        return None
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    label = path or "file"
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{label}", tofile=f"b/{label}",
        lineterm="",
    ))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    truncated = False
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_lines = diff_lines[:MAX_DIFF_LINES]
        truncated = True
    text = "\n".join(diff_lines)
    if truncated:
        text += f"\n… diff truncated at {MAX_DIFF_LINES} lines"
    return {
        "text": text,
        "added": added,
        "removed": removed,
        "new_file": old == "",
        "file": os.path.basename(path) or (path or "file"),
    }

_BINARY_PROBE_BYTES = 8192


def _looks_binary(path: str) -> bool:
    """A NUL byte in the first 8 KB: not text in any encoding a model reads."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_BINARY_PROBE_BYTES)
    except OSError:
        return False


def _utf16_text(path: str) -> Optional[str]:
    """A UTF-16 file (with its byte-order mark, as Windows tools write them)
    decoded, or None. Its NUL bytes are not a sign of a binary file."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(2)
            if head not in (b"\xff\xfe", b"\xfe\xff"):
                return None
            fh.seek(0)
            return fh.read(MAX_READ_CHARS * 2 + 4).decode("utf-16", errors="replace")
    except OSError:
        return None


def _document_result(path: str, shown: str, owner: Optional[str],
                     offset: int = 0, limit: int = 0) -> Optional[Dict[str, Any]]:
    """read_file on a PDF or an Office/EPUB file returns its text.

    Seen live: `read_file contrato.pdf` handed the model the raw PDF bytes
    (`%PDF-1.4 … /Type /Font …`); it then wrote a pypdf script to get at the
    text and stopped on an approval card for it. Other binaries answer with
    what they are instead of pages of mojibake. None = a plain text file,
    read as before."""
    if not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    text: Optional[str] = None
    note = ""
    if ext == ".pdf":
        from src.document_processor import _process_pdf, strip_pdf_content_marker
        text = strip_pdf_content_marker(_process_pdf(path, owner=owner, allow_vision=False)).strip()
        note = (f"{shown}: PDF, text extracted page by page. A long PDF: pdf_outline, then "
                f"pdf_read_section. A page that is only an image: inspect_image with that page.")
        if "needs_ocr=true" in text:
            note += " Some pages have no text layer; look at them with inspect_image."
    else:
        from src.markitdown_runtime import is_markitdown_format, convert_to_markdown
        if is_markitdown_format(path):
            text = convert_to_markdown(path)
            if not text or not text.strip():
                return {"error": f"read_file: {shown}: no text could be extracted from this "
                                 f"{ext[1:].upper()} document", "exit_code": 1}
            note = f"{shown}: {ext[1:].upper()} document, converted to Markdown."
        elif _utf16_text(path) is not None:
            text = _utf16_text(path) or ""
            note = f"{shown}: UTF-16 text."
        elif _looks_binary(path):
            size = os.path.getsize(path)
            # Not a failure: what the file is IS the answer to "read it".
            return {"output": f"{shown}: binary file ({ext or 'no extension'}, {size} bytes); "
                              "it has no text to read. Use a tool made for this kind of file.",
                    "exit_code": 0}
        else:
            return None
    if offset > 0 or limit > 0:
        lines = text.splitlines(keepends=True)
        start = max(offset, 1) - 1
        text = "".join(lines[start:start + limit] if limit > 0 else lines[start:])
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n... [truncated at {MAX_READ_CHARS} chars; use offset/limit]"
    return {"output": f"{note}\n\n{text}", "exit_code": 0}


def _read_text_lf(path: str):
    """Read a text file without newline translation and return
    (text with LF line endings, had_crlf, revision). Models quote text with
    "\\n"; Windows files carry "\\r\\n" — matching happens on the LF form and
    the file is written back with its own convention (see _write_text_lf).
    `revision` (EDIT-01, format `sha256:<hex>` per spec §34.2) is the hash of
    the exact bytes on disk at read time, re-derived from the decoded text
    rather than a second binary read — the file was just opened as UTF-8, so
    encoding it back reproduces the same bytes without racing the first read."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = "\r\n" in raw
    revision = sha256_revision(raw.encode("utf-8"))
    return (raw.replace("\r\n", "\n") if crlf else raw), crlf, revision


def _write_text_lf(path: str, text_lf: str, crlf: bool) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text_lf.replace("\n", "\r\n") if crlf else text_lf)


def sha256_revision(data: bytes) -> str:
    """EDIT-01 `base_revision` / anchor format, per spec §34.2: `sha256:<hex>`."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _normalize_base_revision(raw: object) -> str:
    """The `base_revision` a model sent, in the one form the check accepts.

    `sha256:<hex>` is kept; a bare 64-hex digest (the form `read_file`
    prints in some renderings, and the form models copy most often) gets
    its prefix; anything else — "latest", "current", "none", a truncated
    hash, a made-up token — is treated as ABSENT, i.e. the unverified write
    every call without a base_revision already gets. Until 14-09-2026 that
    last case was a hard error ("must look like 'sha256:<hex>'"), which cost
    a whole round to learn what a precondition the call never needed was
    supposed to look like."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if _REVISION_RE.match(text):
        return text
    if _BARE_SHA256_RE.match(text):
        return "sha256:" + text.lower()
    if text.lower().startswith("sha256:") and _BARE_SHA256_RE.match(text[7:]):
        return "sha256:" + text[7:].lower()
    return ""
#: How much of each side of a base/current/proposed conflict report to keep.
#: "recortado" (trimmed) per EDIT-01 — enough to reconcile by eye, not a full
#: file dump.
_CONFLICT_EXCERPT_CHARS = 4000


def _excerpt(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= _CONFLICT_EXCERPT_CHARS:
        return text
    return text[:_CONFLICT_EXCERPT_CHARS] + f"\n... [truncated at {_CONFLICT_EXCERPT_CHARS} chars]"


def _base_revision_conflict(tool: str, path: str, base_revision: str,
                             current_revision: Optional[str], current_text: str,
                             base_text: Optional[str], proposed_text: str) -> Optional[Dict[str, Any]]:
    """EDIT-01: refuse a write whose `base_revision` no longer matches the
    file on disk, before touching it. Returns a `conflict` result carrying a
    trimmed base/current/proposed diff so the caller can reconcile instead of
    silently overwriting whatever changed underneath it; `None` when
    `base_revision` is empty (today's unverified behavior — kept for
    compatibility) or still matches.

    `base_text` is only ever text this call itself supplied (the `old_string`
    it quoted, a patch hunk's context) — nothing is stored between calls, so
    when there is no such text (a whole-file `write_file`) the three-way
    report says so rather than inventing content."""
    if not base_revision:
        return None
    if current_revision == base_revision:
        return None
    diff = _unified_diff(base_text, current_text, path) if base_text is not None else None
    result: Dict[str, Any] = {
        "error": f"{tool}: {path} changed since base_revision was read "
                 f"(expected {base_revision}, current is "
                 f"{current_revision or 'missing — the file no longer exists'})",
        "exit_code": 1,
        "status": "conflict",
        "error_code": "BASE_REVISION_MISMATCH",
        "next_action": "read_current_and_reconcile",
        "base_revision": base_revision,
        "current_revision": current_revision,
        "three_way": {
            "base": _excerpt(base_text),
            "current": _excerpt(current_text),
            "proposed": _excerpt(proposed_text),
        },
    }
    if base_text is None:
        result["three_way"]["base_unavailable_reason"] = (
            "only the base's hash was recorded, not its text; nothing is "
            "stored between tool calls")
    if diff:
        result["three_way_diff"] = diff
    return result


def _replace_text(original_lf: str, old_lf: str, new_lf: str, replace_all: bool
                   ) -> Tuple[str, Optional[str]]:
    """Pure `edit_file` replace logic, extracted so a doubt-review preview
    (which must NOT write anything) and the real `_apply()` write use the
    exact same matching rules. Returns `(status, updated_text_or_None)` with
    status "ok" / "not_found" / "not_unique:<n>"."""
    count = original_lf.count(old_lf)
    if count == 0:
        return "not_found", None
    if count > 1 and not replace_all:
        return f"not_unique:{count}", None
    updated = original_lf.replace(old_lf, new_lf) if replace_all else original_lf.replace(old_lf, new_lf, 1)
    return "ok", updated


def _effective_preview_review(gate: Optional[Dict[str, Any]]) -> bool:
    """A skipped, failed or unparsed reviewer must retain existing fail-open behavior."""
    review = gate.get("review") if isinstance(gate, dict) else None
    return (isinstance(review, dict) and review.get("verdict") in ("ok", "concerns")
            and not review.get("error") and not review.get("unparsed"))


def _review_preview_conflict(tool: str, path: str, expected: Optional[str],
                             current: Optional[str]) -> Optional[Dict[str, Any]]:
    """None denotes proven absence only; callers keep unknown reads unarmed."""
    if expected == current:
        return None
    return {"error": f"{tool}: {path} changed since the reviewed preview was read",
            "exit_code": 1, "status": "conflict", "error_code": "REVIEW_PREVIEW_MISMATCH",
            "source": "review_preview", "preview_revision": expected,
            "current_revision": current, "next_action": "read_current_and_reconcile"}


def _external_writer_check_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_external_writer_check", True))
    except Exception:  # noqa: BLE001
        return True


def _identity_conflict(tool: str, path: str, expected) -> Optional[Dict[str, Any]]:
    """Refuse an apply when the file's size or mtime differ from the preview's.

    The content revision already catches changed bytes; this also catches a
    rewrite that restored the same bytes (mtime) and is checked inside the
    same mutex, right after the revision comparison. ``expected`` is the
    fingerprint taken when the preview was read, or None when none was taken.
    """
    if expected is None or not _external_writer_check_enabled():
        return None
    from src import resource_claims as _rc
    current = _rc.stat_identity(path)
    changed = _rc.drift(expected, current)
    if not changed or changed == ["unknown"]:
        return None
    return _rc.external_writer_conflict(tool, path, expected, current, changed)


class EditFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        replace_all = args.get("replace_all", False)
        if type(replace_all) is not bool:
            return {"error": "edit_file: replace_all must be a boolean", "exit_code": 1}
        raw_path = (args.get("path") or "").strip()
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        confirm_risky = bool(args.get("confirm_risky", False))
        base_revision = _normalize_base_revision(args.get("base_revision"))
        if not raw_path:
            return {"error": "edit_file: path required", "exit_code": 1}
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"edit_file: {e}", "exit_code": 1}
        if old == "":
            return {"error": "edit_file: old_string required (use write_file to create a file)", "exit_code": 1}
        if old == new:
            return {"error": "edit_file: old_string and new_string are identical", "exit_code": 1}

        # Models quote text with "\n"; Windows files carry "\r\n". Match on
        # LF-normalized text and write the file back with the line endings it
        # already had — never rewrite a whole file's endings on a one-line edit.
        old_lf = old.replace("\r\n", "\n")
        new_lf = new.replace("\r\n", "\n")

        # Doubt review (src/doubt_review.py) block mode: preview the edit
        # WITHOUT writing, so a "concerns" verdict can refuse the write
        # entirely. Advisory mode (the default when doubt review is on)
        # needs no pre-write pass — it never blocks, so the section is
        # appended after the real write below.
        _dr_gate = None
        _preview_known, _preview_revision, _preview_identity = False, None, None
        try:
            from src import doubt_review as _doubt_review
        except Exception:  # noqa: BLE001
            _doubt_review = None
        if _doubt_review is not None and _doubt_review.enabled() and _doubt_review.block_mode_enabled():
            try:
                _preview, _pcrlf, _preview_revision = await asyncio.to_thread(_read_text_lf, path)
                _preview_known = True
                from src import resource_claims as _rc
                _preview_identity = _rc.stat_identity(path)
                _pstatus, _pupdated = _replace_text(_preview, old_lf, new_lf, replace_all)
                if _pstatus == "ok":
                    _pdiff = _unified_diff(_preview, _pupdated, path)
                    _dr_gate = await _doubt_review.check_edit(ctx, path, raw_path, _pdiff,
                                                              confirm_risky=confirm_risky)
            except FileNotFoundError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("[doubt_review] pre-write gate failed for %s", path, exc_info=True)
        if _dr_gate is not None and not _dr_gate.get("proceed", True):
            return _dr_gate["result"]
        _review_guard = _preview_known and _effective_preview_review(_dr_gate)

        def _apply():
            """Read, check the base_revision precondition (EDIT-01), replace
            and write under the mutex shared with write_file in this process."""
            with mutation_lock(path):
                try:
                    original, crlf, revision_now = _read_text_lf(path)
                except FileNotFoundError:
                    if _review_guard:
                        conflict = _review_preview_conflict("edit_file", path, _preview_revision, None)
                        if conflict is not None:
                            return "", conflict, "conflict"
                    raise
                if _review_guard:
                    conflict = _review_preview_conflict("edit_file", path, _preview_revision, revision_now)
                    if conflict is None:
                        conflict = _identity_conflict("edit_file", path, _preview_identity)
                    if conflict is not None:
                        return original, conflict, "conflict"
                if base_revision:
                    conflict = _base_revision_conflict(
                        "edit_file", path, base_revision, revision_now,
                        current_text=original, base_text=old_lf, proposed_text=new_lf)
                    if conflict is not None:
                        return original, conflict, "conflict"
                status, updated = _replace_text(original, old_lf, new_lf, replace_all)
                if status != "ok":
                    return original, None, status
                _write_text_lf(path, updated, crlf)
                written = updated.replace("\n", "\r\n") if crlf else updated
                return original, (updated, sha256_revision(written.encode("utf-8"))), "ok"

        try:
            original, updated, status = await asyncio.to_thread(_apply)
        except FileNotFoundError:
            from src.agent_harness import not_found_error
            from src.tool_execution import get_active_workspace
            return {
                "error": not_found_error("edit_file", raw_path, path, get_active_workspace())
                + " (edit_file only changes EXISTING files; use write_file to create a new one.)",
                "exit_code": 1,
                "not_found": True,
            }
        except (IsADirectoryError, UnicodeDecodeError):
            return {"error": f"edit_file: {path}: not an editable text file", "exit_code": 1}
        except PermissionError:
            return {"error": f"edit_file: {path}: permission denied", "exit_code": 1}
        except OSError as e:
            return {"error": f"edit_file: {path}: {e}", "exit_code": 1}

        if status == "conflict":
            return updated  # `updated` holds the conflict payload for this status
        if status == "not_found":
            # Local models mostly miss by whitespace/indentation or by quoting a
            # paraphrase. Point at the closest real region so the retry can copy
            # it verbatim instead of guessing again.
            hint = ""
            try:
                import difflib
                probe = next((ln for ln in old_lf.splitlines() if ln.strip()), old_lf.strip())[:200]
                file_lines = original.splitlines()
                best_ratio, best_idx = 0.0, -1
                for i, line in enumerate(file_lines):
                    if not line.strip():
                        continue
                    r = difflib.SequenceMatcher(None, probe.strip(), line.strip()).ratio()
                    if r > best_ratio:
                        best_ratio, best_idx = r, i
                if best_idx >= 0 and best_ratio >= 0.55:
                    lo, hi = max(0, best_idx - 1), min(len(file_lines), best_idx + 3)
                    excerpt = "\n".join(f"{n + 1}: {file_lines[n]}" for n in range(lo, hi))
                    hint = (f" Closest match (similarity {best_ratio:.2f}) is around line {best_idx + 1}:\n{excerpt}\n"
                            "Copy the exact text from the file (indentation and quotes included) into old_string, "
                            "or read_file with offset/limit around that line first.")
                elif original.strip():
                    hint = " Nothing similar found — the text you quoted is not in this file; read_file it before editing."
            except Exception:
                hint = ""
            return {"error": f"edit_file: old_string not found in {path}.{hint}", "exit_code": 1}
        if status.startswith("not_unique"):
            n = status.split(":", 1)[1]
            return {"error": f"edit_file: old_string is not unique in {path} ({n} matches). Add surrounding context or set replace_all=true.", "exit_code": 1}

        n = original.count(old_lf)
        updated_text, new_revision = updated
        result = {"output": f"Edited {path} ({n} replacement{'s' if n != 1 else ''})", "exit_code": 0,
                  "revision": new_revision}
        if base_revision:
            result["base_revision"] = base_revision
        else:
            result["unverified_base"] = True
        diff = _unified_diff(original, updated_text, path)
        if diff:
            result["diff"] = diff
        # EDIT-05: best-effort history entry — never affects this result.
        try:
            record_edit_history(path, tool="edit_file",
                                pre_revision=sha256_revision(original.encode("utf-8")),
                                post_revision=new_revision,
                                pre_bytes=original.encode("utf-8"))
        except Exception:
            logger.debug("[edit_history] hook failed for edit_file on %s", path, exc_info=True)
        # Doubt review: append the "Second look..." section. `_dr_gate` is
        # already set (from the pre-write pass) in block mode; advisory mode
        # runs the check now, against the diff that actually landed.
        if _doubt_review is not None and _doubt_review.enabled():
            try:
                if _dr_gate is None:
                    _dr_gate = await _doubt_review.check_edit(ctx, path, raw_path, diff or {},
                                                              confirm_risky=confirm_risky)
                if _dr_gate.get("section"):
                    result["output"] = f"{result['output']}\n\n{_dr_gate['section']}"
                if _dr_gate.get("review") is not None:
                    result["doubt_review"] = _dr_gate["review"]
            except Exception:  # noqa: BLE001
                logger.debug("[doubt_review] post-write section failed for %s", path, exc_info=True)
        return result

_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _image_result(path: str, shown: str) -> Optional[Dict[str, Any]]:
    """read_file on a picture returns the picture. Seen live: after saving a
    chart, the agent reached for `desktop_screenshot` to check it - a capture
    of the user's screen, which does not even show the file. A tool result
    with `images` is attached for the model to see (or described, when it
    cannot see), the same path screenshots take."""
    import base64

    mime = _IMAGE_TYPES.get(os.path.splitext(path)[1].lower())
    if mime is None or not os.path.isfile(path):
        return None
    size = os.path.getsize(path)
    if size > _MAX_IMAGE_BYTES:
        return {"error": f"read_file: {shown}: image of {size} bytes is too large to view "
                         f"(limit {_MAX_IMAGE_BYTES})", "exit_code": 1}
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    return {
        "output": f"{shown}: {mime.split('/')[1].upper()} image, {size} bytes - attached so you can see it.",
        "exit_code": 0,
        "images": [{"data": data, "mimeType": mime}],
    }


class ReadFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        raw_path, offset, limit = content.split("\n", 1)[0].strip(), 0, 0
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                raw_path = str(_a.get("path", "")).strip()
                offset = int(_a.get("offset") or 0)
                limit = int(_a.get("limit") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"read_file: {e}", "exit_code": 1}
        # An un-ranged read of a file too big to return whole answers with a map
        # of it (line count, symbol index, the first lines, the call that fetches
        # any other part) instead of a blind slice off the top — src/read_plan.py.
        # A ranged read, and any file that fits, are untouched below.
        ranged = offset > 0 or limit > 0
        window_tokens = 0 if ranged else read_plan.resolve_window_tokens(ctx)
        # On Windows opening a directory raises PermissionError rather than
        # IsADirectoryError.  Classify it before open() so the agent gets the
        # actionable, platform-independent instruction it has always been
        # promised instead of being told the folder's ACL is wrong.
        if os.path.isdir(path):
            return {"error": f"read_file: {path}: is a directory (use ls)",
                    "exit_code": 1}
        image = await asyncio.to_thread(_image_result, path, raw_path or path)
        if image is not None:
            return image
        try:
            document = await asyncio.to_thread(
                _document_result, path, raw_path or path,
                str(ctx.get("owner") or "") or None, offset, limit)
        except Exception as exc:  # noqa: BLE001 - extraction must not hide the file
            logger.warning("read_file: text extraction failed for %s: %s", path, exc)
            document = {"error": f"read_file: {raw_path or path}: could not extract its text ({exc})",
                        "exit_code": 1}
        if document is not None:
            return document
        try:
            def _read():
                if ranged:
                    start = max(offset, 1)
                    out, n, budget = [], 0, MAX_READ_CHARS
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if i < start:
                                continue
                            if limit > 0 and n >= limit:
                                break
                            out.append(line)
                            n += 1
                            budget -= len(line)
                            if budget <= 0:
                                out.append(f"\n... [truncated at {MAX_READ_CHARS} chars]")
                                break
                    # end=start when n==0 (offset past EOF): an empty, still
                    # well-formed one-line locator rather than an inverted range.
                    return "".join(out), MAX_READ_CHARS, start, start + n - 1 if n else start
                plan = read_plan.plan(path, window_tokens, display_path=raw_path or path)
                if plan.output is not None:
                    return plan.output, plan.budget_chars, 1, None
                # The file fits, or the outline is switched off: read it exactly
                # as before, under the cap this plan settled on.
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read(plan.budget_chars + 1), plan.budget_chars, 1, None
            data, cap, start_line, end_line = await asyncio.to_thread(_read)
        except FileNotFoundError:
            from src.agent_harness import not_found_error
            from src.tool_execution import get_active_workspace
            return {
                "error": not_found_error("read_file", raw_path, path, get_active_workspace()),
                "exit_code": 1,
                "not_found": True,
            }
        except PermissionError:
            return {"error": f"read_file: {path}: permission denied", "exit_code": 1}
        except IsADirectoryError:
            return {"error": f"read_file: {path}: is a directory (use ls)", "exit_code": 1}
        except OSError as e:
            return {"error": f"read_file: {path}: {e}", "exit_code": 1}
        if not ranged and len(data) > cap:
            data = data[:cap] + f"\n... [truncated at {cap} chars]"
        if end_line is None:
            # Unranged (whole file, or read_plan's outline): the line count of
            # what actually made it into `data`, after truncation.
            end_line = len(data.splitlines()) or start_line
        result: Dict[str, Any] = {"output": data, "exit_code": 0}
        # CTX-03/EDIT-01 (lote 18/19 integration): pin what was actually shown
        # as evidence, and hash the file as it stands right now so the model
        # can pass this back as `base_revision` on its next write_file/
        # edit_file/apply_patch — the same format edit_file's own `revision`
        # already returns. Best-effort: a read that succeeded must not be
        # turned into a failure by evidence bookkeeping (mirrors
        # question_store.open_question's try/except in src/agent_loop.py).
        try:
            from src.context_ledger import evidence_for_read
            evidence = evidence_for_read(
                raw_path or path, start_line, end_line, data,
                owner_id=str(ctx.get("owner") or "") or "system",
                project_id=(str(ctx.get("project_id") or "") or None),
            )
            result["evidence_refs"] = [evidence.to_mapping()]
            _, _, revision_now = await asyncio.to_thread(_read_text_lf, path)
            result["revision"] = revision_now
        except Exception:
            pass
        return result

class WriteFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        lines = content.split("\n", 1)
        raw_path = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        # Decode JSON-object args (the fenced inline-args shape
        # ```write_file {"path": "...", "content": "..."}```), matching
        # ReadFileTool above. Without this the whole JSON string becomes the
        # path and the file is written under a garbage name. This is the live
        # path: there is no filesystem MCP server, so write_file always runs
        # here via _direct_fallback, not through _build_mcp_args.
        base_revision = ""
        confirm_risky = False
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                if isinstance(_a, dict) and "path" in _a:
                    raw_path = str(_a.get("path", "")).strip()
                    body = str(_a.get("content", ""))
                    base_revision = _normalize_base_revision(_a.get("base_revision"))
                    confirm_risky = bool(_a.get("confirm_risky", False))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"write_file: {e}", "exit_code": 1}
        # Doubt review (src/doubt_review.py) block mode: preview the diff
        # WITHOUT writing, so a "concerns" verdict can refuse the write
        # entirely. Advisory mode needs no pre-write pass.
        _dr_gate = None
        _preview_known, _preview_revision, _preview_identity = False, None, None
        try:
            from src import doubt_review as _doubt_review
        except Exception:  # noqa: BLE001
            _doubt_review = None
        if _doubt_review is not None and _doubt_review.enabled() and _doubt_review.block_mode_enabled():
            try:
                _preview = ""
                try:
                    _preview, _pcrlf, _preview_revision = await asyncio.to_thread(_read_text_lf, path)
                    _preview_known = True
                    from src import resource_claims as _rc
                    _preview_identity = _rc.stat_identity(path)
                except FileNotFoundError:
                    _preview_known, _preview_revision = True, None
                    from src import resource_claims as _rc
                    _preview_identity = _rc.stat_identity(path)
                except (IsADirectoryError, UnicodeDecodeError, OSError):
                    _preview = ""
                _pdiff = _unified_diff(_preview, body, path)
                _dr_gate = await _doubt_review.check_edit(ctx, path, raw_path, _pdiff or {},
                                                          confirm_risky=confirm_risky)
            except Exception:  # noqa: BLE001
                logger.debug("[doubt_review] pre-write gate failed for %s", path, exc_info=True)
        if _dr_gate is not None and not _dr_gate.get("proceed", True):
            return _dr_gate["result"]
        _review_guard = _preview_known and _effective_preview_review(_dr_gate)
        try:
            def _write():
                with mutation_lock(path):
                    old, crlf, revision_now = "", False, None
                    current_known = False
                    try:
                        old, crlf, revision_now = _read_text_lf(path)
                        current_known = True
                    except FileNotFoundError:
                        current_known = True
                    except (IsADirectoryError, UnicodeDecodeError, OSError):
                        old, crlf, revision_now = "", False, None
                    if _review_guard and current_known:
                        conflict = _review_preview_conflict("write_file", path, _preview_revision, revision_now)
                        if conflict is None:
                            conflict = _identity_conflict("write_file", path, _preview_identity)
                        if conflict is not None:
                            return conflict, None, "conflict"
                    # H4: a turn-scoped RewritePolicy (src/rewrite_policy.py) —
                    # the 2nd whole-file rewrite of an existing large file is
                    # refused with "use edit_file/apply_patch", the 4th blocked.
                    # None (no policy in ctx) means "off", never an error.
                    policy = ctx.get("rewrite_policy") if isinstance(ctx, dict) else None
                    if policy is not None:
                        try:
                            from src.rewrite_policy import deny_result
                            verdict = policy.observe_lines(path, "write_file", old, body)
                            if verdict != "ok":
                                denied = deny_result(raw_path or path, verdict, policy.rewrite_count(path),
                                                     policy.min_lines)
                                denied["count"] = policy.rewrite_count(path)
                                return denied, None, "conflict"
                        except AttributeError:
                            pass
                    if base_revision:
                        conflict = _base_revision_conflict(
                            "write_file", path, base_revision, revision_now,
                            current_text=old, base_text=None, proposed_text=body)
                        if conflict is not None:
                            return conflict, None, "conflict"
                    d = os.path.dirname(path)
                    if d:
                        os.makedirs(d, exist_ok=True)
                    # Overwriting keeps the file's existing line-ending convention;
                    # a new file is written exactly as the model produced it (no
                    # platform translation), so the diff shows content changes only.
                    written_lf = body.replace("\r\n", "\n")
                    # EDIT-03: same treatment as crlf just above — a full
                    # overwrite of a file that had a UTF-8 BOM keeps it, unless
                    # the caller's own content already starts with one. `old`
                    # carries the BOM as an ordinary leading U+FEFF character
                    # (encoding="utf-8" never strips it — see _read_text_lf), so
                    # this is the one place that character gets silently dropped
                    # today: edit_file/apply_patch never touch it (their edits
                    # are partial replacements elsewhere in the text), but
                    # write_file replaces the whole body with what the model
                    # produced, which never retypes an invisible character.
                    if old.startswith("\ufeff") and not written_lf.startswith("\ufeff"):
                        written_lf = "\ufeff" + written_lf
                    _write_text_lf(path, written_lf, crlf)
                    written = written_lf.replace("\n", "\r\n") if crlf else written_lf
                    return old, len(body), sha256_revision(written.encode("utf-8"))
            old_content, size, status_or_revision = await asyncio.to_thread(_write)
        except PermissionError:
            return {"error": f"write_file: {path}: permission denied", "exit_code": 1}
        except OSError as e:
            return {"error": f"write_file: {path}: {e}", "exit_code": 1}
        if size is None:
            return old_content  # conflict payload (see `_write`'s "conflict" branch)
        diff = _unified_diff(old_content, body, path)
        result = {"output": f"Wrote {size} bytes to {path}", "exit_code": 0, "revision": status_or_revision}
        if base_revision:
            result["base_revision"] = base_revision
        else:
            result["unverified_base"] = True
        if diff:
            result["diff"] = diff
        # EDIT-05: best-effort history entry — never affects this result. An
        # empty old_content is ambiguous (new file vs. pre-existing empty
        # file); skip the snapshot rather than guess, but still log the write.
        try:
            record_edit_history(
                path, tool="write_file",
                pre_revision=sha256_revision(old_content.encode("utf-8")) if old_content else None,
                post_revision=status_or_revision,
                pre_bytes=old_content.encode("utf-8") if old_content else None)
        except Exception:
            logger.debug("[edit_history] hook failed for write_file on %s", path, exc_info=True)
        if _doubt_review is not None and _doubt_review.enabled():
            try:
                if _dr_gate is None:
                    _dr_gate = await _doubt_review.check_edit(ctx, path, raw_path, diff or {},
                                                              confirm_risky=confirm_risky)
                if _dr_gate.get("section"):
                    result["output"] = f"{result['output']}\n\n{_dr_gate['section']}"
                if _dr_gate.get("review") is not None:
                    result["doubt_review"] = _dr_gate["review"]
            except Exception:  # noqa: BLE001
                logger.debug("[doubt_review] post-write section failed for %s", path, exc_info=True)
        return result

class ApplyPatchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        """Apply a small Codex-style patch using exact context matching.

        This is deliberately stricter than git-apply: if an update hunk's old
        text is not found exactly once, the whole patch is rejected before any
        file is changed. That keeps agent edits reviewable and avoids fuzzy
        corruption when the model patches stale context.
        """
        from src.tool_execution import _resolve_tool_path
        from src import edit_journal

        patch_text = content or ""
        base_revision = ""
        confirm_risky = False
        stripped = patch_text.strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
                if isinstance(args, dict):
                    patch_text = str(args.get("patch_text") or args.get("patchText") or args.get("patch") or "")
                    base_revision = _normalize_base_revision(args.get("base_revision"))
                    confirm_risky = bool(args.get("confirm_risky", False))
            except (json.JSONDecodeError, TypeError):
                pass
        if not patch_text.strip():
            return {"error": "apply_patch: patch_text required", "exit_code": 1}

        try:
            ops = _parse_agent_patch(patch_text)
            if not ops:
                return {"error": "apply_patch: no file operations found", "exit_code": 1}
            prepared = []
            prepared_revisions = {}
            prepared_identities = {}
            prepared_paths = set()
            for op in ops:
                path = _resolve_tool_path(op["path"])
                identity = canonical_path(path)
                if identity in prepared_paths:
                    return {"error": f"apply_patch: duplicate target path: {op['path']}",
                            "exit_code": 1, "error_code": "DUPLICATE_PATCH_TARGET"}
                prepared_paths.add(identity)
                kind = op["kind"]
                crlf = False
                revision_now = None
                if kind == "add":
                    try:
                        os.lstat(path)
                    except FileNotFoundError:
                        pass
                    else:
                        return {"error": f"apply_patch: {op['path']}: already exists", "exit_code": 1}
                    old = ""
                    new = op["content"]
                elif kind == "delete":
                    if not os.path.isfile(path):
                        return {"error": f"apply_patch: {op['path']}: not found", "exit_code": 1}
                    old, _crlf, revision_now = _read_text_lf(path)
                    new = ""
                else:
                    if not os.path.isfile(path):
                        return {"error": f"apply_patch: {op['path']}: not found", "exit_code": 1}
                    old, crlf, revision_now = _read_text_lf(path)
                    new = _apply_patch_hunks(old, op["hunks"], op["path"])
                # EDIT-01: one base_revision anchors every update/delete op in
                # the patch (they were necessarily read together, as part of
                # preparing this one call) — refuse the WHOLE patch before any
                # file is touched, matching this tool's existing all-or-nothing
                # validation. "add" never carries a base: there is nothing on
                # disk yet for a base_revision to describe.
                if base_revision and kind in ("update", "delete"):
                    conflict = _base_revision_conflict(
                        "apply_patch", path, base_revision, revision_now,
                        current_text=old, base_text=_hunk_base_text(op) if kind == "update" else old,
                        proposed_text=new)
                    if conflict is not None:
                        return conflict
                prepared.append((kind, path, old, new, crlf))
                prepared_revisions[path] = revision_now
                from src import resource_claims as _rc
                prepared_identities[path] = _rc.stat_identity(path)
        except (ValueError, UnicodeDecodeError, PermissionError, OSError) as e:
            return {"error": f"apply_patch: {e}", "exit_code": 1}

        # Doubt review (src/doubt_review.py) block mode: gate on each
        # "update" op's diff BEFORE the journaled write phase below, so a
        # "concerns" verdict on any one file refuses the WHOLE patch (still
        # nothing written). Advisory mode runs after, against what landed.
        _dr_gates: List[Dict[str, Any]] = []
        try:
            from src import doubt_review as _doubt_review
        except Exception:  # noqa: BLE001
            _doubt_review = None
        if _doubt_review is not None and _doubt_review.enabled() and _doubt_review.block_mode_enabled():
            for kind, path, old, new, crlf in prepared:
                if kind != "update":
                    continue
                try:
                    pdiff = _unified_diff(old, new, path)
                    gate = await _doubt_review.check_edit(ctx, path, path, pdiff or {},
                                                          confirm_risky=confirm_risky)
                except Exception:  # noqa: BLE001
                    logger.debug("[doubt_review] pre-write gate failed for %s", path, exc_info=True)
                    continue
                if not gate.get("proceed", True):
                    return gate["result"]
                _dr_gates.append(gate)

        # EDIT-02: the write phase is journaled (src/edit_journal.py) — every
        # target's pre-batch bytes are snapshotted before any write, and a
        # mid-batch OSError compensates (writes back / deletes) every file
        # already applied instead of leaving a silent partial write.
        journal_ops = [{"path": path, "kind": kind, "new": new, "crlf": crlf}
                       for kind, path, old, new, crlf in prepared]

        def _read_bytes(p: str) -> Optional[bytes]:
            try:
                with open(p, "rb") as f:
                    return f.read()
            except FileNotFoundError:
                return None

        def _write_bytes(p: str, data: bytes) -> None:
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)

        def _delete_path(p: str) -> None:
            if os.path.isfile(p) or os.path.islink(p):
                os.remove(p)

        def _apply_op(p: str, op: Dict[str, Any], _pre: Optional[bytes]) -> Optional[bytes]:
            if op["kind"] == "delete":
                os.remove(p)
                return None
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            _write_text_lf(p, op["new"], op["crlf"])
            written = op["new"].replace("\n", "\r\n") if op["crlf"] else op["new"]
            return written.encode("utf-8")

        def _commit_prepared():
            with mutation_locks(prepared_revisions):
                for p, expected in prepared_revisions.items():
                    data = _read_bytes(p)
                    current = edit_journal.revision_of(data)
                    # A dangling symlink is an existing directory entry, not an
                    # absent add target, even when reading its referent fails.
                    occupied = expected is None and os.path.lexists(p)
                    if current != expected or occupied:
                        return None, {
                            "error": f"apply_patch: {p} changed since patch preparation",
                            "exit_code": 1, "status": "conflict",
                            "error_code": "PATCH_PREPARATION_MISMATCH",
                            "source": "patch_preparation", "path": p,
                            "prepared_revision": expected, "current_revision": current,
                            "current_path_exists": occupied or data is not None,
                            "next_action": "read_current_and_reconcile",
                        }
                    external = _identity_conflict("apply_patch", p, prepared_identities.get(p))
                    if external is not None:
                        return None, external
                return edit_journal.apply_batch(
                    journal_ops, read_bytes=_read_bytes, write_bytes=_write_bytes,
                    delete_path=_delete_path, apply_op=_apply_op), None

        try:
            receipt, preparation_conflict = await asyncio.to_thread(_commit_prepared)
        except (PermissionError, OSError) as e:
            return {"error": f"apply_patch: {e}", "exit_code": 1}
        if preparation_conflict is not None:
            return preparation_conflict

        applied_paths = set(receipt["applied"])
        if receipt["failed_at"] is not None:
            # Partial batch: report exactly what the journal says landed,
            # rather than the generic "already exists"-style error — this is
            # the QA-17 shape (`applied`, `failed_at`, `rolled_back`).
            return {
                "error": f"apply_patch: {receipt['failed_at']}: {receipt['failure']} "
                         f"(batch stopped; {len(applied_paths)} of {len(prepared)} file(s) "
                         f"{'rolled back' if receipt['rolled_back'] else 'left applied — rollback itself failed'})",
                "exit_code": 1,
                "status": "partial",
                "journal": receipt,
            }

        diffs = []
        for kind, path, old, new, crlf in prepared:
            diff = _unified_diff(old, new, path)
            if diff:
                diffs.append(diff)

        added = sum(int(d.get("added") or 0) for d in diffs)
        removed = sum(int(d.get("removed") or 0) for d in diffs)
        text_parts = [d.get("text", "") for d in diffs if d.get("text")]
        diff_text = "\n".join(text_parts)
        if len(diff_text.splitlines()) > MAX_DIFF_LINES:
            diff_text = "\n".join(diff_text.splitlines()[:MAX_DIFF_LINES]) + f"\n... diff truncated at {MAX_DIFF_LINES} lines"
        result = {
            "output": f"Applied patch ({len(prepared)} file{'s' if len(prepared) != 1 else ''}, +{added}/-{removed})",
            "exit_code": 0,
        }
        if base_revision:
            result["base_revision"] = base_revision
        else:
            result["unverified_base"] = True
        revisions = {f["path"]: f["post_revision"] for f in receipt["files"] if f["applied"]}
        if revisions:
            result["revisions"] = revisions
        if diffs:
            result["diff"] = {
                "text": diff_text,
                "added": added,
                "removed": removed,
                "new_file": any(d.get("new_file") for d in diffs),
                "file": "patch",
            }
        # Doubt review: block mode already gated (`_dr_gates`) before the
        # write; advisory mode runs now, per "update" op, against what
        # actually landed.
        if _doubt_review is not None and _doubt_review.enabled():
            sections: List[str] = []
            reviews: List[Dict[str, Any]] = []
            gates = _dr_gates
            if not gates:
                for kind, path, old, new, crlf in prepared:
                    if kind != "update":
                        continue
                    try:
                        pdiff = _unified_diff(old, new, path)
                        gates.append(await _doubt_review.check_edit(
                            ctx, path, path, pdiff or {}, confirm_risky=confirm_risky))
                    except Exception:  # noqa: BLE001
                        logger.debug("[doubt_review] post-write section failed for %s", path, exc_info=True)
            for gate in gates:
                if gate.get("section"):
                    sections.append(gate["section"])
                if gate.get("review") is not None:
                    reviews.append(gate["review"])
            if sections:
                result["output"] = result["output"] + "\n\n" + "\n\n".join(sections)
            if reviews:
                result["doubt_review"] = reviews
        return result


def _hunk_base_text(op: Dict[str, Any]) -> str:
    """The context+removed lines of every hunk in an `update` op — the only
    text this call itself supplied about the file's prior state, and so the
    only honest `base` for a three-way conflict report (see
    `_base_revision_conflict`)."""
    parts = []
    for hunk in op.get("hunks", []):
        old_lines = [line[1:] for line in hunk if line[:1] in (" ", "-")]
        parts.append("\n".join(old_lines))
    return "\n...\n".join(parts)

def _parse_agent_patch(patch_text: str) -> List[Dict[str, Any]]:
    lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise ValueError("patch must end with *** End Patch")

    ops: List[Dict[str, Any]] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            body = []
            i += 1
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ValueError(f"add file {path}: every content line must start with +")
                body.append(lines[i][1:])
                i += 1
            ops.append({"kind": "add", "path": path, "content": "\n".join(body) + ("\n" if body else "")})
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            ops.append({"kind": "delete", "path": path})
            i += 1
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            hunks = []
            current = []
            i += 1
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                raise ValueError("move operations are not supported")
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if current:
                        hunks.append(current)
                        current = []
                elif lines[i].startswith((" ", "-", "+")):
                    current.append(lines[i])
                elif lines[i] == "":
                    current.append(" ")
                else:
                    raise ValueError(f"update file {path}: invalid patch line {lines[i]!r}")
                i += 1
            if current:
                hunks.append(current)
            if not hunks:
                raise ValueError(f"update file {path}: no hunks")
            ops.append({"kind": "update", "path": path, "hunks": hunks})
            continue
        raise ValueError(f"unexpected patch line: {line!r}")
    return ops

def _apply_patch_hunks(original: str, hunks: List[List[str]], label: str) -> str:
    updated = original
    for idx, hunk in enumerate(hunks, 1):
        old_lines = []
        new_lines = []
        for line in hunk:
            prefix, body = line[:1], line[1:]
            if prefix in (" ", "-"):
                old_lines.append(body)
            if prefix in (" ", "+"):
                new_lines.append(body)
        old_text = "\n".join(old_lines)
        new_text = "\n".join(new_lines)
        if old_text and old_text in updated:
            occurrences = updated.count(old_text)
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text, new_text, 1)
        elif old_text + "\n" in updated:
            occurrences = updated.count(old_text + "\n")
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text + "\n", new_text + "\n", 1)
        else:
            raise ValueError(f"{label}: hunk {idx} context not found")
    return updated

class LsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        raw_path = ""
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                raw_path = str(json.loads(_s).get("path", "")).strip()
            except json.JSONDecodeError:
                raw_path = ""
        else:
            raw_path = _s.split("\n", 1)[0].strip()
        try:
            root = _resolve_search_root(raw_path)
        except ValueError as e:
            return {"error": f"ls: {e}", "exit_code": 1}

        def _ls():
            if not os.path.isdir(root):
                return None, f"ls: {root}: not a directory"
            rows = []
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            size = entry.stat(follow_symlinks=False).st_size if not is_dir else 0
                        except OSError:
                            continue
                        rows.append((is_dir, entry.name, size))
            except (PermissionError, OSError) as _e:
                return None, f"ls: {_e}"
            rows.sort(key=lambda r: (not r[0], r[1].lower()))
            lines = [f"{root}:"]
            for is_dir, name, size in rows[:_CODENAV_MAX_HITS]:
                lines.append(f"  {name}/" if is_dir else f"  {name}  ({size} B)")
            if len(rows) > _CODENAV_MAX_HITS:
                lines.append(f"  ... [{len(rows) - _CODENAV_MAX_HITS} more]")
            if not rows:
                lines.append("  (empty)")
            return "\n".join(lines), None

        out, err = await asyncio.to_thread(_ls)
        if err:
            return {"error": err, "exit_code": 1}
        return {"output": _truncate(out), "exit_code": 0}

class GlobTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _SENSITIVE_BASENAMES,
            _is_sensitive_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        args = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "glob: pattern is required", "exit_code": 1}
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"glob: {e}", "exit_code": 1}

        def _glob():
            base = os.path.abspath(root)
            if not os.path.isdir(base):
                return None, f"glob: {root}: not a directory"
            rbase = os.path.realpath(base)
            norm_pat = pattern.replace("\\", "/")
            # Fast path: literal pattern (no wildcards) → direct path lookup.
            if not any(c in norm_pat for c in "*?["):
                cand = os.path.realpath(os.path.join(base, norm_pat))
                # Keep the literal lookup inside the search root. os.path.join
                # lets an absolute pattern (or one containing ../) escape `base`,
                # which would turn glob into an existence/path oracle for
                # arbitrary host files — bypassing the workspace/allowlist
                # confinement that _resolve_search_root applies to the root.
                # An escaping literal falls through to the walk, which only ever
                # yields paths under base.
                nbase = os.path.normcase(rbase)
                try:
                    inside = cand == rbase or os.path.commonpath(
                        [os.path.normcase(cand), nbase]
                    ) == nbase
                except ValueError:
                    inside = False
                # A literal that names a deny-listed sensitive file (.env,
                # .ssh/id_rsa, …) falls through to the walk, which skips it —
                # otherwise glob would surface secret paths that read_file /
                # grep already refuse to touch.
                if inside and os.path.exists(cand) and not _is_sensitive_path(cand):
                    return [cand], None
                # Literal not at exact path — fall through to walk so
                # e.g. "foo.py" still matches at any depth (like rglob).
            # Compile glob to regex: * stays within one segment, **/ spans dirs.
            regex = _glob_to_regex(norm_pat)
            matched = []
            cap = _CODENAV_MAX_HITS * 5
            try:
                for dp, dns, fns in os.walk(base):
                    # Prune skipped dirs before descending (unlike rglob which
                    # descends first then filters — fatal on large node_modules).
                    # Sensitive dirs (.ssh, .gnupg, …) are pruned too so glob
                    # never enumerates the keys/tokens inside them.
                    dns[:] = [
                        d for d in dns
                        if d not in _CODENAV_SKIP_DIRS and d not in _SENSITIVE_BASENAMES
                    ]
                    for name in fns + dns:
                        full = os.path.join(dp, name)
                        rel = os.path.relpath(full, base).replace(os.sep, "/")
                        if regex.fullmatch(rel) or regex.fullmatch(name):
                            # Skip deny-listed sensitive files (.env, id_rsa,
                            # known_hosts, …) the same way grep does.
                            if _is_sensitive_path(os.path.realpath(full)):
                                continue
                            try:
                                mtime = os.stat(full).st_mtime
                            except OSError:
                                mtime = 0
                            matched.append((mtime, full))
                    if len(matched) > cap:
                        break
            except OSError as _e:
                return None, f"glob: {_e}"
            matched.sort(key=lambda t: t[0], reverse=True)
            return [pth for _, pth in matched[:_CODENAV_MAX_HITS]], None

        paths, err = await asyncio.to_thread(_glob)
        if err:
            return {"error": err, "exit_code": 1}
        if not paths:
            return {"output": f"No files matching {pattern!r} under {root}", "exit_code": 0}
        out = "\n".join(paths)
        if len(paths) >= _CODENAV_MAX_HITS:
            out += f"\n... [capped at {_CODENAV_MAX_HITS} files]"
        return {"output": _truncate(out), "exit_code": 0}

class GrepTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _SENSITIVE_FILE_PATTERNS,
            _is_sensitive_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        args: Dict[str, Any] = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "grep: pattern is required", "exit_code": 1}
        ignore_case = bool(args.get("ignore_case"))
        glob_pat = str(args.get("glob", "") or "").strip()
        try:
            max_hits = int(args.get("max_results") or _CODENAV_MAX_HITS)
        except (TypeError, ValueError):
            max_hits = _CODENAV_MAX_HITS
        max_hits = max(1, min(max_hits, _CODENAV_MAX_HITS))
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"grep: {e}", "exit_code": 1}

        def _grep():
            import shutil
            rg = shutil.which("rg")
            if rg:
                cmd = [rg, "--line-number", "--no-heading", "--color=never",
                       "--max-count", str(max_hits)]
                if ignore_case:
                    cmd.append("--ignore-case")
                if glob_pat:
                    cmd += ["--glob", glob_pat]
                # --iglob (not --glob) so the exclusion is case-insensitive:
                # on a case-insensitive filesystem "ID_RSA"/"Known_Hosts"
                # resolve to the same secret as their lowercase forms, and the
                # Python fallback below already folds case via _is_sensitive_path.
                for _pat in _SENSITIVE_FILE_PATTERNS:
                    cmd += ["--iglob", f"!*{_pat}*"]
                for _d in _CODENAV_SKIP_DIRS:
                    cmd += ["--glob", f"!**/{_d}/**"]
                # Same size limit as the fallback: model weights and dumps in a workspace
                # that is not a Git repo (no .gitignore) used to eat the whole budget.
                cmd += ["--max-filesize", str(GREP_FALLBACK_MAX_FILE_BYTES)]
                cmd += ["--regexp", pattern, root]
                try:
                    import subprocess
                    import threading
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace")
                    got: List[str] = []
                    enough = threading.Event()

                    def _read():
                        # Stream the matches: stop ripgrep as soon as there are max_hits of
                        # them instead of letting it walk the rest of the tree.
                        try:
                            for ln in p.stdout:
                                ln = ln.rstrip("\r\n")
                                if ln:
                                    got.append(ln)
                                    if len(got) >= max_hits:
                                        break
                        finally:
                            enough.set()

                    err_chunks: List[str] = []   # head: first GREP_STDERR_KEEP_BYTES // 2 bytes
                    err_tail: List[str] = []     # rolling tail: last GREP_STDERR_KEEP_BYTES // 2 bytes
                    err_done = threading.Event()

                    def _read_err():
                        # Drained concurrently and bounded: read bounded blocks and
                        # keep consuming to EOF even after the diagnostic budget is
                        # full. A line-based reader that breaks early stops draining,
                        # the child blocks on the full pipe and a real exit-2 error
                        # is lost to the timeout. What is retained is decided by
                        # _bounded_stderr_chunks (bytes, not characters).
                        try:
                            head, tail = _bounded_stderr_chunks(
                                iter(lambda: p.stderr.read(4096), ""), GREP_STDERR_KEEP_BYTES)
                            err_chunks.extend(head)
                            err_tail.extend(tail)
                        except Exception:
                            pass
                        finally:
                            err_done.set()

                    reader = threading.Thread(target=_read, daemon=True)
                    err_reader = threading.Thread(target=_read_err, daemon=True)
                    reader.start()
                    err_reader.start()
                    finished = enough.wait(GREP_FALLBACK_BUDGET_S)
                    timed_out = not finished
                    killed_by_us = False
                    if p.poll() is None:
                        # rg may be dying right now (stdout EOF does not mean the
                        # process state is reaped yet): give it a moment before
                        # killing, or a real exit-2 error would be mistaken for
                        # our own kill and lost.
                        try:
                            p.wait(timeout=0.05)
                        except Exception:
                            p.kill()  # enough matches, or out of time: keep what was found
                            killed_by_us = True
                    reader.join(timeout=2)
                    err_done.wait(timeout=2)
                    err_text = "".join(err_chunks)
                    if err_tail:
                        if err_chunks:
                            err_text += "\n[stderr truncated]\n"
                        err_text += "".join(err_tail)
                    lines = got[:max_hits]
                    size_notice = (
                        f"[Files over {GREP_FALLBACK_MAX_FILE_BYTES // (1024 * 1024)} MB were not "
                        f"searched (ripgrep --max-filesize): results may be partial. Read one with "
                        f"read_file, or use bash rg on a specific one.]")
                    if timed_out:
                        lines.append(
                            f"[grep stopped early after the time budget of {GREP_FALLBACK_BUDGET_S:.0f}s: results "
                            f"are partial. Narrow the search with path= or glob=.]")
                    else:
                        # A kill of our own (enough matches) reports -9 on POSIX: that is
                        # not a ripgrep failure and must not invent "results may be
                        # incomplete".
                        failed = p.poll() not in (0, 1, None) and not killed_by_us
                        if failed:
                            # rg exit 2 is an error (bad regex, unreadable path...): never
                            # dress it up as "No matches", and never drop it just because
                            # some hits came back first. Bounded: rg prints the cause on
                            # its own "error:" line after a generic header, so prefer that
                            # line; otherwise join the first few lines. Capped either way.
                            err_lines = [ln.strip() for ln in err_text.strip().splitlines() if ln.strip()]
                            detail = next((ln for ln in err_lines if ln.startswith("error:")), "")
                            if not detail:
                                detail = " | ".join(err_lines[:3])
                            detail = detail[:300] or f"ripgrep exited with code {p.returncode}"
                            if lines:
                                lines.append(
                                    f"[grep: {detail} (ripgrep exited with code {p.returncode}): "
                                    f"results may be incomplete.]")
                                lines.append(size_notice)
                            else:
                                return None, f"grep: {detail}"
                        elif not lines:
                            # Never a bare "No matches" that hides the size limit: the
                            # notice is part of this line, so it is not appended twice.
                            lines = [f"[No matches for {pattern!r} under {root}. Files over "
                                     f"{GREP_FALLBACK_MAX_FILE_BYTES // (1024 * 1024)} MB are not searched; use "
                                     f"bash rg on a specific one if it matters.]"]
                        else:
                            # The size limit is declared conservatively on every search
                            # with hits: ripgrep skips files over --max-filesize silently,
                            # and telling exactly which ones would need a second walk that
                            # re-implements glob and .gitignore semantics and can blow the
                            # time budget. The blanket notice costs nothing and never
                            # hides a partial answer behind hits on small files.
                            lines.append(size_notice)
                    return lines, None
                except Exception as _e:
                    return None, f"grep: {_e}"
            # The third-party `regex` engine takes a real timeout per search (and avoids most
            # catastrophic backtracking): the stdlib `re` cannot be interrupted once a search
            # runs, so `(a+)+$` on a 34-byte line used to outlive any budget.
            # Without it there is no bounded search: refuse instead of falling back to `re`.
            try:
                import regex as _rx_engine
            except ImportError:
                return None, ("grep: the 'regex' package is missing (a declared dependency of Faustus, needed "
                              "for a time-bounded search without ripgrep). Install it with "
                              "`pip install regex`, or install ripgrep, then retry.")
            try:
                rx = _rx_engine.compile(pattern, _rx_engine.IGNORECASE if ignore_case else 0)
            except _rx_engine.error as _e:
                return None, f"grep: bad pattern: {_e}"

            def _search(line: str):
                return rx.search(line, timeout=max(0.01, deadline - time.monotonic()))

            hits = []
            deadline = time.monotonic() + GREP_FALLBACK_BUDGET_S
            stopped = ""
            scanned = 0
            walk_cut = []
            too_big: List[str] = []

            def _files():
                # Lazy: the walk advances only as fast as files are searched,
                # and checks the clock per folder, so a glob that matches
                # nothing cannot walk a huge tree unbounded either.
                if os.path.isfile(root):
                    yield root
                    return
                for dp, dns, fns in os.walk(root):
                    if time.monotonic() > deadline:
                        walk_cut.append(True)
                        return
                    dns[:] = [d for d in dns if d not in _CODENAV_SKIP_DIRS]
                    for fn in fns:
                        if glob_pat and not fnmatch.fnmatch(fn, glob_pat):
                            continue
                        yield os.path.join(dp, fn)

            for fp in _files():
                if len(hits) >= max_hits:
                    break
                if time.monotonic() > deadline:
                    stopped = f"time budget of {GREP_FALLBACK_BUDGET_S:.0f}s"
                    break
                if scanned >= GREP_FALLBACK_MAX_FILES:
                    stopped = f"{GREP_FALLBACK_MAX_FILES} files"
                    break
                if _is_sensitive_path(os.path.realpath(fp)):
                    continue
                try:
                    with open(fp, "rb") as fb:
                        if b"\0" in fb.read(4096):
                            continue  # binary: never searched, nothing to report
                    if os.path.getsize(fp) > GREP_FALLBACK_MAX_FILE_BYTES:
                        too_big.append(fp)  # text that was NOT searched: reported below
                        continue
                    scanned += 1
                    with open(fp, "r", encoding="utf-8", errors="strict") as f:
                        for i, line in enumerate(f, 1):
                            # The budget also covers the lines of one file: a single slow
                            # file must not outlive it either.
                            if i % 1000 == 0 and time.monotonic() > deadline:
                                stopped = f"time budget of {GREP_FALLBACK_BUDGET_S:.0f}s (inside {fp})"
                                break
                            try:
                                found = _search(line)
                            except TimeoutError:
                                stopped = (f"time budget of {GREP_FALLBACK_BUDGET_S:.0f}s (the pattern was still "
                                           f"matching line {i} of {fp}; simplify it)")
                                break
                            if found:
                                hits.append(f"{fp}:{i}:{line.rstrip()[:_CODENAV_MAX_LINE]}")
                                if len(hits) >= max_hits:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
                if stopped:
                    break
            if walk_cut and not stopped:
                stopped = f"time budget of {GREP_FALLBACK_BUDGET_S:.0f}s"
            if stopped:
                hits.append(
                    f"[grep stopped early after {stopped} ({scanned} files searched, no ripgrep on "
                    f"this machine): results are partial. Narrow the search with path= or glob=.]"
                )
            if too_big:
                shown = ", ".join(too_big[:3]) + (f" and {len(too_big) - 3} more" if len(too_big) > 3 else "")
                hits.append(
                    f"[{len(too_big)} file(s) over "
                    f"{(str(GREP_FALLBACK_MAX_FILE_BYTES // (1024 * 1024)) + ' MB') if GREP_FALLBACK_MAX_FILE_BYTES >= 1024 * 1024 else str(GREP_FALLBACK_MAX_FILE_BYTES) + ' bytes'} were not "
                    f"searched ({shown}): results may be partial. Read them with read_file, or use bash grep.]"
                )
            return hits, None

        lines, err = await asyncio.to_thread(_grep)
        if err:
            return {"error": err, "exit_code": 1}
        if not lines:
            return {"output": f"No matches for {pattern!r} under {root}", "exit_code": 0}
        out = "\n".join(ln if ln.startswith("[") else ln[:_CODENAV_MAX_LINE] for ln in lines)
        # Notices ("[grep stopped early…]", "[n file(s) … not searched…]") are not matches.
        if sum(1 for ln in lines if not ln.startswith("[")) >= max_hits:
            out += f"\n... [capped at {max_hits} matches]"
        return {"output": _truncate(out), "exit_code": 0}

class GetWorkspaceTool:
    """Report the active workspace folder (no args). File tools are confined to
    it; the shell starts there (cwd) but is NOT sandboxed."""
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import get_active_workspace, get_active_workspace_roots
        ws = get_active_workspace()
        roots = get_active_workspace_roots()
        if ws or roots:
            root_lines = "\n".join(f"- {root}" for root in roots)
            return {
                "output": (
                    f"{ws or '(no primary folder)'}\n"
                    f"Primary working folder: {ws or '(none)'}\n"
                    f"Project work roots:\n{root_lines}\n"
                    "File tools may read and modify these roots. Relative paths use the "
                    "primary folder; the shell starts there but is not sandboxed."
                ),
                "exit_code": 0,
            }
        return {
            "output": "No workspace is set. File tools use the default allowed roots; "
                      "resolve paths from the user or use absolute paths.",
            "exit_code": 0,
        }


# ── EDIT-05: per-file edit history and safe, registered-only cleanup ──────
#
# Two separate guarantees, kept structurally simple on purpose:
#
# * **History with point-in-time restore.** Every successful EditFileTool /
#   WriteFileTool write appends one entry (best-effort — a recording failure
#   must never fail the edit it describes, same posture as
#   `artifact_store._record_manifest_for`) to a hidden JSONL sidecar next to
#   the file, plus a content-addressed snapshot of the bytes it overwrote.
#   `restore_edit_history()` re-verifies a snapshot's hash before writing it
#   back, so a corrupted or hand-edited sidecar can never silently restore
#   the wrong bytes.
# * **Cleanup that cannot reach a user's file.** `cleanup_temp_files()` does
#   not scan a directory and guess what looks disposable — it only ever acts
#   on paths a caller explicitly registered with `mark_temp_file()`. A path
#   nobody registered is invisible to it, by construction, not by a filename
#   heuristic that could misfire on a user's own "scratch.tmp". Removal is
#   always a move into a timestamped trash directory (never `os.remove`), so
#   a bad cleanup call is recoverable via `restore_trashed_files()`.
_EDIT_HISTORY_DIRNAME = ".faustus_edit_history"
_TEMP_TRASH_DIRNAME = ".faustus_temp_trash"
_TEMP_REGISTRY_FILENAME = "_temp_registry.json"


def _faustus_data_dir() -> str:
    env = os.environ.get("ODYSSEUS_DATA_DIR")
    if env:
        return env
    from src.constants import DATA_DIR
    return DATA_DIR


def _private_store_for(directory: str) -> str:
    """Faustus's own bookkeeping for one folder of the user's, kept in
    Faustus's data directory instead of the folder itself.

    It used to live in `<folder>/.faustus_edit_history/`. A benchmark task
    that checks a folder holds exactly the files it asked for failed on that
    alone ("Expected only main.py.c, found: [..., '.faustus_edit_history',
    ...]"), and so does any build, archive or `ls` the user runs there: the
    work folder belongs to the user."""
    folder = os.path.normcase(os.path.abspath(directory))
    key = hashlib.sha256(folder.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    store = os.path.join(_faustus_data_dir(), "edit_history", key)
    os.makedirs(store, exist_ok=True)
    origin = os.path.join(store, "folder.txt")
    if not os.path.exists(origin):
        with open(origin, "w", encoding="utf-8") as fh:
            fh.write(os.path.abspath(directory) + "\n")
    return store


def _legacy_sidecar_dir(path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(path)), _EDIT_HISTORY_DIRNAME)


def _history_sidecar_dir(path: str) -> str:
    return _private_store_for(os.path.dirname(os.path.abspath(path)))


def _ensure_private_dir(directory: str) -> None:
    """Create one of Faustus's own folders inside the user's project, and
    keep it out of their version control.

    The edit history lives next to the files it protects, which means in
    the user's repository. Seen coaching the agent through a small project:
    `git status` listed `.faustus_edit_history/` as untracked after every
    task, one `git add -A` away from committing snapshots into someone's
    history. A `.gitignore` of `*` inside the folder is the convention tool
    caches use: git ignores the folder, and the user's own .gitignore is
    never touched."""
    os.makedirs(directory, exist_ok=True)
    marker = os.path.join(directory, ".gitignore")
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("# Written by Faustus: its own edit history, not part of the project.\n*\n")


def _history_sidecar_path(path: str) -> str:
    return os.path.join(_history_sidecar_dir(path), os.path.basename(path) + ".jsonl")


def record_edit_history(path: str, *, tool: str, pre_revision: Optional[str],
                        post_revision: Optional[str], pre_bytes: Optional[bytes]) -> None:
    """Append one history entry for a write to `path`. Best-effort: logs and
    returns on any OS error rather than raising, so a full disk or a
    permissions quirk on the sidecar directory never turns a successful edit
    into a failed tool call."""
    try:
        sidecar_dir = _history_sidecar_dir(path)
        entry: Dict[str, Any] = {
            "ts": time.time(), "tool": tool,
            "pre_revision": pre_revision, "post_revision": post_revision,
        }
        if pre_bytes is not None and pre_revision:
            snapshot_name = pre_revision.replace("sha256:", "") + ".snapshot"
            snapshot_path = os.path.join(sidecar_dir, snapshot_name)
            if not os.path.exists(snapshot_path):
                with open(snapshot_path, "wb") as fh:
                    fh.write(pre_bytes)
            entry["pre_snapshot"] = snapshot_name
        with open(_history_sidecar_path(path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("[edit_history] could not record history for %s", path, exc_info=True)


def list_edit_history(path: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Oldest-first up to the sidecar's whole log, returning only the most
    recent `limit` entries — the timeline EDIT-05's frontend renders."""
    sidecar = _history_sidecar_path(path)
    if not os.path.isfile(sidecar):
        # History recorded before it moved out of the user's folder.
        sidecar = os.path.join(_legacy_sidecar_dir(path), os.path.basename(path) + ".jsonl")
    if not os.path.isfile(sidecar):
        return []
    out: List[Dict[str, Any]] = []
    with open(sidecar, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:] if limit else out


def restore_edit_history(path: str, *, pre_revision: str) -> Dict[str, Any]:
    """Point-in-time restore: write back the snapshot recorded for
    `pre_revision`. Refuses (rather than guessing) when no matching snapshot
    exists, or when the snapshot's bytes no longer hash to the revision they
    were recorded under — a tampered or corrupted sidecar must not be able to
    silently restore something other than what it claims."""
    for entry in reversed(list_edit_history(path, limit=0)):
        if entry.get("pre_revision") != pre_revision or not entry.get("pre_snapshot"):
            continue
        snapshot_path = os.path.join(_history_sidecar_dir(path), entry["pre_snapshot"])
        if not os.path.isfile(snapshot_path):
            snapshot_path = os.path.join(_legacy_sidecar_dir(path), entry["pre_snapshot"])
        if not os.path.isfile(snapshot_path):
            return {"restored": False, "reason": "snapshot file is missing"}
        with open(snapshot_path, "rb") as fh:
            data = fh.read()
        if sha256_revision(data) != pre_revision:
            return {"restored": False, "reason": "snapshot bytes no longer match the recorded revision"}
        with open(path, "wb") as fh:
            fh.write(data)
        return {"restored": True, "revision": pre_revision, "bytes": len(data)}
    return {"restored": False, "reason": "no snapshot recorded for that revision"}


def _temp_registry_path(workspace_root: str) -> str:
    return os.path.join(_private_store_for(workspace_root), _TEMP_REGISTRY_FILENAME)


def _read_registry(workspace_root: str) -> List[str]:
    reg_path = _temp_registry_path(workspace_root)
    if not os.path.isfile(reg_path):
        return []
    try:
        with open(reg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [str(p) for p in data] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_registry(workspace_root: str, paths: List[str]) -> None:
    reg_path = _temp_registry_path(workspace_root)
    _ensure_private_dir(os.path.dirname(reg_path))
    with open(reg_path, "w", encoding="utf-8") as fh:
        json.dump(paths, fh)


def mark_temp_file(workspace_root: str, path: str) -> None:
    """Register `path` (a file this agent itself created) as safe-to-clean
    temp output. `cleanup_temp_files` iterates this registry and nothing
    else — a path that was never marked here is structurally unreachable by
    cleanup, which is what keeps a user's own file out of its path."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(workspace_root))
    current = _read_registry(workspace_root)
    if rel not in current:
        current.append(rel)
        _write_registry(workspace_root, current)


def cleanup_temp_files(workspace_root: str) -> Dict[str, Any]:
    """Move every REGISTERED temp file into a fresh timestamped trash
    directory (never a hard delete) and clear the registry. Returns what
    moved, what was already gone, and the trash directory so a caller can
    offer `restore_trashed_files` immediately."""
    registry = _read_registry(workspace_root)
    if not registry:
        return {"moved": [], "missing": [], "trash_dir": None}
    trash_dir = os.path.join(workspace_root, _TEMP_TRASH_DIRNAME, str(int(time.time() * 1000)))
    moved: List[Dict[str, str]] = []
    missing: List[str] = []
    for rel in registry:
        abs_path = os.path.join(workspace_root, rel)
        if not os.path.isfile(abs_path):
            missing.append(rel)
            continue
        _ensure_private_dir(os.path.join(workspace_root, _TEMP_TRASH_DIRNAME))
        os.makedirs(trash_dir, exist_ok=True)
        dest = os.path.join(trash_dir, rel.replace(os.sep, "__").replace("/", "__"))
        shutil.move(abs_path, dest)
        moved.append({"path": rel, "trashed_to": dest})
    _write_registry(workspace_root, [])
    return {"moved": moved, "missing": missing, "trash_dir": trash_dir if moved else None}


def restore_trashed_files(trash_dir: str, workspace_root: str) -> Dict[str, Any]:
    """Undo one `cleanup_temp_files` batch: move every file back from
    `trash_dir` to its original registered relative path (encoded in the
    trashed filename by `cleanup_temp_files`)."""
    if not os.path.isdir(trash_dir):
        return {"restored": [], "reason": "trash_dir does not exist"}
    restored: List[str] = []
    for name in os.listdir(trash_dir):
        rel = name.replace("__", os.sep)
        dest = os.path.join(workspace_root, rel)
        os.makedirs(os.path.dirname(dest) or workspace_root, exist_ok=True)
        shutil.move(os.path.join(trash_dir, name), dest)
        restored.append(rel)
    try:
        os.rmdir(trash_dir)
    except OSError:
        pass
    return {"restored": restored}
