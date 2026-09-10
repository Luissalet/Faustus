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
from src.constants import MAX_READ_CHARS, MAX_DIFF_LINES, MAX_OUTPUT_CHARS

logger = logging.getLogger(__name__)

_CODENAV_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".cache", "site-packages", ".idea", ".tox",
})
_CODENAV_MAX_HITS = 200
_CODENAV_MAX_LINE = 400


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


class EditFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        raw_path = (args.get("path") or "").strip()
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))
        base_revision = str(args.get("base_revision") or "").strip()
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
        if base_revision and not _REVISION_RE.match(base_revision):
            return {"error": "edit_file: base_revision must look like 'sha256:<hex>'", "exit_code": 1}

        # Models quote text with "\n"; Windows files carry "\r\n". Match on
        # LF-normalized text and write the file back with the line endings it
        # already had — never rewrite a whole file's endings on a one-line edit.
        old_lf = old.replace("\r\n", "\n")
        new_lf = new.replace("\r\n", "\n")

        def _apply():
            """Read, check the base_revision precondition (EDIT-01), replace
            and write — all inside one thread call so nothing else can slip a
            write in between the check and the write of this process."""
            original, crlf, revision_now = _read_text_lf(path)
            if base_revision:
                conflict = _base_revision_conflict(
                    "edit_file", path, base_revision, revision_now,
                    current_text=original, base_text=old_lf, proposed_text=new_lf)
                if conflict is not None:
                    return original, conflict, "conflict"
            count = original.count(old_lf)
            if count == 0:
                return original, None, "not_found"
            if count > 1 and not replace_all:
                return original, None, f"not_unique:{count}"
            updated = original.replace(old_lf, new_lf) if replace_all else original.replace(old_lf, new_lf, 1)
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
        return result

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
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                if isinstance(_a, dict) and "path" in _a:
                    raw_path = str(_a.get("path", "")).strip()
                    body = str(_a.get("content", ""))
                    base_revision = str(_a.get("base_revision") or "").strip()
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if base_revision and not _REVISION_RE.match(base_revision):
            return {"error": "write_file: base_revision must look like 'sha256:<hex>'", "exit_code": 1}
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"write_file: {e}", "exit_code": 1}
        try:
            def _write():
                old, crlf, revision_now = "", False, None
                try:
                    old, crlf, revision_now = _read_text_lf(path)
                except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError, OSError):
                    old, crlf, revision_now = "", False, None
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
        stripped = patch_text.strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
                if isinstance(args, dict):
                    patch_text = str(args.get("patch_text") or args.get("patchText") or args.get("patch") or "")
                    base_revision = str(args.get("base_revision") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass
        if not patch_text.strip():
            return {"error": "apply_patch: patch_text required", "exit_code": 1}
        if base_revision and not _REVISION_RE.match(base_revision):
            return {"error": "apply_patch: base_revision must look like 'sha256:<hex>'", "exit_code": 1}

        try:
            ops = _parse_agent_patch(patch_text)
            if not ops:
                return {"error": "apply_patch: no file operations found", "exit_code": 1}
            prepared = []
            for op in ops:
                path = _resolve_tool_path(op["path"])
                kind = op["kind"]
                crlf = False
                revision_now = None
                if kind == "add":
                    if os.path.exists(path):
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
        except (ValueError, UnicodeDecodeError, PermissionError, OSError) as e:
            return {"error": f"apply_patch: {e}", "exit_code": 1}

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
            except OSError:
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

        try:
            receipt = await asyncio.to_thread(
                edit_journal.apply_batch, journal_ops, read_bytes=_read_bytes,
                write_bytes=_write_bytes, delete_path=_delete_path, apply_op=_apply_op)
        except (PermissionError, OSError) as e:
            return {"error": f"apply_patch: {e}", "exit_code": 1}

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
            import re as _re
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
                cmd += ["--regexp", pattern, root]
                try:
                    import subprocess
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                    lines = [ln for ln in (p.stdout or "").splitlines() if ln][:max_hits]
                    return lines, None
                except subprocess.TimeoutExpired:
                    return None, "grep: timed out"
                except Exception as _e:
                    return None, f"grep: {_e}"
            try:
                rx = _re.compile(pattern, _re.IGNORECASE if ignore_case else 0)
            except _re.error as _e:
                return None, f"grep: bad pattern: {_e}"
            hits = []
            if os.path.isfile(root):
                file_iter = [root]
            else:
                file_iter = []
                for dp, dns, fns in os.walk(root):
                    dns[:] = [d for d in dns if d not in _CODENAV_SKIP_DIRS]
                    for fn in fns:
                        if glob_pat and not fnmatch.fnmatch(fn, glob_pat):
                            continue
                        file_iter.append(os.path.join(dp, fn))
            for fp in file_iter:
                if len(hits) >= max_hits:
                    break
                if _is_sensitive_path(os.path.realpath(fp)):
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="strict") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append(f"{fp}:{i}:{line.rstrip()[:_CODENAV_MAX_LINE]}")
                                if len(hits) >= max_hits:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
            return hits, None

        lines, err = await asyncio.to_thread(_grep)
        if err:
            return {"error": err, "exit_code": 1}
        if not lines:
            return {"output": f"No matches for {pattern!r} under {root}", "exit_code": 0}
        out = "\n".join(ln[:_CODENAV_MAX_LINE] for ln in lines)
        if len(lines) >= max_hits:
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


def _history_sidecar_dir(path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(path)), _EDIT_HISTORY_DIRNAME)


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
        os.makedirs(sidecar_dir, exist_ok=True)
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
    return os.path.join(workspace_root, _EDIT_HISTORY_DIRNAME, _TEMP_REGISTRY_FILENAME)


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
    os.makedirs(os.path.dirname(reg_path), exist_ok=True)
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
