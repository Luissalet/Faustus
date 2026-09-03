"""Specialist agents with their own corpus — the local answer to "a custom GPT
with my books in it", minus the upload.

Why it is built this way
------------------------
* **The corpus never leaves the machine.** An expert is a directory under
  ``DATA_DIR/experts/<slug>/``: an ``EXPERT.md`` profile (the same
  markdown+frontmatter-on-disk shape ``services/memory/skills.py`` uses), a
  ``corpus/`` folder holding the user's own PDFs and notes, an ``index.json``
  chunk index and a ``usage.json`` sidecar. Nothing is uploaded, there is no
  size limit beyond the disk, and the whole thing is greppable and
  hand-editable.

* **Page-level provenance is the differentiator.** A PDF is extracted PAGE BY
  PAGE, so every chunk knows which page it came from and a citation can be
  checked against the book. When the PDF library cannot give pages the chunk
  gets ``page: None`` and ``page_confidence: "unknown"`` — a page number is
  NEVER guessed.

* **Search degrades, it does not fail.** Tier 1 is a stdlib BM25-lite over the
  chunk index and always runs. Tier 2 is this expert's own embedding
  collection (``odysseus_expert_<slug>``); when it is importable the two
  rankings are fused with Reciprocal Rank Fusion (``Σ 1/(60+rank)``) and the
  answer says ``tier: "hybrid"``. When it is missing or raises, tier 1 is
  served with ``degraded: True``. A freshly installed Faustus that has
  downloaded nothing still searches its own books.

* **Phase 1 is RAG + citations.** No fine-tuning, no LoRA: those need hundreds
  of accepted/rejected corrections that only real use produces, and a PDF
  never belongs in a fine-tune. What is collected now is exactly that signal —
  ``usage.json`` counters, which also drive the Thompson-sampling
  :func:`suggest`.

* **Nothing here raises into a hot path.** :func:`expert_block`,
  :func:`search`, :func:`load_expert` and :func:`record_feedback` are called
  while the user is waiting on a turn; a corrupt index or an unreadable file
  costs the block, not the turn. Corrupt files are moved aside to ``.corrupt``
  and rebuilt, writes are atomic (tmp + ``os.replace``).

Pure stdlib plus what the app already ships (``pypdf`` for text, the optional
``markitdown`` for Office files).
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import os
import random
import re
import shutil
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from services.memory.skill_format import (
    emit_frontmatter,
    parse_frontmatter,
    slugify,
)
from src.personal_docs import (
    config as docs_config,
    extract_office_text,
    extract_pdf_text,
    read_text_file,
    split_chunks,
    tokenize,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Module-level so tests can point the store somewhere disposable.
DATA_DIR = _DEFAULT_DATA_DIR
EXPERTS_DIRNAME = "experts"
PROFILE_FILENAME = "EXPERT.md"
CORPUS_DIRNAME = "corpus"
INDEX_FILENAME = "index.json"
USAGE_FILENAME = "usage.json"

# What may land in a corpus/ folder. Same set personal docs indexes, minus the
# ones that carry no prose.
CORPUS_EXTENSIONS: Tuple[str, ...] = (
    ".pdf", ".txt", ".md", ".markdown", ".rst", ".json",
    ".docx", ".pptx", ".xlsx", ".xls", ".epub",
)

DEFAULT_CONTEXT_CHARS = 2500
DEFAULT_SEARCH_K = 6
RRF_K = 60.0                     # the report's Reciprocal Rank Fusion constant
BM25_K1 = 1.5
BM25_B = 0.75
MAX_EXCERPT_CHARS = 600          # per-citation excerpt cap
READ_CHUNK_BYTES = 1024 * 1024   # streaming copy unit — a huge PDF is never
                                 # held in memory whole

_WORD_RE = re.compile(r"[A-Za-z0-9_\-]+")
_RUBRIC_HEADING_RE = re.compile(r"^##\s+rubric\s*$", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+(.*)$")


class ExpertError(ValueError):
    """Invalid expert input or an unusable store — routes map this to a 400."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def experts_root() -> str:
    """``DATA_DIR/experts``. Read through a function so tests can repoint
    ``experts.DATA_DIR`` at a tmp_path between calls."""
    return os.path.join(DATA_DIR, EXPERTS_DIRNAME)


def expert_dir(slug: Any) -> str:
    return os.path.join(experts_root(), _clean_slug(slug))


def profile_path(slug: Any) -> str:
    return os.path.join(expert_dir(slug), PROFILE_FILENAME)


def corpus_dir(slug: Any) -> str:
    return os.path.join(expert_dir(slug), CORPUS_DIRNAME)


def index_path(slug: Any) -> str:
    return os.path.join(expert_dir(slug), INDEX_FILENAME)


def usage_path(slug: Any) -> str:
    return os.path.join(expert_dir(slug), USAGE_FILENAME)


def _clean_slug(slug: Any) -> str:
    """A slug that can only ever name a direct child of experts_root()."""
    text = str(slug or "").strip()
    if not text:
        return ""
    return slugify(text, fallback="")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write_text(path: str, text: str) -> None:
    try:
        from core.atomic_io import atomic_write_text
        atomic_write_text(path, text)
        return
    except Exception:  # noqa: BLE001 - fall back to the same tmp+replace dance
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _atomic_write_json(path: str, data: Any) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _quarantine(path: str) -> None:
    """Keep the broken copy so nothing is silently destroyed, then rebuild."""
    try:
        os.replace(path, path + ".corrupt")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def experts_enabled() -> bool:
    """``agent_experts``. Never raises — a broken settings file must not
    disable the module's own CRUD."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_experts", True))
    except Exception:  # noqa: BLE001
        return True


def context_budget() -> int:
    """``agent_expert_context_chars`` — the char budget expert_block() uses
    when the caller does not name one."""
    try:
        from src.settings import get_setting
        value = int(get_setting("agent_expert_context_chars", DEFAULT_CONTEXT_CHARS))
        return max(200, min(value, 40_000))
    except Exception:  # noqa: BLE001
        return DEFAULT_CONTEXT_CHARS


# ---------------------------------------------------------------------------
# The profile — EXPERT.md
# ---------------------------------------------------------------------------


def _parse_expert_body(body: str) -> Tuple[str, List[str]]:
    """Split the markdown body into (instructions, rubric items).

    Everything before ``## Rubric`` is the instructions; the ordered list under
    it is the rubric. Without a rubric a local corrector rambles, so the rubric
    is kept as an ordered list rather than free prose.
    """
    instructions: List[str] = []
    rubric_lines: List[str] = []
    in_rubric = False
    for line in (body or "").splitlines():
        if _RUBRIC_HEADING_RE.match(line.strip()):
            in_rubric = True
            continue
        if in_rubric and line.strip().startswith("## "):
            # A heading after the rubric ends it; keep the rest as instructions.
            in_rubric = False
            instructions.append(line)
            continue
        (rubric_lines if in_rubric else instructions).append(line)

    rubric: List[str] = []
    for line in rubric_lines:
        text = line.strip()
        if not text:
            continue
        m = _LIST_ITEM_RE.match(text)
        if m:
            rubric.append(m.group(1).strip())
        elif rubric:
            rubric[-1] = (rubric[-1] + " " + text).strip()
        else:
            rubric.append(text)
    return "\n".join(instructions).strip(), [r for r in rubric if r]


def _emit_expert_body(instructions: str, rubric: Sequence[str]) -> str:
    parts: List[str] = []
    text = (instructions or "").strip()
    if text:
        parts.append(text)
    items = [str(r).strip() for r in (rubric or []) if str(r).strip()]
    if items:
        parts.append("## Rubric\n\n" + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(items)))
    return "\n\n".join(parts) + ("\n" if parts else "")


def _float_or(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return out


def _profile_markdown(profile: Dict[str, Any]) -> str:
    fm = {
        "name": profile.get("name") or profile.get("slug") or "",
        "slug": profile.get("slug") or "",
        "description": profile.get("description") or "",
        "owner": profile.get("owner") or "",
        "model": profile.get("model") or "",
        "temperature": round(_float_or(profile.get("temperature"), 0.2), 3),
        "top_p": round(_float_or(profile.get("top_p"), 1.0), 3),
        # A real bool: emit_frontmatter only drops None / "" / [], so
        # `enabled: false` survives the round-trip.
        "enabled": bool(profile.get("enabled", True)),
        "created_at": profile.get("created_at") or _now_iso(),
        "updated_at": profile.get("updated_at") or _now_iso(),
    }
    body = _emit_expert_body(profile.get("instructions") or "", profile.get("rubric") or [])
    return f"---\n{emit_frontmatter(fm)}\n---\n\n{body}"


def _profile_from_markdown(text: str, slug: str) -> Optional[Dict[str, Any]]:
    """None when the file carries no frontmatter at all — the caller treats
    that as corrupt and rebuilds."""
    fm, body = parse_frontmatter(text or "")
    if not fm:
        return None
    instructions, rubric = _parse_expert_body(body)
    enabled = fm.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("false", "no", "0", "")
    return {
        "slug": slug,
        "name": str(fm.get("name") or slug),
        "description": str(fm.get("description") or ""),
        "owner": str(fm.get("owner") or ""),
        "model": str(fm.get("model") or ""),
        "temperature": _float_or(fm.get("temperature"), 0.2),
        "top_p": _float_or(fm.get("top_p"), 1.0),
        "enabled": bool(enabled),
        "created_at": str(fm.get("created_at") or ""),
        "updated_at": str(fm.get("updated_at") or ""),
        "instructions": instructions,
        "rubric": rubric,
    }


def _blank_profile(slug: str) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "slug": slug,
        "name": slug.replace("-", " ").strip() or slug,
        "description": "",
        "owner": "",
        "model": "",
        "temperature": 0.2,
        "top_p": 1.0,
        "enabled": True,
        "created_at": now,
        "updated_at": now,
        "instructions": "",
        "rubric": [],
    }


def load_expert(slug: Any) -> Optional[Dict[str, Any]]:
    """The expert's profile, or None when there is no such directory.

    A corrupt / unreadable EXPERT.md is moved aside to ``EXPERT.md.corrupt``
    and rebuilt as a blank profile carrying the slug: the corpus and the index
    are the expensive part and they survive. NEVER raises — this is called
    from the prompt path.
    """
    slug = _clean_slug(slug)
    if not slug:
        return None
    directory = expert_dir(slug)
    if not os.path.isdir(directory):
        return None
    path = profile_path(slug)
    text = None
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            logger.warning("experts: %s unreadable (%s); rebuilding profile", path, exc)
    if text is None:
        if os.path.exists(path):
            _quarantine(path)
        profile = _blank_profile(slug)
        _safe_write_profile(profile)
        return profile
    try:
        profile = _profile_from_markdown(text, slug)
    except Exception as exc:  # noqa: BLE001 - a hand-edited file must not raise
        logger.warning("experts: %s unparseable (%s); rebuilding profile", path, exc)
        profile = None
    if profile is None:
        _quarantine(path)
        profile = _blank_profile(slug)
        _safe_write_profile(profile)
    return profile


def _safe_write_profile(profile: Dict[str, Any]) -> bool:
    try:
        save_expert(profile)
        return True
    except Exception as exc:  # noqa: BLE001 - recovery must not raise either
        logger.warning("experts: could not write profile for %s: %s",
                       profile.get("slug"), exc)
        return False


def save_expert(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Atomic write of EXPERT.md. Raises ExpertError on a dead disk."""
    slug = _clean_slug((profile or {}).get("slug"))
    if not slug:
        raise ExpertError("expert has no slug")
    profile = dict(profile)
    profile["slug"] = slug
    try:
        os.makedirs(corpus_dir(slug), exist_ok=True)
        _atomic_write_text(profile_path(slug), _profile_markdown(profile))
    except OSError as exc:
        raise ExpertError(f"Could not save expert '{slug}': {exc}")
    return profile


def list_expert_slugs() -> List[str]:
    """Every expert directory, sorted. Never raises."""
    root = experts_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith(".") or name != _clean_slug(name):
            continue
        if os.path.isdir(os.path.join(root, name)):
            out.append(name)
    return out


def list_experts(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every expert's profile, slug order.

    ``owner`` keeps the experts that user owns PLUS the ownerless ones — this
    is a local single-user app whose experts routes are admin-gated, and
    hiding an un-stamped directory would make the page look empty.
    """
    out = []
    for slug in list_expert_slugs():
        profile = load_expert(slug)
        if not profile:
            continue
        if owner and profile.get("owner") and profile["owner"] != owner:
            continue
        out.append(profile)
    return out


def create_expert(
    name: str,
    *,
    description: str = "",
    instructions: str = "",
    rubric: Optional[Sequence[str]] = None,
    model: str = "",
    temperature: float = 0.2,
    top_p: float = 1.0,
    owner: str = "",
    enabled: bool = True,
) -> Dict[str, Any]:
    """Create ``DATA_DIR/experts/<slug>/`` with an EXPERT.md and a corpus/.

    A slug collision suffixes ``-2``, ``-3``… rather than clobbering an
    existing expert's corpus.
    """
    title = str(name or "").strip()
    if not title:
        raise ExpertError("An expert needs a name")
    base = slugify(title, fallback="expert")
    slug = base
    taken = set(list_expert_slugs())
    i = 2
    while slug in taken:
        slug = f"{base}-{i}"
        i += 1
    now = _now_iso()
    profile = {
        "slug": slug,
        "name": title,
        "description": str(description or ""),
        "owner": str(owner or ""),
        "model": str(model or ""),
        "temperature": _float_or(temperature, 0.2),
        "top_p": _float_or(top_p, 1.0),
        "enabled": bool(enabled),
        "created_at": now,
        "updated_at": now,
        "instructions": str(instructions or ""),
        "rubric": [str(r) for r in (rubric or []) if str(r).strip()],
    }
    save_expert(profile)
    _write_usage(slug, _blank_usage())
    return profile


_EDITABLE_FIELDS = ("name", "description", "owner", "model", "temperature",
                    "top_p", "enabled", "instructions", "rubric")


def update_expert(slug: Any, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Patch any profile field in place (the slug never moves, so the corpus
    and the index stay put). None when there is no such expert."""
    profile = load_expert(slug)
    if not profile:
        return None
    for key in _EDITABLE_FIELDS:
        if key not in (updates or {}):
            continue
        value = updates[key]
        if key == "rubric":
            if isinstance(value, str):
                value = [line.strip() for line in value.splitlines() if line.strip()]
            profile["rubric"] = [str(v) for v in (value or []) if str(v).strip()]
        elif key == "enabled":
            profile["enabled"] = bool(value)
        elif key in ("temperature", "top_p"):
            profile[key] = _float_or(value, profile.get(key))
        else:
            profile[key] = str(value or "")
    if not str(profile.get("name") or "").strip():
        raise ExpertError("An expert needs a name")
    profile["updated_at"] = _now_iso()
    save_expert(profile)
    return profile


def delete_expert(slug: Any) -> bool:
    """Remove the whole directory (profile, corpus, index, counters) plus this
    expert's embedding collection when there is one."""
    slug = _clean_slug(slug)
    if not slug:
        return False
    directory = expert_dir(slug)
    if not os.path.isdir(directory):
        return False
    _drop_vector_collection(slug)
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        logger.warning("experts: could not delete %s: %s", directory, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# usage.json — the sidecar counters (so EXPERT.md does not churn)
# ---------------------------------------------------------------------------


def _blank_usage() -> Dict[str, Any]:
    return {"invocations": 0, "accepted": 0, "rejected": 0, "last_used": None}


def load_usage(slug: Any) -> Dict[str, Any]:
    """Counters for one expert. A corrupt sidecar is moved aside and treated
    as zero — counters are not worth failing a turn over. Never raises."""
    slug = _clean_slug(slug)
    usage = _blank_usage()
    if not slug:
        return usage
    path = usage_path(slug)
    if not os.path.isfile(path):
        return usage
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("usage.json is not an object")
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("experts: usage.json for %s unusable (%s); starting at zero", slug, exc)
        if not isinstance(exc, OSError):
            _quarantine(path)
        return usage
    for key in ("invocations", "accepted", "rejected"):
        try:
            usage[key] = max(0, int(data.get(key) or 0))
        except (TypeError, ValueError):
            usage[key] = 0
    usage["last_used"] = data.get("last_used")
    return usage


def _write_usage(slug: str, usage: Dict[str, Any]) -> bool:
    try:
        os.makedirs(expert_dir(slug), exist_ok=True)
        _atomic_write_json(usage_path(slug), usage)
        return True
    except OSError as exc:
        logger.warning("experts: could not write usage.json for %s: %s", slug, exc)
        return False


def record_invocation(slug: Any) -> Dict[str, Any]:
    """One more turn used this expert. Never raises."""
    slug = _clean_slug(slug)
    usage = load_usage(slug)
    if not slug or not os.path.isdir(expert_dir(slug)):
        return usage
    usage["invocations"] = int(usage.get("invocations") or 0) + 1
    usage["last_used"] = _now_iso()
    _write_usage(slug, usage)
    return usage


def record_feedback(slug: Any, accepted: Any = 0, rejected: Any = 0) -> Dict[str, Any]:
    """Add ``accepted`` / ``rejected`` corrections to this expert's counters.

    This is the phase-2 training signal being collected in phase 1: the Beta
    posterior :func:`suggest` samples is built from exactly these two numbers.
    Booleans work as well as counts. NEVER raises — it is called at the end of
    a review, and a dead sidecar must not cost the review.
    """
    slug = _clean_slug(slug)
    usage = load_usage(slug)
    # An unknown expert must not conjure a directory out of a stale slug.
    if not slug or not os.path.isdir(expert_dir(slug)):
        return usage
    try:
        add_ok = max(0, int(accepted or 0))
    except (TypeError, ValueError):
        add_ok = 0
    try:
        add_no = max(0, int(rejected or 0))
    except (TypeError, ValueError):
        add_no = 0
    if not add_ok and not add_no:
        return usage
    usage["accepted"] = int(usage.get("accepted") or 0) + add_ok
    usage["rejected"] = int(usage.get("rejected") or 0) + add_no
    usage["last_used"] = _now_iso()
    _write_usage(slug, usage)
    return usage


# ---------------------------------------------------------------------------
# Corpus files
# ---------------------------------------------------------------------------


def _safe_filename(name: Any) -> str:
    """A flat, traversal-proof basename for a corpus file."""
    raw = os.path.basename(str(name or "").replace("\\", "/").strip())
    raw = raw.replace("\x00", "")
    if raw in ("", ".", ".."):
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "_", raw).strip(" .")
    return cleaned[:160]


def corpus_target_path(slug: Any, filename: Any) -> str:
    """Where an uploaded file should be written, de-duplicated with ``-2``…

    The route streams into this path so a huge PDF is never held in memory.
    """
    slug = _clean_slug(slug)
    if not slug:
        raise ExpertError("no such expert")
    safe = _safe_filename(filename)
    if not safe:
        raise ExpertError("that filename cannot be stored")
    ext = os.path.splitext(safe)[1].lower()
    if ext not in CORPUS_EXTENSIONS:
        raise ExpertError(f"'{ext or safe}' is not a corpus file type "
                          f"({', '.join(CORPUS_EXTENSIONS)})")
    directory = corpus_dir(slug)
    os.makedirs(directory, exist_ok=True)
    stem = os.path.splitext(safe)[0]
    candidate = safe
    i = 2
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{stem}-{i}{ext}"
        i += 1
    return os.path.join(directory, candidate)


def corpus_files(slug: Any) -> List[Dict[str, Any]]:
    """``[{name, bytes, modified, pages, chunks, indexed_at}]``. Never raises."""
    slug = _clean_slug(slug)
    if not slug:
        return []
    directory = corpus_dir(slug)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for chunk in load_index(slug):
        by_source.setdefault(str(chunk.get("source") or ""), []).append(chunk)
    out = []
    for name in names:
        path = os.path.join(directory, name)
        if name.startswith(".") or not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        chunks = by_source.get(name) or []
        pages = sorted({c["page"] for c in chunks
                        if isinstance(c.get("page"), int)})
        out.append({
            "name": name,
            "bytes": stat.st_size,
            "modified": int(stat.st_mtime),
            "pages": (max(pages) if pages else None),
            "chunks": len(chunks),
            "indexed_at": (chunks[0].get("indexed_at") if chunks else None),
        })
    return out


def delete_corpus_file(slug: Any, filename: Any) -> bool:
    """Remove one file and (via a reindex) its chunks."""
    slug = _clean_slug(slug)
    safe = _safe_filename(filename)
    if not slug or not safe:
        return False
    path = os.path.join(corpus_dir(slug), safe)
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
    except OSError as exc:
        logger.warning("experts: could not delete %s: %s", path, exc)
        return False
    reindex(slug)
    return True


def ingest(slug: Any, path: Any) -> Dict[str, Any]:
    """Copy an existing file on disk into the expert's corpus and reindex.

    Streams in 1 MiB blocks: a 900-page PDF is read once and never held whole.
    """
    slug = _clean_slug(slug)
    if not slug or not os.path.isdir(expert_dir(slug)):
        raise ExpertError("no such expert")
    source = str(path or "")
    if not os.path.isfile(source):
        raise ExpertError(f"no such file: {source}")
    target = corpus_target_path(slug, os.path.basename(source))
    try:
        with open(source, "rb") as src, open(target, "wb") as dst:
            while True:
                block = src.read(READ_CHUNK_BYTES)
                if not block:
                    break
                dst.write(block)
    except OSError as exc:
        raise ExpertError(f"Could not copy {source}: {exc}")
    result = reindex(slug)
    result["file"] = os.path.basename(target)
    return result


# ---------------------------------------------------------------------------
# Extraction with PAGE-LEVEL provenance
# ---------------------------------------------------------------------------


def extract_pdf_pages(file_path: str) -> Tuple[List[Tuple[Optional[int], str]], bool]:
    """``([(page_number, text), …], pages_known)``.

    ``src.personal_docs.extract_pdf_text`` hands back the whole document; this
    is the per-page variant that makes a citation checkable, kept HERE rather
    than in personal_docs (that module's global index is not ours to change).
    Pages are yielded one at a time — the whole document is never concatenated.

    When pypdf is missing or the file cannot be paged, the caller gets the
    whole-document text as a single ``(None, text)`` entry and ``False``, and
    every chunk from it is stamped ``page: None`` /
    ``page_confidence: "unknown"``. A page number is never guessed.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("experts: pypdf not installed; PDF pages unknown")
        text = extract_pdf_text(file_path)
        return ([(None, text)] if text else []), False
    try:
        reader = PdfReader(file_path)
        pages: List[Tuple[Optional[int], str]] = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - one bad page, not the book
                logger.debug("experts: page %s of %s unreadable: %s", number, file_path, exc)
                text = ""
            if text.strip():
                pages.append((number, text))
        if pages:
            return pages, True
        # A readable PDF with no extractable text (a scan). There is nothing to
        # index and nothing to guess.
        return [], True
    except Exception as exc:  # noqa: BLE001
        logger.warning("experts: per-page extraction of %s failed (%s); "
                       "falling back to the whole document with no page numbers",
                       file_path, exc)
        text = extract_pdf_text(file_path)
        return ([(None, text)] if text.strip() else []), False


def _extract_units(path: str) -> Tuple[List[Tuple[Optional[int], str]], bool]:
    """``([(page|None, text)], pages_known)`` for any supported corpus file."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_pdf_pages(path)
    if ext in (".docx", ".pptx", ".xlsx", ".xls", ".epub"):
        try:
            text = extract_office_text(path)
        except Exception as exc:  # noqa: BLE001 - optional dependency
            logger.warning("experts: office extraction of %s failed: %s", path, exc)
            text = ""
        return ([(None, text)] if text.strip() else []), False
    text = read_text_file(path)
    return ([(None, text)] if text.strip() else []), False


# ---------------------------------------------------------------------------
# Chunking with line ranges
# ---------------------------------------------------------------------------


def _chunk_spans(text: str, size: int, overlap: int) -> List[Tuple[int, int]]:
    """Where ``personal_docs.split_chunks`` cuts, as (start, end) offsets into
    the ORIGINAL text — the offsets are what turn a chunk into a line range.

    Driven BY split_chunks rather than reimplementing it, so the two can never
    drift: the chunk texts come from there, and the walk only converts their
    lengths back into offsets (split_chunks strips first, so the leading
    whitespace is added back through ``base``).
    """
    chunks = split_chunks(text, size, overlap)
    if not chunks:
        return []
    stripped = str(text).strip()
    base = str(text).find(stripped)
    if base < 0:                        # cannot happen; stay defensive anyway
        base = 0
    spans: List[Tuple[int, int]] = []
    i = 0
    for chunk in chunks:
        j = i + len(chunk)
        spans.append((base + i, base + j))
        i = j - overlap if j - overlap > i else j
    return spans


def _line_index(text: str) -> List[int]:
    """Offsets of every newline, for O(log n) offset→line lookups."""
    return [m.start() for m in re.finditer("\n", text)]


def _line_of(newlines: Sequence[int], offset: int) -> int:
    return bisect.bisect_right(newlines, offset) + 1


def _terms(text: Any) -> List[str]:
    """The chunk's terms, with term frequency.

    ``personal_docs.tokenize`` owns the policy (lower-case, word charset,
    stop-words, length) and returns the SET; the frequencies come from the same
    word pattern filtered through it, so a query word repeated in a chunk still
    counts more than one that appears once.
    """
    allowed = tokenize(str(text or ""))
    if not allowed:
        return []
    return [w for w in _WORD_RE.findall(str(text or "").lower()) if w in allowed]


def _chunk_id(slug: str, source: str, page: Optional[int], ordinal: int) -> str:
    import hashlib
    key = f"{slug}|{source}|{page if page is not None else '-'}|{ordinal}"
    return "c" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _chunks_for_file(slug: str, directory: str, name: str,
                     stat: os.stat_result, now: str) -> List[Dict[str, Any]]:
    path = os.path.join(directory, name)
    units, pages_known = _extract_units(path)
    size = int(getattr(docs_config, "CHUNK_SIZE", 1000))
    overlap = int(getattr(docs_config, "CHUNK_OVERLAP", 200))
    # "exact"   — the page number came out of the PDF itself
    # "unknown" — a PDF whose pages could not be determined: page stays None
    # "none"    — a file that has no pages at all (text, markdown, docx)
    is_pdf = name.lower().endswith(".pdf")
    fallback_confidence = "unknown" if (is_pdf and not pages_known) else "none"
    out: List[Dict[str, Any]] = []
    ordinal = 0
    for page, text in units:
        newlines = _line_index(text)
        paged = isinstance(page, int)
        for start, end in _chunk_spans(text, size, overlap):
            body = text[start:end]
            if not body.strip():
                continue
            out.append({
                "id": _chunk_id(slug, name, page, ordinal),
                "source": name,
                # Never invented: a PDF whose pages could not be determined
                # gets None here and says so in page_confidence.
                "page": page if paged else None,
                "page_confidence": "exact" if paged else fallback_confidence,
                "start_line": _line_of(newlines, start),
                "end_line": _line_of(newlines, max(start, end - 1)),
                "text": body,
                "tokens": len(_terms(body)),
                "mtime": int(stat.st_mtime),
                "size": int(stat.st_size),
                "indexed_at": now,
                "ordinal": ordinal,
            })
            ordinal += 1
    return out


# ---------------------------------------------------------------------------
# index.json
# ---------------------------------------------------------------------------


def load_index(slug: Any) -> List[Dict[str, Any]]:
    """The chunk index. A corrupt file is renamed ``.corrupt`` and treated as
    empty (the next reindex rebuilds it). NEVER raises."""
    slug = _clean_slug(slug)
    if not slug:
        return []
    path = index_path(slug)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("index.json is not a list")
    except OSError as exc:
        logger.warning("experts: index.json for %s unreadable (%s); treating as empty", slug, exc)
        return []
    except (ValueError, TypeError) as exc:
        logger.warning("experts: index.json for %s corrupt (%s); renaming to .corrupt", slug, exc)
        _quarantine(path)
        return []
    return [c for c in data if isinstance(c, dict) and c.get("id") and c.get("source")]


def save_index(slug: Any, chunks: Sequence[Dict[str, Any]]) -> bool:
    slug = _clean_slug(slug)
    if not slug:
        return False
    try:
        os.makedirs(expert_dir(slug), exist_ok=True)
        _atomic_write_json(index_path(slug), list(chunks))
        return True
    except OSError as exc:
        logger.warning("experts: could not write index.json for %s: %s", slug, exc)
        return False


def indexed_at(slug: Any) -> Optional[str]:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(os.path.getmtime(index_path(slug))))
    except OSError:
        return None


def _stamp(chunk: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """``(mtime, size)`` of the file a chunk came from, or None when the
    record predates the stamp (which forces a re-chunk, never a false skip)."""
    try:
        return int(chunk["mtime"]), int(chunk["size"])
    except (KeyError, TypeError, ValueError):
        return None


def reindex(slug: Any) -> Dict[str, Any]:
    """Incremental rebuild: a file whose mtime+size are unchanged keeps its
    chunks, an edited file is re-chunked, a deleted file's chunks go.

    ``{"indexed", "skipped", "removed", "chunks", "seconds"}``. Never raises —
    an unreadable file is skipped, not fatal.
    """
    started = time.monotonic()
    slug = _clean_slug(slug)
    result = {"indexed": 0, "skipped": 0, "removed": 0, "chunks": 0, "seconds": 0.0}
    if not slug or not os.path.isdir(expert_dir(slug)):
        return result

    directory = corpus_dir(slug)
    try:
        os.makedirs(directory, exist_ok=True)
        names = sorted(n for n in os.listdir(directory)
                       if not n.startswith(".")
                       and os.path.isfile(os.path.join(directory, n))
                       and os.path.splitext(n)[1].lower() in CORPUS_EXTENSIONS)
    except OSError as exc:
        logger.warning("experts: corpus of %s unreadable (%s)", slug, exc)
        names = []

    previous = load_index(slug)
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for chunk in previous:
        by_source.setdefault(str(chunk.get("source") or ""), []).append(chunk)

    now = _now_iso()
    kept: List[Dict[str, Any]] = []
    fresh_chunks: List[Dict[str, Any]] = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            result["skipped"] += 1
            continue
        old = by_source.pop(name, None)
        # `or -1` would be wrong here: an mtime or size of 0 is a real value
        # (an epoch timestamp, an empty file) and must still count as unchanged.
        if old and _stamp(old[0]) == (int(stat.st_mtime), int(stat.st_size)):
            kept.extend(old)
            result["skipped"] += 1
            continue
        try:
            fresh = _chunks_for_file(slug, directory, name, stat, now)
        except Exception as exc:  # noqa: BLE001 - one bad file, not the corpus
            logger.warning("experts: could not index %s for %s: %s", name, slug, exc)
            result["skipped"] += 1
            continue
        kept.extend(fresh)
        fresh_chunks.extend(fresh)
        result["indexed"] += 1
        if old:
            # Before the re-add below: an edited file that still cuts into the
            # same number of chunks reuses their ids.
            _unindex_vectors(slug, [c["id"] for c in old])
    # Anything still in by_source is a file that is gone.
    for name, chunks in by_source.items():
        result["removed"] += 1
        _unindex_vectors(slug, [c["id"] for c in chunks])

    kept.sort(key=lambda c: (str(c.get("source") or ""), int(c.get("ordinal") or 0)))
    save_index(slug, kept)
    _sync_vectors(slug, kept, fresh_chunks)
    result["chunks"] = len(kept)
    result["seconds"] = round(time.monotonic() - started, 4)
    return result


# ---------------------------------------------------------------------------
# Tier 2 — this expert's own embedding collection (may simply not exist)
# ---------------------------------------------------------------------------


_VECTOR_STATE: Dict[str, Dict[str, Any]] = {}


def collection_name(slug: Any) -> str:
    return f"odysseus_expert_{_clean_slug(slug)}"


def set_vector_store(slug: Any, store: Any) -> None:
    """Install (or clear, with None) one expert's semantic lane explicitly."""
    _VECTOR_STATE[_clean_slug(slug)] = {"tried": True, "store": store}


def reset_vector_stores() -> None:
    _VECTOR_STATE.clear()


def vector_store(slug: Any) -> Any:
    """The expert's own vector collection, or None. Tried once per slug per
    process — a missing ChromaDB must not be retried on every keystroke."""
    slug = _clean_slug(slug)
    if not slug:
        return None
    state = _VECTOR_STATE.get(slug)
    if state and state.get("tried"):
        return state.get("store")
    _VECTOR_STATE[slug] = {"tried": True, "store": None}
    try:
        from src.memory_vector import MemoryVectorStore

        # Its own collection: one expert's books must never answer another
        # expert's query, and neither may the app's general memories.
        store_cls = type("_ExpertVectors", (MemoryVectorStore,),
                         {"COLLECTION_NAME": collection_name(slug)})
        store = store_cls(DATA_DIR)
        if getattr(store, "healthy", False):
            _VECTOR_STATE[slug]["store"] = store
        else:
            logger.info("experts: %s has no semantic lane, lexical only", slug)
    except Exception as exc:  # noqa: BLE001 - optional dependency, never fatal
        logger.info("experts: semantic lane for %s unavailable (%s), lexical only", slug, exc)
    return _VECTOR_STATE[slug]["store"]


def _safe_store(slug: str) -> Any:
    """The store if one can be had, else None. Reindex is an explicit action,
    so it may pay the one-off cost of trying to open the collection."""
    try:
        return vector_store(slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("experts: vector store for %s unavailable: %s", slug, exc)
        return None


def _sync_vectors(slug: str, kept: Sequence[Dict[str, Any]],
                  fresh: Sequence[Dict[str, Any]]) -> None:
    """Embed the chunks this pass produced — or the whole index when the lane
    is empty, so a ChromaDB that only came up today back-fills itself instead
    of staying blind to a corpus that was indexed before it existed."""
    store = _safe_store(slug)
    if not store:
        return
    try:
        backfill = int(store.count()) == 0
    except Exception:  # noqa: BLE001 - a store that cannot count still takes adds
        backfill = False
    for chunk in (kept if backfill else fresh):
        try:
            store.add(chunk["id"], chunk.get("text") or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("experts: vector add of %s failed: %s", chunk.get("id"), exc)


def _unindex_vectors(slug: str, ids: Iterable[str]) -> None:
    store = _safe_store(slug)
    if not store:
        return
    for chunk_id in ids:
        try:
            store.remove(chunk_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("experts: vector remove of %s failed: %s", chunk_id, exc)


def _drop_vector_collection(slug: str) -> None:
    _VECTOR_STATE.pop(slug, None)
    try:
        from src.chroma_client import get_chroma_client
        client = get_chroma_client()
        for name in (collection_name(slug),
                     collection_name(slug) + "_custom",
                     collection_name(slug) + "_fastembed"):
            try:
                client.delete_collection(name)
            except Exception:  # noqa: BLE001 - it may simply not exist
                pass
    except Exception as exc:  # noqa: BLE001 - no ChromaDB at all is fine
        logger.debug("experts: no vector collection to drop for %s (%s)", slug, exc)


# ---------------------------------------------------------------------------
# Two-tier search (frankensearch)
# ---------------------------------------------------------------------------


def bm25_scores(query: str, docs: Sequence[Tuple[str, str]]) -> Dict[str, float]:
    """BM25-lite over ``personal_docs.tokenize``, normalised to 0..1 by the
    top hit. Stdlib, instant, and it is what makes tier 1 always available."""
    query_terms = _terms(query)
    if not query_terms or not docs:
        return {}
    tokenised = [(doc_id, _terms(text)) for doc_id, text in docs]
    lengths = [len(t) for _, t in tokenised]
    total_docs = len(tokenised)
    avgdl = (sum(lengths) / total_docs) if total_docs else 0.0
    if avgdl <= 0:
        return {}
    df: Dict[str, int] = {}
    for _, terms in tokenised:
        for term in set(terms):
            df[term] = df.get(term, 0) + 1
    scores: Dict[str, float] = {}
    for doc_id, terms in tokenised:
        if not terms:
            continue
        length = len(terms)
        total = 0.0
        for term in set(query_terms):
            freq = terms.count(term)
            if not freq:
                continue
            n_q = df.get(term, 0)
            idf = math.log(1.0 + (total_docs - n_q + 0.5) / (n_q + 0.5))
            total += idf * (freq * (BM25_K1 + 1.0)) / (
                freq + BM25_K1 * (1.0 - BM25_B + BM25_B * length / avgdl))
        if total > 0:
            scores[doc_id] = total
    top = max(scores.values()) if scores else 0.0
    return {doc_id: value / top for doc_id, value in scores.items()} if top > 0 else {}


def _semantic_ranking(slug: str, query: str, k: int) -> Tuple[List[str], bool]:
    """``([chunk_id ranked best-first], available)``. Never raises: a store
    that blows up is a degradation, not an error."""
    try:
        store = vector_store(slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("experts: vector store for %s unavailable: %s", slug, exc)
        return [], False
    if not store:
        return [], False
    try:
        rows = store.search(query, k) or []
    except Exception as exc:  # noqa: BLE001
        logger.info("experts: semantic lane for %s raised (%s); serving lexical only", slug, exc)
        return [], False
    ranked: List[Tuple[float, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chunk_id = str(row.get("memory_id") or row.get("id") or "")
        if not chunk_id:
            continue
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        ranked.append((score, chunk_id))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]))
    return [chunk_id for _, chunk_id in ranked], True


def _hit(chunk: Dict[str, Any], score: float, tier: str) -> Dict[str, Any]:
    return {
        "chunk_id": str(chunk.get("id") or ""),
        "source": str(chunk.get("source") or ""),
        "page": chunk.get("page") if isinstance(chunk.get("page"), int) else None,
        "start_line": int(chunk.get("start_line") or 1),
        "end_line": int(chunk.get("end_line") or 1),
        "text": str(chunk.get("text") or ""),
        "score": round(float(score), 6),
        "tier": tier,
    }


def search(slug: Any, query: Any, k: int = DEFAULT_SEARCH_K) -> Dict[str, Any]:
    """``{"hits": [...], "tier": "lexical"|"hybrid", "degraded": bool}``.

    Tier 1 (BM25-lite) always runs. When this expert has a working embedding
    collection the two rankings are fused with Reciprocal Rank Fusion
    (``Σ 1/(60+rank)``) and the tier is ``hybrid``; when the vector store is
    missing or raises the answer is tier 1 with ``degraded: True``. NEVER an
    error — a Faustus that has downloaded nothing still searches.
    """
    slug = _clean_slug(slug)
    try:
        k = max(1, min(int(k or DEFAULT_SEARCH_K), 50))
    except (TypeError, ValueError):
        k = DEFAULT_SEARCH_K
    empty = {"hits": [], "tier": "lexical", "degraded": False}
    if not slug:
        return empty
    text = str(query or "").strip()
    chunks = load_index(slug)
    if not chunks or not text:
        # Nothing to rank, so nothing to degrade — do not wake a vector store
        # to tell the caller about an empty corpus.
        return empty

    by_id = {str(c.get("id")): c for c in chunks if c.get("id")}
    lexical = bm25_scores(text, [(cid, c.get("text") or "") for cid, c in by_id.items()])
    lexical_ranked = sorted(lexical, key=lambda cid: (-lexical[cid], cid))

    semantic_ranked, available = _semantic_ranking(slug, text, max(k * 4, 20))
    semantic_ranked = [cid for cid in semantic_ranked if cid in by_id]
    degraded = not available
    tier = "hybrid" if (available and semantic_ranked) else "lexical"

    if tier == "hybrid":
        fused: Dict[str, float] = {}
        for ranking in (lexical_ranked, semantic_ranked):
            for rank, chunk_id in enumerate(ranking, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        ordered = sorted(fused, key=lambda cid: (-fused[cid], cid))
        hits = [_hit(by_id[cid], fused[cid], tier) for cid in ordered[:k] if cid in by_id]
    else:
        hits = [_hit(by_id[cid], lexical[cid], tier) for cid in lexical_ranked[:k]]

    return {"hits": hits, "tier": tier, "degraded": degraded}


# ---------------------------------------------------------------------------
# Citations — so the user can verify against the page
# ---------------------------------------------------------------------------


def citation(slug: Any, chunk_id: Any) -> Optional[Dict[str, Any]]:
    """Resolve one chunk id back to the page (or lines) it came from.

    ``file_url`` is this module's own corpus download route
    (``/api/experts/<slug>/corpus/<file>``, routes/expert_routes.py);
    ``file_path`` is the absolute path on disk, because the point of the
    feature is that the file never left the machine.
    """
    slug = _clean_slug(slug)
    wanted = str(chunk_id or "")
    if not slug or not wanted:
        return None
    for chunk in load_index(slug):
        if str(chunk.get("id")) != wanted:
            continue
        source = str(chunk.get("source") or "")
        text = str(chunk.get("text") or "")
        return {
            "chunk_id": wanted,
            "source": source,
            "page": chunk.get("page") if isinstance(chunk.get("page"), int) else None,
            "page_confidence": str(chunk.get("page_confidence") or "none"),
            "start_line": int(chunk.get("start_line") or 1),
            "end_line": int(chunk.get("end_line") or 1),
            "excerpt": text[:MAX_EXCERPT_CHARS],
            "file_path": os.path.join(corpus_dir(slug), source),
            "file_url": f"/api/experts/{slug}/corpus/{source}",
        }
    return None


def render_page(slug: Any, source: Any, page: Any) -> Dict[str, Any]:
    """A PNG of one PDF page, when a renderer is already installed.

    Faustus ships ``pypdf`` (text only — it cannot rasterise). PyMuPDF CAN,
    and the project already lists it in requirements-optional.txt for the PDF
    viewer, so it is used WHEN PRESENT and never installed for this: no new
    dependency is added for a nicety. Otherwise this answers
    ``{"available": False, "reason": …}`` and the UI links to the file
    instead.
    """
    slug = _clean_slug(slug)
    safe = _safe_filename(source)
    unavailable = {"available": False, "png_base64": None,
                   "file_url": (f"/api/experts/{slug}/corpus/{safe}" if slug and safe else None)}
    if not slug or not safe:
        return {**unavailable, "reason": "unknown expert or file"}
    path = os.path.join(corpus_dir(slug), safe)
    if not os.path.isfile(path):
        return {**unavailable, "reason": f"no such corpus file: {safe}"}
    if not safe.lower().endswith(".pdf"):
        return {**unavailable, "reason": "only PDF pages can be rendered"}
    try:
        number = int(page)
    except (TypeError, ValueError):
        return {**unavailable, "reason": "no page number to render"}
    if number < 1:
        return {**unavailable, "reason": "no page number to render"}
    try:
        from src.pdf_runtime import load_pymupdf_for_pdf_viewer
        fitz = load_pymupdf_for_pdf_viewer()
    except Exception as exc:  # noqa: BLE001 - the documented "not installed" path
        return {**unavailable, "reason": str(exc)}
    try:
        import base64
        with fitz.open(path) as doc:
            if number > doc.page_count:
                return {**unavailable, "reason": f"page {number} is past the end of {safe}"}
            pixmap = doc.load_page(number - 1).get_pixmap(dpi=110)
            png = pixmap.tobytes("png")
        return {
            "available": True,
            "png_base64": base64.b64encode(png).decode("ascii"),
            "page": number,
            "source": safe,
            "file_url": f"/api/experts/{slug}/corpus/{safe}",
        }
    except Exception as exc:  # noqa: BLE001 - a render is never worth a 500
        logger.warning("experts: render of %s p.%s failed: %s", safe, number, exc)
        return {**unavailable, "reason": f"could not render page {number}: {exc}"}


# ---------------------------------------------------------------------------
# The context an expert contributes
# ---------------------------------------------------------------------------


def _marker_line(index: int, hit: Dict[str, Any]) -> str:
    """``[C1] book.pdf p.42`` — or line numbers when the file has no pages.
    NEVER a page number that was not extracted."""
    label = f"[C{index}] {hit.get('source') or 'corpus'}"
    page = hit.get("page")
    if isinstance(page, int):
        return f"{label} p.{page}"
    start, end = hit.get("start_line"), hit.get("end_line")
    if isinstance(start, int) and isinstance(end, int):
        return f"{label} L{start}-{end}"
    return label


def expert_block(slug: Any, query: Any = "",
                 char_budget: Optional[int] = None) -> Dict[str, Any]:
    """The deterministic block this expert contributes to a turn.

    ``{"text": str, "chunk_ids": [...], "degraded": bool}``. The text is the
    expert's instructions, its rubric, its top corpus hits — each prefixed with
    the citation marker ``[C1]``, ``[C2]``… in exactly the order of
    ``chunk_ids`` — and its own learned rules from ``memory_engine.pack(owner,
    project="expert:<slug>", query)``.

    Same profile + same corpus + same query → byte-identical output, and never
    longer than the budget. NEVER raises: it is called while the user waits.
    """
    slug = _clean_slug(slug)
    budget = context_budget() if char_budget is None else char_budget
    try:
        budget = max(0, int(budget))
    except (TypeError, ValueError):
        budget = DEFAULT_CONTEXT_CHARS
    blank = {"text": "", "chunk_ids": [], "degraded": False}
    if not slug or budget <= 0:
        return blank
    try:
        return _expert_block(slug, str(query or ""), budget)
    except Exception as exc:  # noqa: BLE001 - hot path: cost the block, not the turn
        logger.debug("experts: block for %s failed (%s); no block this turn", slug, exc)
        return blank


def _expert_block(slug: str, query: str, budget: int) -> Dict[str, Any]:
    profile = load_expert(slug)
    if not profile:
        return {"text": "", "chunk_ids": [], "degraded": False}

    head: List[str] = [f"## Expert: {profile.get('name') or slug}"]
    if profile.get("description"):
        head.append(str(profile["description"]).strip())
    if profile.get("instructions"):
        head.append("### Instructions\n" + str(profile["instructions"]).strip())
    if profile.get("rubric"):
        head.append("### Rubric\n" + "\n".join(
            f"{i + 1}. {r}" for i, r in enumerate(profile["rubric"])))
    header = "\n\n".join(head)
    # The profile is what the expert IS; it may take at most half the budget so
    # a long rubric can never crowd the corpus out entirely.
    header = _clip(header, max(120, budget // 2))

    found = search(slug, query, k=8)
    degraded = bool(found.get("degraded"))

    # Learned rules are the smallest, most volatile part: give them a quarter.
    rules = ""
    try:
        from src import memory_engine
        rules = memory_engine.pack(profile.get("owner") or None,
                                   f"expert:{slug}", query,
                                   max(200, budget // 4)) or ""
    except Exception as exc:  # noqa: BLE001 - the engine may not be usable
        logger.debug("experts: learned rules for %s unavailable: %s", slug, exc)
        rules = ""

    used = len(header)
    tail = ("\n\n### Learned rules\n" + rules.strip()) if rules.strip() else ""
    room = budget - used - len(tail)

    corpus_parts: List[str] = []
    chunk_ids: List[str] = []
    hits = list(found.get("hits") or [])
    if room > 0 and hits:
        room -= len("\n\n### Corpus\n")
        # An equal share per hit, so a long first excerpt cannot swallow the
        # rest: the block shows several citations rather than one book page.
        fair_share = min(900, max(120, room // len(hits)))
        for hit in hits:
            marker = _marker_line(len(chunk_ids) + 1, hit)
            # -3 keeps room for the marker's newline and the "\n\n" join.
            share = min(fair_share, room - len(marker) - 3)
            if share < 80:
                break
            body = _clip(str(hit.get("text") or "").strip(), share)
            if not body:
                continue
            piece = f"{marker}\n{body}"
            corpus_parts.append(piece)
            chunk_ids.append(str(hit.get("chunk_id")))
            room -= len(piece) + 2

    sections = [header]
    if corpus_parts:
        sections.append("### Corpus\n" + "\n\n".join(corpus_parts))
    text = "\n\n".join(s for s in sections if s)
    if rules.strip():
        text = text + "\n\n### Learned rules\n" + rules.strip()
    if len(text) > budget:
        text = text[:budget].rstrip()
    return {"text": text, "chunk_ids": chunk_ids, "degraded": degraded}


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ""
    return text[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Selection between experts — Thompson sampling (meta_skill)
# ---------------------------------------------------------------------------


def suggest(query: Any = "", owner: Optional[str] = None, k: int = 2,
            *, seed: Optional[int] = None) -> List[Dict[str, Any]]:
    """Which experts to OFFER for this query. It suggests, it never imposes.

    One Beta(1+accepted, 1+rejected) sample per enabled expert, ranked. A
    never-used expert has a flat Beta(1,1), so it is always reachable — there
    is no starvation, which is the whole point of sampling instead of taking
    the running mean. ``seed`` makes it deterministic for tests and replay.
    """
    try:
        k = max(1, min(int(k or 2), 20))
    except (TypeError, ValueError):
        k = 2
    rng = random.Random(seed)
    query_terms = set(_terms(query))
    rows: List[Dict[str, Any]] = []
    for profile in list_experts(owner):
        if not profile.get("enabled", True):
            continue
        slug = profile["slug"]
        usage = load_usage(slug)
        accepted = int(usage.get("accepted") or 0)
        rejected = int(usage.get("rejected") or 0)
        try:
            sample = rng.betavariate(1.0 + accepted, 1.0 + rejected)
        except (ValueError, ZeroDivisionError):  # pragma: no cover - a>0, b>0 always
            sample = 0.5
        haystack = set(_terms(" ".join([
            str(profile.get("name") or ""), str(profile.get("description") or ""),
            " ".join(profile.get("rubric") or []),
        ])))
        relevance = (len(query_terms & haystack) / len(query_terms)) if query_terms else 0.0
        rows.append({
            "slug": slug,
            "name": profile.get("name") or slug,
            "description": profile.get("description") or "",
            "score": round(sample, 6),
            "relevance": round(relevance, 4),
            "accepted": accepted,
            "rejected": rejected,
            "invocations": int(usage.get("invocations") or 0),
        })
    # Ranked by the sample, as the report specifies; the query only breaks the
    # (vanishingly rare) tie, so the ordering stays reproducible under a seed.
    rows.sort(key=lambda r: (-r["score"], -r["relevance"], r["slug"]))
    return rows[:k]


# ---------------------------------------------------------------------------
# Payloads for the API
# ---------------------------------------------------------------------------


def summary(slug: Any) -> Optional[Dict[str, Any]]:
    """One row for the list endpoint. Never raises."""
    profile = load_expert(slug)
    if not profile:
        return None
    usage = load_usage(profile["slug"])
    chunks = load_index(profile["slug"])
    return {
        "slug": profile["slug"],
        "name": profile["name"],
        "description": profile["description"],
        "model": profile["model"],
        "enabled": profile["enabled"],
        "owner": profile["owner"],
        "corpus_files": len(corpus_files(profile["slug"])),
        "chunks": len(chunks),
        "indexed_at": indexed_at(profile["slug"]),
        "invocations": usage["invocations"],
        "accepted": usage["accepted"],
        "rejected": usage["rejected"],
        "updated_at": profile["updated_at"],
    }


def list_payload(owner: Optional[str] = None) -> Dict[str, Any]:
    rows = []
    for profile in list_experts(owner):
        row = summary(profile["slug"])
        if row:
            rows.append(row)
    return {"experts": rows, "enabled": experts_enabled(),
            "context_chars": context_budget()}


def detail_payload(slug: Any) -> Optional[Dict[str, Any]]:
    profile = load_expert(slug)
    if not profile:
        return None
    usage = load_usage(profile["slug"])
    return {
        "expert": profile,
        "usage": usage,
        "files": corpus_files(profile["slug"]),
        "chunks": len(load_index(profile["slug"])),
        "indexed_at": indexed_at(profile["slug"]),
        "collection": collection_name(profile["slug"]),
    }
